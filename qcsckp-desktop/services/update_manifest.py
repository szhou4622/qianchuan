"""Independent, app-scoped update manifest reader for the desktop client."""

from __future__ import annotations

import json
import platform
import re
import sys
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from config import APP_NAME, CURRENT_VERSION
from release_identity import CHANNEL, BUILD_REVISION


UPDATE_LATEST_ENDPOINT = "https://update.dadaozixun.com/api/update/latest"
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


def _version_parts(value: Any) -> tuple[int, ...]:
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    numbers = re.findall(r"\d+", text)
    return tuple(int(item) for item in numbers) if numbers else (0,)


def _newer(latest: str, current: str) -> bool:
    left = list(_version_parts(latest))
    right = list(_version_parts(current))
    size = max(len(left), len(right))
    left.extend([0] * (size - len(left)))
    right.extend([0] * (size - len(right)))
    return tuple(left) > tuple(right)


def platform_key() -> str:
    if sys.platform == "win32":
        return "windows_x64"
    if sys.platform == "darwin":
        machine = platform.machine().strip().lower()
        return "mac_arm64" if machine in {"arm64", "aarch64"} else "mac_x64"
    return ""


def _platform_value(value: Any, key: str) -> str:
    if isinstance(value, Mapping):
        return str(value.get(key) or "").strip()
    return str(value or "").strip()


def _validate_artifact_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or parsed.hostname != "update.dadaozixun.com":
        raise ValueError("更新包地址不是官方 HTTPS 地址")
    return value


def check_for_update(
    current_version: Optional[str] = None,
    *,
    opener=None,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    current = str(current_version or CURRENT_VERSION).strip()
    key = platform_key()
    if not key:
        return {"success": False, "message": "当前系统不支持在线更新"}
    url = UPDATE_LATEST_ENDPOINT + "?" + urlencode({"app_name": APP_NAME, "channel": CHANNEL})
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-store",
            "User-Agent": f"QCSCKP-Desktop/{CURRENT_VERSION}",
        },
        method="GET",
    )
    open_request = opener or urlopen
    try:
        with open_request(request, timeout=max(2.0, float(timeout_seconds))) as response:
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except HTTPError as exc:
        if int(exc.code or 0) == 404:
            return {
                "success": True,
                "message": "当前版本尚未发布到自动更新通道",
                "data": {
                    "latest_version": current,
                    "has_update": False,
                    "download_url": "",
                    "sha256": "",
                    "notes": [],
                    "force": False,
                },
            }
        raise RuntimeError("更新服务暂时不可用，请稍后重试") from exc
    except (URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
        raise RuntimeError("更新服务暂时无法连接，请稍后重试") from exc
    if not isinstance(payload, Mapping) or str(payload.get("app_name") or "") != APP_NAME:
        raise RuntimeError("更新配置的软件标识不匹配")
    returned_channel = payload.get("channel", "production")
    if returned_channel != CHANNEL:
        raise RuntimeError("更新配置的发布渠道不匹配，已阻止跨渠道更新")
    latest = str(payload.get("version") or "").strip()
    if not latest or _version_parts(latest) == (0,):
        raise RuntimeError("更新配置缺少有效版本号")
    download_url = _platform_value(payload.get("download_url"), key)
    checksum = _platform_value(payload.get("sha256"), key).lower()
    revision = payload.get("build_revision", 1)
    if type(revision) is not int or revision < 1:
        raise RuntimeError("更新构建号无效")
    has_update = CHANNEL != "stable" and (_newer(latest, current) or
        (latest == current and revision > BUILD_REVISION))
    if has_update:
        _validate_artifact_url(download_url)
        if not _SHA256_RE.fullmatch(checksum):
            raise RuntimeError("更新配置缺少有效的 SHA256 校验值")
    notes = payload.get("notes")
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, list):
        notes = []
    return {
        "success": True,
        "message": "发现新版本" if has_update else "当前已经是最新版本",
        "data": {
            "latest_version": latest,
            "has_update": has_update,
            "download_url": download_url if has_update else "",
            "sha256": checksum if has_update else "",
            "notes": [str(item)[:500] for item in notes[:30]],
            "force": bool(payload.get("force")) if has_update else False,
            "min_supported_version": str(payload.get("min_supported_version") or ""),
            "platform": key,
            "channel": CHANNEL,
            "build_revision": revision,
        },
    }

