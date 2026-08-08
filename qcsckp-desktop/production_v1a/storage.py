"""V1A SQLite schema、短事务和单写入队列。"""

from __future__ import annotations

import queue
import sqlite3
import threading
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence, TypeVar

from .constants import SCHEMA_VERSION
from .runtime_paths import RuntimePaths
from .timeutils import utc_iso

T = TypeVar("T")


RUNTIME_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_user (
    tool_user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    password_iterations INTEGER NOT NULL,
    recovery_code_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_auth_cache (
    tool_user_id TEXT PRIMARY KEY,
    remote_account_id TEXT NOT NULL UNIQUE,
    encrypted_access_token TEXT NOT NULL,
    token_expires_at TEXT NOT NULL,
    last_online_verified_at TEXT NOT NULL,
    offline_grace_until TEXT NOT NULL,
    auth_status TEXT NOT NULL DEFAULT 'active'
        CHECK(auth_status IN ('active', 'offline_grace', 'expired', 'disabled', 'device_mismatch', 'logged_out')),
    last_error_code TEXT,
    last_error_message TEXT,
    last_used_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(tool_user_id) REFERENCES tool_user(tool_user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS qianchuan_identity (
    login_identity_id TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL UNIQUE,
    encrypted_storage_state TEXT,
    cookie_updated_at TEXT,
    login_status TEXT NOT NULL DEFAULT 'not_configured',
    blocked_reason TEXT,
    last_verified_at TEXT,
    profile_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(tool_user_id) REFERENCES tool_user(tool_user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feishu_route (
    route_id TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    route_name TEXT NOT NULL,
    personal_open_id TEXT,
    group_chat_ids_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tool_user_id, route_name),
    FOREIGN KEY(tool_user_id) REFERENCES tool_user(tool_user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS advertiser_account (
    account_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    account_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    daily_report_enabled INTEGER NOT NULL DEFAULT 0 CHECK(daily_report_enabled IN (0, 1)),
    feishu_route_id TEXT,
    catalog_status TEXT NOT NULL DEFAULT 'not_synced',
    catalog_completed_at TEXT,
    removed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tool_user_id, aavid),
    FOREIGN KEY(tool_user_id) REFERENCES tool_user(tool_user_id) ON DELETE CASCADE,
    FOREIGN KEY(feishu_route_id) REFERENCES feishu_route(route_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS source_plan (
    target_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    plan_system TEXT NOT NULL CHECK(plan_system IN ('global', 'chengfang', 'unknown')),
    promotion_scene TEXT NOT NULL CHECK(promotion_scene IN ('product', 'live', 'unknown')),
    platform_status TEXT NOT NULL DEFAULT 'unknown',
    verification_state TEXT NOT NULL DEFAULT 'unverified',
    catalog_seen_at TEXT,
    monitor_enabled INTEGER NOT NULL DEFAULT 0 CHECK(monitor_enabled IN (0, 1)),
    monitor_eligible INTEGER NOT NULL DEFAULT 0 CHECK(monitor_eligible IN (0, 1)),
    retarget_eligible INTEGER NOT NULL DEFAULT 0 CHECK(retarget_eligible IN (0, 1)),
    pause_eligible INTEGER NOT NULL DEFAULT 0 CHECK(pause_eligible IN (0, 1)),
    adjust_eligible INTEGER NOT NULL DEFAULT 0 CHECK(adjust_eligible IN (0, 1)),
    ineligible_reason TEXT,
    adapter_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tool_user_id, aavid, ad_id),
    FOREIGN KEY(tool_user_id, aavid) REFERENCES advertiser_account(tool_user_id, aavid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS product_identity (
    product_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_name TEXT,
    platform_status TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(tool_user_id, aavid, ad_id, product_id),
    FOREIGN KEY(tool_user_id, aavid, ad_id) REFERENCES source_plan(tool_user_id, aavid, ad_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS material_identity (
    material_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    material_name TEXT,
    material_type TEXT NOT NULL CHECK(material_type IN ('video')),
    material_created_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(tool_user_id, aavid, ad_id, material_id),
    FOREIGN KEY(tool_user_id, aavid, ad_id) REFERENCES source_plan(tool_user_id, aavid, ad_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS product_material_relation (
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(tool_user_id, aavid, ad_id, product_id, material_id),
    FOREIGN KEY(tool_user_id, aavid, ad_id, product_id) REFERENCES product_identity(tool_user_id, aavid, ad_id, product_id) ON DELETE CASCADE,
    FOREIGN KEY(tool_user_id, aavid, ad_id, material_id) REFERENCES material_identity(tool_user_id, aavid, ad_id, material_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS collection_run (
    collection_run_id TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    target_uid TEXT,
    object_type TEXT NOT NULL,
    business_date TEXT NOT NULL,
    filters_hash TEXT NOT NULL,
    platform_total_count INTEGER,
    expected_pages INTEGER,
    successful_pages INTEGER NOT NULL DEFAULT 0,
    failed_pages_json TEXT NOT NULL DEFAULT '[]',
    raw_count INTEGER NOT NULL DEFAULT 0,
    unique_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    platform_server_time TEXT,
    clock_skew_seconds INTEGER,
    data_max_age_seconds INTEGER,
    adapter_version TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    FOREIGN KEY(tool_user_id, aavid) REFERENCES advertiser_account(tool_user_id, aavid) ON DELETE CASCADE,
    FOREIGN KEY(target_uid) REFERENCES source_plan(target_uid) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_collection_latest ON collection_run(tool_user_id, aavid, target_uid, object_type, started_at DESC);

CREATE TABLE IF NOT EXISTS material_status_latest (
    material_uid TEXT PRIMARY KEY,
    delivery_status TEXT,
    show_status TEXT,
    show_status_reason TEXT,
    audit_status TEXT,
    block_status TEXT,
    platform_raw_status_json TEXT NOT NULL DEFAULT '{}',
    is_in_delivery_list INTEGER NOT NULL DEFAULT 0 CHECK(is_in_delivery_list IN (0, 1)),
    is_effectively_deliverable INTEGER NOT NULL DEFAULT 0 CHECK(is_effectively_deliverable IN (0, 1)),
    observed_at TEXT NOT NULL,
    collection_run_id TEXT NOT NULL,
    FOREIGN KEY(material_uid) REFERENCES material_identity(material_uid) ON DELETE CASCADE,
    FOREIGN KEY(collection_run_id) REFERENCES collection_run(collection_run_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS latest_metrics (
    material_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    spend_cent INTEGER NOT NULL DEFAULT 0,
    order_count INTEGER NOT NULL DEFAULT 0,
    gmv_cent INTEGER NOT NULL DEFAULT 0,
    roi_decimal TEXT,
    platform_time TEXT,
    observed_at_utc TEXT NOT NULL,
    observed_at_beijing TEXT NOT NULL,
    collection_run_id TEXT NOT NULL,
    FOREIGN KEY(material_uid) REFERENCES material_identity(material_uid) ON DELETE CASCADE,
    FOREIGN KEY(collection_run_id) REFERENCES collection_run(collection_run_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS hourly_metrics (
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    business_hour TEXT NOT NULL,
    spend_cent INTEGER NOT NULL DEFAULT 0,
    order_count INTEGER NOT NULL DEFAULT 0,
    gmv_cent INTEGER NOT NULL DEFAULT 0,
    roi_decimal TEXT,
    observed_at_utc TEXT NOT NULL,
    platform_time TEXT,
    PRIMARY KEY(tool_user_id, aavid, ad_id, material_id, business_hour)
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    business_date TEXT NOT NULL,
    spend_cent INTEGER NOT NULL DEFAULT 0,
    order_count INTEGER NOT NULL DEFAULT 0,
    gmv_cent INTEGER NOT NULL DEFAULT 0,
    roi_decimal TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    observed_at_utc TEXT NOT NULL,
    platform_time TEXT,
    PRIMARY KEY(tool_user_id, aavid, ad_id, material_id, business_date)
);

CREATE TABLE IF NOT EXISTS platform_control_task (
    control_task_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    source_plan_id TEXT NOT NULL,
    control_task_id TEXT NOT NULL,
    task_name TEXT,
    assist_task_scene INTEGER NOT NULL CHECK(assist_task_scene IN (1, 2, 3)),
    retarget_method TEXT,
    material_ids_json TEXT NOT NULL DEFAULT '[]',
    platform_status TEXT NOT NULL,
    budget_kind TEXT,
    budget_current_cent INTEGER,
    budget_used_cent INTEGER,
    budget_remaining_cent INTEGER,
    budget_utilization_decimal TEXT,
    duration_hours_decimal TEXT,
    start_time_utc TEXT,
    end_time_utc TEXT,
    remaining_duration_hours_decimal TEXT,
    roi_or_bid_decimal TEXT,
    created_at_platform TEXT,
    updated_at_platform TEXT,
    task_revision_fingerprint TEXT NOT NULL,
    last_collection_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tool_user_id, aavid, source_plan_id, control_task_id),
    FOREIGN KEY(source_plan_id) REFERENCES source_plan(target_uid) ON DELETE CASCADE,
    FOREIGN KEY(last_collection_run_id) REFERENCES collection_run(collection_run_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS adapter_evidence (
    evidence_uid TEXT PRIMARY KEY,
    adapter_name TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    endpoint_path TEXT NOT NULL,
    http_method TEXT NOT NULL,
    dataset_key TEXT,
    request_business_fields_hash TEXT NOT NULL,
    response_schema_hash TEXT NOT NULL,
    capability_name TEXT NOT NULL,
    capability_state TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(adapter_name, adapter_version, endpoint_path, http_method, dataset_key, response_schema_hash, capability_name)
);

CREATE TABLE IF NOT EXISTS strategy (
    strategy_id TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    target_uid TEXT NOT NULL,
    strategy_type TEXT NOT NULL CHECK(strategy_type IN ('retarget_create', 'retarget_pause', 'retarget_adjust')),
    trigger_level TEXT NOT NULL DEFAULT 'material' CHECK(trigger_level IN ('material', 'product')),
    title TEXT NOT NULL,
    priority INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    action_mode TEXT NOT NULL DEFAULT 'dry_run' CHECK(action_mode IN ('dry_run')),
    trigger_json TEXT NOT NULL,
    action_params_json TEXT NOT NULL DEFAULT '{}',
    cooldown_minutes INTEGER NOT NULL DEFAULT 30 CHECK(cooldown_minutes >= 0),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tool_user_id, target_uid, title),
    FOREIGN KEY(target_uid) REFERENCES source_plan(target_uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_strategy_target_priority ON strategy(tool_user_id, target_uid, enabled, priority);

CREATE TABLE IF NOT EXISTS candidate_batch (
    candidate_batch_id TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    target_uid TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version INTEGER NOT NULL,
    business_date TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    material_snapshot_json TEXT NOT NULL,
    metrics_snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    terminal_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tool_user_id, aavid, target_uid, strategy_id, strategy_version, candidate_fingerprint),
    FOREIGN KEY(target_uid) REFERENCES source_plan(target_uid) ON DELETE CASCADE,
    FOREIGN KEY(strategy_id) REFERENCES strategy(strategy_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS retarget_group (
    group_uid TEXT PRIMARY KEY,
    candidate_batch_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    group_mode TEXT NOT NULL CHECK(group_mode IN ('selected_group', 'all_group', 'single_each')),
    material_ids_json TEXT NOT NULL,
    material_count INTEGER NOT NULL CHECK(material_count BETWEEN 1 AND 20),
    group_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by_open_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(candidate_batch_id, group_fingerprint),
    UNIQUE(candidate_batch_id, sequence),
    FOREIGN KEY(candidate_batch_id) REFERENCES candidate_batch(candidate_batch_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS adjustment_candidate (
    adjustment_candidate_id TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    target_uid TEXT NOT NULL,
    control_task_uid TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version INTEGER NOT NULL,
    business_date TEXT NOT NULL,
    action_type TEXT NOT NULL,
    budget_kind TEXT,
    budget_before_cent INTEGER,
    budget_delta_cent INTEGER,
    budget_expected_after_cent INTEGER,
    duration_before_hours_decimal TEXT,
    duration_delta_hours_decimal TEXT,
    duration_expected_after_hours_decimal TEXT,
    end_time_before_utc TEXT,
    end_time_expected_after_utc TEXT,
    task_revision_fingerprint TEXT NOT NULL,
    metrics_snapshot_json TEXT NOT NULL,
    trigger_snapshot_json TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tool_user_id, aavid, candidate_fingerprint),
    FOREIGN KEY(target_uid) REFERENCES source_plan(target_uid) ON DELETE CASCADE,
    FOREIGN KEY(control_task_uid) REFERENCES platform_control_task(control_task_uid) ON DELETE CASCADE,
    FOREIGN KEY(strategy_id) REFERENCES strategy(strategy_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS execution_task (
    execution_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    target_uid TEXT NOT NULL,
    control_task_uid TEXT,
    candidate_batch_id TEXT,
    group_uid TEXT,
    adjustment_candidate_id TEXT,
    operation_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    strategy_id TEXT,
    strategy_version INTEGER,
    authorized_by_open_id TEXT,
    authorized_at TEXT,
    status TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    submit_started_at TEXT,
    submit_finished_at TEXT,
    verify_finished_at TEXT,
    platform_object_id TEXT,
    request_snapshot_json TEXT NOT NULL DEFAULT '{}',
    result_snapshot_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tool_user_id, aavid, idempotency_key),
    FOREIGN KEY(target_uid) REFERENCES source_plan(target_uid) ON DELETE RESTRICT,
    FOREIGN KEY(candidate_batch_id) REFERENCES candidate_batch(candidate_batch_id) ON DELETE SET NULL,
    FOREIGN KEY(group_uid) REFERENCES retarget_group(group_uid) ON DELETE SET NULL,
    FOREIGN KEY(adjustment_candidate_id) REFERENCES adjustment_candidate(adjustment_candidate_id) ON DELETE SET NULL
);

CREATE TRIGGER IF NOT EXISTS trg_v1a_block_real_execution_insert
BEFORE INSERT ON execution_task
WHEN NEW.status NOT LIKE 'dry_run_%' AND NEW.status != 'archived_readonly'
BEGIN
    SELECT RAISE(ABORT, 'V1A_REAL_EXECUTION_BLOCKED');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1a_block_real_execution_update
BEFORE UPDATE OF status ON execution_task
WHEN NEW.status NOT LIKE 'dry_run_%' AND NEW.status != 'archived_readonly'
BEGIN
    SELECT RAISE(ABORT, 'V1A_REAL_EXECUTION_BLOCKED');
END;

CREATE TABLE IF NOT EXISTS execution_item (
    execution_item_id TEXT PRIMARY KEY,
    execution_uid TEXT NOT NULL,
    item_type TEXT NOT NULL,
    material_id TEXT,
    before_value TEXT,
    delta_value TEXT,
    expected_after_value TEXT,
    actual_after_value TEXT,
    status TEXT NOT NULL,
    platform_response_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    FOREIGN KEY(execution_uid) REFERENCES execution_task(execution_uid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS adjustment_usage_daily (
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    source_plan_id TEXT NOT NULL,
    control_task_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    business_date TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    reserved_count INTEGER NOT NULL DEFAULT 0,
    budget_increment_cent INTEGER NOT NULL DEFAULT 0,
    budget_reserved_cent INTEGER NOT NULL DEFAULT 0,
    duration_increment_hours_decimal TEXT NOT NULL DEFAULT '0',
    duration_reserved_hours_decimal TEXT NOT NULL DEFAULT '0',
    last_success_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(tool_user_id, aavid, source_plan_id, control_task_id, strategy_id, business_date)
);

CREATE TABLE IF NOT EXISTS task_lease (
    resource_key TEXT PRIMARY KEY,
    owner_instance_id TEXT NOT NULL,
    task_uid TEXT NOT NULL,
    priority INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    fencing_token INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS background_job (
    job_uid TEXT PRIMARY KEY,
    tool_user_id TEXT,
    job_type TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'blocked_user_action')),
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    progress_message TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_background_job_queue ON background_job(status, priority, created_at);

CREATE TABLE IF NOT EXISTS feishu_profile (
    profile_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL UNIQUE,
    app_id TEXT,
    encrypted_app_id TEXT,
    encrypted_app_secret TEXT,
    authorized_open_id TEXT,
    credential_status TEXT NOT NULL DEFAULT 'not_configured',
    transport_status TEXT NOT NULL DEFAULT 'disconnected',
    event_status TEXT NOT NULL DEFAULT 'not_received',
    binding_status TEXT NOT NULL DEFAULT 'unbound',
    send_status TEXT NOT NULL DEFAULT 'unavailable',
    last_event_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(tool_user_id) REFERENCES tool_user(tool_user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feishu_binding_code (
    binding_code_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('personal', 'group')),
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(tool_user_id, code_hash)
);

CREATE TABLE IF NOT EXISTS feishu_inbox (
    event_id TEXT NOT NULL,
    tool_user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    sender_open_id TEXT,
    message_id TEXT,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    status TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(tool_user_id, event_id)
);

CREATE TABLE IF NOT EXISTS feishu_outbox (
    outbox_id TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    route_id TEXT,
    task_uid TEXT,
    message_id TEXT,
    card_version INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    claim_owner TEXT,
    claim_expires_at TEXT,
    sent_at TEXT,
    updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tool_user_id, route_id, task_uid, card_version)
);

CREATE TABLE IF NOT EXISTS operation_event (
    event_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    account_name TEXT NOT NULL,
    source_plan_id TEXT,
    source_plan_name TEXT,
    control_task_id TEXT,
    event_time_utc TEXT NOT NULL,
    event_time_beijing TEXT NOT NULL,
    platform_time TEXT,
    operator_type TEXT,
    operator_id TEXT,
    source TEXT NOT NULL CHECK(source IN ('tool_direct', 'platform_log', 'browser_observed', 'simulation')),
    action_type TEXT NOT NULL,
    result_status TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    delta_json TEXT NOT NULL DEFAULT '{}',
    expected_after_json TEXT NOT NULL DEFAULT '{}',
    actual_after_json TEXT NOT NULL DEFAULT '{}',
    strategy_json TEXT NOT NULL DEFAULT '{}',
    request_result_json TEXT NOT NULL DEFAULT '{}',
    platform_log_id TEXT,
    possible_duplicate INTEGER NOT NULL DEFAULT 0 CHECK(possible_duplicate IN (0, 1)),
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(tool_user_id, aavid, event_uid)
);
CREATE INDEX IF NOT EXISTS idx_operation_event_report ON operation_event(tool_user_id, aavid, event_time_beijing, source);

CREATE TABLE IF NOT EXISTS daily_report_delivery (
    report_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT,
    business_date TEXT NOT NULL,
    route_id TEXT NOT NULL,
    real_summary_json TEXT NOT NULL,
    simulation_summary_json TEXT NOT NULL,
    platform_log_completeness TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tool_user_id, aavid, business_date, route_id)
);

CREATE TABLE IF NOT EXISTS migration_source (
    source_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    database_path TEXT NOT NULL,
    source_version TEXT,
    modified_at TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    account_count INTEGER NOT NULL DEFAULT 0,
    plan_count INTEGER NOT NULL DEFAULT 0,
    operation_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    inspection_error TEXT,
    inspected_at TEXT NOT NULL,
    UNIQUE(tool_user_id, database_path)
);

CREATE TABLE IF NOT EXISTS migration_run (
    migration_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    source_uid TEXT NOT NULL,
    snapshot_path TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    report_path TEXT,
    status TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(source_uid) REFERENCES migration_source(source_uid) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS system_event (
    event_uid TEXT PRIMARY KEY,
    severity TEXT NOT NULL,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


HISTORY_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS hourly_metrics (
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    business_hour TEXT NOT NULL,
    spend_cent INTEGER NOT NULL DEFAULT 0,
    order_count INTEGER NOT NULL DEFAULT 0,
    gmv_cent INTEGER NOT NULL DEFAULT 0,
    roi_decimal TEXT,
    observed_at_utc TEXT NOT NULL,
    platform_time TEXT,
    PRIMARY KEY(tool_user_id, aavid, ad_id, material_id, business_hour)
);
CREATE TABLE IF NOT EXISTS daily_metrics (
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    business_date TEXT NOT NULL,
    spend_cent INTEGER NOT NULL DEFAULT 0,
    order_count INTEGER NOT NULL DEFAULT 0,
    gmv_cent INTEGER NOT NULL DEFAULT 0,
    roi_decimal TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    observed_at_utc TEXT NOT NULL,
    platform_time TEXT,
    PRIMARY KEY(tool_user_id, aavid, ad_id, material_id, business_date)
);
CREATE TABLE IF NOT EXISTS platform_operation_log (
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    platform_log_id TEXT NOT NULL,
    event_time_utc TEXT NOT NULL,
    event_time_beijing TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(tool_user_id, aavid, platform_log_id)
);
CREATE TABLE IF NOT EXISTS long_term_audit (
    audit_uid TEXT PRIMARY KEY,
    tool_user_id TEXT NOT NULL,
    aavid TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class RuntimeDatabase:
    def __init__(self, paths: RuntimePaths, busy_timeout_ms: int = 30_000):
        self.paths = paths
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self, path: Path | None = None) -> sqlite3.Connection:
        target = path or self.paths.runtime_db
        conn = sqlite3.connect(
            str(target),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def initialize(self) -> None:
        self.paths.ensure()
        conn = self.connect()
        try:
            conn.executescript(RUNTIME_SCHEMA)
            self._migrate_schema(conn)
            now = utc_iso()
            conn.execute(
                "INSERT INTO schema_meta(key, value, updated_at) VALUES('schema_version', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(SCHEMA_VERSION), now),
            )
        finally:
            conn.close()

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """只做可重复、向前兼容的轻量迁移，绝不重写旧运行数据。"""

        feishu_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(feishu_profile)")
        }
        if "encrypted_app_id" not in feishu_columns:
            conn.execute("ALTER TABLE feishu_profile ADD COLUMN encrypted_app_id TEXT")

        inbox_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(feishu_inbox)")
        }
        if "payload_json" not in inbox_columns:
            conn.execute(
                "ALTER TABLE feishu_inbox ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'"
            )

        outbox_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(feishu_outbox)")
        }
        if "claim_owner" not in outbox_columns:
            conn.execute("ALTER TABLE feishu_outbox ADD COLUMN claim_owner TEXT")
        if "claim_expires_at" not in outbox_columns:
            conn.execute("ALTER TABLE feishu_outbox ADD COLUMN claim_expires_at TEXT")

        migration_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(migration_source)")
        }
        if "tool_user_id" not in migration_columns:
            conn.execute("ALTER TABLE migration_source ADD COLUMN tool_user_id TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_migration_source_owner_path ON migration_source(tool_user_id, database_path)"
        )

    def initialize_history(self, business_month: str) -> Path:
        path = self.paths.history_db_for_month(business_month)
        conn = self.connect(path)
        try:
            conn.executescript(HISTORY_SCHEMA)
        finally:
            conn.close()
        return path

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def integrity_check(self) -> tuple[bool, list[str]]:
        conn = self.connect()
        try:
            messages = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        finally:
            conn.close()
        return messages == ["ok"], messages


@contextmanager
def short_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """短事务；调用方不得在上下文中访问网络或浏览器。"""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


class StorageWriter:
    """运行库唯一写线程。所有任务通过 Future 返回结果。"""

    def __init__(self, database: RuntimeDatabase):
        self.database = database
        self._queue: queue.Queue[
            tuple[Callable[[sqlite3.Connection], Any] | None, Future[Any] | None]
        ] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name="qcsckp-v1a-storage-writer",
            daemon=True,
        )
        self._started = False
        self._closed = threading.Event()

    def start(self) -> "StorageWriter":
        if not self._started:
            self.database.initialize()
            self._thread.start()
            self._started = True
        return self

    def submit(
        self,
        operation: Callable[[sqlite3.Connection], T],
        *,
        timeout: float = 30.0,
    ) -> T:
        if not self._started:
            self.start()
        if self._closed.is_set():
            raise RuntimeError("storage writer is closed")
        future: Future[T] = Future()
        self._queue.put((operation, future))
        return future.result(timeout=timeout)

    def execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        timeout: float = 30.0,
    ) -> int:
        def op(conn: sqlite3.Connection) -> int:
            with short_transaction(conn):
                cursor = conn.execute(sql, tuple(params))
                return cursor.rowcount

        return self.submit(op, timeout=timeout)

    def executemany(
        self,
        sql: str,
        rows: Iterable[Sequence[Any]],
        *,
        timeout: float = 30.0,
    ) -> int:
        frozen = [tuple(row) for row in rows]

        def op(conn: sqlite3.Connection) -> int:
            with short_transaction(conn):
                cursor = conn.executemany(sql, frozen)
                return cursor.rowcount

        return self.submit(op, timeout=timeout)

    def close(self, timeout: float = 10.0) -> None:
        if not self._started or self._closed.is_set():
            return
        self._closed.set()
        self._queue.put((None, None))
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise TimeoutError("storage writer did not stop")

    def _run(self) -> None:
        conn = self.database.connect()
        try:
            while True:
                operation, future = self._queue.get()
                if operation is None:
                    return
                assert future is not None
                if future.cancelled():
                    continue
                try:
                    future.set_result(operation(conn))
                except BaseException as exc:
                    future.set_exception(exc)
        finally:
            conn.close()
