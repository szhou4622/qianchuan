"""千川官方 API 的本机配置、授权与脱敏状态。"""

from __future__ import annotations

import webbrowser
from typing import Any

from .runtime import get_official_api_service
from .token_provider import (
    api_configuration_status,
    begin_api_authorization,
    clear_api_configuration,
    exchange_authorization_code,
    save_api_credentials,
)


def get_configuration() -> dict[str, Any]:
    try:
        status = api_configuration_status()
        last_error = ""
        if status["configured"] and not status["authorized"]:
            try:
                # 过期但仍有 refresh_token 时在同一刷新锁内自动续期。
                get_official_api_service().client.token_provider.get_token()
                status = api_configuration_status()
            except Exception as exc:
                last_error = str(exc)
        return {"success": True, **status, "last_error": last_error}
    except Exception as exc:
        return {
            "success": False,
            "configured": False,
            "authorized": False,
            "message": str(exc),
        }


def save_configuration(app_id: Any, app_secret: Any) -> dict[str, Any]:
    try:
        status = save_api_credentials(app_id, app_secret)
        return {
            "success": True,
            **status,
            "message": "App ID 和 App Secret 已用 Windows DPAPI 加密保存",
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def start_authorization() -> dict[str, Any]:
    try:
        auth = begin_api_authorization()
        opened = bool(webbrowser.open(str(auth["url"])))
        return {
            "success": True,
            "opened": opened,
            "authorization_pending": True,
            "message": (
                "已打开千川官方授权页；授权后请将回调页中的 auth_code 粘贴回工具"
            ),
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def finish_authorization(authorization_callback: Any) -> dict[str, Any]:
    try:
        exchange_authorization_code(authorization_callback)
        # 这一次真实读请求同时验证 token 与应用的账户权限。
        try:
            accounts, evidence = get_official_api_service().list_business_accounts()
        except Exception as exc:
            return {
                "success": True,
                **api_configuration_status(),
                "account_check_success": False,
                "message": f"官方 API 授权已保存；账户权限检查暂未通过：{exc}",
            }
        return {
            "success": True,
            **api_configuration_status(),
            "account_check_success": True,
            "authorized_account_count": len(accounts),
            "complete": bool(evidence.get("complete")),
            "message": f"官方 API 授权成功，已识别 {len(accounts)} 个千川投放账户",
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def disconnect_configuration() -> dict[str, Any]:
    try:
        clear_api_configuration()
        return {
            "success": True,
            "configured": False,
            "authorized": False,
            "message": "本机千川 API 配置已清除",
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}
