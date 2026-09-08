"""Commit fresh Scene-2 metrics independently of material pagination."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from services.collection_lifecycle import owned_transaction
from services.qianchuan_open_api.collection_context import current_collection_context
from services.qianchuan_open_api.normalizers import normalize_plan_system, normalize_promotion_scene, text_id, require_digit_id


_SCOPE_FIELDS = ("account_uid", "aadvid", "ad_id", "promotion_scene", "plan_system")


def unavailable_bundle() -> dict[str, Any]:
    from services.official_api_collection import _now
    return {"available": False, "control_rows": [], "control_tasks": [],
            "control_request_ids": [], "control_request_id": "", "control_observed_at": _now(),
            "active_control_task_count": 0, "refresh_control_history": False}


def _current_scope(db, target, collection, connection):
    uid = str(target.get("target_uid") or "")
    current = db.select_one("promotion_target", where={"target_uid": uid}, connection=connection) or {}
    account = db.select_one("qianchuan_account", where={"account_uid": current.get("account_uid")},
                            connection=connection) or {}
    owner = str(collection._owner_key() or "").strip().casefold()
    if (not uid or not owner or not current.get("enabled") or not account.get("enabled")
            or str(account.get("owner_username") or "").strip().casefold() != owner
            or str(current.get("capacity_state") or "") != "active"
            or any(str(current.get(key) or "") != str(target.get(key) or "") for key in _SCOPE_FIELDS)):
        raise RuntimeError("调控采集期间账户、监控计划或归属已变化")
    if str(current.get("promotion_scene")) not in {"live", "product"} or normalize_plan_system(current.get("plan_system")) == "unknown":
        raise RuntimeError("调控采集目标场景或计划体系尚未核实")
    return current, owner


def _check_echoed_ids(sources, *, aid: str, pid: str) -> None:
    for source in sources:
        if not isinstance(source, Mapping):
            raise RuntimeError("调控响应结构无法核验")
        for key in ("ad_id", "adId"):
            if source.get(key) not in (None, "") and text_id(source[key]) != pid:
                raise RuntimeError("调控任务响应计划归属与请求不一致")
        for key in ("advertiser_id", "advertiserId", "aavid", "aadvid"):
            if source.get(key) not in (None, "") and text_id(source[key]) != aid:
                raise RuntimeError("调控任务响应账户归属与请求不一致")


def _check_echoed_scope(item: Mapping[str, Any], *, aid: str, pid: str) -> None:
    raw = item.get("raw") or {}
    _check_echoed_ids((item, raw), aid=aid, pid=pid)
    for source in (item, raw):
        scene = str(source.get("scene") or "").upper()
        if scene and scene != "MATERIAL_ADD_BUDGET":
            raise RuntimeError("调控任务响应不是 Scene-2 素材追投")
    require_digit_id(item.get("task_id"), "control_task_id")


def _valid_observation(value: Any) -> bool:
    try:
        stamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        now = datetime.now(stamp.tzinfo) if stamp.tzinfo else datetime.now()
        return -timedelta(minutes=5) <= now - stamp <= timedelta(minutes=30)
    except (TypeError, ValueError):
        return False


def collect_control_metrics(target, *, db):
    from services import official_api_collection as collection
    from services.control_task_cycle import stop_cycle_state

    uid = str(target.get("target_uid") or "")
    # Publish the in-progress gate under the same auth->DB ownership ordering
    # as the final snapshot. No network occurs while either lock is held.
    with owned_transaction(db, target) as connection:
        current, owner = _current_scope(db, target, collection, connection)
        collection._patch_target_sync_in_transaction(
            db, uid, connection=connection, status=str(current.get("last_status") or "pending"),
            error=str(current.get("last_error") or ""), synced=False,
            capability_updates={"assist_independent_sync": True, "assist_sync_enabled": True,
                                "assist_sync_in_progress": True,
                                "control_collection_started_at": collection._now()},
        )
    target = dict(current)
    aid = require_digit_id(target.get("aadvid"), "advertiser_id")
    pid = require_digit_id(target.get("ad_id"), "ad_id")
    service = collection.get_official_api_service()
    detail_observed_at = collection._now()
    detail, response = service.get_plan_detail(aid, pid)
    raw_detail = detail.get("raw") or {}
    sources = [detail, raw_detail]
    if isinstance(raw_detail, Mapping) and isinstance(raw_detail.get("ad_info"), Mapping):
        sources.append(raw_detail["ad_info"])
    _check_echoed_ids(sources, aid=aid, pid=pid)
    expected_goal = "LIVE_PROM_GOODS" if target.get("promotion_scene") == "live" else "VIDEO_PROM_GOODS"
    if (text_id(detail.get("aavid")) != aid or text_id(detail.get("ad_id")) != pid
            or str(detail.get("marketing_goal") or "").upper() != expected_goal
            or normalize_promotion_scene(detail.get("marketing_goal")) != target.get("promotion_scene")
            or normalize_plan_system(detail.get("adlab_scene")) != collection.normalize_plan_system(target.get("plan_system"))
            or detail.get("platform_status") not in {"active", "learning", "waiting_live"}):
        raise RuntimeError("调控采集的账户、主计划、场景或投放状态核验不一致")
    phase = collection._collection_phase_plan(target)
    units = dict(phase["cached_units"])
    if not units:
        units, _ = service.get_report_config(aid, plan_system=target["plan_system"], promotion_scene=target["promotion_scene"])
    if not isinstance(units, Mapping) or not units:
        raise RuntimeError("调控指标单位尚未核验，本轮不提交")
    maintenance_errors = []
    bundle = collection._read_control_bundle(
        target, store=db, service=service, goal=expected_goal, units=units,
        phase_plan=phase, maintenance_phase="control_history" if phase.get("refresh_control_history") else "",
        rotate_maintenance=True, maintenance_errors=maintenance_errors,
    )
    seen = set()
    for item in bundle["control_tasks"]:
        _check_echoed_scope(item, aid=aid, pid=pid)
        task_id = str(item["task_id"])
        if task_id in seen:
            raise RuntimeError("调控任务响应包含重复ID，本轮未完整同步")
        seen.add(task_id)
    if not _valid_observation(bundle["control_observed_at"]):
        raise RuntimeError("调控指标读取时间无效或已过期")
    for row in bundle["control_rows"]:
        if (str(row.get("target_uid")) != uid or text_id(row.get("aadvid")) != aid
                or text_id(row.get("ad_id")) != pid
                or any(str(row.get(key) or "") != str(target.get(key) or "")
                       for key in ("account_uid", "promotion_scene", "plan_system"))
                or row.get("data_source") != "qianchuan_open_api"
                or not _valid_observation(row.get("metrics_observed_at"))):
            raise RuntimeError("调控指标行归属、来源或读取时间无效")
    with owned_transaction(db, target) as connection:
        fresh_target, owner = _current_scope(db, target, collection, connection)
        capability = collection._target_capability(fresh_target)
        previous = db.execute(
            "SELECT COUNT(*) count FROM pmc_roi2_assist_task WHERE target_uid=? AND ad_delivery_type=0",
            (uid,), fetch=True, connection=connection,
        ) or [{}]
        suspicious, streak = collection._empty_is_suspicious(
            current_count=bundle["active_control_task_count"], previous_count=int(previous[0].get("count") or 0),
            capability=capability, streak_key="control_empty_streak",
        )
        rows = []
        for row in bundle["control_rows"]:
            state = stop_cycle_state(db, uid, row["assist_task_id"], assist_row=row,
                                     observed_at=row["task_status_observed_at"], connection=connection)
            guarded = collection._guard_control_snapshot_after_confirmed_stop(
                db, uid, row, observed_at=row["task_status_observed_at"], connection=connection, cycle_state=state,
            )
            guarded["updated_at"] = collection._now()
            rows.append(guarded)
        if not suspicious:
            collection._bulk_upsert_rows(connection, "pmc_roi2_assist_task", rows,
                                         unique_fields=("target_uid", "assist_task_id"))
            ids = [row["assist_task_id"] for row in rows]
            exclusion = " AND assist_task_id NOT IN (" + ",".join("?" for _ in ids) + ")" if ids else ""
            db.execute(
                "UPDATE pmc_roi2_assist_task SET ad_delivery_type=1,ad_delivery_name='NOT_RETURNED_BY_HOT_SYNC' "
                "WHERE target_uid=? AND ad_delivery_type=0" + exclusion, (uid, *ids), connection=connection,
            )
        context = current_collection_context()
        active_count = int(previous[0].get("count") or 0) if suspicious else sum(row.get("ad_delivery_type") == 0 for row in rows)
        updates = {
            "assist_independent_sync": True, "assist_sync_enabled": True,
            "assist_sync_in_progress": False, "assist_sync_ok": not suspicious,
            "assist_synced_at": bundle["control_observed_at"], "control_task_sync_complete": not suspicious,
            "control_empty_streak": streak, "control_request_ids": bundle["control_request_ids"],
            "control_task_count": active_count, "active_control_task_count": active_count,
            "control_observed_at": bundle["control_observed_at"],
            "control_metric_source": "control_task_list", "control_scene_scope": "MATERIAL_ADD_BUDGET",
            "control_scope": {"owner_username": owner, "target_uid": uid,
                              **{key: str(target.get(key) or "") for key in _SCOPE_FIELDS}},
            "control_plan_status": detail["platform_status"],
            "control_plan_verified_at": detail_observed_at,
            "control_plan_request_id": str(getattr(response, "request_id", "") or ""),
            "control_error": "活动调控任务异常空结果，等待复核" if suspicious else "",
            "control_collection_committed_at": collection._now(),
            "control_authorization_identity": dict(getattr(context, "authorization_identity", None) or {}),
            "control_collection_generation": str(getattr(context, "generation", "") or ""),
        }
        if maintenance_errors:
            updates["control_history_error"] = "; ".join(maintenance_errors)[:1000]
        if not suspicious:
            updates["control_last_success_at"] = bundle["control_observed_at"]
        if bundle["refresh_control_history"]:
            updates["control_history_synced_at"] = collection._now()
        collection._patch_target_sync_in_transaction(
            db, uid, connection=connection, status=str(fresh_target.get("last_status") or "pending"),
            error=str(fresh_target.get("last_error") or ""), synced=False, capability_updates=updates,
        )
    bundle = {**bundle, "control_rows": rows, "independently_committed": True,
              "control_suspicious_empty": suspicious}
    if not suspicious:
        try:
            from services.regulation_rule_runner import request_regulation_rule_evaluation
            request_regulation_rule_evaluation("control_metrics_completed", target_uids={uid})
        except Exception:
            # A wake-up is ancillary; its failure must not invalidate an
            # already committed, complete and correctly scoped observation.
            from utils.log import logger
            logger.exception("调控指标已独立提交，但即时唤醒停投规则失败 target=%s", uid)
    return bundle
