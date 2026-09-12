"""Build a local, shareable failure report without uploading business data."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import sqlite3
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from config import DB_FILE
from release_identity import IDENTITY
from utils.log_redaction import redact_text


_SECRET_KEYS = {
    "access_token", "refresh_token", "authorization", "access-token",
    "app_secret", "secret", "device_session", "device_credential",
    "activation_code", "cookie", "cookies", "encrypt_key",
    "verification_token", "poll_secret", "api_key", "sessionid", "session_id",
}
_ID_KEYS = {
    "advertiser_id", "aavid", "aadvid", "ad_id", "material_id",
    "material_ids", "task_id", "control_task_id", "regulate_task_id",
    "anchor_id", "aweme_id", "aweme_uid", "account_uid", "target_uid",
    "receive_id", "open_id", "user_id", "chat_id", "code_id", "task_uid",
    "run_uid", "execution_uid", "incident_uid", "evidence_id", "group_uid",
    "strategy_id", "message_id",
}
_NAME_KEYS = {
    "account_name", "advertiser_name", "plan_name", "material_name",
    "product_name", "anchor_name", "task_name", "title", "name",
}
_MESSAGE_KEYS = {
    "message", "error", "help_message", "last_error", "result_message",
    "detail", "reason",
}
_LONG_ID = re.compile(r"(?<!\d)\d{12,}(?!\d)")
_URL = re.compile(r"https?://[^\s\"']+", re.I)
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\r\n\"']+")
_QUERY_ERRORS: ContextVar[list[dict[str, str]]] = ContextVar("failure_report_query_errors", default=[])


def _digest(value: Any) -> str:
    text = str(value or "")
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _safe_text(value: Any) -> str:
    text = redact_text(value)[:4000]
    text = _URL.sub("<url>", text)
    text = _WINDOWS_PATH.sub("<local-path>", text)
    return _LONG_ID.sub(lambda match: "<id:" + _digest(match.group())[7:] + ">", text)


def _private_values(value: Any) -> tuple[str, ...]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            key = str(key).lower()
            if (key in _SECRET_KEYS or key in _NAME_KEYS or key.endswith("_name")) and isinstance(item, str) and len(item) >= 3:
                found.add(item)
            elif isinstance(item, (Mapping, list, tuple)):
                found.update(_private_values(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_private_values(item))
    return tuple(sorted(found, key=len, reverse=True))[:256]


def _diagnostic_text(value: Any, private_values: tuple[str, ...] = ()) -> dict[str, Any]:
    text = str(value or "")
    # Arbitrary short alphanumeric strings can be secrets or English names.
    allowed = {
        "RuntimeError", "ValueError", "TypeError", "KeyError", "OSError",
        "PermissionError", "FileNotFoundError", "TimeoutError", "ConnectionError",
        "ApiRequestError", "ApiRateLimitError", "ApiWriteOutcomeUnknown",
        "FeishuApiError", "OperationalError", "IntegrityError", "JSONDecodeError",
        "HTTPError", "URLError", "Traceback", "GET", "POST", "PATCH", "PUT",
        "DELETE", "failed", "verifying", "submitted", "confirmed_succeeded",
        "confirmed_failed", "unknown_requires_review",
    }
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{2,80}", redact_text(text)):
        if token not in allowed:
            continue
        if token not in tokens:
            tokens.append(token)
    result = {
        "redacted": True,
        "length": len(text),
        "fingerprint": _digest(text),
        "technical_tokens": tokens[:40],
    }
    # Keep parameter and failure explanations; arbitrary business/name-only
    # messages remain opaque. Known private values are removed before the
    # generic credential, header, URL, path and long-ID scrubbers run.
    if re.search(r"参数|字段|分页|错误|失败|超时|权限|授权|重试|请求|不支持|不一致|不存在|过期|(?i:exception|error|invalid|field|parameter|timeout|page|failed|denied|mismatch)", text):
        for private in private_values:
            text = text.replace(private, "<redacted>")
        text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer <redacted>", text)
        text = _safe_text(text)
        result["text"] = re.sub(r"\b[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*\b", lambda match: match.group(0) if match.group(0) in allowed else "<name>", text)
    return result


def sanitize(value: Any, *, key: str = "", private_values: tuple[str, ...] = ()) -> Any:
    normalized = str(key or "").strip().lower()
    if normalized in {"request_ids", "proof_request_ids", "report_request_ids", "optional_metric_request_ids"} and isinstance(value, (list, tuple)):
        return [sanitize(item, key="request_id") for item in value]
    if normalized == "document_id" and str(value) in {"1804363488115850", "1823297941140569"}:
        return str(value)
    if normalized in _SECRET_KEYS or any(token in normalized for token in ("password", "credential", "secret", "token")):
        return "<redacted>"
    if normalized == "request_id":
        request_id = str(value or "")
        return request_id if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) else _safe_text(request_id)
    if normalized in _ID_KEYS or normalized.endswith("_ids"):
        if isinstance(value, (list, tuple, set)):
            return [_digest(item) for item in value]
        return _digest(value) if value not in (None, "") else ""
    if normalized in _NAME_KEYS or normalized.endswith("_name"):
        text = str(value or "")
        return {"redacted": True, "length": len(text), "fingerprint": _digest(text)}
    if normalized in _MESSAGE_KEYS or normalized.endswith("_message") or normalized.endswith("_error"):
        if isinstance(value, Mapping):
            return {str(k): sanitize(v, key=str(k), private_values=private_values) for k, v in value.items()}
        return _diagnostic_text(value, private_values)
    if isinstance(value, Mapping):
        private_values = tuple(dict.fromkeys((*private_values, *_private_values(value))))
        result = {str(k): sanitize(v, key=str(k), private_values=private_values) for k, v in value.items()}
        filter_field = str(value.get("field") or "").strip().lower()
        if filter_field in _ID_KEYS and "values" in result:
            original = value.get("values") or []
            result["values"] = [_digest(item) for item in original]
        return result
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item, key=normalized, private_values=private_values) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value)


def _parse_json(value: Any) -> Any:
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, (dict, list)) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"invalid_json": True}


def _trace_evidence(value: Any) -> dict[str, Any]:
    text = str(value or "")
    frames = [
        {"file": Path(match.group(1)).name, "line": int(match.group(2))}
        for match in re.finditer(r'File ["\']([^"\']+\.py)["\'], line (\d+)', text)
    ][-16:]
    last = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
    return {"frames": frames, "last_error": _diagnostic_text(last)}


def _rows(conn: sqlite3.Connection, query: str, params=()) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    except sqlite3.Error as exc:
        errors = list(_QUERY_ERRORS.get())
        errors.append({"error_type": type(exc).__name__, "query_fingerprint": _digest(query)})
        _QUERY_ERRORS.set(errors)
        return []


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _current_process_health(targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Whitelist live diagnostics; never export arbitrary object keys or scope data."""
    from services.runtime_supervisor import RUNTIME_SUPERVISOR
    from services.official_api_collection import get_target_collection_progress

    raw = RUNTIME_SUPERVISOR.health_snapshot()
    public = {key: raw[key] for key in ("state", "started", "starting") if key in raw}
    public["services"] = {
        str(name): {key: value[key] for key in ("status", "alive", "error") if key in value}
        for name, value in (raw.get("services") or {}).items()
        if re.fullmatch(r"[a-z_]{1,40}", str(name)) and isinstance(value, Mapping)
    }
    collector = raw.get("collector") or {}
    public["collector"] = {key: collector[key] for key in (
        "status", "thread_alive", "active_batches", "stalled_after_seconds",
        "heartbeat_age_seconds", "watchdog_reason", "retry_after_seconds",
    ) if key in collector}
    for section, fields in (("workers", ("capacity", "occupied", "unacknowledged")),
                            ("resource_pressure", ("available", "critical", "commit_percent", "commit_headroom_mb"))):
        value = collector.get(section)
        if isinstance(value, Mapping):
            public["collector"][section] = {key: value[key] for key in fields if key in value}
    public["target_progress"] = []
    for target in targets:
        progress = get_target_collection_progress(str(target.get("target_uid") or ""))
        if not progress:
            continue
        public["target_progress"].append({"target_uid": target.get("target_uid"),
            **{key: progress[key] for key in ("phase", "page", "page_count", "actual_list_count",
               "attempt", "observed_at", "rescan_count") if key in progress}})
    return {"scope": "current_process", **sanitize(public)}


