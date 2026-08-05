"""平台适配器协议、分页完整性和失败关闭实现。"""

from __future__ import annotations

import hashlib
import json
import math
from abc import ABC
from typing import Any, Protocol

from ..security import PlatformNetworkGuard, PlatformWriteBlocked, stable_json_hash
from .models import (
    AccountIdentity,
    NormalizedControlTask,
    NormalizedMaterial,
    NormalizedPlan,
    PageResult,
    PaginatedResult,
    money_to_cent,
    to_decimal,
)


class ReadTransport(Protocol):
    def request(
        self,
        method: str,
        endpoint_path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]: ...


def _first(value: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        if name in value and value[name] not in (None, ""):
            return value[name]
    return default


def _dig(payload: Any, paths: tuple[tuple[str, ...], ...]) -> Any:
    for path in paths:
        current = payload
        ok = True
        for key in path:
            if not isinstance(current, dict) or key not in current:
                ok = False
                break
            current = current[key]
        if ok:
            return current
    return None


def response_schema_hash(payload: Any) -> str:
    def shape(value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return type(value).__name__
        if isinstance(value, dict):
            return {str(key): shape(value[key], depth + 1) for key in sorted(value)}
        if isinstance(value, list):
            return [shape(value[0], depth + 1)] if value else []
        return type(value).__name__

    return stable_json_hash(shape(payload))


class PlatformAdapter(ABC):
    adapter_name = "base"
    adapter_version = "v1a-1"
    plan_system = "unknown"
    promotion_scene = "unknown"
    mar_goal = 0
    plan_dataset = ""
    material_dataset = ""
    evidence_level = "D"
    read_capability_state = "unobserved"

    PLAN_ENDPOINT = "/ad/api/pmc/v1/uni-promotion/ad/list-required"
    MATERIAL_ENDPOINT = "/ad/api/pmc/v1/uni-promotion/material/list-required"
    CONTROL_ENDPOINT = "/ad/api/pmc/v1/uni-promotion/ad/list-required"
    OPERATION_LOG_ENDPOINT = "/ad/api/pmc/v1/ad/get_opt_log"

    def __init__(self, transport: ReadTransport, guard: PlatformNetworkGuard | None = None):
        self.transport = transport
        self.guard = guard or PlatformNetworkGuard()

    @property
    def identity(self) -> tuple[str, str]:
        return self.plan_system, self.promotion_scene

    def request(
        self,
        method: str,
        endpoint_path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.guard.assert_allowed(method, endpoint_path)
        return self.transport.request(method, endpoint_path, query=query, body=body)

    def fetch_account_identity(self, aavid: str) -> AccountIdentity:
        payload = self.request(
            "GET",
            "/ad/api/v1/account/user/info",
            query={"aavid": aavid},
        )
        info = _dig(payload, (("data", "accountInfo"),))
        if not isinstance(info, dict):
            raise ValueError("schema_changed: data.accountInfo missing")
        adv_id = str(info.get("advId") or "")
        adv_name = str(info.get("advName") or "").strip()
        if adv_id != str(aavid):
            raise ValueError("account_context_conflict")
        if not adv_name:
            raise ValueError("schema_changed: data.accountInfo.advName missing")
        return AccountIdentity(aavid=adv_id, account_name=adv_name)

    def discover_plans(self, aavid: str, page_size: int = 100) -> PaginatedResult:
        def fetch(page: int) -> PageResult:
            body = self.plan_request_body(aavid, page, page_size)
            payload = self.request("POST", self.PLAN_ENDPOINT, body=body)
            return self.extract_page(payload, page, page_size)

        return self._paginate(
            fetch,
            lambda row, _aavid: self.normalize_plan(row, aavid),
            unique_key=lambda row: row.ad_id,
        )

    def plan_request_body(self, aavid: str, page: int, page_size: int) -> dict[str, Any]:
        return {
            "aavid": aavid,
            "Page": page,
            "PageSize": page_size,
            "MarGoal": self.mar_goal,
            "SophonxDataSetKey": self.plan_dataset,
        }

    def verify_plan(self, aavid: str, ad_id: str) -> NormalizedPlan:
        payload = self.request(
            "GET",
            "/ad/api/creation/v1/ad/ad-detail-basic",
            query={"aavid": aavid, "adid": ad_id},
        )
        data = _dig(payload, (("data",),))
        if not isinstance(data, dict):
            raise ValueError("schema_changed: plan detail data missing")
        row = data.get("adInfo") if isinstance(data.get("adInfo"), dict) else data
        normalized = self.normalize_plan(dict(row), aavid)
        if normalized.ad_id != str(ad_id):
            raise ValueError("plan_context_conflict")
        return normalized

    def fetch_materials(self, aavid: str, ad_id: str, page_size: int = 100) -> PaginatedResult:
        def fetch(page: int) -> PageResult:
            body = self.material_request_body(aavid, ad_id, page, page_size)
            payload = self.request("POST", self.MATERIAL_ENDPOINT, body=body)
            return self.extract_page(payload, page, page_size)

        return self._paginate(
            fetch,
            lambda row, _aavid: self.normalize_material(row, aavid, ad_id),
            unique_key=lambda row: row.material_id,
            drop_none=True,
        )

    def material_request_body(
        self, aavid: str, ad_id: str, page: int, page_size: int
    ) -> dict[str, Any]:
        return {
            "aavid": aavid,
            "Page": page,
            "PageSize": page_size,
            "MarGoal": self.mar_goal,
            "AggregateAid": ad_id,
            "DataSetKey": self.material_dataset,
            "MaterialType": 3 if self.promotion_scene == "product" else 4,
        }

    def fetch_control_tasks(
        self,
        aavid: str,
        ad_id: str,
        assist_task_scene: int,
        page_size: int = 100,
    ) -> PaginatedResult:
        if assist_task_scene not in (1, 2, 3):
            raise ValueError("assist_task_scene must be 1, 2 or 3")

        def fetch(page: int) -> PageResult:
            body = {
                "aavid": aavid,
                "Page": page,
                "PageSize": page_size,
                "PrimaryAID": ad_id,
                "AssistTaskScene": assist_task_scene,
                "MarGoal": self.mar_goal,
                "SophonxDataSetKey": self.control_dataset(assist_task_scene),
            }
            payload = self.request("POST", self.CONTROL_ENDPOINT, body=body)
            return self.extract_page(payload, page, page_size)

        return self._paginate(
            fetch,
            lambda row, _aavid: self.normalize_control_task(
                row, aavid, ad_id, assist_task_scene
            ),
            unique_key=lambda row: row.control_task_id,
        )

    def fetch_operation_logs(
        self, aavid: str, ad_id: str, start_time: str, end_time: str, page: int = 1
    ) -> PageResult:
        payload = self.request(
            "GET",
            self.OPERATION_LOG_ENDPOINT,
            query={
                "aavid": aavid,
                "objectID": ad_id,
                "startTime": start_time,
                "endTime": end_time,
                "page": page,
            },
        )
        return self.extract_page(payload, page, 100)

    def get_capabilities(self) -> dict[str, dict[str, Any]]:
        state = "dry_run_ready" if self.plan_system == "chengfang" else "blocked_by_evidence"
        result: dict[str, dict[str, Any]] = {}
        for name in (
            "can_create_volume_retarget",
            "can_create_cost_control_retarget",
            "can_pause_control_task",
            "can_increase_total_budget",
            "can_increase_daily_budget",
            "can_extend_duration",
            "can_adjust_budget_and_duration_atomically",
        ):
            capability_state = state if name == "can_pause_control_task" else "blocked_by_evidence"
            result[name] = {
                "enabled": False,
                "state": capability_state,
                "adapter_version": self.adapter_version,
                "last_verified_at": None,
            }
        return result

    # V1A 服务层不注册写命令；适配器本身也直接拒绝。网络守卫是第三道防线。
    def preflight_create_retarget(self, _snapshot: dict[str, Any]) -> dict[str, Any]:
        return {"allowed": False, "state": "blocked_by_evidence", "mode": "dry_run"}

    def create_retarget(self, _snapshot: dict[str, Any], _idempotency_key: str) -> None:
        raise PlatformWriteBlocked("POST", "adapter://create-retarget", "adapter_v1a_read_only")

    def preflight_pause_control_task(self, _snapshot: dict[str, Any]) -> dict[str, Any]:
        return {"allowed": False, "state": "dry_run_ready", "mode": "dry_run"}

    def pause_control_task(self, _snapshot: dict[str, Any], _idempotency_key: str) -> None:
        raise PlatformWriteBlocked("POST", "adapter://pause-control-task", "adapter_v1a_read_only")

    def preflight_adjust_control_task(self, _snapshot: dict[str, Any]) -> dict[str, Any]:
        return {"allowed": False, "state": "blocked_by_evidence", "mode": "dry_run"}

    def adjust_control_task(self, _snapshot: dict[str, Any], _idempotency_key: str) -> None:
        raise PlatformWriteBlocked("POST", "adapter://adjust-control-task", "adapter_v1a_read_only")

    def control_dataset(self, assist_task_scene: int) -> str:
        return ""

    def normalize_plan(self, row: dict[str, Any], aavid: str) -> NormalizedPlan:
        ad_id = str(_first(row, ("adId", "ad_id", "id", "AdId"), ""))
        plan_name = str(_first(row, ("adName", "planName", "name", "AdName"), "")).strip()
        if not ad_id or not plan_name:
            raise ValueError("schema_changed: plan id or name missing")
        goal = _first(row, ("MarGoal", "marGoal", "marketing_goal", "marketingGoal"))
        try:
            goal_value = int(goal)
        except (TypeError, ValueError):
            goal_value = self.mar_goal
        scene = "product" if goal_value == 1 else "live" if goal_value == 2 else "unknown"
        explicit_system = str(
            _first(row, ("plan_system", "planSystem", "deliverySystem", "promotionSystem"), "")
        ).lower()
        system = self.plan_system
        verification = "verified"
        if self.evidence_level == "C" and explicit_system not in {"global", "chengfang"}:
            verification = "evidence_pending"
        if scene != self.promotion_scene:
            verification = "conflict"
        if explicit_system in {"global", "chengfang"} and explicit_system != self.plan_system:
            verification = "conflict"
        if verification == "conflict":
            system = "unknown"
        return NormalizedPlan(
            aavid=str(aavid),
            ad_id=ad_id,
            plan_name=plan_name,
            plan_system=system,
            promotion_scene=scene,
            platform_status=str(
                _first(row, ("platform_status", "status", "deliveryStatus", "adDeliveryType"), "unknown")
            ),
            verification_state=verification,
            adapter_version=self.adapter_version,
            raw_evidence={
                "MarGoal": goal,
                "AdlabScene": row.get("AdlabScene"),
                "IsOverallRoi": row.get("IsOverallRoi"),
                "explicit_system": explicit_system or None,
            },
        )

    def normalize_material(
        self, row: dict[str, Any], aavid: str, ad_id: str
    ) -> NormalizedMaterial | None:
        material_id = str(
            _first(row, ("material_id", "materialId", "id", "roi2_material_id"), "")
        )
        video_subtype = _first(
            row,
            ("roi2_material_video_type", "video_type", "VideoType"),
        )
        generic_material_type = _first(row, ("material_type", "MaterialType"))
        # roi2_material_video_type 的 2/3 分别是自选投放视频和智能优选视频，
        # 不能按通用素材枚举解释。V1A 采用正向证据：未知类型也不进入候选。
        has_video_signal = bool(
            _first(row, ("video_id", "videoId", "video_play_info"))
            or video_subtype not in (None, "")
            or str(generic_material_type or "").strip().lower()
            in {"video", "short_video", "视频"}
        )
        if not material_id or not has_video_signal:
            return None
        delivery = _first(row, ("roi2_material_status", "material_status", "delivery_status"))
        show = _first(row, ("roi2_material_show_status", "show_status"))
        audit = _first(row, ("audit_status", "material_audit_status"))
        block = _first(row, ("block_status", "material_block_status"))
        in_delivery = str(delivery) == "1"
        show_ok = str(show).lower() in {"1", "true", "allowed", "normal", "可展示"}
        audit_ok = str(audit).lower() in {"1", "true", "approved", "pass", "审核通过"}
        block_ok = str(block).lower() in {"0", "false", "none", "unblocked", "未屏蔽"}
        effective = in_delivery and show_ok and audit_ok and block_ok
        spend = _first(row, ("stat_cost", "cost", "spend", "advertiser_cost"), 0)
        orders = _first(
            row,
            ("order_count", "overall_order_count", "prepay_pay_order_count", "orders"),
            0,
        )
        gmv = _first(
            row,
            ("gmv", "pay_gmv_include_coupon", "成交金额", "order_amount"),
            0,
        )
        spend_cent = money_to_cent(spend)
        gmv_cent = money_to_cent(gmv)
        roi = None if spend_cent == 0 else str((to_decimal(gmv) or 0) / (to_decimal(spend) or 1))
        product_ids_raw = _first(row, ("product_ids", "productIds", "goods_ids"), [])
        if isinstance(product_ids_raw, str):
            product_ids = tuple(filter(None, (part.strip() for part in product_ids_raw.split(","))))
        elif isinstance(product_ids_raw, list):
            product_ids = tuple(str(item) for item in product_ids_raw if item not in (None, ""))
        else:
            product_ids = ()
        return NormalizedMaterial(
            aavid=str(aavid),
            ad_id=str(ad_id),
            material_id=material_id,
            material_name=str(
                _first(
                    row,
                    ("roi2_material_video_name", "material_name", "materialName", "video_name", "name"),
                    material_id,
                )
            ),
            material_created_at=(
                str(_first(row, ("material_created_at", "video_create_time", "create_time")))
                if _first(row, ("material_created_at", "video_create_time", "create_time"))
                else None
            ),
            delivery_status=None if delivery is None else str(delivery),
            show_status=None if show is None else str(show),
            show_status_reason=(
                str(_first(row, ("roi2_material_show_status_reason", "show_status_reason")))
                if _first(row, ("roi2_material_show_status_reason", "show_status_reason"))
                else None
            ),
            audit_status=None if audit is None else str(audit),
            block_status=None if block is None else str(block),
            is_in_delivery_list=in_delivery,
            is_effectively_deliverable=effective,
            product_ids=product_ids,
            spend_cent=spend_cent,
            order_count=int(to_decimal(orders) or 0),
            gmv_cent=gmv_cent,
            roi_decimal=roi,
            platform_raw_status={
                "delivery_status": delivery,
                "show_status": show,
                "audit_status": audit,
                "block_status": block,
            },
        )

    def normalize_control_task(
        self,
        row: dict[str, Any],
        aavid: str,
        ad_id: str,
        assist_task_scene: int,
    ) -> NormalizedControlTask:
        task_id = str(_first(row, ("control_task_id", "taskId", "assistTaskId", "id"), ""))
        if not task_id:
            raise ValueError("schema_changed: control task id missing")
        materials = _first(row, ("material_ids", "materialIds", "materialIdList"), [])
        if not isinstance(materials, list):
            materials = []
        current_budget = _first(row, ("budget_current", "totalBudget", "budget"))
        used_budget = _first(row, ("budget_used", "usedBudget", "cost"))
        revision = stable_json_hash(
            {
                "status": _first(row, ("platform_status", "status", "deliveryStatus"), "unknown"),
                "budget": current_budget,
                "end_time": _first(row, ("end_time", "endTime")),
                "materials": sorted(str(value) for value in materials),
                "updated_at": _first(row, ("updated_at", "updateTime")),
            }
        )
        return NormalizedControlTask(
            aavid=str(aavid),
            source_plan_id=str(ad_id),
            control_task_id=task_id,
            task_name=str(_first(row, ("task_name", "taskName", "name"), task_id)),
            assist_task_scene=assist_task_scene,
            retarget_method=(
                str(_first(row, ("retarget_method", "retargetMethod")))
                if _first(row, ("retarget_method", "retargetMethod"))
                else None
            ),
            material_ids=tuple(sorted(str(value) for value in materials)),
            platform_status=str(
                _first(row, ("platform_status", "status", "deliveryStatus"), "unknown")
            ),
            budget_kind=(
                str(_first(row, ("budget_kind", "budgetType")))
                if _first(row, ("budget_kind", "budgetType"))
                else None
            ),
            budget_current_cent=(
                money_to_cent(current_budget) if current_budget is not None else None
            ),
            budget_used_cent=(money_to_cent(used_budget) if used_budget is not None else None),
            duration_hours_decimal=(
                str(_first(row, ("duration_hours", "durationHours")))
                if _first(row, ("duration_hours", "durationHours")) is not None
                else None
            ),
            start_time_utc=(
                str(_first(row, ("start_time", "startTime")))
                if _first(row, ("start_time", "startTime"))
                else None
            ),
            end_time_utc=(
                str(_first(row, ("end_time", "endTime")))
                if _first(row, ("end_time", "endTime"))
                else None
            ),
            roi_or_bid_decimal=(
                str(_first(row, ("roi_or_bid", "roiGoal", "bid")))
                if _first(row, ("roi_or_bid", "roiGoal", "bid")) is not None
                else None
            ),
            updated_at_platform=(
                str(_first(row, ("updated_at", "updateTime")))
                if _first(row, ("updated_at", "updateTime"))
                else None
            ),
            task_revision_fingerprint=revision,
        )

    @staticmethod
    def extract_page(payload: dict[str, Any], page: int, page_size: int) -> PageResult:
        rows = _dig(
            payload,
            (
                ("data", "list"),
                ("data", "rows"),
                ("data", "items"),
                ("data", "data"),
                ("list",),
                ("rows",),
            ),
        )
        if not isinstance(rows, list):
            raise ValueError("schema_changed: page rows missing")
        total = _dig(
            payload,
            (
                ("data", "total"),
                ("data", "totalCount"),
                ("data", "pagination", "total"),
                ("total",),
            ),
        )
        try:
            total_count = int(total) if total is not None else None
        except (TypeError, ValueError):
            total_count = None
        has_more_value = _dig(
            payload,
            (("data", "hasMore"), ("data", "has_more"), ("hasMore",)),
        )
        if has_more_value is None:
            has_more = bool(
                (total_count is not None and page * page_size < total_count)
                or len(rows) >= page_size
            )
        else:
            has_more = bool(has_more_value)
        platform_time = _dig(
            payload,
            (("serverTime",), ("data", "serverTime"), ("meta", "server_time")),
        )
        return PageResult(
            rows=tuple(row for row in rows if isinstance(row, dict)),
            page_number=page,
            page_size=page_size,
            total_count=total_count,
            has_more=has_more,
            platform_server_time=str(platform_time) if platform_time else None,
            response_schema_hash=response_schema_hash(payload),
        )

    def _paginate(self, fetch_page, normalize, unique_key, drop_none: bool = False) -> PaginatedResult:
        page = 1
        raw_count = 0
        successful_pages = 0
        failed_pages: list[int] = []
        normalized_rows: list[Any] = []
        seen: set[str] = set()
        duplicates = 0
        total: int | None = None
        expected_pages: int | None = None
        platform_time: str | None = None
        error_code: str | None = None
        error_message: str | None = None
        schema_hashes: set[str] = set()
        while page <= 10_000:
            try:
                result = fetch_page(page)
                schema_hashes.add(result.response_schema_hash)
                successful_pages += 1
                raw_count += len(result.rows)
                platform_time = result.platform_server_time or platform_time
                if total is None and result.total_count is not None:
                    total = result.total_count
                    expected_pages = math.ceil(total / result.page_size) if total else 0
                for raw in result.rows:
                    value = normalize(raw, str(raw.get("aavid") or ""))
                    if value is None and drop_none:
                        continue
                    key = str(unique_key(value))
                    if key in seen:
                        duplicates += 1
                        continue
                    seen.add(key)
                    normalized_rows.append(value)
                if not result.has_more:
                    break
                page += 1
            except Exception as exc:
                failed_pages.append(page)
                error_message = str(exc)
                error_code = "schema_changed" if "schema_changed" in str(exc) else type(exc).__name__
                break
        if failed_pages:
            status = "schema_changed" if error_code == "schema_changed" else "partial"
        elif total not in (None, 0) and not normalized_rows:
            status = "suspicious_empty"
        elif total == 0 and successful_pages:
            status = "complete"
        elif successful_pages:
            status = "complete"
        else:
            status = "failed"
        return PaginatedResult(
            rows=tuple(normalized_rows),
            platform_total_count=total,
            expected_pages=expected_pages,
            successful_pages=successful_pages,
            failed_pages=tuple(failed_pages),
            raw_count=raw_count,
            unique_count=len(normalized_rows),
            duplicate_count=duplicates,
            status=status,
            platform_server_time=platform_time,
            error_code=error_code,
            error_message=error_message,
            response_schema_hashes=tuple(sorted(schema_hashes)),
        )
