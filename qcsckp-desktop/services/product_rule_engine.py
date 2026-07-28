"""商品全域的商品汇总、规则命中与候选素材选择。"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from api.rule_retargeting_config import evaluate_trigger


ADDITIVE_METRICS = (
    "currentCost",
    "costDiff",
    "netAmount",
    "overallAmount",
    "netOrderCount",
    "overallOrderCount",
    "overallShowCount",
    "overallClickCount",
)


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _safe_ratio(numerator: Any, denominator: Any) -> Optional[float]:
    den = _number(denominator)
    if den <= 0:
        return None
    return _number(numerator) / den


def normalize_product_ids(value: Any) -> List[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = [x.strip() for x in text.split(",") if x.strip()]
    if not isinstance(value, (list, tuple, set)):
        return []
    out: List[str] = []
    seen = set()
    for item in value:
        product_id = str(item or "").strip()
        if product_id and product_id not in seen:
            seen.add(product_id)
            out.append(product_id)
    return out


def product_ids_for_material(
    row: Mapping[str, Any],
    relation_map: Optional[Mapping[str, Sequence[Any]]] = None,
) -> List[str]:
    material_id = str(row.get("id") or row.get("material_id") or "").strip()
    direct = normalize_product_ids(
        row.get("product_ids")
        if row.get("product_ids") is not None
        else row.get("product_ids_json")
    )
    if direct:
        return direct
    if relation_map and material_id in relation_map:
        return normalize_product_ids(relation_map[material_id])
    return []


def aggregate_product_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    relation_map: Optional[Mapping[str, Sequence[Any]]] = None,
    product_names: Optional[Mapping[str, str]] = None,
    allowed_product_ids: Optional[Sequence[Any]] = None,
) -> List[Dict[str, Any]]:
    """按商品聚合素材；一条素材关联多个商品时分别进入对应商品。"""
    allowed = set(normalize_product_ids(allowed_product_ids)) if allowed_product_ids else None
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        material_id = str(row.get("id") or row.get("material_id") or "").strip()
        if not material_id:
            continue
        product_ids = product_ids_for_material(row, relation_map)
        for product_id in product_ids:
            if allowed is not None and product_id not in allowed:
                continue
            group = groups.setdefault(
                product_id,
                {
                    "id": product_id,
                    "productId": product_id,
                    "productName": str((product_names or {}).get(product_id) or ""),
                    "materialCount": 0,
                    "materialIds": [],
                    "materials": [],
                    **{metric: 0.0 for metric in ADDITIVE_METRICS},
                },
            )
            group["materialCount"] += 1
            group["materialIds"].append(material_id)
            group["materials"].append(row)
            for metric in ADDITIVE_METRICS:
                group[metric] += _number(row.get(metric))

    result: List[Dict[str, Any]] = []
    for group in groups.values():
        cost = group["costDiff"]
        overall_amount = group["overallAmount"]
        net_amount = group["netAmount"]
        shows = group["overallShowCount"]
        clicks = group["overallClickCount"]
        orders = group["overallOrderCount"]
        net_roi = _safe_ratio(net_amount, cost)
        overall_roi = _safe_ratio(overall_amount, cost)
        settle_rate = _safe_ratio(net_amount, overall_amount)
        ctr = _safe_ratio(clicks, shows)
        conversion = _safe_ratio(orders, clicks)
        estimated_ecpm = _safe_ratio(cost * 1000.0, shows)
        refund_rate = None if settle_rate is None else max(0.0, min(1.0, 1.0 - settle_rate))
        group.update(
            {
                "netRoi": net_roi,
                "overallPayRoi": overall_roi,
                "netSettleRate": settle_rate,
                "hourRefundRate": refund_rate,
                "overallCtr": ctr,
                "overallConversionRate": conversion,
                "estimatedEcpm": estimated_ecpm,
            }
        )
        result.append(group)
    result.sort(key=lambda item: (str(item.get("productName") or ""), str(item["productId"])))
    return result


def material_net_roi_sort_key(row: Mapping[str, Any]) -> tuple:
    """可投优先、净成交 ROI 高优先；再按成交额，最后按素材 ID 稳定排序。"""
    cost = _number(row.get("costDiff"))
    roi_raw = row.get("netRoi")
    try:
        roi = float(roi_raw)
        valid_roi = math.isfinite(roi) and cost > 0
    except (TypeError, ValueError):
        roi, valid_roi = 0.0, False
    amount = _number(row.get("netAmount"))
    material_id = str(row.get("id") or row.get("material_id") or "")
    return (0 if valid_roi else 1, -roi if valid_roi else 0.0, -amount, material_id)


def select_product_candidates(
    product_row: Mapping[str, Any],
    *,
    candidate_trigger: Optional[Dict[str, Any]] = None,
    limit: Any = 1,
) -> List[Dict[str, Any]]:
    materials = [
        dict(row)
        for row in (product_row.get("materials") or [])
        if isinstance(row, dict)
    ]
    if isinstance(candidate_trigger, dict):
        materials = [row for row in materials if evaluate_trigger(candidate_trigger, row)]
    try:
        max_count = int(limit)
    except (TypeError, ValueError):
        max_count = 1
    max_count = max(1, min(max_count, 20))
    materials.sort(key=material_net_roi_sort_key)
    return materials[:max_count]


def evaluate_product_strategy(
    rows: Iterable[Dict[str, Any]],
    strategy: Dict[str, Any],
    *,
    relation_map: Optional[Mapping[str, Sequence[Any]]] = None,
    product_names: Optional[Mapping[str, str]] = None,
    allowed_product_ids: Optional[Sequence[Any]] = None,
) -> List[Dict[str, Any]]:
    """返回命中的商品及其候选素材，候选为空的商品不会进入结果。"""
    trigger = strategy.get("trigger") if isinstance(strategy, dict) else {}
    candidate_trigger = strategy.get("candidate_trigger") if isinstance(strategy, dict) else None
    limit = strategy.get("candidate_limit", 1) if isinstance(strategy, dict) else 1
    products = aggregate_product_rows(
        rows,
        relation_map=relation_map,
        product_names=product_names,
        allowed_product_ids=allowed_product_ids,
    )
    hits: List[Dict[str, Any]] = []
    for product in products:
        if not isinstance(trigger, dict) or not evaluate_trigger(trigger, product):
            continue
        candidates = select_product_candidates(
            product,
            candidate_trigger=candidate_trigger if isinstance(candidate_trigger, dict) else None,
            limit=limit,
        )
        if not candidates:
            continue
        out = dict(product)
        out["candidates"] = candidates
        hits.append(out)
    return hits
