# -*- coding: utf-8 -*-
"""Pure, fail-closed helpers for existing control-task budget increases."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Optional


MONEY_QUANT = Decimal("0.01")
TASK_METRIC_FIELDS = {
    "assistCost": "stat_cost_for_roi2_assist",
    "assistRoi": "total_prepay_and_pay_order_roi2_assist",
}


def _decimal(value: Any, *, field: str) -> Decimal:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        raise ValueError(f"调控任务缺少{field}")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"调控任务{field}不是有效数字") from exc
    if not parsed.is_finite():
        raise ValueError(f"调控任务{field}不是有限数字")
    return parsed


def assist_task_metric_value(metric: str, row: Dict[str, Any]) -> Optional[float]:
    field = TASK_METRIC_FIELDS.get(str(metric or ""))
    if not field:
        return None
    value = row.get(field)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def classify_assist_task(row: Dict[str, Any]) -> str:
    """Return volume/cost_control_roi/cost_control_conversion/unknown.

    Explicit cost-control evidence wins.  Unknown rows remain blocked instead
    of being guessed as volume.
    """
    roi_goal = row.get("ecp_roi2_goal")
    if roi_goal is not None and str(roi_goal).strip() not in {"", "0", "0.0"}:
        return "cost_control_roi"
    action_name = " ".join(
        str(row.get(key) or "")
        for key in ("deep_external_action_name", "external_action_name")
    )
    bid = row.get("bid")
    if "成交" in action_name and bid is not None and str(bid).strip() not in {"", "0", "0.0"}:
        return "cost_control_conversion"
    # A bounded start/end window plus no cost-control target is the evidence
    # currently stored for volume tasks.  Platform contract validation still
    # runs again before any real write.
    if str(row.get("start_time") or "").strip() and str(row.get("end_time") or "").strip():
        return "volume"
    return "unknown"


def calculate_budget_increase(
    row: Dict[str, Any],
    increase: Dict[str, Any],
) -> Dict[str, Any]:
    """Calculate latest-budget + increment without mutating any platform state."""
    current = _decimal(row.get("budget"), field="当前预算")
    if current < 0:
        raise ValueError("调控任务当前预算不能为负数")
    mode = str(increase.get("mode") or "fixed").strip().lower()
    spend: Optional[Decimal] = None
    if mode == "fixed":
        increment = _decimal(increase.get("fixed_amount_yuan"), field="新增金额")
    elif mode == "spend_percentage":
        percentage = _decimal(increase.get("spend_percentage"), field="消耗金额百分比")
        if percentage <= 0 or percentage > Decimal("1000"):
            raise ValueError("消耗金额百分比须在0到1000之间")
        spend = _decimal(row.get("stat_cost_for_roi2_assist"), field="最新调控消耗")
        if spend < 0:
            raise ValueError("最新调控消耗不能为负数")
        increment = spend * percentage / Decimal("100")
    else:
        raise ValueError("追加预算计算方式无效")
    increment = increment.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    if increment <= 0:
        raise ValueError("本次计算出的新增预算必须大于0元")
    current = current.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    new_budget = (current + increment).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    task_kind = classify_assist_task(row)
    if task_kind == "unknown":
        raise ValueError("无法确认调控任务属于放量、ROI控成本还是成交控成本")
    result: Dict[str, Any] = {
        "assist_task_id": str(row.get("assist_task_id") or ""),
        "task_kind": task_kind,
        "mode": mode,
        "current_budget_yuan": float(current),
        "increment_budget_yuan": float(increment),
        "new_budget_yuan": float(new_budget),
        "latest_spend_yuan": float(spend) if spend is not None else None,
        "spend_percentage": (
            float(_decimal(increase.get("spend_percentage"), field="消耗金额百分比"))
            if mode == "spend_percentage"
            else None
        ),
        "extend_hours": None,
    }
    if task_kind == "volume":
        hours = _decimal(increase.get("volume_extend_hours"), field="放量延长时长")
        if hours < Decimal("0.5") or hours > Decimal("24") or hours * 2 != (hours * 2).to_integral_value():
            raise ValueError("放量延长时长须为0.5到24小时且按0.5小时递增")
        result["extend_hours"] = float(hours)
    return result


def budget_increase_fingerprint(
    *,
    target_uid: str,
    strategy_id: str,
    calculation: Dict[str, Any],
) -> str:
    payload = {
        "target_uid": str(target_uid or ""),
        "strategy_id": str(strategy_id or ""),
        "assist_task_id": str(calculation.get("assist_task_id") or ""),
        "current_budget_yuan": calculation.get("current_budget_yuan"),
        "increment_budget_yuan": calculation.get("increment_budget_yuan"),
        "new_budget_yuan": calculation.get("new_budget_yuan"),
        "extend_hours": calculation.get("extend_hours"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

