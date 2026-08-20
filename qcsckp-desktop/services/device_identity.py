"""Versioned, privacy-preserving device identity for software licensing.

Raw hardware values are collected only in memory.  Callers receive only the
versioned MC1 device code; raw board, disk, registry and CPU values must never
be persisted, logged or exposed to the WebView.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Mapping


DEVICE_CODE_VERSION = 1
DEVICE_CODE_PREFIX = f"MC{DEVICE_CODE_VERSION}"
_FORMAT_RE = re.compile(r"^MC1-(?:[A-F0-9]{4}-){3}[A-F0-9]{4}$")
_ALLOWED_FEATURES = {
    "BOARD_UUID",
    "SYSTEM_DISK_SERIAL",
    "WINDOWS_MACHINE_GUID",
    "CPU_ID",
    "CPU_VENDOR",
    "CPU_NAME",
    "PLATFORM_UUID",
    "SYSTEM_MACHINE_ID",
    "MAC_PLATFORM_UUID",
    "MAC_HARDWARE_SERIAL",
    "MAC_BOARD_ID",
    "MAC_MODEL",
    "MAC_CPU_BRAND",
    "MAC_CPU_FAMILY",
}
_INVALID_VALUES = {
    "0",
    "0000",
    "NONE",
    "NULL",
    "UNKNOWN",
    "NOTAVAILABLE",
    "NOTAPPLICABLE",
    "DEFAULT",
    "DEFAULTSTRING",
    "SYSTEMSERIALNUMBER",
    "TOBEFILLEDBYOEM",
    "INVALID",
    "UNSPECIFIED",
}
_CACHE_LOCK = threading.Lock()
_CACHED_DEVICE_CODE = ""


class DeviceIdentityError(RuntimeError):
    pass


def _platform_name() -> str:
    return sys.platform


def _clean_feature(value: Any) -> str:
    text = str(value or "").replace("\x00", "").strip().upper()
    compact = re.sub(r"[^A-Z0-9]+", "", text)
    if not compact or compact in _INVALID_VALUES:
        return ""
    if len(compact) < 4:
        return ""
    if len(compact) >= 8 and len(set(compact)) == 1:
        return ""
    if compact in {
        "03000200040005000006000700080009",
        "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
    }:
        return ""
    return compact[:256]


def _powershell_executable() -> str:
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = os.path.join(
        system_root,
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    return candidate if os.path.isfile(candidate) else (shutil.which("powershell.exe") or "")


def _read_windows_cim_features() -> dict[str, Any]:
    executable = _powershell_executable()
    if not executable:
        return {}
    script = r"""
$ErrorActionPreference='SilentlyContinue'
$board=(Get-CimInstance Win32_ComputerSystemProduct | Select-Object -First 1 -ExpandProperty UUID)
$drive=$env:SystemDrive.TrimEnd(':')
$disk=(Get-Partition -DriveLetter $drive | Get-Disk | Select-Object -First 1 -ExpandProperty SerialNumber)
$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1 ProcessorId,Manufacturer,Name
[ordered]@{
  board_uuid=$board
  disk_serial=$disk
  cpu_id=$cpu.ProcessorId
  cpu_vendor=$cpu.Manufacturer
  cpu_name=$cpu.Name
} | ConvertTo-Json -Compress
"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=flags,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return {}
        payload = json.loads(completed.stdout.strip())
        return dict(payload) if isinstance(payload, Mapping) else {}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {}


def _read_windows_machine_guid() -> str:
    try:
        import winreg

        access = winreg.KEY_READ
        if hasattr(winreg, "KEY_WOW64_64KEY"):
            access |= winreg.KEY_WOW64_64KEY
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            access,
        ) as key:
            return str(winreg.QueryValueEx(key, "MachineGuid")[0] or "")
    except (OSError, ImportError, AttributeError, TypeError):
        return ""


