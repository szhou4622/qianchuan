"""可注入的千川令牌提供器。

当前不提供 App ID/App Secret 前端。联调可通过环境变量或直接注入 provider；未来
OAuth 页面只需调用 ``save_token_bundle``，不需要修改 API 客户端。
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import QIANCHUAN_API_TOKEN_FILE, QIANCHUAN_OFFICIAL_API_BASE_URL
from .errors import ApiTokenError, OfficialApiNotConfigured


@dataclass(frozen=True)
class AccessTokenBundle:
    access_token: str
    refresh_token: str = ""
    app_id: str = ""
    app_secret: str = ""
    expires_at: float = 0.0

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
                    "千川官方 API 尚未配置；请先注入 access_token，后续再接入 OAuth 配置页"
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
        if str(os.getenv("QCSCKP_OE_ACCESS_TOKEN") or "").strip():
            return self._env.get_token(force_refresh=force_refresh)
        return self._dpapi.get_token(force_refresh=force_refresh)


_DEFAULT_PROVIDER: Optional[DefaultTokenProvider] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_token_provider() -> DefaultTokenProvider:
    global _DEFAULT_PROVIDER
    with _DEFAULT_LOCK:
        if _DEFAULT_PROVIDER is None:
            _DEFAULT_PROVIDER = DefaultTokenProvider()
        return _DEFAULT_PROVIDER
