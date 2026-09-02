"""Build a local, shareable failure report without uploading business data."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from config import DB_FILE
from release_identity import IDENTITY


_SECRET_KEYS = {
    "access_token", "refresh_token", "authorization", "access-token",
    "app_secret", "secret", "device_session", "device_credential",
    "activation_code", "cookie", "cookies", "encrypt_key",
    "verification_token", "poll_secret",
}
_ID_KEYS = {
    "advertiser_id", "aavid", "aadvid", "ad_id", "material_id",
    "material_ids", "task_id", "control_task_id", "regulate_task_id",
    "anchor_id", "aweme_id", "aweme_uid", "account_uid", "target_uid",
    "receive_id", "open_id", "user_id", "chat_id", "code_id", "task_uid",
    "run_uid",
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


def _digest(value: Any) -> str:
    text = str(value or "")
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _safe_text(value: Any) -> str:
    text = str(value or "")[:4000]
    text = _URL.sub("<url>", text)
    text = _WINDOWS_PATH.sub("<local-path>", text)
    return _LONG_ID.sub(lambda match: "<id:" + _digest(match.group())[7:] + ">", text)


def _diagnostic_text(value: Any) -> dict[str, Any]:
    text = str(value or "")
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{2,80}", text):
        lowered = token.lower()
        if "/" in token or "." in token or lowered.startswith("http"):
            continue
        if any(secret in lowered for secret in ("secret", "password", "token", "cookie", "credential", "authorization")):
            continue
        if len(token) > 40 and re.fullmatch(r"[A-Za-z0-9_-]+", token):
            continue
        if token not in tokens:
            tokens.append(token)
    return {
        "redacted": True,
        "length": len(text),
        "fingerprint": _digest(text),
        "technical_tokens": tokens[:40],
    }


def sanitize(value: Any, *, key: str = "") -> Any:
    normalized = str(key or "").strip().lower()
    if normalized in _SECRET_KEYS or any(token in normalized for token in ("password", "credential", "secret", "token")):
        return "<redacted>"
    if normalized in _ID_KEYS or normalized.endswith("_ids"):
        if isinstance(value, (list, tuple, set)):
            return [_digest(item) for item in value]
        return _digest(value) if value not in (None, "") else ""
    if normalized in _NAME_KEYS or normalized.endswith("_name"):
        text = str(value or "")
        return {"redacted": True, "length": len(text), "fingerprint": _digest(text)}
    if normalized in _MESSAGE_KEYS or normalized.endswith("_message") or normalized.endswith("_error"):
        return _diagnostic_text(value)
    if isinstance(value, Mapping):
        result = {str(k): sanitize(v, key=str(k)) for k, v in value.items()}
        filter_field = str(value.get("field") or "").strip().lower()
        if filter_field in _ID_KEYS and "values" in result:
            original = value.get("values") or []
            result["values"] = [_digest(item) for item in original]
        return result
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item, key=normalized) for item in value]
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
    except sqlite3.Error:
        return []


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def build_failure_report(*, db_path: str = DB_FILE) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "qcsckp-failure-report-v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "application": {
            "version": IDENTITY.get("version"),
            "channel": IDENTITY.get("channel"),
            "build_revision": IDENTITY.get("build_revision"),
            "source_commit": str(IDENTITY.get("source_commit") or "")[:40],
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
        },
        "privacy": {
            "uploaded": False,
            "contains_credentials": False,
            "identifiers_hashed": True,
            "names_redacted": True,
        },
        "database": {"available": False, "quick_check": "not_run"},
        "api_recent": [],
        "retarget_failures": [],
        "stop_failures": [],
        "regulation_failures": [],
        "reconciliation": [],
        "feishu_outbox": [],
        "target_errors": [],
        "diagnostic_events": [],
    }
    path = Path(db_path)
    if not path.is_file():
        return report
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        report["database"] = {"available": False, "quick_check": "open_failed", "error_type": type(exc).__name__}
        return report
    try:
        report["database"] = {
            "available": True,
            "quick_check": str(conn.execute("PRAGMA quick_check").fetchone()[0]),
        }
        if _table_exists(conn, "qianchuan_api_audit"):
            audits = _rows(conn, "SELECT endpoint,method,aavid,ad_id,task_id,request_id,error_code,status,request_summary_json,response_summary_json,created_at FROM qianchuan_api_audit ORDER BY id DESC LIMIT 200")
            report["api_recent"] = [sanitize({
                **{k: row.get(k) for k in ("endpoint", "method", "aavid", "ad_id", "task_id", "request_id", "error_code", "status", "created_at")},
                "request": _parse_json(row.get("request_summary_json")),
                "response": _parse_json(row.get("response_summary_json")),
            }) for row in reversed(audits)]
        if _table_exists(conn, "local_retarget_task"):
            rows = _rows(conn, "SELECT task_uid,action_type,status,result_message,result_detail,regulate_task_id,created_at,finished_at FROM local_retarget_task WHERE status IN ('failed','unknown_requires_review','expired','cancelled') ORDER BY id DESC LIMIT 100")
            normalized_tasks = [sanitize({
                **{k: row.get(k) for k in ("task_uid", "action_type", "status", "result_message", "regulate_task_id", "created_at", "finished_at")},
                "trace": _trace_evidence(row.get("result_detail")),
            }) for row in rows]
            report["retarget_failures"] = [
                item for item in normalized_tasks if item.get("action_type") == "retarget"
            ][:50]
            report["stop_failures"] = [
                item for item in normalized_tasks if item.get("action_type") == "stop"
            ][:50]
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
                    + " FROM feishu_outbox WHERE status IN ('queued','sending','failed') "
                    "ORDER BY rowid DESC LIMIT 100",
                )
                report["feishu_outbox"] = [sanitize(row) for row in rows]
        if _table_exists(conn, "promotion_target"):
            rows = _rows(conn, "SELECT target_uid,aadvid,ad_id,promotion_scene,plan_system,platform_status,last_status,last_error,last_sync_at,updated_at FROM promotion_target WHERE enabled=1 AND (last_error<>'' OR last_status NOT IN ('ok','healthy','normal_monitoring')) ORDER BY updated_at DESC LIMIT 100")
            report["target_errors"] = [sanitize(row) for row in rows]
    finally:
        conn.close()
    try:
        from channel_runtime import layout

        shared_path = layout().shared / "execution.sqlite3"
        if (
            Path(db_path).resolve() == Path(DB_FILE).resolve()
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
        if events_path.is_file():
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
    return report


def failure_report_json(*, db_path: str = DB_FILE) -> str:
    return json.dumps(build_failure_report(db_path=db_path), ensure_ascii=False, indent=2) + "\n"
