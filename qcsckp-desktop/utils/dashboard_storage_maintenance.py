"""Compact dashboard history without weakening metric accuracy.

Official API collections keep the complete current material state in
``pmc_promotion_material_latest`` and store only changed numeric metrics in the
five-minute snapshot table.  This module migrates legacy full-row history,
rolls snapshots older than 48 hours into hourly points, and prunes old API
audit rows in bounded transactions.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from utils.sqlite_store import SQLiteStore, init_sqlite_schema


SNAPSHOT_RETENTION_HOURS = 48
API_AUDIT_RETENTION_DAYS = 30
HOURLY_RETENTION_DAYS = 180
REMOVED_MATERIAL_RETENTION_DAYS = 7
COMPACT_MIGRATION_VERSION = "1"

METRIC_COLUMNS = (
    "stat_cost",
    "order_settle_count_1h",
    "order_settle_amount_1h",
    "order_settle_rate_1h",
    "prepay_pay_order_count",
    "pay_gmv_include_coupon",
    "prepay_pay_settle_1h",
    "refund_rate_1h",
    "overall_order_count",
    "overall_show_count",
    "overall_click_count",
    "overall_ctr",
    "overall_conversion_rate",
)

LATEST_COPY_COLUMNS = (
    "aadvid",
    "target_uid",
    "ad_id",
    "promotion_scene",
    "plan_system",
    "material_id",
    "product_ids_json",
    "video_name",
    "material_status",
    "show_status",
    "show_status_reason",
    "upload_time",
    "video_type",
    "video_id",
    "aweme_item_id",
    "cover_url",
    "cover_width",
    "cover_height",
    "video_duration",
    "video_title",
    "lego_source",
    "video_create_time",
    "tag_list",
    *METRIC_COLUMNS,
    "data_source",
    "api_request_id",
    "stat_date",
)


def _cutoff(hours: int) -> str:
    return (datetime.now() - timedelta(hours=max(1, int(hours)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _five_minute_bucket_sql(column: str) -> str:
    return (
        f"strftime('%Y-%m-%d %H:', {column}) || "
        f"printf('%02d:00', (CAST(strftime('%M', {column}) AS INTEGER) / 5) * 5)"
    )


def _migration_state_key(owner_username: Optional[str]) -> str:
    owner = str(owner_username or "all").strip().casefold() or "all"
    return f"compact_history_migration:{owner}"


def _migration_completed(store: SQLiteStore, owner_username: Optional[str]) -> bool:
    row = store.select_one(
        "dashboard_storage_state",
        where={"state_key": _migration_state_key(owner_username)},
    )
    return str((row or {}).get("state_value") or "") == COMPACT_MIGRATION_VERSION


def _mark_migration_completed(
    store: SQLiteStore, owner_username: Optional[str]
) -> None:
    store.insert_or_update(
        "dashboard_storage_state",
        {
            "state_key": _migration_state_key(owner_username),
            "state_value": COMPACT_MIGRATION_VERSION,
        },
        unique_fields=["state_key"],
    )


def backfill_latest_from_legacy(
    *, db: Optional[SQLiteStore] = None, owner_username: Optional[str] = None
) -> int:
    """Fill missing latest rows from the newest trustworthy legacy snapshot."""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    owner_clause = " AND qa.owner_username=?" if owner_username else ""
    params: tuple[Any, ...] = (str(owner_username).casefold(),) if owner_username else ()
    columns = ",".join(LATEST_COPY_COLUMNS)
    selected = ",".join(f"h.{name}" for name in LATEST_COPY_COLUMNS)
    sql = f"""
        INSERT OR IGNORE INTO pmc_promotion_material_latest
            ({columns}, collected_at, created_at, updated_at)
        SELECT {selected}, h.created_at, h.created_at,
               COALESCE(h.updated_at, h.created_at)
        FROM pmc_promotion_material h
        INNER JOIN (
            SELECT source.target_uid, source.material_id, MAX(source.id) AS max_id
            FROM pmc_promotion_material source
            INNER JOIN promotion_target pt ON pt.target_uid=source.target_uid
            INNER JOIN qianchuan_account qa ON qa.account_uid=pt.account_uid
            WHERE source.data_source='qianchuan_open_api'{owner_clause}
            GROUP BY source.target_uid, source.material_id
        ) newest ON newest.max_id=h.id
    """
    return int(store.execute(sql, params) or 0)


def backfill_recent_metric_snapshots(
    *,
    db: Optional[SQLiteStore] = None,
    owner_username: Optional[str] = None,
    retention_hours: int = SNAPSHOT_RETENTION_HOURS,
) -> int:
    """Convert recent legacy full rows into one narrow row per five-minute bucket."""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    cutoff = _cutoff(retention_hours)
    owner_clause = " AND qa.owner_username=?" if owner_username else ""
    params: tuple[Any, ...] = (cutoff,) + (
        (str(owner_username).casefold(),) if owner_username else ()
    )
    bucket = _five_minute_bucket_sql("h.created_at")
    signature = " || '|' || ".join(
        f"printf('%.8f',COALESCE(h.{name},0))" for name in METRIC_COLUMNS
    )
    metric_names = ",".join(METRIC_COLUMNS)
    metric_select = ",".join(f"h.{name}" for name in METRIC_COLUMNS)
    before = store.count("pmc_material_metric_snapshot")
    sql = f"""
        WITH raw AS (
            SELECT h.id,h.target_uid,h.material_id,{bucket} AS bucket_key,
                   {signature} AS metric_signature
            FROM pmc_promotion_material h
            INNER JOIN promotion_target pt ON pt.target_uid=h.target_uid
            INNER JOIN qianchuan_account qa ON qa.account_uid=pt.account_uid
            WHERE h.data_source='qianchuan_open_api' AND h.created_at>=?{owner_clause}
        ), ordered AS (
            SELECT raw.*,
                   LAG(metric_signature) OVER (
                       PARTITION BY target_uid,material_id ORDER BY id ASC
                   ) AS previous_signature
            FROM raw
        ), candidates AS (
            SELECT id,target_uid,material_id,bucket_key FROM ordered
            WHERE previous_signature IS NULL OR metric_signature<>previous_signature
        ), newest AS (
            SELECT target_uid, material_id, bucket_key, MAX(id) AS max_id
            FROM candidates GROUP BY target_uid, material_id, bucket_key
        )
        INSERT OR IGNORE INTO pmc_material_metric_snapshot (
            account_username, account_uid, aadvid, target_uid, ad_id,
            material_id, bucket_key, collected_at, stat_date,
            {metric_names}, api_request_id, created_at, updated_at
        )
        SELECT qa.owner_username, pt.account_uid, h.aadvid, h.target_uid, h.ad_id,
               h.material_id, newest.bucket_key, h.created_at,
               COALESCE(h.stat_date, substr(h.created_at,1,10)),
               {metric_select}, h.api_request_id, h.created_at,
               COALESCE(h.updated_at,h.created_at)
        FROM newest
        INNER JOIN pmc_promotion_material h ON h.id=newest.max_id
        INNER JOIN promotion_target pt ON pt.target_uid=h.target_uid
        INNER JOIN qianchuan_account qa ON qa.account_uid=pt.account_uid
    """
    store.execute(sql, params)
    return max(0, store.count("pmc_material_metric_snapshot") - before)


def backfill_legacy_hourly_metrics(
    *,
    db: Optional[SQLiteStore] = None,
    owner_username: Optional[str] = None,
    retention_hours: int = SNAPSHOT_RETENTION_HOURS,
) -> int:
    """Preserve one last trustworthy point per old hour before pruning legacy rows."""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    cutoff = _cutoff(retention_hours)
    owner_clause = " AND qa.owner_username=?" if owner_username else ""
    params: tuple[Any, ...] = (cutoff,) + (
        (str(owner_username).casefold(),) if owner_username else ()
    )
    metric_names = ",".join(METRIC_COLUMNS)
    metric_select = ",".join(f"h.{name}" for name in METRIC_COLUMNS)
    before = store.count("pmc_material_metric_hourly")
    sql = f"""
        WITH candidates AS (
            SELECT h.id,h.target_uid,h.material_id,
                   substr(h.created_at,1,13)||':00:00' AS hour_key
            FROM pmc_promotion_material h
            INNER JOIN promotion_target pt ON pt.target_uid=h.target_uid
            INNER JOIN qianchuan_account qa ON qa.account_uid=pt.account_uid
            WHERE h.data_source='qianchuan_open_api' AND h.created_at<?{owner_clause}
        ), newest AS (
            SELECT target_uid,material_id,hour_key,MAX(id) AS max_id
            FROM candidates GROUP BY target_uid,material_id,hour_key
        )
        INSERT OR IGNORE INTO pmc_material_metric_hourly (
            account_username,account_uid,aadvid,target_uid,ad_id,material_id,
            hour_key,collected_at,{metric_names},api_request_id,created_at,updated_at
        )
        SELECT qa.owner_username,pt.account_uid,h.aadvid,h.target_uid,h.ad_id,
               h.material_id,newest.hour_key,h.created_at,{metric_select},
               h.api_request_id,h.created_at,COALESCE(h.updated_at,h.created_at)
        FROM newest
        INNER JOIN pmc_promotion_material h ON h.id=newest.max_id
        INNER JOIN promotion_target pt ON pt.target_uid=h.target_uid
        INNER JOIN qianchuan_account qa ON qa.account_uid=pt.account_uid
    """
    store.execute(sql, params)
    return max(0, store.count("pmc_material_metric_hourly") - before)


def rollup_old_metric_snapshots(
    *,
    db: Optional[SQLiteStore] = None,
    retention_hours: int = SNAPSHOT_RETENTION_HOURS,
    delete_limit: int = 50_000,
) -> dict[str, int]:
    """Persist the last point of each old hour, then delete a bounded raw batch."""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    cutoff = _cutoff(retention_hours)
    metric_names = ",".join(METRIC_COLUMNS)
    metric_select = ",".join(f"s.{name}" for name in METRIC_COLUMNS)
    before = store.count("pmc_material_metric_hourly")
    store.execute(
            f"""
            WITH newest AS (
                SELECT account_username,target_uid,material_id,
                       substr(collected_at,1,13)||':00:00' AS hour_key,
                       MAX(id) AS max_id
                FROM pmc_material_metric_snapshot
                WHERE collected_at<?
                GROUP BY account_username,target_uid,material_id,hour_key
            )
            INSERT OR REPLACE INTO pmc_material_metric_hourly (
                account_username,account_uid,aadvid,target_uid,ad_id,material_id,
                hour_key,collected_at,{metric_names},api_request_id,created_at,updated_at
            )
            SELECT s.account_username,s.account_uid,s.aadvid,s.target_uid,s.ad_id,
                   s.material_id,newest.hour_key,s.collected_at,{metric_select},
                   s.api_request_id,s.created_at,datetime('now','+8 hours')
            FROM newest INNER JOIN pmc_material_metric_snapshot s ON s.id=newest.max_id
            """,
            (cutoff,),
        )
    rolled = max(0, store.count("pmc_material_metric_hourly") - before)
    deleted = int(
        store.execute(
            "DELETE FROM pmc_material_metric_snapshot WHERE id IN ("
            "SELECT id FROM pmc_material_metric_snapshot WHERE collected_at<? "
            "ORDER BY id ASC LIMIT ?)",
            (cutoff, max(1, int(delete_limit))),
        )
        or 0
    )
    return {"rolled": rolled, "deleted": deleted}


def prune_api_audit(
    *, db: Optional[SQLiteStore] = None, retention_days: int = API_AUDIT_RETENTION_DAYS
) -> int:
    store = db or SQLiteStore()
    cutoff = (datetime.now() - timedelta(days=max(1, int(retention_days)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return int(
        store.execute("DELETE FROM qianchuan_api_audit WHERE created_at<?", (cutoff,))
        or 0
    )


def prune_hourly_metrics(
    *, db: Optional[SQLiteStore] = None, retention_days: int = HOURLY_RETENTION_DAYS
) -> int:
    store = db or SQLiteStore()
    cutoff = (datetime.now() - timedelta(days=max(1, int(retention_days)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return int(
        store.execute(
            "DELETE FROM pmc_material_metric_hourly WHERE collected_at<?",
            (cutoff,),
        )
        or 0
    )


def prune_removed_material_latest(
    *,
    db: Optional[SQLiteStore] = None,
    retention_days: int = REMOVED_MATERIAL_RETENTION_DAYS,
) -> int:
    store = db or SQLiteStore()
    cutoff = (datetime.now() - timedelta(days=max(1, int(retention_days)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return int(
        store.execute(
            "DELETE FROM pmc_promotion_material_latest "
            "WHERE delivery_state='removed' AND last_seen_at IS NOT NULL "
            "AND last_seen_at<?",
            (cutoff,),
        )
        or 0
    )


def prune_migrated_official_history(
    *,
    db: Optional[SQLiteStore] = None,
    delete_limit: int = 50_000,
    migration_verified: bool = False,
) -> int:
    """Delete only rows whose compact replacement is already verified present."""
    store = db or SQLiteStore()
    # Keep the compatibility argument, but never bypass replacement checks.
    # The production path previously marked migration complete immediately
    # before pruning, making the safe branch unreachable and deleting rows
    # from removed/unscoped targets without a compact replacement.
    _ = migration_verified
    return int(
        store.execute(
            f"""
            DELETE FROM pmc_promotion_material WHERE id IN (
                SELECT h.id FROM pmc_promotion_material h
                INNER JOIN promotion_target pt ON pt.target_uid=h.target_uid
                INNER JOIN qianchuan_account qa ON qa.account_uid=pt.account_uid
                WHERE h.data_source='qianchuan_open_api'
                  AND EXISTS (
                      SELECT 1 FROM pmc_promotion_material_latest latest
                      WHERE latest.target_uid=h.target_uid
                        AND latest.material_id=h.material_id
                  )
                  AND (
                      EXISTS (
                          SELECT 1 FROM pmc_material_metric_snapshot snap
                          WHERE snap.account_username=qa.owner_username
                            AND snap.target_uid=h.target_uid
                            AND snap.material_id=h.material_id
                      )
                      OR EXISTS (
                          SELECT 1 FROM pmc_material_metric_hourly hourly
                          WHERE hourly.account_username=qa.owner_username
                            AND hourly.target_uid=h.target_uid
                            AND hourly.material_id=h.material_id
                      )
                  )
                ORDER BY h.id ASC LIMIT ?
            )
            """,
            (max(1, int(delete_limit)),),
        )
        or 0
    )


def run_dashboard_storage_maintenance(
    *, db: Optional[SQLiteStore] = None, owner_username: Optional[str] = None
) -> dict[str, Any]:
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    latest = 0
    recent = 0
    legacy_hourly = 0
    if not _migration_completed(store, owner_username):
        latest = backfill_latest_from_legacy(db=store, owner_username=owner_username)
        recent = backfill_recent_metric_snapshots(
            db=store, owner_username=owner_username
        )
        legacy_hourly = backfill_legacy_hourly_metrics(
            db=store, owner_username=owner_username
        )
        _mark_migration_completed(store, owner_username)
    rolled = rollup_old_metric_snapshots(db=store)
    audit = prune_api_audit(db=store)
    hourly = prune_hourly_metrics(db=store)
    removed_latest = prune_removed_material_latest(db=store)
    legacy = prune_migrated_official_history(
        db=store,
        migration_verified=False,
    )
    return {
        "latest_backfilled": latest,
        "recent_snapshots_backfilled": recent,
        "legacy_hourly_backfilled": legacy_hourly,
        "hourly_rolled": rolled["rolled"],
        "snapshots_deleted": rolled["deleted"],
        "audit_deleted": audit,
        "hourly_deleted": hourly,
        "removed_latest_deleted": removed_latest,
        "legacy_rows_deleted": legacy,
    }
