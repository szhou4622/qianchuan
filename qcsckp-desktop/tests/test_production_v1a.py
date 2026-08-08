import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from production_v1a.adapters import AdapterRegistry
from production_v1a.adapters.models import (
    AccountIdentity,
    NormalizedControlTask,
    NormalizedMaterial,
    PageResult,
    PaginatedResult,
)
from production_v1a.auth import LocalAdminService
from production_v1a.browser_worker import LoginRequired, PlaywrightBrowserWorker
from production_v1a.candidates import CandidateBlocked, CandidateService
from production_v1a.collections import CollectionService, _classify_operation, material_uid
from production_v1a.feishu import (
    FeishuError,
    FeishuService,
    build_adjustment_preview_card,
    build_candidate_preview_card,
)
from production_v1a.leases import LeaseManager, StaleFencingToken
from production_v1a.jobs import EventBus, JobService
from production_v1a.migration import LegacyMigrationService
from production_v1a.reports import OperationReportService
from production_v1a.runtime_paths import RuntimePaths
from production_v1a.security import (
    PlatformNetworkGuard,
    PlatformWriteBlocked,
    sanitize_exception_text,
)
from production_v1a.service_main import start_service
from production_v1a.single_instance import mutex_name
from production_v1a.runtime import V1AScheduler, collection_interval_for_relation_count
from production_v1a.storage import RuntimeDatabase, StorageWriter
from production_v1a.strategies import StrategyService
from production_v1a.timeutils import beijing_iso, business_date, utc_iso


class EmptyTransport:
    def request(self, *_args, **_kwargs):
        raise AssertionError("unexpected transport call")


class TestDatabase:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qcsckp-v1a-test-")
        self.paths = RuntimePaths.from_root(self.tmp.name).ensure()
        self.database = RuntimeDatabase(self.paths)
        self.writer = StorageWriter(self.database).start()
        self.user = "user_test"
        now = utc_iso()
        self.writer.execute(
            "INSERT INTO tool_user(tool_user_id, username, password_salt, password_hash, password_iterations, recovery_code_hash, status, created_at, updated_at) VALUES(?, 'tester', '00', '00', 1, '00', 'active', ?, ?)",
            (self.user, now, now),
        )
        self.writer.execute(
            "INSERT INTO qianchuan_identity(login_identity_id, tool_user_id, login_status, profile_path, created_at, updated_at) VALUES('identity_test', ?, 'authenticated', ?, ?, ?)",
            (self.user, str(self.paths.browser_profile_dir), now, now),
        )
        self.writer.execute(
            "INSERT INTO feishu_profile(profile_uid, tool_user_id, created_at, updated_at) VALUES('feishu_test', ?, ?, ?)",
            (self.user, now, now),
        )

    def seed_target(self, *, aavid="1001", ad_id="2001", scene="product", system="chengfang"):
        now = utc_iso()
        target = f"target_{aavid}_{ad_id}"
        self.writer.execute(
            "INSERT OR IGNORE INTO advertiser_account(account_uid, tool_user_id, aavid, account_name, enabled, catalog_status, catalog_completed_at, created_at, updated_at) VALUES(?, ?, ?, ?, 1, 'complete', ?, ?, ?)",
            (f"account_{aavid}", self.user, aavid, f"账户{aavid}", now, now, now),
        )
        self.writer.execute(
            """
            INSERT INTO source_plan(target_uid, tool_user_id, aavid, ad_id, plan_name,
                plan_system, promotion_scene, platform_status, verification_state,
                catalog_seen_at, monitor_enabled, monitor_eligible, retarget_eligible, pause_eligible,
                adjust_eligible, adapter_version, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, 'active', 'verified', ?, 1, 1, 0, 0, 0, 'test-v1', ?, ?)
            """,
            (target, self.user, aavid, ad_id, f"计划{ad_id}", system, scene, now, now, now),
        )
        return target

    def seed_material_batch(self, target, materials, run_suffix=""):
        plan = self.database.query_one("SELECT * FROM source_plan WHERE target_uid=?", (target,))
        run = "collect_test_" + target + str(run_suffix)
        now = utc_iso()
        self.writer.execute(
            """
            INSERT INTO collection_run(collection_run_id, tool_user_id, aavid, target_uid,
                object_type, business_date, filters_hash, successful_pages, raw_count,
                unique_count, started_at, completed_at, adapter_version, status)
            VALUES(?, ?, ?, ?, 'video_material', ?, 'test', 1, ?, ?, ?, ?, 'test-v1', 'complete')
            """,
            (run, self.user, plan["aavid"], target, business_date(), len(materials), len(materials), now, now),
        )
        CollectionService(self.database, self.writer, AdapterRegistry(EmptyTransport()))._persist_material_batch(
            self.user, plan, run, materials
        )
        return run

    def close(self):
        self.writer.close()
        self.tmp.cleanup()


