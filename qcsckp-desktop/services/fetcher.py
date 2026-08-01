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
from urllib.parse import urlparse, parse_qs, urlunparse
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright, Page, Browser, BrowserContext, Response

from config import (
    API_BASE_URL,
    PMC_AD_DETAIL_BASIC_MAX_ROWS,
    PMC_AD_DETAIL_BASIC_PATH,
    PMC_CLOUD_BACKUP_MAX_ROWS,
    PMC_CLOUD_BACKUP_PATH,
    REMOTE_SERVICES_ENABLED,
)
from utils.clean_promotion import clean_pmc_promotion_data, clean_pmc_roi2_assist_task_data
from utils.common import require_executable_path, build_qianchuan_url
from services.control_panel_config import load_scrape_service_config
from services.plan_system import normalize_plan_system
from services.product_scene_adapter import extract_product_scene_snapshot
from utils.log import logger
from api.promotion_targets import (
    LEGACY_TARGET_UID,
    normalize_scene,
    replace_material_product_links,
    upsert_products,
)


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
AD_DETAIL_PLUS_PREFIX = (
    "https://qianchuan.jinritemai.com/ad/api/creation/v1/ad/ad-detail-plus"
)
# 等待 ad-detail-basic 返回「投放中」的最长时间（秒），超时则本轮放弃
AD_DETAIL_GATE_TIMEOUT_SEC = 10

PRODUCT_MATERIAL_METRICS = (
    "product_show_count_for_roi2",
    "product_click_count_for_roi2",
    "product_cvr_rate_for_roi2",
    "product_convert_rate_for_roi2",
    "total_pay_order_count_for_roi2",
    "total_pay_order_gmv_include_coupon_for_roi2",
    "total_pay_order_gmv_for_roi2",
    "total_pay_order_coupon_amount_for_roi2",
    "total_ecom_platform_subsidy_amount_for_roi2",
    "stat_cost_for_roi2",
    "total_prepay_and_pay_order_roi2",
    "total_cost_per_pay_order_for_roi2",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_order_settle_amount_for_roi2_1h",
    "total_order_settle_count_for_roi2_1h",
    "total_cost_per_pay_order_settle_for_roi2_1h",
    "total_order_settle_amount_rate_for_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_rate",
)

PRODUCT_MATERIAL_DIMENSIONS = (
    "material_id",
    "roi2_material_status",
    "roi2_material_video_type",
    "roi2_material_video_name",
    "roi2_material_video_play_info",
    "material_tag_list",
    "roi2_material_show_status",
    "roi2_material_show_status_reason",
    "roi2_material_upload_time",
)



