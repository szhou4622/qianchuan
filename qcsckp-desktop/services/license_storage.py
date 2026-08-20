"""Secure, per-device storage for the license protocol v2 client.

Opaque device credentials, the original activation code, and the stable random
machine code are stored only in the platform credential facility.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from config import (
    LICENSE_APP_NAME,
    LICENSE_CREDENTIAL_FILE,
    LICENSE_MACHINE_CODE_FILE,
    LICENSE_METADATA_FILE,
)


_KEYCHAIN_SERVICE = f"com.dadaozixun.{LICENSE_APP_NAME.lower()}.license-v2"
_CREDENTIAL_ACCOUNT = "device-credentials"
_MACHINE_ACCOUNT = "machine-code"
_WINDOWS_DESCRIPTION = f"{LICENSE_APP_NAME} License Protocol v2"
_LICENSE_SNAPSHOT_FIELDS = {
    "app_name",
    "code_id",
    "license_type",
    "license_type_label",
    "duration_days",
    "activated_at",
    "expires_at",
    "is_permanent",
}


class LicenseStorageError(RuntimeError):
    pass


def _atomic_write_text(path: str, value: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def _dpapi_protect(cleartext: str) -> str:
    if os.name != "nt":
        raise LicenseStorageError("DPAPI is only available on Windows")
    try:
        import win32crypt

        encrypted = win32crypt.CryptProtectData(
            cleartext.encode("utf-8"),
            _WINDOWS_DESCRIPTION,
            None,
            None,
            None,
            0,
        )
        return base64.b64encode(encrypted).decode("ascii")
    except Exception as exc:
        raise LicenseStorageError("无法使用 Windows DPAPI 保存授权凭证") from exc


def _dpapi_unprotect(ciphertext: str) -> str:
    if os.name != "nt":
        raise LicenseStorageError("DPAPI is only available on Windows")
    try:
        import win32crypt

        encrypted = base64.b64decode(ciphertext.encode("ascii"), validate=True)
        return win32crypt.CryptUnprotectData(
            encrypted,
            None,
            None,
            None,
            0,
        )[1].decode("utf-8")
    except Exception as exc:
        raise LicenseStorageError("本机授权凭证无法解密") from exc


def _mac_keyring():
    try:
        import keyring

        backend = keyring.get_keyring()
    except Exception as exc:
        raise LicenseStorageError("macOS Keychain 组件不可用") from exc
    module_name = type(backend).__module__.lower()
    class_name = type(backend).__name__.lower()
    if "macos" not in module_name and "keychain" not in class_name:
        raise LicenseStorageError("当前凭据后端不是 macOS Keychain")
    return keyring


class LicenseSecureStore:
    def __init__(
        self,
        *,
        credential_file: str = LICENSE_CREDENTIAL_FILE,
        machine_code_file: str = LICENSE_MACHINE_CODE_FILE,
        metadata_file: str = LICENSE_METADATA_FILE,
        platform_name: Optional[str] = None,
    ) -> None:
        self.credential_file = credential_file
        self.machine_code_file = machine_code_file
        self.metadata_file = metadata_file
        self.platform_name = platform_name or sys.platform

    @property
    def _is_macos(self) -> bool:
        return self.platform_name == "darwin"

    def load_credentials(self) -> Optional[dict[str, str]]:
        payload = self._load_secret_payload()
        session = str(payload.get("device_session") or "").strip()
        credential = str(payload.get("device_credential") or "").strip()
        if not session or not credential:
            return None
        return {
            "device_session": session,
            "device_credential": credential,
        }

    def _load_secret_payload(self) -> dict[str, Any]:
        try:
            if self._is_macos:
                raw = _mac_keyring().get_password(
                    _KEYCHAIN_SERVICE,
                    _CREDENTIAL_ACCOUNT,
                )
            else:
                path = Path(self.credential_file)
                if not path.is_file():
                    return {}
                raw = _dpapi_unprotect(path.read_text(encoding="ascii").strip())
            if not raw:
                return {}
            payload = json.loads(raw)
            if not isinstance(payload, Mapping):
                return {}
            return dict(payload)
        except LicenseStorageError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise LicenseStorageError("本机授权凭证读取失败") from exc

    def load_activation_context(self) -> dict[str, str]:
        payload = self._load_secret_payload()
        return {
            "activation_code": str(payload.get("activation_code") or "").strip(),
            "code_id": str(payload.get("code_id") or "").strip(),
            "machine_code": str(payload.get("machine_code") or "").strip(),
        }

    def load_license_snapshot(self) -> dict[str, Any]:
        payload = self._load_secret_payload()
        snapshot = payload.get("license_snapshot")
        if not isinstance(snapshot, Mapping):
            return {}
        return {
            key: snapshot.get(key)
            for key in _LICENSE_SNAPSHOT_FIELDS
            if key in snapshot
        }

    def _save_secret_payload(self, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if self._is_macos:
            try:
                _mac_keyring().set_password(
                    _KEYCHAIN_SERVICE,
                    _CREDENTIAL_ACCOUNT,
                    raw,
                )
            except LicenseStorageError:
                raise
            except Exception as exc:
                raise LicenseStorageError("无法写入 macOS Keychain") from exc
            return
        _atomic_write_text(self.credential_file, _dpapi_protect(raw))

    def save_credentials(self, payload: Mapping[str, Any]) -> None:
        session = str(payload.get("device_session") or "").strip()
        credential = str(payload.get("device_credential") or "").strip()
        if not session or not credential:
            raise LicenseStorageError("授权服务未返回完整设备凭证")
        existing = self._load_secret_payload()
        existing.update(
            {
                "device_session": session,
                "device_credential": credential,
            }
        )
        for name in ("activation_code", "code_id", "machine_code"):
            value = str(payload.get(name) or "").strip()
            if value:
                existing[name] = value
        self._save_secret_payload(existing)

    def save_license_snapshot(self, payload: Mapping[str, Any]) -> None:
        snapshot = {
            key: payload.get(key)
            for key in _LICENSE_SNAPSHOT_FIELDS
            if key in payload
        }
        if not snapshot.get("app_name") or not snapshot.get("code_id"):
            raise LicenseStorageError("授权服务未返回完整的加密授权快照")
        existing = self._load_secret_payload()
        existing["license_snapshot"] = snapshot
        self._save_secret_payload(existing)

    def clear_credentials(self) -> None:
        existing = self._load_secret_payload()
        existing.pop("device_session", None)
        existing.pop("device_credential", None)
        preserved = {
            key: existing.get(key)
            for key in (
                "activation_code",
                "code_id",
                "machine_code",
                "license_snapshot",
            )
            if (
                isinstance(existing.get(key), Mapping)
                or str(existing.get(key) or "").strip()
            )
        }
        if preserved:
            self._save_secret_payload(preserved)
            return
        if self._is_macos:
            try:
                _mac_keyring().delete_password(
                    _KEYCHAIN_SERVICE,
                    _CREDENTIAL_ACCOUNT,
                )
            except Exception:
                pass
            return
        try:
            Path(self.credential_file).unlink(missing_ok=True)
        except OSError as exc:
            raise LicenseStorageError("无法清除本机授权凭证") from exc

    def get_or_create_machine_code(self) -> str:
        if self._is_macos:
            keyring = _mac_keyring()
            value = keyring.get_password(_KEYCHAIN_SERVICE, _MACHINE_ACCOUNT)
            if value:
                return str(value)
            value = f"machine_{uuid.uuid4().hex}"
            keyring.set_password(_KEYCHAIN_SERVICE, _MACHINE_ACCOUNT, value)
            return value

        path = Path(self.machine_code_file)
        if path.is_file():
            value = _dpapi_unprotect(path.read_text(encoding="ascii").strip())
            if value:
                return value
        value = f"machine_{uuid.uuid4().hex}"
        _atomic_write_text(self.machine_code_file, _dpapi_protect(value))
        return value

    def load_metadata(self) -> dict[str, Any]:
        try:
            payload = json.loads(Path(self.metadata_file).read_text(encoding="utf-8"))
            return dict(payload) if isinstance(payload, Mapping) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def save_metadata(self, payload: Mapping[str, Any]) -> None:
        # This file contains display-only server metadata.  Credentials and
        # activation codes are intentionally excluded by construction.
        allowed = {
            "app_name",
            "code_id",
            "binding_status",
            "license_type",
            "license_type_label",
            "duration_days",
            "activated_at",
            "expires_at",
            "remaining_days",
            "is_permanent",
            "transfer_count",
            "self_transfers_used_30d",
            "self_transfers_remaining_30d",
            "action",
            "grant_score",
            "current_device",
            "license_status",
            "last_verified_at",
        }
        clean = {key: payload.get(key) for key in allowed if key in payload}
        _atomic_write_text(
            self.metadata_file,
            json.dumps(clean, ensure_ascii=False, sort_keys=True),
        )
