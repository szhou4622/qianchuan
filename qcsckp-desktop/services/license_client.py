"""Minimal protocol-v2 HTTP client for the shared time-license service."""

from __future__ import annotations

import json
import random
import socket
import time
import uuid
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import (
    CURRENT_VERSION,
    LICENSE_APP_NAME,
    LICENSE_PROTOCOL_VERSION,
    LICENSE_SERVICE_BASE_URL,
)


_ACTIVATE_PATH = "/activate"
_DEVICE_STATUS_PATH = "/device/status"
_DEVICE_UNBIND_PATH = "/device/unbind"


class LicenseServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 0,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.message = message
        self.http_status = int(http_status or 0)
        self.retryable = bool(retryable)
        super().__init__(message)


class LicenseNetworkError(LicenseServiceError):
    def __init__(self) -> None:
        super().__init__(
            "license_service_unavailable",
            "授权服务器暂时无法连接，请重试",
            retryable=True,
        )


def _message_from_payload(payload: Any, fallback: str) -> str:
    if not isinstance(payload, Mapping):
        return fallback
    direct = payload.get("message")
    if direct:
        return str(direct)
    error = payload.get("error")
    if isinstance(error, Mapping) and error.get("message"):
        return str(error.get("message"))
    if isinstance(error, str) and error.strip():
        return error.strip()
    return fallback


def _data_from_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise LicenseServiceError("invalid_response", "授权服务器响应格式无效")
    license_data = payload.get("license")
    if isinstance(license_data, Mapping):
        result = dict(license_data)
        summary = payload.get("data")
        if isinstance(summary, Mapping):
            for key, value in summary.items():
                result.setdefault(str(key), value)
        if payload.get("message") and not result.get("message"):
            result["message"] = str(payload.get("message"))
        return result
    data = payload.get("data")
    if isinstance(data, Mapping):
        result = dict(data)
        if payload.get("message") and not result.get("message"):
            result["message"] = str(payload.get("message"))
        return result
    return dict(payload)


_STATUS_SENSITIVE_FIELDS = {
    "activation_code",
    "primary_activation_code",
    "device_session",
    "device_credential",
    "access_token",
    "refresh_token",
    "token",
    "app_secret",
    "secret",
}


def _sanitize_status_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop secrets the status endpoint does not need to expose to the app."""
    return {
        str(key): value
        for key, value in payload.items()
        if str(key).strip().lower() not in _STATUS_SENSITIVE_FIELDS
    }


class LicenseHttpClient:
    def __init__(
        self,
        *,
        base_url: str = LICENSE_SERVICE_BASE_URL,
        timeout_seconds: float = 8.0,
        status_attempts: int = 2,
        opener=None,
        sleeper=None,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout_seconds = max(2.0, float(timeout_seconds))
        self.status_attempts = max(1, min(3, int(status_attempts)))
        self._opener = opener or urlopen
        self._sleep = sleeper or time.sleep

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        credentials: Optional[Mapping[str, str]] = None,
        attempts: int = 1,
    ) -> dict[str, Any]:
        url = self.base_url + path
        headers = {
            "Accept": "application/json",
            "User-Agent": f"QCSCKP-Desktop/{CURRENT_VERSION}",
            "X-Client-Request-ID": uuid.uuid4().hex,
        }
        payload_bytes = None
        if body is not None:
            payload_bytes = json.dumps(
                dict(body),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if credentials:
            headers["Authorization"] = (
                "Bearer " + str(credentials.get("device_session") or "")
            )
            headers["X-Device-Credential"] = str(
                credentials.get("device_credential") or ""
            )

        for attempt in range(1, max(1, attempts) + 1):
            request = Request(
                url,
                data=payload_bytes,
                headers=headers,
                method=method.upper(),
            )
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    raw = response.read(1024 * 1024)
                    status = int(getattr(response, "status", 200) or 200)
            except HTTPError as exc:
                try:
                    error_payload = json.loads(exc.read(1024 * 1024).decode("utf-8"))
                except Exception:
                    error_payload = {}
                status = int(getattr(exc, "code", 0) or 0)
                if status >= 500 and attempt < attempts:
                    self._sleep(0.35 * attempt + random.random() * 0.15)
                    continue
                if status == 401:
                    message = "当前设备授权已失效，请重新激活"
                else:
                    message = _message_from_payload(
                        error_payload,
                        "授权请求失败，请重试",
                    )
                raise LicenseServiceError(
                    str(
                        (error_payload.get("code") if isinstance(error_payload, Mapping) else "")
                        or f"http_{status}"
                    ),
                    message,
                    http_status=status,
                    retryable=status >= 500,
                ) from exc
            except (URLError, socket.timeout, TimeoutError, OSError):
                if attempt < attempts:
                    self._sleep(0.35 * attempt + random.random() * 0.15)
                    continue
                raise LicenseNetworkError()

            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, TypeError) as exc:
                raise LicenseServiceError(
                    "invalid_response",
                    "授权服务器响应格式无效",
                    http_status=status,
                ) from exc
            if isinstance(envelope, Mapping):
                ok = envelope.get("ok")
                success = envelope.get("success")
                if ok is False or success is False:
                    raise LicenseServiceError(
                        str(envelope.get("code") or "business_error"),
                        _message_from_payload(envelope, "授权请求失败，请重试"),
                        http_status=status,
                    )
            return _data_from_payload(envelope)
        raise LicenseNetworkError()

    def activate(
        self,
        activation_code: str,
        machine_code: str,
        *,
        current_code_id: str = "",
        credential_refresh: bool = False,
    ) -> dict[str, Any]:
        # POST is deliberately not retried.  If the response is unknown, the
        # user can retry manually with the same code and stable machine code.
        body: dict[str, Any] = {
            "app_name": LICENSE_APP_NAME,
            "activation_code": activation_code,
            "machine_code": machine_code,
            "client_version": CURRENT_VERSION,
            "license_protocol_version": LICENSE_PROTOCOL_VERSION,
        }
        if credential_refresh:
            body["current_code_id"] = str(current_code_id or "").strip()
            body["credential_refresh"] = True
        return self._request(
            "POST",
            _ACTIVATE_PATH,
            body=body,
            attempts=1,
        )

    def device_status(
        self,
        credentials: Mapping[str, str],
        *,
        machine_code: str,
        code_id: str,
    ) -> dict[str, Any]:
        query = urlencode(
            {
                "license_protocol_version": LICENSE_PROTOCOL_VERSION,
                "app_name": LICENSE_APP_NAME,
                "machine_code": str(machine_code or "").strip(),
                "code_id": str(code_id or "").strip(),
                "client_version": CURRENT_VERSION,
            }
        )
        result = self._request(
            "GET",
            _DEVICE_STATUS_PATH + "?" + query,
            credentials=credentials,
            attempts=self.status_attempts,
        )
        return _sanitize_status_payload(result)

    def unbind(
        self,
        credentials: Mapping[str, str],
        *,
        machine_code: str,
        code_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            _DEVICE_UNBIND_PATH,
            body={
                "license_protocol_version": LICENSE_PROTOCOL_VERSION,
                "app_name": LICENSE_APP_NAME,
                "machine_code": str(machine_code or "").strip(),
                "current_code_id": str(code_id or "").strip(),
                "client_version": CURRENT_VERSION,
            },
            credentials=credentials,
            attempts=1,
        )
