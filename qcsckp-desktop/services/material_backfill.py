"""Low-priority, date-bound report recovery; never refreshes current metrics."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from services.qianchuan_open_api.errors import ApiPermissionError, ApiRateLimitError, ApiRequestError, ApiTokenError
from services.qianchuan_open_api.client import QianchuanOpenApiClient
from services.qianchuan_open_api.service import QianchuanOfficialApiService
from utils.sqlite_store import SQLiteStore

STATE_KEY = "material_backfill_state"
JOB_KIND = "material_backfill"
PRIORITY = 0


class _HistoryReadClient(QianchuanOpenApiClient):
    """Private facade: borrow capacity per GET, never for an entire pagination."""
    def __init__(self, source, admission):
        self.source = source
        self.admission = admission

    def get(self, *args, **kwargs):
        with self.admission():
            return self.source.get(*args, **kwargs)

    def get_all_pages(self, *args, **kwargs):
        kwargs["parallel_workers"] = 1
        return super().get_all_pages(*args, **kwargs)

    def post(self, *args, **kwargs):
        raise RuntimeError("历史回补禁止写请求")


def _hot_work_pending(target: dict, *, db: SQLiteStore) -> bool:
    from services import official_api_collection as collection
    with collection._ACTIVE_LOCK:
        active = tuple(collection._ACTIVE_TARGET_UIDS)
    if active:
        placeholders = ",".join("?" for _ in active)
        if db.count("promotion_target", where=f"account_uid=? AND enabled=1 AND target_uid IN ({placeholders})",
                    params=(target.get("account_uid"), *active)):
            return True
    return bool(db.execute(
        "SELECT 1 FROM collection_job j JOIN promotion_target pt ON pt.target_uid=j.target_uid "
        "WHERE j.account_uid=? AND j.job_kind='hot_collection' AND pt.enabled=1 "
        "AND pt.capacity_state='active' AND (j.status='leased' OR "
        "(j.status='queued' AND j.due_at<=?)) LIMIT 1",
        (target.get("account_uid"), _text(datetime.now())), fetch=True))


def _request_admission(job: dict, target: dict, *, db: SQLiteStore):
    from services import official_api_collection as collection
    heartbeat = [0.0]

    @contextmanager
    def admit():
        while True:
            if collection._STOP.is_set() or collection._owner_key() != job.get("owner_username"):
                raise RuntimeError("回补已停止或账号已切换")
            if time.monotonic() >= heartbeat[0]:
                renewed = db.execute(
                    "UPDATE collection_job SET lease_expires_at=? WHERE id=? AND status='leased' "
                    "AND lease_owner=? AND fencing_token=?",
                    (_text(datetime.now() + timedelta(seconds=collection.COLLECTION_JOB_LEASE_SECONDS)),
                     job.get("id"), job.get("lease_owner"), job.get("fencing_token")))
                if not renewed:
                    raise RuntimeError("回补租约已失效")
                heartbeat[0] = time.monotonic() + 30
            if _hot_work_pending(target, db=db):
                collection._STOP.wait(0.1)
                continue
            remaining = collection._account_backoff_remaining(collection._target_account_key(target), db=db)
            if remaining:
                raise ApiRateLimitError("账户冷却中", retry_after=remaining)
            with collection._account_collection_slot(collection._target_account_key(target)):
                # A hot job can arrive while this thread waits for the slot.
                if _hot_work_pending(target, db=db):
                    continue
                yield
                return
    return admit


def _text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _date(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def verification_phases(day: str) -> list[tuple[str, datetime]]:
    start = _date(day)
    if start is None:
        return []
    return [("initial", start + timedelta(days=1)),
            ("d1", start + timedelta(days=1, hours=2)),
            ("d2", start + timedelta(days=2, hours=6))]


def _mutate(db: SQLiteStore, uid: str, change: Callable, *, connection=None) -> dict:
    if connection is None:
        with db.transaction() as conn:
            db.execute("BEGIN IMMEDIATE", connection=conn)
            return _mutate(db, uid, change, connection=conn)
    row = db.select_one("promotion_target", where={"target_uid": uid}, connection=connection)
    if not row:
        return {}
    try:
        capability = json.loads(row.get("capability_json") or "{}")
    except (TypeError, ValueError):
        capability = {}
    if not isinstance(capability, dict):
        capability = {}
    before = json.dumps(capability, sort_keys=True, ensure_ascii=False)
    states = capability.get(STATE_KEY)
    states = states if isinstance(states, dict) else {}
    change(states, capability)
    capability[STATE_KEY] = states
    after = json.dumps(capability, sort_keys=True, ensure_ascii=False)
    if after != before:
        # Merge only the owned JSON key into the latest row in this transaction;
        # a hot collector's unrelated capability updates are never replaced.
        db.update("promotion_target", {"capability_json": after},
                  where={"target_uid": uid}, connection=connection)
    return states


def _next_due(states: dict) -> str:
    return min((str(row.get("next_attempt_at") or "") for row in states.values()
                if isinstance(row, dict) and row.get("status") in {"queued", "backoff", "running"}
                and row.get("next_attempt_at")), default="")


def prepare_target(target: dict, *, db: SQLiteStore, now: Optional[datetime] = None) -> str:
    current = now or datetime.now()
    uid = str(target.get("target_uid") or "")

    def change(states, capability):
        dates = {(current - timedelta(days=n)).strftime("%Y-%m-%d") for n in (1, 2)}
        legacy = capability.get("recovery_backfill_pending_dates")
        if isinstance(legacy, list):
            dates.update(day for day in legacy if isinstance(day, str) and _date(day))
        dates.update(states)
        for day in dates:
            parsed = _date(day)
            if not parsed or parsed.date() >= current.date():
                continue
            entry = states.get(day)
            entry = dict(entry) if isinstance(entry, dict) else {}
            if parsed < current - timedelta(days=45):
                if entry.get("status") == "succeeded":
                    states.pop(day, None)
                continue
            success = str(entry.get("last_success_at") or "")
            pending = [(phase, due) for phase, due in verification_phases(day) if success < _text(due)]
            if not pending:
                entry["status"] = "succeeded"
            elif entry.get("status") != "running":
                ready = [item for item in pending if item[1] <= current]
                phase, due = ready[-1] if ready else pending[0]
                if entry.get("phase") != phase:
                    due_text = _text(due)
                    if entry.get("status") == "backoff":
                        due_text = max(due_text, str(entry.get("next_attempt_at") or ""))
                    entry.update(phase=phase, status="queued", attempts=0,
                                 next_attempt_at=due_text, last_error="")
                elif not entry.get("status"):
                    entry.update(status="queued", attempts=0, next_attempt_at=_text(due))
            states[day] = entry
    states = _mutate(db, uid, change)
    due = _next_due(states)
    if due:
        from services.official_api_collection import _enqueue_collection_jobs
        _enqueue_collection_jobs([uid], db=db, priority=PRIORITY,
                                 due_at=datetime.strptime(due, "%Y-%m-%d %H:%M:%S"), kind=JOB_KIND)
    return due


def schedule_material_backfills(targets, *, db: SQLiteStore, now: Optional[datetime] = None) -> None:
    for target in targets:
        try:
            prepare_target(dict(target), db=db, now=now)
        except Exception:
            from utils.log import logger
            logger.exception("历史素材回补调度失败 target=%s", target.get("target_uid"))


def run_material_backfill_job(job: dict, *, db: SQLiteStore) -> dict[str, Any]:
    # Local import avoids initializing collectors while this module is loaded.
    from services import official_api_collection as collection
    uid = str(job.get("target_uid") or "")
    owner = collection._owner_key()
    if owner != str(job.get("owner_username") or "") or collection._STOP.is_set():
        return {"success": False, "job_idle": True, "message": "工具账号已切换"}
    target = db.select_one("promotion_target", where={"target_uid": uid}) or {}
    account = db.select_one("qianchuan_account", where={"account_uid": target.get("account_uid")}) or {}
    if not target.get("enabled") or not account.get("enabled") or account.get("owner_username") != owner:
        return {"success": False, "job_idle": True, "message": "回补目标已停用或归属变化"}
    with collection._ACTIVE_LOCK:
        if uid in collection._ACTIVE_TARGET_UIDS:
            return {"success": False, "deferred": True, "retry_seconds": 15}
    # Do not occupy the hot collector's per-target guard during historical
    # paging. Only one background future runs; the two paths own different
    # tables/JSON keys and merge their commits transactionally.
    delay = collection._account_backoff_remaining(collection._target_account_key(target), db=db)
    if delay:
        return {"success": False, "deferred": True, "retry_seconds": delay}
    return _read_one_date(job, target, db=db)


def _read_one_date(job: dict, target: dict, *, db: SQLiteStore) -> dict[str, Any]:
    from services import official_api_collection as collection
    uid = str(target["target_uid"])
    now = datetime.now()
    selected: dict = {}
    capability = collection._target_capability(target)
    units = capability.get("report_metric_units") or {}
    metrics = collection._supported_material_metrics(units)
    if not metrics or not capability.get("marketing_goal"):
        return {"success": False, "deferred": True, "retry_seconds": 60,
                "message": "等待当前采集验证报表字段"}

    def claim(states, cap):
        due = [(day, entry) for day, entry in states.items() if isinstance(entry, dict)
               and entry.get("status") in {"queued", "backoff", "running"}
               and str(entry.get("next_attempt_at") or "") <= _text(now)]
        if due:
            day, entry = min(due, key=lambda item: (item[1].get("next_attempt_at", ""), item[0]))
            entry.update(status="running", attempts=int(entry.get("attempts") or 0) + 1)
            selected.update(day=day, **entry)
    states = _mutate(db, uid, claim)
    if not selected:
        return {"success": True, "next_due_at": _next_due(states), "job_idle": not _next_due(states)}
    day = selected["day"]
    observed_at = _text(now)
    rows = []
    error = None
    request_ids = []
    try:
        service = collection.get_official_api_service()
        if isinstance(service, QianchuanOfficialApiService):
            # Do not monkeypatch the singleton client used by hot collectors.
            service = QianchuanOfficialApiService(
                client=_HistoryReadClient(service.client, _request_admission(job, target, db=db)),
                allow_writes=False)
        materials, request_ids = service.list_plan_materials(
            target["aadvid"], target["ad_id"], start_date=day, end_date=day,
            fields=metrics, delivery_only=False, parallel_workers=1)
        reports, report_ids = service.list_material_report(
            target["aadvid"], plan_system=target["plan_system"],
            promotion_scene=target["promotion_scene"], start_date=day, end_date=day,
            metrics=metrics, filter_context=capability.get("material_report_filter_context") or {})
        request_ids = [*request_ids, *report_ids]
        for material in collection._merge_material_report(materials, reports):
            if not str(material.get("material_id") or ""):
                continue
            row = collection._material_snapshot(material, target=target, units=units,
                                                request_id=request_ids[-1] if request_ids else "")
            row["stat_date"] = day
            rows.append(collection._metric_snapshot_row(row, target=target, observed_at=f"{day} 23:59:59"))
    except Exception as exc:
        error = exc
    finished_at = _text(datetime.now())
    attempt = int(selected["attempts"])
    retry_delay = min(480, 60 * 2 ** min(3, attempt - 1))
    if isinstance(error, ApiRateLimitError):
        retry_delay = max(int(getattr(error, "retry_after", 0)), min(900, 120 * 2 ** min(3, attempt - 1)))
        collection._set_account_backoff(collection._target_account_key(target), retry_delay, db=db)
    failed = bool(error) and (
        isinstance(error, (ApiTokenError, ApiPermissionError))
        or (not isinstance(error, ApiRateLimitError)
            and attempt >= (3 if isinstance(error, ApiRequestError) else 8)))

    def finish(states, capability):
        entry = states.get(day) or {}
        if entry.get("phase") != selected.get("phase"):
            return
        entry.update(status="failed" if failed else "backoff" if error else "succeeded",
                     last_finished_at=finished_at, last_error=str(error or "")[:1000],
                     request_ids=request_ids)
        if error:
            entry["next_attempt_at"] = _text(datetime.now() + timedelta(seconds=retry_delay))
        else:
            entry.update(last_success_at=observed_at, rows=len(rows))
        states[day] = entry

    with db.transaction() as connection:
        db.execute("BEGIN IMMEDIATE", connection=connection)
        lease = db.select_one("collection_job", where={"id": job["id"]}, connection=connection) or {}
        current_target = db.select_one("promotion_target", where={"target_uid": uid}, connection=connection) or {}
        current_account = db.select_one("qianchuan_account", where={"account_uid": current_target.get("account_uid")},
                                        connection=connection) or {}
        if (collection._STOP.is_set() or collection._owner_key() != job.get("owner_username")
                or not current_target.get("enabled") or not current_account.get("enabled")
                or current_account.get("owner_username") != job.get("owner_username")
                or current_target.get("aadvid") != target.get("aadvid") or current_target.get("ad_id") != target.get("ad_id")
                or lease.get("status") != "leased" or lease.get("lease_owner") != job.get("lease_owner")
                or str(lease.get("lease_expires_at") or "") <= _text(datetime.now())
                or int(lease.get("fencing_token") or 0) != int(job.get("fencing_token") or 0)):
            return {"success": False, "deferred": True, "retry_seconds": 15, "message": "回补租约或目标已变化"}
        if not error:
            collection._bulk_upsert_rows(connection, "pmc_material_metric_snapshot", rows,
                unique_fields=("account_username", "target_uid", "material_id", "bucket_key"))
        _mutate(db, uid, finish, connection=connection)
    due = prepare_target(target, db=db)
    return {"success": error is None, "next_due_at": due, "job_idle": not due,
            "message": str(error or ""), "stat_date": day, "rows": len(rows)}
