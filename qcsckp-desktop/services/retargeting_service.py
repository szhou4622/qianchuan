# -*- coding: utf-8 -*-
"""
千川「素材追投」Playwright 自动化（独立模块，不混入 QianChuanFetcher / run_services）。

职责概要：
- 用 aavid / adId 构建投放详情 URL（复用 fetcher.build_qianchuan_url_by_params）
- goto 后切换「素材 → 视频」卡片（与抓取服务一致）
- 按素材 ID 搜索 → 行内「追投」→ 按 rule_retargeting.json 中 retargeting 块填表并提交

会话级参数（浏览器、读入 storage_state、无头）放在 __init__；
Cookie 文件写入仅由抓取服务负责，本服务 run 结束不保存 storage_state。
单次执行参数（aavid、ad_id、material_id、retargeting 配置）放在 run()。

run() 始终返回 RetargetingRunResult：含 success、message（简短摘要）、detail（异常堆栈、
接口原文等冗长信息，可为空）、step、finished_at、素材/计划 ID 等，便于写入 SQLite。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import platform
import random
import traceback
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import DATA_DIR, QIANCHUAN_BACKEND
from api.promotion_targets import extract_target_ids, normalize_scene
from playwright.async_api import Browser, BrowserContext, Page, Response, async_playwright
from services.fetcher import build_qianchuan_url_by_params
from services.product_scene_adapter import (
    find_visible_exact_text,
    goto_and_confirm_product_target,
    validate_exact_product_target_payload,
)
from services.plan_system import (
    confirm_live_page_plan_system,
    normalize_plan_system,
)
from services.promotion_capability import (
    RETARGET_FORM_PROBE_VERSION,
    check_target_capability,
)
from utils.common import require_executable_path
from utils.log import logger


def retarget_log_tag(
    *,
    strategy_title: Optional[str] = None,
    immediate: bool = False,
    scheduler: bool = False,
) -> str:
    """
    日志前缀：即刻追投为 [即刻追投]；调度层无策略名为 [规则化追投]；
    否则为 [策略名 追投]（策略名过长时截断）。"""
    if immediate:
        return "[即刻追投]"
    if scheduler:
        return "[规则化追投]"
    t = (strategy_title or "").strip() or "策略"
    if len(t) > 64:
        t = t[:64]
    return f"[{t} 追投]"


DEFAULT_BASE_URL = "https://qianchuan.jinritemai.com/uni-prom/detail"

# 与前端 rule_retargeting「任务名称后缀」留空默认一致；空串时仍追加后缀到任务名称
DEFAULT_TASK_NAME_SUFFIX = "素材看盘自动追投"

# 提交追投后由前端调用的创建调控任务接口（成功判定以响应为准，见 dev_files/zuitou.md）
ASSIST_TASK_API_SUBSTRING = "/ad/api/pmc/v1/uni-promotion/ad/create-uni-prom-assist-task"

# 只有完整验证过当前版本商品追投表单结构的证据，才允许规则调度发卡。
# 选择器或必填结构发生变化时升级版本，可让旧证据自动失效并要求重新验证。
RETARGET_PROBE_VERSION = RETARGET_FORM_PROBE_VERSION


def retarget_capability_matches(
    capability: Any,
    *,
    promotion_scene: str,
    plan_system: str,
) -> bool:
    """判断追投能力证据是否与当前计划场景、体系和探测器版本一致。"""
    ok, _ = check_target_capability(
        capability,
        action="retarget",
        promotion_scene=promotion_scene,
        plan_system=plan_system,
    )
    return ok


def _resolve_task_name_suffix(r: Dict[str, Any]) -> str:
    s = str(r.get("task_name_suffix") or "").strip()
    return s if s else DEFAULT_TASK_NAME_SUFFIX


def _is_assist_task_response(resp: Response) -> bool:
    try:
        u = resp.url or ""
        return ASSIST_TASK_API_SUBSTRING in u
    except Exception:
        return False


def _parse_assist_task_json(body: Any) -> Tuple[Optional[str], str, str]:
    """
    解析 create-uni-prom-assist-task 响应 JSON。
    成功：(None, "", data.id)。
    失败：(简短 message, detail 含 status/message/原文片段, "")。
    """
    if not isinstance(body, dict):
        raw = _safe_json(body) if body is not None else ""
        return (
            "调控接口响应格式异常",
            (raw[:4000] if raw else repr(body))[:8000],
            "",
        )
    try:
        sc = int(body.get("status_code"))
    except (TypeError, ValueError):
        sc = None
    msg = body.get("message")
    if sc != 0:
        return (
            "调控接口返回失败",
            f"status_code={sc!r} message={msg!r}\n{_safe_json(body)[:4000]}",
            "",
        )
    if str(msg) != "success":
        return (
            "调控接口返回失败",
            f"message={msg!r}\n{_safe_json(body)[:4000]}",
            "",
        )
    data = body.get("data")
    tid = ""
    if isinstance(data, dict):
        v = data.get("id")
        if v is not None:
            tid = str(v).strip()
    return None, "", tid


def _now_ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return ""


def _resolve_path(p: Optional[str]) -> Optional[str]:
    if not p or not str(p).strip():
        return None
    s = str(p).strip()
    if os.path.isabs(s):
        return s
    return os.path.join(DATA_DIR, s)


def _default_storage_state_path() -> Any:
    from services.qianchuan_session import load_qianchuan_storage_state

    return load_qianchuan_storage_state()


@dataclass
class RetargetingSessionOptions:
    """浏览器会话：与单次追投任务无关，适合放在 __init__。"""

    headless: bool = True
    storage_state: Any = None
    """Playwright storage_state；None时读取当前工具账号的DPAPI加密会话。"""
    base_url: str = DEFAULT_BASE_URL
    browser_executable_path: Optional[str] = None
    goto_timeout_ms: int = 60_000


@dataclass
class RetargetingRunResult:
    """
    单次追投执行结果：无论成功失败均填齐，供业务层落库。

    - message：简短摘要（成功/失败一句话）
    - detail：冗长信息（异常堆栈、接口返回原文等）；无则空串
    - step：阶段标识（validate / build_url / browser / search_material / fill_or_submit / submit_api / done / exception）
    - regulate_task_id：调控任务 id（来自 create-uni-prom-assist-task 的 data.id）；失败或未返回时为空
    """

    success: bool
    message: str
    step: str
    detail: str = ""
    aavid: str = ""
    ad_id: str = ""
    material_id: str = ""
    regulate_task_id: str = ""
    retargeting_method: str = ""
    retargeting_json: str = ""
    finished_at: str = ""
    headless: bool = False

    def to_log_dict(self) -> Dict[str, Any]:
        """
        扁平 dict，便于 insert 到 SQLite（列名可按需映射；success 用整数 0/1 亦可再转）。
        """
        return {
            "success": self.success,
            "message": self.message,
            "detail": self.detail,
            "step": self.step,
            "aavid": self.aavid,
            "ad_id": self.ad_id,
            "material_id": self.material_id,
            "regulate_task_id": self.regulate_task_id,
            "retargeting_method": self.retargeting_method,
            "retargeting_json": self.retargeting_json,
            "finished_at": self.finished_at,
            "headless": self.headless,
        }

    def asdict(self) -> Dict[str, Any]:
        """与 to_log_dict 相同语义，保留 dataclasses.asdict 风格别名。"""
        return asdict(self)


class QianChuanRetargetingService:
    """
    追投专用 Playwright 服务；与抓取 Fetcher 分离，仅复用 URL 构建函数。

    用法（单次）：
        result = await svc.run(aavid=123, ad_id=456, material_id="789", retargeting={...})
        await svc.close()

    规则化调度连续多条：每条均完整打开详情 URL（reuse_session=False），close_session=False，策略结束后 close()。
    """

    def __init__(self, options: Optional[RetargetingSessionOptions] = None):
        self._opt = options or RetargetingSessionOptions()
        ss = self._opt.storage_state
        if ss is None:
            self._storage_state = _default_storage_state_path()
        else:
            self._storage_state = ss if isinstance(ss, dict) else _resolve_path(ss)

        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._log_tag = retarget_log_tag()

    def _make_result(
        self,
        *,
        success: bool,
        message: str,
        step: str,
        detail: str = "",
        aavid: Any = "",
        ad_id: Any = "",
        material_id: str = "",
        regulate_task_id: str = "",
        retargeting: Any = None,
    ) -> RetargetingRunResult:
        r = retargeting if isinstance(retargeting, dict) else {}
        method = str(r.get("method") or "").strip().lower() if r else ""
        rid = str(regulate_task_id).strip() if regulate_task_id is not None else ""
        det = str(detail).strip() if detail is not None else ""
        return RetargetingRunResult(
            success=success,
            message=message,
            step=step,
            detail=det,
            aavid=str(aavid).strip() if aavid is not None else "",
            ad_id=str(ad_id).strip() if ad_id is not None else "",
            material_id=str(material_id).strip() if material_id is not None else "",
            regulate_task_id=rid,
            retargeting_method=method,
            retargeting_json=_safe_json(r),
            finished_at=_now_ts(),
            headless=bool(self._opt.headless),
        )

    async def _confirm_live_target_delivering(
        self,
        page: Page,
        *,
        expected_aavid: Any,
        expected_ad_id: Any,
    ) -> Optional[str]:
        """提交前只读复核直播主计划身份与明确的投放中状态。"""
        aavid = str(expected_aavid or "").strip()
        ad_id = str(expected_ad_id or "").strip()
        if not aavid.isdigit() or not ad_id.isdigit():
            return "直播追投缺少有效账户或计划ID，已安全停止"
        try:
            response = await page.evaluate(
                """async ({ aavid, adId }) => {
                    const query = new URLSearchParams({ aavid, adid: adId });
                    const result = await fetch(
                        `/ad/api/creation/v1/ad/ad-detail-basic?${query.toString()}`,
                        { credentials: "include" }
                    );
                    let payload = null;
                    try { payload = await result.json(); } catch (_) {}
                    return { httpStatus: result.status, payload };
                }""",
                {"aavid": aavid, "adId": ad_id},
            )
        except Exception as exc:
            return f"直播计划投放状态复核失败：{exc}"
        if not isinstance(response, dict) or int(
            response.get("httpStatus") or 0
        ) != 200:
            return "直播计划投放状态接口无有效响应，已安全停止"
        payload = response.get("payload")
        if not isinstance(payload, dict):
            return "直播计划投放状态响应格式异常，已安全停止"
        status_code = payload.get("status_code")
        if status_code not in (None, 0, "0"):
            return str(payload.get("message") or "直播计划投放状态接口返回失败")
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        detail = data.get("adDetailInfo")
        if not isinstance(detail, dict):
            return "直播计划详情未返回主计划，已安全停止"
        actual_ad_id = str(
            detail.get("id") or detail.get("adId") or detail.get("ad_id") or ""
        ).strip()
        if actual_ad_id != ad_id:
            return (
                f"直播计划不匹配：期望 {ad_id}，实际 "
                f"{actual_ad_id or '未返回'}"
            )
        actual_aavid = str(
            detail.get("advId")
            or detail.get("aavid")
            or detail.get("advertiserId")
            or data.get("advId")
            or data.get("aavid")
            or data.get("advertiserId")
            or ""
        ).strip()
        if actual_aavid != aavid:
            return (
                f"直播计划账户不匹配：期望 {aavid}，实际 "
                f"{actual_aavid or '未返回'}"
            )
        delivery_name = str(detail.get("adDeliveryName") or "").strip()
        try:
            delivery_type = int(detail.get("adDeliveryType"))
        except (TypeError, ValueError):
            delivery_type = -1
        if delivery_name != "投放中" or delivery_type != 0:
            return (
                "直播计划当前未取得明确投放中证据："
                f"{delivery_name or '未知状态'} / {delivery_type}"
            )
        return None

    async def _confirm_product_target_delivering(
        self,
        page: Page,
        *,
        expected_aavid: Any,
        expected_ad_id: Any,
        expected_plan_system: Any,
    ) -> Optional[str]:
        """商品追投最终提交前重新读取精确主计划详情，未知即拒绝。"""
        aavid = str(expected_aavid or "").strip()
        ad_id = str(expected_ad_id or "").strip()
        if not aavid.isdigit() or not ad_id.isdigit():
            return "商品追投缺少有效账户或计划ID，已安全停止"
        try:
            payload = await page.evaluate(
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
                {"aavid": aavid, "adId": ad_id},
            )
        except Exception as exc:
            return f"商品计划投放状态复核失败：{exc}"
        return validate_exact_product_target_payload(
            payload,
            expected_aavid=aavid,
            expected_ad_id=ad_id,
            expected_plan_system=expected_plan_system,
            require_delivering=True,
        )

    # ---------- 页面操作（与 fetcher 一致：先素材 Tab 再视频） ----------

    async def _switch_to_video_tab(self, promotion_scene: str = "live") -> Optional[str]:
        page = self.page
        if not page:
            return "页面不存在"
        scene = normalize_scene(promotion_scene or "live")
        try:
            for tab_text in ("素材", "创意"):
                sucai_tab = await page.query_selector(
                    'div[class*="ovui-tabs__nav-list"] '
                    f'>> div[class*="oc-new-badge"]:has-text("{tab_text}")'
                )
                if sucai_tab:
                    await sucai_tab.click()
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                    break

            video_tab = page.locator(
                'div[class*="oc-radio-group-button"] >> div[class*="ovui-radio-item"]:has-text("视频")'
            )
            if await video_tab.count() > 0:
                await video_tab.last.hover()
                await video_tab.last.click()
                logger.info("%s 已切换视频选项卡", self._log_tag)
                await asyncio.sleep(random.uniform(2.0, 3.5))
                return None
            else:
                logger.warning("%s 未找到视频选项卡", self._log_tag)
                if scene == "product":
                    # 商品全域部分页面直接展示素材表，不再有二级“视频”选项。
                    has_material_table = (
                        await page.locator('div[class*="ovui-table__body-wrapper"]').count() > 0
                        and await page.locator('div[class*="oc-filter-input-filter"] input').count() > 0
                    )
                    if has_material_table:
                        logger.info("%s 商品全域页面直接展示素材表，继续执行", self._log_tag)
                        return None
                return "未识别到当前场景的素材列表，已安全停止"
        except Exception as e:
            logger.warning("%s 切换视频选项卡失败: %s", self._log_tag, e)
            return f"切换视频选项卡失败: {e}"

    async def _dismiss_dialog_before_next_material(self, page: Page) -> None:
        """同一会话连续追投下一条素材前：尝试关闭残留弹层，避免挡住列表与搜索。"""
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(0.35, 0.7))
        except Exception:
            pass
        try:
            cancel = await page.query_selector(
                'div[class*="footer-wrapper"] >> div[class*="oc-button-wrap"] >> button:has-text("取消")'
            )
            if cancel and await cancel.is_visible():
                await cancel.click()
                await asyncio.sleep(random.uniform(0.35, 0.65))
        except Exception:
            pass

    async def _attach_popup_switcher(self) -> List[Any]:
        """新标签页时切换 active page（与 run_services 思路一致）。"""
        handlers: List[Any] = []

        async def _switch(p: Page) -> None:
            try:
                await p.bring_to_front()
            except Exception:
                pass
            self.page = p
            try:
                u = p.url
            except Exception:
                u = ""
            logger.info("%s 已切换到新页面 url=%s", self._log_tag, u)

        def _on_new_page(p: Page) -> None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_switch(p))
            except RuntimeError:
                pass

        ctx = self.context
        if ctx:
            ctx.on("page", _on_new_page)
            handlers.append(("context_page", _on_new_page))
        pg = self.page
        if pg:
            pg.on("popup", _on_new_page)
            handlers.append(("page_popup", _on_new_page))
        return handlers

    def _detach_popup_switcher(self, handlers: List[Any]) -> None:
        _ = handlers

    # ---------- 表单辅助 ----------

    async def _fill_labeled_number_input(self, page: Page, label: str, value: Any) -> Optional[str]:
        s = "" if value is None else str(value).strip()
        sel = (
            f'div[class*="oc-row"]:has(span:has-text("{label}")) '
            f'>> div[class*="oc-input-group-wrap"] >> input[class*="ovui-input"]'
        )
        el = await page.query_selector(sel)
        if not el:
            return f"未找到输入框：{label}"
        try:
            await el.fill(s)
        except Exception:
            await el.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(s, delay=50)

        err = await page.query_selector(
            f'div[class*="oc-row"]:has(span:has-text("{label}")) '
            f'>> div[class*="oc-msg-danger"] >> span[class*="oc-typography-value-int"]'
        )
        if err:
            try:
                if await err.is_visible():
                    t = (await err.inner_text() or "").strip()
                    return t or f"{label} 校验失败"
            except Exception:
                pass
        return None

    async def _append_task_name_suffix(self, page: Page, suffix: str) -> Optional[str]:
        label = "任务名称"
        sel = (
            f'div[class*="oc-row"]:has(span:has-text("{label}")) '
            f'>> div[class*="oc-input-group-wrap"] >> input[class*="ovui-input"]'
        )
        el = await page.query_selector(sel)
        if not el:
            return f"未找到输入框：{label}"
        await el.click()
        await page.keyboard.press("End")
        suf = suffix or ""
        await page.keyboard.type(f"_{suf}", delay=50)

        err = await page.query_selector(
            f'div[class*="oc-row"]:has(span:has-text("{label}")) '
            f'>> div[class*="oc-msg-danger"] >> span[class*="oc-typography-value-int"]'
        )
        if err:
            try:
                if await err.is_visible():
                    t = (await err.inner_text() or "").strip()
                    return t or "任务名称校验失败"
            except Exception:
                pass
        return None

    async def _click_radio_option(self, page: Page, row_hint: str, option_text: str) -> Optional[str]:
        sel = (
            f'div[class*="oc-row"]:has(span:has-text("{row_hint}")) '
            f'>> div[class*="ovui-radio-item-group"] >> div[class*="ovui-radio-item"]:has-text("{option_text}")'
        )
        el = await page.query_selector(sel)
        if not el:
            return f"未找到单选项：{row_hint} / {option_text}"
        await el.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))
        return None

    @staticmethod
    async def _find_visible_button(page: Page, text: str) -> Optional[Any]:
        """兼容直播旧表单和商品新弹层，返回文本完全匹配的可见按钮。"""
        buttons = page.locator("button").filter(has_text=text)
        count = await buttons.count()
        for index in range(count - 1, -1, -1):
            candidate = buttons.nth(index)
            try:
                if await candidate.is_visible() and (
                    await candidate.inner_text()
                ).strip() == text:
                    return candidate
            except Exception:
                continue
        return None

    async def _click_submit_and_wait_assist(self, page: Page) -> Tuple[Optional[str], str, str]:
        """
        在点击「提交」**之前**注册 expect_response，仅等待本次点击触发的 create-uni-prom-assist-task
        响应；with 结束后等待器移除，不会误用上一轮响应。
        返回 (简短错误信息, detail, 调控任务 id)；成功时 (None, "", tid)。
        """
        btn = await self._find_visible_button(page, "提交")
        if not btn:
            return "未找到提交按钮", "", ""
        timeout_ms = min(max(self._opt.goto_timeout_ms, 30_000), 120_000)
        try:
            async with page.expect_response(_is_assist_task_response, timeout=timeout_ms) as resp_info:
                await btn.hover()
                await btn.click()
            resp = await resp_info.value
        except Exception as e:
            return "调控接口无响应或超时", str(e), ""

        try:
            body = await resp.json()
        except Exception as e:
            return "调控接口响应解析失败", str(e), ""

        err_s, det, tid = _parse_assist_task_json(body)
        if err_s:
            return err_s, det, tid
        await asyncio.sleep(random.uniform(0.3, 0.6))
        return None, "", tid

    async def _wait_assist_task_response_user_submit(self, page: Page) -> Tuple[Optional[str], str, str]:
        """
        **不**执行提交按钮的 click；仅注册等待用户手动点击「提交」后触发的
        create-uni-prom-assist-task 响应。超时、JSON 解析与业务失败判定与 _click_submit_and_wait_assist 一致。
        返回 (简短错误信息, detail, 调控任务 id)；成功时 (None, "", tid)。
        """
        btn = await self._find_visible_button(page, "提交")
        if not btn:
            return "未找到提交按钮", "", ""
        timeout_ms = min(max(self._opt.goto_timeout_ms, 30_000), 120_000)
        logger.info("%s 表单已就绪，等待用户在浏览器中点击「提交」（拦截调控接口响应）", self._log_tag)
        try:
            async with page.expect_response(_is_assist_task_response, timeout=timeout_ms) as resp_info:
                await btn.hover()
            resp = await resp_info.value
        except Exception as e:
            return "调控接口无响应或超时", str(e), ""

        try:
            body = await resp.json()
        except Exception as e:
            return "调控接口响应解析失败", str(e), ""

        err_s, det, tid = _parse_assist_task_json(body)
        if err_s:
            return err_s, det, tid
        await asyncio.sleep(random.uniform(0.3, 0.6))
        return None, "", tid

    # ---------- 浏览器生命周期 ----------

    async def _ensure_browser(self) -> None:
        if self.browser and self.context and self.page:
            return

        self._playwright = await async_playwright().start()
        exe = require_executable_path(self._opt.browser_executable_path)
        self.browser = await self._playwright.chromium.launch(
            headless=self._opt.headless,
            chromium_sandbox=False,
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-geolocation",
                "--deny-permission-prompts",
                "--disable-blink-features=AutomationControlled",
            ],
            executable_path=exe,
        )
        sys_platform = platform.system().lower()
        if "darwin" in sys_platform or "mac" in sys_platform:
            user_agent = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
            )
        else:
            user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            )
        self.context = await self.browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1280, "height": 720},
            storage_state=self._storage_state,
        )
        self.page = await self.context.new_page()

    async def close(self) -> None:
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self.context = None
        self.page = None

    # ---------- 校验 ----------

    @staticmethod
    def _validate_retargeting_payload(
        r: Dict[str, Any],
        promotion_scene: str = "live",
    ) -> Optional[str]:
        if not isinstance(r, dict):
            return "retargeting 须为对象"
        method = str(r.get("method") or "volume").strip().lower()
        if method == "volume":
            vol = r.get("volume") or {}
            if not isinstance(vol, dict):
                return "retargeting.volume 须为对象"
            tb = vol.get("total_budget_yuan")
            if tb is None or (isinstance(tb, str) and not str(tb).strip()):
                return "放量追投须填写调控总预算 total_budget_yuan"
            try:
                budget_value = float(tb)
            except Exception:
                return "调控总预算须为数字"
            dh = vol.get("duration_hours")
            if dh is None or (isinstance(dh, str) and not str(dh).strip()):
                return "放量追投须填写调控时长 duration_hours"
            try:
                duration_value = float(dh)
            except Exception:
                return "调控时长须为数字"
            if normalize_scene(promotion_scene or "live") == "product":
                if budget_value < 100:
                    return "商品全域调控预算不得低于100元"
                if duration_value < 0.5 or duration_value > 24:
                    return "商品全域调控时长须在0.5至24小时之间"
            return None
        if method == "cost_control":
            if normalize_scene(promotion_scene or "live") == "product":
                return "当前商品全域素材追投页面仅支持调控预算和时长，暂不支持控成本追投"
            cc = r.get("cost_control") or {}
            if not isinstance(cc, dict):
                return "retargeting.cost_control 须为对象"
            og = str(cc.get("optimization_goal") or "net_roi").strip().lower()
            if og == "net_roi":
                nr = cc.get("net_roi") or {}
                if not isinstance(nr, dict):
                    return "cost_control.net_roi 须为对象"
                for k in ("daily_budget_yuan", "net_roi_target"):
                    x = nr.get(k)
                    if x is None or (isinstance(x, str) and not str(x).strip()):
                        return f"净成交ROI 须填写 {k}"
                    try:
                        float(x)
                    except Exception:
                        return f"{k} 须为数字"
            elif og == "live_room":
                if normalize_scene(promotion_scene or "live") == "product":
                    return "商品全域不支持直播间成交出价，请改用净成交ROI目标"
                lr = cc.get("live_room") or {}
                if not isinstance(lr, dict):
                    return "cost_control.live_room 须为对象"
                for k in ("daily_budget_yuan", "bid_per_conversion_yuan"):
                    x = lr.get(k)
                    if x is None or (isinstance(x, str) and not str(x).strip()):
                        return f"直播间成交 须填写 {k}"
                    try:
                        float(x)
                    except Exception:
                        return f"{k} 须为数字"
            else:
                return f"未知 optimization_goal: {og}"
            return None
        return f"未知追投方式 method: {method}"

    # ---------- 主流程 ----------

    @staticmethod
    async def _click_last_visible(locator: Any) -> bool:
        count = await locator.count()
        for index in range(count - 1, -1, -1):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                try:
                    await candidate.click(timeout=5_000)
                except Exception:
                    # 千川右下角智能助手可能覆盖弹层底部按钮；目标已经按
                    # 精确角色/文本定位时，直接触发该元素自身的 click，
                    # 避免坐标点击仍被浮层截获。
                    await candidate.evaluate("element => element.click()")
                return True
        return False

    async def _open_product_retarget_dialog(
        self,
        page: Page,
        ad_id: Any,
    ) -> Optional[str]:
        """从商品自选的精确计划行打开「素材追投」新建任务表单。"""
        plan_id = str(ad_id or "").strip()
        id_text = await find_visible_exact_text(
            page,
            f"ID：{plan_id}",
            timeout_ms=min(self._opt.goto_timeout_ms, 15_000),
        )
        if id_text is None:
            return f"商品自选列表未找到计划 {plan_id}"
        plan_row = id_text.locator("xpath=ancestor::tr[1]")
        if await plan_row.count() < 1:
            return f"无法定位商品计划 {plan_id} 的操作行"
        row_text = (await plan_row.inner_text() or "").strip()
        if "投放中" not in row_text:
            return f"商品计划 {plan_id} 当前不是投放中状态"

        retarget_label = plan_row.get_by_text("素材追投", exact=True)
        try:
            await retarget_label.first.wait_for(
                state="visible",
                timeout=min(self._opt.goto_timeout_ms, 15_000),
            )
        except Exception:
            return f"商品计划 {plan_id} 未提供素材追投入口"
        assist_task = retarget_label.first.locator(
            "xpath=ancestor::*[contains(@class,'assist-task')][1]"
        )
        if await assist_task.count() < 1:
            return f"商品计划 {plan_id} 的素材追投入口结构无法识别"
        await assist_task.hover()
        plus_icon = assist_task.locator(
            'iconpark-icon[name="oc-icon-plus"]'
        )
        try:
            await plus_icon.first.wait_for(
                state="visible",
                timeout=min(self._opt.goto_timeout_ms, 15_000),
            )
        except Exception:
            return f"商品计划 {plan_id} 的新建追投按钮不可用"
        await plus_icon.first.click()
        try:
            await page.get_by_text("新建调控任务", exact=True).last.wait_for(
                state="visible",
                timeout=min(self._opt.goto_timeout_ms, 15_000),
            )
            await page.get_by_text("添加视频", exact=True).last.wait_for(
                state="visible",
                timeout=min(self._opt.goto_timeout_ms, 15_000),
            )
        except Exception:
            return "商品素材追投表单未正常打开"
        return None

    async def _select_product_materials(
        self,
        page: Page,
        material_ids: List[str],
        *,
        scene_label: str = "商品",
        expected_total: Optional[int] = None,
    ) -> Optional[str]:
        """在已打开的追投表单中按精确素材ID批量添加视频，最多20条。"""
        ids: List[str] = []
        for raw_id in material_ids:
            mid = str(raw_id or "").strip()
            if mid and mid not in ids:
                ids.append(mid)
        if not ids:
            return f"{scene_label}追投任务缺少素材"
        if len(ids) > 20:
            return f"{scene_label}追投单次最多支持20条素材"

        add_video = page.get_by_text("添加视频", exact=True)
        if not await self._click_last_visible(add_video):
            return f"{scene_label}素材追投表单未找到「添加视频」"

        search_input = page.locator(
            'input[placeholder="输入视频名称/ID后回车搜索"]'
        )
        try:
            await search_input.last.wait_for(
                state="visible",
                timeout=min(self._opt.goto_timeout_ms, 15_000),
            )
        except Exception:
            return f"{scene_label}素材选择器未找到视频搜索框"
        # 弹层先绘制行骨架，再异步补齐素材可选状态；过早读取会把可用
        # 素材误判为 disabled。
        await page.wait_for_timeout(2_000)

        selector_modal = search_input.last.locator(
            "xpath=ancestor::div["
            "contains(concat(' ',normalize-space(@class),' '),' ovui-modal ')"
            "][1]"
        )
        if await selector_modal.count() < 1:
            return f"{scene_label}素材选择弹层结构无法识别"

        for selected_count, mid in enumerate(ids, start=1):
            await search_input.last.fill(mid)
            await search_input.last.press("Enter")
            material_matches = page.get_by_text(f"素材ID: {mid}", exact=True)
            try:
                await material_matches.last.wait_for(
                    state="visible",
                    timeout=min(self._opt.goto_timeout_ms, 20_000),
                )
            except Exception:
                return f"{scene_label}计划未找到素材 ID：{mid}"
            await page.wait_for_timeout(1_000)

            material_row = None
            disabled_row = None
            for index in range(await material_matches.count() - 1, -1, -1):
                text_candidate = material_matches.nth(index)
                if not await text_candidate.is_visible():
                    continue
                row_candidate = text_candidate.locator("xpath=ancestor::tr[1]")
                label_candidate = row_candidate.locator("label.ovui-checkbox")
                label_class = (
                    await label_candidate.first.get_attribute("class")
                    if await label_candidate.count() > 0
                    else ""
                ) or ""
                if "ovui-checkbox--disabled" not in label_class:
                    material_row = row_candidate
                    break
                if disabled_row is None:
                    disabled_row = row_candidate
            if material_row is None:
                material_row = disabled_row
            if material_row is None or await material_row.count() < 1:
                return f"无法定位{scene_label}素材 {mid} 的选择行"

            checkbox_input = material_row.locator('input[type="checkbox"]')
            if await checkbox_input.count() > 0:
                try:
                    checkbox_label = checkbox_input.first.locator(
                        "xpath=ancestor::label[1]"
                    )
                    label_class = (
                        await checkbox_label.get_attribute("class")
                        if await checkbox_label.count() > 0
                        else ""
                    ) or ""
                    if "ovui-checkbox--disabled" in label_class:
                        try:
                            await page.wait_for_function(
                                """label => !String(label && label.className || "")
                                    .includes("ovui-checkbox--disabled")""",
                                arg=await checkbox_label.element_handle(),
                                timeout=min(self._opt.goto_timeout_ms, 8_000),
                            )
                        except Exception:
                            return f"{scene_label}素材 {mid} 当前不可追投（选择框禁用）"
                    if not await checkbox_input.first.is_checked():
                        # 点击组件 label，让千川自身的选择事件更新「已选」计数。
                        if await checkbox_label.count() > 0:
                            await checkbox_label.click()
                        else:
                            await checkbox_input.first.evaluate("element => element.click()")
                    if not await checkbox_input.first.is_checked():
                        return f"{scene_label}素材 {mid} 未被选中"
                except Exception:
                    return f"{scene_label}素材 {mid} 的选择框无法勾选"
            else:
                checkbox_inner = material_row.locator(
                    'div[class*="ovui-checkbox__inner"]'
                )
                if await checkbox_inner.count() < 1:
                    return f"{scene_label}素材 {mid} 没有可用的选择框"
                await checkbox_inner.first.click(force=True)

            try:
                await page.wait_for_function(
                    """expected => {
                        const text = document.body ? document.body.innerText : "";
                        const match = text.match(/已选\\s*(\\d+)\\s*\\/\\s*20/);
                        return !!match && Number(match[1]) >= Number(expected);
                    }""",
                    arg=selected_count,
                    timeout=min(self._opt.goto_timeout_ms, 15_000),
                )
            except Exception:
                return f"{scene_label}素材 {mid} 勾选后未进入已选列表"

        confirm = selector_modal.get_by_role("button", name="确定", exact=True)
        if not await self._click_last_visible(confirm):
            return f"{scene_label}素材选择器未找到「确定」按钮"
        form_total = int(expected_total or len(ids))
        try:
            await selector_modal.wait_for(
                state="hidden",
                timeout=min(self._opt.goto_timeout_ms, 15_000),
            )
            await page.wait_for_function(
                """expected => {
                    const text = document.body ? document.body.innerText : "";
                    const match = text.match(/已添加[：:]\\s*(\\d+)\\s*\\/\\s*20/);
                    return !!match && Number(match[1]) === Number(expected);
                }""",
                arg=form_total,
                timeout=min(self._opt.goto_timeout_ms, 15_000),
            )
        except Exception:
            return f"{form_total}条{scene_label}素材的选择结果未写入追投表单"
        return None

    async def _select_product_material(
        self,
        page: Page,
        material_id: str,
    ) -> Optional[str]:
        """兼容单素材调用。"""
        return await self._select_product_materials(page, [material_id])

    async def _search_material_and_open_dialog(self, page: Page, material_id: str) -> Optional[str]:
        mid = str(material_id).strip()
        search_input = await page.query_selector(
            'div[class*="oc-filter-input-filter"] >> input[class*="ovui-input"]'
        )
        if search_input:
            try:
                await search_input.fill(mid)
                await search_input.press("Enter")
                await asyncio.sleep(random.uniform(1.0, 2.0))
            except Exception:
                await search_input.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(mid, delay=50)
                await page.keyboard.press("Enter")
                await asyncio.sleep(random.uniform(1.0, 2.0))
        else:
            return "未找到素材搜索框"
        await asyncio.sleep(random.uniform(1.0, 2.0))

        row_sel = (
            f'div[class*="ovui-table__body-wrapper"] >> div[class*="oc-promotion-media-group-info"] '
            f'>> div[class*="info-id"]:has-text("{mid}")'
        )
        row_sel2 = (
            f'div[class*="ovui-table__body-wrapper"] >> td[class*="ovui-table-cell"][class*="ovui-td"] '
            f'>> div[class*="oc-promotion-media-group-info"]:has-text("{mid}")'
        )
        is_item = await page.query_selector(row_sel) or await page.query_selector(row_sel2)
        if not is_item:
            return f"未找到素材 ID：{mid}"

        disabled = await page.query_selector(
            f'tr[class*="ovui-tr"]:has(div[class*="info-id"]:has-text("{mid}")) '
            f'>> div[class*="oc-promotion-operation-wrapper"] '
            f'>> span[class*="oc-promotion-operation-disabled-item"]:has-text("追投")'
        )
        if disabled:
            return f"素材 {mid} 当前不可追投（按钮禁用）"

        apply_btn = await page.query_selector(
            f'tr[class*="ovui-tr"]:has(div[class*="info-id"]:has-text("{mid}")) '
            f'>> div[class*="oc-promotion-operation-wrapper"] '
            f'>> span[class*="oc-promotion-operation-action-item"]:has-text("追投")'
        )
        if not apply_btn:
            return f"未找到素材 {mid} 的「追投」操作按钮"
        await apply_btn.hover()
        await apply_btn.click()
        await asyncio.sleep(random.uniform(0.5, 1.0))
        return None

    async def _run_volume(self, page: Page, r: Dict[str, Any]) -> Optional[str]:
        vol = r.get("volume") or {}
        total_budget = vol.get("total_budget_yuan")
        duration_hours = vol.get("duration_hours")
        suffix = _resolve_task_name_suffix(r)

        err = await self._click_radio_option(page, "追投方式", "放量追投")
        if err:
            return err
        e2 = await self._fill_labeled_number_input(page, "调控总预算", total_budget)
        if e2:
            return e2
        e3 = await self._fill_labeled_number_input(page, "调控时长", duration_hours)
        if e3:
            return e3
        e4 = await self._append_task_name_suffix(page, suffix)
        if e4:
            return e4
        return None

    async def _run_product_volume(
        self,
        page: Page,
        r: Dict[str, Any],
    ) -> Optional[str]:
        """填写商品全域素材追投表单；实际页面只有预算、时长和任务名称。"""
        vol = r.get("volume") or {}
        e1 = await self._fill_labeled_number_input(
            page,
            "调控预算",
            vol.get("total_budget_yuan"),
        )
        if e1:
            return e1
        e2 = await self._fill_labeled_number_input(
            page,
            "调控时长",
            vol.get("duration_hours"),
        )
        if e2:
            return e2
        e3 = await self._append_task_name_suffix(
            page,
            _resolve_task_name_suffix(r),
        )
        if e3:
            return e3
        return None

    async def _probe_product_volume_form_structure(
        self,
        page: Page,
    ) -> Optional[str]:
        """只读确认商品放量追投所需完整表单结构；不填写、不点击提交。"""
        return await self._probe_form_inputs_and_submit(
            page,
            ("调控预算", "调控时长", "任务名称"),
            scene_label="商品",
        )

    async def _probe_form_inputs_and_submit(
        self,
        page: Page,
        labels: Tuple[str, ...],
        *,
        scene_label: str,
    ) -> Optional[str]:
        """只读检查指定输入框与提交按钮的可见可用结构。"""
        for label in labels:
            selector = (
                f'div[class*="oc-row"]:has(span:has-text("{label}")) '
                f'>> div[class*="oc-input-group-wrap"] '
                f'>> input[class*="ovui-input"]'
            )
            element = await page.query_selector(selector)
            if not element:
                return f"{scene_label}追投表单未找到输入框：{label}"
            try:
                if not await element.is_visible():
                    return f"{scene_label}追投表单输入框不可见：{label}"
                if await element.is_disabled():
                    return f"{scene_label}追投表单输入框不可用：{label}"
            except Exception:
                return f"{scene_label}追投表单输入框状态无法确认：{label}"

        submit = await self._find_visible_button(page, "提交")
        if submit is None:
            return f"{scene_label}追投表单未找到提交按钮"
        return None

    async def _probe_live_retarget_form_structure(
        self,
        page: Page,
    ) -> Optional[str]:
        """只读切换直播追投表单各模式并验证字段；绝不填写或提交。"""
        error = await self._click_radio_option(page, "追投方式", "放量追投")
        if error:
            return error
        error = await self._probe_form_inputs_and_submit(
            page,
            ("调控总预算", "调控时长", "任务名称"),
            scene_label="直播放量",
        )
        if error:
            return error

        error = await self._click_radio_option(page, "追投方式", "控成本追投")
        if error:
            return error
        error = await self._click_radio_option(page, "优化目标", "净成交ROI")
        if error:
            return error
        error = await self._probe_form_inputs_and_submit(
            page,
            ("调控日预算", "净成交ROI目标", "任务名称"),
            scene_label="直播控成本",
        )
        if error:
            return error

        error = await self._click_radio_option(page, "优化目标", "直播间成交")
        if error:
            return error
        error = await self._probe_form_inputs_and_submit(
            page,
            ("调控日预算", "我的出价", "任务名称"),
            scene_label="直播控成本",
        )
        if error:
            return error
        return None

    async def _run_cost_control(self, page: Page, r: Dict[str, Any]) -> Optional[str]:
        cc = r.get("cost_control") or {}
        og = str(cc.get("optimization_goal") or "net_roi").strip().lower()
        suffix = _resolve_task_name_suffix(r)

        err = await self._click_radio_option(page, "追投方式", "控成本追投")
        if err:
            return err

        if og == "net_roi":
            nr = cc.get("net_roi") or {}
            e0 = await self._click_radio_option(page, "优化目标", "净成交ROI")
            if e0:
                return e0
            e1 = await self._fill_labeled_number_input(page, "调控日预算", nr.get("daily_budget_yuan"))
            if e1:
                return e1
            e2 = await self._fill_labeled_number_input(page, "净成交ROI目标", nr.get("net_roi_target"))
            if e2:
                return e2
        else:
            lr = cc.get("live_room") or {}
            e0 = await self._click_radio_option(page, "优化目标", "直播间成交")
            if e0:
                return e0
            e1 = await self._fill_labeled_number_input(page, "调控日预算", lr.get("daily_budget_yuan"))
            if e1:
                return e1
            e2 = await self._fill_labeled_number_input(page, "我的出价", lr.get("bid_per_conversion_yuan"))
            if e2:
                return e2

        e4 = await self._append_task_name_suffix(page, suffix)
        if e4:
            return e4
        return None

    async def run(
        self,
        *,
        aavid: int,
        ad_id: int,
        material_id: str,
        retargeting: Dict[str, Any],
        material_ids: Optional[List[str]] = None,
        strategy_title: Optional[str] = None,
        target_uid: Optional[str] = None,
        promotion_scene: str = "live",
        plan_system: str = "unknown",
        source_url: Optional[str] = None,
        reuse_session: bool = False,
        close_session: bool = True,
    ) -> RetargetingRunResult:
        """
        执行一次追投；**始终**返回 RetargetingRunResult（含 message、detail、finished_at、to_log_dict()）。

        :param retargeting: 与 data/rule_retargeting.json 中 retargeting 节点结构一致。
        :param strategy_title: 用于日志前缀 [策略名 追投]；不传则默认「策略」。
        :param reuse_session: True 时跳过打开详情页/切 Tab（同 aavid+ad_id 的上一条已打开浏览器时）。
        :param close_session: False 时不关闭浏览器，供同一策略本轮内连续多条素材复用；由调用方最后 close()。
        """
        self._log_tag = retarget_log_tag(strategy_title=strategy_title, immediate=False)
        rdict = retargeting if isinstance(retargeting, dict) else {}
        batch_material_ids: List[str] = []
        for raw_id in material_ids or [material_id]:
            mid = str(raw_id or "").strip()
            if mid and mid not in batch_material_ids:
                batch_material_ids.append(mid)
        if not batch_material_ids and str(material_id or "").strip():
            batch_material_ids.append(str(material_id).strip())
        material_id = batch_material_ids[0] if batch_material_ids else ""
        try:
            scene = normalize_scene(promotion_scene or "live")
        except ValueError as e:
            return self._make_result(
                success=False,
                message=str(e),
                step="validate_scene",
                retargeting=rdict,
                aavid=aavid,
                ad_id=ad_id,
                material_id=material_id,
            )
        system = normalize_plan_system(plan_system or "unknown")
        if system == "unknown":
            return self._make_result(
                success=False,
                message="计划体系未确认，已安全停止追投",
                step="validate_plan_system",
                retargeting=rdict,
                aavid=aavid,
                ad_id=ad_id,
                material_id=material_id,
            )

        vmsg = self._validate_retargeting_payload(rdict, scene)
        if not vmsg and not batch_material_ids:
            vmsg = "追投任务缺少素材"
        if not vmsg and len(batch_material_ids) > 20:
            vmsg = "单次追投最多支持20条素材"
        if vmsg:
            return self._make_result(
                success=False,
                message=vmsg,
                step="validate",
                retargeting=rdict,
                aavid=aavid,
                ad_id=ad_id,
                material_id=material_id,
            )

        fetch_url = ""
        try:
            fetch_url = build_qianchuan_url_by_params(
                base_url=self._opt.base_url,
                aavid=int(aavid),
                ad_id=int(ad_id),
                promotion_scene=scene,
                source_url=source_url,
            )
        except Exception as e:
            return self._make_result(
                success=False,
                message="构建投放详情 URL 失败",
                detail=str(e),
                step="build_url",
                retargeting=rdict,
                aavid=aavid,
                ad_id=ad_id,
                material_id=material_id,
            )

        await self._ensure_browser()
        try:
            page = self.page
            if not page:
                return self._make_result(
                    success=False,
                    message="页面未初始化",
                    detail="ensure_browser 后 self.page 仍为空",
                    step="browser",
                    retargeting=rdict,
                    aavid=aavid,
                    ad_id=ad_id,
                    material_id=material_id,
                )

            pop_handlers: List[Any] = []
            try:
                if not reuse_session:
                    pop_handlers = await self._attach_popup_switcher()
                    logger.info("%s 正在打开投放详情页", self._log_tag)
                    if scene == "product":
                        target_error = await goto_and_confirm_product_target(
                            page,
                            fetch_url,
                            expected_aavid=aavid,
                            expected_ad_id=ad_id,
                            expected_plan_system=system,
                            timeout_ms=self._opt.goto_timeout_ms,
                        )
                        if target_error:
                            return self._make_result(
                                success=False,
                                message=target_error,
                                step="target_mismatch",
                                retargeting=rdict,
                                aavid=aavid,
                                ad_id=ad_id,
                                material_id=material_id,
                            )
                    else:
                        await page.goto(
                            fetch_url,
                            wait_until="domcontentloaded",
                            timeout=self._opt.goto_timeout_ms,
                        )
                    await asyncio.sleep(random.uniform(2.5, 4.0))

                    page_aavid, page_ad_id = extract_target_ids(page.url)
                    if scene != "product" and (
                        str(page_aavid or "") != str(aavid)
                        or str(page_ad_id or "") != str(ad_id)
                    ):
                        return self._make_result(
                            success=False,
                            message="打开后的账户或计划与任务不一致，已安全停止",
                            step="target_mismatch",
                            retargeting=rdict,
                            aavid=aavid,
                            ad_id=ad_id,
                            material_id=material_id,
                        )

                    if scene != "product":
                        system_error = await confirm_live_page_plan_system(
                            page,
                            expected_plan_system=system,
                            aavid=aavid,
                            ad_id=ad_id,
                        )
                        if system_error:
                            return self._make_result(
                                success=False,
                                message=system_error,
                                step="plan_system_mismatch",
                                retargeting=rdict,
                                aavid=aavid,
                                ad_id=ad_id,
                                material_id=material_id,
                            )
                        err = await self._switch_to_video_tab(scene)
                        if err:
                            return self._make_result(
                                success=False,
                                message=err,
                                step="switch_to_video_tab",
                                retargeting=rdict,
                                aavid=aavid,
                                ad_id=ad_id,
                                material_id=material_id,
                            )
                else:
                    await self._dismiss_dialog_before_next_material(page)
                    logger.info("%s 复用浏览器会话，处理下一条素材", self._log_tag)

                if scene == "product":
                    err = await self._open_product_retarget_dialog(page, ad_id)
                    if not err:
                        err = await self._select_product_materials(page, batch_material_ids)
                else:
                    err = await self._search_material_and_open_dialog(page, material_id)
                    if not err and len(batch_material_ids) > 1:
                        err = await self._select_product_materials(
                            page,
                            batch_material_ids[1:],
                            scene_label="直播",
                            expected_total=len(batch_material_ids),
                        )
                if err:
                    return self._make_result(
                        success=False,
                        message=err,
                        step="search_material",
                        retargeting=rdict,
                        aavid=aavid,
                        ad_id=ad_id,
                        material_id=material_id,
                    )

                method = str(rdict.get("method") or "volume").strip().lower()
                if method == "volume":
                    if scene == "product":
                        err2 = await self._run_product_volume(page, rdict)
                    else:
                        err2 = await self._run_volume(page, rdict)
                else:
                    err2 = await self._run_cost_control(page, rdict)

                if err2:
                    return self._make_result(
                        success=False,
                        message=err2,
                        step="fill_or_submit",
                        retargeting=rdict,
                        aavid=aavid,
                        ad_id=ad_id,
                        material_id=material_id,
                    )

                if scene == "live":
                    live_gate_error = await self._confirm_live_target_delivering(
                        page,
                        expected_aavid=aavid,
                        expected_ad_id=ad_id,
                    )
                    if live_gate_error:
                        return self._make_result(
                            success=False,
                            message=live_gate_error,
                            step="live_delivery_recheck",
                            retargeting=rdict,
                            aavid=aavid,
                            ad_id=ad_id,
                            material_id=material_id,
                        )
                else:
                    product_gate_error = (
                        await self._confirm_product_target_delivering(
                            page,
                            expected_aavid=aavid,
                            expected_ad_id=ad_id,
                            expected_plan_system=system,
                        )
                    )
                    if product_gate_error:
                        return self._make_result(
                            success=False,
                            message=product_gate_error,
                            step="product_delivery_recheck",
                            retargeting=rdict,
                            aavid=aavid,
                            ad_id=ad_id,
                            material_id=material_id,
                        )

                api_err, api_detail, rid = await self._click_submit_and_wait_assist(page)
                if api_err:
                    return self._make_result(
                        success=False,
                        message=api_err,
                        detail=api_detail,
                        step="submit_api",
                        retargeting=rdict,
                        aavid=aavid,
                        ad_id=ad_id,
                        material_id=material_id,
                        regulate_task_id=rid,
                    )

                return self._make_result(
                    success=True,
                    message="追投成功",
                    detail="",
                    step="done",
                    retargeting=rdict,
                    aavid=aavid,
                    ad_id=ad_id,
                    material_id=material_id,
                    regulate_task_id=rid,
                )
            except Exception as e:
                logger.exception("%s 执行异常", self._log_tag)
                return self._make_result(
                    success=False,
                    message="执行异常",
                    detail=traceback.format_exc()[:8000],
                    step="exception",
                    retargeting=rdict,
                    aavid=aavid,
                    ad_id=ad_id,
                    material_id=material_id,
                )
            finally:
                self._detach_popup_switcher(pop_handlers)
        finally:
            if close_session:
                await self.close()

    async def run_prepare_for_manual_submit(
        self,
        *,
        aavid: int,
        ad_id: int,
        material_id: str,
        retargeting: Dict[str, Any],
        strategy_title: Optional[str] = None,
        target_uid: Optional[str] = None,
        promotion_scene: str = "live",
        plan_system: str = "unknown",
        source_url: Optional[str] = None,
    ) -> RetargetingRunResult:
        """
        打开投放页、搜索素材并填好追投表单；**不由程序**点击「提交」。
        使用 wait_for_response 等待用户手动点击后触发的 create-uni-prom-assist-task 响应，
        解析调控任务 id；超时、接口失败与 run() 一致。结束时 close() 浏览器（与 run() 相同生命周期）。
        日志前缀固定为 [即刻追投]（与 strategy_title 无关）。
        """
        self._log_tag = retarget_log_tag(strategy_title=strategy_title, immediate=True)
        rdict = retargeting if isinstance(retargeting, dict) else {}
        try:
            scene = normalize_scene(promotion_scene or "live")
        except ValueError as e:
            return self._make_result(
                success=False,
                message=str(e),
                step="validate_scene",
                retargeting=rdict,
                aavid=aavid,
                ad_id=ad_id,
                material_id=material_id,
            )
        system = normalize_plan_system(plan_system or "unknown")
        if system == "unknown":
            return self._make_result(
                success=False,
                message="计划体系未确认，已安全停止追投",
                step="validate_plan_system",
                retargeting=rdict,
                aavid=aavid,
                ad_id=ad_id,
                material_id=material_id,
            )

        vmsg = self._validate_retargeting_payload(rdict, scene)
        if vmsg:
            return self._make_result(
                success=False,
                message=vmsg,
                step="validate",
                retargeting=rdict,
                aavid=aavid,
                ad_id=ad_id,
                material_id=material_id,
            )

        fetch_url = ""
        try:
            fetch_url = build_qianchuan_url_by_params(
                base_url=self._opt.base_url,
                aavid=int(aavid),
                ad_id=int(ad_id),
                promotion_scene=scene,
                source_url=source_url,
            )
        except Exception as e:
            return self._make_result(
                success=False,
                message="构建投放详情 URL 失败",
                detail=str(e),
                step="build_url",
                retargeting=rdict,
                aavid=aavid,
                ad_id=ad_id,
                material_id=material_id,
            )

        await self._ensure_browser()
        try:
            page = self.page
            if not page:
                return self._make_result(
                    success=False,
                    message="页面未初始化",
                    detail="ensure_browser 后 self.page 仍为空",
                    step="browser",
                    retargeting=rdict,
                    aavid=aavid,
                    ad_id=ad_id,
                    material_id=material_id,
                )

            pop_handlers: List[Any] = []
            try:
                pop_handlers = await self._attach_popup_switcher()
                logger.info("%s 正在打开投放详情页", self._log_tag)
                if scene == "product":
                    target_error = await goto_and_confirm_product_target(
                        page,
                        fetch_url,
                        expected_aavid=aavid,
                        expected_ad_id=ad_id,
                        expected_plan_system=system,
                        timeout_ms=self._opt.goto_timeout_ms,
                    )
                    if target_error:
                        return self._make_result(
                            success=False,
                            message=target_error,
                            step="target_mismatch",
                            retargeting=rdict,
                            aavid=aavid,
                            ad_id=ad_id,
                            material_id=material_id,
                        )
                else:
                    await page.goto(
                        fetch_url,
                        wait_until="domcontentloaded",
                        timeout=self._opt.goto_timeout_ms,
                    )
                await asyncio.sleep(random.uniform(2.5, 4.0))
                page_aavid, page_ad_id = extract_target_ids(page.url)
                if scene != "product" and (
                    str(page_aavid or "") != str(aavid)
                    or str(page_ad_id or "") != str(ad_id)
                ):
                    return self._make_result(
                        success=False,
                        message="打开后的账户或计划与监控目标不一致，已安全停止",
                        detail=f"target={target_uid or ''}",
                        step="target_mismatch",
                        retargeting=rdict,
                        aavid=aavid,
                        ad_id=ad_id,
                        material_id=material_id,
                    )

                if scene != "product":
                    system_error = await confirm_live_page_plan_system(
                        page,
                        expected_plan_system=system,
                        aavid=aavid,
                        ad_id=ad_id,
                    )
                    if system_error:
                        return self._make_result(
                            success=False,
                            message=system_error,
                            step="plan_system_mismatch",
                            retargeting=rdict,
                            aavid=aavid,
                            ad_id=ad_id,
                            material_id=material_id,
                        )
                    err = await self._switch_to_video_tab(scene)
                    if err:
                        return self._make_result(
                            success=False,
                            message=err,
                            step="switch_to_video_tab",
                            retargeting=rdict,
                            aavid=aavid,
                            ad_id=ad_id,
                            material_id=material_id,
                        )

                if scene == "product":
                    err = await self._open_product_retarget_dialog(page, ad_id)
                    if not err:
                        err = await self._select_product_material(page, material_id)
                else:
                    err = await self._search_material_and_open_dialog(page, material_id)
                if err:
                    return self._make_result(
                        success=False,
                        message=err,
                        step="search_material",
                        retargeting=rdict,
                        aavid=aavid,
                        ad_id=ad_id,
                        material_id=material_id,
                    )

                method = str(rdict.get("method") or "volume").strip().lower()
                if method == "volume":
                    if scene == "product":
                        err2 = await self._run_product_volume(page, rdict)
                    else:
                        err2 = await self._run_volume(page, rdict)
                else:
                    err2 = await self._run_cost_control(page, rdict)

                if err2:
                    return self._make_result(
                        success=False,
                        message=err2,
                        step="fill_or_submit",
                        retargeting=rdict,
                        aavid=aavid,
                        ad_id=ad_id,
                        material_id=material_id,
                    )

                api_err, api_detail, rid = await self._wait_assist_task_response_user_submit(page)
                if api_err:
                    return self._make_result(
                        success=False,
                        message=api_err,
                        detail=api_detail,
                        step="submit_api",
                        retargeting=rdict,
                        aavid=aavid,
                        ad_id=ad_id,
                        material_id=material_id,
                        regulate_task_id=rid,
                    )

                return self._make_result(
                    success=True,
                    message="追投成功",
                    detail="",
                    step="done",
                    retargeting=rdict,
                    aavid=aavid,
                    ad_id=ad_id,
                    material_id=material_id,
                    regulate_task_id=rid,
                )
            except Exception:
                logger.exception("%s 执行异常", self._log_tag)
                return self._make_result(
                    success=False,
                    message="执行异常",
                    detail=traceback.format_exc()[:8000],
                    step="exception",
                    retargeting=rdict,
                    aavid=aavid,
                    ad_id=ad_id,
                    material_id=material_id,
                )
            finally:
                self._detach_popup_switcher(pop_handlers)
        finally:
            await self.close()

    async def probe_product_retarget_capability(
        self,
        *,
        aavid: int,
        ad_id: int,
        material_id: str,
        material_ids: Optional[List[str]] = None,
        target_uid: Optional[str] = None,
        promotion_scene: str = "product",
        plan_system: str = "unknown",
        source_url: Optional[str] = None,
    ) -> RetargetingRunResult:
        """只读验证指定直播/商品计划的完整追投表单，绝不填写或提交。"""
        try:
            scene = normalize_scene(promotion_scene or "product")
        except ValueError as exc:
            return self._make_result(
                success=False,
                message=str(exc),
                step="validate_scene",
                aavid=aavid,
                ad_id=ad_id,
                material_id=str(material_id or "").strip(),
            )
        system = normalize_plan_system(plan_system or "unknown")
        if system == "unknown":
            return self._make_result(
                success=False,
                message="计划体系未确认，无法验证追投能力",
                step="validate_plan_system",
                aavid=aavid,
                ad_id=ad_id,
                material_id=str(material_id or "").strip(),
            )
        scene_label = "商品" if scene == "product" else "直播"
        self._log_tag = f"[{scene_label}追投能力验证]"
        probe_material_ids: List[str] = []
        for raw_id in material_ids or [material_id]:
            value = str(raw_id or "").strip()
            if value and value not in probe_material_ids:
                probe_material_ids.append(value)
        mid = probe_material_ids[0] if probe_material_ids else ""
        if not mid:
            return self._make_result(
                success=False,
                message="能力验证缺少素材ID",
                step="validate",
                aavid=aavid,
                ad_id=ad_id,
                material_id=mid,
            )
        try:
            fetch_url = build_qianchuan_url_by_params(
                base_url=self._opt.base_url,
                aavid=int(aavid),
                ad_id=int(ad_id),
                promotion_scene=scene,
                source_url=source_url,
            )
        except Exception as exc:
            return self._make_result(
                success=False,
                message=f"构建{scene_label}计划地址失败",
                detail=str(exc),
                step="build_url",
                aavid=aavid,
                ad_id=ad_id,
                material_id=mid,
            )

        try:
            await self._ensure_browser()
            page = self.page
            if not page:
                return self._make_result(
                    success=False,
                    message="能力验证浏览器未初始化",
                    step="browser",
                    aavid=aavid,
                    ad_id=ad_id,
                    material_id=mid,
                )
            pop_handlers: List[Any] = []
            try:
                pop_handlers = await self._attach_popup_switcher()
                if scene == "product":
                    target_error = await goto_and_confirm_product_target(
                        page,
                        fetch_url,
                        expected_aavid=aavid,
                        expected_ad_id=ad_id,
                        expected_plan_system=system,
                        timeout_ms=self._opt.goto_timeout_ms,
                    )
                    if target_error:
                        return self._make_result(
                            success=False,
                            message=target_error,
                            step="target_mismatch",
                            aavid=aavid,
                            ad_id=ad_id,
                            material_id=mid,
                        )
                else:
                    await page.goto(
                        fetch_url,
                        wait_until="domcontentloaded",
                        timeout=self._opt.goto_timeout_ms,
                    )
                    page_aavid, page_ad_id = extract_target_ids(page.url)
                    if (
                        str(page_aavid or "") != str(aavid)
                        or str(page_ad_id or "") != str(ad_id)
                    ):
                        return self._make_result(
                            success=False,
                            message="打开后的账户或计划与能力验证目标不一致",
                            step="target_mismatch",
                            aavid=aavid,
                            ad_id=ad_id,
                            material_id=mid,
                        )
                    system_error = await confirm_live_page_plan_system(
                        page,
                        expected_plan_system=system,
                        aavid=aavid,
                        ad_id=ad_id,
                    )
                    if system_error:
                        return self._make_result(
                            success=False,
                            message=system_error,
                            step="plan_system_mismatch",
                            aavid=aavid,
                            ad_id=ad_id,
                            material_id=mid,
                        )
                await asyncio.sleep(random.uniform(1.0, 1.8))
                if scene == "product":
                    error = await self._open_product_retarget_dialog(page, ad_id)
                    if not error:
                        error = await self._select_product_materials(
                            page,
                            probe_material_ids,
                        )
                    if not error:
                        error = await self._probe_product_volume_form_structure(
                            page
                        )
                else:
                    error = await self._switch_to_video_tab(scene)
                    if not error:
                        error = await self._search_material_and_open_dialog(
                            page,
                            mid,
                        )
                    if not error and len(probe_material_ids) > 1:
                        error = await self._select_product_materials(
                            page,
                            probe_material_ids[1:],
                            scene_label="直播",
                            expected_total=len(probe_material_ids),
                        )
                    if not error:
                        error = await self._probe_live_retarget_form_structure(
                            page
                        )
                if error:
                    return self._make_result(
                        success=False,
                        message=error,
                        step="capability_probe",
                        aavid=aavid,
                        ad_id=ad_id,
                        material_id=mid,
                    )
                return self._make_result(
                    success=True,
                    message=(
                        f"{scene_label}{'放量' if scene == 'product' else '放量及控成本'}"
                        "追投完整表单能力验证通过（未填写、未点击提交）"
                    ),
                    step="capability_probe",
                    aavid=aavid,
                    ad_id=ad_id,
                    material_id=mid,
                )
            finally:
                self._detach_popup_switcher(pop_handlers)
        except Exception:
            logger.exception("%s 验证异常", self._log_tag)
            return self._make_result(
                success=False,
                message=f"{scene_label}追投表单能力验证异常",
                detail=traceback.format_exc()[:8000],
                step="exception",
                aavid=aavid,
                ad_id=ad_id,
                material_id=mid,
            )
        finally:
            await self.close()

    @classmethod
    def from_rule_file_dict(
        cls,
        full_config: Dict[str, Any],
        *,
        storage_state_override: Any = None,
        base_url: Optional[str] = None,
    ) -> "QianChuanRetargetingService":
        import config as runtime_config

        if runtime_config.QIANCHUAN_BACKEND == "official_api":
            from services.official_api_execution import OfficialApiRetargetingService

            return OfficialApiRetargetingService(full_config)  # type: ignore[return-value]
        headless = bool(full_config.get("browser_headless", True))
        be = str(full_config.get("browser_executable_path") or "").strip()
        opt = RetargetingSessionOptions(
            headless=headless,
            storage_state=storage_state_override,
            base_url=base_url or DEFAULT_BASE_URL,
            browser_executable_path=be if be else None,
        )
        return cls(opt)


def retargeting_block_from_full_config(full_config: Dict[str, Any]) -> Dict[str, Any]:
    """从完整 JSON 中取出 retargeting 节点（不存在则空对象）。"""
    r = full_config.get("retargeting")
    return r if isinstance(r, dict) else {}
