"""Fail-closed online activation state for the desktop application."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from config import LICENSE_APP_NAME, SOFTWARE_CHINESE_NAME
from services.license_client import (
    LicenseHttpClient,
    LicenseNetworkError,
    LicenseServiceError,
)
from services.license_storage import LicenseSecureStore, LicenseStorageError


_MONTH_TYPES = {
    "month",
    "monthly",
    "month_card",
    "monthly_card",
    "time_month",
    "time_30d",
    "月卡",
}
_WEEK_TYPES = {
    "week",
    "weekly",
    "week_card",
    "weekly_card",
    "time_week",
    "time_7d",
    "7d",
    "周卡",
}
_YEAR_TYPES = {
    "year",
    "yearly",
    "annual",
    "year_card",
    "yearly_card",
    "annual_card",
    "time_year",
    "time_365d",
    "年卡",
}
_PERMANENT_TYPES = {
    "permanent",
    "lifetime",
    "forever",
    "perpetual",
    "unlimited",
    "unlimited_time",
    "unlimited_duration",
    "permanent_card",
    "lifetime_card",
    "time_permanent",
    "time_forever",
    "永久",
    "永久卡",
}
_GENERIC_TIME_TYPES = {
    "time",
    "timed",
    "time_based",
    "time_limited",
    "duration",
    "duration_based",
    "standard",
    "时间授权",
}
_BLOCKED_LICENSE_STATUSES = {"expired", "disabled", "revoked", "inactive"}
_DEFAULT_RUNTIME_NETWORK_GRACE_SECONDS = 30 * 60


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _int_field(data: Mapping[str, Any], name: str, *, minimum: int = 0) -> int:
    try:
        value = int(data.get(name))
    except (TypeError, ValueError) as exc:
        raise LicenseServiceError(
            "invalid_response",
            f"授权服务器未返回有效的 {name}",
        ) from exc
    if value < minimum:
        raise LicenseServiceError(
            "invalid_response",
            f"授权服务器返回的 {name} 无效",
        )
    return value


def _optional_int_field(
    data: Mapping[str, Any],
    name: str,
    *,
    minimum: int = 0,
) -> Optional[int]:
    value = data.get(name)
    if value in (None, ""):
        return None
    return _int_field(data, name, minimum=minimum)


def _optional_number_field(data: Mapping[str, Any], name: str) -> Optional[float]:
    value = data.get(name)
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LicenseServiceError(
            "invalid_response",
            f"授权服务器返回的 {name} 无效",
        ) from exc
    if number < 0:
        raise LicenseServiceError(
            "invalid_response",
            f"授权服务器返回的 {name} 无效",
        )
    return int(number) if number.is_integer() else number


def _license_type_label(value: Any, duration_days: Optional[int]) -> tuple[str, str]:
    normalized = str(value or "").strip().lower()
    if normalized in _PERMANENT_TYPES:
        return "permanent", "永久授权"
    if normalized in _WEEK_TYPES:
        if duration_days not in (None, 7):
            raise LicenseServiceError("invalid_response", "周卡授权天数不是7天")
        return normalized, "周卡"
    if normalized in _MONTH_TYPES:
        if duration_days not in (None, 30):
            raise LicenseServiceError("invalid_response", "月卡授权天数不是30天")
        return normalized, "月卡"
    if normalized in _YEAR_TYPES:
        if duration_days not in (None, 365):
            raise LicenseServiceError("invalid_response", "年卡授权天数不是365天")
        return normalized, "年卡"
    if normalized in _GENERIC_TIME_TYPES:
        # The existing administration backend represents a permanent
        # entitlement as the generic `standard`/time type with no positive
        # duration.  This is a server-issued semantic, not a code-text guess.
        if duration_days in (None, 0):
            return "permanent", "永久授权"
        if duration_days == 7:
            return "time_7d", "周卡"
        if duration_days == 30:
            return "time_30d", "月卡"
        if duration_days == 365:
            return "time_365d", "年卡"
    raise LicenseServiceError(
        "unsupported_license_type",
        "当前软件只支持周卡、月卡、年卡或永久授权",
    )


def _required_text(data: Mapping[str, Any], name: str) -> str:
    value = str(data.get(name) or "").strip()
    if not value:
        raise LicenseServiceError(
            "invalid_response",
            f"授权服务器未返回 {name}",
        )
    return value


class LicenseManager:
    def __init__(
        self,
        *,
        client: Optional[LicenseHttpClient] = None,
        store: Optional[LicenseSecureStore] = None,
        monotonic=None,
        runtime_network_grace_seconds: float = _DEFAULT_RUNTIME_NETWORK_GRACE_SECONDS,
    ) -> None:
        self.client = client or LicenseHttpClient()
        self.store = store or LicenseSecureStore()
        self._lock = threading.RLock()
        self._activation_lock = threading.Lock()
        self._unbind_lock = threading.Lock()
        self._monotonic = monotonic or time.monotonic
        self._runtime_network_grace_seconds = max(
            0.0,
            float(runtime_network_grace_seconds),
        )
        self._runtime_network_failure_started: Optional[float] = None
        self._authorized = False
        self._last_public_state: dict[str, Any] = self._inactive_state()

    @staticmethod
    def _inactive_state(message: str = "请输入激活码完成授权") -> dict[str, Any]:
        return {
            "success": True,
            "authorized": False,
            "needs_activation": True,
            "network_error": False,
            "software_name": SOFTWARE_CHINESE_NAME,
            "app_name": LICENSE_APP_NAME,
            "message": message,
            "license": None,
        }

    @staticmethod
    def _error_state(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, LicenseServiceError):
            message = exc.message
            code = exc.code
            network_error = bool(
                isinstance(exc, LicenseNetworkError)
                or (exc.retryable and int(exc.http_status or 0) >= 500)
            )
            http_status = exc.http_status
        elif isinstance(exc, LicenseStorageError):
            message = str(exc)
            code = "license_storage_error"
            network_error = False
            http_status = 0
        else:
            message = "授权验证失败，请重试"
            code = "license_check_failed"
            network_error = False
            http_status = 0
        return {
            "success": False,
            "authorized": False,
            "needs_activation": not network_error,
            "network_error": network_error,
            "software_name": SOFTWARE_CHINESE_NAME,
            "app_name": LICENSE_APP_NAME,
            "code": code,
            "http_status": http_status,
            "message": message,
            "license": None,
        }

    def _normalize_license(
        self,
        data: Mapping[str, Any],
        *,
        require_credentials: bool,
    ) -> tuple[dict[str, Any], Optional[dict[str, str]]]:
        returned_app_name = str(data.get("app_name") or LICENSE_APP_NAME).strip()
        if returned_app_name != LICENSE_APP_NAME:
            raise LicenseServiceError(
                "app_name_mismatch",
                "授权结果不属于当前软件",
            )
        raw_duration_days = _optional_int_field(data, "duration_days", minimum=0)
        license_type, type_label = _license_type_label(
            data.get("license_type"),
            raw_duration_days,
        )
        is_permanent = license_type == "permanent"
        if not is_permanent and raw_duration_days is None:
            raise LicenseServiceError(
                "invalid_response",
                "授权服务器未返回有效的 duration_days",
            )
        binding_status = _required_text(data, "binding_status").lower()
        license_status = str(data.get("license_status") or data.get("status") or "active").strip().lower()
        action = str(data.get("action") or "").strip().lower()
        grant_score = _optional_number_field(data, "grant_score")
        if action == "rebound" and grant_score not in (None, 0):
            raise LicenseServiceError(
                "invalid_response",
                "换机绑定响应异常：服务器不得重新发放初始权益",
            )
        expires_at = str(data.get("expires_at") or "").strip()
        remaining_days = _optional_int_field(data, "remaining_days", minimum=0)
        if not is_permanent:
            if not expires_at:
                raise LicenseServiceError(
                    "invalid_response",
                    "授权服务器未返回 expires_at",
                )
            if remaining_days is None:
                raise LicenseServiceError(
                    "invalid_response",
                    "授权服务器未返回有效的 remaining_days",
                )
        metadata = {
            "app_name": LICENSE_APP_NAME,
            "code_id": _required_text(data, "code_id"),
            "binding_status": binding_status,
            "license_type": license_type,
            "license_type_label": type_label,
            "duration_days": 0 if is_permanent else int(raw_duration_days or 0),
            "activated_at": _required_text(data, "activated_at"),
            "expires_at": expires_at,
            "remaining_days": None if is_permanent else remaining_days,
            "is_permanent": is_permanent,
            "transfer_count": _int_field(data, "transfer_count", minimum=0),
            "self_transfers_used_30d": _optional_int_field(
                data, "self_transfers_used_30d", minimum=0
            ),
            "self_transfers_remaining_30d": _optional_int_field(
                data, "self_transfers_remaining_30d", minimum=0
            ),
            "action": action,
            "grant_score": 0 if action == "rebound" else grant_score,
            "license_status": license_status,
            "last_verified_at": _now_text(),
        }
        machine_code = self.store.get_or_create_machine_code()
        returned_machine = str(data.get("machine_code") or "").strip()
        if returned_machine and returned_machine.casefold() != machine_code.casefold():
            raise LicenseServiceError(
                "machine_code_mismatch",
                "授权结果不属于当前设备",
            )
        metadata["current_device"] = (
            str(data.get("device_name") or "").strip()
            or f"本机设备 · {machine_code[-8:]}"
        )
        credentials = None
        if require_credentials:
            credentials = {
                "device_session": _required_text(data, "device_session"),
                "device_credential": _required_text(data, "device_credential"),
            }
        return metadata, credentials

    def _preserve_recoverable_binding(
        self,
        data: Mapping[str, Any],
        activation_code: str,
    ) -> bool:
        """Keep a newly issued device credential if display normalization fails.

        Activation is state-changing on the server.  If a newer server license
        label is not understood by this client, discarding the returned
        credential would leave the code bound but unrecoverable locally.  This
        method validates the binding envelope and stores only the secure device
        credential plus a small display-only metadata subset; it never grants
        runtime access by itself.
        """
        try:
            returned_app_name = str(data.get("app_name") or "").strip()
            if returned_app_name != LICENSE_APP_NAME:
                return False
            if str(data.get("binding_status") or "").strip().lower() != "active":
                return False
            returned_machine = str(data.get("machine_code") or "").strip()
            local_machine = self.store.get_or_create_machine_code()
            if returned_machine and returned_machine.casefold() != local_machine.casefold():
                return False
            credentials = {
                "device_session": _required_text(data, "device_session"),
                "device_credential": _required_text(data, "device_credential"),
                "activation_code": str(activation_code or "").strip(),
                "code_id": _required_text(data, "code_id"),
                "machine_code": local_machine,
            }
            metadata = {
                "app_name": LICENSE_APP_NAME,
                "code_id": _required_text(data, "code_id"),
                "binding_status": "active",
                "license_type": str(data.get("license_type") or "").strip(),
                "duration_days": data.get("duration_days"),
                "activated_at": str(data.get("activated_at") or "").strip(),
                "expires_at": str(data.get("expires_at") or "").strip(),
                "remaining_days": data.get("remaining_days"),
                "transfer_count": data.get("transfer_count", 0),
                "license_status": str(
                    data.get("license_status") or data.get("status") or "active"
                ).strip(),
                "last_verified_at": _now_text(),
            }
            self.store.save_credentials(credentials)
            self.store.save_metadata(metadata)
            return True
        except Exception:
            return False

    @staticmethod
    def _is_server_active(metadata: Mapping[str, Any]) -> bool:
        return (
            str(metadata.get("binding_status") or "").lower() == "active"
            and str(metadata.get("license_status") or "active").lower()
            not in _BLOCKED_LICENSE_STATUSES
        )

    @staticmethod
    def _public_active_state(metadata: Mapping[str, Any]) -> dict[str, Any]:
        action = str(metadata.get("action") or "").strip().lower()
        return {
            "success": True,
            "authorized": True,
            "needs_activation": False,
            "network_error": False,
            "software_name": SOFTWARE_CHINESE_NAME,
            "app_name": LICENSE_APP_NAME,
            "message": "换机绑定成功" if action == "rebound" else "授权有效",
            "license": dict(metadata),
        }

    @staticmethod
    def _snapshot_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: metadata.get(key)
            for key in (
                "app_name",
                "code_id",
                "license_type",
                "license_type_label",
                "duration_days",
                "activated_at",
                "expires_at",
                "is_permanent",
            )
            if key in metadata
        }

    @staticmethod
    def _merge_status_with_snapshot(
        response: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        merged = dict(response)
        for field in (
            "app_name",
            "code_id",
            "license_type",
            "duration_days",
            "activated_at",
            "expires_at",
        ):
            if merged.get(field) in (None, "") and snapshot.get(field) not in (None, ""):
                merged[field] = snapshot.get(field)
        return merged

    def _legacy_snapshot_is_migratable(
        self,
        response: Mapping[str, Any],
        saved: Mapping[str, Any],
        context: Mapping[str, Any],
        machine_code: str,
    ) -> bool:
        """Permit one safe migration from the old display-only metadata file."""
        response_app = str(response.get("app_name") or "").strip()
        response_code = str(response.get("code_id") or "").strip()
        response_machine = str(response.get("machine_code") or "").strip()
        context_code = str(context.get("code_id") or "").strip()
        saved_code = str(saved.get("code_id") or "").strip()
        if response_app != LICENSE_APP_NAME:
            return False
        if not response_code or response_code not in {context_code, saved_code}:
            return False
        if response_machine and response_machine.casefold() != machine_code.casefold():
            return False
        if str(saved.get("app_name") or "").strip() != LICENSE_APP_NAME:
            return False
        if not context.get("activation_code") or not context_code:
            return False
        response_expiry = str(response.get("expires_at") or "").strip()
        saved_expiry = str(saved.get("expires_at") or "").strip()
        if response_expiry and saved_expiry != response_expiry:
            return False
        if not response_expiry and not bool(saved.get("is_permanent")):
            return False
        return bool(saved.get("license_type") and saved.get("activated_at"))

    def is_runtime_authorized(self) -> bool:
        with self._lock:
            return bool(self._authorized)

    def startup_check(self, *, allow_network_grace: bool = False) -> dict[str, Any]:
        with self._lock:
            previously_authorized = bool(self._authorized)
            if not allow_network_grace:
                self._authorized = False
                self._runtime_network_failure_started = None
        try:
            credentials = self.store.load_credentials()
            if not credentials:
                saved = self.store.load_metadata()
                context = self.store.load_activation_context()
                if str(saved.get("binding_status") or "").lower() == "unbound":
                    state = self._inactive_state(
                        "已解绑，请输入原激活码重新绑定"
                    )
                    state["license"] = saved
                elif saved.get("code_id") and context.get("activation_code"):
                    state = self._inactive_state(
                        "检测到旧版授权，请重新输入原激活码刷新设备凭证"
                    )
                    state["credential_refresh_required"] = True
                    state["license"] = saved
                else:
                    state = self._inactive_state()
            else:
                machine_code = self.store.get_or_create_machine_code()
                context = self.store.load_activation_context()
                saved = self.store.load_metadata()
                encrypted_snapshot = self.store.load_license_snapshot()
                code_id = str(
                    context.get("code_id")
                    or encrypted_snapshot.get("code_id")
                    or saved.get("code_id")
                    or ""
                ).strip()
                response = self.client.device_status(
                    credentials,
                    machine_code=machine_code,
                    code_id=code_id,
                )
                if str(response.get("binding_status") or "").strip().lower() == "unbound":
                    metadata = saved
                    metadata.update(
                        {
                            "app_name": LICENSE_APP_NAME,
                            "binding_status": "unbound",
                            "last_verified_at": _now_text(),
                        }
                    )
                    self.store.clear_credentials()
                    self.store.save_metadata(metadata)
                    state = self._inactive_state("当前设备已解绑，请重新激活")
                    state["license"] = metadata
                    with self._lock:
                        self._authorized = False
                        self._runtime_network_failure_started = None
                        self._last_public_state = dict(state)
                    return state
                # The protocol-v2 status endpoint intentionally omits static
                # card fields.  They were issued by the server at activation
                # time and are used only for display/type validation; active,
                # expiry and binding decisions still come from this fresh
                # status response.
                response = dict(response)
                if not encrypted_snapshot:
                    if not self._legacy_snapshot_is_migratable(
                        response,
                        saved,
                        context,
                        machine_code,
                    ):
                        raise LicenseServiceError(
                            "credential_refresh_required",
                            "检测到旧版授权，请重新输入原激活码刷新安全凭证",
                        )
                    encrypted_snapshot = self._snapshot_from_metadata(saved)
                response = self._merge_status_with_snapshot(
                    response,
                    encrypted_snapshot,
                )
                metadata, _ = self._normalize_license(
                    response,
                    require_credentials=False,
                )
                if not self._is_server_active(metadata):
                    self.store.save_metadata(metadata)
                    state = self._inactive_state(
                        f"授权不可用，到期时间：{metadata.get('expires_at') or '—'}"
                    )
                    state["license"] = metadata
                else:
                    self.store.save_license_snapshot(
                        self._snapshot_from_metadata(metadata)
                    )
                    self.store.save_metadata(metadata)
                    state = self._public_active_state(metadata)
                    with self._lock:
                        self._authorized = True
                        self._runtime_network_failure_started = None
        except Exception as exc:
            if isinstance(exc, LicenseServiceError) and exc.http_status == 401:
                try:
                    self.store.clear_credentials()
                    metadata = self.store.load_metadata()
                    metadata.update(
                        {
                            "binding_status": "invalid",
                            "last_verified_at": _now_text(),
                        }
                    )
                    self.store.save_metadata(metadata)
                except Exception:
                    metadata = {}
                state = self._inactive_state(
                    "当前设备授权已失效，请重新激活"
                )
                state["success"] = False
                state["code"] = exc.code
                state["http_status"] = 401
                state["license"] = metadata or None
            else:
                state = self._error_state(exc)
                if (
                    isinstance(exc, LicenseServiceError)
                    and exc.code == "credential_refresh_required"
                ):
                    state["credential_refresh_required"] = True
                    state["license"] = self.store.load_metadata() or None
            try:
                credentials_remain = bool(self.store.load_credentials())
            except Exception:
                credentials_remain = False
            if (
                allow_network_grace
                and previously_authorized
                and state.get("network_error")
                and credentials_remain
            ):
                now = float(self._monotonic())
                with self._lock:
                    if self._runtime_network_failure_started is None:
                        self._runtime_network_failure_started = now
                    elapsed = max(
                        0.0,
                        now - float(self._runtime_network_failure_started),
                    )
                remaining = max(
                    0,
                    int(self._runtime_network_grace_seconds - elapsed),
                )
                if elapsed < self._runtime_network_grace_seconds:
                    state.update(
                        {
                            "success": True,
                            "authorized": True,
                            "needs_activation": False,
                            "network_error": True,
                            "network_grace": True,
                            "network_grace_remaining_seconds": remaining,
                            "message": "授权服务器连接异常，正在重试",
                            "license": self.store.load_metadata() or None,
                        }
                    )
                    with self._lock:
                        self._authorized = True
                else:
                    state["message"] = "授权服务器连续30分钟无法连接，后台任务已停止"
                    state["network_grace"] = False
                    state["network_grace_remaining_seconds"] = 0
                    with self._lock:
                        self._authorized = False
            elif not state.get("authorized"):
                with self._lock:
                    self._authorized = False
                    self._runtime_network_failure_started = None
        with self._lock:
            if not state.get("authorized"):
                self._authorized = False
            self._last_public_state = dict(state)
        return state

    def runtime_check(self) -> dict[str, Any]:
        return self.startup_check(allow_network_grace=True)

    def activate(self, activation_code: Any) -> dict[str, Any]:
        if self.is_runtime_authorized():
            # A delayed duplicate UI event must not submit or rewrite the
            # already active binding.
            return self.management_info(refresh=False)
        code = str(activation_code or "").strip()
        if not code:
            return self._error_state(
                LicenseServiceError("activation_code_required", "请输入激活码")
            )
        if len(code) > 256:
            return self._error_state(
                LicenseServiceError("activation_code_invalid", "激活码格式无效")
            )
        if not self._activation_lock.acquire(blocking=False):
            return self._error_state(
                LicenseServiceError("activation_in_progress", "正在激活，请勿重复点击")
            )
        response: Mapping[str, Any] = {}
        try:
            machine_code = self.store.get_or_create_machine_code()
            existing_credentials = self.store.load_credentials()
            saved = self.store.load_metadata()
            context = self.store.load_activation_context()
            current_code_id = str(
                context.get("code_id") or saved.get("code_id") or ""
            ).strip()
            refresh_credentials = bool(
                not existing_credentials
                and str(saved.get("binding_status") or "").strip().lower()
                in {"active", "invalid"}
                and current_code_id
                and str(context.get("activation_code") or "").strip() == code
                and str(context.get("machine_code") or "").strip().casefold()
                == machine_code.casefold()
            )
            response = self.client.activate(
                code,
                machine_code,
                current_code_id=current_code_id,
                credential_refresh=refresh_credentials,
                credentials=existing_credentials,
            )
            metadata, credentials = self._normalize_license(
                response,
                require_credentials=True,
            )
            if not self._is_server_active(metadata):
                raise LicenseServiceError(
                    "license_not_active",
                    str(response.get("message") or "授权未处于可用状态"),
                )
            credential_payload = dict(credentials or {})
            credential_payload.update(
                {
                    "activation_code": code,
                    "code_id": metadata["code_id"],
                    "machine_code": machine_code,
                }
            )
            self.store.save_credentials(credential_payload)
            self.store.save_license_snapshot(
                self._snapshot_from_metadata(metadata)
            )
            self.store.save_metadata(metadata)
            state = self._public_active_state(metadata)
            with self._lock:
                self._authorized = True
                self._last_public_state = dict(state)
            return state
        except Exception as exc:
            if isinstance(exc, LicenseServiceError) and exc.code in {
                "unsupported_license_type",
                "invalid_response",
            }:
                self._preserve_recoverable_binding(response, code)
            state = self._error_state(exc)
            with self._lock:
                self._authorized = False
                self._last_public_state = dict(state)
            return state
        finally:
            self._activation_lock.release()

    def management_info(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            return (
                self.runtime_check()
                if self.is_runtime_authorized()
                else self.startup_check()
            )
        with self._lock:
            return dict(self._last_public_state)

    def unbind_current_device(self) -> dict[str, Any]:
        if not self._unbind_lock.acquire(blocking=False):
            return self._error_state(
                LicenseServiceError(
                    "unbind_in_progress",
                    "正在解绑当前设备，请勿重复点击",
                )
            )
        try:
            credentials = self.store.load_credentials()
            if not credentials:
                raise LicenseServiceError(
                    "license_not_activated",
                    "本机没有可解绑的设备授权",
                )
            machine_code = self.store.get_or_create_machine_code()
            context = self.store.load_activation_context()
            metadata = self.store.load_metadata()
            code_id = str(
                context.get("code_id") or metadata.get("code_id") or ""
            ).strip()
            response = self.client.unbind(
                credentials,
                machine_code=machine_code,
                code_id=code_id,
            )
            if str(response.get("binding_status") or "").strip().lower() != "unbound":
                raise LicenseServiceError(
                    "invalid_unbind_response",
                    "授权服务器未确认设备已解绑，本机仍保持原授权状态",
                )
            metadata.update(
                {
                    "app_name": LICENSE_APP_NAME,
                    "binding_status": "unbound",
                    "license_status": str(
                        response.get("license_status")
                        or metadata.get("license_status")
                        or "active"
                    ),
                    "last_verified_at": _now_text(),
                }
            )
            for field in (
                "transfer_count",
                "self_transfers_used_30d",
                "self_transfers_remaining_30d",
            ):
                if response.get(field) not in (None, ""):
                    metadata[field] = response.get(field)
            # Preserve server-issued activated_at/expires_at and all display
            # metadata.  Only the current device credentials are removed.
            self.store.clear_credentials()
            self.store.save_metadata(metadata)
            with self._lock:
                self._authorized = False
            message = str(response.get("message") or "当前设备已解绑")
            state = self._inactive_state(message)
            state["license"] = metadata
            with self._lock:
                self._last_public_state = dict(state)
            return state
        except Exception as exc:
            if isinstance(exc, LicenseServiceError) and exc.http_status == 401:
                try:
                    self.store.clear_credentials()
                    metadata = self.store.load_metadata()
                    metadata.update(
                        {
                            "binding_status": "invalid",
                            "last_verified_at": _now_text(),
                        }
                    )
                    self.store.save_metadata(metadata)
                    with self._lock:
                        self._authorized = False
                except Exception:
                    pass
            state = self._error_state(exc)
            with self._lock:
                self._last_public_state = dict(state)
            return state
        finally:
            self._unbind_lock.release()
