"""Local-only regression coverage for result reconciliation and card ordering."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services import local_feishu_bridge as bridge
from services import official_api_reconciliation as reconciliation
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class ExecutionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "recovery.db")
        init_sqlite_schema(database=self.path)
        self.store = SQLiteStore(database=self.path)
        self.owner = "test-owner"
        self.manager = SimpleNamespace(account=self.owner, bridge=lambda: None)
        for item in (patch.object(bridge, "DB_FILE", self.path),
                     patch.object(bridge, "_MANAGER", self.manager)):
            item.start()
            self.addCleanup(item.stop)

    def task(self, uid="card", *, action="retarget", status="verifying", payload=None):
        self.store.insert("local_retarget_task", {
            "task_uid": uid, "account_username": self.owner,
            "action_type": action, "status": status, "action_nonce": "nonce",
            "claim_token": "claim", "active_dedupe_key": uid,
            "expires_at": "2099-01-01 00:00:00", "payload_json": json.dumps(payload or {}),
            "card_messages_json": json.dumps([{"message_id": "message-" + uid}]),
        })
        return uid

    def intent(self, uid="card", *, attempt=1, action="retarget", status="verifying"):
        execution_uid = f"{uid}:attempt:{attempt}"
        self.store.insert("execution_reconciliation", {
            "reconciliation_uid": execution_uid, "account_username": self.owner,
            "task_uid": uid, "action_type": action, "aavid": "10001", "ad_id": "20002",
            "control_task_id": "30003", "idempotency_key": execution_uid,
            "status": status, "lease_owner": "lease", "fencing_token": 1,
            "payload_json": json.dumps({"execution_uid": execution_uid}),
        })
        return self.store.select_one("execution_reconciliation", where={"reconciliation_uid": execution_uid})

    def run_row(self, intent):
        action = intent["action_type"]
        table = "pmc_regulation_run" if action == "stop" else "pmc_retargeting_run"
        data = {"aavid": "10001", "ad_id": "20002", "started_at": "2026-09-05 12:00:00",
                "ended_at": "2026-09-05 12:00:01", "status": -1,
                "execution_uid": intent["idempotency_key"], "execution_state": "submitted_verifying",
                "step": "submitted_verifying"}
        data["assist_task_id" if action == "stop" else "material_id"] = "30003"
        self.store.insert(table, data)
        return table

    def test_single_card_explicit_failure_then_retry_success(self):
        self.task()
        self.intent(attempt=1, status="confirmed_failed")
        current = self.intent(attempt=2)
        self.run_row(current)
        reconciliation._finish(self.store, current, status="confirmed_succeeded", verified={"status": "PROCESSING"})
        task = bridge._task_row("card")
        self.assertEqual("succeeded", task["status"])
        self.assertEqual("confirmed_succeeded", json.loads(task["result_json"])["step"])

    def test_unknown_prior_attempt_is_not_hidden_by_another_logical_group(self):
        self.task()
        self.intent("card:group:1", status="unknown_requires_review")
        self.store.execute("UPDATE execution_reconciliation SET task_uid='card'")
        current = self.intent("card:group:2")
        self.store.execute("UPDATE execution_reconciliation SET task_uid='card'")
        current["task_uid"] = "card"
        self.run_row(current)
        reconciliation._finish(self.store, current, status="confirmed_succeeded", verified={"status": "PROCESSING"})
        self.assertEqual("unknown_requires_review", bridge._task_row("card")["status"])

    def test_fast_reconciliation_then_late_report_repairs_card_and_run(self):
        for action in ("retarget", "stop"):
            with self.subTest(action=action):
                uid = self.task(action, action=action, status="executing")
                intent = self.intent(uid, action=action)
                reconciliation._finish(self.store, intent, status="confirmed_succeeded",
                                       verified={"status": "DISABLE" if action == "stop" else "PROCESSING"})
                self.assertEqual("executing", bridge._task_row(uid)["status"])
                table = self.run_row(intent)
                reply = bridge.report_local_retarget_task(uid, "claim", "verifying")
                self.assertTrue(reply["success"])
                self.assertEqual("succeeded", bridge._task_row(uid)["status"])
                run = self.store.select_one(table, where={"execution_uid": intent["idempotency_key"]})
                self.assertEqual("confirmed_succeeded", run["execution_state"])
                # Durable replay is harmless after a restart or repeated callback.
                reconciliation.replay_terminal_reconciliations(self.owner, db=self.store, task_uid=uid)
                self.assertEqual("succeeded", bridge._task_row(uid)["status"])

    def test_projection_failure_remains_replayable_after_terminal_commit(self):
        self.task()
        intent = self.intent()
        self.run_row(intent)
        original = self.store.execute
        def fail_run(sql, *args, **kwargs):
            if sql.startswith("UPDATE pmc_retargeting_run"):
                raise RuntimeError("simulated process interruption")
            return original(sql, *args, **kwargs)
        with patch.object(self.store, "execute", side_effect=fail_run):
            with self.assertRaises(RuntimeError):
                reconciliation._finish(self.store, intent, status="confirmed_succeeded", verified={"status": "PROCESSING"})
        saved = self.store.select_one("execution_reconciliation", where={"task_uid": "card"})
        self.assertEqual("confirmed_succeeded", saved["status"])
        self.assertTrue(json.loads(saved["payload_json"])["finalization_pending"])
        reconciliation.replay_terminal_reconciliations(self.owner, db=self.store)
        self.assertEqual("succeeded", bridge._task_row("card")["status"])

    def test_late_natural_expiry_report_keeps_distinct_terminal_result(self):
        self.task(action="stop", status="executing")
        intent = self.intent(action="stop")
        reconciliation._finish(self.store, intent, status="confirmed_succeeded", verified={"status": "OFFLINE_TIME"})
        self.run_row(intent)
        bridge.report_local_stop_task("card", "claim", "verifying")
        self.assertEqual("naturally_expired", bridge._task_row("card")["status"])
        self.assertIsNone(bridge._task_row("card")["active_dedupe_key"])

    def test_stop_replay_never_overwrites_new_or_same_second_resume_observation(self):
        intent = self.intent("auto-stop", action="stop")
        self.run_row(intent)
        self.store.insert("pmc_roi2_assist_task", {
            "assist_task_id": "30003", "aadvid": "10001", "ad_id": "20002",
            "ad_delivery_type": 0, "ad_delivery_name": "PROCESSING",
            "task_status_source": "api", "task_status_observed_at": "2026-09-05 11:00:00",
            "updated_at": "2026-09-05 11:00:00",
        })
        self.store.execute("UPDATE pmc_roi2_assist_task SET updated_at='2026-09-05 11:00:00'")
        with patch.object(reconciliation, "_now", return_value="2026-09-05 12:00:00"), \
             patch.object(reconciliation, "_notify_reconciled_auto_stop") as notify:
            reconciliation._finish(self.store, intent, status="confirmed_succeeded", verified={"status": "DISABLE"})
            for observed_at in ("2026-09-05 12:00:00", "2026-09-05 12:00:01"):
                self.store.execute(
                    "UPDATE pmc_roi2_assist_task SET ad_delivery_type=0,ad_delivery_name='PROCESSING',"
                    "task_status_observed_at=? WHERE assist_task_id='30003'", (observed_at,),
                )
                reconciliation.replay_terminal_reconciliations(self.owner, db=self.store, task_uid="auto-stop")
                task = self.store.select_one("pmc_roi2_assist_task", where={"assist_task_id": "30003"})
                self.assertEqual("PROCESSING", task["ad_delivery_name"])
                self.assertEqual(observed_at, task["task_status_observed_at"])
                self.assertEqual("2026-09-05 11:00:00", task["updated_at"])
            notify.assert_called_once()

    def test_auto_stop_terminal_notification_outbox_retries_without_replay_duplicates(self):
        intent = self.intent("auto-stop", action="stop")
        self.run_row(intent)
        actual = bridge.LocalFeishuBridge(self.owner)
        def notify(*args, **kwargs):
            return actual.send_bound_card({"header": {"title": {"content": "terminal stop"}}},
                                          targets=[("open_id", "recipient")], task_uid="auto-stop")
        with patch.object(reconciliation, "_notify_reconciled_auto_stop", side_effect=notify), \
             patch.object(actual, "_send_card", side_effect=RuntimeError("offline")):
            reconciliation._finish(self.store, intent, status="confirmed_succeeded", verified={"status": "DISABLE"})
        saved = self.store.select_one("execution_reconciliation", where={"task_uid": "auto-stop"})
        self.assertEqual("queued", saved["card_update_state"])
        with patch.object(actual, "_send_card", return_value="remote-message") as send:
            self.assertTrue(actual._deliver_outbox_once())
            send.assert_called_once()
        with patch.object(reconciliation, "_notify_reconciled_auto_stop") as notify_again:
            reconciliation.replay_terminal_reconciliations(self.owner, db=self.store, task_uid="auto-stop")
            notify_again.assert_not_called()
        self.assertEqual("sent", self.store.select_one("execution_reconciliation", where={"task_uid": "auto-stop"})["card_update_state"])

    def test_background_and_foreground_projection_do_not_send_duplicate_terminal_cards(self):
        intent = self.intent("auto-stop", action="stop")
        self.run_row(intent)
        entered, release = threading.Event(), threading.Event()
        def notify(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
        with patch.object(reconciliation, "_notify_reconciled_auto_stop", side_effect=notify) as send:
            worker = threading.Thread(target=reconciliation._finish, args=(self.store, intent),
                                      kwargs={"status": "confirmed_succeeded", "verified": {"status": "DISABLE"}})
            worker.start()
            self.assertTrue(entered.wait(5))
            replay = threading.Thread(target=reconciliation.replay_terminal_reconciliations,
                                      args=(self.owner,), kwargs={"db": self.store, "task_uid": "auto-stop"})
            replay.start()
            release.set()
            worker.join(5)
            replay.join(5)
            self.assertFalse(worker.is_alive() or replay.is_alive())
            send.assert_called_once()

    def test_stop_terminal_reports_release_dedupe_and_do_not_coerce_unknown(self):
        for state in ("naturally_expired", "unknown_requires_review"):
            self.task(state, action="stop", status="executing")
            result = bridge.report_local_stop_task(state, "claim", state, result={"step": state})
            self.assertTrue(result["success"])
            row = bridge._task_row(state)
            self.assertEqual(state, row["status"])
            self.assertIsNone(row["active_dedupe_key"])
        self.task("unknown-reconciliation", action="stop")
        row = self.intent("unknown-reconciliation", action="stop")
        self.run_row(row)
        reconciliation._finish(self.store, row, status="unknown_requires_review")
        self.assertEqual("unknown_requires_review", bridge._task_row("unknown-reconciliation")["status"])
        self.assertIn("无法确认", bridge._task_row("unknown-reconciliation")["result_message"])
        self.assertNotIn("失败", bridge._task_row("unknown-reconciliation")["result_message"])

    def test_stop_terminal_cards_have_chinese_status_and_no_confirmation_button(self):
        for status, label in (("invalidated", "策略已更新，本卡失效"),
                              ("unknown_requires_review", "结果无法确认，需人工检查")):
            card = bridge.build_stop_task_card({"task_uid": "card", "status": status})
            rendered = json.dumps(card, ensure_ascii=False)
            self.assertIn(label, rendered)
            self.assertNotIn(status, rendered)
            self.assertNotIn('"action": "approve"', rendered)
            self.assertEqual("orange", card["header"]["template"])

    def test_auto_stop_stale_submitted_send_is_suppressed_after_terminal(self):
        actual = bridge.LocalFeishuBridge(self.owner)
        self.intent("auto-stop", action="stop", status="submitted")
        with patch.object(actual, "_send_card", side_effect=RuntimeError("offline")):
            with self.assertRaises(bridge.FeishuApiError):
                actual.send_bound_card({"phase": "submitted"}, targets=[("open_id", "recipient")],
                                       task_uid="auto-stop", delivery_stage="submitted")
        self.store.execute("UPDATE execution_reconciliation SET status='confirmed_succeeded'")
        with patch.object(actual, "_send_card", return_value="terminal-message") as send:
            actual.send_bound_card({"phase": "terminal"}, targets=[("open_id", "recipient")],
                                   task_uid="auto-stop", delivery_stage="terminal")
            self.assertTrue(actual._deliver_outbox_once())
            actual.send_bound_card({"phase": "submitted"}, targets=[("open_id", "recipient")],
                                   task_uid="auto-stop", delivery_stage="submitted")
            send.assert_called_once()
            self.assertEqual({"phase": "terminal"}, send.call_args.args[2])
        self.assertEqual("superseded", self.store.select_one("feishu_outbox", where={"task_uid": "auto-stop"})["status"])

    def test_old_queue_cannot_overwrite_successful_direct_patch(self):
        self.task(status="executing")
        actual = bridge.LocalFeishuBridge(self.owner)
        with patch.object(actual, "_request", side_effect=RuntimeError("offline")):
            actual.update_task_cards("card")
        self.store.execute("UPDATE local_retarget_task SET status='succeeded' WHERE task_uid='card'")
        sent = []
        with patch.object(bridge, "build_local_task_card", side_effect=lambda task, **kw: {"status": task["status"]}), \
             patch.object(actual, "_request", side_effect=lambda *args, **kw: sent.append(json.loads(kw["payload"]["content"]))):
            actual.update_task_cards("card")
            self.assertFalse(actual._deliver_outbox_once())
        self.assertEqual([{"status": "succeeded"}], sent)
        self.assertEqual("superseded", self.store.select_one("feishu_outbox", where={"task_uid": "card"})["status"])

    def test_queued_retry_rebuilds_latest_state_without_a_direct_patch(self):
        self.task(status="executing")
        actual = bridge.LocalFeishuBridge(self.owner)
        actual._queue_outbox(operation="update_card", message_id="message-card",
                             payload={"task_uid": "card", "content": '{"status":"executing"}'})
        self.store.execute("UPDATE local_retarget_task SET status='succeeded' WHERE task_uid='card'")
        with patch.object(bridge, "build_local_task_card", side_effect=lambda task, **kw: {"status": task["status"]}), \
             patch.object(actual, "_request", return_value={}) as request:
            self.assertTrue(actual._deliver_outbox_once())
        self.assertEqual({"status": "succeeded"}, json.loads(request.call_args.kwargs["payload"]["content"]))

    def test_direct_and_retry_patch_share_a_serialized_message_lane(self):
        self.task(status="executing")
        actual = bridge.LocalFeishuBridge(self.owner)
        actual._queue_outbox(operation="update_card", message_id="message-card",
                             payload={"task_uid": "card", "content": "old"})
        entered, release = threading.Event(), threading.Event()
        sent = []
        def request(*args, **kwargs):
            state = json.loads(kwargs["payload"]["content"])["status"]
            if state == "executing":
                entered.set()
                self.assertTrue(release.wait(5))
            sent.append(state)
        with patch.object(bridge, "build_local_task_card", side_effect=lambda task, **kw: {"status": task["status"]}), \
             patch.object(actual, "_request", side_effect=request):
            retry = threading.Thread(target=actual._deliver_outbox_once)
            retry.start()
            self.assertTrue(entered.wait(5))
            self.store.execute("UPDATE local_retarget_task SET status='succeeded' WHERE task_uid='card'")
            direct = threading.Thread(target=actual.update_task_cards, args=("card",))
            direct.start()
            release.set()
            retry.join(5)
            direct.join(5)
            self.assertFalse(retry.is_alive() or direct.is_alive())
        self.assertEqual(["executing", "succeeded"], sent)

    def test_obsolete_stop_cards_invalidate_only_unclaimed_owner_scope(self):
        from services.regulation_rule_runner import _stop_strategy_snapshot
        strategy = {"id": "s", "title": "stop", "action_mode": "card_confirm"}
        frozen = hashlib.sha256(bridge._json(_stop_strategy_snapshot(strategy)).encode()).hexdigest()
        payload = {"strategy_id": "s", "strategy_hash": frozen}
        for state in ("pending", "approved_queued", "claimed", "executing", "verifying"):
            self.task(state, action="stop", status=state, payload=payload)
        self.task("other", action="stop", status="pending", payload=payload)
        self.store.execute("UPDATE local_retarget_task SET account_username='other' WHERE task_uid='other'")
        intent = self.intent("pending", action="stop", status="submitting")
        unchanged = bridge.invalidate_obsolete_local_stop_tasks(self.owner, {"enabled": True, "strategies": [strategy]})
        self.assertEqual(0, unchanged["count"])
        changed = bridge.invalidate_obsolete_local_stop_tasks(self.owner, {"enabled": True, "strategies": [{**strategy, "title": "changed"}]})
        self.assertEqual({"pending", "approved_queued"}, set(changed["task_uids"]))
        for state in ("claimed", "executing", "verifying"):
            self.assertEqual(state, bridge._task_row(state)["status"])
        self.assertEqual("pending", bridge._task_row("other")["status"])
        self.assertEqual("submitting", self.store.select_one("execution_reconciliation", where={"idempotency_key": intent["idempotency_key"]})["status"])
