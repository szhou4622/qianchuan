"""把官方 API 响应规范化为 v0.1.46 现有业务字段。"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from utils.operation_log_identity import (
    operation_log_content_items,
    operation_log_control_task_id,
)


def text_id(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def require_digit_id(value: Any, field: str) -> str:
    result = text_id(value)
    if not result.isdigit():
        raise ValueError(f"{field} 必须为完整数字 ID")
    return result


def id_number(value: Any, field: str) -> int:
    """Python int 可无损承载千川长 ID，JSON 序列化不会发生 JS Number 精度丢失。"""
    return int(require_digit_id(value, field))


def first(mapping: Any, *keys: str, default: Any = "") -> Any:
    if not isinstance(mapping, Mapping):
        return default
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    for value in mapping.values():
        if isinstance(value, Mapping):
            found = first(value, *keys, default=None)
            if found not in (None, ""):
                return found
    return default


def _id_list(value: Any, *keys: str) -> list[str]:
    """Normalize ID arrays without ever exposing a JavaScript Number.

    Several Ocean Engine responses use arrays of objects instead of arrays of
    bare IDs.  Keeping this normalization in one place prevents a Python dict
    representation from accidentally becoming a material/product identifier.
    """
    values = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    result: list[str] = []
    for item in values:
        raw = first(item, *keys) if isinstance(item, Mapping) else item
        identifier = text_id(raw)
        if identifier and identifier not in result:
            result.append(identifier)
    return result


def normalize_account(row: Mapping[str, Any]) -> dict[str, Any]:
    advertiser_id = text_id(
        first(
            row,
            "advertiser_id",
            "advertiserId",
            "adv_id",
            "account_id",
            "aavid",
            "id",
        )
    )
    return {
        "aavid": advertiser_id,
        "advertiser_id": advertiser_id,
        "advertiser_name": str(
            first(
                row,
                "advertiser_name",
                "advertiserName",
                "adv_name",
                "account_name",
                "name",
            )
        ).strip(),
        "role": str(
            first(row, "role", "advertiser_role", "account_role", "account_type")
        ).strip(),
        "shop_id": text_id(first(row, "shop_id", "shopId")),
        "status": str(first(row, "status", "advertiser_status", default="available")).strip(),
        "raw": dict(row),
    }


def normalize_plan_system(value: Any) -> str:
    raw = str(value or "").strip().upper()
    # The list endpoint returns enum strings while detail currently returns
    # numeric evidence (1=OVERALL_PROJECT, 0=UNI_PROJECT).
    if raw in {"1", "OVERALL_PROJECT"} or "CHENGFANG" in raw:
        return "chengfang"
    if raw in {"0", "UNI_PROJECT", "GLOBAL", "UNI"}:
        return "global"
    return "unknown"


def normalize_promotion_scene(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw == "LIVE_PROM_GOODS" or "LIVE" in raw:
        return "live"
    if raw == "VIDEO_PROM_GOODS" or "VIDEO" in raw or "PRODUCT" in raw:
        return "product"
    return ""


def normalize_platform_status(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in {
        "ENABLE",
        "ENABLED",
        "ACTIVE",
        "DELIVERY",
        "DELIVERING",
        "DELIVERY_OK",
        "RUNNING",
        "投放中",
    }:
        return "active"
    if raw in {"LEARNING", "LEARN", "学习中"}:
        return "learning"
    if raw in {
        "PAUSE",
        "PAUSED",
        "DISABLE",
        "DISABLED",
        "SUSPEND",
        "EXTERNAL_URL_DISABLE",
        "FROZEN",
        "LIVE_ROOM_OFF",
        "NO_SCHEDULE",
        "OFFLINE_AUDIT",
        "OFFLINE_BALANCE",
        "OFFLINE_BUDGET",
        "QUOTA_DISABLE",
        "ROI2_DISABLE",
        "SYSTEM_DISABLE",
        "TIME_NO_REACH",
        "已暂停",
    }:
        return "paused"
    if raw in {"FINISH", "FINISHED", "ENDED", "END", "TIME_DONE", "已结束", "完成"}:
        return "ended"
    if raw in {"DELETE", "DELETED", "REMOVED", "已删除"}:
        return "deleted"
    return "unknown"


def normalize_plan_platform_status(row: Mapping[str, Any]) -> str:
    """Normalize effective status without confusing an offline room with a paused plan.

    Qianchuan can return ``status=LIVE_ROOM_OFF`` while ``opt_status=ENABLE``.
    The source plan is still enabled in that combination and may be monitored
    before the room starts, but it is not yet eligible for a write action.
    """
    status = first(row, "status", "delivery_status", "ad_status")
    opt_status = first(row, "opt_status", "optStatus")
    if str(status or "").strip().upper() == "LIVE_ROOM_OFF":
        if normalize_platform_status(opt_status) == "active":
            return "waiting_live"
        return "paused"
    return normalize_platform_status(status or opt_status)


def normalize_plan(row: Mapping[str, Any], *, advertiser_id: Any = "") -> dict[str, Any]:
    # The live list endpoint wraps the plan under ``ad_info`` while detail
    # returns a flat object.  Prefer the explicit wrapper so generic nested
    # product/room IDs can never be mistaken for the plan ID.
    plan_row = row.get("ad_info") if isinstance(row.get("ad_info"), Mapping) else row
    ad_id = text_id(first(plan_row, "ad_id", "adId", "promotion_id", "promotionId", "id"))
    marketing_goal = str(first(plan_row, "marketing_goal", "marketingGoal")).strip().upper()
    adlab_scene = str(first(plan_row, "adlab_scene", "adlabScene")).strip().upper()
    return {
        "aavid": text_id(advertiser_id or first(plan_row, "advertiser_id", "aavid")),
        "ad_id": ad_id,
        "plan_name": str(first(plan_row, "name", "ad_name", "promotion_name", "adName")).strip(),
        "marketing_goal": marketing_goal,
        "adlab_scene": adlab_scene,
        "promotion_scene": normalize_promotion_scene(marketing_goal),
        "plan_system": normalize_plan_system(adlab_scene),
        "platform_status": normalize_plan_platform_status(plan_row),
        "create_time": str(first(plan_row, "create_time", "createTime")).strip(),
        "modify_time": str(first(plan_row, "modify_time", "modifyTime", "update_time")).strip(),
        "raw": dict(row),
    }


def normalize_material(row: Mapping[str, Any]) -> dict[str, Any]:
    material_info = first(row, "material_info", "materialInfo", default={})
    if not isinstance(material_info, Mapping):
        material_info = {}
    video = first(
        material_info,
        "video_material",
        "videoMaterial",
        "video_info",
        "video",
        default=first(row, "video_info", "video", default={}),
    )
    if not isinstance(video, Mapping):
        video = {}
    material_id = text_id(
        first(
            video,
            "material_id",
            "materialId",
            "id",
            default=first(material_info, "material_id", "materialId", "id", default=first(row, "material_id", "materialId", "id")),
        )
    )
    product_info = first(row, "product_info", "productInfo", default=[])
    product_ids = first(row, "product_ids", "productIds", "products", default=product_info)
    return {
        "material_id": material_id,
        "material_name": str(first(video, "title", "material_name", "name", default=first(row, "material_name", "title", "name", "video_name"))).strip(),
        "material_type": str(first(material_info, "material_type", "type", default=first(row, "material_type", "type", default="VIDEO"))).strip().upper(),
        "material_status": str(first(row, "material_status", "status")).strip(),
        "audit_status": str(first(row, "audit_status", "auditStatus")).strip(),
        "video_id": text_id(first(video, "video_id", "videoId")),
        "product_ids": _id_list(product_ids, "product_id", "productId", "id"),
        "stats_info": dict(first(row, "stats_info", "stats", default={}) or {}),
        "create_time": str(first(video, "create_time", "createTime", default=first(row, "create_time", "createTime"))).strip(),
        "raw": dict(row),
    }


def normalize_product(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "product_id": text_id(first(row, "product_id", "productId", "id")),
        "product_name": str(first(row, "product_name", "title", "name")).strip(),
        "product_status": str(first(row, "product_status", "status")).strip(),
        "image_url": str(first(row, "image_url", "image", "cover_url")).strip(),
        "material_ids": _id_list(
            first(row, "material_ids", "materialIds", "materials", default=[]),
            "material_id",
            "materialId",
            "id",
        ),
        "raw": dict(row),
    }


def normalize_control_task(row: Mapping[str, Any]) -> dict[str, Any]:
    task_id = text_id(first(row, "task_id", "control_task_id", "assist_task_id", "id"))
    material_ids = first(row, "material_ids", "materialIds", "materials", "material_list", default=[])
    metrics = first(row, "metrics", "stats_info", "statsInfo", "stats", default={})
    return {
        "task_id": task_id,
        "assist_task_id": task_id,
        "ad_id": text_id(first(row, "ad_id", "adId")),
        "task_name": str(first(row, "name", "task_name")).strip(),
        "scene": str(first(row, "scene")).strip(),
        "status": str(first(row, "status", "task_status")).strip(),
        "budget": first(row, "budget"),
        "duration": first(row, "duration"),
        "material_ids": _id_list(material_ids, "material_id", "materialId", "id"),
        "create_time": str(first(row, "create_time", "createTime")).strip(),
        "modify_time": str(first(row, "modify_time", "modifyTime")).strip(),
        "metrics": dict(metrics) if isinstance(metrics, Mapping) else {},
        "raw": dict(row),
    }


def normalize_operation_log(row: Mapping[str, Any]) -> dict[str, Any]:
    log_id = text_id(first(row, "log_id", "logId", "id"))
    sub_logs = first(row, "sub_logs", "subLogs", default=[])
    content_log = "；".join(operation_log_content_items(row))
    return {
        "log_id": log_id,
        "platform_event_id": log_id,
        "occurred_at": str(first(row, "create_time", "createTime", "operation_time")).strip(),
        "operator_name": str(first(row, "operator_name", "operatorName", "operator")).strip(),
        "operator_id": text_id(first(row, "operator_id", "operatorId")),
        "object_name": str(first(row, "object_name", "objectName")).strip(),
        "object_id": text_id(first(row, "object_id", "objectId")),
        "content_title": str(first(row, "content_title", "contentTitle", "title")).strip(),
        "content_log": content_log,
        "control_task_id": operation_log_control_task_id(row),
        "opt_ip": str(first(row, "opt_ip", "ip")).strip(),
        "sub_logs": sub_logs if isinstance(sub_logs, list) else [],
        "raw": dict(row),
    }


UNIT_FACTORS = {
    "1": Decimal("0.00001"),  # 千分之一分 -> 元
    "2": Decimal("0.01"),     # 分 -> 元
    "3": Decimal("1"),        # 元
    "4": Decimal("1"),        # 个
    "10": Decimal("0.01"),    # 百分比 -> 小数
}


def normalize_metric_value(value: Any, unit: Any) -> Decimal:
    try:
        number = Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return number * UNIT_FACTORS.get(str(unit or ""), Decimal("1"))


def build_metric_unit_map(config_payload: Any) -> dict[str, str]:
    result: dict[str, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            field = first(value, "field", "field_name", "metric", "name")
            unit = first(value, "unit", "unit_type", "unitType")
            if field not in (None, "") and unit not in (None, ""):
                result[str(field)] = str(unit)
            for child in value.values():
                visit(child)
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            for child in value:
                visit(child)

    visit(config_payload)
    return result


def stable_material_set(material_ids: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({require_digit_id(value, "material_id") for value in material_ids}))


def raw_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
