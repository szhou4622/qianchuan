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
        if _SERVICE is None:
            audit = OfficialApiAuditStore()
            client = QianchuanOpenApiClient(audit_sink=audit.record)
            _SERVICE = QianchuanOfficialApiService(client)
        return _SERVICE


def replace_official_api_service_for_tests(service: Optional[QianchuanOfficialApiService]) -> None:
    global _SERVICE
    with _LOCK:
        _SERVICE = service
