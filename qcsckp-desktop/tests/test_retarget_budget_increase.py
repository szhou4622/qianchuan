# -*- coding: utf-8 -*-
import asyncio
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from api.rule_retargeting_config import (
    _normalize_full,
    validate_rule_retargeting_config,
)
from services.retarget_budget_increase import (
    assist_task_sync_ready,
    budget_increase_fingerprint,
    calculate_budget_increase,
    classify_assist_task,
)
from services.local_feishu_bridge import build_budget_increase_task_card
from services import retargeting_rule_runner


def _strategy(*, task_action="increase_budget", metric="assistCost", increase=None):
    return {
        "id": "strategy-budget-1",
        "title": "调控任务加预算",
        "account_uid": "account-1",
        "target_uid": "target-1",
        "task_action": task_action,
        "action_mode": "card_confirm",
        "trigger": {
            "group_combine": "or",
            "groups": [
                {"join": "and", "conditions": [{"metric": metric, "op": "gt", "value": 100}]}
            ],
        },
        "retargeting": {
            "method": "volume",
            "volume": {"total_budget_yuan": 200, "duration_hours": 1},
            "budget_increase": increase
            or {
                "mode": "fixed",
                "fixed_amount_yuan": 200,
                "spend_percentage": None,
                "volume_extend_hours": 1,
            },
            "interval": {"window_seconds": 86400, "max_count": 1},
        },
    }


class RetargetBudgetIncreaseConfigTests(unittest.TestCase):
    def test_normalize_preserves_budget_increase_action(self):
        cfg = _normalize_full({"enabled": True, "strategies": [_strategy()]})
        strategy = cfg["strategies"][0]
        self.assertEqual("increase_budget", strategy["task_action"])
        self.assertEqual(200, strategy["retargeting"]["budget_increase"]["fixed_amount_yuan"])

    def test_increase_budget_accepts_only_task_metrics(self):
        cfg = {"enabled": True, "strategies": [_strategy(metric="costDiff")]}
        ok, message = validate_rule_retargeting_config(cfg)
        self.assertFalse(ok)
        self.assertIn("只能使用调控消耗和调控ROI", message)

    def test_create_retarget_rejects_task_metrics(self):
        cfg = {"enabled": True, "strategies": [_strategy(task_action="create_retarget")]}
        ok, message = validate_rule_retargeting_config(cfg)
        self.assertFalse(ok)
        self.assertIn("不能使用调控任务指标", message)

    def test_percentage_mode_validates_percentage(self):
        cfg = {
            "enabled": True,
            "strategies": [
                _strategy(
                    increase={
                        "mode": "spend_percentage",
                        "fixed_amount_yuan": None,
                        "spend_percentage": 20,
                        "volume_extend_hours": 1,
                    }
                )
            ],
        }
        self.assertEqual((True, ""), validate_rule_retargeting_config(cfg))


