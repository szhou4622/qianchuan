"""Local status/diagnostic rendering only; no live API, workers or business DB."""
import json
from contextlib import closing
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api.promotion_targets import _target_row
from services.failure_report import build_failure_report


def target(status="error", **capability):
    return {"target_uid": "private-target", "enabled": 1, "account_enabled": 1,
            "capacity_state": "active", "last_status": status, "last_error": "素材分页失败",
            "last_sync_at": "2026-09-08 12:00:00", "capability_json": json.dumps(capability)}


class TargetStatusTests(unittest.TestCase):
    def test_auth_and_permission_never_become_rate_limit(self):
        for status, cap, health in (
            ("auth_required", {}, "auth_required"), ("permission_denied", {}, "permission_denied"),
            ("rate_limited", {"collection_error_kind": "token"}, "auth_required"),
            ("rate_limited", {"collection_error_kind": "account_backoff", "collection_error_code": "41013"}, "auth_required"),
            ("error", {"collection_error_kind": "permission"}, "permission_denied"),
            ("rate_limited", {"collection_error_kind": "rate_limit"}, "backoff"),
        ):
            with self.subTest(status=status, cap=cap):
                self.assertEqual(health, _target_row(target(status, **cap))["collection_health"])

    def test_collecting_uses_live_progress_without_old_failure(self):
        with patch("services.official_api_collection.get_target_collection_progress", return_value={
            "phase": "http_send", "page": 24, "rescan_count": 1, "app_secret": "not-exported",
        }) as read:
            row = _target_row(target("collecting", collection_error_kind="token"))
        read.assert_called_once_with("private-target")
        self.assertEqual("collecting", row["collection_health"])
        self.assertEqual({"phase": "http_send", "page": 24, "rescan_count": 1}, row["collection_progress"])
        self.assertEqual("", row["collection_streams"]["material"]["error"])

    def test_resource_and_deadline_are_distinct(self):
        self.assertEqual("resource_pressure", _target_row(target("resource_pressure"))["collection_health"])
        self.assertEqual("deadline", _target_row(target(collection_error_code="client_deadline"))["collection_health"])
        self.assertEqual("error", _target_row(target("worker_unavailable"))["collection_health"])
        self.assertEqual("error", _target_row(target("collection_cancelled"))["collection_health"])

    def test_control_success_does_not_promote_failed_material(self):
        row = _target_row(target("pagination_error", assist_sync_enabled=True, assist_sync_ok=True,
                                control_task_sync_complete=True, assist_synced_at="2026-09-08 13:00:00"))
        self.assertEqual("error", row["collection_health"])
        self.assertEqual("healthy", row["collection_streams"]["control"]["status"])
        self.assertEqual("2026-09-08 12:00:00", row["collection_streams"]["material"]["last_success_at"])
        self.assertEqual("2026-09-08 13:00:00", row["collection_streams"]["control"]["last_success_at"])

    def test_control_failure_does_not_call_untrusted_read_a_success(self):
        row = _target_row(target("ok", assist_sync_ok=False, assist_synced_at="2026-09-08 13:00:00", control_error="权限错误"))
        self.assertIsNone(row["collection_streams"]["control"]["last_success_at"])
        row = _target_row(target("ok", assist_sync_ok=False, control_last_success_at="2026-09-08 11:00:00", control_error="权限错误"))
        self.assertEqual("2026-09-08 11:00:00", row["collection_streams"]["control"]["last_success_at"])


