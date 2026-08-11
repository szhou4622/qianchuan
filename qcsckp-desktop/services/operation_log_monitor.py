# -*- coding: utf-8 -*-
"""记录模式浏览器与千川/巨量纵横操作日志发现、增量同步。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from api.operation_events import (
    ingest_platform_log_rows,
    make_event_uid,
    normalize_action_type,
    update_platform_sync_state,
    upsert_operation_event,
)
from config import DATA_DIR, QIANCHUAN_BACKEND
from services.promotion_browser_lock import (
    PRIORITY_OPERATION_LOG,
    exclusive_browser_operation,
)
from services.qianchuan_session import (
    automation_session_ready,
    current_session_owner,
    load_qianchuan_storage_state,
)
from services.fetcher import QianChuanFetcher, build_qianchuan_url_by_params
from utils.log import logger
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


SYNC_INTERVAL_SECONDS = 300
OPERATION_LOG_ENDPOINT = "/ad/api/pmc/v1/ad/get_opt_log"
QIANCHUAN_ORIGIN = "https://qianchuan.jinritemai.com"
_monitor_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop: Optional[threading.Event] = None
_monitor_status: Dict[str, Any] = {"running": False, "aavid": "", "message": "未启动"}
_platform_sync_lock = threading.Lock()
_platform_sync_threads: Dict[str, threading.Thread] = {}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or ""))
    except Exception:
        return {}


def _walk_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield item
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield item
            yield from _walk_values(item)


def _find_value(value: Any, names: Iterable[str]) -> str:
    wanted = {x.lower() for x in names}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in wanted and item not in (None, ""):
                return str(item)
        for item in value.values():
            found = _find_value(item, names)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_value(item, names)
            if found:
                return found
    return ""


def _extract_platform_rows(payload: Any) -> List[Dict[str, Any]]:
    candidates: List[List[Dict[str, Any]]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            sample = value[:5]
            score = 0
            for row in sample:
                keys = {str(k).lower() for k in row.keys()}
                if keys & {
                    "operation",
                    "operation_name",
                    "action_name",
                    "description",
                    "content",
                    "optcontent",
                    "operatetype",
                    "operationtype",
                    "操作",
                    "内容",
                }:
                    score += 2
                if keys & {
                    "operate_time",
                    "operation_time",
                    "created_at",
                    "create_time",
                    "time",
                    "operatetime",
                    "opttime",
                    "createtime",
                }:
                    score += 1
                if keys & {
                    "operator_name",
                    "operator_id",
                    "user_name",
                    "operate_user_name",
                    "operatorname",
                    "username",
                    "operator",
                    "操作人",
                }:
                    score += 1
            if score >= max(2, len(sample)):
                candidates.append(value)
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    visit(item)

    visit(payload)
    return max(candidates, key=len) if candidates else []


def _looks_like_log_url(url: str) -> bool:
    u = str(url or "").lower()
    return any(x in u for x in ("get_opt_log", "operation-log", "operation_log", "operation/list", "operation/history", "operate-log", "operate_log", "operate/list", "operate/record", "audit-log", "audit_log", "/log/list", "/log/query", "action/log"))


def _with_30_day_range(url: str) -> str:
    """对包含常见日期参数的已发现日志页刷新最近30天范围；未知参数原样保留。"""
    try:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        now = datetime.now().replace(hour=23, minute=59, second=59, microsecond=0)
        start_dt = (now - timedelta(days=29)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        start = start_dt.strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")
        for key in ("dr", "date_range", "daterange", "time_range", "timerange"):
            if key in query:
                query[key] = f"{start},{today}"
        for key in (
            "start_date",
            "date_from",
            "start_time",
            "starttime",
            "begin_time",
            "begintime",
        ):
            if key in query:
                query[key] = _date_value_like(query[key], start_dt)
        for key in (
            "end_date",
            "date_to",
            "end_time",
            "endtime",
            "finish_time",
            "finishtime",
        ):
            if key in query:
                query[key] = _date_value_like(query[key], now)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except Exception:
        return url


def _thirty_day_coverage_window() -> Tuple[str, str]:
    now = datetime.now()
    start = (now - timedelta(days=29)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _date_range_key_state(value: Any) -> Tuple[bool, bool, bool]:
    range_keys = {
        "dr",
        "date_range",
        "daterange",
        "time_range",
        "timerange",
    }
    start_keys = {
        "start_date",
        "date_from",
        "start_time",
        "starttime",
        "begin_time",
        "begintime",
    }
    end_keys = {
        "end_date",
        "date_to",
        "end_time",
        "endtime",
        "finish_time",
        "finishtime",
    }
    if isinstance(value, dict):
        has_range = False
        has_start = False
        has_end = False
        for key, item in value.items():
            normalized = str(key).lower()
            has_range = has_range or normalized in range_keys
            has_start = has_start or normalized in start_keys
            has_end = has_end or normalized in end_keys
            nested = _date_range_key_state(item)
            has_range = has_range or nested[0]
            has_start = has_start or nested[1]
            has_end = has_end or nested[2]
        return has_range, has_start, has_end
    if isinstance(value, list):
        states = [_date_range_key_state(item) for item in value]
        return (
            any(item[0] for item in states),
            any(item[1] for item in states),
            any(item[2] for item in states),
        )
    return False, False, False


def _replay_has_explicit_30_day_range(
    api_url: str,
    post_data: Any,
) -> bool:
    try:
        query = dict(
            parse_qsl(urlsplit(str(api_url or "")).query, keep_blank_values=True)
        )
    except Exception:
        query = {}
    raw = str(post_data or "")
    try:
        body = json.loads(raw)
    except Exception:
        body = dict(parse_qsl(raw, keep_blank_values=True)) if raw else {}
    url_state = _date_range_key_state(query)
    body_state = _date_range_key_state(body)
    has_range = url_state[0] or body_state[0]
    has_start = url_state[1] or body_state[1]
    has_end = url_state[2] or body_state[2]
    return has_range or (has_start and has_end)


def _date_value_like(current: Any, value: datetime) -> Any:
    if isinstance(current, (int, float)):
        return int(value.timestamp() * (1000 if abs(float(current)) > 10_000_000_000 else 1))
    text = str(current or "")
    if text.isdigit() and len(text) in (10, 13):
        stamp = int(value.timestamp() * (1000 if len(text) == 13 else 1))
        return str(stamp)
    if "T" in text:
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if len(text) > 10 or ":" in text:
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return value.strftime("%Y-%m-%d")


def _with_30_day_body(value: Any) -> Any:
    """只改写请求里已存在的常见日期字段，不臆造平台参数。"""
    now = datetime.now()
    start = now - timedelta(days=29)
    start_keys = {"start_date", "date_from", "start_time", "starttime", "begin_time", "begintime"}
    end_keys = {"end_date", "date_to", "end_time", "endtime", "finish_time", "finishtime"}
    range_keys = {"dr", "date_range", "daterange", "time_range", "timerange"}
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in start_keys:
                result[key] = _date_value_like(item, start.replace(hour=0, minute=0, second=0, microsecond=0))
            elif normalized in end_keys:
                result[key] = _date_value_like(item, now.replace(hour=23, minute=59, second=59, microsecond=0))
            elif normalized in range_keys and isinstance(item, list) and len(item) >= 2:
                result[key] = [
                    _date_value_like(item[0], start.replace(hour=0, minute=0, second=0, microsecond=0)),
                    _date_value_like(item[1], now.replace(hour=23, minute=59, second=59, microsecond=0)),
                    *item[2:],
                ]
            elif normalized in range_keys and isinstance(item, str):
                result[key] = f"{start:%Y-%m-%d},{now:%Y-%m-%d}"
            else:
                result[key] = _with_30_day_body(item)
        return result
    if isinstance(value, list):
        return [_with_30_day_body(item) for item in value]
    return value


def _prepare_replay_body(post_data: Any) -> tuple[Any, Dict[str, str]]:
    raw = str(post_data or "")
    if not raw:
        return None, {}
    try:
        parsed = json.loads(raw)
        return _with_30_day_body(parsed), {"Content-Type": "application/json; charset=utf-8"}
    except Exception:
        pairs = parse_qsl(raw, keep_blank_values=True)
        if pairs:
            changed = _with_30_day_body(dict(pairs))
            return urlencode(changed), {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        return raw, {}


_PAGE_KEYS = {"page", "page_no", "pageno", "page_num", "pagenum", "page_index", "pageindex", "current", "current_page"}
_OFFSET_KEYS = {"offset", "start", "start_index", "startindex"}
_PAGE_SIZE_KEYS = {"page_size", "pagesize", "limit"}


def _paginate_value(value: Any, page_number: int) -> Tuple[Any, bool]:
    """只改写请求中已经存在的页码或 offset 字段，避免臆造平台参数。"""
    page_number = max(1, int(page_number))
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        supports_paging = False
        discovered_size = 100
        for key, item in value.items():
            if str(key).lower() in _PAGE_SIZE_KEYS:
                try:
                    discovered_size = max(1, min(500, int(item)))
                except Exception:
                    discovered_size = 100
                discovered_size = max(discovered_size, 100)
                break
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _PAGE_SIZE_KEYS:
                result[key] = discovered_size
            elif normalized in _PAGE_KEYS:
                result[key] = str(page_number) if isinstance(item, str) else page_number
                supports_paging = True
            elif normalized in _OFFSET_KEYS:
                offset = (page_number - 1) * discovered_size
                result[key] = str(offset) if isinstance(item, str) else offset
                supports_paging = True
            else:
                nested, nested_support = _paginate_value(item, page_number)
                result[key] = nested
                supports_paging = supports_paging or nested_support
        return result, supports_paging
    if isinstance(value, list):
        result_list = []
        supports_paging = False
        for item in value:
            nested, nested_support = _paginate_value(item, page_number)
            result_list.append(nested)
            supports_paging = supports_paging or nested_support
        return result_list, supports_paging
    return value, False


def _paginate_url(url: str, page_number: int) -> Tuple[str, bool]:
    try:
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if not pairs:
            return url, False
        data = dict(pairs)
        changed, supports = _paginate_value(data, page_number)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(changed), parts.fragment)), supports
    except Exception:
        return url, False


def _paginate_body(body: Any, content_type: str, page_number: int) -> Tuple[Any, bool]:
    if isinstance(body, (dict, list)):
        return _paginate_value(body, page_number)
    if isinstance(body, str) and "application/x-www-form-urlencoded" in content_type:
        values = dict(parse_qsl(body, keep_blank_values=True))
        changed, supports = _paginate_value(values, page_number)
        return urlencode(changed), supports
    return body, False


def _explicit_has_more(payload: Any) -> Optional[bool]:
    if isinstance(payload, dict):
        for key, item in payload.items():
            normalized = str(key).lower()
            if normalized in {"has_more", "hasmore", "has_next", "hasnext"}:
                if isinstance(item, bool):
                    return item
                if str(item).strip().lower() in {"0", "false", "no"}:
                    return False
                if str(item).strip().lower() in {"1", "true", "yes"}:
                    return True
        for item in payload.values():
            result = _explicit_has_more(item)
            if result is not None:
                return result
    elif isinstance(payload, list):
        for item in payload:
            result = _explicit_has_more(item)
            if result is not None:
                return result
    return None


def _classify_write(url: str, request_payload: Any) -> str:
    u = str(url or "").lower()
    text = (u + " " + json.dumps(request_payload, ensure_ascii=False, default=str)).lower()
    action = normalize_action_type(text)
    if action != "other":
        return action
    if "create-uni-prom-assist-task" in u:
        return "retarget"
    if "batch_delete_operation" in u or "batch_update_operation" in u:
        return "stop"
    if "copy" in u and any(x in u for x in ("campaign", "plan", "ad/")):
        return "plan_copy"
    if "create" in u and any(x in u for x in ("campaign", "plan", "ad/")):
        return "plan_create"
    opt_status = _find_value(request_payload, ("opt_status", "operation_status", "action")).strip().lower()
    if opt_status == "delete" or any(x in text for x in ('"opt_status":"delete"', 'opt_status=delete')):
        return "plan_delete"
    if opt_status in ("disable", "pause") or any(x in text for x in ('"opt_status":"disable"', 'opt_status=disable')):
        return "plan_disable"
    if opt_status in ("enable", "start") or any(x in text for x in ('"opt_status":"enable"', 'opt_status=enable')):
        return "plan_enable"
    if "budget" in text and any(x in u for x in ("update", "modify", "edit")):
        return "budget_update"
    if "roi" in text and any(x in u for x in ("update", "modify", "edit")):
        return "roi_update"
    if "bid" in text and any(x in u for x in ("update", "modify", "edit")):
        return "bid_update"
    return "other"


def _looks_like_mutation(url: str, request_payload: Any, method: str) -> bool:
    if method == "DELETE":
        return True
    if _looks_like_log_url(url):
        return False
    text = (str(url or "") + " " + json.dumps(request_payload, ensure_ascii=False, default=str)).lower()
    return any(
        token in text
        for token in (
            "create", "copy", "delete", "remove", "update", "modify", "edit", "enable", "disable",
            "pause", "stop", "batch_operation", "opt_status", "新建", "复制", "删除", "修改", "启用", "暂停", "停投", "追投",
        )
    )


def _response_success(status_code: int, payload: Any) -> bool:
    if status_code < 200 or status_code >= 300:
        return False
    if isinstance(payload, dict):
        if "status_code" in payload:
            try:
                return int(payload["status_code"]) == 0
            except Exception:
                return False
        if "code" in payload:
            try:
                return int(payload["code"]) == 0
            except Exception:
                pass
    return True


async def _ingest_page_fallback(
    page: Any,
    aavid: str,
    owner_username: str = "",
) -> int:
    """网络日志接口无法识别时，从明确的操作日志表格页面读取可见行。"""
    try:
        marker = (str(page.url or "") + " " + await page.title()).lower()
        if not any(x in marker for x in ("operation", "operate", "audit", "操作日志", "操作记录")):
            body_text = await page.locator("body").inner_text(timeout=3000)
            if "操作日志" not in body_text and "操作记录" not in body_text:
                return 0
        rows = await page.evaluate(
            """
            () => {
              const tables = [...document.querySelectorAll('table')];
              for (const table of tables) {
                const headers = [...table.querySelectorAll('thead th')].map(x => (x.innerText || '').trim());
                const joined = headers.join('|');
                if (!joined.includes('操作') || (!joined.includes('时间') && !joined.includes('操作人'))) continue;
                return [...table.querySelectorAll('tbody tr')].map(tr => {
                  const cells = [...tr.querySelectorAll('td')];
                  const row = {};
                  cells.forEach((cell, i) => row[headers[i] || `列${i + 1}`] = (cell.innerText || '').trim());
                  return row;
                }).filter(row => Object.values(row).some(Boolean));
              }
              return [];
            }
            """
        )
        if not isinstance(rows, list) or not rows:
            return 0
        count = ingest_platform_log_rows(
            aavid,
            [row for row in rows if isinstance(row, dict)],
            owner_username=owner_username or None,
        )
        update_platform_sync_state(
            aavid,
            owner_username=owner_username or None,
            last_status="ok",
            last_error="",
            last_sync_at=_now(),
            discovered_page_url=_with_30_day_range(str(page.url or "")),
        )
        return count
    except Exception:
        return 0


async def _handle_response(
    response: Any,
    aavid: str,
    page: Any,
    owner_username: str = "",
) -> None:
    try:
        if owner_username and current_session_owner() != owner_username:
            return
        request = response.request
        method = str(request.method or "GET").upper()
        url = str(response.url or "")
        try:
            payload = await response.json()
        except Exception:
            payload = {}

        rows = _extract_platform_rows(payload) if _looks_like_log_url(url) else []
        if rows:
            inserted = ingest_platform_log_rows(
                aavid,
                rows,
                owner_username=owner_username or None,
            )
            update_platform_sync_state(
                aavid,
                owner_username=owner_username or None,
                last_status="ok",
                last_error="",
                last_sync_at=_now(),
                discovered_page_url=_with_30_day_range(str(page.url or "")),
                discovered_api_url=url,
                discovered_request_json={"method": method, "post_data": request.post_data or ""},
            )
            logger.info("[账户操作流水] 后台操作日志同步 aavid=%s rows=%s", aavid, inserted)

        if method not in ("POST", "PUT", "PATCH", "DELETE"):
            return
        request_payload = _safe_json(request.post_data)
        action = _classify_write(url, request_payload)
        if action == "other" and not _looks_like_mutation(url, request_payload, method):
            return
        success = _response_success(int(response.status), payload)
        plan_id = _find_value(request_payload, ("plan_id", "campaign_id"))
        plan_name = _find_value(request_payload, ("plan_name", "campaign_name"))
        material_id = _find_value(request_payload, ("material_id",))
        material_name = _find_value(request_payload, ("material_name",))
        regulate_task_id = _find_value(payload, ("regulate_task_id", "assist_task_id", "task_id"))
        if not regulate_task_id:
            regulate_task_id = _find_value(request_payload, ("regulate_task_id", "assist_task_id", "task_id"))
        regulate_task_name = _find_value(request_payload, ("regulate_task_name", "assist_task_name", "task_name"))
        if action == "retarget":
            object_type, object_id, object_name = "material", material_id, material_name
        elif action == "stop":
            object_type, object_id, object_name = "assist_task", regulate_task_id, regulate_task_name
        else:
            object_type, object_id, object_name = "plan", plan_id, plan_name
        if not object_id:
            object_id = _find_value(request_payload, ("ad_id", "id"))
        if not object_name:
            object_name = _find_value(request_payload, ("ad_name", "name"))
        occurred = _now()
        request_id = _find_value(payload, ("request_id", "log_id", "operation_id"))
        event_uid = make_event_uid(
            "browser_observed",
            owner_username,
            aavid,
            request_id or occurred,
            method,
            url,
            action,
            object_id,
            request_payload,
        )
        from services.qianchuan_accounts import ensure_qianchuan_account

        account_uid = str(
            ensure_qianchuan_account(
                aavid,
                owner_username=owner_username or None,
                directory_selected=False,
            ).get("account_uid")
            or ""
        )
        upsert_operation_event(
            {
                "event_uid": event_uid,
                "aavid": aavid,
                "ad_id": _find_value(request_payload, ("ad_id",)),
                "source": "browser_observed",
                "action_type": action,
                "object_type": object_type,
                "object_id": object_id,
                "object_name": object_name,
                "plan_id": plan_id,
                "plan_name": plan_name,
                "material_id": material_id,
                "material_name": material_name,
                "regulate_task_id": regulate_task_id,
                "regulate_task_name": regulate_task_name,
                "operator_name": "记录模式浏览器",
                "status": "success" if success else "failed",
                "summary": "记录模式捕获到千川操作",
                "before": _find_value(request_payload, ("before", "before_value", "old_value", "origin_value")),
                "after": _find_value(request_payload, ("after", "after_value", "new_value", "target_value")),
                "request": {"method": method, "url": url, "body": request_payload},
                "response": payload,
                "occurred_at": occurred,
                "account_uid": account_uid,
            }
        )
    except Exception:
        logger.exception("[账户操作流水] 解析浏览器响应失败")


async def _run_record_browser_unlocked(
    aavid: str,
    ad_id: str,
    stop_event: threading.Event,
    owner_username: str,
) -> None:
    global _monitor_status
    if current_session_owner() != owner_username:
        raise RuntimeError("工具账号已经切换，记录模式已停止")
    storage_state = load_qianchuan_storage_state(owner_username)
    if storage_state is None:
        _monitor_status = {"running": False, "aavid": aavid, "message": "请先在服务控制中登录千川"}
        return
    fetcher = QianChuanFetcher(headless=False, storage_state=storage_state)
    try:
        await fetcher._init_browser()
        page = fetcher.page
        if page is None:
            raise RuntimeError("浏览器页面创建失败")
        page.on(
            "response",
            lambda resp: asyncio.create_task(
                _handle_response(resp, aavid, page, owner_username)
            ),
        )
        url = build_qianchuan_url_by_params(
            base_url="https://qianchuan.jinritemai.com/uni-prom/detail",
            aavid=int(aavid),
            ad_id=int(ad_id),
        )
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        _monitor_status = {"running": True, "aavid": aavid, "message": "记录模式运行中；可在该浏览器内操作或打开后台操作日志"}
        next_dom_check = 0.0
        while not stop_event.is_set() and not page.is_closed():
            if current_session_owner() != owner_username:
                raise RuntimeError("工具账号已经切换，记录模式已停止")
            if time.time() >= next_dom_check:
                await _ingest_page_fallback(page, aavid, owner_username)
                next_dom_check = time.time() + 5
            await asyncio.sleep(1)
    except Exception as exc:
        logger.exception("[账户操作流水] 记录模式浏览器异常")
        _monitor_status = {"running": False, "aavid": aavid, "message": str(exc)}
    finally:
        await fetcher.close()
        if _monitor_status.get("running"):
            _monitor_status = {"running": False, "aavid": aavid, "message": "记录模式已结束"}


async def _run_record_browser(
    aavid: str,
    ad_id: str,
    stop_event: threading.Event,
    owner_username: str,
) -> None:
    async with exclusive_browser_operation(
        f"人工操作记录:{aavid}:{ad_id}",
        priority=30,
        timeout_seconds=900,
    ):
        if current_session_owner() != owner_username:
            raise RuntimeError("工具账号已经切换，记录模式已停止")
        await _run_record_browser_unlocked(
            aavid,
            ad_id,
            stop_event,
            owner_username,
        )


def start_record_browser(aavid: Any, ad_id: Any) -> Dict[str, Any]:
    global _monitor_thread, _monitor_stop, _monitor_status
    aid = str(aavid or "").strip()
    ad = str(ad_id or "").strip()
    owner = current_session_owner()
    if not aid or not ad:
        return {"success": False, "message": "缺少千川账户或广告ID"}
    if not owner:
        return {"success": False, "message": "请先登录工具账号"}
    with _monitor_lock:
        if _monitor_thread and _monitor_thread.is_alive():
            return {"success": False, "message": "已有记录模式浏览器正在运行", "data": dict(_monitor_status)}
        _monitor_stop = threading.Event()
        _monitor_status = {"running": True, "aavid": aid, "message": "正在打开记录模式浏览器"}

        def entry() -> None:
            asyncio.run(
                _run_record_browser(aid, ad, _monitor_stop, owner)
            )

        _monitor_thread = threading.Thread(target=entry, name="operation-record-browser", daemon=True)
        _monitor_thread.start()
    return {"success": True, "data": dict(_monitor_status)}


def stop_record_browser() -> Dict[str, Any]:
    if _monitor_stop:
        _monitor_stop.set()
    return {"success": True}


def record_browser_status() -> Dict[str, Any]:
    return {"success": True, "data": dict(_monitor_status)}


def _account_log_targets(
    aavid: str,
    owner_username: str,
    store: SQLiteStore,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from services.qianchuan_accounts import get_qianchuan_account

    account = get_qianchuan_account(
        aavid,
        owner_username=owner_username,
        db=store,
    )
    if not account:
        raise ValueError("千川账户不存在或不属于当前工具账号")
    rows = store.select(
        "promotion_target",
        where={"account_uid": str(account.get("account_uid") or "")},
    )
    targets: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in sorted(
        rows,
        key=lambda item: (
            not bool(item.get("enabled")),
            str(item.get("ad_id") or ""),
        ),
    ):
        ad_id = str(row.get("ad_id") or "").strip()
        if (
            not ad_id
            or ad_id in seen
            or str(row.get("verification_state") or "") != "verified"
        ):
            continue
        seen.add(ad_id)
        targets.append(row)
    return account, targets


def _enabled_log_account_ids(
    store: SQLiteStore,
    owner_username: str,
) -> List[str]:
    rows = store.execute(
        "SELECT a.aavid FROM qianchuan_account a "
        "WHERE a.enabled=1 AND a.directory_selected=1 "
        "AND a.owner_username=? ORDER BY a.updated_at DESC",
        (owner_username,),
        fetch=True,
    ) or []
    return [str(row.get("aavid") or "") for row in rows if row.get("aavid")]


def _platform_log_window(
    state: Dict[str, Any],
) -> Tuple[datetime, datetime, bool]:
    end = datetime.now().replace(microsecond=0)
    coverage_from = str(state.get("coverage_from") or "").strip()
    last_sync_at = str(state.get("last_sync_at") or "").strip()
    first_backfill = not coverage_from
    if first_backfill:
        start = (end - timedelta(days=29)).replace(
            hour=0,
            minute=0,
            second=0,
        )
    else:
        try:
            last_sync = datetime.strptime(last_sync_at, "%Y-%m-%d %H:%M:%S")
        except Exception:
            last_sync = end - timedelta(minutes=10)
        start = min(end, last_sync) - timedelta(minutes=10)
    return start, end, first_backfill


def _platform_payload_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "千川日志接口返回格式异常"
    for key in ("status_code", "code"):
        if key not in payload:
            continue
        try:
            if int(payload.get(key) or 0) == 0:
                continue
        except Exception:
            continue
        return str(
            payload.get("message")
            or payload.get("msg")
            or f"千川日志接口返回错误码 {payload.get(key)}"
        )
    return ""


async def _fetch_platform_log_payload(page: Any, url: str) -> Dict[str, Any]:
    last_error = ""
    for attempt in range(6):
        result: Dict[str, Any] = {}
        try:
            result = await page.evaluate(
                """
                async ({url, timeoutMs}) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), timeoutMs);
                  try {
                    const response = await fetch(url, {
                      method: 'GET',
                      credentials: 'include',
                      signal: controller.signal,
                    });
                    return {
                      ok: response.ok,
                      status: response.status,
                      retryAfter: response.headers.get('retry-after') || '',
                      contentType: response.headers.get('content-type') || '',
                      text: await response.text(),
                    };
                  } finally {
                    clearTimeout(timer);
                  }
                }
                """,
                {"url": url, "timeoutMs": 60_000},
            )
        except Exception as exc:
            last_error = str(exc or "千川日志接口网络请求失败")
            if attempt == 5:
                raise RuntimeError(last_error) from exc
            await asyncio.sleep(min(0.5 * (2**attempt), 8.0))
            continue

        status = int(result.get("status") or 0)
        text = str(result.get("text") or "")
        content_type = str(result.get("contentType") or "").lower()
        if status in {401, 403} or "text/html" in content_type:
            raise RuntimeError("千川登录状态已失效，请重新登录")
        if status == 429 or status >= 500:
            last_error = f"千川日志接口请求失败 HTTP {status}"
            if attempt == 5:
                raise RuntimeError(last_error)
            try:
                delay = float(str(result.get("retryAfter") or ""))
            except Exception:
                delay = 0.5 * (2**attempt)
            await asyncio.sleep(min(max(delay, 0.5), 8.0))
            continue
        if not result.get("ok"):
            raise RuntimeError(f"千川日志接口请求失败 HTTP {status}")
        try:
            payload = json.loads(text)
        except Exception as exc:
            raise RuntimeError("千川日志接口没有返回JSON数据") from exc
        error = _platform_payload_error(payload)
        if not error:
            return payload
        last_error = error
        transient = any(
            marker in error.casefold()
            for marker in (
                "网络超时",
                "稍后重试",
                "请求频繁",
                "系统繁忙",
                "timeout",
                "too many requests",
                "temporarily unavailable",
            )
        )
        if not transient or attempt == 5:
            raise RuntimeError(error)
        await asyncio.sleep(min(0.5 * (2**attempt), 8.0))
    raise RuntimeError(last_error or "千川日志接口读取失败")


def _platform_log_page_info(
    payload: Any,
    *,
    current_page: int,
    row_count: int,
    page_size: int,
) -> Tuple[int, int, bool]:
    data = payload.get("data") if isinstance(payload, dict) else None
    pagination = data.get("pagination") if isinstance(data, dict) else None
    if not isinstance(pagination, dict):
        return current_page, row_count, row_count >= page_size
    try:
        total_page = max(current_page, int(pagination.get("totalPage") or 0))
    except Exception:
        total_page = current_page
    try:
        total_num = max(row_count, int(pagination.get("totalNum") or 0))
    except Exception:
        total_num = row_count
    return total_page, total_num, current_page < total_page


async def _prepare_platform_log_page(
    page: Any,
    *,
    aavid: str,
    target: Dict[str, Any],
    start: datetime,
    end: datetime,
) -> None:
    source_url = str(target.get("sanitized_page_url") or "").strip()
    # 目录同步保存的经常是 /uni-prom 列表页。日志接口所需的页面 SDK
    # 只有进入统一详情路由后才会稳定初始化；保留来源查询参数，但不能
    # 让 build_qianchuan_url_by_params 把详情路径重新覆盖回列表页。
    if source_url:
        source_parts = urlsplit(source_url)
        if source_parts.scheme in {"http", "https"} and source_parts.netloc:
            source_url = urlunsplit(
                (
                    source_parts.scheme,
                    source_parts.netloc,
                    "/uni-prom/detail",
                    source_parts.query,
                    "",
                )
            )
    detail_url = build_qianchuan_url_by_params(
        base_url=f"{QIANCHUAN_ORIGIN}/uni-prom/detail",
        aavid=int(aavid),
        ad_id=int(str(target.get("ad_id") or "0")),
        promotion_scene=str(target.get("promotion_scene") or "live"),
        source_url=source_url or None,
    )
    parts = urlsplit(detail_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["dr"] = f"{start:%Y-%m-%d},{end:%Y-%m-%d}"
    detail_url = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )
    await page.goto(
        detail_url,
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    if "login" in str(page.url or "").lower():
        raise RuntimeError("千川登录状态已失效，请重新登录")
    # 不再依赖页面上是否渲染“日志”文字。不同计划状态会隐藏页签，但
    # 后台只读日志接口仍可用；直接用最小请求验证真实读取能力更可靠。
    await page.wait_for_timeout(800)
    probe_query = urlencode(
        {
            "objectID": str(target.get("ad_id") or ""),
            "startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": (
                end.replace(hour=0, minute=0, second=0)
                + timedelta(days=1)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "currentPage": 1,
            "pageSize": 1,
            "type": 1,
            "aavid": aavid,
        }
    )
    try:
        await _fetch_platform_log_payload(
            page,
            f"{OPERATION_LOG_ENDPOINT}?{probe_query}",
        )
    except Exception as exc:
        raise RuntimeError(f"计划详情日志能力校验失败：{exc}") from exc


def _enrich_platform_log_rows(
    rows: Iterable[Dict[str, Any]],
    target: Dict[str, Any],
    aavid: str,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item.setdefault("aavid", aavid)
        item.setdefault("ad_id", str(target.get("ad_id") or ""))
        item.setdefault("target_uid", str(target.get("target_uid") or ""))
        item.setdefault(
            "promotion_scene",
            str(target.get("promotion_scene") or ""),
        )
        item.setdefault("plan_system", str(target.get("plan_system") or ""))
        item.setdefault("plan_id", str(target.get("ad_id") or ""))
        item.setdefault("plan_name", str(target.get("plan_name") or ""))
        result.append(item)
    return result


async def _sync_account_platform_logs_unlocked(
    aavid: str,
    owner_username: str,
    *,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    if not owner_username or current_session_owner() != owner_username:
        raise RuntimeError("工具账号已经切换，日志同步已停止")
    gate = automation_session_ready(owner_username)
    if not gate.get("ready"):
        raise RuntimeError(
            str(gate.get("message") or "千川登录状态失效或不存在")
        )
    storage_state = load_qianchuan_storage_state(owner_username)
    if storage_state is None:
        raise RuntimeError("千川登录状态失效或不存在")
    store = db or SQLiteStore()
    account, targets = _account_log_targets(aavid, owner_username, store)
    if not targets:
        raise RuntimeError("该账户还没有可读取日志的千川计划，请先刷新账户计划")
    sync_state = store.select_one(
        "platform_log_sync_state",
        where={
            "account_uid": str(account.get("account_uid") or ""),
            "aavid": aavid,
        },
    ) or {}
    start, end, first_backfill = _platform_log_window(sync_state)
    fetcher = QianChuanFetcher(headless=True, storage_state=storage_state)
    processed = 0
    failed: List[str] = []
    try:
        await fetcher._init_browser()
        page = fetcher.page
        if page is None:
            raise RuntimeError("后台日志同步浏览器创建失败")
        prepared = False
        prepare_errors: List[str] = []
        for index, target in enumerate(targets, start=1):
            update_platform_sync_state(
                aavid,
                owner_username=owner_username,
                db=store,
                last_status="syncing",
                last_error=(
                    f"正在验证日志读取能力 {index}/{len(targets)}："
                    f"{target.get('plan_name') or target.get('ad_id') or '未命名计划'}"
                ),
            )
            try:
                await _prepare_platform_log_page(
                    page,
                    aavid=aavid,
                    target=target,
                    start=start,
                    end=end,
                )
            except Exception as exc:
                prepare_errors.append(
                    f"{target.get('plan_name') or target.get('ad_id') or index}：{exc}"
                )
                continue
            prepared = True
            break
        if not prepared:
            detail = "；".join(prepare_errors[:3])
            raise RuntimeError(
                f"已尝试{len(targets)}条计划，均无法建立日志读取环境"
                + (f"：{detail}" if detail else "")
            )
        page_size = 100
        for target_index, target in enumerate(targets, start=1):
            ad_id = str(target.get("ad_id") or "")
            fingerprints: set[str] = set()
            target_processed = 0
            update_platform_sync_state(
                aavid,
                owner_username=owner_username,
                db=store,
                last_status="syncing",
                last_error=(
                    f"正在读取计划 {target_index}/{len(targets)}："
                    f"{target.get('plan_name') or ad_id}"
                ),
            )
            try:
                for page_number in range(1, 201):
                    query = urlencode(
                        {
                            "objectID": ad_id,
                            "startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                            # 千川页面以右开区间查询，结束时间使用次日零点。
                            "endTime": (
                                end.replace(hour=0, minute=0, second=0)
                                + timedelta(days=1)
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                            "currentPage": page_number,
                            "pageSize": page_size,
                            "type": 1,
                            "aavid": aavid,
                        }
                    )
                    payload = await _fetch_platform_log_payload(
                        page,
                        f"{OPERATION_LOG_ENDPOINT}?{query}",
                    )
                    rows = _extract_platform_rows(payload)
                    if not rows:
                        break
                    fingerprint = hashlib.sha256(
                        json.dumps(
                            rows,
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()
                    if fingerprint in fingerprints:
                        raise RuntimeError("日志接口分页结果重复")
                    fingerprints.add(fingerprint)
                    enriched = _enrich_platform_log_rows(rows, target, aavid)
                    ingest_platform_log_rows(
                        aavid,
                        enriched,
                        owner_username=owner_username,
                        db=store,
                        update_sync_state=False,
                    )
                    processed += len(enriched)
                    target_processed += len(enriched)
                    total_page, total, has_more = _platform_log_page_info(
                        payload,
                        current_page=page_number,
                        row_count=len(rows),
                        page_size=page_size,
                    )
                    if not has_more or target_processed >= total:
                        break
                    # 避免连续翻页触发千川的瞬时超时；异常由上层退避重试。
                    await asyncio.sleep(0.15)
                else:
                    raise RuntimeError("日志接口超过200页，已停止同步")
            except Exception as exc:
                failed.append(
                    f"{target.get('plan_name') or ad_id}：{exc}"
                )
        stored_rows = store.execute(
            "SELECT COUNT(*) AS n FROM account_operation_event "
            "WHERE account_uid=? AND aavid=? AND source='platform_log'",
            (str(account.get("account_uid") or ""), aavid),
            fetch=True,
        ) or []
        stored_count = int(stored_rows[0].get("n") or 0) if stored_rows else 0
        # 增量轮询没有新行不等于账户没有流水；已有历史记录时仍是正常。
        status = (
            "partial"
            if failed
            else ("ok" if processed or stored_count else "empty")
        )
        values: Dict[str, Any] = {
            "last_status": status,
            "last_error": "；".join(failed[:5]),
            "last_sync_at": _now(),
            "discovered_page_url": f"{QIANCHUAN_ORIGIN}/?aavid={aavid}",
            "discovered_api_url": (
                f"{QIANCHUAN_ORIGIN}{OPERATION_LOG_ENDPOINT}"
            ),
            "discovered_request_json": {
                "method": "GET",
                "source": "built_in_readonly_contract",
            },
        }
        if not failed:
            values.update(
                {
                    "coverage_from": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "coverage_to": end.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        else:
            # 任一计划缺页都不能保留“已完整覆盖”的假象；下次继续执行30天补录。
            values.update({"coverage_from": "", "coverage_to": ""})
        update_platform_sync_state(
            aavid,
            owner_username=owner_username,
            db=store,
            **values,
        )
        logger.info(
            "[账户操作流水] 只读同步完成 aavid=%s plans=%s rows=%s status=%s first=%s",
            aavid,
            len(targets),
            processed,
            status,
            first_backfill,
        )
        return {
            "success": not failed,
            "aavid": aavid,
            "plan_count": len(targets),
            "row_count": processed,
            "stored_row_count": stored_count,
            "status": status,
            "first_backfill": first_backfill,
            "message": (
                (
                    "账户流水同步完成"
                    if processed
                    else (
                        "账户流水同步正常，本次没有新增记录"
                        if stored_count
                        else "最近30天没有平台操作记录"
                    )
                )
                if not failed
                else "部分计划日志读取失败，请查看同步状态"
            ),
        }
    finally:
        await fetcher.close()


async def _sync_account_platform_logs(
    aavid: str,
    owner_username: str,
    *,
    db: Optional[SQLiteStore] = None,
    priority: int = PRIORITY_OPERATION_LOG,
) -> Dict[str, Any]:
    async with exclusive_browser_operation(
        f"账户操作日志同步:{aavid}",
        priority=priority,
        timeout_seconds=900,
    ):
        return await _sync_account_platform_logs_unlocked(
            aavid,
            owner_username,
            db=db,
        )


def request_platform_log_sync(
    aavid: Any,
    *,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    if QIANCHUAN_BACKEND == "official_api":
        from services.official_api_operation_logs import (
            request_official_operation_log_sync,
        )

        return request_official_operation_log_sync(aavid, db=db)
    aid = str(aavid or "").strip()
    owner = current_session_owner()
    if not aid:
        return {"success": False, "message": "请先选择千川账户"}
    if not owner:
        return {"success": False, "message": "请先登录工具账号"}
    store = db or SQLiteStore()
    try:
        account, _targets = _account_log_targets(aid, owner, store)
    except Exception as exc:
        return {"success": False, "message": str(exc)}
    with _platform_sync_lock:
        running = _platform_sync_threads.get(aid)
        if running and running.is_alive():
            return {
                "success": True,
                "running": True,
                "message": "该账户流水正在同步，请稍候",
            }
        update_platform_sync_state(
            aid,
            owner_username=owner,
            db=store,
            last_status="syncing",
            last_error="正在等待共享浏览器队列",
            last_sync_at=_now(),
        )

        def entry() -> None:
            try:
                # 用户手动触发优先于普通采集，但仍低于追投/停投写任务。
                asyncio.run(
                    _sync_account_platform_logs(
                        aid,
                        owner,
                        db=store,
                        priority=25,
                    )
                )
            except Exception as exc:
                update_platform_sync_state(
                    aid,
                    owner_username=owner,
                    db=store,
                    last_status=(
                        "login_required"
                        if "登录" in str(exc)
                        else "error"
                    ),
                    last_error=str(exc),
                    last_sync_at=_now(),
                )
                logger.warning(
                    "[账户操作流水] 立即同步失败 aavid=%s: %s",
                    aid,
                    exc,
                )
            finally:
                with _platform_sync_lock:
                    _platform_sync_threads.pop(aid, None)

        thread = threading.Thread(
            target=entry,
            name=f"platform-log-sync-{aid}",
            daemon=True,
        )
        _platform_sync_threads[aid] = thread
        thread.start()
    return {
        "success": True,
        "running": True,
        "account_uid": str(account.get("account_uid") or ""),
        "message": "已开始同步该账户最近30天操作流水",
    }


async def _sync_one_unlocked(
    aavid: str,
    page_url: str,
    api_url: str = "",
    request_json: Any = "",
    owner_username: str = "",
) -> None:
    if not owner_username or current_session_owner() != owner_username:
        raise RuntimeError("工具账号已经切换，日志同步已停止")
    gate = automation_session_ready(owner_username)
    if not gate.get("ready"):
        update_platform_sync_state(
            aavid,
            owner_username=owner_username,
            last_status="login_required",
            last_error=str(gate.get("message") or "千川登录状态失效或不存在"),
        )
        return
    storage_state = load_qianchuan_storage_state(owner_username)
    if storage_state is None:
        update_platform_sync_state(
            aavid,
            owner_username=owner_username,
            last_status="login_required",
            last_error="千川登录状态失效或不存在",
        )
        return
    fetcher = QianChuanFetcher(headless=True, storage_state=storage_state)
    try:
        sync_started = _now()
        await fetcher._init_browser()
        page = fetcher.page
        if page is None:
            raise RuntimeError("同步页面创建失败")
        page.on(
            "response",
            lambda resp: asyncio.create_task(
                _handle_response(resp, aavid, page, owner_username)
            ),
        )
        await page.goto(_with_30_day_range(page_url), wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(3)
        request_spec = _safe_json(request_json)
        replay_error = ""
        replay_rows = 0
        pagination_complete = True
        if api_url and isinstance(request_spec, dict) and fetcher.context is not None:
            try:
                method = str(request_spec.get("method") or "GET").upper()
                explicit_window = _replay_has_explicit_30_day_range(
                    api_url,
                    request_spec.get("post_data"),
                )
                base_body, headers = _prepare_replay_body(request_spec.get("post_data"))
                content_type = str(headers.get("Content-Type") or "")
                fingerprints = set()
                for page_number in range(1, 201):
                    replay_url, url_supports_paging = _paginate_url(_with_30_day_range(api_url), page_number)
                    replay_body, body_supports_paging = _paginate_body(base_body, content_type, page_number)
                    supports_paging = url_supports_paging or body_supports_paging
                    if page_number > 1 and not supports_paging:
                        break
                    kwargs: Dict[str, Any] = {"method": method, "headers": headers, "timeout": 60_000}
                    if replay_body is not None and method not in ("GET", "HEAD"):
                        kwargs["data"] = replay_body
                    replay = await fetcher.context.request.fetch(replay_url, **kwargs)
                    payload = await replay.json()
                    rows = _extract_platform_rows(payload)
                    if not rows:
                        break
                    fingerprint = hashlib.sha256(
                        json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                    ).hexdigest()
                    if fingerprint in fingerprints:
                        pagination_complete = False
                        replay_error = "日志接口分页结果重复，已停止继续补录"
                        break
                    fingerprints.add(fingerprint)
                    replay_rows += len(rows)
                    if current_session_owner() != owner_username:
                        raise RuntimeError("工具账号已经切换，日志同步已停止")
                    ingest_platform_log_rows(
                        aavid,
                        rows,
                        owner_username=owner_username,
                    )
                    has_more = _explicit_has_more(payload)
                    if has_more is False:
                        break
                    if has_more is True and not supports_paging:
                        pagination_complete = False
                        replay_error = "日志接口显示仍有下一页，但请求中未发现可安全改写的分页字段"
                        break
                else:
                    pagination_complete = False
                    replay_error = "日志接口超过200页，已停止补录并标记为不完整"
                if pagination_complete and not explicit_window:
                    pagination_complete = False
                    replay_error = (
                        "日志请求中未发现可安全改写的日期字段，"
                        "不能确认已完整扫描最近30天"
                    )
                coverage_from, coverage_to = _thirty_day_coverage_window()
                if replay_rows or not replay_error:
                    update_platform_sync_state(
                        aavid,
                        owner_username=owner_username,
                        last_status="ok" if pagination_complete else "partial",
                        last_error="" if pagination_complete else replay_error,
                        last_sync_at=_now(),
                        **(
                            {
                                "coverage_from": coverage_from,
                                "coverage_to": coverage_to,
                            }
                            if pagination_complete
                            else {}
                        ),
                    )
            except Exception as exc:
                replay_error = str(exc)
        await asyncio.sleep(6)
        await _ingest_page_fallback(page, aavid, owner_username)
        await asyncio.sleep(3)
        if replay_error:
            from services.qianchuan_accounts import get_qianchuan_account

            account = get_qianchuan_account(
                aavid,
                owner_username=owner_username,
            )
            state = (
                SQLiteStore().select_one(
                    "platform_log_sync_state",
                    where={
                        "account_uid": str(
                            (account or {}).get("account_uid") or ""
                        ),
                        "aavid": aavid,
                    },
                )
                or {}
            )
            if replay_rows:
                update_platform_sync_state(
                    aavid,
                    owner_username=owner_username,
                    last_status="partial",
                    last_error="后台日志只完成部分补录：" + replay_error,
                    last_sync_at=_now(),
                )
            elif str(state.get("last_status") or "") != "ok" or str(state.get("last_sync_at") or "") < sync_started:
                raise RuntimeError("日志接口重放失败，页面也未读取到操作表格：" + replay_error)
    except Exception as exc:
        update_platform_sync_state(
            aavid,
            owner_username=owner_username,
            last_status="error",
            last_error=str(exc),
            last_sync_at=_now(),
        )
        logger.warning("[账户操作流水] 平台日志同步失败 aavid=%s: %s", aavid, exc)
    finally:
        await fetcher.close()


async def _sync_one(
    aavid: str,
    page_url: str,
    api_url: str = "",
    request_json: Any = "",
    owner_username: str = "",
) -> None:
    async with exclusive_browser_operation(
        f"账户操作日志同步:{aavid}",
        priority=40,
        timeout_seconds=900,
    ):
        if not owner_username or current_session_owner() != owner_username:
            raise RuntimeError("工具账号已经切换，日志同步已停止")
        await _sync_one_unlocked(
            aavid,
            page_url,
            api_url,
            request_json,
            owner_username,
        )


async def platform_log_sync_loop() -> None:
    init_sqlite_schema()
    await asyncio.sleep(20)
    while True:
        try:
            owner = current_session_owner()
            if not owner:
                await asyncio.sleep(SYNC_INTERVAL_SECONDS)
                continue
            for aid in _enabled_log_account_ids(SQLiteStore(), owner):
                with _platform_sync_lock:
                    immediate = _platform_sync_threads.get(aid)
                    if immediate and immediate.is_alive():
                        continue
                try:
                    await _sync_account_platform_logs(aid, owner)
                except Exception as exc:
                    update_platform_sync_state(
                        aid,
                        owner_username=owner,
                        last_status=(
                            "login_required"
                            if "登录" in str(exc)
                            else "error"
                        ),
                        last_error=str(exc),
                        last_sync_at=_now(),
                    )
                    logger.warning(
                        "[账户操作流水] 周期同步失败 aavid=%s: %s",
                        aid,
                        exc,
                    )
        except Exception:
            logger.exception("[账户操作流水] 五分钟同步循环异常")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


def start_platform_log_sync_background_thread() -> threading.Thread:
    if QIANCHUAN_BACKEND == "official_api":
        from services.official_api_operation_logs import (
            start_official_operation_log_sync_background_thread,
        )

        return start_official_operation_log_sync_background_thread()
    def entry() -> None:
        asyncio.run(platform_log_sync_loop())

    thread = threading.Thread(target=entry, name="platform-log-sync", daemon=True)
    thread.start()
    return thread
