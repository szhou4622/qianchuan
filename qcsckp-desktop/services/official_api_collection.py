"""Official API collector that feeds the existing v0.1.46 read models.

The UI and rule engine continue to read ``pmc_promotion_material`` and
``pmc_roi2_assist_task``.  This module is the only production writer of those
tables when ``QCSCKP_QIANCHUAN_BACKEND=official_api``.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from api.promotion_targets import (
    patch_target_sync_state,
    replace_material_product_links,
    upsert_products,
)
from services.plan_system import normalize_plan_system
from services.promotion_browser_lock import (
    PRIORITY_COLLECTION,
    exclusive_qianchuan_operation,
)
from services.qianchuan_accounts import schedulable_promotion_targets
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
_THREAD: Optional[threading.Thread] = None
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date_window(days: int = 30) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=max(1, int(days)))
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
    init_sqlite_schema(database=store.config.get("database"))
    service = get_official_api_service()
    aavid = text_id(target.get("aadvid"))
    ad_id = text_id(target.get("ad_id"))
    target_uid = str(target.get("target_uid") or "")
    expected_scene = str(target.get("promotion_scene") or "")
    expected_system = normalize_plan_system(target.get("plan_system"))

    detail, detail_response = service.get_plan_detail(aavid, ad_id)
    actual_scene = normalize_promotion_scene(detail.get("marketing_goal"))
    actual_system = normalize_api_plan_system(detail.get("adlab_scene"))
    if detail.get("aavid") != aavid or detail.get("ad_id") != ad_id:
        raise RuntimeError("官方 API 计划详情与监控账户或计划不一致")
    if actual_scene != expected_scene or actual_system != expected_system:
        raise RuntimeError("官方 API 计划详情的推广方式或计划体系已变化")
    if str(detail.get("platform_status") or "") not in {"active", "learning"}:
        raise RuntimeError("官方 API 返回的计划当前不可投放")

    goal = str(detail.get("marketing_goal") or "")
    units, config_response = service.get_report_config(aavid, marketing_goal=goal)
    if not units:
        raise RuntimeError("官方 API 报表配置未返回字段单位，本轮数据不入库")

    start_date, end_date = _date_window(30)
    materials, material_request_ids = service.list_plan_materials(
        aavid,
        ad_id,
        start_date=start_date,
        end_date=end_date,
        fields=MATERIAL_METRICS,
    )
    material_request_id = material_request_ids[-1] if material_request_ids else detail_response.request_id
    snapshots = [
        _material_snapshot(item, target=target, units=units, request_id=material_request_id)
        for item in materials
        if text_id(item.get("material_id"))
    ]

    products: list[dict[str, Any]] = []
    product_request_ids: list[str] = []
    if expected_scene == "product":
        products, product_request_ids = service.list_plan_products(aavid, ad_id)

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

    if products:
        upsert_products(target_uid, products, db=store)
    current_product_ids = [text_id(item.get("product_id")) for item in products if text_id(item.get("product_id"))]
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
        replace_material_product_links(
            target_uid,
            mid,
            pids,
            material_name=str(material.get("material_name") or ""),
            db=store,
        )

    patch_target_sync_state(
        target_uid,
        status="ok",
        error="",
        synced=True,
        capability_updates={
            "source": "qianchuan_open_api",
            "material_sync_complete": True,
            "material_count": len(snapshots),
            "control_task_sync_complete": True,
            "control_task_count": len(control_rows),
            "assist_sync_enabled": True,
            "assist_sync_in_progress": False,
            "assist_sync_ok": True,
            "assist_synced_at": _now(),
            "report_config_request_id": config_response.request_id,
            "material_request_ids": material_request_ids,
            "product_request_ids": product_request_ids,
            "control_request_ids": control_request_ids,
            "collected_at": _now(),
        },
        db=store,
    )
    return {
        "success": True,
        "target_uid": target_uid,
        "material_count": len(snapshots),
        "product_count": len(products),
        "control_task_count": len(control_rows),
    }


def run_collection_cycle(*, db: Optional[SQLiteStore] = None) -> dict[str, Any]:
    store = db or SQLiteStore()
    results: list[dict[str, Any]] = []
    for target in schedulable_promotion_targets(db=store):
        try:
            with exclusive_qianchuan_operation(
                f"官方API采集:{target.get('target_uid')}",
                priority=PRIORITY_COLLECTION,
            ):
                results.append(collect_target(target, db=store))
        except Exception as exc:
            logger.exception("官方 API 采集失败 target=%s", target.get("target_uid"))
            patch_target_sync_state(
                target.get("target_uid"),
                status="error",
                error=str(exc),
                synced=False,
                capability_updates={
                    "source": "qianchuan_open_api",
                    "material_sync_complete": False,
                    "collection_error_at": _now(),
                },
                db=store,
            )
            results.append({"success": False, "target_uid": target.get("target_uid"), "message": str(exc)})
    return {
        "success": all(item.get("success") for item in results) if results else True,
        "target_count": len(results),
        "results": results,
    }


def _loop(interval_seconds: int) -> None:
    while not _STOP.is_set():
        try:
            run_collection_cycle()
        except Exception:
            logger.exception("官方 API 采集轮次异常")
        _STOP.wait(max(30, int(interval_seconds)))


def start_official_api_collection_background_thread(interval_seconds: int = 300) -> threading.Thread:
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return _THREAD
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_loop,
            args=(interval_seconds,),
            name="qianchuan-official-api-collection",
            daemon=True,
        )
        _THREAD.start()
        return _THREAD


def stop_official_api_collection_background_thread() -> None:
    _STOP.set()
