"""Report attribution and raw report values must survive diagnostic export."""
from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from api.dashboard_optimized import OptimizedDashboardQueries
from services.failure_report import build_failure_report
from services.material_metric_contract import FIELDS, REPORT_SCOPE_LABEL, REPORT_SOURCE, evidence
from services.official_api_collection import _material_snapshot
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class ChengfangMetricEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qcsckp-report-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "data.db")
        init_sqlite_schema(database=self.path)
        self.store = SQLiteStore(database=self.path)
        self.now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.scope = {"target_uid": "target", "account_uid": "account", "aadvid": "1001",
                      "ad_id": "2001", "promotion_scene": "live", "plan_system": "chengfang"}
        self.store.insert("qianchuan_account", {"account_uid": "account", "aavid": "1001",
                          "owner_username": "test-owner", "enabled": 1})
        self.store.insert("promotion_target", {**self.scope, "enabled": 1, "last_status": "ok"})
        self.queries = OptimizedDashboardQueries(self.store)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(OptimizedDashboardQueries, "_owner", return_value="test-owner"))

    def seed_trace(self, optional_errors=None):
        materials = []
        snapshots = []
        for mid, value in (("3001", 12.5), ("3002", 0), ("3003", None)):
            stats = {field: value for field in FIELDS} if value is not None else {}
            raw = {field: {"Value": value * 100, "ValueStr": str(value * 100), "unit": 2,
                           "access_token": "private-token-not-for-report"} for field in FIELDS} if value is not None else {}
            material = {"material_id": mid, "stats_info": stats, "report_row_present": value is not None,
                        "raw": {"stats_info": raw, "material_report": {"private-name": "private-response-name"}}}
            snapshot = _material_snapshot(material, target=self.scope, units={}, request_id="request-1")
            snapshot.update(collected_at=self.now, stat_date=self.now[:10],
                            delivery_state="delivering", account_username="test-owner")
            self.store.insert("pmc_promotion_material_latest", snapshot)
            materials.append(material)
            snapshots.append(snapshot)
        scope = {"metric_scope": "chengfang_anchor_material", "attribution": "unique_plan_in_complete_catalog",
                 "aadvid": "1001", "ad_id": "2001", "anchor_id": "987654321098765",
                 "ecp_app_id": "1", "data_period": "ALL_DATA", "private_name": "private-scope-name",
                 "catalog_plan_count": 2, "catalog_start_time": self.now[:10]+' 00:00:00',
                 "catalog_bid_types": ["SMART_BID_CUSTOM", "SMART_BID_CONSERVATIVE"],
                 "proof_request_ids": ["202609102230009999ABCDEF"]}
        if optional_errors is not None:
            scope["optional_metric_errors"] = optional_errors
            scope["optional_metric_request_ids"] = ["optional-request-1"]
        trace = evidence(materials, snapshots, requested_fields=list(FIELDS), report_units={field: "2" for field in FIELDS},
                         observed_at=self.now, stat_date=self.now[:10], source=REPORT_SOURCE, scope=scope)
        trace["request_ids"] = ["202609102230009999ABCDEF"]
        self.store.update("promotion_target", {"capability_json": json.dumps({
            "material_metric_source": REPORT_SOURCE, "material_metric_evidence": trace})}, where={"target_uid": "target"})
        return trace

    def test_raw_report_units_normalized_values_and_missing_rows_are_distinct(self):
        trace = self.seed_trace()
        self.assertEqual("1823297941140569", trace["document_id"])
        self.assertEqual("report_values_normalized_once", trace["conversion"])
        self.assertEqual(2, trace["report_rows_present"])
        self.assertEqual({"valid": 2, "zero": 1, "missing": 1, "null": 0, "invalid": 0},
                         trace["field_counts"]["stat_cost_for_roi2"])
        by_id = {sample["material_id"]: sample for sample in trace["samples"]}
        self.assertEqual(1250, by_id["3001"]["fields"]["stat_cost_for_roi2"]["raw_value"]["Value"])
        self.assertEqual(12.5, by_id["3001"]["fields"]["stat_cost_for_roi2"]["parsed_value"])
        self.assertTrue(by_id["3002"]["report_row_present"])
        self.assertEqual(0, by_id["3002"]["fields"]["stat_cost_for_roi2"]["parsed_value"])
        self.assertFalse(by_id["3003"]["report_row_present"])
        self.assertFalse(by_id["3003"]["raw_response_available"])
        self.assertIsNone(by_id["3003"]["fields"]["stat_cost_for_roi2"]["parsed_value"])
        self.assertNotIn("private", json.dumps(trace))

    def test_failure_export_keeps_report_source_and_hashes_scope_identifiers(self):
        self.seed_trace()
        report = build_failure_report(db_path=self.path)
        self.assertNotIn("metric_evidence_read_error", report)
        trace = report["material_metric_evidence"][0]
        self.assertEqual(REPORT_SOURCE, trace["source"])
        self.assertEqual("1823297941140569", trace["document_id"])
        self.assertEqual("unique_plan_in_complete_catalog", trace["scope"]["attribution"])
        self.assertEqual(2,trace['scope']['catalog_plan_count'])
        self.assertEqual(["SMART_BID_CUSTOM", "SMART_BID_CONSERVATIVE"],trace['scope']['catalog_bid_types'])
        self.assertEqual(["202609102230009999ABCDEF"],trace['scope']['proof_request_ids'])
        self.assertTrue(trace["scope"]["anchor_id"].startswith("sha256:"))
        self.assertNotIn("987654321098765", json.dumps(report))
        self.assertNotIn("private", json.dumps(report))
        self.assertEqual("202609102230009999ABCDEF", trace["request_ids"][0])
        positive = next(sample for sample in trace["samples"] if sample["fields"]["stat_cost_for_roi2"]["parsed_value"] == 12.5)
        field = positive["fields"]["stat_cost_for_roi2"]
        self.assertEqual((12.5, 12.5), (field["stored_value"], field["dashboard_value"]))
        self.assertTrue(positive["same_observation"])
        self.assertEqual(REPORT_SOURCE, report["metric_findings"][0]["source"])

    def test_dashboard_identifies_anchor_period_scope_without_changing_totals(self):
        self.seed_trace()
        rows = self.queries.get_table_data()["data"]
        self.assertEqual(3, len(rows))
        self.assertEqual({REPORT_SCOPE_LABEL}, {row["metricScopeLabel"] for row in rows})
        total = self.queries.get_latest_cost_sum()
        self.assertEqual(12.5, total["totalCost"])
        self.assertEqual(1, total["missingCostCount"])
        self.assertEqual(REPORT_SCOPE_LABEL, total["metricScopeLabel"])
        self.store.update("promotion_target", {"capability_json": "{}"}, where={"target_uid": "target"})
        self.assertEqual("", self.queries.get_table_data()["data"][0]["metricScopeLabel"])
        self.assertEqual("", self.queries.get_latest_cost_sum()["metricScopeLabel"])

    def test_optional_traffic_failure_is_exported_without_losing_core_values(self):
        self.seed_trace(optional_errors=[{
            "fields": ["live_show_count_for_roi2_v2", "live_watch_count_for_roi2_v2"],
            "endpoint": "/open_api/v1.0/qianchuan/report/uni_promotion/data/get/",
            "code": "40000", "request_id": "optional-request-1",
            "message": "千川可选流量指标暂不可用，经营指标已保留", "raw_response": "private-body",
        }])
        report = build_failure_report(db_path=self.path)
        trace = report["material_metric_evidence"][0]
        self.assertEqual(1, trace["optional_metric_error_count"])
        auxiliary = trace["scope"]["optional_metric_errors"][0]
        self.assertEqual(["live_show_count_for_roi2_v2", "live_watch_count_for_roi2_v2"], auxiliary["fields"])
        self.assertEqual("40000", auxiliary["code"])
        self.assertEqual("optional-request-1", auxiliary["request_id"])
        self.assertEqual(["optional-request-1"], trace["scope"]["optional_metric_request_ids"])
        self.assertEqual("optional_metrics_unavailable_core_values_preserved", report["metric_findings"][0]["warning"])
        self.assertEqual(12.5, self.queries.get_latest_cost_sum()["totalCost"])
        self.assertNotIn("private-body", json.dumps(report))

    def test_optional_error_evidence_is_bounded_and_strips_secrets(self):
        item = {"fields": ["live_show_count_for_roi2_v2", "app_secret", "private-field"],
                "endpoint": "/open_api/v1.0/qianchuan/report/uni_promotion/data/get/?access_token=private-endpoint-token",
                "request_id": "optional-request-1", "code": "40000",
                "message": "请求失败 access_token=private-message-token URL https://private.example/?token=123",
                "access_token": "private-value"}
        trace = evidence([], [], requested_fields=list(FIELDS), report_units={}, observed_at=self.now,
                         stat_date=self.now[:10], source=REPORT_SOURCE,
                         scope={"optional_metric_errors": [item] * 100})
        self.assertEqual(8, len(trace["scope"]["optional_metric_errors"]))
        self.assertTrue(trace["scope"]["optional_metric_errors_truncated"])
        self.assertEqual(["live_show_count_for_roi2_v2"], trace["scope"]["optional_metric_errors"][0]["fields"])
        self.assertNotIn("private", json.dumps(trace))

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend execution checks")
    def test_frontend_keeps_scope_in_tooltips_and_warns_for_real_missing_values(self):
        html = (Path(__file__).resolve().parents[1] / "static" / "dashboard.html").read_text(encoding="utf-8")
        functions = []
        for name in ("renderTable", "formatYuanCompact", "updateLatestCrawlCostDisplay"):
            match = re.search(r"        (?:async )?function " + name + r"\([\s\S]*?\n        }", html)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        body = """
const fs=require('fs'),assert=require('assert'); const p=JSON.parse(fs.readFileSync(0,'utf8'));
eval(p.functions.join('\\n'));
const rows=[],tbody={innerHTML:'',appendChild:r=>rows.push(r)}, val={},meta={};
const document={getElementById:id=>id==='latestCrawlCostValue'?val:id==='latestCrawlCostMeta'?meta:tbody,createElement:()=>({})};
const VELOCITY_CONFIG={negative:'n',normal:'n',high:'h',threshold:10};
let selectedMaterial=null,currentPeriod='1h',currentSortBy='costDiff',currentSortOrder='desc',dashboardScopeGeneration=1;
const lucide={createIcons(){}};
function getMaterialDisplayTitle(item){return item.title;}
function escapeHtml(value){return String(value).replaceAll('<','&lt;');}
function updateColumnVisibility(){} function updatePagination(){}
async function fetchLatestCrawlCostSum(){return {rowCount:3,totalCost:12.5,missingCostCount:1,metricScopeLabel:p.label};}
renderTable([{id:'1',title:'material',velocity:0,currentCost:12.5,metricScopeLabel:p.label}]);
assert.ok(!rows[0].innerHTML.includes(p.label));assert.ok(rows[0].title.includes(p.label));
updateLatestCrawlCostDisplay().then(()=>{assert.strictEqual(meta.textContent,'总统计 3 条素材 · 指标不完整');assert.ok(meta.title.includes(p.label));assert.ok(meta.title.includes('不包含直播画面'));assert.ok(val.textContent.includes('12.50'));}).catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", body], input=json.dumps({
            "functions": functions, "label": REPORT_SCOPE_LABEL}), text=True, encoding="utf-8",
            capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
