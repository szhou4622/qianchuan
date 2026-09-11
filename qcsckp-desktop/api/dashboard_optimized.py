"""Owner-scoped, indexed queries for the multi-account dashboard."""
from __future__ import annotations

import json
import hashlib
import os
import sys
import math
from datetime import datetime, timedelta
from typing import Any, Optional

from config import CURRENT_VERSION, DATA_DIR, TEST_MODE
from services.qianchuan_session import current_session_owner
from services.material_metric_contract import REPORT_SCOPE_LABEL
from utils.log import logger
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


DASHBOARD_CONTRACT_VERSION = 2

# Display convention only. These keys never replace the numeric API values
# consumed by rule evaluation, exports, history or execution preflight.
_ZERO_DISPLAY_FIELDS = {
    "stat_cost_for_roi2": "currentCost", "total_pay_order_count_for_roi2": "overallOrderCount",
    "total_pay_order_gmv_include_coupon_for_roi2": "overallAmount",
    "total_prepay_and_pay_order_roi2": "overallPayRoi",
    "total_order_settle_amount_for_roi2_1h": "netAmount",
    "total_order_settle_count_for_roi2_1h": "netOrderCount",
    "total_prepay_and_pay_settle_roi2_1h": "netRoi",
    "total_order_settle_amount_rate_for_roi2_1h": "netSettleRate",
    "total_refund_order_gmv_for_roi2_1h_rate": "hourRefundRate",
    "live_show_count_for_roi2_v2": "overallShowCount", "live_watch_count_for_roi2_v2": "overallClickCount",
    "live_cvr_rate_for_roi2_v2": "overallCtr", "live_convert_rate_for_roi2_v2": "overallConversionRate",
}


def _zero_display_eligible():
    return ("CASE WHEN l.metric_row_state='not_in_report' AND t.last_status='ok' "
            "AND l.stat_date=date('now','+8 hours') "
            "AND l.collected_at BETWEEN datetime('now','+8 hours','-10 minutes') AND datetime('now','+8 hours') "
            "AND CASE WHEN json_valid(t.capability_json) THEN "
            "json_extract(t.capability_json,'$.material_sync_complete')=1 "
            "AND json_extract(t.capability_json,'$.material_metric_source')='chengfang_anchor_material_report' "
            "AND json_extract(t.capability_json,'$.material_metric_scope.data_period')='ALL_DATA' "
            "AND json_array_length(t.capability_json,'$.material_metric_evidence.requested_fields')>0 "
            "ELSE 0 END THEN 1 ELSE 0 END")


def _metric_contract_since(alias="t"):
    return f"COALESCE(CASE WHEN json_valid({alias}.capability_json) THEN json_extract({alias}.capability_json,'$.material_metric_contract_since') END,'')"


