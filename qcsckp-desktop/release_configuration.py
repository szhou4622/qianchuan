"""The public Windows build has one configuration policy for every user.

Only directory overrides are used for isolated verification. Developer test
switches and injected credentials must never change an installed release.
Personal choices still come from the normal persisted, owner-scoped settings.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any

from release_identity import IDENTITY


POLICY_VERSION = 3
DEFAULTS = {
    "backend": "official_api",
    "auth_mode": "local",
    "official_api_origin": "https://api.oceanengine.com",
    "collection_interval_seconds": 300,
    "stop_interval_seconds": 300,
    "material_page_size": 100,
    "report_page_size": 200,
    "application_qps": 12,
    "account_qps": 2,
    "execution_permission_source": "owner_settings_and_saved_rules",
    "injected_credentials": False,
    "test_mode": False,
    "shadow_mode": False,
    "windows_dotnet_runtime": "netfx",
    "windows_gui_backend": "edgechromium",
}
DEVELOPMENT_ENVIRONMENT_KEYS = (
    "QCSCKP_TEST_MODE", "QCSCKP_TEST_AAVID", "QCSCKP_TEST_MATERIAL_ID",
    "QCSCKP_ALLOW_LIVE_RETARGET", "QCSCKP_LOCAL_TEST_SECRETS_FILE",
    "QCSCKP_QIANCHUAN_BACKEND", "QCSCKP_OE_API_BASE_URL",
    "QCSCKP_ALLOW_LIVE_API_WRITES", "QCSCKP_AUTH_MODE", "QCSCKP_API_BASE_URL",
    "QCSCKP_LOCAL_AUTH_USERNAME", "QCSCKP_LOCAL_AUTH_PASSWORD_HASH",
    "QCSCKP_OE_ACCESS_TOKEN", "QCSCKP_OE_REFRESH_TOKEN", "QCSCKP_OE_EXPIRES_AT",
    "QCSCKP_OE_APP_ID", "QCSCKP_OE_APP_SECRET", "QCSCKP_REGULATION_SHADOW_MODE",
    "QCSCKP_RETARGET_TASK_BACKEND", "REGULATION_ASSIST_UPDATED_WITHIN_MINUTES",
    "REGULATION_STRATEGY_PARALLEL", "REGULATION_RULE_INTERVAL_SEC",
    "QCSCKP_SESSION_OWNER", "QCSCKP_AUTO_START_SERVICE", "QCSCKP_AUTO_START_INTERVAL",
    "QCSCKP_LICENSE_RECHECK_SECONDS", "QCSCKP_FORCE_TARGET_RESELECT",
    "QCSCKP_ALLOW_LEGACY_LOCAL_AUTH", "QCSCKP_AUTH_BASE_URL", "QCSCKP_TEST_DEVICE_ID",
    "QCSCKP_V1A_AUTH_MODE", "QCSCKP_LEGACY_SCAN_ROOTS",
    "QCSCKP_CHROME_PATH", "QCSCKP_V1A_DATA_DIR",
)
DIRECTORY_ENVIRONMENT_KEYS = frozenset({"QCSCKP_HOME", "QCSCKP_DATA_DIR"})
WINDOWS_RUNTIME_ENVIRONMENT = {"PYTHONNET_RUNTIME": "netfx", "PYWEBVIEW_GUI": "edgechromium"}
_ignored_keys: set[str] = set()


def packaged_policy_active() -> bool:
    return bool(getattr(sys, "frozen", False))


def _development_override(name: str) -> bool:
    upper = str(name).upper()
    return (upper in DEVELOPMENT_ENVIRONMENT_KEYS
            or (upper.startswith("QCSCKP_") and upper not in DIRECTORY_ENVIRONMENT_KEYS)
            or (sys.platform == "win32" and (upper.startswith("PYTHONNET_") or upper == "PYWEBVIEW_GUI")))


def enforce_packaged_configuration(*, for_build: bool = False) -> tuple[str, ...]:
    """Remove process-local development overrides, never OS/user settings."""
    if packaged_policy_active() or for_build:
        for key in list(os.environ):
            if _development_override(key):
                if os.environ[key] != WINDOWS_RUNTIME_ENVIRONMENT.get(key):
                    _ignored_keys.add(key)
                os.environ.pop(key, None)
        if sys.platform == "win32":
            os.environ.update(WINDOWS_RUNTIME_ENVIRONMENT)
    return tuple(sorted(_ignored_keys))


def environment_value(name: str, default: Any = None) -> Any:
    if packaged_policy_active():
        if sys.platform == "win32" and name in WINDOWS_RUNTIME_ENVIRONMENT:
            return WINDOWS_RUNTIME_ENVIRONMENT[name]
        if _development_override(name):
            return default
    return os.environ.get(name, default)


def public_default_configuration(identity: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build-time, public-only fingerprint. Runtime never reads this JSON."""
    identity = IDENTITY if identity is None else identity
    contract = {
        "policy_version": POLICY_VERSION,
        "version": identity.get("version"),
        "channel": identity.get("channel"),
        "build_revision": identity.get("build_revision"),
        "source_commit": identity.get("source_commit"),
        "defaults": dict(DEFAULTS),
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    return {
        **contract,
        "software_contract_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def public_runtime_contract() -> dict[str, Any]:
    return {
        **public_default_configuration(),
        "packaged_policy_enforced": packaged_policy_active(),
        # Names only: values may include credentials and must never be exported.
        "ignored_development_override_names": list(sorted(_ignored_keys)),
    }
