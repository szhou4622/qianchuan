"""Metrics from ad/material/get are native values, not report/data/get units."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import math
import re

from utils.log_redaction import redact_text

DOCUMENT_ID = "1804363488115850"
REPORT_DOCUMENT_ID = "1823297941140569"
REPORT_SOURCE = "chengfang_anchor_material_report"
REPORT_SCOPE_LABEL = "乘方：对应抖音号素材整体数据"
OPTIONAL_REPORT_FIELDS = {
    "live_show_count_for_roi2_v2", "live_watch_count_for_roi2_v2",
    "live_cvr_rate_for_roi2_v2", "live_convert_rate_for_roi2_v2",
}
FIELDS = {
    "stat_cost_for_roi2": "stat_cost",
    "total_prepay_and_pay_order_roi2": "prepay_pay_order_count",
    "total_pay_order_gmv_include_coupon_for_roi2": "pay_gmv_include_coupon",
    "total_order_settle_amount_for_roi2_1h": "order_settle_amount_1h",
    "total_prepay_and_pay_settle_roi2_1h": "prepay_pay_settle_1h",
    "total_pay_order_count_for_roi2": "overall_order_count",
    "total_order_settle_count_for_roi2_1h": "order_settle_count_1h",
    "live_show_count_for_roi2_v2": "overall_show_count",
    "live_watch_count_for_roi2_v2": "overall_click_count",
    "live_cvr_rate_for_roi2_v2": "overall_ctr",
    "live_convert_rate_for_roi2_v2": "overall_conversion_rate",
}
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def native_value(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    text = str(value).strip()
    if len(text) > 80 or not _NUMBER.fullmatch(text):
        return None
    try:
        result = float(Decimal(text))
        return result if math.isfinite(result) else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def native_metric(stats, *names):
    for name in names:
        if name in stats:
            return native_value(stats[name])
    return None


def safe_numeric_evidence(value, depth=0):
    if depth > 2:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        text = str(value)
        if len(text) > 80:
            return None
        return value if isinstance(value, (int, float)) and math.isfinite(value) else text
    if isinstance(value, str) and len(value) <= 80:
        if value in {"null", "None", "NaN", "inf", "Infinity", "N/A"} or re.fullmatch(r"[\s\d.,+\-eE%￥¥元]*", value):
            return value
    if isinstance(value, Mapping):
        return {key: safe_numeric_evidence(value[key], depth + 1) for key in ("value", "Value", "value_str", "ValueStr", "unit", "Unit") if key in value}
    return None


def _optional_metric_errors(value):
    """Whitelist bounded auxiliary failures without persisting response bodies."""
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value[:8]:
        if not isinstance(item, Mapping):
            continue
        raw_fields = item.get("fields")
        fields = [field for field in raw_fields[:16] if isinstance(field, str) and field in OPTIONAL_REPORT_FIELDS] if isinstance(raw_fields, (list, tuple)) else []
        if not fields:
            continue
        endpoint = str(item.get("endpoint") or "").split("?", 1)[0]
        endpoint = endpoint if endpoint == "/open_api/v1.0/qianchuan/report/uni_promotion/data/get/" else ""
        code = str(item.get("code") or "")
        request_id = str(item.get("request_id") or "")
        result.append({"fields": list(dict.fromkeys(fields)), "endpoint": endpoint,
                       "code": code if re.fullmatch(r"\d{1,8}", code) else "",
                       "request_id": request_id if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) else "",
                       "message": redact_text(str(item.get("message") or "")[:8000])[:1000]})
    return result


def evidence(materials, snapshots, *, requested_fields, report_units, observed_at, stat_date,
             source="ad_material_stats_info", scope=None):
    """Bounded samples: numbers and markers only, never arbitrary response text."""
    source = str(source or "ad_material_stats_info")
    if source not in {"ad_material_stats_info", REPORT_SOURCE}:
        raise ValueError("Unsupported material metric evidence source")
    is_report = source == REPORT_SOURCE
    scope = scope if isinstance(scope, Mapping) else {}
    # Preserve the attribution proof's dimensions, not arbitrary metadata,
    # names, credentials, or full response bodies. Export later hashes IDs.
    safe_scope = {key: scope[key] for key in (
        "metric_scope", "attribution", "aadvid", "advertiser_id", "ad_id",
        "anchor_id", "ecp_app_id", "data_period", "observed_at",
        "catalog_observed_at", "catalog_complete", "matching_plan_count",
        "report_observed_at", "report_complete", "ad_id_filtered",
        "catalog_start_time", "catalog_end_time", "catalog_plan_count", "start_date", "end_date",
    ) if key in scope and isinstance(scope[key], (str, int, float, bool, type(None)))}
    for key in ("catalog_bid_types", "proof_request_ids", "report_request_ids", "optional_metric_request_ids"):
        values = scope.get(key)
        if isinstance(values, list):
            safe_scope[key] = [v for v in values[:100] if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,100}", v)]
    optional_errors = _optional_metric_errors(scope.get("optional_metric_errors")) if is_report else []
    if optional_errors:
        safe_scope["optional_metric_errors"] = optional_errors
        safe_scope["optional_metric_errors_truncated"] = len(scope["optional_metric_errors"]) > 8
    by_id = {str(row.get("material_id") or ""): row for row in snapshots}
    fields = {field: column for field, column in FIELDS.items() if field in requested_fields}
    counts = {field: {"valid": 0, "zero": 0, "missing": 0, "null": 0, "invalid": 0} for field in fields}
    reported_counts = {field: dict(value) for field, value in counts.items()}
    groups = {"invalid": [], "missing": [], "zero": [], "positive": []}
    for material in materials:
        mid = str(material.get("material_id") or "")
        if mid not in by_id:
            continue
        stats = material.get("stats_info")
        stats = stats if isinstance(stats, Mapping) else {}
        response_row = material.get("raw")
        response_available = isinstance(response_row, Mapping)
        report_row_present = material.get("report_row_present") is True
        if is_report:
            response_available = response_available and report_row_present
        original_stats = response_row.get("stats_info") if response_available else ({} if is_report else stats)
        original_stats = original_stats if isinstance(original_stats, Mapping) else {}
        selected = {}
        category = "positive"
        for field, column in fields.items():
            raw = stats.get(field)
            value = native_value(raw)
            state = "missing" if field not in stats else "null" if raw is None or raw == "" else "invalid" if value is None else "valid"
            counts[field][state] += 1
            if not is_report or report_row_present:
                reported_counts[field][state] += 1
                if value == 0:
                    reported_counts[field]["zero"] += 1
            if value == 0:
                counts[field]["zero"] += 1
            if state == "invalid":
                category = "invalid"
            elif state in {"missing", "null"} and category != "invalid":
                category = "missing"
            elif field == "stat_cost_for_roi2" and value == 0 and category == "positive":
                category = "zero"
            raw_safe = safe_numeric_evidence(raw)
            selected[field] = {"present": field in stats, "normalized_type": type(raw).__name__,
                               "normalized_value": raw_safe, "value_state": state,
                               "raw_present": field in original_stats,
                               "raw_value": safe_numeric_evidence(original_stats.get(field)),
                               "raw_type": type(original_stats.get(field)).__name__,
                               "parsed_value": by_id[mid].get(column), "database_column": column}
        if len(groups[category]) < 2:
            sample = {"material_id": mid, "raw_response_available": response_available, "fields": selected}
            if is_report:
                sample["report_row_present"] = report_row_present
            groups[category].append(sample)
    samples = [sample for group in groups.values() for sample in group][:6]
    result = {"source": source, "document_id": REPORT_DOCUMENT_ID if is_report else DOCUMENT_ID,
            "conversion": "report_values_normalized_once" if is_report else "native_values_no_report_unit_scaling", "observed_at": observed_at,
            "requested_fields": list(requested_fields),
            "stat_date": stat_date, "row_count": len(by_id), "field_counts": counts,
            "reported_field_counts": reported_counts,
            "report_unit_context": {field: str(report_units.get(field)) if str(report_units.get(field)) in {"0","1","2","3","4","10"} else "not_declared" for field in fields},
            "sample_limit": 6, "samples": samples}
    if is_report:
        result["scope"] = safe_scope
        result["scope_label"] = REPORT_SCOPE_LABEL
        result["optional_metric_error_count"] = len(optional_errors)
        result["report_rows_present"] = sum(
            1 for material in materials
            if str(material.get("material_id") or "") in by_id and material.get("report_row_present") is True
        )
        result["report_rows_absent"] = len(by_id) - result["report_rows_present"]
        result["attribution_basis"] = "local_complete_catalog_unique_plan_not_official_ad_dimension"
    return result