@unittest.skipUnless(shutil.which("node"), "Node.js required for actual frontend execution")
class AccountCollectionDisplayTests(unittest.TestCase):
    def run_js(self, body, names=("collectionDisplay",)):
        html = (Path(__file__).resolve().parents[1] / "static" / "qianchuan_accounts.html").read_text(encoding="utf-8")
        function = "\n".join(re.search(r"  function " + name + r"\([\s\S]*?\n  }", html).group() for name in names)
        code = "const fs=require('fs'),vm=require('vm'),assert=require('assert');const p=JSON.parse(fs.readFileSync(0,'utf8'));for(const s of p.scripts)new vm.Script(s);eval(p.fn);" + body
        result = subprocess.run([shutil.which("node"), "-e", code], input=json.dumps({
            "fn": function, "scripts": re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I),
        }), text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_authorization_is_not_throttling(self):
        self.run_js("""
            for(const status of ['auth_required','permission_denied']){
              const d=collectionDisplay({last_status:status,collection_health:'backoff'});
              assert.ok(!d.label.includes('限流'));assert.ok(d.warning);
            }
            assert.ok(collectionDisplay({last_status:'rate_limited',capability:{collection_error_code:'41013'}}).label.includes('重新授权'));
        """)

    def test_live_page_rescan_and_resources(self):
        self.run_js("""
            const d=collectionDisplay({last_status:'collecting',last_error:'OLD',collection_progress:{phase:'http_send',page:24,rescan_count:1}});
            assert.ok(d.label.includes('24'));assert.ok(d.label.includes('重采 1'));assert.strictEqual(d.warning,false);assert.ok(!d.lines.join().includes('OLD'));
            assert.ok(collectionDisplay({collection_health:'resource_pressure'}).label.includes('提交内存'));
            const waiting=collectionDisplay({last_status:'ok',collection_health:'healthy',resource_wait:{critical:true,commit_percent:98.4}});
            assert.strictEqual(waiting.health,'resource_pressure');assert.ok(waiting.label.includes('采集等待'));assert.ok(waiting.warning);
            assert.ok(collectionDisplay({collection_health:'deadline'}).label.includes('迟到结果不入库'));
            for(const status of ['worker_unavailable','collection_cancelled']){
              const d=collectionDisplay({last_status:status,collection_health:'error'});
              assert.ok(!d.label.includes('超时'));assert.ok(d.warning);
            }
        """)

    def test_material_and_control_are_separately_named(self):
        self.run_js("""
            const d=collectionDisplay({last_status:'pagination_error',collection_health:'error',collection_streams:{
              material:{last_success_at:'12:00',error:'分页失败'},control:{status:'healthy',last_success_at:'13:00'}
            }});
            assert.ok(d.lines.includes('素材最近成功：12:00'));assert.ok(d.lines.includes('调控最近成功：13:00'));
            assert.ok(d.lines.includes('素材异常：分页失败'));assert.ok(!d.label.includes('全部数据成功'));
        """)

    def test_actual_plan_row_renders_both_streams_and_escapes_errors(self):
        self.run_js("""
            const groupDefs=[['global','product','商品']];
            const filteredPlans=p=>p,scene=x=>x,system=x=>x,platformStatus=x=>x,verificationText=x=>x,capText=x=>x,dataAge=x=>x;
            const esc=v=>String(v??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
            const html=planRows([{target_uid:'t',plan_system:'global',promotion_scene:'product',monitor_eligible:true,enabled:true,capacity_state:'active',last_status:'pagination_error',collection_health:'error',collection_streams:{material:{last_success_at:'12:00',error:'<bad>'},control:{status:'healthy',last_success_at:'13:00'}}}],true);
            assert.ok(html.includes('素材最近成功：12:00'));assert.ok(html.includes('调控最近成功：13:00'));assert.ok(html.includes('&lt;bad&gt;'));assert.ok(!html.includes('<bad>'));
        """, names=("collectionDisplay", "planRows"))


class RuntimeReportTests(unittest.TestCase):
    def database(self, root):
        path = str(Path(root) / "test.sqlite3")
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE promotion_target(target_uid, aadvid, ad_id, promotion_scene, plan_system, platform_status, last_status, last_error, last_sync_at, updated_at, enabled)")
            conn.execute("INSERT INTO promotion_target VALUES ('private-target','123','456','product','global','active','collecting','','old','now',1)")
        return path

    def test_current_runtime_evidence_is_whitelisted_and_identifiers_hashed(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.database(root)
            health = {"state": "degraded", "started": True, "app_secret": "short-secret", "services": {"collection": {"status": "running", "alive": True, "path": "private-path"}},
                      "collector": {"status": "resource_pressure", "resource_pressure": {"critical": True, "commit_percent": 99.0, "cookie": "short-cookie"}}}
            with patch("services.failure_report.DB_FILE", path), patch("services.runtime_supervisor.RUNTIME_SUPERVISOR.health_snapshot", return_value=health), patch("services.official_api_collection.get_target_collection_progress", return_value={"phase": "http_send", "page": 24, "access_token": "short-token"}), patch("channel_runtime.layout", return_value=SimpleNamespace(shared=Path(root), profile=Path(root))):
                report = build_failure_report(db_path=path)
            runtime = report["runtime_health"]
            self.assertEqual("current_process", runtime["scope"])
            self.assertEqual(24, runtime["target_progress"][0]["page"])
            self.assertEqual(99, runtime["collector"]["resource_pressure"]["commit_percent"])
            for forbidden in ("private-target", "short-secret", "short-cookie", "short-token", "private-path"):
                self.assertNotIn(forbidden, json.dumps(runtime))

    def test_external_database_never_gets_this_process_health_or_events(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.database(root)
            with patch("services.failure_report.DB_FILE", str(Path(root) / "different.sqlite3")), patch("services.runtime_supervisor.RUNTIME_SUPERVISOR.health_snapshot") as health, patch("services.official_api_collection.get_target_collection_progress") as progress, patch("channel_runtime.layout", return_value=SimpleNamespace(shared=Path(root), profile=Path(root))):
                report = build_failure_report(db_path=path)
            health.assert_not_called()
            progress.assert_not_called()
            self.assertEqual("unavailable_for_external_database", report["runtime_health"]["scope"])
            self.assertEqual([], report["diagnostic_events"])

    def test_running_target_old_error_is_not_current_failure(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.database(root)
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("UPDATE promotion_target SET last_error='previous failure'")
            with patch("channel_runtime.layout", return_value=SimpleNamespace(shared=Path(root), profile=Path(root))):
                report = build_failure_report(db_path=path)
            self.assertEqual([], report["target_errors"])
            self.assertEqual(1, len(report["targets_in_progress"]))
            self.assertNotIn("previous failure", json.dumps(report["targets_in_progress"]))


if __name__ == "__main__":
    unittest.main()
