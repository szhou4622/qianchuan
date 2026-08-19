"""通过千川官方 API 同步账户与已监控计划的真实操作日志。"""

from __future__ import annotations

import threading
import time
import json
from datetime import datetime, timedelta
from typing import Any, Optional

from api.operation_events import ingest_platform_log_rows
from api.promotion_targets import list_promotion_targets
from services.qianchuan_accounts import get_qianchuan_account
from services.qianchuan_accounts import list_qianchuan_accounts
from services.qianchuan_open_api.runtime import get_official_api_service
from services.promotion_browser_lock import (
    PRIORITY_OPERATION_LOG,
    exclusive_qianchuan_operation,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


_LOCK = threading.Lock()
_RUNNING: set[str] = set()
_STOP = threading.Event()
_THREAD: Optional[threading.Thread] = None
INCREMENTAL_OVERLAP_MINUTES = 10
MAX_SYNC_WINDOW_HOURS = 24


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _set_state(
    db: SQLiteStore,
    account: dict[str, Any],
    *,
    status: str,
    error: str = "",
    coverage_from: str = "",
    coverage_to: str = "",
    request_evidence: Optional[dict[str, Any]] = None,
) -> None:
    values: dict[str, Any] = {
        "aavid": str(account.get("aavid") or ""),
        "account_uid": str(account.get("account_uid") or ""),
        "last_sync_at": _now(),
        "last_status": status,
        "last_error": str(error or "")[:2000],
        "discovered_api_url": get_official_api_service().OPERATION_LOGS,
        "discovered_request_json": json.dumps(
            request_evidence or {"source": "qianchuan_open_api"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if coverage_from:
        values["coverage_from"] = coverage_from
    if coverage_to:
        values["coverage_to"] = coverage_to
    db.insert_or_update(
        "platform_log_sync_state",
        values,
        unique_fields=["account_uid", "aavid"],
    )


def _ingest_rows(
    aavid: str,
    rows: list[dict[str, Any]],
    *,
    target: Optional[dict[str, Any]] = None,
    request_ids: Optional[list[str]] = None,
    db: SQLiteStore,
) -> int:
    shaped: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(row.get("raw") or {})
        raw.update(
            {
                "aavid": aavid,
                "log_id": row.get("log_id"),
                "create_time": row.get("occurred_at"),
                "operator_name": row.get("operator_name"),
                "operator_id": row.get("operator_id"),
                "object_name": row.get("object_name"),
                "object_id": row.get("object_id"),
                "contentTitle": row.get("content_title"),
                "contentLog": [row.get("content_log")] if row.get("content_log") else [],
                "opt_ip": row.get("opt_ip"),
                "sub_logs": row.get("sub_logs") or [],
                "api_request_ids": [str(item) for item in (request_ids or []) if str(item)],
                "data_source": "qianchuan_open_api",
            }
        )
        if target:
            raw.update(
                {
                    "ad_id": target.get("ad_id"),
                    "plan_id": target.get("ad_id"),
                    "plan_name": target.get("plan_name"),
                    "target_uid": target.get("target_uid"),
                    "promotion_scene": target.get("promotion_scene"),
                    "plan_system": target.get("plan_system"),
                }
            )
        shaped.append(raw)
    return ingest_platform_log_rows(
        aavid,
        shaped,
        db=db,
        update_sync_state=False,
        source="qianchuan_open_api",
    )


def sync_official_operation_logs(
    aavid: Any,
    *,
    days: int = 30,
    db: Optional[SQLiteStore] = None,
) -> dict[str, Any]:
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    aid = str(aavid or "").strip()
    account = get_qianchuan_account(aid, db=store)
    if not account:
        raise ValueError("该千川账户尚未添加")
    end = datetime.now()
    state = store.select_one(
        "platform_log_sync_state",
        where={
            "account_uid": str(account.get("account_uid") or ""),
            "aavid": aid,
        },
    ) or {}
    previous_to = str(state.get("coverage_to") or "").strip()
    try:
        high_water = datetime.strptime(previous_to, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        high_water = None
    if high_water is not None:
        start = min(end, high_water) - timedelta(minutes=INCREMENTAL_OVERLAP_MINUTES)
        start_text = start.strftime("%Y-%m-%d %H:%M:%S")
    else:
        start = (end - timedelta(days=max(1, min(180, int(days))))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_text = start.strftime("%Y-%m-%d %H:%M:%S")
    window_end = min(end, start + timedelta(hours=MAX_SYNC_WINDOW_HOURS))
    end_text = window_end.strftime("%Y-%m-%d %H:%M:%S")
    persisted_coverage_from = str(state.get("coverage_from") or "").strip() or start_text
    _set_state(store, account, status="syncing")
    inserted = 0
    try:
        service = get_official_api_service()
        with exclusive_qianchuan_operation(
            f"官方API账户日志:{aid}",
            priority=PRIORITY_OPERATION_LOG,
        ):
            rows, account_request_ids = service.list_operation_logs(
                aid,
                start_time=start_text,
                end_time=end_text,
                object_type="ACCOUNT",
            )
        inserted += _ingest_rows(aid, rows, request_ids=account_request_ids, db=store)
        plan_request_ids: dict[str, list[str]] = {}
        targets = [
            row
            for row in list_promotion_targets(enabled=True, db=store)
            if str(row.get("aadvid") or "") == aid
        ]
        for target in targets:
            with exclusive_qianchuan_operation(
                f"官方API计划日志:{target.get('target_uid')}",
                priority=PRIORITY_OPERATION_LOG,
            ):
                plan_rows, request_ids = service.list_operation_logs(
                    aid,
                    start_time=start_text,
                    end_time=end_text,
                    object_type="AD",
                    object_id=target.get("ad_id"),
                )
            plan_request_ids[str(target.get("ad_id") or "")] = request_ids
            inserted += _ingest_rows(
                aid,
                plan_rows,
                target=target,
                request_ids=request_ids,
                db=store,
            )
        _set_state(
            store,
            account,
            # A complete successful API response with zero operations is still
            # complete coverage, not a sync failure.
            status=("ok" if window_end >= end else "backfilling"),
            coverage_from=persisted_coverage_from,
            coverage_to=end_text,
            request_evidence={
                "source": "qianchuan_open_api",
                "account_request_ids": account_request_ids,
                "plan_request_ids": plan_request_ids,
            },
        )
        return {
            "success": True,
            "running": False,
            "backend": "official_api",
            "inserted": inserted,
            "coverage_from": persisted_coverage_from,
            "coverage_to": end_text,
            "sync_window_from": start_text,
            "backfill_remaining": window_end < end,
            "message": f"千川官方 API 操作日志同步完成，共处理 {inserted} 条",
        }
    except Exception as exc:
        _set_state(store, account, status="error", error=str(exc))
        raise


def _worker(aavid: str, run_key: str, store: SQLiteStore) -> None:
    try:
        sync_official_operation_logs(aavid, db=store)
    finally:
        with _LOCK:
            _RUNNING.discard(run_key)


def request_official_operation_log_sync(aavid: Any, *, db: Optional[SQLiteStore] = None) -> dict[str, Any]:
    aid = str(aavid or "").strip()
    if not aid:
        return {"success": False, "running": False, "message": "请先选择千川账户"}
    store = db or SQLiteStore()
    account = get_qianchuan_account(aid, db=store)
    if not account:
        return {"success": False, "running": False, "message": "该千川账户尚未添加"}
    run_key = f"{account.get('account_uid') or ''}:{aid}"
    with _LOCK:
        if run_key in _RUNNING:
            return {"success": True, "running": True, "backend": "official_api", "message": "该账户操作日志正在同步"}
        _RUNNING.add(run_key)
        _set_state(store, account, status="syncing")
        threading.Thread(
            target=_worker,
            args=(aid, run_key, store),
            name=f"official-api-log-{aid}",
            daemon=True,
        ).start()
    return {"success": True, "running": True, "backend": "official_api", "message": "已开始通过千川官方 API 同步真实操作日志"}


def start_official_operation_log_sync_background_thread() -> threading.Thread:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()

    def loop() -> None:
        while not _STOP.is_set():
            try:
                store = SQLiteStore()
                for account in list_qianchuan_accounts(db=store):
                    if account.get("enabled") and account.get("directory_selected"):
                        request_official_operation_log_sync(account.get("aavid"), db=store)
            except Exception:
                from utils.log import logger

                logger.exception("千川官方 API 操作日志调度失败")
            _STOP.wait(5 * 60)

    thread = threading.Thread(target=loop, name="official-api-operation-log-scheduler", daemon=True)
    thread.start()
    _THREAD = thread
    return thread


def stop_official_operation_log_sync_background_thread(timeout: float = 5.0) -> None:
    _STOP.set()
    thread = _THREAD
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=max(0.0, float(timeout)))
