"""Stable identities for Qianchuan operation-log rows.

One official log batch may reuse the same ``log_id`` for several distinct
control-task changes.  Identity therefore includes the stable row content and
the control-task ID extracted from that content.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping


_TASK_ID_PATTERNS = (
    re.compile(r"(?:任务|调控任务)\s*ID\s*[:：]\s*(\d+)", re.IGNORECASE),
    re.compile(r"素材追投\s*[,，]?\s*ID\s*[:：]\s*(\d+)", re.IGNORECASE),
)


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(unicodedata.normalize("NFKC", str(value)).strip().split())


def _direct(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    raw = row.get("raw")
    if isinstance(raw, Mapping):
        for key in keys:
            if raw.get(key) not in (None, ""):
                return raw.get(key)
    return ""


def operation_log_content_items(row: Mapping[str, Any]) -> tuple[str, ...]:
    value = _direct(row, "content_log", "contentLog", "description", "optContent")
    if isinstance(value, (list, tuple)):
        return tuple(item for item in (_text(part) for part in value) if item)
    if isinstance(value, Mapping):
        stable = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return (_text(stable),) if stable else ()
    text = _text(value)
    return (text,) if text else ()


def operation_log_control_task_id(row: Mapping[str, Any]) -> str:
    direct = _text(
        _direct(
            row,
            "control_task_id",
            "controlTaskId",
            "regulate_task_id",
            "assist_task_id",
            "assistTaskId",
            "task_id",
            "taskId",
        )
    )
    if direct.isdigit():
        return direct
    content = " ; ".join(operation_log_content_items(row))
    for pattern in _TASK_ID_PATTERNS:
        match = pattern.search(content)
        if match:
            return match.group(1)
    return ""


def operation_log_row_identity(row: Mapping[str, Any]) -> str:
    """Return a stable row identity while preserving the official log ID."""
    log_id = _text(_direct(row, "log_id", "logId", "id"))
    stable = {
        "log_id": log_id,
        "occurred_at": _text(
            _direct(
                row,
                "occurred_at",
                "create_time",
                "createTime",
                "operation_time",
                "operate_time",
                "operateTime",
            )
        ),
        "object_id": _text(_direct(row, "object_id", "objectId")),
        "control_task_id": operation_log_control_task_id(row),
        "content_title": _text(
            _direct(row, "content_title", "contentTitle", "title")
        ),
        "content_log": operation_log_content_items(row),
    }
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{log_id or 'missing'}:{digest}"
