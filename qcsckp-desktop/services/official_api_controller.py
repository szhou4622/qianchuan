"""Compatibility controller for the unchanged v0.1.46 service bridge.

The official API runtime deliberately does not import ``services.run_services``.
That module owns Playwright/Chrome and is retained only for Git rollback while the
official backend is being accepted.
"""

from __future__ import annotations

import os
from typing import Any

from config import LOGS_DIR
from services.official_api_catalog import (
    official_api_catalog_status,
    official_api_session_status,
    start_official_api_catalog_scheduler,
    start_official_api_catalog_sync,
    stop_official_api_catalog_scheduler,
)
from services.official_api_collection import (
    start_official_api_collection_background_thread,
    stop_official_api_collection_background_thread,
)


class OfficialApiController:
    """Expose the legacy UI controller surface without starting a browser."""

    def __init__(self) -> None:
        self._running = False

    @staticmethod
    def _pick_latest_app_log() -> str:
        if not os.path.isdir(LOGS_DIR):
            return ""
        candidates: list[tuple[float, str]] = []
        for name in os.listdir(LOGS_DIR):
            if name == "app" or name.startswith("app."):
                path = os.path.join(LOGS_DIR, name)
                if os.path.isfile(path):
                    candidates.append((os.path.getmtime(path), path))
        return max(candidates, default=(0.0, ""))[1]

    def status(self) -> dict[str, Any]:
        session = official_api_session_status()
        catalog = official_api_catalog_status()
        available = bool(session.get("available"))
        return {
            "success": True,
            "running": bool(self._running and available),
            "phase": "running" if self._running and available else "stopped",
            "message": (
                "千川官方 API 后台调度中"
                if self._running and available
                else str(session.get("message") or "千川官方 API 尚未配置")
            ),
            "target": None,
            "lastFetchTime": 0,
            "interval": 300,
            "fetchProgress": None,
            "assistProgress": None,
            "backend": "official_api",
            "catalog": catalog,
            "browser": {
                "available": False,
                "name": "不使用浏览器",
                "path": "",
                "phase": "官方 API 模式",
                "is_chrome": False,
            },
            "qianchuanLogin": {
                "status": str(session.get("message") or "官方 API 尚未授权"),
                "cookie_saved": False,
                "cookie_updated_at": "",
                "encrypted": True,
                "owner_username": "",
            },
        }

    def start(self) -> dict[str, Any]:
        if not official_api_session_status().get("available"):
            return {**self.status(), "success": False}
        start_official_api_catalog_scheduler()
        start_official_api_collection_background_thread()
        self._running = True
        return self.status()

    def stop(self) -> dict[str, Any]:
        stop_official_api_catalog_scheduler()
        stop_official_api_collection_background_thread()
        self._running = False
        return self.status()

    def stop_and_wait(self, _timeout_seconds: float = 30.0) -> dict[str, Any]:
        return self.stop()

    def start_catalog_sync(self, account_uid: Any = "") -> dict[str, Any]:
        return start_official_api_catalog_sync(account_uid)

    def catalog_sync_status(self) -> dict[str, Any]:
        return official_api_catalog_status()

    def start_from_saved_session(self) -> dict[str, Any]:
        return self.start()

    def start_target_discovery(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "success": False,
            "backend": "official_api",
            "code": "oauth_ui_pending",
            "message": "官方 API 模式不打开 Chrome；OAuth 配置页面尚未开发",
        }

    def target_discovery_status(self) -> dict[str, Any]:
        return {
            "success": True,
            "running": False,
            "backend": "official_api",
            "message": "官方 API 模式不使用浏览器识别账户",
        }

    def set_cloud_backup_credentials(self, _username: str, _password: str) -> None:
        return None

    def setInterval(self, interval: int) -> dict[str, Any]:
        return {
            "success": False,
            "interval": int(interval or 300),
            "message": "官方 API 频控由调度器管理，旧采集间隔设置不再生效",
        }

    def setFeishuBitableConfig(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "success": False,
            "message": "旧飞书多维表采集配置不属于官方 API 运行路径",
        }

    def read_logs(self, limit: int = 300) -> dict[str, Any]:
        try:
            path = self._pick_latest_app_log()
            if not path:
                return {"success": True, "lines": []}
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return {"success": True, "lines": handle.readlines()[-max(1, int(limit)): ]}
        except Exception as exc:
            return {"success": False, "lines": [], "message": str(exc)}

    def clear_logs(self) -> dict[str, Any]:
        try:
            path = self._pick_latest_app_log()
            if path:
                with open(path, "w", encoding="utf-8"):
                    pass
            return {"success": True}
        except Exception as exc:
            return {"success": False, "message": str(exc)}


_CONTROLLER: OfficialApiController | None = None


def get_official_api_controller() -> OfficialApiController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = OfficialApiController()
    return _CONTROLLER
