"""
大屏数据接口
提供素材列表、历史曲线等数据查询
"""
import json
import os
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from utils.pmc_estimated_ecpm import attach_estimated_ecpm

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DashboardApi:
    """大屏数据 API"""

    def __init__(self):
        """初始化数据库连接"""
        sys_path = os.path.dirname(PROJECT_ROOT)
        if sys_path not in os.environ.get('PYTHONPATH', ''):
            os.environ['PYTHONPATH'] = sys_path

        from utils.sqlite_store import SQLiteStore
        self.db = SQLiteStore()
        from api.dashboard_optimized import OptimizedDashboardQueries
        self.optimized = OptimizedDashboardQueries(self.db)

    def get_scope_options(self) -> Dict[str, Any]:
        return self.optimized.get_scope_options()

    def get_bootstrap(self) -> Dict[str, Any]:
        return self.optimized.get_bootstrap()

    def get_refresh_state(
        self, aavid: Any = "", target_uid: Any = ""
    ) -> Dict[str, Any]:
        return self.optimized.get_refresh_state(
            aavid=aavid, target_uid=target_uid
        )

    def get_material_history_recent(
        self,
        material_id: str,
        limit: int = 200,
        target_uid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取素材最近 N 条历史点（按 created_at），用于前端折线图轮询刷新。
        限制：最多200条，且日期必须大于今日00:00:00（+8时区）

        Returns:
            {
              "success": True/False,
              "data": [{"time": "...", "timestamp": 0, "cost": 0.0, "roi": 0.0, "amount": 0.0}, ...],
              "total": 0
            }
        """
        return self.optimized.get_material_history(
            material_id, target_uid=target_uid or "", limit=limit
        )

    def get_scope_history_recent(
        self,
        aavid: str = "",
        target_uid: str = "",
        limit: int = 200,
    ) -> Dict[str, Any]:
        """获取当前账户/计划筛选范围的汇总历史曲线。"""
        return self.optimized.get_scope_history(
            aavid=aavid or "", target_uid=target_uid or "", limit=limit
        )
        try:
            limit = int(limit) if limit else 200
            if limit <= 0 or limit > 200:
                limit = 200

            # created_at 写入时已使用 '+8 hours' 口径存储，这里也用 UTC+8 计算 today_start
            now_beijing = datetime.utcnow() + timedelta(hours=8)
            today_start = now_beijing.replace(hour=0, minute=0, second=0, microsecond=0)

            where = "material_id = ? AND created_at > ?"
            params: tuple = (material_id, today_start.strftime('%Y-%m-%d %H:%M:%S'))
            if target_uid:
                where += " AND target_uid = ?"
                params += (str(target_uid).strip(),)
            rows = self.db.select(
                table='pmc_promotion_material',
                where=where,
                params=params,
                order_by='created_at DESC',
                limit=limit
            )

            if not rows:
                return {"success": True, "data": [], "total": 0}

            # 反转为时间正序
            rows = list(reversed(rows))
            result = []
            for r in rows:
                created_at = r.get('created_at')
                ts_ms = None
                t_label = None
                if created_at:
                    try:
                        # 兼容 "YYYY-MM-DD HH:MM:SS"
                        dt = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                        ts_ms = int(dt.timestamp() * 1000)
                        t_label = dt.strftime('%m-%d %H:%M')
                    except Exception:
                        # 不可解析就原样输出
                        t_label = str(created_at)
                result.append({
                    "time": t_label,
                    "timestamp": ts_ms,
                    "cost": r.get('stat_cost', 0) or 0,
                    "roi": r.get('prepay_pay_order_count', 0) or 0,
                    "amount": r.get('pay_gmv_include_coupon', 0) or 0
                })

            return {"success": True, "data": result, "total": len(result)}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"success": False, "data": [], "message": str(e)}

    def get_table_data(
        self,
        period: str = "1h",
        sort_by: str = "costDiff",
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 50,
        target_uid: Optional[str] = None,
        aavid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取表格数据 - 按周期查询素材的首尾差值

        Args:
            period: 查询周期，支持 "1h"(1小时), "15m"(15分钟), "2h", "4h" 等，默认 "1h"
            sort_by: 排序字段，支持 "currentCost"(整体消耗), "costDiff"(时段流速),
                    "overallPayRoi"(整体支付ROI), "overallAmount"(整体成交金额),
                    "netRoi"(净成交ROI), "netAmount"(净成交金额),
                    "estimatedEcpm"(预估ECPM，后端按公式计算后排序)
            sort_order: 排序方式 "asc" 或 "desc"，默认 "desc"
            page: 页码，默认 1
            page_size: 每页数量，默认 50

        Returns:
            {
                "success": True/False,
                "data": [...],  # 素材列表
                "total": 0,     # 总数
                "period": "1小时",
                "page": 1,
                "pageSize": 50,
                "totalPages": 1
            }
        """
        return self.optimized.get_table_data(
            period,
            sort_by,
            sort_order,
            page,
            page_size,
            aavid=aavid or "",
            target_uid=target_uid or "",
        )
        try:
            # 解析周期字符串
            period = period.strip().lower()
            if period.endswith('m'):
                # 分钟，如 "15m"
                value = int(period[:-1])
                time_str = f"-{value} minutes"
                display_period = f"{value}分钟"
            elif period.endswith('h'):
                # 小时，如 "1h", "2h"
                value = int(period[:-1])
                time_str = f"-{value} hours"
                display_period = f"{value}小时"
            else:
                # 默认当作小时
                value = int(period) if period.isdigit() else 1
                time_str = f"-{value} hours"
                display_period = f"{value}小时"

            # 排序字段映射（预估ECPM 在 SQL 中无列，先按 material_id 取全量再在 Python 中排序）
            sort_field_map = {
                'currentCost': 'stat_cost',
                'costDiff': 'stat_cost_diff',
                'overallPayRoi': 'prepay_pay_order_count_latest',
                'overallAmount': 'pay_gmv_include_coupon_latest',
                'netRoi': 'prepay_pay_settle_1h_latest',
                'netAmount': 'order_settle_amount_1h_latest',
                'estimatedEcpm': 'material_id',
            }
            order_by_field = sort_field_map.get(sort_by, 'stat_cost_diff')
            order_direction = "ASC" if sort_order.lower() == "asc" else "DESC"
            if sort_by == 'estimatedEcpm':
                order_by_field = 'material_id'
                order_direction = 'ASC'

            # 一次查询获取所有数据，然后 Python 切片分页
            target_uid = str(target_uid or "").strip()
            target_filter_sql = " AND target_uid = ?" if target_uid else ""
            sql = f"""
            WITH RawData AS (
                SELECT * FROM pmc_promotion_material
                WHERE created_at >= datetime('now', '+8 hours', '{time_str}')
                {target_filter_sql}
            ),
            WindowedData AS (
                SELECT
                    COALESCE(target_uid, 'legacy_unscoped') as target_uid,
                    material_id,
                    LAST_VALUE(ad_id) OVER w as ad_id,
                    LAST_VALUE(promotion_scene) OVER w as promotion_scene,
                    LAST_VALUE(plan_system) OVER w as plan_system,
                    LAST_VALUE(product_ids_json) OVER w as product_ids_json,
                    LAST_VALUE(aadvid) OVER w as aadvid,
                    LAST_VALUE(video_name) OVER w as video_name,
                    LAST_VALUE(material_status) OVER w as material_status,
                    LAST_VALUE(show_status) OVER w as show_status,
                    LAST_VALUE(video_type) OVER w as video_type,
                    LAST_VALUE(video_id) OVER w as video_id,
                    LAST_VALUE(aweme_item_id) OVER w as aweme_item_id,
                    LAST_VALUE(cover_url) OVER w as cover_url,
                    LAST_VALUE(video_duration) OVER w as video_duration,
                    LAST_VALUE(video_create_time) OVER w as video_create_time,
                    LAST_VALUE(lego_source) OVER w as lego_source,
                    LAST_VALUE(stat_cost) OVER w as stat_cost,
                    FIRST_VALUE(stat_cost) OVER w as s_cost, LAST_VALUE(stat_cost) OVER w as e_cost,
                    FIRST_VALUE(order_settle_count_1h) OVER w as s_osc, LAST_VALUE(order_settle_count_1h) OVER w as e_osc,
                    FIRST_VALUE(order_settle_amount_1h) OVER w as s_osa, LAST_VALUE(order_settle_amount_1h) OVER w as e_osa,
                    FIRST_VALUE(order_settle_rate_1h) OVER w as s_osr, LAST_VALUE(order_settle_rate_1h) OVER w as e_osr,
                    FIRST_VALUE(prepay_pay_order_count) OVER w as s_ppoc, LAST_VALUE(prepay_pay_order_count) OVER w as e_ppoc,
                    FIRST_VALUE(pay_gmv_include_coupon) OVER w as s_gmv, LAST_VALUE(pay_gmv_include_coupon) OVER w as e_gmv,
                    FIRST_VALUE(prepay_pay_settle_1h) OVER w as s_pps, LAST_VALUE(prepay_pay_settle_1h) OVER w as e_pps,
                    FIRST_VALUE(refund_rate_1h) OVER w as s_rr, LAST_VALUE(refund_rate_1h) OVER w as e_rr,
                    LAST_VALUE(overall_order_count) OVER w as e_all_osc,
                    LAST_VALUE(overall_show_count) OVER w as e_show,
                    LAST_VALUE(overall_click_count) OVER w as e_clk,
                    LAST_VALUE(overall_ctr) OVER w as e_ctr,
                    LAST_VALUE(overall_conversion_rate) OVER w as e_cconv,
                    MIN(created_at) OVER (
                        PARTITION BY COALESCE(target_uid, 'legacy_unscoped'), material_id
                    ) as period_start_time,
                    MAX(created_at) OVER (
                        PARTITION BY COALESCE(target_uid, 'legacy_unscoped'), material_id
                    ) as period_end_time
                FROM RawData
                WINDOW w AS (
                    PARTITION BY COALESCE(target_uid, 'legacy_unscoped'), material_id
                    ORDER BY created_at ASC
                    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                )
            )
            SELECT
                target_uid,
                material_id,
                ad_id,
                promotion_scene,
                plan_system,
                product_ids_json,
                aadvid,
                video_name,
                material_status,
                show_status,
                video_type,
                video_id,
                aweme_item_id,
                cover_url,
                video_duration,
                video_create_time,
                lego_source,
                stat_cost,
                COALESCE(e_osc, 0) AS order_settle_count_1h_latest,
                ROUND(COALESCE(e_osa, 0), 2) AS order_settle_amount_1h_latest,
                ROUND(COALESCE(e_osr, 0), 4) AS order_settle_rate_1h_latest,
                ROUND(COALESCE(e_ppoc, 0), 2) AS prepay_pay_order_count_latest,
                ROUND(COALESCE(e_gmv, 0), 2) AS pay_gmv_include_coupon_latest,
                ROUND(COALESCE(e_pps, 0), 2) AS prepay_pay_settle_1h_latest,
                ROUND(COALESCE(e_rr, 0), 4) AS refund_rate_1h_latest,
                COALESCE(e_all_osc, 0) AS overall_order_count_latest,
                COALESCE(e_show, 0) AS overall_show_count_latest,
                COALESCE(e_clk, 0) AS overall_click_count_latest,
                ROUND(COALESCE(e_ctr, 0), 4) AS overall_ctr_latest,
                ROUND(COALESCE(e_cconv, 0), 4) AS overall_conversion_rate_latest,
                ROUND(COALESCE(e_cost, 0) - COALESCE(s_cost, 0), 2) AS stat_cost_diff,
                period_start_time,
                period_end_time
            FROM WindowedData
            GROUP BY target_uid, material_id
            ORDER BY {order_by_field} {order_direction};
            """

            all_results = self.db.execute(
                sql,
                params=(target_uid,) if target_uid else None,
                fetch=True,
            )
            rows_list = [dict(r) for r in (all_results or [])]

            if rows_list:
                attach_estimated_ecpm(
                    rows_list,
                    self.db,
                    gmv_key="pay_gmv_include_coupon_latest",
                    orders_key="overall_order_count_latest",
                    ctr_key="overall_ctr_latest",
                    cvr_key="overall_conversion_rate_latest",
                    out_key="_estimated_ecpm",
                )

            if sort_by == 'estimatedEcpm':
                desc = sort_order.lower() != 'asc'

                def _ecpm_sort_key(x: Dict[str, Any]) -> Any:
                    v = x.get('_estimated_ecpm')
                    if v is None:
                        return (1, 0.0)
                    return (0, -float(v)) if desc else (0, float(v))

                rows_list.sort(key=_ecpm_sort_key)

            total = len(rows_list)
            total_pages = (total + page_size - 1) // page_size if page_size > 0 else 1

            offset = (page - 1) * page_size
            results = rows_list[offset : offset + page_size] if rows_list else []

            if not results:
                return {"success": True, "data": [], "total": total, "period": display_period,
                        "page": page, "pageSize": page_size, "totalPages": total_pages}

            # 转换为前端需要的格式
            data = []
            for row in results:
                try:
                    product_ids = json.loads(row.get('product_ids_json') or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    product_ids = []
                if not isinstance(product_ids, list):
                    product_ids = []
                data.append({
                    'id': row.get('material_id'),
                    'targetUid': row.get('target_uid') or 'legacy_unscoped',
                    'adId': row.get('ad_id'),
                    'promotionScene': row.get('promotion_scene') or 'live',
                    'planSystem': row.get('plan_system') or 'unknown',
                    'productIds': [str(v) for v in product_ids if str(v or '').strip()],
                    'aadvid': row.get('aadvid'),
                    'title': row.get('video_name', '未命名'),
                    'materialStatus': row.get('material_status'),
                    'showStatus': row.get('show_status'),
                    'videoType': row.get('video_type'),
                    'videoId': row.get('video_id'),
                    'awemeItemId': row.get('aweme_item_id'),
                    'cover': row.get('cover_url', ''),
                    'duration': row.get('video_duration'),
                    'createTime': row.get('video_create_time'),
                    'currentCost': row.get('stat_cost', 0) or 0,
                    # cost 使用差值其它指标使用最新值
                    'costDiff': row.get('stat_cost_diff', 0) or 0,
                    'netRoi': row.get('prepay_pay_settle_1h_latest', 0) or 0,
                    'netAmount': row.get('order_settle_amount_1h_latest', 0) or 0,
                    'hourRefundRate': row.get('refund_rate_1h_latest', 0) or 0,
                    'overallPayRoi': row.get('prepay_pay_order_count_latest', 0) or 0,
                    'overallAmount': row.get('pay_gmv_include_coupon_latest', 0) or 0,
                    'netSettleRate': row.get('order_settle_rate_1h_latest', 0) or 0,
                    'netOrderCount': row.get('order_settle_count_1h_latest', 0) or 0,
                    'overallOrderCount': int(row.get('overall_order_count_latest') or 0),
                    'overallShowCount': int(row.get('overall_show_count_latest') or 0),
                    'overallClickCount': int(row.get('overall_click_count_latest') or 0),
                    'overallCtr': row.get('overall_ctr_latest', 0) or 0,
                    'overallConversionRate': row.get('overall_conversion_rate_latest', 0) or 0,
                    'estimatedEcpm': row.get('_estimated_ecpm'),
                    # 时间周期
                    'periodStartTime': row.get('period_start_time'),
                    'periodEndTime': row.get('period_end_time'),
                })

            return {
                "success": True,
                "data": data,
                "total": total,
                "period": display_period,
                "page": page,
                "pageSize": page_size,
                "totalPages": total_pages
            }

        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"success": False, "data": [], "total": 0, "message": str(e)}

    def get_top20_by_cost(
        self,
        hours: int = 1,
        aavid: Optional[str] = None,
        target_uid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取最近 N 小时内每个素材最新的一条数据，按整体消耗排序取 Top 20

        Args:
            hours: 最近多少小时，默认 1 小时

        Returns:
            {
                "success": True/False,
                "data": [...],  # 素材列表，按 stat_cost 降序
                "total": 0
            }
        """
        return self.optimized.get_top20_by_cost(
            aavid=aavid or "", target_uid=target_uid or ""
        )
        try:
            time_str = f"-{hours} hours"

            # 查询最近 N 小时内每个素材的最新一条数据，按 stat_cost 排序取 Top 20
            sql = f"""
            WITH LatestData AS (
                -- 按 material_id 分组，取 created_at 最新的那条记录
                SELECT * FROM (
                    SELECT
                        *,
                        ROW_NUMBER() OVER (PARTITION BY material_id ORDER BY created_at DESC) as rn
                    FROM pmc_promotion_material
                    WHERE created_at >= datetime('now', '+8 hours', '{time_str}')
                )
                WHERE rn = 1
            )
            SELECT
                material_id,
                aadvid,
                video_name,
                material_status,
                show_status,
                video_type,
                video_id,
                aweme_item_id,
                cover_url,
                video_duration,
                video_create_time,
                lego_source,
                stat_cost,
                created_at
            FROM LatestData
            ORDER BY stat_cost DESC
            LIMIT 20;
            """

            results = self.db.execute(sql, fetch=True)

            if not results:
                return {"success": True, "data": [], "total": 0}

            # 转换为前端需要的格式
            data = []
            for row in results:
                data.append({
                    'id': row.get('material_id'),
                    'aadvid': row.get('aadvid'),
                    'title': row.get('video_name', '未命名'),
                    'materialStatus': row.get('material_status'),
                    'showStatus': row.get('show_status'),
                    'videoType': row.get('video_type'),
                    'videoId': row.get('video_id'),
                    'awemeItemId': row.get('aweme_item_id'),
                    'cover': row.get('cover_url', ''),
                    'duration': row.get('video_duration'),
                    'createTime': row.get('video_create_time'),
                    'currentCost': row.get('stat_cost', 0) or 0,
                    'createdAt': row.get('created_at'),
                })

            return {
                "success": True,
                "data": data,
                "total": len(data)
            }

        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"success": False, "data": [], "total": 0, "message": str(e)}

    def get_latest_crawl_cost_sum(
        self,
        hours: int = 1,
        aavid: Optional[str] = None,
        target_uid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        在「与 Top20 相同」的最近 N 小时时间窗内，按 material_id 分组，
        每个素材只取 created_at 最新的一条记录，再对这些记录的 stat_cost 求和。
        rowCount 为参与汇总的素材条数（去重后的素材数）。

        Returns:
            {
              "success": True/False,
              "totalCost": float,
              "rowCount": int,
              "batchMinuteKey": None,  # 已废弃，兼容旧字段
              "latestCreatedAt": str | None  # 上述「最新一条」里较晚的入库时间
            }
        """
        return self.optimized.get_latest_cost_sum(
            aavid=aavid or "", target_uid=target_uid or ""
        )
        try:
            hours = int(hours) if hours else 1
            if hours <= 0:
                hours = 1
            time_str = f"-{hours} hours"

            sql = f"""
            WITH LatestPerMaterial AS (
                SELECT
                    material_id,
                    stat_cost,
                    created_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY material_id ORDER BY created_at DESC
                    ) AS rn
                FROM pmc_promotion_material
                WHERE created_at >= datetime('now', '+8 hours', '{time_str}')
            )
            SELECT
                COALESCE(SUM(COALESCE(stat_cost, 0)), 0) AS total_cost,
                COUNT(*) AS row_count,
                MAX(created_at) AS latest_created_at
            FROM LatestPerMaterial
            WHERE rn = 1;
            """

            rows = self.db.execute(sql, fetch=True)
            if not rows:
                return {
                    "success": True,
                    "totalCost": 0.0,
                    "rowCount": 0,
                    "batchMinuteKey": None,
                    "latestCreatedAt": None,
                }

            row = rows[0]
            total = float(row.get("total_cost") or 0)
            cnt = int(row.get("row_count") or 0)
            latest_at = row.get("latest_created_at")

            return {
                "success": True,
                "totalCost": round(total, 2),
                "rowCount": cnt,
                "batchMinuteKey": None,
                "latestCreatedAt": str(latest_at) if latest_at else None,
            }
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "totalCost": 0.0,
                "rowCount": 0,
                "batchMinuteKey": None,
                "latestCreatedAt": None,
                "message": str(e),
            }

    # 调控任务表排序白名单（列名与 SQLite 一致）
    _ROI2_ASSIST_SORT_COLUMNS = frozenset(
        {
            "assist_task_id",
            "aadvid",
            "ad_id",
            "task_name",
            "create_time",
            "updated_at",
            "budget",
            "bid",
            "ecp_roi2_goal",
            "modify_time",
            "ad_delivery_name",
            "lab_ad_type",
            "deep_external_action_name",
            "deep_bid_type",
            "qcpx_mode",
            "adlab_mode",
            "daily_delivery_seconds",
            "show_cnt_for_roi2_assist",
            "click_cnt_for_roi2_assist",
            "ctr_for_roi2_assist",
            "convert_rate_for_roi2_assist",
            "stat_cost_for_roi2_assist",
            "total_pay_order_count_for_roi2_assist",
            "total_pay_order_gmv_include_coupon_for_roi2_assist",
            "total_prepay_and_pay_order_roi2_assist",
            "total_cost_per_pay_order_for_roi2_assist",
            "pay_convert_cost_for_roi2_assist",
            "pay_convert_cnt_for_roi2_assist",
            "total_order_settle_amount_for_roi2_1h_assist",
            "total_order_settle_count_for_roi2_1h_assist",
            "total_refund_order_gmv_for_roi2_1h_rate_assist",
            "total_prepay_and_pay_settle_roi2_1h_assist",
            "total_pay_order_gmv_for_roi2_assist",
            "total_pay_order_coupon_amount_for_roi2_assist",
        }
    )

    def get_roi2_assist_table_data(
        self,
        aadvid: Optional[str] = None,
        sort_by: str = "stat_cost_for_roi2_assist",
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 50,
        *,
        ad_delivery_type: Optional[int] = None,
        regulation_full_scan: bool = False,
        assist_updated_within_minutes: Optional[int] = None,
        search: Optional[str] = None,
        target_uid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        全域调控任务（pmc_roi2_assist_task）列表，分页排序。
        aadvid 为空则不过滤广告主。
        默认：updated_at（本表入库更新时间，东八区）落在近 1 小时内。
        若传入 assist_updated_within_minutes>0，则改为「近 N 分钟内曾更新」，用于规则化停投减少无效遍历。
        ad_delivery_type 非空时追加等值过滤（如 0=调控中，与 schema 中 ad_delivery_type 一致）。
        regulation_full_scan=True 时放宽单页上限（供规则化停投拉全量），默认仍限制 200 条防前端一次过大。
        search 非空时按 assist_task_id（字符串包含）与 task_name（子串，不区分大小写）筛选。
        """
        if regulation_full_scan:
            # Rule evaluation must stay bounded.  Active monitored targets are
            # evaluated independently, so a 20k page is enough for the target
            # scale while preventing a single cycle from materialising half a
            # million rows in Python memory.
            ps = max(1, min(20_000, int(page_size or 20_000)))
        else:
            ps = max(1, min(200, int(page_size or 50)))
        try:
            order_col = sort_by if sort_by in self._ROI2_ASSIST_SORT_COLUMNS else "stat_cost_for_roi2_assist"
            order_dir = "ASC" if (sort_order or "").lower() == "asc" else "DESC"
            page = max(1, int(page or 1))
            page_size = ps
            offset = (page - 1) * page_size

            # 时间窗：默认近 1 天；assist_updated_within_minutes 指定时改为近 N 分钟（与 datetime('now','+8 hours') 一致）
            _mins: Optional[int] = None
            if assist_updated_within_minutes is not None:
                try:
                    _m = int(assist_updated_within_minutes)
                    if _m > 0:
                        _mins = min(_m, 24 * 60)
                except (TypeError, ValueError):
                    _mins = None
            if _mins is not None:
                _updated_ge = (
                    "datetime(REPLACE(SUBSTR(TRIM(updated_at), 1, 19), 'T', ' ')) >= "
                    f"datetime('now', '+8 hours', '-{_mins} minutes')"
                )
            else:
                _updated_ge = (
                    "datetime(REPLACE(SUBSTR(TRIM(updated_at), 1, 19), 'T', ' ')) >= "
                    "datetime('now', '+8 hours', '-1 hours')"
                )
            where_clauses = [
                "updated_at IS NOT NULL",
                "TRIM(updated_at) != ''",
                _updated_ge,
            ]
            params: List[Any] = []
            aadvid_s = (aadvid or "").strip()
            if aadvid_s:
                where_clauses.insert(0, "aadvid = ?")
                params.append(aadvid_s)
            target_uid_s = (target_uid or "").strip()
            if target_uid_s:
                where_clauses.insert(0, "target_uid = ?")
                params.insert(0, target_uid_s)
            if ad_delivery_type is not None:
                where_clauses.append("ad_delivery_type = ?")
                params.append(int(ad_delivery_type))
            search_s = ""
            if search is not None:
                search_s = str(search).strip()[:200]
            if search_s:
                needle = search_s.lower()
                where_clauses.append(
                    "("
                    "instr(lower(trim(cast(assist_task_id AS TEXT))), ?) > 0 "
                    "OR instr(lower(ifnull(task_name, '')), ?) > 0"
                    ")"
                )
                params.append(needle)
                params.append(needle)
            where_sql = " WHERE " + " AND ".join(where_clauses)

            count_sql = f"SELECT COUNT(*) AS c FROM pmc_roi2_assist_task{where_sql}"
            count_rows = self.db.execute(count_sql, tuple(params) if params else None, fetch=True)
            total = int((count_rows[0] or {}).get("c") or 0) if count_rows else 0
            total_pages = (total + page_size - 1) // page_size if page_size > 0 else 1

            data_sql = f"""
            SELECT
                id,
                assist_task_id,
                target_uid,
                promotion_scene,
                plan_system,
                product_ids_json,
                aadvid,
                ad_id,
                task_name,
                budget,
                bid,
                start_time,
                end_time,
                modify_time,
                create_time,
                order_id,
                aggregate_cid,
                ecp_roi2_goal,
                creator_user_id,
                mar_goal,
                prom_way,
                external_action,
                external_action_name,
                smart_bid_type,
                budget_mode,
                ad_cost_protect_status,
                deep_external_action,
                lab_ad_type,
                deep_external_action_name,
                deep_bid_type,
                qcpx_mode,
                adlab_mode,
                ad_delivery_type,
                ad_delivery_name,
                task_status_source,
                task_status_observed_at,
                ad_opt_type,
                hint_type,
                learning_phase,
                daily_delivery_seconds,
                show_cnt_for_roi2_assist,
                click_cnt_for_roi2_assist,
                ctr_for_roi2_assist,
                convert_rate_for_roi2_assist,
                stat_cost_for_roi2_assist,
                total_pay_order_count_for_roi2_assist,
                total_pay_order_gmv_include_coupon_for_roi2_assist,
                total_prepay_and_pay_order_roi2_assist,
                total_cost_per_pay_order_for_roi2_assist,
                pay_convert_cost_for_roi2_assist,
                pay_convert_cnt_for_roi2_assist,
                total_order_settle_amount_for_roi2_1h_assist,
                total_order_settle_count_for_roi2_1h_assist,
                total_refund_order_gmv_for_roi2_1h_rate_assist,
                total_prepay_and_pay_settle_roi2_1h_assist,
                total_pay_order_gmv_for_roi2_assist,
                total_pay_order_coupon_amount_for_roi2_assist,
                assist_materials_json,
                data_source,
                created_at,
                updated_at
            FROM pmc_roi2_assist_task
            {where_sql}
            ORDER BY {order_col} {order_dir}
            LIMIT ? OFFSET ?
            """
            q_params = list(params) + [page_size, offset]
            rows = self.db.execute(data_sql, tuple(q_params), fetch=True)
            data = [dict(r) for r in (rows or [])]

            return {
                "success": True,
                "data": data,
                "total": total,
                "page": page,
                "pageSize": page_size,
                "totalPages": total_pages,
                "sortBy": order_col,
                "sortOrder": order_dir.lower(),
            }
        except Exception as e:
            import traceback

            traceback.print_exc()
            return {
                "success": False,
                "data": [],
                "total": 0,
                "page": 1,
                "pageSize": ps,
                "totalPages": 0,
                "message": str(e),
            }

    def get_dashboard_account_label(self) -> Dict[str, Any]:
        """读取大屏账户标注（空字符串表示未自定义，前端展示默认占位文案）。"""
        from config import DASHBOARD_ACCOUNT_LABEL_FILE

        try:
            path = DASHBOARD_ACCOUNT_LABEL_FILE
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                label = (data.get("label") or "").strip()
                return {"success": True, "label": label}
            return {"success": True, "label": ""}
        except Exception as e:
            return {"success": False, "label": "", "message": str(e)}

    def set_dashboard_account_label(self, label: str) -> Dict[str, Any]:
        """保存大屏账户标注；空字符串表示清除为未自定义。"""
        from config import DASHBOARD_ACCOUNT_LABEL_FILE

        try:
            s = (label or "").strip()[:200]
            path = DASHBOARD_ACCOUNT_LABEL_FILE
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"label": s}, f, ensure_ascii=False, indent=2)
            return {"success": True, "label": s}
        except Exception as e:
            return {"success": False, "message": str(e)}
