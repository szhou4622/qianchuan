"""Read-only comparison between the last legacy snapshot and official API.

This is an acceptance aid, not a runtime fallback.  It never launches Chrome,
never changes monitoring selections, and never writes to Ocean Engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from api.promotion_targets import list_promotion_targets
from services.official_api_collection import MATERIAL_METRICS, _material_snapshot
from services.qianchuan_accounts import get_qianchuan_account
from services.qianchuan_open_api.normalizers import text_id
from services.qianchuan_open_api.runtime import get_official_api_service
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


METRIC_FIELDS = (
    "stat_cost",
    "overall_order_count",
    "pay_gmv_include_coupon",
    "prepay_pay_settle_1h",
)


def _ids(rows: list[Mapping[str, Any]], key: str) -> set[str]:
    return {text_id(row.get(key)) for row in rows if text_id(row.get(key))}


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _metric_diff(local: Mapping[str, Any], official: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in METRIC_FIELDS:
        left = _decimal(local.get(field))
        right = _decimal(official.get(field))
        if left is None or right is None:
            result[field] = {"local": local.get(field), "official": official.get(field), "comparable": False}
            continue
        result[field] = {
            "local": str(left),
            "official": str(right),
            "delta": str(right - left),
            "comparable": True,
        }
    return result


def reconcile_account_snapshot(
    aavid: Any,
    *,
    db: Optional[SQLiteStore] = None,
) -> dict[str, Any]:
    """Compare one actively added account and its enabled plans."""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    aid = text_id(aavid)
    account = get_qianchuan_account(aid, db=store)
    if not account:
        raise ValueError("该千川账户尚未添加到本机工具")

    service = get_official_api_service()
    business_accounts, account_evidence = service.list_business_accounts()
    official_account = next(
        (row for row in business_accounts if text_id(row.get("advertiser_id")) == aid),
        None,
    )
    if not official_account:
        return {
            "success": False,
            "complete": False,
            "aavid": aid,
            "error": "该账户不在当前官方 API 授权链中",
            "account_evidence": account_evidence,
        }

    official_plans, plan_evidence = service.list_all_plans(aid)
    local_plans = [
        row
        for row in list_promotion_targets(db=store)
        if text_id(row.get("aadvid")) == aid
    ]
    official_by_id = {text_id(row.get("ad_id")): row for row in official_plans if text_id(row.get("ad_id"))}
    local_by_id = {text_id(row.get("ad_id")): row for row in local_plans if text_id(row.get("ad_id"))}
    official_ids = set(official_by_id)
    local_ids = set(local_by_id)

    class_mismatches: list[dict[str, Any]] = []
    for ad_id in sorted(official_ids & local_ids):
        local = local_by_id[ad_id]
        official = official_by_id[ad_id]
        expected = (str(local.get("plan_system") or ""), str(local.get("promotion_scene") or ""))
        actual = (str(official.get("plan_system") or ""), str(official.get("promotion_scene") or ""))
        if expected != actual:
            class_mismatches.append({"ad_id": ad_id, "local": expected, "official": actual})

    target_results: list[dict[str, Any]] = []
    end = datetime.now()
    start_date = (end - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date = end.strftime("%Y-%m-%d")
    for target in [row for row in local_plans if bool(row.get("enabled"))]:
        ad_id = text_id(target.get("ad_id"))
        goal = "LIVE_PROM_GOODS" if str(target.get("promotion_scene")) == "live" else "VIDEO_PROM_GOODS"
        units, config_response = service.get_report_config(
            aid,
            plan_system=str(target.get("plan_system") or ""),
            promotion_scene=str(target.get("promotion_scene") or ""),
        )
        if not units:
            raise RuntimeError("官方 API 报表配置未返回字段单位，禁止生成指标对账结论")
        materials, material_request_ids = service.list_plan_materials(
            aid,
            ad_id,
            start_date=start_date,
            end_date=end_date,
            fields=tuple(field for field in MATERIAL_METRICS if field in units),
        )
        products, product_request_ids = (
            service.list_plan_products(
                aid,
                ad_id,
                start_date=start_date,
                end_date=end_date,
            )
            if str(target.get("promotion_scene")) == "product"
            else ([], [])
        )
        control_start = (end - timedelta(days=179)).strftime("%Y-%m-%d 00:00:00")
        controls, control_request_ids = service.list_control_tasks(
            aid,
            ad_id=ad_id,
            marketing_goal=goal,
            start_time=control_start,
            end_time=(end + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59"),
        )

        local_materials = store.execute(
            "SELECT p.* FROM pmc_promotion_material p "
            "JOIN (SELECT material_id, MAX(id) AS max_id FROM pmc_promotion_material "
            "WHERE target_uid=? GROUP BY material_id) latest ON latest.max_id=p.id",
            (str(target.get("target_uid") or ""),),
            fetch=True,
        ) or []
        local_products = store.execute(
            "SELECT product_id FROM promotion_product WHERE target_uid=?",
            (str(target.get("target_uid") or ""),),
            fetch=True,
        ) or []
        local_controls = store.execute(
            "SELECT assist_task_id FROM pmc_roi2_assist_task WHERE target_uid=?",
            (str(target.get("target_uid") or ""),),
            fetch=True,
        ) or []
        request_id = material_request_ids[-1] if material_request_ids else config_response.request_id
        official_snapshots = {
            text_id(item.get("material_id")): _material_snapshot(
                item,
                target=target,
                units=units,
                request_id=request_id,
            )
            for item in materials
            if text_id(item.get("material_id"))
        }
        local_snapshots = {text_id(item.get("material_id")): item for item in local_materials}
        matched_materials = sorted(set(local_snapshots) & set(official_snapshots))
        target_results.append(
            {
                "target_uid": str(target.get("target_uid") or ""),
                "ad_id": ad_id,
                "local_material_count": len(local_snapshots),
                "official_material_count": len(official_snapshots),
                "material_only_local": sorted(set(local_snapshots) - set(official_snapshots)),
                "material_only_official": sorted(set(official_snapshots) - set(local_snapshots)),
                "local_product_ids": sorted(_ids(local_products, "product_id")),
                "official_product_ids": sorted(_ids(products, "product_id")),
                "local_control_task_ids": sorted(_ids(local_controls, "assist_task_id")),
                "official_control_task_ids": sorted(_ids(controls, "task_id")),
                "metric_comparison": {
                    material_id: _metric_diff(local_snapshots[material_id], official_snapshots[material_id])
                    for material_id in matched_materials
                },
                "request_ids": {
                    "report_config": config_response.request_id,
                    "materials": material_request_ids,
                    "products": product_request_ids,
                    "control_tasks": control_request_ids,
                },
            }
        )

    account_name_local = str(account.get("account_name") or "")
    account_name_official = str(official_account.get("advertiser_name") or "")
    return {
        "success": True,
        "complete": bool(account_evidence.get("complete") and plan_evidence.get("complete")),
        "aavid": aid,
        "account_name": {
            "local": account_name_local,
            "official": account_name_official,
            "matched": account_name_local == account_name_official,
        },
        "plans": {
            "local_count": len(local_ids),
            "official_count": len(official_ids),
            "only_local": sorted(local_ids - official_ids),
            "only_official": sorted(official_ids - local_ids),
            "class_mismatches": class_mismatches,
        },
        "targets": target_results,
        "account_evidence": account_evidence,
        "plan_evidence": plan_evidence,
    }
