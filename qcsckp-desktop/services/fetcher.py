"""
千川投放数据抓取模块
使用 Playwright 拦截特定 API 请求并获取投放数据
"""
import random
import asyncio
import datetime
import json
import platform
import time
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright, Page, Browser, BrowserContext, Response

from config import (
    API_BASE_URL,
    PMC_AD_DETAIL_BASIC_MAX_ROWS,
    PMC_AD_DETAIL_BASIC_PATH,
    PMC_CLOUD_BACKUP_MAX_ROWS,
    PMC_CLOUD_BACKUP_PATH,
)
from utils.clean_promotion import clean_pmc_promotion_data, clean_pmc_roi2_assist_task_data
from utils.common import require_executable_path, build_qianchuan_url
from services.control_panel_config import load_scrape_service_config
from utils.log import logger


class GlobalAuthExpiredError(Exception):
    """千川页面出现「全域投放授权已失效」类弹窗，应终止抓取。"""


# 全域投放授权失效弹窗标题/正文片段（与页面文案一致即可匹配）
_AUTH_EXPIRED_TEXT_SNIPPETS = (
    "以下全域投放授权已失效",
    "全域投放授权已失效",
)
# 须与上述文案同时出现（AND），避免页面其它区域单独命中片段导致误判
_AUTH_EXPIRED_MODAL_CONFIRM_TEXT = "我知道了"

# 需要拦截的 API URL 前缀（只保留素材列表API）
API_PREFIXES = (
    "https://qianchuan.jinritemai.com/ad/api/pmc/v1/uni-promotion/material/list-required",
)

# 调控任务列表（素材追投 Tab）：ad/list-required
AD_ASSIST_LIST_REQUIRED_PREFIX = (
    "https://qianchuan.jinritemai.com/ad/api/pmc/v1/uni-promotion/ad/list-required"
)

# 广告详情 API：用于判断计划是否「投放中」，非投放中则本轮直接结束
AD_DETAIL_BASIC_PREFIX = (
    "https://qianchuan.jinritemai.com/ad/api/creation/v1/ad/ad-detail-basic"
)
# 等待 ad-detail-basic 返回「投放中」的最长时间（秒），超时则本轮放弃
AD_DETAIL_GATE_TIMEOUT_SEC = 10



