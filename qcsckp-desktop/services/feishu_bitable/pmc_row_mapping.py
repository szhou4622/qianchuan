"""
将本地 SQLite 表 ``pmc_promotion_material`` 的行（英文列名、与千川接口字段一致）
映射为飞书多维表格写入所需的「列显示名」→ 值。

推送飞书时**只写入** ``DEFAULT_PMC_MATERIAL_TO_FEISHU`` 中出现的本地字段；
顺序固定为：**创建时间**在**素材ID**前，其余键按字典定义顺序（勿在业务侧再塞未映射字段）。

``FeishuBaseOperator.add_rows`` 的 key 必须是飞书上的列显示名；
若你直接用 SQLite 的 dict 推送，请先 ``map_pmc_material_row_to_feishu``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Union

# SQLite 列名 -> 飞书列显示名（需与多维表格列标题完全一致）
# 字典键顺序 = 推送到飞书的列顺序（Python 3.7+ 保留插入顺序）
DEFAULT_PMC_MATERIAL_TO_FEISHU: Dict[str, str] = {
    "created_at": "创建时间",
    "material_id": "素材ID",
    "video_name": "视频名称",
    "video_type": "视频类型",
    "tag_list": "标签",
    "upload_time": "视频上传时间",
    "stat_cost": "整体消耗(元)",
    "prepay_pay_settle_1h": "净成交ROI",
    "order_settle_amount_1h": "净成交金额(元)",
    "refund_rate_1h": "1小时内退款率",
    "prepay_pay_order_count": "整体成交ROI",
    "pay_gmv_include_coupon": "整体成交金额(元)",
    "order_settle_rate_1h": "净成交结算率",
    "order_settle_count_1h": "净成交订单数",
    "overall_order_count": "整体成交订单数",
    "overall_show_count": "整体展现次数",
    "overall_click_count": "整体点击次数",
    "overall_ctr": "整体点击率",
    "overall_conversion_rate": "整体转化率",
}

# 与 DEFAULT_PMC_MATERIAL_TO_FEISHU 列顺序一致；供飞书 ensure_headers 按位对齐（首列即主键「文本」→「创建时间」）
PMC_FEISHU_COLUMN_HEADERS: List[str] = list(DEFAULT_PMC_MATERIAL_TO_FEISHU.values())


def pmc_feishu_column_headers(local_to_feishu: Optional[Dict[str, str]] = None) -> List[str]:
    """合并自定义映射后，返回本次同步要使用的飞书列显示名顺序（与 map 输出键一致）。"""
    merged: Dict[str, str] = dict(DEFAULT_PMC_MATERIAL_TO_FEISHU)
    if local_to_feishu:
        merged.update(local_to_feishu)
    return list(merged.values())


# 与千川 roi2MaterialVideoType 数值一致（入库多为 int）
VIDEO_TYPE_MAP: Dict[int, str] = {
    1: "全部",
    2: "自选投放视频",
    3: "智能优选视频",
    4: "AIGC动态创意",
}


def _video_type_to_label(value: Any) -> Any:
    """飞书「视频类型」列展示为中文说明，而非原始数字。"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip().isdigit():
        return value
    try:
        n = int(value)
    except (TypeError, ValueError):
        return value
    return VIDEO_TYPE_MAP.get(n, str(value))


def map_pmc_material_row_to_feishu(
    row: Union[Dict[str, Any], Mapping[str, Any]],
    *,
    local_to_feishu: Optional[Dict[str, str]] = None,
    drop_none: bool = True,
    drop_id: bool = True,
) -> Dict[str, Any]:
    """
    仅将 ``DEFAULT_PMC_MATERIAL_TO_FEISHU``（及 ``local_to_feishu`` 覆盖）中出现的本地列
    映射为飞书列名；**未在映射表中的本地键一律不输出**。

    :param row: SQLite Row 或 dict，键为英文列名（与 ``pmc_promotion_material`` 一致）。
    :param local_to_feishu: 覆盖/合并到默认映射；key 为本地列名，value 为飞书列显示名。
    :param drop_none: 值为 None 的键是否丢弃。
    :param drop_id: 是否丢弃本地自增 ``id``（本函数不遍历 id，保留参数兼容调用方）。
    """
    merged: Dict[str, str] = dict(DEFAULT_PMC_MATERIAL_TO_FEISHU)
    if local_to_feishu:
        merged.update(local_to_feishu)

    r = dict(row) if not isinstance(row, dict) else row

    out: Dict[str, Any] = {}
    # 按映射表顺序遍历，保证「创建时间」在「素材ID」前，且只输出白名单字段
    for lk, feishu_name in merged.items():
        if isinstance(lk, str) and lk == "id" and drop_id:
            continue
        if lk not in r:
            continue
        raw = r[lk]
        if lk == "video_type":
            raw = _video_type_to_label(raw)
        if drop_none and raw is None:
            continue
        out[feishu_name] = raw
    return out
