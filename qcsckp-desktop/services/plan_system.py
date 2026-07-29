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
    # 乘方是更具体的计划体系标记。响应里偶尔会同时包含历史全域字段，
    # 因此必须先完整检查乘方证据，不能依赖字典字段顺序。
    for key, value in direct_values:
        if isinstance(value, bool) and "cheng" in key:
            if value:
                return "chengfang"
        text = str(value or "").strip().lower()
        if "乘方" in text or "chengfang" in text or "cheng_fang" in text:
            return "chengfang"
    for key, value in direct_values:
        if isinstance(value, bool) and "cheng" in key:
            return "global"
        text = str(value or "").strip().lower()
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
    if "千川乘方" in visible or "乘方计划" in visible or "乘方" in visible:
        return "chengfang"
    if (
        "传统全域" in visible
        or "全域计划（旧版）" in visible
        or "直播全域" in visible
        or "全域计划" in visible
    ):
        return "global"
    # “商品全域推广”是推广场景名称，并不能单独证明计划属于传统全域；
    # 需要“全域计划”等明确体系文案，或结构化字段，才判定为 global。
    return "unknown"


async def confirm_live_page_plan_system(
    page: Any,
    *,
    expected_plan_system: Any,
    aavid: Any,
    ad_id: Any,
) -> str:
    """用当前直播计划的精确详情响应复核体系；通过返回空串，否则返回原因。"""
    expected = normalize_plan_system(expected_plan_system or "unknown")
    if expected == "unknown":
        return "直播计划体系尚未确认，已安全停止"
    payload: Any = None
    try:
        payload = await page.evaluate(
            """async ({ aavid, adId }) => {
                const query = new URLSearchParams({ aavid, adid: adId });
                const response = await fetch(
                    `/ad/api/creation/v1/ad/ad-detail-basic?${query.toString()}`,
                    { credentials: "include" }
                );
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return await response.json();
            }""",
            {"aavid": str(aavid), "adId": str(ad_id)},
        )
    except Exception:
        payload = None
    page_text = ""
    try:
        page_text = await page.locator("body").inner_text(timeout=10_000)
    except Exception:
        page_text = ""
    actual = detect_plan_system(page_text=page_text, payload=payload)
    if actual == "unknown":
        return "直播计划体系无法从千川详情响应或页面中确认，已安全停止"
    if actual != expected:
        return f"直播计划体系不匹配：配置为 {expected}，页面实际为 {actual}"
    return ""
