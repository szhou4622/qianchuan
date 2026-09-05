import json
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import tests.test_official_operation_logs as fixtures
from services import material_backfill as backfill
from services import official_api_collection as collection
from services import official_api_operation_logs as logs
from services.qianchuan_accounts import get_qianchuan_account
from services.qianchuan_open_api.errors import ApiRateLimitError, ApiRequestError
from services.qianchuan_open_api.client import ApiResponse


class RecoveryFixture(unittest.TestCase):
    _add_target = fixtures.OfficialOperationLogWindowTests._add_target

    def setUp(self):
        fixtures.OfficialOperationLogWindowTests.setUp(self)
        self._add_target()
        self.account = get_qianchuan_account("1001", db=self.db)
        self.uid = "target-log-1001-2001"
        self.capability = {
            "marketing_goal": "VIDEO_PROM_GOODS", "report_metric_units": {"stat_cost_for_roi2": "3"},
            "unrelated_hot": "preserve", "collection_committed_at": "2026-09-05 02:59:00",
            backfill.STATE_KEY: {"2026-09-03": {"phase": "d1", "status": "succeeded",
                                                "last_success_at": "2026-09-04 02:01:00"}},
        }
        self.db.update("promotion_target", {"capability_json": json.dumps(self.capability),
            "last_sync_at": "2026-09-05 02:59:00"}, where={"target_uid": self.uid})
        self.target = self.db.select_one("promotion_target", where={"target_uid": self.uid})
        class Clock(datetime):
            current = datetime(2026, 9, 5, 3)
            @classmethod
            def now(cls, tz=None):
                return cls.current
        self.clock = Clock
        self.stack = ExitStack()
        for module in (collection, backfill, logs):
            self.stack.enter_context(patch.object(module, "datetime", Clock))
        stop = MagicMock()
        stop.is_set.return_value = False
        self.stack.enter_context(patch.object(collection, "_STOP", stop))
        self.service = MagicMock()
        self.service.OPERATION_LOGS = "/mock/logs"
        self.service.list_operation_logs.return_value = ([], ["mock-log-request"])
        self.service.list_plan_materials.return_value = ([{"material_id": "3001", "material_status": "PAUSED",
            "stats_info": {"stat_cost_for_roi2": 80}}], ["mock-material-request"])
        self.service.list_material_report.return_value = ([], ["mock-report-request"])
        self.stack.enter_context(patch.object(collection, "get_official_api_service", return_value=self.service))
        self.stack.enter_context(patch.object(logs, "get_official_api_service", return_value=self.service))

    def tearDown(self):
        self.stack.close()
        fixtures.OfficialOperationLogWindowTests.tearDown(self)

    def state(self):
        return json.loads(self.db.select_one("promotion_target", where={"target_uid": self.uid})["capability_json"])

    def claim_backfill(self):
        # SQLite's clock remains real; only the test job is made claimable when
        # the deterministic phase clock moves into tomorrow.
        self.db.update("collection_job", {"due_at": "2000-01-01 00:00:00"},
                       where={"job_kind": backfill.JOB_KIND})
        jobs = collection._claim_collection_jobs(db=self.db, limit=1, kind=backfill.JOB_KIND)
        self.assertEqual(1, len(jobs))
        return jobs[0]

    def finish_backfill(self):
        job = self.claim_backfill()
        result = backfill.run_material_backfill_job(job, db=self.db)
        collection._finish_collection_job(job, result, db=self.db)
        return result


