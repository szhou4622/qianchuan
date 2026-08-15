"""千川官方 API 的本机配置、授权与脱敏状态。"""

from __future__ import annotations

import webbrowser
from typing import Any

from .runtime import get_official_api_service
from .runtime_settings import persist_official_api_runtime
from .token_provider import (
    api_configuration_status,
    begin_api_authorization,
    clear_api_configuration,
    exchange_authorization_code,
    poll_api_authorization,
    save_api_credentials,
)


def get_configuration() -> dict[str, Any]:
    try:
        status = api_configuration_status()
        last_error = ""
        if status.get("requires_reentry"):
            last_error = "检测到其他电脑留下的加密配置，请重新输入 App ID 和 App Secret"
        if (
            status["configured"]
            and not status["authorized"]
            and not status["authorization_pending"]
        ):
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


def start_authorization(app_id: Any = None, app_secret: Any = None) -> dict[str, Any]:
    try:
        if app_id is not None or app_secret is not None:
            save_api_credentials(app_id, app_secret)
        persist_official_api_runtime()
        auth = begin_api_authorization()
        opened = bool(webbrowser.open(str(auth["url"])))
        return {
            "success": True,
            "opened": opened,
            "authorization_pending": True,
            "message": (
                "已打开千川官方授权页；同意授权后，应用后台登记的公网回调服务"
                "会把结果转交给本机工具"
            ),
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def save_and_start_authorization(app_id: Any, app_secret: Any) -> dict[str, Any]:
    """用户只点击一次：保存凭据、创建会话并打开官方授权页。"""
    return start_authorization(app_id, app_secret)


def finish_authorization(authorization_callback: Any) -> dict[str, Any]:
    try:
        if str(authorization_callback or "").strip():
            # 仅保留旧版桥接兼容；普通用户界面不再要求复制回调地址。
            exchange_authorization_code(authorization_callback)
        else:
            polled = poll_api_authorization()
            if not polled.get("completed"):
                return {
                    "success": True,
                    **api_configuration_status(),
                    "completed": False,
                    "message": "等待在官方页面同意授权",
                }
        persist_official_api_runtime()
        # 这一次真实读请求同时验证 token 与应用的账户权限。
        try:
            accounts, evidence = get_official_api_service().list_business_accounts()
        except Exception as exc:
            return {
                "success": True,
                **api_configuration_status(),
                "completed": True,
                "account_check_success": False,
                "message": f"官方 API 授权已保存；账户权限检查暂未通过：{exc}",
            }
        return {
            "success": True,
            **api_configuration_status(),
            "completed": True,
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
