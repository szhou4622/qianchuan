import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from api.dashboard import DashboardApi
from services.rule_wakeup import TargetWakeBatch
from services import regulation_rule_runner as stop
from services import retargeting_rule_runner as retarget
from services import retarget_task_worker as worker
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class ScopedWakeTests(unittest.TestCase):
    def test_targets_merge_and_events_during_scan_survive(self):
        wake = TargetWakeBatch()
        with patch("services.rule_wakeup.time.monotonic", return_value=10):
            self.assertFalse(wake.request(set()))
            self.assertFalse(wake.event.is_set())
            wake.request({"A"})
            wake.request({"B", "A"})
            self.assertEqual(1, wake.remaining())
        with patch("services.rule_wakeup.time.monotonic", return_value=11):
            self.assertEqual(0, wake.remaining())
        self.assertEqual((True, {"A", "B"}), wake.take())
        wake.request({"C"})
        self.assertTrue(wake.event.is_set())
        self.assertEqual((True, {"C"}), wake.take())
        self.assertEqual((False, set()), wake.take())

    def test_full_scan_dominates_pending_targets(self):
        wake = TargetWakeBatch()
        wake.request({"A"})
        wake.request(None)
        wake.request({"B"})
        self.assertEqual((True, None), wake.take())
        self.assertEqual((True, None), wake.take(full_scan=True))

    def test_unrelated_target_skips_dashboard_but_still_polls_approved_stop(self):
        config = {"enabled": True, "strategies": [{"id": "rule", "target_uid": "A"}]}
        with patch.object(retarget, "load_rule_retargeting_config", return_value=config), patch.object(retarget, "DashboardApi") as dash:
            asyncio.run(retarget.run_one_cycle(Mock(), target_uids={"B"}))
            dash.assert_not_called()
        with patch.object(stop, "load_rule_regulation_config", return_value=config), \
             patch.object(stop, "_process_approved_stop_tasks", new_callable=AsyncMock) as approved, \
             patch("services.qianchuan_session.current_session_owner", return_value="owner"), \
             patch("services.qianchuan_session.automation_session_ready", return_value={"ready": True, "session_epoch": 1}), \
             patch.object(stop, "DashboardApi") as dash:
            asyncio.run(stop.run_one_cycle(Mock(), target_uids={"B"}))
            dash.assert_not_called()
            approved.assert_awaited_once()

    def test_stop_initial_report_rejection_never_calls_execution(self):
        task = {"task_uid": "card", "claim_token": "old-token", "fencing_token": 1,
                "aavid": "10001", "ad_id": "20002", "target_uid": "target", "assist_task_id": "30003",
                "rule_snapshot": {"id": "strategy", "trigger": {"groups": [1]}},
                "trigger": {"groups": [1]}, "regulation_stop_action": "pause"}
        with patch("services.local_feishu_bridge.pull_local_stop_task", return_value={"success": True, "data": task}), \
             patch("services.local_feishu_bridge.report_local_stop_task", return_value={"success": False, "message": "lease invalid"}) as report, \
             patch("services.qianchuan_session.current_session_owner", return_value="owner"), \
             patch.object(stop.QianChuanRegulationStopService, "from_rule_file_dict") as service:
            asyncio.run(stop._process_approved_stop_tasks(Mock(), max_tasks=1))
            service.assert_not_called()
            self.assertEqual(1, report.call_args.kwargs["fencing_token"])

    def test_heartbeat_failure_cancels_before_send_across_to_thread(self):
        async def scenario():
            event = threading.Event()
            token = worker._LEASE_CANCEL_EVENT.set(event)
            try:
                with patch.object(worker, "_WORKER_STOP") as stopping, patch.object(worker, "report_retarget_task", return_value={"success": False}), patch.object(worker.asyncio, "sleep", new_callable=AsyncMock):
                    stopping.is_set.return_value = False
                    stopping.wait.return_value = False
                    await worker._heartbeat_lease("card", "token", fencing_token=2, cancelled=event)
                    self.assertTrue(event.is_set())
                    with self.assertRaisesRegex(RuntimeError, "领取权"):
                        await asyncio.to_thread(worker._assert_execution_not_cancelled)
            finally:
                worker._LEASE_CANCEL_EVENT.reset(token)
        asyncio.run(scenario())


class R19MetricMigrationTests(unittest.TestCase):
    def test_legacy_success_timestamp_is_backfilled_but_failure_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "old.db")
            columns = dict(SQLiteStore.TABLE_SCHEMAS["operation_log_sync_window"]["columns"])
            columns.pop("last_success_at")
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE operation_log_sync_window (" + ",".join(f'"{k}" {v}' for k,v in columns.items()) + ")")
                for i, state in enumerate(("succeeded", "empty", "failed")):
                    connection.execute("INSERT INTO operation_log_sync_window(window_uid,aavid,window_start,window_end,status,completed_at) VALUES (?,?,?,?,?,?)",
                                       (str(i), str(i), "2026-09-01 00:00:00", "2026-09-01 23:59:59", state, "2026-09-02 01:00:00"))
            init_sqlite_schema(database=path)
            store = SQLiteStore(database=path)
            rows = store.select("operation_log_sync_window", order_by="id")
            self.assertEqual(["2026-09-02 01:00:00", "2026-09-02 01:00:00", None], [r["last_success_at"] for r in rows])
            init_sqlite_schema(database=path)
            self.assertIsNone(store.select_one("operation_log_sync_window", where={"status": "failed"})["last_success_at"])

    def test_metrics_filter_does_not_fall_back_to_recent_status_update(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "metrics.db")
            init_sqlite_schema(database=path)
            db = SQLiteStore(database=path)
            now = datetime.now()
            for task_id, observed in (("old", (now-timedelta(hours=1)).isoformat()), ("missing", None), ("fresh", now.isoformat())):
                db.insert("pmc_roi2_assist_task", {"assist_task_id": task_id, "aadvid": "10001", "ad_id": "20002", "target_uid": "A",
                                                  "ad_delivery_type": 0, "metrics_observed_at": observed})
            api = DashboardApi.__new__(DashboardApi)
            api.db = db
            result = api.get_roi2_assist_table_data(regulation_full_scan=True, assist_updated_within_minutes=10, target_uids=["A"])
            self.assertTrue(result["success"], result)
            self.assertEqual(["fresh"], [row["assist_task_id"] for row in result["data"]])
            self.assertEqual([], api.get_roi2_assist_table_data(regulation_full_scan=True, target_uids=["B"])["data"])


if __name__ == "__main__":
    unittest.main()
