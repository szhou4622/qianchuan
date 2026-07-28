"""传统全域/千川乘方计划体系的保守识别。"""

from __future__ import annotations

import re
from typing import Any, List, Tuple


ALLOWED_PLAN_SYSTEMS = frozenset({"global", "chengfang", "unknown"})


def normalize_plan_system(value: Any, *, allow_unknown: bool = True) -> str:
    system = str(value or "").strip().lower()
    aliases = {
        "full_domain": "global",
        "full-domain": "global",
        "universal": "global",
        "全域": "global",
        "传统全域": "global",
        "千川全域": "global",
        "cheng_fang": "chengfang",
        "cheng-fang": "chengfang",
        "乘方": "chengfang",
        "千川乘方": "chengfang",
        "": "unknown",
        "unconfirmed": "unknown",
        "待确认": "unknown",
    }
    system = aliases.get(system, system)
    if system not in ALLOWED_PLAN_SYSTEMS:
        raise ValueError("plan_system 仅支持 global、chengfang 或 unknown")
    if system == "unknown" and not allow_unknown:
        raise ValueError("计划体系尚未确认是全域还是千川乘方")
    return system


def detect_plan_system(
    *,
    page_text: str = "",
    payload: Any = None,
    explicit_system: Any = None,
) -> str:
    """只依据明确标志识别传统全域/千川乘方；证据不足时返回 unknown。"""
    if explicit_system not in (None, ""):
        return normalize_plan_system(explicit_system)

    direct_keys = {
        "plansystem",
        "plan_system",
        "deliverysystem",
        "delivery_system",
        "promotionsystem",
        "promotion_system",
        "ischengfang",
        "is_chengfang",
        "ischeng_fang",
        "is_cheng_fang",
        "chengfang",
        "cheng_fang",
    }
    direct_values: List[Tuple[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = str(key or "").strip().lower()
                if normalized_key in direct_keys:
                    direct_values.append((normalized_key, child))
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value[:100]:
                walk(child)

    walk(payload)
    for key, value in direct_values:
        if isinstance(value, bool) and "cheng" in key:
            return "chengfang" if value else "global"
        text = str(value or "").strip().lower()
        if "乘方" in text or "chengfang" in text or "cheng_fang" in text:
            return "chengfang"
        if text in {
            "global",
            "full_domain",
            "full-domain",
            "universal",
            "全域",
            "传统全域",
            "千川全域",
        }:
            return "global"

    visible = re.sub(r"\s+", " ", str(page_text or "")).strip().lower()
    if "千川乘方" in visible or "乘方计划" in visible:
        return "chengfang"
    if "传统全域" in visible or "全域计划（旧版）" in visible:
        return "global"
    return "unknown"
