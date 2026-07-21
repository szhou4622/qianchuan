"""
千川推广数据清洗模块
将接口原始 JSON 转换为可直接入库的标准字典
对应表见 dev_files/pmc_promotion.sql、docs/schema_pmc_roi2_assist_task.sqlite.sql
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# 与 SQLite datetime('now', '+8 hours') 一致，接口 Unix 秒按东八区展示
_TZ_CN = timezone(timedelta(hours=8))


def clean_basic_info(raw_data: dict, aadvid: str) -> dict:
    """
    清洗 basic_info 接口数据，返回可入库的账号基础信息
    aadvid 唯一，使用更新逻辑（upsert）

    Args:
        raw_data: 接口返回的完整 JSON（顶层对象）
        aadvid:   广告主ID（外部传入）

    Returns:
        对应 pmc_account_info 表的入库字典

    使用示例：
        row = clean_basic_info(api_response, "1700364274540551")
        db.upsert("pmc_account_info", row, unique_key="aadvid")
    """
    data = raw_data.get("data", {})
    aggregate_aid = data.get("roi2TcGroupInfo", {}).get("AggregateAid")

    return {
        "aadvid":        aadvid,
        "aggregate_aid": aggregate_aid,
        "user_id":       data.get("userId"),
        "nick_name":     data.get("nickName"),
        "avatar":        data.get("avatar"),
        "show_id":       data.get("showId"),
        "auth_role":     data.get("authRole"),
    }


def clean_pmc_promotion_row(row: dict) -> Optional[dict]:
    """
    清洗 statsData.rows 中的单条记录，返回可入库的标准字典

    Args:
        row: 接口 statsData.rows 中的单条记录

    Returns:
        入库数据字典；若为聚合汇总行（materialId="-2"）则返回 None
    """
    dims    = row.get("dimensions", {})
    metrics = row.get("metrics", {})

    def dv(key):
        """取 dimensions 字段的 value"""
        return dims.get(key, {}).get("value")

    def mv(key):
        """取 metrics 字段的 value"""
        return metrics.get(key, {}).get("value")

    material_id = dv("materialId")

    # 聚合汇总行跳过（materialId="-2" 是 AIGC动态创意素材集合 的合计行）
    if not material_id:
        return None

    # ---------- 上传时间："-" 视为空 ----------
    upload_time_raw = dv("roi2MaterialUploadTime")
    upload_time = upload_time_raw if (upload_time_raw and upload_time_raw != "-") else None

    # ---------- 视频播放信息：拆解各子字段 ----------
    play_info_raw = dv("roi2MaterialVideoPlayInfo")
    try:
        pi = json.loads(play_info_raw) if play_info_raw else {}
    except (json.JSONDecodeError, TypeError):
        pi = {}
    cover = pi.get("CoverImage") or {}
    video_create_time_raw = pi.get("CreateTime")
    video_create_time = video_create_time_raw if video_create_time_raw else None

    # ---------- 标签列表：解析数组后转逗号分隔字符串 ----------
    tag_list_raw = dv("materialTagList")
    try:
        tag_list_obj = json.loads(tag_list_raw) if tag_list_raw else []
    except (json.JSONDecodeError, TypeError):
        tag_list_obj = []
    tag_list = ",".join(tag_list_obj) if tag_list_obj else None

    # ---------- 展示状态原因："null" 字符串视为空 ----------
    show_status_reason = dv("roi2MaterialShowStatusReason")
    if show_status_reason == "null":
        show_status_reason = None

    return {
        # 维度
        "material_id":            material_id,
        "video_name":             dv("roi2MaterialVideoName"),
        "material_status":        dv("roi2MaterialStatus"),
        "show_status":            dv("roi2MaterialShowStatus"),
        "show_status_reason":     show_status_reason,
        "upload_time":            upload_time,
        "video_type":             dv("roi2MaterialVideoType"),
        "tag_list":               tag_list,
        "video_id":               pi.get("VideoId"),
        "aweme_item_id":          pi.get("AwemeItemId"),
        "cover_url":              cover.get("WebUrl"),
        "cover_width":            cover.get("Width"),
        "cover_height":           cover.get("Height"),
        "video_duration":         pi.get("VideoDuration"),
        "video_title":            pi.get("Title"),
        "lego_source":            pi.get("LegoSource"),
        "video_create_time":      video_create_time,
        # 指标
        "stat_cost":              mv("statCostForRoi2"),
        "order_settle_count_1h":  mv("totalOrderSettleCountForRoi21H"),
        "order_settle_amount_1h": mv("totalOrderSettleAmountForRoi21H"),
        "order_settle_rate_1h":   mv("totalOrderSettleAmountRateForRoi21H"),
        "prepay_pay_order_count": mv("totalPrepayAndPayOrderRoi2"),
        "pay_gmv_include_coupon": mv("totalPayOrderGmvIncludeCouponForRoi2"),
        "prepay_pay_settle_1h":   mv("totalPrepayAndPaySettleRoi21H"),
        "refund_rate_1h":         mv("totalRefundOrderGmvForRoi21HRate"),
        "overall_order_count":    mv("totalPayOrderCountForRoi2"),
        "overall_show_count":     mv("liveShowCountForRoi2V2"),
        "overall_click_count":    mv("liveWatchCountForRoi2V2"),
        "overall_ctr":            mv("liveCvrRateForRoi2V2"),
        "overall_conversion_rate": mv("liveConvertRateForRoi2V2"),
    }


def clean_pmc_promotion_data(raw_data: dict) -> list[dict]:
    """
    清洗千川推广接口完整响应数据

    Args:
        raw_data: 接口返回的完整 JSON（顶层对象），或按 materialId 去重的字典 {materialId: row_data}

    Returns:
        可入库的字典列表（已过滤聚合行，保留有效素材记录）

    使用示例：
        rows = clean_pmc_promotion_data(api_response)
        db.insert_many("pmc_promotion_material", rows)
    """
    # 判断是 dict 还是 list
    if isinstance(raw_data, dict):
        # 去重后的 dict 格式：{materialId: row_data}
        raw_rows = list(raw_data.values())
    else:
        # 原始 list 格式
        raw_rows = (
            raw_data
            .get("data", {})
            .get("statsData", {})
            .get("rows", [])
        )

    result = []
    for row in raw_rows:
        cleaned = clean_pmc_promotion_row(row)
        if cleaned is not None:
            result.append(cleaned)

    return result


def _parse_int_optional(val: Any) -> Optional[int]:
    """接口常返回数字字符串，统一为 int 或 None。"""
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        try:
            return int(val)
        except (ValueError, OverflowError):
            return None
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return None


def _unix_ts_to_cn_datetime_str(val: Any) -> Optional[str]:
    """
    接口 Unix 时间戳（秒）→ 东八区 'YYYY-MM-DD HH:mm:ss'，供入库 TEXT 列。
    """
    ts = _parse_int_optional(val)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, _TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return None


def _assist_metric_value(metrics: dict, api_key: str) -> Any:
    block = metrics.get(api_key)
    if not isinstance(block, dict):
        return None
    return block.get("value")


# adStatsMap.*.metrics 的 camelCase 键 -> 表列名（与 mapping.json Name 一致）
_ASSIST_METRIC_KEYS: List[tuple] = [
    ("showCntForRoi2Assist", "show_cnt_for_roi2_assist"),
    ("clickCntForRoi2Assist", "click_cnt_for_roi2_assist"),
    ("ctrForRoi2Assist", "ctr_for_roi2_assist"),
    ("convertRateForRoi2Assist", "convert_rate_for_roi2_assist"),
    ("statCostForRoi2Assist", "stat_cost_for_roi2_assist"),
    ("totalPayOrderCountForRoi2Assist", "total_pay_order_count_for_roi2_assist"),
    ("totalPayOrderGmvIncludeCouponForRoi2Assist", "total_pay_order_gmv_include_coupon_for_roi2_assist"),
    ("totalPrepayAndPayOrderRoi2Assist", "total_prepay_and_pay_order_roi2_assist"),
    ("totalCostPerPayOrderForRoi2Assist", "total_cost_per_pay_order_for_roi2_assist"),
    ("payConvertCostForRoi2Assist", "pay_convert_cost_for_roi2_assist"),
    ("payConvertCntForRoi2Assist", "pay_convert_cnt_for_roi2_assist"),
    ("totalOrderSettleAmountForRoi21HAssist", "total_order_settle_amount_for_roi2_1h_assist"),
    ("totalRefundOrderGmvForRoi21HRateAssist", "total_refund_order_gmv_for_roi2_1h_rate_assist"),
    ("totalPrepayAndPaySettleRoi21HAssist", "total_prepay_and_pay_settle_roi2_1h_assist"),
    ("totalPayOrderGmvForRoi2Assist", "total_pay_order_gmv_for_roi2_assist"),
    ("totalPayOrderCouponAmountForRoi2Assist", "total_pay_order_coupon_amount_for_roi2_assist"),
]


def _assist_materials_to_json(
    assist_materials_map: Optional[dict],
    task_id: str,
) -> Optional[str]:
    """assistTaskMaterialInfoMap[taskId] -> JSON 字符串 [{"material_id","title"},...]"""
    if not assist_materials_map:
        return None
    raw = assist_materials_map.get(task_id)
    if raw is None:
        raw = assist_materials_map.get(str(task_id))
    if not raw or not isinstance(raw, list):
        return None
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        vm = item.get("videoMaterial") or {}
        mid = item.get("materialId") or vm.get("materialId")
        title = vm.get("title")
        if mid is not None or title is not None:
            out.append({"material_id": str(mid) if mid is not None else None, "title": title})
    if not out:
        return None
    return json.dumps(out, ensure_ascii=False)


def clean_pmc_roi2_assist_task_row(
    ad_info: dict,
    *,
    aadvid: str,
    plan_ad_id: str,
    stats_map: Optional[dict] = None,
    assist_materials_map: Optional[dict] = None,
) -> Optional[dict]:
    """
    清洗单条 adInfos 记录 + 对应 adStatsMap / assistTaskMaterialInfoMap，返回可入库字典。

    Args:
        ad_info: adInfos 数组中的单条
        aadvid: 采集上下文广告主 ID（URL aavid，与素材表 aadvid 一致）
        plan_ad_id: 当前页全域投放计划 ID（URL adId，与 aadvid 不同；勿用行内 advId，advId 多为广告主维度）
        stats_map: data.adStatsMap
        assist_materials_map: data.assistTaskMaterialInfoMap

    Returns:
        对应 pmc_roi2_assist_task 的字段字典；缺少 id 时返回 None。
        start/end/modify/create 时间为东八区 YYYY-MM-DD HH:mm:ss 字符串。
    """
    tid = ad_info.get("id")
    if not tid:
        return None
    task_id = str(tid)
    pid = str(plan_ad_id).strip()
    if not pid:
        return None

    metrics: dict = {}
    if stats_map:
        entry = stats_map.get(task_id)
        if entry is None:
            entry = stats_map.get(tid)
        if isinstance(entry, dict):
            metrics = entry.get("metrics") or {}
            if not isinstance(metrics, dict):
                metrics = {}

    row: Dict[str, Any] = {
        "assist_task_id": task_id,
        "aadvid": str(aadvid),
        "ad_id": pid,
        "task_name": ad_info.get("name"),
        "budget": ad_info.get("budget"),
        "bid": ad_info.get("bid"),
        "start_time": _unix_ts_to_cn_datetime_str(ad_info.get("startTime")),
        "end_time": _unix_ts_to_cn_datetime_str(ad_info.get("endTime")),
        "modify_time": _unix_ts_to_cn_datetime_str(ad_info.get("modifyTime")),
        "create_time": _unix_ts_to_cn_datetime_str(ad_info.get("createTime")),
        "order_id": str(ad_info["orderId"]) if ad_info.get("orderId") is not None else None,
        "aggregate_cid": str(ad_info["aggregateCid"]) if ad_info.get("aggregateCid") is not None else None,
        "ecp_roi2_goal": ad_info.get("ecpRoi2Goal"),
        "creator_user_id": str(ad_info["creatorUserId"]) if ad_info.get("creatorUserId") is not None else None,
        "mar_goal": ad_info.get("marGoal"),
        "prom_way": ad_info.get("promWay"),
        "external_action": ad_info.get("externalAction"),
        "external_action_name": ad_info.get("externalActionName"),
        "smart_bid_type": ad_info.get("smartBidType"),
        "budget_mode": ad_info.get("budgetMode"),
        "ad_cost_protect_status": ad_info.get("adCostProtectStatus"),
        "deep_external_action": ad_info.get("deepExternalAction"),
        "lab_ad_type": ad_info.get("labAdType"),
        "deep_external_action_name": ad_info.get("deepExternalActionName"),
        "deep_bid_type": ad_info.get("deepBidType"),
        "qcpx_mode": ad_info.get("qcpxMode"),
        "adlab_mode": ad_info.get("adlabMode"),
        "ad_delivery_type": ad_info.get("adDeliveryType"),
        "ad_delivery_name": ad_info.get("adDeliveryName"),
        "ad_opt_type": ad_info.get("adOptType"),
        "hint_type": ad_info.get("hintType"),
        "learning_phase": ad_info.get("learningPhase"),
        "daily_delivery_seconds": _parse_int_optional(ad_info.get("dailyDeliverySeconds")),
    }

    for api_key, col in _ASSIST_METRIC_KEYS:
        row[col] = _assist_metric_value(metrics, api_key)

    row["assist_materials_json"] = _assist_materials_to_json(assist_materials_map, task_id)
    return row


def clean_pmc_roi2_assist_task_data(raw_data: dict, aadvid: str, plan_ad_id: str) -> List[dict]:
    """
    清洗调控任务列表接口完整响应（含 adInfos + adStatsMap + assistTaskMaterialInfoMap）。

    Args:
        raw_data: 接口顶层 JSON（含 data）
        aadvid: 广告主 ID（URL aavid）
        plan_ad_id: 当前投放计划 ID（URL adId，与 aadvid 不同）

    Returns:
        可写入 pmc_roi2_assist_task 的字典列表

    使用示例：
        rows = clean_pmc_roi2_assist_task_data(api_response, "1700364274540551", "1819317176101401")
        db.insert_or_update("pmc_roi2_assist_task", rows, unique_fields=["assist_task_id"])
    """
    data = raw_data.get("data") or {}
    ad_infos = data.get("adInfos") or []
    stats_map = data.get("adStatsMap") or {}
    assist_materials_map = data.get("assistTaskMaterialInfoMap") or {}

    result: List[dict] = []
    for ad in ad_infos:
        if not isinstance(ad, dict):
            continue
        cleaned = clean_pmc_roi2_assist_task_row(
            ad,
            aadvid=aadvid,
            plan_ad_id=plan_ad_id,
            stats_map=stats_map if isinstance(stats_map, dict) else None,
            assist_materials_map=assist_materials_map if isinstance(assist_materials_map, dict) else None,
        )
        if cleaned is not None:
            result.append(cleaned)
    return result
