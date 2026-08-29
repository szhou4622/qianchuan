# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from api.rule_regulation_config import (
    _normalize_full,
    bind_and_validate_strategy_targets,
    evaluate_trigger_roi2_assist,
    validate_rule_regulation_config,
)
from api.views import Api
from services.regulation_rule_runner import (
    DEFAULT_INTERVAL_SEC,
    _revalidate_stop_candidate,
    _send_auto_stop_submitted_notification,
    _shadow_mode_enabled,
    _stop_strategy_snapshot,
)


class StopStrategyAccountScopeTests(unittest.TestCase):
    def test_stop_rule_interval_is_five_minutes(self):
        self.assertEqual(300, DEFAULT_INTERVAL_SEC)

    def test_stop_shadow_mode_is_explicit_and_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QCSCKP_REGULATION_SHADOW_MODE", None)
            self.assertFalse(_shadow_mode_enabled())
        with patch.dict(os.environ, {"QCSCKP_REGULATION_SHADOW_MODE": "1"}):
            self.assertTrue(_shadow_mode_enabled())

    def test_unsupported_legacy_metric_is_preserved_and_blocks_enable(self):
        config = self._config()
        config["strategies"][0]["trigger"]["groups"][0]["conditions"][0][
            "metric"
        ] = "show_cnt_for_roi2_assist"
        normalized = _normalize_full(config)
        strategy = normalized["strategies"][0]
        condition = strategy["trigger"]["groups"][0]["conditions"][0]
        self.assertEqual("show_cnt_for_roi2_assist", condition["metric"])
        self.assertIn("不支持的指标", strategy["validation_error"])
        ok, message = validate_rule_regulation_config(normalized)
        self.assertFalse(ok)
        self.assertIn("不是当前官方调控接口支持", message)

    def test_net_settled_order_count_is_supported(self):
        config = self._config()
        config["strategies"][0]["trigger"]["groups"][0]["conditions"][0][
            "metric"
        ] = "total_order_settle_count_for_roi2_1h_assist"
        normalized = _normalize_full(config)
        ok, message = validate_rule_regulation_config(normalized)
        self.assertTrue(ok, message)

    def test_all_seven_task_metrics_can_trigger_and_null_cannot(self):
        metrics = (
            "stat_cost_for_roi2_assist",
            "total_pay_order_count_for_roi2_assist",
            "total_pay_order_gmv_include_coupon_for_roi2_assist",
            "total_prepay_and_pay_order_roi2_assist",
            "total_order_settle_amount_for_roi2_1h_assist",
            "total_prepay_and_pay_settle_roi2_1h_assist",
            "total_order_settle_count_for_roi2_1h_assist",
        )
        for metric in metrics:
            with self.subTest(metric=metric):
                trigger = {
                    "group_combine": "or",
                    "groups": [
                        {
                            "join": "and",
                            "conditions": [
                                {"metric": metric, "op": "gt", "value": 1}
                            ],
                        }
                    ],
                }
                self.assertTrue(evaluate_trigger_roi2_assist(trigger, {metric: 2}))
                self.assertFalse(evaluate_trigger_roi2_assist(trigger, {metric: None}))
                self.assertFalse(evaluate_trigger_roi2_assist(trigger, {}))

    def test_auto_stop_submission_sends_verifying_card(self):
        with patch(
            "services.qianchuan_accounts.resolve_account_feishu_targets",
            return_value=[("open_id", "ou_owner")],
        ), patch(
            "services.local_feishu_bridge.send_local_feishu_bound_card"
        ) as send:
            _send_auto_stop_submitted_notification(
                Mock(),
                owner="owner-a",
                aavid="10001",
                account_name="测试账户",
                plan_name="测试计划",
                ad_id="20002",
                promotion_scene="live",
                plan_system="chengfang",
                task_name="测试调控任务",
                assist_task_id="30003",
                stop_action="delete",
                message="官方 API 停投已提交，正在核验平台最终状态",
            )
        card = send.call_args.args[0]
        self.assertIn("已提交，正在核验", card["header"]["title"]["content"])
        self.assertIn("结束调控", card["elements"][0]["text"]["content"])
        self.assertFalse(send.call_args.kwargs["require_connected"])

    def _config(self, **strategy_overrides):
        strategy = {
            "id": "stop-one",
            "title": "低ROI停投",
            "account_uid": "account-one",
            "aavid": "10001",
            "target_uid": "target-one",
            "trigger": {
                "group_combine": "or",
                "groups": [
                    {
                        "join": "and",
                        "conditions": [
                            {
                                "metric": "stat_cost_for_roi2_assist",
                                "op": "gte",
                                "value": 100,
                            }
                        ],
                    }
                ],
            },
            "regulation_stop_action": "pause",
            "action_mode": "card_confirm",
        }
        strategy.update(strategy_overrides)
        return {"enabled": True, "strategies": [strategy]}

    def _target(self, **overrides):
        target = {
            "target_uid": "target-one",
            "account_uid": "account-one",
            "aadvid": "10001",
            "enabled": True,
            "monitor_eligible": True,
            "stop_eligible": True,
        }
        target.update(overrides)
        return target

    def test_normalization_persists_explicit_account_and_plan_scope(self):
        normalized = _normalize_full(self._config())
        strategy = normalized["strategies"][0]
        self.assertEqual("account-one", strategy["account_uid"])
        self.assertEqual("10001", strategy["aavid"])
        self.assertEqual("target-one", strategy["target_uid"])

    def test_legacy_target_only_strategy_is_bound_to_exact_account(self):
        config = self._config(account_uid="", aavid="")
        target = self._target()

        ok, message = bind_and_validate_strategy_targets(
            config,
            {"target-one": target},
            {"account-one": {"account_uid": "account-one", "enabled": True}},
        )

        self.assertTrue(ok, message)
        self.assertEqual("account-one", config["strategies"][0]["account_uid"])
        self.assertEqual("10001", config["strategies"][0]["aavid"])

    def test_cross_account_plan_selection_is_rejected(self):
        config = self._config(account_uid="account-other", aavid="20002")

        ok, message = bind_and_validate_strategy_targets(
            config,
            {"target-one": self._target()},
            {"account-one": {"account_uid": "account-one", "enabled": True}},
        )

        self.assertFalse(ok)
        self.assertIn("账户与监控计划不一致", message)

    def test_enabled_strategy_requires_enabled_account_and_stop_eligible_plan(self):
        config = self._config()
        ok, message = bind_and_validate_strategy_targets(
            config,
            {"target-one": self._target()},
            {"account-one": {"account_uid": "account-one", "enabled": False}},
        )
        self.assertFalse(ok)
        self.assertIn("账户尚未启用", message)

        ok, message = bind_and_validate_strategy_targets(
            self._config(),
            {"target-one": self._target(stop_eligible=False)},
            {"account-one": {"account_uid": "account-one", "enabled": True}},
        )
        self.assertFalse(ok)
        self.assertIn("尚未取得停投资格", message)

    def test_waiting_live_plan_can_save_enabled_stop_strategy(self):
        ok, message = bind_and_validate_strategy_targets(
            self._config(),
            {
                "target-one": self._target(
                    stop_eligible=False,
                    promotion_scene="live",
                    platform_status="waiting_live",
                    verification_state="verified",
                )
            },
            {"account-one": {"account_uid": "account-one", "enabled": True}},
        )
        self.assertTrue(ok, message)

    def test_disabled_config_can_be_saved_even_if_old_target_disappeared(self):
        config = self._config()
        config["enabled"] = False

        ok, message = bind_and_validate_strategy_targets(config, {}, {})

        self.assertTrue(ok, message)

    def test_stop_authorization_snapshot_includes_account_scope(self):
        snapshot = _stop_strategy_snapshot(self._config()["strategies"][0])

        self.assertEqual("account-one", snapshot["account_uid"])
        self.assertEqual("10001", snapshot["aavid"])
        self.assertEqual("target-one", snapshot["target_uid"])

    def test_api_save_hydrates_legacy_account_scope_before_persisting(self):
        api = Api.__new__(Api)
        api.db = object()
        target = self._target()
        config = self._config(account_uid="", aavid="")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rule_regulation.json")
            with (
                patch(
                    "api.rule_regulation_config.config_path",
                    return_value=path,
                ),
                patch(
                    "api.promotion_targets.get_promotion_target",
                    return_value=target,
                ),
                patch(
                    "services.qianchuan_accounts.list_qianchuan_accounts",
                    return_value=[
                        {"account_uid": "account-one", "enabled": True}
                    ],
                ),
            ):
                result = api.setRuleRegulationConfig(config)

            self.assertTrue(result["success"], result.get("message"))
            with open(path, "r", encoding="utf-8") as handle:
                persisted = json.load(handle)
            strategy = persisted["strategies"][0]
            self.assertEqual("account-one", strategy["account_uid"])
            self.assertEqual("10001", strategy["aavid"])

    def test_api_save_enabled_rule_restores_disabled_monitor_target(self):
        api = Api.__new__(Api)
        api.db = object()
        target = self._target(enabled=False)
        restored = self._target(enabled=True)
        config = self._config(account_uid="", aavid="")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rule_regulation.json")
            with (
                patch("api.views.QIANCHUAN_BACKEND", "browser_legacy"),
                patch("api.rule_regulation_config.config_path", return_value=path),
                patch(
                    "api.promotion_targets.get_promotion_target",
                    return_value=target,
                ),
                patch(
                    "api.promotion_targets.set_promotion_target_enabled",
                    return_value=restored,
                ) as enable_target,
                patch(
                    "services.qianchuan_accounts.list_qianchuan_accounts",
                    return_value=[{"account_uid": "account-one", "enabled": True}],
                ),
            ):
                result = api.setRuleRegulationConfig(config)

        self.assertTrue(result["success"], result.get("message"))
        enable_target.assert_called_once_with("target-one", True, db=api.db)

    def test_api_rejects_cross_account_plan_tampering(self):
        api = Api.__new__(Api)
        api.db = object()
        config = self._config(account_uid="account-other", aavid="20002")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rule_regulation.json")
            with (
                patch(
                    "api.rule_regulation_config.config_path",
                    return_value=path,
                ),
                patch(
                    "api.promotion_targets.get_promotion_target",
                    return_value=self._target(),
                ),
                patch(
                    "services.qianchuan_accounts.list_qianchuan_accounts",
                    return_value=[
                        {"account_uid": "account-one", "enabled": True}
                    ],
                ),
            ):
                result = api.setRuleRegulationConfig(config)

            self.assertFalse(result["success"])
            self.assertIn("账户与监控计划不一致", result["message"])
            self.assertFalse(os.path.exists(path))

    def test_execution_revalidation_rejects_strategy_account_mismatch(self):
        target = {
            **self._target(),
            "ad_id": "20001",
            "promotion_scene": "product",
            "plan_system": "global",
            "capacity_state": "active",
            "automation_write_blocked": False,
            "last_status": "ok",
        }
        rows = {
            "promotion_target": target,
            "qianchuan_account": {
                "account_uid": "account-one",
                "owner_username": "owner",
                "enabled": True,
            },
            "pmc_roi2_assist_task": {
                "target_uid": "target-one",
                "assist_task_id": "assist-one",
                "account_uid": "account-one",
                "aadvid": "10001",
                "ad_id": "20001",
                "promotion_scene": "product",
                "plan_system": "global",
                "ad_delivery_type": 0,
            },
        }

        class FakeStore:
            def select_one(self, table, **_kwargs):
                return rows.get(table)

        strategy = self._config(
            account_uid="account-other",
            aavid="20002",
        )["strategies"][0]
        with (
            patch(
                "services.qianchuan_session.current_session_owner",
                return_value="owner",
            ),
            patch(
                "services.qianchuan_session.automation_session_ready",
                return_value={"ready": True, "session_epoch": 1},
            ),
            patch(
                "services.regulation_rule_runner.load_rule_regulation_config",
                return_value={"enabled": True, "strategies": [strategy]},
            ),
        ):
            _target, _row, _system, error = _revalidate_stop_candidate(
                FakeStore(),
                original_strategy=strategy,
                expected_owner="owner",
                expected_session_epoch=1,
                target_uid="target-one",
                assist_task_id="assist-one",
                aavid="10001",
                ad_id="20001",
                promotion_scene="product",
                trigger=strategy["trigger"],
                max_age_minutes=30,
            )

        self.assertIn("目标身份变化", error)


if __name__ == "__main__":
    unittest.main()
