"""Native plan metrics must reach SQLite and dashboard without report scaling."""
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api.dashboard_optimized import OptimizedDashboardQueries
from services.failure_report import build_failure_report
from services.material_metric_contract import FIELDS, evidence, native_value, safe_numeric_evidence
from services.official_api_collection import _material_snapshot, _merge_material_report
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class NativePlanMetricsTests(unittest.TestCase):
    def test_plan_yuan_is_not_rescaled_by_report_units(self):
        for unit in ("0", "1", "2", "3"):
            for system in ("global", "chengfang"):
                for scene in ("live", "product"):
                    with self.subTest(unit=unit, system=system, scene=scene):
                        row = _material_snapshot({"material_id": "3", "stats_info": {
                            "stat_cost_for_roi2": 12.5, "total_prepay_and_pay_order_roi2": 4,
                            "total_pay_order_gmv_include_coupon_for_roi2": 50}},
                            target={"target_uid": "t", "plan_system": system, "promotion_scene": scene},
                            units={field: unit for field in FIELDS}, request_id="request")
                        self.assertEqual((12.5, 4, 50), (row["stat_cost"], row["prepay_pay_order_count"], row["pay_gmv_include_coupon"]))

    def test_invalid_null_and_zero_remain_distinct(self):
        for value in (None, "", "--", "1,234.56", True, {}, float("nan"), float("inf")):
            self.assertIsNone(native_value(value), repr(value))
        self.assertEqual(0, native_value(0))
        self.assertEqual(12.5, native_value("12.50"))

    def test_account_report_must_not_replace_another_plans_zero(self):
        materials = [{"material_id": "3", "stats_info": {"stat_cost_for_roi2": 0}}]
        account = [{"material_id": "3", "stats_info": {"stat_cost_for_roi2": 999}}]
        self.assertEqual(0, _merge_material_report(materials, account)[0]["stats_info"]["stat_cost_for_roi2"])

    def test_samples_are_bounded_and_do_not_keep_invalid_private_text(self):
        materials = [{"material_id": str(i), "stats_info": {"stat_cost_for_roi2": "private-secret-value"}} for i in range(100)]
        samples = [{"material_id": str(i), "stat_cost": None} for i in range(100)]
        trace = evidence(materials, samples, requested_fields=["stat_cost_for_roi2"], report_units={}, observed_at="now", stat_date="date")
        self.assertEqual(100, trace["field_counts"]["stat_cost_for_roi2"]["invalid"])
        self.assertLessEqual(len(trace["samples"]), 6)
        self.assertNotIn("private-secret-value", json.dumps(trace))

    def test_numeric_format_evidence_is_kept_without_arbitrary_text_or_secrets(self):
        self.assertEqual("1,234.56", safe_numeric_evidence("1,234.56"))
        self.assertEqual("--", safe_numeric_evidence("--"))
        self.assertIsNone(safe_numeric_evidence("app_secret=private-value"))
        self.assertEqual({"Value": "12.50"}, safe_numeric_evidence({"Value": "12.50", "access_token": "private-token"}))


class StoredMetricEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qcsckp-metric-regression-")
        self.path = str(Path(self.temp.name) / "data.db")
        init_sqlite_schema(database=self.path)
        self.store = SQLiteStore(database=self.path)
        self.scope = {"target_uid": "target", "account_uid": "account", "aadvid": "1001", "ad_id": "2001", "promotion_scene": "live", "plan_system": "chengfang"}
        self.store.insert("qianchuan_account", {"account_uid": "account", "aavid": "1001", "owner_username": "test-owner", "enabled": 1})
        self.store.insert("promotion_target", {**self.scope, "enabled": 1, "last_status": "ok"})
        self.now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.queries = OptimizedDashboardQueries(self.store)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(OptimizedDashboardQueries, "_owner", return_value="test-owner"))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def seed(self, stats):
        material = {"material_id": "3001", "stats_info": stats, "raw": {"stats_info": stats}}
        row = _material_snapshot(material, target=self.scope, units={field: "1" for field in FIELDS}, request_id="2026090920295658E51445C0BD8EE4F276")
        row.update(collected_at=self.now, stat_date=self.now[:10], delivery_state="delivering", account_username="test-owner")
        self.store.insert("pmc_promotion_material_latest", row)
        trace = evidence([material], [row], requested_fields=list(FIELDS), report_units={field: "1" for field in FIELDS}, observed_at=self.now, stat_date=self.now[:10])
        trace["request_ids"] = [row["api_request_id"]]
        self.store.update("promotion_target", {"capability_json": json.dumps({"material_metric_evidence": trace})}, where={"target_uid": "target"})

    def test_native_values_survive_storage_and_dashboard_and_report(self):
        stats = {field: 1 for field in FIELDS}
        stats.update(stat_cost_for_roi2=12.5, total_prepay_and_pay_order_roi2=4, total_pay_order_gmv_include_coupon_for_roi2=50)
        self.seed(stats)
        table = self.queries.get_table_data(target_uid="target")["data"][0]
        self.assertEqual((12.5, 4, 50), (table["currentCost"], table["overallPayRoi"], table["overallAmount"]))
        self.assertEqual("available", self.queries.get_refresh_state()["metricQuality"])
        self.assertEqual(12.5, self.queries.get_latest_cost_sum()["totalCost"])
        report = build_failure_report(db_path=self.path)
        self.assertNotIn("metric_evidence_read_error", report)
        sample = report["material_metric_evidence"][0]["samples"][0]
        self.assertTrue(sample["same_observation"])
        field = sample["fields"]["stat_cost_for_roi2"]
        self.assertEqual([12.5] * 4, [field[k] for k in ("raw_value", "parsed_value", "stored_value", "dashboard_value")])
        self.assertEqual("2026090920295658E51445C0BD8EE4F276", report["material_metric_evidence"][0]["request_ids"][0])

    def test_missing_cost_is_not_a_zero_total(self):
        self.seed({})
        self.assertEqual("missing", self.queries.get_refresh_state()["metricQuality"])
        summary = self.queries.get_latest_cost_sum()
        self.assertIsNone(summary["totalCost"])
        history = self.queries.get_scope_history()["data"]
        self.assertTrue(history)
        self.assertIsNone(history[-1]["cost"])
        self.assertIsNone(history[-1]["roi"])
        self.assertEqual(1, summary["missingCostCount"])
        report = build_failure_report(db_path=self.path)
        self.assertEqual("missing_or_invalid_metrics", report["metric_findings"][0]["finding"])

    def test_real_zero_is_kept_and_marked_for_comparison_not_changed(self):
        self.seed({field: 0 for field in FIELDS})
        self.assertEqual(0, self.queries.get_table_data()["data"][0]["currentCost"])
        self.assertEqual("all_zero", self.queries.get_refresh_state()["metricQuality"])
        self.assertEqual(0, self.queries.get_latest_cost_sum()["totalCost"])

    def test_upgrade_does_not_subtract_old_scaled_snapshot(self):
        self.seed({field: 12.5 for field in FIELDS})
        current = datetime.strptime(self.now, "%Y-%m-%d %H:%M:%S")
        previous = (current - timedelta(minutes=70)).strftime("%Y-%m-%d %H:%M:%S")
        for timestamp, cost in ((previous, 0.000125), (self.now, 12.5)):
            self.store.insert("pmc_material_metric_snapshot", {"account_username": "test-owner", **self.scope,
                "material_id": "3001", "collected_at": timestamp, "bucket_key": timestamp,
                "stat_date": self.now[:10], "stat_cost": cost, "pay_gmv_include_coupon": cost})
        self.store.update("promotion_target", {"capability_json": json.dumps({"material_metric_contract_since": self.now})}, where={"target_uid": "target"})
        row = self.queries.get_table_data()["data"][0]
        self.assertEqual(0, row["costDiff"])
        self.assertEqual(self.now, row["periodStartTime"])
        self.assertEqual([12.5], [point["cost"] for point in self.queries.get_scope_history()["data"]])
        self.assertEqual([12.5], [point["cost"] for point in self.queries.get_material_history("3001", target_uid="target")["data"]])