def _run_text(command: list[str], *, timeout: float = 6.0) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _read_macos_features() -> dict[str, Any]:
    """Read stable Apple hardware identifiers without network-derived data."""
    output = _run_text(
        ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        timeout=8,
    )

    def quoted(name: str) -> str:
        matched = re.search(
            rf'"{re.escape(name)}"\s*=\s*"([^"]+)"',
            output,
        )
        return matched.group(1) if matched else ""

    board_match = re.search(r'"board-id"\s*=\s*<([0-9A-Fa-f]+)>', output)
    return {
        "MAC_PLATFORM_UUID": quoted("IOPlatformUUID"),
        "MAC_HARDWARE_SERIAL": quoted("IOPlatformSerialNumber"),
        "MAC_BOARD_ID": board_match.group(1) if board_match else quoted("board-id"),
        "MAC_MODEL": _run_text(["sysctl", "-n", "hw.model"]),
        "MAC_CPU_BRAND": _run_text(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "MAC_CPU_FAMILY": _run_text(["sysctl", "-n", "hw.cpufamily"]),
    }


def _read_non_windows_features() -> dict[str, Any]:
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return {"SYSTEM_MACHINE_ID": value}
        except OSError:
            continue
    return {}


def _collect_raw_device_features() -> dict[str, str]:
    """Internal-only hardware collection. Never return this through an API."""
    raw: dict[str, Any] = {}
    if _platform_name() == "darwin":
        try:
            raw.update(_read_macos_features())
        except Exception:
            pass
    elif os.name == "nt":
        try:
            cim = _read_windows_cim_features()
        except Exception:
            cim = {}
        try:
            machine_guid = _read_windows_machine_guid()
        except Exception:
            machine_guid = ""
        raw.update(
            {
                "BOARD_UUID": cim.get("board_uuid"),
                "SYSTEM_DISK_SERIAL": cim.get("disk_serial"),
                "WINDOWS_MACHINE_GUID": machine_guid,
                "CPU_ID": cim.get("cpu_id"),
                "CPU_VENDOR": cim.get("cpu_vendor"),
                "CPU_NAME": cim.get("cpu_name"),
            }
        )
    else:
        try:
            raw.update(_read_non_windows_features())
        except Exception:
            pass
    cleaned: dict[str, str] = {}
    for name, value in raw.items():
        if name not in _ALLOWED_FEATURES:
            continue
        normalized = _clean_feature(value)
        if normalized:
            cleaned[name] = normalized
    return cleaned


def _checksum(payload: str) -> str:
    return hashlib.sha256(
        f"{DEVICE_CODE_PREFIX}|{payload}|CHECK".encode("ascii")
    ).hexdigest().upper()[:4]


def generate_device_code(features: Mapping[str, Any] | None = None) -> str:
    source = dict(features) if features is not None else _collect_raw_device_features()
    normalized: list[str] = []
    for name, value in source.items():
        key = str(name or "").strip().upper()
        if key not in _ALLOWED_FEATURES:
            continue
        cleaned = _clean_feature(value)
        if cleaned:
            normalized.append(f"{key}={cleaned}")
    if not normalized:
        raise DeviceIdentityError("未取得可用的设备特征")
    canonical = "\n".join(sorted(set(normalized)))
    digest = hashlib.sha256(
        f"QCSCKP|{DEVICE_CODE_PREFIX}\n{canonical}".encode("utf-8")
    ).hexdigest().upper()
    payload = digest[:12]
    compact = payload + _checksum(payload)
    return f"{DEVICE_CODE_PREFIX}-" + "-".join(
        compact[index : index + 4] for index in range(0, 16, 4)
    )


def validate_device_code(value: Any) -> bool:
    code = str(value or "").strip().upper()
    if not _FORMAT_RE.fullmatch(code):
        return False
    compact = code.replace("-", "")[3:]
    payload, provided_checksum = compact[:12], compact[12:]
    return hmac.compare_digest(provided_checksum, _checksum(payload))


def get_authorization_device_fingerprint() -> str:
    global _CACHED_DEVICE_CODE
    with _CACHE_LOCK:
        if validate_device_code(_CACHED_DEVICE_CODE):
            return _CACHED_DEVICE_CODE
        _CACHED_DEVICE_CODE = generate_device_code()
        return _CACHED_DEVICE_CODE


__all__ = [
    "DEVICE_CODE_VERSION",
    "DEVICE_CODE_PREFIX",
    "DeviceIdentityError",
    "generate_device_code",
    "validate_device_code",
    "get_authorization_device_fingerprint",
]
