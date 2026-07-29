# -*- coding: utf-8 -*-
"""
千川「规则化停投」Playwright：在投放详情页「素材-视频」列表中按 assist_task_id 定位调控任务并执行暂停或删除（见 dev_files/qianchuan_login_test.py 选择器）。
成功判定以 batch_update_operation / batch_delete_operation 响应为准。
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
from typing import Any, Callable, Dict, List, Optional, Tuple

from playwright.async_api import Browser, BrowserContext, Page, Response, async_playwright

from config import DATA_DIR
from services.fetcher import build_qianchuan_url_by_params
from services.product_scene_adapter import goto_and_confirm_product_target
from services.plan_system import (
    confirm_live_page_plan_system,
    normalize_plan_system,
)
from services.promotion_capability import REGULATION_MANUAL_PROBE_VERSION
from api.promotion_targets import extract_target_ids, normalize_scene
from utils.common import require_executable_path
from utils.log import logger

DEFAULT_BASE_URL = "https://qianchuan.jinritemai.com/uni-prom/detail"

BATCH_UPDATE_SUBSTRING = "/ad/api/pmc/v1/batch_update_operation"
BATCH_DELETE_SUBSTRING = "/ad/api/pmc/v1/batch_delete_operation"
# 手动可见浏览器完成一次 batch 操作并校验响应后写入目标级停投能力证据。
REGULATION_PROBE_VERSION = REGULATION_MANUAL_PROBE_VERSION


def regulation_log_tag(*, strategy_title: Optional[str] = None, scheduler: bool = False) -> str:
    if scheduler:
        return "[规则化停投]"
    t = (strategy_title or "").strip() or "策略"
    if len(t) > 64:
        t = t[:64]
    return f"[{t} 停投]"


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


def _default_storage_state_path() -> Optional[str]:
    cand = os.path.join(DATA_DIR, "qcookie.json")
    return cand if os.path.isfile(cand) else None


def _is_batch_update_response(resp: Response) -> bool:
    try:
        return BATCH_UPDATE_SUBSTRING in (resp.url or "")
    except Exception:
        return False


def _is_batch_delete_response(resp: Response) -> bool:
    try:
        return BATCH_DELETE_SUBSTRING in (resp.url or "")
    except Exception:
        return False


def _parse_batch_operation_json(body: Any, expect_object_id: str) -> Tuple[Optional[str], str]:
    """成功：(None, '')；失败：(短消息, detail)。"""
    oid = str(expect_object_id or "").strip()
    if not isinstance(body, dict):
        raw = _safe_json(body) if body is not None else ""
        return ("接口响应格式异常", (raw[:4000] if raw else repr(body))[:8000])
    try:
        sc = int(body.get("status_code"))
    except (TypeError, ValueError):
        sc = None
    msg = body.get("message")
    if sc != 0:
        return ("接口返回失败", f"status_code={sc!r} message={msg!r}\n{_safe_json(body)[:4000]}")
    if str(msg) != "success":
        return ("接口返回失败", f"message={msg!r}\n{_safe_json(body)[:4000]}")
    data = body.get("data")
    results = None
    if isinstance(data, dict):
        results = data.get("results")
    if not isinstance(results, list) or not results:
        return ("接口 data.results 为空", _safe_json(body)[:4000])
    r0 = results[0]
    if not isinstance(r0, dict):
        return ("接口 results[0] 非法", _safe_json(body)[:4000])
    if not r0.get("flag"):
        return ("接口返回 flag=false", _safe_json(body)[:4000])
    obj = r0.get("object")
    if isinstance(obj, dict):
        got = str(obj.get("objectId") or "").strip()
        if oid and got and got != oid:
            return ("接口 objectId 与预期不一致", f"expect={oid!r} got={got!r}\n{_safe_json(body)[:2000]}")
    return None, ""


@dataclass
class RegulationSessionOptions:
    headless: bool = True
    storage_state: Optional[str] = None
    base_url: str = DEFAULT_BASE_URL
    browser_executable_path: Optional[str] = None
    goto_timeout_ms: int = 60_000


@dataclass
class RegulationRunResult:
    success: bool
    message: str
    step: str
    detail: str = ""
    aavid: str = ""
    ad_id: str = ""
    assist_task_id: str = ""
    stop_action: str = ""
    finished_at: str = ""
    headless: bool = False

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "message": self.message,
            "detail": self.detail,
            "step": self.step,
            "aavid": self.aavid,
            "ad_id": self.ad_id,
            "assist_task_id": self.assist_task_id,
            "stop_action": self.stop_action,
            "finished_at": self.finished_at,
            "headless": self.headless,
        }

    def asdict(self) -> Dict[str, Any]:
        return asdict(self)


class QianChuanRegulationStopService:
    def __init__(self, options: Optional[RegulationSessionOptions] = None):
        self._opt = options or RegulationSessionOptions()
        ss = self._opt.storage_state
        self._storage_state = _default_storage_state_path() if ss is None else _resolve_path(ss)
        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._log_tag = regulation_log_tag()

    def _make_result(
        self,
        *,
        success: bool,
        message: str,
        step: str,
        detail: str = "",
        aavid: Any = "",
        ad_id: Any = "",
        assist_task_id: str = "",
        stop_action: str = "",
    ) -> RegulationRunResult:
        return RegulationRunResult(
            success=success,
            message=message,
            step=step,
            detail=str(detail or "").strip()[:8000],
            aavid=str(aavid).strip() if aavid is not None else "",
            ad_id=str(ad_id).strip() if ad_id is not None else "",
            assist_task_id=str(assist_task_id or "").strip(),
            stop_action=str(stop_action or "").strip().lower(),
            finished_at=_now_ts(),
            headless=bool(self._opt.headless),
        )

    async def _switch_to_assist_tab(self) -> Optional[str]:
        page = self.page
        if not page:
            return "页面不存在"
        try:
            assist_tab = await page.query_selector(
                'div[class*="ovui-tabs__nav-list"] >> div[class*="oc-new-badge"]:has-text("调控")'
            )
            if assist_tab:
                await assist_tab.click()
                await asyncio.sleep(random.uniform(0.8, 1.5))

            assist_tab = page.locator(
                'div[class*="oc-radio-group-button"] >> div[class*="ovui-radio-item"]:has-text("素材追投")'
            )
            if await assist_tab.count() > 0:
                await assist_tab.last.hover()
                await assist_tab.last.click()
                logger.info("%s 已切换调控选项卡", self._log_tag)
                await asyncio.sleep(random.uniform(2.0, 3.5))
                return None
            return "未找到调控选项卡"
        except Exception as e:
            logger.warning("%s 切换调控选项卡失败: %s", self._log_tag, e)
            return f"切换调控选项卡失败: {e}"

    async def _attach_popup_switcher(self):
        handlers = []

        async def _switch(p: Page) -> None:
            try:
                await p.bring_to_front()
            except Exception:
                pass
            self.page = p

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

    def _detach_popup_switcher(self, handlers) -> None:
        _ = handlers

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

    def _tr_base(self, assist_task_id: str) -> str:
        aid = str(assist_task_id).strip()
        return f'tr[class*="ovui-tr"]:has(div[class*="oc-promotion-bid"]:has-text("{aid}"))'

    _ASSIST_FILTER_INPUT = 'div[class*="oc-filter-input-filter"] >> input[class*="ovui-input"]'

    async def _apply_assist_task_search_filter(
        self, page: Page, assist_task_id: str
    ) -> Optional[str]:
        """切换调控 Tab 后：在列表筛选框输入 task id 并回车，收窄当前表格。成功返回 None，失败返回短错误信息。"""
        aid = str(assist_task_id).strip()
        if not aid:
            return None
        try:
            await page.wait_for_selector(self._ASSIST_FILTER_INPUT, timeout=15_000)
        except Exception:
            pass
        search_input = await page.query_selector(self._ASSIST_FILTER_INPUT)
        if not search_input:
            return "未找到调控搜索框"
        try:
            await search_input.click()
            await asyncio.sleep(random.uniform(0.12, 0.28))
            await search_input.fill("")
            await search_input.fill(aid)
            await search_input.press("Enter")
        except Exception:
            try:
                await search_input.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(aid, delay=45)
                await page.keyboard.press("Enter")
            except Exception as e:
                return f"调控列表搜索输入失败: {e}"
        await asyncio.sleep(random.uniform(2.0, 3.5))
        return None

    async def _assist_task_row_visible(self, page: Page, assist_task_id: str) -> bool:
        """在已按 task id 筛选（或全量）的表格中，判断调控任务 id 是否出现在当前视图。"""
        aid = str(assist_task_id).strip()
        s1 = (
            f'div[class*="ovui-table__body-wrapper"] >> div[class*="oc-promotion-data-group-desc"] '
            f'>> span[class*="oc-typography-value-int"]:has-text("{aid}")'
        )
        s2 = (
            f'div[class*="ovui-table__body-wrapper"] >> '
            f'div[class*="oc-promotion-data-group-desc"]:has-text("{aid}")'
        )
        el = await page.query_selector(s1) or await page.query_selector(s2)
        return el is not None

    async def _query_confirm_button(self, page: Page):
        """弹层「确定」按钮（暂停/删除二次确认共用）。"""
        btn = await page.query_selector(
            'div[class*="oc-button-wrap"] >> button[class*="ovui-button"] >> span:has-text("确定")'
        )
        if not btn:
            btn = await page.query_selector(
                'div[class*="oc-button-wrap"] >> button[class*="ovui-button"]:has-text("确定")'
            )
        return btn

    async def _confirm_submit_batch_operation(
        self,
        page: Page,
        *,
        assist_task_id: str,
        is_batch_response: Callable[[Response], bool],
        timeout_ms: int,
        network_error_message: str,
        api_step: str,
    ) -> Tuple[Optional[str], str, str]:
        """点击「确定」并等待对应 batch 接口响应后解析。(error_msg, detail, step)"""
        confirm_btn = await self._query_confirm_button(page)
        if not confirm_btn:
            return "未找到确定按钮", "", "confirm"
        try:
            async with page.expect_response(is_batch_response, timeout=timeout_ms) as resp_info:
                await confirm_btn.hover()
                await confirm_btn.click()
            resp = await resp_info.value
            body = await resp.json()
        except Exception as e:
            return network_error_message, str(e), api_step
        err_s, det = _parse_batch_operation_json(body, assist_task_id)
        if err_s:
            return err_s, det, api_step
        return None, "", "done"

    async def _run_pause_flow(
        self, page: Page, assist_task_id: str
    ) -> Tuple[Optional[str], str, str]:
        """(error_msg, detail, step)"""
        tr_sel = self._tr_base(assist_task_id)
        tr_el = await page.query_selector(tr_sel)
        if not tr_el:
            return "未找到调控任务行", "", "assist_row"
        await tr_el.hover()

        play = await page.query_selector(
            f'{tr_sel} >> div[class="right"] >> div[class^="oc-popover"] >> iconpark-icon[name*="Play"]'
        )
        if play:
            status_selector = await page.query_selector(
                f'{tr_sel} >> div[class="left"]'
            )
            if status_selector:
                status_selector_text = await status_selector.inner_text()
                if status_selector_text:
                    clean_text = status_selector_text.splitlines()[0].strip()
                    if "调控中" not in clean_text:
                        return f"当前状态为：{clean_text}, 跳过暂停", "", "done_already_paused"
            return "当前不是调控中，跳过暂停", "", "done_already_paused"

        status_selector = await page.query_selector(
            f'{tr_sel} >> div[class="left"]'
        )
        if status_selector:
            status_selector_text = await status_selector.inner_text()
            if status_selector_text:
                clean_text = status_selector_text.splitlines()[0].strip()
                if "调控中" not in clean_text:
                    return f"当前状态为：{clean_text}, 跳过暂停", "", "done_already_paused"

        pause_btn = await page.query_selector(
            f'{tr_sel} >> div[class="right"] >> div[class^="oc-popover"] >> iconpark-icon[name*="Pause"]'
        )
        if not pause_btn:
            return "未找到暂停按钮", "", "pause_btn"

        timeout_ms = min(max(self._opt.goto_timeout_ms, 30_000), 120_000)
        await pause_btn.hover()
        await pause_btn.click()
        await asyncio.sleep(random.uniform(1.0, 2.0))

        return await self._confirm_submit_batch_operation(
            page,
            assist_task_id=assist_task_id,
            is_batch_response=_is_batch_update_response,
            timeout_ms=timeout_ms,
            network_error_message="暂停接口无响应或超时",
            api_step="batch_update_api",
        )

    async def _run_delete_flow(
        self, page: Page, assist_task_id: str
    ) -> Tuple[Optional[str], str, str]:
        tr_sel = self._tr_base(assist_task_id)
        tr_el = await page.query_selector(tr_sel)
        if not tr_el:
            return "未找到调控任务行", "", "assist_row"
        await tr_el.hover()

        play = await page.query_selector(
            f'{tr_sel} >> div[class="right"] >> div[class^="oc-popover"] >> iconpark-icon[name*="Play"]'
        )
        if play:
            status_selector = await page.query_selector(
                f'{tr_sel} >> div[class="left"]'
            )
            if status_selector:
                status_selector_text = await status_selector.inner_text()
                if status_selector_text:
                    clean_text = status_selector_text.splitlines()[0].strip()
                    if "调控中" not in clean_text:
                        return f"当前状态为：{clean_text}, 跳过删除", "", "done_already_paused"
            return "当前不是调控中，跳过删除", "", "done_already_paused"

        status_selector = await page.query_selector(
            f'{tr_sel} >> div[class="left"]'
        )
        if status_selector:
            status_selector_text = await status_selector.inner_text()
            if status_selector_text:
                clean_text = status_selector_text.splitlines()[0].strip()
                if "调控中" not in clean_text:
                    return f"当前状态为：{clean_text}, 跳过删除", "", "done_already_paused"

        del_btn = await page.query_selector(
            f'{tr_sel} >> div[class="right"] >> div[class^="oc-popover"] >> iconpark-icon[name*="delete"]'
        )
        if not del_btn:
            return "未找到删除按钮", "", "delete_btn"

        timeout_ms = min(max(self._opt.goto_timeout_ms, 30_000), 120_000)
        await del_btn.hover()
        await del_btn.click()
        await asyncio.sleep(random.uniform(1.0, 2.0))

        return await self._confirm_submit_batch_operation(
            page,
            assist_task_id=assist_task_id,
            is_batch_response=_is_batch_delete_response,
            timeout_ms=timeout_ms,
            network_error_message="删除接口无响应或超时",
            api_step="batch_delete_api",
        )

    async def run(
        self,
        *,
        aavid: int,
        ad_id: int,
        assist_task_id: str,
        stop_action: str,
        strategy_title: Optional[str] = None,
        target_uid: Optional[str] = None,
        promotion_scene: str = "live",
        plan_system: str = "unknown",
        source_url: Optional[str] = None,
        reuse_session: bool = False,
        close_session: bool = True,
    ) -> RegulationRunResult:
        self._log_tag = regulation_log_tag(strategy_title=strategy_title)
        try:
            scene = normalize_scene(promotion_scene or "live")
        except ValueError as e:
            return self._make_result(
                success=False,
                message=str(e),
                step="validate_scene",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=assist_task_id,
                stop_action=stop_action,
            )
        system = normalize_plan_system(plan_system or "unknown")
        if system == "unknown":
            return self._make_result(
                success=False,
                message="计划体系尚未确认，已安全停止",
                step="validate_plan_system",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=assist_task_id,
                stop_action=stop_action,
            )
        aid = str(assist_task_id).strip()
        act = str(stop_action or "").strip().lower()
        if act not in ("pause", "delete"):
            return self._make_result(
                success=False,
                message="stop_action 须为 pause 或 delete",
                step="validate",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=act,
            )
        if not aid:
            return self._make_result(
                success=False,
                message="assist_task_id 为空，无法停投",
                step="validate",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id="",
                stop_action=act,
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
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=act,
            )

        pop_handlers: List[Any] = []
        try:
            await self._ensure_browser()
            page = self.page
            if not page:
                return self._make_result(
                    success=False,
                    message="页面未初始化",
                    step="browser",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )

            if not reuse_session:
                pop_handlers = await self._attach_popup_switcher()
                logger.info("%s 打开投放详情页", self._log_tag)
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
                            assist_task_id=aid,
                            stop_action=act,
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
                        message="打开后的账户或计划与停投任务不一致，已安全停止",
                        step="target_mismatch",
                        aavid=aavid,
                        ad_id=ad_id,
                        assist_task_id=aid,
                        stop_action=act,
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
                            aavid=aavid,
                            ad_id=ad_id,
                            assist_task_id=aid,
                            stop_action=act,
                        )
                err = await self._switch_to_assist_tab()
                if err:
                    return self._make_result(
                        success=False,
                        message=err,
                        step="switch_to_assist_tab",
                        aavid=aavid,
                        ad_id=ad_id,
                        assist_task_id=aid,
                        stop_action=act,
                    )

            search_err = await self._apply_assist_task_search_filter(page, aid)
            if search_err:
                return self._make_result(
                    success=False,
                    message=search_err,
                    step="search_assist_task",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )

            if not await self._assist_task_row_visible(page, aid):
                return self._make_result(
                    success=False,
                    message=f"调控任务 {aid} 在列表中不存在",
                    step="assist_not_found",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )

            if act == "pause":
                emsg, det, st = await self._run_pause_flow(page, aid)
                if st == "done_already_paused":
                    return self._make_result(
                        success=True,
                        message=emsg,
                        step="done_already_paused",
                        detail=det,
                        aavid=aavid,
                        ad_id=ad_id,
                        assist_task_id=aid,
                        stop_action=act,
                    )
                if emsg:
                    return self._make_result(
                        success=False,
                        message=emsg,
                        detail=det,
                        step=st,
                        aavid=aavid,
                        ad_id=ad_id,
                        assist_task_id=aid,
                        stop_action=act,
                    )
                return self._make_result(
                    success=True,
                    message="已暂停调控任务",
                    step="done",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )

            emsg, det, st = await self._run_delete_flow(page, aid)
            if st == "done_already_paused":
                return self._make_result(
                    success=True,
                    message=emsg,
                    step="done_already_paused",
                    detail=det,
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )
            if emsg:
                return self._make_result(
                    success=False,
                    message=emsg,
                    detail=det,
                    step=st,
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )
            return self._make_result(
                success=True,
                message="已删除调控任务",
                step="done",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=act,
            )
        except Exception as e:
            logger.exception("%s 执行异常", self._log_tag)
            return self._make_result(
                success=False,
                message="执行异常",
                detail=traceback.format_exc()[:8000],
                step="exception",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=act,
            )
        finally:
            self._detach_popup_switcher(pop_handlers)
            if close_session:
                await self.close()

    async def _manual_stop_precheck_pause(
        self,
        page: Page,
        assist_task_id: str,
        *,
        aavid: Any,
        ad_id: Any,
        stop_action: str,
    ) -> Optional[RegulationRunResult]:
        """定位行与状态；已非调控中则返回跳过结果；否则返回 None 表示可等待用户点击暂停。"""
        aid = str(assist_task_id).strip()
        tr_sel = self._tr_base(aid)
        tr_el = await page.query_selector(tr_sel)
        if not tr_el:
            return self._make_result(
                success=False,
                message="未找到调控任务行",
                step="assist_row",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=stop_action,
            )
        await tr_el.hover()

        play = await page.query_selector(
            f'{tr_sel} >> div[class="right"] >> div[class^="oc-popover"] >> iconpark-icon[name*="Play"]'
        )
        if play:
            status_selector = await page.query_selector(f'{tr_sel} >> div[class="left"]')
            if status_selector:
                status_selector_text = await status_selector.inner_text()
                if status_selector_text:
                    clean_text = status_selector_text.splitlines()[0].strip()
                    if "调控中" not in clean_text:
                        return self._make_result(
                            success=True,
                            message=f"当前状态为：{clean_text}, 跳过暂停",
                            step="done_already_paused",
                            aavid=aavid,
                            ad_id=ad_id,
                            assist_task_id=aid,
                            stop_action=stop_action,
                        )
            return self._make_result(
                success=True,
                message="当前不是调控中，跳过暂停",
                step="done_already_paused",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=stop_action,
            )

        status_selector = await page.query_selector(f'{tr_sel} >> div[class="left"]')
        if status_selector:
            status_selector_text = await status_selector.inner_text()
            if status_selector_text:
                clean_text = status_selector_text.splitlines()[0].strip()
                if "调控中" not in clean_text:
                    return self._make_result(
                        success=True,
                        message=f"当前状态为：{clean_text}, 跳过暂停",
                        step="done_already_paused",
                        aavid=aavid,
                        ad_id=ad_id,
                        assist_task_id=aid,
                        stop_action=stop_action,
                    )

        pause_btn = await page.query_selector(
            f'{tr_sel} >> div[class="right"] >> div[class^="oc-popover"] >> iconpark-icon[name*="Pause"]'
        )
        if not pause_btn:
            return self._make_result(
                success=False,
                message="未找到暂停按钮",
                step="pause_btn",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=stop_action,
            )
        return None

    async def _manual_stop_precheck_delete(
        self,
        page: Page,
        assist_task_id: str,
        *,
        aavid: Any,
        ad_id: Any,
        stop_action: str,
    ) -> Optional[RegulationRunResult]:
        aid = str(assist_task_id).strip()
        tr_sel = self._tr_base(aid)
        tr_el = await page.query_selector(tr_sel)
        if not tr_el:
            return self._make_result(
                success=False,
                message="未找到调控任务行",
                step="assist_row",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=stop_action,
            )
        await tr_el.hover()

        play = await page.query_selector(
            f'{tr_sel} >> div[class="right"] >> div[class^="oc-popover"] >> iconpark-icon[name*="Play"]'
        )
        if play:
            status_selector = await page.query_selector(f'{tr_sel} >> div[class="left"]')
            if status_selector:
                status_selector_text = await status_selector.inner_text()
                if status_selector_text:
                    clean_text = status_selector_text.splitlines()[0].strip()
                    if "调控中" not in clean_text:
                        return self._make_result(
                            success=True,
                            message=f"当前状态为：{clean_text}, 跳过删除",
                            step="done_already_paused",
                            aavid=aavid,
                            ad_id=ad_id,
                            assist_task_id=aid,
                            stop_action=stop_action,
                        )
            return self._make_result(
                success=True,
                message="当前不是调控中，跳过删除",
                step="done_already_paused",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=stop_action,
            )

        status_selector = await page.query_selector(f'{tr_sel} >> div[class="left"]')
        if status_selector:
            status_selector_text = await status_selector.inner_text()
            if status_selector_text:
                clean_text = status_selector_text.splitlines()[0].strip()
                if "调控中" not in clean_text:
                    return self._make_result(
                        success=True,
                        message=f"当前状态为：{clean_text}, 跳过删除",
                        step="done_already_paused",
                        aavid=aavid,
                        ad_id=ad_id,
                        assist_task_id=aid,
                        stop_action=stop_action,
                    )

        del_btn = await page.query_selector(
            f'{tr_sel} >> div[class="right"] >> div[class^="oc-popover"] >> iconpark-icon[name*="delete"]'
        )
        if not del_btn:
            return self._make_result(
                success=False,
                message="未找到删除按钮",
                step="delete_btn",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=stop_action,
            )
        return None

    async def _open_manual_confirm_dialog(
        self,
        page: Page,
        assist_task_id: str,
        *,
        stop_action: str,
        aavid: Any,
        ad_id: Any,
    ) -> Optional[RegulationRunResult]:
        """
        在已通过前置校验的前提下，代为点击暂停/删除图标以弹出二次确认层。
        不点击「确定」，由用户在弹窗内自行确认。
        """
        aid = str(assist_task_id).strip()
        act = str(stop_action or "").strip().lower()
        tr_sel = self._tr_base(aid)
        if act == "pause":
            btn = await page.query_selector(
                f'{tr_sel} >> div[class="right"] >> div[class^="oc-popover"] >> iconpark-icon[name*="Pause"]'
            )
            if not btn:
                return self._make_result(
                    success=False,
                    message="未找到暂停按钮",
                    step="pause_btn",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )
            await btn.hover()
            await btn.click()
        elif act == "delete":
            btn = await page.query_selector(
                f'{tr_sel} >> div[class="right"] >> div[class^="oc-popover"] >> iconpark-icon[name*="delete"]'
            )
            if not btn:
                return self._make_result(
                    success=False,
                    message="未找到删除按钮",
                    step="delete_btn",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )
            await btn.hover()
            await btn.click()
        else:
            return self._make_result(
                success=False,
                message="stop_action 须为 pause 或 delete",
                step="validate",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=act,
            )
        await asyncio.sleep(random.uniform(1.0, 2.0))
        return None

    async def run_prepare_for_manual_stop(
        self,
        *,
        aavid: int,
        ad_id: int,
        assist_task_id: str,
        stop_action: str,
        strategy_title: Optional[str] = None,
        target_uid: Optional[str] = None,
        promotion_scene: str = "live",
        plan_system: str = "unknown",
        source_url: Optional[str] = None,
    ) -> RegulationRunResult:
        """
        打开投放详情并定位调控任务；由程序代为点击暂停/删除图标以弹出确认层，
        **不由程序**点击「确定」；使用 expect_response 等待用户确认后触发的
        batch_update_operation / batch_delete_operation；结束时 close() 浏览器。
        """
        self._log_tag = regulation_log_tag(strategy_title=strategy_title or "手动停投")
        try:
            scene = normalize_scene(promotion_scene or "live")
        except ValueError as e:
            return self._make_result(
                success=False,
                message=str(e),
                step="validate_scene",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=assist_task_id,
                stop_action=stop_action,
            )
        system = normalize_plan_system(plan_system or "unknown")
        if system == "unknown":
            return self._make_result(
                success=False,
                message="计划体系尚未确认，已安全停止",
                step="validate_plan_system",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=assist_task_id,
                stop_action=stop_action,
            )
        aid = str(assist_task_id).strip()
        act = str(stop_action or "").strip().lower()
        if act not in ("pause", "delete"):
            return self._make_result(
                success=False,
                message="stop_action 须为 pause 或 delete",
                step="validate",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=act,
            )
        if not aid:
            return self._make_result(
                success=False,
                message="assist_task_id 为空，无法停投",
                step="validate",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id="",
                stop_action=act,
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
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=act,
            )

        pop_handlers: List[Any] = []
        try:
            await self._ensure_browser()
            page = self.page
            if not page:
                return self._make_result(
                    success=False,
                    message="页面未初始化",
                    step="browser",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )

            pop_handlers = await self._attach_popup_switcher()
            logger.info("%s 打开投放详情页（手动停投：程序弹窗，用户点确定）", self._log_tag)
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
                        assist_task_id=aid,
                        stop_action=act,
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
                    message="页面账户或计划与监控目标不一致，已阻止停投",
                    detail=f"expected={aavid}/{ad_id}, actual={page_aavid}/{page_ad_id}, target={target_uid or ''}",
                    step="target_mismatch",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
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
                        aavid=aavid,
                        ad_id=ad_id,
                        assist_task_id=aid,
                        stop_action=act,
                    )
            err = await self._switch_to_assist_tab()
            if err:
                return self._make_result(
                    success=False,
                    message=err,
                    step="switch_to_assist_tab",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )

            search_err = await self._apply_assist_task_search_filter(page, aid)
            if search_err:
                return self._make_result(
                    success=False,
                    message=search_err,
                    step="search_assist_task",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )

            if not await self._assist_task_row_visible(page, aid):
                return self._make_result(
                    success=False,
                    message=f"调控任务 {aid} 在列表中不存在",
                    step="assist_not_found",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )

            if act == "pause":
                pre = await self._manual_stop_precheck_pause(
                    page, aid, aavid=aavid, ad_id=ad_id, stop_action=act
                )
                if pre is not None:
                    return pre
                op = await self._open_manual_confirm_dialog(
                    page, aid, stop_action=act, aavid=aavid, ad_id=ad_id
                )
                if op is not None:
                    return op
                logger.info(
                    "%s 已代为点开暂停确认层，请在弹窗中点击「确定」完成操作",
                    self._log_tag,
                )
                try:
                    # Python Playwright 无 wait_for_response，需用 expect_response + value
                    async with page.expect_response(_is_batch_update_response, timeout=180_000) as resp_info:
                        resp = await resp_info.value
                    body = await resp.json()
                except Exception as e:
                    return self._make_result(
                        success=False,
                        message="暂停接口无响应或超时",
                        detail=str(e),
                        step="batch_update_api",
                        aavid=aavid,
                        ad_id=ad_id,
                        assist_task_id=aid,
                        stop_action=act,
                    )
                err_s, det = _parse_batch_operation_json(body, aid)
                if err_s:
                    return self._make_result(
                        success=False,
                        message=err_s,
                        detail=det,
                        step="batch_update_api",
                        aavid=aavid,
                        ad_id=ad_id,
                        assist_task_id=aid,
                        stop_action=act,
                    )
                return self._make_result(
                    success=True,
                    message="已暂停调控任务",
                    step="done",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )

            pre_d = await self._manual_stop_precheck_delete(
                page, aid, aavid=aavid, ad_id=ad_id, stop_action=act
            )
            if pre_d is not None:
                return pre_d
            op_d = await self._open_manual_confirm_dialog(
                page, aid, stop_action=act, aavid=aavid, ad_id=ad_id
            )
            if op_d is not None:
                return op_d
            logger.info(
                "%s 已代为点开删除确认层，请在弹窗中点击「确定」完成操作",
                self._log_tag,
            )
            try:
                async with page.expect_response(_is_batch_delete_response, timeout=180_000) as resp_info:
                    resp = await resp_info.value
                body = await resp.json()
            except Exception as e:
                return self._make_result(
                    success=False,
                    message="删除接口无响应或超时",
                    detail=str(e),
                    step="batch_delete_api",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )
            err_s, det = _parse_batch_operation_json(body, aid)
            if err_s:
                return self._make_result(
                    success=False,
                    message=err_s,
                    detail=det,
                    step="batch_delete_api",
                    aavid=aavid,
                    ad_id=ad_id,
                    assist_task_id=aid,
                    stop_action=act,
                )
            return self._make_result(
                success=True,
                message="已删除调控任务",
                step="done",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=act,
            )
        except Exception:
            logger.exception("%s 执行异常", self._log_tag)
            return self._make_result(
                success=False,
                message="执行异常",
                detail=traceback.format_exc()[:8000],
                step="exception",
                aavid=aavid,
                ad_id=ad_id,
                assist_task_id=aid,
                stop_action=act,
            )
        finally:
            self._detach_popup_switcher(pop_handlers)
            await self.close()

    @classmethod
    def from_rule_file_dict(
        cls,
        full_config: Dict[str, Any],
        *,
        storage_state_override: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> "QianChuanRegulationStopService":
        headless = bool(full_config.get("browser_headless", True))
        be = str(full_config.get("browser_executable_path") or "").strip()
        opt = RegulationSessionOptions(
            headless=headless,
            storage_state=storage_state_override,
            base_url=base_url or DEFAULT_BASE_URL,
            browser_executable_path=be if be else None,
        )
        return cls(opt)
