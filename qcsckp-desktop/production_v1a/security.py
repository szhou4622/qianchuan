"""V1A 安全边界：凭据保护、脱敏和千川网络写入熔断。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping


class PlatformWriteBlocked(RuntimeError):
    """任何 V1A 千川真实写请求都会抛出此异常。"""

    def __init__(self, method: str, endpoint_path: str, reason: str = "v1a_read_only"):
        self.method = method.upper()
        self.endpoint_path = endpoint_path
        self.reason = reason
        super().__init__(
            f"V1A blocked platform request: {self.method} {self.endpoint_path} ({reason})"
        )


@dataclass(frozen=True)
class ReadContract:
    method: str
    endpoint_pattern: str

    def matches(self, method: str, endpoint_path: str) -> bool:
        return self.method == method.upper() and bool(
            re.fullmatch(self.endpoint_pattern, endpoint_path)
        )


READ_ONLY_CONTRACTS = (
    ReadContract("GET", r"/ad/api/v1/account/user/info"),
    ReadContract("GET", r"/ad/api/creation/v1/ad/ad-detail-(?:basic|plus)"),
    ReadContract("POST", r"/ad/api/pmc/v1/uni-promotion/ad/(?:list-required|list-summary|list-optional|query_ad_ids_by_cond)"),
    ReadContract("POST", r"/ad/api/pmc/v1/uni-promotion/material/(?:list-required|list-optional)"),
    ReadContract("GET", r"/ad/api/pmc/v1/uni-promotion/material/get-ad-material-counts"),
    ReadContract("GET", r"/ad/api/creation/v1/ad/blockVideoMaterial"),
    ReadContract("POST", r"/ad/api/pmc/v1/standard/get_summary_info"),
    ReadContract("GET", r"/ad/api/creation/v1/shop-prom/get-config(?:-v2)?"),
    ReadContract("POST", r"/ad/api/data/v1/common/(?:statQuery|getUserConfAndDataSet)"),
    ReadContract("GET", r"/ad/api/data/v1/common/metric/templates"),
    ReadContract("GET", r"/ad/api/pmc/v1/uni-promotion/ad/get-assist-task-total-budget"),
    ReadContract("GET", r"/ad/api/pmc/v1/ad/get_opt_log"),
)


class PlatformNetworkGuard:
    """只允许证据文档列出的千川只读业务请求。"""

    def __init__(self, on_block: Callable[[dict[str, Any]], None] | None = None):
        self._on_block = on_block

    def assert_allowed(self, method: str, endpoint_path: str) -> None:
        normalized = endpoint_path.split("?", 1)[0]
        if any(c.matches(method, normalized) for c in READ_ONLY_CONTRACTS):
            return
        event = {
            "event": "platform_write_guard_blocked",
            "method": method.upper(),
            "endpoint_path": normalized,
            "reason": "not_in_v1a_read_allowlist",
        }
        if self._on_block:
            self._on_block(event)
        raise PlatformWriteBlocked(method, normalized, event["reason"])


SENSITIVE_KEY_RE = re.compile(
    r"(?:cookie|token|secret|csrf|signature|authorization|password|storage_state)",
    re.IGNORECASE,
)

SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(r"(?i)((?:app_?secret|access_?token|refresh_?token|csrf|cookie|password)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)([?&](?:token|secret|signature|authorization|password)=)[^&\s]+"),
)


def sanitize_exception_text(value: Any, limit: int = 1000) -> str:
    """持久化或返回异常前统一去除常见凭据文本。"""

    text = str(value)
    for pattern in SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text[:limit]


def redact_mapping(value: Any) -> Any:
    """递归删除诊断快照中的凭据和签名资源参数。"""

    if isinstance(value, Mapping):
        return {
            str(k): "[REDACTED]" if SENSITIVE_KEY_RE.search(str(k)) else redact_mapping(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(item) for item in value)
    if isinstance(value, str) and (
        "X-Amz-Signature=" in value
        or "x-expires=" in value.lower()
        or "authorization=" in value.lower()
    ):
        return value.split("?", 1)[0]
    return value


def stable_json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def protect_for_current_windows_user(cleartext: str) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    import win32crypt

    protected = win32crypt.CryptProtectData(
        cleartext.encode("utf-8"),
        "QCSCKP production-v1a",
        None,
        None,
        None,
        0,
    )
    return base64.b64encode(protected).decode("ascii")


def unprotect_for_current_windows_user(ciphertext: str) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    import win32crypt

    raw = base64.b64decode(ciphertext.encode("ascii"))
    return win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1].decode("utf-8")
