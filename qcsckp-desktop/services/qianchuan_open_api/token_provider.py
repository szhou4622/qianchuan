"""可注入的千川令牌提供器与本机 OAuth 配置存储。"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from config import (
    QIANCHUAN_API_TOKEN_FILE,
    QIANCHUAN_OAUTH_CALLBACK_URL,
    QIANCHUAN_OAUTH_RELAY_BASE_URL,
    QIANCHUAN_OFFICIAL_API_BASE_URL,
)
from .errors import ApiTokenError, OfficialApiNotConfigured


QIANCHUAN_OAUTH_PAGE = "https://qianchuan.jinritemai.com/openapi/qc/audit/oauth.html"


@dataclass(frozen=True)
class AccessTokenBundle:
    access_token: str
    refresh_token: str = ""
    app_id: str = ""
    app_secret: str = ""
    expires_at: float = 0.0
    oauth_state: str = ""
    oauth_started_at: float = 0.0
    oauth_poll_secret: str = ""

    def usable(self, skew_seconds: int = 120) -> bool:
        if not self.access_token:
            return False
        return not self.expires_at or self.expires_at > time.time() + skew_seconds


class TokenProvider(Protocol):
    def get_token(self, *, force_refresh: bool = False) -> AccessTokenBundle: ...


class InjectedTokenProvider:
    """测试和联调注入；不会持久化令牌。"""

    def __init__(
        self,
        bundle: AccessTokenBundle,
        refresh_callback: Optional[Callable[[AccessTokenBundle], AccessTokenBundle]] = None,
    ) -> None:
        self._bundle = bundle
        self._refresh_callback = refresh_callback
        self._lock = threading.Lock()

    def get_token(self, *, force_refresh: bool = False) -> AccessTokenBundle:
        if self._bundle.usable() and not force_refresh:
            return self._bundle
        with self._lock:
            if self._bundle.usable() and not force_refresh:
                return self._bundle
            if not self._refresh_callback:
                if self._bundle.access_token and not self._bundle.expires_at:
                    return self._bundle
                raise ApiTokenError("千川 Open API access_token 已过期，且未配置刷新器")
            self._bundle = self._refresh_callback(self._bundle)
            return self._bundle


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OfficialApiNotConfigured("令牌持久化仅支持 Windows DPAPI")
    import win32crypt  # type: ignore

    return win32crypt.CryptProtectData(data, "QCSCKP Open API", None, None, None, 0)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OfficialApiNotConfigured("令牌持久化仅支持 Windows DPAPI")
    import win32crypt  # type: ignore

    return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]


def save_token_bundle(bundle: AccessTokenBundle, path: str = QIANCHUAN_API_TOKEN_FILE) -> None:
    """为后续 OAuth 界面预留；磁盘上只保存 DPAPI 密文。"""
    payload = json.dumps(asdict(bundle), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    wrapper = {
        "format": "qcsckp-oceanengine-token-dpapi-v1",
        "ciphertext": base64.b64encode(_protect(payload)).decode("ascii"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(wrapper, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temp, path)


def _load_saved_bundle(path: str = QIANCHUAN_API_TOKEN_FILE) -> Optional[AccessTokenBundle]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            wrapper = json.load(handle)
        plain = _unprotect(base64.b64decode(str(wrapper.get("ciphertext") or "")))
        data = json.loads(plain.decode("utf-8"))
        return AccessTokenBundle(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            app_id=str(data.get("app_id") or ""),
            app_secret=str(data.get("app_secret") or ""),
            expires_at=float(data.get("expires_at") or 0),
            oauth_state=str(data.get("oauth_state") or ""),
            oauth_started_at=float(data.get("oauth_started_at") or 0),
            oauth_poll_secret=str(data.get("oauth_poll_secret") or ""),
        )
    except OfficialApiNotConfigured:
        raise
    except Exception as exc:
        raise ApiTokenError("本机千川 Open API 令牌无法解密，请重新授权") from exc


class DpapiTokenProvider:
    def __init__(self, path: str = QIANCHUAN_API_TOKEN_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _refresh(self, bundle: AccessTokenBundle) -> AccessTokenBundle:
        if not bundle.refresh_token or not bundle.app_id or not bundle.app_secret:
            raise ApiTokenError("千川 Open API 令牌已过期，请重新授权")
        body = urlencode(
            {
                "app_id": bundle.app_id,
                "secret": bundle.app_secret,
                "grant_type": "refresh_token",
                "refresh_token": bundle.refresh_token,
            }
        ).encode("utf-8")
        request = Request(
            QIANCHUAN_OFFICIAL_API_BASE_URL + "/open_api/oauth2/refresh_token/",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ApiTokenError("千川 Open API 令牌刷新失败") from exc
        if str(result.get("code") or "0") not in {"", "0"}:
            raise ApiTokenError(
                str(result.get("message") or "千川 Open API 令牌刷新失败"),
                code=result.get("code"),
                request_id=result.get("request_id"),
            )
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        expires_in = float(data.get("expires_in") or 0)
        refreshed = AccessTokenBundle(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or bundle.refresh_token),
            app_id=bundle.app_id,
            app_secret=bundle.app_secret,
            expires_at=time.time() + expires_in if expires_in else 0,
            oauth_state="",
            oauth_started_at=0,
            oauth_poll_secret="",
        )
        if not refreshed.access_token:
            raise ApiTokenError("千川 Open API 刷新响应缺少 access_token")
        save_token_bundle(refreshed, self.path)
        return refreshed

    def get_token(self, *, force_refresh: bool = False) -> AccessTokenBundle:
        with self._lock:
            bundle = _load_saved_bundle(self.path)
            if not bundle:
                raise OfficialApiNotConfigured(
                    "千川官方 API 尚未配置；请先在千川账户管理页面保存 App ID、App Secret 并完成官方授权"
                )
            if bundle.usable() and not force_refresh:
                return bundle
            return self._refresh(bundle)


class EnvironmentTokenProvider:
    """仅供开发联调；环境变量不会写入日志或数据库。"""

    def get_token(self, *, force_refresh: bool = False) -> AccessTokenBundle:
        token = str(os.getenv("QCSCKP_OE_ACCESS_TOKEN") or "").strip()
        if not token:
            raise OfficialApiNotConfigured("未注入 QCSCKP_OE_ACCESS_TOKEN")
        try:
            expires_at = float(os.getenv("QCSCKP_OE_EXPIRES_AT") or 0)
        except ValueError:
            expires_at = 0
        bundle = AccessTokenBundle(access_token=token, expires_at=expires_at)
        if force_refresh or not bundle.usable():
            raise ApiTokenError("开发注入的千川 access_token 已失效，请重新注入")
        return bundle


class DefaultTokenProvider:
    def __init__(self) -> None:
        self._env = EnvironmentTokenProvider()
        self._dpapi = DpapiTokenProvider()

    def get_token(self, *, force_refresh: bool = False) -> AccessTokenBundle:
        # 用户在页面保存的 DPAPI 配置始终优先。环境变量只是未配置
        # 本机文件时的开发联调后备，避免用户授权后仍读到旧令牌。
        if os.path.isfile(self._dpapi.path):
            return self._dpapi.get_token(force_refresh=force_refresh)
        if str(os.getenv("QCSCKP_OE_ACCESS_TOKEN") or "").strip():
            return self._env.get_token(force_refresh=force_refresh)
        return self._dpapi.get_token(force_refresh=force_refresh)


def api_configuration_status(path: str = QIANCHUAN_API_TOKEN_FILE) -> dict[str, Any]:
    """返回可向前端展示的脱敏状态，绝不返回 secret 或 token。"""
    try:
        bundle = _load_saved_bundle(path)
    except ApiTokenError:
        # DPAPI 密文只能由创建它的 Windows 用户解密。测试包若曾被连同
        # data 目录复制到另一台电脑，状态查询必须退回可重新配置状态，
        # 不能因为旧密文不可读而永久锁死“保存并授权”入口。
        return {
            "configured": False,
            "authorized": False,
            "app_id": "",
            "app_secret_saved": False,
            "authorization_pending": False,
            "expires_at": 0,
            "oauth_callback_url": QIANCHUAN_OAUTH_CALLBACK_URL,
            "requires_reentry": True,
            "configuration_error": "unreadable_local_encryption",
        }
    if bundle is None:
        return {
            "configured": False,
            "authorized": False,
            "app_id": "",
            "app_secret_saved": False,
            "authorization_pending": False,
            "expires_at": 0,
            "oauth_callback_url": QIANCHUAN_OAUTH_CALLBACK_URL,
            "requires_reentry": False,
            "configuration_error": "",
        }
    return {
        "configured": bool(bundle.app_id and bundle.app_secret),
        "authorized": bool(bundle.usable()),
        "app_id": bundle.app_id,
        "app_secret_saved": bool(bundle.app_secret),
        "authorization_pending": bool(
            bundle.oauth_state
            and bundle.oauth_started_at
            and bundle.oauth_poll_secret
            and time.time() - bundle.oauth_started_at <= 10 * 60
        ),
        "expires_at": bundle.expires_at,
        "oauth_callback_url": QIANCHUAN_OAUTH_CALLBACK_URL,
        "requires_reentry": False,
        "configuration_error": "",
    }


def save_api_credentials(
    app_id: Any,
    app_secret: Any,
    path: str = QIANCHUAN_API_TOKEN_FILE,
) -> dict[str, Any]:
    aid = str(app_id or "").strip()
    secret = str(app_secret or "").strip()
    if not aid.isdigit() or len(aid) < 6:
        raise ValueError("App ID 格式不正确")
    try:
        existing = _load_saved_bundle(path)
    except ApiTokenError:
        # 用户明确输入了新的 Secret 时，允许覆盖从另一台电脑复制过来、
        # 当前 Windows 用户无法解密的 DPAPI 文件。否则用户会永远卡在
        # “配置读取失败”，连重新配置的入口也无法使用。
        if not secret:
            raise
        existing = None
    if not secret and existing and existing.app_id == aid:
        secret = existing.app_secret
    if len(secret) < 6:
        raise ValueError("请输入 App Secret")
    same_credentials = bool(
        existing and existing.app_id == aid and existing.app_secret == secret
    )
    save_token_bundle(
        AccessTokenBundle(
            access_token=existing.access_token if same_credentials else "",
            refresh_token=existing.refresh_token if same_credentials else "",
            app_id=aid,
            app_secret=secret,
            expires_at=existing.expires_at if same_credentials else 0,
        ),
        path,
    )
    return api_configuration_status(path)


def _relay_json_request(
    endpoint: str,
    payload: dict[str, Any],
    *,
    timeout: int = 15,
) -> tuple[int, dict[str, Any]]:
    request = Request(
        QIANCHUAN_OAUTH_RELAY_BASE_URL + endpoint,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cache-Control": "no-store",
            # Cloudflare may challenge urllib's default Python-urllib user agent.
            # Use an explicit desktop-client identity so OAuth session creation and
            # polling follow the same path as other supported HTTP clients.
            "User-Agent": "QCSCKP-Desktop/0.1.48",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            result = {}
        raise ApiTokenError(
            str(result.get("message") or "千川授权中转服务暂不可用"),
            code=exc.code,
        ) from exc
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ApiTokenError("千川授权中转服务连接失败，请检查网络后重试") from exc
    if not isinstance(result, dict):
        raise ApiTokenError("千川授权中转服务响应格式异常")
    return status, result


def begin_api_authorization(
    path: str = QIANCHUAN_API_TOKEN_FILE,
    *,
    relay_request: Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]] = _relay_json_request,
) -> dict[str, Any]:
    bundle = _load_saved_bundle(path)
    if not bundle or not bundle.app_id or not bundle.app_secret:
        raise OfficialApiNotConfigured("请先保存 App ID 和 App Secret")
    state = secrets.token_urlsafe(24)
    poll_secret = secrets.token_urlsafe(32)
    started = time.time()
    status, relay = relay_request(
        "/oauth/session",
        {"state": state, "poll_secret": poll_secret},
    )
    if status not in {200, 201} or not relay.get("success"):
        raise ApiTokenError(str(relay.get("message") or "创建千川授权会话失败"))
    save_token_bundle(
        AccessTokenBundle(
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token,
            app_id=bundle.app_id,
            app_secret=bundle.app_secret,
            expires_at=bundle.expires_at,
            oauth_state=state,
            oauth_started_at=started,
            oauth_poll_secret=poll_secret,
        ),
        path,
    )
    return {
        "url": QIANCHUAN_OAUTH_PAGE
        + "?"
        + urlencode({"app_id": bundle.app_id, "state": state, "material_auth": "1"}),
        "started_at": started,
    }


def poll_api_authorization(
    path: str = QIANCHUAN_API_TOKEN_FILE,
    *,
    relay_request: Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]] = _relay_json_request,
) -> dict[str, Any]:
    """领取一次性授权码并在本机换令牌；中转服务永远拿不到应用密钥。"""
    bundle = _load_saved_bundle(path)
    if not bundle or not bundle.app_id or not bundle.app_secret:
        raise OfficialApiNotConfigured("请先保存 App ID 和 App Secret")
    if (
        not bundle.oauth_state
        or not bundle.oauth_poll_secret
        or not bundle.oauth_started_at
    ):
        if bundle.usable():
            return {"completed": True, "authorized": True}
        raise ApiTokenError("没有等待中的授权，请点击保存并授权")
    if time.time() - bundle.oauth_started_at > 10 * 60:
        raise ApiTokenError("授权请求已过期，请重新点击保存并授权")
    status, relay = relay_request(
        "/oauth/result",
        {"state": bundle.oauth_state, "poll_secret": bundle.oauth_poll_secret},
    )
    relay_status = str(relay.get("status") or "").lower()
    if status == 202 or relay_status == "pending":
        return {"completed": False, "authorized": False}
    if status != 200 or not relay.get("success") or relay_status != "ready":
        raise ApiTokenError(str(relay.get("message") or "读取千川授权结果失败"))
    auth_code = str(relay.get("auth_code") or "").strip()
    returned_state = str(relay.get("state") or "").strip()
    if not auth_code or not returned_state:
        raise ApiTokenError("千川授权结果不完整，请重新授权")
    exchange_authorization_code(
        urlencode({"auth_code": auth_code, "state": returned_state}),
        path,
    )
    return {"completed": True, "authorized": True}


def exchange_authorization_code(
    authorization_callback: Any,
    path: str = QIANCHUAN_API_TOKEN_FILE,
) -> AccessTokenBundle:
    callback = str(authorization_callback or "").strip()
    query = urlparse(callback).query if "://" in callback else callback.lstrip("?")
    params = parse_qs(query, keep_blank_values=True)
    code = str((params.get("auth_code") or [""])[0]).strip()
    returned_state = str((params.get("state") or [""])[0]).strip()
    if not code or len(code) < 6 or not returned_state:
        raise ValueError("请粘贴包含 auth_code 和 state 的完整授权回调地址")
    bundle = _load_saved_bundle(path)
    if not bundle or not bundle.app_id or not bundle.app_secret:
        raise OfficialApiNotConfigured("请先保存 App ID 和 App Secret")
    if (
        not bundle.oauth_state
        or not bundle.oauth_started_at
        or time.time() - bundle.oauth_started_at > 10 * 60
    ):
        raise ApiTokenError("授权请求已过期，请重新打开官方授权页")
    if not secrets.compare_digest(returned_state, bundle.oauth_state):
        raise ApiTokenError("授权回调 state 校验失败，请重新授权")
    body = urlencode(
        {
            "app_id": bundle.app_id,
            "secret": bundle.app_secret,
            "auth_code": code,
        }
    ).encode("utf-8")
    request = Request(
        QIANCHUAN_OFFICIAL_API_BASE_URL + "/open_api/oauth2/access_token/",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise ApiTokenError("千川 Open API 授权码交换失败") from exc
    if str(result.get("code") or "0") not in {"", "0"}:
        raise ApiTokenError(
            str(result.get("message") or "千川 Open API 授权失败"),
            code=result.get("code"),
            request_id=result.get("request_id"),
        )
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    expires_in = float(data.get("expires_in") or 0)
    authorized = AccessTokenBundle(
        access_token=str(data.get("access_token") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        app_id=bundle.app_id,
        app_secret=bundle.app_secret,
        expires_at=time.time() + expires_in if expires_in else 0,
    )
    if not authorized.access_token or not authorized.refresh_token:
        raise ApiTokenError("千川 Open API 授权响应缺少令牌")
    save_token_bundle(authorized, path)
    return authorized


def clear_api_configuration(path: str = QIANCHUAN_API_TOKEN_FILE) -> None:
    if os.path.isfile(path):
        os.remove(path)


_DEFAULT_PROVIDER: Optional[DefaultTokenProvider] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_token_provider() -> DefaultTokenProvider:
    global _DEFAULT_PROVIDER
    with _DEFAULT_LOCK:
        if _DEFAULT_PROVIDER is None:
            _DEFAULT_PROVIDER = DefaultTokenProvider()
        return _DEFAULT_PROVIDER
