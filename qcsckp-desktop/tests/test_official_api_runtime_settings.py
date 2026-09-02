import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.views import Api
from services import retargeting_rule_runner
from services.qianchuan_open_api.runtime_settings import (
    enable_execution_for_saved_rules,
    persist_official_api_runtime,
)


class OfficialApiRuntimeSettingsTests(unittest.TestCase):
    def test_api_configuration_endpoints_are_available_before_backend_bootstrap(self):
        api = Api.__new__(Api)
        with (
            patch("api.views.QIANCHUAN_BACKEND", "browser_legacy"),
            patch(
                "services.qianchuan_open_api.configuration.get_configuration",
                return_value={"success": True, "configured": False},
            ) as get_configuration,
            patch(
                "services.qianchuan_open_api.configuration.save_and_start_authorization",
                return_value={"success": True, "authorization_pending": True},
            ) as start_authorization,
        ):
            status = api.getQianchuanOfficialApiConfig()
            started = api.saveAndStartQianchuanOfficialApiAuthorization(
                {"app_id": "1869344049893595", "app_secret": "secret-value"}
            )

        self.assertTrue(status["success"])
        self.assertTrue(started["success"])
        get_configuration.assert_called_once_with()
        start_authorization.assert_called_once_with(
            "1869344049893595", "secret-value"
        )

    def test_persisted_backend_and_write_permission_survive_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "runtime.json"
            target.write_text('{"preserved": true}', encoding="utf-8")

            result = persist_official_api_runtime(
                allow_live_writes=True,
                path=str(target),
                apply_runtime=False,
            )

            self.assertEqual("official_api", result["backend"])
            self.assertTrue(result["allow_live_api_writes"])
            self.assertTrue(result["preserved"])
            self.assertEqual(result, json.loads(target.read_text(encoding="utf-8")))

    def test_disabled_rules_do_not_enable_real_writes(self):
        with patch(
            "services.qianchuan_open_api.runtime_settings.persist_official_api_runtime"
        ) as persist:
            result = enable_execution_for_saved_rules(
                {"enabled": False, "strategies": [{"action_mode": "auto_execute"}]}
            )

        persist.assert_not_called()
        self.assertIsInstance(result, dict)

    def test_active_saved_rule_is_explicit_write_opt_in(self):
        with patch(
            "services.qianchuan_open_api.runtime_settings.persist_official_api_runtime",
            return_value={"backend": "official_api", "allow_live_api_writes": True},
        ) as persist:
            result = enable_execution_for_saved_rules(
                {"enabled": True, "strategies": [{"action_mode": "auto_execute"}]}
            )

        persist.assert_called_once_with(allow_live_writes=True)
        self.assertTrue(result["allow_live_api_writes"])

    def test_saved_auto_rule_restores_write_runtime_after_restart(self):
        config = {
            "enabled": True,
            "strategies": [{"action_mode": "auto_execute"}],
        }
        with (
            patch("config.QIANCHUAN_BACKEND", "official_api"),
            patch(
                "services.qianchuan_open_api.runtime_settings.load_runtime_settings",
                return_value={"backend": "official_api"},
            ),
            patch(
                "services.qianchuan_open_api.runtime_settings.enable_execution_for_saved_rules",
                return_value={"allow_live_api_writes": True},
            ) as enable_writes,
        ):
            restored = (
                retargeting_rule_runner.ensure_official_api_auto_execution_runtime(
                    config
                )
            )

        self.assertTrue(restored)
        enable_writes.assert_called_once_with(config)

    def test_retarget_rule_save_restores_monitor_only_after_validation(self):
        api = Api.__new__(Api)
        api.db = object()
        merged = {
            "enabled": True,
            "strategies": [
                {"target_uid": "target-one", "account_uid": ""}
            ],
        }
        disabled_target = {
            "target_uid": "target-one",
            "account_uid": "account-one",
            "enabled": False,
            "account_enabled": True,
            "retarget_eligible": True,
        }
        restored_target = {**disabled_target, "enabled": True}

        def validate_end_state(_config, targets):
            self.assertTrue(targets["target-one"]["enabled"])
            return True, ""

        with (
            patch("api.views.QIANCHUAN_BACKEND", "browser_legacy"),
            patch(
                "api.rule_retargeting_config.preview_merge",
                return_value=merged,
            ),
            patch(
                "api.rule_retargeting_config.validate_rule_retargeting_config",
                return_value=(True, ""),
            ),
            patch(
                "api.rule_retargeting_config.validate_strategy_target_compatibility",
                side_effect=validate_end_state,
            ),
            patch(
                "api.rule_retargeting_config.merge_and_save",
                return_value=merged,
            ),
            patch(
                "api.promotion_targets.get_promotion_target",
                return_value=disabled_target,
            ),
            patch(
                "api.promotion_targets.set_promotion_target_enabled",
                return_value=restored_target,
            ) as enable_target,
        ):
            result = api.setRuleRetargetingConfig(merged)

        self.assertTrue(result["success"])
        self.assertEqual("account-one", merged["strategies"][0]["account_uid"])
        enable_target.assert_called_once_with("target-one", True, db=api.db)

    def test_invalid_retarget_rule_never_enables_monitor_as_side_effect(self):
        api = Api.__new__(Api)
        api.db = object()
        merged = {
            "enabled": True,
            "strategies": [
                {"target_uid": "target-one", "account_uid": "account-other"}
            ],
        }
        disabled_target = {
            "target_uid": "target-one",
            "account_uid": "account-one",
            "enabled": False,
            "account_enabled": True,
            "retarget_eligible": True,
        }
        with (
            patch(
                "api.rule_retargeting_config.preview_merge",
                return_value=merged,
            ),
            patch(
                "api.rule_retargeting_config.validate_rule_retargeting_config",
                return_value=(True, ""),
            ),
            patch(
                "api.rule_retargeting_config.validate_strategy_target_compatibility",
                return_value=(False, "account mismatch"),
            ),
            patch(
                "api.promotion_targets.get_promotion_target",
                return_value=disabled_target,
            ),
            patch(
                "api.promotion_targets.set_promotion_target_enabled"
            ) as enable_target,
        ):
            result = api.setRuleRetargetingConfig(merged)

        self.assertFalse(result["success"])
        enable_target.assert_not_called()

    def test_official_api_rule_save_enables_writes_and_wakes_scheduler(self):
        api = Api.__new__(Api)
        api.db = object()
        merged = {
            "enabled": True,
            "strategies": [
                {
                    "id": "strategy-one",
                    "target_uid": "target-one",
                    "account_uid": "account-one",
                    "action_mode": "auto_execute",
                }
            ],
        }
        target = {
            "target_uid": "target-one",
            "account_uid": "account-one",
            "enabled": True,
            "account_enabled": True,
            "retarget_eligible": True,
        }
        with (
            patch("api.views.QIANCHUAN_BACKEND", "official_api"),
            patch(
                "api.rule_retargeting_config.preview_merge",
                return_value=merged,
            ),
            patch(
                "api.rule_retargeting_config.validate_rule_retargeting_config",
                return_value=(True, ""),
            ),
            patch(
                "api.rule_retargeting_config.validate_strategy_target_compatibility",
                return_value=(True, ""),
            ),
            patch(
                "api.rule_retargeting_config.merge_and_save",
                return_value=merged,
            ),
            patch(
                "api.promotion_targets.get_promotion_target",
                return_value=target,
            ),
            patch(
                "services.qianchuan_open_api.runtime_settings.enable_execution_for_saved_rules",
                return_value={"allow_live_api_writes": True},
            ) as enable_writes,
            patch(
                "services.retarget_task_worker._strategy_hash",
                return_value="a" * 64,
            ) as strategy_hash,
            patch(
                "services.local_feishu_bridge.invalidate_stale_local_retarget_tasks",
                return_value={"success": True, "count": 2, "task_uids": ["1", "2"]},
            ) as invalidate_cards,
            patch(
                "services.retargeting_rule_runner.request_retargeting_rule_evaluation",
                return_value=True,
            ) as wake_runner,
        ):
            result = api.setRuleRetargetingConfig(merged)

        self.assertTrue(result["success"])
        self.assertTrue(result["officialApiWritesEnabled"])
        self.assertEqual(2, result["invalidatedCardCount"])
        enable_writes.assert_called_once_with(merged)
        strategy_hash.assert_called_once_with(merged["strategies"][0])
        invalidate_cards.assert_called_once_with(
            {"strategy-one": "a" * 64},
            config_enabled=True,
        )
        wake_runner.assert_called_once_with("rule_saved")


if __name__ == "__main__":
    unittest.main()
