"""Production V1A account catalog and trusted read-collection regressions."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace

from production_v1a.adapters import AdapterRegistry
from production_v1a.adapters.base import PlatformAdapter
from production_v1a.adapters.models import (
    AccountIdentity,
    NormalizedMaterial,
    NormalizedPlan,
    PageResult,
    PaginatedResult,
)
from production_v1a.candidates import CandidateBlocked, CandidateService
from production_v1a.collections import CollectionService, target_uid
from production_v1a.runtime import RuntimeContext
from production_v1a.runtime_paths import RuntimePaths
from production_v1a.storage import RuntimeDatabase, StorageWriter
from production_v1a.timeutils import business_date, utc_iso, utc_now


class RejectTransport:
    def request(self, *_args, **_kwargs):
        raise AssertionError("unexpected network request")


def page_result(
    rows=(),
    *,
    total=0,
    page=1,
    page_size=100,
    has_more=False,
):
    return PageResult(
        rows=tuple(rows),
        page_number=page,
        page_size=page_size,
        total_count=total,
        has_more=has_more,
        platform_server_time=None,
        response_schema_hash=f"schema-{page}",
    )


def paginated(rows=(), *, status="complete", total=None):
    values = tuple(rows)
    return PaginatedResult(
        rows=values,
        platform_total_count=len(values) if total is None else total,
        expected_pages=1 if values else 0,
        successful_pages=1,
        failed_pages=(),
        raw_count=len(values),
        unique_count=len(values),
        duplicate_count=0,
        status=status,
        platform_server_time=None,
        response_schema_hashes=("schema",),
    )


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qcsckp-v1a-account-")
        self.paths = RuntimePaths.from_root(self.tmp.name).ensure()
        self.database = RuntimeDatabase(self.paths)
        self.writer = StorageWriter(self.database).start()
        self.user = "tool-user-account"
        now = utc_iso()
        self.writer.execute(
            """
            INSERT INTO tool_user(
                tool_user_id, username, password_salt, password_hash,
                password_iterations, recovery_code_hash, status, created_at, updated_at
            ) VALUES(?, 'account-test', '00', '00', 1, '00', 'active', ?, ?)
            """,
            (self.user, now, now),
        )

    def seed_account(self, aavid="1001", *, status="complete", enabled=1, completed_at=None):
        now = utc_iso()
        completed = now if completed_at is None else completed_at
        self.writer.execute(
            """
            INSERT INTO advertiser_account(
                account_uid, tool_user_id, aavid, account_name, enabled,
                catalog_status, catalog_completed_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"account-{aavid}",
                self.user,
                aavid,
                f"账户{aavid}",
                enabled,
                status,
                completed,
                now,
                now,
            ),
        )

    def seed_plan(
        self,
        aavid="1001",
        ad_id="2001",
        *,
        system="global",
        scene="product",
        monitored=1,
    ):
        now = utc_iso()
        uid = target_uid(self.user, aavid, ad_id)
        self.writer.execute(
            """
            INSERT INTO source_plan(
                target_uid, tool_user_id, aavid, ad_id, plan_name,
                plan_system, promotion_scene, platform_status,
                verification_state, catalog_seen_at, monitor_enabled,
                monitor_eligible, retarget_eligible, pause_eligible,
                adjust_eligible, adapter_version, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'active', 'verified', ?, ?, 1, 0, 0, 0,
                     'test-adapter', ?, ?)
            """,
            (
                uid,
                self.user,
                aavid,
                ad_id,
                f"计划{ad_id}",
                system,
                scene,
                now,
                monitored,
                now,
                now,
            ),
        )
        return self.database.query_one("SELECT * FROM source_plan WHERE target_uid=?", (uid,))

    def close(self):
        self.writer.close()
        self.tmp.cleanup()


