"""Standard-library-only redaction for persisted logs and local exports."""
from __future__ import annotations

import re
from typing import Any


_SECRET_NAME = (
    r"(?:app[_ -]?secret|access[_ -]?token|refresh[_ -]?token|"
    r"activation[_ -]?code|device[_ -]?(?:credential|session)|"
    r"api[_ -]?key|password|passwd|poll[_ -]?secret|encrypt[_ -]?key|"
    r"verification[_ -]?token|sessionid|session_id|secret|token)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"']?" + _SECRET_NAME + r"[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}\]]+)"
)
_HEADER = re.compile(
    r"(?im)([\"']?(?:authorization|proxy-authorization|cookie|set-cookie)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]+)"
)


def redact_text(value: Any) -> str:
    text = str(value or "")
    text = _HEADER.sub(r"\1<redacted>", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1<redacted>", text)
    # Keep only code basenames in genuine Python traceback frame syntax.
    # All ordinary paths (including business document/video names) remain
    # fully redacted by the generic rules below.
    text = re.sub(
        r'''(?m)(\bFile\s+["'])([^"'\r\n]+\.py)(["'],\s+line\s+\d+)''',
        lambda match: (
            match.group(1) + '<local-path>/'
            + re.split(r'[\\/]', match.group(2))[-1] + match.group(3)
        ),
        text,
    )
    text = re.sub(r"(?i)https?://[^\s\"'<>]+", "<url>", text)
    text = re.sub(r"(?i)([\"'])[A-Z]:[\\/][^\r\n]*?\1", '"<local-path>"', text)
    text = re.sub(r"(?i)[A-Z]:[\\/][^\s\"'<>]+", "<local-path>", text)
    text = re.sub(r"(?<!\d)\d{12,}(?!\d)", "<business-id>", text)
    return text
