import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.official_api_operation_logs import (
    _claim_window,
    _daily_windows,
    _enqueue_incremental_range,
    _enqueue_range,
    _ensure_workers,
    _parse_manual_range,
    _process_window,
    _prune_completed_windows,
    _refresh_batch_state,
    _worker_loop,
    sync_official_operation_logs,
)
from services.qianchuan_open_api.errors import ApiRateLimitError, ApiRequestError
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

    def test_daily_windows_preserve_incremental_start_and_split_midnight(self):
        windows = _daily_windows(
            datetime(2026, 9, 4, 23, 55), datetime(2026, 9, 5, 0, 5)
        )
        self.assertEqual([
            ("2026-09-05 00:00:00", "2026-09-05 00:05:00"),
            ("2026-09-04 23:55:00", "2026-09-04 23:59:59"),
        ], windows)

    def test_scheduler_preserves_existing_backoff_and_visible_error(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        start, end = _parse_manual_range("2026-08-20", "2026-08-20")
        _enqueue_range(account, start, end, request_kind="history", db=self.db)
        task = _claim_window(self.db, "worker-one")
        service = MagicMock()
        service.list_operation_logs.side_effect = ApiRateLimitError("请求过于频繁")
        with patch("services.official_api_operation_logs.get_official_api_service", return_value=service):
            _process_window(self.db, task)
            before = self.db.select_one("operation_log_sync_window", where={"id": task["id"]})
            _enqueue_range(account, start, end, request_kind="history", db=self.db)
        after = self.db.select_one("operation_log_sync_window", where={"id": task["id"]})
        for field in ("status", "attempt_count", "next_attempt_at", "last_error"):
            self.assertEqual(before[field], after[field], field)
        state = self.db.select_one("platform_log_sync_state", where={"aavid": "1001"})
        self.assertIn("请求过于频繁", state["last_error"])
        self.assertEqual(before["next_attempt_at"], state["next_retry_at"])

    def test_incomplete_pages_fail_after_three_attempts_until_manual_retry(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        start, end = _parse_manual_range("2026-08-20", "2026-08-20")
        _enqueue_range(account, start, end, request_kind="history", db=self.db)
        self.db.update("operation_log_sync_window", {"attempt_count": 2})
        task = _claim_window(self.db, "worker-three")
        service = MagicMock()
        service.list_operation_logs.side_effect = ApiRequestError("分页记录数与总数不一致，结果不完整")
        with patch("services.official_api_operation_logs.get_official_api_service", return_value=service), patch(
            "services.official_api_operation_logs._ingest_rows"
        ) as ingest:
            _process_window(self.db, task)
            _enqueue_range(account, start, end, request_kind="history", db=self.db)
            row = self.db.select_one("operation_log_sync_window", where={"id": task["id"]})
            self.assertEqual("failed", row["status"])
            self.assertEqual(3, row["attempt_count"])
            self.assertIn("不完整", row["last_error"])
            ingest.assert_not_called()
            _enqueue_range(account, start, end, request_kind="manual", db=self.db)
        row = self.db.select_one("operation_log_sync_window", where={"id": task["id"]})
        self.assertEqual("queued", row["status"])
        self.assertEqual(0, row["attempt_count"])

    def test_worker_survives_sqlite_lock_during_claim_progress_and_completion(self):
        for stage in ("_claim_window", "_refresh_batch_state", "_process_window"):
            with self.subTest(stage=stage):
                stop = MagicMock()
                stop.is_set.side_effect = [False, False, True]
                with patch("services.official_api_operation_logs._STOP", stop), patch(
                    "services.official_api_operation_logs.SQLiteStore", return_value=self.db
                ), patch("services.official_api_operation_logs.init_sqlite_schema"), patch(
                    "services.official_api_operation_logs._claim_window", return_value={"id": 1}
                ) as claim, patch(
                    "services.official_api_operation_logs._refresh_batch_state"
                ) as progress, patch(
                    "services.official_api_operation_logs._process_window"
                ) as process:
                    selected = {"_claim_window": claim, "_refresh_batch_state": progress,
                                "_process_window": process}[stage]
                    selected.side_effect = [sqlite3.OperationalError("database is locked"),
                                            {"id": 2} if stage == "_claim_window" else None]
                    _worker_loop("mock-lock-worker")
                self.assertEqual(2, claim.call_count)
                stop.wait.assert_any_call(5.0)

    def test_concurrent_worker_recovery_starts_only_one_writer(self):
        thread_class = threading.Thread
        barrier = threading.Barrier(2)
        worker = MagicMock()
        worker.is_alive.return_value = True
        stop = MagicMock()
        stop.is_set.return_value = False
        with patch("services.official_api_operation_logs._WORKERS", []), patch(
            "services.official_api_operation_logs._STOP", stop
        ), patch("services.official_api_operation_logs.SQLiteStore", return_value=self.db), patch(
            "services.official_api_operation_logs.init_sqlite_schema"
        ), patch("services.official_api_operation_logs._migrate_legacy_coverage", side_effect=lambda db: barrier.wait(2)), patch(
            "services.official_api_operation_logs.threading.Thread", return_value=worker
        ) as factory:
            callers = [thread_class(target=_ensure_workers) for _ in range(2)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(3)
                self.assertFalse(caller.is_alive())
            factory.assert_called_once()
            worker.start.assert_called_once()

    def test_incremental_is_single_inflight_and_uses_watermark_overlap(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        first_end = datetime(2026, 9, 5, 10)
        first = _enqueue_incremental_range(account, first_end, db=self.db)
        self.assertEqual("2026-09-05 00:00:00", first["requested_from"])
        self.assertIsNone(_enqueue_incremental_range(account, first_end + timedelta(minutes=5), db=self.db))
        self.assertEqual(2, self.db.count("operation_log_sync_window"))
        self.db.update("operation_log_sync_window", {"status": "empty"})
        second = _enqueue_incremental_range(account, first_end + timedelta(minutes=5), db=self.db)
        self.assertEqual("2026-09-05 09:50:00", second["requested_from"])
        self.assertEqual("2026-09-05 10:05:00", second["requested_to"])
        self.assertEqual(4, self.db.count("operation_log_sync_window"))

    def test_new_monitored_object_does_not_inherit_another_objects_watermark(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        _enqueue_incremental_range(account, datetime(2026, 9, 5, 10), db=self.db)
        self.db.update("operation_log_sync_window", {"status": "empty"})
        self._add_target(ad_id="2002")
        result = _enqueue_incremental_range(account, datetime(2026, 9, 5, 10, 5), db=self.db)
        self.assertEqual("2026-09-05 00:00:00", result["requested_from"])
        self.assertEqual(3, result["object_count"])

    def test_disabled_objects_pending_increment_does_not_block_enabled_objects(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        _enqueue_incremental_range(account, datetime(2026, 9, 5, 10), db=self.db)
        self.db.update("operation_log_sync_window", {"status": "empty"}, where={"object_type": "ACCOUNT"})
        self.db.update("promotion_target", {"enabled": 0}, where={"ad_id": "2001"})
        self._add_target(ad_id="2002")
        result = _enqueue_incremental_range(account, datetime(2026, 9, 5, 10, 5), db=self.db)
        self.assertIsNotNone(result)
        self.assertEqual(2, result["object_count"])

    def test_completed_daily_coverage_prunes_only_successful_incremental_metadata(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        for minute in (0, 5):
            _enqueue_range(account, datetime(2026, 9, 4), datetime(2026, 9, 4, 10, minute),
                           request_kind="incremental", db=self.db)
        self.db.update("operation_log_sync_window", {"status": "empty"})
        _enqueue_range(account, datetime(2026, 9, 4, 10, 5), datetime(2026, 9, 4, 10, 10),
                       request_kind="incremental", db=self.db)
        self.db.update("operation_log_sync_window", {"status": "failed", "last_error": "不完整"},
                       where={"status": "queued"})
        _enqueue_range(account, datetime(2026, 9, 4), datetime(2026, 9, 4, 23, 59, 59),
                       request_kind="history", db=self.db)
        self.db.update("operation_log_sync_window", {"status": "empty"}, where={"status": "queued"})
        _prune_completed_windows(account, db=self.db, now=datetime(2026, 9, 5, 12))
        self.assertEqual(4, self.db.count("operation_log_sync_window"))
        self.assertEqual(2, self.db.count("operation_log_sync_window", where={"status": "failed"}))
        self.assertEqual(2, self.db.count("operation_log_sync_window", where={"request_kind": "history"}))

    def test_new_success_does_not_hide_unresolved_window_error(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        _enqueue_range(account, datetime(2026, 9, 4), datetime(2026, 9, 4, 23, 59, 59),
                       request_kind="history", db=self.db)
        self.db.update("operation_log_sync_window", {"status": "failed", "last_error": "分页不完整",
                                                     "attempt_count": 3})
        result = _enqueue_range(account, datetime(2026, 9, 5), datetime(2026, 9, 5, 10),
                                request_kind="incremental", db=self.db)
        self.db.update("operation_log_sync_window", {"status": "empty"}, where={"status": "queued"})
        _refresh_batch_state(self.db, {**result, "account_uid": account["account_uid"],
                                      "aavid": "1001", "request_kind": "incremental"})
        state = self.db.select_one("platform_log_sync_state", where={"aavid": "1001"})
        self.assertEqual("partial", state["last_status"])
        self.assertIn("分页不完整", state["last_error"])
        self.assertIn("3 次", state["last_error"])

    def test_later_complete_cover_resolves_failure_without_erasing_its_evidence(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        _enqueue_range(account, datetime(2026, 9, 4, 10), datetime(2026, 9, 4, 10, 10),
                       request_kind="incremental", db=self.db)
        self.db.update("operation_log_sync_window", {"status": "failed", "last_error": "分页不完整",
                                                     "completed_at": "2026-09-05 10:00:00"})
        result = _enqueue_range(account, datetime(2026, 9, 4), datetime(2026, 9, 4, 23, 59, 59),
                                request_kind="manual", db=self.db)
        self.db.update("operation_log_sync_window", {"status": "empty", "completed_at": "2026-09-05 09:55:00"},
                       where={"status": "queued"})
        refresh_task = {**result, "account_uid": account["account_uid"],
                        "aavid": "1001", "request_kind": "manual"}
        _refresh_batch_state(self.db, refresh_task)
        self.assertEqual(2, self.db.count("operation_log_sync_window", where={"status": "failed"}))
        self.db.update("operation_log_sync_window", {"completed_at": "2026-09-05 10:05:00"},
                       where={"status": "empty"})
        _refresh_batch_state(self.db, {**result, "account_uid": account["account_uid"],
                                      "aavid": "1001", "request_kind": "manual"})
        self.assertEqual(0, self.db.count("operation_log_sync_window", where={"status": "failed"}))
        self.assertEqual(2, self.db.count("operation_log_sync_window", where={"status": "superseded"}))
        resolved = self.db.select_one("operation_log_sync_window", where={"status": "superseded"})
        self.assertIn("分页不完整", resolved["last_error"])
        self.assertIn("已由完整窗口覆盖", resolved["last_error"])
        state = self.db.select_one("platform_log_sync_state", where={"aavid": "1001"})
        self.assertEqual("empty", state["last_status"])

    def test_other_batch_backoff_cannot_look_healthy_after_current_batch_completes(self):
        self._add_target()
        account = get_qianchuan_account("1001", db=self.db)
        _enqueue_range(account, datetime(2026, 9, 4), datetime(2026, 9, 4, 23, 59, 59),
                       request_kind="history", db=self.db)
        self.db.update("operation_log_sync_window", {"status": "backoff", "last_error": "请求过于频繁",
                                                     "next_attempt_at": "2026-09-05 15:00:00"})
        result = _enqueue_range(account, datetime(2026, 9, 5), datetime(2026, 9, 5, 10),
                                request_kind="manual", db=self.db)
        self.db.update("operation_log_sync_window", {"status": "empty"}, where={"status": "queued"})
        _refresh_batch_state(self.db, {**result, "account_uid": account["account_uid"],
                                      "aavid": "1001", "request_kind": "manual"})
        state = self.db.select_one("platform_log_sync_state", where={"aavid": "1001"})
        self.assertEqual("backfilling", state["last_status"])
        self.assertIn("请求过于频繁", state["last_error"])


if __name__ == "__main__":
    unittest.main()
