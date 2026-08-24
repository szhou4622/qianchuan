"""Official API collector that feeds the production read models.

Current material state is upserted into ``pmc_promotion_material_latest``;
changed numeric metrics use the narrow snapshot table, while retarget control
tasks continue to use ``pmc_roi2_assist_task``.  The legacy full-row history is
no longer expanded by every five-minute official API cycle.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from api.promotion_targets import (
    patch_target_sync_state,
    record_target_verification_failure,
    replace_material_product_links,
    update_target_catalog_evidence,
    upsert_products,
)
from services.plan_system import normalize_plan_system
from services.qianchuan_accounts import (
    _owner_key,
    record_target_duration,
    refresh_monitor_capacity,
    schedulable_promotion_targets,
)
from services.qianchuan_open_api.errors import (
    ApiPermissionError,
    ApiRateLimitError,
    ApiRequestError,
    ApiTokenError,
)
from services.qianchuan_open_api.normalizers import (
    first,
    normalize_metric_value,
    normalize_plan_system as normalize_api_plan_system,
    normalize_promotion_scene,
    raw_json,
    text_id,
)
from services.qianchuan_open_api.runtime import get_official_api_service
from utils.log import logger
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


MATERIAL_METRICS = (
    "stat_cost_for_roi2",
    "total_order_settle_count_for_roi2_1h",
    "total_order_settle_amount_for_roi2_1h",
    "total_order_settle_amount_rate_for_roi2_1h",
    "total_prepay_and_pay_order_roi2",
    "total_pay_order_gmv_include_coupon_for_roi2",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_rate",
    "total_pay_order_count_for_roi2",
    "live_show_count_for_roi2_v2",
    "live_watch_count_for_roi2_v2",
    "live_cvr_rate_for_roi2_v2",
    "live_convert_rate_for_roi2_v2",
    "product_show_count_for_roi2",
    "product_click_count_for_roi2",
    "product_cvr_rate_for_roi2",
    "product_convert_rate_for_roi2",
)

_STOP = threading.Event()
_WAKE = threading.Event()
_THREAD: Optional[threading.Thread] = None
_LOCK = threading.Lock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_TARGET_UIDS: set[str] = set()
_PENDING_TARGET_UIDS: set[str] = set()
_ACCOUNT_COLLECTION_LOCKS: dict[str, threading.BoundedSemaphore] = {}
_ACCOUNT_DEGRADED_LOCKS: dict[str, threading.Lock] = {}
_ACCOUNT_RATE_LIMITED: set[str] = set()
_ACCOUNT_CLEAN_CYCLES: dict[str, int] = {}
_ACCOUNT_BACKOFF_UNTIL: dict[str, float] = {}
_RATE_LIMIT_ACCOUNT_EVENTS: dict[str, float] = {}
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY_DATABASES: set[str] = set()

# One target is refreshed on a fixed five-minute start-to-start cadence.  The
# scheduler only sleeps for a short tick so a slow account never adds another
# five minutes to every account behind it.
COLLECTION_INTERVAL_SECONDS = 5 * 60
COLLECTION_SCHEDULER_TICK_SECONDS = 5
COLLECTION_MAX_WORKERS = 6
ACCOUNT_MAX_CONCURRENT_PLANS = 2
COLLECTION_PERIODIC_BATCH_SIZE = COLLECTION_MAX_WORKERS
COLLECTION_TRANSIENT_RETRY_SECONDS = 60
REPORT_CONFIG_INTERVAL_SECONDS = 30 * 60
PRODUCT_CATALOG_INTERVAL_SECONDS = 30 * 60
PLAN_DETAIL_INTERVAL_SECONDS = 20 * 60
CONTROL_HISTORY_INTERVAL_SECONDS = 60 * 60
CONTROL_HOT_WINDOW_DAYS = 7
COLLECTION_JOB_LEASE_SECONDS = 10 * 60
ADAPTIVE_RECOVERY_CLEAN_BATCHES = 10

_METRIC_SNAPSHOT_FIELDS = (
    "stat_cost",
    "order_settle_count_1h",
    "order_settle_amount_1h",
    "order_settle_rate_1h",
    "prepay_pay_order_count",
    "pay_gmv_include_coupon",
    "prepay_pay_settle_1h",
    "refund_rate_1h",
    "overall_order_count",
    "overall_show_count",
    "overall_click_count",
    "overall_ctr",
    "overall_conversion_rate",
)

_ADAPTIVE_LOCK = threading.Lock()
_ADAPTIVE_WORKERS = COLLECTION_MAX_WORKERS
_ADAPTIVE_CLEAN_BATCHES = 0


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _five_minute_bucket(value: Any = None) -> str:
    observed = _parse_local_time(value) if value is not None else None
    observed = observed or datetime.now()
    minute = observed.minute - (observed.minute % 5)
    return observed.replace(minute=minute, second=0, microsecond=0).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _metric_snapshot_row(
    row: Mapping[str, Any],
    *,
    target: Mapping[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    snapshot = {
        "account_username": _owner_key(),
        "account_uid": str(target.get("account_uid") or ""),
        "aadvid": str(row.get("aadvid") or target.get("aadvid") or ""),
        "target_uid": str(row.get("target_uid") or target.get("target_uid") or ""),
        "ad_id": str(row.get("ad_id") or target.get("ad_id") or ""),
        "material_id": str(row.get("material_id") or ""),
        "bucket_key": _five_minute_bucket(observed_at),
        "collected_at": observed_at,
        "stat_date": str(row.get("stat_date") or observed_at[:10]),
        "api_request_id": str(row.get("api_request_id") or ""),
    }
    for field in _METRIC_SNAPSHOT_FIELDS:
        snapshot[field] = row.get(field)
    return snapshot


def _metric_values_changed(
    previous: Optional[Mapping[str, Any]], current: Mapping[str, Any]
) -> bool:
    """Store history only when a business metric changes.

    ``pmc_promotion_material_latest`` is still refreshed every successful
    cycle, so skipping an unchanged history point does not reduce freshness or
    rule accuracy.  It only removes thousands of identical five-minute rows.
    """
    if not previous:
        return True
    for field in _METRIC_SNAPSHOT_FIELDS:
        old = previous.get(field)
        new = current.get(field)
        if new is None:
            continue
        try:
            if old is None or round(float(old), 8) != round(float(new), 8):
                return True
        except (TypeError, ValueError):
            if str(old or "") != str(new or ""):
                return True
    return False


def _parse_local_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _target_capability(target: Mapping[str, Any]) -> dict[str, Any]:
    capability = target.get("capability")
    if isinstance(capability, Mapping):
        return dict(capability)
    raw = target.get("capability_json")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _official_api_error_detail(exc: BaseException) -> str:
    """生成可直接展示给用户的脱敏官方错误证据。"""
    parts = [str(exc or "千川官方 API 请求失败").strip()]
    endpoint = str(getattr(exc, "endpoint", "") or "").strip()
    code = str(getattr(exc, "code", "") or "").strip()
    request_id = str(getattr(exc, "request_id", "") or "").strip()
    http_status = getattr(exc, "http_status", None)
    if endpoint:
        parts.append(f"接口 {endpoint}")
    if code:
        parts.append(f"错误码 {code}")
    if http_status not in (None, ""):
        parts.append(f"HTTP {http_status}")
    if request_id:
        parts.append(f"request_id {request_id}")
    return "；".join(part for part in parts if part)[:2000]


def _phase_is_due(
    capability: Mapping[str, Any],
    timestamp_key: str,
    interval_seconds: int,
    *,
    now: Optional[datetime] = None,
) -> bool:
    last_run = _parse_local_time(capability.get(timestamp_key))
    if last_run is None:
        return True
    return ((now or datetime.now()) - last_run).total_seconds() >= max(
        30, int(interval_seconds)
    )


def _collection_phase_plan(
    target: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Plan high/low-frequency reads without trusting stale empty caches."""
    capability = _target_capability(target)
    cached_units = capability.get("report_metric_units")
    if not isinstance(cached_units, Mapping):
        cached_units = {}
    waiting_live = (
        str(target.get("promotion_scene") or "").strip().lower() == "live"
        and str(target.get("platform_status") or "").strip().lower()
        == "waiting_live"
    )
    return {
        "capability": capability,
        "cached_units": dict(cached_units),
        "refresh_report_config": not cached_units
        or _phase_is_due(
            capability,
            "report_config_synced_at",
            REPORT_CONFIG_INTERVAL_SECONDS,
            now=now,
        ),
        "refresh_products": str(target.get("promotion_scene") or "") == "product"
        and _phase_is_due(
            capability,
            "product_catalog_synced_at",
            PRODUCT_CATALOG_INTERVAL_SECONDS,
            now=now,
        ),
        # 直播计划未开播时仍可配置策略；每轮复核开播状态，
        # 一旦平台返回投放中立即恢复真实写入资格。
        "refresh_plan_detail": waiting_live
        or _phase_is_due(
            capability,
            "plan_detail_synced_at",
            PLAN_DETAIL_INTERVAL_SECONDS,
            now=now,
        ),
        "force_plan_detail": waiting_live,
        "refresh_control_history": _phase_is_due(
            capability,
            "control_history_synced_at",
            CONTROL_HISTORY_INTERVAL_SECONDS,
            now=now,
        ),
    }


def _rotating_maintenance_phase(phase_plan: Mapping[str, Any]) -> str:
    """Choose at most one optional low-frequency phase for a hot cycle.

    Core material metrics and active control tasks are always collected.  The
    heavier, lower-frequency reads rotate so a coincident 20/30/60 minute
    boundary cannot make every plan perform all expensive pagination at once.
    """
    if bool(phase_plan.get("force_plan_detail")):
        return "plan_detail"
    phases = (
        # Product relations and historical control rows are both optional for
        # the core metrics pass and occur after the material request.  Refresh
        # them first so cached detail/config reads cannot delay fresh metrics.
        ("products", "refresh_products"),
        ("control_history", "refresh_control_history"),
        ("plan_detail", "refresh_plan_detail"),
        ("report_config", "refresh_report_config"),
    )
    capability = phase_plan.get("capability")
    last_phase = (
        str(capability.get("maintenance_last_phase") or "")
        if isinstance(capability, Mapping)
        else ""
    )
    names = [name for name, _ in phases]
    if last_phase in names:
        pivot = names.index(last_phase) + 1
        phases = phases[pivot:] + phases[:pivot]
    for name, key in phases:
        if bool(phase_plan.get(key)):
            return name
    return ""


def _fixed_cadence_due(
    started_at: Any,
    interval_seconds: int,
    *,
    now: Optional[datetime] = None,
) -> datetime:
    """Return the next start-to-start deadline for a completed hot cycle."""
    current = now or datetime.now()
    started = (
        started_at
        if isinstance(started_at, datetime)
        else _parse_local_time(started_at)
    ) or current
    due = started + timedelta(seconds=max(30, int(interval_seconds)))
    return due if due > current else current + timedelta(seconds=5)


