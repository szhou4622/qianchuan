"""Assemble bounded, exactly-related diagnostic incidents for report revision 5."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping


FAIL_STATUSES = ("failed", "unknown_requires_review", "partial_succeeded")
SUCCESS_STATUSES = ("succeeded", "naturally_expired")
MAX_REPORT_BYTES = 5 * 1024 * 1024
MAX_INCIDENT_BYTES = 256 * 1024


def _json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        result = json.loads(str(value or ""))
        return result if isinstance(result, (dict, list)) else ({} if default is None else default)
    except (TypeError, ValueError):
        return {} if default is None else default


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _reason(text: Any, status: str = "") -> str:
    value = str(text or "")
    rules = (
        ("最新素材数据中已找不到", "material_missing_from_current_scope"),
        ("最新数据已不满足", "rule_no_longer_matches"),
        ("实时数据已超过10分钟", "metric_stale"),
        ("分页", "pagination_incomplete"),
        ("领取权", "claim_lost"),
        ("次数上限", "rate_limit_reached"),
        ("策略", "strategy_changed"),
        ("授权", "authorization_changed"),
    )
    return next((code for marker, code in rules if marker in value), status or "unclassified")


def _reconciliation(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = _json(row.get("payload_json"))
    return {key: row.get(key) for key in (
        "task_uid", "action_type", "status", "request_id", "control_task_id",
        "idempotency_key", "attempt_count", "card_update_state", "created_at", "updated_at",
    )} | {"execution_uid": payload.get("execution_uid"),
           "submission_phase": payload.get("submission_phase"),
           "terminal_result": payload.get("terminal_result")}


def _audit(row: Mapping[str, Any]) -> dict[str, Any]:
    request = _json(row.get("request_summary_json"))
    response = _json(row.get("response_summary_json"))
    body = request.get("body") if isinstance(request.get("body"), Mapping) else {}
    query = request.get("query") if isinstance(request.get("query"), Mapping) else {}
    allowed = ("advertiser_id", "ad_id", "task_id", "task_ids", "scene", "budget", "duration",
               "material_ids", "metrics", "fields", "filtering", "page", "page_size", "start_time", "end_time")
    return {key: row.get(key) for key in ("endpoint", "method", "request_id", "status", "error_code", "created_at")} | {
        "request": {key: (body.get(key) if key in body else query.get(key)) for key in allowed
                    if key in body or key in query},
        "response": {key: response.get(key) for key in ("code", "message", "verification_error", "pagination")
                     if key in response},
    }


def _run(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in (
        "execution_uid", "execution_state", "step", "status", "message", "regulate_task_id",
        "assist_task_id", "started_at", "created_at", "ended_at", "aavid", "ad_id", "target_uid",
        "promotion_scene", "plan_system",
    )} | {"trigger_snapshot": _json(row.get("trigger_snapshot_json")),
           "query_snapshot": _json(row.get("query_snapshot_json")),
           "materials": _json(row.get("materials_json"), [])}


def _delivery(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = _json(row.get("payload_json"))
    return {key: row.get(key) for key in (
        "operation", "message_id", "status", "attempt_count", "created_at", "updated_at",
    )} | {"receipt": {key: payload.get(key) for key in (
        "business_version", "content_sha256", "delivery_stage") if key in payload}}


def _scope(payload: Mapping[str, Any]) -> tuple[str, ...]:
    query = payload.get("query_snapshot") if isinstance(payload.get("query_snapshot"), Mapping) else {}
    return tuple(str(value or "") for value in (
        payload.get("aavid"), payload.get("ad_id"), payload.get("target_uid"),
        payload.get("strategy_id"), payload.get("strategy_hash"),
        payload.get("promotion_scene"), payload.get("plan_system"), query.get("query_period"),
    ))


def _run_scope(row: Mapping[str, Any]) -> tuple[str, ...]:
    rule = _json(row.get("rule_full_json"))
    return tuple(str(value or "") for value in (
        row.get("aavid"), row.get("ad_id"), row.get("target_uid"),
        rule.get("id"), rule.get("strategy_hash"), row.get("promotion_scene"),
        row.get("plan_system"), (_json(row.get("query_snapshot_json")).get("query_period")),
    ))


def _task_candidates(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    cutoff = "-24 hours"
    failures = _rows(conn,
        "SELECT * FROM local_retarget_task WHERE status IN (?,?,?) "
        "AND created_at>=datetime('now','+8 hours',?) ORDER BY id DESC LIMIT 20",
        (*FAIL_STATUSES, cutoff))
    if not failures:
        cutoff = "-7 days"
        failures = _rows(conn,
            "SELECT * FROM local_retarget_task WHERE status IN (?,?,?) "
            "AND created_at>=datetime('now','+8 hours',?) ORDER BY id DESC LIMIT 20",
            (*FAIL_STATUSES, cutoff))
    successes = _rows(conn,
        "SELECT * FROM local_retarget_task WHERE status IN (?,?) "
        "AND created_at>=datetime('now','+8 hours','-7 days') ORDER BY id DESC LIMIT 100",
        SUCCESS_STATUSES)
    return failures, successes, "24h" if cutoff == "-24 hours" else "7d"


def _query_shared(task_uids: list[str], errors: list[dict[str, str]]) -> dict[str, list[dict[str, Any]]]:
    result = {uid: [] for uid in task_uids}
    if not task_uids:
        return result
    try:
        from channel_runtime import layout
        path = layout().shared / "execution.sqlite3"
        if not path.is_file():
            return result
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            marks = ",".join("?" for _ in task_uids)
            for row in _rows(conn, f"SELECT * FROM execution_reconciliation WHERE task_uid IN ({marks}) ORDER BY id", tuple(task_uids)):
                result.setdefault(str(row.get("task_uid") or ""), []).append(row)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        errors.append({"stage": "shared_execution", "error_type": type(exc).__name__})
    return result


def _incident(conn: sqlite3.Connection, task: Mapping[str, Any], events: list[Mapping[str, Any]],
              reconciliations: list[Mapping[str, Any]], errors: list[dict[str, str]]) -> dict[str, Any]:
    uid = str(task.get("task_uid") or "")
    payload = _json(task.get("payload_json"))
    task_events = [dict(event) for event in events if str(event.get("task_uid") or "") == uid]
    runs = []
    try:
        if str(task.get("action_type") or "retarget") == "retarget":
            runs = _rows(conn, "SELECT execution_uid,execution_state,step,status,message,regulate_task_id,"
                               "started_at,ended_at,trigger_snapshot_json,query_snapshot_json,materials_json "
                               "FROM pmc_retargeting_run WHERE trigger_source LIKE ? ORDER BY id", (f"feishu_card:{uid}%",))
        else:
            runs = _rows(conn, "SELECT execution_uid,execution_state,step,status,message,assist_task_id,"
                               "created_at,ended_at,trigger_snapshot_json,query_snapshot_json "
                               "FROM pmc_regulation_run WHERE trigger_source LIKE ? ORDER BY id", (f"feishu_card:{uid}%",))
    except sqlite3.Error as exc:
        errors.append({"stage": "incident_runs", "task_uid": uid, "error_type": type(exc).__name__})
    messages = _json(task.get("card_messages_json"), [])
    message_ids = [str(item.get("message_id") or "") for item in messages if isinstance(item, Mapping) and item.get("message_id")]
    outbox = []
    try:
        outbox = _rows(conn, "SELECT operation,message_id,status,attempt_count,payload_json,created_at,updated_at "
                            "FROM feishu_outbox WHERE task_uid=? ORDER BY id", (uid,))
    except sqlite3.Error as exc:
        errors.append({"stage": "incident_outbox", "task_uid": uid, "error_type": type(exc).__name__})
    request_ids = {str(row.get("request_id") or "") for row in reconciliations if row.get("request_id")}
    audits = []
    try:
        if request_ids:
            marks = ",".join("?" for _ in request_ids)
            audits = _rows(conn, f"SELECT endpoint,method,request_id,status,error_code,request_summary_json,"
                                 f"response_summary_json,created_at FROM qianchuan_api_audit WHERE request_id IN ({marks}) ORDER BY id",
                           tuple(sorted(request_ids)))
    except sqlite3.Error as exc:
        errors.append({"stage": "incident_audit", "task_uid": uid, "error_type": type(exc).__name__})
    card_recorded = any(event.get("kind") == "retarget_card_issued" for event in task_events)
    confirmation_recorded = any(event.get("kind") == "retarget_card_action" and event.get("action") == "approve" for event in task_events)
    revalidations = [event for event in task_events if event.get("kind") == "retarget_revalidation"]
    timeline = sorted(task_events, key=lambda event: (str(event.get("at") or ""), str(event.get("evidence_id") or "")))
    result = _json(task.get("result_json"))
    return {
        "incident_uid": uid,
        "action_type": task.get("action_type"),
        "status": task.get("status"),
        "reason_code": (str(revalidations[-1].get("reason_code")) if revalidations
                        else _reason(task.get("result_message"), str(task.get("status") or ""))),
        "created_at": task.get("created_at"), "finished_at": task.get("finished_at"),
        "scope": {key: payload.get(key) for key in ("aavid", "ad_id", "target_uid", "promotion_scene", "plan_system",
                                                       "strategy_id", "strategy_hash", "trigger_level")},
        "groups": result.get("group_results") or payload.get("retarget_groups") or [],
        "card_snapshot": {"status": "recorded" if card_recorded else "recovered_from_task_payload",
                          "triggered_at": payload.get("triggered_at"),
                          "trigger_snapshot": payload.get("trigger_snapshot") or {"status": "not_recorded"},
                          "query_snapshot": payload.get("query_snapshot") or {"status": "not_recorded"}},
        "confirmation_snapshot": {"status": "recorded" if confirmation_recorded else "not_recorded",
                                  "selection": payload.get("selection_snapshot") or {"status": "not_recorded"}},
        "timeline": timeline,
        "runs": [_run(row) for row in runs],
        "reconciliation": [_reconciliation(row) for row in reconciliations],
        "api_requests": [_audit(row) for row in audits],
        "delivery": {"message_ids": message_ids, "outbox": [_delivery(row) for row in outbox],
                     "coverage": "recorded" if outbox else "not_recorded"},
    }


def _auto_incident(row: Mapping[str, Any], events: list[Mapping[str, Any]],
                   reconciliations: list[Mapping[str, Any]]) -> dict[str, Any]:
    uid = str(row.get("execution_uid") or "")
    timeline = [dict(event) for event in events if str(event.get("execution_uid") or "") == uid]
    return {
        "incident_uid": uid,
        "source": "auto_execute",
        "action_type": "retarget",
        "status": row.get("execution_state") or ("succeeded" if int(row.get("status") or 0) > 0 else "failed"),
        "reason_code": _reason(row.get("message"), str(row.get("step") or "unclassified")),
        "created_at": row.get("started_at"), "finished_at": row.get("ended_at"),
        "scope": {key: row.get(key) for key in ("aavid", "ad_id", "target_uid", "promotion_scene", "plan_system")},
        "groups": _json(row.get("materials_json"), []),
        "card_snapshot": {"status": "not_applicable", "mode": "auto_execute"},
        "confirmation_snapshot": {"status": "not_applicable", "mode": "auto_execute"},
        "trigger_snapshot": _json(row.get("trigger_snapshot_json")) or {"status": "not_recorded"},
        "query_snapshot": _json(row.get("query_snapshot_json")) or {"status": "not_recorded"},
        "timeline": sorted(timeline, key=lambda event: str(event.get("at") or "")),
        "runs": [_run(row)], "reconciliation": [_reconciliation(item) for item in reconciliations],
        "api_requests": [], "delivery": {"status": "not_applicable"},
    }


def _cap_incident(incident: dict[str, Any]) -> dict[str, Any]:
    def size() -> int:
        return len(json.dumps(incident, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    omitted = []
    if size() > MAX_INCIDENT_BYTES and len(incident.get("timeline") or []) > 10:
        omitted.append({"stage": "older_timeline", "count": len(incident["timeline"]) - 10})
        incident["timeline"] = incident["timeline"][-10:]
    if size() > MAX_INCIDENT_BYTES:
        card = incident.get("card_snapshot") or {}
        query = card.get("query_snapshot") or {}
        if isinstance(query.get("materials"), list) and len(query["materials"]) > 10:
            removed = len(query["materials"]) - 10
            query["materials"] = query["materials"][:10]
            omitted.append({"stage": "card_query_success_materials", "count": removed})
    if size() > MAX_INCIDENT_BYTES and len(incident.get("runs") or []) > 5:
        incident["runs"] = incident["runs"][-5:]
        omitted.append({"stage": "older_runs", "count": len(incident["runs"]) - 5})
    # Preserve the failure's identity and outcome. A large snapshot must not
    # silently break the per-incident bound or disguise a missing stage.
    for key in ("api_requests", "delivery", "reconciliation", "runs", "timeline", "groups",
                "card_snapshot", "confirmation_snapshot", "query_snapshot", "trigger_snapshot"):
        if size() <= MAX_INCIDENT_BYTES:
            break
        value = incident.get(key)
        if not value:
            continue
        count = len(value) if isinstance(value, list) else 1
        incident[key] = {"status": "truncated_for_capacity", "omitted_count": count}
        omitted.append({"stage": key, "count": count})
    if omitted:
        incident["capacity"] = {"truncated": True, "omitted_stages": omitted}
    return incident


def build_sections(conn: sqlite3.Connection, *, sanitize: Callable[[Any], Any], current_database: bool = True) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    try:
        failures, successes, window = _task_candidates(conn)
    except sqlite3.Error as exc:
        return {"summary": {"status": "query_failed"}, "incidents": [], "comparisons": [],
                "coverage": {"query_errors": [{"stage": "task_selection", "error_type": type(exc).__name__}]}}
    try:
        from services.operation_diagnostics import read_events_with_status
        event_status = read_events_with_status() if current_database else {
            "events": [], "status": "unavailable_for_external_database", "error_type": ""}
        events = event_status["events"]
    except Exception as exc:
        event_status = {"events": [], "status": "read_failed", "error_type": type(exc).__name__}
        events = []
    selected = failures[:20]
    automatic_failures: list[dict[str, Any]] = []
    automatic_successes: list[dict[str, Any]] = []
    try:
        automatic_failures = _rows(conn,
            "SELECT * FROM pmc_retargeting_run WHERE COALESCE(trigger_source,'') NOT LIKE 'feishu_card:%' AND status=-1 "
            "AND started_at>=datetime('now','+8 hours',?) ORDER BY id DESC LIMIT ?",
            ("-24 hours" if window == "24h" else "-7 days", max(0, 20-len(selected))))
        automatic_successes = _rows(conn,
            "SELECT * FROM pmc_retargeting_run WHERE COALESCE(trigger_source,'') NOT LIKE 'feishu_card:%' AND status=1 "
            "AND started_at>=datetime('now','+8 hours','-7 days') ORDER BY id DESC LIMIT 50")
    except sqlite3.Error as exc:
        errors.append({"stage": "automatic_runs", "error_type": type(exc).__name__})
    failure_scopes = {_scope(_json(row.get("payload_json"))): str(row.get("task_uid") or "")
                      for row in selected if all(_scope(_json(row.get("payload_json"))))}
    comparisons = [row for row in successes if _scope(_json(row.get("payload_json"))) in failure_scopes][:5]
    automatic_scopes = {_run_scope(row): str(row.get("execution_uid") or "")
                        for row in automatic_failures if all(_run_scope(row))}
    automatic_comparisons = [row for row in automatic_successes if _run_scope(row) in automatic_scopes]
    automatic_comparisons = automatic_comparisons[:max(0, 5-len(comparisons))]
    all_rows = selected + comparisons
    all_uids = [str(row.get("task_uid") or "") for row in all_rows]
    all_uids += [str(row.get("execution_uid") or "") for row in automatic_failures + automatic_comparisons]
    shared = _query_shared(all_uids, errors) if current_database else {uid: [] for uid in all_uids}
    task_incidents = [_cap_incident(_incident(conn, row, events, shared.get(str(row.get("task_uid") or ""), []), errors))
                      for row in all_rows]
    failure_incidents = task_incidents[:len(selected)] + [
        _cap_incident(_auto_incident(row, events, shared.get(str(row.get("execution_uid") or ""), [])))
        for row in automatic_failures]
    comparison_incidents = task_incidents[len(selected):] + [
        _cap_incident(_auto_incident(row, events, shared.get(str(row.get("execution_uid") or ""), [])))
        for row in automatic_comparisons]
    for incident, row in zip(comparison_incidents, comparisons + automatic_comparisons):
        scope = (_scope(_json(row.get("payload_json"))) if "task_uid" in row else _run_scope(row))
        incident["matched_failure_incident_uid"] = (failure_scopes if "task_uid" in row else automatic_scopes)[scope]
    issue_counts = Counter(item["reason_code"] for item in failure_incidents)
    stop_events = [event for event in events if str(event.get("kind") or "").startswith("stop_")][-200:]
    collection_events = [event for event in events if str(event.get("kind") or "").startswith("collection_")][-100:]
    summary_items = [{"reason_code": code, "count": count,
                      "last_at": max((str(item.get("finished_at") or item.get("created_at") or "")
                                      for item in failure_incidents if item["reason_code"] == code), default=""),
                      "confirmed_reason": code if code not in {"failed", "unknown_requires_review", "unclassified"} else None,
                      "pending_confirmation": code in {"failed", "unknown_requires_review", "unclassified"},
                      "incident_ids": [item["incident_uid"] for item in failure_incidents
                                       if item["reason_code"] == code],
                      "evidence_ids": [event.get("evidence_id") for item in failure_incidents
                                       if item["reason_code"] == code for event in item.get("timeline") or []
                                       if isinstance(event, Mapping) and event.get("evidence_id")][:50]}
                     for code, count in issue_counts.most_common()]
    sections = {
        "summary": {"selection_window": window, "failure_or_unknown_count": len(failure_incidents[:20]),
                    "success_comparison_count": len(comparison_incidents[:5]), "issues": summary_items,
                    "stop_scan_count": sum(1 for event in stop_events if event.get("kind") == "stop_scan_started"),
                    "last_stop_summary": next((event for event in reversed(stop_events)
                                               if event.get("kind") == "stop_summary"), {"status": "not_recorded"})},
        "incidents": failure_incidents[:20],
        "comparisons": comparison_incidents[:5],
        "coverage": {"runtime_evidence": event_status.get("status"),
                     "runtime_evidence_error": event_status.get("error_type"),
                     "query_errors": errors,
                     "legacy_missing_confirmation_is_not_inferred": True,
                     "limits": {"failures": 20, "success_comparisons": 5, "per_material_successes": 10,
                                "report_bytes": MAX_REPORT_BYTES},
                     "omitted": {"failures": max(0, len(failures) + len(automatic_failures) - len(failure_incidents[:20])),
                                 "successes": max(0, len(successes) - len(comparisons))}},
        "stop_diagnostics": stop_events,
        "collection_diagnostics": collection_events,
    }
    return sanitize(sections)


def enforce_size(report: dict[str, Any]) -> dict[str, Any]:
    def size() -> int:
        return len(json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    coverage = report.setdefault("coverage", {})
    omitted = coverage.setdefault("size_omissions", [])
    while size() > MAX_REPORT_BYTES and report.get("comparisons"):
        report["comparisons"].pop()
        omitted.append("success_comparison")
    for key in ("api_recent", "material_metric_evidence", "operation_evidence", "diagnostic_events",
                "stop_diagnostics", "collection_diagnostics"):
        while size() > MAX_REPORT_BYTES and report.get(key):
            before = len(report[key])
            report[key] = report[key][max(1, before // 2):]
            omitted.append({"stage": key, "count": before - len(report[key])})
    while size() > MAX_REPORT_BYTES and report.get("incidents"):
        incident = report["incidents"][-1]
        if isinstance(incident.get("timeline"), list) and len(incident["timeline"]) > 1:
            before = len(incident["timeline"])
            incident["timeline"] = incident["timeline"][-max(1, before // 2):]
            omitted.append({"stage": "incident_timeline", "count": before - len(incident["timeline"])})
        elif len(report["incidents"]) > 1:
            report["incidents"].pop()
            omitted.append({"stage": "incident", "count": 1})
        else:
            break
    # Legacy sections remain available, but they cannot make an exported
    # report unbounded. Remove them last and identify the lost stage.
    for key in tuple(report):
        if size() <= MAX_REPORT_BYTES:
            break
        if key in {"report_revision", "summary", "incidents", "comparisons", "coverage"}:
            continue
        if report.get(key):
            count = len(report[key]) if isinstance(report[key], list) else 1
            report[key] = [] if isinstance(report[key], list) else {"status": "truncated_for_capacity"}
            omitted.append({"stage": key, "count": count})
    if size() > MAX_REPORT_BYTES and report.get("incidents"):
        report["incidents"] = [{"incident_uid": incident.get("incident_uid"),
                                "reason_code": incident.get("reason_code"),
                                "status": incident.get("status"),
                                "capacity": {"truncated": True, "omitted_stages": ["incident_detail"]}}
                               for incident in report["incidents"]]
        omitted.append({"stage": "incident_detail", "count": len(report["incidents"])})
    if size() > MAX_REPORT_BYTES:
        report["summary"] = {"status": "truncated_for_capacity"}
        omitted.append({"stage": "summary_detail", "count": 1})
    coverage["report_bytes"] = size()
    coverage["truncated"] = bool(omitted)
    return report
