"""
SQLite 数据库 CRUD 操作类
支持连接池、事务、批量操作等功能
"""
import sqlite3
from sqlite3 import Row
from contextlib import contextmanager
from typing import Dict, List, Any, Optional, Tuple, Union
import os
import threading
import time
from .log import logger
from config import DB_FILE, SQLITE_BUSY_TIMEOUT_SEC, SQLITE_JOURNAL_MODE_WAL


class SQLiteStore:
    """SQLite 数据库操作类"""

    # 表结构定义
    TABLE_SCHEMAS = {
        'pmc_promotion_material': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'aadvid': 'TEXT NOT NULL',
                'material_id': 'TEXT NOT NULL',
                'video_name': 'TEXT',
                'material_status': 'INTEGER',
                'show_status': 'INTEGER',
                'show_status_reason': 'TEXT',
                'upload_time': 'TEXT',
                'video_type': 'INTEGER',
                'video_id': 'TEXT',
                'aweme_item_id': 'INTEGER',
                'cover_url': 'TEXT',
                'cover_width': 'INTEGER',
                'cover_height': 'INTEGER',
                'video_duration': 'INTEGER',
                'video_title': 'TEXT',
                'lego_source': 'INTEGER',
                'video_create_time': 'TEXT',
                'tag_list': 'TEXT',
                'stat_cost': 'REAL',
                'order_settle_count_1h': 'INTEGER',
                'order_settle_amount_1h': 'REAL',
                'order_settle_rate_1h': 'REAL',
                'prepay_pay_order_count': 'REAL',
                'pay_gmv_include_coupon': 'REAL',
                'prepay_pay_settle_1h': 'REAL',
                'refund_rate_1h': 'REAL',
                'overall_order_count': 'INTEGER',
                'overall_show_count': 'INTEGER',
                'overall_click_count': 'INTEGER',
                'overall_ctr': 'REAL',
                'overall_conversion_rate': 'REAL',
                'stat_date': "TEXT NOT NULL DEFAULT (date('now', '+8 hours'))",
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_material_aadvid', 'aadvid'),
                ('idx_material_stat_date', 'stat_date'),
                ('idx_material_id', 'material_id'),
                ('idx_material_video_type', 'video_type'),
                ('idx_material_status', 'material_status'),
                ('idx_material_created_at', 'created_at'),
                ('idx_material_perf_lead', 'created_at, material_id'),
            ]
        },
        'pmc_ad_detail_basic': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'aadvid': 'TEXT NOT NULL',
                'ad_id': 'TEXT NOT NULL',
                'budget': 'TEXT',
                'audience_coverage_count': 'TEXT',
                'compensation_convert': 'TEXT',
                'ecp_roi2_goal': 'REAL',
                'creative_type': 'INTEGER',
                'user_info_id': 'TEXT',
                'user_info_name': 'TEXT',
                'user_info_unique_id': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_ad_detail_ad_id', 'ad_id'),
            ],
            # 业务唯一：同一广告主仅一行（与 insert_or_update unique_fields=['aadvid'] 一致）
            'unique_indexes': [
                ('uk_pmc_ad_detail_basic_aadvid', 'aadvid'),
            ],
        },
        # 追投执行流水（与 docs/schema_pmc_retargeting_run.sqlite.sql 一致；以本处为自动建表来源）
        'pmc_retargeting_run': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'aavid': 'TEXT NOT NULL',
                'ad_id': 'TEXT NOT NULL',
                'material_id': 'TEXT NOT NULL',
                'material_name': 'TEXT',
                'strategy_name': 'TEXT',
                'regulate_task_id': 'TEXT',
                'started_at': 'TEXT NOT NULL',
                'ended_at': 'TEXT NOT NULL',
                'duration_ms': 'INTEGER',
                'status': 'INTEGER NOT NULL CHECK (status IN (-1, 1))',
                'step': 'TEXT',
                'message': 'TEXT',
                'detail': 'TEXT',
                'retargeting_method': 'TEXT',
                'optimization_goal': 'TEXT',
                'retargeting_json': 'TEXT',
                'rule_full_json': 'TEXT',
                'trigger_snapshot_json': 'TEXT',
                'query_snapshot_json': 'TEXT',
                'headless': 'INTEGER NOT NULL DEFAULT 0 CHECK (headless IN (0, 1))',
                'browser_headless_rule': 'INTEGER CHECK (browser_headless_rule IN (0, 1))',
                'trigger_source': 'TEXT',
                'app_version': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_pmc_retargeting_run_started', 'started_at'),
                ('idx_pmc_retargeting_run_account_material', 'aavid, material_id, started_at'),
                ('idx_pmc_retargeting_run_status_time', 'status, started_at'),
            ],
        },
        # 规则追投限频（每素材一行；窗口与次数由 rule retargeting.interval 解释，不存库）
        'pmc_retargeting_rate_limit': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'material_id': 'TEXT NOT NULL',
                'limit_started_at': 'TEXT NOT NULL',
                'use_count': 'INTEGER NOT NULL DEFAULT 0',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_pmc_retargeting_rate_limit_material_id', 'material_id'),
            ],
            'unique_indexes': [
                ('uk_pmc_retargeting_rate_limit_material_id', 'material_id'),
            ],
        },
        # 规则追投限频（按策略）：per_strategy_rate_limit 启用时，每 (material_id, strategy_id) 一行
        'pmc_retargeting_rate_limit_strategy': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'material_id': 'TEXT NOT NULL',
                'strategy_id': 'TEXT NOT NULL',
                'limit_started_at': 'TEXT NOT NULL',
                'use_count': 'INTEGER NOT NULL DEFAULT 0',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_pmc_rr_rl_strat_material', 'material_id'),
                ('idx_pmc_rr_rl_strat_strategy', 'strategy_id'),
            ],
            'unique_indexes': [
                ('uk_pmc_rr_rl_strat_mat_sid', 'material_id, strategy_id'),
            ],
        },
        # 规则化停投流水（与 docs/schema_pmc_regulation_run.sqlite.sql 一致）
        'pmc_regulation_run': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'aavid': 'TEXT NOT NULL',
                'ad_id': 'TEXT NOT NULL',
                'assist_task_id': 'TEXT',
                'task_name': 'TEXT',
                'strategy_name': 'TEXT',
                'stop_action': 'TEXT',
                'started_at': 'TEXT NOT NULL',
                'ended_at': 'TEXT NOT NULL',
                'duration_ms': 'INTEGER',
                'status': 'INTEGER NOT NULL CHECK (status IN (-1, 1, 2))',
                'step': 'TEXT',
                'message': 'TEXT',
                'detail': 'TEXT',
                'rule_full_json': 'TEXT',
                'trigger_snapshot_json': 'TEXT',
                'query_snapshot_json': 'TEXT',
                'headless': 'INTEGER NOT NULL DEFAULT 0 CHECK (headless IN (0, 1))',
                'browser_headless_rule': 'INTEGER CHECK (browser_headless_rule IN (0, 1))',
                'trigger_source': 'TEXT',
                'app_version': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_pmc_regulation_run_started', 'started_at'),
                ('idx_pmc_regulation_run_aavid_time', 'aavid, started_at'),
                ('idx_pmc_regulation_run_status_time', 'status, started_at'),
            ],
        },
        # 全域调控任务（素材追投等，与 docs/schema_pmc_roi2_assist_task.sqlite.sql 一致）
        'pmc_roi2_assist_task': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'assist_task_id': 'TEXT NOT NULL',
                'aadvid': 'TEXT NOT NULL',
                'ad_id': 'TEXT NOT NULL',
                'task_name': 'TEXT',
                'budget': 'TEXT',
                'bid': 'TEXT',
                'start_time': 'TEXT',
                'end_time': 'TEXT',
                'modify_time': 'TEXT',
                'create_time': 'TEXT',
                'order_id': 'TEXT',
                'aggregate_cid': 'TEXT',
                'ecp_roi2_goal': 'REAL',
                'creator_user_id': 'TEXT',
                'mar_goal': 'INTEGER',
                'prom_way': 'INTEGER',
                'external_action': 'INTEGER',
                'external_action_name': 'TEXT',
                'smart_bid_type': 'INTEGER',
                'budget_mode': 'INTEGER',
                'ad_cost_protect_status': 'INTEGER',
                'deep_external_action': 'INTEGER',
                'lab_ad_type': 'INTEGER',
                'deep_external_action_name': 'TEXT',
                'deep_bid_type': 'INTEGER',
                'qcpx_mode': 'INTEGER',
                'adlab_mode': 'INTEGER',
                'ad_delivery_type': 'INTEGER',
                'ad_delivery_name': 'TEXT',
                'ad_opt_type': 'INTEGER',
                'hint_type': 'INTEGER',
                'learning_phase': 'INTEGER',
                'daily_delivery_seconds': 'INTEGER',
                'show_cnt_for_roi2_assist': 'REAL',
                'click_cnt_for_roi2_assist': 'REAL',
                'ctr_for_roi2_assist': 'REAL',
                'convert_rate_for_roi2_assist': 'REAL',
                'stat_cost_for_roi2_assist': 'REAL',
                'total_pay_order_count_for_roi2_assist': 'REAL',
                'total_pay_order_gmv_include_coupon_for_roi2_assist': 'REAL',
                'total_prepay_and_pay_order_roi2_assist': 'REAL',
                'total_cost_per_pay_order_for_roi2_assist': 'REAL',
                'pay_convert_cost_for_roi2_assist': 'REAL',
                'pay_convert_cnt_for_roi2_assist': 'REAL',
                'total_order_settle_amount_for_roi2_1h_assist': 'REAL',
                'total_refund_order_gmv_for_roi2_1h_rate_assist': 'REAL',
                'total_prepay_and_pay_settle_roi2_1h_assist': 'REAL',
                'total_pay_order_gmv_for_roi2_assist': 'REAL',
                'total_pay_order_coupon_amount_for_roi2_assist': 'REAL',
                'assist_materials_json': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_pmc_roi2_assist_aadvid', 'aadvid'),
                ('idx_pmc_roi2_assist_created', 'created_at'),
            ],
            'unique_indexes': [
                ('uk_pmc_roi2_assist_task_assist_id', 'assist_task_id'),
            ],
        },
    }

    # 与列 DEFAULT 一致；UPDATE 时刷新 updated_at（SQLite 无内置「更新时自动改时间」）
    SQL_EXPR_DB_NOW = "datetime('now', '+8 hours')"

    @staticmethod
    def _load_config_from_env() -> Dict[str, Any]:
        """
        从环境变量或 .env 文件加载数据库配置

        Returns:
            数据库配置字典
        """
        return {
            'database': DB_FILE,
            'timeout': SQLITE_BUSY_TIMEOUT_SEC,
            'journal_wal': SQLITE_JOURNAL_MODE_WAL,
        }

    def __init__(
        self,
        database: Optional[str] = None,
        auto_create_tables: Optional[bool] = None,
        **kwargs
    ):
        """
        初始化数据库连接配置

        Args:
            database: 数据库文件路径，默认从环境变量或配置文件读取
            auto_create_tables: 已废弃，保留仅为兼容旧调用；建表请使用 init_sqlite_schema()
            **kwargs: 其他 sqlite3 连接参数
        """
        # 重新加载配置（支持动态更新）
        default_config = self._load_config_from_env()
        kw_journal = kwargs.pop('journal_wal', None)
        self._journal_wal = (
            kw_journal if kw_journal is not None else default_config.get('journal_wal', True)
        )

        # 使用传入的参数或默认配置（仅传入 sqlite3.connect 支持的键）
        self.config = {
            'database': database or default_config['database'],
            'check_same_thread': False,
            'timeout': default_config['timeout'],
            **kwargs
        }

        # 确保数据库目录存在
        db_path = self.config['database']
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        # 表字段缓存，格式: {table_name: set(column_names)}
        self._table_columns_cache: Dict[str, set] = {}

    def _get_connection(self) -> sqlite3.Connection:
        """
        获取数据库连接

        Returns:
            sqlite3.Connection: 数据库连接对象
        """
        try:
            connect_kw = {
                k: v
                for k, v in self.config.items()
                if k
                in (
                    "database",
                    "timeout",
                    "detect_types",
                    "isolation_level",
                    "check_same_thread",
                    "factory",
                    "cached_statements",
                    "uri",
                )
            }
            connection = sqlite3.connect(**connect_kw)
            # 设置Row工厂，使得查询结果可以通过列名访问
            connection.row_factory = Row
            # 启用外键约束
            connection.execute("PRAGMA foreign_keys = ON")
            if self._journal_wal:
                try:
                    jm = connection.execute("PRAGMA journal_mode=WAL").fetchone()
                    jm_val = jm[0] if jm else ""
                    if str(jm_val).upper() != "WAL":
                        logger.debug("SQLite journal_mode=%s（非 WAL 时并发更易 locked）", jm_val)
                except Exception as e:
                    logger.warning("PRAGMA journal_mode=WAL 失败: %s", e)
            return connection
        except Exception as e:
            logger.error(f"数据库连接失败: {e}")
            raise

    def _close_connection(self, connection: sqlite3.Connection):
        """关闭数据库连接"""
        try:
            if connection:
                connection.close()
        except Exception as e:
            logger.error(f"关闭连接失败: {e}")

    @contextmanager
    def _get_cursor(self, connection: Optional[sqlite3.Connection] = None):
        """
        获取游标的上下文管理器

        Args:
            connection: 数据库连接，如果为 None 则创建新连接

        Yields:
            sqlite3.Cursor: 游标对象
        """
        conn = connection or self._get_connection()
        cursor = None
        try:
            cursor = conn.cursor()
            yield cursor
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"数据库操作失败: {e}")
            raise
        finally:
            if cursor:
                cursor.close()
            if not connection:  # 如果是新创建的连接，则关闭
                self._close_connection(conn)

    def execute(
        self,
        sql: str,
        params: Optional[Union[Tuple, Dict, List]] = None,
        fetch: bool = False,
        commit: bool = True,
        connection: Optional[sqlite3.Connection] = None
    ) -> Optional[Union[List[Dict], int]]:
        """
        执行 SQL 语句

        Args:
            sql: SQL 语句
            params: 参数（元组、字典或列表）
            fetch: 是否返回查询结果，默认 False
            commit: 是否自动提交，默认 True
            connection: 数据库连接，如果提供则使用该连接（用于事务）

        Returns:
            如果 fetch=True，返回查询结果列表；否则返回影响的行数
        """
        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                if params:
                    if isinstance(params, dict):
                        # 字典参数需要转换格式
                        cursor.execute(sql, params)
                    else:
                        cursor.execute(sql, params)
                else:
                    cursor.execute(sql)

                if commit and not connection:
                    conn.commit()

                if fetch:
                    # 转换为字典列表
                    rows = cursor.fetchall()
                    if rows:
                        return [dict(row) for row in rows]
                    return []
                return cursor.rowcount
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"执行 SQL 失败: {sql[:100]}... 错误: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def execute_many(
        self,
        sql: str,
        params_list: List[Union[Tuple, Dict]],
        commit: bool = True,
        connection: Optional[sqlite3.Connection] = None
    ) -> int:
        """
        批量执行 SQL 语句

        Args:
            sql: SQL 语句
            params_list: 参数列表
            commit: 是否自动提交，默认 True
            connection: 数据库连接，如果提供则使用该连接（用于事务）

        Returns:
            影响的总行数
        """
        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                cursor.executemany(sql, params_list)

                if commit and not connection:
                    conn.commit()

                return cursor.rowcount
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"批量执行 SQL 失败: {sql[:100]}... 错误: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def select_pmc_latest_per_material_by_created_range(
        self,
        start_ts: str,
        end_ts: str,
        aadvid: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        查询 created_at 落在 [start_ts, end_ts) 内的行；每个 material_id 只保留 id 最大的一条（周期内最新一条）。
        """
        tbl = "pmc_promotion_material"
        if aadvid:
            sql = f"""
            SELECT t.* FROM {tbl} t
            INNER JOIN (
              SELECT material_id, MAX(id) AS max_id
              FROM {tbl}
              WHERE created_at >= ? AND created_at < ? AND aadvid = ?
              GROUP BY material_id
            ) x ON t.id = x.max_id
            """
            return self.execute(sql, (start_ts, end_ts, str(aadvid)), fetch=True) or []
        sql = f"""
            SELECT t.* FROM {tbl} t
            INNER JOIN (
              SELECT material_id, MAX(id) AS max_id
              FROM {tbl}
              WHERE created_at >= ? AND created_at < ?
              GROUP BY material_id
            ) x ON t.id = x.max_id
            """
        return self.execute(sql, (start_ts, end_ts), fetch=True) or []

    def select_pmc_latest_per_material_in_last_hour_utc8(
        self,
        aadvid: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        近 1 小时滚动窗口（与库表 created_at 语义一致）：
        created_at > datetime('now', '+8 hours', '-1 hours')
        每个 material_id 只保留 id 最大的一条（该周期内最新一条）。
        """
        tbl = "pmc_promotion_material"
        window_where = "created_at > datetime('now', '+8 hours', '-1 hours')"
        if aadvid:
            sql = f"""
            SELECT t.* FROM {tbl} t
            INNER JOIN (
              SELECT material_id, MAX(id) AS max_id
              FROM {tbl}
              WHERE {window_where} AND aadvid = ?
              GROUP BY material_id
            ) x ON t.id = x.max_id
            """
            return self.execute(sql, (str(aadvid),), fetch=True) or []
        sql = f"""
            SELECT t.* FROM {tbl} t
            INNER JOIN (
              SELECT material_id, MAX(id) AS max_id
              FROM {tbl}
              WHERE {window_where}
              GROUP BY material_id
            ) x ON t.id = x.max_id
            """
        return self.execute(sql, fetch=True) or []

    # ==================== 字段过滤辅助方法 ====================

    def _get_table_columns(
        self,
        table: str,
        connection: Optional[sqlite3.Connection] = None
    ) -> set:
        """
        获取表的字段列表（带缓存）

        Args:
            table: 表名
            connection: 数据库连接（用于事务）

        Returns:
            字段名集合
        """
        # 检查缓存
        if table in self._table_columns_cache:
            return self._table_columns_cache[table]

        # 从数据库获取表结构
        try:
            table_info = self.get_table_info(table)
            columns = {row['name'] for row in table_info}
            # 缓存结果
            self._table_columns_cache[table] = columns
            logger.debug(f"表 {table} 的字段列表已缓存: {columns}")
            return columns
        except Exception as e:
            logger.warning(f"获取表 {table} 的字段列表失败: {e}，将使用传入的所有字段")
            # 如果获取失败，返回空集合，表示不过滤
            return set()

    def _filter_data_by_table_columns(
        self,
        table: str,
        data: Dict[str, Any],
        connection: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Any]:
        """
        过滤数据，只保留表中存在的字段

        Args:
            table: 表名
            data: 数据字典
            connection: 数据库连接（用于事务）

        Returns:
            过滤后的数据字典
        """
        if not data:
            return data

        # 获取表的字段列表
        table_columns = self._get_table_columns(table, connection)

        # 如果无法获取表结构（空集合），则不过滤，直接返回原数据
        if not table_columns:
            return data

        # 过滤数据，只保留表中存在的字段
        filtered_data = {k: v for k, v in data.items() if k in table_columns}

        # 如果过滤后为空，返回原数据（可能是INSERT操作）
        if not filtered_data and data:
            logger.debug(f"表 {table} 过滤后数据为空，使用原始数据")
            return data

        return filtered_data

    def _table_schema_columns(self, table: str) -> Dict[str, str]:
        sch = self.TABLE_SCHEMAS.get(table)
        if not sch:
            return {}
        return sch.get("columns") or {}

    def _table_has_column(self, table: str, column: str) -> bool:
        return column in self._table_schema_columns(table)

    def _strip_app_managed_timestamps(self, table: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """INSERT / upsert 的 INSERT 分支不写 created_at、updated_at，由列 DEFAULT 填充。"""
        if not data:
            return data
        cols = self._table_schema_columns(table)
        out = dict(data)
        if "created_at" in cols:
            out.pop("created_at", None)
        if "updated_at" in cols:
            out.pop("updated_at", None)
        return out

    def clear_table_cache(self, table: Optional[str] = None):
        """
        清除表字段缓存

        Args:
            table: 表名，如果为 None 则清除所有缓存
        """
        if table:
            self._table_columns_cache.pop(table, None)
        else:
            self._table_columns_cache.clear()

    # ==================== 核心 CRUD 方法 ====================

    def insert(
        self,
        table: str,
        data: Dict[str, Any],
        connection: Optional[sqlite3.Connection] = None
    ) -> int:
        """
        插入数据

        Args:
            table: 表名
            data: 数据字典
            connection: 数据库连接（用于事务）

        Returns:
            插入的行ID
        """
        # 过滤不存在的字段
        data = self._filter_data_by_table_columns(table, data, connection)
        data = self._strip_app_managed_timestamps(table, data)

        if not data:
            logger.warning(f"表 {table} 没有有效数据可插入")
            return 0

        # 构建 SQL
        columns = ', '.join(data.keys())
        placeholders = ', '.join(['?' for _ in data])
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"

        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                cursor.execute(sql, tuple(data.values()))
                if not connection:
                    conn.commit()
                return cursor.lastrowid
        except Exception as e:
            if conn and not connection:
                conn.rollback()
            logger.error(f"插入数据失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def insert_many(
        self,
        table: str,
        data_list: List[Dict[str, Any]],
        connection: Optional[sqlite3.Connection] = None
    ) -> int:
        """
        批量插入数据

        Args:
            table: 表名
            data_list: 数据字典列表
            connection: 数据库连接（用于事务）

        Returns:
            插入的总行数
        """
        if not data_list:
            return 0

        # 过滤每个数据项
        filtered_list = []
        for data in data_list:
            filtered_data = self._filter_data_by_table_columns(table, data, connection)
            filtered_data = self._strip_app_managed_timestamps(table, filtered_data)
            if filtered_data:
                filtered_list.append(filtered_data)

        if not filtered_list:
            return 0

        # 使用第一个数据项的键构建 SQL
        columns = list(filtered_list[0].keys())
        columns_str = ', '.join(columns)
        placeholders = ', '.join(['?' for _ in columns])
        sql = f"INSERT INTO {table} ({columns_str}) VALUES ({placeholders})"

        # 构建参数列表
        params_list = [tuple(data.get(col) for col in columns) for data in filtered_list]

        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                cursor.executemany(sql, params_list)
                if not connection:
                    conn.commit()
                return cursor.rowcount
        except Exception as e:
            if conn and not connection:
                conn.rollback()
            logger.error(f"批量插入数据失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def insert_or_update(
        self,
        table: str,
        data: Dict[str, Any],
        unique_fields: List[str],
        update_fields: Optional[List[str]] = None,
        connection: Optional[sqlite3.Connection] = None
    ) -> int:
        """
        插入或更新数据（存在则更新，不存在则插入）

        Args:
            table: 表名
            data: 数据字典
            unique_fields: 唯一约束字段列表，用于判断是否存在
            update_fields: 更新字段列表，如果为 None 则更新除 unique_fields 外的所有字段
            connection: 数据库连接（用于事务）

        Returns:
            影响的行数
        """
        # 过滤不存在的字段
        data = self._filter_data_by_table_columns(table, data, connection)
        data = self._strip_app_managed_timestamps(table, data)

        if not data:
            logger.warning(f"表 {table} 没有有效数据可插入")
            return 0

        # 构建查询条件
        where_parts = [f"{field} = ?" for field in unique_fields]
        where_clause = ' AND '.join(where_parts)
        where_values = tuple(data.get(field) for field in unique_fields)

        # 检查是否存在
        sql = f"SELECT 1 FROM {table} WHERE {where_clause} LIMIT 1"
        conn = connection or self._get_connection()

        try:
            with self._get_cursor(conn) as cursor:
                cursor.execute(sql, where_values)
                exists = cursor.fetchone() is not None

                if exists:
                    # 存在则更新：不改 created_at；updated_at 用库侧表达式刷新（SQLite 无 ON UPDATE 默认）
                    if update_fields is None:
                        update_fields = [k for k in data.keys() if k not in unique_fields]
                    else:
                        update_fields = list(update_fields)

                    update_fields = [
                        f for f in update_fields
                        if f not in ("created_at", "updated_at")
                    ]

                    set_parts = [f"{field} = ?" for field in update_fields]
                    update_values = tuple(data.get(field) for field in update_fields)
                    if self._table_has_column(table, "updated_at"):
                        set_parts.append(f"updated_at = {self.SQL_EXPR_DB_NOW}")

                    if not set_parts:
                        return 0

                    set_clause = ', '.join(set_parts)
                    sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
                    params = update_values + where_values
                    cursor.execute(sql, params)
                else:
                    # 不存在则插入
                    columns = ', '.join(data.keys())
                    placeholders = ', '.join(['?' for _ in data])
                    sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
                    cursor.execute(sql, tuple(data.values()))

                if not connection:
                    conn.commit()
                return cursor.rowcount
        except Exception as e:
            if conn and not connection:
                conn.rollback()
            logger.error(f"插入或更新数据失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def select(
        self,
        table: str,
        fields: Optional[Union[str, List[str]]] = None,
        where: Optional[Union[str, Dict[str, Any]]] = None,
        params: Optional[Union[Tuple, List]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        connection: Optional[sqlite3.Connection] = None
    ) -> List[Dict[str, Any]]:
        """
        查询数据

        Args:
            table: 表名
            fields: 查询字段，字符串或列表，默认 *
            where: WHERE 条件，字符串或字典
            params: WHERE 参数（元组或列表）
            order_by: 排序字段
            limit: 限制返回行数
            offset: 偏移量
            connection: 数据库连接（用于事务）

        Returns:
            查询结果列表
        """
        # 构建字段列表
        if fields is None:
            field_str = '*'
        elif isinstance(fields, str):
            field_str = fields
        else:
            field_str = ', '.join(fields)

        # 构建 SQL
        sql = f"SELECT {field_str} FROM {table}"

        # 构建 WHERE 条件
        where_clause = ""
        where_params = []
        if where:
            if isinstance(where, str):
                where_clause = f" WHERE {where}"
                where_params = list(params) if params else []
            elif isinstance(where, dict):
                where_parts = []
                for k, v in where.items():
                    if v is None:
                        where_parts.append(f"{k} IS NULL")
                    else:
                        where_parts.append(f"{k} = ?")
                        where_params.append(v)
                where_clause = f" WHERE {' AND '.join(where_parts)}"

        sql += where_clause

        # 添加排序
        if order_by:
            sql += f" ORDER BY {order_by}"

        # 添加分页
        if limit is not None:
            sql += f" LIMIT {limit}"
        if offset is not None:
            sql += f" OFFSET {offset}"

        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                cursor.execute(sql, tuple(where_params))
                rows = cursor.fetchall()
                return [dict(row) for row in rows] if rows else []
        except Exception as e:
            logger.error(f"查询数据失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def select_one(
        self,
        table: str,
        fields: Optional[Union[str, List[str]]] = None,
        where: Optional[Union[str, Dict[str, Any]]] = None,
        params: Optional[Union[Tuple, List]] = None,
        order_by: Optional[str] = None,
        connection: Optional[sqlite3.Connection] = None
    ) -> Optional[Dict[str, Any]]:
        """
        查询单条数据

        Args:
            table: 表名
            fields: 查询字段
            where: WHERE 条件
            params: WHERE 参数
            order_by: 排序字段
            connection: 数据库连接（用于事务）

        Returns:
            查询结果字典，如果没有结果返回 None
        """
        results = self.select(
            table=table,
            fields=fields,
            where=where,
            params=params,
            order_by=order_by,
            limit=1,
            connection=connection
        )
        return results[0] if results else None

    def update(
        self,
        table: str,
        data: Dict[str, Any],
        where: Optional[Union[str, Dict[str, Any]]] = None,
        params: Optional[Union[Tuple, List]] = None,
        connection: Optional[sqlite3.Connection] = None
    ) -> int:
        """
        更新数据

        Args:
            table: 表名
            data: 更新的数据字典
            where: WHERE 条件
            params: WHERE 参数
            connection: 数据库连接（用于事务）

        Returns:
            影响的行数
        """
        # 过滤不存在的字段
        data = self._filter_data_by_table_columns(table, data, connection)

        if not data:
            return 0

        # 构建 SQL
        set_parts = []
        set_values = []
        for k, v in data.items():
            if v is None:
                set_parts.append(f"{k} = NULL")
            else:
                set_parts.append(f"{k} = ?")
                set_values.append(v)

        set_clause = ', '.join(set_parts)
        sql = f"UPDATE {table} SET {set_clause}"

        # 构建 WHERE 条件
        where_params = []
        if where:
            if isinstance(where, str):
                sql += f" WHERE {where}"
                where_params = list(params) if params else []
            elif isinstance(where, dict):
                where_parts = []
                for k, v in where.items():
                    if v is None:
                        where_parts.append(f"{k} IS NULL")
                    else:
                        where_parts.append(f"{k} = ?")
                        where_params.append(v)
                sql += f" WHERE {' AND '.join(where_parts)}"

        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                cursor.execute(sql, tuple(set_values + where_params))
                if not connection:
                    conn.commit()
                return cursor.rowcount
        except Exception as e:
            if conn and not connection:
                conn.rollback()
            logger.error(f"更新数据失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def delete(
        self,
        table: str,
        where: Optional[Union[str, Dict[str, Any]]] = None,
        params: Optional[Union[Tuple, List]] = None,
        connection: Optional[sqlite3.Connection] = None
    ) -> int:
        """
        删除数据

        Args:
            table: 表名
            where: WHERE 条件
            params: WHERE 参数
            connection: 数据库连接（用于事务）

        Returns:
            影响的行数
        """
        if not where:
            logger.warning(f"表 {table} 的删除操作没有 WHERE 条件，拒绝执行")
            return 0

        # 构建 SQL
        sql = f"DELETE FROM {table}"

        # 构建 WHERE 条件
        where_params = []
        if isinstance(where, str):
            sql += f" WHERE {where}"
            where_params = list(params) if params else []
        elif isinstance(where, dict):
            where_parts = []
            for k, v in where.items():
                if v is None:
                    where_parts.append(f"{k} IS NULL")
                else:
                    where_parts.append(f"{k} = ?")
                    where_params.append(v)
            sql += f" WHERE {' AND '.join(where_parts)}"

        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                cursor.execute(sql, tuple(where_params))
                if not connection:
                    conn.commit()
                return cursor.rowcount
        except Exception as e:
            if conn and not connection:
                conn.rollback()
            logger.error(f"删除数据失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def count(
        self,
        table: str,
        where: Optional[Union[str, Dict[str, Any]]] = None,
        params: Optional[Union[Tuple, List]] = None,
        field: str = '*',
        connection: Optional[sqlite3.Connection] = None
    ) -> int:
        """
        统计数据数量

        Args:
            table: 表名
            where: WHERE 条件
            params: WHERE 参数
            field: 统计字段
            connection: 数据库连接（用于事务）

        Returns:
            数量
        """
        sql = f"SELECT COUNT({field}) as cnt FROM {table}"

        # 构建 WHERE 条件
        where_params = []
        if where:
            if isinstance(where, str):
                sql += f" WHERE {where}"
                where_params = list(params) if params else []
            elif isinstance(where, dict):
                where_parts = []
                for k, v in where.items():
                    if v is None:
                        where_parts.append(f"{k} IS NULL")
                    else:
                        where_parts.append(f"{k} = ?")
                        where_params.append(v)
                sql += f" WHERE {' AND '.join(where_parts)}"

        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                cursor.execute(sql, tuple(where_params))
                result = cursor.fetchone()
                return result['cnt'] if result else 0
        except Exception as e:
            logger.error(f"统计数据失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def exists(
        self,
        table: str,
        where: Union[str, Dict[str, Any]],
        params: Optional[Union[Tuple, List]] = None,
        connection: Optional[sqlite3.Connection] = None
    ) -> bool:
        """
        检查数据是否存在

        Args:
            table: 表名
            where: WHERE 条件
            params: WHERE 参数
            connection: 数据库连接（用于事务）

        Returns:
            是否存在
        """
        results = self.select(
            table=table,
            fields='1',
            where=where,
            params=params,
            limit=1,
            connection=connection
        )
        return len(results) > 0

    @contextmanager
    def transaction(self, connection: Optional[sqlite3.Connection] = None):
        """
        事务上下文管理器

        Args:
            connection: 数据库连接，如果为 None 则创建新连接

        Usage:
            with db.transaction() as conn:
                db.insert('table', data, connection=conn)
                db.update('table', data, where, connection=conn)
        """
        conn = connection or self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"事务执行失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    # ==================== 表管理方法 ====================

    def create_table(
        self,
        table: str,
        columns: Dict[str, str],
        if_not_exists: bool = True,
        connection: Optional[sqlite3.Connection] = None
    ):
        """
        创建表

        Args:
            table: 表名
            columns: 字段定义字典，格式: {字段名: 字段类型和约束}
            if_not_exists: 是否使用 IF NOT EXISTS
            connection: 数据库连接（用于事务）
        """
        # 构建字段定义
        column_defs = []
        for name, definition in columns.items():
            column_defs.append(f"{name} {definition}")

        if_str = "IF NOT EXISTS " if if_not_exists else ""
        sql = f"CREATE TABLE {if_str}{table} (\n  " + ",\n  ".join(column_defs) + "\n)"

        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                cursor.execute(sql)
                if not connection:
                    conn.commit()
            # 清除表缓存
            self.clear_table_cache(table)
            logger.info(f"表 {table} 创建成功")
        except Exception as e:
            if conn and not connection:
                conn.rollback()
            logger.error(f"创建表 {table} 失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def drop_table(
        self,
        table: str,
        if_exists: bool = True,
        connection: Optional[sqlite3.Connection] = None
    ):
        """
        删除表

        Args:
            table: 表名
            if_exists: 是否使用 IF EXISTS
            connection: 数据库连接（用于事务）
        """
        if_str = "IF EXISTS " if if_exists else ""
        sql = f"DROP TABLE {if_str}{table}"

        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                cursor.execute(sql)
                if not connection:
                    conn.commit()
            # 清除表缓存
            self.clear_table_cache(table)
            logger.info(f"表 {table} 删除成功")
        except Exception as e:
            if conn and not connection:
                conn.rollback()
            logger.error(f"删除表 {table} 失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def truncate_table(
        self,
        table: str,
        connection: Optional[sqlite3.Connection] = None
    ):
        """
        清空表数据（使用 DELETE）

        Args:
            table: 表名
            connection: 数据库连接（用于事务）
        """
        sql = f"DELETE FROM {table}"

        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                cursor.execute(sql)
                if not connection:
                    conn.commit()
            logger.info(f"表 {table} 数据已清空")
        except Exception as e:
            if conn and not connection:
                conn.rollback()
            logger.error(f"清空表 {table} 失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def test_connection(self) -> bool:
        """
        测试数据库连接

        Returns:
            连接是否成功
        """
        try:
            self.execute("SELECT 1", fetch=True)
            return True
        except Exception as e:
            logger.error(f"数据库连接测试失败: {e}")
            return False

    def get_table_info(self, table: str) -> List[Dict]:
        """
        获取表结构信息

        Args:
            table: 表名

        Returns:
            字段信息列表
        """
        sql = f"PRAGMA table_info({table})"
        return self.execute(sql, fetch=True) or []

    def get_tables(self) -> List[str]:
        """
        获取所有表名

        Returns:
            表名列表
        """
        sql = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        results = self.execute(sql, fetch=True) or []
        return [row['name'] for row in results]

    def get_table_definitions(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有表的定义信息

        Returns:
            表定义字典
        """
        tables = self.get_tables()
        definitions = {}
        for table in tables:
            definitions[table] = {
                'columns': self.get_table_info(table),
            }
        return definitions

    def _ensure_table_columns_match_schema(
        self,
        table_name: str,
        columns: Dict[str, str],
        connection: sqlite3.Connection,
    ) -> None:
        """
        对照 TABLE_SCHEMAS：若表已存在且缺少列，则 ALTER TABLE ADD COLUMN。
        用于已有数据的旧库在新增字段后自动补齐结构。
        """
        cursor = connection.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        existing = {row["name"] for row in cursor.fetchall()}

        for col_name, col_def in columns.items():
            if col_name in existing:
                continue
            if not self._validate_sql_identifier(col_name):
                logger.warning(f"跳过非法列名，无法 ADD COLUMN: {col_name}")
                continue
            alter_sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}"
            cursor.execute(alter_sql)
            logger.info(f"表 {table_name} 已新增列 {col_name}")

        self.clear_table_cache(table_name)

    def create_all_tables(self, connection: Optional[sqlite3.Connection] = None):
        """
        创建所有预定义的表

        Args:
            connection: 数据库连接（用于事务）
        """
        conn = connection or self._get_connection()
        try:
            with self._get_cursor(conn) as cursor:
                # 遍历表结构定义创建表和索引
                for table_name, table_schema in self.TABLE_SCHEMAS.items():
                    # 构建字段定义
                    column_defs = []
                    for col_name, col_def in table_schema['columns'].items():
                        column_defs.append(f"{col_name} {col_def}")

                    sql = f"CREATE TABLE IF NOT EXISTS {table_name} (\n  " + ",\n  ".join(column_defs) + "\n)"
                    cursor.execute(sql)

                    # 须在创建索引之前补齐列：旧库缺列时索引会引用尚不存在的字段
                    self._ensure_table_columns_match_schema(
                        table_name, table_schema['columns'], conn
                    )

                    # 创建索引
                    if 'indexes' in table_schema:
                        for idx_name, idx_col in table_schema['indexes']:
                            idx_sql = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_name}({idx_col})"
                            cursor.execute(idx_sql)

                    # 唯一索引（显式 UNIQUE 约束，便于旧库补建与 PRAGMA 可见）
                    if table_schema.get('unique_indexes'):
                        self._ensure_unique_indexes(
                            table_name,
                            table_schema['unique_indexes'],
                            conn,
                        )

                    logger.info(f"表 {table_name} 创建成功")

                if not connection:
                    conn.commit()

            # 清除所有表缓存
            self.clear_table_cache()
            logger.info("所有表创建成功")
        except Exception as e:
            if conn and not connection:
                conn.rollback()
            logger.error(f"创建表失败: {e}")
            raise
        finally:
            if not connection:
                self._close_connection(conn)

    def init_tables(self):
        """
        初始化表（别名方法，与 create_all_tables 相同）
        """
        self.create_all_tables()

    @staticmethod
    def _validate_sql_identifier(name: str) -> bool:
        if not name or not name.isidentifier():
            return False
        return name.replace("_", "").isalnum()

    @staticmethod
    def _validate_index_columns_expr(cols_expr: str) -> bool:
        """校验 CREATE INDEX ... ON t(a,b) 中的列表达式（逗号分隔，均为合法标识符）。"""
        if not cols_expr or not str(cols_expr).strip():
            return False
        for part in str(cols_expr).split(","):
            p = part.strip()
            if not p or not SQLiteStore._validate_sql_identifier(p):
                return False
        return True

    def _ensure_unique_indexes(
        self,
        table_name: str,
        unique_indexes: List[Tuple[str, str]],
        connection: sqlite3.Connection,
    ) -> None:
        """
        为已存在的表补建 UNIQUE INDEX（CREATE UNIQUE INDEX IF NOT EXISTS）。
        新建表时在 create_all_tables 中同样调用，保证唯一约束与 TABLE_SCHEMAS 一致。
        """
        if not unique_indexes:
            return
        if not self._validate_sql_identifier(table_name):
            logger.warning(f"跳过非法表名，无法创建唯一索引: {table_name}")
            return
        cursor = connection.cursor()
        for uk_name, uk_cols in unique_indexes:
            if not self._validate_sql_identifier(uk_name):
                logger.warning(f"跳过非法唯一索引名: {uk_name}")
                continue
            if not self._validate_index_columns_expr(uk_cols):
                logger.warning(f"跳过非法唯一索引列: {table_name}.{uk_name}({uk_cols})")
                continue
            idx_sql = (
                f"CREATE UNIQUE INDEX IF NOT EXISTS {uk_name} "
                f"ON {table_name}({uk_cols})"
            )
            try:
                cursor.execute(idx_sql)
                logger.debug(f"表 {table_name} 唯一索引: {uk_name}({uk_cols})")
            except sqlite3.OperationalError as e:
                # 常见：历史数据存在重复，无法加 UNIQUE
                logger.error(
                    f"表 {table_name} 创建唯一索引失败 {uk_name}({uk_cols}): {e}；"
                    f"请清理重复数据后重试或手动处理"
                )
                raise

    def prune_table_keep_latest_by_id(
        self,
        table: str,
        max_rows: int,
        id_column: str = "id",
        batch_size: int = 50_000,
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        """
        只保留 id 最大的 max_rows 行，删除更早的行（用于控制单表行数与库文件膨胀）。

        实现：先求「最新 max_rows 行里最小的 id」作为 cutoff，再分批 DELETE id < cutoff。
        走主键/rowid 范围删除，避免大事务一次性锁住过久。

        Args:
            table: 表名（须在 TABLE_SCHEMAS 中）
            max_rows: 保留的最大行数（按 id 降序取前 max_rows）
            id_column: 单调递增主键列名，默认 id
            batch_size: 每批删除行数上限
            connection: 外部事务连接；不传则自开连接并提交

        Returns:
            累计删除行数
        """
        if max_rows < 1:
            return 0
        if table not in self.TABLE_SCHEMAS:
            raise ValueError(f"prune 仅支持预定义表: {list(self.TABLE_SCHEMAS.keys())}")
        if not self._validate_sql_identifier(id_column):
            raise ValueError(f"非法 id 列名: {id_column}")
        if batch_size < 1:
            batch_size = 50_000

        # 子查询求 cutoff：最新 max_rows 行中的最小 id
        cutoff_sql = (
            f"SELECT MIN({id_column}) FROM ("
            f"SELECT {id_column} FROM {table} ORDER BY {id_column} DESC LIMIT ?)"
        )

        delete_sql = (
            f"DELETE FROM {table} WHERE {id_column} IN ("
            f"SELECT {id_column} FROM {table} WHERE {id_column} < ? "
            f"ORDER BY {id_column} ASC LIMIT {batch_size})"
        )

        def _run(conn: sqlite3.Connection, commit_each_batch: bool) -> int:
            total = 0
            cursor = conn.cursor()
            try:
                cursor.execute(cutoff_sql, (max_rows,))
                row = cursor.fetchone()
                cutoff = row[0] if row else None
                if cutoff is None:
                    return 0

                while True:
                    cursor.execute(delete_sql, (cutoff,))
                    n = cursor.rowcount or 0
                    total += n
                    if commit_each_batch:
                        conn.commit()
                    if n == 0:
                        break
            finally:
                cursor.close()
            return total

        if connection is not None:
            return _run(connection, commit_each_batch=False)

        conn = self._get_connection()
        try:
            total = _run(conn, commit_each_batch=True)
            if total:
                logger.info(f"表 {table} 裁剪完成，删除 {total} 行（保留最近 {max_rows} 条，按 {id_column}）")
            return total
        except Exception as e:
            conn.rollback()
            logger.error(f"表 {table} 裁剪失败: {e}")
            raise
        finally:
            self._close_connection(conn)

    def prune_table_keep_latest_by_id_limited(
        self,
        table: str,
        max_rows: int,
        max_delete: int,
        id_column: str = "id",
        batch_size: int = 5000,
        sleep_between_batches_sec: float = 0.0,
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        """
        与 prune_table_keep_latest_by_id 相同的 cutoff（保留 id 最大的 max_rows 行），
        但本会话最多删除 max_delete 行；每批最多 batch_size 行，独立连接时每批 commit，
        批间可 sleep 以降低锁竞争。
        """
        if max_rows < 1 or max_delete < 1:
            return 0
        if table not in self.TABLE_SCHEMAS:
            raise ValueError(f"prune 仅支持预定义表: {list(self.TABLE_SCHEMAS.keys())}")
        if not self._validate_sql_identifier(id_column):
            raise ValueError(f"非法 id 列名: {id_column}")
        if batch_size < 1:
            batch_size = 5000

        cutoff_sql = (
            f"SELECT MIN({id_column}) FROM ("
            f"SELECT {id_column} FROM {table} ORDER BY {id_column} DESC LIMIT ?)"
        )
        delete_sql = (
            f"DELETE FROM {table} WHERE {id_column} IN ("
            f"SELECT {id_column} FROM {table} WHERE {id_column} < ? "
            f"ORDER BY {id_column} ASC LIMIT ?)"
        )

        def _run(conn: sqlite3.Connection, commit_each_batch: bool) -> int:
            total = 0
            cursor = conn.cursor()
            try:
                cursor.execute(cutoff_sql, (max_rows,))
                row = cursor.fetchone()
                cutoff = row[0] if row else None
                if cutoff is None:
                    return 0

                while total < max_delete:
                    chunk = min(batch_size, max_delete - total)
                    if chunk < 1:
                        break
                    cursor.execute(delete_sql, (cutoff, chunk))
                    n = cursor.rowcount or 0
                    total += n
                    if commit_each_batch:
                        conn.commit()
                    if n == 0:
                        break
                    if total >= max_delete:
                        break
                    if (
                        commit_each_batch
                        and sleep_between_batches_sec > 0
                        and total < max_delete
                    ):
                        time.sleep(sleep_between_batches_sec)
            finally:
                cursor.close()
            return total

        if connection is not None:
            return _run(connection, commit_each_batch=False)

        conn = self._get_connection()
        try:
            total = _run(conn, commit_each_batch=True)
            if total:
                logger.info(
                    f"表 {table} 限流裁剪删除 {total} 行（目标保留 {max_rows} 条，本会话上限 {max_delete}，按 {id_column}）"
                )
            return total
        except Exception as e:
            conn.rollback()
            logger.error(f"表 {table} 限流裁剪失败: {e}")
            raise
        finally:
            self._close_connection(conn)


_sqlite_schema_lock = threading.Lock()
_sqlite_schema_initialized: set = set()


def init_sqlite_schema(
    database: Optional[str] = None,
    auto_create: Optional[bool] = None,
) -> None:
    """
    在进程启动阶段调用一次（例如 Api 初始化前）：按 AUTO_CREATE_TABLES 创建表。
    同一进程内对同一数据库路径重复调用为 no-op。
    """
    if auto_create is None:
        auto_create = os.getenv('AUTO_CREATE_TABLES', 'true').lower() == 'true'
    if not auto_create:
        return

    cfg = SQLiteStore._load_config_from_env()
    db_path = database or cfg['database']
    abs_path = os.path.abspath(db_path)

    with _sqlite_schema_lock:
        if abs_path in _sqlite_schema_initialized:
            return
        try:
            store = SQLiteStore(database=db_path)
            store.create_all_tables()
            _sqlite_schema_initialized.add(abs_path)
        except Exception as e:
            logger.warning(f"启动时创建表失败: {e}，可稍后手动调用 SQLiteStore.create_all_tables()")
