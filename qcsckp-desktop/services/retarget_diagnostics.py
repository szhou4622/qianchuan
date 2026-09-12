"""Structured, local-only retarget evidence; never changes business outcomes."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from api.rule_retargeting_config import build_trigger_evaluation_snapshot
from services.operation_diagnostics import record


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _materials(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    source = task.get("materials") or task.get("candidate_materials") or []
    result = []
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, Mapping):
            continue
        mid = str(item.get("material_id") or "").strip()
        if mid and mid not in {row["material_id"] for row in result}:
            result.append({"material_id": mid, "product_ids": list(item.get("product_ids") or [])[:20]})
    return result[:20]


def _card_evaluations(task: Mapping[str, Any]) -> dict[str, Any]:
    trigger = task.get("trigger_snapshot") if isinstance(task.get("trigger_snapshot"), Mapping) else {}
    return {
        str(item.get("material_id") or ""): item.get("evaluation")
        for item in trigger.get("materials") or [] if isinstance(item, Mapping)
    }


def _reason_code(error: BaseException | str) -> str:
    text = str(error or "")
    rules = (
        ("最新素材数据中已找不到", "material_missing_from_current_scope"),
        ("最新数据已不满足", "rule_no_longer_matches"),
        ("实时数据已超过10分钟", "metric_stale"),
        ("策略", "strategy_changed"),
        ("授权", "authorization_changed"),
        ("领取权", "claim_lost"),
        ("次数上限", "rate_limit_reached"),
        ("分页", "pagination_incomplete"),
        ("素材列表变化", "pagination_incomplete"),
        ("等待监控容量", "capacity_waiting"),
    )
    if type(error).__name__ == "PaginationDriftError":
        return "pagination_incomplete"
    return next((code for marker, code in rules if marker in text), "revalidation_failed")


def _material_probe(db: Any, task: Mapping[str, Any], material_id: str,
                    *, current_ids: set[str], query_incomplete: bool) -> dict[str, Any]:
    target_uid = str(task.get("target_uid") or "")
    result: dict[str, Any] = {"material_id": material_id, "queried_at": _now()}
    try:
        latest = db.select_one("pmc_promotion_material_latest", where={
            "target_uid": target_uid, "material_id": material_id,
        }) or {}
        history = db.execute(
            "SELECT collected_at,metric_row_state FROM pmc_material_metric_snapshot "
            "WHERE target_uid=? AND material_id=? ORDER BY collected_at DESC,id DESC LIMIT 1",
            (target_uid, material_id), fetch=True,
        ) or []
        if query_incomplete:
            result.update(reason_code="pagination_incomplete", latest_record=bool(latest),
                          history_record=bool(history))
            return result
        if not latest:
            result.update(reason_code=("latest_missing_history_only" if history else "no_local_record"),
                          latest_record=False, history_record=bool(history),
                          history_observed_at=(history[0].get("collected_at") if history else None))
            return result
        delivery = str(latest.get("delivery_state") or "delivering")
        target = db.select_one("promotion_target", where={"target_uid": target_uid}) or {}
        if delivery != "delivering":
            code = "excluded_delivery_state"
        elif not target.get("enabled") or not target.get("monitor_eligible"):
            code = "excluded_target_state"
        elif material_id not in current_ids:
            observed = str(latest.get("collected_at") or "")
            try:
                code = ("metric_stale" if datetime.now() - datetime.fromisoformat(observed) > timedelta(minutes=10)
                        else "excluded_by_current_query")
            except ValueError:
                code = "observation_time_unavailable"
        else:
            code = "present_in_latest"
        result.update(reason_code=code, latest_record=True, history_record=bool(history),
                      delivery_state=delivery, metric_row_state=latest.get("metric_row_state"),
                      observed_at=latest.get("collected_at"), target_enabled=bool(target.get("enabled")),
                      monitor_eligible=bool(target.get("monitor_eligible")))
    except Exception as exc:
        result.update(reason_code="diagnostic_query_error", query_error_type=type(exc).__name__)
    return result


def _comparisons(task: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    card = _card_evaluations(task)
    trigger_snapshot = task.get("trigger_snapshot") if isinstance(task.get("trigger_snapshot"), Mapping) else {}
    trigger = trigger_snapshot.get("trigger_config") if isinstance(trigger_snapshot.get("trigger_config"), Mapping) else {}
    current = {str(row.get("id") or ""): row for row in rows}
    failed = []
    passed = []
    for material in _materials(task):
        mid = material["material_id"]
        row = current.get(mid)
        item = {
            "material_id": mid,
            "card_evaluation": card.get(mid, {"status": "not_recorded"}),
            "current_evaluation": (build_trigger_evaluation_snapshot(trigger, dict(row)) if row and trigger
                                   else {"status": "not_recorded" if not row else "trigger_not_recorded"}),
            "current_observed_at": ((row or {}).get("periodEndTime") or (row or {}).get("createdAt")),
            "metric_row_state": (row or {}).get("metricRowState"),
            "display_zero_fields": list((row or {}).get("displayZeroFields") or []),
        }
        evaluation = item["current_evaluation"]
        (passed if evaluation.get("passed") is True else failed).append(item)
    return failed + passed[:10]


def card_issued(task_uid: str, payload: Mapping[str, Any]) -> str:
    return record("retarget_card_issued", stage="card_issued", reason_code="candidate_matched",
                  task_uid=task_uid, target_uid=payload.get("target_uid"), aavid=payload.get("aavid"),
                  ad_id=payload.get("ad_id"), strategy_id=payload.get("strategy_id"),
                  strategy_hash=payload.get("strategy_hash"), triggered_at=payload.get("triggered_at"),
                  metric_scope=(payload.get("query_snapshot") or {}).get("target"),
                  query_snapshot=payload.get("query_snapshot"), trigger_snapshot=payload.get("trigger_snapshot"),
                  groups=payload.get("retarget_groups") or [], materials=_materials(payload))


def card_action(task_uid: str, action: str, payload: Mapping[str, Any], **extra: Any) -> str:
    return record("retarget_card_action", stage="card_action", reason_code=str(action or "unknown"),
                  task_uid=task_uid, action=action, selected_at=_now(),
                  target_uid=payload.get("target_uid"), strategy_id=payload.get("strategy_id"),
                  strategy_hash=payload.get("strategy_hash"), groups=payload.get("retarget_groups") or [],
                  selection_snapshot=payload.get("selection_snapshot"), **extra)


def revalidation(task: Mapping[str, Any], db: Any, *, rows: list[Mapping[str, Any]] | None = None,
                 error: BaseException | None = None, group_uid: str = "", execution_uid: str = "") -> str:
    rows = list(rows or [])
    current_ids = {str(row.get("id") or "") for row in rows}
    query_incomplete = _reason_code(error) == "pagination_incomplete" if error else False
    probes = [_material_probe(db, task, item["material_id"], current_ids=current_ids,
                              query_incomplete=query_incomplete) for item in _materials(task)]
    return record("retarget_revalidation", stage="revalidation_failed" if error else "revalidation_passed",
                  reason_code=_reason_code(error) if error else "rule_still_matches",
                  task_uid=task.get("parent_task_uid") or task.get("task_uid"),
                  execution_uid=execution_uid or task.get("execution_uid") or task.get("task_uid"),
                  group_uid=group_uid, target_uid=task.get("target_uid"), aavid=task.get("aavid"),
                  ad_id=task.get("ad_id"), strategy_id=task.get("strategy_id"),
                  strategy_hash=task.get("strategy_hash"), checked_at=_now(),
                  error_type=type(error).__name__ if error else "", material_probes=probes,
                  comparisons=_comparisons(task, rows))
