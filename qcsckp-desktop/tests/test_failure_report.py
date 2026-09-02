import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.failure_report import build_failure_report, failure_report_json, sanitize
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class FailureReportTests(unittest.TestCase):
    def test_sanitizer_keeps_debug_shape_but_removes_secrets_and_identity(self):
        source = {
            "app_secret": "SECRET-VALUE",
            "access_token": "TOKEN-VALUE",
            "advertiser_id": "1862251436023940",
            "material_ids": ["7677514675981959204"],
            "account_name": "真实账户名称",
            "budget": 100,
            "duration": 24,
            "dimensions": ["material_id", "roi2_material_video_type"],
            "filters": [{"field": "anchor_id", "values": ["1234567890123456"]}],
            "message": "错误ID 1234567890123456 https://private.example/path",
        }
        safe = sanitize(source)
        encoded = json.dumps(safe, ensure_ascii=False)
        for private in ("SECRET-VALUE", "TOKEN-VALUE", "1862251436023940", "7677514675981959204", "真实账户名称", "1234567890123456", "private.example"):
            self.assertNotIn(private, encoded)
        self.assertEqual(100, safe["budget"])
        self.assertEqual(24, safe["duration"])
        self.assertEqual(["material_id", "roi2_material_video_type"], safe["dimensions"])
        self.assertTrue(safe["account_name"]["redacted"])

    def test_report_contains_failure_sequence_and_no_plain_credentials(self):
        with tempfile.TemporaryDirectory(prefix="qcsckp-failure-report-") as root:
            db = Path(root) / "qianchuan.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE qianchuan_api_audit (
                    id INTEGER PRIMARY KEY, endpoint TEXT, method TEXT, aavid TEXT,
                    ad_id TEXT, task_id TEXT, request_id TEXT, error_code TEXT,
                    status TEXT, request_summary_json TEXT,
                    response_summary_json TEXT, created_at TEXT
                );
                CREATE TABLE local_retarget_task (
                    id INTEGER PRIMARY KEY, task_uid TEXT, action_type TEXT,
                    status TEXT, result_message TEXT, result_detail TEXT,
                    regulate_task_id TEXT, created_at TEXT, finished_at TEXT
                );
                CREATE TABLE promotion_target (
                    target_uid TEXT, aadvid TEXT, ad_id TEXT, promotion_scene TEXT,
                    plan_system TEXT, platform_status TEXT, last_status TEXT,
                    last_error TEXT, last_sync_at TEXT, updated_at TEXT, enabled INTEGER
                );
                """
            )
            conn.execute(
                "INSERT INTO qianchuan_api_audit VALUES(1,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "/open_api/v1.0/example/", "POST", "1862251436023940",
                    "1865969523311863", "", "REQ-ABCDEF1234567890", "400153",
                    "failed",
                    json.dumps({"body": {"app_secret": "SECRET", "budget": 100, "material_ids": ["7677514675981959204"]}}),
                    json.dumps({"message": "参数错误 1862251436023940"}),
                    "2026-09-01 20:40:04",
                ),
            )
            conn.execute(
                "INSERT INTO local_retarget_task VALUES(1,?,?,?,?,?,?,?,?)",
                (
                    "task-private-uid", "retarget", "failed", "失败 1862251436023940",
                    'Traceback\n  File "C:\\Users\\Private\\service.py", line 77\nApiRequestError: bad',
                    "", "2026-09-01 20:39:47", "2026-09-01 20:40:04",
                ),
            )
            conn.execute(
                "INSERT INTO promotion_target VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "target-private", "1862251436023940", "1865969523311863",
                    "live", "global", "active", "error", "账户名称真实值",
                    "", "2026-09-01 20:40:04", 1,
                ),
            )
            conn.commit()
            conn.close()
            report = build_failure_report(db_path=str(db))
            text = json.dumps(report, ensure_ascii=False)
            self.assertEqual("ok", report["database"]["quick_check"])
            self.assertEqual("/open_api/v1.0/example/", report["api_recent"][0]["endpoint"])
            self.assertEqual(100, report["api_recent"][0]["request"]["body"]["budget"])
            self.assertEqual("service.py", report["retarget_failures"][0]["trace"]["frames"][0]["file"])
            for private in ("SECRET", "1862251436023940", "1865969523311863", "7677514675981959204", "账户名称真实值", "C:\\Users\\Private"):
                self.assertNotIn(private, text)

    def test_report_module_has_no_upload_implementation(self):
        source = (Path(__file__).resolve().parents[1] / "services" / "failure_report.py").read_text(encoding="utf-8")
        for forbidden in ("urlopen", "requests.post", "diagnostics.example", "/api/qcsckp/diagnostics"):
            self.assertNotIn(forbidden, source)

    def test_report_separates_stop_failures_and_reads_shared_reconciliation(self):
        with tempfile.TemporaryDirectory(prefix="qcsckp-failure-shared-") as root:
            root_path = Path(root)
            db = root_path / "qianchuan.db"
            shared_dir = root_path / "shared"
            shared_dir.mkdir()
            shared_db = shared_dir / "execution.sqlite3"
            init_sqlite_schema(database=str(db))
            store = SQLiteStore(database=str(db))
            store.insert(
                "pmc_regulation_run",
                {
                    "aavid": "1001",
                    "ad_id": "2001",
                    "assist_task_id": "3001",
                    "started_at": "2026-09-02 10:00:00",
                    "ended_at": "2026-09-02 10:00:01",
                    "status": 1,
                    "execution_state": "confirmed_succeeded",
                },
            )
            store.insert(
                "pmc_regulation_run",
                {
                    "aavid": "1001",
                    "ad_id": "2001",
                    "assist_task_id": "3002",
                    "started_at": "2026-09-02 10:01:00",
                    "ended_at": "2026-09-02 10:01:01",
                    "status": -1,
                    "execution_state": "confirmed_failed",
                    "message": "failed",
                },
            )
            store.insert(
                "local_retarget_task",
                {
                    "task_uid": "stop-failed-card",
                    "account_username": "owner",
                    "action_type": "stop",
                    "status": "failed",
                    "action_nonce": "nonce",
                    "payload_json": "{}",
                    "expires_at": "2026-09-02 11:00:00",
                    "result_message": "failed",
                },
            )
            store.insert(
                "feishu_outbox",
                {
                    "outbox_uid": "outbox-failed",
                    "account_username": "owner",
                    "operation": "update_card",
                    "task_uid": "stop-failed-card",
                    "status": "failed",
                    "last_error": "network failed",
                },
            )
            shared = sqlite3.connect(shared_db)
            shared.execute(
                "CREATE TABLE execution_reconciliation ("
                "id INTEGER PRIMARY KEY,task_uid TEXT,action_type TEXT,status TEXT,"
                "request_id TEXT,control_task_id TEXT,last_error TEXT,attempt_count INTEGER,"
                "card_update_state TEXT,created_at TEXT,updated_at TEXT)"
            )
            shared.execute(
                "INSERT INTO execution_reconciliation VALUES(1,?,?,?,?,?,?,?,?,?,?)",
                (
                    "shared-stop",
                    "stop",
                    "confirmed_succeeded",
                    "request-1",
                    "3001",
                    "",
                    1,
                    "sent",
                    "2026-09-02 10:00:00",
                    "2026-09-02 10:00:02",
                ),
            )
            shared.commit()
            shared.close()
            with patch("services.failure_report.DB_FILE", str(db)), patch(
                "channel_runtime.layout",
                return_value=SimpleNamespace(
                    shared=shared_dir,
                    profile=root_path / "profile",
                ),
            ):
                report = build_failure_report(db_path=str(db))
            self.assertEqual(1, len(report["regulation_failures"]))
            self.assertEqual(-1, report["regulation_failures"][0]["status"])
            self.assertEqual(1, len(report["stop_failures"]))
            self.assertEqual([], report["retarget_failures"])
            self.assertEqual("sent", report["reconciliation"][0]["card_update_state"])
            self.assertEqual("failed", report["feishu_outbox"][0]["status"])

    def test_ui_exposes_local_export_without_version_diagnostics(self):
        root = Path(__file__).resolve().parents[1]
        page = (root / "static" / "index.html").read_text(encoding="utf-8")
        bridge = (root / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn('id="sidebarFailureReportBtn"', page)
        self.assertIn("api.saveFailureReport", page)
        feishu = (root / "static" / "feishu_binding.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="outboxState"', feishu)
        self.assertIn("latest.outbox", feishu)
        self.assertIn("def saveFailureReport", bridge)
        self.assertNotIn("channel-settings", page)
        self.assertNotIn("版本与诊断", page)

    def test_json_output_is_valid_and_utf8_friendly(self):
        with tempfile.TemporaryDirectory(prefix="qcsckp-empty-report-") as root:
            payload = json.loads(failure_report_json(db_path=str(Path(root) / "missing.db")))
        self.assertEqual("qcsckp-failure-report-v1", payload["schema"])
        self.assertFalse(payload["database"]["available"])
        self.assertFalse(payload["privacy"]["uploaded"])

    def test_desktop_bridge_writes_only_after_user_selects_a_file(self):
        import gui_app

        class Window:
            def __init__(self, destination):
                self.destination = destination

            def create_file_dialog(self, *_args, **_kwargs):
                return (str(self.destination),)

        with tempfile.TemporaryDirectory(prefix="qcsckp-failure-save-") as root:
            destination = Path(root) / "failure-report.json"
            bridge = object.__new__(gui_app.JSApi)
            with patch.object(gui_app.webview, "windows", [Window(destination)]), patch(
                "services.failure_report.failure_report_json",
                return_value='{"schema":"qcsckp-failure-report-v1"}\n',
            ):
                result = bridge.saveFailureReport()
            self.assertTrue(result["success"])
            self.assertFalse(result["cancelled"])
            self.assertEqual(
                {"schema": "qcsckp-failure-report-v1"},
                json.loads(destination.read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()
