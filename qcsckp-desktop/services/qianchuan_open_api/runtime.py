"""官方 API 单例运行时。"""

from __future__ import annotations

import threading
from typing import Optional

from .audit import OfficialApiAuditStore
from .client import QianchuanOpenApiClient
from .service import QianchuanOfficialApiService


_LOCK = threading.Lock()
_SERVICE: Optional[QianchuanOfficialApiService] = None


def get_official_api_service() -> QianchuanOfficialApiService:
    global _SERVICE
    with _LOCK:
        import config as runtime_config
        from .runtime_settings import load_runtime_settings

        saved = load_runtime_settings()
        if "allow_live_api_writes" in saved:
            live_writes = bool(saved.get("allow_live_api_writes"))
        else:
            live_writes = bool(runtime_config.ALLOW_LIVE_OFFICIAL_API_WRITES)
        # Resolve the active owner on every access. A tool-account switch must
        # never inherit the previous owner's permission, while a restart must
        # restore the choice that this owner explicitly saved with a strategy.
        runtime_config.ALLOW_LIVE_OFFICIAL_API_WRITES = live_writes
        if _SERVICE is None:
            audit = OfficialApiAuditStore()
            client = QianchuanOpenApiClient(audit_sink=audit.record)
            _SERVICE = QianchuanOfficialApiService(
                client,
                allow_writes=live_writes,
            )
        else:
            _SERVICE.allow_writes = live_writes
        return _SERVICE


def replace_official_api_service_for_tests(service: Optional[QianchuanOfficialApiService]) -> None:
    global _SERVICE
    with _LOCK:
        _SERVICE = service


def apply_live_write_permission(enabled: bool) -> None:
    """Apply a persisted permission to an already-running singleton."""
    with _LOCK:
        if _SERVICE is not None:
            _SERVICE.allow_writes = bool(enabled)