def _fair_order_targets(
    targets: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Round-robin accounts while preserving oldest-first order per account."""
    buckets: "OrderedDict[str, deque[Mapping[str, Any]]]" = OrderedDict()
    for target in targets:
        account_key = str(
            target.get("aadvid")
            or target.get("account_uid")
            or target.get("target_uid")
            or "unknown"
        )
        buckets.setdefault(account_key, deque()).append(target)
    ordered: list[Mapping[str, Any]] = []
    while buckets:
        exhausted: list[str] = []
        for account_key, account_targets in buckets.items():
            ordered.append(account_targets.popleft())
            if not account_targets:
                exhausted.append(account_key)
        for account_key in exhausted:
            buckets.pop(account_key, None)
    return ordered


def _target_account_key(target: Mapping[str, Any]) -> str:
    return str(
        target.get("aadvid")
        or target.get("account_uid")
        or target.get("target_uid")
        or "unknown"
    )


def _account_collection_lock(account_key: str) -> threading.BoundedSemaphore:
    with _ACTIVE_LOCK:
        return _ACCOUNT_COLLECTION_LOCKS.setdefault(
            account_key,
            threading.BoundedSemaphore(ACCOUNT_MAX_CONCURRENT_PLANS),
        )


def _account_degraded_lock(account_key: str) -> threading.Lock:
    with _ACTIVE_LOCK:
        return _ACCOUNT_DEGRADED_LOCKS.setdefault(account_key, threading.Lock())


@contextmanager
def _account_collection_slot(account_key: str):
    """Allow two plan reads per account, or one after an account-level 429."""
    capacity = _account_collection_lock(account_key)
    capacity.acquire()
    degraded: Optional[threading.Lock] = None
    try:
        with _ACTIVE_LOCK:
            is_degraded = account_key in _ACCOUNT_RATE_LIMITED
        if is_degraded:
            degraded = _account_degraded_lock(account_key)
            degraded.acquire()
        yield
    finally:
        if degraded is not None:
            degraded.release()
        capacity.release()


def _observe_account_result(account_key: str, *, rate_limited: bool) -> None:
    with _ACTIVE_LOCK:
        if rate_limited:
            _ACCOUNT_RATE_LIMITED.add(account_key)
            _ACCOUNT_CLEAN_CYCLES[account_key] = 0
            return
        if account_key not in _ACCOUNT_RATE_LIMITED:
            return
        clean = int(_ACCOUNT_CLEAN_CYCLES.get(account_key, 0)) + 1
        if clean >= ADAPTIVE_RECOVERY_CLEAN_BATCHES:
            _ACCOUNT_RATE_LIMITED.discard(account_key)
            _ACCOUNT_CLEAN_CYCLES.pop(account_key, None)
        else:
            _ACCOUNT_CLEAN_CYCLES[account_key] = clean


def _quota_scope_key(owner: str, scope_type: str, scope_id: str) -> str:
    raw = f"{owner}|{scope_type}|{scope_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _persist_quota_backoff(
    *,
    owner: str,
    scope_type: str,
    scope_id: str,
    seconds: int,
    db: SQLiteStore,
    error: str = "rate_limit",
) -> None:
    backoff_until = datetime.now() + timedelta(seconds=max(30, int(seconds)))
    key = _quota_scope_key(owner, scope_type, scope_id)
    existing = db.select_one("api_quota_state", where={"scope_key": key}) or {}
    if not isinstance(existing, Mapping):
        # A database-compatible test double may not implement persistent quota
        # rows.  The in-memory backoff above remains authoritative for it.
        return
    count = int(existing.get("rate_limit_count") or 0) + 1
    db.insert_or_update(
        "api_quota_state",
        {
            "scope_key": key,
            "owner_username": owner,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "backoff_until": backoff_until.strftime("%Y-%m-%d %H:%M:%S"),
            "rate_limit_count": count,
            "last_error": str(error or "rate_limit")[:300],
            "last_request_at": _now(),
        },
        unique_fields=["scope_key"],
    )


def _persistent_backoff_remaining(
    *, owner: str, scope_type: str, scope_id: str, db: SQLiteStore
) -> int:
    row = db.select_one(
        "api_quota_state",
        where={"scope_key": _quota_scope_key(owner, scope_type, scope_id)},
    )
    if not isinstance(row, Mapping):
        return 0
    due = _parse_local_time(row.get("backoff_until"))
    if due is None:
        return 0
    return max(0, int((due - datetime.now()).total_seconds()))


def _account_backoff_remaining(
    account_key: str,
    *,
    db: Optional[SQLiteStore] = None,
) -> int:
    with _ACTIVE_LOCK:
        due = float(_ACCOUNT_BACKOFF_UNTIL.get(account_key, 0.0) or 0.0)
    remaining = max(0, int(round(due - time.monotonic())))
    if db is None:
        if remaining > 0:
            with _ACTIVE_LOCK:
                _ACCOUNT_RATE_LIMITED.add(account_key)
                _ACCOUNT_CLEAN_CYCLES[account_key] = 0
        return remaining
    owner = _owner_key()
    remaining = max(
        remaining,
        _persistent_backoff_remaining(
            owner=owner, scope_type="application", scope_id="official_api", db=db
        ),
        _persistent_backoff_remaining(
            owner=owner, scope_type="account", scope_id=account_key, db=db
        ),
    )
    if remaining > 0:
        # Rehydrate account degradation from the persisted Retry-After window
        # after a process restart. Once the window expires, ten clean cycles
        # are still required before the second account lane is restored.
        with _ACTIVE_LOCK:
            _ACCOUNT_RATE_LIMITED.add(account_key)
            _ACCOUNT_CLEAN_CYCLES[account_key] = 0
    return remaining


def _set_account_backoff(
    account_key: str,
    seconds: int,
    *,
    db: Optional[SQLiteStore] = None,
    include_application: bool = False,
    error: str = "rate_limit",
) -> None:
    with _ACTIVE_LOCK:
        _ACCOUNT_BACKOFF_UNTIL[account_key] = max(
            float(_ACCOUNT_BACKOFF_UNTIL.get(account_key, 0.0) or 0.0),
            time.monotonic() + max(30, int(seconds)),
        )
    if db is not None:
        owner = _owner_key()
        _persist_quota_backoff(
            owner=owner,
            scope_type="account",
            scope_id=account_key,
            seconds=seconds,
            db=db,
            error=error,
        )
        if include_application:
            _persist_quota_backoff(
                owner=owner,
                scope_type="application",
                scope_id="official_api",
                seconds=seconds,
                db=db,
                error=error,
            )


def _should_escalate_application_backoff(
    account_key: str,
    *,
    now_monotonic: Optional[float] = None,
    window_seconds: int = 60,
    distinct_account_threshold: int = 2,
) -> bool:
    """Escalate only when several advertisers are throttled together."""
    observed = time.monotonic() if now_monotonic is None else float(now_monotonic)
    cutoff = observed - max(10, int(window_seconds))
    with _ACTIVE_LOCK:
        stale = [
            key for key, timestamp in _RATE_LIMIT_ACCOUNT_EVENTS.items()
            if float(timestamp or 0.0) < cutoff
        ]
        for key in stale:
            _RATE_LIMIT_ACCOUNT_EVENTS.pop(key, None)
        _RATE_LIMIT_ACCOUNT_EVENTS[str(account_key or "unknown")] = observed
        return len(_RATE_LIMIT_ACCOUNT_EVENTS) >= max(
            2, int(distinct_account_threshold)
        )


def _ensure_collection_schema(store: SQLiteStore) -> None:
    """Initialize SQLite once per runtime database, not once per target."""
    raw_database = getattr(store, "config", {}).get("database")
    # Unit tests and embedders may supply a database-compatible mock rather
    # than a concrete SQLiteStore.  Its query methods are still usable, but a
    # synthetic ``Mock`` path must never be handed to os.path during schema
    # initialization.
    if raw_database is not None and not isinstance(raw_database, (str, bytes, os.PathLike)):
        return
    database = os.fspath(raw_database).strip() if raw_database is not None else ""
    # Separate in-memory databases must not share readiness state.
    key = f"memory:{id(store)}" if not database or database == ":memory:" else database
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY_DATABASES:
            return
        init_sqlite_schema(database=raw_database)
        _SCHEMA_READY_DATABASES.add(key)


def _adaptive_worker_limit(configured_workers: int) -> int:
    with _ADAPTIVE_LOCK:
        return max(1, min(int(configured_workers), int(_ADAPTIVE_WORKERS)))


def _observe_collection_results(results: Iterable[Mapping[str, Any]]) -> int:
    """Back off globally on 429, then recover one lane after clean batches."""
    global _ADAPTIVE_WORKERS, _ADAPTIVE_CLEAN_BATCHES
    rows = list(results)
    with _ADAPTIVE_LOCK:
        if any(str(row.get("error_kind") or "") == "rate_limit" for row in rows):
            _ADAPTIVE_WORKERS = 3 if _ADAPTIVE_WORKERS > 3 else 1
            _ADAPTIVE_CLEAN_BATCHES = 0
        elif any(bool(row.get("success")) for row in rows):
            # Only a batch with a real successful collection proves the quota
            # window is healthy. Deferred/backoff/permission failures must not
            # accidentally restore six workers without touching the API.
            _ADAPTIVE_CLEAN_BATCHES += 1
            if (
                _ADAPTIVE_WORKERS < COLLECTION_MAX_WORKERS
                and _ADAPTIVE_CLEAN_BATCHES >= ADAPTIVE_RECOVERY_CLEAN_BATCHES
            ):
                _ADAPTIVE_WORKERS = 3 if _ADAPTIVE_WORKERS <= 1 else COLLECTION_MAX_WORKERS
                _ADAPTIVE_CLEAN_BATCHES = 0
        return _ADAPTIVE_WORKERS


def _reset_adaptive_collection_state_for_tests() -> None:
    global _ADAPTIVE_WORKERS, _ADAPTIVE_CLEAN_BATCHES
    with _ADAPTIVE_LOCK:
        _ADAPTIVE_WORKERS = COLLECTION_MAX_WORKERS
        _ADAPTIVE_CLEAN_BATCHES = 0
    with _ACTIVE_LOCK:
        _ACCOUNT_COLLECTION_LOCKS.clear()
        _ACCOUNT_DEGRADED_LOCKS.clear()
        _ACCOUNT_RATE_LIMITED.clear()
        _ACCOUNT_CLEAN_CYCLES.clear()
        _ACCOUNT_BACKOFF_UNTIL.clear()
        _RATE_LIMIT_ACCOUNT_EVENTS.clear()


def _target_is_due(
    target: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    interval_seconds: int = COLLECTION_INTERVAL_SECONDS,
) -> bool:
    current = now or datetime.now()
    due_at = _parse_local_time(target.get("next_due_at"))
    if due_at is not None:
        return due_at <= current
    last_sync = _parse_local_time(target.get("last_sync_at"))
    if last_sync is None:
        return True
    return (current - last_sync).total_seconds() >= max(30, int(interval_seconds))


def _set_retry_due(
    target_uid: str,
    *,
    delay_seconds: int,
    db: SQLiteStore,
) -> None:
    retry_at = datetime.now() + timedelta(seconds=max(30, int(delay_seconds)))
    db.update(
        "promotion_target",
        {"next_due_at": retry_at.strftime("%Y-%m-%d %H:%M:%S")},
        where={"target_uid": target_uid},
    )


def _date_window(days: int = 0) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=max(0, int(days)))
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _merge_material_report(
    materials: Iterable[Mapping[str, Any]],
    report_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    report_by_material = {
        text_id(row.get("material_id")): row
        for row in report_rows
        if text_id(row.get("material_id"))
    }
    merged: list[dict[str, Any]] = []
    for material in materials:
        item = dict(material)
        report = report_by_material.get(text_id(item.get("material_id")))
        if report:
            stats = dict(item.get("stats_info") or {})
            stats.update(dict(report.get("stats_info") or {}))
            item["stats_info"] = stats
            if not str(item.get("material_name") or "").strip():
                item["material_name"] = str(report.get("material_name") or "")
        merged.append(item)
    return merged


def _metric_block(stats: Mapping[str, Any], *names: str) -> tuple[Any, Any]:
    for name in names:
        if name not in stats:
            continue
        value = stats.get(name)
        if isinstance(value, Mapping):
            return value.get("value"), first(value, "unit", "unit_type", "unitType")
        return value, None
    return None, None


def _metric(
    stats: Mapping[str, Any],
    units: Mapping[str, str],
    *names: str,
) -> Optional[float]:
    raw, inline_unit = _metric_block(stats, *names)
    if raw is None:
        return None
    unit = inline_unit
    if unit in (None, ""):
        for name in names:
            if name in units:
                unit = units[name]
                break
    return float(normalize_metric_value(raw, unit))


def _supported_material_metrics(units: Mapping[str, Any]) -> tuple[str, ...]:
    """Return only metrics exposed by the selected report topic."""
    return tuple(field for field in MATERIAL_METRICS if field in units)


def _status_number(value: Any) -> int:
    raw = str(value or "").strip().upper()
    if raw in {
        "ENABLE", "ENABLED", "ACTIVE", "DELIVERY", "DELIVERING",
        "DELIVERY_OK", "RUNNING", "SUCCESS",
    }:
        return 1
    if raw in {"PAUSE", "PAUSED", "DISABLE", "DISABLED", "DELETED", "FAILED", "REJECTED"}:
        return 0
    return -1


def _material_snapshot(
    material: Mapping[str, Any],
    *,
    target: Mapping[str, Any],
    units: Mapping[str, str],
    request_id: str,
) -> dict[str, Any]:
    raw = _mapping(material.get("raw"))
    stats = _mapping(material.get("stats_info"))
    if not stats:
        stats = _mapping(first(raw, "stats_info", "statsInfo", "stats", default={}))
    video = _mapping(first(raw, "video_info", "videoInfo", "video", default={}))
    cover = _mapping(first(video, "cover", "cover_info", "coverInfo", default={}))
    product_ids = [
        text_id(item)
        for item in (material.get("product_ids") or [])
        if text_id(item)
    ]
    return {
        "aadvid": text_id(target.get("aadvid")),
        "target_uid": str(target.get("target_uid") or ""),
        "ad_id": text_id(target.get("ad_id")),
        "promotion_scene": str(target.get("promotion_scene") or "product"),
        "plan_system": normalize_plan_system(target.get("plan_system")),
        "material_id": text_id(material.get("material_id")),
        "product_ids_json": json.dumps(product_ids, ensure_ascii=False, separators=(",", ":")),
        "video_name": str(material.get("material_name") or "")[:512],
        "material_status": _status_number(material.get("material_status")),
        "show_status": _status_number(first(raw, "show_status", "showStatus", "delivery_status")),
        "show_status_reason": str(first(raw, "show_status_reason", "showStatusReason"))[:1000],
        "upload_time": str(first(raw, "upload_time", "uploadTime", "create_time", "createTime"))[:64],
        "video_type": 1,
        "video_id": text_id(material.get("video_id") or first(video, "video_id", "videoId", "id")),
        "aweme_item_id": text_id(first(video, "aweme_item_id", "awemeItemId")) or None,
        "cover_url": str(first(cover, "url", "web_url", "webUrl", "image_url"))[:2000],
        "cover_width": first(cover, "width"),
        "cover_height": first(cover, "height"),
        "video_duration": first(video, "duration", "video_duration", "videoDuration"),
        "video_title": str(first(video, "title") or material.get("material_name") or "")[:1000],
        "lego_source": first(video, "lego_source", "legoSource"),
        "video_create_time": str(material.get("create_time") or first(video, "create_time", "createTime"))[:64],
        "tag_list": raw_json(first(raw, "tags", "tag_list", "tagList", default=[])),
        "stat_cost": _metric(stats, units, "stat_cost_for_roi2", "statCostForRoi2"),
        "order_settle_count_1h": _metric(stats, units, "total_order_settle_count_for_roi2_1h", "totalOrderSettleCountForRoi21H"),
        "order_settle_amount_1h": _metric(stats, units, "total_order_settle_amount_for_roi2_1h", "totalOrderSettleAmountForRoi21H"),
        "order_settle_rate_1h": _metric(stats, units, "total_order_settle_amount_rate_for_roi2_1h", "totalOrderSettleAmountRateForRoi21H"),
        "prepay_pay_order_count": _metric(stats, units, "total_prepay_and_pay_order_roi2", "totalPrepayAndPayOrderRoi2"),
        "pay_gmv_include_coupon": _metric(stats, units, "total_pay_order_gmv_include_coupon_for_roi2", "totalPayOrderGmvIncludeCouponForRoi2"),
        "prepay_pay_settle_1h": _metric(stats, units, "total_prepay_and_pay_settle_roi2_1h", "totalPrepayAndPaySettleRoi21H"),
        "refund_rate_1h": _metric(stats, units, "total_refund_order_gmv_for_roi2_1h_rate", "totalRefundOrderGmvForRoi21HRate"),
        "overall_order_count": _metric(stats, units, "total_pay_order_count_for_roi2", "totalPayOrderCountForRoi2"),
        "overall_show_count": _metric(
            stats, units,
            "live_show_count_for_roi2_v2", "liveShowCountForRoi2V2",
            "product_show_count_for_roi2", "productShowCountForRoi2",
        ),
        "overall_click_count": _metric(
            stats, units,
            "live_watch_count_for_roi2_v2", "liveWatchCountForRoi2V2",
            "product_click_count_for_roi2", "productClickCountForRoi2",
        ),
        "overall_ctr": _metric(
            stats, units,
            "live_cvr_rate_for_roi2_v2", "liveCvrRateForRoi2V2",
            "product_cvr_rate_for_roi2", "productCvrRateForRoi2",
        ),
        "overall_conversion_rate": _metric(
            stats, units,
            "live_convert_rate_for_roi2_v2", "liveConvertRateForRoi2V2",
            "product_convert_rate_for_roi2", "productConvertRateForRoi2",
        ),
        "data_source": "qianchuan_open_api",
        "api_request_id": str(request_id or "")[:256],
        "stat_date": datetime.now().strftime("%Y-%m-%d"),
    }


def _control_snapshot(
    task: Mapping[str, Any],
    *,
    target: Mapping[str, Any],
    request_id: str,
    units: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    raw = _mapping(task.get("raw"))
    stats = _mapping(first(raw, "stats_info", "statsInfo", "stats", "metrics", default={}))
    material_ids = [text_id(item) for item in (task.get("material_ids") or []) if text_id(item)]
    materials = [{"material_id": item, "title": ""} for item in material_ids]

    def raw_metric(*names: str) -> Any:
        # Control-task metrics follow the same unit contract as material
        # reports.  Converting here prevents cents/ratio fields from entering
        # stop rules with a different scale than the dashboard.
        return _metric(stats, units or {}, *names)

    status = str(task.get("status") or "").upper()
    active = status in {
        "ENABLE",
        "ENABLED",
        "ACTIVE",
        "RUNNING",
        "DELIVERING",
        "PROCESSING",
    }
    return {
        "assist_task_id": text_id(task.get("task_id")),
        "aadvid": text_id(target.get("aadvid")),
        "account_uid": str(target.get("account_uid") or ""),
        "ad_id": text_id(target.get("ad_id")),
        "target_uid": str(target.get("target_uid") or ""),
        "promotion_scene": str(target.get("promotion_scene") or "product"),
        "plan_system": normalize_plan_system(target.get("plan_system")),
        "task_name": str(task.get("task_name") or "")[:512],
        "budget": str(task.get("budget") or ""),
        "bid": str(first(raw, "bid") or ""),
        "start_time": str(first(raw, "start_time", "startTime"))[:64],
        "end_time": str(first(raw, "end_time", "endTime"))[:64],
        "modify_time": str(task.get("modify_time") or "")[:64],
        "create_time": str(task.get("create_time") or "")[:64],
        "ecp_roi2_goal": first(raw, "roi2_goal", "ecp_roi2_goal", "roi2Goal"),
        "ad_delivery_type": 0 if active else 1,
        "ad_delivery_name": status,
        "daily_delivery_seconds": (
            int(Decimal(str(task.get("duration"))) * Decimal("3600"))
            if task.get("duration") not in (None, "")
            else None
        ),
        "stat_cost_for_roi2_assist": raw_metric("stat_cost_for_roi2_assist", "statCostForRoi2Assist"),
        "total_pay_order_count_for_roi2_assist": raw_metric("total_pay_order_count_for_roi2_assist", "totalPayOrderCountForRoi2Assist"),
        "total_pay_order_gmv_include_coupon_for_roi2_assist": raw_metric("total_pay_order_gmv_include_coupon_for_roi2_assist", "totalPayOrderGmvIncludeCouponForRoi2Assist"),
        "total_prepay_and_pay_order_roi2_assist": raw_metric("total_prepay_and_pay_order_roi2_assist", "totalPrepayAndPayOrderRoi2Assist"),
        "total_order_settle_amount_for_roi2_1h_assist": raw_metric("total_order_settle_amount_for_roi2_1h_assist", "totalOrderSettleAmountForRoi21HAssist"),
        "total_prepay_and_pay_settle_roi2_1h_assist": raw_metric("total_prepay_and_pay_settle_roi2_1h_assist", "totalPrepayAndPaySettleRoi21HAssist"),
        "assist_materials_json": json.dumps(materials, ensure_ascii=False, separators=(",", ":")),
        "data_source": "qianchuan_open_api",
        "api_request_id": str(request_id or "")[:256],
        "reconciliation_status": "not_required",
    }


def _control_is_active(task: Mapping[str, Any]) -> bool:
    return str(task.get("status") or "").strip().upper() in {
        "ENABLE",
        "ENABLED",
        "ACTIVE",
        "RUNNING",
        "DELIVERING",
        "PROCESSING",
    }


def _count_rows(store: SQLiteStore, table: str, target_uid: str) -> int:
    rows = store.execute(
        f"SELECT COUNT(1) AS count FROM {table} WHERE target_uid=?",
        (target_uid,),
        fetch=True,
    ) or []
    return int((rows[0] if rows else {}).get("count") or 0)


def _patch_target_sync_in_transaction(
    store: SQLiteStore,
    target_uid: str,
    *,
    connection: Any,
    status: str,
    error: str,
    synced: bool,
    capability_updates: Mapping[str, Any],
    capability_remove_keys: Iterable[str] = (),
) -> None:
    """Commit hot data and its success marker in the same SQLite transaction."""
    row = store.select_one(
        "promotion_target",
        fields="capability_json",
        where={"target_uid": target_uid},
        connection=connection,
    )
    if not row:
        raise RuntimeError("监控目标不存在")
    try:
        capability = json.loads(str(row.get("capability_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        capability = {}
    if not isinstance(capability, dict):
        capability = {}
    capability.update(dict(capability_updates or {}))
    for key in capability_remove_keys or ():
        capability.pop(str(key), None)
    capability_json = json.dumps(
        capability,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if synced:
        store.execute(
            "UPDATE promotion_target SET last_status=?,last_error=?,"
            "capability_json=?,last_sync_at=datetime('now','+8 hours'),"
            "updated_at=datetime('now','+8 hours') WHERE target_uid=?",
            (status[:64], error[:2000], capability_json, target_uid),
            connection=connection,
        )
    else:
        store.update(
            "promotion_target",
            {
                "last_status": status[:64],
                "last_error": error[:2000],
                "capability_json": capability_json,
            },
            where={"target_uid": target_uid},
            connection=connection,
        )


def _bulk_upsert_rows(
    connection: Any,
    table: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    unique_fields: Iterable[str],
) -> int:
    """Native SQLite UPSERT for measured high-frequency collector tables."""
    payloads = [dict(row) for row in rows if isinstance(row, Mapping) and row]
    if not payloads:
        return 0
    columns: list[str] = []
    seen: set[str] = set()
    for row in payloads:
        for column in row:
            name = str(column)
            if name not in seen:
                seen.add(name)
                columns.append(name)
    keys = [str(field) for field in unique_fields]
    if any(key not in seen for key in keys):
        raise RuntimeError(f"{table} 批量写入缺少唯一键")
    quoted_columns = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    conflict = ",".join(f'"{key}"' for key in keys)
    updates = ",".join(
        f'"{column}"=excluded."{column}"'
        for column in columns
        if column not in keys
    )
    sql = (
        f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders}) '
        f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
    )
    connection.executemany(
        sql,
        [tuple(row.get(column) for column in columns) for row in payloads],
    )
    return len(payloads)


def _empty_is_suspicious(
    *,
    current_count: int,
    previous_count: int,
    capability: Mapping[str, Any],
    streak_key: str,
) -> tuple[bool, int]:
    if current_count or previous_count <= 0:
        return False, 0
    try:
        previous_streak = int(capability.get(streak_key) or 0)
    except (TypeError, ValueError):
        previous_streak = 0
    streak = previous_streak + 1
    # A single successful empty response is not trusted. A second complete
    # empty result may replace the old snapshot.
    return streak < 2, streak


def collect_target(
    target: Mapping[str, Any],
    *,
    rotate_maintenance: bool = False,
    db: Optional[SQLiteStore] = None,
) -> dict[str, Any]:
    store = db or SQLiteStore()
    _ensure_collection_schema(store)
    service = get_official_api_service()
    aavid = text_id(target.get("aadvid"))
    ad_id = text_id(target.get("ad_id"))
    target_uid = str(target.get("target_uid") or "")
    expected_scene = str(target.get("promotion_scene") or "")
    expected_system = normalize_plan_system(target.get("plan_system"))
    phase_plan = _collection_phase_plan(target)
    capability = phase_plan["capability"]
    maintenance_phase = (
        _rotating_maintenance_phase(phase_plan) if rotate_maintenance else "all"
    )
    maintenance_errors: list[str] = []

    goal = str(capability.get("marketing_goal") or "")
    refresh_plan_detail = bool(phase_plan["refresh_plan_detail"]) and (
        not rotate_maintenance or maintenance_phase == "plan_detail"
    )
    detail_request_id = str(capability.get("plan_detail_request_id") or "")
    detail_status = str(target.get("platform_status") or "")
    detail_capability_updates: dict[str, Any] = {}
    if refresh_plan_detail or not goal:
        try:
            detail, detail_response = service.get_plan_detail(aavid, ad_id)
            actual_scene = normalize_promotion_scene(detail.get("marketing_goal"))
            actual_system = normalize_api_plan_system(detail.get("adlab_scene"))
            if detail.get("aavid") != aavid or detail.get("ad_id") != ad_id:
                raise RuntimeError("官方 API 计划详情与监控账户或计划不一致")
            if actual_scene != expected_scene or actual_system != expected_system:
                raise RuntimeError("官方 API 计划详情的推广方式或计划体系已变化")
            detail_status = str(detail.get("platform_status") or "")
            if detail_status not in {"active", "learning", "waiting_live"}:
                raise RuntimeError("官方 API 返回的计划当前不可投放")
            goal = str(detail.get("marketing_goal") or "")
            detail_request_id = str(detail_response.request_id or "")
            # A selected live plan may move between waiting for broadcast and
            # live while the hot collector is running.
            update_target_catalog_evidence(
                target_uid,
                platform_status=detail_status,
                verification_state="verified",
                plan_system=expected_system,
                promotion_scene=expected_scene,
                db=store,
            )
            from services.official_api_catalog import _capability as catalog_capability

            detail_capability_updates = catalog_capability(detail, target_uid)
        except (ApiTokenError, ApiPermissionError):
            raise
        except RuntimeError as exc:
            # Account/plan/scene/system/status mismatches are deterministic
            # safety failures, not transient transport errors. Stop this
            # selected plan until a later trusted catalog/detail proof restores it.
            record_target_verification_failure(target_uid, str(exc), db=store)
            raise
        except (ApiRateLimitError, ApiRequestError, TimeoutError, ConnectionError, OSError) as exc:
            if not goal:
                raise
            refresh_plan_detail = False
            maintenance_errors.append(f"plan_detail:{exc}")
            logger.warning(
                "计划详情低频刷新失败，继续使用已验证证据 target=%s error=%s",
                target_uid,
                exc,
            )
    report_config_refreshed = bool(phase_plan["refresh_report_config"]) and (
        not rotate_maintenance or maintenance_phase == "report_config"
    )
    report_config_request_id = str(
        capability.get("report_config_request_id") or ""
    )
    units = dict(phase_plan["cached_units"])
    if report_config_refreshed or not units:
        try:
            units, config_response = service.get_report_config(
                aavid,
                plan_system=expected_system,
                promotion_scene=expected_scene,
            )
            report_config_request_id = config_response.request_id
            report_config_refreshed = True
        except (ApiTokenError, ApiPermissionError):
            raise
        except (ApiRateLimitError, ApiRequestError, TimeoutError, ConnectionError, OSError) as exc:
            if not units:
                raise
            report_config_refreshed = False
            maintenance_errors.append(f"report_config:{exc}")
            logger.warning(
                "报表字段配置低频刷新失败，继续使用已验证单位 target=%s error=%s",
                target_uid,
                exc,
            )
    if not units:
        raise RuntimeError("官方 API 报表配置未返回字段单位，本轮数据不入库")

    # V1A rules use today's cumulative values. Restricting the material query
    # to today also avoids repeatedly paging through historical material rows.
    start_date, end_date = _date_window(0)
    # The material endpoint rejects fields that do not belong to the selected
    # report topic (for example live_show_count on a product plan).  The report
    # config is the source of truth for the current plan class, so only request
    # metrics that the platform explicitly exposes for this topic.
    supported_material_metrics = _supported_material_metrics(units)
    if not supported_material_metrics:
        raise RuntimeError(
            "官方 API 报表字段已变化，未找到可用素材指标，本轮数据不入库"
        )
    patch_target_sync_state(
        target_uid,
        status="collecting",
        error="",
        synced=False,
        capability_updates={"collection_stage": "scanning_active_materials"},
        db=store,
    )
    materials, material_request_ids = service.list_plan_materials(
        aavid,
        ad_id,
        start_date=start_date,
        end_date=end_date,
        fields=supported_material_metrics,
        delivery_only=True,
        parallel_workers=3,
    )
    report_result = service.list_material_report(
        aavid,
        plan_system=expected_system,
        promotion_scene=expected_scene,
        start_date=start_date,
        end_date=end_date,
        metrics=supported_material_metrics,
    )
    if isinstance(report_result, tuple) and len(report_result) == 2:
        material_report_rows, material_report_request_ids = report_result
    else:  # compatibility for narrow legacy/test doubles
        material_report_rows, material_report_request_ids = [], []
    materials = _merge_material_report(materials, material_report_rows)
    material_request_id = (
        material_report_request_ids[-1]
        if material_report_request_ids
        else material_request_ids[-1]
        if material_request_ids
        else detail_request_id
    )
    snapshots = [
        _material_snapshot(item, target=target, units=units, request_id=material_request_id)
        for item in materials
        if text_id(item.get("material_id"))
    ]
    active_material_with_spend_count = sum(
        1 for row in snapshots if float(row.get("stat_cost") or 0) > 0
    )
    patch_target_sync_state(
        target_uid,
        status="collecting",
        error="",
        synced=False,
        capability_updates={
            "collection_stage": "updating_metrics",
            "active_material_count": len(snapshots),
            "material_scan_page_count": max(1, len(material_request_ids) - 1),
        },
        db=store,
    )

    # If the computer was closed across a date boundary, recover yesterday's
    # final cumulative metrics once into history. They never replace today's
    # latest state and never participate in today's rule evaluation.
    recovery_snapshots: list[dict[str, Any]] = []
    recovery_completed = False
    recovery_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    last_sync = _parse_local_time(target.get("last_sync_at"))
    recovery_needed = bool(
        last_sync
        and last_sync.date() < datetime.now().date()
        and str(capability.get("recovery_backfill_date") or "") != recovery_date
    )
    if recovery_needed:
        try:
            recovery_materials, recovery_request_ids = service.list_plan_materials(
                aavid,
                ad_id,
                start_date=recovery_date,
                end_date=recovery_date,
                fields=supported_material_metrics,
                delivery_only=True,
                parallel_workers=3,
            )
            recovery_report_result = service.list_material_report(
                aavid,
                plan_system=expected_system,
                promotion_scene=expected_scene,
                start_date=recovery_date,
                end_date=recovery_date,
                metrics=supported_material_metrics,
            )
            if (
                isinstance(recovery_report_result, tuple)
                and len(recovery_report_result) == 2
            ):
                recovery_report_rows, recovery_report_request_ids = (
                    recovery_report_result
                )
            else:
                recovery_report_rows, recovery_report_request_ids = [], []
            recovery_materials = _merge_material_report(
                recovery_materials, recovery_report_rows
            )
            recovery_request_id = (
                recovery_report_request_ids[-1]
                if recovery_report_request_ids
                else recovery_request_ids[-1]
                if recovery_request_ids
                else ""
            )
            recovery_snapshots = [
                {
                    **_material_snapshot(
                        item,
                        target=target,
                        units=units,
                        request_id=recovery_request_id,
                    ),
                    "stat_date": recovery_date,
                }
                for item in recovery_materials
                if text_id(item.get("material_id"))
            ]
            recovery_completed = True
        except (ApiTokenError, ApiPermissionError):
            raise
        except (
            ApiRateLimitError,
            ApiRequestError,
            RuntimeError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as exc:
            maintenance_errors.append(f"recovery_backfill:{exc}")

    products_refreshed = bool(phase_plan["refresh_products"]) and (
        not rotate_maintenance or maintenance_phase == "products"
    )
    products: list[dict[str, Any]] = []
    product_request_ids: list[str] = []
    if products_refreshed:
        try:
            products, product_request_ids = service.list_plan_products(
                aavid,
                ad_id,
                start_date=start_date,
                end_date=end_date,
            )
        except (ApiTokenError, ApiPermissionError):
            raise
        except (
            ApiRateLimitError,
            ApiRequestError,
            RuntimeError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as exc:
            if not rotate_maintenance:
                raise
            products_refreshed = False
            maintenance_errors.append(f"products:{exc}")
            logger.warning(
                "商品目录低频刷新失败，不影响本轮核心指标 target=%s error=%s",
                target_uid,
                exc,
            )

    now = datetime.now()
    refresh_control_history = bool(phase_plan["refresh_control_history"]) and (
        not rotate_maintenance or maintenance_phase == "control_history"
    )

    def fetch_controls(
        days: int, *, active_only: bool = False
    ) -> tuple[list[dict[str, Any]], list[str]]:
        return service.list_control_tasks(
            aavid,
            ad_id=ad_id,
            marketing_goal=goal,
            start_time=(now - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00"),
            end_time=(now + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59"),
            active_only=active_only,
        )

    # Active retarget task metrics (spend/orders/GMV/ROI/status/budget/duration)
    # are part of every five-minute hot cycle.  Hourly 179-day history is an
    # optional supplement and may never prevent the hot seven-day read.
    active_control_task_count = 0
    if rotate_maintenance:
        hot_control_tasks, control_request_ids = fetch_controls(
            CONTROL_HOT_WINDOW_DAYS,
            active_only=True,
        )
        control_tasks = [
            item for item in hot_control_tasks if _control_is_active(item)
        ]
        active_control_task_count = len(control_tasks)
        if refresh_control_history:
            try:
                history_tasks, history_request_ids = fetch_controls(
                    179, active_only=False
                )
                # Historical rows are refreshed only in this low-frequency
                # phase. Active rows from the hot request remain authoritative
                # for stop-rule freshness.
                by_task = {
                    text_id(item.get("task_id")): item
                    for item in history_tasks
                    if text_id(item.get("task_id"))
                }
                for item in control_tasks:
                    by_task[text_id(item.get("task_id"))] = item
                control_tasks = list(by_task.values())
                control_request_ids = [
                    *control_request_ids,
                    *history_request_ids,
                ]
            except (ApiTokenError, ApiPermissionError):
                raise
            except (
                ApiRateLimitError,
                ApiRequestError,
                RuntimeError,
                TimeoutError,
                ConnectionError,
                OSError,
            ) as exc:
                refresh_control_history = False
                maintenance_errors.append(f"control_history:{exc}")
                logger.warning(
                    "追投任务历史补录失败，已保留本轮活动任务数据 target=%s error=%s",
                    target_uid,
                    exc,
                )
    else:
        raw_control_tasks, control_request_ids = fetch_controls(
            179 if refresh_control_history else CONTROL_HOT_WINDOW_DAYS,
            active_only=not refresh_control_history,
        )
        control_tasks = (
            raw_control_tasks
            if refresh_control_history
            else [item for item in raw_control_tasks if _control_is_active(item)]
        )
        active_control_task_count = sum(
            1 for item in raw_control_tasks if _control_is_active(item)
        )
    control_request_id = control_request_ids[-1] if control_request_ids else ""
    control_rows = [
        _control_snapshot(
            item,
            target=target,
            request_id=control_request_id,
            units=units,
        )
        for item in control_tasks
        if str(item.get("scene") or "").upper() in {"", "MATERIAL_ADD_BUDGET"}
        and text_id(item.get("task_id"))
    ]

    active_material_rows = store.execute(
        "SELECT COUNT(1) AS count FROM pmc_promotion_material_latest "
        "WHERE target_uid=? AND delivery_state='delivering'",
        (target_uid,),
        fetch=True,
    ) or []
    previous_material_count = int(
        (active_material_rows[0] if active_material_rows else {}).get("count") or 0
    )
    material_suspicious, material_empty_streak = _empty_is_suspicious(
        current_count=len(snapshots),
        previous_count=previous_material_count,
        capability=capability,
        streak_key="material_empty_streak",
    )
    active_control_rows = store.execute(
        "SELECT COUNT(1) AS count FROM pmc_roi2_assist_task "
        "WHERE target_uid=? AND ad_delivery_type=0",
        (target_uid,),
        fetch=True,
    ) or []
    previous_control_count = int(
        (active_control_rows[0] if active_control_rows else {}).get("count") or 0
    )
    control_suspicious, control_empty_streak = _empty_is_suspicious(
        current_count=active_control_task_count,
        previous_count=previous_control_count,
        capability=capability,
        streak_key="control_empty_streak",
    )
    previous_product_count = _count_rows(store, "promotion_product", target_uid)
    product_suspicious, product_empty_streak = _empty_is_suspicious(
        current_count=len(products),
        previous_count=previous_product_count,
        capability=capability,
        streak_key="product_empty_streak",
    ) if products_refreshed else (False, int(capability.get("product_empty_streak") or 0))

    cycle_observed_at = _now()
    any_suspicious_empty = bool(material_suspicious or control_suspicious)
    with _ACTIVE_LOCK:
        account_concurrency = (
            1 if _target_account_key(target) in _ACCOUNT_RATE_LIMITED
            else ACCOUNT_MAX_CONCURRENT_PLANS
        )
    core_capability_updates: dict[str, Any] = {
        "source": "qianchuan_open_api",
        "material_sync_complete": not material_suspicious,
        "material_count": (
            previous_material_count if material_suspicious else len(snapshots)
        ),
        "active_material_count": (
            previous_material_count if material_suspicious else len(snapshots)
        ),
        "active_material_with_spend_count": (
            int(capability.get("active_material_with_spend_count") or 0)
            if material_suspicious
            else active_material_with_spend_count
        ),
        "material_scan_total_count": len(materials),
        "material_scan_page_count": max(1, len(material_request_ids) - 1),
        "material_empty_streak": material_empty_streak,
        "control_task_sync_complete": not control_suspicious,
        "control_task_count": (
            previous_control_count
            if control_suspicious
            else active_control_task_count
        ),
        "active_control_task_count": (
            previous_control_count
            if control_suspicious
            else active_control_task_count
        ),
        "control_empty_streak": control_empty_streak,
        "assist_sync_enabled": True,
        "assist_sync_in_progress": False,
        "assist_sync_ok": not control_suspicious,
        "assist_synced_at": cycle_observed_at,
        "material_request_ids": material_request_ids,
        "material_report_request_ids": material_report_request_ids,
        "material_report_count": len(material_report_rows),
        "control_request_ids": control_request_ids,
        "collected_at": cycle_observed_at,
        "collection_window_start": start_date,
        "collection_window_end": end_date,
        "collection_scope": "active_video_materials_and_running_scene2_tasks",
        "collection_stage": "healthy",
        "account_collection_concurrency": account_concurrency,
        "application_worker_limit": _adaptive_worker_limit(
            COLLECTION_MAX_WORKERS
        ),
        "application_qps_limit": 12,
        "account_qps_limit": 2,
        **detail_capability_updates,
    }
    if rotate_maintenance and maintenance_phase:
        core_capability_updates.update(
            {
                "maintenance_last_phase": maintenance_phase,
                "maintenance_last_attempted_at": _now(),
            }
        )
    if recovery_completed:
        core_capability_updates.update(
            {
                "recovery_backfill_date": recovery_date,
                "recovery_backfill_count": len(recovery_snapshots),
            }
        )
    if maintenance_errors:
        core_capability_updates.update(
            {
                "maintenance_error_at": _now(),
                "maintenance_error": "; ".join(maintenance_errors)[:1000],
            }
        )
    if refresh_plan_detail:
        core_capability_updates.update(
            {
                "plan_detail_synced_at": _now(),
                "plan_detail_request_id": detail_request_id,
                "marketing_goal": goal,
            }
        )
    if refresh_control_history:
        core_capability_updates["control_history_synced_at"] = _now()
    if report_config_refreshed:
        core_capability_updates.update(
            {
                "report_config_request_id": report_config_request_id,
                "report_config_synced_at": _now(),
                "report_metric_units": dict(units),
            }
        )
    current_snapshot_ids = [
        str(row.get("material_id") or "")
        for row in snapshots
        if str(row.get("material_id") or "")
    ]
    if current_snapshot_ids:
        current_placeholders = ",".join("?" for _ in current_snapshot_ids)
        previous_latest_rows = store.execute(
            "SELECT * FROM pmc_promotion_material_latest WHERE target_uid=? "
            "AND (delivery_state IN ('delivering','pending_inactive') "
            f"OR material_id IN ({current_placeholders}))",
            (target_uid, *current_snapshot_ids),
            fetch=True,
        ) or []
    else:
        previous_latest_rows = store.execute(
            "SELECT * FROM pmc_promotion_material_latest WHERE target_uid=? "
            "AND delivery_state IN ('delivering','pending_inactive')",
            (target_uid,),
            fetch=True,
        ) or []
    previous_latest_by_material = {
        str(item.get("material_id") or ""): item
        for item in previous_latest_rows
        if str(item.get("material_id") or "")
    }
    previous_control_rows = store.select(
        "pmc_roi2_assist_task", where={"target_uid": target_uid}
    ) or []
    previous_control_by_task = {
        str(item.get("assist_task_id") or ""): item
        for item in previous_control_rows
        if str(item.get("assist_task_id") or "")
    }
    recovery_metric_upserts = [
        _metric_snapshot_row(
            recovery_row,
            target=target,
            observed_at=f"{recovery_date} 23:59:59",
        )
        for recovery_row in recovery_snapshots
    ]
    latest_upserts: list[dict[str, Any]] = []
    metric_upserts: list[dict[str, Any]] = []
    for row in snapshots:
        material_id = str(row.get("material_id") or "")
        previous_latest = previous_latest_by_material.get(material_id) or {}
        effective_row = dict(row)
        for field in _METRIC_SNAPSHOT_FIELDS:
            if (
                effective_row.get(field) is None
                and previous_latest.get(field) is not None
            ):
                effective_row[field] = previous_latest.get(field)
        metrics_changed = _metric_values_changed(previous_latest, effective_row)
        latest_row = dict(effective_row)
        latest_row.update(
            {
                "collected_at": cycle_observed_at,
                "last_seen_at": cycle_observed_at,
                "delivery_state": "delivering",
                "updated_at": cycle_observed_at,
            }
        )
        latest_upserts.append(latest_row)
        if metrics_changed:
            metric_upserts.append(
                _metric_snapshot_row(
                    effective_row,
                    target=target,
                    observed_at=cycle_observed_at,
                )
            )
    control_upserts: list[dict[str, Any]] = []
    for row in control_rows:
        task_id = str(row.get("assist_task_id") or "")
        effective_control = dict(row)
        previous_control = previous_control_by_task.get(task_id) or {}
        for field in (
            "stat_cost_for_roi2_assist",
            "total_pay_order_count_for_roi2_assist",
            "total_pay_order_gmv_include_coupon_for_roi2_assist",
            "total_prepay_and_pay_order_roi2_assist",
            "total_order_settle_amount_for_roi2_1h_assist",
            "total_prepay_and_pay_settle_roi2_1h_assist",
        ):
            if (
                effective_control.get(field) is None
                and previous_control.get(field) is not None
            ):
                effective_control[field] = previous_control.get(field)
        effective_control["updated_at"] = cycle_observed_at
        control_upserts.append(effective_control)
    with store.transaction() as connection:
        _bulk_upsert_rows(
            connection,
            "pmc_material_metric_snapshot",
            recovery_metric_upserts,
            unique_fields=(
                "account_username",
                "target_uid",
                "material_id",
                "bucket_key",
            ),
        )
        _bulk_upsert_rows(
            connection,
            "pmc_promotion_material_latest",
            latest_upserts,
            unique_fields=("target_uid", "material_id"),
        )
        _bulk_upsert_rows(
            connection,
            "pmc_material_metric_snapshot",
            metric_upserts,
            unique_fields=(
                "account_username",
                "target_uid",
                "material_id",
                "bucket_key",
            ),
        )
        if snapshots:
            current_material_ids = [str(row["material_id"]) for row in snapshots]
            placeholders = ",".join("?" for _ in current_material_ids)
            store.execute(
                "UPDATE pmc_promotion_material_latest SET delivery_state="
                "CASE WHEN delivery_state='pending_inactive' THEN 'removed' "
                "ELSE 'pending_inactive' END, "
                "updated_at=datetime('now', '+8 hours') WHERE target_uid=? "
                "AND delivery_state IN ('delivering','pending_inactive') "
                f"AND material_id NOT IN ({placeholders})",
                (target_uid, *current_material_ids),
                connection=connection,
            )
        elif not material_suspicious:
            store.execute(
                "UPDATE pmc_promotion_material_latest SET delivery_state='removed', "
                "updated_at=datetime('now', '+8 hours') WHERE target_uid=? "
                "AND delivery_state IN ('delivering','pending_inactive')",
                (target_uid,),
                connection=connection,
            )
        _bulk_upsert_rows(
            connection,
            "pmc_roi2_assist_task",
            control_upserts,
            unique_fields=("target_uid", "assist_task_id"),
        )
        # The API list call is complete-or-raise.  Only after a complete list
        # may stale local control tasks be removed; otherwise old complete data
        # remains available and no stop candidate is created from a partial page.
        current_task_ids = [str(row["assist_task_id"]) for row in control_rows]
        if current_task_ids:
            placeholders = ",".join("?" for _ in current_task_ids)
            if refresh_control_history:
                store.execute(
                    "DELETE FROM pmc_roi2_assist_task WHERE target_uid=? "
                    f"AND assist_task_id NOT IN ({placeholders})",
                    (target_uid, *current_task_ids),
                    connection=connection,
                )
            else:
                # Scene 2 tasks last at most one day in the current UI. Any
                # previously active row absent from the complete seven-day hot
                # list is no longer an actionable stop target.
                store.execute(
                    "UPDATE pmc_roi2_assist_task SET ad_delivery_type=1, "
                    "ad_delivery_name='NOT_RETURNED_BY_HOT_SYNC', "
                    "updated_at=datetime('now', '+8 hours') "
                    "WHERE target_uid=? AND ad_delivery_type=0 "
                    f"AND assist_task_id NOT IN ({placeholders})",
                    (target_uid, *current_task_ids),
                    connection=connection,
                )
        elif not control_suspicious:
            if refresh_control_history:
                store.execute(
                    "DELETE FROM pmc_roi2_assist_task WHERE target_uid=?",
                    (target_uid,),
                    connection=connection,
                )
            else:
                store.execute(
                    "UPDATE pmc_roi2_assist_task SET ad_delivery_type=1, "
                    "ad_delivery_name='NOT_RETURNED_BY_HOT_SYNC', "
                    "updated_at=datetime('now', '+8 hours') "
                    "WHERE target_uid=? AND ad_delivery_type=0",
                    (target_uid,),
                    connection=connection,
                )

        _patch_target_sync_in_transaction(
            store,
            target_uid,
            connection=connection,
            status="suspicious_empty" if any_suspicious_empty else "ok",
            error=(
                "官方 API 本轮异常返回空数据，已保留上次可信结果并等待复核"
                if any_suspicious_empty
                else ""
            ),
            synced=not any_suspicious_empty,
            capability_updates=core_capability_updates,
            capability_remove_keys=(
                "collection_error_at",
                "collection_error_kind",
                "collection_retry_seconds",
                "collection_consecutive_failures",
                *(
                    ()
                    if maintenance_errors
                    else ("maintenance_error_at", "maintenance_error")
                ),
            ),
        )

    if products_refreshed and not product_suspicious:
        if products:
            upsert_products(target_uid, products, db=store)
        current_product_ids = [
            text_id(item.get("product_id"))
            for item in products
            if text_id(item.get("product_id"))
        ]
        with store.transaction() as connection:
            if current_product_ids:
                placeholders = ",".join("?" for _ in current_product_ids)
                store.execute(
                    "DELETE FROM promotion_product WHERE target_uid=? "
                    f"AND product_id NOT IN ({placeholders})",
                    (target_uid, *current_product_ids),
                    connection=connection,
                )
            else:
                store.execute(
                    "DELETE FROM promotion_product WHERE target_uid=?",
                    (target_uid,),
                    connection=connection,
                )
    product_to_material: dict[str, list[str]] = {}
    for product in products:
        pid = text_id(product.get("product_id"))
        for mid in product.get("material_ids") or []:
            product_to_material.setdefault(text_id(mid), []).append(pid)
    for material in materials:
        mid = text_id(material.get("material_id"))
        pids = list(material.get("product_ids") or []) or product_to_material.get(mid, [])
        # During a metrics-only cycle, an endpoint that omits product IDs must
        # not erase the last complete product-material relationship snapshot.
        if pids or (products_refreshed and not product_suspicious):
            replace_material_product_links(
                target_uid,
                mid,
                pids,
                material_name=str(material.get("material_name") or ""),
                db=store,
            )

    post_commit_capability_updates = dict(core_capability_updates)
    if products_refreshed:
        post_commit_capability_updates.update(
            {
                "product_catalog_synced_at": _now(),
                "product_sync_complete": not product_suspicious,
                "product_count": (
                    previous_product_count
                    if product_suspicious
                    else len(products)
                ),
                "product_empty_streak": product_empty_streak,
                "product_request_ids": product_request_ids,
            }
        )
    # The transaction above already committed latest rows and last_sync_at.
    # This no-op capability merge only asks the existing helper to recompute
    # eligibility after the atomic commit is visible; it cannot move the
    # success timestamp into a second transaction.
    patch_target_sync_state(
        target_uid,
        status="suspicious_empty" if any_suspicious_empty else "ok",
        error=(
            "官方 API 本轮异常返回空数据，已保留上次可信结果并等待复核"
            if any_suspicious_empty
            else ""
        ),
        synced=False,
        capability_updates=post_commit_capability_updates,
        db=store,
    )
    # 计划刚加入监控或本轮数据更新完成后，立刻让规则线程复核；不再让用户
    # 额外等待下一次 5 分钟轮询。局部导入避免采集模块与调度模块循环加载。
    try:
        from services.retargeting_rule_runner import (
            request_retargeting_rule_evaluation,
        )

        if not any_suspicious_empty:
            request_retargeting_rule_evaluation("collection_completed")
    except Exception:
        logger.exception("官方 API 采集完成后唤醒追投规则失败 target=%s", target_uid)
    try:
        cached_product_count = int(capability.get("product_count") or 0)
    except (TypeError, ValueError):
        cached_product_count = 0
    return {
        "success": True,
        "target_uid": target_uid,
        "material_count": len(snapshots),
        "material_with_spend_count": active_material_with_spend_count,
        "product_count": (
            len(products)
            if products_refreshed
            else cached_product_count
        ),
        "product_catalog_refreshed": products_refreshed,
        "report_config_refreshed": report_config_refreshed,
        "control_task_count": active_control_task_count,
        "suspicious_empty": any_suspicious_empty,
    }


def _collect_target_safely(
    target: Mapping[str, Any],
    *,
    db: SQLiteStore,
    interval_seconds: int,
) -> dict[str, Any]:
    """Collect one target and contain every failure to that target."""
    _ensure_collection_schema(db)
    target_uid = str(target.get("target_uid") or "").strip()
    with _ACTIVE_LOCK:
        if target_uid in _ACTIVE_TARGET_UIDS:
            return {
                "success": False,
                "target_uid": target_uid,
                "already_collecting": True,
                "deferred": True,
                "message": "计划已有采集任务正在执行",
            }
        _ACTIVE_TARGET_UIDS.add(target_uid)

    cycle_started_at = datetime.now()
    started = time.monotonic()
    account_key = _target_account_key(target)
    logger.info(
        "[官方API采集] 开始：账户=%s 计划=%s（%s）",
        str(target.get("aadvid") or ""),
        str(target.get("plan_name") or target_uid)[:120],
        target_uid,
    )
    try:
        # Two plans under the same advertiser may collect concurrently. After
        # an account-level 429, the degraded lock temporarily reduces it to one
        # until ten clean cycles have completed.
        with _account_collection_slot(account_key):
            backoff_seconds = _account_backoff_remaining(account_key, db=db)
            if backoff_seconds > 0:
                previous_detail = str(
                    _target_capability(target).get("collection_error_detail")
                    or ""
                ).strip()
                cooldown_message = (
                    f"接口冷却中，剩余约 {backoff_seconds} 秒"
                    + (f"；上次原因：{previous_detail}" if previous_detail else "")
                )
                patch_target_sync_state(
                    target_uid,
                    status="rate_limited",
                    error=cooldown_message,
                    synced=False,
                    capability_updates={
                        "collection_error_kind": "account_backoff",
                        "collection_retry_seconds": backoff_seconds,
                    },
                    db=db,
                )
                _set_retry_due(
                    target_uid,
                    delay_seconds=backoff_seconds,
                    db=db,
                )
                return {
                    "success": False,
                    "target_uid": target_uid,
                    "message": cooldown_message,
                    "error_kind": "account_backoff",
                    "retry_seconds": backoff_seconds,
                    "deferred": True,
                }
            patch_target_sync_state(
                target_uid,
                status="collecting",
                error="",
                synced=False,
                capability_updates={
                    "collection_started_at": _now(),
                    "collection_stage": "plan_verification",
                },
                db=db,
            )
            result = collect_target(target, rotate_maintenance=True, db=db)
            _observe_account_result(account_key, rate_limited=False)
        duration_ms = int((time.monotonic() - started) * 1000)
        record_target_duration(
            target_uid,
            duration_ms,
            interval_seconds=interval_seconds,
            cycle_started_at=cycle_started_at,
            refresh_capacity=False,
            db=db,
        )
        patch_target_sync_state(
            target_uid,
            status=None,
            error="",
            synced=False,
            capability_updates={
                "collection_duration_ms": duration_ms,
                "collection_finished_at": _now(),
            },
            db=db,
        )
        logger.info(
            "[官方API采集] 完成：计划=%s（%s） 投放中素材=%s "
            "有消耗素材=%s 运行中追投任务=%s 耗时=%.1f秒%s",
            str(target.get("plan_name") or target_uid)[:120],
            target_uid,
            int(result.get("material_count") or 0),
            int(result.get("material_with_spend_count") or 0),
            int(result.get("control_task_count") or 0),
            duration_ms / 1000.0,
            "，已保留上次可信数据"
            if result.get("suspicious_empty")
            else "",
        )
        return result
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        capability = _target_capability(target)
        try:
            consecutive_failures = max(
                1,
                int(capability.get("collection_consecutive_failures") or 0) + 1,
            )
        except (TypeError, ValueError):
            consecutive_failures = 1
        error_detail = _official_api_error_detail(exc)
        error_kind = (
            "rate_limit"
            if isinstance(exc, ApiRateLimitError)
            else "token"
            if isinstance(exc, ApiTokenError)
            else "permission"
            if isinstance(exc, ApiPermissionError)
            else "network"
            if isinstance(exc, (TimeoutError, ConnectionError, OSError))
            else "api"
            if isinstance(exc, ApiRequestError)
            else "unknown"
        )
        # Token and permission failures cannot heal by hammering the API.  API
        # throttling and transient downstream errors get one bounded early
        # retry; either way other accounts continue immediately.
        retry_seconds = (
            interval_seconds
            if isinstance(exc, (ApiTokenError, ApiPermissionError))
            else min(
                interval_seconds,
                COLLECTION_TRANSIENT_RETRY_SECONDS
                * (2 ** min(3, consecutive_failures - 1)),
            )
        )
        if isinstance(exc, ApiRateLimitError):
            developer_throttle = (
                str(getattr(exc, "code", "") or "") == "40110"
                or int(getattr(exc, "http_status", 0) or 0) == 429
            )
            retry_seconds = max(
                int(math.ceil(float(getattr(exc, "retry_after", 0) or 0))),
                300 if developer_throttle else 30,
                min(
                    interval_seconds,
                    120 * (2 ** min(2, consecutive_failures - 1)),
                ),
            )
            _observe_account_result(account_key, rate_limited=True)
        if error_kind in {"rate_limit", "token", "permission"}:
            _set_account_backoff(
                account_key,
                retry_seconds,
                db=db,
                error=error_detail,
                include_application=(
                    error_kind == "rate_limit"
                    and _should_escalate_application_backoff(account_key)
                ),
            )
        logger.exception("官方 API 采集失败 target=%s", target_uid)
        pagination_error = (
            error_kind == "api"
            and any(
                marker in str(exc)
                for marker in ("分页", "总记录数", "重复记录", "排序发生变化")
            )
        )
        target_status = (
            "rate_limited"
            if error_kind == "rate_limit"
            else "pagination_error"
            if pagination_error
            else "error"
        )
        patch_target_sync_state(
            target_uid,
            status=target_status,
            error=error_detail,
            synced=False,
            capability_updates={
                "source": "qianchuan_open_api",
                "material_sync_complete": False,
                "collection_error_at": _now(),
                "collection_error_kind": error_kind,
                "collection_error_detail": error_detail,
                "collection_error_endpoint": str(
                    getattr(exc, "endpoint", "") or ""
                ),
                "collection_error_code": str(getattr(exc, "code", "") or ""),
                "collection_error_request_id": str(
                    getattr(exc, "request_id", "") or ""
                ),
                "collection_duration_ms": duration_ms,
                "collection_retry_seconds": retry_seconds,
                "collection_consecutive_failures": consecutive_failures,
                "collection_stage": (
                    "rate_limit_wait"
                    if error_kind == "rate_limit"
                    else "pagination_error"
                    if pagination_error
                    else "data_delay"
                ),
            },
            db=db,
        )
        _set_retry_due(target_uid, delay_seconds=retry_seconds, db=db)
        return {
            "success": False,
            "target_uid": target_uid,
            "message": error_detail,
            "error_kind": error_kind,
            "retry_seconds": retry_seconds,
        }
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_TARGET_UIDS.discard(target_uid)


def run_collection_cycle(
    *,
    db: Optional[SQLiteStore] = None,
    target_uids: Optional[Iterable[str]] = None,
    interval_seconds: int = COLLECTION_INTERVAL_SECONDS,
    max_workers: int = COLLECTION_MAX_WORKERS,
    max_batch_size: Optional[int] = None,
) -> dict[str, Any]:
    """Collect due targets with bounded concurrency and per-target isolation."""
    store = db or SQLiteStore()
    requested = {
        str(item or "").strip()
        for item in (target_uids or ())
        if str(item or "").strip()
    }
    # Capacity is recalculated on settings/evidence changes and after each
    # completed batch.  Do not rewrite every target row on the five-second
    # scheduler tick when no target is due.
    targets = schedulable_promotion_targets(db=store, refresh_capacity=False)
    if requested:
        # Explicitly requested targets are collected immediately regardless of
        # their next periodic due time.
        targets = [
            target
            for target in targets
            if str(target.get("target_uid") or "") in requested
        ]
    else:
        now = datetime.now()
        targets = [
            target
            for target in targets
            if _target_is_due(target, now=now, interval_seconds=interval_seconds)
        ]

    if not targets:
        return {
            "success": True,
            "target_count": 0,
            "adaptive_worker_limit": _adaptive_worker_limit(max_workers),
            "results": [],
        }

    targets = list(_fair_order_targets(targets))
    if not requested and max_batch_size is not None:
        targets = targets[: max(1, int(max_batch_size))]
    adaptive_limit = _adaptive_worker_limit(max_workers)
    worker_count = max(1, min(adaptive_limit, len(targets)))
    ordered_results: list[Optional[dict[str, Any]]] = [None] * len(targets)
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="qianchuan-api-collect",
    ) as pool:
        futures = {
            pool.submit(
                _collect_target_safely,
                target,
                db=store,
                interval_seconds=max(30, int(interval_seconds)),
            ): index
            for index, target in enumerate(targets)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                ordered_results[index] = future.result()
            except Exception as exc:  # defensive: worker must never kill loop
                target_uid = str(targets[index].get("target_uid") or "")
                logger.exception("官方 API 采集线程异常 target=%s", target_uid)
                ordered_results[index] = {
                    "success": False,
                    "target_uid": target_uid,
                    "message": str(exc),
                }
    results = [item for item in ordered_results if item is not None]
    _observe_collection_results(results)
    next_worker_limit = _adaptive_worker_limit(max_workers)
    try:
        refresh_monitor_capacity(db=store)
    except Exception:
        logger.exception("官方 API 采集后刷新监控容量失败")
    return {
        "success": all(item.get("success") for item in results),
        "target_count": len(results),
        "worker_count": worker_count,
        "adaptive_worker_limit": next_worker_limit,
        "rate_limited": any(
            str(item.get("error_kind") or "") == "rate_limit"
            for item in results
        ),
        "results": results,
    }


def _take_pending_targets() -> set[str]:
    with _ACTIVE_LOCK:
        pending = set(_PENDING_TARGET_UIDS)
        _PENDING_TARGET_UIDS.clear()
    return pending


def _collection_job_uid(owner: str, target_uid: str, kind: str) -> str:
    raw = f"{owner}|{target_uid}|{kind}".encode("utf-8")
    return "collect_" + hashlib.sha256(raw).hexdigest()[:32]


def _enqueue_collection_jobs(
    target_uids: Iterable[str],
    *,
    db: SQLiteStore,
    priority: int,
    due_at: Optional[datetime] = None,
    kind: str = "hot_collection",
) -> int:
    _ensure_collection_schema(db)
    owner = _owner_key()
    due_text = (due_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    for target_uid in {str(v or "").strip() for v in target_uids if str(v or "").strip()}:
        target = db.select_one("promotion_target", where={"target_uid": target_uid})
        if not target:
            continue
        account = db.select_one(
            "qianchuan_account",
            where={"account_uid": str(target.get("account_uid") or "")},
        )
        if account and str(account.get("owner_username") or "").casefold() != owner:
            continue
        existing = db.select_one(
            "collection_job",
            where={
                "owner_username": owner,
                "target_uid": target_uid,
                "job_kind": kind,
            },
        ) or {}
        existing_due = _parse_local_time(existing.get("due_at"))
        requested_due = _parse_local_time(due_text) or datetime.now()
        effective_due = (
            min(existing_due, requested_due)
            if existing_due is not None
            else requested_due
        )
        payload = {
            "job_uid": _collection_job_uid(owner, target_uid, kind),
            "owner_username": owner,
            "account_uid": str(target.get("account_uid") or ""),
            "aavid": str(target.get("aadvid") or ""),
            "target_uid": target_uid,
            "job_kind": kind,
            "priority": max(int(existing.get("priority") or 0), int(priority)),
            "due_at": effective_due.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # A refresh request arriving while the job is executing may bring the
        # due time forward and raise priority, but must never revoke the live
        # lease. The current worker's fencing CAS then remains valid.
        if str(existing.get("status") or "") != "leased":
            payload.update(
                {
                    "status": "queued",
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error": "",
                }
            )
        db.insert_or_update(
            "collection_job",
            payload,
            unique_fields=["owner_username", "target_uid", "job_kind"],
        )
        count += 1
    return count


def _claim_collection_jobs(
    *, db: SQLiteStore, limit: int
) -> list[dict[str, Any]]:
    _ensure_collection_schema(db)
    owner = _owner_key()
    worker = f"{threading.current_thread().name}:{uuid.uuid4().hex[:8]}"
    lease_until = datetime.now() + timedelta(seconds=COLLECTION_JOB_LEASE_SECONDS)
    claimed: list[dict[str, Any]] = []
    with db.transaction() as connection:
        db.execute(
            "UPDATE collection_job SET status='queued', lease_owner=NULL, "
            "lease_expires_at=NULL, updated_at=datetime('now', '+8 hours') "
            "WHERE owner_username=? AND status='leased' "
            "AND lease_expires_at < datetime('now', '+8 hours')",
            (owner,),
            connection=connection,
        )
        rows = db.execute(
            "SELECT j.* FROM collection_job j "
            "INNER JOIN promotion_target pt ON pt.target_uid=j.target_uid "
            "INNER JOIN qianchuan_account qa ON qa.account_uid=pt.account_uid "
            "WHERE j.owner_username=? AND j.status='queued' "
            "AND j.due_at <= datetime('now', '+8 hours') "
            "AND qa.enabled=1 AND pt.enabled=1 AND pt.capacity_state='active' "
            "ORDER BY j.priority DESC,j.due_at ASC,j.id ASC LIMIT 2000",
            (owner,),
            fetch=True,
            connection=connection,
        ) or []
        # Preserve strict priority between bands while retaining account
        # round-robin fairness inside each band.
        fair_rows: list[dict[str, Any]] = []
        account_claims: dict[str, int] = {}
        claim_limit = max(1, int(limit))
        priorities = sorted(
            {int(raw.get("priority") or 0) for raw in rows}, reverse=True
        )
        for priority in priorities:
            band = [
                dict(raw) for raw in rows
                if int(raw.get("priority") or 0) == priority
            ]
            for candidate in _fair_order_targets(band):
                account_key = _target_account_key(candidate)
                if account_claims.get(account_key, 0) >= ACCOUNT_MAX_CONCURRENT_PLANS:
                    continue
                fair_rows.append(dict(candidate))
                account_claims[account_key] = account_claims.get(account_key, 0) + 1
                if len(fair_rows) >= claim_limit:
                    break
            if len(fair_rows) >= claim_limit:
                break
        fair_rows = fair_rows[:claim_limit]
        for raw in fair_rows:
            row = dict(raw)
            changed = db.execute(
                "UPDATE collection_job SET status='leased', lease_owner=?, "
                "lease_expires_at=?, fencing_token=fencing_token+1, "
                "attempt_count=attempt_count+1, last_started_at=datetime('now', '+8 hours'), "
                "updated_at=datetime('now', '+8 hours') "
                "WHERE id=? AND status='queued'",
                (
                    worker,
                    lease_until.strftime("%Y-%m-%d %H:%M:%S"),
                    row["id"],
                ),
                connection=connection,
            )
            if changed:
                refreshed = db.execute(
                    "SELECT * FROM collection_job WHERE id=?",
                    (row["id"],),
                    fetch=True,
                    connection=connection,
                ) or []
                if refreshed:
                    claimed.append(dict(refreshed[0]))
    return claimed


def _finish_collection_job(
    job: Mapping[str, Any], result: Mapping[str, Any], *, db: SQLiteStore
) -> None:
    success = bool(result.get("success"))
    if success:
        next_due = _fixed_cadence_due(
            job.get("last_started_at"), COLLECTION_INTERVAL_SECONDS
        )
    else:
        delay = int(
            result.get("retry_seconds") or (15 if result.get("deferred") else 60)
        )
        next_due = datetime.now() + timedelta(seconds=max(5, delay))
    # The lease owner and fencing token are both part of the CAS. An expired
    # worker can never overwrite a task re-leased after process recovery.
    claimed_priority = int(job.get("priority") or 0)
    immediate_due = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE collection_job SET status='queued', "
        "priority=CASE WHEN priority>? THEN priority ELSE 20 END, "
        "due_at=CASE WHEN priority>? THEN MIN(due_at,?) ELSE ? END, "
        "lease_owner=NULL, lease_expires_at=NULL, last_error=?, "
        "last_finished_at=datetime('now', '+8 hours'), "
        "updated_at=datetime('now', '+8 hours') "
        "WHERE id=? AND status='leased' AND lease_owner=? AND fencing_token=?",
        (
            claimed_priority,
            claimed_priority,
            immediate_due,
            next_due.strftime("%Y-%m-%d %H:%M:%S"),
            "" if success else str(result.get("message") or "采集失败")[:500],
            int(job.get("id") or 0),
            str(job.get("lease_owner") or ""),
            int(job.get("fencing_token") or 0),
        ),
    )


def get_collection_queue_health(*, db: Optional[SQLiteStore] = None) -> dict[str, Any]:
    store = db or SQLiteStore()
    _ensure_collection_schema(store)
    owner = _owner_key()
    rows = store.execute(
        "SELECT j.status,COUNT(1) AS count,MIN(j.due_at) AS next_due_at "
        "FROM collection_job j "
        "INNER JOIN promotion_target pt ON pt.target_uid=j.target_uid "
        "INNER JOIN qianchuan_account qa ON qa.account_uid=pt.account_uid "
        "WHERE j.owner_username=? AND qa.enabled=1 AND pt.enabled=1 "
        "AND pt.capacity_state='active' GROUP BY j.status",
        (owner,),
        fetch=True,
    ) or []
    counts = {str(row.get("status") or ""): int(row.get("count") or 0) for row in rows}
    next_due = min(
        [str(row.get("next_due_at") or "") for row in rows if row.get("next_due_at")]
        or [""]
    )
    quota_rows = store.execute(
        "SELECT scope_type,scope_id,backoff_until,last_error,rate_limit_count "
        "FROM api_quota_state "
        "WHERE owner_username=? AND backoff_until > datetime('now', '+8 hours') "
        "ORDER BY backoff_until DESC LIMIT 20",
        (owner,),
        fetch=True,
    ) or []
    freshness_rows = store.execute(
        "SELECT MAX(pt.last_sync_at) AS last_success_at, "
        "MIN(pt.last_sync_at) AS oldest_success_at, "
        "SUM(CASE WHEN pt.last_sync_at IS NULL OR pt.last_sync_at='' THEN 1 ELSE 0 END) AS never_synced "
        "FROM promotion_target pt INNER JOIN qianchuan_account qa "
        "ON qa.account_uid=pt.account_uid "
        "WHERE qa.owner_username=? AND qa.enabled=1 AND pt.enabled=1",
        (owner,),
        fetch=True,
    ) or [{}]
    freshness = freshness_rows[0] if freshness_rows else {}
    capability_rows = store.execute(
        "SELECT pt.capability_json,pt.last_status FROM promotion_target pt "
        "INNER JOIN qianchuan_account qa ON qa.account_uid=pt.account_uid "
        "WHERE qa.owner_username=? AND qa.enabled=1 AND pt.enabled=1",
        (owner,),
        fetch=True,
    ) or []
    active_material_count = 0
    scanned_pages = 0
    stages: dict[str, int] = {}
    for row in capability_rows:
        try:
            capability = json.loads(str(row.get("capability_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            capability = {}
        try:
            active_material_count += int(
                capability.get("active_material_count") or 0
            )
            scanned_pages += int(capability.get("material_scan_page_count") or 0)
        except (TypeError, ValueError):
            pass
        stage = str(
            capability.get("collection_stage")
            or row.get("last_status")
            or "unknown"
        )
        stages[stage] = stages.get(stage, 0) + 1
    with _ACTIVE_LOCK:
        degraded_accounts = len(_ACCOUNT_RATE_LIMITED)
    oldest_success_at = str(freshness.get("oldest_success_at") or "")
    oldest_age_seconds: Optional[int] = None
    if oldest_success_at:
        try:
            oldest_age_seconds = max(
                0,
                int(
                    (
                        datetime.now()
                        - datetime.strptime(oldest_success_at, "%Y-%m-%d %H:%M:%S")
                    ).total_seconds()
                ),
            )
        except (TypeError, ValueError):
            oldest_age_seconds = None
    return {
        "queued": counts.get("queued", 0),
        "leased": counts.get("leased", 0),
        "next_due_at": next_due,
        "backoffs": [dict(row) for row in quota_rows],
        "last_success_at": str(freshness.get("last_success_at") or ""),
        "oldest_success_at": oldest_success_at,
        "oldest_data_age_seconds": oldest_age_seconds,
        "never_synced": int(freshness.get("never_synced") or 0),
        "active_material_count": active_material_count,
        "last_scan_page_count": scanned_pages,
        "collection_stages": stages,
        "global_worker_limit": _adaptive_worker_limit(
            COLLECTION_MAX_WORKERS
        ),
        "global_worker_max": COLLECTION_MAX_WORKERS,
        "account_concurrency": ACCOUNT_MAX_CONCURRENT_PLANS,
        "degraded_account_count": degraded_accounts,
        "application_qps_limit": 12,
        "account_qps_limit": 2,
    }


def _loop(interval_seconds: int) -> None:
    interval = max(30, int(interval_seconds))
    tick = min(COLLECTION_SCHEDULER_TICK_SECONDS, interval)
    while not _STOP.is_set():
        claimed: list[dict[str, Any]] = []
        try:
            store = SQLiteStore()
            _ensure_collection_schema(store)
            pending = _take_pending_targets()
            if pending:
                _enqueue_collection_jobs(
                    pending, db=store, priority=100, due_at=datetime.now()
                )
            due_targets = [
                str(target.get("target_uid") or "")
                for target in schedulable_promotion_targets(
                    db=store, refresh_capacity=False
                )
                if _target_is_due(target, interval_seconds=interval)
            ]
            if due_targets:
                _enqueue_collection_jobs(
                    due_targets, db=store, priority=20, due_at=datetime.now()
                )
            claimed = _claim_collection_jobs(
                db=store,
                limit=max(1, _adaptive_worker_limit(COLLECTION_MAX_WORKERS)),
            )
            if claimed:
                owner_at_claim = _owner_key()
                result = run_collection_cycle(
                    db=store,
                    target_uids=[str(job.get("target_uid") or "") for job in claimed],
                    interval_seconds=interval,
                )
                by_target = {
                    str(item.get("target_uid") or ""): item
                    for item in result.get("results") or []
                }
                owner_changed = _owner_key() != owner_at_claim
                for job in claimed:
                    target_uid = str(job.get("target_uid") or "")
                    item = by_target.get(target_uid) or {
                        "success": False,
                        "deferred": True,
                        "retry_seconds": 15,
                        "message": (
                            "工具账号已切换，旧账号采集已取消"
                            if owner_changed
                            else "采集任务未返回结果，稍后重试"
                        ),
                    }
                    _finish_collection_job(job, item, db=store)
        except Exception:
            logger.exception("官方 API 采集轮次异常")
        if claimed and not _STOP.is_set():
            # Drain an existing backlog immediately in bounded 3-worker waves.
            # The API limiter and persisted quota gate still apply, but a
            # 200-target queue no longer pays an extra five-second scheduler
            # sleep after every three targets.
            _STOP.wait(0.2)
            continue
        _WAKE.wait(tick)
        _WAKE.clear()


def start_official_api_collection_background_thread(
    interval_seconds: int = COLLECTION_INTERVAL_SECONDS,
) -> threading.Thread:
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return _THREAD
        _STOP.clear()
        _WAKE.clear()
        _THREAD = threading.Thread(
            target=_loop,
            args=(interval_seconds,),
            name="qianchuan-official-api-collection",
            daemon=True,
        )
        _THREAD.start()
        return _THREAD


def request_official_api_collection(
    target_uids: Iterable[Any],
    *,
    db: Optional[SQLiteStore] = None,
) -> dict[str, Any]:
    """Start enabled targets immediately; the periodic loop remains a fallback."""
    requested = {
        str(item or "").strip() for item in target_uids if str(item or "").strip()
    }
    if not requested:
        return {
            "success": True,
            "running": bool(_THREAD and _THREAD.is_alive()),
            "queued_count": 0,
            "message": "没有需要立即采集的监控计划",
        }
    store = db or SQLiteStore()
    started: list[str] = []
    with _ACTIVE_LOCK:
        already_collecting = requested.intersection(_ACTIVE_TARGET_UIDS)
        already_queued = requested.intersection(_PENDING_TARGET_UIDS)
        to_start = requested.difference(already_collecting).difference(already_queued)
        _PENDING_TARGET_UIDS.update(to_start)
    valid_to_start: set[str] = set()
    for target_uid in to_start:
        try:
            patch_target_sync_state(
                target_uid,
                status="queued",
                error="",
                synced=False,
                capability_updates={"collection_queued_at": _now()},
                db=store,
            )
            valid_to_start.add(target_uid)
        except ValueError:
            # The scheduler will ignore targets that were removed between save
            # and queueing.  Do not let one stale UI row block other targets.
            with _ACTIVE_LOCK:
                _PENDING_TARGET_UIDS.discard(target_uid)
            continue
    queued_count = _enqueue_collection_jobs(
        valid_to_start,
        db=store,
        priority=100,
        due_at=datetime.now(),
    )
    start_official_api_collection_background_thread()
    _WAKE.set()
    started.extend(sorted(valid_to_start))
    return {
        "success": True,
        "running": True,
        "queued_count": queued_count,
        "started_count": len(started),
        "already_collecting_count": len(already_collecting) + len(already_queued),
        "target_uids": started,
        "message": "设置已保存，官方 API 已开始采集新监控计划",
    }


def stop_official_api_collection_background_thread(timeout: float = 12.0) -> None:
    global _THREAD
    _STOP.set()
    _WAKE.set()
    thread = _THREAD
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=max(0.1, float(timeout)))
    if thread is None or not thread.is_alive():
        _THREAD = None