class CatalogAdapter:
    PLAN_ENDPOINT = "/ad/api/pmc/v1/uni-promotion/ad/list-required"
    adapter_version = "catalog-test-v1"
    read_capability_state = "read_verified"
    evidence_level = "A"
    mar_goal = 1

    def __init__(self, system, scene, result):
        self.plan_system = system
        self.promotion_scene = scene
        self.plan_dataset = f"{system}-{scene}"
        self.adapter_name = f"{system}-{scene}-adapter"
        self.result = result

    def fetch_account_identity(self, aavid):
        return AccountIdentity(str(aavid), "权威千川账户名")

    def discover_plans(self, _aavid):
        return self.result


class CatalogRegistry:
    def __init__(self, adapters):
        self.adapters = tuple(adapters)

    def all(self):
        return self.adapters


class V1AAccountCollectionTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def test_platform_false_string_does_not_request_a_phantom_page(self):
        payload = {"data": {"list": [{"id": "1"}], "total": 1, "hasMore": "false"}}
        page = PlatformAdapter.extract_page(payload, 1, 100)
        self.assertFalse(page.has_more)

    def test_pagination_total_mismatch_is_partial_not_complete(self):
        adapter = PlatformAdapter(RejectTransport())
        result = adapter._paginate(
            lambda _page: page_result(({"id": "1"},), total=3, has_more=False),
            lambda row, _aavid: row,
            unique_key=lambda row: row["id"],
        )
        self.assertEqual("partial", result.status)
        self.assertEqual("pagination_incomplete", result.error_code)

    def test_empty_page_with_has_more_fails_closed(self):
        adapter = PlatformAdapter(RejectTransport())
        result = adapter._paginate(
            lambda _page: page_result((), total=2, has_more=True),
            lambda row, _aavid: row,
            unique_key=lambda row: row["id"],
        )
        self.assertEqual("schema_changed", result.status)
        self.assertEqual((1,), result.failed_pages)

    def test_all_four_adapters_require_matching_scene_and_system_evidence(self):
        registry = AdapterRegistry(RejectTransport())
        for adapter in registry.all():
            row = {
                "adId": f"{adapter.plan_system}-{adapter.promotion_scene}",
                "adName": "计划",
                "MarGoal": adapter.mar_goal,
                "planSystem": adapter.plan_system,
                "status": "active",
            }
            normalized = adapter.normalize_plan(row, "1001")
            self.assertEqual(adapter.plan_system, normalized.plan_system)
            self.assertEqual(adapter.promotion_scene, normalized.promotion_scene)
            self.assertEqual("verified", normalized.verification_state)
            conflict = adapter.normalize_plan(
                {**row, "MarGoal": 2 if adapter.mar_goal == 1 else 1}, "1001"
            )
            self.assertEqual("conflict", conflict.verification_state)
            self.assertEqual("unknown", conflict.plan_system)
            delivery = adapter.normalize_plan(
                {
                    **row,
                    "status": None,
                    "adDeliveryName": "投放中",
                    "adDeliveryType": 0,
                },
                "1001",
            )
            self.assertEqual("投放中", delivery.platform_status)

    def test_unobserved_global_control_dataset_never_sends_empty_dataset_request(self):
        adapter = AdapterRegistry(RejectTransport()).get("global", "live")
        result = adapter.fetch_control_tasks("1001", "2001", 2)
        self.assertEqual("failed", result.status)
        self.assertEqual("control_dataset_unobserved", result.error_code)

    def test_suspicious_empty_isolated_class_preserves_previous_plan(self):
        self.fx.seed_account()
        old = self.fx.seed_plan(system="global", scene="product")
        live_plan = NormalizedPlan(
            aavid="1001",
            ad_id="live-1",
            plan_name="直播新计划",
            plan_system="global",
            promotion_scene="live",
            platform_status="active",
            verification_state="verified",
            adapter_version="catalog-test-v1",
        )
        registry = CatalogRegistry(
            (
                CatalogAdapter("global", "product", paginated()),
                CatalogAdapter("global", "live", paginated((live_plan,))),
                CatalogAdapter("chengfang", "product", paginated()),
                CatalogAdapter("chengfang", "live", paginated()),
            )
        )
        result = CollectionService(
            self.fx.database, self.fx.writer, registry
        ).refresh_catalog(self.fx.user, "1001")
        self.assertEqual("suspicious_empty", result["status"])
        kept = self.fx.database.query_one(
            "SELECT * FROM source_plan WHERE target_uid=?", (old["target_uid"],)
        )
        self.assertEqual("active", kept["platform_status"])
        self.assertEqual(1, kept["monitor_eligible"])
        self.assertIsNotNone(
            self.fx.database.query_one(
                "SELECT 1 FROM source_plan WHERE tool_user_id=? AND aavid='1001' AND ad_id='live-1'",
                (self.fx.user,),
            )
        )

    def test_deleted_or_unadded_account_cannot_be_recreated_by_refresh(self):
        registry = CatalogRegistry(
            (CatalogAdapter("global", "product", paginated()),)
        )
        with self.assertRaises(KeyError):
            CollectionService(
                self.fx.database, self.fx.writer, registry
            ).refresh_catalog(self.fx.user, "not-added")
        self.assertIsNone(
            self.fx.database.query_one(
                "SELECT 1 FROM advertiser_account WHERE tool_user_id=? AND aavid='not-added'",
                (self.fx.user,),
            )
        )

    def test_add_account_prepares_headless_before_authoritative_identity_read(self):
        calls = []

        class Browser:
            prepared = False

            def open_visible_login_and_capture(self, *_args, **_kwargs):
                calls.append("visible")
                return {"account": {"aavid": "1001", "account_name": "页面名"}}

            def prepare_headless(self, _user):
                self.prepared = True
                calls.append("headless")

        browser = Browser()

        class Collections:
            def add_or_refresh_account(self, _user, _aavid):
                self.assert_prepared()
                calls.append("identity")
                return {"aavid": "1001", "account_name": "权威名"}

            def refresh_catalog(self, *_args):
                self.assert_prepared()
                calls.append("catalog")
                return {"status": "complete"}

            @staticmethod
            def assert_prepared():
                if not browser.prepared:
                    raise AssertionError("headless browser not prepared")

        runtime = SimpleNamespace(
            browser=browser,
            collections=Collections(),
            database=self.fx.database,
            _require_collection_storage=lambda: None,
        )
        context = SimpleNamespace(
            tool_user_id=self.fx.user,
            update_progress=lambda *_args: None,
        )
        result = RuntimeContext._job_add_account(runtime, context)
        self.assertEqual(["visible", "headless", "identity", "catalog"], calls)
        self.assertEqual("complete", result["catalog"]["status"])

    def test_monitor_selection_is_independent_from_feishu_binding(self):
        self.fx.seed_account(enabled=0)
        plan = self.fx.seed_plan(monitored=0)
        runtime = SimpleNamespace(database=self.fx.database, writer=self.fx.writer)
        context = SimpleNamespace(
            tool_user_id=self.fx.user,
            payload={
                "aavid": "1001",
                "enabled": True,
                "daily_report_enabled": False,
                "target_uids": [plan["target_uid"]],
            },
        )
        result = RuntimeContext._job_monitor_setup_save(runtime, context)
        self.assertEqual(1, result["monitored_plan_count"])
        saved = self.fx.database.query_one(
            "SELECT monitor_enabled FROM source_plan WHERE target_uid=?",
            (plan["target_uid"],),
        )
        self.assertEqual(1, saved["monitor_enabled"])

    def test_active_plan_directory_excludes_removed_accounts(self):
        self.fx.seed_account()
        self.fx.seed_plan()
        runtime = SimpleNamespace(database=self.fx.database)
        self.assertEqual(
            1,
            len(RuntimeContext.list_plans(runtime, self.fx.user, aavid="1001")),
        )
        self.fx.writer.execute(
            "UPDATE advertiser_account SET removed_at=? WHERE tool_user_id=? AND aavid='1001'",
            (utc_iso(), self.fx.user),
        )
        self.assertEqual(
            [], RuntimeContext.list_plans(runtime, self.fx.user, aavid="1001")
        )

    def test_product_material_links_are_replaced_by_latest_complete_batch(self):
        self.fx.seed_account()
        plan = self.fx.seed_plan(system="chengfang", scene="product")
        service = CollectionService(
            self.fx.database,
            self.fx.writer,
            AdapterRegistry(RejectTransport()),
        )
        first = NormalizedMaterial(
            "1001", "2001", "m1", "视频", None, "1", "1", None, "1", "0",
            True, True, ("product-old",), 100, 1, 300, "3", {},
        )
        second = NormalizedMaterial(
            "1001", "2001", "m1", "视频", None, "1", "1", None, "1", "0",
            True, True, ("product-new",), 200, 2, 600, "3", {},
        )
        now = utc_iso()
        for run_id in ("run-first", "run-second"):
            self.fx.writer.execute(
                """
                INSERT INTO collection_run(
                    collection_run_id, tool_user_id, aavid, target_uid,
                    object_type, business_date, filters_hash, successful_pages,
                    raw_count, unique_count, started_at, completed_at,
                    adapter_version, status
                ) VALUES(?, ?, '1001', ?, 'video_material', ?, 'test', 1,
                         1, 1, ?, ?, 'test-adapter', 'complete')
                """,
                (run_id, self.fx.user, plan["target_uid"], business_date(), now, now),
            )
        service._persist_material_batch(self.fx.user, plan, "run-first", [first])
        service._persist_material_batch(self.fx.user, plan, "run-second", [second])
        links = self.fx.database.query_all(
            "SELECT product_id FROM product_material_relation WHERE tool_user_id=? AND aavid='1001' AND ad_id='2001'",
            (self.fx.user,),
        )
        self.assertEqual(["product-new"], [row["product_id"] for row in links])

    def test_candidate_generation_blocks_incomplete_and_stale_catalog(self):
        self.fx.seed_account(status="partial")
        plan = self.fx.seed_plan()
        candidates = CandidateService(self.fx.database, self.fx.writer)
        with self.assertRaisesRegex(CandidateBlocked, "account_catalog_not_complete"):
            candidates.generate_for_target(self.fx.user, plan["target_uid"])
        stale = utc_iso(utc_now() - timedelta(hours=2))
        self.fx.writer.execute(
            "UPDATE advertiser_account SET catalog_status='complete', catalog_completed_at=? WHERE tool_user_id=? AND aavid='1001'",
            (stale, self.fx.user),
        )
        with self.assertRaisesRegex(CandidateBlocked, "account_catalog_stale"):
            candidates.generate_for_target(self.fx.user, plan["target_uid"])

    def test_material_context_conflict_is_never_persisted(self):
        self.fx.seed_account()
        plan = self.fx.seed_plan(system="chengfang", scene="product")
        wrong = NormalizedMaterial(
            "other-account", "2001", "m1", "视频", None, "1", "1", None,
            "1", "0", True, True, (), 100, 1, 300, "3", {},
        )

        class Adapter:
            adapter_name = "context-adapter"
            adapter_version = "context-v1"
            plan_system = "chengfang"
            promotion_scene = "product"
            read_capability_state = "read_verified"
            evidence_level = "A"
            MATERIAL_ENDPOINT = "/ad/api/pmc/v1/uni-promotion/material/list-required"
            material_dataset = "video"

            def fetch_materials(self, *_args):
                return paginated((wrong,))

        class Registry:
            @staticmethod
            def get(*_args):
                return Adapter()

        result = CollectionService(
            self.fx.database, self.fx.writer, Registry()
        ).collect_materials(self.fx.user, plan)
        self.assertEqual("schema_changed", result["status"])
        self.assertEqual(
            0,
            self.fx.database.query_one("SELECT COUNT(*) AS c FROM material_identity")["c"],
        )

    def test_target_uid_isolated_by_tool_user_account_and_plan(self):
        self.assertNotEqual(target_uid("u1", "a1", "p1"), target_uid("u2", "a1", "p1"))
        self.assertNotEqual(target_uid("u1", "a1", "p1"), target_uid("u1", "a2", "p1"))
        self.assertNotEqual(target_uid("u1", "a1", "p1"), target_uid("u1", "a1", "p2"))


if __name__ == "__main__":
    unittest.main()
