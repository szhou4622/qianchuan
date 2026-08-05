"""V1A 后台服务容器、作业处理器和只读调度。"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from .adapters import AdapterRegistry
from .auth import AdminSessionStore, LocalAdminService
from .browser_worker import PlaywrightBrowserWorker
from .candidates import CandidateBlocked, CandidateService
from .collections import CollectionService
from .constants import (
    COLLECTION_INTERVAL_MAX_SECONDS,
    COLLECTION_INTERVAL_MIN_SECONDS,
    COLLECTION_RELATION_HARD_LIMIT,
    COLLECTION_RELATION_SOFT_LIMIT,
    MAX_ACCOUNTS,
    MAX_MONITORED_PLANS_PER_ACCOUNT,
    MIN_FREE_DISK_BYTES,
    PRODUCT_VERSION,
    SCHEMA_VERSION,
)
from .feishu import FeishuService
from .jobs import EventBus, JobContext, JobService, JobWorker
from .leases import LeaseManager
from .migration import LegacyMigrationService
from .reports import OperationReportService
from .runtime_paths import RuntimePaths
from .security import PlatformNetworkGuard
from .storage import RuntimeDatabase, StorageWriter, short_transaction
from .strategies import StrategyService
from .timeutils import BEIJING, business_date, utc_iso, utc_now


def collection_interval_for_relation_count(relation_count: int) -> tuple[int, str]:
    relation_count = max(0, int(relation_count))
    if relation_count <= COLLECTION_RELATION_SOFT_LIMIT:
        return COLLECTION_INTERVAL_MIN_SECONDS, "within_5_minutes"
    if relation_count <= COLLECTION_RELATION_HARD_LIMIT:
        ratio = (relation_count - COLLECTION_RELATION_SOFT_LIMIT) / (
            COLLECTION_RELATION_HARD_LIMIT - COLLECTION_RELATION_SOFT_LIMIT
        )
        interval = round(
            COLLECTION_INTERVAL_MIN_SECONDS
            + ratio
            * (COLLECTION_INTERVAL_MAX_SECONDS - COLLECTION_INTERVAL_MIN_SECONDS)
        )
        return interval, "adaptive_5_to_10_minutes"
    return COLLECTION_INTERVAL_MAX_SECONDS, "capacity_exceeded"


class RuntimeContext:
    def __init__(self, paths: RuntimePaths | None = None):
        self.paths = (paths or RuntimePaths.default()).ensure()
        self.database = RuntimeDatabase(self.paths)
        self.writer = StorageWriter(self.database).start()
        self.events = EventBus()
        self.sessions = AdminSessionStore()
        self.auth = LocalAdminService(self.database, self.writer)
        self.guard = PlatformNetworkGuard(self._record_guard_block)
        self.browser = PlaywrightBrowserWorker(self.database, self.writer, self.guard)
        self.adapters = AdapterRegistry(self.browser, self.guard)
        self.collections = CollectionService(self.database, self.writer, self.adapters)
        self.strategies = StrategyService(self.database, self.writer)
        self.candidates = CandidateService(self.database, self.writer)
        self.feishu = FeishuService(self.database, self.writer, self.candidates)
        self.reports = OperationReportService(self.database, self.writer)
        self.migrations = LegacyMigrationService(self.paths, self.database, self.writer)
        self.instance_id = f"instance_{uuid.uuid4().hex}"
        self.leases = LeaseManager(self.database, self.writer)
        self.jobs = JobService(
            self.database,
            self.writer,
            self.leases,
            self.events,
            self.instance_id,
        )
        self._register_jobs()
        self.worker = JobWorker(self.jobs, on_stop=self.browser.close).start()
        self.scheduler = V1AScheduler(self).start()

    def _record_guard_block(self, details: dict[str, Any]) -> None:
        self.writer.execute(
            "INSERT INTO system_event(event_uid, severity, event_type, details_json, created_at) VALUES(?, 'critical', 'platform_write_blocked', ?, ?)",
            (
                f"system_{uuid.uuid4().hex}",
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                utc_iso(),
            ),
        )
        self.events.publish({"type": "security.platform_write_blocked", **details})

    def _register_jobs(self) -> None:
        self.jobs.register("qianchuan_login", self._job_qianchuan_login)
        self.jobs.register("qianchuan_add_account", self._job_add_account)
        self.jobs.register("qianchuan_delete_account", self._job_delete_account)
        self.jobs.register("catalog_refresh", self._job_catalog_refresh)
        self.jobs.register("monitor_setup_save", self._job_monitor_setup_save)
        self.jobs.register("target_collect", self._job_target_collect)
        self.jobs.register("strategy_save", self._job_strategy_save)
        self.jobs.register("strategy_toggle", self._job_strategy_toggle)
        self.jobs.register("strategy_reorder", self._job_strategy_reorder)
        self.jobs.register("candidate_generate", self._job_candidate_generate)
        self.jobs.register("candidate_group_save", self._job_candidate_group_save)
        self.jobs.register("candidate_preview_send", self._job_candidate_preview_send)
        self.jobs.register("feishu_reconnect", self._job_feishu_reconnect)
        self.jobs.register("feishu_credentials_test", self._job_feishu_credentials_test)
        self.jobs.register("feishu_binding_code", self._job_feishu_binding_code)
        self.jobs.register("feishu_test_send", self._job_feishu_test_send)
        self.jobs.register("migration_scan", self._job_migration_scan)
        self.jobs.register("migration_execute", self._job_migration_execute)
        self.jobs.register("migration_restore", self._job_migration_restore)
        self.jobs.register("operation_log_sync", self._job_operation_log_sync)
        self.jobs.register("daily_report_send", self._job_daily_report_send)

    def health(self) -> dict[str, Any]:
        ok, integrity = self.database.integrity_check()
        admin_required = not self.auth.admin_exists()
        user = self.database.query_one(
            "SELECT tool_user_id, username FROM tool_user WHERE status='active' LIMIT 1"
        )
        setup = self._setup_progress(user["tool_user_id"] if user else None)
        return {
            "product_version": PRODUCT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "runtime_dir": str(self.paths.root),
            "instance_id": self.instance_id,
            "database": {"ok": ok, "integrity": integrity},
            "admin_required": admin_required,
            "real_platform_writes": {
                "registered": False,
                "network_guard": "enforced",
                "database_guard": "enforced",
                "mode": "V1A_read_only",
            },
            "setup_progress": setup,
            "browser": {
                "owner": "Browser Worker",
                "headless_for_collection": True,
                "visible_for_login": True,
            },
            "collection_capacity": self.collection_capacity(),
        }

    def collection_capacity(self) -> dict[str, Any]:
        row = self.database.query_one(
            """
            SELECT COUNT(m.material_uid) AS c
            FROM material_identity m
            JOIN source_plan p
              ON p.tool_user_id=m.tool_user_id AND p.aavid=m.aavid AND p.ad_id=m.ad_id
            JOIN advertiser_account a
              ON a.tool_user_id=p.tool_user_id AND a.aavid=p.aavid
            WHERE p.monitor_enabled=1 AND a.enabled=1 AND a.removed_at IS NULL
            """
        )
        relation_count = int((row or {}).get("c") or 0)
        interval, state = collection_interval_for_relation_count(relation_count)
        disk = shutil.disk_usage(self.paths.root)
        return {
            "active_plan_material_relations": relation_count,
            "target_interval_seconds": interval,
            "state": state,
            "soft_limit": COLLECTION_RELATION_SOFT_LIMIT,
            "hard_limit": COLLECTION_RELATION_HARD_LIMIT,
            "disk_free_bytes": int(disk.free),
            "disk_state": "ready" if disk.free >= MIN_FREE_DISK_BYTES else "insufficient",
            "minimum_disk_free_bytes": MIN_FREE_DISK_BYTES,
        }

    def _require_collection_storage(self) -> None:
        capacity = self.collection_capacity()
        if capacity["disk_state"] != "ready":
            raise OSError(
                f"disk_insufficient: free={capacity['disk_free_bytes']} "
                f"required={capacity['minimum_disk_free_bytes']}"
            )

    def _setup_progress(self, tool_user_id: str | None) -> list[dict[str, Any]]:
        if not tool_user_id:
            return [
                {"key": "local_admin", "label": "创建本机管理员", "status": "required"},
                {"key": "qianchuan", "label": "千川登录及账户目录", "status": "waiting"},
                {"key": "feishu", "label": "飞书长连接和绑定", "status": "waiting"},
                {"key": "monitor", "label": "监控计划", "status": "waiting"},
                {"key": "strategy", "label": "模拟策略", "status": "waiting"},
            ]
        identity = self.database.query_one(
            "SELECT login_status FROM qianchuan_identity WHERE tool_user_id=?",
            (tool_user_id,),
        )
        account = self.database.query_one(
            "SELECT COUNT(*) AS c FROM advertiser_account WHERE tool_user_id=? AND removed_at IS NULL",
            (tool_user_id,),
        )
        feishu = self.feishu.status(tool_user_id)
        monitors = self.database.query_one(
            "SELECT COUNT(*) AS c FROM source_plan WHERE tool_user_id=? AND monitor_enabled=1",
            (tool_user_id,),
        )
        strategies = self.database.query_one(
            "SELECT COUNT(*) AS c FROM strategy WHERE tool_user_id=? AND enabled=1",
            (tool_user_id,),
        )
        return [
            {"key": "local_admin", "label": "本机管理员", "status": "complete"},
            {
                "key": "qianchuan",
                "label": "千川登录及账户目录",
                "status": "complete"
                if identity and identity["login_status"] == "authenticated" and int(account["c"]) > 0
                else "required",
            },
            {
                "key": "feishu",
                "label": "飞书长连接和绑定",
                "status": "complete"
                if feishu["credential"] == "valid" and feishu["binding"] == "bound"
                else "required",
            },
            {
                "key": "monitor",
                "label": "监控计划",
                "status": "complete" if int(monitors["c"]) > 0 else "required",
            },
            {
                "key": "strategy",
                "label": "模拟策略",
                "status": "complete" if int(strategies["c"]) > 0 else "required",
            },
        ]

    def list_accounts(self, tool_user_id: str) -> list[dict[str, Any]]:
        return self.database.query_all(
            """
            SELECT a.*,
                   COUNT(p.target_uid) AS plan_count,
                   SUM(CASE WHEN p.monitor_enabled=1 THEN 1 ELSE 0 END) AS monitored_plan_count
            FROM advertiser_account a
            LEFT JOIN source_plan p ON p.tool_user_id=a.tool_user_id AND p.aavid=a.aavid
            WHERE a.tool_user_id=? AND a.removed_at IS NULL
            GROUP BY a.account_uid
            ORDER BY a.created_at ASC
            """,
            (tool_user_id,),
        )

    def list_plans(
        self,
        tool_user_id: str,
        *,
        aavid: str | None = None,
        plan_system: str | None = None,
        promotion_scene: str | None = None,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["tool_user_id=?"]
        params: list[Any] = [tool_user_id]
        for column, value in (
            ("aavid", aavid),
            ("plan_system", plan_system),
            ("promotion_scene", promotion_scene),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if keyword:
            clauses.append("(plan_name LIKE ? OR ad_id LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        return self.database.query_all(
            "SELECT p.*, (SELECT MAX(completed_at) FROM collection_run c WHERE c.tool_user_id=p.tool_user_id AND c.target_uid=p.target_uid AND c.object_type='video_material' AND c.status='complete') AS last_successful_collection_at FROM source_plan p WHERE "
            + " AND ".join(clauses)
            + " ORDER BY aavid, plan_system, promotion_scene, plan_name",
            params,
        )

    def _job_qianchuan_login(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        return self.browser.open_visible_login_and_capture(
            context.tool_user_id,
            require_account_selection=False,
            progress=context.update_progress,
        )

    def _job_add_account(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        result = self.browser.open_visible_login_and_capture(
            context.tool_user_id,
            require_account_selection=True,
            progress=context.update_progress,
        )
        account = result.get("account")
        if not account:
            raise ValueError("未识别到用户选择的千川账户")
        existing = self.database.query_one(
            "SELECT 1 FROM advertiser_account WHERE tool_user_id=? AND aavid=? AND removed_at IS NULL",
            (context.tool_user_id, str(account["aavid"])),
        )
        account_count = self.database.query_one(
            "SELECT COUNT(*) AS c FROM advertiser_account WHERE tool_user_id=? AND removed_at IS NULL",
            (context.tool_user_id,),
        )
        if not existing and int((account_count or {}).get("c") or 0) >= MAX_ACCOUNTS:
            raise ValueError(f"V1A最多主动添加{MAX_ACCOUNTS}个千川账户")
        self._require_collection_storage()
        saved = self.collections.add_or_refresh_account(
            context.tool_user_id, str(account["aavid"])
        )
        # 账户添加完成后立即进行该账户目录刷新，不扫描全部授权账户。
        self.browser.prepare_headless(context.tool_user_id)
        catalog = self.collections.refresh_catalog(
            context.tool_user_id,
            str(account["aavid"]),
            context.update_progress,
        )
        return {"account": saved, "catalog": catalog}

    def _job_delete_account(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        aavid = str(context.payload["aavid"])
        now = utc_iso()

        def op(conn):
            with short_transaction(conn):
                row = conn.execute(
                    "SELECT account_uid FROM advertiser_account WHERE tool_user_id=? AND aavid=? AND removed_at IS NULL",
                    (context.tool_user_id, aavid),
                ).fetchone()
                if not row:
                    raise KeyError(aavid)
                conn.execute(
                    "UPDATE advertiser_account SET enabled=0, daily_report_enabled=0, removed_at=?, updated_at=? WHERE tool_user_id=? AND aavid=?",
                    (now, now, context.tool_user_id, aavid),
                )
                conn.execute(
                    "UPDATE source_plan SET monitor_enabled=0, updated_at=? WHERE tool_user_id=? AND aavid=?",
                    (now, context.tool_user_id, aavid),
                )
                conn.execute(
                    "UPDATE candidate_batch SET status='cancelled', terminal_at=?, updated_at=? WHERE tool_user_id=? AND aavid=? AND status IN ('draft','frozen','pending_approval','partially_approved')",
                    (now, now, context.tool_user_id, aavid),
                )

        self.writer.submit(op)
        return {"aavid": aavid, "removed": True}

    def _job_catalog_refresh(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        self._require_collection_storage()
        self.browser.prepare_headless(context.tool_user_id)
        return self.collections.refresh_catalog(
            context.tool_user_id,
            str(context.payload["aavid"]),
            context.update_progress,
        )

    def _job_monitor_setup_save(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        aavid = str(context.payload["aavid"])
        target_uids = [str(value) for value in context.payload.get("target_uids") or []]
        if len(set(target_uids)) > MAX_MONITORED_PLANS_PER_ACCOUNT:
            raise ValueError("每个账户最多监控10条计划")
        feishu_status = self.feishu.status(context.tool_user_id)
        enabling = bool(context.payload.get("enabled")) or bool(target_uids)
        if enabling and not (
            feishu_status["credential"] == "valid" and feishu_status["binding"] == "bound"
        ):
            raise ValueError("请先完成飞书凭据验证和授权人绑定")
        now = utc_iso()

        def op(conn):
            with short_transaction(conn):
                account = conn.execute(
                    "SELECT * FROM advertiser_account WHERE tool_user_id=? AND aavid=? AND removed_at IS NULL",
                    (context.tool_user_id, aavid),
                ).fetchone()
                if not account:
                    raise KeyError(aavid)
                if target_uids:
                    placeholders = ",".join("?" for _ in target_uids)
                    rows = conn.execute(
                        f"SELECT target_uid, monitor_eligible FROM source_plan WHERE tool_user_id=? AND aavid=? AND target_uid IN ({placeholders})",
                        [context.tool_user_id, aavid, *target_uids],
                    ).fetchall()
                    if len(rows) != len(set(target_uids)):
                        raise ValueError("监控计划包含其他账户或不存在的计划")
                    blocked = [row["target_uid"] for row in rows if int(row["monitor_eligible"]) != 1]
                    if blocked:
                        raise ValueError("计划身份、状态或证据未确认，不能参与监控")
                route_id = context.payload.get("feishu_route_id")
                if route_id:
                    route = conn.execute(
                        "SELECT 1 FROM feishu_route WHERE tool_user_id=? AND route_id=? AND enabled=1",
                        (context.tool_user_id, route_id),
                    ).fetchone()
                    if not route:
                        raise ValueError("飞书接收位置无效")
                conn.execute(
                    """
                    UPDATE advertiser_account
                    SET enabled=?, daily_report_enabled=?, feishu_route_id=?, updated_at=?
                    WHERE tool_user_id=? AND aavid=?
                    """,
                    (
                        int(bool(context.payload.get("enabled"))),
                        int(bool(context.payload.get("daily_report_enabled"))),
                        route_id,
                        now,
                        context.tool_user_id,
                        aavid,
                    ),
                )
                conn.execute(
                    "UPDATE source_plan SET monitor_enabled=0, updated_at=? WHERE tool_user_id=? AND aavid=?",
                    (now, context.tool_user_id, aavid),
                )
                if target_uids:
                    placeholders = ",".join("?" for _ in target_uids)
                    conn.execute(
                        f"UPDATE source_plan SET monitor_enabled=1, updated_at=? WHERE tool_user_id=? AND aavid=? AND target_uid IN ({placeholders})",
                        [now, context.tool_user_id, aavid, *target_uids],
                    )

        self.writer.submit(op)
        return {"aavid": aavid, "monitored_plan_count": len(set(target_uids))}

    def _job_target_collect(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        self._require_collection_storage()
        target = self.database.query_one(
            "SELECT * FROM source_plan WHERE tool_user_id=? AND target_uid=?",
            (context.tool_user_id, str(context.payload["target_uid"])),
        )
        if not target:
            raise KeyError(context.payload["target_uid"])
        self.browser.prepare_headless(context.tool_user_id)
        context.update_progress(0, 3, "采集视频素材")
        materials = self.collections.collect_materials(context.tool_user_id, target)
        context.update_progress(1, 3, "读取三类调控任务")
        control_tasks = self.collections.collect_control_tasks(context.tool_user_id, target)
        context.update_progress(2, 3, "运行规则模拟并冻结候选")
        candidate_ids: list[str] = []
        adjustment_candidate_ids: list[str] = []
        if materials.get("status") == "complete":
            candidate_ids = self.candidates.generate_for_target(
                context.tool_user_id, str(target["target_uid"])
            )
            adjustment_candidate_ids = self.candidates.generate_adjustments_for_target(
                context.tool_user_id, str(target["target_uid"])
            )
            if self.feishu.status(context.tool_user_id)["sending"] == "ready":
                for candidate_id in candidate_ids:
                    self.feishu.enqueue_candidate_preview(
                        context.tool_user_id, candidate_id
                    )
                for adjustment_candidate_id in adjustment_candidate_ids:
                    self.feishu.enqueue_adjustment_preview(
                        context.tool_user_id, adjustment_candidate_id
                    )
        context.update_progress(3, 3, "只读采集和模拟完成")
        return {
            "materials": materials,
            "control_tasks": control_tasks,
            "candidate_batch_ids": candidate_ids,
            "adjustment_candidate_ids": adjustment_candidate_ids,
        }

    def _job_strategy_save(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        payload = dict(context.payload)
        strategy_id = self.strategies.save(
            tool_user_id=context.tool_user_id,
            target_uid=str(payload["target_uid"]),
            title=str(payload["title"]),
            priority=int(payload["priority"]),
            trigger_level=str(payload.get("trigger_level") or "material"),
            trigger=dict(payload["trigger"]),
            strategy_type=str(payload.get("strategy_type") or "retarget_create"),
            action_params=dict(payload.get("action_params") or {}),
            enabled=bool(payload.get("enabled")),
            cooldown_minutes=int(payload.get("cooldown_minutes") or 30),
            strategy_id=str(payload["strategy_id"]) if payload.get("strategy_id") else None,
        )
        return {"strategy_id": strategy_id}

    def _job_strategy_toggle(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        self.strategies.set_enabled(
            context.tool_user_id,
            str(context.payload["strategy_id"]),
            bool(context.payload["enabled"]),
        )
        return {"strategy_id": context.payload["strategy_id"], "enabled": bool(context.payload["enabled"])}

    def _job_strategy_reorder(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        ids = [str(value) for value in context.payload.get("strategy_ids") or []]
        self.strategies.reorder(context.tool_user_id, ids)
        return {"strategy_ids": ids}

    def _job_candidate_generate(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        ids = self.candidates.generate_for_target(
            context.tool_user_id, str(context.payload["target_uid"])
        )
        return {"candidate_batch_ids": ids}

    def _job_candidate_group_save(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        ids = self.candidates.save_groups(
            context.tool_user_id,
            str(context.payload["candidate_batch_id"]),
            list(context.payload.get("groups") or []),
        )
        return {"group_uids": ids, "dry_run": True}

    def _job_candidate_preview_send(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        ids = self.feishu.enqueue_candidate_preview(
            context.tool_user_id, str(context.payload["candidate_batch_id"])
        )
        delivered = self.feishu.deliver_outbox_once(context.tool_user_id)
        return {"outbox_ids": ids, "delivered": delivered}

    def _job_feishu_reconnect(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        self.feishu.start_long_connection(context.tool_user_id)
        return {"status": self.feishu.status(context.tool_user_id)}

    def _job_feishu_credentials_test(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        return self.feishu.test_credentials(context.tool_user_id)

    def _job_feishu_binding_code(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        code = self.feishu.issue_binding_code(
            context.tool_user_id, str(context.payload["purpose"])
        )
        return {"code": code, "expires_in_seconds": 600}

    def _job_feishu_test_send(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        profile, secret = self.feishu._profile_with_secret(context.tool_user_id)
        from .feishu import FeishuApiClient

        client = FeishuApiClient(str(profile["app_id"]), secret)
        route = self.database.query_one(
            "SELECT * FROM feishu_route WHERE tool_user_id=? AND route_name='管理员默认位置'",
            (context.tool_user_id,),
        )
        if not route or not route.get("personal_open_id"):
            raise ValueError("请先完成个人绑定")
        card = {
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": "V1A 飞书链路测试"}},
            "elements": [
                {"tag": "markdown", "content": "凭据、发送链路已通过。事件接收状态需要在飞书中点击或发送绑定消息后单独确认。\n\n**V1A只读：不会执行千川操作。**"}
            ],
        }
        message_id = client.send_card("open_id", str(route["personal_open_id"]), card)
        return {"message_id": message_id}

    def _job_migration_scan(self, context: JobContext) -> dict[str, Any]:
        roots = [str(value) for value in context.payload.get("roots") or []]
        return {"sources": self.migrations.scan(roots)}

    def _job_migration_execute(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        return self.migrations.migrate(
            context.tool_user_id, str(context.payload["source_uid"])
        )

    def _job_migration_restore(self, context: JobContext) -> dict[str, Any]:
        return self.migrations.restore_pre_migration_snapshot(
            str(context.payload["migration_uid"])
        )

    def _job_operation_log_sync(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        self._require_collection_storage()
        aavid = str(context.payload["aavid"])
        end = utc_now().astimezone(BEIJING)
        first = self.database.query_one(
            "SELECT 1 FROM collection_run WHERE tool_user_id=? AND aavid=? AND object_type='operation_log' LIMIT 1",
            (context.tool_user_id, aavid),
        )
        start = end - (timedelta(minutes=10) if first else timedelta(days=30))
        self.browser.prepare_headless(context.tool_user_id)
        return self.collections.collect_operation_logs(
            context.tool_user_id,
            aavid,
            start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _job_daily_report_send(self, context: JobContext) -> dict[str, Any]:
        assert context.tool_user_id
        report_date = str(context.payload.get("business_date") or business_date(utc_now() - timedelta(days=1)))
        if self.feishu.status(context.tool_user_id)["sending"] != "ready":
            raise ValueError("飞书发送链路不可用")
        default_route = self.database.query_one(
            "SELECT * FROM feishu_route WHERE tool_user_id=? AND route_name='管理员默认位置' AND enabled=1",
            (context.tool_user_id,),
        )
        if not default_route:
            raise ValueError("未配置管理员默认接收位置")
        outbox_ids: list[str] = []
        overview = self.reports.daily_summary(context.tool_user_id, report_date)
        overview_uid = self.reports.record_delivery(
            context.tool_user_id, report_date, str(default_route["route_id"]), overview
        )
        outbox_ids.extend(
            self.feishu.enqueue_daily_report(
                context.tool_user_id, overview_uid, overview, default_route
            )
        )
        accounts = self.database.query_all(
            "SELECT * FROM advertiser_account WHERE tool_user_id=? AND daily_report_enabled=1 AND removed_at IS NULL",
            (context.tool_user_id,),
        )
        for account in accounts:
            summary = self.reports.daily_summary(
                context.tool_user_id, report_date, str(account["aavid"])
            )
            completeness = summary.get("platform_log_completeness") or {}
            has_activity = bool(
                summary["real_platform_operations"]["total"]
                or summary["simulation_candidates"]["total"]
            )
            incomplete = not completeness or any(value != "complete" for value in completeness.values())
            if not has_activity and not incomplete:
                continue
            route = default_route
            if account.get("feishu_route_id"):
                route = self.database.query_one(
                    "SELECT * FROM feishu_route WHERE tool_user_id=? AND route_id=? AND enabled=1",
                    (context.tool_user_id, account["feishu_route_id"]),
                ) or default_route
            report_uid = self.reports.record_delivery(
                context.tool_user_id,
                report_date,
                str(route["route_id"]),
                summary,
                aavid=str(account["aavid"]),
            )
            outbox_ids.extend(
                self.feishu.enqueue_daily_report(
                    context.tool_user_id,
                    report_uid,
                    summary,
                    route,
                    account_name=str(account["account_name"]),
                )
            )
        delivered = self.feishu.deliver_outbox_once(context.tool_user_id)
        return {"business_date": report_date, "outbox_ids": outbox_ids, "delivered": delivered}

    def close(self) -> None:
        self.scheduler.stop()
        self.feishu.stop_long_connection()
        self.worker.stop()
        self.sessions.revoke_all()
        self.writer.close()


class V1AScheduler:
    """仅调度用户明确启用的计划；所有工作仍进入持久化队列。"""

    def __init__(self, runtime: RuntimeContext):
        self.runtime = runtime
        self._stop = threading.Event()
        self._last_tick = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name="qcsckp-v1a-scheduler",
            daemon=True,
        )

    def start(self) -> "V1AScheduler":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(30):
            try:
                self._handle_resume(time.monotonic())
                self._enqueue_due_catalogs()
                self._enqueue_due_targets()
                self._enqueue_operation_logs()
                self._enqueue_daily_reports()
                self._deliver_outbox()
            except Exception as exc:
                self.runtime.events.publish(
                    {"type": "scheduler.error", "error_code": type(exc).__name__}
                )

    def _handle_resume(self, current_tick: float) -> bool:
        gap = max(0.0, current_tick - self._last_tick)
        self._last_tick = current_tick
        if gap <= 90:
            return False
        details = {"sleep_gap_seconds": round(gap, 3)}
        self.runtime.writer.execute(
            "INSERT INTO system_event(event_uid, severity, event_type, details_json, created_at) VALUES(?, 'warning', 'windows_resume_detected', ?, ?)",
            (
                f"system_{uuid.uuid4().hex}",
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                utc_iso(),
            ),
        )
        self.runtime.events.publish({"type": "system.resume_detected", **details})
        return True

    def _enqueue_due_targets(self) -> None:
        interval_seconds = int(
            self.runtime.collection_capacity()["target_interval_seconds"]
        )
        targets = self.runtime.database.query_all(
            """
            SELECT p.* FROM source_plan p
            JOIN advertiser_account a ON a.tool_user_id=p.tool_user_id AND a.aavid=p.aavid
            WHERE p.monitor_enabled=1 AND p.monitor_eligible=1
              AND a.enabled=1 AND a.removed_at IS NULL
            """
        )
        for target in targets:
            if not self._authenticated(str(target["tool_user_id"])):
                continue
            active = self.runtime.database.query_one(
                """
                SELECT 1 FROM background_job
                WHERE job_type='target_collect' AND status IN ('queued','running')
                  AND json_extract(payload_json, '$.target_uid')=?
                LIMIT 1
                """,
                (target["target_uid"],),
            )
            if active:
                continue
            last = self.runtime.database.query_one(
                """
                SELECT completed_at FROM collection_run
                WHERE tool_user_id=? AND target_uid=? AND object_type='video_material'
                ORDER BY started_at DESC, rowid DESC LIMIT 1
                """,
                (target["tool_user_id"], target["target_uid"]),
            )
            if last and last.get("completed_at"):
                try:
                    completed = __import__("datetime").datetime.fromisoformat(
                        str(last["completed_at"]).replace("Z", "+00:00")
                    )
                    if (__import__("datetime").datetime.now(__import__("datetime").timezone.utc) - completed).total_seconds() < interval_seconds:
                        continue
                except Exception:
                    pass
            self.runtime.jobs.create(
                "target_collect",
                {"target_uid": target["target_uid"]},
                tool_user_id=target["tool_user_id"],
                priority=40,
            )

    def _authenticated(self, tool_user_id: str) -> bool:
        row = self.runtime.database.query_one(
            "SELECT login_status FROM qianchuan_identity WHERE tool_user_id=?",
            (tool_user_id,),
        )
        return bool(row and row["login_status"] == "authenticated")

    def _job_active(self, job_type: str, field: str, value: str) -> bool:
        row = self.runtime.database.query_one(
            f"""
            SELECT 1 FROM background_job
            WHERE job_type=? AND status IN ('queued','running')
              AND json_extract(payload_json, '$.{field}')=? LIMIT 1
            """,
            (job_type, value),
        )
        return bool(row)

    def _enqueue_due_catalogs(self) -> None:
        accounts = self.runtime.database.query_all(
            "SELECT * FROM advertiser_account WHERE removed_at IS NULL"
        )
        now = utc_now()
        for account in accounts:
            user_id = str(account["tool_user_id"])
            if not self._authenticated(user_id) or self._job_active("catalog_refresh", "aavid", str(account["aavid"])):
                continue
            completed = account.get("catalog_completed_at")
            if completed:
                try:
                    parsed = __import__("datetime").datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
                    if (now - parsed).total_seconds() < 1800:
                        continue
                except Exception:
                    pass
            self.runtime.jobs.create(
                "catalog_refresh", {"aavid": account["aavid"]}, tool_user_id=user_id, priority=45
            )

    def _enqueue_operation_logs(self) -> None:
        accounts = self.runtime.database.query_all(
            "SELECT * FROM advertiser_account WHERE daily_report_enabled=1 AND removed_at IS NULL"
        )
        for account in accounts:
            user_id = str(account["tool_user_id"])
            aavid = str(account["aavid"])
            if not self._authenticated(user_id) or self._job_active("operation_log_sync", "aavid", aavid):
                continue
            last = self.runtime.database.query_one(
                "SELECT completed_at FROM collection_run WHERE tool_user_id=? AND aavid=? AND object_type='operation_log' ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (user_id, aavid),
            )
            if last and last.get("completed_at"):
                try:
                    parsed = __import__("datetime").datetime.fromisoformat(str(last["completed_at"]).replace("Z", "+00:00"))
                    if (utc_now() - parsed).total_seconds() < 300:
                        continue
                except Exception:
                    pass
            self.runtime.jobs.create(
                "operation_log_sync", {"aavid": aavid}, tool_user_id=user_id, priority=50
            )

    def _enqueue_daily_reports(self) -> None:
        now = utc_now().astimezone(BEIJING)
        if now.hour < 9:
            return
        report_date = (now.date() - timedelta(days=1)).isoformat()
        users = self.runtime.database.query_all("SELECT tool_user_id FROM tool_user WHERE status='active'")
        for user in users:
            user_id = str(user["tool_user_id"])
            existing = self.runtime.database.query_one(
                "SELECT 1 FROM daily_report_delivery WHERE tool_user_id=? AND aavid IS NULL AND business_date=? LIMIT 1",
                (user_id, report_date),
            )
            if existing or self._job_active("daily_report_send", "business_date", report_date):
                continue
            self.runtime.jobs.create(
                "daily_report_send",
                {"business_date": report_date},
                tool_user_id=user_id,
                priority=60,
            )

    def _deliver_outbox(self) -> None:
        users = self.runtime.database.query_all(
            "SELECT tool_user_id FROM tool_user WHERE status='active'"
        )
        for user in users:
            try:
                self.runtime.feishu.deliver_outbox_once(str(user["tool_user_id"]))
            except Exception:
                continue
