# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import local_feishu_bridge as bridge
from utils.sqlite_store import init_sqlite_schema


def task_payload(material_count: int = 2) -> dict:
    return {
        "aavid": "10001",
        "ad_id": "20002",
        "target_uid": "target-product-1",
        "account_name": "测试千川账户",
        "plan_name": "商品全域测试计划",
        "promotion_scene": "product",
        "plan_system": "global",
        "trigger_level": "product",
        "strategy_id": "strategy-1",
        "strategy_name": "商品ROI追投",
        "strategy_hash": "a" * 64,
        "rule_snapshot": {"id": "strategy-1"},
        "trigger_snapshot": {"reason": "商品ROI达到阈值"},
        "retargeting": {
            "method": "volume",
            "volume": {"total_budget_yuan": 100, "duration_hours": 24},
        },
        "materials": [
            {
                "material_id": str(70000 + index),
                "material_name": f"测试视频{index + 1}",
                "product_id": "90001",
                "product_name": "测试商品",
            }
            for index in range(material_count)
        ],
    }


class FakeFeishuBridge:
    def __init__(self, account: str):
        self.account_username = account
        self.updated = []

    def status(self):
        return {"success": True, "connected": True, "status": "connected"}

    def profile(self, **_kwargs):
        return {
            "authorized_open_id": "ou_owner",
            "groups": [],
            "send_personal": True,
            "send_groups": False,
        }

    def send_task_cards(self, _task):
        return [
            {
                "receive_type": "open_id",
                "receive_id": "ou_owner",
                "message_id": "om_test",
            }
        ]

    def update_task_cards(self, task_uid: str, **_kwargs):
        self.updated.append(task_uid)


class FakeManager:
    def __init__(self, account: str):
        self.account = account
        self.instance = FakeFeishuBridge(account)

    def bridge(self):
        return self.instance


class LocalFeishuTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "tasks.db")
        self.profile_path = os.path.join(self.temp.name, "profiles.json")
        self.manager = FakeManager("tool-user-a")
        self.patches = [
            patch.object(bridge, "DB_FILE", self.db_path),
            patch.object(bridge, "PROFILE_FILE", self.profile_path),
            patch.object(bridge, "_MANAGER", self.manager),
        ]
        for item in self.patches:
            item.start()
        init_sqlite_schema(database=self.db_path)
        bridge._update_profile(
            "tool-user-a",
            {"authorized_open_id": "ou_owner", "groups": []},
        )

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_local_task_is_deduplicated_and_executes_only_once(self):
        first = bridge.create_local_retarget_task(task_payload())
        self.assertTrue(first["success"])
        task_uid = first["data"]["task_uid"]
        duplicate = bridge.create_local_retarget_task(task_payload())
        self.assertTrue(duplicate["success"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(task_uid, duplicate["data"]["task_uid"])

        row = bridge._task_row(task_uid, "tool-user-a")
        self.assertIsNotNone(row)
        nonce = row["action_nonce"]
        denied = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="approve",
            operator_open_id="ou_other",
        )
        self.assertFalse(denied["success"])
        forged = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce="wrong",
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertFalse(forged["success"])

        approved = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertTrue(approved["success"])
        repeated = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertTrue(repeated["success"])
        pulled = bridge.pull_local_retarget_task()
        self.assertEqual(task_uid, pulled["data"]["task_uid"])
        claim_token = pulled["data"]["claim_token"]
        self.assertTrue(
            bridge.report_local_retarget_task(
                task_uid, claim_token, "executing", message="执行中"
            )["success"]
        )
        self.assertTrue(
            bridge.report_local_retarget_task(
                task_uid,
                claim_token,
                "succeeded",
                message="追投成功",
                regulate_task_id="regulate-1",
                result={"success": True},
            )["success"]
        )
        self.assertIsNone(bridge.pull_local_retarget_task()["data"])
        final = bridge._task_row(task_uid, "tool-user-a")
        self.assertEqual("succeeded", final["status"])
        self.assertEqual("regulate-1", final["regulate_task_id"])

    def test_one_card_accepts_at_most_twenty_materials(self):
        accepted = bridge.create_local_retarget_task(task_payload(20))
        self.assertTrue(accepted["success"])
        rejected = bridge.create_local_retarget_task(
            {**task_payload(21), "strategy_id": "strategy-2"}
        )
        self.assertFalse(rejected["success"])

    def test_card_shows_plan_scene_account_and_materials(self):
        task = task_payload(3)
        task.update(
            {
                "task_uid": "task-1",
                "status": "pending",
                "action_nonce": "nonce-1",
                "expires_at": "2030-01-01 12:00:00",
            }
        )
        raw = json.dumps(bridge.build_task_card(task), ensure_ascii=False)
        self.assertIn("测试千川账户", raw)
        self.assertIn("商品全域测试计划", raw)
        self.assertIn("推商品", raw)
        self.assertIn("传统全域", raw)
        self.assertIn("测试视频1", raw)
        self.assertIn("测试视频3", raw)


class LocalFeishuBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.profile_path = os.path.join(self.temp.name, "profiles.json")
        self.profile_patch = patch.object(bridge, "PROFILE_FILE", self.profile_path)
        self.profile_patch.start()
        bridge._update_profile(
            "tool-user-a",
            {
                "enabled": True,
                "app_id": "cli_test",
                "send_personal": True,
                "send_groups": True,
            },
        )
        self.instance = bridge.LocalFeishuBridge("tool-user-a")
        self.sent = []
        self.instance.send_text = lambda chat_id, text: self.sent.append(("chat", chat_id, text))
        self.instance.send_private_text = lambda open_id, text: self.sent.append(("user", open_id, text))

    def tearDown(self):
        self.profile_patch.stop()
        self.temp.cleanup()

    def message(self, *, message_id, text, open_id, chat_id, chat_type):
        return SimpleNamespace(
            message_id=message_id,
            content_text=text,
            sender=SimpleNamespace(open_id=open_id),
            chat_id=chat_id,
            chat_type=chat_type,
        )

    def test_binding_code_is_one_time_and_only_owner_can_bind_group(self):
        personal = self.instance.issue_binding_code("personal")
        self.instance._on_message(
            self.message(
                message_id="m1",
                text=personal["command"],
                open_id="ou_owner",
                chat_id="oc_personal",
                chat_type="p2p",
            )
        )
        profile = bridge._profile_for("tool-user-a")
        self.assertEqual("ou_owner", profile["authorized_open_id"])
        self.instance._on_message(
            self.message(
                message_id="m2",
                text=personal["command"],
                open_id="ou_other",
                chat_id="oc_other",
                chat_type="p2p",
            )
        )
        self.assertIn("无效或已过期", self.sent[-1][2])

        group = self.instance.issue_binding_code("group")
        self.instance._on_message(
            self.message(
                message_id="m3",
                text=group["command"],
                open_id="ou_other",
                chat_id="oc_group",
                chat_type="group",
            )
        )
        self.assertFalse(bridge._profile_for("tool-user-a")["groups"])
        self.instance._on_message(
            self.message(
                message_id="m4",
                text=group["command"],
                open_id="ou_owner",
                chat_id="oc_group",
                chat_type="group",
            )
        )
        self.assertEqual("oc_group", bridge._profile_for("tool-user-a")["groups"][0]["chat_id"])

    def test_connection_errors_have_user_facing_states(self):
        self.assertEqual(
            "permission_missing",
            bridge._connection_error_status("Forbidden: permission denied 99991672"),
        )
        self.assertEqual(
            "app_unpublished",
            bridge._connection_error_status("application is not published"),
        )
        self.assertEqual("error", bridge._connection_error_status("network timeout"))

    def test_connection_test_button_is_one_time_and_owner_only(self):
        bridge._update_profile(
            "tool-user-a", {"authorized_open_id": "ou_owner"}
        )
        cards = []
        self.instance._send_card = (
            lambda receive_type, receive_id, card: cards.append(
                (receive_type, receive_id, card)
            )
            or "om_test"
        )
        sent = self.instance.send_test_card()
        self.assertTrue(sent["success"])
        value = cards[0][2]["elements"][1]["actions"][0]["value"]
        denied = self.instance._consume_connection_test(
            value["nonce"], "ou_other"
        )
        self.assertFalse(denied["success"])
        accepted = self.instance._consume_connection_test(
            value["nonce"], "ou_owner"
        )
        self.assertTrue(accepted["success"])
        repeated = self.instance._consume_connection_test(
            value["nonce"], "ou_owner"
        )
        self.assertFalse(repeated["success"])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_app_secret_uses_dpapi_and_is_not_written_as_plaintext(self):
        secret = "local-secret-value"
        protected = bridge._protect_secret(secret)
        self.assertTrue(protected.startswith("dpapi:"))
        self.assertNotIn(secret, protected)
        self.assertEqual(secret, bridge._unprotect_secret(protected))
        bridge._update_profile("tool-user-a", {"app_secret_protected": protected})
        with open(self.profile_path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn(secret, raw)
        self.assertEqual(secret, bridge._profile_for("tool-user-a", include_secret=True)["app_secret"])


if __name__ == "__main__":
    unittest.main()
