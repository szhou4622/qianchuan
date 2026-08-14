"""Persistent non-secret runtime choices for the Qianchuan Open API backend."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from config import QIANCHUAN_RUNTIME_SETTINGS_FILE


def load_runtime_settings(path: Optional[str] = None) -> dict[str, Any]:
    target = Path(path or QIANCHUAN_RUNTIME_SETTINGS_FILE)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def persist_official_api_runtime(
    *,
    allow_live_writes: Optional[bool] = None,
    path: Optional[str] = None,
    apply_runtime: bool = True,
) -> dict[str, Any]:
    """Persist backend and execution choice without storing credentials."""
    target = Path(path or QIANCHUAN_RUNTIME_SETTINGS_FILE)
    payload = load_runtime_settings(str(target))
    payload["backend"] = "official_api"
    if allow_live_writes is not None:
        payload["allow_live_api_writes"] = bool(allow_live_writes)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

    if apply_runtime:
        import config

        config.QIANCHUAN_BACKEND = "official_api"
        if allow_live_writes is not None:
            config.ALLOW_LIVE_OFFICIAL_API_WRITES = bool(allow_live_writes)
            from .runtime import apply_live_write_permission

            apply_live_write_permission(bool(allow_live_writes))
    return payload


def enable_execution_for_saved_rules(
    rule_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Enable writes only after an active execution rule is explicitly saved."""
    if not bool(rule_config.get("enabled")):
        return load_runtime_settings()
    strategies = [
        item
        for item in (rule_config.get("strategies") or [])
        if isinstance(item, Mapping)
    ]
    if not strategies:
        return load_runtime_settings()
    return persist_official_api_runtime(allow_live_writes=True)
