from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from services import failure_report, operation_diagnostics, retarget_diagnostics
from services.failure_report_v5 import build_sections, enforce_size, _cap_incident, MAX_REPORT_BYTES, MAX_INCIDENT_BYTES
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class FailureReportV5Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = str(self.root / "qianchuan.db")
        init_sqlite_schema(database=self.db_path)
        self.store = SQLiteStore(database=self.db_path)
        self.events = self.root / "operation-evidence.json"
        self.patches = [
            patch.object(failure_report, "DB_FILE", self.db_path),
            patch.object(operation_diagnostics, "_path", return_value=self.events),
            patch("services.qianchuan_session.current_session_owner", return_value="owner"),
        ]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)
        self.task_uid = "task-123"
        self.target_uid = "target-123"
        self.material_id = "7681234567890123"
        trigger = {"group_combine": "or", "groups": [{"join": "and", "conditions": [
            {"metric": "overallPayRoi", "op": "gt", "value": 4},
            {"metric": "currentCost", "op": "gt", "value": 100},
        ]}]}
        self.card_row = {"id": self.material_id, "overallPayRoi": 5, "currentCost": 120,
                         "periodEndTime": "2026-09-12 18:00:00", "metricRowState": "reported"}
        self.payload = {
            "aavid": "1862251436023940", "ad_id": "1865969523311863",
            "target_uid": self.target_uid, "strategy_id": "strategy-1", "strategy_hash": "a"*64,
            "promotion_scene": "live", "plan_system": "global", "triggered_at": "2026-09-12 18:00:00",
            "materials": [{"material_id": self.material_id, "material_name": "private material"}],
            "retarget_groups": [{"group_uid": "g1", "material_ids": [self.material_id]}],
            "trigger_snapshot": {"trigger_config": trigger, "materials": [{"material_id": self.material_id,
                "evaluation": {"passed": True, "groups": [{"conditions": [
                    {"metric": "overallPayRoi", "actual": 5, "threshold": 4, "op": "gt", "passed": True},
                    {"metric": "currentCost", "actual": 120, "threshold": 100, "op": "gt", "passed": True},
                ]}]} }]},
            "query_snapshot": {"query_at": "2026-09-12 18:00:00", "query_period": "1h",
                               "target": {"target_uid": self.target_uid},
                               "materials": [{"material_id": self.material_id, "material_row": self.card_row}]},
            "selection_snapshot": {"selected_at": "2026-09-12 18:01:00", "selected_material_ids": [self.material_id]},
        }
        self.store.insert("local_retarget_task", {"task_uid": self.task_uid, "account_username": "owner",
            "action_type": "retarget", "status": "failed", "action_nonce": "n", "payload_json": json.dumps(self.payload),
            "card_messages_json": "[]", "result_message": "以下素材最新数据已不满足追投规则",
            "result_detail": "RuntimeError: 以下素材最新数据已不满足追投规则", "created_at": "2099-01-01 00:00:00",
            "finished_at": "2099-01-01 00:01:00", "expires_at": "2099-01-01 00:30:00"})

    def report(self):
        return failure_report.build_failure_report(db_path=self.db_path)

    def test_card_hit_then_roi_drop_is_explicit_and_correlated(self):
        retarget_diagnostics.card_issued(self.task_uid, self.payload)
        retarget_diagnostics.card_action(self.task_uid, "approve", self.payload)
        current = dict(self.card_row, overallPayRoi=3, periodEndTime="2026-09-12 18:01:00")
        retarget_diagnostics.revalidation({**self.payload, "task_uid": self.task_uid}, self.store,
                                          rows=[current], error=RuntimeError("最新数据已不满足追投规则"))
        report = self.report()
        self.assertEqual(5, report["report_revision"])
        incident = report["incidents"][0]
        self.assertEqual("rule_no_longer_matches", incident["reason_code"])
        compare = next(event for event in incident["timeline"] if event["kind"] == "retarget_revalidation")["comparisons"][0]
        self.assertEqual(5, compare["card_evaluation"]["groups"][0]["conditions"][0]["actual"])
        self.assertEqual(3, compare["current_evaluation"]["groups"][0]["conditions"][0]["actual"])
        self.assertNotEqual(incident["incident_uid"], self.task_uid)
        self.assertNotIn("private material", json.dumps(report, ensure_ascii=False))

    def test_missing_material_reasons_are_distinct(self):
        self.store.insert("pmc_material_metric_snapshot", {"aadvid": self.payload["aavid"],
            "account_username": "owner", "target_uid": self.target_uid, "ad_id": self.payload["ad_id"],
            "material_id": self.material_id, "bucket_key": "2026-09-12 18:00:00",
            "collected_at": "2026-09-12 18:00:00", "stat_date": "2026-09-12"})
        retarget_diagnostics.revalidation({**self.payload, "task_uid": self.task_uid}, self.store,
            error=RuntimeError("最新素材数据中已找不到素材"))
        event = operation_diagnostics.read_events()[-1]
        self.assertEqual("latest_missing_history_only", event["material_probes"][0]["reason_code"])
        self.store.insert("pmc_promotion_material_latest", {"aadvid": self.payload["aavid"],
            "target_uid": self.target_uid, "ad_id": self.payload["ad_id"], "material_id": self.material_id,
            "delivery_state": "paused", "collected_at": "2026-09-12 18:01:00", "stat_date": "2026-09-12"})
        retarget_diagnostics.revalidation({**self.payload, "task_uid": self.task_uid}, self.store,
            error=RuntimeError("最新素材数据中已找不到素材"))
        self.assertEqual("excluded_delivery_state", operation_diagnostics.read_events()[-1]["material_probes"][0]["reason_code"])

    def test_present_local_but_query_excluded_or_incomplete_is_not_deleted(self):
        from datetime import datetime
        self.store.insert("promotion_target", {"target_uid":self.target_uid,
            "aadvid":self.payload["aavid"], "ad_id":self.payload["ad_id"], "enabled":1,
            "monitor_eligible":1, "promotion_scene":"live"})
        self.store.insert("pmc_promotion_material_latest", {"aadvid":self.payload["aavid"],
            "target_uid":self.target_uid, "ad_id":self.payload["ad_id"],
            "material_id":self.material_id, "delivery_state":"delivering",
            "collected_at":datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        task = {**self.payload, "task_uid":self.task_uid}
        retarget_diagnostics.revalidation(task, self.store,
            error=RuntimeError("最新素材数据中已找不到素材"))
        probe = operation_diagnostics.read_events()[-1]["material_probes"][0]
        self.assertEqual("excluded_by_current_query", probe["reason_code"])
        retarget_diagnostics.revalidation(task, self.store,
            error=RuntimeError("素材列表变化，分页不完整"))
        event = operation_diagnostics.read_events()[-1]
        self.assertEqual("pagination_incomplete", event["reason_code"])
        self.assertEqual("pagination_incomplete", event["material_probes"][0]["reason_code"])

    def test_diagnostic_query_failure_does_not_change_execution(self):
        with patch.object(self.store, "select_one", side_effect=sqlite3.OperationalError("diagnostic query failed")):
            retarget_diagnostics.revalidation({**self.payload, "task_uid":self.task_uid}, self.store,
                error=RuntimeError("最新素材数据中已找不到素材"))
        probe = operation_diagnostics.read_events()[-1]["material_probes"][0]
        self.assertEqual("diagnostic_query_error", probe["reason_code"])
        self.assertEqual("OperationalError", probe["query_error_type"])

    def test_query_error_is_coverage_not_empty_success(self):
        conn = sqlite3.connect(self.db_path); conn.row_factory = sqlite3.Row
        try:
            with patch("services.failure_report_v5._task_candidates", side_effect=sqlite3.OperationalError("bad query")):
                sections = build_sections(conn, sanitize=failure_report.sanitize, current_database=False)
        finally:
            conn.close()
        self.assertEqual("query_failed", sections["summary"]["status"])
        self.assertEqual("OperationalError", sections["coverage"]["query_errors"][0]["error_type"])

    def test_multigroup_and_success_comparison_are_kept_exact(self):
        result = {"group_results": [{"group_index": 1, "status": "succeeded", "regulate_task_ids": ["9001"]},
                                    {"group_index": 2, "status": "failed", "message": "not eligible"},
                                    {"group_index": 3, "status": "succeeded", "regulate_task_ids": ["9003"]}]}
        self.store.update("local_retarget_task", {"status": "partial_succeeded", "result_json": json.dumps(result)},
                          where={"task_uid": self.task_uid})
        success_uid = "task-success"
        self.store.insert("local_retarget_task", {"task_uid": success_uid, "account_username": "owner",
            "action_type": "retarget", "status": "succeeded", "action_nonce": "s", "payload_json": json.dumps(self.payload),
            "card_messages_json": "[]", "result_json": json.dumps({"regulate_task_ids":["9010"]}),
            "created_at": "2099-01-01 00:02:00", "finished_at": "2099-01-01 00:03:00", "expires_at": "2099-01-01 00:32:00"})
        report = self.report()
        self.assertEqual(3, len(report["incidents"][0]["groups"]))
        self.assertEqual(1, len(report["comparisons"]))
        self.assertNotEqual(report["incidents"][0]["incident_uid"], report["comparisons"][0]["incident_uid"])

    def test_legacy_confirmation_is_marked_not_recorded(self):
        report = self.report()
        self.assertEqual("not_recorded", report["incidents"][0]["confirmation_snapshot"]["status"])

    def test_nested_card_and_request_secrets_never_escape_as_json_strings(self):
        self.store.insert("feishu_outbox", {"outbox_uid":"out","account_username":"owner",
            "operation":"update_card","task_uid":self.task_uid,"message_id":"msg","status":"failed",
            "payload_json":json.dumps({"frozen_content":"PRIVATE CARD BODY", "access_token":"TOKEN-LEAK",
                                       "business_version":"v1","content_sha256":"h1"})})
        self.store.insert("qianchuan_api_audit", {"request_uid":"req-uid","account_username":"owner",
            "endpoint":"/x","method":"POST","request_id":"request-safe","status":"failed",
            "request_summary_json":json.dumps({"headers":{"Authorization":"Bearer TOKEN-LEAK"},
                                               "body":{"advertiser_id":self.payload["aavid"],"secret":"TOKEN-LEAK"}}),
            "response_summary_json":json.dumps({"code":400,"raw":"PRIVATE RESPONSE"})})
        # Exact incident linkage uses a reconciliation request id.
        report = self.report()
        raw = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("TOKEN-LEAK", raw)
        self.assertNotIn("PRIVATE CARD BODY", raw)
        self.assertNotIn("PRIVATE RESPONSE", raw)

    def test_success_from_other_scope_is_not_a_comparison(self):
        other = dict(self.payload, aavid="different-account")
        self.store.insert("local_retarget_task", {"task_uid":"other-success","account_username":"owner",
            "action_type":"retarget","status":"succeeded","action_nonce":"x","payload_json":json.dumps(other),
            "card_messages_json":"[]","created_at":"2099-01-01 00:02:00","expires_at":"2099-01-01 00:32:00"})
        self.assertEqual([], self.report()["comparisons"])

    def test_size_limit_is_explicit(self):
        report = {"coverage": {}, "comparisons": [{"x":"a"*2_000_000} for _ in range(3)],
                  "incidents": [], "api_recent": [], "material_metric_evidence": [],
                  "operation_evidence": [], "diagnostic_events": []}
        result = enforce_size(report)
        self.assertLessEqual(result["coverage"]["report_bytes"], MAX_REPORT_BYTES)
        self.assertTrue(result["coverage"]["truncated"])

    def test_single_huge_incident_and_legacy_section_are_bounded(self):
        incident = {"incident_uid":"task-huge", "reason_code":"failed", "status":"failed",
                    "timeline":[{"stage":"failed", "detail":"x"*1_000_000}]}
        bounded = _cap_incident(incident)
        self.assertLessEqual(len(json.dumps(bounded).encode()), MAX_INCIDENT_BYTES)
        self.assertTrue(bounded["capacity"]["truncated"])
        report = {"report_revision":5, "summary":{"status":"failed"}, "coverage":{},
                  "incidents":[bounded], "comparisons":[], "old_section":["a"*6_000_000]}
        result = enforce_size(report)
        self.assertLessEqual(result["coverage"]["report_bytes"], MAX_REPORT_BYTES)
        self.assertIn("old_section", [item["stage"] for item in result["coverage"]["size_omissions"]])


class DiagnosticCapacityTests(unittest.TestCase):
    def test_retention_count_size_and_set_normalization(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(operation_diagnostics, "_path", return_value=Path(temp)/"e.json"), \
             patch.object(operation_diagnostics, "MAX_EVENTS", 3), patch.object(operation_diagnostics, "MAX_FILE_BYTES", 30_000):
            for index in range(6):
                operation_diagnostics.record("sample", index=index, ids={"b", "a"}, text="x"*100)
            rows = operation_diagnostics.read_events()
            self.assertEqual(3, len(rows))
            self.assertEqual(["a", "b"], rows[-1]["ids"])
            self.assertLessEqual((Path(temp)/"e.json").stat().st_size, 30_000)

    def test_records_older_than_seven_days_are_removed(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(operation_diagnostics, "_path", return_value=Path(temp)/"e.json"):
            path=Path(temp)/"e.json"
            path.write_text(json.dumps([{"at":"2000-01-01 00:00:00","kind":"old"}]),encoding="utf-8")
            operation_diagnostics.record("new")
            self.assertEqual(["new"],[row["kind"] for row in operation_diagnostics.read_events()])


if __name__ == "__main__":
    unittest.main()
