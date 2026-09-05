"""千川官方 API 的业务级封装。"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import json
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Optional

from utils.operation_log_identity import operation_log_row_identity

from .client import ApiResponse, QianchuanOpenApiClient
from .errors import OfficialApiWriteDisabled
from .normalizers import (
    build_metric_unit_map,
    first,
    id_number,
    normalize_account,
    normalize_control_task,
    normalize_material,
    normalize_operation_log,
    normalize_plan,
    normalize_product,
    require_digit_id,
    stable_material_set,
    text_id,
)


CONTROL_CREATE_EXTRA_FIELDS = frozenset(
    {
        "smart_bid_type",
        "external_action",
        "deep_external_action",
        "roi2_goal",
        "bid",
    }
)


# ``control_task/list`` only populates ``task_list[].metrics`` when the
# requested metric fields are explicit.  These seven fields are the verified
# task-scoped inputs used by the stop-rule page and must travel together so a
# partially populated task can never be evaluated as a complete snapshot.
CONTROL_TASK_METRIC_FIELDS = (
    "stat_cost_for_roi2_assist",
    "total_pay_order_count_for_roi2_assist",
    "total_pay_order_gmv_include_coupon_for_roi2_assist",
    "total_prepay_and_pay_order_roi2_assist",
    "total_order_settle_amount_for_roi2_1h_assist",
    "total_prepay_and_pay_settle_roi2_1h_assist",
    "total_order_settle_count_for_roi2_1h_assist",
)


def material_report_filter_context(plan: Mapping[str, Any]) -> dict[str, str]:
    """Extract the plan-scoped filters required by the global-live topic."""
    raw = plan.get("raw") if isinstance(plan.get("raw"), Mapping) else plan
    anchor_id = text_id(first(raw, "anchor_id", "aweme_id", "aweme_uid"))
    smart_bid_type = str(first(raw, "smart_bid_type", "smartBidType")).strip().upper()
    aggregate_smart_bid_type = {
        "SMART_BID_CUSTOM": "0",
        "0": "0",
        "SMART_BID_CONSERVATIVE": "7",
        "7": "7",
    }.get(smart_bid_type, "")
    # This Open API reads Qianchuan PC plans. Detail responses observed from
    # the official endpoint omit ecp_app_id, whose config enum is 1=Qianchuan
    # PC and 2=Douyin Shop easy promotion. Preserve a returned value if the
    # contract starts exposing it; otherwise use the only applicable source.
    ecp_app_id = text_id(first(raw, "ecp_app_id", "ecpAppId")) or "1"
    if not anchor_id or not aggregate_smart_bid_type:
        return {}
    return {
        "anchor_id": anchor_id,
        "aggregate_smart_bid_type": aggregate_smart_bid_type,
        "ecp_app_id": ecp_app_id,
    }


def _validated_decimal(
    value: Any,
    label: str,
    *,
    minimum: Decimal,
    maximum: Optional[Decimal] = None,
    max_decimal_places: int = 2,
    step: Optional[Decimal] = None,
) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{label}必须是有效数字") from exc
    if not number.is_finite():
        raise ValueError(f"{label}必须是有效数字")
    if number < minimum:
        raise ValueError(f"{label}不得低于 {minimum} 元" if "预算" in label else f"{label}不得低于 {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label}不得高于 {maximum} 元" if "预算" in label or "出价" in label else f"{label}不得高于 {maximum}")
    decimal_places = max(0, -number.as_tuple().exponent)
    if decimal_places > max_decimal_places:
        raise ValueError(f"{label}最多支持{max_decimal_places}位小数")
    if step is not None and (number - minimum) % step != 0:
        raise ValueError(f"{label}必须按 {step} 递增")
    return number


def build_material_control_task_body(
    *,
    advertiser_id: Any,
    ad_id: Any,
    marketing_goal: str,
    name: str,
    budget: Any,
    duration: Any,
    material_ids: Iterable[Any],
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build one strict official control-task request without passthrough fields."""
    aid = require_digit_id(advertiser_id, "advertiser_id")
    pid = require_digit_id(ad_id, "ad_id")
    goal = str(marketing_goal or "").strip().upper()
    if goal not in {"LIVE_PROM_GOODS", "VIDEO_PROM_GOODS"}:
        raise ValueError("marketing_goal 仅支持推直播或推商品")
    task_name = str(name or "").strip()
    if not 1 <= len(task_name) <= 50:
        raise ValueError("追投任务名称须为1至50个字符")
    materials = stable_material_set(material_ids)
    if not 1 <= len(materials) <= 20:
        raise ValueError("每条追投调控任务必须包含 1 至 20 条视频素材")
    money = _validated_decimal(
        budget,
        "追投预算",
        minimum=Decimal("100"),
        max_decimal_places=2,
    )
    hours = None
    if duration not in (None, ""):
        hours = _validated_decimal(
            duration,
            "追投时长",
            minimum=Decimal("0.5"),
            maximum=Decimal("24"),
            max_decimal_places=1,
            step=Decimal("0.5"),
        )
    supplied_extra = dict(extra or {})
    unknown = sorted(set(supplied_extra) - CONTROL_CREATE_EXTRA_FIELDS)
    if unknown:
        raise ValueError("追投请求包含官方接口未声明的字段：" + ",".join(unknown))
    clean_extra = {
        key: value
        for key, value in supplied_extra.items()
        if key in CONTROL_CREATE_EXTRA_FIELDS and value not in (None, "")
    }

    smart_bid_type = str(clean_extra.get("smart_bid_type") or "").strip().upper()
    cost_fields = {
        key for key in ("external_action", "deep_external_action", "roi2_goal", "bid")
        if key in clean_extra
    }
    if goal == "VIDEO_PROM_GOODS":
        if hours is None:
            raise ValueError("推商品放量追投必须填写调控时长")
        if clean_extra:
            raise ValueError("推商品放量追投不能携带直播控成本参数")
    else:
        if smart_bid_type == "SMART_BID_CONSERVATIVE":
            if hours is None:
                raise ValueError("推直播放量追投必须填写调控时长")
            if cost_fields:
                raise ValueError("推直播放量追投不能携带ROI、出价或转化目标字段")
        elif smart_bid_type == "SMART_BID_CUSTOM":
            if hours is not None:
                raise ValueError("推直播控成本追投不能携带调控时长")
            if clean_extra.get("external_action") != "AD_CONVERT_TYPE_LIVE_SUCCESSORDER_PAY":
                raise ValueError("推直播控成本追投的转化目标参数不正确")
            has_roi = "roi2_goal" in clean_extra
            has_bid = "bid" in clean_extra
            if has_roi == has_bid:
                raise ValueError("推直播控成本追投必须且只能填写综合营销ROI或直播间成交出价")
            if has_roi:
                if clean_extra.get("deep_external_action") not in {
                    "AD_CONVERT_TYPE_LIVE_PAY_ROI",
                    "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI",
                }:
                    raise ValueError("综合营销ROI对应的深度转化目标参数不正确")
                clean_extra["roi2_goal"] = float(
                    _validated_decimal(
                        clean_extra["roi2_goal"],
                        "综合营销ROI目标",
                        minimum=Decimal("0.01"),
                        maximum=Decimal("100"),
                        max_decimal_places=2,
                    )
                )
            else:
                if "deep_external_action" in clean_extra:
                    raise ValueError("直播间成交出价不能携带ROI深度转化目标")
                bid = _validated_decimal(
                    clean_extra["bid"],
                    "直播间成交出价",
                    minimum=Decimal("0.1"),
                    maximum=Decimal("10000"),
                    max_decimal_places=2,
                )
                if bid > money:
                    raise ValueError("直播间成交出价不能高于追投预算")
                clean_extra["bid"] = float(bid)
        else:
            raise ValueError("推直播追投必须明确放量或控成本方式")

    body: dict[str, Any] = {
        "advertiser_id": id_number(aid, "advertiser_id"),
        "ad_id": id_number(pid, "ad_id"),
        "name": task_name,
        "scene": "MATERIAL_ADD_BUDGET",
        "budget": float(money),
        "material_type": "VIDEO",
        "material_ids": [id_number(value, "material_id") for value in materials],
    }
    if hours is not None:
        body["duration"] = float(hours)
    body.update(clean_extra)
    return body


