import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.failure_report import build_failure_report, sanitize
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class ReportRecoveryEvidenceTests(unittest.TestCase):
    def test_parameter_help_is_readable_but_no_credentials_or_identity_leak(self):
        payload = {
            "request_id": "2026090722091659A364F200B5EB48EB2B",
            "request": {"body": {"app_secret": "PRIVATE-SECRET"}, "account_name": "私密账户"},
            "response": {"help_message": "参数 page_size 超过100，私密账户 app_secret=PRIVATE-SECRET；access_token=PRIVATE-TOKEN；账户1862251436023940；https://private.invalid/?token=VALUE"},
        }
        safe = sanitize(payload)
        help_text = safe["response"]["help_message"]["text"]
        self.assertIn("page_size", help_text)
        self.assertIn("超过100", help_text)
        self.assertEqual(payload["request_id"], safe["request_id"])
        encoded = json.dumps(safe, ensure_ascii=False)
        for value in ("PRIVATE-SECRET", "PRIVATE-TOKEN", "1862251436023940", "私密账户", "private.invalid"):
            self.assertNotIn(value, encoded)

    def test_active_collection_and_old_expiry_are_not_execution_failures(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "qianchuan.db")
            init_sqlite_schema(database=path)
            db = SQLiteStore(database=path)
            db.insert("promotion_target", {"target_uid": "t", "account_uid": "a", "aadvid": "1", "ad_id": "2", "promotion_scene": "product", "plan_system": "global", "enabled": 1, "last_status": "collecting", "last_error": ""})
            for status in ("expired", "failed", "unknown_requires_review"):
                db.insert("local_retarget_task", {"task_uid": status, "account_username": "owner", "action_type": "retarget", "status": status, "action_nonce": status, "payload_json": "{}", "expires_at": "2026-01-01 00:00:00"})
            report = build_failure_report(db_path=path)
            self.assertEqual([], report["target_errors"])
            self.assertEqual(1, len(report["targets_in_progress"]))
            self.assertEqual(["failed"], [r["status"] for r in report["retarget_failures"]])
            self.assertEqual(["expired"], [r["status"] for r in report["task_lifecycle"]])
            self.assertEqual(["unknown_requires_review"], [r["status"] for r in report["outcomes_requiring_review"]])

    def test_failure_survives_more_than_two_hundred_successful_pages(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "qianchuan.db")
            init_sqlite_schema(database=path)
            db = SQLiteStore(database=path)
            for i in range(210):
                db.insert("qianchuan_api_audit", {"request_uid": str(i), "endpoint": "/open_api/example/", "method": "GET", "status": "failed" if i == 0 else "success", "error_code": "400153" if i == 0 else "0", "response_summary_json": json.dumps({"help_message": "字段错误 page_size"})})
            report = build_failure_report(db_path=path)
            self.assertEqual(200, len(report["api_recent"]))
            self.assertEqual(1, len(report["api_failures"]))
            self.assertEqual("400153", report["api_failures"][0]["error_code"])


if __name__ == "__main__":
    unittest.main()
