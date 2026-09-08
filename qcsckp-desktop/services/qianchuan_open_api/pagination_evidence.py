"""Small structural diagnostics; never retain returned rows or credentials."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

from utils.log_redaction import redact_text


def fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_error_message(value: Any) -> str:
    # Share the application's credential/header vocabulary; short values must
    # not escape merely because they do not resemble long opaque tokens.
    text = redact_text(str(value or "")[:8000])
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer <redacted>", text)
    text = re.sub(r"(?i)[\"']?(access[_-]?token|refresh[_-]?token|app[_-]?secret|authorization|password|secret)[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^,;\r\n]+)", r"\1=<redacted>", text)
    text = re.sub(r"https?://[^\s<>\"']+", "<url>", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<email>", text)
    text = re.sub(r"(?i)[a-z]:[\\/][^\r\n\"']+", "<local-path>", text)
    text = re.sub(r"(?<![A-Za-z_])\d{6,}(?![A-Za-z_])", "<id>", text)
    text = re.sub(r"(?<![A-Za-z0-9_])[A-Za-z0-9\-]{24,}(?![A-Za-z0-9_])", "<opaque>", text)
    technical_camel = {"ValueStr", "Value", "PageInfo", "PageSize", "TotalNumber", "TotalPage",
                       "MaterialId", "AdvertiserId", "OpenAPI", "TypeError", "ValueError",
                       "InvalidParameter", "DataList", "AdMaterialInfos", "UInt64", "Int64"}
    text = re.sub(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b",
                  lambda match: match.group(0) if match.group(0) in technical_camel else "<private-name>", text)
    return text[:1000]


def help_evidence(value: Any) -> dict[str, Any]:
    raw = str(value or "")
    if not raw:
        return {}
    clean = safe_error_message(raw)
    lower = clean.lower()
    parameter_error = any(term in lower for term in (
        "invalid parameter", "invalidparameter", "invalid field", "unsupported field", "parameter is invalid",
        "参数错误", "参数不合法", "无效参数", "字段不支持", "不支持的字段",
        "字段不存在", "参数超出", "page out of range", "超出分页范围",
    ))
    fields = set()
    basic = {"fields", "field", "page", "page_size", "total_number", "total_page", "material_id",
             "advertiser_id", "ad_id", "dimensions", "metrics", "order_by", "order_field",
             "order_type", "data_topic", "start_time", "end_time", "start_date", "end_date"}
    for word in re.findall(r"\b[a-z][a-z0-9_]{1,100}\b", clean):
        if word in basic or "_for_roi2" in word or word.startswith("roi2_material_"):
            fields.add(word)
    category = "parameter_error" if parameter_error else "upstream_error" if any(
        word in lower for word in ("timeout", "temporar", "服务异常", "服务繁忙", "超时")) else "unknown"
    return {"category": category, "parameter_error": parameter_error, "field_names": sorted(fields),
            "length": len(raw), "fingerprint": fingerprint(raw),
            "text": clean,
            "meaning": {"parameter_error": "参数或字段校验错误", "upstream_error": "上游服务异常", "unknown": "技术说明尚未归类"}[category]}


def page_evidence(data: Any, *, items_key: str | None = None, identity_getter=None) -> dict[str, Any]:
    from .pagination import extract_items
    if not isinstance(data, Mapping):
        return {"response_type": type(data).__name__}
    evidence: dict[str, Any] = {"items_key": items_key or "auto"}
    for name in ("page_info", "pageInfo", "pagination"):
        info = data.get(name)
        if isinstance(info, Mapping):
            evidence["page_info"] = {}
            evidence["page_info_types"] = {}
            for key in ("page", "page_index", "page_size", "pageSize", "total_page", "total_pages",
                        "total_number", "total_num", "total", "count", "has_more"):
                if key not in info:
                    continue
                value = info[key]
                evidence["page_info_types"][key] = type(value).__name__
                if isinstance(value, (int, float, bool)) and not (isinstance(value, float) and not math.isfinite(value)):
                    evidence["page_info"][key] = value
                elif isinstance(value, str) and value.isdigit() and len(value) <= 20:
                    evidence["page_info"][key] = int(value)
            break
    try:
        rows = extract_items(data, items_key=items_key)
    except Exception:
        evidence["primary_list_valid"] = False
        return evidence
    evidence.update(primary_list_valid=True, actual_list_count=len(rows))
    dimensions = sorted({str(key) for row in rows if isinstance(row.get("dimensions"), Mapping)
                         for key in row["dimensions"] if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", str(key))})
    if dimensions:
        evidence["dimension_fields"] = dimensions
    if identity_getter is not None:
        try:
            ids = [fingerprint(identity_getter(row)) for row in rows]
            evidence.update(identity_count=len(set(ids)), identity_sequence_hash=fingerprint(ids),
                            first_identity_hash=ids[0] if ids else "", last_identity_hash=ids[-1] if ids else "")
        except Exception:
            evidence["identity_valid"] = False
    return evidence