class MaterialBackfillTests(RecoveryFixture):
    def test_degraded_account_history_yields_its_slot_between_pages_to_hot_read(self):
        backfill.prepare_target(self.target, db=self.db)
        job = self.claim_backfill()
        first_entered, hot_announced = threading.Event(), threading.Event()
        hot_acquired, release_hot = threading.Event(), threading.Event()
        pages, errors = [], []
        source = MagicMock()
        def read(endpoint, query, **kwargs):
            page = query["page"]
            pages.append(page)
            if page == 1:
                first_entered.set()
                if not hot_announced.wait(3):
                    raise AssertionError("hot work did not arrive")
            return ApiResponse(data={"list": [{"id": str(page)}],
                "page_info": {"page": page, "page_size": 1, "total_number": 2}},
                raw={}, request_id=f"page-{page}")
        source.get.side_effect = read
        facade = backfill._HistoryReadClient(source, backfill._request_admission(job, self.target, db=self.db))
        account_key = collection._target_account_key(self.target)
        previous = account_key in collection._ACCOUNT_RATE_LIMITED
        collection._ACCOUNT_RATE_LIMITED.add(account_key)
        def history():
            try:
                facade.get_all_pages("/mock", {}, page_size=1, parallel_workers=3)
            except Exception as exc:
                errors.append(exc)
        def hot():
            try:
                with collection._account_collection_slot(account_key):
                    hot_acquired.set()
                    release_hot.wait(3)
            finally:
                with collection._ACTIVE_LOCK:
                    collection._ACTIVE_TARGET_UIDS.discard(self.uid)
        history_thread = threading.Thread(target=history)
        hot_thread = threading.Thread(target=hot)
        with patch.object(collection, "_STOP", threading.Event()):
            try:
                history_thread.start()
                self.assertTrue(first_entered.wait(3))
                with collection._ACTIVE_LOCK:
                    collection._ACTIVE_TARGET_UIDS.add(self.uid)
                hot_thread.start()
                hot_announced.set()
                self.assertTrue(hot_acquired.wait(3))
                self.assertEqual([1], pages)
            finally:
                hot_announced.set()
                release_hot.set()
                with collection._ACTIVE_LOCK:
                    collection._ACTIVE_TARGET_UIDS.discard(self.uid)
                history_thread.join(5)
                if hot_thread.ident:
                    hot_thread.join(5)
                if not previous:
                    collection._ACCOUNT_RATE_LIMITED.discard(account_key)
        self.assertFalse(history_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual([1, 2], pages)
        with self.assertRaisesRegex(RuntimeError, "禁止写"):
            facade.post("/never-write", {})
        source.post.assert_not_called()

    def test_dispatcher_keeps_hot_collection_running_while_background_future_waits(self):
        stop_flag = [False]
        stop = MagicMock()
        stop.is_set.side_effect = lambda: stop_flag[0]
        future = MagicMock()
        future.done.return_value = False
        executor = MagicMock()
        executor.submit.return_value = future
        counts = [0]
        job = {"target_uid": self.uid, "owner_username": "log-owner", "job_kind": "hot_collection"}
        def claim(**kwargs):
            if kwargs.get("kind") == "material_backfill":
                return [dict(job, job_kind="material_backfill")]
            counts[0] += 1
            return [] if counts[0] == 1 else [job]
        def hot(**kwargs):
            stop_flag[0] = True
            return {"results": [{"target_uid": self.uid, "success": True}]}
        with patch.object(collection, "_STOP", stop), patch.object(collection, "_WAKE"), patch.object(
            collection, "ThreadPoolExecutor", return_value=executor
        ), patch.object(collection, "SQLiteStore", return_value=self.db), patch.object(
            collection, "_take_pending_targets", return_value=set()
        ), patch.object(collection, "schedulable_promotion_targets", return_value=[self.target]), patch.object(
            collection, "_target_is_due", return_value=True
        ), patch.object(collection, "_enqueue_collection_jobs"), patch.object(
            backfill, "schedule_material_backfills"
        ), patch.object(collection, "_claim_collection_jobs", side_effect=claim), patch.object(
            collection, "run_collection_cycle", side_effect=hot
        ) as hot_cycle, patch.object(collection, "_finish_collection_job"):
            collection._loop(300)
        hot_cycle.assert_called_once()
        executor.submit.assert_called_once()
        future.result.assert_not_called()

    def test_phase_rollover_preserves_a_later_existing_backoff(self):
        cap = self.state()
        cap[backfill.STATE_KEY]["2026-09-03"] = {"phase": "d1", "status": "backoff", "attempts": 2,
            "next_attempt_at": "2026-09-05 06:20:00", "last_error": "cooldown"}
        self.db.update("promotion_target", {"capability_json": json.dumps(cap)}, where={"target_uid": self.uid})
        self.clock.current = datetime(2026, 9, 5, 6, 5)
        backfill.prepare_target(self.target, db=self.db)
        entry = self.state()[backfill.STATE_KEY]["2026-09-03"]
        self.assertEqual("d2", entry["phase"])
        self.assertEqual("2026-09-05 06:20:00", entry["next_attempt_at"])

    def test_background_reads_history_only_and_preserves_hot_freshness(self):
        backfill.prepare_target(self.target, db=self.db)
        before = self.db.select_one("promotion_target", where={"target_uid": self.uid})
        with patch("services.retargeting_rule_runner.request_retargeting_rule_evaluation") as wake:
            result = self.finish_backfill()
        self.assertTrue(result["success"])
        self.assertEqual("2026-09-04", result["stat_date"])
        self.assertFalse(self.service.list_plan_materials.call_args.kwargs["delivery_only"])
        self.assertEqual(0, self.db.count("pmc_promotion_material_latest"))
        snapshot = self.db.select_one("pmc_material_metric_snapshot", where={"material_id": "3001"})
        self.assertEqual("2026-09-04", snapshot["stat_date"])
        after = self.db.select_one("promotion_target", where={"target_uid": self.uid})
        self.assertEqual(before["last_sync_at"], after["last_sync_at"])
        self.assertEqual("2026-09-05 02:59:00", self.state()["collection_committed_at"])
        wake.assert_not_called()

    def test_retry_is_persistent_bounded_and_not_reset_by_discovery(self):
        backfill.prepare_target(self.target, db=self.db)
        self.service.list_plan_materials.side_effect = ApiRequestError("分页不完整")
        for attempt in range(1, 4):
            result = self.finish_backfill()
            entry = self.state()[backfill.STATE_KEY]["2026-09-04"]
            self.assertEqual(attempt, entry["attempts"])
            self.assertFalse(result["success"])
            backfill.prepare_target(self.target, db=self.db)
            self.assertEqual(attempt, self.state()[backfill.STATE_KEY]["2026-09-04"]["attempts"])
            self.clock.current += timedelta(minutes=3)
        self.assertEqual("failed", self.state()[backfill.STATE_KEY]["2026-09-04"]["status"])
        self.assertEqual(0, self.db.count("pmc_material_metric_snapshot"))

    def test_d1_d2_are_one_successful_recheck_each_and_catch_up_once(self):
        backfill.prepare_target(self.target, db=self.db)
        self.finish_backfill()
        entry = self.state()[backfill.STATE_KEY]["2026-09-04"]
        self.assertEqual("d2", entry["phase"])
        self.assertEqual("2026-09-06 06:00:00", entry["next_attempt_at"])
        self.clock.current = datetime(2026, 9, 7, 8)
        backfill.prepare_target(self.target, db=self.db)
        # Expired D1/D2 phases of all due dates require at most one read/date.
        for _ in range(8):
            states = self.state()[backfill.STATE_KEY]
            if states["2026-09-04"].get("last_success_at", "") >= "2026-09-06 06:00:00":
                break
            self.finish_backfill()
        self.assertEqual(2, sum(call.kwargs["start_date"] == "2026-09-04"
                                for call in self.service.list_plan_materials.call_args_list))
        backfill.prepare_target(self.target, db=self.db)
        self.assertEqual("succeeded", self.state()[backfill.STATE_KEY]["2026-09-04"]["status"])

    def test_latest_json_is_merged_after_concurrent_hot_metadata_change(self):
        backfill.prepare_target(self.target, db=self.db)
        original = self.service.list_plan_materials.return_value
        def read(*args, **kwargs):
            cap = self.state()
            cap["unrelated_hot"] = "new-value"
            cap[backfill.STATE_KEY]["2026-09-01"] = {"phase": "d2", "status": "failed", "last_error": "keep"}
            self.db.update("promotion_target", {"capability_json": json.dumps(cap)}, where={"target_uid": self.uid})
            return original
        self.service.list_plan_materials.side_effect = read
        self.finish_backfill()
        self.assertEqual("new-value", self.state()["unrelated_hot"])
        self.assertEqual("keep", self.state()[backfill.STATE_KEY]["2026-09-01"]["last_error"])

    def test_lost_lease_does_not_commit_history(self):
        backfill.prepare_target(self.target, db=self.db)
        job = self.claim_backfill()
        def reports(*args, **kwargs):
            self.db.update("collection_job", {"fencing_token": int(job["fencing_token"]) + 1}, where={"id": job["id"]})
            return [], ["mock-report-request"]
        self.service.list_material_report.side_effect = reports
        result = backfill.run_material_backfill_job(job, db=self.db)
        self.assertFalse(result["success"])
        self.assertEqual(0, self.db.count("pmc_material_metric_snapshot"))


class LogRecoveryTests(RecoveryFixture):
    def enqueue(self, start, end, kind="incremental", objects=None):
        return logs._enqueue_range(self.account, start, end, request_kind=kind, objects=objects, db=self.db)

    def test_one_object_backoff_does_not_block_other_object_increment(self):
        logs._enqueue_incremental_range(self.account, self.clock.current, db=self.db)
        self.db.update("operation_log_sync_window", {"status": "backoff"}, where={"object_type": "AD"})
        self.clock.current += timedelta(minutes=5)
        result = logs._enqueue_incremental_range(self.account, self.clock.current, db=self.db)
        self.assertEqual(1, result["object_count"])
        self.assertEqual(3, self.db.count("operation_log_sync_window"))

    def test_read_started_before_phase_deadline_does_not_satisfy_later_phase(self):
        self.clock.current = datetime(2026, 9, 5, 1, 59)
        self.enqueue(datetime(2026, 9, 4), datetime(2026, 9, 4, 23, 59, 59), "history")
        task = logs._claim_window(self.db, "slow-reader")
        def read(*args, **kwargs):
            self.clock.current = datetime(2026, 9, 5, 2, 10)
            return [], ["slow-read"]
        self.service.list_operation_logs.side_effect = read
        logs._process_window(self.db, task)
        row = self.db.select_one("operation_log_sync_window", where={"id": task["id"]})
        self.assertEqual("2026-09-05 01:59:00", row["last_success_at"])
        self.assertEqual("2026-09-05 02:10:00", row["completed_at"])
        logs._schedule_verifications(self.account, db=self.db, now=self.clock.current)
        self.assertEqual("queued", self.db.select_one("operation_log_sync_window", where={"id": task["id"]})["status"])

    def test_successful_phase_repair_union_does_not_restart_full_day_forever(self):
        self.enqueue(datetime(2026, 9, 4), datetime(2026, 9, 4, 23, 59, 59), "verification")
        task = logs._claim_window(self.db, "split-phase")
        logs._split_failed_window(self.db, task, "分页不完整")
        self.db.update("operation_log_sync_window", {"status": "empty", "last_success_at": "2026-09-05 03:01:00"},
                       where={"request_kind": "repair"})
        logs._resolve_covered_failures(self.db, self.account["account_uid"], "1001")
        for _ in range(2):
            logs._schedule_verifications(self.account, db=self.db, now=self.clock.current)
            self.assertEqual("superseded", self.db.select_one("operation_log_sync_window", where={"id": task["id"]})["status"])

    def test_persisted_failed_long_windows_are_split_once_on_recovery(self):
        self.enqueue(datetime(2026, 9, 5), datetime(2026, 9, 5, 1, 9, 59), "history")
        self.db.update("operation_log_sync_window", {"status": "failed", "last_error": "分页不完整",
                                                     "completed_at": "2026-09-05 02:00:00"})
        logs._schedule_failed_repairs(self.account, db=self.db)
        self.assertEqual(2, self.db.count("operation_log_sync_window", where={"status": "repairing"}))
        self.assertEqual(6, self.db.count("operation_log_sync_window", where={"request_kind": "repair"}))
        logs._schedule_failed_repairs(self.account, db=self.db)
        self.assertEqual(8, self.db.count("operation_log_sync_window"))

    def test_single_second_gap_is_not_merged_into_coverage(self):
        account_object, ad_object = logs._sync_objects(self.account, db=self.db)
        self.enqueue(datetime(2026, 9, 5), datetime(2026, 9, 5, 1), objects=[account_object])
        self.enqueue(datetime(2026, 9, 5), datetime(2026, 9, 5, 0, 29, 58), objects=[ad_object])
        self.enqueue(datetime(2026, 9, 5, 0, 30), datetime(2026, 9, 5, 0, 59, 59), objects=[ad_object])
        self.db.update("operation_log_sync_window", {"status": "empty", "last_success_at": "2026-09-05 03:00:00"})
        start, end, complete = logs._completed_coverage(self.account["account_uid"], "1001", self.db)
        self.assertEqual("2026-09-05 00:30:00", start)
        self.assertFalse(complete)

    def test_missed_d2_phase_is_rechecked_once_after_downtime(self):
        self.enqueue(datetime(2026, 9, 4), datetime(2026, 9, 4, 23, 59, 59), "history")
        self.db.update("operation_log_sync_window", {"status": "empty", "last_success_at": "2026-09-05 03:00:00"})
        self.clock.current = datetime(2026, 9, 7, 8)
        logs._schedule_verifications(self.account, db=self.db, now=self.clock.current)
        rows = self.db.select("operation_log_sync_window", where={"window_start": "2026-09-04 00:00:00"})
        self.assertTrue(all(row["status"] == "queued" for row in rows))
        self.db.update("operation_log_sync_window", {"status": "empty", "last_success_at": "2026-09-07 08:01:00"},
                       where={"window_start": "2026-09-04 00:00:00"})
        logs._schedule_verifications(self.account, db=self.db, now=self.clock.current)
        self.assertEqual("empty", self.db.select_one("operation_log_sync_window", where={"id": rows[0]["id"]})["status"])

    def test_log_cooldown_does_not_block_another_account(self):
        from services.qianchuan_accounts import ensure_qianchuan_account
        other = ensure_qianchuan_account("1002", owner_username="log-owner", enabled=True, seen=True, db=self.db)
        self._add_target("1002", "2002")
        self.enqueue(datetime(2026, 9, 5), datetime(2026, 9, 5, 1))
        task = logs._claim_window(self.db, "rate-worker")
        self.service.list_operation_logs.side_effect = ApiRateLimitError("rate limited", retry_after=900)
        logs._process_window(self.db, task)
        logs._enqueue_range(other, datetime(2026, 9, 5), datetime(2026, 9, 5, 1), request_kind="incremental", db=self.db)
        self.assertEqual("1002", logs._claim_window(self.db, "other-worker")["aavid"])

    def test_long_gap_splits_without_holes_and_does_not_claim_false_coverage(self):
        start, end = datetime(2026, 9, 5), datetime(2026, 9, 5, 1, 9, 59)
        self.enqueue(start, end)
        self.db.update("operation_log_sync_window", {"attempt_count": 2})
        task = logs._claim_window(self.db, "split-worker")
        self.service.list_operation_logs.side_effect = ApiRequestError("分页记录数与总数不一致")
        logs._process_window(self.db, task)
        parent = self.db.select_one("operation_log_sync_window", where={"id": task["id"]})
        self.assertEqual("repairing", parent["status"])
        self.assertIsNone(parent["last_success_at"])
        children = self.db.select("operation_log_sync_window", where={"request_kind": "repair"}, order_by="window_start")
        self.assertEqual(3, len(children))
        self.assertEqual("2026-09-05 00:29:59", children[0]["window_end"])
        self.assertEqual("2026-09-05 00:30:00", children[1]["window_start"])
        self.assertEqual("2026-09-05 01:09:59", children[-1]["window_end"])
        self.assertEqual(("", "", False), logs._completed_coverage(self.account["account_uid"], "1001", self.db))

    def test_coverage_intersects_required_objects_with_different_boundaries(self):
        objects = logs._sync_objects(self.account, db=self.db)
        self.enqueue(datetime(2026, 9, 5), datetime(2026, 9, 5, 2), objects=[objects[0]])
        self.enqueue(datetime(2026, 9, 5, 0, 30), datetime(2026, 9, 5, 1), objects=[objects[1]])
        self.db.update("operation_log_sync_window", {"status": "empty", "last_success_at": "2026-09-05 03:00:00"})
        begin, end, complete = logs._completed_coverage(self.account["account_uid"], "1001", self.db)
        self.assertEqual(("2026-09-05 00:30:00", "2026-09-05 01:00:00"), (begin, end))
        self.assertFalse(complete)

    def test_rate_limit_cools_entire_log_endpoint_scope_even_new_live_window(self):
        self.enqueue(datetime(2026, 9, 5, 1), datetime(2026, 9, 5, 2))
        task = logs._claim_window(self.db, "limited-worker")
        self.service.list_operation_logs.side_effect = ApiRateLimitError("rate limited", retry_after=900)
        logs._process_window(self.db, task)
        self.clock.current += timedelta(minutes=5)
        logs._enqueue_incremental_range(self.account, self.clock.current, db=self.db)
        self.assertIsNone(logs._claim_window(self.db, "early-worker"))
        self.clock.current += timedelta(minutes=11)
        self.assertIsNotNone(logs._claim_window(self.db, "after-cooldown"))

    def test_force_refresh_and_d1_d2_do_not_reset_inflight_retries(self):
        start, end = datetime(2026, 9, 4), datetime(2026, 9, 4, 23, 59, 59)
        self.enqueue(start, end, "history")
        self.db.update("operation_log_sync_window", {"status": "empty", "last_success_at": "2026-09-05 00:01:00"})
        logs._schedule_verifications(self.account, db=self.db, now=self.clock.current)
        rows = self.db.select("operation_log_sync_window", where={"window_start": "2026-09-04 00:00:00"})
        self.assertTrue(all(row["status"] == "queued" and row["priority"] == 30 for row in rows))
        self.db.update("operation_log_sync_window", {"status": "backoff", "attempt_count": 2,
            "next_attempt_at": "2026-09-05 03:10:00"}, where={"window_start": "2026-09-04 00:00:00"})
        logs._schedule_verifications(self.account, db=self.db, now=self.clock.current)
        self.assertEqual(2, self.db.select_one("operation_log_sync_window", where={"id": rows[0]["id"]})["attempt_count"])
        self.db.update("operation_log_sync_window", {"status": "empty", "last_success_at": "2026-09-05 03:00:00"},
                       where={"window_start": "2026-09-04 00:00:00"})
        logs._enqueue_range(self.account, start, end, request_kind="manual", force_refresh=True, db=self.db)
        self.assertEqual("queued", self.db.select_one("operation_log_sync_window", where={"id": rows[0]["id"]})["status"])
        self.assertEqual("2026-09-05 03:00:00", self.db.select_one("operation_log_sync_window", where={"id": rows[0]["id"]})["last_success_at"])

    def test_multiple_successful_children_resolve_parent_only_without_gap(self):
        self.enqueue(datetime(2026, 9, 5), datetime(2026, 9, 5, 0, 59, 59))
        task = logs._claim_window(self.db, "split-worker")
        logs._split_failed_window(self.db, task, "分页不完整")
        children = self.db.select("operation_log_sync_window", where={"request_kind": "repair"}, order_by="window_start")
        self.db.update("operation_log_sync_window", {"status": "empty", "last_success_at": "2026-09-05 03:01:00"},
                       where={"id": children[0]["id"]})
        logs._resolve_covered_failures(self.db, self.account["account_uid"], "1001")
        self.assertEqual("repairing", self.db.select_one("operation_log_sync_window", where={"id": task["id"]})["status"])
        self.db.update("operation_log_sync_window", {"status": "empty", "last_success_at": "2026-09-05 03:02:00"},
                       where={"id": children[1]["id"]})
        logs._resolve_covered_failures(self.db, self.account["account_uid"], "1001")
        self.assertEqual("superseded", self.db.select_one("operation_log_sync_window", where={"id": task["id"]})["status"])


if __name__ == "__main__":
    unittest.main()
