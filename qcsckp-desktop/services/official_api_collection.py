"""Official API collector that feeds the existing v0.1.46 read models.

The UI and rule engine continue to read ``pmc_promotion_material`` and
``pmc_roi2_assist_task``.  This module is the only production writer of those
tables when ``QCSCKP_QIANCHUAN_BACKEND=official_api``.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from api.promotion_targets import (
    patch_target_sync_state,
    replace_material_product_links,
    update_target_catalog_evidence,
    upsert_products,
)
from services.plan_system import normalize_plan_system
from services.qianchuan_accounts import (
    CAPACITY_PARALLEL_WORKERS,
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
)

_STOP = threading.Event()
_WAKE = threading.Event()
_THREAD: Optional[threading.Thread] = None
_LOCK = threading.Lock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_TARGET_UIDS: set[str] = set()
_PENDING_TARGET_UIDS: set[str] = set()
_ACCOUNT_COLLECTION_LOCKS: dict[str, threading.Lock] = {}
_ACCOUNT_BACKOFF_UNTIL: dict[str, float] = {}
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY_DATABASES: set[str] = set()

# One target is refreshed every five minutes from its own last successful
# collection.  The scheduler only sleeps for a short tick so a slow account
# never adds another five minutes to every account behind it.
COLLECTION_INTERVAL_SECONDS = 5 * 60
COLLECTION_SCHEDULER_TICK_SECONDS = 5
COLLECTION_MAX_WORKERS = CAPACITY_PARALLEL_WORKERS
COLLECTION_PERIODIC_BATCH_SIZE = COLLECTION_MAX_WORKERS
COLLECTION_TRANSIENT_RETRY_SECONDS = 60
REPORT_CONFIG_INTERVAL_SECONDS = 30 * 60
PRODUCT_CATALOG_INTERVAL_SECONDS = 30 * 60
ADAPTIVE_RECOVERY_CLEAN_BATCHES = 3

_ADAPTIVE_LOCK = threading.Lock()
_ADAPTIVE_WORKERS = COLLECTION_MAX_WORKERS
_ADAPTIVE_CLEAN_BATCHES = 0


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
    }


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


def _account_collection_lock(account_key: str) -> threading.Lock:
    with _ACTIVE_LOCK:
        return _ACCOUNT_COLLECTION_LOCKS.setdefault(account_key, threading.Lock())


def _account_backoff_remaining(account_key: str) -> int:
    with _ACTIVE_LOCK:
        due = float(_ACCOUNT_BACKOFF_UNTIL.get(account_key, 0.0) or 0.0)
    return max(0, int(round(due - time.monotonic())))


def _set_account_backoff(account_key: str, seconds: int) -> None:
    with _ACTIVE_LOCK:
        _ACCOUNT_BACKOFF_UNTIL[account_key] = max(
            float(_ACCOUNT_BACKOFF_UNTIL.get(account_key, 0.0) or 0.0),
            time.monotonic() + max(30, int(seconds)),
        )


def _ensure_collection_schema(store: SQLiteStore) -> None:
    """Initialize SQLite once per runtime database, not once per target."""
    database = str(store.config.get("database") or "").strip()
    # Separate in-memory databases must not share readiness state.
    key = f"memory:{id(store)}" if not database or database == ":memory:" else database
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY_DATABASES:
            return
        init_sqlite_schema(database=store.config.get("database"))
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
            _ADAPTIVE_WORKERS = max(1, _ADAPTIVE_WORKERS - 1)
            _ADAPTIVE_CLEAN_BATCHES = 0
        elif rows and all(bool(row.get("success")) for row in rows):
            _ADAPTIVE_CLEAN_BATCHES += 1
            if (
                _ADAPTIVE_WORKERS < COLLECTION_MAX_WORKERS
                and _ADAPTIVE_CLEAN_BATCHES >= ADAPTIVE_RECOVERY_CLEAN_BATCHES
            ):
                _ADAPTIVE_WORKERS += 1
                _ADAPTIVE_CLEAN_BATCHES = 0
        elif rows:
            _ADAPTIVE_CLEAN_BATCHES = 0
        return _ADAPTIVE_WORKERS


def _reset_adaptive_collection_state_for_tests() -> None:
    global _ADAPTIVE_WORKERS, _ADAPTIVE_CLEAN_BATCHES
    with _ADAPTIVE_LOCK:
        _ADAPTIVE_WORKERS = COLLECTION_MAX_WORKERS
        _ADAPTIVE_CLEAN_BATCHES = 0
    with _ACTIVE_LOCK:
        _ACCOUNT_COLLECTION_LOCKS.clear()
        _ACCOUNT_BACKOFF_UNTIL.clear()


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
) -> float:
    raw, inline_unit = _metric_block(stats, *names)
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
    if raw in {"ENABLE", "ENABLED", "ACTIVE", "DELIVERY", "DELIVERING", "RUNNING", "SUCCESS"}:
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
        "overall_show_count": _metric(stats, units, "live_show_count_for_roi2_v2", "liveShowCountForRoi2V2"),
        "overall_click_count": _metric(stats, units, "live_watch_count_for_roi2_v2", "liveWatchCountForRoi2V2"),
        "overall_ctr": _metric(stats, units, "live_cvr_rate_for_roi2_v2", "liveCvrRateForRoi2V2"),
        "overall_conversion_rate": _metric(stats, units, "live_convert_rate_for_roi2_v2", "liveConvertRateForRoi2V2"),
        "data_source": "qianchuan_open_api",
        "api_request_id": str(request_id or "")[:256],
        "stat_date": datetime.now().strftime("%Y-%m-%d"),
    }


def _control_snapshot(
    task: Mapping[str, Any],
    *,
    target: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    raw = _mapping(task.get("raw"))
    stats = _mapping(first(raw, "stats_info", "statsInfo", "stats", "metrics", default={}))
    material_ids = [text_id(item) for item in (task.get("material_ids") or []) if text_id(item)]
    materials = [{"material_id": item, "title": ""} for item in material_ids]

    def raw_metric(*names: str) -> Any:
        value, _ = _metric_block(stats, *names)
        return value

    status = str(task.get("status") or "").upper()
    active = status in {"ENABLE", "ENABLED", "ACTIVE", "RUNNING", "DELIVERING"}
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


def collect_target(target: Mapping[str, Any], *, db: Optional[SQLiteStore] = None) -> dict[str, Any]:
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
    # A selected live plan may move between waiting for broadcast and live
    # while the 5-minute collector is running. Persist that transition here so
    # the user does not need to refresh the 30-minute catalog or save again.
    update_target_catalog_evidence(
        target_uid,
        platform_status=detail_status,
        verification_state="verified",
        plan_system=expected_system,
        promotion_scene=expected_scene,
        db=store,
    )

    goal = str(detail.get("marketing_goal") or "")
    report_config_refreshed = bool(phase_plan["refresh_report_config"])
    report_config_request_id = str(
        capability.get("report_config_request_id") or ""
    )
    if report_config_refreshed:
        units, config_response = service.get_report_config(
            aavid,
            plan_system=expected_system,
            promotion_scene=expected_scene,
        )
        report_config_request_id = config_response.request_id
    else:
        units = dict(phase_plan["cached_units"])
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
    materials, material_request_ids = service.list_plan_materials(
        aavid,
        ad_id,
        start_date=start_date,
        end_date=end_date,
        fields=supported_material_metrics,
    )
    material_request_id = material_request_ids[-1] if material_request_ids else detail_response.request_id
    snapshots = [
        _material_snapshot(item, target=target, units=units, request_id=material_request_id)
        for item in materials
        if text_id(item.get("material_id"))
    ]

    products_refreshed = bool(phase_plan["refresh_products"])
    products: list[dict[str, Any]] = []
    product_request_ids: list[str] = []
    if products_refreshed:
        products, product_request_ids = service.list_plan_products(
            aavid,
            ad_id,
            start_date=start_date,
            end_date=end_date,
        )

    now = datetime.now()
    control_tasks, control_request_ids = service.list_control_tasks(
        aavid,
        ad_id=ad_id,
        marketing_goal=goal,
        start_time=(now - timedelta(days=179)).strftime("%Y-%m-%d 00:00:00"),
        end_time=(now + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59"),
    )
    control_request_id = control_request_ids[-1] if control_request_ids else ""
    control_rows = [
        _control_snapshot(item, target=target, request_id=control_request_id)
        for item in control_tasks
        if str(item.get("scene") or "").upper() == "MATERIAL_ADD_BUDGET"
        and text_id(item.get("task_id"))
    ]

    with store.transaction() as connection:
        for row in snapshots:
            store.insert("pmc_promotion_material", row, connection=connection)
        for row in control_rows:
            store.insert_or_update(
                "pmc_roi2_assist_task",
                row,
                unique_fields=["target_uid", "assist_task_id"],
                connection=connection,
            )
        # The API list call is complete-or-raise.  Only after a complete list
        # may stale local control tasks be removed; otherwise old complete data
        # remains available and no stop candidate is created from a partial page.
        current_task_ids = [str(row["assist_task_id"]) for row in control_rows]
        if current_task_ids:
            placeholders = ",".join("?" for _ in current_task_ids)
            store.execute(
                "DELETE FROM pmc_roi2_assist_task WHERE target_uid=? "
                f"AND assist_task_id NOT IN ({placeholders})",
                (target_uid, *current_task_ids),
                connection=connection,
            )
        else:
            store.execute(
                "DELETE FROM pmc_roi2_assist_task WHERE target_uid=?",
                (target_uid,),
                connection=connection,
            )

    if products_refreshed:
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
        if pids or products_refreshed:
            replace_material_product_links(
                target_uid,
                mid,
                pids,
                material_name=str(material.get("material_name") or ""),
                db=store,
            )

    capability_updates = {
        "source": "qianchuan_open_api",
        "material_sync_complete": True,
        "material_count": len(snapshots),
        "control_task_sync_complete": True,
        "control_task_count": len(control_rows),
        "assist_sync_enabled": True,
        "assist_sync_in_progress": False,
        "assist_sync_ok": True,
        "assist_synced_at": _now(),
        "material_request_ids": material_request_ids,
        "control_request_ids": control_request_ids,
        "collected_at": _now(),
    }
    if report_config_refreshed:
        capability_updates.update(
            {
                "report_config_request_id": report_config_request_id,
                "report_config_synced_at": _now(),
                "report_metric_units": dict(units),
            }
        )
    if products_refreshed:
        capability_updates.update(
            {
                "product_catalog_synced_at": _now(),
                "product_sync_complete": True,
                "product_count": len(products),
                "product_request_ids": product_request_ids,
            }
        )
    patch_target_sync_state(
        target_uid,
        status="ok",
        error="",
        synced=True,
        capability_updates=capability_updates,
        capability_remove_keys=(
            "collection_error_at",
            "collection_error_kind",
            "collection_retry_seconds",
            "collection_consecutive_failures",
        ),
        db=store,
    )
    # 计划刚加入监控或本轮数据更新完成后，立刻让规则线程复核；不再让用户
    # 额外等待下一次 5 分钟轮询。局部导入避免采集模块与调度模块循环加载。
    try:
        from services.retargeting_rule_runner import (
            request_retargeting_rule_evaluation,
        )

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
        "product_count": (
            len(products)
            if products_refreshed
            else cached_product_count
        ),
        "product_catalog_refreshed": products_refreshed,
        "report_config_refreshed": report_config_refreshed,
        "control_task_count": len(control_rows),
    }


def _collect_target_safely(
    target: Mapping[str, Any],
    *,
    db: SQLiteStore,
    interval_seconds: int,
) -> dict[str, Any]:
    """Collect one target and contain every failure to that target."""
    target_uid = str(target.get("target_uid") or "").strip()
    with _ACTIVE_LOCK:
        if target_uid in _ACTIVE_TARGET_UIDS:
            return {
                "success": True,
                "target_uid": target_uid,
                "already_collecting": True,
            }
        _ACTIVE_TARGET_UIDS.add(target_uid)

    started = time.monotonic()
    account_key = _target_account_key(target)
    try:
        # Official API quotas are commonly scoped by advertiser/account.  Do
        # not let several monitored plans under one advertiser burst in
        # parallel merely because global worker lanes are available.
        with _account_collection_lock(account_key):
            backoff_seconds = _account_backoff_remaining(account_key)
            if backoff_seconds > 0:
                patch_target_sync_state(
                    target_uid,
                    status="rate_limited",
                    error="同一千川账户正在限流冷却，工具将自动重试",
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
                    "message": "同一千川账户正在限流冷却，工具将自动重试",
                    "error_kind": "account_backoff",
                    "retry_seconds": backoff_seconds,
                    "deferred": True,
                }
            patch_target_sync_state(
                target_uid,
                status="collecting",
                error="",
                synced=False,
                capability_updates={"collection_started_at": _now()},
                db=db,
            )
            result = collect_target(target, db=db)
        duration_ms = int((time.monotonic() - started) * 1000)
        record_target_duration(
            target_uid,
            duration_ms,
            interval_seconds=interval_seconds,
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
            retry_seconds = max(
                120,
                min(
                    interval_seconds,
                    120 * (2 ** min(2, consecutive_failures - 1)),
                ),
            )
        if error_kind in {"rate_limit", "token", "permission"}:
            _set_account_backoff(account_key, retry_seconds)
        logger.exception("官方 API 采集失败 target=%s", target_uid)
        patch_target_sync_state(
            target_uid,
            status="error",
            error=str(exc),
            synced=False,
            capability_updates={
                "source": "qianchuan_open_api",
                "material_sync_complete": False,
                "collection_error_at": _now(),
                "collection_error_kind": error_kind,
                "collection_duration_ms": duration_ms,
                "collection_retry_seconds": retry_seconds,
                "collection_consecutive_failures": consecutive_failures,
            },
            db=db,
        )
        _set_retry_due(target_uid, delay_seconds=retry_seconds, db=db)
        return {
            "success": False,
            "target_uid": target_uid,
            "message": str(exc),
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


def _loop(interval_seconds: int) -> None:
    interval = max(30, int(interval_seconds))
    tick = min(COLLECTION_SCHEDULER_TICK_SECONDS, interval)
    while not _STOP.is_set():
        try:
            pending = _take_pending_targets()
            run_collection_cycle(
                target_uids=pending or None,
                interval_seconds=interval,
                max_batch_size=(
                    None if pending else COLLECTION_PERIODIC_BATCH_SIZE
                ),
            )
        except Exception:
            logger.exception("官方 API 采集轮次异常")
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
    start_official_api_collection_background_thread()
    _WAKE.set()
    started.extend(sorted(valid_to_start))
    return {
        "success": True,
        "running": True,
        "queued_count": 0,
        "started_count": len(started),
        "already_collecting_count": len(already_collecting) + len(already_queued),
        "target_uids": started,
        "message": "设置已保存，官方 API 已开始采集新监控计划",
    }


def stop_official_api_collection_background_thread() -> None:
    _STOP.set()
    _WAKE.set()
