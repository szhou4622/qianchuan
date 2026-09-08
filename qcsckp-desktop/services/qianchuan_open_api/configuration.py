"""Owner-bound Open API configuration, authorization and non-secret events."""
from __future__ import annotations

from typing import Any, Mapping
from utils.log import logger
from .runtime import get_official_api_service
from .runtime_settings import persist_official_api_runtime
from .errors import ApiTokenError
from .token_provider import (
    AuthorizationContextChanged, _authorization_context, _assert_current_owner,
    api_configuration_status, authorization_identity_is_current,
    begin_api_authorization, clear_api_configuration, exchange_authorization_code,
    get_authorization_identity, open_oauth_authorization_browser,
    poll_api_authorization, save_api_credentials,
)


def _notify_authorization_changed(previous: Mapping[str, str], current: Mapping[str, str],
                                  *, event: str) -> str:
    """Called only after a committed change, outside the credential file lock."""
    try:
        get_official_api_service().clear_business_account_cache()
        from services.official_api_collection import handle_authorization_change
        handle_authorization_change(dict(previous), dict(current), event=event)
        return ""
    except Exception as exc:
        logger.warning("授权已保存，状态通知暂未完成 type=%s", type(exc).__name__)
        return "授权已保存，采集状态刷新暂未完成，请稍后重新验证"


def _announce(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("authorization_changed"):
        warning = _notify_authorization_changed(
            result["previous_authorization_identity"], result["authorization_identity"],
            event=str(result.get("authorization_event") or "authorization_completed"),
        )
        if warning:
            result["authorization_notification_warning"] = warning
    return result


def get_configuration() -> dict[str, Any]:
    try:
        status = api_configuration_status()
        last_error = "检测到其他电脑留下的加密配置，请重新输入 App ID 和 App Secret" if status.get("requires_reentry") else ""
        if status["configured"] and not status["authorized"] and not status["authorization_pending"]:
            try:
                get_official_api_service().client.token_provider.get_token()
                status = api_configuration_status()
            except Exception as exc:
                last_error = str(exc)
        return {"success": True, **status, "last_error": last_error}
    except Exception as exc:
        return {"success": False, "configured": False, "authorized": False, "message": str(exc)}


def save_configuration(app_id: Any, app_secret: Any) -> dict[str, Any]:
    try:
        status = _announce(save_api_credentials(app_id, app_secret))
        return {"success": True, **status, "message": "App ID 和 App Secret 已用 Windows DPAPI 加密保存"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def start_authorization(app_id: Any = None, app_secret: Any = None) -> dict[str, Any]:
    try:
        owner, path = _authorization_context()
        saved: dict[str, Any] = {}
        if app_id is not None or app_secret is not None:
            saved = _announce(save_api_credentials(app_id, app_secret, path, owner_username=owner))
        _assert_current_owner(owner)
        persist_official_api_runtime(owner_username=owner)
        auth = begin_api_authorization(path, owner_username=owner)
        _assert_current_owner(owner)
        if not authorization_identity_is_current(auth["authorization_identity"], path):
            raise AuthorizationContextChanged()
        opened = open_oauth_authorization_browser(str(auth["url"]), str(auth["state"]))
        return {"success": True, **saved, "opened": opened, "authorization_pending": True,
                "authorization_identity": auth["authorization_identity"],
                "message": "已打开千川官方授权页；同意授权后工具会自动完成连接"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def save_and_start_authorization(app_id: Any, app_secret: Any) -> dict[str, Any]:
    return start_authorization(app_id, app_secret)


def finish_authorization(authorization_callback: Any) -> dict[str, Any]:
    try:
        owner, path = _authorization_context()
        previous = get_authorization_identity(path, owner_username=owner)
        if str(authorization_callback or "").strip():
            exchange_authorization_code(authorization_callback, path, owner_username=owner)
        else:
            polled = poll_api_authorization(path, owner_username=owner)
            if not polled.get("completed"):
                return {"success": True, **api_configuration_status(path, owner_username=owner), "completed": False,
                        "message": "等待在官方页面同意授权"}
        _assert_current_owner(owner)
        status = api_configuration_status(path, owner_username=owner)
        identity = status["authorization_identity"]
        event = _announce({**status, "previous_authorization_identity": previous,
                           "authorization_changed": previous != identity,
                           "authorization_event": "authorization_completed"})
        persist_official_api_runtime(owner_username=owner)
        service = get_official_api_service()
        if not event["authorization_changed"]:
            service.clear_business_account_cache()
        try:
            if not authorization_identity_is_current(identity, path):
                raise AuthorizationContextChanged()
            accounts, evidence = service.list_business_accounts(force_refresh=True)
            if not authorization_identity_is_current(identity, path):
                raise AuthorizationContextChanged()
        except AuthorizationContextChanged:
            raise
        except Exception as exc:
            return {"success": True, **event, "completed": True, "account_check_success": False,
                    "message": f"官方 API 授权已保存；账户权限检查暂未通过：{exc}"}
        return {"success": True, **event, "completed": True, "account_check_success": True,
                "authorized_account_count": len(accounts), "complete": bool(evidence.get("complete")),
                "message": f"官方 API 授权成功，已识别 {len(accounts)} 个千川投放账户"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def disconnect_configuration() -> dict[str, Any]:
    try:
        owner, path = _authorization_context()
        try:
            previous = get_authorization_identity(path, owner_username=owner)
        except AuthorizationContextChanged:
            raise
        except ApiTokenError:
            previous = {"owner_username": owner, "app_id": "", "auth_generation": "unreadable"}
        clear_api_configuration(path, owner_username=owner)
        current = {"owner_username": owner, "app_id": "", "auth_generation": "unconfigured"}
        event = _announce({"authorization_identity": current, "previous_authorization_identity": previous,
                           "authorization_changed": previous != current, "authorization_event": "disconnected"})
        return {"success": True, **event, "configured": False, "authorized": False,
                "message": "本机千川 API 配置已清除"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}