class QianChuanFetcher:
    """千川投放数据抓取器"""

    def __init__(self, headless: bool = True, storage_state: str = None):
        """
        初始化抓取器

        Args:
            headless: 是否使用无头模式运行浏览器
            storage_state: 登录状态文件路径（JSON格式）
        """
        self.headless = headless
        self.storage_state = storage_state
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.playwright = None  # 保存 playwright 实例

        # 存储拦截到的数据
        self._material_data_dict: dict = {}  # 按 materialId 去重存储: {materialId: row_data}
        self._material_total_count: int = 0  # 总数据条数
        self._material_current_count: int = 0  # 当前已获取条数
        self._is_collecting: bool = False  # 是否正在收集翻页数据
        self._last_saved_count: int = 0  # 上一次入库后的数据条数

        # 当前广告主ID、广告ID（来自页面 URL，用于匹配 ad-detail-basic）
        self._current_aadvid: Optional[str] = None
        self._current_adid: Optional[str] = None

        # 投放中门控：仅当 ad-detail-basic 判定为投放中后才继续抓素材
        self._ad_detail_gate_queue: Optional[asyncio.Queue] = None
        self._ad_detail_gate_active: bool = False
        # fetch() 门控阶段写入 ad-detail-basic 表时使用（与当前页 URL 的 aavid 一致）
        self._sqlite_store: Optional[Any] = None

        # 存储请求的时间范围（用于验证）
        self._material_start_time: Optional[str] = None  # 开始时间
        self._material_end_time: Optional[str] = None  # 结束时间

        # 本轮抓取开始时由 run_services 快照传入；仅三者齐全时才在入库后同步飞书
        self._feishu_app_token: Optional[str] = None
        self._feishu_personal_base_token: Optional[str] = None
        self._feishu_table_id: Optional[str] = None
        # each_crawl：每批入库后推送；hourly_latest：仅整点由 run_services 从库汇总推送
        self._feishu_push_mode: str = "each_crawl"
        # 云端备份：与启动服务时账号校验使用同一组账号密码（内存，不写盘）
        self._cloud_backup_username: Optional[str] = None
        self._cloud_backup_password: str = ""
        # 广告基础信息：须在素材备份成功后再 POST（见 API 说明）；门控写入本地后暂存，待素材云端成功后再刷
        self._pending_ad_detail_basic_cloud_row: Optional[Dict[str, Any]] = None

        # 调控任务（素材追投列表）：同轮次在素材抓取之后；不入库云端
        self._assist_task_ids: Dict[str, bool] = {}
        self._assist_total_count: int = 0
        self._assist_current_count: int = 0
        self._is_assist_collecting: bool = False
        self._assist_fetch_db: Optional[Any] = None
    async def _init_browser(self):
        """初始化浏览器"""
        self.playwright = await async_playwright().start()

        # 获取浏览器路径（服务控制页可配置；空或无效文件则 require_executable_path 回退 Edge/Chrome）
        scrape_cfg = load_scrape_service_config()
        raw_exe = (scrape_cfg.get("browser_executable_path") or "").strip()
        browser_path = require_executable_path(raw_exe if raw_exe else None)

        # 使用反检测配置启动浏览器
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            chromium_sandbox=False,
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-geolocation",
                "--deny-permission-prompts",
                "--disable-blink-features=AutomationControlled"
            ],
            executable_path=browser_path
        )
        # 按 headless 状态和操作系统区分 user agent
        sys_platform = platform.system().lower()
        if "darwin" in sys_platform or "mac" in sys_platform:
            # macOS UA
            user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        else:
            # 默认 win UA
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        self.context = await self.browser.new_context(
            user_agent=user_agent,
            viewport={'width': 1280, 'height': 720},
            storage_state=self.storage_state
        )
        self.page = await self.context.new_page()

    def _is_target_api(self, url: str) -> bool:
        """检查 URL 是否为目标 API 并包含有效的参数"""
        if not any(url.startswith(prefix) for prefix in API_PREFIXES):
            return False

        # 解析 URL 参数
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)

        # 检查是否有 aavid 参数
        aavid_list = query_params.get("aavid", [])
        if not aavid_list:
            return False

        # 验证 aavid 是否匹配当前目标
        if self._current_aadvid and aavid_list[0] != self._current_aadvid:
            return False

        # 验证 reqFrom 参数
        req_from = query_params.get("reqFrom", [])
        if not req_from or req_from[0] != "uni-prom-creative-tab-list":
            return False

        return True

    def _is_target_assist_api(self, url: str) -> bool:
        """调控任务列表 ad/list-required：需 aavid 与当前页一致。"""
        if not url.startswith(AD_ASSIST_LIST_REQUIRED_PREFIX):
            return False
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        aavid_list = query_params.get("aavid", [])
        if not aavid_list:
            return False
        if self._current_aadvid and aavid_list[0] != self._current_aadvid:
            return False
        return True

    def _is_ad_detail_gate_url(self, url: str) -> bool:
        """是否为当前账号/计划对应的 ad-detail-basic 请求（与页面 URL 中的 adId、aavid 一致）。"""
        if not url.startswith(AD_DETAIL_BASIC_PREFIX):
            return False
        if not self._current_aadvid or not self._current_adid:
            return False
        parsed = urlparse(url)
        q = parse_qs(parsed.query)
        adid = (q.get("adid") or q.get("adId") or [None])[0]
        aavid = (q.get("aavid") or q.get("aadvid") or [None])[0]
        if not adid or not aavid:
            return False
        return str(adid) == str(self._current_adid) and str(aavid) == str(self._current_aadvid)

    @staticmethod
    def _ad_detail_is_delivering(detail: Optional[dict]) -> bool:
        """adDetailInfo：adDeliveryName 为「投放中」且 adDeliveryType 为 0 视为投放中。"""
        if not detail:
            return False
        name = detail.get("adDeliveryName")
        dtype = detail.get("adDeliveryType")
        try:
            di = int(dtype) if dtype is not None else -1
        except (TypeError, ValueError):
            di = -1
        return name == "投放中" or di == 0

    @staticmethod
    def _pick_user_info_for_ad_detail(detail: dict, user_map: Any) -> Dict[str, Any]:
        """从 userInfoMap 取一条：优先与 creativeSetting.iesCoreUserId 匹配，否则取第一条。"""
        if not isinstance(user_map, dict) or not user_map:
            return {}
        ies = (detail.get("creativeSetting") or {}).get("iesCoreUserId")
        if ies is not None:
            key = str(ies)
            if key in user_map and isinstance(user_map[key], dict):
                return user_map[key]
        first = next(iter(user_map.values()), None)
        return first if isinstance(first, dict) else {}

    def _build_pmc_ad_detail_basic_row(self, payload: dict, detail: dict) -> Optional[Dict[str, Any]]:
        """ad-detail-basic 成功响应 → 本地表一行（不含主键 id）。"""
        aadvid = self._current_aadvid
        if not aadvid:
            return None
        ad_id = detail.get("id")
        if ad_id is None or str(ad_id).strip() == "":
            return None
        user_map = (payload.get("data") or {}).get("userInfoMap")
        uinfo = self._pick_user_info_for_ad_detail(detail, user_map)
        roi = detail.get("ecpRoi2Goal")
        try:
            ecp = float(roi) if roi is not None and roi != "" else None
        except (TypeError, ValueError):
            ecp = None
        ct = detail.get("creativeType")
        try:
            creative_type = int(ct) if ct is not None else None
        except (TypeError, ValueError):
            creative_type = None
        now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _s(v: Any) -> Optional[str]:
            if v is None:
                return None
            return str(v)

        return {
            "aadvid": str(aadvid).strip(),
            "ad_id": str(ad_id).strip(),
            "budget": _s(detail.get("budget")),
            "audience_coverage_count": _s(detail.get("audienceCoverageCount")),
            "compensation_convert": _s(detail.get("compensationConvert")),
            "ecp_roi2_goal": ecp,
            "creative_type": creative_type,
            "user_info_id": _s(uinfo.get("id")),
            "user_info_name": _s(uinfo.get("name")),
            "user_info_unique_id": _s(uinfo.get("uniqueId")),
            "updated_at": now_ts,
        }

    async def _on_response_ad_detail_gate(self, response: Response):
        """拦截 ad-detail-basic，将是否投放中写入队列供门控等待。"""
        if not self._ad_detail_gate_active or not self._ad_detail_gate_queue:
            return
        url = response.url
        if not self._is_ad_detail_gate_url(url):
            return
        try:
            data = await response.json()
        except Exception:
            return
        sc = data.get("status_code")
        if sc is not None and sc != 0:
            # 业务失败时不作为门控依据（避免误判为未投放中）
            return
        detail = (data.get("data") or {}).get("adDetailInfo")
        if not isinstance(detail, dict):
            return
        ok = self._ad_detail_is_delivering(detail)
        name = detail.get("adDeliveryName")
        dtype = detail.get("adDeliveryType")
        try:
            self._ad_detail_gate_queue.put_nowait((ok, name, dtype))
        except Exception:
            pass

        if ok and self._sqlite_store:
            row = self._build_pmc_ad_detail_basic_row(data, detail)
            if row:
                try:
                    self._sqlite_store.insert_or_update(
                        table="pmc_ad_detail_basic",
                        data=row,
                        unique_fields=["aadvid"],
                        update_fields=[
                            "ad_id",
                            "budget",
                            "audience_coverage_count",
                            "compensation_convert",
                            "ecp_roi2_goal",
                            "creative_type",
                            "user_info_id",
                            "user_info_name",
                            "user_info_unique_id",
                            "updated_at",
                        ],
                    )
                    logger.info(
                        f"[数据库] ad-detail-basic 已写入/更新 aadvid={row.get('aadvid')} ad_id={row.get('ad_id')}"
                    )
                    if (
                        self._cloud_backup_username
                        and self._cloud_backup_password
                    ):
                        self._pending_ad_detail_basic_cloud_row = dict(row)
                except Exception as e:
                    logger.warning(f"[数据库] ad-detail-basic 写入失败: {e}")

    async def _wait_for_ad_delivery_gate(self) -> bool:
        """
        等待 ad-detail-basic 响应：仅「投放中」返回 True 继续抓取；
        若先收到非投放中则 False；若 AD_DETAIL_GATE_TIMEOUT_SEC 内无有效投放中则 False。
        若 URL 未带 adId/aavid 则不做门控，返回 True。
        """
        if not self._current_aadvid or not self._current_adid:
            logger.info("[抓取] URL 未包含 adId 与 aavid，跳过投放中门控")
            return True
        if not self._ad_detail_gate_queue:
            return True

        logger.info(
            f"[抓取] 等待 ad-detail-basic 投放中（adId={self._current_adid}, "
            f"aavid={self._current_aadvid}，最长 {AD_DETAIL_GATE_TIMEOUT_SEC}s）..."
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + AD_DETAIL_GATE_TIMEOUT_SEC

        while True:
            await self._raise_if_global_auth_expired()
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    f"[抓取] {AD_DETAIL_GATE_TIMEOUT_SEC}s 内未拦截到投放中 ad-detail-basic，结束本轮"
                )
                return False
            try:
                ok, name, dtype = await asyncio.wait_for(
                    self._ad_detail_gate_queue.get(),
                    timeout=min(remaining, 0.25),
                )
            except asyncio.TimeoutError:
                continue
            if ok:
                logger.info("[抓取] 已确认投放中，继续抓取素材列表")
                return True
            logger.warning(
                f"[抓取] 当前非投放中（adDeliveryName={name!r}, adDeliveryType={dtype}），结束本轮"
            )
            return False

    async def _detect_global_auth_expired_modal(self) -> bool:
        """
        检测「全域投放授权已失效」弹窗是否可见（投放管理页常见阻断弹窗）。
        使用文案片段定位；须同时出现「我知道了」按钮/文案（AND），避免单独命中正文片段误判。
        """
        page = self.page
        if not page:
            return False
        snippet_hit = False
        for snippet in _AUTH_EXPIRED_TEXT_SNIPPETS:
            try:
                loc = page.get_by_text(snippet, exact=False)
                n = await loc.count()
                if n == 0:
                    continue
                first = loc.first
                try:
                    if await first.is_visible(timeout=800):
                        snippet_hit = True
                        break
                except Exception:
                    continue
            except Exception:
                continue
        if not snippet_hit:
            return False
        try:
            confirm = page.get_by_text(_AUTH_EXPIRED_MODAL_CONFIRM_TEXT, exact=False)
            if await confirm.count() == 0:
                return False
            return await confirm.first.is_visible(timeout=800)
        except Exception:
            return False

    async def _raise_if_global_auth_expired(self) -> None:
        """若检测到授权失效弹窗则记录日志并抛出 GlobalAuthExpiredError。"""
        if await self._detect_global_auth_expired_modal():
            logger.error("[抓取] 检测到「全域投放授权已失效」弹窗，终止本轮抓取")
            raise GlobalAuthExpiredError("全域投放授权已失效")

    async def _apply_promotion_detail_column_preset(self) -> None:
        """
        投放详情页：打开自定义列 → 选「净成交ROI目标」→ 自定义勾选四项指标 → 确定。
        与 dev_files/qianchuan_login_test.py 中步骤一致；失败仅打日志，不中断抓取。
        """
        page = self.page
        if not page:
            return
        try:
            tr = await page.query_selector('div[class*="oc-table-wrapper"] >> table[class*="ovui-table"] >> tr[class*="ovui-tr"]')
            if tr:
                tr_text = await tr.inner_text()
                required_labels = {"整体消耗", "净成交金额", "1小时内退款率", "整体支付ROI", "整体成交金额", "净成交金额结算率", "净成交订单数", "整体展现次数", "整体点击次数", "整体点击率", "整体转化率", "整体成交订单数", "净成交ROI"}
           
                if all(label in tr_text for label in required_labels):
                    logger.info("[抓取] 已完成投放详情自定义列预设")
                    return

            def hover_and_click(locator, click_timeout=20000, hover_timeout=5000):
                """Try to hover, then click the element, if fail just skip."""
                async def try_hover_and_click():
                    try:
                        if await locator.count() == 0:
                            return False
                        elm = locator.first
                        if not await elm.is_visible(timeout=hover_timeout):
                            return False
                        await elm.hover(timeout=hover_timeout)
                        await asyncio.sleep(0.2)
                        await elm.click(timeout=click_timeout)
                        return True
                    except Exception:
                        return False
                return try_hover_and_click()

            # 1. 首次点击按钮
            await hover_and_click(
                page.locator('div[class*="oc-promotion-custom-column"] >> div[class*="oc-button-wrap"]')
            )
            await asyncio.sleep(random.uniform(0.5, 1.5))

            # 2. 点击净成交ROI目标
            await hover_and_click(
                page.locator('div[class*="system-template-item"] >> div[class*="oc-popover"]:has-text("净成交ROI目标")')
            )
            await asyncio.sleep(random.uniform(1.5, 3.0))

            # 3. 再次点击按钮
            await hover_and_click(
                page.locator('div[class*="oc-promotion-custom-column"] >> div[class*="oc-button-wrap"]')
            )
            await asyncio.sleep(random.uniform(0.5, 1.5))

            # 4. 点击「自定义」
            await hover_and_click(
                page.locator('div[class*="oc-template-list-custom-btn"] >> button[class*="ovui-button--text"]:has-text("自定义")')
            )
            await asyncio.sleep(random.uniform(1, 1.5))

            # 5. 勾选每个指标
            # 获取已勾选的标签文本，避免重复点击已勾选项
            selected_text = await page.locator('div[class*="selected-item-list"]').first.inner_text()
            for label in (
                "整体消耗", "净成交金额", "1小时内退款率", "整体支付ROI", "整体成交金额",
                "净成交金额结算率", "净成交订单数", "整体展现次数", "整体点击次数",
                "整体点击率", "整体转化率", "整体成交订单数", "净成交ROI"
            ):
                if label in selected_text:
                    continue  # 跳过已经勾选的
                await hover_and_click(
                    page.locator(
                        f'div[class*="metric-select-area"] >> span[class*="oc-typography-value-int"]:has-text("{label}")'
                    ),
                    click_timeout=15000
                )
                await asyncio.sleep(random.uniform(0.5, 2.0))
         

            # 6. 点击确定
            await hover_and_click(
                page.locator('div[class*="oc-button-wrap"] >> button[class*="ovui-button"]:has-text("确定")')
            )
            await asyncio.sleep(random.uniform(1, 2.0))
            logger.info("[抓取] 已完成投放详情自定义列预设")
        except Exception as e:
            logger.warning(f"[抓取] 投放详情自定义列预设失败（将仍继续抓取）: {e}")

    async def _switch_to_video_tab(self):
        """切换到视频选项卡"""
        try:
            sucai_tab = await self.page.query_selector('div[class*="ovui-tabs__nav-list"] >> div[class*="oc-new-badge"]:has-text("素材")')
            if sucai_tab:
                await sucai_tab.click()
                await asyncio.sleep(random.uniform(0.5, 1.5))

            video_tab = self.page.locator('div[class*="oc-radio-group-button"] >> div[class*="ovui-radio-item"]:has-text("视频")')
            if await video_tab.count() > 0:
                await video_tab.last.hover()
                await video_tab.last.click()
                logger.info("[抓取] 已点击视频选项卡")
                await asyncio.sleep(3)  # 等待3秒
            else:
                logger.warning("[抓取] 未找到视频选项卡")
        except Exception as e:
            logger.warning(f"[抓取] 切换视频选项卡失败: {e}")

    async def _switch_to_100_per_page(self):
        """切换到每页100条"""
        try:
            # 先切换到第一页
            await self._go_to_first_page()
            # 尝试查找分页选择器
            # 先找到分页容器中的下拉框
            select_locator = self.page.locator('div[class*="oc-table-pagination-item"] >> div[class*="ovui-page-select"]')
            if await select_locator.count() > 0:
                # hover 并点击打开下拉框
                await select_locator.first.hover()
                await select_locator.first.click()
                await asyncio.sleep(random.uniform(0.3, 0.5))

                # 查找100选项
                option_100 = self.page.locator('div[class*="ovui-select__options"] >> div[class*="ovui-option__content"]:has-text("100")')
                if await option_100.count() > 0:
                    await option_100.first.click()
                    logger.info("[抓取] 已切换到每页100条")
                    await asyncio.sleep(random.uniform(2.5, 3))
                    return

                # 备选选择器
                option_100_alt = self.page.locator('div[class*="ovui-select__options"] >> div[class*="ovui-option--single"]:has-text("100")')
                if await option_100_alt.count() > 0:
                    await option_100_alt.first.click()
                    logger.info("[抓取] 已切换到每页100条")
                    await asyncio.sleep(random.uniform(2.5, 3))
                    return

                # 如果没找到100，关闭下拉框
                logger.warning("[抓取] 未找到每页100条选项")
            else:
                logger.warning("[抓取] 未找到分页下拉框，可能已设置为100条或其他值")
        except Exception as e:
            logger.warning(f"[抓取] 切换每页条数失败: {e}")

    async def _switch_to_regulation_material_chase_tabs(self) -> None:
        """投放详情内：主 Tab「调控」→ 子「素材追投」（与素材 Tab 切换不同）。"""
        page = self.page
        if not page:
            return
        try:
            sucai_tab = await self.page.query_selector('div[class*="ovui-tabs__nav-list"] >> div[class*="oc-new-badge"]:has-text("调控")')
            if sucai_tab:
                await sucai_tab.click()
                await asyncio.sleep(random.uniform(0.5, 1.5))
                logger.info("[调控任务] 已点击「素材追投」")
            else:
                logger.warning("[调控任务] 未找到主 Tab「调控」")
                return

            video_tab = self.page.locator('div[class*="oc-radio-group-button"] >> div[class*="ovui-radio-item"]:has-text("素材追投")')
            if await video_tab.count() > 0:
                await video_tab.last.hover()
                await video_tab.last.click()
                logger.info("[抓取] 已点击素材追投选项卡")
                await asyncio.sleep(random.uniform(5, 7))  # 等待3秒
            else:
                logger.warning("[调控任务] 未找到「素材追投」子 Tab")
        except Exception as e:
            logger.warning(f"[调控任务] Tab 切换失败: {e}")

    async def _go_to_first_page(self) -> None:
        """切换到第一页"""
        try:
            first_button = self.page.locator(
                'div[class*="oc-table-pagination-item"] >> li[class*="ovui-page-turner__item"]:has-text("1")'
            )
            if await first_button.count() > 0:
                await first_button.first.hover()
                await first_button.first.click()
                await asyncio.sleep(random.uniform(0.5, 1.0))
            else:
                logger.warning("[抓取] 未找到第一页按钮")
        except Exception as e:
            logger.warning(f"[抓取] 切换到第一页失败: {e}")

    async def _go_to_next_page(self) -> bool:
        """
        点击下一页按钮

        Returns:
            True 表示成功翻页，False 表示已经是最后一页或翻页失败
        """
        try:
            # 先检查是否有禁用状态的下一页按钮（已到达最后一页）
            disabled_button = self.page.locator(
                'div[class*="oc-table-pagination-item"] >> li[class*="ovui-page-turner__item--disabled"] >> div[class*="ovui-page-turner__next-icon"]'
            )

            if await disabled_button.count() > 0:
                logger.info("[抓取] 已到达最后一页")
                return False

            # 查找下一页按钮
            next_button = self.page.locator(
                'div[class*="oc-table-pagination-item"] >> li[class*="ovui-page-turner__item"] >> div[class*="ovui-page-turner__next-icon"]'
            )

            if await next_button.count() > 0:
                # hover 并点击
                await next_button.first.hover()
                await next_button.first.click()

                # 停顿3秒等待页面加载
                await asyncio.sleep(random.uniform(2.0, 3.0))
                await self._raise_if_global_auth_expired()
                logger.info("[抓取] 已翻页")
                return True
            else:
                logger.warning("[抓取] 未找到下一页按钮")
                return False
        except Exception as e:
            logger.warning(f"[抓取] 翻页失败: {e}")
            return False

    async def _on_request(self, request):
        """拦截请求并获取 payload"""
        url = request.url

        if not self._is_target_api(url):
            return

        # 获取 POST 请求的 payload，提取时间范围
        try:
            post_data = request.post_data
            if post_data:
                payload = json.loads(post_data)
                # 提取时间范围
                self._material_start_time = payload.get("StartTime")
                self._material_end_time = payload.get("EndTime")
                logger.info(f"[请求] 时间范围: {self._material_start_time} ~ {self._material_end_time}")

        except Exception as e:
            logger.warning(f"[警告] 解析请求 payload 失败: {url}, 错误: {e}")

    async def _on_response(self, response: Response):
        """拦截响应并处理"""
        url = response.url

        if not self._is_target_api(url):
            return

        try:
            # 尝试解析 JSON 响应
            data = await response.json()

            # 素材列表 API - 处理分页数据
            await self._handle_material_response(data, url)

        except Exception as e:
            logger.warning(f"[警告] 解析响应失败: {url}, 错误: {e}")

    def _detach_fetch_listeners(self) -> None:
        """卸下本抓取器注册的 response/request 监听，避免多轮 fetch 重复 page.on 叠加。"""
        page = self.page
        if not page:
            return
        try:
            page.remove_listener("response", self._on_response)
        except Exception:
            pass
        try:
            page.remove_listener("request", self._on_request)
        except Exception:
            pass

    def _attach_fetch_listeners(self) -> None:
        if not self.page:
            return
        self.page.on("response", self._on_response)
        self.page.on("request", self._on_request)

    def _detach_assist_listeners(self) -> None:
        page = self.page
        if not page:
            return
        try:
            page.remove_listener("response", self._on_response_assist)
        except Exception:
            pass

    def _attach_assist_listeners(self) -> None:
        if not self.page:
            return
        self.page.on("response", self._on_response_assist)

    def _reset_assist_fetch_state(self) -> None:
        self._assist_task_ids = {}
        self._assist_total_count = 0
        self._assist_current_count = 0
        self._is_assist_collecting = False

    async def _on_response_assist(self, response: Response):
        url = response.url
        if not self._is_target_assist_api(url):
            return
        try:
            data = await response.json()
        except Exception as e:
            logger.warning(f"[调控任务] 解析响应失败: {url}, {e}")
            return
        await self._handle_assist_response(data, url)

    async def _handle_assist_response(self, data: dict, url: str):
        """处理 ad/list-required 响应：更新进度并 upsert 本地 pmc_roi2_assist_task。"""
        if data.get("status_code") != 0:
            logger.warning(f"[调控任务] API 业务异常: {data.get('message')}")
            return

        d = data.get("data") or {}
        ad_infos = d.get("adInfos") or []
        if not isinstance(ad_infos, list):
            ad_infos = []

        # 总条数 data.pagination.totalNum
        total_count = 0
        pagination = d.get("pagination")
        if isinstance(pagination, dict):
            tn = pagination.get("totalNum")
            if tn is not None and str(tn).strip() != "":
                try:
                    total_count = int(tn)
                except (TypeError, ValueError):
                    total_count = 0
        if total_count <= 0:
            total_raw = d.get("totalCount")
            if total_raw is None:
                total_raw = d.get("total")
            try:
                total_count = int(total_raw) if total_raw is not None else 0
            except (TypeError, ValueError):
                total_count = 0

        for ad in ad_infos:
            if not isinstance(ad, dict):
                continue
            tid = ad.get("id")
            if tid is None:
                continue
            ts = str(tid).strip()
            if ts:
                self._assist_task_ids[ts] = True

        if not self._is_assist_collecting:
            if total_count <= 0 and ad_infos:
                total_count = len(ad_infos)
            self._assist_total_count = total_count
            self._is_assist_collecting = True
            logger.info(
                f"[调控任务] 第1页: 本页 {len(ad_infos)} 条，"
                f"累计唯一 {len(self._assist_task_ids)}/{self._assist_total_count or '?'}"
            )
        else:
            logger.info(
                f"[调控任务] 翻页: 本页 {len(ad_infos)} 条，"
                f"累计唯一 {len(self._assist_task_ids)}/{self._assist_total_count or '?'}"
            )

        self._assist_current_count = len(self._assist_task_ids)
        await self._persist_assist_api_response_to_db(data)

    async def _persist_assist_api_response_to_db(self, raw_top: dict) -> None:
        """单条 list-required 完整 JSON → clean → upsert（无云端备份）。"""
        db = self._assist_fetch_db
        aadvid = self._current_aadvid
        ad_id = self._current_adid
        if not db or not aadvid or not ad_id:
            return
        try:
            rows = clean_pmc_roi2_assist_task_data(
                raw_top, str(aadvid).strip(), str(ad_id).strip()
            )
        except Exception as e:
            logger.warning(f"[调控任务] 清洗失败: {e}")
            return
        if not rows:
            return

        def _run() -> None:
            for row in rows:
                db.insert_or_update(
                    table="pmc_roi2_assist_task",
                    data=dict(row),
                    unique_fields=["assist_task_id"],
                    update_fields=None,
                )

        await asyncio.to_thread(_run)
        logger.info(f"[数据库·调控任务] 本页已写入/更新 {len(rows)} 条")

    async def _handle_material_response(self, data: dict, url: str):
        """处理素材列表 API 响应（按 materialId 去重）"""
        # 检查响应是否有效
        if data.get("status_code") != 0:
            logger.warning(f"[警告] 素材API响应异常: {data.get('message')}")
            return

        stats_data = data.get("data", {}).get("statsData", {})
        rows = stats_data.get("rows", [])
        total_count = int(stats_data.get("totalCount", "0"))

        # 按 materialId 去重存储
        new_count = 0
        for row in rows:
            # 提取 materialId
            dims = row.get("dimensions", {})
            material_id = dims.get("materialId", {}).get("value")

            # 跳过聚合汇总行（materialId="-2"）
            if not material_id:
                continue

            # 如果 materialId 不存在，则添加
            if material_id not in self._material_data_dict:
                self._material_data_dict[material_id] = row
                new_count += 1

        # 第一次获取数据时，记录总数
        if not self._is_collecting:
            self._material_total_count = total_count
            self._material_current_count = new_count
            self._is_collecting = True
            logger.info(f"[抓取] 第1页: 获取 {new_count} 条新数据，总计 {total_count} 条")
        else:
            # 翻页数据
            self._material_current_count += new_count
            current_page = len(self._material_data_dict) // 100 + 1  # 假设每页100条
            logger.info(f"[抓取] 第{current_page}页: 获取 {new_count} 条新数据，累计 {self._material_current_count}/{total_count} 条（去重后）")

    async def _save_to_database(self, db):
        """每页数据抓取完成后立即清洗并入库（只入库新增数据）"""
        if not db or not self._material_data_dict:
            return

        try:
            # 获取当前总数
            current_count = len(self._material_data_dict)

            # 如果没有新数据，则不入库
            if current_count <= self._last_saved_count:
                return

            # 只处理新增加的数据
            all_data = clean_pmc_promotion_data(self._material_data_dict)
            new_data = all_data[self._last_saved_count:]  # 切片获取新增数据

            if not new_data:
                return

            # 添加 aadvid、统计日期（created_at 由 SQLite DEFAULT 写入，不手动插入）
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for item in new_data:
                item["aadvid"] = self._current_aadvid
                item["stat_date"] = today

            # 直接插入（不更新，允许重复数据）
            for item in new_data:
                db.insert(
                    table="pmc_promotion_material",
                    data=item
                )

            # 更新已保存计数
            self._last_saved_count = current_count

            logger.info(f"[数据库] 已插入 {len(new_data)} 条新数据")
            # 顺序：本地已落库 → 飞书（可选）→ 云端 MySQL 备份（可选）
            if self._feishu_push_mode != "hourly_latest":
                feishu_rows = [{**item, "created_at": now_ts} for item in new_data]
                await self._sync_batch_to_feishu(feishu_rows)
            cloud_rows = self._material_rows_for_cloud_backup(new_data)
            await self._sync_batch_to_cloud_backup(cloud_rows)

        except Exception as e:
            logger.error(f"[数据库] 保存失败: {e}")

    async def _sync_batch_to_feishu(self, rows: List[Dict[str, Any]]) -> None:
        """SQLite 写入成功后同步同一批数据到飞书；失败仅打日志，不影响抓取。"""
        a = self._feishu_app_token
        p = self._feishu_personal_base_token
        t = self._feishu_table_id
        if not rows or not (a and p and t):
            return

        def _run() -> None:
            try:
                from services.feishu_bitable import BitableTable

                BitableTable(a, p, t).insert_pmc_material_rows(rows)
                logger.info(f"[飞书] 已同步 {len(rows)} 条")
            except Exception as e:
                logger.warning(f"[飞书] 同步失败（已跳过）: {e}")

        await asyncio.to_thread(_run)

    @staticmethod
    def _row_ad_detail_basic_for_cloud_api(row: Dict[str, Any]) -> Dict[str, Any]:
        """本地 pmc_ad_detail_basic 行 → 云端 /api/pmc_ad_detail_basic.php 单行。"""
        out: Dict[str, Any] = {
            "aadvid": str(row.get("aadvid", "")).strip(),
            "ad_id": str(row.get("ad_id", "")).strip(),
        }
        for k in (
            "budget",
            "audience_coverage_count",
            "compensation_convert",
            "user_info_id",
            "user_info_name",
            "user_info_unique_id",
        ):
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                out[k] = v
        if row.get("ecp_roi2_goal") is not None:
            out["ecp_roi2_goal"] = row["ecp_roi2_goal"]
        if row.get("creative_type") is not None:
            out["creative_type"] = row["creative_type"]
        ts = row.get("updated_at")
        if ts:
            out["created_at"] = ts
            out["updated_at"] = ts
        return out

    def _upload_ad_detail_basic_batches(
        self, username: str, password: str, rows: List[Dict[str, Any]]
    ) -> None:
        """广告基础信息云端同步 POST，按 PMC_AD_DETAIL_BASIC_MAX_ROWS 分批。"""
        if not rows:
            return
        url = API_BASE_URL.rstrip("/") + PMC_AD_DETAIL_BASIC_PATH
        batches = (len(rows) + PMC_AD_DETAIL_BASIC_MAX_ROWS - 1) // PMC_AD_DETAIL_BASIC_MAX_ROWS
        max_attempts = 3
        retry_http_codes = (408, 500, 502, 503, 504)

        for bi, start in enumerate(range(0, len(rows), PMC_AD_DETAIL_BASIC_MAX_ROWS)):
            chunk = rows[start : start + PMC_AD_DETAIL_BASIC_MAX_ROWS]
            payload = {"username": username.strip(), "password": password, "rows": chunk}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            raw = None
            for attempt in range(max_attempts):
                req = Request(
                    url,
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                try:
                    with urlopen(req, timeout=120) as resp:
                        raw = resp.read()
                    break
                except HTTPError as e:
                    if attempt < max_attempts - 1 and e.code in retry_http_codes:
                        logger.warning(
                            f"[云端备份·基础信息] 第 {bi + 1}/{batches} 批 HTTP {e.code}，"
                            f"{attempt + 1}/{max_attempts} 次，将重试…"
                        )
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    err_raw = e.read() if e.fp else b""
                    try:
                        msg = json.loads(err_raw.decode("utf-8", errors="replace")).get(
                            "message", str(e)
                        )
                    except Exception:
                        msg = err_raw.decode("utf-8", errors="replace") or str(e)
                    raise RuntimeError(msg) from e
                except (URLError, OSError) as e:
                    if attempt < max_attempts - 1:
                        logger.warning(
                            f"[云端备份·基础信息] 第 {bi + 1}/{batches} 批 网络/SSL 错误，"
                            f"{attempt + 1}/{max_attempts} 次: {e}，将重试…"
                        )
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    if isinstance(e, URLError) and getattr(e, "reason", None):
                        raise RuntimeError(str(e.reason)) from e
                    raise RuntimeError(str(e)) from e
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                raise RuntimeError("云端备份·基础信息响应非 JSON") from e
            if not data.get("success"):
                raise RuntimeError(data.get("message") or "云端备份·基础信息失败")
            n = (data.get("data") or {}).get("upserted")
            logger.info(
                f"[云端备份·基础信息] 第 {bi + 1}/{batches} 批完成，"
                f"upserted={n if n is not None else len(chunk)}"
            )

    def _flush_pending_ad_detail_basic_cloud_sync(
        self, username: str, password: str
    ) -> None:
        """素材已成功备份后调用：将待同步的一条广告基础信息 POST 上去；成功则清空 pending。"""
        pending = self._pending_ad_detail_basic_cloud_row
        if not pending:
            return
        try:
            api_row = self._row_ad_detail_basic_for_cloud_api(pending)
            self._upload_ad_detail_basic_batches(username, password, [api_row])
            self._pending_ad_detail_basic_cloud_row = None
            logger.info(
                f"[云端备份·基础信息] 已同步 aadvid={api_row.get('aadvid')} ad_id={api_row.get('ad_id')}"
            )
        except Exception as e:
            logger.warning(f"[云端备份·基础信息] 同步失败（将稍后重试或本轮结束时重试）: {e}")

    @staticmethod
    def _material_rows_for_cloud_backup(new_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """本地 INSERT 行 → 云端备份 API 的 rows（去掉 id/user_id；附带 created_at/updated_at）。"""
        out: List[Dict[str, Any]] = []
        for item in new_data:
            row: Dict[str, Any] = {}
            for k, v in item.items():
                if k in ("id", "user_id"):
                    continue
                if v is None:
                    continue
                row[k] = v
            row["material_id"] = str(row.get("material_id", ""))
            row["aadvid"] = str(row.get("aadvid", ""))
            row["stat_date"] = str(row.get("stat_date", ""))
            out.append(row)
        return out

    def _upload_cloud_backup_batches(self, username: str, password: str, rows: List[Dict[str, Any]]) -> None:
        """同步 HTTP POST，按 PMC_CLOUD_BACKUP_MAX_ROWS 分批。"""
        if not rows:
            return
        url = API_BASE_URL.rstrip("/") + PMC_CLOUD_BACKUP_PATH
        batches = (len(rows) + PMC_CLOUD_BACKUP_MAX_ROWS - 1) // PMC_CLOUD_BACKUP_MAX_ROWS
        _cloud_backup_max_attempts = 3
        _retry_http_codes = (408, 500, 502, 503, 504)

        for bi, start in enumerate(range(0, len(rows), PMC_CLOUD_BACKUP_MAX_ROWS)):
            chunk = rows[start : start + PMC_CLOUD_BACKUP_MAX_ROWS]
            payload = {"username": username.strip(), "password": password, "rows": chunk}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            raw = None
            for attempt in range(_cloud_backup_max_attempts):
                req = Request(
                    url,
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                try:
                    with urlopen(req, timeout=120) as resp:
                        raw = resp.read()
                    break
                except HTTPError as e:
                    if (
                        attempt < _cloud_backup_max_attempts - 1
                        and e.code in _retry_http_codes
                    ):
                        logger.warning(
                            f"[云端备份] 第 {bi + 1}/{batches} 批 HTTP {e.code}，"
                            f"{attempt + 1}/{_cloud_backup_max_attempts} 次，将重试…"
                        )
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    err_raw = e.read() if e.fp else b""
                    try:
                        msg = json.loads(err_raw.decode("utf-8", errors="replace")).get("message", str(e))
                    except Exception:
                        msg = err_raw.decode("utf-8", errors="replace") or str(e)
                    raise RuntimeError(msg) from e
                except (URLError, OSError) as e:
                    if attempt < _cloud_backup_max_attempts - 1:
                        logger.warning(
                            f"[云端备份] 第 {bi + 1}/{batches} 批 网络/SSL 错误，"
                            f"{attempt + 1}/{_cloud_backup_max_attempts} 次: {e}，将重试…"
                        )
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    if isinstance(e, URLError) and getattr(e, "reason", None):
                        raise RuntimeError(str(e.reason)) from e
                    raise RuntimeError(str(e)) from e
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                raise RuntimeError("云端备份响应非 JSON") from e
            if not data.get("success"):
                raise RuntimeError(data.get("message") or "云端备份失败")
            ins = (data.get("data") or {}).get("inserted")
            logger.info(
                f"[云端备份] 第 {bi + 1}/{batches} 批完成，插入 {ins if ins is not None else len(chunk)} 条"
            )

    async def _sync_batch_to_cloud_backup(self, rows: List[Dict[str, Any]]) -> None:
        """SQLite 与飞书之后，将同一批数据 POST 到服务端 MySQL；失败仅打日志。"""
        u = self._cloud_backup_username
        p = self._cloud_backup_password
        if not rows or not u or not p:
            return

        def _run() -> None:
            try:
                self._upload_cloud_backup_batches(u, p, rows)
            except Exception as e:
                logger.warning(f"[云端备份] 失败（已跳过）: {e}")
                return
            self._flush_pending_ad_detail_basic_cloud_sync(u, p)

        await asyncio.to_thread(_run)

    async def _finalize_pending_ad_detail_basic_cloud(self) -> None:
        """本轮抓取结束前再尝试一次待同步的基础信息（弥补中途仅素材失败等情形）。"""
        u = self._cloud_backup_username
        p = self._cloud_backup_password
        if not self._pending_ad_detail_basic_cloud_row or not u or not p:
            return

        def _run() -> None:
            self._flush_pending_ad_detail_basic_cloud_sync(u, p)

        await asyncio.to_thread(_run)
        if self._pending_ad_detail_basic_cloud_row:
            logger.warning(
                "[云端备份·基础信息] 本轮结束时仍未同步成功（常见原因：素材云端备份未成功，"
                "服务端要求先同步素材）。已保留本地 SQLite 记录。"
            )
            self._pending_ad_detail_basic_cloud_row = None

    async def _wait_for_full_data(self, timeout: int = 600, db=None):
        """
        等待收集完整数据（直到达到 totalCount），支持手动翻页

        Args:
            timeout: 最大等待时间（秒）
            db: 数据库实例，用于每页数据入库
        """
        interval = 1
        waited = 0
        last_count = 0

        while waited < timeout:
            await self._raise_if_global_auth_expired()
            # 检查是否已收集完整数据
            if self._material_total_count > 0 and self._material_current_count >= self._material_total_count:
                logger.info(f"[抓取] 数据收集完成！共 {len(self._material_data_dict)} 条数据")
                # 最后一次入库
                await self._save_to_database(db)
                return True

            # 检查是否有新数据到达，如果没有则尝试翻页
            if self._material_current_count == last_count and self._material_current_count < self._material_total_count:
                # 没有新数据，尝试翻页
                success = await self._go_to_next_page()
                if not success:
                    # 无法翻页（已到最后一页），退出等待
                    logger.warning(f"[抓取] 无法继续翻页，已获取 {self._material_current_count}/{self._material_total_count} 条数据")
                    # 入库当前数据
                    await self._save_to_database(db)
                    break
                waited += 3  # 翻页后已经等待了3秒
                # 翻页成功后立即入库
                await self._save_to_database(db)

            last_count = self._material_current_count

            await asyncio.sleep(interval)
            waited += interval

        # 超时但已有部分数据
        if self._material_current_count > 0:
            logger.warning(f"[抓取] 等待超时！已获取 {self._material_current_count}/{self._material_total_count} 条数据")
            await self._save_to_database(db)
            return True

        return False

    async def _wait_for_full_assist_data(self, timeout: int = 600):
        """调控任务列表翻页，直到达到 totalCount 或无法翻页（逻辑同素材）。"""
        interval = 1
        waited = 0
        last_count = 0

        while waited < timeout:
            await self._raise_if_global_auth_expired()
            if self._assist_total_count > 0 and self._assist_current_count >= self._assist_total_count:
                logger.info(f"[调控任务] 列表收集完成，共 {len(self._assist_task_ids)} 条（去重 id）")
                return True

            if (
                self._assist_current_count == last_count
                and self._assist_total_count > 0
                and self._assist_current_count < self._assist_total_count
            ):
                success = await self._go_to_next_page()
                if not success:
                    logger.warning(
                        f"[调控任务] 无法继续翻页，已获取 {self._assist_current_count}/"
                        f"{self._assist_total_count} 条"
                    )
                    break
                waited += 3
            last_count = self._assist_current_count
            await asyncio.sleep(interval)
            waited += interval

        if self._assist_current_count > 0:
            logger.warning(
                f"[调控任务] 等待超时或提前结束：{self._assist_current_count}/"
                f"{self._assist_total_count or '?'}"
            )
            return True
        return False

    async def _fetch_roi2_assist_tasks(self, db, timeout: int) -> None:
        """
        同轮次第二段：素材完成后切换「调控 > 素材追投」，拦截 ad/list-required，入库 pmc_roi2_assist_task。
        不做飞书/云端备份。
        """
        if not self._current_aadvid:
            logger.warning("[调控任务] 无 aavid，跳过")
            return
        self._reset_assist_fetch_state()
        self._assist_fetch_db = db
        try:
            self._detach_assist_listeners()
            self._attach_assist_listeners()
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await self._switch_to_regulation_material_chase_tabs()
            await self._raise_if_global_auth_expired()
            try:
                await self._switch_to_100_per_page()
                await self._raise_if_global_auth_expired()

                first_page_timeout = 30
                step = 0.5
                w = 0.0
                while w < first_page_timeout:
                    await self._raise_if_global_auth_expired()
                    if self._assist_task_ids:
                        break
                    await asyncio.sleep(step)
                    w += step

                if not self._assist_task_ids:
                    logger.warning("[调控任务] 未拦截到首屏 ad/list-required，跳过翻页")
                    return

                logger.info(
                    f"[调控任务] 开始翻页拉全量（预计 {self._assist_total_count or '?'} 条）..."
                )
                await self._wait_for_full_assist_data(timeout)
            finally:
                self._detach_assist_listeners()
        except GlobalAuthExpiredError:
            raise
        except Exception as e:
            logger.warning(f"[调控任务] 抓取异常（已忽略，本轮仍继续）: {e}")
        finally:
            self._assist_fetch_db = None

    async def fetch(
        self,
        url: str,
        db=None,
        timeout: int = 600,
        *,
        feishu_app_token: Optional[str] = None,
        feishu_personal_base_token: Optional[str] = None,
        feishu_table_id: Optional[str] = None,
        feishu_push_mode: str = "each_crawl",
        cloud_backup_username: Optional[str] = None,
        cloud_backup_password: Optional[str] = None,
    ) -> dict:
        """
        访问指定 URL 并抓取千川投放数据（自动翻页获取全部数据，不关闭浏览器）
        每抓取一页数据后立即清洗并入库

        Args:
            url: 要访问的页面 URL
            db: 数据库实例，用于每页数据入库
            timeout: 等待分页数据的超时时间（秒），默认5分钟

        Returns:
            包含以下键的字典:
                - material_data: 清洗后的素材数据（所有页合并）
                - material_data_raw: 原始素材数据列表（所有页）
                - material_page_count: 获取的页数
                - material_total_count: 总数据条数
                - aadvid: 广告主ID
        """
        # 重置状态（不复用浏览器）
        self._material_data_dict = {}
        self._material_total_count = 0
        self._material_current_count = 0
        self._is_collecting = False
        self._last_saved_count = 0
        self._pending_ad_detail_basic_cloud_row = None

        def _strip_opt(v: Optional[str]) -> Optional[str]:
            if v is None:
                return None
            s = str(v).strip()
            return s if s else None

        fa, fp, ft = _strip_opt(feishu_app_token), _strip_opt(feishu_personal_base_token), _strip_opt(feishu_table_id)
        if fa and fp and ft:
            self._feishu_app_token = fa
            self._feishu_personal_base_token = fp
            self._feishu_table_id = ft
        else:
            self._feishu_app_token = None
            self._feishu_personal_base_token = None
            self._feishu_table_id = None
        pm = (feishu_push_mode or "each_crawl").strip().lower()
        self._feishu_push_mode = pm if pm in ("each_crawl", "hourly_latest") else "each_crawl"

        cu = _strip_opt(cloud_backup_username)
        cp = cloud_backup_password if cloud_backup_password is not None else ""
        if cu and cp:
            self._cloud_backup_username = cu
            self._cloud_backup_password = cp
        else:
            self._cloud_backup_username = None
            self._cloud_backup_password = ""

        # 从 URL 中提取 aavid、adId，用于过滤 API 响应与投放中门控
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        self._current_aadvid = query_params.get("aavid", [None])[0]
        self._current_adid = query_params.get("adId", [None])[0] or query_params.get("adid", [None])[0]

        # 投放中门控：须在 goto 前监听，以便捕获首屏 ad-detail-basic
        self._sqlite_store = db
        self._ad_detail_gate_queue = asyncio.Queue()
        self._ad_detail_gate_active = True
        self.page.on("response", self._on_response_ad_detail_gate)

        # 访问目标页面
        logger.info(f"[抓取] 正在访问: {url}")
        gate_ok = False
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(random.uniform(3, 5))
            await self._raise_if_global_auth_expired()
            gate_ok = await self._wait_for_ad_delivery_gate()
        finally:
            self._ad_detail_gate_active = False
            try:
                self.page.remove_listener("response", self._on_response_ad_detail_gate)
            except Exception:
                pass
            self._ad_detail_gate_queue = None
            self._sqlite_store = None

        if not gate_ok:
            return await self._create_empty_result()

        # 1. 切换到视频卡片 tab，并检测/应用投放详情自定义列（每轮执行，表头已齐则跳过）
        await self._switch_to_video_tab()
        await self._apply_promotion_detail_column_preset()
        await self._raise_if_global_auth_expired()

        # 2. 每轮先卸下再注册，避免上一轮未清理时重复绑定
        self._detach_fetch_listeners()
        self._attach_fetch_listeners()
        try:
            # 3. 切换每页100条
            await self._switch_to_100_per_page()
            await self._raise_if_global_auth_expired()

            # 等待第一页数据被抓取（最多等待 30 秒）
            first_page_timeout = 30
            interval = 0.5
            waited = 0

            while waited < first_page_timeout:
                await self._raise_if_global_auth_expired()
                if self._material_data_dict:  # 已有第一页数据
                    break
                await asyncio.sleep(interval)
                waited += interval

            if not self._material_data_dict:
                logger.warning("[抓取] 未能获取第一页数据")
                self._pending_ad_detail_basic_cloud_row = None
                return await self._create_empty_result()

            # 第一页数据立即入库
            await self._save_to_database(db)

            # 自动翻页获取全部数据（每翻一页后立即入库）
            logger.info(f"[抓取] 开始等待完整数据（预计 {self._material_total_count} 条）...")
            await self._wait_for_full_data(timeout, db)

            await self._finalize_pending_ad_detail_basic_cloud()

            # 同轮次第二段：调控任务（需在采集服务配置中开启）
            scrape_cfg = load_scrape_service_config()
            if scrape_cfg.get("fetch_assist_tasks"):
                await self._fetch_roi2_assist_tasks(db, timeout)
            else:
                logger.info("[调控任务] 未启用（采集服务配置中关闭），跳过")

            # 返回抓取结果
            return {
                "aadvid": self._current_aadvid,
                "material_data": None,  # 已经每页入库了，不需要返回
                "material_data_raw": self._material_data_dict,
                "material_page_count": len(self._material_data_dict),
                "material_total_count": self._material_total_count,
                "material_time_range": {
                    "start_time": self._material_start_time,
                    "end_time": self._material_end_time,
                },
                "assist_task_count": len(self._assist_task_ids),
                "assist_task_total_count": self._assist_total_count,
            }
        finally:
            self._detach_fetch_listeners()

    async def _create_empty_result(self) -> dict:
        """创建空结果"""
        return {
            "aadvid": self._current_aadvid,
            "material_data": None,
            "material_data_raw": {},
            "material_page_count": 0,
            "material_total_count": 0,
            "material_time_range": {
                "start_time": self._material_start_time,
                "end_time": self._material_end_time,
            },
        }

    async def close(self):
        """关闭浏览器"""
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None


def build_qianchuan_url_by_params(
    base_url: str,
    aavid: int,
    ad_id: int,
    ct: int = 1,
    live_qcpx_mode: int = 0,
    umg: int = 2,
    uni_video_tab: int = 2,
    usbt: int = 0
) -> str:
    """
    根据参数构建千川 URL

    Args:
        base_url: 基础 URL（如 https://qianchuan.jinritemai.com/uni-prom/detail）
        aavid: 广告主ID
        ad_id: 广告ID
        ct: 默认 1
        live_qcpx_mode: 默认 0
        umg: 默认 2
        uni_video_tab: 默认 2
        usbt: 默认 0

    Returns:
        构建好的千川 URL
    """
    # 获取今日日期
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    dr = f"{today},{today}"

    # 构建查询参数
    query_params = {
        "aavid": aavid,
        "adId": ad_id,
        "ct": ct,
        "dr": dr,
        "liveQcpxMode": live_qcpx_mode,
        "uniDetail": {}
    }

    # 构建哈希参数
    hash_params = {
        "adr": {},
        "umg": umg,
        "uniVideoTab": uni_video_tab,
        "usbt": usbt,
        "uniDetail": {
            "tb": "creative",
            "edc": "liveRace",
            "cst": 0,
            "creativeTabAutoVideoChase": "",
            "uniTaskCenterAssistTaskScene": "",
            "bcf": {},
            "cfs": {},
            "bfs": {},
            "afs": {},
            "pfs": {},
            "aId": "",
            "jf": "uniAd",
            "uAId": ad_id,
            "cId": "",
            "bpId": "",
            "agId": "",
            "awemeId": "",
            "pdId": ""
        },
        "cc": {
            "sk": "",
            "ccft": 0,
            "p": 1,
            "ps": 10,
            "st": "asc",
            "sf": ""
        }
    }

    # 构建完整 URL
    return build_qianchuan_url(base_url, query_params, hash_params)
