# -*- coding: utf-8 -*-
import unittest

from api.rule_retargeting_config import (
    _normalize_full,
    validate_rule_retargeting_config,
)
from services.retarget_budget_increase import (
    budget_increase_fingerprint,
    calculate_budget_increase,
    classify_assist_task,
)


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


if __name__ == "__main__":
    unittest.main()