class ProductionV1ASafetyTests(unittest.TestCase):
    def setUp(self):
        self.fx = TestDatabase()

    def tearDown(self):
        self.fx.close()

    def test_exception_text_is_redacted_before_persistence(self):
        message = sanitize_exception_text(
            "Authorization: Bearer top-secret app_secret=hidden access_token=token123"
        )
        self.assertNotIn("top-secret", message)
        self.assertNotIn("hidden", message)
        self.assertNotIn("token123", message)
        self.assertGreaterEqual(message.count("[REDACTED]"), 3)

    def test_sqlite_guards_wal_foreign_keys_and_real_execution(self):
        conn = self.fx.database.connect()
        try:
            self.assertEqual("wal", conn.execute("PRAGMA journal_mode").fetchone()[0].lower())
            self.assertEqual(1, conn.execute("PRAGMA foreign_keys").fetchone()[0])
        finally:
            conn.close()
        target = self.fx.seed_target()
        with self.assertRaises(sqlite3.IntegrityError) as raised:
            self.fx.writer.execute(
                "INSERT INTO execution_task(execution_uid, tool_user_id, aavid, target_uid, operation_type, idempotency_key, status, created_at, updated_at) VALUES('real', ?, '1001', ?, 'retarget_create', 'real-key', 'submitting', ?, ?)",
                (self.fx.user, target, utc_iso(), utc_iso()),
            )
        self.assertIn("V1A_REAL_EXECUTION_BLOCKED", str(raised.exception))

    def test_network_guard_is_fail_closed(self):
        guard = PlatformNetworkGuard()
        guard.assert_allowed("GET", "/ad/api/v1/account/user/info")
        with self.assertRaises(PlatformWriteBlocked):
            guard.assert_allowed("POST", "/ad/api/v1/control/create")
        with self.assertRaises(PlatformWriteBlocked):
            guard.assert_allowed("DELETE", "/ad/api/pmc/v1/ad/get_opt_log")

    def test_material_normalization_requires_positive_video_evidence(self):
        adapter = AdapterRegistry(EmptyTransport()).get("chengfang", "product")
        base = {
            "material_id": "m1",
            "roi2_material_status": 1,
            "roi2_material_show_status": 1,
            "audit_status": 1,
            "block_status": 0,
        }
        self.assertIsNone(
            adapter.normalize_material(
                {**base, "material_type": "image"}, "1001", "2001"
            )
        )
        self.assertIsNone(adapter.normalize_material(base, "1001", "2001"))
        normalized = adapter.normalize_material(
            {**base, "roi2_material_video_type": 2}, "1001", "2001"
        )
        self.assertIsNotNone(normalized)
        self.assertEqual("m1", normalized.material_id)

    def test_lease_fencing_rejects_old_owner(self):
        manager = LeaseManager(self.fx.database, self.fx.writer)
        first = manager.acquire("browser-worker", "one", "task1", 10, ttl_seconds=0)
        self.assertIsNotNone(first)
        second = manager.acquire("browser-worker", "two", "task2", 10, ttl_seconds=30)
        self.assertIsNotNone(second)
        self.assertGreater(second.fencing_token, first.fencing_token)
        with self.assertRaises(StaleFencingToken):
            manager.assert_current(first)

    def test_capability_matrix_has_four_adapters_and_no_enabled_write(self):
        registry = AdapterRegistry(EmptyTransport())
        rows = registry.capability_matrix()
        self.assertEqual(4, len(rows))
        self.assertEqual(
            {("global", "product"), ("global", "live"), ("chengfang", "product"), ("chengfang", "live")},
            {(row["plan_system"], row["promotion_scene"]) for row in rows},
        )
        self.assertFalse(any(cap["enabled"] for row in rows for cap in row["write_capabilities"].values()))
        for adapter in registry.all():
            with self.assertRaises(PlatformWriteBlocked):
                adapter.create_retarget({}, "blocked")
            with self.assertRaises(PlatformWriteBlocked):
                adapter.pause_control_task({}, "blocked")
            with self.assertRaises(PlatformWriteBlocked):
                adapter.adjust_control_task({}, "blocked")

    def test_adapter_schema_evidence_is_persisted_without_raw_payload(self):
        adapter = AdapterRegistry(EmptyTransport()).all()[0]
        service = CollectionService(self.fx.database, self.fx.writer, AdapterRegistry(EmptyTransport()))
        result = PaginatedResult(
            rows=(),
            platform_total_count=0,
            expected_pages=0,
            successful_pages=1,
            failed_pages=(),
            raw_count=0,
            unique_count=0,
            duplicate_count=0,
            status="complete",
            platform_server_time=None,
            response_schema_hashes=("schema_hash_1",),
        )
        service._record_adapter_evidence(
            adapter,
            result,
            endpoint_path=adapter.PLAN_ENDPOINT,
            dataset_key=adapter.plan_dataset,
            capability_name="read_catalog",
        )
        row = self.fx.database.query_one(
            "SELECT * FROM adapter_evidence WHERE response_schema_hash='schema_hash_1'"
        )
        self.assertIsNotNone(row)
        self.assertEqual(adapter.adapter_name, row["adapter_name"])
        self.assertNotIn("raw_payload", row)

    def test_suspicious_empty_catalog_keeps_last_complete_plan(self):
        target = self.fx.seed_target(aavid="3001", ad_id="4001")

        class EmptyAdapter:
            plan_system = "global"
            promotion_scene = "product"
            plan_dataset = "test_dataset"
            mar_goal = "PRODUCT"
            adapter_version = "empty-v1"
            adapter_name = "empty_adapter"
            read_capability_state = "read_verified"
            evidence_level = "A"
            PLAN_ENDPOINT = "/read/catalog"

            def fetch_account_identity(self, aavid):
                return AccountIdentity(str(aavid), "权威账户名")

            def discover_plans(self, _aavid):
                return PaginatedResult(
                    rows=(), platform_total_count=0, expected_pages=0,
                    successful_pages=1, failed_pages=(), raw_count=0,
                    unique_count=0, duplicate_count=0, status="complete",
                    platform_server_time=None,
                    response_schema_hashes=("empty_schema",),
                )

        class EmptyRegistry:
            def __init__(self):
                self.adapters = [EmptyAdapter() for _ in range(4)]

            def all(self):
                return self.adapters

        result = CollectionService(
            self.fx.database, self.fx.writer, EmptyRegistry()
        ).refresh_catalog(self.fx.user, "3001")
        self.assertEqual("suspicious_empty", result["status"])
        preserved = self.fx.database.query_one(
            "SELECT target_uid FROM source_plan WHERE target_uid=?", (target,)
        )
        self.assertIsNotNone(preserved)

    def test_product_rule_keeps_all_eligible_videos_and_priority_claims_once(self):
        target = self.fx.seed_target()
        materials = [
            NormalizedMaterial("1001", "2001", "m1", "视频1", "2026-08-01T01:00:00+00:00", "1", "1", None, "1", "0", True, True, ("p1",), 10000, 2, 30000, "3", {}),
            NormalizedMaterial("1001", "2001", "m2", "视频2", "2026-08-01T02:00:00+00:00", "1", "1", None, "1", "0", True, True, ("p1",), 5000, 1, 20000, "4", {}),
        ]
        self.fx.seed_material_batch(target, materials)
        strategies = StrategyService(self.fx.database, self.fx.writer)
        high = strategies.save(tool_user_id=self.fx.user, target_uid=target, title="商品优先", priority=1, trigger_level="product", trigger={"conditions": [{"metric": "order_count", "operator": "gte", "value": 3}]}, enabled=True)
        strategies.save(tool_user_id=self.fx.user, target_uid=target, title="素材后备", priority=2, trigger_level="material", trigger={"conditions": [{"metric": "spend_cent", "operator": "gte", "value": 1}]}, enabled=True)
        batches = CandidateService(self.fx.database, self.fx.writer).generate_for_target(self.fx.user, target)
        self.assertEqual(1, len(batches))
        batch = self.fx.database.query_one("SELECT * FROM candidate_batch WHERE candidate_batch_id=?", (batches[0],))
        self.assertEqual(high, batch["strategy_id"])
        frozen = json.loads(batch["material_snapshot_json"])
        self.assertEqual(["m1", "m2"], [item["material_id"] for item in frozen])

    def test_unchanged_new_collection_does_not_duplicate_active_candidate(self):
        target = self.fx.seed_target()
        materials = [
            NormalizedMaterial("1001", "2001", "m1", "视频1", None, "1", "1", None, "1", "0", True, True, (), 100, 1, 300, "3", {})
        ]
        self.fx.seed_material_batch(target, materials, "_first")
        StrategyService(self.fx.database, self.fx.writer).save(
            tool_user_id=self.fx.user,
            target_uid=target,
            title="去重",
            priority=1,
            trigger_level="material",
            trigger={"conditions": [{"metric": "order_count", "operator": "gte", "value": 1}]},
            enabled=True,
        )
        service = CandidateService(self.fx.database, self.fx.writer)
        first = service.generate_for_target(self.fx.user, target)
        self.fx.seed_material_batch(target, materials, "_second")
        second = service.generate_for_target(self.fx.user, target)
        self.assertEqual(first, second)
        self.assertEqual(
            1,
            self.fx.database.query_one("SELECT COUNT(*) AS c FROM candidate_batch")["c"],
        )

    def test_scene2_adjustment_simulation_uses_highest_priority_strategy(self):
        target = self.fx.seed_target()
        materials = [
            NormalizedMaterial("1001", "2001", "m1", "视频1", None, "1", "1", None, "1", "0", True, True, (), 10_000, 2, 30_000, "3", {})
        ]
        self.fx.seed_material_batch(target, materials)
        now = utc_iso()
        run_id = "control_scene2_test"
        self.fx.writer.execute(
            """
            INSERT INTO collection_run(collection_run_id, tool_user_id, aavid, target_uid,
                object_type, business_date, filters_hash, successful_pages, raw_count,
                unique_count, started_at, completed_at, adapter_version, status)
            VALUES(?, ?, '1001', ?, 'control_task:scene_2', ?, 'test', 1, 1, 1, ?, ?, 'test-v1', 'complete')
            """,
            (run_id, self.fx.user, target, business_date(), now, now),
        )
        task = NormalizedControlTask(
            "1001", "2001", "control1", "素材追投1", 2, "volume", ("m1",),
            "running", "total", 10000, 2000, "24", now, now, "3", now, "revision1"
        )
        collection = CollectionService(
            self.fx.database, self.fx.writer, AdapterRegistry(EmptyTransport())
        )
        plan = self.fx.database.query_one("SELECT * FROM source_plan WHERE target_uid=?", (target,))
        collection._persist_control_tasks(self.fx.user, plan, run_id, [task])
        strategies = StrategyService(self.fx.database, self.fx.writer)
        strategies.save(
            tool_user_id=self.fx.user,
            target_uid=target,
            title="后备暂停",
            priority=2,
            strategy_type="retarget_pause",
            trigger_level="material",
            trigger={"conditions": [{"metric": "spend_cent", "operator": "gte", "value": 1}]},
            enabled=True,
        )
        winning = strategies.save(
            tool_user_id=self.fx.user,
            target_uid=target,
            title="优先调整",
            priority=1,
            strategy_type="retarget_adjust",
            trigger_level="material",
            trigger={"conditions": [{"metric": "roi_decimal", "operator": "gte", "value": 2}]},
            action_params={"budget_delta_cent": 1000, "duration_delta_hours": "1.5"},
            enabled=True,
        )
        candidates = CandidateService(self.fx.database, self.fx.writer).generate_adjustments_for_target(
            self.fx.user, target
        )
        self.assertEqual(1, len(candidates))
        row = self.fx.database.query_one(
            "SELECT * FROM adjustment_candidate WHERE adjustment_candidate_id=?",
            (candidates[0],),
        )
        self.assertNotEqual(winning, row["strategy_id"])
        self.assertEqual("retarget_pause", row["action_type"])
        self.assertEqual(0, row["budget_delta_cent"])
        self.assertEqual("0", row["duration_delta_hours_decimal"])
        self.assertEqual(0, self.fx.database.query_one("SELECT COUNT(*) AS c FROM execution_task")["c"])
        self.fx.writer.execute(
            """
            INSERT INTO collection_run(collection_run_id, tool_user_id, aavid, target_uid,
                object_type, business_date, filters_hash, started_at, completed_at,
                adapter_version, status)
            VALUES('control_scene2_partial', ?, '1001', ?, 'control_task:scene_2', ?,
                   'newer', ?, ?, 'test-v1', 'partial')
            """,
            (self.fx.user, target, business_date(), now, now),
        )
        with self.assertRaisesRegex(CandidateBlocked, "latest_scene2_batch_not_complete"):
            CandidateService(self.fx.database, self.fx.writer).generate_adjustments_for_target(
                self.fx.user, target
            )

    def test_multiple_overlapping_groups_remain_dry_run(self):
        target = self.fx.seed_target()
        materials = [NormalizedMaterial("1001", "2001", f"m{i}", f"视频{i}", None, "1", "1", None, "1", "0", True, True, (), 100, i, i * 100, str(i), {}) for i in range(1, 5)]
        self.fx.seed_material_batch(target, materials)
        strategies = StrategyService(self.fx.database, self.fx.writer)
        strategies.save(tool_user_id=self.fx.user, target_uid=target, title="全部", priority=1, trigger_level="material", trigger={"conditions": [{"metric": "spend_cent", "operator": "gte", "value": 1}]}, enabled=True)
        service = CandidateService(self.fx.database, self.fx.writer)
        batch = service.generate_for_target(self.fx.user, target)[0]
        saved = service.save_groups(
            self.fx.user,
            batch,
            [
                {"mode": "selected_group", "material_ids": ["m1", "m2"]},
                {"mode": "selected_group", "material_ids": ["m2", "m3"]},
                {"mode": "all_group", "material_ids": ["m1", "m2", "m3", "m4"]},
            ],
        )
        self.assertEqual(3, len(saved))
        self.assertEqual(0, self.fx.database.query_one("SELECT COUNT(*) c FROM execution_task")["c"])
        self.assertEqual(
            {"frozen"},
            {row["status"] for row in self.fx.database.query_all("SELECT status FROM retarget_group")},
        )
        self.assertEqual(
            "grouped",
            self.fx.database.query_one(
                "SELECT status FROM candidate_batch WHERE candidate_batch_id=?", (batch,)
            )["status"],
        )
        self.fx.writer.execute(
            "UPDATE candidate_batch SET status='pending_approval' WHERE candidate_batch_id=?",
            (batch,),
        )
        confirmed = service.confirm_groups(
            self.fx.user, batch, authorized_by_open_id="ou_admin"
        )
        self.assertEqual(3, len(confirmed))
        statuses = {row["status"] for row in self.fx.database.query_all("SELECT status FROM execution_task")}
        self.assertEqual({"dry_run_succeeded"}, statuses)
        self.assertEqual(3, self.fx.database.query_one("SELECT COUNT(*) c FROM operation_event WHERE source='simulation'")["c"])
        self.assertEqual(
            confirmed,
            service.confirm_groups(
                self.fx.user, batch, authorized_by_open_id="ou_admin"
            ),
        )
        self.assertEqual(3, self.fx.database.query_one("SELECT COUNT(*) c FROM execution_task")["c"])

    def test_desktop_group_resave_atomically_replaces_frozen_snapshot(self):
        target = self.fx.seed_target()
        materials = [
            NormalizedMaterial("1001", "2001", f"m{i}", f"视频{i}", None, "1", "1", None, "1", "0", True, True, (), 100, 1, 100, "1", {})
            for i in range(1, 4)
        ]
        self.fx.seed_material_batch(target, materials)
        StrategyService(self.fx.database, self.fx.writer).save(
            tool_user_id=self.fx.user,
            target_uid=target,
            title="全部",
            priority=1,
            trigger_level="material",
            trigger={"conditions": [{"metric": "spend_cent", "operator": "gte", "value": 1}]},
            enabled=True,
        )
        service = CandidateService(self.fx.database, self.fx.writer)
        batch = service.generate_for_target(self.fx.user, target)[0]
        service.save_groups(
            self.fx.user,
            batch,
            [
                {"mode": "selected_group", "material_ids": ["m1"]},
                {"mode": "selected_group", "material_ids": ["m2"]},
            ],
        )
        service.save_groups(
            self.fx.user,
            batch,
            [{"mode": "selected_group", "material_ids": ["m3"]}],
        )
        rows = self.fx.database.query_all(
            "SELECT sequence, material_ids_json FROM retarget_group WHERE candidate_batch_id=?",
            (batch,),
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(["m3"], json.loads(rows[0]["material_ids_json"]))

    def test_expired_candidates_and_unsent_cards_are_closed_persistently(self):
        target = self.fx.seed_target()
        materials = [NormalizedMaterial("1001", "2001", "m1", "视频1", None, "1", "1", None, "1", "0", True, True, (), 100, 1, 100, "1", {})]
        self.fx.seed_material_batch(target, materials)
        StrategyService(self.fx.database, self.fx.writer).save(
            tool_user_id=self.fx.user,
            target_uid=target,
            title="全部",
            priority=1,
            trigger_level="material",
            trigger={"conditions": [{"metric": "spend_cent", "operator": "gte", "value": 1}]},
            enabled=True,
        )
        service = CandidateService(self.fx.database, self.fx.writer)
        batch = service.generate_for_target(self.fx.user, target)[0]
        service.save_groups(
            self.fx.user,
            batch,
            [{"mode": "selected_group", "material_ids": ["m1"]}],
        )
        past = "2000-01-01T00:00:00+00:00"
        self.fx.writer.execute(
            "UPDATE candidate_batch SET status='pending_approval', expires_at=? WHERE candidate_batch_id=?",
            (past, batch),
        )
        self.fx.writer.execute(
            "INSERT INTO feishu_outbox(outbox_id, tool_user_id, route_id, task_uid, payload_json, status, created_at, updated_at) VALUES('outbox_expired', ?, 'open_id:ou', ?, '{}', 'queued', ?, ?)",
            (self.fx.user, batch, utc_iso(), utc_iso()),
        )
        self.assertEqual(
            {"candidate_batches": 1, "adjustment_candidates": 0},
            service.expire_due_candidates(self.fx.user),
        )
        self.assertEqual(
            "expired",
            self.fx.database.query_one(
                "SELECT status FROM candidate_batch WHERE candidate_batch_id=?", (batch,)
            )["status"],
        )
        self.assertEqual(
            "cancelled",
            self.fx.database.query_one(
                "SELECT status FROM feishu_outbox WHERE outbox_id='outbox_expired'"
            )["status"],
        )

    def test_feishu_inbox_deduplicates_and_rejects_other_user(self):
        service = FeishuService(self.fx.database, self.fx.writer, CandidateService(self.fx.database, self.fx.writer))
        code = service.issue_binding_code(self.fx.user, "personal")
        result = service.process_message(self.fx.user, event_id="evt1", sender_open_id="ou_admin", chat_id="chat1", chat_type="p2p", text=f"绑定 {code}")
        self.assertEqual("personal_bound", result["action"])
        replay = service.process_message(self.fx.user, event_id="evt1", sender_open_id="ou_admin", chat_id="chat1", chat_type="p2p", text=f"绑定 {code}")
        self.assertEqual("duplicate_event", replay["reason"])
        with self.assertRaises(FeishuError):
            service.process_card_action(self.fx.user, event_id="evt2", operator_open_id="ou_other", message_id="m", value={"action": "v1a_view_task_center", "candidate_batch_id": "x"})

    def test_feishu_app_id_and_secret_are_both_dpapi_encrypted(self):
        service = FeishuService(
            self.fx.database,
            self.fx.writer,
            CandidateService(self.fx.database, self.fx.writer),
        )
        service.save_credentials(self.fx.user, "cli_test_app", "secret-value")
        row = self.fx.database.query_one(
            "SELECT app_id, encrypted_app_id, encrypted_app_secret FROM feishu_profile WHERE tool_user_id=?",
            (self.fx.user,),
        )
        self.assertIsNone(row["app_id"])
        self.assertNotIn("cli_test_app", row["encrypted_app_id"])
        self.assertNotIn("secret-value", row["encrypted_app_secret"])
        profile, secret = service._profile_with_secret(self.fx.user)
        self.assertEqual("cli_test_app", profile["app_id"])
        self.assertEqual("secret-value", secret)

    def test_platform_operation_log_classification_is_business_facing(self):
        self.assertEqual("retarget_pause", _classify_operation("暂停素材追投任务"))
        self.assertEqual("plan_pause", _classify_operation("暂停计划"))
        self.assertEqual("budget_update", _classify_operation("修改预算"))
        self.assertEqual("roi_update", _classify_operation("修改ROI目标"))
        self.assertEqual("bid_update", _classify_operation("修改出价"))

    def test_daily_report_separates_real_simulation_and_browser(self):
        now = beijing_iso()
        for index, source in enumerate(("platform_log", "tool_direct", "simulation", "browser_observed")):
            self.fx.writer.execute(
                "INSERT INTO operation_event(event_uid, tool_user_id, aavid, account_name, event_time_utc, event_time_beijing, source, action_type, result_status, created_at) VALUES(?, ?, '1001', '账户', ?, ?, ?, 'retarget_create', 'succeeded', ?)",
                (f"event{index}", self.fx.user, utc_iso(), now, source, utc_iso()),
            )
        report = OperationReportService(self.fx.database, self.fx.writer).daily_summary(self.fx.user, business_date())
        self.assertEqual(2, report["real_platform_operations"]["total"])
        self.assertEqual(0, report["simulation_candidates"]["total"])
        self.assertEqual(1, report["simulation_candidates"]["dry_run_audits"]["total"])

    def test_daily_report_overview_accepts_only_enabled_account_scope(self):
        now = beijing_iso()
        for aavid in ("1001", "1002"):
            self.fx.writer.execute(
                "INSERT INTO operation_event(event_uid, tool_user_id, aavid, account_name, event_time_utc, event_time_beijing, source, action_type, result_status, created_at) VALUES(?, ?, ?, ?, ?, ?, 'platform_log', 'plan_pause', 'succeeded', ?)",
                (f"event_scope_{aavid}", self.fx.user, aavid, f"账户{aavid}", utc_iso(), now, utc_iso()),
            )
        report = OperationReportService(self.fx.database, self.fx.writer).daily_summary(
            self.fx.user, business_date(), aavids=["1001"]
        )
        self.assertEqual(1, report["real_platform_operations"]["total"])

    def test_daily_report_completeness_is_isolated_by_account_and_delivery_is_idempotent(self):
        first = self.fx.seed_target(aavid="1001", ad_id="2001")
        second = self.fx.seed_target(aavid="1002", ad_id="2002")
        now = utc_iso()
        for index, (aavid, target, status) in enumerate(
            (("1001", first, "complete"), ("1002", second, "partial")), start=1
        ):
            self.fx.writer.execute(
                """
                INSERT INTO collection_run(
                    collection_run_id, tool_user_id, aavid, target_uid,
                    object_type, business_date, filters_hash, started_at,
                    completed_at, adapter_version, status
                ) VALUES(?, ?, ?, ?, 'operation_log', ?, 'filters', ?, ?, 'test', ?)
                """,
                (f"op_run_{index}", self.fx.user, aavid, target, business_date(), now, now, status),
            )
        service = OperationReportService(self.fx.database, self.fx.writer)
        summary = service.daily_summary(self.fx.user, business_date(), "1001")
        self.assertEqual({"1001": "complete"}, summary["platform_log_completeness"])
        self.fx.writer.execute(
            "INSERT INTO feishu_route(route_id, tool_user_id, route_name, created_at, updated_at) VALUES('route_report', ?, '日报', ?, ?)",
            (self.fx.user, now, now),
        )
        first_uid = service.record_delivery(
            self.fx.user, business_date(), "route_report", summary
        )
        second_uid = service.record_delivery(
            self.fx.user, business_date(), "route_report", summary
        )
        self.assertEqual(first_uid, second_uid)
        self.assertEqual(
            1,
            self.fx.database.query_one(
                "SELECT COUNT(*) c FROM daily_report_delivery WHERE report_uid=?",
                (first_uid,),
            )["c"],
        )

    def test_feishu_inbox_payload_is_encrypted_and_recoverable(self):
        service = FeishuService(
            self.fx.database,
            self.fx.writer,
            CandidateService(self.fx.database, self.fx.writer),
        )
        payload = {
            "sender_open_id": "ou_admin",
            "chat_id": "chat1",
            "chat_type": "p2p",
            "text": "普通消息",
            "message_id": "message1",
        }
        self.assertTrue(
            service.ingest_event(
                tool_user_id=self.fx.user,
                event_id="evt_recover",
                event_type="im.message.receive_v1",
                sender_open_id="ou_admin",
                message_id="message1",
                payload=payload,
            )
        )
        stored = self.fx.database.query_one(
            "SELECT payload_json, status FROM feishu_inbox WHERE tool_user_id=? AND event_id='evt_recover'",
            (self.fx.user,),
        )
        self.assertNotIn("普通消息", stored["payload_json"])
        self.assertEqual({"processed": 1, "failed": 0, "unrecoverable": 0}, service.recover_inbox(self.fx.user))
        self.assertEqual(
            "processed",
            self.fx.database.query_one(
                "SELECT status FROM feishu_inbox WHERE tool_user_id=? AND event_id='evt_recover'",
                (self.fx.user,),
            )["status"],
        )

    def test_feishu_outbox_partial_failure_does_not_report_ready(self):
        service = FeishuService(
            self.fx.database,
            self.fx.writer,
            CandidateService(self.fx.database, self.fx.writer),
        )
        service.save_credentials(self.fx.user, "cli_test_app", "secret-value")
        now = utc_iso()
        for index in (1, 2):
            self.fx.writer.execute(
                """
                INSERT INTO feishu_outbox(
                    outbox_id, tool_user_id, route_id, task_uid,
                    payload_json, status, next_attempt_at, created_at, updated_at
                ) VALUES(?, ?, ?, 'task_partial', ?, 'queued', ?, ?, ?)
                """,
                (
                    f"outbox_partial_{index}",
                    self.fx.user,
                    f"open_id:ou_{index}",
                    json.dumps(
                        {
                            "receive_type": "open_id",
                            "receive_id": f"ou_{index}",
                            "card": {"elements": []},
                        }
                    ),
                    now,
                    now,
                    now,
                ),
            )
        with patch(
            "production_v1a.feishu.FeishuApiClient.send_card",
            side_effect=["message_ok", FeishuError("temporary")],
        ):
            self.assertEqual(1, service.deliver_outbox_once(self.fx.user))
        self.assertEqual(
            {"sent", "retry"},
            {
                row["status"]
                for row in self.fx.database.query_all(
                    "SELECT status FROM feishu_outbox WHERE task_uid='task_partial'"
                )
            },
        )
        self.assertEqual(
            "error",
            self.fx.database.query_one(
                "SELECT send_status FROM feishu_profile WHERE tool_user_id=?",
                (self.fx.user,),
            )["send_status"],
        )

    def test_scheduler_active_job_deduplication_is_scoped_to_tool_user(self):
        now = utc_iso()
        self.fx.writer.execute(
            "INSERT INTO tool_user(tool_user_id, username, password_salt, password_hash, password_iterations, recovery_code_hash, status, created_at, updated_at) VALUES('user_two', 'two', '00', '00', 1, '00', 'active', ?, ?)",
            (now, now),
        )
        self.fx.writer.execute(
            "INSERT INTO background_job(job_uid, tool_user_id, job_type, priority, payload_json, status, created_at, updated_at) VALUES('job_other', 'user_two', 'catalog_refresh', 1, '{\"aavid\":\"same\"}', 'queued', ?, ?)",
            (now, now),
        )
        scheduler = object.__new__(V1AScheduler)
        scheduler.runtime = SimpleNamespace(database=self.fx.database)
        self.assertFalse(
            scheduler._job_active(self.fx.user, "catalog_refresh", "aavid", "same")
        )
        self.assertTrue(
            scheduler._job_active("user_two", "catalog_refresh", "aavid", "same")
        )

    def test_card_only_previews_desktop_frozen_groups(self):
        batch = {"aavid": "1001", "candidate_batch_id": "b1", "expires_at": utc_iso(), "material_snapshot_json": json.dumps([{"sequence": 1, "material_id": "m1", "material_name": "视频1"}])}
        groups = [{"sequence": 1, "material_ids_json": json.dumps(["m1"])}]
        card = build_candidate_preview_card(batch, account_name="账户", plan_name="计划", plan_system="global", promotion_scene="product", groups=groups)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("V1A 模拟，不执行任何千川操作", text)
        self.assertNotIn("multi_select_static", text)
        self.assertNotIn("v1a_selected_group", text)
        self.assertIn("v1a_confirm_groups", text)
        self.assertIn("v1a_reject_groups", text)

    def test_large_feishu_candidate_card_has_page_navigation(self):
        materials = [{"sequence": index, "material_id": f"m{index}", "material_name": f"视频{index}"} for index in range(1, 7)]
        groups = [{"sequence": index, "material_ids_json": json.dumps([f"m{index}"])} for index in range(1, 7)]
        batch = {
            "aavid": "1001",
            "candidate_batch_id": "b21",
            "expires_at": utc_iso(),
            "material_snapshot_json": json.dumps(materials, ensure_ascii=False),
        }
        first = json.dumps(
            build_candidate_preview_card(
                batch,
                account_name="账户",
                plan_name="计划",
                plan_system="global",
                promotion_scene="product",
                groups=groups,
                page=1,
            ),
            ensure_ascii=False,
        )
        second = json.dumps(
            build_candidate_preview_card(
                batch,
                account_name="账户",
                plan_name="计划",
                plan_system="global",
                promotion_scene="product",
                groups=groups,
                page=2,
            ),
            ensure_ascii=False,
        )
        self.assertIn("v1a_next_page", first)
        self.assertNotIn("v1a_previous_page", first)
        self.assertIn("v1a_previous_page", second)

    def test_adjustment_preview_card_is_explicitly_non_executable(self):
        card = build_adjustment_preview_card(
            {
                "aavid": "1001",
                "action_type": "retarget_pause",
                "expires_at": utc_iso(),
                "metrics_snapshot_json": json.dumps(
                    {"spend_cent": 100, "order_count": 1, "gmv_cent": 300, "roi_decimal": "3"}
                ),
            },
            account_name="账户",
            plan_name="计划",
            task_name="任务",
            control_task_id="control1",
            plan_system="chengfang",
            promotion_scene="live",
        )
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("V1A模拟，不执行任何千川操作", text)
        self.assertNotIn('"tag": "button"', text)

    def test_429_read_request_retries_and_then_succeeds(self):
        class FakePage:
            def __init__(self):
                self.calls = 0

            def evaluate(self, *_args):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "ok": False,
                        "status": 429,
                        "retryAfter": "0.1",
                        "contentType": "application/json",
                        "text": "{}",
                    }
                return {
                    "ok": True,
                    "status": 200,
                    "retryAfter": "",
                    "contentType": "application/json",
                    "text": '{"data":{"ok":true}}',
                }

        worker = PlaywrightBrowserWorker(
            self.fx.database, self.fx.writer, PlatformNetworkGuard()
        )
        worker._page = FakePage()
        worker._tool_user_id = self.fx.user
        result = worker.request("GET", "/ad/api/v1/account/user/info")
        self.assertTrue(result["data"]["ok"])
        self.assertEqual(2, worker._page.calls)

    def test_login_required_job_waits_for_user_instead_of_generic_failure(self):
        service = JobService(
            self.fx.database,
            self.fx.writer,
            LeaseManager(self.fx.database, self.fx.writer),
            EventBus(),
            "test-instance",
        )

        def blocked(_context):
            raise LoginRequired("please_login")

        service.register("blocked", blocked)
        job_uid = service.create("blocked", {}, tool_user_id=self.fx.user)
        claimed = service.claim_next()
        self.assertIsNotNone(claimed)
        service.execute_claimed(*claimed)
        self.assertEqual("blocked_user_action", service.get(job_uid)["status"])

    def test_collection_capacity_is_adaptive_and_bounded(self):
        self.assertEqual((300, "within_5_minutes"), collection_interval_for_relation_count(20_000))
        self.assertEqual((450, "adaptive_5_to_10_minutes"), collection_interval_for_relation_count(35_000))
        self.assertEqual((600, "adaptive_5_to_10_minutes"), collection_interval_for_relation_count(50_000))
        self.assertEqual((600, "capacity_exceeded"), collection_interval_for_relation_count(50_001))

    def test_platform_operation_log_is_archived_by_business_month(self):
        target_uid = self.fx.seed_target()
        account = self.fx.database.query_one(
            "SELECT * FROM advertiser_account WHERE tool_user_id=? AND aavid='1001'",
            (self.fx.user,),
        )
        target = self.fx.database.query_one(
            "SELECT * FROM source_plan WHERE target_uid=?", (target_uid,)
        )
        inserted = CollectionService(
            self.fx.database, self.fx.writer, AdapterRegistry(EmptyTransport())
        )._persist_operation_logs(
            self.fx.user,
            account,
            target,
            [
                {
                    "logId": "log_august",
                    "operateTime": "2026-08-05T09:00:00+08:00",
                    "content": "修改预算",
                }
            ],
        )
        self.assertEqual(1, inserted)
        history_path = self.fx.paths.history_db_for_month("2026-08")
        conn = self.fx.database.connect(history_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM platform_operation_log WHERE platform_log_id='log_august'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(1, count)


class ProductionV1AAdminAndApiTests(unittest.TestCase):
    def test_single_instance_mutex_is_global_and_user_scoped(self):
        self.assertTrue(mutex_name().startswith("Global\\QCSCKP-production-v1a-"))

    def test_initial_admin_and_offline_recovery_rotate_code(self):
        with tempfile.TemporaryDirectory(prefix="qcsckp-v1a-admin-") as root:
            database = RuntimeDatabase(RuntimePaths.from_root(root).ensure())
            writer = StorageWriter(database).start()
            try:
                auth = LocalAdminService(database, writer)
                created = auth.create_initial_admin("admin_local", "StrongPass123")
                self.assertTrue(auth.verify_password("admin_local", "StrongPass123"))
                replacement = auth.reset_password_with_recovery_code("admin_local", created.recovery_code, "NewStrongPass456")
                self.assertNotEqual(created.recovery_code, replacement)
                self.assertIsNone(auth.verify_password("admin_local", "StrongPass123"))
                self.assertTrue(auth.verify_password("admin_local", "NewStrongPass456"))
            finally:
                writer.close()

    def test_local_api_requires_launch_token_and_reports_zero_writes(self):
        with tempfile.TemporaryDirectory(prefix="qcsckp-v1a-api-") as root:
            service = start_service(paths=RuntimePaths.from_root(root).ensure())
            try:
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(service.base_url + "/api/v1/health", timeout=5)
                self.assertEqual(401, denied.exception.code)
                request = urllib.request.Request(service.base_url + "/api/v1/health", headers={"Authorization": "Bearer " + service.launch_token})
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.load(response)
                self.assertTrue(payload["success"])
                self.assertFalse(payload["data"]["real_platform_writes"]["registered"])
            finally:
                service.close()

    def test_legacy_scan_does_not_modify_source_database(self):
        with tempfile.TemporaryDirectory(prefix="qcsckp-v1a-migration-") as root:
            root_path = Path(root)
            legacy_dir = root_path / "legacy"
            legacy_dir.mkdir()
            legacy_db = legacy_dir / "qianchuan.db"
            conn = sqlite3.connect(legacy_db)
            conn.execute("CREATE TABLE qianchuan_account(aavid TEXT, account_name TEXT)")
            conn.execute("INSERT INTO qianchuan_account VALUES('1001','原账户')")
            conn.commit(); conn.close()
            before = legacy_db.read_bytes()
            paths = RuntimePaths.from_root(root_path / "runtime").ensure()
            database = RuntimeDatabase(paths); writer = StorageWriter(database).start()
            try:
                service = LegacyMigrationService(paths, database, writer)
                sources = service.scan([str(legacy_dir)])
                source = next(item for item in sources if Path(item["database_path"]).resolve() == legacy_db.resolve())
                self.assertEqual(1, source["account_count"])
                self.assertEqual(before, legacy_db.read_bytes())
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main()
