"""r19 delivery/claim protocol: no live Feishu, no production data."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services import local_feishu_bridge as bridge
from utils.sqlite_store import SQLiteStore, init_sqlite_schema
from tests import test_local_feishu_bridge as fixtures


class FrozenDeliveryTests(unittest.TestCase):
    _stop_payload = fixtures.LocalFeishuTaskTests._stop_payload

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = os.path.join(self.temp.name, "tasks.db")
        self.owner = "tool-user-a"
        self.actual = bridge.LocalFeishuBridge(self.owner)
        self.manager = SimpleNamespace(account=self.owner, bridge=lambda: self.actual)
        for item in (patch.object(bridge, "DB_FILE", self.db_path),
                     patch.object(bridge, "PROFILE_FILE", os.path.join(self.temp.name, "profiles.json")),
                     patch.object(bridge, "_MANAGER", self.manager),
                     patch.object(self.actual, "status", return_value={"connected": True}),
                     patch.object(bridge, "_EVENT_EXECUTOR", Mock()),
                     patch("services.qianchuan_session.automation_session_ready",
                           return_value={"ready": True, "session_epoch": 1})):
            item.start()
            self.addCleanup(item.stop)
        init_sqlite_schema(database=self.db_path)
        self.store = SQLiteStore(database=self.db_path)
        bridge._update_profile(self.owner, {"authorized_open_id": "personal", "groups": []})

    def create(self):
        result = bridge.create_local_stop_task(self._stop_payload())
        self.assertTrue(result["success"])
        return result["data"]["task_uid"]

    def outbox(self, uid):
        return self.store.select_one("feishu_outbox", where={"task_uid": uid, "operation": "send_card"})

    def due(self, uid):
        self.store.execute("UPDATE feishu_outbox SET next_attempt_at='2000-01-01 00:00:00' WHERE task_uid=?", (uid,))

    def test_not_ready_creation_atomically_queues_without_sending(self):
        with patch.object(self.actual, "status", return_value={"connected": False}), patch.object(self.actual, "_request", side_effect=AssertionError("must not send")):
            uid = self.create()
            before = bridge._task_row(uid)["expires_at"]
            self.actual._deliver_outbox_once()
            self.assertEqual("waiting_for_callback", json.loads(bridge._task_row(uid)["payload_json"])["delivery_state"])
            self.assertEqual(before, bridge._task_row(uid)["expires_at"])
        row = bridge._task_row(uid)
        self.assertEqual("pending", row["status"])
        self.assertEqual([], json.loads(row["card_messages_json"]))
        delivery = self.outbox(uid)
        payload = json.loads(delivery["payload_json"])
        self.assertEqual("queued", delivery["status"])
        self.assertEqual(0, delivery["attempt_count"])
        self.assertEqual("", payload["first_attempt_at"])
        self.assertEqual(32, len(payload["delivery_uuid"]))

    def test_callback_ready_wakes_initial_queue_but_terminal_is_independent(self):
        self.actual._outbox_wake.clear()
        self.actual._set_connection_state("connected")
        self.assertTrue(self.actual._outbox_wake.is_set())
        with patch.object(self.actual, "status", return_value={"connected": False}), patch.object(self.actual, "_request", return_value={"data": {"message_id": "terminal"}}):
            delivered = self.actual.send_bound_card({"terminal": True}, delivery_stage="terminal")
        self.assertEqual("terminal", delivered[0]["message_id"])

    def test_outbox_insert_failure_rolls_back_task_too(self):
        payload = self._stop_payload()
        with patch.object(bridge, "_insert_initial_deliveries", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                bridge.create_local_stop_task(payload)
        self.assertEqual([], self.store.execute("SELECT task_uid FROM local_retarget_task", fetch=True))
        self.assertEqual([], self.store.execute("SELECT outbox_uid FROM feishu_outbox", fetch=True))

    def test_unknown_send_reuses_frozen_uuid_body_and_recipient(self):
        uid = self.create()
        first = json.loads(self.outbox(uid)["payload_json"])
        posts = []
        def request(method, path, **kwargs):
            if method == "POST":
                posts.append(kwargs["payload"])
                if len(posts) == 1:
                    raise TimeoutError("accepted but response lost")
                return {"data": {"message_id": "same-message"}}
            return {}
        with patch.object(self.actual, "_request", side_effect=request):
            self.actual._deliver_outbox_once()
            saved_clock = json.loads(self.outbox(uid)["payload_json"])["first_attempt_at"]
            self.assertTrue(saved_clock)
            self.assertEqual("pending", bridge._task_row(uid)["status"])
            bridge._update_profile(self.owner, {"authorized_open_id": "different-person"})
            self.due(uid)
            self.actual._deliver_outbox_once()
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(first["delivery_uuid"], posts[0]["uuid"])
        self.assertEqual("personal", posts[0]["receive_id"])
        self.assertEqual("sent", self.outbox(uid)["status"])
        self.assertEqual("same-message", self.outbox(uid)["message_id"])
        self.assertEqual(saved_clock, json.loads(self.outbox(uid)["payload_json"])["first_attempt_at"])
        self.assertEqual(["same-message"], [x["message_id"] for x in json.loads(bridge._task_row(uid)["card_messages_json"])])

    def test_missing_message_id_remains_unknown_after_eight_attempts(self):
        uid = self.create()
        with patch.object(self.actual, "_request", return_value={}) as request:
            for _ in range(8):
                self.due(uid)
                self.assertTrue(self.actual._deliver_outbox_once())
            self.assertFalse(self.actual._deliver_outbox_once())
        self.assertEqual(8, request.call_count)
        self.assertEqual("unknown", self.outbox(uid)["status"])
        self.assertEqual("pending", bridge._task_row(uid)["status"])
        self.assertIsNotNone(bridge._task_row(uid)["active_dedupe_key"])

    def test_ttl_and_uuid_window_never_rotate_uuid(self):
        for kind in ("ttl", "uuid_window"):
            with self.subTest(kind=kind):
                self.store.execute("DELETE FROM local_retarget_task")
                self.store.execute("DELETE FROM feishu_outbox")
                uid = self.create()
                row = self.outbox(uid)
                data = json.loads(row["payload_json"])
                original_uuid = data["delivery_uuid"]
                data["first_attempt_at"] = bridge._dt(bridge._now() - timedelta(minutes=51 if kind == "uuid_window" else 1))
                data["expires_at"] = bridge._dt(bridge._now() + timedelta(hours=1) if kind == "uuid_window" else bridge._now() - timedelta(seconds=1))
                self.store.execute("UPDATE feishu_outbox SET payload_json=?,attempt_count=1 WHERE task_uid=?", (json.dumps(data), uid))
                with patch.object(self.actual, "_request", side_effect=AssertionError("no new POST")):
                    self.assertTrue(self.actual._deliver_outbox_once())
                saved = self.outbox(uid)
                self.assertEqual("unknown", saved["status"])
                self.assertEqual(original_uuid, json.loads(saved["payload_json"])["delivery_uuid"])

    def test_legacy_orphan_is_marked_unknown_without_resending_or_extending_ttl(self):
        uid = self.create()
        old = bridge._task_row(uid)
        self.store.execute("DELETE FROM feishu_outbox WHERE task_uid=?", (uid,))
        data = json.loads(old["payload_json"])
        data.pop("delivery_protocol", None)
        self.store.execute("UPDATE local_retarget_task SET payload_json=? WHERE task_uid=?", (json.dumps(data), uid))
        with patch.object(self.actual, "_request", side_effect=AssertionError("must not guess unsent")):
            result = bridge.create_local_stop_task(self._stop_payload())
        self.assertTrue(result["duplicate"])
        self.assertEqual("unknown", result["data"]["delivery_state"])
        self.assertIsNone(self.outbox(uid))
        self.assertEqual(old["expires_at"], bridge._task_row(uid)["expires_at"])

    def test_receipt_and_message_list_are_one_transaction(self):
        uid = self.create()
        with patch.object(self.actual, "_request", return_value={"data": {"message_id": "sent-once"}}):
            with patch.object(bridge, "_merge_task_message", side_effect=RuntimeError("receipt commit failure")):
                self.actual._deliver_outbox_once()
            self.assertNotEqual("sent", self.outbox(uid)["status"])
            self.assertEqual([], json.loads(bridge._task_row(uid)["card_messages_json"]))
            self.due(uid)
            self.actual._deliver_outbox_once()
        self.assertEqual("sent", self.outbox(uid)["status"])
        self.assertEqual(1, len(json.loads(bridge._task_row(uid)["card_messages_json"])))

    def test_multiple_recipients_merge_receipts_without_overwrite(self):
        bridge._update_profile(self.owner, {"groups": [{"chat_id": "group"}]})
        with patch("services.qianchuan_accounts.resolve_account_feishu_targets", return_value=[("open_id", "personal"), ("chat_id", "group")]):
            uid = self.create()
        def request(method, path, **kwargs):
            return {"data": {"message_id": "msg-" + kwargs["payload"]["receive_id"]}} if method == "POST" else {}
        with patch.object(self.actual, "_request", side_effect=request):
            workers = [threading.Thread(target=self.actual._deliver_outbox_once) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(5)
                self.assertFalse(worker.is_alive())
        self.assertEqual({"msg-personal", "msg-group"}, {x["message_id"] for x in json.loads(bridge._task_row(uid)["card_messages_json"])})

    def test_generic_send_contract_returns_only_real_receipts(self):
        with patch.object(self.actual, "_request", return_value={"data": {"message_id": "actual"}}):
            result = self.actual.send_bound_card({"text": "notification"})
        self.assertEqual([{"receive_type": "open_id", "receive_id": "personal", "message_id": "actual"}], result)

    def test_same_logical_notification_never_changes_frozen_body_or_uuid(self):
        with patch.object(self.actual, "_request", return_value={"data": {"message_id": "actual"}}) as request:
            self.actual.send_bound_card({"text": "original"}, task_uid="notice", delivery_stage="terminal")
            self.actual.send_bound_card({"text": "changed later"}, task_uid="notice", delivery_stage="terminal")
        self.assertEqual(1, request.call_count)
        row = self.outbox("notice")
        self.assertEqual({"text": "original"}, json.loads(json.loads(row["payload_json"])["frozen_content"]))

    def claimed(self):
        uid = self.create()
        self.store.execute("UPDATE local_retarget_task SET status='approved_queued' WHERE task_uid=?", (uid,))
        task = bridge.pull_local_stop_task()["data"]
        self.assertEqual(1, task["fencing_token"])
        self.assertTrue(bridge.report_local_stop_task(uid, task["claim_token"], "executing", fencing_token=task["fencing_token"])["success"])
        return task

    def test_claim_fence_and_expiry_are_checked_at_submission_and_report(self):
        task = self.claimed()
        claim = {key: task[key] for key in ("task_uid", "account_username", "claim_token", "fencing_token")}
        conn = bridge._db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.assertEqual(task["task_uid"], bridge.assert_valid_local_claim(conn, claim)["task_uid"])
            with self.assertRaises(bridge.LocalClaimInvalidError):
                bridge.assert_valid_local_claim(conn, {**claim, "fencing_token": 0})
            conn.commit()
        finally:
            conn.close()
        self.assertFalse(bridge.report_local_stop_task(task["task_uid"], task["claim_token"], "executing")["success"])
        self.store.execute("UPDATE local_retarget_task SET claim_expires_at='2000-01-01 00:00:00' WHERE task_uid=?", (task["task_uid"],))
        self.assertFalse(bridge.report_local_stop_task(task["task_uid"], task["claim_token"], "executing", fencing_token=1)["success"])
        renewed = bridge.pull_local_stop_task()["data"]
        self.assertEqual(2, renewed["fencing_token"])
        self.assertFalse(bridge.report_local_stop_task(task["task_uid"], task["claim_token"], "failed", fencing_token=1)["success"])

    def test_reserved_intent_prevents_expired_claim_from_being_reclaimed(self):
        task = self.claimed()
        self.store.insert("execution_reconciliation", {"reconciliation_uid": "reserved", "account_username": self.owner,
                          "task_uid": task["task_uid"], "idempotency_key": "reserved", "status": "submitting"})
        self.store.execute("UPDATE local_retarget_task SET claim_expires_at='2000-01-01 00:00:00' WHERE task_uid=?", (task["task_uid"],))
        self.assertIsNone(bridge.pull_local_stop_task()["data"])
        self.assertEqual("verifying", bridge._task_row(task["task_uid"])["status"])

    def test_metrics_display_does_not_fall_back_to_commit_time(self):
        text = bridge._stop_metrics_snapshot_detail({"metrics_snapshot": {"updated_at": "2099-01-01 00:00:00"}})
        self.assertIn("指标采集时间：--", text)
        self.assertNotIn("2099", text)

    def test_terminal_intent_can_finish_original_expired_claim_but_not_new_owner(self):
        task = self.claimed()
        claim = {key: task[key] for key in ("task_uid", "account_username", "claim_token", "fencing_token")}
        intent = {"reconciliation_uid": "accepted", "account_username": self.owner, "task_uid": task["task_uid"],
                  "idempotency_key": "accepted", "status": "confirmed_succeeded", "payload_json": json.dumps({"submission_claim": claim})}
        self.store.insert("execution_reconciliation", intent)
        with self.store.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.assertFalse(bridge.promote_reconciled_local_claim(conn, intent))
        self.assertEqual("executing", bridge._task_row(task["task_uid"])["status"])
        self.store.execute("UPDATE local_retarget_task SET expires_at='2000-01-01 00:00:00',claim_expires_at='2000-01-01 00:00:00' WHERE task_uid=?", (task["task_uid"],))
        self.assertFalse(bridge.report_local_stop_task(task["task_uid"], task["claim_token"], "verifying", fencing_token=task["fencing_token"])["success"])
        with self.store.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.assertTrue(bridge.promote_reconciled_local_claim(conn, intent))
        self.assertEqual("verifying", bridge._task_row(task["task_uid"])["status"])
        self.store.execute("UPDATE local_retarget_task SET status='executing',fencing_token=fencing_token+1 WHERE task_uid=?", (task["task_uid"],))
        with self.store.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.assertFalse(bridge.promote_reconciled_local_claim(conn, intent))
