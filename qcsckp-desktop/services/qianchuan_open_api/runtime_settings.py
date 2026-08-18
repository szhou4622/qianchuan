"""Persistent non-secret runtime choices for the Qianchuan Open API backend."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from config import QIANCHUAN_RUNTIME_SETTINGS_FILE


def _current_owner() -> str:
    try:
        from services.qianchuan_session import current_session_owner

        owner = str(current_session_owner() or "").strip().casefold()
    except Exception:
        owner = ""
    return owner or "local_default"


def _read_payload(target: Path) -> dict[str, Any]:
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def load_runtime_settings(path: Optional[str] = None) -> dict[str, Any]:
    target = Path(path or QIANCHUAN_RUNTIME_SETTINGS_FILE)
    payload = _read_payload(target)
    # Explicit paths are used by tests, diagnostics and rollback tools and keep
    # the legacy flat-file contract.
    if path is not None:
        return payload
    profiles = payload.get("profiles")
    if isinstance(profiles, dict):
        profile = profiles.get(_current_owner())
        if isinstance(profile, dict):
            return dict(profile)
    # The pre-isolation file may contain a flat runtime choice. It is returned
    # only when no owner has claimed it yet; persist_official_api_runtime()
    # records the first owner and prevents a later login inheriting live writes.
    legacy_owner = str(payload.get("legacy_owner") or "").strip().casefold()
    if not legacy_owner or legacy_owner == _current_owner():
        return {
            key: payload[key]
            for key in ("backend", "allow_live_api_writes")
            if key in payload
        }
    return {"backend": "official_api", "allow_live_api_writes": False}


def persist_official_api_runtime(
    *,
    allow_live_writes: Optional[bool] = None,
    path: Optional[str] = None,
    apply_runtime: bool = True,
) -> dict[str, Any]:
    """Persist backend and execution choice without storing credentials."""
    target = Path(path or QIANCHUAN_RUNTIME_SETTINGS_FILE)
    if path is not None:
        payload = load_runtime_settings(str(target))
        payload["backend"] = "official_api"
        if allow_live_writes is not None:
            payload["allow_live_api_writes"] = bool(allow_live_writes)
        result = payload
    else:
        payload = _read_payload(target)
        owner = _current_owner()
        profiles = payload.get("profiles")
        if not isinstance(profiles, dict):
            profiles = {}
        current = profiles.get(owner)
        if not isinstance(current, dict):
            legacy_owner = str(payload.get("legacy_owner") or "").strip().casefold()
            if not legacy_owner:
                current = {
                    key: payload[key]
                    for key in ("backend", "allow_live_api_writes")
                    if key in payload
                }
                payload["legacy_owner"] = owner
            else:
                current = {}
        current = dict(current)
        current["backend"] = "official_api"
        if allow_live_writes is not None:
            current["allow_live_api_writes"] = bool(allow_live_writes)
        profiles[owner] = current
        payload["profiles"] = profiles
        result = current
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
    return result


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
