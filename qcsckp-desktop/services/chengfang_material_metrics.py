"""Strict attribution and conversion for Chengfang live material reports.

The report has no ad_id dimension. An account/anchor/PC report may only be
attached to a plan after a complete official catalog establishes unique scope.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from services.material_metric_contract import native_value
from services.qianchuan_open_api.errors import ApiRequestError
from services.qianchuan_open_api.normalizers import normalize_plan_system, normalize_promotion_scene

SOURCE = "chengfang_anchor_material_report"
CONTRACT = "chengfang_anchor_pc_overall_v1"
DATA_PERIOD = "ALL_DATA"
_UNIT_FACTORS = {"0": Decimal(1), "1": Decimal("0.00001"), "2": Decimal("0.01"),
                 "3": Decimal(1), "4": Decimal(1), "10": Decimal("0.01")}


def _fail(message: str, *, missing: bool = False) -> None:
    raise ApiRequestError(message, code="client_metric_scope_missing" if missing else "client_metric_scope")


def _blocks(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = plan.get("raw") if isinstance(plan.get("raw"), Mapping) else plan
    ad = raw.get("ad_info") if isinstance(raw.get("ad_info"), Mapping) else raw
    result = [plan, ad]
    for container in (raw, ad):
        for name in ("anchor_info", "aweme_info", "room_info", "delivery_setting"):
            if isinstance(container.get(name), Mapping):
                result.append(container[name])
    return result


def _one_id(blocks: Iterable[Mapping[str, Any]], names: tuple[str, ...], label: str,
            *, required: bool = True) -> str:
    found = set()
    for block in blocks:
        for name in names:
            value = block.get(name)
            if value in (None, ""):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit():
                _fail(label + "缺少无损数字标识")
            found.add(str(int(str(value).strip())))
    if len(found) > 1:
        _fail(label + "在官方响应中不一致")
    if not found and required:
        _fail(label + "缺少官方证据", missing=True)
    return next(iter(found), "")


def chengfang_live_context(plan: Mapping[str, Any], *, advertiser_id: Any,
                           ad_id: Any) -> dict[str, str]:
    """Read only known plan containers; never find unrelated nested object IDs."""
    blocks = _blocks(plan)
    pid = _one_id(blocks, ("ad_id", "adId"), "主计划ID")
    aid = _one_id(blocks, ("advertiser_id", "aavid", "aadvid"), "千川账户", required=False)
    if pid != str(ad_id) or (aid and aid != str(advertiser_id)):
        _fail("乘方素材报表账户或主计划归属不一致")
    system_values = [block.get("adlab_scene") for block in blocks if block.get("adlab_scene") not in (None, "")]
    system_values += [plan.get("plan_system")] if plan.get("plan_system") not in (None, "", "unknown") else []
    scene_values = [block.get("marketing_goal") for block in blocks if block.get("marketing_goal") not in (None, "")]
    scene_values += [plan.get("promotion_scene")] if plan.get("promotion_scene") else []
    if not system_values:
        _fail("官方计划缺少计划体系证据", missing=True)
    if any(normalize_plan_system(value) != "chengfang" for value in system_values):
        _fail("官方计划尚未核验为乘方")
    if not scene_values:
        _fail("官方计划缺少推广场景证据", missing=True)
    if any(normalize_promotion_scene(value) != "live" for value in scene_values):
        _fail("官方计划尚未核验为推直播")
    anchor = _one_id(blocks, ("anchor_id", "aweme_id", "aweme_uid"), "抖音号ID")
    platform = _one_id(blocks, ("ecp_app_id", "ecpAppId"), "下单平台", required=False) or "1"
    if platform != "1":
        _fail("当前乘方计划不是已验证的千川PC下单平台")
    return {"advertiser_id": str(advertiser_id), "ad_id": pid, "anchor_id": anchor,
            "ecp_app_id": platform, "roi2_material_type_v3": "3", "data_period": DATA_PERIOD}


def report_metric_value(block: Any, unit: Any = None) -> float | None:
    """Keep null/invalid unknown, true zero zero; only declared units convert."""
    raw = block
    inline_units = []
    if isinstance(block, Mapping):
        raw = next((block[key] for key in ("Value", "value", "ValueStr", "value_str") if key in block), None)
        inline_units = [str(block[key]).strip() for key in ("unit", "Unit", "unit_type", "unitType")
                        if block.get(key) not in (None, "")]
    declared = str(unit).strip() if unit not in (None, "") else ""
    if isinstance(block, Mapping) and str(block.get("ValueStr") or block.get("value_str") or "").strip().endswith("%"):
        # Live report returns percentage points (e.g. Value=0.5, ValueStr=0.50%).
        # Treat this explicit unit consistently even for values below one.
        if not inline_units and declared in ("", "0"):
            inline_units = ["10"]
    if any(value not in _UNIT_FACTORS for value in inline_units) or (declared and declared not in _UNIT_FACTORS):
        raise ApiRequestError("乘方报表返回未验证的指标单位", code="client_metric_unit")
    if len(set(inline_units)) > 1 or (inline_units and declared not in ("", "0", inline_units[0])):
        raise ApiRequestError("乘方报表指标单位与配置不一致", code="client_metric_unit")
    value = native_value(raw)
    if value is None:
        return None
    factor = _UNIT_FACTORS.get(inline_units[0] if inline_units else declared, Decimal(1))
    return native_value(Decimal(str(value)) * factor)


def merge_chengfang_material_metrics(materials, report_rows, *, scope, units=None):
    """Intersect membership; an absent report row must never reuse native zero."""
    if (not isinstance(scope, Mapping) or scope.get("attribution") != "unique_plan_in_complete_catalog"
            or scope.get("metric_scope") != "chengfang_anchor_material"
            or scope.get("data_period") != DATA_PERIOD):
        _fail("乘方素材指标缺少完整的唯一计划归属证据")
    reports = {}
    for row in report_rows:
        mid = _one_id([row], ("material_id",), "素材ID")
        if mid in reports:
            _fail("同一素材出现多个报表行，未合并不明确维度")
        reports[mid] = row
    result = []
    seen = set()
    for material in materials:
        mid = _one_id([material], ("material_id",), "素材ID")
        if mid in seen:
            _fail("计划素材目录出现重复素材")
        seen.add(mid)
        item = dict(material)
        report = reports.get(mid)
        report_raw = report.get("raw") if report and isinstance(report.get("raw"), Mapping) else {}
        raw_stats = report_raw.get("metrics") if isinstance(report_raw.get("metrics"), Mapping) else (report.get("stats_info") if report else {})
        raw_stats = dict(raw_stats) if isinstance(raw_stats, Mapping) else {}
        item["stats_info"] = {field: report_metric_value(value, (units or {}).get(field))
                              for field, value in raw_stats.items()}
        raw = dict(item.get("raw") or {})
        # The snapshot parser also checks raw.stats_info when stats are empty.
        raw["stats_info"] = raw_stats
        raw["material_report"] = dict(report_raw)
        item.update(raw=raw, metric_source=SOURCE, metric_scope=dict(scope),
                    report_row_present=report is not None,
                    metric_row_state="reported" if report is not None else "not_in_report")
        if report and not str(item.get("material_name") or "").strip():
            item["material_name"] = str(report.get("material_name") or "")
        result.append(item)
    return result
