"""适配器向业务层输出的规范化对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, "", "--", "-"):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def money_to_cent(value: Any) -> int:
    amount = to_decimal(value) or Decimal("0")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class AccountIdentity:
    aavid: str
    account_name: str


@dataclass(frozen=True)
class NormalizedPlan:
    aavid: str
    ad_id: str
    plan_name: str
    plan_system: str
    promotion_scene: str
    platform_status: str
    verification_state: str
    adapter_version: str
    raw_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedMaterial:
    aavid: str
    ad_id: str
    material_id: str
    material_name: str
    material_created_at: str | None
    delivery_status: str | None
    show_status: str | None
    show_status_reason: str | None
    audit_status: str | None
    block_status: str | None
    is_in_delivery_list: bool
    is_effectively_deliverable: bool
    product_ids: tuple[str, ...]
    spend_cent: int
    order_count: int
    gmv_cent: int
    roi_decimal: str | None
    platform_raw_status: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedControlTask:
    aavid: str
    source_plan_id: str
    control_task_id: str
    task_name: str
    assist_task_scene: int
    retarget_method: str | None
    material_ids: tuple[str, ...]
    platform_status: str
    budget_kind: str | None
    budget_current_cent: int | None
    budget_used_cent: int | None
    duration_hours_decimal: str | None
    start_time_utc: str | None
    end_time_utc: str | None
    roi_or_bid_decimal: str | None
    updated_at_platform: str | None
    task_revision_fingerprint: str


@dataclass(frozen=True)
class PageResult:
    rows: tuple[dict[str, Any], ...]
    page_number: int
    page_size: int
    total_count: int | None
    has_more: bool
    platform_server_time: str | None
    response_schema_hash: str


@dataclass(frozen=True)
class PaginatedResult:
    rows: tuple[Any, ...]
    platform_total_count: int | None
    expected_pages: int | None
    successful_pages: int
    failed_pages: tuple[int, ...]
    raw_count: int
    unique_count: int
    duplicate_count: int
    status: str
    platform_server_time: str | None
    error_code: str | None = None
    error_message: str | None = None
    response_schema_hashes: tuple[str, ...] = ()
