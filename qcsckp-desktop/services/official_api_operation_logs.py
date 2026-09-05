"""通过千川官方 API 同步账户与已监控计划的真实操作日志。"""

from __future__ import annotations

import threading
import time
import json
import hashlib
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from api.operation_events import ingest_platform_log_rows
from api.promotion_targets import list_promotion_targets
from services.qianchuan_accounts import get_qianchuan_account
from services.qianchuan_accounts import list_qianchuan_accounts
from services.qianchuan_session import current_session_owner
from services.qianchuan_open_api.runtime import get_official_api_service
from services.qianchuan_open_api.errors import (
    ApiPermissionError,
    ApiRateLimitError,
    ApiRequestError,
    ApiTokenError,
)
from services.promotion_browser_lock import (
    PRIORITY_BACKFILL,
    PRIORITY_OPERATION_LOG,
    exclusive_qianchuan_operation,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


_LOCK = threading.Lock()
_RUNNING: set[str] = set()
_STOP = threading.Event()
_THREAD: Optional[threading.Thread] = None
_WORKERS: list[threading.Thread] = []
INCREMENTAL_OVERLAP_MINUTES = 10
MAX_SYNC_WINDOW_HOURS = 24
MAX_MANUAL_RANGE_DAYS = 30
# Operation-log backfill is deliberately single-worker. API collection owns
# the multi-account concurrency SLA; low-priority history ingestion must not
# race another log writer for SQLite's single writer slot.
OPERATION_LOG_WORKERS = 1
LEASE_SECONDS = 60
POLL_SECONDS = 1.0
MAX_REQUEST_ERROR_ATTEMPTS = 3
MAX_TRANSIENT_ERROR_ATTEMPTS = 8
COMPLETED_WINDOW_RETENTION_DAYS = 45
_KIND_PRIORITY = {
    "manual": 100,
    "report": 80,
    "incremental": 60,
    "repair": 40,
    "verification": 30,
    "history": 20,
}
_DONE_STATUSES = {"succeeded", "empty", "superseded"}
_ACTIVE_STATUSES = {"queued", "running", "backoff", "repairing"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _set_state(
    db: SQLiteStore,
    account: dict[str, Any],
    *,
    status: str,
    error: str = "",
    coverage_from: Optional[str] = None,
    coverage_to: Optional[str] = None,
    request_evidence: Optional[dict[str, Any]] = None,
    **progress: Any,
) -> None:
    values: dict[str, Any] = {
        "aavid": str(account.get("aavid") or ""),
        "account_uid": str(account.get("account_uid") or ""),
        "last_sync_at": _now(),
        "last_status": status,
        "last_error": str(error or "")[:2000],
        "discovered_api_url": str(
            get_official_api_service().OPERATION_LOGS or ""
        ),
        "discovered_request_json": json.dumps(
            request_evidence or {"source": "qianchuan_open_api"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if coverage_from is not None:
        values["coverage_from"] = coverage_from
    if coverage_to is not None:
        values["coverage_to"] = coverage_to
    allowed_progress = {
        "active_batch_uid",
        "requested_from",
        "requested_to",
        "progress_completed",
        "progress_total",
        "progress_rows_seen",
        "progress_rows_inserted",
        "current_object",
        "history_complete",
        "next_retry_at",
        "last_progress_at",
    }
    for key, value in progress.items():
        if key in allowed_progress:
            values[key] = value
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
                "control_task_id": row.get("control_task_id"),
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


def _task_uid(
    owner_username: str,
    account_uid: str,
    aavid: str,
    object_type: str,
    object_id: str,
    window_start: str,
    window_end: str,
) -> str:
    raw = ":".join(
        (
            owner_username,
            account_uid,
            aavid,
            object_type,
            object_id,
            window_start,
            window_end,
        )
    )
    return "logwin_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _parse_manual_range(date_from: Any, date_to: Any) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(str(date_from or "").strip(), "%Y-%m-%d")
        end_day = datetime.strptime(str(date_to or "").strip(), "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError("请选择有效的开始日期和结束日期") from exc
    if end_day < start:
        raise ValueError("结束日期不能早于开始日期")
    if (end_day.date() - start.date()).days + 1 > MAX_MANUAL_RANGE_DAYS:
        raise ValueError(f"单次最多同步 {MAX_MANUAL_RANGE_DAYS} 天操作流水")
    now = datetime.now()
    if start.date() > now.date():
        raise ValueError("开始日期不能晚于今天")
    end = min(
        now,
        end_day.replace(hour=23, minute=59, second=59, microsecond=0),
    )
    return start, end


def _daily_windows(start: datetime, end: datetime) -> list[tuple[str, str]]:
    windows: list[tuple[str, str]] = []
    cursor = start.replace(microsecond=0)
    while cursor <= end:
        window_end = min(
            end,
            cursor.replace(hour=23, minute=59, second=59, microsecond=0),
        )
        windows.append(
            (
                cursor.strftime("%Y-%m-%d %H:%M:%S"),
                window_end.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    # 手动查询和日报都先给出最新日期的可见结果。
    return list(reversed(windows))


def _sync_objects(
    account: dict[str, Any],
    *,
    db: SQLiteStore,
) -> list[dict[str, str]]:
    account_uid = str(account.get("account_uid") or "")
    aavid = str(account.get("aavid") or "")
    targets = [
        row
        for row in list_promotion_targets(enabled=True, db=db)
        if str(row.get("account_uid") or "") == account_uid
        and str(row.get("aadvid") or "") == aavid
    ]
    if not targets:
        return []
    objects = [
        {
            "object_type": "ACCOUNT",
            "object_id": "",
            "target_uid": "",
        }
    ]
    for target in targets:
        ad_id = str(target.get("ad_id") or "").strip()
        if ad_id:
            objects.append(
                {
                    "object_type": "AD",
                    "object_id": ad_id,
                    "target_uid": str(target.get("target_uid") or ""),
                }
            )
    return objects


def _enqueue_range(
    account: dict[str, Any],
    start: datetime,
    end: datetime,
    *,
    request_kind: str,
    db: SQLiteStore,
    batch_uid: str = "",
    objects: Optional[list[dict[str, str]]] = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    init_sqlite_schema(database=db.config.get("database"))
    kind = str(request_kind or "history").strip().lower()
    if kind not in _KIND_PRIORITY:
        raise ValueError("未知的操作日志同步类型")
    owner = str(account.get("owner_username") or "").strip().casefold()
    account_uid = str(account.get("account_uid") or "")
    aavid = str(account.get("aavid") or "")
    if not owner or not account_uid or not aavid:
        raise ValueError("千川账户归属不完整，无法同步操作日志")
    batch = str(batch_uid or f"logbatch_{uuid.uuid4().hex}")
    windows = _daily_windows(start, end)
    objects = _sync_objects(account, db=db) if objects is None else objects
    if not objects:
        raise ValueError("请先启用该千川账户并勾选至少一条监控计划")
    now_text = _now()
    priority = int(_KIND_PRIORITY[kind])
    total = 0
    with db.transaction() as connection:
        db.execute("BEGIN IMMEDIATE", connection=connection)
        for window_start, window_end in windows:
            for item in objects:
                total += 1
                object_type = item["object_type"]
                object_id = item["object_id"]
                scope = {
                    "owner_username": owner,
                    "account_uid": account_uid,
                    "aavid": aavid,
                    "object_type": object_type,
                    "object_id": object_id,
                    "window_start": window_start,
                    "window_end": window_end,
                }
                existing = db.select_one(
                    "operation_log_sync_window",
                    where=scope,
                    connection=connection,
                )
                if existing:
                    status = str(existing.get("status") or "")
                    existing_priority = int(existing.get("priority") or 0)
                    restarting = force_refresh and status not in _ACTIVE_STATUSES
                    promote = restarting or priority >= existing_priority
                    values: dict[str, Any] = {
                        "batch_uid": (
                            batch if promote else str(existing.get("batch_uid") or batch)
                        ),
                        "target_uid": item["target_uid"],
                        "request_kind": (
                            kind if promote else str(existing.get("request_kind") or kind)
                        ),
                        "priority": priority if restarting else max(priority, existing_priority),
                        "updated_at": now_text,
                    }
                    # Scheduled discovery is not a retry request. In particular,
                    # never undo a worker's backoff or revive a terminal failure.
                    if ((kind == "manual" and status in {"failed", "cancelled", "repairing"})
                            or (force_refresh and status not in {"running", "backoff", "queued", "repairing"})):
                        values.update(
                            {
                                "status": "queued",
                                "next_attempt_at": now_text,
                                "last_error": "",
                                "attempt_count": 0,
                            }
                        )
                    db.update(
                        "operation_log_sync_window",
                        values,
                        where={"id": existing["id"]},
                        connection=connection,
                    )
                    continue
                db.insert(
                    "operation_log_sync_window",
                    {
                        "window_uid": _task_uid(
                            owner,
                            account_uid,
                            aavid,
                            object_type,
                            object_id,
                            window_start,
                            window_end,
                        ),
                        "batch_uid": batch,
                        "owner_username": owner,
                        "account_uid": account_uid,
                        "aavid": aavid,
                        "object_type": object_type,
                        "object_id": object_id,
                        "target_uid": item["target_uid"],
                        "window_start": window_start,
                        "window_end": window_end,
                        "request_kind": kind,
                        "priority": priority,
                        "status": "queued",
                        "next_attempt_at": now_text,
                    },
                    connection=connection,
                )
    current_state = db.select_one(
        "platform_log_sync_state",
        where={"account_uid": account_uid, "aavid": aavid},
    ) or {}
    active_batch = str(current_state.get("active_batch_uid") or "")
    active_manual = bool(
        kind != "manual"
        and active_batch
        and db.select_one(
            "operation_log_sync_window",
            where="batch_uid=? AND request_kind='manual' AND status IN ('queued','running','backoff')",
            params=(active_batch,),
        )
    )
    if not active_manual:
        _set_state(
            db,
            account,
            status="syncing" if kind == "manual" else "backfilling",
            active_batch_uid=batch,
            requested_from=start.strftime("%Y-%m-%d %H:%M:%S"),
            requested_to=end.strftime("%Y-%m-%d %H:%M:%S"),
            progress_completed=0,
            progress_total=len(windows),
            progress_rows_seen=0,
            progress_rows_inserted=0,
            current_object="等待同步队列",
            next_retry_at="",
            last_progress_at=now_text,
            request_evidence={
                "source": "qianchuan_open_api",
                "batch_uid": batch,
                "request_kind": kind,
                "window_count": len(windows),
                "object_count": len(objects),
            },
        )
    # 相同范围重复点击时，已完成窗口不会再进入工作器。
    # 立即从持久化任务结算进度，避免页面停在“正在同步”。
    _refresh_batch_state(
        db,
        {
            "batch_uid": batch,
            "account_uid": account_uid,
            "aavid": aavid,
            "request_kind": kind,
        },
    )
    return {
        "batch_uid": batch,
        "task_count": total,
        "window_count": len(windows),
        "object_count": len(objects),
        "requested_from": start.strftime("%Y-%m-%d %H:%M:%S"),
        "requested_to": end.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _enqueue_incremental_range(
    account: dict[str, Any], end: datetime, *, db: SQLiteStore
) -> Optional[dict[str, Any]]:
    """One in-flight incremental batch per account, with a per-object watermark.

    Failed terminal windows remain visible gaps, while later increments can
    continue. A newly monitored object has no watermark and gets today's full
    range once. History jobs independently cover completed calendar days.
    """
    account_uid = str(account.get("account_uid") or "")
    aavid = str(account.get("aavid") or "")
    today_start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    results = []
    batch = f"logbatch_{uuid.uuid4().hex}"
    for item in _sync_objects(account, db=db):
        if db.select_one(
            "operation_log_sync_window",
            where="account_uid=? AND aavid=? AND object_type=? AND object_id=? "
            "AND request_kind='incremental' AND status IN ('queued','running')",
            params=(account_uid, aavid, item["object_type"], item["object_id"]),
        ):
            continue
        rows = db.execute(
            "SELECT MAX(window_end) AS watermark FROM operation_log_sync_window "
            "WHERE account_uid=? AND aavid=? AND object_type=? AND object_id=? "
            "AND request_kind IN ('incremental','repair') "
            "AND window_end>=? AND window_end<=?",
            (account_uid, aavid, item["object_type"], item["object_id"],
             today_start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
            fetch=True,
        ) or []
        watermark = str((rows[0] if rows else {}).get("watermark") or "")
        try:
            previous_end = datetime.strptime(watermark, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            previous_end = today_start
        start = max(today_start, previous_end - timedelta(minutes=INCREMENTAL_OVERLAP_MINUTES))
        results.append(_enqueue_range(account, start, end, request_kind="incremental",
                                      objects=[item], batch_uid=batch, db=db))
    if not results:
        return None
    return {**results[-1], "batch_uid": batch,
            "task_count": sum(result["task_count"] for result in results),
            "object_count": len(results), "requested_from": min(result["requested_from"] for result in results)}


def _merge_intervals(rows) -> list[tuple[datetime, datetime]]:
    intervals = []
    for row in rows:
        try:
            start = datetime.strptime(str(row["window_start"]), "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(str(row["window_end"]), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if start <= end:
            intervals.append((start, end))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + timedelta(seconds=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _intersect_intervals(left, right):
    return _merge_intervals([
        {"window_start": max(a, c).strftime("%Y-%m-%d %H:%M:%S"),
         "window_end": min(b, d).strftime("%Y-%m-%d %H:%M:%S")}
        for a, b in left for c, d in right if max(a, c) <= min(b, d)
    ])


def _resolve_covered_failures(db: SQLiteStore, account_uid: str, aavid: str) -> None:
    gaps = db.select("operation_log_sync_window",
                     where="account_uid=? AND aavid=? AND status IN ('failed','repairing')",
                     params=(account_uid, aavid)) or []
    for gap in gaps:
        rows = db.select("operation_log_sync_window",
            where="owner_username=? AND account_uid=? AND aavid=? AND object_type=? AND object_id=? "
                  "AND last_success_at IS NOT NULL AND last_success_at>=? AND window_end>=? AND window_start<=?",
            params=(gap["owner_username"], account_uid, aavid, gap["object_type"], gap["object_id"],
                    gap.get("completed_at") or gap.get("updated_at") or "", gap["window_start"], gap["window_end"])) or []
        start = datetime.strptime(gap["window_start"], "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(gap["window_end"], "%Y-%m-%d %H:%M:%S")
        if any(a <= start and b >= end for a, b in _merge_intervals(rows)):
            evidence = ",".join(str(row["window_uid"]) for row in rows)[:1200]
            db.update("operation_log_sync_window", {
                "status": "superseded", "updated_at": _now(),
                "last_error": f"{gap.get('last_error') or ''}；已由完整窗口覆盖：{evidence}",
            }, where="id=? AND status IN ('failed','repairing')", params=(gap["id"],))


def _schedule_verifications(account: dict, *, db: SQLiteStore, now: Optional[datetime] = None) -> None:
    from services.material_backfill import verification_phases
    current = now or datetime.now()
    objects = _sync_objects(account, db=db)
    dates = {(current - timedelta(days=days)).strftime("%Y-%m-%d") for days in (1, 2)}
    # Catch up missed phases after downtime, including dates now older than D+2.
    for row in db.select("operation_log_sync_window",
            where="account_uid=? AND aavid=? AND window_start>=?",
            params=(account["account_uid"], account["aavid"],
                    (current - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00"))) or []:
        if str(row["window_start"]).endswith("00:00:00") and str(row["window_end"]).endswith("23:59:59"):
            dates.add(str(row["window_start"])[:10])
    for date_text in sorted(dates, reverse=True):
        day = datetime.strptime(date_text, "%Y-%m-%d")
        due_phases = [due for name, due in verification_phases(day.strftime("%Y-%m-%d"))
                      if name != "initial" and due <= current]
        if not due_phases:
            continue
        due = max(due_phases).strftime("%Y-%m-%d %H:%M:%S")
        end = day.replace(hour=23, minute=59, second=59)
        for item in objects:
            existing = db.select_one("operation_log_sync_window", where={
                "account_uid": account["account_uid"], "aavid": account["aavid"],
                "object_type": item["object_type"], "object_id": item["object_id"],
                "window_start": day.strftime("%Y-%m-%d %H:%M:%S"),
                "window_end": end.strftime("%Y-%m-%d %H:%M:%S"),
            }) or {}
            if (str(existing.get("last_success_at") or "") >= due
                    or existing.get("status") in _ACTIVE_STATUSES
                    or (existing.get("status") == "failed" and str(existing.get("completed_at") or "") >= due)):
                continue
            proof = db.select("operation_log_sync_window",
                where="account_uid=? AND aavid=? AND object_type=? AND object_id=? "
                      "AND last_success_at>=? AND window_end>=? AND window_start<=?",
                params=(account["account_uid"], account["aavid"], item["object_type"], item["object_id"],
                        due, day.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))) or []
            # A successfully repaired union is a bounded successful phase too.
            # Do not retry the failed full-day pagination forever after all its
            # smaller windows have already been read after this phase deadline.
            if any(a <= day and b >= end for a, b in _merge_intervals(proof)):
                continue
            _enqueue_range(account, day, end, request_kind="verification", objects=[item],
                           force_refresh=True, db=db)


def _prune_completed_windows(
    account: dict[str, Any], *, db: SQLiteStore, now: Optional[datetime] = None
) -> None:
    """Prune scheduler metadata only; never delete ingested operation events.

    Yesterday's completed daily window supersedes its overlapping successful
    increments. Keep incomplete/failed evidence, plus 45 days of daily coverage
    (longer than the supported 30-day history request).
    """
    current = now or datetime.now()
    db.execute(
        "DELETE FROM operation_log_sync_window AS w "
        "WHERE w.account_uid=? AND w.aavid=? AND w.status IN ('succeeded','empty','superseded') "
        "AND (w.window_end<? OR (w.status IN ('succeeded','empty') "
        "AND w.request_kind='incremental' AND w.window_end<? "
        "AND EXISTS (SELECT 1 FROM operation_log_sync_window d "
        "WHERE d.owner_username=w.owner_username AND d.account_uid=w.account_uid "
        "AND d.aavid=w.aavid AND d.object_type=w.object_type AND d.object_id=w.object_id "
        "AND d.request_kind<>'incremental' AND d.status IN ('succeeded','empty') "
        "AND d.window_start<=w.window_start AND d.window_end>=w.window_end)))",
        (str(account.get("account_uid") or ""), str(account.get("aavid") or ""),
         (current - timedelta(days=COMPLETED_WINDOW_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S"),
         current.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")),
    )


def _event_count(account_uid: str, aavid: str, db: SQLiteStore) -> int:
    rows = db.execute(
        "SELECT COUNT(*) AS n FROM account_operation_event "
        "WHERE account_uid=? AND aavid=? AND source='qianchuan_open_api'",
        (account_uid, aavid),
        fetch=True,
    ) or []
    return int((rows[0] if rows else {}).get("n") or 0)


def _resume_event_count(target_uid: str, db: SQLiteStore) -> int:
    if not str(target_uid or "").strip():
        return 0
    rows = db.execute(
        "SELECT COUNT(*) AS n FROM account_operation_event "
        "WHERE target_uid=? AND source='qianchuan_open_api' "
        "AND action_type='control_resume' AND status='success'",
        (str(target_uid).strip(),),
        fetch=True,
    ) or []
    return int((rows[0] if rows else {}).get("n") or 0)


def _migrate_legacy_coverage(db: SQLiteStore) -> int:
    """将旧版连续覆盖摘要保留为只读完成窗口。

    这些窗口只用于覆盖展示；用户手动同步同一日期时仍会创建
    ACCOUNT/AD 真实任务，不会把旧摘要当作新的 API 证据。
    """
    rows = db.execute(
        "SELECT s.account_uid,s.aavid,s.coverage_from,s.coverage_to,"
        "a.owner_username FROM platform_log_sync_state s "
        "JOIN qianchuan_account a ON a.account_uid=s.account_uid "
        "WHERE COALESCE(s.coverage_from,'')<>'' AND COALESCE(s.coverage_to,'')<>'' "
        "AND NOT EXISTS (SELECT 1 FROM operation_log_sync_window w "
        "WHERE w.account_uid=s.account_uid AND w.aavid=s.aavid)",
        fetch=True,
    ) or []
    inserted = 0
    for row in rows:
        owner = str(row.get("owner_username") or "").strip().casefold()
        account_uid = str(row.get("account_uid") or "")
        aid = str(row.get("aavid") or "")
        start = str(row.get("coverage_from") or "")
        end = str(row.get("coverage_to") or "")
        if not owner or not account_uid or not aid or not start or not end:
            continue
        db.insert_or_update(
            "operation_log_sync_window",
            {
                "window_uid": _task_uid(
                    owner, account_uid, aid, "LEGACY", "", start, end
                ),
                "batch_uid": "legacy_coverage",
                "owner_username": owner,
                "account_uid": account_uid,
                "aavid": aid,
                "object_type": "LEGACY",
                "object_id": "",
                "window_start": start,
                "window_end": end,
                "request_kind": "history",
                "priority": 0,
                "status": "succeeded",
                "completed_at": _now(),
            },
            unique_fields=[
                "owner_username",
                "account_uid",
                "aavid",
                "object_type",
                "object_id",
                "window_start",
                "window_end",
            ],
        )
        inserted += 1
    return inserted


def _claim_window(db: SQLiteStore, worker_uid: str) -> Optional[dict[str, Any]]:
    now_text = _now()
    lease_until = (datetime.now() + timedelta(seconds=LEASE_SECONDS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with db.transaction() as connection:
        db.execute("BEGIN IMMEDIATE", connection=connection)
        db.execute(
            "UPDATE operation_log_sync_window SET status='queued',lease_owner=NULL,"
            "lease_expires_at=NULL,updated_at=? WHERE status='running' "
            "AND COALESCE(lease_expires_at,'')<>'' AND lease_expires_at<=?",
            (now_text, now_text),
            connection=connection,
        )
        rows = db.execute(
            "SELECT w.* FROM operation_log_sync_window w "
            "JOIN qianchuan_account a ON a.account_uid=w.account_uid "
            "WHERE w.owner_username=? AND w.status IN ('queued','backoff') AND w.next_attempt_at<=? "
            "AND (w.request_kind='manual' OR "
            "(a.enabled=1 AND a.directory_selected=1 "
            "AND EXISTS (SELECT 1 FROM promotion_target mt "
            "WHERE mt.account_uid=w.account_uid AND mt.enabled=1))) "
            "AND (w.object_type<>'AD' OR EXISTS "
            "(SELECT 1 FROM promotion_target pt WHERE pt.account_uid=w.account_uid "
            "AND pt.ad_id=w.object_id AND pt.enabled=1)) "
            "AND NOT EXISTS (SELECT 1 FROM operation_log_sync_window r "
            "WHERE r.account_uid=w.account_uid AND r.aavid=w.aavid "
            "AND r.status='running' AND r.lease_expires_at>?) "
            "AND NOT EXISTS (SELECT 1 FROM api_quota_state q WHERE q.owner_username=w.owner_username "
            "AND q.scope_type='operation_logs' AND q.scope_id=w.account_uid AND q.backoff_until>?) "
            "ORDER BY w.priority DESC,w.window_start DESC,w.id ASC LIMIT 1",
            (str(current_session_owner() or "").strip().casefold(), now_text, now_text, now_text),
            connection=connection,
            fetch=True,
        ) or []
        if not rows:
            return None
        row = dict(rows[0])
        token = int(row.get("fencing_token") or 0) + 1
        changed = db.update(
            "operation_log_sync_window",
            {
                "status": "running",
                "lease_owner": worker_uid,
                "lease_expires_at": lease_until,
                "fencing_token": token,
                "attempt_count": int(row.get("attempt_count") or 0) + 1,
                "updated_at": now_text,
            },
            where="id=? AND status IN ('queued','backoff')",
            params=(row["id"],),
            connection=connection,
        )
        if not changed:
            return None
        row.update(
            {
                "status": "running",
                "lease_owner": worker_uid,
                "lease_expires_at": lease_until,
                "fencing_token": token,
                "attempt_count": int(row.get("attempt_count") or 0) + 1,
            }
        )
        return row


def _target_for_task(task: dict[str, Any], db: SQLiteStore) -> Optional[dict[str, Any]]:
    if str(task.get("object_type") or "") != "AD":
        return None
    rows = [
        row
        for row in list_promotion_targets(db=db)
        if str(row.get("account_uid") or "") == str(task.get("account_uid") or "")
        and str(row.get("aadvid") or "") == str(task.get("aavid") or "")
        and str(row.get("ad_id") or "") == str(task.get("object_id") or "")
    ]
    return rows[0] if rows else None


def _finish_task(
    db: SQLiteStore,
    task: dict[str, Any],
    *,
    status: str,
    rows_seen: int = 0,
    rows_inserted: int = 0,
    request_ids: Optional[list[str]] = None,
    error: str = "",
    next_attempt_at: str = "",
    connection=None,
) -> bool:
    values: dict[str, Any] = {
        "status": status,
        "last_error": str(error or "")[:2000],
        "lease_owner": None,
        "lease_expires_at": None,
        "updated_at": _now(),
    }
    if status in {"succeeded", "empty"}:
        values.update(last_success_at=str(task.get("_read_started_at") or _now()), rows_seen=int(rows_seen),
                      rows_inserted=int(rows_inserted),
                      request_ids_json=json.dumps(request_ids or [], ensure_ascii=False))
    if status in _DONE_STATUSES or status in {"failed", "repairing"}:
        values["completed_at"] = _now()
    if status == "backoff" and task.get("request_kind") == "incremental":
        values.update(request_kind="repair", priority=_KIND_PRIORITY["repair"])
    if next_attempt_at:
        values["next_attempt_at"] = next_attempt_at
    changed = db.update(
        "operation_log_sync_window",
        values,
        where="id=? AND lease_owner=? AND fencing_token=? AND status='running'",
        params=(
            task["id"],
            task["lease_owner"],
            task["fencing_token"],
        ),
        connection=connection,
    )
    return bool(changed)


def _is_incomplete_error(error: Any) -> bool:
    # These are the complete-or-raise client's locally generated validation
    # messages, not arbitrary HTTP/business failures that splitting cannot fix.
    return any(marker in str(error) for marker in ("分页", "不完整", "唯一标识", "重复记录", "pagination"))


def _split_failed_window(db: SQLiteStore, task: dict, error: str) -> bool:
    start = datetime.strptime(task["window_start"], "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(task["window_end"], "%Y-%m-%d %H:%M:%S")
    if (end - start).total_seconds() < 30 * 60:
        return False
    with db.transaction() as connection:
        db.execute("BEGIN IMMEDIATE", connection=connection)
        if task.get("status") == "failed":
            if not db.update("operation_log_sync_window", {"status": "repairing", "updated_at": _now()},
                             where="id=? AND status='failed'", params=(task["id"],), connection=connection):
                return False
        elif not _finish_task(db, task, status="repairing", error=error, connection=connection):
            return False
        repair_batch = f"repair_{task['window_uid']}_{task.get('fencing_token') or 0}"
        cursor = start
        while cursor <= end:
            child_end = min(end, cursor + timedelta(minutes=30) - timedelta(seconds=1))
            scope = {key: task[key] for key in ("owner_username", "account_uid", "aavid", "object_type", "object_id")}
            scope.update(window_start=cursor.strftime("%Y-%m-%d %H:%M:%S"),
                         window_end=child_end.strftime("%Y-%m-%d %H:%M:%S"))
            existing = db.select_one("operation_log_sync_window", where=scope, connection=connection)
            if not existing:
                db.insert("operation_log_sync_window", {
                    **scope, "window_uid": _task_uid(*[scope[key] for key in
                        ("owner_username", "account_uid", "aavid", "object_type", "object_id", "window_start", "window_end")]),
                    "batch_uid": repair_batch, "target_uid": task.get("target_uid") or "",
                    "request_kind": "repair", "priority": _KIND_PRIORITY["repair"], "status": "queued",
                    "next_attempt_at": _now(),
                }, connection=connection)
            elif existing.get("batch_uid") != repair_batch and existing.get("status") not in _ACTIVE_STATUSES:
                db.update("operation_log_sync_window", {"status": "queued", "batch_uid": repair_batch,
                    "request_kind": "repair", "priority": _KIND_PRIORITY["repair"], "attempt_count": 0,
                    "next_attempt_at": _now()}, where={"id": existing["id"]}, connection=connection)
            cursor = child_end + timedelta(seconds=1)
    return True


def _schedule_failed_repairs(account: dict, *, db: SQLiteStore) -> None:
    required = {(item["object_type"], item["object_id"]) for item in _sync_objects(account, db=db)}
    for task in db.select("operation_log_sync_window",
            where="account_uid=? AND aavid=? AND status='failed'",
            params=(account["account_uid"], account["aavid"])) or []:
        if (task.get("object_type"), task.get("object_id")) in required and _is_incomplete_error(task.get("last_error")):
            _split_failed_window(db, task, str(task.get("last_error") or ""))


def _completed_coverage(
    account_uid: str,
    aavid: str,
    db: SQLiteStore,
) -> tuple[str, str, bool]:
    account = db.select_one("qianchuan_account", where={"account_uid": account_uid, "aavid": aavid}) or {}
    required_objects = _sync_objects(account, db=db)
    segments = None
    pending_error = False
    for item in required_objects:
        rows = db.select("operation_log_sync_window",
            where="account_uid=? AND aavid=? AND object_type=? AND object_id=? AND window_end>=?",
            params=(account_uid, aavid, item["object_type"], item["object_id"],
                    (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S"))) or []
        pending_error = pending_error or any(row.get("status") in {"failed", "backoff", "repairing"} for row in rows)
        # A prior successful read remains evidence during a recheck. A failed
        # first read has no last_success_at and cannot manufacture coverage.
        complete = _merge_intervals([row for row in rows if row.get("last_success_at")])
        segments = complete if segments is None else _intersect_intervals(segments, complete)
        if not segments:
            return "", "", False
    if not segments:
        return "", "", False
    # 页面展示与当前最相关的最新连续覆盖段，绝不跨越缺口。
    coverage_start, coverage_end = segments[-1]
    required_start = (datetime.now() - timedelta(days=30)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    history_complete = (not pending_error and coverage_start <= required_start
                        and coverage_end >= datetime.now() - timedelta(minutes=15))
    return (
        coverage_start.strftime("%Y-%m-%d %H:%M:%S"),
        coverage_end.strftime("%Y-%m-%d %H:%M:%S"),
        history_complete,
    )


def _refresh_batch_state(db: SQLiteStore, task: dict[str, Any]) -> None:
    batch_uid = str(task.get("batch_uid") or "")
    if not batch_uid:
        return
    account = get_qianchuan_account(str(task.get("aavid") or ""), db=db)
    if not account:
        return
    _resolve_covered_failures(
        db, str(task.get("account_uid") or ""), str(task.get("aavid") or "")
    )
    current_state = db.select_one(
        "platform_log_sync_state",
        where={
            "account_uid": str(task.get("account_uid") or ""),
            "aavid": str(task.get("aavid") or ""),
        },
    ) or {}
    visible_batch = str(current_state.get("active_batch_uid") or "")
    if visible_batch and visible_batch != batch_uid:
        visible_manual = db.select_one(
            "operation_log_sync_window",
            where="batch_uid=? AND request_kind='manual' AND status IN ('queued','running','backoff')",
            params=(visible_batch,),
        )
        if visible_manual:
            return
    rows = db.execute(
        "SELECT status,rows_seen,rows_inserted,window_start,window_end,"
        "object_type,object_id,last_error,next_attempt_at FROM operation_log_sync_window "
        "WHERE batch_uid=? AND account_uid=? AND aavid=? ORDER BY id",
        (batch_uid, task.get("account_uid"), task.get("aavid")),
        fetch=True,
    ) or []
    total = len(rows)
    completed = sum(1 for row in rows if str(row.get("status") or "") in _DONE_STATUSES)
    window_states: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        window_key = (
            str(row.get("window_start") or ""),
            str(row.get("window_end") or ""),
        )
        window_states.setdefault(window_key, []).append(str(row.get("status") or ""))
    completed_windows = sum(
        1
        for statuses in window_states.values()
        if statuses and all(status in _DONE_STATUSES for status in statuses)
    )
    # A newer successful batch must not hide an unresolved failure elsewhere
    # in the requested history. Backoff is also an error, not healthy progress.
    unresolved = db.execute(
        "SELECT status,last_error,next_attempt_at,attempt_count FROM operation_log_sync_window "
        "WHERE account_uid=? AND aavid=? AND status IN ('failed','backoff','repairing') "
        "ORDER BY CASE WHEN status='failed' THEN 0 ELSE 1 END,updated_at DESC",
        (task.get("account_uid"), task.get("aavid")), fetch=True,
    ) or []
    failed = [row for row in unresolved if str(row.get("status") or "") == "failed"]
    repairing = [row for row in unresolved if str(row.get("status") or "") == "repairing"]
    backing_off = [row for row in unresolved if str(row.get("status") or "") == "backoff"]
    active = [row for row in rows if str(row.get("status") or "") in _ACTIVE_STATUSES]
    seen = sum(int(row.get("rows_seen") or 0) for row in rows)
    inserted = sum(int(row.get("rows_inserted") or 0) for row in rows)
    running = next((row for row in rows if str(row.get("status") or "") == "running"), None)
    current_object = ""
    if running:
        current_object = (
            "账户日志"
            if str(running.get("object_type") or "") == "ACCOUNT"
            else f"计划 {running.get('object_id') or ''}"
        )
    elif active or backing_off or repairing:
        current_object = "等待同步队列"
    coverage_from, coverage_to, history_complete = _completed_coverage(
        str(task.get("account_uid") or ""),
        str(task.get("aavid") or ""),
        db,
    )
    kind = str(task.get("request_kind") or "history")
    if failed:
        status = "partial" if completed else "error"
        error = str(failed[0].get("last_error") or "部分日志窗口同步失败")
    elif active or backing_off or repairing:
        status = "syncing" if kind == "manual" and active else "backfilling"
        error = ""
    else:
        status = "empty" if seen == 0 else "ok"
        error = "" if history_complete else "当前查询范围已完整；30天历史仍在后台补录"
    if unresolved:
        latest_error = unresolved[0]
        error = (
            f"{latest_error.get('last_error') or '日志窗口同步失败'}"
            f"（已尝试 {int(latest_error.get('attempt_count') or 0)} 次"
            + ("，需手动重新同步）" if failed else "，正在按30分钟窗口修补）" if repairing else "，等待退避重试）")
        )
    next_retry = min(
        (
            str(row.get("next_attempt_at") or "")
            for row in backing_off
            if str(row.get("status") or "") == "backoff"
            and str(row.get("next_attempt_at") or "")
        ),
        default="",
    )
    requested_from = min((str(row.get("window_start") or "") for row in rows), default="")
    requested_to = max((str(row.get("window_end") or "") for row in rows), default="")
    _set_state(
        db,
        account,
        status=status,
        error=error,
        coverage_from=coverage_from,
        coverage_to=coverage_to,
        active_batch_uid=batch_uid,
        requested_from=requested_from,
        requested_to=requested_to,
        progress_completed=completed_windows,
        progress_total=len(window_states),
        progress_rows_seen=seen,
        progress_rows_inserted=inserted,
        current_object=current_object,
        history_complete=1 if history_complete else 0,
        next_retry_at=next_retry,
        last_progress_at=_now(),
    )


def _window_scope_is_current(db: SQLiteStore, task: dict) -> bool:
    if str(current_session_owner() or "").strip().casefold() != task.get("owner_username"):
        return False
    account = db.select_one("qianchuan_account", where={"account_uid": task.get("account_uid")}) or {}
    if (not account.get("enabled") or not account.get("directory_selected")
            or account.get("aavid") != task.get("aavid")
            or account.get("owner_username") != task.get("owner_username")):
        return False
    if task.get("object_type") == "AD":
        return bool(db.select_one("promotion_target", where={"account_uid": task.get("account_uid"),
                    "aadvid": task.get("aavid"), "ad_id": task.get("object_id"), "enabled": 1}))
    return True


def _process_window(db: SQLiteStore, task: dict[str, Any]) -> None:
    aid = str(task.get("aavid") or "")
    object_type = str(task.get("object_type") or "ACCOUNT")
    object_id = str(task.get("object_id") or "")
    try:
        if not _window_scope_is_current(db, task):
            _finish_task(db, task, status="cancelled", error="账户或监控对象已切换")
            return
        service = get_official_api_service()
        target = _target_for_task(task, db)
        lock_priority = (
            PRIORITY_BACKFILL
            if str(task.get("request_kind") or "") in {"history", "repair", "verification"}
            else PRIORITY_OPERATION_LOG
        )
        with exclusive_qianchuan_operation(
            f"官方API操作日志:{aid}:{object_type}:{object_id}",
            priority=lock_priority,
        ):
            task["_read_started_at"] = _now()
            rows, request_ids = service.list_operation_logs(
                aid,
                start_time=str(task.get("window_start") or ""),
                end_time=str(task.get("window_end") or ""),
                object_type=object_type,
                object_id=object_id,
            )
        if not _window_scope_is_current(db, task):
            _finish_task(db, task, status="cancelled", error="读取期间账户或监控对象已切换")
            return
        before = _event_count(str(task.get("account_uid") or ""), aid, db)
        before_resume = _resume_event_count(
            str((target or {}).get("target_uid") or ""), db
        )
        _ingest_rows(
            aid,
            rows,
            target=target,
            request_ids=request_ids,
            db=db,
        )
        after = _event_count(str(task.get("account_uid") or ""), aid, db)
        after_resume = _resume_event_count(
            str((target or {}).get("target_uid") or ""), db
        )
        finished = _finish_task(
            db,
            task,
            status="succeeded" if rows else "empty",
            rows_seen=len(rows),
            rows_inserted=max(0, after - before),
            request_ids=request_ids,
        )
        if finished and target and after_resume > before_resume:
            # A new official resume event must be followed by a fresh control
            # task read.  The collection pipeline performs the explicit
            # status observation and wakes the stop-rule runner only after the
            # resulting snapshot commits.
            try:
                from services.official_api_collection import (
                    request_official_api_collection,
                )

                request_official_api_collection(
                    [str(target.get("target_uid") or "")], db=db
                )
            except Exception:
                from utils.log import logger

                logger.exception(
                    "官方恢复调控日志入库后重新采集失败 target=%s",
                    target.get("target_uid"),
                )
    except Exception as exc:
        attempt = max(1, int(task.get("attempt_count") or 1))
        if isinstance(exc, (ApiTokenError, ApiPermissionError)):
            _finish_task(db, task, status="failed", error=str(exc))
        elif isinstance(exc, ApiRateLimitError):
            delay = max(int(getattr(exc, "retry_after", 0)), min(15 * 60, 120 * (2 ** min(3, attempt - 1))))
            from services.official_api_collection import _persist_quota_backoff
            _persist_quota_backoff(owner=str(task.get("owner_username") or ""),
                scope_type="operation_logs", scope_id=str(task.get("account_uid") or ""),
                seconds=delay, db=db, error=str(exc))
            retry_at = (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
            _finish_task(
                db,
                task,
                status="backoff",
                error=str(exc),
                next_attempt_at=retry_at,
            )
        elif isinstance(exc, (ApiRequestError, TimeoutError, ConnectionError, socket.timeout, OSError)):
            max_attempts = (
                MAX_REQUEST_ERROR_ATTEMPTS if isinstance(exc, ApiRequestError)
                else MAX_TRANSIENT_ERROR_ATTEMPTS
            )
            if attempt >= max_attempts:
                if isinstance(exc, ApiRequestError) and _is_incomplete_error(exc) and _split_failed_window(db, task, str(exc)):
                    return
                _finish_task(db, task, status="failed", error=str(exc))
                return
            delay = min(10 * 60, 60 * (2 ** min(3, attempt - 1)))
            retry_at = (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
            _finish_task(
                db,
                task,
                status="backoff",
                error=str(exc),
                next_attempt_at=retry_at,
            )
        else:
            _finish_task(db, task, status="failed", error=str(exc))
    finally:
        _refresh_batch_state(db, task)


def _worker_loop(worker_uid: str) -> None:
    store = None
    while not _STOP.is_set():
        try:
            if store is None:
                candidate = SQLiteStore()
                init_sqlite_schema(database=candidate.config.get("database"))
                store = candidate
            task = _claim_window(store, worker_uid)
            if not task:
                _STOP.wait(POLL_SECONDS)
                continue
            _refresh_batch_state(store, task)
            _process_window(store, task)
        except Exception:
            # SQLite contention can occur in claim, progress, or completion.
            # Keep this worker alive; an unfinished claim becomes retryable
            # after its fenced lease expires, without dropping the window.
            from utils.log import logger

            logger.exception("官方操作日志工作器暂时失败，将恢复 worker=%s", worker_uid)
            _STOP.wait(5.0)


def _ensure_workers() -> None:
    store = SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    _migrate_legacy_coverage(store)
    # Manual sync and the scheduler can both request recovery. Protect the
    # check/start pair so they cannot accidentally create two log writers.
    with _LOCK:
        alive = [worker for worker in _WORKERS if worker.is_alive()]
        _WORKERS[:] = alive
        while len(_WORKERS) < OPERATION_LOG_WORKERS and not _STOP.is_set():
            worker_uid = f"operation-log-worker-{uuid.uuid4().hex[:8]}"
            thread = threading.Thread(
                target=_worker_loop,
                args=(worker_uid,),
                name=worker_uid,
                daemon=True,
            )
            thread.start()
            _WORKERS.append(thread)


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


def request_official_operation_log_sync(
    aavid: Any,
    *,
    date_from: Any = "",
    date_to: Any = "",
    db: Optional[SQLiteStore] = None,
    force_refresh: bool = True,
) -> dict[str, Any]:
    aid = str(aavid or "").strip()
    if not aid:
        return {"success": False, "running": False, "message": "请先选择千川账户"}
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    _migrate_legacy_coverage(store)
    account = get_qianchuan_account(aid, db=store)
    if not account:
        return {"success": False, "running": False, "message": "该千川账户尚未添加"}
    if not account.get("enabled") or not account.get("directory_selected"):
        return {
            "success": False,
            "running": False,
            "message": "请先在千川账户管理中启用该账户",
        }
    try:
        if date_from or date_to:
            start, end = _parse_manual_range(date_from, date_to)
        else:
            end = datetime.now()
            start = (end - timedelta(days=6)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        result = _enqueue_range(
            account,
            start,
            end,
            request_kind="manual",
            force_refresh=force_refresh,
            db=store,
        )
    except Exception as exc:
        return {
            "success": False,
            "running": False,
            "backend": "official_api",
            "message": str(exc),
        }
    _STOP.clear()
    _ensure_workers()
    return {
        "success": True,
        "running": True,
        "backend": "official_api",
        "message": "已按当前筛选日期优先同步真实操作日志",
        **result,
    }


def start_official_operation_log_sync_background_thread() -> threading.Thread:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()
    _ensure_workers()

    def loop() -> None:
        while not _STOP.is_set():
            try:
                store = SQLiteStore()
                now = datetime.now()
                yesterday = (now - timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                yesterday_end = yesterday.replace(hour=23, minute=59, second=59)
                history_start = (now - timedelta(days=30)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                for account in list_qianchuan_accounts(db=store):
                    if not (
                        account.get("enabled")
                        and account.get("directory_selected")
                        and int(account.get("enabled_plan_count") or 0) > 0
                    ):
                        continue
                    if account.get("report_enabled"):
                        _enqueue_range(
                            account,
                            yesterday,
                            yesterday_end,
                            request_kind="report",
                            db=store,
                        )
                    _enqueue_incremental_range(account, now, db=store)
                    _enqueue_range(
                        account,
                        history_start,
                        yesterday_end,
                        request_kind="history",
                        db=store,
                    )
                    _schedule_failed_repairs(account, db=store)
                    _schedule_verifications(account, db=store, now=now)
                    _prune_completed_windows(account, db=store, now=now)
                _ensure_workers()
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
    deadline = time.monotonic() + max(0.0, float(timeout))
    for worker in list(_WORKERS):
        if worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
    _WORKERS[:] = [worker for worker in _WORKERS if worker.is_alive()]
