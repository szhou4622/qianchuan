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


_sqlite_upsert_lock = threading.RLock()


class SQLiteStore:
    """SQLite 数据库操作类"""

    # 表结构定义
    TABLE_SCHEMAS = {
        'pmc_promotion_material': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'aadvid': 'TEXT NOT NULL',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'ad_id': "TEXT NOT NULL DEFAULT ''",
                'promotion_scene': "TEXT NOT NULL DEFAULT 'live'",
                'plan_system': "TEXT NOT NULL DEFAULT 'unknown'",
                'material_id': 'TEXT NOT NULL',
                'product_ids_json': 'TEXT',
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
                'data_source': "TEXT NOT NULL DEFAULT 'browser_legacy'",
                'api_request_id': 'TEXT',
                'stat_date': "TEXT NOT NULL DEFAULT (date('now', '+8 hours'))",
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_material_aadvid', 'aadvid'),
                ('idx_material_target_created', 'target_uid, created_at'),
                ('idx_material_target_material', 'target_uid, material_id'),
                ('idx_material_stat_date', 'stat_date'),
                ('idx_material_id', 'material_id'),
                ('idx_material_video_type', 'video_type'),
                ('idx_material_status', 'material_status'),
                ('idx_material_created_at', 'created_at'),
                ('idx_material_perf_lead', 'created_at, material_id'),
            ]
        },
        # 每个计划-素材只保留最新一行。官方 API 的高频历史指标写入下方
        # 精简快照表，不再把素材名称、封面等固定字段每五分钟重复写入
        # pmc_promotion_material。
        'pmc_promotion_material_latest': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'aadvid': 'TEXT NOT NULL',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'ad_id': "TEXT NOT NULL DEFAULT ''",
                'promotion_scene': "TEXT NOT NULL DEFAULT 'live'",
                'plan_system': "TEXT NOT NULL DEFAULT 'unknown'",
                'material_id': 'TEXT NOT NULL',
                'product_ids_json': 'TEXT',
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
                'data_source': "TEXT NOT NULL DEFAULT 'browser_legacy'",
                'api_request_id': 'TEXT',
                'stat_date': "TEXT NOT NULL DEFAULT (date('now', '+8 hours'))",
                'collected_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'delivery_state': "TEXT NOT NULL DEFAULT 'delivering'",
                'last_seen_at': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_material_latest_target_time', 'target_uid, collected_at'),
                ('idx_material_latest_account_time', 'aadvid, collected_at'),
                ('idx_material_latest_status', 'target_uid, material_status, show_status'),
                ('idx_material_latest_stat_date', 'stat_date, target_uid'),
            ],
            'unique_indexes': [
                ('uk_material_latest_target_material', 'target_uid, material_id'),
            ],
        },
        # 五分钟核心指标快照。固定素材资料只在 latest 表保存一份，避免
        # 大量重复文本和九组历史索引把数据库推到 GB 级。
        'pmc_material_metric_snapshot': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'account_username': "TEXT NOT NULL DEFAULT 'local_default'",
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'aadvid': 'TEXT NOT NULL',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'ad_id': "TEXT NOT NULL DEFAULT ''",
                'material_id': 'TEXT NOT NULL',
                'bucket_key': 'TEXT NOT NULL',
                'collected_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'stat_date': "TEXT NOT NULL DEFAULT (date('now', '+8 hours'))",
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
                'api_request_id': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_metric_snapshot_owner_time', 'account_username, collected_at'),
                ('idx_metric_snapshot_account_time', 'account_username, aadvid, collected_at'),
                ('idx_metric_snapshot_target_time', 'target_uid, collected_at'),
                ('idx_metric_snapshot_material_time', 'target_uid, material_id, collected_at'),
            ],
            'unique_indexes': [
                ('uk_metric_snapshot_bucket', 'account_username, target_uid, material_id, bucket_key'),
            ],
        },
        # 超过48小时的五分钟快照按小时保留最后一个可信点，供长期曲线和
        # 对账使用；大屏最近一小时仍读取精确的五分钟快照。
        'pmc_material_metric_hourly': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'account_username': "TEXT NOT NULL DEFAULT 'local_default'",
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'aadvid': 'TEXT NOT NULL',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'ad_id': "TEXT NOT NULL DEFAULT ''",
                'material_id': 'TEXT NOT NULL',
                'hour_key': 'TEXT NOT NULL',
                'collected_at': 'TEXT NOT NULL',
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
                'api_request_id': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_metric_hourly_owner_time', 'account_username, hour_key'),
                ('idx_metric_hourly_target_time', 'target_uid, hour_key'),
                ('idx_metric_hourly_material_time', 'target_uid, material_id, hour_key'),
            ],
            'unique_indexes': [
                ('uk_metric_hourly_point', 'account_username, target_uid, material_id, hour_key'),
            ],
        },
        'dashboard_storage_state': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'state_key': 'TEXT NOT NULL',
                'state_value': "TEXT NOT NULL DEFAULT ''",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'unique_indexes': [
                ('uk_dashboard_storage_state_key', 'state_key'),
            ],
        },
        'pmc_ad_detail_basic': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'aadvid': 'TEXT NOT NULL',
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'ad_id': 'TEXT NOT NULL',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'plan_name': 'TEXT',
                'promotion_scene': "TEXT NOT NULL DEFAULT 'live'",
                'plan_system': "TEXT NOT NULL DEFAULT 'unknown'",
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
                ('idx_ad_detail_aadvid', 'aadvid'),
                ('idx_ad_detail_ad_id', 'ad_id'),
            ],
            'obsolete_indexes': [
                'uk_pmc_ad_detail_basic_aadvid',
                'uk_pmc_ad_detail_basic_aadvid_adid',
            ],
            # 业务唯一：同一广告主下允许多条计划，以账户 + 计划隔离。
            'unique_indexes': [
                (
                    'uk_pmc_ad_detail_basic_account_aadvid_adid',
                    'account_uid, aadvid, ad_id',
                ),
            ],
        },
        # 一个工具账号下只保存一份千川登录会话；该会话可访问多个 aavid。
        # 账户目录负责启停、日报选择和飞书接收路由。
        'qianchuan_account': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'account_uid': 'TEXT NOT NULL',
                'owner_username': "TEXT NOT NULL DEFAULT 'local_default'",
                'aavid': 'TEXT NOT NULL',
                'account_name': 'TEXT',
                # 只有用户明确选择/添加的账户才进入账户管理和自动化。
                # NULL 仅用于从旧版本一次性迁移，0 为已移除，1 为已选择。
                'directory_selected': 'INTEGER CHECK (directory_selected IN (0, 1))',
                'enabled': 'INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))',
                'report_enabled': 'INTEGER NOT NULL DEFAULT 0 CHECK (report_enabled IN (0, 1))',
                'route_mode': "TEXT NOT NULL DEFAULT 'default' CHECK (route_mode IN ('default', 'custom'))",
                'route_send_personal': 'INTEGER NOT NULL DEFAULT 1 CHECK (route_send_personal IN (0, 1))',
                'route_group_ids_json': "TEXT NOT NULL DEFAULT '[]'",
                'catalog_status': "TEXT NOT NULL DEFAULT 'not_synced'",
                'catalog_last_sync_at': 'TEXT',
                'catalog_error': 'TEXT',
                'catalog_counts_json': "TEXT NOT NULL DEFAULT '{}'",
                'last_seen_at': 'TEXT',
                'last_status': "TEXT NOT NULL DEFAULT 'pending'",
                'last_error': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_qianchuan_account_owner_enabled', 'owner_username, enabled'),
                ('idx_qianchuan_account_aavid', 'aavid'),
                ('idx_qianchuan_account_report', 'owner_username, report_enabled'),
            ],
            'unique_indexes': [
                ('uk_qianchuan_account_uid', 'account_uid'),
                ('uk_qianchuan_account_owner_aavid', 'owner_username, aavid'),
            ],
        },
        # 账户内的直播/商品全域监控目标。target_uid 稳定派生自 aavid + ad_id。
        'promotion_target': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'target_uid': 'TEXT NOT NULL',
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'aadvid': 'TEXT NOT NULL',
                'ad_id': 'TEXT NOT NULL',
                'plan_name': 'TEXT',
                'promotion_scene': "TEXT NOT NULL CHECK (promotion_scene IN ('live', 'product'))",
                'plan_system': "TEXT NOT NULL DEFAULT 'unknown' CHECK (plan_system IN ('global', 'chengfang', 'unknown'))",
                'platform_status': "TEXT NOT NULL DEFAULT 'unknown'",
                'verification_state': "TEXT NOT NULL DEFAULT 'legacy_unverified'",
                'catalog_seen_at': 'TEXT',
                'last_verified_at': 'TEXT',
                'last_verification_error': 'TEXT',
                'monitor_eligible': 'INTEGER NOT NULL DEFAULT 0 CHECK (monitor_eligible IN (0, 1))',
                'retarget_eligible': 'INTEGER NOT NULL DEFAULT 0 CHECK (retarget_eligible IN (0, 1))',
                'stop_eligible': 'INTEGER NOT NULL DEFAULT 0 CHECK (stop_eligible IN (0, 1))',
                'ineligible_reason': 'TEXT',
                'enabled': 'INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))',
                'product_filter_mode': "TEXT NOT NULL DEFAULT 'all' CHECK (product_filter_mode IN ('all', 'selected'))",
                'product_ids_json': 'TEXT',
                'sanitized_page_url': 'TEXT',
                'capability_json': 'TEXT',
                'last_sync_at': 'TEXT',
                'last_status': "TEXT NOT NULL DEFAULT 'pending'",
                'last_error': 'TEXT',
                'automation_write_blocked': 'INTEGER NOT NULL DEFAULT 0 CHECK (automation_write_blocked IN (0, 1))',
                'write_block_reason': 'TEXT',
                'write_block_origin': "TEXT NOT NULL DEFAULT ''",
                'write_blocked_at': 'TEXT',
                'capacity_state': "TEXT NOT NULL DEFAULT 'active' CHECK (capacity_state IN ('active', 'capacity_waiting', 'disabled'))",
                'last_duration_ms': 'INTEGER',
                'next_due_at': 'TEXT',
                'last_lag_seconds': 'INTEGER NOT NULL DEFAULT 0',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_promotion_target_account', 'aadvid'),
                ('idx_promotion_target_account_uid', 'account_uid'),
                ('idx_promotion_target_enabled', 'enabled'),
                ('idx_promotion_target_capacity', 'enabled, capacity_state'),
                ('idx_promotion_target_scene', 'promotion_scene'),
                ('idx_promotion_target_system', 'plan_system'),
                ('idx_promotion_target_catalog_state', 'account_uid, verification_state, platform_status'),
                ('idx_promotion_target_monitor_eligible', 'account_uid, monitor_eligible, enabled'),
            ],
            'obsolete_indexes': [
                'uk_promotion_target_account_plan',
            ],
            'unique_indexes': [
                ('uk_promotion_target_uid', 'target_uid'),
                (
                    'uk_promotion_target_owner_account_plan',
                    'account_uid, aadvid, ad_id',
                ),
            ],
        },
        # 商品全域计划中的商品快照。
        'promotion_product': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'target_uid': 'TEXT NOT NULL',
                'product_id': 'TEXT NOT NULL',
                'product_name': 'TEXT',
                'product_status': 'TEXT',
                'image_url': 'TEXT',
                'raw_json': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_promotion_product_target', 'target_uid'),
                ('idx_promotion_product_name', 'product_name'),
            ],
            'unique_indexes': [
                ('uk_promotion_product_target_product', 'target_uid, product_id'),
            ],
        },
        # 商品与素材为多对多关系；同一素材可关联多个商品。
        'promotion_material_product': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'target_uid': 'TEXT NOT NULL',
                'material_id': 'TEXT NOT NULL',
                'product_id': 'TEXT NOT NULL',
                'material_name': 'TEXT',
                'product_name': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_material_product_target_material', 'target_uid, material_id'),
                ('idx_material_product_target_product', 'target_uid, product_id'),
            ],
            'unique_indexes': [
                (
                    'uk_material_product_target_material_product',
                    'target_uid, material_id, product_id',
                ),
            ],
        },
        # 追投执行流水（与 docs/schema_pmc_retargeting_run.sqlite.sql 一致；以本处为自动建表来源）
        'pmc_retargeting_run': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'aavid': 'TEXT NOT NULL',
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'ad_id': 'TEXT NOT NULL',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'promotion_scene': "TEXT NOT NULL DEFAULT 'live'",
                'plan_system': "TEXT NOT NULL DEFAULT 'unknown'",
                'product_id': 'TEXT',
                'product_name': 'TEXT',
                'trigger_level': "TEXT NOT NULL DEFAULT 'material'",
                'material_id': 'TEXT NOT NULL',
                'material_name': 'TEXT',
                'materials_json': 'TEXT',
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
                # Stable local execution identity used by the persistent
                # reconciliation worker.  The legacy numeric ``status``
                # column keeps its historical -1/1 constraint; non-final
                # state lives here so old databases can migrate additively.
                'execution_uid': 'TEXT',
                'execution_state': "TEXT NOT NULL DEFAULT 'final'",
                'app_version': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_pmc_retargeting_run_started', 'started_at'),
                ('idx_pmc_retargeting_run_target_started', 'target_uid, started_at'),
                ('idx_pmc_retargeting_run_account_material', 'aavid, material_id, started_at'),
                ('idx_pmc_retargeting_run_status_time', 'status, started_at'),
                ('idx_pmc_retargeting_run_execution', 'execution_uid, execution_state'),
            ],
        },
        # 规则追投限频（每素材一行；窗口与次数由 rule retargeting.interval 解释，不存库）
        'pmc_retargeting_rate_limit': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'material_id': 'TEXT NOT NULL',
                'limit_started_at': 'TEXT NOT NULL',
                'use_count': 'INTEGER NOT NULL DEFAULT 0',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_pmc_retargeting_rate_limit_material_id', 'material_id'),
                ('idx_pmc_retargeting_rate_limit_target', 'target_uid'),
            ],
            'obsolete_indexes': [
                'uk_pmc_retargeting_rate_limit_material_id',
            ],
            'unique_indexes': [
                (
                    'uk_pmc_retargeting_rate_limit_target_material',
                    'target_uid, material_id',
                ),
            ],
        },
        # 规则追投限频（按策略）：per_strategy_rate_limit 启用时，每 (material_id, strategy_id) 一行
        'pmc_retargeting_rate_limit_strategy': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
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
                ('idx_pmc_rr_rl_strat_target', 'target_uid'),
            ],
            'obsolete_indexes': [
                'uk_pmc_rr_rl_strat_mat_sid',
            ],
            'unique_indexes': [
                (
                    'uk_pmc_rr_rl_strat_target_mat_sid',
                    'target_uid, material_id, strategy_id',
                ),
            ],
        },
        # 规则化停投流水（与 docs/schema_pmc_regulation_run.sqlite.sql 一致）
        'pmc_regulation_run': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'aavid': 'TEXT NOT NULL',
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'ad_id': 'TEXT NOT NULL',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'promotion_scene': "TEXT NOT NULL DEFAULT 'live'",
                'plan_system': "TEXT NOT NULL DEFAULT 'unknown'",
                'product_id': 'TEXT',
                'product_name': 'TEXT',
                'assist_task_id': 'TEXT',
                'task_name': 'TEXT',
                'strategy_name': 'TEXT',
                'stop_action': 'TEXT',
                'started_at': 'TEXT NOT NULL',
                'ended_at': 'TEXT NOT NULL',
                'duration_ms': 'INTEGER',
                'status': 'INTEGER NOT NULL CHECK (status IN (-1, 1, 2))',
                'execution_uid': "TEXT NOT NULL DEFAULT ''",
                'execution_state': "TEXT NOT NULL DEFAULT 'completed'",
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
                ('idx_pmc_regulation_run_target_started', 'target_uid, started_at'),
                ('idx_pmc_regulation_run_aavid_time', 'aavid, started_at'),
                ('idx_pmc_regulation_run_status_time', 'status, started_at'),
                ('idx_pmc_regulation_run_execution', 'execution_uid, execution_state'),
            ],
        },
        # 单账户统一操作流水：工具直执、记录浏览器、平台操作日志统一写入。
        'account_operation_event': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'event_uid': 'TEXT NOT NULL',
                'aavid': 'TEXT NOT NULL',
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'ad_id': 'TEXT',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'promotion_scene': 'TEXT',
                'plan_system': "TEXT NOT NULL DEFAULT 'unknown'",
                'source': 'TEXT NOT NULL',
                'action_type': 'TEXT NOT NULL',
                'object_type': 'TEXT',
                'object_id': 'TEXT',
                'object_name': 'TEXT',
                'plan_id': 'TEXT',
                'plan_name': 'TEXT',
                'material_id': 'TEXT',
                'material_name': 'TEXT',
                'product_id': 'TEXT',
                'product_name': 'TEXT',
                'regulate_task_id': 'TEXT',
                'regulate_task_name': 'TEXT',
                'operator_id': 'TEXT',
                'operator_name': 'TEXT',
                'status': 'TEXT NOT NULL',
                'summary': 'TEXT',
                'detail': 'TEXT',
                'before_json': 'TEXT',
                'after_json': 'TEXT',
                'trigger_json': 'TEXT',
                'request_json': 'TEXT',
                'response_json': 'TEXT',
                'raw_json': 'TEXT',
                'cloud_task_id': 'TEXT',
                'platform_event_id': 'TEXT',
                'related_event_uid': 'TEXT',
                'possible_duplicate': 'INTEGER NOT NULL DEFAULT 0 CHECK (possible_duplicate IN (0, 1))',
                'occurred_at': 'TEXT NOT NULL',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_account_operation_aavid_time', 'aavid, occurred_at'),
                ('idx_account_operation_account_uid_time', 'account_uid, occurred_at'),
                ('idx_account_operation_target_time', 'target_uid, occurred_at'),
                ('idx_account_operation_action_time', 'action_type, occurred_at'),
                ('idx_account_operation_source_time', 'source, occurred_at'),
                ('idx_account_operation_status_time', 'status, occurred_at'),
                ('idx_account_operation_cloud_task', 'cloud_task_id'),
                ('idx_account_operation_plan', 'aavid, plan_id'),
                ('idx_account_operation_material', 'aavid, material_id'),
                ('idx_account_operation_regulate_task', 'aavid, regulate_task_id'),
            ],
            'unique_indexes': [
                ('uk_account_operation_event_uid', 'event_uid'),
            ],
        },
        # 云端追投任务在本机的幂等结果缓存，防止领取租约恢复后重复追投。
        'cloud_retarget_task_local': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'cloud_task_id': 'TEXT NOT NULL',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'promotion_scene': 'TEXT',
                'plan_system': "TEXT NOT NULL DEFAULT 'unknown'",
                'status': 'TEXT NOT NULL',
                'result_json': 'TEXT',
                'claimed_at': 'TEXT',
                'finished_at': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_cloud_retarget_local_status', 'status'),
            ],
            'unique_indexes': [
                ('uk_cloud_retarget_local_task', 'cloud_task_id'),
            ],
        },
        # 本地飞书长连接模式的追投确认任务。active_dedupe_key 在终态后置空。
        'local_retarget_task': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'task_uid': 'TEXT NOT NULL',
                'account_username': 'TEXT NOT NULL',
                'qianchuan_account_uid': "TEXT NOT NULL DEFAULT ''",
                'action_type': "TEXT NOT NULL DEFAULT 'retarget' CHECK (action_type IN ('retarget', 'stop'))",
                'control_cycle_key': "TEXT NOT NULL DEFAULT ''",
                'active_dedupe_key': 'TEXT',
                'status': 'TEXT NOT NULL',
                'action_nonce': 'TEXT NOT NULL',
                'payload_json': 'TEXT NOT NULL',
                'card_messages_json': "TEXT NOT NULL DEFAULT '[]'",
                'approved_by': 'TEXT',
                'claim_token': 'TEXT',
                'claim_expires_at': 'TEXT',
                'fencing_token': 'INTEGER NOT NULL DEFAULT 0',
                'result_message': 'TEXT',
                'result_detail': 'TEXT',
                'regulate_task_id': 'TEXT',
                'result_json': 'TEXT',
                'expires_at': 'TEXT NOT NULL',
                'approved_at': 'TEXT',
                'claimed_at': 'TEXT',
                'finished_at': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_local_retarget_account_status', 'account_username, status'),
                ('idx_local_retarget_action_status', 'account_username, action_type, status'),
                ('idx_local_stop_cycle', 'account_username, action_type, control_cycle_key, status'),
                ('idx_local_retarget_expires', 'expires_at'),
            ],
            'unique_indexes': [
                ('uk_local_retarget_task_uid', 'task_uid'),
                ('uk_local_retarget_active_dedupe', 'active_dedupe_key'),
            ],
        },
        # 统一、持久化的采集任务队列。相同 owner/target/kind 只保留一条
        # 活动任务；进程崩溃后由租约超时恢复。
        'collection_job': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'job_uid': 'TEXT NOT NULL',
                'owner_username': "TEXT NOT NULL DEFAULT 'local_default'",
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'aavid': "TEXT NOT NULL DEFAULT ''",
                'target_uid': "TEXT NOT NULL DEFAULT ''",
                'job_kind': "TEXT NOT NULL DEFAULT 'hot_collection'",
                'priority': 'INTEGER NOT NULL DEFAULT 20',
                'due_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'status': "TEXT NOT NULL DEFAULT 'queued'",
                'attempt_count': 'INTEGER NOT NULL DEFAULT 0',
                'lease_owner': 'TEXT',
                'lease_expires_at': 'TEXT',
                'fencing_token': 'INTEGER NOT NULL DEFAULT 0',
                'last_error': 'TEXT',
                'last_started_at': 'TEXT',
                'last_finished_at': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_collection_job_due', 'owner_username, status, due_at, priority'),
                ('idx_collection_job_account', 'owner_username, aavid, status'),
                ('idx_collection_job_lease', 'status, lease_expires_at'),
            ],
            'unique_indexes': [
                ('uk_collection_job_uid', 'job_uid'),
                ('uk_collection_job_target_kind', 'owner_username, target_uid, job_kind'),
            ],
        },
        # 官方 API 应用级及账户级配额/退避状态。重启和休眠恢复后继续
        # 遵守 Retry-After，避免所有账户同时抢跑。
        'api_quota_state': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'scope_key': 'TEXT NOT NULL',
                'owner_username': "TEXT NOT NULL DEFAULT 'local_default'",
                'scope_type': "TEXT NOT NULL DEFAULT 'account'",
                'scope_id': "TEXT NOT NULL DEFAULT ''",
                'backoff_until': 'TEXT',
                'rate_limit_count': 'INTEGER NOT NULL DEFAULT 0',
                'last_error': 'TEXT',
                'last_request_at': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_api_quota_owner_type', 'owner_username, scope_type, backoff_until'),
            ],
            'unique_indexes': [
                ('uk_api_quota_scope', 'scope_key'),
            ],
        },
        # POST 提交后的最终一致性核验。提交成功不等于平台已生效；核验
        # 结果会在重启后继续推进，且 fencing_token 防止旧线程回写。
        'execution_reconciliation': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'reconciliation_uid': 'TEXT NOT NULL',
                'account_username': "TEXT NOT NULL DEFAULT ''",
                'task_uid': "TEXT NOT NULL DEFAULT ''",
                'action_type': "TEXT NOT NULL DEFAULT 'retarget'",
                'aavid': "TEXT NOT NULL DEFAULT ''",
                'ad_id': "TEXT NOT NULL DEFAULT ''",
                'control_task_id': "TEXT NOT NULL DEFAULT ''",
                'request_id': "TEXT NOT NULL DEFAULT ''",
                'idempotency_key': "TEXT NOT NULL DEFAULT ''",
                'status': "TEXT NOT NULL DEFAULT 'submitted'",
                'attempt_count': 'INTEGER NOT NULL DEFAULT 0',
                'next_attempt_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'lease_owner': 'TEXT',
                'lease_expires_at': 'TEXT',
                'fencing_token': 'INTEGER NOT NULL DEFAULT 0',
                'last_error': 'TEXT',
                'payload_json': "TEXT NOT NULL DEFAULT '{}'",
                'card_update_state': "TEXT NOT NULL DEFAULT 'pending'",
                'confirmed_at': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_execution_reconcile_due', 'status, next_attempt_at'),
                ('idx_execution_reconcile_owner_task', 'account_username, task_uid'),
                ('idx_execution_reconcile_platform_task', 'aavid, control_task_id'),
            ],
            'unique_indexes': [
                ('uk_execution_reconcile_uid', 'reconciliation_uid'),
                ('uk_execution_reconcile_idempotency', 'account_username, idempotency_key'),
            ],
        },
        # 飞书事件先落 Inbox 再处理；Outbox 保存需要发送/更新的消息，
        # 失败后退避重试，避免进程退出造成卡片永久停留在旧状态。
        'feishu_inbox': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'account_username': "TEXT NOT NULL DEFAULT ''",
                'event_id': 'TEXT NOT NULL',
                'event_type': "TEXT NOT NULL DEFAULT ''",
                'payload_json': "TEXT NOT NULL DEFAULT '{}'",
                'payload_hash': "TEXT NOT NULL DEFAULT ''",
                'status': "TEXT NOT NULL DEFAULT 'received'",
                'attempt_count': 'INTEGER NOT NULL DEFAULT 0',
                'last_error': 'TEXT',
                'processed_at': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_feishu_inbox_status', 'account_username, status, created_at'),
            ],
            'unique_indexes': [
                ('uk_feishu_inbox_event', 'account_username, event_id'),
            ],
        },
        'feishu_outbox': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'outbox_uid': 'TEXT NOT NULL',
                'account_username': "TEXT NOT NULL DEFAULT ''",
                'operation': "TEXT NOT NULL DEFAULT 'send'",
                'receive_type': "TEXT NOT NULL DEFAULT ''",
                'receive_id': "TEXT NOT NULL DEFAULT ''",
                'message_id': "TEXT NOT NULL DEFAULT ''",
                'task_uid': "TEXT NOT NULL DEFAULT ''",
                'payload_json': "TEXT NOT NULL DEFAULT '{}'",
                'status': "TEXT NOT NULL DEFAULT 'queued'",
                'attempt_count': 'INTEGER NOT NULL DEFAULT 0',
                'next_attempt_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'lease_owner': 'TEXT',
                'lease_expires_at': 'TEXT',
                'fencing_token': 'INTEGER NOT NULL DEFAULT 0',
                'last_error': 'TEXT',
                'sent_at': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_feishu_outbox_due', 'account_username, status, next_attempt_at'),
                ('idx_feishu_outbox_lease', 'status, lease_expires_at'),
                ('idx_feishu_outbox_task', 'account_username, task_uid, operation, message_id, id'),
            ],
            'unique_indexes': [
                ('uk_feishu_outbox_uid', 'outbox_uid'),
            ],
        },
        # 平台操作日志同步状态；每个账户保存覆盖范围、游标和最近错误。
        'platform_log_sync_state': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'aavid': 'TEXT NOT NULL',
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'coverage_from': 'TEXT',
                'coverage_to': 'TEXT',
                'last_sync_at': 'TEXT',
                'last_status': 'TEXT NOT NULL DEFAULT \'not_configured\'',
                'last_error': 'TEXT',
                'active_batch_uid': "TEXT NOT NULL DEFAULT ''",
                'requested_from': 'TEXT',
                'requested_to': 'TEXT',
                'progress_completed': 'INTEGER NOT NULL DEFAULT 0',
                'progress_total': 'INTEGER NOT NULL DEFAULT 0',
                'progress_rows_seen': 'INTEGER NOT NULL DEFAULT 0',
                'progress_rows_inserted': 'INTEGER NOT NULL DEFAULT 0',
                'current_object': 'TEXT',
                'history_complete': 'INTEGER NOT NULL DEFAULT 0',
                'next_retry_at': 'TEXT',
                'last_progress_at': 'TEXT',
                'discovered_page_url': 'TEXT',
                'discovered_api_url': 'TEXT',
                'discovered_request_json': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'obsolete_indexes': [
                'uk_platform_log_sync_aavid',
            ],
            'unique_indexes': [
                ('uk_platform_log_sync_account_aavid', 'account_uid, aavid'),
            ],
        },
        # 官方 API 操作日志按24小时窗口和账户/计划对象持久化。
        # 任务可在软件重启后续跑，同一对象窗口始终幂等。
        'operation_log_sync_window': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'window_uid': 'TEXT NOT NULL',
                'batch_uid': "TEXT NOT NULL DEFAULT ''",
                'owner_username': "TEXT NOT NULL DEFAULT ''",
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'aavid': 'TEXT NOT NULL',
                'object_type': "TEXT NOT NULL DEFAULT 'ACCOUNT'",
                'object_id': "TEXT NOT NULL DEFAULT ''",
                'target_uid': "TEXT NOT NULL DEFAULT ''",
                'window_start': 'TEXT NOT NULL',
                'window_end': 'TEXT NOT NULL',
                'request_kind': "TEXT NOT NULL DEFAULT 'history'",
                'priority': 'INTEGER NOT NULL DEFAULT 20',
                'status': "TEXT NOT NULL DEFAULT 'queued'",
                'attempt_count': 'INTEGER NOT NULL DEFAULT 0',
                'next_attempt_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'lease_owner': 'TEXT',
                'lease_expires_at': 'TEXT',
                'fencing_token': 'INTEGER NOT NULL DEFAULT 0',
                'rows_seen': 'INTEGER NOT NULL DEFAULT 0',
                'rows_inserted': 'INTEGER NOT NULL DEFAULT 0',
                'request_ids_json': "TEXT NOT NULL DEFAULT '[]'",
                'last_error': 'TEXT',
                'completed_at': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_operation_log_window_due', 'status, priority, next_attempt_at'),
                ('idx_operation_log_window_account', 'account_uid, aavid, status, window_start'),
                ('idx_operation_log_window_batch', 'batch_uid, status, window_start'),
                ('idx_operation_log_window_lease', 'status, lease_expires_at'),
            ],
            'unique_indexes': [
                (
                    'uk_operation_log_window_scope',
                    'owner_username, account_uid, aavid, object_type, object_id, window_start, window_end',
                ),
                ('uk_operation_log_window_uid', 'window_uid'),
            ],
        },
        # 前一日账户操作日报的发送记录；按工具账号、千川账户、日期和飞书接收位置幂等。
        # 千川官方 API 请求审计。只保存脱敏摘要、官方 request_id 与对账状态。
        'qianchuan_api_audit': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'request_uid': 'TEXT NOT NULL',
                'account_username': "TEXT NOT NULL DEFAULT ''",
                'source': "TEXT NOT NULL DEFAULT 'qianchuan_open_api'",
                'endpoint': 'TEXT NOT NULL',
                'method': 'TEXT NOT NULL',
                'aavid': "TEXT NOT NULL DEFAULT ''",
                'ad_id': "TEXT NOT NULL DEFAULT ''",
                'task_id': "TEXT NOT NULL DEFAULT ''",
                'request_id': "TEXT NOT NULL DEFAULT ''",
                'error_code': "TEXT NOT NULL DEFAULT ''",
                'permission_status': "TEXT NOT NULL DEFAULT 'unknown'",
                'reconciliation_status': "TEXT NOT NULL DEFAULT 'not_required'",
                'status': "TEXT NOT NULL DEFAULT 'requested'",
                'request_summary_json': "TEXT NOT NULL DEFAULT '{}'",
                'response_summary_json': "TEXT NOT NULL DEFAULT '{}'",
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_qianchuan_api_audit_account_time', 'account_username, aavid, created_at'),
                ('idx_qianchuan_api_audit_status', 'status, reconciliation_status'),
                ('idx_qianchuan_api_audit_request_id', 'request_id'),
            ],
            'unique_indexes': [
                ('uk_qianchuan_api_audit_request_uid', 'request_uid'),
            ],
        },
        'operation_daily_report_delivery': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'delivery_key': 'TEXT',
                'report_uid': 'TEXT NOT NULL',
                'account_username': 'TEXT NOT NULL',
                'aavid': 'TEXT NOT NULL',
                'qianchuan_account_uid': "TEXT NOT NULL DEFAULT ''",
                'report_date': 'TEXT NOT NULL',
                'delivery_mode': "TEXT NOT NULL DEFAULT 'scheduled'",
                'receive_type': 'TEXT NOT NULL',
                'receive_id': 'TEXT NOT NULL',
                'message_id': 'TEXT',
                'status': "TEXT NOT NULL DEFAULT 'pending'",
                'event_count': 'INTEGER NOT NULL DEFAULT 0',
                'last_error': 'TEXT',
                'sent_at': 'TEXT',
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_operation_daily_account_date', 'account_username, report_date'),
                ('idx_operation_daily_aavid_date', 'aavid, report_date'),
                ('idx_operation_daily_status', 'status, updated_at'),
            ],
            'unique_indexes': [
                ('uk_operation_daily_delivery_key', 'delivery_key'),
            ],
        },
        # 全域调控任务（素材追投等，与 docs/schema_pmc_roi2_assist_task.sqlite.sql 一致）
        'pmc_roi2_assist_task': {
            'columns': {
                'id': 'INTEGER PRIMARY KEY AUTOINCREMENT',
                'assist_task_id': 'TEXT NOT NULL',
                'aadvid': 'TEXT NOT NULL',
                'account_uid': "TEXT NOT NULL DEFAULT ''",
                'ad_id': 'TEXT NOT NULL',
                'target_uid': "TEXT NOT NULL DEFAULT 'legacy_unscoped'",
                'promotion_scene': "TEXT NOT NULL DEFAULT 'live'",
                'plan_system': "TEXT NOT NULL DEFAULT 'unknown'",
                'product_ids_json': 'TEXT',
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
                'task_status_source': "TEXT NOT NULL DEFAULT ''",
                'task_status_observed_at': 'TEXT',
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
                'total_order_settle_count_for_roi2_1h_assist': 'REAL',
                'total_refund_order_gmv_for_roi2_1h_rate_assist': 'REAL',
                'total_prepay_and_pay_settle_roi2_1h_assist': 'REAL',
                'total_pay_order_gmv_for_roi2_assist': 'REAL',
                'total_pay_order_coupon_amount_for_roi2_assist': 'REAL',
                'assist_materials_json': 'TEXT',
                'data_source': "TEXT NOT NULL DEFAULT 'browser_legacy'",
                'api_request_id': 'TEXT',
                'reconciliation_status': "TEXT NOT NULL DEFAULT 'not_required'",
                'created_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
                'updated_at': "TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))",
            },
            'indexes': [
                ('idx_pmc_roi2_assist_aadvid', 'aadvid'),
                ('idx_pmc_roi2_assist_target', 'target_uid'),
                ('idx_pmc_roi2_assist_created', 'created_at'),
            ],
            'obsolete_indexes': [
                'uk_pmc_roi2_assist_task_assist_id',
            ],
            'unique_indexes': [
                (
                    'uk_pmc_roi2_assist_target_task',
                    'target_uid, assist_task_id',
                ),
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
            from services.channel_ledger import managed_database, GuardConnection, attach
            shared_ledger = managed_database(self.config['database'])
            if shared_ledger:
                connect_kw['factory'] = GuardConnection
            connection = sqlite3.connect(**connect_kw)
            if shared_ledger:
                attach(connection, self.TABLE_SCHEMAS)
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
        target_uid: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        近 1 小时滚动窗口（与库表 created_at 语义一致）：
        created_at > datetime('now', '+8 hours', '-1 hours')
        每个 material_id 只保留 id 最大的一条（该周期内最新一条）。
        """
        tbl = "pmc_promotion_material"
        window_where = "created_at > datetime('now', '+8 hours', '-1 hours')"
        if target_uid:
            sql = f"""
            SELECT t.* FROM {tbl} t
            INNER JOIN (
              SELECT target_uid, material_id, MAX(id) AS max_id
              FROM {tbl}
              WHERE {window_where} AND target_uid = ?
              GROUP BY target_uid, material_id
            ) x ON t.id = x.max_id
            """
            return self.execute(sql, (str(target_uid),), fetch=True) or []
        if aadvid:
            sql = f"""
            SELECT t.* FROM {tbl} t
            INNER JOIN (
              SELECT target_uid, material_id, MAX(id) AS max_id
              FROM {tbl}
              WHERE {window_where} AND aadvid = ?
              GROUP BY target_uid, material_id
            ) x ON t.id = x.max_id
            """
            return self.execute(sql, (str(aadvid),), fetch=True) or []
        sql = f"""
            SELECT t.* FROM {tbl} t
            INNER JOIN (
              SELECT target_uid, material_id, MAX(id) AS max_id
              FROM {tbl}
              WHERE {window_where}
              GROUP BY target_uid, material_id
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

        # Build a stable union of all row keys. Using only the first row
        # silently discarded valid columns that appeared in later rows.
        columns = list(filtered_list[0].keys())
        seen_columns = set(columns)
        for item in filtered_list[1:]:
            for column in item.keys():
                if column not in seen_columns:
                    columns.append(column)
                    seen_columns.add(column)
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

        _sqlite_upsert_lock.acquire()
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
            _sqlite_upsert_lock.release()

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

                    # 业务唯一键升级时先移除旧索引；DROP INDEX 不删除任何数据。
                    if table_schema.get('obsolete_indexes'):
                        for old_idx in table_schema['obsolete_indexes']:
                            if not self._validate_sql_identifier(old_idx):
                                logger.warning(f"跳过非法废弃索引名: {old_idx}")
                                continue
                            cursor.execute(f"DROP INDEX IF EXISTS {old_idx}")

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
