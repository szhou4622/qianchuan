# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import tempfile
import threading
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
        card = bridge.build_task_card(task)
        summary = card["elements"][0]["text"]["content"]
        self.assertIn("测试千川账户", raw)
        self.assertIn("商品全域测试计划", raw)
        self.assertIn("推商品", raw)
        self.assertIn("全域", raw)
        self.assertNotIn("传统全域", raw)
        self.assertIn("测试视频1", raw)
        self.assertIn("测试视频3", raw)
        self.assertIn("\n账户ID：10001\n计划名称：商品全域测试计划", summary)
        self.assertIn("\n计划ID：20002\n", summary)
        self.assertIn("\n素材ID：70000\n", summary)
        self.assertNotIn("\n   素材ID", summary)
        self.assertIn("当前已选3条", summary)
        self.assertIn("【已选】 1. 测试视频1", summary)
        action_values = [
            action.get("value") or {}
            for element in card["elements"]
            if element.get("tag") == "action"
            for action in element.get("actions") or []
        ]
        self.assertTrue(
            any(value.get("action") == "select_all" for value in action_values)
        )
        self.assertTrue(
            any(
                value.get("action") == "toggle_material"
                and value.get("material_id") == "70002"
                for value in action_values
            )
        )

    def test_owner_can_select_one_partial_or_all_before_approval(self):
        created = bridge.create_local_retarget_task(task_payload(4))
        self.assertTrue(created["success"])
        task_uid = created["data"]["task_uid"]
        nonce = bridge._task_row(task_uid, "tool-user-a")["action_nonce"]

        cleared = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="clear_selection",
            operator_open_id="ou_owner",
        )
        self.assertTrue(cleared["success"])
        empty_approval = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertFalse(empty_approval["success"])
        self.assertIn("至少选择1条", empty_approval["message"])

        selected_one = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="toggle_material",
            operator_open_id="ou_owner",
            material_id="70002",
        )
        self.assertTrue(selected_one["success"])
        approved = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertTrue(approved["success"])
        self.assertIn("1条素材", approved["message"])
        pulled = bridge.pull_local_retarget_task()["data"]
        self.assertEqual(["70002"], [item["material_id"] for item in pulled["materials"]])
        self.assertEqual(4, pulled["selection_snapshot"]["candidate_count"])
        self.assertEqual(1, pulled["selection_snapshot"]["selected_count"])

        partial_payload = {**task_payload(4), "strategy_id": "strategy-partial"}
        partial = bridge.create_local_retarget_task(partial_payload)
        partial_uid = partial["data"]["task_uid"]
        partial_nonce = bridge._task_row(partial_uid, "tool-user-a")["action_nonce"]
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=partial_uid,
            nonce=partial_nonce,
            action="clear_selection",
            operator_open_id="ou_owner",
        )
        for material_id in ("70000", "70003"):
            result = bridge.handle_local_card_action(
                "tool-user-a",
                task_uid=partial_uid,
                nonce=partial_nonce,
                action="toggle_material",
                operator_open_id="ou_owner",
                material_id=material_id,
            )
            self.assertTrue(result["success"])
        partial_task = bridge._task_payload(
            bridge._task_row(partial_uid, "tool-user-a")
        )
        self.assertEqual(["70000", "70003"], partial_task["selected_material_ids"])

        all_payload = {**task_payload(4), "strategy_id": "strategy-all"}
        all_task = bridge.create_local_retarget_task(all_payload)
        all_uid = all_task["data"]["task_uid"]
        all_nonce = bridge._task_row(all_uid, "tool-user-a")["action_nonce"]
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=all_uid,
            nonce=all_nonce,
            action="clear_selection",
            operator_open_id="ou_owner",
        )
        selected_all = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=all_uid,
            nonce=all_nonce,
            action="select_all",
            operator_open_id="ou_owner",
        )
        self.assertTrue(selected_all["success"])
        all_task_payload = bridge._task_payload(
            bridge._task_row(all_uid, "tool-user-a")
        )
        self.assertEqual(4, len(all_task_payload["selected_material_ids"]))

    def test_material_selection_rejects_forged_or_late_changes(self):
        created = bridge.create_local_retarget_task(task_payload(3))
        task_uid = created["data"]["task_uid"]
        nonce = bridge._task_row(task_uid, "tool-user-a")["action_nonce"]
        forged = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="toggle_material",
            operator_open_id="ou_owner",
            material_id="forged-material",
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
        late = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="clear_selection",
            operator_open_id="ou_owner",
        )
        self.assertFalse(late["success"])


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

    def test_card_action_returns_valid_immediate_ack(self):
        from lark_oapi import LogLevel

        received = []
        ready = threading.Event()
        channel = bridge._build_feishu_long_connection_channel(
            app_id="cli_test",
            app_secret="secret",
            log_level=LogLevel.CRITICAL,
            on_card_action=lambda event: (
                received.append(event),
                ready.set(),
            ),
        )
        try:
            response = channel._on_p2_card_action_trigger(
                SimpleNamespace(
                    event=SimpleNamespace(
                        operator=SimpleNamespace(open_id="ou_owner"),
                        action=SimpleNamespace(
                            value={"action": "connection_test", "nonce": "n1"}
                        ),
                        context=SimpleNamespace(
                            open_message_id="om_test",
                            open_chat_id="oc_test",
                        ),
                    )
                )
            )
            self.assertIsNotNone(response.toast)
            self.assertEqual("info", response.toast.type)
            self.assertIn("请求已收到", response.toast.content)
            self.assertTrue(ready.wait(1.0))
            self.assertEqual("om_test", received[0].message_id)
            self.assertEqual(
                "connection_test", received[0].action.value["action"]
            )
            dispatched = channel._dispatcher._do_without_validation(
                json.dumps(
                    {
                        "schema": "2.0",
                        "header": {
                            "event_id": "evt_test",
                            "event_type": "card.action.trigger",
                            "create_time": "0",
                            "token": "",
                            "app_id": "cli_test",
                            "tenant_key": "tenant_test",
                        },
                        "event": {
                            "operator": {"open_id": "ou_owner"},
                            "action": {
                                "value": {
                                    "action": "connection_test",
                                    "nonce": "n2",
                                }
                            },
                            "context": {
                                "open_message_id": "om_dispatch",
                                "open_chat_id": "oc_dispatch",
                            },
                        },
                    }
                ).encode("utf-8")
            )
            self.assertEqual("info", dispatched.toast.type)
            self.assertIn("请求已收到", dispatched.toast.content)
        finally:
            channel.stop()

    def test_raw_message_event_is_normalized_for_binding_handler(self):
        from lark_oapi import LogLevel

        received = []
        ready = threading.Event()
        channel = bridge._build_feishu_long_connection_channel(
            app_id="cli_test",
            app_secret="secret",
            log_level=LogLevel.CRITICAL,
            on_message=lambda message: (
                received.append(message),
                ready.set(),
            ),
        )
        try:
            channel._on_p2_im_message_receive_v1(
                SimpleNamespace(
                    event=SimpleNamespace(
                        sender=SimpleNamespace(
                            sender_id=SimpleNamespace(open_id="ou_owner")
                        ),
                        message=SimpleNamespace(
                            message_id="om_bind",
                            content=json.dumps(
                                {"text": "@_user_1 绑定群 123456"},
                                ensure_ascii=False,
                            ),
                            chat_id="oc_group",
                            chat_type="group",
                        ),
                    )
                )
            )
            self.assertTrue(ready.wait(1.0))
            self.assertEqual("绑定群 123456", received[0].content_text)
            self.assertEqual("ou_owner", received[0].sender.open_id)
            self.assertEqual("oc_group", received[0].chat_id)
            self.assertEqual("group", received[0].chat_type)
        finally:
            channel.stop()

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


class LocalFeishuSessionRestoreTests(unittest.TestCase):
    class Manager:
        def __init__(self):
            self.account = ""
            self.activated = []

        def activate(self, username):
            self.account = str(username)
            self.activated.append(str(username))

    def test_device_session_restores_account_without_password(self):
        manager = self.Manager()
        with (
            patch.object(bridge, "_MANAGER", manager),
            patch(
                "services.cloud_retarget_client.load_device_session",
                return_value={"username": "local_test", "token": "device-token"},
            ),
        ):
            self.assertTrue(
                bridge.restore_local_feishu_account_from_device_session()
            )
            self.assertEqual("local_test", bridge.current_local_feishu_account())
            self.assertEqual(["local_test"], manager.activated)

    def test_incomplete_device_session_does_not_restore_account(self):
        manager = self.Manager()
        with (
            patch.object(bridge, "_MANAGER", manager),
            patch(
                "services.cloud_retarget_client.load_device_session",
                return_value={"username": "local_test", "token": ""},
            ),
        ):
            self.assertFalse(
                bridge.restore_local_feishu_account_from_device_session()
            )
            self.assertEqual("", manager.account)
            self.assertEqual([], manager.activated)


if __name__ == "__main__":
    unittest.main()
