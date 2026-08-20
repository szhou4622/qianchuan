"""Lazy, validated contact-author configuration for the desktop client.

The UI never talks to the shared update service directly.  This module is the
only remote reader; it validates the application identity and image URL before
persisting a small, non-secret cache in the writable runtime data directory.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from config import (
    APP_NAME,
    CONTACT_CONFIG_CACHE_FILE,
    CONTACT_FALLBACK_IMAGE_FILE,
    CURRENT_VERSION,
)


CONTACT_REMOTE_ENDPOINT = "https://update.dadaozixun.com/api/contact"
CACHE_FORMAT = "qcsckp-contact-config-cache-v1"


class ContactConfigError(RuntimeError):
    """Remote or cached contact configuration is invalid."""


class ContactConfigNotConfigured(ContactConfigError):
    """The remote service is healthy but has no configuration for this app."""


def _parse_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ContactConfigError("enabled must be a boolean")


def _validate_https_image_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ContactConfigError("qr_image_url must be an HTTPS URL")
    return text


def _unwrap_remote_payload(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContactConfigError("contact response must be an object")
    nested = payload.get("data")
    if isinstance(nested, Mapping):
        return nested
    return payload


def validate_contact_config(
    payload: Any,
    *,
    expected_app_name: str = APP_NAME,
) -> dict[str, Any]:
    data = _unwrap_remote_payload(payload)
    returned_app_name = str(data.get("app_name") or "").strip()
    if returned_app_name != expected_app_name:
        raise ContactConfigError("returned app_name does not match this application")
    enabled = _parse_enabled(data.get("enabled"))
    return {
        "app_name": expected_app_name,
        "enabled": enabled,
        # Once the backend disables contact, an old URL is deliberately
        # discarded before caching so it cannot reappear during an outage.
        "qr_image_url": (
            _validate_https_image_url(data.get("qr_image_url")) if enabled else ""
        ),
        "updated_at": str(data.get("updated_at") or "").strip()[:128],
        "state": "configured",
    }


def _default_remote_loader(url: str, timeout: float) -> Mapping[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"QCSCKP-Desktop/{CURRENT_VERSION}",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
    except HTTPError as exc:
        if int(getattr(exc, "code", 0) or 0) == 404:
            raise ContactConfigNotConfigured("contact is not configured") from exc
        raise ContactConfigError("contact service returned an HTTP error") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ContactConfigError("contact service is unavailable") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ContactConfigError("contact service returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ContactConfigError("contact service returned invalid data")
    return payload


@dataclass
class ContactConfigService:
    app_name: str = APP_NAME
    cache_file: str = CONTACT_CONFIG_CACHE_FILE
    fallback_image_file: str = CONTACT_FALLBACK_IMAGE_FILE
    remote_endpoint: str = CONTACT_REMOTE_ENDPOINT
    timeout_seconds: float = 4.0
    remote_loader: Optional[Callable[[str, float], Mapping[str, Any]]] = None

    @property
    def remote_url(self) -> str:
        return f"{self.remote_endpoint}?{urlencode({'app_name': self.app_name})}"

    def _load_remote(self) -> dict[str, Any]:
        loader = self.remote_loader or _default_remote_loader
        payload = loader(self.remote_url, float(self.timeout_seconds))
        return validate_contact_config(payload, expected_app_name=self.app_name)

    def _validate_cached_payload(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ContactConfigError("cache must be an object")
        if str(payload.get("format") or "") != CACHE_FORMAT:
            raise ContactConfigError("cache format is invalid")
        if str(payload.get("app_name") or "") != self.app_name:
            raise ContactConfigError("cache belongs to another application")
        data = payload.get("config")
        if not isinstance(data, Mapping):
            raise ContactConfigError("cache config is missing")
        state = str(data.get("state") or "configured")
        if state == "unconfigured":
            return {
                "app_name": self.app_name,
                "enabled": True,
                "qr_image_url": "",
                "updated_at": str(data.get("updated_at") or "")[:128],
                "state": "unconfigured",
            }
        if state != "configured":
            raise ContactConfigError("cache state is invalid")
        return validate_contact_config(data, expected_app_name=self.app_name)

    def _read_cache(self) -> Optional[dict[str, Any]]:
        try:
            with open(self.cache_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return self._validate_cached_payload(payload)
        except (OSError, ValueError, TypeError, ContactConfigError):
            return None

    def _write_cache(self, config: Mapping[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self.cache_file))
        os.makedirs(directory, exist_ok=True)
        payload = {
            "format": CACHE_FORMAT,
            "app_name": self.app_name,
            "config": dict(config),
        }
        descriptor, temporary_path = tempfile.mkstemp(
            prefix="contact-config-",
            suffix=".tmp",
            dir=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.cache_file)
        finally:
            try:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)
            except OSError:
                pass

    @staticmethod
    def _public_response(
        config: Mapping[str, Any],
        *,
        source: str,
        used_cache: bool = False,
    ) -> dict[str, Any]:
        state = str(config.get("state") or "configured")
        enabled = bool(config.get("enabled"))
        image_url = str(config.get("qr_image_url") or "")
        result = {
            "app_name": str(config.get("app_name") or APP_NAME),
            "enabled": enabled,
            "qr_image_url": image_url,
            "updated_at": str(config.get("updated_at") or ""),
            "source": source,
            "cached": bool(used_cache),
            "use_builtin_image": False,
            "status": "configured",
            "message": "",
        }
        if state == "unconfigured":
            result.update(
                {
                    "qr_image_url": "",
                    "status": "fallback",
                    "message": "",
                    "use_builtin_image": True,
                }
            )
        elif not enabled:
            result.update(
                {
                    "qr_image_url": "",
                    "status": "disabled",
                    "message": "联系方式暂未开放",
                }
            )
        elif not image_url:
            result.update(
                {
                    "status": "missing_image",
                    "message": "联系方式图片暂未配置",
                }
            )
        return result

    def get_contact_config(self) -> dict[str, Any]:
        """Read remote configuration lazily and fall back without leaking errors."""
        try:
            config = self._load_remote()
            try:
                self._write_cache(config)
            except OSError:
                # A read-only/full disk must not hide a currently valid
                # remote configuration from the user.
                pass
            return self._public_response(config, source="remote")
        except ContactConfigNotConfigured:
            config = {
                "app_name": self.app_name,
                "enabled": True,
                "qr_image_url": "",
                "updated_at": "",
                "state": "unconfigured",
            }
            # A healthy 404 is an authoritative state.  Cache it so an older
            # QR image cannot reappear during the next temporary outage.
            try:
                self._write_cache(config)
            except OSError:
                pass
            return self._public_response(config, source="remote_unconfigured")
        except ContactConfigError:
            cached = self._read_cache()
            if cached is not None:
                return self._public_response(cached, source="cache", used_cache=True)
            fallback = {
                "app_name": self.app_name,
                "enabled": True,
                "qr_image_url": "",
                "updated_at": "",
                "state": "unconfigured",
            }
            return self._public_response(fallback, source="builtin")