class QianChuanFetcher:
    """千川投放数据抓取器"""

    def __init__(self, headless: bool = True, storage_state: Any = None):
        """
        初始化抓取器

        Args:
            headless: 是否使用无头模式运行浏览器
            storage_state: Playwright登录状态对象或兼容的JSON文件路径
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
        self._current_target_uid: str = LEGACY_TARGET_UID
        self._current_account_uid: str = ""
        self._current_promotion_scene: str = "live"
        self._current_plan_system: str = "unknown"
        self._current_plan_name: str = ""

        # 投放中门控：仅当 ad-detail-basic 判定为投放中后才继续抓素材
        self._ad_detail_gate_queue: Optional[asyncio.Queue] = None
        self._ad_detail_gate_active: bool = False
        self._delivery_gate_detail: Dict[str, Any] = {
            "ok": False,
            "reason": "not_checked",
        }
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
        self._assist_response_seen: bool = False
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
        context_state = self.storage_state
        session_storage = {}
        if isinstance(context_state, dict):
            session_storage = context_state.get(
                "_qcsckp_session_storage"
            ) or {}
            # Playwright 不接受自定义字段；只把标准 storage_state 交给
            # new_context，sessionStorage 通过初始化脚本恢复。
            context_state = {
                key: value
                for key, value in context_state.items()
                if key in {"cookies", "origins"}
            }
        self.context = await self.browser.new_context(
            user_agent=user_agent,
            viewport={'width': 1280, 'height': 720},
            storage_state=context_state
        )
        if isinstance(session_storage, dict) and session_storage:
            serialized = json.dumps(
                session_storage,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await self.context.add_init_script(
                script=(
                    "(() => {"
                    f"const stores={serialized};"
                    "const values=stores[location.origin];"
                    "if(!values||typeof values!=='object')return;"
                    "for(const [key,value] of Object.entries(values)){"
                    "try{sessionStorage.setItem(key,String(value));}catch(_e){}"
                    "}"
                    "})();"
                )
            )
        self.page = await self.context.new_page()

    @staticmethod
    def _decode_request_payload(request_payload: Any) -> Optional[dict]:
        """把 Playwright 的 POST 正文转换为字典；无法确认时返回 None。"""
        payload = request_payload
        if isinstance(payload, (bytes, bytearray)):
            try:
                payload = payload.decode("utf-8", errors="replace")
            except Exception:
                return None
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def _extract_request_plan_ids(
        cls,
        url: str,
        request_payload: Any = None,
    ) -> set:
        """
        从 URL 或 POST Filters 中提取明确的计划 ID。

        同一账户可能并行加载多条计划；只有请求明确限定为当前 ad_id 时，
        响应才允许进入当前目标。无计划证据的旧请求安全忽略。
        """
        plan_ids = set()

        def add(value: Any) -> None:
            if isinstance(value, dict) and "value" in value:
                add(value.get("value"))
                return
            if isinstance(value, (list, tuple, set)):
                for child in value:
                    add(child)
                return
            if value is None or isinstance(value, bool):
                return
            text = str(value).strip()
            if text:
                plan_ids.add(text)

        parsed = urlparse(url)
        for key, values in parse_qs(parsed.query).items():
            if str(key or "").strip().lower() in {
                "adid",
                "ad_id",
                "planid",
                "plan_id",
            }:
                add(values)

        payload = cls._decode_request_payload(request_payload)
        if not payload:
            return plan_ids

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                # 千川报表请求通常把计划范围放在
                # Filters.Conditions[{Field:"ad_id", Values:[...]}]。
                field = str(
                    value.get("Field")
                    or value.get("field")
                    or ""
                ).strip().lower()
                if field in {"adid", "ad_id", "planid", "plan_id"}:
                    add(
                        value.get("Values")
                        if "Values" in value
                        else value.get("values")
                    )
                    if "Value" in value:
                        add(value.get("Value"))
                    if "value" in value:
                        add(value.get("value"))
                for key, child in value.items():
                    normalized_key = str(key or "").strip().lower()
                    if normalized_key in {
                        "adid",
                        "ad_id",
                        "planid",
                        "plan_id",
                    }:
                        add(child)
                    walk(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    walk(child)

        walk(payload)
        return plan_ids

    def _request_matches_current_plan(
        self,
        url: str,
        request_payload: Any = None,
    ) -> bool:
        current_adid = str(self._current_adid or "").strip()
        if not current_adid:
            return False
        request_plan_ids = self._extract_request_plan_ids(
            url,
            request_payload,
        )
        # 多计划请求同样不能归入单个监控目标；必须且只能命中当前计划。
        return request_plan_ids == {current_adid}

    def _is_target_api(self, url: str, request_payload: Any = None) -> bool:
        """检查素材 API 是否同时匹配当前账户、计划和请求来源。"""
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

        # 旧版页面把 reqFrom 放在查询参数中；新版商品全域页改放在 POST JSON 正文中。
        req_from = query_params.get("reqFrom", [])
        req_from_value = req_from[0] if req_from else None
        if not req_from_value and request_payload is not None:
            payload = self._decode_request_payload(request_payload)
            if payload:
                req_from_value = payload.get("reqFrom")
        if req_from_value != "uni-prom-creative-tab-list":
            return False

        return self._request_matches_current_plan(url, request_payload)

    def _is_target_assist_api(
        self,
        url: str,
        request_payload: Any = None,
    ) -> bool:
        """调控任务列表：须同时匹配当前账户与当前计划。"""
        if not url.startswith(AD_ASSIST_LIST_REQUIRED_PREFIX):
            return False
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        aavid_list = query_params.get("aavid", [])
        if not aavid_list:
            return False
        if self._current_aadvid and aavid_list[0] != self._current_aadvid:
            return False
        return self._request_matches_current_plan(url, request_payload)

    def _is_ad_detail_gate_url(self, url: str) -> bool:
        """是否为当前计划详情请求；商品全域使用 ad-detail-plus。"""
        if not url.startswith((AD_DETAIL_BASIC_PREFIX, AD_DETAIL_PLUS_PREFIX)):
            return False
        if not self._current_aadvid or not self._current_adid:
            return False
        parsed = urlparse(url)
        q = parse_qs(parsed.query)
        adid = (q.get("adid") or q.get("adId") or [None])[0]
        aavid = (q.get("aavid") or q.get("aadvid") or [None])[0]
        if adid and str(adid) != str(self._current_adid):
            return False
        if aavid and str(aavid) != str(self._current_aadvid):
            return False
        # 新版商品页可能把计划放在响应体而不是查询参数；响应回调还会再次核对 detail.id。
        return True

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
            "account_uid": self._current_account_uid,
            "ad_id": str(ad_id).strip(),
            "target_uid": self._current_target_uid,
            "plan_name": self._current_plan_name,
            "promotion_scene": self._current_promotion_scene,
            "plan_system": self._current_plan_system,
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
        detail_id = str(detail.get("id") or detail.get("adId") or "").strip()
        if detail_id and detail_id != str(self._current_adid or ""):
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
                        unique_fields=["account_uid", "aadvid", "ad_id"],
                        update_fields=[
                            "account_uid",
                            "target_uid",
                            "plan_name",
                            "promotion_scene",
                            "plan_system",
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
                        f"[数据库] ad-detail-basic 已写入/更新 target={row.get('target_uid')} "
                        f"aadvid={row.get('aadvid')} ad_id={row.get('ad_id')}"
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
        URL 缺少 adId/aavid 或监听队列不可用时也必须安全失败，不能把未知状态
        当成投放中继续。
        """
        if not self._current_aadvid or not self._current_adid:
            logger.warning("[抓取] URL 未包含 adId 与 aavid，无法核验投放状态")
            self._delivery_gate_detail = {
                "ok": False,
                "reason": "ids_missing",
            }
            return False
        if not self._ad_detail_gate_queue:
            self._delivery_gate_detail = {
                "ok": False,
                "reason": "gate_unavailable",
            }
            return False

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
                self._delivery_gate_detail = {
                    "ok": False,
                    "reason": "detail_timeout",
                }
                return False
            try:
                ok, name, dtype = await asyncio.wait_for(
                    self._ad_detail_gate_queue.get(),
                    timeout=min(remaining, 0.25),
                )
            except asyncio.TimeoutError:
                continue
            if ok:
                self._delivery_gate_detail = {
                    "ok": True,
                    "reason": "delivering",
                    "delivery_name": name,
                    "delivery_type": dtype,
                }
                logger.info("[抓取] 已确认投放中，继续抓取素材列表")
                return True
            self._delivery_gate_detail = {
                "ok": False,
                "reason": "not_delivering",
                "delivery_name": name,
                "delivery_type": dtype,
            }
            logger.warning(
                f"[抓取] 当前非投放中（adDeliveryName={name!r}, adDeliveryType={dtype}），结束本轮"
            )
            return False

    async def _check_product_delivery_gate(self, db=None) -> bool:
        """商品页会默认加载另一条全店计划，须按目标 ad_id 主动只读复核。"""
        if not self.page or not self._current_aadvid or not self._current_adid:
            self._delivery_gate_detail = {
                "ok": False,
                "reason": "ids_missing",
            }
            return False
        try:
            payload = await self.page.evaluate(
                """async ({ aavid, adId }) => {
                    const query = new URLSearchParams({ aavid, adid: adId });
                    const response = await fetch(
                        `/ad/api/creation/v1/ad/ad-detail-plus?${query.toString()}`,
                        { credentials: "include" }
                    );
                    if (!response.ok) {
                        throw new Error(`HTTP ${response.status}`);
                    }
                    return await response.json();
                }""",
                {
                    "aavid": str(self._current_aadvid),
                    "adId": str(self._current_adid),
                },
            )
        except Exception as exc:
            self._delivery_gate_detail = {
                "ok": False,
                "reason": "product_detail_error",
                "message": str(exc),
            }
            logger.warning("[商品抓取] 目标计划详情读取失败：%s", exc)
            return False

        if not isinstance(payload, dict) or payload.get("status_code") != 0:
            self._delivery_gate_detail = {
                "ok": False,
                "reason": "product_detail_error",
                "message": (
                    payload.get("message")
                    if isinstance(payload, dict)
                    else "响应格式异常"
                ),
            }
            return False
        detail = (payload.get("data") or {}).get("adDetailInfo")
        if not isinstance(detail, dict):
            self._delivery_gate_detail = {
                "ok": False,
                "reason": "product_detail_missing",
            }
            return False
        actual_ad_id = str(detail.get("id") or detail.get("adId") or "").strip()
        if actual_ad_id != str(self._current_adid):
            self._delivery_gate_detail = {
                "ok": False,
                "reason": "product_detail_mismatch",
                "actual_ad_id": actual_ad_id,
            }
            return False

        ok = self._ad_detail_is_delivering(detail)
        name = detail.get("adDeliveryName")
        dtype = detail.get("adDeliveryType")
        self._delivery_gate_detail = {
            "ok": bool(ok),
            "reason": "delivering" if ok else "not_delivering",
            "delivery_name": name,
            "delivery_type": dtype,
        }
        if not ok:
            logger.warning(
                "[商品抓取] 当前非投放中（adDeliveryName=%r, adDeliveryType=%r）",
                name,
                dtype,
            )
            return False

        row = self._build_pmc_ad_detail_basic_row(payload, detail)
        if row and db:
            try:
                db.insert_or_update(
                    table="pmc_ad_detail_basic",
                    data=row,
                    unique_fields=["account_uid", "aadvid", "ad_id"],
                    update_fields=[
                        "account_uid",
                        "target_uid",
                        "plan_name",
                        "promotion_scene",
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
                if self._cloud_backup_username and self._cloud_backup_password:
                    self._pending_ad_detail_basic_cloud_row = dict(row)
            except Exception as exc:
                logger.warning("[商品抓取] 基础信息写入失败：%s", exc)
        logger.info("[商品抓取] 已按目标计划 ID 确认投放中")
        return True

    def _build_product_material_request_body(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """构造商品全域素材只读查询；模板来自当前千川页面实际请求。"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        return {
            "DataSetKey": "site_promotion_product_post_data_video",
            "Metrics": list(PRODUCT_MATERIAL_METRICS),
            "Filters": {
                "ConditionRelationshipType": 1,
                "Conditions": [
                    {"Field": "query_type", "Operator": 7, "Values": ["all"]},
                    {
                        "Field": "roi2_material_type_v3",
                        "Operator": 7,
                        "Values": ["1001"],
                    },
                    {"Field": "marketing_goal", "Operator": 7, "Values": ["1"]},
                    {
                        "Field": "roi2_material_status",
                        "Operator": 7,
                        "Values": ["1"],
                    },
                    {
                        "Field": "ad_id",
                        "Operator": 7,
                        "Values": [str(self._current_adid or "")],
                    },
                    {
                        "Field": "roi2_material_video_type",
                        "Operator": 7,
                        "Values": ["11"],
                    },
                ],
            },
            "StartTime": f"{today} 00:00:00",
            "EndTime": f"{today} 23:59:59",
            "PageParams": {
                "Limit": max(1, min(int(limit), 100)),
                "Offset": max(0, int(offset)),
            },
            "OrderBy": [
                {"Type": 2, "Field": "product_show_count_for_roi2"}
            ],
            "Dimensions": list(PRODUCT_MATERIAL_DIMENSIONS),
            "reqFrom": "uni-prom-creative-tab-list",
        }

    async def _fetch_product_material_pages(self, db, timeout: int) -> None:
        """按目标商品计划 ID 直接分页读取素材，避免页面默认切回其它计划。"""
        offset = 0
        page_size = 100
        deadline = asyncio.get_running_loop().time() + max(30, int(timeout))
        while True:
            await self._raise_if_global_auth_expired()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("商品素材分页读取超时")
            body = self._build_product_material_request_body(
                offset=offset,
                limit=page_size,
            )
            self._material_start_time = body["StartTime"]
            self._material_end_time = body["EndTime"]
            payload = await asyncio.wait_for(
                self.page.evaluate(
                    """async ({ aavid, body }) => {
                        const query = new URLSearchParams({ aavid });
                        const response = await fetch(
                            `/ad/api/pmc/v1/uni-promotion/material/list-required?${query.toString()}`,
                            {
                                method: "POST",
                                credentials: "include",
                                headers: { "content-type": "application/json" },
                                body: JSON.stringify(body)
                            }
                        );
                        if (!response.ok) {
                            throw new Error(`HTTP ${response.status}`);
                        }
                        return await response.json();
                    }""",
                    {
                        "aavid": str(self._current_aadvid),
                        "body": body,
                    },
                ),
                timeout=min(remaining, 60),
            )
            if not isinstance(payload, dict) or payload.get("status_code") != 0:
                message = (
                    payload.get("message")
                    if isinstance(payload, dict)
                    else "响应格式异常"
                )
                raise RuntimeError(f"商品素材接口异常：{message}")
            stats_data = (payload.get("data") or {}).get("statsData") or {}
            rows = stats_data.get("rows") or []
            await self._handle_material_response(payload, API_PREFIXES[0])
            await self._save_to_database(db)
            total = int(stats_data.get("totalCount") or 0)
            offset += len(rows)
            if not rows or (total > 0 and offset >= total):
                break
        logger.info(
            "[商品抓取] 素材分页完成，共 %s 条",
            len(self._material_data_dict),
        )

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
        payload = None
        try:
            post_data = request.post_data
            if post_data:
                payload = json.loads(post_data)
        except Exception:
            payload = None

        if not self._is_target_api(url, payload):
            return

        # 获取 POST 请求的 payload，提取时间范围
        try:
            if isinstance(payload, dict):
                # 提取时间范围
                self._material_start_time = payload.get("StartTime")
                self._material_end_time = payload.get("EndTime")
                logger.info(f"[请求] 时间范围: {self._material_start_time} ~ {self._material_end_time}")

        except Exception as e:
            logger.warning(f"[警告] 解析请求 payload 失败: {url}, 错误: {e}")

    async def _on_response(self, response: Response):
        """拦截响应并处理"""
        url = response.url
        request_payload = None
        try:
            request_payload = response.request.post_data
        except Exception:
            pass
        if not self._is_target_api(url, request_payload):
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
        self._assist_response_seen = False

    async def _on_response_assist(self, response: Response):
        url = response.url
        request_payload = None
        try:
            request_payload = response.request.post_data
        except Exception:
            pass
        if not self._is_target_assist_api(url, request_payload):
            return
        try:
            data = await response.json()
        except Exception as e:
            logger.warning(f"[调控任务] 解析响应失败: {url}, {e}")
            return
        if await self._handle_assist_response(data, url):
            self._assist_response_seen = True

    async def _handle_assist_response(self, data: dict, url: str) -> bool:
        """处理 ad/list-required 响应：更新进度并 upsert 本地 pmc_roi2_assist_task。"""
        if not isinstance(data, dict):
            logger.warning("[调控任务] API 响应不是对象，忽略本次响应")
            return False
        if data.get("status_code") != 0:
            logger.warning(f"[调控任务] API 业务异常: {data.get('message')}")
            return False

        d = data.get("data")
        if not isinstance(d, dict):
            logger.warning("[调控任务] API 响应缺少有效 data 对象，忽略本次响应")
            return False
        if "adInfos" not in d:
            logger.warning("[调控任务] API 响应缺少 adInfos，忽略本次响应")
            return False
        ad_infos = d.get("adInfos")
        if not isinstance(ad_infos, list):
            logger.warning("[调控任务] API 响应 adInfos 不是列表，忽略本次响应")
            return False

        # 总条数 data.pagination.totalNum
        total_count = 0
        total_count_known = False
        pagination = d.get("pagination")
        if isinstance(pagination, dict):
            tn = pagination.get("totalNum")
            if tn is not None and str(tn).strip() != "":
                try:
                    total_count = int(tn)
                    total_count_known = True
                except (TypeError, ValueError):
                    total_count = 0
        if total_count <= 0:
            total_raw = d.get("totalCount")
            if total_raw is None:
                total_raw = d.get("total")
            try:
                if total_raw is not None:
                    total_count = int(total_raw)
                    total_count_known = True
            except (TypeError, ValueError):
                total_count = 0
        if not ad_infos and (not total_count_known or total_count != 0):
            logger.warning(
                "[调控任务] 空任务响应缺少明确的 total=0，忽略本次响应"
            )
            return False
        if ad_infos and not total_count_known:
            logger.warning(
                "[调控任务] 非空任务响应缺少明确总数，禁止判定为全量同步"
            )
            return False
        if total_count < len(ad_infos):
            logger.warning(
                "[调控任务] 响应总数小于本页任务数，忽略本次响应"
            )
            return False

        page_task_ids: List[str] = []
        for ad in ad_infos:
            if not isinstance(ad, dict):
                continue
            tid = ad.get("id")
            if tid is None:
                continue
            ts = str(tid).strip()
            if ts:
                page_task_ids.append(ts)

        if ad_infos and not page_task_ids:
            logger.warning("[调控任务] 非空响应没有有效任务ID，忽略本次响应")
            return False
        if ad_infos and not await self._persist_assist_api_response_to_db(data):
            return False
        for task_id in page_task_ids:
            self._assist_task_ids[task_id] = True

        if not self._is_assist_collecting:
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
        return True

    async def _persist_assist_api_response_to_db(self, raw_top: dict) -> bool:
        """单条 list-required 完整 JSON → clean → upsert（无云端备份）。"""
        db = self._assist_fetch_db
        aadvid = self._current_aadvid
        ad_id = self._current_adid
        if not db or not aadvid or not ad_id:
            return False
        if self._current_promotion_scene == "product":
            try:
                snapshot = extract_product_scene_snapshot(raw_top)
                products = snapshot.get("products") or []
                if products:
                    upsert_products(self._current_target_uid, products, db=db)
                for material in snapshot.get("materials") or []:
                    material_id = str(material.get("material_id") or "").strip()
                    product_ids = material.get("product_ids") or []
                    if not material_id or not product_ids:
                        continue
                    replace_material_product_links(
                        self._current_target_uid,
                        material_id,
                        product_ids,
                        material_name=str(material.get("material_name") or ""),
                        db=db,
                    )
            except Exception as e:
                logger.warning("[调控任务] 商品与素材关系写入失败: %s", e)
                return False
        try:
            rows = clean_pmc_roi2_assist_task_data(
                raw_top, str(aadvid).strip(), str(ad_id).strip()
            )
        except Exception as e:
            logger.warning(f"[调控任务] 清洗失败: {e}")
            return False
        if not rows:
            logger.warning("[调控任务] 非空响应未清洗出任何任务行")
            return False

        def _run() -> None:
            for row in rows:
                row = dict(row)
                row["account_uid"] = self._current_account_uid
                row["target_uid"] = self._current_target_uid
                row["promotion_scene"] = self._current_promotion_scene
                row["plan_system"] = self._current_plan_system
                if self._current_promotion_scene == "product":
                    material_ids: List[str] = []
                    try:
                        materials = json.loads(
                            row.get("assist_materials_json") or "[]"
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        materials = []
                    for material in materials if isinstance(materials, list) else []:
                        if not isinstance(material, dict):
                            continue
                        material_id = str(
                            material.get("material_id")
                            or material.get("materialId")
                            or ""
                        ).strip()
                        if material_id and material_id not in material_ids:
                            material_ids.append(material_id)
                    product_ids: List[str] = []
                    if material_ids:
                        placeholders = ",".join("?" for _ in material_ids)
                        relation_rows = db.execute(
                            "SELECT DISTINCT product_id "
                            "FROM promotion_material_product "
                            f"WHERE target_uid=? AND material_id IN ({placeholders})",
                            (self._current_target_uid, *material_ids),
                            fetch=True,
                        )
                        product_ids = [
                            str(item.get("product_id") or "").strip()
                            for item in relation_rows or []
                            if str(item.get("product_id") or "").strip()
                        ]
                    row["product_ids_json"] = json.dumps(
                        product_ids,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                db.insert_or_update(
                    table="pmc_roi2_assist_task",
                    data=row,
                    unique_fields=["target_uid", "assist_task_id"],
                    update_fields=None,
                )

        await asyncio.to_thread(_run)
        logger.info(f"[数据库·调控任务] 本页已写入/更新 {len(rows)} 条")
        return True

    async def _prune_stale_assist_rows_after_full_sync(self, db: Any) -> bool:
        """全量同步完成后移除该计划已不存在的旧调控任务，避免旧指标触发停投。"""
        target_uid = str(self._current_target_uid or "").strip()
        if (
            not db
            or not target_uid
            or target_uid == LEGACY_TARGET_UID
        ):
            return False
        assist_ids = sorted(
            str(task_id).strip()
            for task_id in self._assist_task_ids
            if str(task_id).strip()
        )

        def _run() -> None:
            if assist_ids:
                placeholders = ",".join("?" for _ in assist_ids)
                db.execute(
                    "DELETE FROM pmc_roi2_assist_task "
                    f"WHERE target_uid=? AND assist_task_id NOT IN ({placeholders})",
                    (target_uid, *assist_ids),
                )
            else:
                db.execute(
                    "DELETE FROM pmc_roi2_assist_task WHERE target_uid=?",
                    (target_uid,),
                )

        try:
            await asyncio.to_thread(_run)
            return True
        except Exception as exc:
            logger.warning(
                "[调控任务] 清理已下线旧任务失败，禁止将本轮标记为完整同步: %s",
                exc,
            )
            return False

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

    @staticmethod
    def _extract_product_ids_from_material_row(row: Dict[str, Any]) -> List[str]:
        """兼容不同商品全域响应结构，提取素材关联商品 ID。"""
        product_keys = {
            "productid",
            "product_id",
            "productids",
            "product_ids",
            "commodityid",
            "commodity_id",
            "goodsid",
            "goods_id",
            "pdid",
        }
        result: List[str] = []
        seen = set()

        def add(value: Any) -> None:
            if isinstance(value, dict) and "value" in value:
                value = value.get("value")
            if isinstance(value, str):
                stripped = value.strip()
                if stripped[:1] in ("[", "{"):
                    try:
                        add(json.loads(stripped))
                        return
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                for part in stripped.split(","):
                    text = part.strip()
                    if text and text not in seen:
                        seen.add(text)
                        result.append(text)
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    add(item)
                return
            if isinstance(value, dict):
                for key in ("id", "productId", "product_id", "commodityId", "goodsId"):
                    if key in value:
                        add(value.get(key))
                return
            if value is not None:
                text = str(value).strip()
                if text and text not in seen:
                    seen.add(text)
                    result.append(text)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized = str(key or "").replace("-", "_").lower()
                    if normalized in product_keys:
                        add(child)
                    elif isinstance(child, (dict, list, tuple)):
                        walk(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    walk(child)

        walk(row)
        return result

    @classmethod
    def _extract_products_from_material_row(cls, row: Dict[str, Any]) -> List[Dict[str, Any]]:
        product_ids = cls._extract_product_ids_from_material_row(row)
        if not product_ids:
            return []
        product_name = ""
        dimensions = row.get("dimensions") if isinstance(row, dict) else {}
        if isinstance(dimensions, dict):
            for key in (
                "productName",
                "product_name",
                "commodityName",
                "goodsName",
                "roi2ProductName",
            ):
                block = dimensions.get(key)
                if isinstance(block, dict):
                    block = block.get("value")
                if block is not None and str(block).strip():
                    product_name = str(block).strip()
                    break
        return [
            {"product_id": product_id, "product_name": product_name}
            for product_id in product_ids
        ]

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
                item["target_uid"] = self._current_target_uid
                item["ad_id"] = self._current_adid
                item["promotion_scene"] = self._current_promotion_scene
                item["plan_system"] = self._current_plan_system
                item["stat_date"] = today
                raw_row = self._material_data_dict.get(str(item.get("material_id") or "")) or {}
                product_ids = self._extract_product_ids_from_material_row(raw_row)
                item["product_ids_json"] = json.dumps(
                    product_ids,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

            # 直接插入（不更新，允许重复数据）
            missing_product_links: List[str] = []
            for item in new_data:
                db.insert(
                    table="pmc_promotion_material",
                    data=item
                )
                if self._current_promotion_scene == "product":
                    raw_row = self._material_data_dict.get(str(item.get("material_id") or "")) or {}
                    products = self._extract_products_from_material_row(raw_row)
                    if products:
                        upsert_products(self._current_target_uid, products, db=db)
                        replace_material_product_links(
                            self._current_target_uid,
                            item.get("material_id"),
                            [x.get("product_id") for x in products],
                            material_name=str(item.get("video_name") or ""),
                            db=db,
                        )
                    else:
                        missing_product_links.append(
                            str(item.get("material_id") or "")
                        )
            if missing_product_links:
                logger.warning(
                    "[抓取] 本批有 %s 条商品全域素材未识别到商品关联；"
                    "已保留既有关系，这些素材仍可参加素材级规则，但不参加商品级汇总",
                    len(missing_product_links),
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
        if not REMOTE_SERVICES_ENABLED or not rows:
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
        if not REMOTE_SERVICES_ENABLED or not rows:
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
        if not REMOTE_SERVICES_ENABLED or not rows or not u or not p:
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
        if (
            not REMOTE_SERVICES_ENABLED
            or not self._pending_ad_detail_basic_cloud_row
            or not u
            or not p
        ):
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
            return False
        return False

    async def _fetch_roi2_assist_tasks(self, db, timeout: int) -> bool:
        """
        同轮次第二段：素材完成后切换「调控 > 素材追投」，拦截 ad/list-required，入库 pmc_roi2_assist_task。
        不做飞书/云端备份。
        """
        if not self._current_aadvid or not self._current_adid:
            logger.warning("[调控任务] 无 aavid 或 ad_id，跳过")
            return False
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
                    if self._assist_response_seen:
                        break
                    await asyncio.sleep(step)
                    w += step

                if not self._assist_response_seen:
                    logger.warning("[调控任务] 未拦截到首屏 ad/list-required，跳过翻页")
                    return False
                if self._assist_total_count <= 0:
                    if not await self._prune_stale_assist_rows_after_full_sync(db):
                        return False
                    logger.info("[调控任务] 当前计划没有调控任务，完整同步成功")
                    return True

                logger.info(
                    f"[调控任务] 开始翻页拉全量（预计 {self._assist_total_count or '?'} 条）..."
                )
                complete = bool(await self._wait_for_full_assist_data(timeout))
                if not complete:
                    return False
                return bool(
                    await self._prune_stale_assist_rows_after_full_sync(db)
                )
            finally:
                self._detach_assist_listeners()
        except GlobalAuthExpiredError:
            raise
        except Exception as e:
            logger.warning(f"[调控任务] 抓取异常（已忽略，本轮仍继续）: {e}")
            return False
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
        target_uid: Optional[str] = None,
        promotion_scene: str = "live",
        plan_system: str = "unknown",
        plan_name: Optional[str] = None,
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
        self._reset_assist_fetch_state()
        self._pending_ad_detail_basic_cloud_row = None
        self._delivery_gate_detail = {
            "ok": False,
            "reason": "not_checked",
        }
        self._current_target_uid = str(target_uid or LEGACY_TARGET_UID).strip() or LEGACY_TARGET_UID
        self._current_promotion_scene = normalize_scene(promotion_scene or "live")
        self._current_plan_system = normalize_plan_system(
            plan_system or "unknown"
        )
        self._current_plan_name = str(plan_name or "").strip()[:256]

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
        self._current_account_uid = ""
        if db and self._current_target_uid != LEGACY_TARGET_UID:
            try:
                target = db.select_one(
                    "promotion_target",
                    fields="account_uid",
                    where={"target_uid": self._current_target_uid},
                ) or {}
                self._current_account_uid = str(
                    target.get("account_uid") or ""
                ).strip()
            except Exception:
                self._current_account_uid = ""

        # 访问目标页面
        logger.info(f"[抓取] 正在访问: {url}")
        gate_ok = False
        if self._current_promotion_scene == "product":
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(random.uniform(3, 5))
            await self._raise_if_global_auth_expired()
            gate_ok = await self._check_product_delivery_gate(db)
        else:
            # 直播门控：须在 goto 前监听，以便捕获首屏 ad-detail-basic。
            self._sqlite_store = db
            self._ad_detail_gate_queue = asyncio.Queue()
            self._ad_detail_gate_active = True
            self.page.on("response", self._on_response_ad_detail_gate)
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

        if self._current_promotion_scene == "product":
            await self._fetch_product_material_pages(db, timeout)
            await self._finalize_pending_ad_detail_basic_cloud()
            scrape_cfg = load_scrape_service_config()
            assist_sync_enabled = bool(scrape_cfg.get("fetch_assist_tasks"))
            assist_sync_ok = False
            if assist_sync_enabled:
                assist_sync_ok = await self._fetch_roi2_assist_tasks(db, timeout)
            else:
                logger.info(
                    "[调控任务] 未启用（采集服务配置中关闭），跳过"
                )
            return {
                "aadvid": self._current_aadvid,
                "ad_id": self._current_adid,
                "target_uid": self._current_target_uid,
                "promotion_scene": self._current_promotion_scene,
                "plan_system": self._current_plan_system,
                "material_data": None,
                "material_data_raw": self._material_data_dict,
                "material_page_count": len(self._material_data_dict),
                "material_total_count": self._material_total_count,
                "material_time_range": {
                    "start_time": self._material_start_time,
                    "end_time": self._material_end_time,
                },
                "assist_task_count": len(self._assist_task_ids),
                "assist_task_total_count": self._assist_total_count,
                "assist_sync_enabled": assist_sync_enabled,
                "assist_sync_ok": assist_sync_ok,
                "delivery_gate": dict(self._delivery_gate_detail),
            }

        # 素材 Tab 首次打开就会立即请求首屏数据，监听必须先于 Tab 切换注册。
        self._detach_fetch_listeners()
        self._attach_fetch_listeners()
        try:
            # 1. 切换到视频卡片 tab，并检测/应用投放详情自定义列（每轮执行，表头已齐则跳过）
            await self._switch_to_video_tab()
            await self._apply_promotion_detail_column_preset()
            await self._raise_if_global_auth_expired()

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
            assist_sync_enabled = bool(scrape_cfg.get("fetch_assist_tasks"))
            assist_sync_ok = False
            if assist_sync_enabled:
                assist_sync_ok = await self._fetch_roi2_assist_tasks(db, timeout)
            else:
                logger.info("[调控任务] 未启用（采集服务配置中关闭），跳过")

            # 返回抓取结果
            return {
                "aadvid": self._current_aadvid,
                "ad_id": self._current_adid,
                "target_uid": self._current_target_uid,
                "promotion_scene": self._current_promotion_scene,
                "plan_system": self._current_plan_system,
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
                "assist_sync_enabled": assist_sync_enabled,
                "assist_sync_ok": assist_sync_ok,
                "delivery_gate": dict(self._delivery_gate_detail),
            }
        finally:
            self._detach_fetch_listeners()

    async def _create_empty_result(self) -> dict:
        """创建空结果"""
        return {
            "aadvid": self._current_aadvid,
            "ad_id": self._current_adid,
            "target_uid": self._current_target_uid,
            "promotion_scene": self._current_promotion_scene,
            "plan_system": self._current_plan_system,
            "material_data": None,
            "material_data_raw": {},
            "material_page_count": 0,
            "material_total_count": 0,
            "delivery_gate": dict(self._delivery_gate_detail),
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
    usbt: int = 0,
    promotion_scene: str = "live",
    source_url: Optional[str] = None,
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
    scene = normalize_scene(promotion_scene or "live")
    source_parsed = urlparse(str(source_url or "").strip())
    if source_parsed.scheme in ("http", "https") and source_parsed.netloc:
        base_url = urlunparse(
            (
                source_parsed.scheme,
                source_parsed.netloc,
                source_parsed.path,
                "",
                "",
                "",
            )
        )
        source_query = parse_qs(source_parsed.query)
        try:
            ct = int(source_query.get("ct", [ct])[0])
        except (TypeError, ValueError):
            pass
        try:
            live_qcpx_mode = int(
                source_query.get("liveQcpxMode", [live_qcpx_mode])[0]
            )
        except (TypeError, ValueError):
            pass

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
            "edc": "liveRace" if scene == "live" else "productRace",
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
