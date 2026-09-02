# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from api.promotion_targets import upsert_promotion_target
from services.qianchuan_accounts import ensure_qianchuan_account
from services import local_feishu_bridge as bridge
from services.retarget_budget_increase import (
    budget_increase_fingerprint,
    calculate_budget_increase,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


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

    def bound_targets(self):
        return [("open_id", "ou_owner")]

    def send_task_cards(self, _task, **_kwargs):
        return [
            {
                "receive_type": "open_id",
                "receive_id": "ou_owner",
                "message_id": "om_test",
            }
        ]

    def send_bound_card(self, _card, **_kwargs):
        return [
            {
                "receive_type": "open_id",
                "receive_id": "ou_owner",
                "message_id": "om_notice",
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
            patch(
                "services.qianchuan_session.automation_session_ready",
                return_value={
                    "ready": True,
                    "available": True,
                    "status": "available",
                    "session_epoch": 1,
                },
            ),
        ]
        for item in self.patches:
            item.start()
        init_sqlite_schema(database=self.db_path)
        bridge._update_profile(
            "tool-user-a",
            {"authorized_open_id": "ou_owner", "groups": []},
        )
        ensure_qianchuan_account(
            "10001",
            account_name="测试千川账户",
            owner_username="tool-user-a",
            enabled=True,
            seen=True,
            db=SQLiteStore(database=self.db_path),
        )

    def _stop_payload(self) -> dict:
        db = SQLiteStore(database=self.db_path)
        ensure_qianchuan_account(
            "10001",
            account_name="测试千川账户",
            owner_username="tool-user-a",
            enabled=True,
            seen=True,
            db=db,
        )
        target = upsert_promotion_target(
            {
                "aavid": "10001",
                "ad_id": "20002",
                "account_name": "测试千川账户",
                "plan_name": "全域推直播测试计划",
                "promotion_scene": "live",
                "plan_system": "global",
                "platform_status": "active",
                "verification_state": "verified",
                "enabled": True,
            },
            owner_username="tool-user-a",
            trusted_catalog=True,
            db=db,
        )
        return {
            "aavid": "10001",
            "ad_id": "20002",
            "target_uid": target["target_uid"],
            "account_name": "测试千川账户",
            "plan_name": "全域推直播测试计划",
            "assist_task_id": "assist-30003",
            "assist_task_name": "调控任务A",
            "strategy_id": "stop-strategy-1",
            "strategy_name": "低ROI停投",
            "strategy_hash": "b" * 64,
            "rule_snapshot": {
                "id": "stop-strategy-1",
                "title": "低ROI停投",
                "action_mode": "card_confirm",
            },
            "trigger": {
                "group_combine": "or",
                "groups": [
                    {
                        "join": "and",
                        "conditions": [
                            {
                                "metric": "total_prepay_and_pay_order_roi2_assist",
                                "op": "lt",
                                "value": 1.5,
                            }
                        ],
                    }
                ],
            },
            "trigger_snapshot": {"reason": "ROI低于1.5"},
            "metrics_snapshot": {
                "total_prepay_and_pay_order_roi2_assist": 1.2
            },
            "regulation_stop_action": "pause",
        }

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_result_notification_can_send_when_long_connection_is_offline(self):
        self.manager.instance.status = lambda: {
            "success": True,
            "connected": False,
            "status": "reconnecting",
        }
        with self.assertRaises(bridge.FeishuApiError):
            bridge.send_local_feishu_bound_card({"elements": []})
        sent = bridge.send_local_feishu_bound_card(
            {"elements": []},
            require_connected=False,
        )
        self.assertEqual("om_notice", sent[0]["message_id"])

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

    def test_budget_increase_card_needs_no_materials_and_queues_once(self):
        calculation = calculate_budget_increase(
            {
                "assist_task_id": "assist-budget-1",
                "budget": 400,
                "ecp_roi2_goal": 2.8,
                "stat_cost_for_roi2_assist": 300,
            },
            {
                "mode": "spend_percentage",
                "spend_percentage": 20,
                "volume_extend_hours": 1,
            },
        )
        payload = {
            "task_operation": "increase_budget",
            "aavid": "10001",
            "ad_id": "20002",
            "target_uid": "target-product-1",
            "account_name": "测试千川账户",
            "plan_name": "商品全域测试计划",
            "promotion_scene": "product",
            "plan_system": "global",
            "strategy_id": "strategy-budget-1",
            "strategy_name": "调控任务追加预算",
            "strategy_hash": "c" * 64,
            "rule_snapshot": {
                "id": "strategy-budget-1",
                "task_action": "increase_budget",
            },
            "trigger_snapshot": {"reason": "调控消耗大于100元"},
            "assist_task_id": "assist-budget-1",
            "assist_task_name": "控成本ROI任务",
            "budget_increase": {
                "mode": "spend_percentage",
                "spend_percentage": 20,
                "volume_extend_hours": 1,
            },
            "calculation_snapshot": calculation,
            "calculation_fingerprint": budget_increase_fingerprint(
                target_uid="target-product-1",
                strategy_id="strategy-budget-1",
                calculation=calculation,
            ),
        }

        created = bridge.create_local_retarget_task(payload)
        self.assertTrue(created["success"])
        duplicate = bridge.create_local_retarget_task(payload)
        self.assertTrue(duplicate["success"])
        self.assertTrue(duplicate["duplicate"])
        task_uid = created["data"]["task_uid"]
        self.assertEqual(task_uid, duplicate["data"]["task_uid"])

        row = bridge._task_row(task_uid, "tool-user-a")
        task = bridge._task_payload(row)
        self.assertEqual("increase_budget", task["task_operation"])
        self.assertEqual([], task["materials"])
        card = bridge.build_local_task_card(task)
        actions = [
            action
            for element in card["elements"]
            if element.get("tag") == "action"
            for action in element.get("actions", [])
        ]
        self.assertIn("approve", [action["value"]["action"] for action in actions])
        self.assertIn("460", json.dumps(card, ensure_ascii=False))

        approved = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=row["action_nonce"],
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertTrue(approved["success"])
        pulled = bridge.pull_local_retarget_task()["data"]
        self.assertEqual(task_uid, pulled["task_uid"])
        self.assertEqual("increase_budget", pulled["task_operation"])

    def test_one_card_accepts_at_most_twenty_materials(self):
        accepted = bridge.create_local_retarget_task(task_payload(20))
        self.assertTrue(accepted["success"])
        rejected = bridge.create_local_retarget_task(
            {**task_payload(21), "strategy_id": "strategy-2"}
        )
        self.assertFalse(rejected["success"])

    def test_stop_card_is_authorized_deduplicated_and_pulled_separately(self):
        created = bridge.create_local_stop_task(self._stop_payload())
        self.assertTrue(created["success"])
        duplicate = bridge.create_local_stop_task(self._stop_payload())
        self.assertTrue(duplicate["success"])
        self.assertTrue(duplicate["duplicate"])
        task_uid = created["data"]["task_uid"]
        row = bridge._task_row(task_uid, "tool-user-a")
        self.assertEqual("stop", row["action_type"])

        card = bridge.build_local_task_card(bridge._task_payload(row))
        raw = json.dumps(card, ensure_ascii=False)
        self.assertIn("全域", raw)
        self.assertIn("推直播", raw)
        self.assertIn("调控任务A", raw)
        self.assertIn("确认停投", raw)

        denied = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=row["action_nonce"],
            action="approve",
            operator_open_id="ou_other",
        )
        self.assertFalse(denied["success"])
        approved = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=row["action_nonce"],
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertTrue(approved["success"])
        repeated = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=row["action_nonce"],
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertTrue(repeated["success"])
        self.assertIsNone(bridge.pull_local_retarget_task()["data"])
        pulled = bridge.pull_local_stop_task()["data"]
        self.assertEqual(task_uid, pulled["task_uid"])
        self.assertEqual("tool-user-a", pulled["account_username"])

    def test_strategy_save_invalidates_old_card_and_releases_dedupe(self):
        payload = task_payload(2)
        created = bridge.create_local_retarget_task(payload)
        self.assertTrue(created["success"])
        old_uid = created["data"]["task_uid"]
        old_row = bridge._task_row(old_uid, "tool-user-a")

        invalidated = bridge.invalidate_stale_local_retarget_tasks(
            {"strategy-1": "b" * 64},
            config_enabled=True,
            account_username="tool-user-a",
        )

        self.assertTrue(invalidated["success"])
        self.assertEqual(1, invalidated["count"])
        saved = bridge._task_row(old_uid, "tool-user-a")
        self.assertEqual("invalidated", saved["status"])
        self.assertIsNone(saved["active_dedupe_key"])
        self.assertIn("未向千川提交", saved["result_message"])
        raw = json.dumps(
            bridge.build_local_task_card(bridge._task_payload(saved)),
            ensure_ascii=False,
        )
        self.assertIn("策略已更新，本卡失效", raw)
        self.assertIn("未向千川提交", raw)
        self.assertNotIn("确认追投", raw)

        click_old = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=old_uid,
            nonce=old_row["action_nonce"],
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertTrue(click_old["success"])
        self.assertIn("未向千川提交", click_old["message"])
        self.assertIsNone(bridge.pull_local_retarget_task()["data"])

        fresh_payload = {**payload, "strategy_hash": "b" * 64}
        fresh = bridge.create_local_retarget_task(fresh_payload)
        self.assertTrue(fresh["success"])
        self.assertFalse(fresh.get("duplicate", False))
        unchanged = bridge.invalidate_stale_local_retarget_tasks(
            {"strategy-1": "b" * 64},
            config_enabled=True,
            account_username="tool-user-a",
        )
        self.assertEqual(0, unchanged["count"])
        fresh_row = bridge._task_row(fresh["data"]["task_uid"], "tool-user-a")
        self.assertEqual("pending", fresh_row["status"])

    def test_strategy_invalidation_only_changes_safe_unclaimed_states(self):
        def create(strategy_id: str, strategy_hash: str) -> tuple[str, dict]:
            payload = {
                **task_payload(1),
                "strategy_id": strategy_id,
                "strategy_name": strategy_id,
                "strategy_hash": strategy_hash,
            }
            result = bridge.create_local_retarget_task(payload)
            self.assertTrue(result["success"])
            task_uid = result["data"]["task_uid"]
            return task_uid, bridge._task_row(task_uid, "tool-user-a")

        approved_uid, approved_row = create("approved", "a" * 64)
        approved = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=approved_uid,
            nonce=approved_row["action_nonce"],
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertTrue(approved["success"])
        claimed_uid, _ = create("claimed", "b" * 64)
        verifying_uid, _ = create("verifying", "c" * 64)
        keep_uid, _ = create("keep", "d" * 64)
        conn = bridge._db()
        try:
            conn.execute(
                "UPDATE local_retarget_task SET status='claimed' WHERE task_uid=?",
                (claimed_uid,),
            )
            conn.execute(
                "UPDATE local_retarget_task SET status='verifying' WHERE task_uid=?",
                (verifying_uid,),
            )
            conn.commit()
        finally:
            conn.close()

        result = bridge.invalidate_stale_local_retarget_tasks(
            {"keep": "d" * 64},
            config_enabled=True,
            account_username="tool-user-a",
        )

        self.assertEqual(1, result["count"])
        self.assertEqual(
            "invalidated",
            bridge._task_row(approved_uid, "tool-user-a")["status"],
        )
        self.assertEqual(
            "claimed",
            bridge._task_row(claimed_uid, "tool-user-a")["status"],
        )
        self.assertEqual(
            "verifying",
            bridge._task_row(verifying_uid, "tool-user-a")["status"],
        )
        self.assertEqual(
            "pending",
            bridge._task_row(keep_uid, "tool-user-a")["status"],
        )

        disabled = bridge.invalidate_stale_local_retarget_tasks(
            {"keep": "d" * 64},
            config_enabled=False,
            account_username="tool-user-a",
        )
        self.assertEqual(1, disabled["count"])
        self.assertEqual(
            "invalidated",
            bridge._task_row(keep_uid, "tool-user-a")["status"],
        )

    def test_stop_card_shows_complete_strategy_and_task_metric_snapshot(self):
        payload = self._stop_payload()
        trigger = payload["trigger"]
        payload["rule_snapshot"]["trigger"] = trigger
        payload["trigger_snapshot"] = {
            "strategy_title": "低ROI停投",
            "trigger_config": trigger,
            "evaluation": {
                "group_combine": "or",
                "passed": True,
                "groups": [
                    {
                        "join": "and",
                        "passed": True,
                        "conditions": [
                            {
                                "metric": "total_prepay_and_pay_order_roi2_assist",
                                "op": "lt",
                                "threshold": 1.5,
                                "actual": 1.2,
                                "passed": True,
                            }
                        ],
                    }
                ],
            },
        }
        payload["metrics_snapshot"] = {
            "ad_delivery_name": "PROCESSING",
            "stat_cost_for_roi2_assist": 88.5,
            "total_pay_order_count_for_roi2_assist": 2,
            "total_pay_order_gmv_include_coupon_for_roi2_assist": 160,
            "total_prepay_and_pay_order_roi2_assist": 1.2,
            "total_order_settle_amount_for_roi2_1h_assist": 120,
            "total_prepay_and_pay_settle_roi2_1h_assist": 0.9,
            "total_order_settle_count_for_roi2_1h_assist": 1,
            "updated_at": "2026-09-01 22:22:36",
        }

        created = bridge.create_local_stop_task(payload)
        self.assertTrue(created["success"])
        row = bridge._task_row(created["data"]["task_uid"], "tool-user-a")
        card = bridge.build_local_task_card(bridge._task_payload(row))
        raw = json.dumps(card, ensure_ascii=False)

        self.assertIn("命中策略明细", raw)
        self.assertIn("触发层级：调控任务级", raw)
        self.assertIn("调控支付ROI < 1.5", raw)
        self.assertIn("当前调控任务：整体命中", raw)
        self.assertIn("调控支付ROI：实际 1.2 < 阈值 1.5 → 命中", raw)
        self.assertIn("停投参数：** 暂停调控", raw)
        self.assertIn("策略检查：** 每5分钟一轮", raw)
        self.assertIn("调控任务指标快照", raw)
        self.assertIn("平台状态：PROCESSING", raw)
        self.assertIn("调控消耗：88.5 元", raw)
        self.assertIn("调控成交订单数：2 单", raw)
        self.assertIn("调控净成交ROI：0.9", raw)
        self.assertLess(len(raw.encode("utf-8")), 30000)

    def test_expired_execution_lease_recovers_while_card_is_still_valid(self):
        created = bridge.create_local_stop_task(self._stop_payload())
        task_uid = created["data"]["task_uid"]
        row = bridge._task_row(task_uid, "tool-user-a")
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=row["action_nonce"],
            action="approve",
            operator_open_id="ou_owner",
        )
        claimed = bridge.pull_local_stop_task()["data"]
        self.assertEqual("claimed", bridge._task_row(task_uid)["status"])
        conn = bridge._db()
        try:
            conn.execute(
                "UPDATE local_retarget_task SET claim_expires_at=?,expires_at=? "
                "WHERE task_uid=?",
                (
                    (datetime.now() - timedelta(minutes=1)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    (datetime.now() + timedelta(minutes=10)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    task_uid,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        recovered = bridge.pull_local_stop_task()["data"]
        self.assertEqual(task_uid, recovered["task_uid"])
        self.assertNotEqual(claimed["claim_token"], recovered["claim_token"])

    def test_card_shows_plan_scene_account_and_materials(self):
        task = task_payload(3)
        task.update(
            {
                "task_uid": "task-1",
                "status": "pending",
                "action_nonce": "nonce-1",
                "triggered_at": "2026-08-14 20:43:00",
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
        self.assertTrue(
            any(value.get("action") == "save_group" for value in action_values)
        )
        self.assertTrue(
            any(
                value.get("action") == "save_individual_groups"
                for value in action_values
            )
        )
        self.assertIn("合并为1条追投", raw)
        self.assertIn("选中素材分别追投（3条）", raw)
        self.assertIn("触发时间", raw)
        self.assertIn("2026-08-14 20:43:00", raw)
        self.assertIn("策略检查：** 每5分钟一轮", raw)
        self.assertIn("成功限频：** 同一素材24小时内最多1次", raw)
        self.assertTrue(
            all(value.get("instance_uid") for value in action_values)
        )

    def test_success_card_shows_frozen_strategy_rules_and_actual_values(self):
        task = task_payload(2)
        task.update(
            {
                "task_uid": "task-strategy-detail",
                "status": "succeeded",
                "action_nonce": "nonce-strategy-detail",
                "strategy_name": "高ROI素材追投",
                "trigger_level": "material",
                "rule_snapshot": {
                    "id": "strategy-1",
                    "title": "高ROI素材追投",
                    "priority": 1,
                },
                "trigger_snapshot": {
                    "strategy_title": "高ROI素材追投",
                    "trigger_level": "material",
                    "trigger_config": {
                        "group_combine": "or",
                        "groups": [
                            {
                                "join": "and",
                                "conditions": [
                                    {"metric": "currentCost", "op": "gt", "value": 10},
                                    {"metric": "overallPayRoi", "op": "gte", "value": 5},
                                ],
                            }
                        ],
                    },
                    "materials": [
                        {
                            "material_id": "70000",
                            "evaluation": {
                                "group_combine": "or",
                                "passed": True,
                                "groups": [
                                    {
                                        "join": "and",
                                        "passed": True,
                                        "conditions": [
                                            {"metric": "currentCost", "op": "gt", "threshold": 10, "actual": 31.91, "passed": True},
                                            {"metric": "overallPayRoi", "op": "gte", "threshold": 5, "actual": 25.04, "passed": True},
                                        ],
                                    }
                                ],
                            },
                        },
                        {
                            "material_id": "70001",
                            "evaluation": {
                                "group_combine": "or",
                                "passed": True,
                                "groups": [
                                    {
                                        "join": "and",
                                        "passed": True,
                                        "conditions": [
                                            {"metric": "currentCost", "op": "gt", "threshold": 10, "actual": 18.2, "passed": True},
                                            {"metric": "overallPayRoi", "op": "gte", "threshold": 5, "actual": 7.5, "passed": True},
                                        ],
                                    }
                                ],
                            },
                        },
                    ],
                },
                "result_message": "官方 API 追投已核验成功",
                "triggered_at": "2026-09-01 21:46:20",
                "expires_at": "2026-09-01 22:16:23",
            }
        )
        card = bridge.build_task_card(task)
        raw = json.dumps(card, ensure_ascii=False)
        self.assertIn("命中策略明细", raw)
        self.assertIn("策略名称：高ROI素材追投", raw)
        self.assertIn("策略优先级：1", raw)
        self.assertIn("触发层级：素材级", raw)
        self.assertIn("组间关系：任一条件组满足（或）", raw)
        self.assertIn("条件组1（全部条件都满足）", raw)
        self.assertIn("整体消耗 > 10 元", raw)
        self.assertIn("整体支付ROI ≥ 5", raw)
        self.assertIn("命中素材汇总（2条）", raw)
        self.assertIn("测试视频1（素材ID：70000）", raw)
        self.assertIn("测试视频2（素材ID：70001）", raw)
        self.assertIn("实际指标：整体消耗 31.91 元；整体支付ROI 25.04", raw)
        self.assertIn("实际指标：整体消耗 18.2 元；整体支付ROI 7.5", raw)
        self.assertNotIn("候选素材1：整体命中", raw)
        self.assertNotIn("候选素材2：整体命中", raw)
        self.assertNotIn("规则条件已命中", raw)
        self.assertNotIn("currentCost", raw)
        self.assertLess(len(raw.encode("utf-8")), 30000)

    def test_created_card_task_records_local_instance_and_trigger_time(self):
        payload = task_payload(1)
        payload["query_snapshot"] = {"query_at": "2026-08-14 21:00:05"}
        created = bridge.create_local_retarget_task(payload)
        self.assertTrue(created["success"])
        row = bridge._task_row(created["data"]["task_uid"], "tool-user-a")
        task = bridge._task_payload(row or {})
        self.assertEqual(bridge._local_instance_uid(), task["instance_uid"])
        self.assertEqual("2026-08-14 21:00:05", task["triggered_at"])

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
        self.assertIn("1条追投组", approved["message"])
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

    def test_preview_selection_finishes_without_entering_execution_queue(self):
        preview_payload = {
            **task_payload(3),
            "strategy_id": "strategy-preview-only",
            "preview_only": True,
        }
        created = bridge.create_local_retarget_task(preview_payload)
        self.assertTrue(created["success"])
        task_uid = created["data"]["task_uid"]
        row = bridge._task_row(task_uid, "tool-user-a")
        nonce = row["action_nonce"]

        preview_card = bridge.build_task_card(bridge._task_payload(row))
        preview_raw = json.dumps(preview_card, ensure_ascii=False)
        self.assertIn("安全测试：本卡只验证素材选择，不会触发千川操作", preview_raw)
        self.assertIn("确认1组（不追投）", preview_raw)
        self.assertNotIn("确认追投", preview_raw)

        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="clear_selection",
            operator_open_id="ou_owner",
        )
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="toggle_material",
            operator_open_id="ou_owner",
            material_id="70001",
        )
        approved = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertTrue(approved["success"])
        self.assertIn("未进入千川追投队列", approved["message"])
        self.assertIsNone(bridge.pull_local_retarget_task()["data"])

        finished = bridge._task_row(task_uid, "tool-user-a")
        self.assertEqual("cancelled", finished["status"])
        self.assertIsNone(finished["active_dedupe_key"])
        finished_card = json.dumps(
            bridge.build_task_card(bridge._task_payload(finished)),
            ensure_ascii=False,
        )
        self.assertIn("素材自选安全测试 · 测试完成", finished_card)
        self.assertIn("测试结果", finished_card)
        self.assertNotIn("确认1组（不追投）", finished_card)

    def test_one_candidate_batch_can_create_three_overlapping_retarget_groups(self):
        created = bridge.create_local_retarget_task(
            {**task_payload(10), "strategy_id": "strategy-three-groups"}
        )
        task_uid = created["data"]["task_uid"]
        nonce = bridge._task_row(task_uid, "tool-user-a")["action_nonce"]

        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="clear_selection",
            operator_open_id="ou_owner",
        )
        for material_id in ("70000", "70001", "70002"):
            bridge.handle_local_card_action(
                "tool-user-a",
                task_uid=task_uid,
                nonce=nonce,
                action="toggle_material",
                operator_open_id="ou_owner",
                material_id=material_id,
            )
        first_group = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="save_group",
            operator_open_id="ou_owner",
        )
        self.assertTrue(first_group["success"])

        for material_id in ("70000", "70003", "70004", "70005", "70006"):
            bridge.handle_local_card_action(
                "tool-user-a",
                task_uid=task_uid,
                nonce=nonce,
                action="toggle_material",
                operator_open_id="ou_owner",
                material_id=material_id,
            )
        second_group = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="save_group",
            operator_open_id="ou_owner",
        )
        self.assertTrue(second_group["success"])

        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="select_all",
            operator_open_id="ou_owner",
        )
        approved = bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="approve",
            operator_open_id="ou_owner",
        )
        self.assertTrue(approved["success"])
        pulled = bridge.pull_local_retarget_task()["data"]
        groups = pulled["retarget_groups"]
        self.assertEqual([3, 5, 10], [len(group["material_ids"]) for group in groups])
        self.assertIn("70000", groups[0]["material_ids"])
        self.assertIn("70000", groups[1]["material_ids"])
        self.assertIn("70000", groups[2]["material_ids"])
        self.assertEqual(10, len(pulled["materials"]))
        self.assertEqual(3, pulled["selection_snapshot"]["group_count"])
        self.assertEqual(18, pulled["selection_snapshot"]["group_material_count"])

    def test_single_and_multi_material_groups_can_be_mixed(self):
        created = bridge.create_local_retarget_task(
            {**task_payload(4), "strategy_id": "strategy-mixed-groups"}
        )
        task_uid = created["data"]["task_uid"]
        nonce = bridge._task_row(task_uid, "tool-user-a")["action_nonce"]
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="clear_selection",
            operator_open_id="ou_owner",
        )
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="toggle_material",
            operator_open_id="ou_owner",
            material_id="70000",
        )
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="save_group",
            operator_open_id="ou_owner",
        )
        for material_id in ("70001", "70002", "70003"):
            bridge.handle_local_card_action(
                "tool-user-a",
                task_uid=task_uid,
                nonce=nonce,
                action="toggle_material",
                operator_open_id="ou_owner",
                material_id=material_id,
            )
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="approve",
            operator_open_id="ou_owner",
        )
        pulled = bridge.pull_local_retarget_task()["data"]
        self.assertEqual(
            [1, 3],
            [len(group["material_ids"]) for group in pulled["retarget_groups"]],
        )

    def test_selected_materials_can_be_split_into_individual_retarget_groups(self):
        for scene in ("product", "live"):
            with self.subTest(scene=scene):
                created = bridge.create_local_retarget_task(
                    {
                        **task_payload(4),
                        "strategy_id": f"strategy-individual-{scene}",
                        "promotion_scene": scene,
                    }
                )
                task_uid = created["data"]["task_uid"]
                nonce = bridge._task_row(task_uid, "tool-user-a")["action_nonce"]
                split = bridge.handle_local_card_action(
                    "tool-user-a",
                    task_uid=task_uid,
                    nonce=nonce,
                    action="save_individual_groups",
                    operator_open_id="ou_owner",
                )
                self.assertTrue(split["success"])
                self.assertIn("4个单素材追投组", split["message"])
                approved = bridge.handle_local_card_action(
                    "tool-user-a",
                    task_uid=task_uid,
                    nonce=nonce,
                    action="approve",
                    operator_open_id="ou_owner",
                )
                self.assertTrue(approved["success"])
                pulled = bridge.pull_local_retarget_task()["data"]
                self.assertEqual(scene, pulled["promotion_scene"])
                self.assertEqual(
                    [1, 1, 1, 1],
                    [
                        len(group["material_ids"])
                        for group in pulled["retarget_groups"]
                    ],
                )

    def test_individual_and_merged_groups_can_be_combined_on_one_card(self):
        created = bridge.create_local_retarget_task(
            {**task_payload(4), "strategy_id": "strategy-individual-and-merged"}
        )
        task_uid = created["data"]["task_uid"]
        nonce = bridge._task_row(task_uid, "tool-user-a")["action_nonce"]
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="clear_selection",
            operator_open_id="ou_owner",
        )
        for material_id in ("70000", "70001"):
            bridge.handle_local_card_action(
                "tool-user-a",
                task_uid=task_uid,
                nonce=nonce,
                action="toggle_material",
                operator_open_id="ou_owner",
                material_id=material_id,
            )
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="save_individual_groups",
            operator_open_id="ou_owner",
        )
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="select_all",
            operator_open_id="ou_owner",
        )
        bridge.handle_local_card_action(
            "tool-user-a",
            task_uid=task_uid,
            nonce=nonce,
            action="approve",
            operator_open_id="ou_owner",
        )
        pulled = bridge.pull_local_retarget_task()["data"]
        self.assertEqual(
            [1, 1, 4],
            [len(group["material_ids"]) for group in pulled["retarget_groups"]],
        )


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

    def test_binding_code_survives_websocket_bridge_restart(self):
        personal = self.instance.issue_binding_code("personal")
        self.assertTrue(personal["success"])

        # Saving Feishu settings and reconnecting replaces the bridge object.
        # The command already shown to the user must remain valid for 10 minutes.
        self.instance.stop()
        restarted = bridge.LocalFeishuBridge("tool-user-a")
        restarted.send_text = lambda chat_id, text: self.sent.append(("chat", chat_id, text))
        restarted.send_private_text = lambda open_id, text: self.sent.append(("user", open_id, text))
        restarted._on_message(
            self.message(
                message_id="restart-m1",
                text=personal["command"],
                open_id="ou_owner",
                chat_id="oc_personal",
                chat_type="p2p",
            )
        )

        profile = bridge._profile_for("tool-user-a")
        self.assertEqual("ou_owner", profile["authorized_open_id"])
        with open(self.profile_path, "r", encoding="utf-8") as handle:
            persisted = json.load(handle)
        self.assertNotIn("binding_codes", persisted)
        self.assertNotIn(personal["code"], json.dumps(persisted, ensure_ascii=False))

    def test_binding_code_typo_does_not_invalidate_valid_code(self):
        personal = self.instance.issue_binding_code("personal")
        wrong = "000000" if personal["code"] != "000000" else "999999"
        self.assertFalse(self.instance._consume_binding_code("personal", wrong))
        self.assertTrue(self.instance._consume_binding_code("personal", personal["code"]))
        self.assertFalse(self.instance._consume_binding_code("personal", personal["code"]))

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

    def test_foreign_installation_card_action_is_silently_ignored(self):
        local_instance_uid = bridge._local_instance_uid()
        self.instance._on_card_action(
            SimpleNamespace(
                operator=SimpleNamespace(open_id="ou_owner"),
                action=SimpleNamespace(
                    value={
                        "action": "approve",
                        "task_uid": "foreign-local-task",
                        "nonce": "foreign-nonce",
                        "instance_uid": "f" * 32,
                    }
                ),
                message_id="om_foreign",
            )
        )
        self.assertNotEqual("f" * 32, local_instance_uid)
        self.assertEqual([], self.sent)

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

    def test_device_session_switches_an_already_active_other_account(self):
        manager = self.Manager()
        manager.account = "old_user"
        with (
            patch.object(bridge, "_MANAGER", manager),
            patch(
                "services.cloud_retarget_client.load_device_session",
                return_value={"username": "new_user", "token": "device-token"},
            ),
        ):
            self.assertTrue(
                bridge.restore_local_feishu_account_from_device_session()
            )
            self.assertEqual("new_user", manager.account)
            self.assertEqual(["new_user"], manager.activated)


if __name__ == "__main__":
    unittest.main()