class RetargetBudgetIncreaseCalculationTests(unittest.TestCase):
    def test_fixed_amount_adds_to_latest_budget(self):
        result = calculate_budget_increase(
            {
                "assist_task_id": "assist-volume",
                "budget": "400",
                "start_time": "2026-08-10 10:00:00",
                "end_time": "2026-08-10 12:00:00",
                "stat_cost_for_roi2_assist": 300,
            },
            {"mode": "fixed", "fixed_amount_yuan": 200, "volume_extend_hours": 1.5},
        )
        self.assertEqual(600.0, result["new_budget_yuan"])
        self.assertEqual(200.0, result["increment_budget_yuan"])
        self.assertEqual(1.5, result["extend_hours"])
        self.assertEqual("volume", result["task_kind"])

    def test_percentage_uses_latest_control_task_spend(self):
        result = calculate_budget_increase(
            {
                "assist_task_id": "assist-roi",
                "budget": "400",
                "ecp_roi2_goal": 2.8,
                "stat_cost_for_roi2_assist": "300",
            },
            {"mode": "spend_percentage", "spend_percentage": 20, "volume_extend_hours": 1},
        )
        self.assertEqual(60.0, result["increment_budget_yuan"])
        self.assertEqual(460.0, result["new_budget_yuan"])
        self.assertIsNone(result["extend_hours"])
        self.assertEqual("cost_control_roi", result["task_kind"])

    def test_conversion_task_only_increases_budget(self):
        row = {
            "assist_task_id": "assist-conversion",
            "budget": 400,
            "bid": 20,
            "deep_external_action_name": "直播间成交",
        }
        self.assertEqual("cost_control_conversion", classify_assist_task(row))
        result = calculate_budget_increase(
            row,
            {"mode": "fixed", "fixed_amount_yuan": 200, "volume_extend_hours": 3},
        )
        self.assertEqual(600.0, result["new_budget_yuan"])
        self.assertIsNone(result["extend_hours"])

    def test_fingerprint_changes_when_latest_budget_changes(self):
        first = calculate_budget_increase(
            {"assist_task_id": "a1", "budget": 400, "start_time": "a", "end_time": "b"},
            {"mode": "fixed", "fixed_amount_yuan": 200, "volume_extend_hours": 1},
        )
        second = calculate_budget_increase(
            {"assist_task_id": "a1", "budget": 600, "start_time": "a", "end_time": "b"},
            {"mode": "fixed", "fixed_amount_yuan": 200, "volume_extend_hours": 1},
        )
        self.assertNotEqual(
            budget_increase_fingerprint(target_uid="t", strategy_id="s", calculation=first),
            budget_increase_fingerprint(target_uid="t", strategy_id="s", calculation=second),
        )

    def test_assist_sync_must_be_complete_and_fresh(self):
        ready, message = assist_task_sync_ready(
            {
                "capability_json": {
                    "assist_sync_enabled": True,
                    "assist_sync_ok": True,
                    "assist_sync_in_progress": False,
                    "assist_synced_at": datetime.now().isoformat(timespec="seconds"),
                }
            }
        )
        self.assertTrue(ready)
        self.assertEqual("", message)
        blocked, blocked_message = assist_task_sync_ready(
            {"capability_json": {"assist_sync_enabled": True, "assist_sync_ok": False}}
        )
        self.assertFalse(blocked)
        self.assertIn("未完整同步", blocked_message)

    def test_budget_card_shows_percentage_basis_and_final_budget(self):
        card = build_budget_increase_task_card(
            {
                "task_uid": "task-1",
                "action_nonce": "nonce-1",
                "status": "pending",
                "account_name": "测试账户",
                "aavid": "123",
                "plan_name": "测试计划",
                "ad_id": "456",
                "promotion_scene": "live",
                "plan_system": "chengfang",
                "assist_task_id": "assist-1",
                "assist_task_name": "放量追投1",
                "strategy_name": "消耗达标加预算",
                "budget_increase": {"mode": "spend_percentage"},
                "calculation_snapshot": {
                    "task_kind": "volume",
                    "mode": "spend_percentage",
                    "current_budget_yuan": 400,
                    "latest_spend_yuan": 300,
                    "spend_percentage": 20,
                    "increment_budget_yuan": 60,
                    "new_budget_yuan": 460,
                    "extend_hours": 1,
                },
            }
        )
        content = str(card)
        self.assertIn("按最新消耗", content)
        self.assertIn("新增后预算", content)
        self.assertIn("460", content)
        self.assertIn("确认追加预算", content)

    def test_runner_matches_control_task_and_creates_budget_card(self):
        strategy = _strategy(
            increase={
                "mode": "spend_percentage",
                "fixed_amount_yuan": None,
                "spend_percentage": 20,
                "volume_extend_hours": 1,
            }
        )
        target = {
            "target_uid": "target-1",
            "account_uid": "account-1",
            "aadvid": "1001",
            "ad_id": "2001",
            "plan_name": "直播计划",
            "promotion_scene": "live",
            "plan_system": "global",
            "retarget_eligible": 1,
            "enabled": 1,
            "last_status": "ok",
            "capability_json": json.dumps(
                {
                    "assist_sync_enabled": True,
                    "assist_sync_ok": True,
                    "assist_sync_in_progress": False,
                    "assist_synced_at": datetime.now().isoformat(timespec="seconds"),
                }
            ),
        }
        assist_row = {
            "target_uid": "target-1",
            "aadvid": "1001",
            "ad_id": "2001",
            "assist_task_id": "assist-1",
            "task_name": "放量任务1",
            "budget": 400,
            "start_time": "2026-08-10 10:00:00",
            "end_time": "2026-08-10 12:00:00",
            "ad_delivery_type": 0,
            "stat_cost_for_roi2_assist": 300,
            "total_prepay_and_pay_order_roi2_assist": 2.5,
        }

        class FakeDashboard:
            def get_table_data(self, **_kwargs):
                raise AssertionError("budget-only strategies must not read material data")

            def get_roi2_assist_table_data(self, **_kwargs):
                return {"success": True, "data": [assist_row], "total": 1}

            def get_dashboard_account_label(self):
                return {"label": "测试账户"}

        class FakeStore:
            def select(self, table, **_kwargs):
                return [target] if table == "promotion_target" else []

            def select_one(self, table, **_kwargs):
                if table == "pmc_ad_detail_basic":
                    return {"user_info_name": "测试账户"}
                return None

        with patch(
            "services.retargeting_rule_runner.load_rule_retargeting_config",
            return_value={
                "enabled": True,
                "trigger_query_period": "1h",
                "strategies": [strategy],
            },
        ), patch(
            "services.retargeting_rule_runner.DashboardApi",
            return_value=FakeDashboard(),
        ), patch(
            "services.retargeting_rule_runner.evaluate_trigger",
            return_value=True,
        ), patch(
            "services.retargeting_rule_runner.build_trigger_evaluation_snapshot",
            return_value={"passed": True},
        ), patch(
            "services.retargeting_rule_runner.create_retarget_task",
            return_value={"success": True, "data": {"task_uid": "budget-card-1"}},
        ) as create_task:
            asyncio.run(retargeting_rule_runner.run_one_cycle(FakeStore()))

        self.assertEqual(1, create_task.call_count)
        payload = create_task.call_args.args[0]
        self.assertEqual("increase_budget", payload["task_operation"])
        self.assertEqual("assist-1", payload["assist_task_id"])
        self.assertEqual(60.0, payload["calculation_snapshot"]["increment_budget_yuan"])
        self.assertEqual(460.0, payload["calculation_snapshot"]["new_budget_yuan"])


if __name__ == "__main__":
    unittest.main()
