"""v0.1.46 多数据源扫描、人工选择、快照迁移与恢复。"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .collections import account_uid, target_uid
from .runtime_paths import RuntimePaths
from .security import stable_json_hash
from .storage import RuntimeDatabase, StorageWriter, short_transaction
from .timeutils import utc_iso

LEGACY_SIDE_FILES = (
    "qcookie.json",
    "qcookie.legacy.rc23.json",
    "feishu_local_profiles.json",
    "rule_retargeting.json",
    "rule_regulation.json",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


class LegacyMigrationService:
    def __init__(
        self,
        paths: RuntimePaths,
        database: RuntimeDatabase,
        writer: StorageWriter,
    ):
        self.paths = paths
        self.database = database
        self.writer = writer

    def scan(self, extra_roots: Iterable[str | Path] = ()) -> list[dict[str, Any]]:
        candidates = self._candidate_databases(extra_roots)
        inspected = []
        for path in sorted(candidates, key=lambda item: str(item).lower()):
            inspected.append(self._inspect(path))
        return inspected

    def _candidate_databases(self, extra_roots: Iterable[str | Path]) -> set[Path]:
        candidates: set[Path] = set()
        configured = [
            Path(value)
            for value in (os.getenv("QCSCKP_LEGACY_SCAN_ROOTS") or "").split(os.pathsep)
            if value.strip()
        ]
        roots = [Path(root) for root in extra_roots] + configured
        project_legacy = Path(__file__).resolve().parents[1] / "data" / "qianchuan.db"
        if project_legacy.is_file():
            candidates.add(project_legacy.resolve())
        desktop = Path.home() / "Desktop"
        if desktop.exists():
            roots.extend([desktop / "工具", desktop])
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            roots.append(Path(local_app_data) / "qcsckp-test-runtime")
        for root in roots:
            if root.is_file() and root.name.lower() == "qianchuan.db":
                candidates.add(root.resolve())
                continue
            if not root.exists() or not root.is_dir():
                continue
            patterns = (
                "qianchuan.db",
                "data/qianchuan.db",
                "QCSCKP*/data/qianchuan.db",
                "QCSCKP*/*/qianchuan.db",
            )
            for pattern in patterns:
                for path in root.glob(pattern):
                    if path.is_file():
                        candidates.add(path.resolve())
        return candidates

    def _inspect(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        source_uid = "legacy_" + stable_json_hash(str(path).lower())[:24]
        result = {
            "source_uid": source_uid,
            "database_path": str(path),
            "source_version": "0.1.46-compatible",
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(
                timespec="seconds"
            ),
            "size_bytes": stat.st_size,
            "account_count": 0,
            "plan_count": 0,
            "operation_count": 0,
            "status": "available",
            "inspection_error": None,
            "inspected_at": utc_iso(),
        }
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                result["account_count"] = _count(conn, "qianchuan_account")
                result["plan_count"] = _count(conn, "promotion_target")
                result["operation_count"] = _count(conn, "account_operation_event")
                integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                if integrity != "ok":
                    result["status"] = "invalid"
                    result["inspection_error"] = integrity[:500]
            finally:
                conn.close()
        except Exception as exc:
            result["status"] = "invalid"
            result["inspection_error"] = str(exc)[:500]
        self.writer.execute(
            """
            INSERT INTO migration_source(
                source_uid, database_path, source_version, modified_at,
                size_bytes, account_count, plan_count, operation_count,
                status, inspection_error, inspected_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(database_path) DO UPDATE SET
                source_version=excluded.source_version,
                modified_at=excluded.modified_at,
                size_bytes=excluded.size_bytes,
                account_count=excluded.account_count,
                plan_count=excluded.plan_count,
                operation_count=excluded.operation_count,
                status=excluded.status,
                inspection_error=excluded.inspection_error,
                inspected_at=excluded.inspected_at
            """,
            tuple(result[key] for key in (
                "source_uid", "database_path", "source_version", "modified_at",
                "size_bytes", "account_count", "plan_count", "operation_count",
                "status", "inspection_error", "inspected_at"
            )),
        )
        return result

    def migrate(self, tool_user_id: str, source_uid: str) -> dict[str, Any]:
        source = self.database.query_one(
            "SELECT * FROM migration_source WHERE source_uid=?", (source_uid,)
        )
        if not source or source["status"] != "available":
            raise ValueError("迁移源不存在或不可用")
        legacy_db = Path(str(source["database_path"]))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        snapshot_dir = self.paths.snapshots_dir / f"migration-{stamp}-{uuid.uuid4().hex[:8]}"
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        legacy_snapshot = snapshot_dir / "legacy-qianchuan.db"
        pre_runtime_snapshot = snapshot_dir / "pre-migration-runtime.db"
        shutil.copy2(legacy_db, legacy_snapshot)
        if self.paths.runtime_db.exists():
            self._sqlite_backup(self.paths.runtime_db, pre_runtime_snapshot)
        for filename in LEGACY_SIDE_FILES:
            candidate = legacy_db.parent / filename
            if candidate.is_file():
                shutil.copy2(candidate, snapshot_dir / filename)
        manifest_path = snapshot_dir / "manifest.json"
        migration_uid = f"migration_{uuid.uuid4().hex}"
        manifest = {
            "migration_uid": migration_uid,
            "source_uid": source_uid,
            "source_database": str(legacy_db),
            "legacy_snapshot": str(legacy_snapshot),
            "pre_runtime_snapshot": str(pre_runtime_snapshot)
            if pre_runtime_snapshot.exists()
            else None,
            "created_at": utc_iso(),
            "original_untouched": True,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self.writer.execute(
            """
            INSERT INTO migration_run(
                migration_uid, tool_user_id, source_uid, snapshot_path,
                manifest_path, status, started_at
            ) VALUES(?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                migration_uid,
                tool_user_id,
                source_uid,
                str(snapshot_dir),
                str(manifest_path),
                utc_iso(),
            ),
        )
        try:
            counts = self._copy_legacy(tool_user_id, legacy_snapshot, snapshot_dir)
            report_path = snapshot_dir / "migration-report.json"
            report = {
                **manifest,
                "status": "succeeded",
                "counts": counts,
                "rollback": {
                    "runtime_snapshot": str(pre_runtime_snapshot),
                    "command": "在迁移、诊断与恢复页选择此迁移记录并点击恢复",
                },
                "completed_at": utc_iso(),
            }
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            self.writer.execute(
                """
                UPDATE migration_run SET status='succeeded', counts_json=?,
                    report_path=?, completed_at=? WHERE migration_uid=?
                """,
                (
                    json.dumps(counts, ensure_ascii=False, sort_keys=True),
                    str(report_path),
                    utc_iso(),
                    migration_uid,
                ),
            )
            return report
        except Exception as exc:
            self.writer.execute(
                "UPDATE migration_run SET status='failed', error_code=?, error_message=?, completed_at=? WHERE migration_uid=?",
                (type(exc).__name__, str(exc)[:1000], utc_iso(), migration_uid),
            )
            raise

    def _copy_legacy(
        self, tool_user_id: str, legacy_snapshot: Path, snapshot_dir: Path
    ) -> dict[str, int]:
        legacy = sqlite3.connect(str(legacy_snapshot))
        legacy.row_factory = sqlite3.Row
        counts = {"accounts": 0, "plans": 0, "operations": 0, "strategies": 0, "archived_tasks": 0}
        try:
            accounts = (
                legacy.execute("SELECT * FROM qianchuan_account").fetchall()
                if _table_exists(legacy, "qianchuan_account")
                else []
            )
            plans = (
                legacy.execute("SELECT * FROM promotion_target").fetchall()
                if _table_exists(legacy, "promotion_target")
                else []
            )
            operations = (
                legacy.execute("SELECT * FROM account_operation_event").fetchall()
                if _table_exists(legacy, "account_operation_event")
                else []
            )

            def op(conn):
                with short_transaction(conn):
                    now = utc_iso()
                    for row in accounts:
                        data = dict(row)
                        aavid = str(data.get("aavid") or "")
                        if not aavid:
                            continue
                        conn.execute(
                            """
                            INSERT INTO advertiser_account(
                                account_uid, tool_user_id, aavid, account_name,
                                enabled, daily_report_enabled, catalog_status,
                                catalog_completed_at, created_at, updated_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(tool_user_id, aavid) DO NOTHING
                            """,
                            (
                                account_uid(tool_user_id, aavid),
                                tool_user_id,
                                aavid,
                                str(data.get("account_name") or aavid),
                                int(bool(data.get("enabled"))),
                                int(bool(data.get("report_enabled"))),
                                str(data.get("catalog_status") or "legacy_imported"),
                                data.get("catalog_last_sync_at"),
                                data.get("created_at") or now,
                                now,
                            ),
                        )
                        counts["accounts"] += 1
                    for row in plans:
                        data = dict(row)
                        aavid = str(data.get("aadvid") or data.get("aavid") or "")
                        ad_id = str(data.get("ad_id") or "")
                        if not aavid or not ad_id:
                            continue
                        # 少数旧库只有计划没有账户目录；先建立明确标注的占位账户，
                        # 保证外键完整且不把未知名称冒充为真实账户名。
                        conn.execute(
                            """
                            INSERT INTO advertiser_account(
                                account_uid, tool_user_id, aavid, account_name,
                                enabled, daily_report_enabled, catalog_status,
                                created_at, updated_at
                            ) VALUES(?, ?, ?, ?, 0, 0, 'legacy_unverified', ?, ?)
                            ON CONFLICT(tool_user_id, aavid) DO NOTHING
                            """,
                            (
                                account_uid(tool_user_id, aavid),
                                tool_user_id,
                                aavid,
                                f"待重新识别账户 {aavid}",
                                now,
                                now,
                            ),
                        )
                        # 旧计划全部要求新适配器重新验证；监控选择保留，但不可立即生成候选。
                        conn.execute(
                            """
                            INSERT INTO source_plan(
                                target_uid, tool_user_id, aavid, ad_id, plan_name,
                                plan_system, promotion_scene, platform_status,
                                verification_state, catalog_seen_at, monitor_enabled,
                                monitor_eligible, retarget_eligible, pause_eligible,
                                adjust_eligible, ineligible_reason, adapter_version,
                                created_at, updated_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'legacy_unverified', ?, ?, 0, 0, 0, 0,
                                     '迁移后等待V1A只读适配器重新验证', 'legacy-v0.1.46', ?, ?)
                            ON CONFLICT(tool_user_id, aavid, ad_id) DO NOTHING
                            """,
                            (
                                target_uid(tool_user_id, aavid, ad_id),
                                tool_user_id,
                                aavid,
                                ad_id,
                                str(data.get("plan_name") or ad_id),
                                str(data.get("plan_system") or "unknown")
                                if str(data.get("plan_system") or "unknown") in {"global", "chengfang", "unknown"}
                                else "unknown",
                                str(data.get("promotion_scene") or "unknown")
                                if str(data.get("promotion_scene") or "unknown") in {"product", "live", "unknown"}
                                else "unknown",
                                str(data.get("platform_status") or "unknown"),
                                data.get("catalog_seen_at"),
                                int(bool(data.get("enabled"))),
                                data.get("created_at") or now,
                                now,
                            ),
                        )
                        counts["plans"] += 1
                    for row in operations:
                        data = dict(row)
                        aavid = str(data.get("aavid") or "")
                        if not aavid:
                            continue
                        event_uid = "legacy_" + stable_json_hash(
                            [str(legacy_snapshot), data.get("event_uid"), data.get("id")]
                        )[:40]
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO operation_event(
                                event_uid, tool_user_id, aavid, account_name,
                                source_plan_id, source_plan_name, control_task_id,
                                event_time_utc, event_time_beijing, operator_type,
                                operator_id, source, action_type, result_status,
                                before_json, actual_after_json, request_result_json,
                                platform_log_id, possible_duplicate, error_code,
                                error_message, created_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                event_uid,
                                tool_user_id,
                                aavid,
                                str(data.get("account_name") or aavid),
                                data.get("target_uid"),
                                data.get("plan_name"),
                                data.get("regulate_task_id"),
                                data.get("event_time") or data.get("created_at") or now,
                                data.get("event_time") or data.get("created_at") or now,
                                "legacy",
                                data.get("operator_id"),
                                str(data.get("source") or "browser_observed")
                                if str(data.get("source") or "browser_observed") in {"tool_direct", "platform_log", "browser_observed"}
                                else "browser_observed",
                                str(data.get("action_type") or "other"),
                                str(data.get("status") or "unknown"),
                                str(data.get("before_json") or "{}"),
                                str(data.get("after_json") or "{}"),
                                json.dumps({"legacy_detail": data.get("detail")}, ensure_ascii=False),
                                data.get("platform_record_id"),
                                int(bool(data.get("possible_duplicate"))),
                                data.get("failure_reason"),
                                data.get("failure_reason"),
                                now,
                            ),
                        )
                        counts["operations"] += 1

            self.writer.submit(op)
            counts["strategies"] = self._migrate_rule_files(tool_user_id, snapshot_dir)
            if _table_exists(legacy, "local_retarget_task"):
                active = legacy.execute(
                    "SELECT COUNT(*) FROM local_retarget_task WHERE status NOT IN ('succeeded','failed','rejected','expired','cancelled')"
                ).fetchone()[0]
                counts["archived_tasks"] = int(active)
            return counts
        finally:
            legacy.close()

    def _migrate_rule_files(self, tool_user_id: str, snapshot_dir: Path) -> int:
        inserted = 0
        for filename, strategy_type in (
            ("rule_retargeting.json", "retarget_create"),
            ("rule_regulation.json", "retarget_pause"),
        ):
            path = snapshot_dir / filename
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            raw_strategies = payload.get("strategies") if isinstance(payload, dict) else None
            if not isinstance(raw_strategies, list):
                continue
            for index, raw in enumerate(raw_strategies, start=1):
                if not isinstance(raw, dict):
                    continue
                legacy_target = str(raw.get("target_uid") or "")
                target = self.database.query_one(
                    "SELECT target_uid FROM source_plan WHERE tool_user_id=? AND target_uid=?",
                    (tool_user_id, legacy_target),
                )
                if not target:
                    continue
                now = utc_iso()
                self.writer.execute(
                    """
                    INSERT OR IGNORE INTO strategy(
                        strategy_id, tool_user_id, target_uid, strategy_type,
                        trigger_level, title, priority, enabled, action_mode,
                        trigger_json, action_params_json, cooldown_minutes,
                        version, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, 'dry_run', ?, ?, 30, 1, ?, ?)
                    """,
                    (
                        "legacy_strategy_" + stable_json_hash([filename, index, raw])[:24],
                        tool_user_id,
                        target["target_uid"],
                        strategy_type,
                        str(raw.get("trigger_level") or "material")
                        if str(raw.get("trigger_level") or "material") in {"material", "product"}
                        else "material",
                        str(raw.get("title") or f"迁移草稿 {index}"),
                        int(raw.get("priority") or index),
                        json.dumps(
                            {
                                "legacy_disabled_draft": True,
                                "legacy_trigger": raw.get("trigger") or {},
                                "logic": "AND",
                                "window": "today_cumulative",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps(
                            {"legacy_action": raw.get("retargeting") or raw.get("action") or {}},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                        now,
                    ),
                )
                inserted += 1
        return inserted

    def restore_pre_migration_snapshot(self, migration_uid: str) -> dict[str, Any]:
        row = self.database.query_one(
            "SELECT * FROM migration_run WHERE migration_uid=?", (migration_uid,)
        )
        if not row:
            raise KeyError(migration_uid)
        snapshot = Path(str(row["snapshot_path"])) / "pre-migration-runtime.db"
        if not snapshot.is_file():
            raise ValueError("此迁移记录没有可恢复的运行库快照")
        restore_copy = self.paths.snapshots_dir / f"before-restore-{uuid.uuid4().hex}.db"
        self._sqlite_backup(self.paths.runtime_db, restore_copy)
        request_path = self.paths.root / "restore-request.json"
        request_path.write_text(
            json.dumps(
                {
                    "migration_uid": migration_uid,
                    "snapshot": str(snapshot.resolve()),
                    "snapshot_sha256": _sha256_file(snapshot),
                    "safety_copy": str(restore_copy.resolve()),
                    "requested_at": utc_iso(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "status": "restart_required",
            "request_path": str(request_path),
            "safety_copy": str(restore_copy),
        }

    @staticmethod
    def _sqlite_backup(source: Path, destination: Path) -> None:
        source_conn = sqlite3.connect(str(source))
        destination_conn = sqlite3.connect(str(destination))
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
            source_conn.close()
