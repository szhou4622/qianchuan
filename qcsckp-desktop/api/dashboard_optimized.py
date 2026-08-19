"""Owner-scoped, indexed queries for the multi-account dashboard."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from services.qianchuan_session import current_session_owner
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


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
            "COALESCE(l.delivery_state,'delivering')!='removed'",
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

    def get_refresh_state(self, *, aavid: Any = "", target_uid: Any = "") -> dict[str, Any]:
        owner = self._owner()
        scope_where, scope_params = self._scope_where(aavid, target_uid)
        row = self.db.execute(
            "SELECT COUNT(*) AS material_count,MAX(l.collected_at) AS newest_at,"
            "MIN(l.collected_at) AS oldest_at "
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
                SELECT l.*,t.account_uid,t.plan_name,a.account_name
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
                WHERE s.account_username=?
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
                       ROUND(COALESCE(sc.stat_cost,0)-COALESCE(base.stat_cost,sc.stat_cost,0),2)
                           AS stat_cost_diff,
                       CASE WHEN totals.total_orders>0 AND goals.roi_goal>0 THEN
                           (CASE WHEN COALESCE(sc.overall_conversion_rate,0)>1
                                 THEN sc.overall_conversion_rate/100.0
                                 ELSE COALESCE(sc.overall_conversion_rate,0) END)
                           * (CASE WHEN COALESCE(sc.overall_ctr,0)>1
                                   THEN sc.overall_ctr/100.0
                                   ELSE COALESCE(sc.overall_ctr,0) END)
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
                    "currentCost": float(row.get("stat_cost") or 0),
                    "costDiff": float(row.get("stat_cost_diff") or 0),
                    "netRoi": float(row.get("prepay_pay_settle_1h") or 0),
                    "netAmount": float(row.get("order_settle_amount_1h") or 0),
                    "hourRefundRate": float(row.get("refund_rate_1h") or 0),
                    "overallPayRoi": float(row.get("prepay_pay_order_count") or 0),
                    "overallAmount": float(row.get("pay_gmv_include_coupon") or 0),
                    "netSettleRate": float(row.get("order_settle_rate_1h") or 0),
                    "netOrderCount": int(row.get("order_settle_count_1h") or 0),
                    "overallOrderCount": int(row.get("overall_order_count") or 0),
                    "overallShowCount": int(row.get("overall_show_count") or 0),
                    "overallClickCount": int(row.get("overall_click_count") or 0),
                    "overallCtr": float(row.get("overall_ctr") or 0),
                    "overallConversionRate": float(row.get("overall_conversion_rate") or 0),
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
            f"WHERE {scope_where} ORDER BY COALESCE(l.stat_cost,0) DESC LIMIT 20",
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
                    "currentCost": float(row.get("stat_cost") or 0),
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
            "SELECT COALESCE(SUM(COALESCE(l.stat_cost,0)),0) total_cost,COUNT(*) row_count "
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
            "totalCost": round(float(row.get("total_cost") or 0), 2),
            "rowCount": int(row.get("row_count") or 0),
            "batchMinuteKey": None,
            "latestCreatedAt": state.get("newestAt"),
            "dataAgeSeconds": state.get("dataAgeSeconds"),
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
                "cost": float(row.get("stat_cost") or 0),
                "roi": float(row.get("prepay_pay_order_count") or 0),
                "amount": float(row.get("pay_gmv_include_coupon") or 0),
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
                        "cost": float(latest.get("stat_cost") or 0),
                        "roi": float(latest.get("prepay_pay_order_count") or 0),
                        "amount": float(latest.get("pay_gmv_include_coupon") or 0),
                    }
                )
        return {"success": True, "data": result, "total": len(result)}
