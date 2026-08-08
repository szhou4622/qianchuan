"""V1A 跨层共享常量。"""

from __future__ import annotations

PRODUCT_VERSION = "1A.0.2-dev"
SCHEMA_VERSION = 4
RUNTIME_NAME = "production-v1a"
AUTH_OFFLINE_GRACE_HOURS = 72
AUTH_ACCESS_TOKEN_HOURS = 12
DEFAULT_CANDIDATE_COOLDOWN_MINUTES = 30
MAX_ACCOUNTS = 20
MAX_MONITORED_PLANS_PER_ACCOUNT = 10
MAX_MATERIALS_PER_GROUP = 20
MIN_FREE_DISK_BYTES = 1 * 1024 * 1024 * 1024
COLLECTION_RELATION_SOFT_LIMIT = 20_000
COLLECTION_RELATION_HARD_LIMIT = 50_000
COLLECTION_INTERVAL_MIN_SECONDS = 300
COLLECTION_INTERVAL_MAX_SECONDS = 600

COLLECTION_TERMINAL_STATES = frozenset(
    {
        "complete",
        "partial",
        "timeout",
        "suspicious_empty",
        "login_required",
        "permission_denied",
        "schema_changed",
        "failed",
    }
)

CANDIDATE_STATES = frozenset(
    {
        "draft",
        "frozen",
        "pending_approval",
        "partially_approved",
        "completed",
        "rejected",
        "expired",
        "cancelled",
    }
)

# V1A 只允许模拟任务。真实写状态保留在 schema 中供未来迁移和只读归档，
# 但服务层不得创建或推进到 submitting。
EXECUTION_STATES = frozenset(
    {
        "dry_run_queued",
        "dry_run_running",
        "dry_run_succeeded",
        "dry_run_failed",
        "archived_readonly",
        "pending_approval",
        "approved_queued",
        "claimed",
        "preflight",
        "failed_pre_submit",
        "submitting",
        "verifying",
        "succeeded",
        "partial_success",
        "failed",
        "result_unknown",
        "manual_review",
        "rejected",
        "expired",
        "cancelled",
    }
)

REAL_EXECUTION_STATES = frozenset(
    {
        "pending_approval",
        "approved_queued",
        "claimed",
        "preflight",
        "submitting",
        "verifying",
        "succeeded",
        "partial_success",
        "failed_pre_submit",
        "result_unknown",
        "manual_review",
    }
)
