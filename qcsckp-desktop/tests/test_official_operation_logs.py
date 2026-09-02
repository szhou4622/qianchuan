import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.official_api_operation_logs import (
    _claim_window,
    _enqueue_range,
    _parse_manual_range,
    _process_window,
    sync_official_operation_logs,
)
from services.qianchuan_open_api.errors import ApiRateLimitError
from services.qianchuan_accounts import (
    ensure_qianchuan_account,
    get_qianchuan_account,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class OfficialOperationLogWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temp.name, "official-logs.db")
        init_sqlite_schema(database=self.database)
        self.db = SQLiteStore(database=self.database)
        self.owner = patch.dict(os.environ, {"QCSCKP_SESSION_OWNER": "log-owner"})
        self.owner.start()
        ensure_qianchuan_account(
            "1001",
            owner_username="log-owner",
            account_name="日志账户",
            enabled=True,
            seen=True,
            db=self.db,
        )

    def _add_target(self, aavid="1001", ad_id="2001"):
        account = get_qianchuan_account(aavid, db=self.db)
        self.db.insert(
            "promotion_target",
            {
                "target_uid": f"target-log-{aavid}-{ad_id}",
                "account_uid": account["account_uid"],
                "aadvid": aavid,
                "ad_id": ad_id,
                "plan_name": f"监控计划 {ad_id}",
                "promotion_scene": "product",
                "plan_system": "global",
                "platform_status": "active",
                "verification_state": "verified",
                "monitor_eligible": 1,
                "retarget_eligible": 1,
                "stop_eligible": 1,
                "enabled": 1,
            },
        )

    def tearDown(self):
        self.owner.stop()
        self.temp.cleanup()

    def test_initial_thirty_day_backfill_advances_in_bounded_windows(self):
        service = MagicMock()
        service.OPERATION_LOGS = "/operation/logs"
        service.list_operation_logs.return_value = ([], ["req-log"])
        with patch(
            "services.official_api_operation_logs.get_official_api_service",
            return_value=service,
        ):
            first = sync_official_operation_logs("1001", days=30, db=self.db)
            second = sync_official_operation_logs("1001", days=30, db=self.db)
        first_start = datetime.strptime(first["sync_window_from"], "%Y-%m-%d %H:%M:%S")
        first_end = datetime.strptime(first["coverage_to"], "%Y-%m-%d %H:%M:%S")
        second_end = datetime.strptime(second["coverage_to"], "%Y-%m-%d %H:%M:%S")
        self.assertLessEqual((first_end - first_start).total_seconds(), 24 * 3600)
        self.assertTrue(first["backfill_remaining"])
        self.assertGreater(second_end, first_end)
        state = self.db.select_one(
            "platform_log_sync_state", where={"aavid": "1001"}
        )
        self.assertEqual("backfilling", state["last_status"])

    def test_manual_seven_day_range_is_persisted_newest_first(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        start, end = _parse_manual_range("2026-08-14", "2026-08-20")
        result = _enqueue_range(
            account,
            start,
            end,
            request_kind="manual",
            db=self.db,
            batch_uid="batch-seven-days",
        )
        self.assertEqual(7, result["window_count"])
        self.assertEqual(14, result["task_count"])
        self.assertEqual(2, result["object_count"])
        claimed = _claim_window(self.db, "worker-one")
        self.assertEqual("2026-08-20 00:00:00", claimed["window_start"])
        self.assertEqual("manual", claimed["request_kind"])

    def test_account_without_selected_plan_cannot_enqueue_logs(self):
        account = get_qianchuan_account("1001", db=self.db)
        start, end = _parse_manual_range("2026-08-20", "2026-08-20")
        with self.assertRaisesRegex(ValueError, "勾选至少一条监控计划"):
            _enqueue_range(
                account,
                start,
                end,
                request_kind="manual",
                db=self.db,
            )

    def test_same_account_is_serial_but_another_account_can_be_claimed(self):
        self._add_target()
        first = get_qianchuan_account("1001", db=self.db)
        second = ensure_qianchuan_account(
            "1002",
            owner_username="log-owner",
            account_name="日志账户2",
            enabled=True,
            seen=True,
            db=self.db,
        )
        self._add_target("1002", "2002")
        start, end = _parse_manual_range("2026-08-20", "2026-08-20")
        _enqueue_range(first, start, end, request_kind="manual", db=self.db)
        claimed_first = _claim_window(self.db, "worker-one")
        self.assertEqual("1001", claimed_first["aavid"])
        self.assertIsNone(_claim_window(self.db, "worker-two"))
        _enqueue_range(second, start, end, request_kind="manual", db=self.db)
        claimed_second = _claim_window(self.db, "worker-two")
        self.assertEqual("1002", claimed_second["aavid"])

    def test_only_enabled_monitored_plan_adds_ad_log_tasks(self):
        self._add_target()
        self._add_target(ad_id="2002")
        account = get_qianchuan_account("1001", db=self.db)
        start, end = _parse_manual_range("2026-08-19", "2026-08-20")
        result = _enqueue_range(
            account,
            start,
            end,
            request_kind="manual",
            db=self.db,
        )
        self.assertEqual(2, result["window_count"])
        self.assertEqual(3, result["object_count"])
        self.assertEqual(6, result["task_count"])
        types = self.db.execute(
            "SELECT object_type,COUNT(*) AS n FROM operation_log_sync_window "
            "GROUP BY object_type ORDER BY object_type",
            fetch=True,
        )
        self.assertEqual(
            [{"object_type": "ACCOUNT", "n": 2}, {"object_type": "AD", "n": 4}],
            types,
        )

    def test_completed_window_updates_progress_and_inserts_only_once(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        start, end = _parse_manual_range("2026-08-20", "2026-08-20")
        _enqueue_range(
            account,
            start,
            end,
            request_kind="manual",
            db=self.db,
            batch_uid="batch-complete",
        )
        service = MagicMock()
        service.list_operation_logs.return_value = (
            [
                {
                    "log_id": "platform-log-1",
                    "occurred_at": "2026-08-20 10:30:00",
                    "operator_name": "测试用户",
                    "object_name": "测试计划",
                    "object_id": "20001",
                    "content_title": "修改预算",
                    "content_log": "预算100元改为200元",
                    "raw": {},
                }
            ],
            ["request-1"],
        )
        with patch(
            "services.official_api_operation_logs.get_official_api_service",
            return_value=service,
        ):
            tasks = []
            for index in range(2):
                task = _claim_window(self.db, f"worker-{index}")
                tasks.append(task)
                _process_window(self.db, task)
        rows = [
            self.db.select_one("operation_log_sync_window", where={"id": task["id"]})
            for task in tasks
        ]
        self.assertTrue(all(row["status"] == "succeeded" for row in rows))
        self.assertEqual(2, sum(row["rows_seen"] for row in rows))
        self.assertEqual(1, sum(row["rows_inserted"] for row in rows))
        state = self.db.select_one(
            "platform_log_sync_state", where={"aavid": "1001"}
        )
        self.assertEqual("ok", state["last_status"])
        self.assertEqual(1, state["progress_completed"])
        self.assertEqual(1, state["progress_total"])

    def test_new_official_resume_event_requeues_target_collection(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        start, end = _parse_manual_range("2026-09-02", "2026-09-02")
        _enqueue_range(
            account,
            start,
            end,
            request_kind="manual",
            db=self.db,
            batch_uid="batch-resume",
        )
        service = MagicMock()
        service.list_operation_logs.side_effect = [
            ([], ["request-account"]),
            (
                [
                    {
                        "log_id": "resume-log",
                        "occurred_at": "2026-09-02 20:34:24",
                        "operator_name": "测试用户",
                        "object_name": "监控计划 2001",
                        "object_id": "2001",
                        "content_title": "修改",
                        "content_log": (
                            "操作内容：素材追投，ID：3001；"
                            "调控状态：已暂停 -> 调控中"
                        ),
                        "control_task_id": "3001",
                        "raw": {
                            "log_id": "resume-log",
                            "object_id": "2001",
                            "contentTitle": "修改",
                            "contentLog": [
                                "操作内容：素材追投，ID：3001",
                                "调控状态：已暂停 -> 调控中",
                            ],
                            "create_time": "2026-09-02 20:34:24",
                        },
                    }
                ],
                ["request-plan"],
            ),
        ]
        with patch(
            "services.official_api_operation_logs.get_official_api_service",
            return_value=service,
        ), patch(
            "services.official_api_collection.request_official_api_collection"
        ) as wake_collection:
            account_task = _claim_window(self.db, "worker-account")
            self.assertEqual("ACCOUNT", account_task["object_type"])
            _process_window(self.db, account_task)
            plan_task = _claim_window(self.db, "worker-plan")
            self.assertEqual("AD", plan_task["object_type"])
            _process_window(self.db, plan_task)
        wake_collection.assert_called_once_with(
            ["target-log-1001-2001"], db=self.db
        )
        event = self.db.select_one(
            "account_operation_event",
            where={"platform_event_id": "resume-log"},
        )
        self.assertEqual("control_resume", event["action_type"])
        self.assertEqual("3001", event["regulate_task_id"])

    def test_manual_range_rejects_more_than_thirty_days(self):
        with self.assertRaisesRegex(ValueError, "最多同步 30 天"):
            _parse_manual_range("2026-07-01", "2026-08-20")

    def test_rate_limit_keeps_window_and_sets_retry_time(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        start, end = _parse_manual_range("2026-08-20", "2026-08-20")
        _enqueue_range(account, start, end, request_kind="manual", db=self.db)
        task = _claim_window(self.db, "worker-one")
        service = MagicMock()
        service.OPERATION_LOGS = "/operation/logs"
        service.list_operation_logs.side_effect = ApiRateLimitError("请求过于频繁")
        with patch(
            "services.official_api_operation_logs.get_official_api_service",
            return_value=service,
        ):
            _process_window(self.db, task)
        row = self.db.select_one(
            "operation_log_sync_window", where={"id": task["id"]}
        )
        self.assertEqual("backoff", row["status"])
        self.assertTrue(str(row["next_attempt_at"]))
        state = self.db.select_one(
            "platform_log_sync_state", where={"aavid": "1001"}
        )
        self.assertEqual("syncing", state["last_status"])
        self.assertTrue(str(state["next_retry_at"]))

    def test_operation_page_passes_filter_dates_and_translates_backfill(self):
        html = (
            Path(__file__).resolve().parents[1] / "static" / "operation_events.html"
        ).read_text(encoding="utf-8")
        self.assertIn("syncOperationLogsNow(f.aavid,f.date_from,f.date_to)", html)
        self.assertIn("backfilling:'正在补录历史日志'", html)
        self.assertIn("进度：", html)


if __name__ == "__main__":
    unittest.main()