def _report_metric_source(alias="t"):
    return f"COALESCE(CASE WHEN json_valid({alias}.capability_json) THEN json_extract({alias}.capability_json,'$.material_metric_source')='chengfang_anchor_material_report' END,0)"


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class OptimizedDashboardQueries:
    def __init__(self, db: SQLiteStore) -> None:
        self.db = db
        init_sqlite_schema(database=db.config.get("database"))

    @staticmethod
    def _owner() -> str:
        return str(current_session_owner() or "local_default").strip().casefold()

    @staticmethod
    def _period(period: str) -> tuple[int, str]:
        text = str(period or "1h").strip().lower()
        try:
            if text.endswith("m"):
                minutes = max(5, min(24 * 60, int(text[:-1])))
                return minutes, f"{minutes}分钟"
            hours = max(1, min(24, int(text[:-1] if text.endswith("h") else text)))
        except (TypeError, ValueError):
            hours = 1
        return hours * 60, f"{hours}小时"

    @staticmethod
    def _scope_where(aavid: Any = "", target_uid: Any = "") -> tuple[str, list[Any]]:
        clauses = [
            "a.owner_username=?",
            "a.enabled=1",
            "t.enabled=1",
            # Only a freshly confirmed active material enters dashboards and
            # rule candidates. ``pending_inactive`` is a safe one-cycle hold
            # before historical removal, not an actionable delivery state.
            "COALESCE(l.delivery_state,'delivering')='delivering'",
        ]
        params: list[Any] = []
        account_id = str(aavid or "").strip()
        target_id = str(target_uid or "").strip()
        if account_id:
            clauses.append("a.aavid=?")
            params.append(account_id)
        if target_id:
            clauses.append("t.target_uid=?")
            params.append(target_id)
        return " AND ".join(clauses), params

    def get_scope_options(self) -> dict[str, Any]:
        owner = self._owner()
        accounts = self.db.execute(
            "SELECT a.account_uid,a.aavid,a.account_name,a.last_status,a.last_error,"
            "COUNT(CASE WHEN t.enabled=1 THEN 1 END) AS monitored_plan_count,"
            "MAX(CASE WHEN t.enabled=1 THEN t.last_sync_at END) AS last_sync_at "
            "FROM qianchuan_account a LEFT JOIN promotion_target t "
            "ON t.account_uid=a.account_uid "
            "WHERE a.owner_username=? AND a.enabled=1 "
            "GROUP BY a.account_uid,a.aavid,a.account_name,a.last_status,a.last_error "
            "ORDER BY a.account_name ASC,a.aavid ASC",
            (owner,),
            fetch=True,
        ) or []
        plans = self.db.execute(
            "SELECT t.target_uid,t.account_uid,t.aadvid AS aavid,t.ad_id,t.plan_name,"
            "t.promotion_scene,t.plan_system,t.platform_status,t.last_sync_at,"
            "t.last_status,t.last_error,t.capacity_state,a.account_name "
            "FROM promotion_target t INNER JOIN qianchuan_account a "
            "ON a.account_uid=t.account_uid "
            "WHERE a.owner_username=? AND a.enabled=1 AND t.enabled=1 "
            "ORDER BY a.account_name ASC,t.plan_system ASC,t.promotion_scene ASC,t.plan_name ASC",
            (owner,),
            fetch=True,
        ) or []
        newest = max(
            [str(item.get("last_sync_at") or "") for item in plans] or [""]
        )
        waiting_count = sum(
            1
            for item in plans
            if str(item.get("capacity_state") or "") == "capacity_waiting"
        )
        return {
            "success": True,
            "accounts": [dict(item) for item in accounts],
            "plans": [dict(item) for item in plans],
            "capacityWaitingCount": waiting_count,
            "dataVersion": newest,
        }

    @staticmethod
    def _runtime_identity() -> tuple[str, str]:
        if bool(TEST_MODE):
            mode = "test"
        elif bool(getattr(sys, "frozen", False)):
            mode = "production"
        else:
            mode = "development"
        canonical = os.path.normcase(os.path.realpath(DATA_DIR))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        return mode, f"{mode}-{digest}"

    def get_bootstrap(self) -> dict[str, Any]:
        """Return one atomic dashboard contract for filters and table scope."""
        owner = self._owner()
        scope = self.get_scope_options()
        refresh = self.get_refresh_state()
        accounts = list(scope.get("accounts") or [])
        plans = list(scope.get("plans") or [])
        material_count = int(refresh.get("materialCount") or 0)
        runtime_mode, runtime_instance_id = self._runtime_identity()
        scope_revision_source = "|".join(
            [
                owner,
                *(str(item.get("aavid") or "") for item in accounts),
                *(str(item.get("target_uid") or "") for item in plans),
                str(refresh.get("dataVersion") or ""),
            ]
        )
        scope_revision = hashlib.sha256(
            scope_revision_source.encode("utf-8")
        ).hexdigest()[:16]
        response = {
            "success": True,
            "dashboardContractVersion": DASHBOARD_CONTRACT_VERSION,
            "appVersion": str(CURRENT_VERSION),
            "runtimeMode": runtime_mode,
            "runtimeInstanceId": runtime_instance_id,
            "ownerUsername": owner,
            "accounts": accounts,
            "plans": plans,
            "accountCount": len(accounts),
            "planCount": len(plans),
            "materialCount": material_count,
            "capacityWaitingCount": int(scope.get("capacityWaitingCount") or 0),
            "dataVersion": str(refresh.get("dataVersion") or ""),
            "scopeRevision": scope_revision,
            "lastCollectedAt": str(refresh.get("newestAt") or ""),
            "oldestCollectedAt": str(refresh.get("oldestAt") or ""),
            "dataAgeSeconds": refresh.get("dataAgeSeconds"),
            "defaultScope": {
                "mode": "all_enabled_accounts",
                "aavid": "",
                "targetUid": "",
            },
            "capabilities": {
                "table": True,
                "accountFilter": True,
                "planFilter": True,
                "scopeRetry": True,
            },
        }
        logger.info(
            "[DashboardBootstrap] contract=%s app=%s runtime=%s owner=%s "
            "accounts=%s plans=%s materials=%s",
            DASHBOARD_CONTRACT_VERSION,
            CURRENT_VERSION,
            runtime_instance_id,
            owner,
            len(accounts),
            len(plans),
            material_count,
        )
        return response

    def get_refresh_state(self, *, aavid: Any = "", target_uid: Any = "") -> dict[str, Any]:
        owner = self._owner()
        scope_where, scope_params = self._scope_where(aavid, target_uid)
        row = self.db.execute(
            "SELECT COUNT(*) AS material_count,MAX(l.collected_at) AS newest_at,"
            "MIN(l.collected_at) AS oldest_at, "
            "COUNT(l.stat_cost) AS cost_count, COUNT(l.prepay_pay_order_count) AS roi_count, "
            "COUNT(l.pay_gmv_include_coupon) AS gmv_count, "
            "SUM(CASE WHEN l.stat_cost=0 THEN 1 ELSE 0 END) AS zero_cost_count, "
            "SUM(CASE WHEN l.metric_row_state='not_in_report' THEN 1 ELSE 0 END) AS no_report_count, "
            "SUM(CASE WHEN l.metric_row_state<>'not_in_report' AND "
            "(l.stat_cost IS NULL OR l.prepay_pay_order_count IS NULL OR l.pay_gmv_include_coupon IS NULL) THEN 1 ELSE 0 END) AS missing_reported_count, "
            "MAX(CASE WHEN t.last_status IN ('error','failed','authorization_required') THEN 1 ELSE 0 END) AS collection_failed, "
            "MAX(CASE WHEN t.last_status='collecting' THEN 1 ELSE 0 END) AS collecting, "
            f"SUM({_zero_display_eligible()}) AS zero_display_count, "
            f"SUM({_report_metric_source()}) AS report_scope_count "
            "FROM pmc_promotion_material_latest l "
            "INNER JOIN promotion_target t ON t.target_uid=l.target_uid "
            "INNER JOIN qianchuan_account a ON a.account_uid=t.account_uid "
            f"WHERE {scope_where}",
            (owner, *scope_params),
            fetch=True,
        ) or [{}]
        data = dict(row[0] if row else {})
        newest = str(data.get("newest_at") or "")
        oldest = str(data.get("oldest_at") or "")
        age: Optional[int] = None
        if oldest:
            try:
                age = max(
                    0,
                    int(
                        (
                            datetime.now()
                            - datetime.strptime(oldest, "%Y-%m-%d %H:%M:%S")
                        ).total_seconds()
                    ),
                )
            except (TypeError, ValueError):
                age = None
        return {
            "success": True,
            "dataVersion": f"{newest}:{int(data.get('material_count') or 0)}",
            "newestAt": newest,
            "oldestAt": oldest,
            "dataAgeSeconds": age,
            "materialCount": int(data.get("material_count") or 0),
            "noReportMaterialCount": int(data.get("no_report_count") or 0),
            "zeroDisplayMaterialCount": int(data.get("zero_display_count") or 0),
            "reportedMaterialCount": int(data.get("material_count") or 0)-int(data.get("no_report_count") or 0),
            "missingReportedMetricCount": int(data.get("missing_reported_count") or 0),
            "collectionStatus": "failed" if data.get("collection_failed") else "collecting" if data.get("collecting") else "complete",
            "metricScopeLabel": REPORT_SCOPE_LABEL if int(data.get("report_scope_count") or 0) else "",
            "metricQuality": (
                "empty" if not int(data.get("material_count") or 0) else
                "missing" if int(data.get("missing_reported_count") or 0) else
                "sparse_report" if int(data.get("no_report_count") or 0) else
                "all_zero" if int(data.get("zero_cost_count") or 0) == int(data.get("material_count") or 0) else "available"
            ),
        }

    def get_table_data(
        self,
        period: str = "1h",
        sort_by: str = "costDiff",
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 50,
        *,
        aavid: Any = "",
        target_uid: Any = "",
    ) -> dict[str, Any]:
        owner = self._owner()
        minutes, display_period = self._period(period)
        cutoff = (datetime.now() - timedelta(minutes=minutes)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        page = max(1, int(page or 1))
        # The UI uses 50 rows, while the rule engine intentionally asks for up
        # to 20,000 rows of one already-scoped monitored plan.  Keep SQL-side
        # scoping and pagination without silently truncating strategy input.
        page_size = max(10, min(20_000, int(page_size or 50)))
        offset = (page - 1) * page_size
        direction = "ASC" if str(sort_order).lower() == "asc" else "DESC"
        sort_fields = {
            "currentCost": "stat_cost",
            "costDiff": "stat_cost_diff",
            "overallPayRoi": "prepay_pay_order_count",
            "overallAmount": "pay_gmv_include_coupon",
            "netRoi": "prepay_pay_settle_1h",
            "netAmount": "order_settle_amount_1h",
            "estimatedEcpm": "estimated_ecpm",
            "createTime": "video_create_time",
        }
        order_field = sort_fields.get(str(sort_by), "stat_cost_diff")
        scope_where, scope_params = self._scope_where(aavid, target_uid)
        sql = f"""
            WITH Scope AS (
                SELECT l.*,t.account_uid,t.plan_name,a.account_name,
                       {_zero_display_eligible()} AS zero_display_eligible,
                       CASE WHEN json_valid(t.capability_json) THEN json_extract(t.capability_json,'$.material_metric_evidence.requested_fields') END AS requested_metric_fields,
                       {_report_metric_source()} AS report_metric_source,
                       {_metric_contract_since()} AS metric_contract_since
                FROM pmc_promotion_material_latest l
                INNER JOIN promotion_target t ON t.target_uid=l.target_uid
                INNER JOIN qianchuan_account a ON a.account_uid=t.account_uid
                WHERE {scope_where}
            ), SnapshotBounds AS (
                SELECT s.target_uid,s.material_id,
                       MAX(CASE WHEN s.collected_at<=? THEN s.id END) AS before_id,
                       MIN(CASE WHEN s.collected_at>? THEN s.id END) AS after_id
                FROM pmc_material_metric_snapshot s
                INNER JOIN Scope sc ON sc.target_uid=s.target_uid
                                   AND sc.material_id=s.material_id
                WHERE s.account_username=? AND s.collected_at>=sc.metric_contract_since
                GROUP BY s.target_uid,s.material_id
            ), BaselineChoice AS (
                SELECT target_uid,material_id,COALESCE(before_id,after_id) AS baseline_id
                FROM SnapshotBounds
            ), AccountTotals AS (
                SELECT aadvid,SUM(COALESCE(pay_gmv_include_coupon,0)) AS total_gmv,
                       SUM(COALESCE(overall_order_count,0)) AS total_orders
                FROM Scope GROUP BY aadvid
            ), Goals AS (
                SELECT aadvid,MAX(COALESCE(ecp_roi2_goal,0)) AS roi_goal
                FROM pmc_ad_detail_basic GROUP BY aadvid
            ), Enriched AS (
                SELECT sc.*,base.collected_at AS period_start_time,
                       sc.collected_at AS period_end_time,
                       CASE WHEN sc.stat_date=base.stat_date
                            THEN ROUND(sc.stat_cost-base.stat_cost,2) ELSE NULL END
                           AS stat_cost_diff,
                       CASE WHEN totals.total_orders>0 AND goals.roi_goal>0 THEN
                           (CASE WHEN sc.overall_conversion_rate>1
                                 THEN sc.overall_conversion_rate/100.0
                                 ELSE sc.overall_conversion_rate END)
                           * (CASE WHEN sc.overall_ctr>1
                                   THEN sc.overall_ctr/100.0
                                   ELSE sc.overall_ctr END)
                           * ((totals.total_gmv/totals.total_orders)/goals.roi_goal) * 1000.0
                       ELSE NULL END AS estimated_ecpm
                FROM Scope sc
                LEFT JOIN BaselineChoice choice
                  ON choice.target_uid=sc.target_uid AND choice.material_id=sc.material_id
                LEFT JOIN pmc_material_metric_snapshot base ON base.id=choice.baseline_id
                LEFT JOIN AccountTotals totals ON totals.aadvid=sc.aadvid
                LEFT JOIN Goals goals ON goals.aadvid=sc.aadvid
            )
            SELECT Enriched.*,COUNT(*) OVER() AS total_count,
                   MAX(collected_at) OVER() AS scope_newest_at
            FROM Enriched
            ORDER BY {order_field} {direction},target_uid ASC,material_id ASC
            LIMIT ? OFFSET ?
        """
        params = (
            owner,
            *scope_params,
            cutoff,
            cutoff,
            owner,
            page_size,
            offset,
        )
        rows = self.db.execute(sql, params, fetch=True) or []
        total = int(rows[0].get("total_count") or 0) if rows else 0
        data: list[dict[str, Any]] = []
        for row in rows:
            try:
                product_ids = json.loads(row.get("product_ids_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                product_ids = []
            if not isinstance(product_ids, list):
                product_ids = []
            display_zero_fields = []
            if row.get("zero_display_eligible"):
                try:
                    requested = json.loads(row.get("requested_metric_fields") or "[]")
                    if isinstance(requested, list):
                        display_zero_fields = list(dict.fromkeys(_ZERO_DISPLAY_FIELDS[field] for field in requested
                                                   if isinstance(field,str) and field in _ZERO_DISPLAY_FIELDS))
                except (ValueError, TypeError):
                    pass
            data.append(
                {
                    "id": str(row.get("material_id") or ""),
                    "targetUid": str(row.get("target_uid") or ""),
                    "adId": str(row.get("ad_id") or ""),
                    "aadvid": str(row.get("aadvid") or ""),
                    "accountName": str(row.get("account_name") or ""),
                    "planName": str(row.get("plan_name") or ""),
                    "promotionScene": str(row.get("promotion_scene") or "live"),
                    "planSystem": str(row.get("plan_system") or "unknown"),
                    "metricRowState": str(row.get("metric_row_state") or "legacy_unknown"),
                    "displayZeroFields": display_zero_fields,
                    "metricScopeLabel": REPORT_SCOPE_LABEL if row.get("report_metric_source") else "",
                    "productIds": [str(v) for v in product_ids if str(v or "").strip()],
                    "title": str(row.get("video_name") or "未命名"),
                    "materialStatus": row.get("material_status"),
                    "deliveryState": str(row.get("delivery_state") or "delivering"),
                    "showStatus": row.get("show_status"),
                    "videoType": row.get("video_type"),
                    "videoId": str(row.get("video_id") or ""),
                    "awemeItemId": str(row.get("aweme_item_id") or ""),
                    "cover": str(row.get("cover_url") or ""),
                    "duration": row.get("video_duration"),
                    "createTime": str(row.get("video_create_time") or ""),
                    "currentCost": _optional_float(row.get("stat_cost")),
                    "costDiff": _optional_float(row.get("stat_cost_diff")),
                    "netRoi": _optional_float(row.get("prepay_pay_settle_1h")),
                    "netAmount": _optional_float(row.get("order_settle_amount_1h")),
                    "hourRefundRate": _optional_float(row.get("refund_rate_1h")),
                    "overallPayRoi": _optional_float(row.get("prepay_pay_order_count")),
                    "overallAmount": _optional_float(row.get("pay_gmv_include_coupon")),
                    "netSettleRate": _optional_float(row.get("order_settle_rate_1h")),
                    "netOrderCount": _optional_int(row.get("order_settle_count_1h")),
                    "overallOrderCount": _optional_int(row.get("overall_order_count")),
                    "overallShowCount": _optional_int(row.get("overall_show_count")),
                    "overallClickCount": _optional_int(row.get("overall_click_count")),
                    "overallCtr": _optional_float(row.get("overall_ctr")),
                    "overallConversionRate": _optional_float(row.get("overall_conversion_rate")),
                    "estimatedEcpm": (
                        round(float(row.get("estimated_ecpm")), 4)
                        if row.get("estimated_ecpm") is not None
                        else None
                    ),
                    "periodStartTime": row.get("period_start_time"),
                    "periodEndTime": row.get("period_end_time"),
                    "collectedAt": str(row.get("collected_at") or ""),
                }
            )
        total_pages = max(1, (total + page_size - 1) // page_size)
        return {
            "success": True,
            "data": data,
            "total": total,
            "period": display_period,
            "page": page,
            "pageSize": page_size,
            "totalPages": total_pages,
            "dataVersion": (
                f"{str(rows[0].get('scope_newest_at') or '') if rows else ''}:{total}"
            ),
        }

    def get_top20_by_cost(
        self, *, aavid: Any = "", target_uid: Any = ""
    ) -> dict[str, Any]:
        owner = self._owner()
        scope_where, scope_params = self._scope_where(aavid, target_uid)
        rows = self.db.execute(
            "SELECT l.material_id,l.target_uid,l.aadvid,l.video_name,l.stat_cost,"
            "l.collected_at,a.account_name,t.plan_name "
            "FROM pmc_promotion_material_latest l "
            "INNER JOIN promotion_target t ON t.target_uid=l.target_uid "
            "INNER JOIN qianchuan_account a ON a.account_uid=t.account_uid "
            f"WHERE {scope_where} ORDER BY l.stat_cost DESC LIMIT 20",
            (owner, *scope_params),
            fetch=True,
        ) or []
        return {
            "success": True,
            "data": [
                {
                    "id": str(row.get("material_id") or ""),
                    "targetUid": str(row.get("target_uid") or ""),
                    "aadvid": str(row.get("aadvid") or ""),
                    "title": str(row.get("video_name") or "未命名"),
                    "accountName": str(row.get("account_name") or ""),
                    "planName": str(row.get("plan_name") or ""),
                    "currentCost": _optional_float(row.get("stat_cost")),
                    "createdAt": str(row.get("collected_at") or ""),
                }
                for row in rows
            ],
            "total": len(rows),
        }

    def get_latest_cost_sum(
        self, *, aavid: Any = "", target_uid: Any = ""
    ) -> dict[str, Any]:
        state = self.get_refresh_state(aavid=aavid, target_uid=target_uid)
        owner = self._owner()
        scope_where, scope_params = self._scope_where(aavid, target_uid)
        rows = self.db.execute(
            "SELECT SUM(l.stat_cost) total_cost,COUNT(*) row_count,COUNT(l.stat_cost) valid_cost_count "
            "FROM pmc_promotion_material_latest l "
            "INNER JOIN promotion_target t ON t.target_uid=l.target_uid "
            "INNER JOIN qianchuan_account a ON a.account_uid=t.account_uid "
            f"WHERE {scope_where}",
            (owner, *scope_params),
            fetch=True,
        ) or [{}]
        row = rows[0] if rows else {}
        return {
            "success": True,
            "totalCost": round(float(row["total_cost"]), 2) if row.get("total_cost") is not None else None,
            "rowCount": int(row.get("row_count") or 0),
            "validCostCount": int(row.get("valid_cost_count") or 0),
            "missingCostCount": int(row.get("row_count") or 0) - int(row.get("valid_cost_count") or 0),
            "noReportMaterialCount": state.get("noReportMaterialCount", 0),
            "zeroDisplayMaterialCount": state.get("zeroDisplayMaterialCount", 0),
            "reportedMaterialCount": state.get("reportedMaterialCount", 0),
            "batchMinuteKey": None,
            "latestCreatedAt": state.get("newestAt"),
            "dataAgeSeconds": state.get("dataAgeSeconds"),
            "metricScopeLabel": state.get("metricScopeLabel", ""),
        }

    def get_material_history(
        self,
        material_id: Any,
        *,
        target_uid: Any = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        owner = self._owner()
        material = str(material_id or "").strip()
        target = str(target_uid or "").strip()
        if not material:
            return {"success": False, "data": [], "message": "素材ID不能为空"}
        limit = max(1, min(200, int(limit or 200)))
        clauses = ["s.account_username=?", "s.material_id=?"]
        params: list[Any] = [owner, material]
        if target:
            clauses.append("s.target_uid=?")
            params.append(target)
        today = datetime.now().strftime("%Y-%m-%d 00:00:00")
        clauses.append("s.collected_at>=?")
        clauses.append("s.collected_at>=" + "COALESCE((SELECT " + _metric_contract_since() + " FROM promotion_target t WHERE t.target_uid=s.target_uid),'')")
        params.append(today)
        rows = self.db.execute(
            "SELECT s.target_uid,s.material_id,s.collected_at,s.stat_cost,"
            "s.prepay_pay_order_count,s.pay_gmv_include_coupon "
            "FROM pmc_material_metric_snapshot s WHERE "
            + " AND ".join(clauses)
            + " ORDER BY s.collected_at DESC,s.id DESC LIMIT ?",
            (*params, limit),
            fetch=True,
        ) or []
        rows = list(reversed(rows))
        result = [
            {
                "time": str(row.get("collected_at") or "")[5:16],
                "timestamp": int(
                    datetime.strptime(
                        str(row.get("collected_at")), "%Y-%m-%d %H:%M:%S"
                    ).timestamp()
                    * 1000
                ),
                "cost": _optional_float(row.get("stat_cost")),
                "roi": _optional_float(row.get("prepay_pay_order_count")),
                "amount": _optional_float(row.get("pay_gmv_include_coupon")),
            }
            for row in rows
            if row.get("collected_at")
        ]
        latest_clauses = [
            "a.owner_username=?",
            "a.enabled=1",
            "t.enabled=1",
            "l.material_id=?",
        ]
        latest_params: list[Any] = [owner, material]
        if target:
            latest_clauses.append("l.target_uid=?")
            latest_params.append(target)
        latest_rows = self.db.execute(
            "SELECT l.target_uid,l.collected_at,l.stat_cost,l.prepay_pay_order_count,"
            "l.pay_gmv_include_coupon FROM pmc_promotion_material_latest l "
            "INNER JOIN promotion_target t ON t.target_uid=l.target_uid "
            "INNER JOIN qianchuan_account a ON a.account_uid=t.account_uid WHERE "
            + " AND ".join(latest_clauses)
            + " ORDER BY l.collected_at DESC LIMIT 1",
            tuple(latest_params),
            fetch=True,
        ) or []
        if latest_rows:
            latest = latest_rows[0]
            latest_at = str(latest.get("collected_at") or "")
            if latest_at and (not rows or latest_at > str(rows[-1].get("collected_at") or "")):
                latest_dt = datetime.strptime(latest_at, "%Y-%m-%d %H:%M:%S")
                result.append(
                    {
                        "time": latest_dt.strftime("%m-%d %H:%M"),
                        "timestamp": int(latest_dt.timestamp() * 1000),
                        "cost": _optional_float(latest.get("stat_cost")),
                        "roi": _optional_float(latest.get("prepay_pay_order_count")),
                        "amount": _optional_float(latest.get("pay_gmv_include_coupon")),
                    }
                )
        return {"success": True, "data": result, "total": len(result)}

    def get_scope_history(
        self,
        *,
        aavid: Any = "",
        target_uid: Any = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return cumulative curves for the current dashboard scope.

        Metric snapshots are intentionally sparse: a row is written only when
        one of the material metrics changes.  Summing each bucket directly
        would therefore under-count unchanged materials.  The query first
        turns every material's sparse cumulative values into deltas, then sums
        those deltas by five-minute bucket and rebuilds the scope totals.
        """

        owner = self._owner()
        account_id = str(aavid or "").strip()
        target_id = str(target_uid or "").strip()
        limit = max(1, min(200, int(limit or 200)))
        today = datetime.now().strftime("%Y-%m-%d 00:00:00")

        clauses = [
            "s.account_username=?",
            "a.owner_username=?",
            "a.enabled=1",
            "t.enabled=1",
            "COALESCE(l.delivery_state,'delivering')='delivering'",
            # ``account_username,collected_at`` is indexed for the all-account
            # dashboard path; bucket_key is still used for five-minute grouping.
            "s.collected_at>=?",
            "s.collected_at>=" + _metric_contract_since(),
        ]
        params: list[Any] = [owner, owner, today]
        if account_id:
            clauses.append("a.aavid=?")
            params.append(account_id)
        if target_id:
            clauses.append("t.target_uid=?")
            params.append(target_id)

        rows = self.db.execute(
            "WITH scoped AS ("
            " SELECT s.id,s.target_uid,s.material_id,s.bucket_key,s.collected_at,"
            " COALESCE(s.stat_cost,0) AS stat_cost,"
            " COALESCE(s.pay_gmv_include_coupon,0) AS gmv,"
            " CASE WHEN s.stat_cost IS NULL AND s.metric_row_state<>'not_in_report' THEN 1 ELSE 0 END AS missing_cost,"
            " CASE WHEN s.pay_gmv_include_coupon IS NULL AND s.metric_row_state<>'not_in_report' THEN 1 ELSE 0 END AS missing_gmv,"
            " CASE WHEN s.stat_cost IS NOT NULL THEN 1 ELSE 0 END AS known_cost,"
            " CASE WHEN s.pay_gmv_include_coupon IS NOT NULL THEN 1 ELSE 0 END AS known_gmv"
            " FROM pmc_material_metric_snapshot s"
            " INNER JOIN promotion_target t ON t.target_uid=s.target_uid"
            " INNER JOIN qianchuan_account a ON a.account_uid=t.account_uid"
            " INNER JOIN pmc_promotion_material_latest l"
            " ON l.target_uid=s.target_uid AND l.material_id=s.material_id"
            " WHERE "
            + " AND ".join(clauses)
            + "), dedup AS ("
            " SELECT *,ROW_NUMBER() OVER ("
            " PARTITION BY target_uid,material_id,bucket_key"
            " ORDER BY collected_at DESC,id DESC) AS rn"
            " FROM scoped"
            "), changes AS ("
            " SELECT bucket_key,target_uid,material_id,"
            " stat_cost-COALESCE(LAG(stat_cost) OVER ("
            " PARTITION BY target_uid,material_id ORDER BY bucket_key),0) AS delta_cost,"
            " gmv-COALESCE(LAG(gmv) OVER ("
            " PARTITION BY target_uid,material_id ORDER BY bucket_key),0) AS delta_gmv,"
            " missing_cost-COALESCE(LAG(missing_cost) OVER (PARTITION BY target_uid,material_id ORDER BY bucket_key),0) AS delta_missing_cost,"
            " missing_gmv-COALESCE(LAG(missing_gmv) OVER (PARTITION BY target_uid,material_id ORDER BY bucket_key),0) AS delta_missing_gmv,"
            " known_cost-COALESCE(LAG(known_cost) OVER (PARTITION BY target_uid,material_id ORDER BY bucket_key),0) AS delta_known_cost,"
            " known_gmv-COALESCE(LAG(known_gmv) OVER (PARTITION BY target_uid,material_id ORDER BY bucket_key),0) AS delta_known_gmv"
            " FROM dedup WHERE rn=1"
            "), bucketed AS ("
            " SELECT bucket_key,SUM(delta_cost) AS delta_cost,"
            " SUM(delta_gmv) AS delta_gmv,SUM(delta_missing_cost) AS delta_missing_cost,"
            " SUM(delta_missing_gmv) AS delta_missing_gmv,SUM(delta_known_cost) AS delta_known_cost,"
            " SUM(delta_known_gmv) AS delta_known_gmv FROM changes GROUP BY bucket_key"
            "), running AS ("
            " SELECT bucket_key,"
            " SUM(delta_cost) OVER (ORDER BY bucket_key) AS total_cost,"
            " SUM(delta_gmv) OVER (ORDER BY bucket_key) AS total_gmv,"
            " SUM(delta_missing_cost) OVER (ORDER BY bucket_key) AS missing_cost,"
            " SUM(delta_missing_gmv) OVER (ORDER BY bucket_key) AS missing_gmv,"
            " SUM(delta_known_cost) OVER (ORDER BY bucket_key) AS known_cost,"
            " SUM(delta_known_gmv) OVER (ORDER BY bucket_key) AS known_gmv"
            " FROM bucketed"
            ") SELECT bucket_key,total_cost,total_gmv,missing_cost,missing_gmv,known_cost,known_gmv FROM running"
            " ORDER BY bucket_key DESC LIMIT ?",
            (*params, limit),
            fetch=True,
        ) or []
        rows = list(reversed(rows))
        result: list[dict[str, Any]] = []
        for row in rows:
            bucket = str(row.get("bucket_key") or "")
            if not bucket:
                continue
            try:
                observed = datetime.strptime(bucket, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            cost = None if row.get("missing_cost") or not row.get("known_cost") else _optional_float(row.get("total_cost"))
            amount = None if row.get("missing_gmv") or not row.get("known_gmv") else _optional_float(row.get("total_gmv"))
            result.append(
                {
                    "time": observed.strftime("%m-%d %H:%M"),
                    "timestamp": int(observed.timestamp() * 1000),
                    "cost": round(cost, 4) if cost is not None else None,
                    "roi": None if cost is None or amount is None else round(amount / cost, 4) if cost > 0 else 0.0,
                    "amount": round(amount, 4) if amount is not None else None,
                }
            )

        # ``latest`` is refreshed on every successful collection even when no
        # metric changed.  Append/replace a final point so the aggregate curve
        # reaches the same fresh value shown by the table and summary cards.
        scope_where, scope_params = self._scope_where(account_id, target_id)
        latest_rows = self.db.execute(
            "SELECT MAX(l.collected_at) AS collected_at,"
            "CASE WHEN SUM(CASE WHEN l.stat_cost IS NULL AND l.metric_row_state<>'not_in_report' THEN 1 ELSE 0 END)=0 THEN SUM(l.stat_cost) ELSE NULL END AS total_cost,"
            "CASE WHEN SUM(CASE WHEN l.pay_gmv_include_coupon IS NULL AND l.metric_row_state<>'not_in_report' THEN 1 ELSE 0 END)=0 THEN SUM(l.pay_gmv_include_coupon) ELSE NULL END AS total_gmv "
            "FROM pmc_promotion_material_latest l "
            "INNER JOIN promotion_target t ON t.target_uid=l.target_uid "
            "INNER JOIN qianchuan_account a ON a.account_uid=t.account_uid "
            f"WHERE {scope_where}",
            (owner, *scope_params),
            fetch=True,
        ) or []
        if latest_rows and latest_rows[0].get("collected_at"):
            latest = latest_rows[0]
            latest_at = str(latest.get("collected_at") or "")
            try:
                observed = datetime.strptime(latest_at, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                observed = None
            if observed is not None:
                cost = _optional_float(latest.get("total_cost"))
                amount = _optional_float(latest.get("total_gmv"))
                point = {
                    "time": observed.strftime("%m-%d %H:%M"),
                    "timestamp": int(observed.timestamp() * 1000),
                    "cost": round(cost, 4) if cost is not None else None,
                    "roi": None if cost is None or amount is None else round(amount / cost, 4) if cost > 0 else 0.0,
                    "amount": round(amount, 4) if amount is not None else None,
                }
                if result and result[-1]["time"] == point["time"]:
                    result[-1] = point
                elif not result or point["timestamp"] > result[-1]["timestamp"]:
                    result.append(point)

        result = result[-limit:]
        return {"success": True, "data": result, "total": len(result)}
