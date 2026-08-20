import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from api.dashboard_optimized import OptimizedDashboardQueries
from api.dashboard_optimized import DASHBOARD_CONTRACT_VERSION
from config import CURRENT_VERSION
from utils.dashboard_storage_maintenance import (
    backfill_latest_from_legacy,
    backfill_recent_metric_snapshots,
    prune_migrated_official_history,
    prune_hourly_metrics,
    prune_removed_material_latest,
    rollup_old_metric_snapshots,
)
from services.official_api_collection import _metric_values_changed
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class DashboardOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "dashboard.db")
        init_sqlite_schema(database=self.database)
        self.db = SQLiteStore(database=self.database)
        self.owner_patch = patch.dict(
            os.environ, {"QCSCKP_SESSION_OWNER": "dashboard-owner"}
        )
        self.owner_patch.start()
        self.now = datetime.now().replace(microsecond=0)
        self._seed_scope()
        self.queries = OptimizedDashboardQueries(self.db)

    def tearDown(self):
        self.owner_patch.stop()
        self.temp.cleanup()

    def _account(self, uid, aavid, name, *, owner="dashboard-owner", enabled=1):
        self.db.insert(
            "qianchuan_account",
            {
                "account_uid": uid,
                "owner_username": owner,
                "aavid": aavid,
                "account_name": name,
                "enabled": enabled,
                "directory_selected": 1,
            },
        )

    def _target(self, uid, account_uid, aavid, ad_id, name, *, enabled=1):
        self.db.insert(
            "promotion_target",
            {
                "target_uid": uid,
                "account_uid": account_uid,
                "aadvid": aavid,
                "ad_id": ad_id,
                "plan_name": name,
                "promotion_scene": "product",
                "plan_system": "global",
                "platform_status": "active",
                "verification_state": "verified",
                "monitor_eligible": 1,
                "enabled": enabled,
                "last_sync_at": self.now.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def _latest(self, target_uid, aavid, ad_id, material_id, cost):
        self.db.insert(
            "pmc_promotion_material_latest",
            {
                "aadvid": aavid,
                "target_uid": target_uid,
                "ad_id": ad_id,
                "promotion_scene": "product",
                "plan_system": "global",
                "material_id": material_id,
                "video_name": f"素材{material_id}",
                "stat_cost": cost,
                "pay_gmv_include_coupon": cost * 2,
                "prepay_pay_order_count": 2.0,
                "prepay_pay_settle_1h": 1.5,
                "overall_order_count": 2,
                "overall_ctr": 3.0,
                "overall_conversion_rate": 4.0,
                "data_source": "qianchuan_open_api",
                "stat_date": self.now.strftime("%Y-%m-%d"),
                "collected_at": self.now.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def _snapshot(self, target_uid, account_uid, aavid, ad_id, material_id, cost):
        observed = self.now - timedelta(minutes=55)
        self.db.insert(
            "pmc_material_metric_snapshot",
            {
                "account_username": "dashboard-owner",
                "account_uid": account_uid,
                "aadvid": aavid,
                "target_uid": target_uid,
                "ad_id": ad_id,
                "material_id": material_id,
                "bucket_key": observed.strftime("%Y-%m-%d %H:%M:00"),
                "collected_at": observed.strftime("%Y-%m-%d %H:%M:%S"),
                "stat_date": observed.strftime("%Y-%m-%d"),
                "stat_cost": cost,
                "pay_gmv_include_coupon": cost * 2,
                "prepay_pay_order_count": 2.0,
            },
        )

    def _seed_scope(self):
        self._account("account-1", "1001", "账户甲")
        self._account("account-2", "1002", "账户乙")
        self._account("account-disabled", "1003", "停用账户", enabled=0)
        self._account("account-other", "9999", "其他用户", owner="other-owner")
        self._target("target-1", "account-1", "1001", "2001", "计划甲")
        self._target("target-2", "account-2", "1002", "2002", "计划乙")
        self._target("target-disabled", "account-disabled", "1003", "2003", "停用计划")
        self._target("target-other", "account-other", "9999", "2999", "其他计划")
        self._latest("target-1", "1001", "2001", "3001", 100)
        self._latest("target-2", "1002", "2002", "3002", 200)
        self._latest("target-disabled", "1003", "2003", "3003", 999)
        self._latest("target-other", "9999", "2999", "3999", 999)
        self._snapshot("target-1", "account-1", "1001", "2001", "3001", 80)
        self._snapshot("target-2", "account-2", "1002", "2002", "3002", 180)

    def test_default_scope_aggregates_only_current_owner_enabled_accounts(self):
        result = self.queries.get_table_data()
        self.assertTrue(result["success"])
        self.assertEqual(2, result["total"])
        self.assertEqual({"1001", "1002"}, {row["aadvid"] for row in result["data"]})
        self.assertEqual({20.0}, {row["costDiff"] for row in result["data"]})

    def test_account_and_plan_filters_are_exact(self):
        account = self.queries.get_table_data(aavid="1001")
        plan = self.queries.get_table_data(target_uid="target-2")
        self.assertEqual(["3001"], [row["id"] for row in account["data"]])
        self.assertEqual(["3002"], [row["id"] for row in plan["data"]])

    def test_rule_engine_large_page_size_is_not_silently_clamped_to_ui_size(self):
        result = self.queries.get_table_data(target_uid="target-1", page_size=20_000)
        self.assertEqual(20_000, result["pageSize"])
        self.assertEqual(1, result["total"])

    def test_scope_options_only_show_enabled_monitored_rows(self):
        result = self.queries.get_scope_options()
        self.assertEqual({"1001", "1002"}, {row["aavid"] for row in result["accounts"]})
        self.assertEqual(
            {"target-1", "target-2"},
            {row["target_uid"] for row in result["plans"]},
        )

    def test_bootstrap_uses_one_versioned_scope_contract(self):
        result = self.queries.get_bootstrap()
        self.assertTrue(result["success"])
        self.assertEqual(DASHBOARD_CONTRACT_VERSION, result["dashboardContractVersion"])
        self.assertEqual(CURRENT_VERSION, result["appVersion"])
        self.assertEqual("dashboard-owner", result["ownerUsername"])
        self.assertEqual(2, result["accountCount"])
        self.assertEqual(2, result["planCount"])
        self.assertEqual(2, result["materialCount"])
        self.assertEqual("all_enabled_accounts", result["defaultScope"]["mode"])
        self.assertTrue(result["runtimeInstanceId"])
        self.assertNotIn(str(Path(self.temp.name)), result["runtimeInstanceId"])

    def test_bootstrap_never_reports_zero_scope_with_scoped_materials(self):
        result = self.queries.get_bootstrap()
        self.assertGreater(result["materialCount"], 0)
        self.assertGreater(result["accountCount"], 0)
        self.assertGreater(result["planCount"], 0)

    def test_scope_options_expose_capacity_waiting_warning_count(self):
        self.db.update(
            "promotion_target",
            {"capacity_state": "capacity_waiting"},
            where={"target_uid": "target-2"},
        )
        result = self.queries.get_scope_options()
        self.assertEqual(1, result["capacityWaitingCount"])
        target = next(row for row in result["plans"] if row["target_uid"] == "target-2")
        self.assertEqual("capacity_waiting", target["capacity_state"])

    def test_summary_and_top20_respect_scope(self):
        total = self.queries.get_latest_cost_sum()
        account = self.queries.get_latest_cost_sum(aavid="1001")
        top = self.queries.get_top20_by_cost(target_uid="target-2")
        self.assertEqual(300.0, total["totalCost"])
        self.assertEqual(100.0, account["totalCost"])
        self.assertEqual(["3002"], [row["id"] for row in top["data"]])

    def test_history_is_target_scoped(self):
        result = self.queries.get_material_history("3001", target_uid="target-1")
        self.assertEqual(2, result["total"])
        self.assertEqual(80.0, result["data"][0]["cost"])
        self.assertEqual(100.0, result["data"][-1]["cost"])

    def test_legacy_rows_backfill_latest_and_narrow_snapshot(self):
        observed = self.now - timedelta(minutes=10)
        self.db.insert(
            "pmc_promotion_material",
            {
                "aadvid": "1001",
                "target_uid": "target-1",
                "ad_id": "2001",
                "promotion_scene": "product",
                "plan_system": "global",
                "material_id": "legacy-1",
                "video_name": "旧素材",
                "stat_cost": 12.5,
                "data_source": "qianchuan_open_api",
                "stat_date": observed.strftime("%Y-%m-%d"),
                "created_at": observed.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        backfill_latest_from_legacy(db=self.db, owner_username="dashboard-owner")
        backfill_recent_metric_snapshots(db=self.db, owner_username="dashboard-owner")
        latest = self.db.select_one(
            "pmc_promotion_material_latest",
            where={"target_uid": "target-1", "material_id": "legacy-1"},
        )
        snapshot = self.db.select_one(
            "pmc_material_metric_snapshot",
            where={"target_uid": "target-1", "material_id": "legacy-1"},
        )
        self.assertIsNotNone(latest)
        self.assertIsNotNone(snapshot)
        self.assertEqual(12.5, snapshot["stat_cost"])
        self.assertEqual(1, prune_migrated_official_history(db=self.db, delete_limit=10))
        self.assertIsNone(
            self.db.select_one(
                "pmc_promotion_material",
                where={"target_uid": "target-1", "material_id": "legacy-1"},
            )
        )

    def test_unchanged_metrics_do_not_create_repeated_history_points(self):
        previous = {"stat_cost": 10, "overall_order_count": 2}
        same = {"stat_cost": 10.0, "overall_order_count": 2}
        changed = {"stat_cost": 10.01, "overall_order_count": 2}
        self.assertFalse(_metric_values_changed(previous, same))
        self.assertTrue(_metric_values_changed(previous, changed))

    def test_verified_flag_never_prunes_history_without_compact_replacement(self):
        self.db.insert(
            "pmc_promotion_material",
            {
                "aadvid": "1001",
                "target_uid": "legacy_unscoped",
                "ad_id": "",
                "promotion_scene": "product",
                "plan_system": "global",
                "material_id": "unbackfilled-material",
                "data_source": "qianchuan_open_api",
            },
        )
        deleted = prune_migrated_official_history(
            db=self.db,
            delete_limit=10,
            migration_verified=True,
        )
        self.assertEqual(0, deleted)
        self.assertIsNotNone(
            self.db.select_one(
                "pmc_promotion_material",
                where={"material_id": "unbackfilled-material"},
            )
        )

    def test_snapshots_older_than_48_hours_roll_up_to_hourly(self):
        old = self.now - timedelta(days=3)
        self.db.insert(
            "pmc_material_metric_snapshot",
            {
                "account_username": "dashboard-owner",
                "account_uid": "account-1",
                "aadvid": "1001",
                "target_uid": "target-1",
                "ad_id": "2001",
                "material_id": "old-material",
                "bucket_key": old.strftime("%Y-%m-%d %H:%M:00"),
                "collected_at": old.strftime("%Y-%m-%d %H:%M:%S"),
                "stat_date": old.strftime("%Y-%m-%d"),
                "stat_cost": 42.5,
            },
        )
        result = rollup_old_metric_snapshots(db=self.db, delete_limit=10)
        self.assertEqual(1, result["rolled"])
        self.assertEqual(1, result["deleted"])
        hourly = self.db.select_one(
            "pmc_material_metric_hourly",
            where={"target_uid": "target-1", "material_id": "old-material"},
        )
        self.assertIsNotNone(hourly)
        self.assertEqual(42.5, hourly["stat_cost"])

    def test_hourly_and_soft_removed_retention_are_bounded(self):
        old = self.now - timedelta(days=200)
        self.db.insert(
            "pmc_material_metric_hourly",
            {
                "account_username": "dashboard-owner",
                "account_uid": "account-1",
                "aadvid": "1001",
                "target_uid": "target-1",
                "ad_id": "2001",
                "material_id": "old-hourly",
                "hour_key": old.strftime("%Y-%m-%d %H:00:00"),
                "collected_at": old.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        self.db.insert(
            "pmc_promotion_material_latest",
            {
                "aadvid": "1001",
                "target_uid": "target-1",
                "ad_id": "2001",
                "promotion_scene": "product",
                "plan_system": "global",
                "material_id": "old-removed",
                "delivery_state": "removed",
                "last_seen_at": old.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        self.assertEqual(1, prune_hourly_metrics(db=self.db))
        self.assertEqual(1, prune_removed_material_latest(db=self.db))

    def test_insert_many_keeps_columns_introduced_by_later_rows(self):
        self.db.insert_many(
            "pmc_promotion_material_latest",
            [
                {
                    "aadvid": "1001",
                    "target_uid": "target-1",
                    "material_id": "union-1",
                },
                {
                    "aadvid": "1001",
                    "target_uid": "target-1",
                    "material_id": "union-2",
                    "video_name": "第二行新列",
                },
            ],
        )
        saved = self.db.select_one(
            "pmc_promotion_material_latest", where={"material_id": "union-2"}
        )
        self.assertEqual("第二行新列", saved["video_name"])

    def test_concurrent_upsert_keeps_one_unique_latest_row(self):
        errors = []

        def write(index):
            try:
                self.db.insert_or_update(
                    "pmc_promotion_material_latest",
                    {
                        "aadvid": "1001",
                        "target_uid": "target-1",
                        "material_id": "concurrent-material",
                        "stat_cost": float(index),
                    },
                    unique_fields=["target_uid", "material_id"],
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual([], errors)
        rows = self.db.select(
            "pmc_promotion_material_latest",
            where={
                "target_uid": "target-1",
                "material_id": "concurrent-material",
            },
        )
        self.assertEqual(1, len(rows))


if __name__ == "__main__":
    unittest.main()