def _append_metric_evidence(conn, report):
    try:
        # Successful batches are also evidence: a zero-valued or partial
        # batch need not have raised an exception. Never read other hosts.
        from services.material_metric_contract import FIELDS
        from api.dashboard_optimized import _optional_float
        candidates = conn.execute("SELECT target_uid,ad_id,aadvid,capability_json FROM promotion_target WHERE enabled=1 ORDER BY updated_at DESC LIMIT 30").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM promotion_target WHERE enabled=1").fetchone()[0]
        report["metric_evidence_coverage"] = {"target_count": total, "included_targets": len(candidates), "truncated": total > len(candidates), "samples_per_target_limit": 6}
        for target in candidates:
            capability = _parse_json(target["capability_json"])
            trace = capability.get("material_metric_evidence") if isinstance(capability, dict) else None
            if not isinstance(trace, dict):
                report["metric_findings"].append(sanitize({"target_uid": target["target_uid"], "finding": "raw_metric_evidence_not_recorded"}))
                continue
            samples = []
            for sample in trace.get("samples", [])[:6]:
                mid = str(sample.get("material_id") or "")
                columns = ",".join(FIELDS.values())
                stored = conn.execute("SELECT collected_at,stat_date," + columns + " FROM pmc_promotion_material_latest WHERE target_uid=? AND material_id=?", (target["target_uid"], mid)).fetchone()
                fields = {}
                for field, entry in sample.get("fields", {}).items():
                    if field not in FIELDS or not isinstance(entry, dict):
                        continue
                    fields[field] = {**entry, "stored_value": stored[FIELDS[field]] if stored else None,
                                         "dashboard_value": _optional_float(stored[FIELDS[field]]) if stored else None}
                samples.append({**sample, "fields": fields, "row_found": stored is not None,
                                "stored_observed_at": stored["collected_at"] if stored else None,
                                "same_observation": bool(stored and stored["collected_at"] == trace.get("observed_at") and stored["stat_date"] == trace.get("stat_date"))})
            report["material_metric_evidence"].append(sanitize({"target_uid": target["target_uid"], "ad_id": target["ad_id"], "aadvid": target["aadvid"], **trace, "samples": samples,
                "dashboard_value_source": "server_numeric_conversion_not_browser_capture"}))
            counts = trace.get("reported_field_counts", trace.get("field_counts", {}))
            incomplete = any(int(item.get("missing", 0))+int(item.get("null", 0))+int(item.get("invalid", 0)) for item in counts.values())
            cost = counts.get("stat_cost_for_roi2", {})
            finding = "missing_or_invalid_metrics" if incomplete else "sparse_report_complete" if trace.get("report_rows_absent", 0) else "all_cost_values_zero_requires_comparison" if cost.get("valid", 0) and cost.get("valid") == cost.get("zero") else "metric_evidence_available"
            optional_error_count = int(trace.get("optional_metric_error_count") or 0)
            report["metric_findings"].append(sanitize({"target_uid": target["target_uid"], "finding": finding,
                "source": trace.get("source"), "scope": trace.get("scope"),
                "optional_metric_error_count": optional_error_count,
                "warning": "optional_metrics_unavailable_core_values_preserved" if optional_error_count else "",
                "report_rows_present": trace.get("report_rows_present"), "observed_at": trace.get("observed_at")}))
    except (sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        report["metric_evidence_read_error"] = {"type": type(exc).__name__, "message": _safe_text(str(exc))}


def build_failure_report(*, db_path: str = DB_FILE) -> dict[str, Any]:
    from release_configuration import public_runtime_contract

    report: dict[str, Any] = {
        "schema": "qcsckp-failure-report-v1",
        "report_revision": 5,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "application": {
            "version": IDENTITY.get("version"),
            "channel": IDENTITY.get("channel"),
            "build_revision": IDENTITY.get("build_revision"),
            "source_commit": str(IDENTITY.get("source_commit") or "")[:40],
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "runtime_contract": public_runtime_contract(),
            "data_source_id": _digest(platform.node() + "|" + str(Path(db_path).resolve())),
        },
        "privacy": {
            "uploaded": False,
            "contains_credentials": False,
            "identifiers_hashed": True,
            "names_redacted": True,
        },
        "database": {"available": False, "quick_check": "not_run"},
        "metric_findings": [],
        "material_metric_evidence": [],
        "api_recent": [],
        "api_failures": [],
        "task_lifecycle": [],
        "outcomes_requiring_review": [],
        "targets_in_progress": [],
        "collection_health": {},
        "retarget_failures": [],
        "stop_failures": [],
        "regulation_failures": [],
        "reconciliation": [],
        "feishu_outbox": [],
        "target_errors": [],
        "diagnostic_events": [],
        "runtime_health": {"scope": "not_collected"},
        "summary": {"status": "not_collected"},
        "incidents": [],
        "comparisons": [],
        "coverage": {},
    }
    _QUERY_ERRORS.set([])
    path = Path(db_path)
    current_database = path.resolve() == Path(DB_FILE).resolve()
    live_targets: list[dict[str, Any]] = []
    if not current_database:
        report["runtime_health"] = {"scope": "unavailable_for_external_database"}
    if not path.is_file():
        return report
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
    except sqlite3.Error as exc:
        report["database"] = {"available": False, "quick_check": "open_failed", "error_type": type(exc).__name__}
        return report
    try:
        report["database"] = {
            "available": True,
            "quick_check": str(conn.execute("PRAGMA quick_check").fetchone()[0]),
        }
        if _table_exists(conn, "qianchuan_api_audit"):
            audit_fields = "endpoint,method,aavid,ad_id,task_id,request_id,error_code,status,request_summary_json,response_summary_json,created_at"
            audits = _rows(conn, "SELECT " + audit_fields + " FROM qianchuan_api_audit ORDER BY id DESC LIMIT 200")
            failures = _rows(conn, "SELECT " + audit_fields + " FROM qianchuan_api_audit WHERE status IN ('failed','unknown','incomplete') ORDER BY id DESC LIMIT 100")
            def audit_rows(rows):
                result = []
                for row in reversed(rows):
                    request = _parse_json(row.get("request_summary_json"))
                    response = _parse_json(row.get("response_summary_json"))
                    result.append(sanitize({
                        **{k: row.get(k) for k in ("endpoint", "method", "aavid", "ad_id", "task_id", "request_id", "error_code", "status", "created_at")},
                        "request": {key: request.get(key) for key in ("query", "body") if key in request},
                        "response": {key: response.get(key) for key in (
                            "code", "message", "help_message", "verification_error", "pagination",
                            "attempt", "page_attempt", "http_attempt", "sent_at", "elapsed_ms",
                        ) if key in response},
                    }))
                return result
            report["api_recent"] = audit_rows(audits)
            report["api_failures"] = audit_rows(failures)
        if _table_exists(conn, "local_retarget_task"):
            rows = _rows(conn, "SELECT task_uid,action_type,status,result_message,result_detail,regulate_task_id,created_at,finished_at FROM local_retarget_task WHERE status IN ('failed','unknown_requires_review','expired','cancelled','invalidated','partial_succeeded') ORDER BY id DESC LIMIT 200")
            normalized_tasks = [sanitize({
                **{k: row.get(k) for k in ("task_uid", "action_type", "status", "result_message", "regulate_task_id", "created_at", "finished_at")},
                "trace": _trace_evidence(row.get("result_detail")),
            }) for row in rows]
            report["retarget_failures"] = [
                item for item in normalized_tasks if item.get("action_type") == "retarget"
                and item.get("status") == "failed"
            ][:50]
            report["stop_failures"] = [
                item for item in normalized_tasks if item.get("action_type") == "stop"
                and item.get("status") == "failed"
            ][:50]
            report["task_lifecycle"] = [item for item in normalized_tasks if item.get("status") in {"expired", "cancelled", "invalidated"}][:100]
            report["outcomes_requiring_review"] = [item for item in normalized_tasks if item.get("status") in {"unknown_requires_review", "partial_succeeded"}][:50]
        if _table_exists(conn, "pmc_regulation_run"):
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pmc_regulation_run)")}
            wanted = [name for name in ("execution_uid", "execution_state", "step", "status", "message", "assist_task_id", "created_at", "ended_at", "updated_at") if name in columns]
            if wanted:
                rows = _rows(
                    conn,
                    "SELECT " + ",".join(wanted)
                    + " FROM pmc_regulation_run WHERE NOT (status IN (1,2) "
                    "OR COALESCE(execution_state,'')='confirmed_succeeded' "
                    "OR COALESCE(step,'') IN ('confirmed_succeeded','terminal_natural')) "
                    "ORDER BY rowid DESC LIMIT 50",
                )
                report["regulation_failures"] = [sanitize(row) for row in rows]
        if _table_exists(conn, "execution_reconciliation"):
            columns = {row[1] for row in conn.execute("PRAGMA table_info(execution_reconciliation)")}
            wanted = [name for name in ("task_uid", "action_type", "status", "request_id", "control_task_id", "last_error", "attempt_count", "card_update_state", "created_at", "updated_at") if name in columns]
            if wanted:
                rows = _rows(conn, "SELECT " + ",".join(wanted) + " FROM execution_reconciliation ORDER BY rowid DESC LIMIT 100")
                report["reconciliation"] = [sanitize(row) for row in rows]
        if _table_exists(conn, "feishu_outbox"):
            columns = {row[1] for row in conn.execute("PRAGMA table_info(feishu_outbox)")}
            wanted = [name for name in ("task_uid", "operation", "receive_type", "message_id", "status", "attempt_count", "last_error", "created_at", "updated_at") if name in columns]
            if wanted:
                rows = _rows(
                    conn,
                    "SELECT " + ",".join(wanted)
                    + ",payload_json FROM feishu_outbox WHERE operation='update_card' OR status IN ('queued','sending','failed','unknown') "
                    "ORDER BY rowid DESC LIMIT 100",
                )
                report["feishu_outbox"] = []
                for row in rows:
                    try:
                        payload = json.loads(row.pop("payload_json", "") or "{}")
                    except (ValueError, TypeError):
                        payload = {}
                    row["receipt"] = {key: payload.get(key) for key in ("business_version", "content_sha256")}
                    report["feishu_outbox"].append(sanitize(row))
        if _table_exists(conn, "promotion_target"):
            _append_metric_evidence(conn, report)
            fields = "target_uid,aadvid,ad_id,promotion_scene,plan_system,platform_status,last_status,last_error,last_sync_at,updated_at"
            rows = _rows(conn, "SELECT " + fields + " FROM promotion_target WHERE enabled=1 AND COALESCE(last_status,'') NOT IN ('collecting','queued') AND (COALESCE(last_error,'')<>'' OR last_status IN ('error','failed','pagination_error','rate_limited','auth_required','permission_denied','suspicious_empty','resource_pressure','deadline','collection_deadline')) ORDER BY updated_at DESC LIMIT 100")
            report["target_errors"] = [sanitize(row) for row in rows]
            rows = _rows(conn, "SELECT " + fields + " FROM promotion_target WHERE enabled=1 AND last_status IN ('collecting','queued') ORDER BY updated_at DESC LIMIT 100")
            report["targets_in_progress"] = [sanitize({**row, "last_error": ""}) for row in rows]
            live_targets = rows
        if _table_exists(conn, "collection_job"):
            report["collection_health"] = {
                "queue": _rows(conn, "SELECT status,COUNT(*) count,MIN(due_at) earliest_due,MAX(last_finished_at) last_finished_at FROM collection_job GROUP BY status"),
                "expired_leases": _rows(conn, "SELECT target_uid,job_kind,status,last_started_at,lease_expires_at FROM collection_job WHERE status='leased' AND lease_expires_at<datetime('now','+8 hours') LIMIT 20"),
            }
            report["collection_health"] = sanitize(report["collection_health"])
    finally:
        conn.close()
    if current_database:
        try:
            from services.operation_diagnostics import read_events
            events = list(reversed(read_events()))
            priority = [e for e in events if e.get("kind") in {"stop_skipped", "stop_scan_skipped", "collection_resource_wait"}]
            normal = [e for e in events if e.get("kind") not in {"stop_skipped", "stop_scan_skipped", "collection_resource_wait"}]
            report["operation_evidence"] = [sanitize(event) for event in (priority[:100] + normal[:100])]
            report["runtime_health"] = _current_process_health(live_targets)
        except Exception as exc:
            report["runtime_health"] = {"scope": "current_process", "available": False, "error_type": type(exc).__name__}
    try:
        from channel_runtime import layout

        shared_path = layout().shared / "execution.sqlite3"
        if (
            current_database
            and shared_path.is_file()
        ):
            shared = sqlite3.connect(
                shared_path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=5,
            )
            shared.row_factory = sqlite3.Row
            try:
                if _table_exists(shared, "execution_reconciliation"):
                    columns = {
                        row[1]
                        for row in shared.execute(
                            "PRAGMA table_info(execution_reconciliation)"
                        )
                    }
                    wanted = [
                        name
                        for name in (
                            "task_uid",
                            "action_type",
                            "status",
                            "request_id",
                            "control_task_id",
                            "last_error",
                            "attempt_count",
                            "card_update_state",
                            "created_at",
                            "updated_at",
                        )
                        if name in columns
                    ]
                    if wanted:
                        rows = _rows(
                            shared,
                            "SELECT " + ",".join(wanted)
                            + " FROM execution_reconciliation ORDER BY rowid DESC LIMIT 100",
                        )
                        report["reconciliation"] = [sanitize(row) for row in rows]
            finally:
                shared.close()
    except (OSError, sqlite3.Error):
        report["reconciliation_read_error"] = True
    try:
        from channel_runtime import layout

        events_path = layout().profile / "diagnostics" / "events.sqlite3"
        if current_database and events_path.is_file():
            events = sqlite3.connect(events_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                rows = events.execute("SELECT payload FROM events ORDER BY created DESC LIMIT 200").fetchall()
                report["diagnostic_events"] = [
                    sanitize(_parse_json(row[0])) for row in reversed(rows)
                ]
            finally:
                events.close()
    except (OSError, sqlite3.Error):
        report["diagnostic_events"] = [{"read_error": True}]
    try:
        from services.failure_report_v5 import build_sections, enforce_size
        incident_conn = sqlite3.connect(uri, uri=True, timeout=5)
        incident_conn.row_factory = sqlite3.Row
        incident_conn.execute("PRAGMA query_only=ON")
        try:
            v5 = build_sections(conn=incident_conn, sanitize=sanitize, current_database=current_database)
        finally:
            incident_conn.close()
    except Exception as exc:
        v5 = {"summary": {"status": "assembly_failed", "error_type": type(exc).__name__},
              "incidents": [], "comparisons": [], "coverage": {"assembly_error": True}}
    report.update(v5)
    report.setdefault("coverage", {})["legacy_query_errors"] = _QUERY_ERRORS.get()
    from services.failure_report_v5 import enforce_size
    return enforce_size(report)


def failure_report_json(*, db_path: str = DB_FILE) -> str:
    return json.dumps(build_failure_report(db_path=db_path), ensure_ascii=False, indent=2) + "\n"
