"""Read-only API doubles: current metrics and independent recovery retries."""

import copy
import sqlite3
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from services.official_api_collection import (
    _bulk_upsert_rows,
    _metric_values_changed,
    collect_target,
)
from services.qianchuan_open_api.errors import ApiRequestError


class CollectionFreshnessTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.today = now[:10]
        self.yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.target = {
            "target_uid": "freshness-target", "account_uid": "freshness-account",
            "aadvid": "1001", "ad_id": "2001", "promotion_scene": "product",
            "plan_system": "global", "platform_status": "active", "last_sync_at": now,
            "capability": {
                "marketing_goal": "VIDEO_PROM_GOODS",
                "report_metric_units": {
                    "stat_cost_for_roi2": "3", "total_prepay_and_pay_order_roi2": "3",
                    "stat_cost_for_roi2_assist": "3",
                },
                "report_config_synced_at": now, "product_catalog_synced_at": now,
                "control_history_synced_at": now, "plan_detail_synced_at": now,
            },
        }
        self.service = MagicMock()
        self.service.list_plan_materials.return_value = ([{
            "material_id": "3001", "material_status": "DELIVERY_OK",
            "stats_info": {"stat_cost_for_roi2": 0},
        }], ["current-material-request"])
        self.service.list_material_report.return_value = ([], ["current-report-request"])
        self.service.list_control_tasks.return_value = ([{
            "task_id": "4001", "status": "PROCESSING", "status_source": "api",
            "metrics": {},
        }], ["current-control-request"])
        self.previous_material = {
            "material_id": "3001", "stat_date": self.yesterday,
            "stat_cost": 900.0, "prepay_pay_order_count": 20.0,
        }
        self.previous_control = {
            "assist_task_id": "4001", "stat_cost_for_roi2_assist": 500.0,
            "total_pay_order_count_for_roi2_assist": 0,
            "total_prepay_and_pay_order_roi2_assist": 0,
        }
        self.store = MagicMock(config={"database": ":memory:"})
        self.store.execute.side_effect = lambda sql, *args, **kwargs: (
            [self.previous_material] if "SELECT * FROM pmc_promotion_material_latest" in sql else []
        )
        self.store.select.return_value = [self.previous_control]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        patches = {
            "get_official_api_service": {"return_value": self.service},
            "_ensure_collection_schema": {},
            "_count_rows": {"return_value": 0},
            "patch_target_sync_state": {},
            "_patch_target_sync_in_transaction": {},
            "_bulk_upsert_rows": {},
            "stop_cycle_state": {"return_value": {"blocked": False}},
            "_guard_control_snapshot_after_confirmed_stop": {
                "side_effect": lambda db, uid, snapshot, **kw: dict(snapshot)
            },
        }
        self.mocks = {
            name: self.stack.enter_context(patch(f"services.official_api_collection.{name}", **options))
            for name, options in patches.items()
        }
        for module in ("retargeting_rule_runner", "regulation_rule_runner"):
            kind = module.removesuffix("_runner")
            self.stack.enter_context(patch(f"services.{module}.request_{kind}_evaluation"))

    def _rows(self, table):
        return [row for call in self.mocks["_bulk_upsert_rows"].call_args_list
                if call.args[1] == table for row in call.args[2]]

    def test_cross_day_missing_material_metric_stays_null(self):
        self.assertTrue(collect_target(self.target, db=self.store)["success"])
        row = self._rows("pmc_promotion_material_latest")[0]
        self.assertEqual(0, row["stat_cost"])
        self.assertIsNone(row["prepay_pay_order_count"])
        self.assertEqual(self.today, row["stat_date"])
        self.assertIsNone(self._rows("pmc_material_metric_snapshot")[0]["prepay_pay_order_count"])

    def test_same_day_missing_metrics_do_not_inherit_previous_stop_evidence(self):
        self.previous_material["stat_date"] = self.today
        collect_target(self.target, db=self.store)
        self.assertIsNone(self._rows("pmc_promotion_material_latest")[0]["prepay_pay_order_count"])
        row = self._rows("pmc_roi2_assist_task")[0]
        for field in (
            "stat_cost_for_roi2_assist", "total_pay_order_count_for_roi2_assist",
            "total_pay_order_gmv_include_coupon_for_roi2_assist",
            "total_prepay_and_pay_order_roi2_assist",
            "total_order_settle_amount_for_roi2_1h_assist",
            "total_prepay_and_pay_settle_roi2_1h_assist",
            "total_order_settle_count_for_roi2_1h_assist",
        ):
            self.assertIsNone(row[field], field)
        self.assertEqual(0, row["ad_delivery_type"])

    def test_failed_backfill_is_retried_after_today_success_and_reconstructed_target(self):
        self.target["last_sync_at"] = f"{self.yesterday} 23:00:00"
        current = self.service.list_plan_materials.return_value
        self.service.list_plan_materials.side_effect = [current, ApiRequestError("分页不完整")]
        self.assertTrue(collect_target(self.target, db=self.store)["success"])
        state = self.mocks["_patch_target_sync_in_transaction"].call_args.kwargs["capability_updates"]
        self.assertEqual([self.yesterday], state["recovery_backfill_pending_dates"])
        resumed_target = copy.deepcopy(self.target)
        resumed_target["last_sync_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        resumed_target["capability"].update(state)
        self.service.list_plan_materials.side_effect = [current, current]
        self.service.list_plan_materials.reset_mock()
        self.mocks["_bulk_upsert_rows"].reset_mock()
        self.assertTrue(collect_target(resumed_target, db=self.store)["success"])
        calls = self.service.list_plan_materials.call_args_list
        self.assertEqual(2, len(calls))
        self.assertEqual(self.yesterday, calls[1].kwargs["start_date"])
        state = self.mocks["_patch_target_sync_in_transaction"].call_args.kwargs["capability_updates"]
        self.assertEqual([], state["recovery_backfill_pending_dates"])
        self.assertEqual(self.yesterday, state["recovery_backfill_date"])
        self.assertEqual(self.today, self._rows("pmc_promotion_material_latest")[0]["stat_date"])

    def test_pending_older_date_is_not_lost_at_next_day_boundary(self):
        older = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        self.target["last_sync_at"] = f"{self.yesterday} 23:00:00"
        self.target["capability"]["recovery_backfill_pending_dates"] = [older]
        collect_target(self.target, db=self.store)
        self.assertEqual(older, self.service.list_plan_materials.call_args.kwargs["start_date"])
        state = self.mocks["_patch_target_sync_in_transaction"].call_args.kwargs["capability_updates"]
        self.assertEqual([self.yesterday], state["recovery_backfill_pending_dates"])

    def test_collection_that_crosses_midnight_does_not_stamp_old_day_as_fresh(self):
        with patch("services.official_api_collection._date_window", return_value=(self.yesterday, self.yesterday)):
            with self.assertRaisesRegex(RuntimeError, "统计日期已切换"):
                collect_target(self.target, db=self.store)
        self.mocks["_bulk_upsert_rows"].assert_not_called()
        self.mocks["_patch_target_sync_in_transaction"].assert_not_called()

    def test_null_and_date_changes_are_history_points_but_same_null_is_not(self):
        self.assertTrue(_metric_values_changed({"stat_cost": 0}, {"stat_cost": None}))
        self.assertFalse(_metric_values_changed({"stat_cost": None}, {"stat_cost": None}))
        self.assertTrue(_metric_values_changed(
            {"stat_date": self.yesterday, "stat_cost": 0},
            {"stat_date": self.today, "stat_cost": 0},
        ))

    def test_native_upsert_actually_clears_an_old_metric(self):
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE metric (target_uid TEXT PRIMARY KEY, stat_cost REAL)")
            connection.execute("INSERT INTO metric VALUES ('target', 99)")
            _bulk_upsert_rows(connection, "metric", [{"target_uid": "target", "stat_cost": None}],
                              unique_fields=("target_uid",))
            self.assertIsNone(connection.execute("SELECT stat_cost FROM metric").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