class QianchuanOfficialApiService:
    AUTHORIZED_ADVERTISERS = "/open_api/oauth2/advertiser/get/"
    SHOP_ADVERTISERS = "/open_api/v1.0/qianchuan/shop/advertiser/list/"
    ENTERPRISE_ADVERTISERS = "/open_api/2/ebp/advertiser/list/"
    ADVERTISER_PUBLIC_INFO = "/open_api/2/advertiser/public_info/"
    PLAN_LIST = "/open_api/v1.0/qianchuan/uni_promotion/list/"
    PLAN_DETAIL = "/open_api/v1.0/qianchuan/uni_promotion/ad/detail/"
    PLAN_MATERIALS = "/open_api/v1.0/qianchuan/uni_promotion/ad/material/get/"
    PLAN_PRODUCTS = "/open_api/v1.0/qianchuan/uni_promotion/ad/product/get/"
    REPORT_CONFIG = "/open_api/v1.0/qianchuan/report/uni_promotion/config/get/"
    REPORT_DATA = "/open_api/v1.0/qianchuan/report/uni_promotion/data/get/"
    CONTROL_LIST = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/list/"
    CONTROL_CREATE = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/create/"
    CONTROL_UPDATE = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/update/"
    CONTROL_STATUS = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/status/update/"
    CONTROL_BUDGET = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/budget/update/"
    CONTROL_DURATION = "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/duration/update/"
    OPERATION_LOGS = "/open_api/v1.0/qianchuan/tools/log_search/"

    MARKETING_GOALS = ("LIVE_PROM_GOODS", "VIDEO_PROM_GOODS")
    REPORT_MATERIAL_TOPICS = {
        ("chengfang", "live"): "OVERALL_ROI_LIVE_MATERIAL_VIDEO",
        ("chengfang", "product"): "OVERALL_ROI_PRODUCT_MATERIAL",
        ("global", "live"): "SITE_PROMOTION_POST_DATA_VIDEO",
        ("global", "product"): "SITE_PROMOTION_PRODUCT_POST_DATA_VIDEO",
    }
    # config/get is the contract source for each topic.  In particular,
    # SITE_PROMOTION_POST_DATA_VIDEO marks all three entries below as required;
    # omitting video_type returns business code 40000 even though HTTP is 200.
    # Keep the exact required set per topic instead of adding a dimension to
    # every class: global-product does not declare video_type as a dimension.
    REPORT_MATERIAL_DIMENSIONS = {
        ("chengfang", "live"): (
            "material_id",
            "roi2_material_video_name",
        ),
        ("chengfang", "product"): (
            "material_id",
            "roi2_material_video_name",
        ),
        ("global", "live"): (
            "material_id",
            "roi2_material_video_name",
            "roi2_material_video_type",
        ),
        ("global", "product"): (
            "material_id",
            "roi2_material_video_name",
        ),
    }

    def __init__(
        self,
        client: Optional[QianchuanOpenApiClient] = None,
        *,
        allow_writes: Optional[bool] = None,
    ) -> None:
        self.client = client or QianchuanOpenApiClient()
        if allow_writes is None:
            import config as runtime_config

            self.allow_writes = bool(
                runtime_config.ALLOW_LIVE_OFFICIAL_API_WRITES
            )
        else:
            self.allow_writes = bool(allow_writes)
        self._business_account_cache_lock = threading.Lock()
        self._business_account_cache: Optional[
            tuple[float, list[dict[str, Any]], dict[str, Any]]
        ] = None

    def _require_writes(self) -> None:
        if not self.allow_writes:
            raise OfficialApiWriteDisabled(
                "千川官方 API 真实写入未开启；当前只允许读取、规则判断和飞书确认排队"
            )

    def list_authorized_accounts(self) -> list[dict[str, Any]]:
        response = self.client.get(self.AUTHORIZED_ADVERTISERS)
        rows = self.client.extract_items(response.data)
        return [item for item in (normalize_account(row) for row in rows) if item["advertiser_id"]]

    def list_shop_advertisers(self, shop_id: Any, *, permission: str = "QC_AWEME") -> list[dict[str, Any]]:
        sid = require_digit_id(shop_id, "shop_id")
        rows, _ = self.client.get_all_pages(
            self.SHOP_ADVERTISERS,
            {"shop_id": sid, "permission": [permission]},
            page_size=100,
        )
        return [item for item in (normalize_account(row) for row in rows) if item["advertiser_id"]]

    def list_enterprise_advertisers(
        self,
        enterprise_organization_id: Any,
        *,
        account_source: str = "QIANCHUAN",
    ) -> list[dict[str, Any]]:
        """Expand an authorized enterprise/BP subject to final ad accounts."""
        enterprise_id = require_digit_id(
            enterprise_organization_id, "enterprise_organization_id"
        )
        source = str(account_source or "").strip().upper()
        if source not in {"AD", "LOCAL", "QIANCHUAN"}:
            raise ValueError("account_source must be AD, LOCAL, or QIANCHUAN")
        rows, _ = self.client.get_all_pages(
            self.ENTERPRISE_ADVERTISERS,
            {
                "enterprise_organization_id": enterprise_id,
                "account_source": source,
            },
            page_size=100,
        )
        return [
            item
            for item in (normalize_account(row) for row in rows)
            if item["advertiser_id"]
        ]

    def list_advertiser_public_info(
        self, advertiser_ids: Iterable[Any]
    ) -> list[dict[str, Any]]:
        """Return authoritative public names for up to 100 advertisers per call."""
        ids = list(dict.fromkeys(require_digit_id(value, "advertiser_id") for value in advertiser_ids))
        result: list[dict[str, Any]] = []
        for offset in range(0, len(ids), 100):
            batch = ids[offset : offset + 100]
            response = self.client.get(
                self.ADVERTISER_PUBLIC_INFO,
                {"advertiser_ids": batch},
            )
            result.extend(
                item
                for item in (
                    normalize_account(row)
                    for row in QianchuanOpenApiClient.extract_items(response.data)
                )
                if item["advertiser_id"]
            )
        return result

    def clear_business_account_cache(self) -> None:
        with self._business_account_cache_lock:
            self._business_account_cache = None

    def list_business_accounts(
        self,
        *,
        cache_ttl_seconds: float = 60.0,
        force_refresh: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Resolve OAuth subjects to final Qianchuan advertiser accounts.

        A shop subject is not itself an advertiser.  It must be expanded through
        ``shop/advertiser/list`` before any plan or write API is called.
        """
        ttl = max(0.0, float(cache_ttl_seconds or 0))
        with self._business_account_cache_lock:
            cached = self._business_account_cache
            if (
                not force_refresh
                and cached is not None
                and time.monotonic() - cached[0] <= ttl
            ):
                return (
                    [dict(item) for item in cached[1]],
                    json.loads(json.dumps(cached[2], ensure_ascii=False)),
                )

        authorized = self.list_authorized_accounts()
        resolved: dict[str, dict[str, Any]] = {}
        evidence: dict[str, Any] = {"complete": True, "subjects": []}
        for subject in authorized:
            role = str(subject.get("role") or "").upper()
            subject_id = text_id(subject.get("advertiser_id"))
            shop_id = text_id(subject.get("shop_id"))
            is_shop = "SHOP" in role or bool(shop_id)
            if not is_shop:
                is_enterprise = any(
                    marker in role for marker in ("ENTERPRISE", "BP_OPERATOR")
                )
                if subject_id and is_enterprise:
                    try:
                        rows = self.list_enterprise_advertisers(subject_id)
                        for row in rows:
                            row = dict(row)
                            row["enterprise_organization_id"] = subject_id
                            resolved[text_id(row.get("advertiser_id"))] = row
                        evidence["subjects"].append(
                            {
                                "subject_id": subject_id,
                                "role": role,
                                "resolved": len(rows),
                                "type": "enterprise",
                                "account_source": "QIANCHUAN",
                            }
                        )
                    except Exception as exc:
                        evidence["complete"] = False
                        evidence["subjects"].append(
                            {
                                "subject_id": subject_id,
                                "role": role,
                                "resolved": 0,
                                "type": "enterprise",
                                "account_source": "QIANCHUAN",
                                "error": str(exc),
                            }
                        )
                    continue
                is_final_advertiser = "ADVERTISER" in role and not any(
                    marker in role for marker in ("OPERATOR", "ENTERPRISE", "AGENT", "BP")
                )
                if subject_id and is_final_advertiser:
                    resolved[subject_id] = subject
                    evidence["subjects"].append(
                        {"subject_id": subject_id, "role": role, "resolved": 1, "type": "advertiser"}
                    )
                else:
                    # Enterprise/BP/operator identities are OAuth subjects,
                    # not final Qianchuan advertiser accounts.  Presenting one
                    # in the selector leads to an account that plan APIs cannot
                    # read, so keep it as evidence only.
                    evidence["subjects"].append(
                        {
                            "subject_id": subject_id,
                            "role": role,
                            "resolved": 0,
                            "type": "unsupported_subject",
                            "reason": "not_final_advertiser",
                            "ignored": True,
                        }
                    )
                continue
            sid = shop_id or subject_id
            try:
                rows = self.list_shop_advertisers(sid)
                for row in rows:
                    row = dict(row)
                    row["shop_id"] = sid
                    if len(rows) == 1 and not row.get("advertiser_name"):
                        row["advertiser_name"] = subject.get("advertiser_name") or ""
                    resolved[text_id(row.get("advertiser_id"))] = row
                evidence["subjects"].append(
                    {"subject_id": subject_id, "shop_id": sid, "role": role, "resolved": len(rows), "type": "shop"}
                )
            except Exception as exc:
                evidence["complete"] = False
                evidence["subjects"].append(
                    {"subject_id": subject_id, "shop_id": sid, "role": role, "resolved": 0, "type": "shop", "error": str(exc)}
                )
        missing_name_ids = [
            account_id
            for account_id, account in resolved.items()
            if not str(account.get("advertiser_name") or "").strip()
        ]
        evidence["account_names_complete"] = not missing_name_ids
        if missing_name_ids:
            try:
                public_rows = self.list_advertiser_public_info(missing_name_ids)
                public_by_id = {
                    text_id(row.get("advertiser_id")): row for row in public_rows
                }
                for account_id in missing_name_ids:
                    public = public_by_id.get(account_id) or {}
                    public_name = str(public.get("advertiser_name") or "").strip()
                    if public_name:
                        resolved[account_id]["advertiser_name"] = public_name
                evidence["account_names_complete"] = all(
                    str(account.get("advertiser_name") or "").strip()
                    for account in resolved.values()
                )
            except Exception as exc:
                # Account IDs are still valid and may be selected.  A name lookup
                # failure must not make the plan catalog itself incomplete.
                evidence["account_name_error"] = str(exc)
        result = list(resolved.values())
        with self._business_account_cache_lock:
            self._business_account_cache = (
                time.monotonic(),
                [dict(item) for item in result],
                json.loads(json.dumps(evidence, ensure_ascii=False)),
            )
        return result, evidence

    @staticmethod
    def _default_plan_window() -> tuple[str, str]:
        end = datetime.now()
        start = end - timedelta(days=179)
        return start.strftime("%Y-%m-%d 00:00:00"), end.strftime("%Y-%m-%d 23:59:59")

    def list_plans(
        self,
        advertiser_id: Any,
        *,
        marketing_goal: str,
        adlab_scene: str = "",
        start_time: str = "",
        end_time: str = "",
        fields: Optional[Iterable[str]] = None,
        filtering: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        goal = str(marketing_goal or "").strip().upper()
        if goal not in self.MARKETING_GOALS:
            raise ValueError("marketing_goal 仅支持 LIVE_PROM_GOODS 或 VIDEO_PROM_GOODS")
        scene = str(adlab_scene or "").strip().upper()
        if scene and scene not in {"UNI_PROJECT", "OVERALL_PROJECT"}:
            raise ValueError("adlab_scene 仅支持 UNI_PROJECT 或 OVERALL_PROJECT")
        if not start_time or not end_time:
            start_time, end_time = self._default_plan_window()
        query: dict[str, Any] = {
            "advertiser_id": aid,
            "start_time": start_time,
            "end_time": end_time,
            "marketing_goal": goal,
            "fields": list(fields or []),
        }
        if scene:
            query["adlab_scene"] = scene
        if filtering:
            query["filtering"] = dict(filtering)
        rows, request_ids = self.client.get_all_pages(
            self.PLAN_LIST, query, advertiser_id=aid, page_size=100
        )
        return [normalize_plan(row, advertiser_id=aid) for row in rows], request_ids

    def list_all_plans(
        self,
        advertiser_id: Any,
        *,
        progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        combined: list[dict[str, Any]] = []
        evidence: dict[str, Any] = {"complete": True, "classes": {}}
        # The official endpoint defaults to UNI_PROJECT when adlab_scene is
        # omitted.  Query all four classes explicitly or Chengfang plans are
        # silently absent even though the request itself succeeds.
        classes = (
            ("chengfang_live", "LIVE_PROM_GOODS", "OVERALL_PROJECT"),
            ("chengfang_product", "VIDEO_PROM_GOODS", "OVERALL_PROJECT"),
            ("global_live", "LIVE_PROM_GOODS", "UNI_PROJECT"),
            ("global_product", "VIDEO_PROM_GOODS", "UNI_PROJECT"),
        )
        for class_index, (class_key, goal, scene) in enumerate(classes, 1):
            if progress_callback:
                progress_callback(
                    {
                        "phase": "catalog_classes",
                        "class_key": class_key,
                        "class_index": class_index,
                        "class_total": len(classes),
                        "completed_classes": class_index - 1,
                        "discovered_plans": len(combined),
                    }
                )
            try:
                plans, request_ids = self.list_plans(
                    aid,
                    marketing_goal=goal,
                    adlab_scene=scene,
                )
                combined.extend(plans)
                evidence["classes"][class_key] = {
                    "complete": True,
                    "count": len(plans),
                    "marketing_goal": goal,
                    "adlab_scene": scene,
                    "request_ids": request_ids,
                }
            except Exception as exc:
                evidence["complete"] = False
                evidence["classes"][class_key] = {
                    "complete": False,
                    "count": 0,
                    "marketing_goal": goal,
                    "adlab_scene": scene,
                    "error": str(exc),
                }
            if progress_callback:
                progress_callback(
                    {
                        "phase": "catalog_classes",
                        "class_key": class_key,
                        "class_index": class_index,
                        "class_total": len(classes),
                        "completed_classes": class_index,
                        "discovered_plans": len(combined),
                    }
                )
        deduped: dict[str, dict[str, Any]] = {}
        for plan in combined:
            if plan.get("ad_id"):
                deduped[str(plan["ad_id"])] = plan
        return list(deduped.values()), evidence

    def get_plan_detail(self, advertiser_id: Any, ad_id: Any) -> tuple[dict[str, Any], ApiResponse]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        response = self.client.get(
            self.PLAN_DETAIL,
            {"advertiser_id": aid, "ad_id": pid},
            advertiser_id=aid,
        )
        data = response.data if isinstance(response.data, Mapping) else {}
        return normalize_plan(data, advertiser_id=aid), response

    def list_plan_materials(
        self,
        advertiser_id: Any,
        ad_id: Any,
        *,
        start_date: str,
        end_date: str,
        fields: Optional[Iterable[str]] = None,
        delivery_only: bool = False,
        parallel_workers: int = 1,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        filtering = {
            "material_type": "VIDEO",
            "start_date": start_date,
            "end_date": end_date,
            "material_select_type": "ALL",
        }
        if delivery_only:
            # This value is accepted by the live Open API contract and cuts a
            # large live plan from every historical material to only the rows
            # that can currently participate in monitoring and rules.
            filtering["material_status"] = "DELIVERY_OK"
        query: dict[str, Any] = {
            "advertiser_id": aid,
            "ad_id": pid,
            "filtering": filtering,
            "fields": list(fields or []),
        }
        if not delivery_only:
            # Legacy callers keep their existing spend ordering. The active
            # scanner intentionally uses the API's stable default order so a
            # changing spend value cannot move rows between parallel pages.
            query.update(
                {
                    "order_type": "DESC",
                    "order_field": "stat_cost_for_roi2",
                }
            )
        rows, request_ids = self.client.get_all_pages(
            self.PLAN_MATERIALS,
            query,
            advertiser_id=aid,
            page_size=100,
            parallel_workers=(
                max(1, min(3, int(parallel_workers or 1)))
                if delivery_only
                else 1
            ),
            identity_getter=(
                (lambda row: normalize_material(row).get("material_id"))
                if delivery_only
                else None
            ),
            verify_stability=bool(delivery_only),
        )
        materials = [normalize_material(row) for row in rows]
        materials = [
            item
            for item in materials
            if item["material_id"] not in {"", "-2"}
            and item["material_type"] == "VIDEO"
        ]
        if delivery_only:
            active_statuses = {"DELIVERY_OK"}
            passed_audits = {"PASS", "PASSED", "APPROVED", "AUDIT_PASS"}
            inactive_show_statuses = {
                "PAUSE",
                "PAUSED",
                "DISABLE",
                "DISABLED",
                "DELETED",
                "INVALID",
                "FAILED",
                "REJECTED",
            }
            materials = [
                item
                for item in materials
                if str(item.get("material_status") or "").strip().upper()
                in active_statuses
                and str(item.get("audit_status") or "").strip().upper()
                in passed_audits
                and str(
                    first(
                        item.get("raw") or {},
                        "show_status",
                        "showStatus",
                        "delivery_status",
                    )
                    or ""
                ).strip().upper()
                not in inactive_show_statuses
            ]
        return materials, request_ids

    def list_plan_products(
        self,
        advertiser_id: Any,
        ad_id: Any,
        *,
        start_date: str,
        end_date: str,
        fields: Optional[Iterable[str]] = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        rows, request_ids = self.client.get_all_pages(
            self.PLAN_PRODUCTS,
            {
                "advertiser_id": aid,
                "ad_id": pid,
                "start_date": str(start_date or "").strip(),
                "end_date": str(end_date or "").strip(),
                # The endpoint requires this member, while identity/name fields
                # are returned by default and rejected when listed explicitly.
                "fields": list(fields or []),
            },
            advertiser_id=aid,
            page_size=100,
        )
        return [normalize_product(row) for row in rows], request_ids

    def get_report_config(
        self,
        advertiser_id: Any,
        *,
        plan_system: str,
        promotion_scene: str,
    ) -> tuple[dict[str, str], ApiResponse]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        system = str(plan_system or "").strip().lower()
        scene = str(promotion_scene or "").strip().lower()
        topic = self.REPORT_MATERIAL_TOPICS.get((system, scene))
        if not topic:
            raise ValueError("plan_system/promotion_scene 必须是已确认的乘方/全域 × 推直播/推商品")
        response = self.client.get(
            self.REPORT_CONFIG,
            {"advertiser_id": aid, "data_topics": [topic]},
            advertiser_id=aid,
        )
        units = build_metric_unit_map(response.data)
        # The live config contract currently returns metric fields without the
        # documented ``unit`` member. Values from the plan-material endpoint
        # are already expressed in their documented business units (yuan,
        # counts, ROI/rates), so retain them without scaling rather than
        # rejecting a complete read-only batch.
        if not units and isinstance(response.data, Mapping):
            for config in response.data.get("custom_config_datas") or []:
                if not isinstance(config, Mapping):
                    continue
                for metric in config.get("metrics") or []:
                    if isinstance(metric, Mapping) and metric.get("field"):
                        units.setdefault(str(metric["field"]), "0")
        return units, response

    def query_report(self, advertiser_id: Any, query: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        payload = dict(query)
        payload["advertiser_id"] = aid
        page_size = int(payload.pop("page_size", 100) or 100)
        return self.client.get_all_pages(
            self.REPORT_DATA,
            payload,
            advertiser_id=aid,
            page_size=page_size,
        )

    @staticmethod
    def _report_value(block: Any) -> Any:
        if isinstance(block, Mapping):
            for key in ("Value", "value", "ValueStr", "value_str"):
                if block.get(key) not in (None, ""):
                    return block.get(key)
            return None
        return block

    def list_material_report(
        self,
        advertiser_id: Any,
        *,
        plan_system: str,
        promotion_scene: str,
        start_date: str,
        end_date: str,
        metrics: Iterable[str],
        filter_context: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Read authoritative material metrics for one account/topic/day.

        Plan membership still comes from ``ad/material/get``. The report topic
        aggregates by material and is intersected with each monitored plan by
        the collector, matching the platform's own all-domain/Chengfang UI.
        """
        aid = require_digit_id(advertiser_id, "advertiser_id")
        system = str(plan_system or "").strip().lower()
        scene = str(promotion_scene or "").strip().lower()
        topic = self.REPORT_MATERIAL_TOPICS.get((system, scene))
        dimensions = self.REPORT_MATERIAL_DIMENSIONS.get((system, scene))
        if not topic or not dimensions:
            raise ValueError("计划体系或推广场景未确认，无法查询素材报表")
        metric_fields = [str(field) for field in metrics if str(field)]
        if not metric_fields:
            raise ValueError("素材报表至少需要一个指标")
        filters: list[dict[str, Any]] = []
        if system == "chengfang":
            # Chengfang material topics require the explicit video material
            # type even though the topic name itself already says VIDEO.
            filters.append(
                {
                    "field": "roi2_material_type_v3",
                    "operator": 7,
                    "values": ["3"],
                }
            )
        elif system == "global" and scene == "live":
            context = {
                str(key): str(value or "").strip()
                for key, value in dict(filter_context or {}).items()
            }
            required = (
                "anchor_id",
                "aggregate_smart_bid_type",
                "ecp_app_id",
            )
            missing = [field for field in required if not context.get(field)]
            if missing:
                raise ValueError(
                    "全域推直播素材报表缺少计划详情筛选证据："
                    + ",".join(missing)
                )
            if context["aggregate_smart_bid_type"] not in {"0", "7"}:
                raise ValueError("全域推直播投放类型证据无效")
            if context["ecp_app_id"] not in {"1", "2"}:
                raise ValueError("全域推直播下单平台证据无效")
            filters.extend(
                {
                    "field": field,
                    "operator": 7,
                    "values": [context[field]],
                }
                for field in required
            )
        rows, request_ids = self.client.get_all_pages(
            self.REPORT_DATA,
            {
                "advertiser_id": aid,
                "data_topic": topic,
                "dimensions": list(dimensions),
                "metrics": metric_fields,
                "filters": filters,
                "start_time": f"{str(start_date).strip()} 00:00:00",
                "end_time": f"{str(end_date).strip()} 23:59:59",
                "order_by": [{"field": "material_id", "type": 1}],
                "data_period": "ALL_DATA",
            },
            advertiser_id=aid,
            page_size=200,
            identity_getter=lambda row: self._report_value(
                (row.get("dimensions") or {}).get("material_id")
            ),
            verify_stability=True,
        )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            dimensions = row.get("dimensions") or {}
            raw_metrics = row.get("metrics") or {}
            material_id = text_id(
                self._report_value(dimensions.get("material_id"))
            )
            if not material_id:
                continue
            normalized.append(
                {
                    "material_id": material_id,
                    "material_name": str(
                        self._report_value(
                            dimensions.get("roi2_material_video_name")
                        )
                        or ""
                    ),
                    "stats_info": {
                        str(field): self._report_value(block)
                        for field, block in raw_metrics.items()
                    },
                    "raw": dict(row),
                }
            )
        return normalized, request_ids

    def list_control_tasks(
        self,
        advertiser_id: Any,
        *,
        ad_id: Any,
        marketing_goal: str,
        start_time: str,
        end_time: str,
        scene: str = "MATERIAL_ADD_BUDGET",
        active_only: bool = False,
        fields: Optional[Iterable[str]] = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        query = {
            "advertiser_id": aid,
            "ad_id": pid,
            "marketing_goal": str(marketing_goal).upper(),
            "start_time": start_time,
            "end_time": end_time,
            "scene": scene,
            "fields": list(fields or CONTROL_TASK_METRIC_FIELDS),
        }
        if active_only:
            # The live contract enumerates PROCESSING as the only running
            # Scene-2 task status. Offline/frozen/disabled/history states are
            # reconciled by the hourly low-frequency pass instead.
            query["filtering"] = {"task_status": "PROCESSING"}
        rows, request_ids = self.client.get_all_pages(
            self.CONTROL_LIST,
            query,
            advertiser_id=aid,
            page_size=100,
            identity_getter=lambda row: text_id(
                first(row, "task_id", "control_task_id", "assist_task_id", "id")
            ),
            verify_stability=True,
            parallel_workers=1,
        )
        normalized = [normalize_control_task(row) for row in rows]
        for item in normalized:
            if str(item.get("status") or "").strip():
                # A status echoed by a server-filtered PROCESSING request is
                # explicit, but it is not the unfiltered observation required
                # to open a new cycle after a confirmed stop.
                item["status_source"] = "api_filtered" if active_only else "api"
            else:
                item["status_source"] = "missing"
        if active_only:
            # The server already accepted the exact PROCESSING filter.  Some
            # task rows still return ``task_status=null``; only in this scoped
            # request may the missing echo inherit the filter value.  Never
            # infer a state for the unfiltered history query.
            for item in normalized:
                if not str(item.get("status") or "").strip():
                    item["status"] = "PROCESSING"
                    item["status_source"] = "request_filter_inferred"
        return normalized, request_ids

    def find_duplicate_control_task(
        self,
        advertiser_id: Any,
        *,
        ad_id: Any,
        marketing_goal: str,
        task_name: str,
        budget: Any,
        duration: Any,
        material_ids: Iterable[Any],
    ) -> Optional[dict[str, Any]]:
        now = datetime.now()
        tasks, _ = self.list_control_tasks(
            advertiser_id,
            ad_id=ad_id,
            marketing_goal=marketing_goal,
            start_time=(now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00"),
            end_time=(now + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59"),
        )
        wanted = set(stable_material_set(material_ids))
        wanted_name = str(task_name or "").strip()
        if not wanted_name:
            return None
        wanted_budget = Decimal(str(budget))
        wanted_duration = (
            None if duration in (None, "") else Decimal(str(duration))
        )
        for task in tasks:
            # Existing control tasks are valid business objects, not implicit
            # duplicates.  Only the stable name generated for this exact local
            # execution is an idempotency marker.  This lets a user explicitly
            # create another retarget for the same material/budget/duration,
            # while still reconciling a retry of the same Feishu confirmation.
            if str(task.get("task_name") or "").strip() != wanted_name:
                continue
            existing = set(stable_material_set(task.get("material_ids") or []))
            try:
                same_numbers = Decimal(str(task.get("budget"))) == wanted_budget
                if wanted_duration is not None:
                    same_numbers = (
                        same_numbers
                        and Decimal(str(task.get("duration"))) == wanted_duration
                    )
            except Exception:
                same_numbers = False
            # The name, frozen group and parameters must all identify the same
            # execution.  Overlapping or even identical groups from a different
            # confirmation remain legal business operations.
            if existing == wanted and same_numbers:
                return task
        return None

    def create_material_control_task(
        self,
        advertiser_id: Any,
        *,
        ad_id: Any,
        marketing_goal: str,
        name: str,
        budget: Any,
        duration: Any,
        material_ids: Iterable[Any],
        extra: Optional[Mapping[str, Any]] = None,
        before_send: Optional[Callable[[], None]] = None,
    ) -> ApiResponse:
        self._require_writes()
        aid = require_digit_id(advertiser_id, "advertiser_id")
        pid = require_digit_id(ad_id, "ad_id")
        materials = stable_material_set(material_ids)
        # 千川调控任务名称上限为50字符；唯一执行标识也计入该上限。
        task_name = str(name or "素材追投")[:50]
        body = build_material_control_task_body(
            advertiser_id=aid,
            ad_id=pid,
            marketing_goal=marketing_goal,
            name=task_name,
            budget=budget,
            duration=duration,
            material_ids=materials,
            extra=extra,
        )
        money = Decimal(str(body["budget"]))
        hours = (
            None
            if "duration" not in body
            else Decimal(str(body["duration"]))
        )
        duplicate = self.find_duplicate_control_task(
            aid,
            ad_id=pid,
            marketing_goal=marketing_goal,
            task_name=task_name,
            budget=money,
            duration=hours,
            material_ids=materials,
        )
        if duplicate:
            # A retry/recovery of the same local execution is already complete
            # on the platform.  Return its task ID as a reconciled success and
            # never submit a second POST.
            return ApiResponse(
                data={"task_id": duplicate.get("task_id")},
                raw={"code": 0, "reconciled_existing": True},
                request_id="",
                message="同一执行任务已存在，已完成幂等对账",
            )
        return self.client.post(self.CONTROL_CREATE, body, advertiser_id=aid,
                                **({"before_send": before_send} if before_send is not None else {}))

    def update_control_status(self, advertiser_id: Any, task_ids: Iterable[Any], *, action: str,
                              before_send: Optional[Callable[[], None]] = None) -> ApiResponse:
        self._require_writes()
        aid = require_digit_id(advertiser_id, "advertiser_id")
        ids = tuple(dict.fromkeys(require_digit_id(value, "task_id") for value in task_ids))
        if not 1 <= len(ids) <= 10:
            raise ValueError("一次只能操作 1 至 10 条已验证调控任务")
        opt_type = str(action or "").strip().upper()
        if opt_type not in {"PAUSE", "DISABLE", "ENABLE"}:
            raise ValueError("调控任务状态仅允许 PAUSE、DISABLE 或 ENABLE；禁止自动 DELETE")
        return self.client.post(
            self.CONTROL_STATUS,
            {
                "advertiser_id": id_number(aid, "advertiser_id"),
                "task_ids": [id_number(value, "task_id") for value in ids],
                "opt_type": opt_type,
            },
            advertiser_id=aid,
            **({"before_send": before_send} if before_send is not None else {}),
        )

    def update_control_budget(self, advertiser_id: Any, task_id: Any, budget: Any, *,
                              before_send: Optional[Callable[[], None]] = None) -> ApiResponse:
        self._require_writes()
        aid = require_digit_id(advertiser_id, "advertiser_id")
        return self.client.post(
            self.CONTROL_BUDGET,
            {"advertiser_id": id_number(aid, "advertiser_id"), "task_id": id_number(task_id, "task_id"), "budget": float(Decimal(str(budget)))},
            advertiser_id=aid,
            **({"before_send": before_send} if before_send is not None else {}),
        )

    def update_control_duration(self, advertiser_id: Any, task_id: Any, duration: Any, *,
                                before_send: Optional[Callable[[], None]] = None) -> ApiResponse:
        self._require_writes()
        aid = require_digit_id(advertiser_id, "advertiser_id")
        return self.client.post(
            self.CONTROL_DURATION,
            {"advertiser_id": id_number(aid, "advertiser_id"), "task_id": id_number(task_id, "task_id"), "duration": float(Decimal(str(duration)))},
            advertiser_id=aid,
            **({"before_send": before_send} if before_send is not None else {}),
        )

    def list_operation_logs(
        self,
        advertiser_id: Any,
        *,
        start_time: str,
        end_time: str,
        object_type: str = "ACCOUNT",
        object_id: Any = "",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        aid = require_digit_id(advertiser_id, "advertiser_id")
        kind = str(object_type or "ACCOUNT").strip().upper()
        if kind not in {"ACCOUNT", "AD"}:
            raise ValueError("操作日志 object_type 仅支持 ACCOUNT 或 AD")
        query: dict[str, Any] = {
            "advertiser_id": aid,
            "object_type": kind,
            "start_time": start_time,
            "end_time": end_time,
        }
        if kind == "AD":
            query["object_id"] = require_digit_id(object_id, "object_id")
        rows, request_ids = self.client.get_all_pages(
            self.OPERATION_LOGS,
            query,
            advertiser_id=aid,
            page_size=20,
            identity_getter=operation_log_row_identity,
            verify_stability=True,
            parallel_workers=1,
        )
        return [normalize_operation_log(row) for row in rows], request_ids
