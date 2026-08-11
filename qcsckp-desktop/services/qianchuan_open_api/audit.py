"""官方 API 请求审计与结果对账状态。"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from utils.sqlite_store import SQLiteStore, init_sqlite_schema
from .normalizers import first, text_id


def _owner_username() -> str:
    try:
        from services.cloud_retarget_client import load_device_session

        return str((load_device_session() or {}).get("username") or "").strip().casefold()
    except Exception:
        return ""


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), default=str)


class OfficialApiAuditStore:
    def __init__(self, db: Optional[SQLiteStore] = None) -> None:
        self.db = db or SQLiteStore()
        init_sqlite_schema(database=self.db.config.get("database"))

    def record(self, event: Mapping[str, Any]) -> None:
        request = event.get("request") if isinstance(event.get("request"), Mapping) else {}
        body = request.get("body") if isinstance(request.get("body"), Mapping) else {}
        query = request.get("query") if isinstance(request.get("query"), Mapping) else {}
        status = str(event.get("status") or "requested")
        error_code = str(event.get("error_code") or "")
        explicit_permission = str(event.get("permission_status") or "").strip().lower()
        permission_status = (
            explicit_permission
            if explicit_permission in {"denied", "granted", "unknown"}
            else (
                "denied"
                if error_code in {"401", "403"}
                else ("granted" if status == "success" else "unknown")
            )
        )
        self.db.insert_or_update(
            "qianchuan_api_audit",
            {
                "request_uid": str(event.get("request_uid") or ""),
                "account_username": _owner_username(),
                "source": "qianchuan_open_api",
                "endpoint": str(event.get("endpoint") or ""),
                "method": str(event.get("method") or "GET").upper(),
                "aavid": text_id(event.get("aavid") or first(body, "advertiser_id") or first(query, "advertiser_id")),
                "ad_id": text_id(first(body, "ad_id") or first(query, "ad_id")),
                "task_id": text_id(first(body, "task_id") or first(body, "task_ids")),
                "request_id": str(event.get("request_id") or ""),
                "error_code": error_code,
                "permission_status": permission_status,
                "reconciliation_status": "required" if status == "unknown" else "not_required",
                "status": status,
                "request_summary_json": _json(request),
                "response_summary_json": _json(event.get("response")),
            },
            unique_fields=["request_uid"],
        )

    def mark_reconciled(
        self,
        request_uid: str,
        *,
        status: str,
        task_id: Any = "",
        response: Any = None,
    ) -> None:
        self.db.update(
            "qianchuan_api_audit",
            {
                "reconciliation_status": str(status or "unknown"),
                "task_id": text_id(task_id),
                "response_summary_json": _json(response),
            },
            where={"request_uid": str(request_uid or "")},
        )
