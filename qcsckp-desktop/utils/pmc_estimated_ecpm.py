"""
预估 ECPM：与大屏 ``dashboard.get_table_data`` 公式一致。
CPA 由当前批次内全部行的整体成交金额、整体成交订单数与 ``pmc_ad_detail_basic.ecp_roi2_goal``（按 aadvid）汇总；
CTR/CVR 取单行素材。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _rate_to_ratio(v: Any) -> float:
    """点击率/转化率：>1 视为百分比，否则视为 0~1 小数。"""
    x = _to_float(v)
    if x <= 0:
        return 0.0
    if x > 1.0:
        return x / 100.0
    return x


def attach_estimated_ecpm(
    rows: List[Dict[str, Any]],
    db: Any,
    *,
    gmv_key: str = "pay_gmv_include_coupon",
    orders_key: str = "overall_order_count",
    ctr_key: str = "overall_ctr",
    cvr_key: str = "overall_conversion_rate",
    aadvid_key: str = "aadvid",
    out_key: str = "estimated_ecpm",
) -> None:
    """
    就地写入每行 ``out_key``；无法计算时写入 None。

    :param db: ``SQLiteStore`` 实例（含 ``select``）。
    """
    if not rows:
        return

    total_gmv = sum(_to_float(r.get(gmv_key)) for r in rows)
    total_orders = sum(int(_to_float(r.get(orders_key))) for r in rows)

    aadvid = next(
        (str(r.get(aadvid_key)).strip() for r in rows if r.get(aadvid_key)),
        None,
    )
    target_roi: Optional[float] = None
    if aadvid:
        ad_rows = db.select(
            table="pmc_ad_detail_basic",
            fields=["ecp_roi2_goal"],
            where={"aadvid": aadvid},
            limit=1,
        )
        if ad_rows:
            raw = ad_rows[0].get("ecp_roi2_goal")
            if raw is not None:
                target_roi = _to_float(raw)
                if target_roi <= 0:
                    target_roi = None

    cpa: Optional[float] = None
    if total_orders > 0 and target_roi and target_roi > 0:
        cpa = (total_gmv / float(total_orders)) / target_roi

    for r in rows:
        if cpa is None or cpa <= 0:
            r[out_key] = None
            continue
        ctr = _rate_to_ratio(r.get(ctr_key))
        cvr = _rate_to_ratio(r.get(cvr_key))
        r[out_key] = round(cvr * ctr * cpa * 1000.0, 4)
