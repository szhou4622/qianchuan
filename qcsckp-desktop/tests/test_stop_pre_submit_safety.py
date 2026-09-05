"""Stop authorization regressions. All API writes and credentials are mocked."""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta
import unittest
from unittest.mock import ANY, Mock, patch

from api.views import Api
from services.official_api_execution import OfficialApiRegulationStopService
from services.qianchuan_open_api.client import ApiResponse
from services.qianchuan_open_api.errors import ApiRateLimitError
from services.regulation_rule_runner import (
    _pre_submit_stop_check, _revalidate_stop_candidate,
)


class StopSubmitSafetyTests(unittest.TestCase):
    def run_stop(self, service, guard):
        effect = service.update_control_status.side_effect
        response = service.update_control_status.return_value
        service.transport = Mock()
        def send(*args, before_send=None, **kwargs):
            if before_send:
                before_send()
            service.transport(*args, **kwargs)
            if isinstance(effect, BaseException):
                raise effect
            return response
        service.update_control_status.side_effect = send
        with ExitStack() as stack:
            stack.enter_context(patch("services.official_api_execution.get_official_api_service", return_value=service))
            stack.enter_context(patch("services.official_api_execution._check_plan", return_value={}))
            stack.enter_context(patch("services.official_api_execution._find_control_task", return_value={
                "task_id": "30003", "scene": "MATERIAL_ADD_BUDGET", "status": "PROCESSING",
            }))
            stack.enter_context(patch("services.official_api_reconciliation.reserve_execution_intent", return_value=({}, True)))
            finish = stack.enter_context(patch("services.official_api_reconciliation.record_execution_submission_phase"))
            enqueue = stack.enter_context(patch("services.official_api_reconciliation.enqueue_execution_reconciliation"))
            stack.enter_context(patch("services.official_api_reconciliation.start_official_api_reconciliation_background_thread"))
            result = asyncio.run(OfficialApiRegulationStopService().run(
                aavid=10001, ad_id=20002, assist_task_id="30003", stop_action="pause",
                execution_uid="test-stop", control_cycle_key="cycle-one", pre_submit_check=guard,
            ))
        return result, finish, enqueue

    def test_changed_authorization_after_api_reads_blocks_post(self):
        for reason in ("规则化停投已关闭", "停投策略参数已经变更", "调控任务已加入停投白名单"):
            with self.subTest(reason=reason):
                service, guard = Mock(), Mock(return_value=reason)
                result, finish, enqueue = self.run_stop(service, guard)
                guard.assert_called_once()
                service.transport.assert_not_called()
                enqueue.assert_not_called()
                self.assertEqual("stop_preflight_blocked", result.step)
                self.assertIn("未向千川提交", result.message)
                finish.assert_not_called()  # authorization failed before intent/send

    def test_valid_final_guard_sends_exactly_once(self):
        service, guard = Mock(), Mock(return_value="")
        service.update_control_status.return_value = ApiResponse(data={}, raw={"code": 0}, request_id="req-stop")
        result, _finish, enqueue = self.run_stop(service, guard)
        self.assertEqual("submitted_verifying", result.step)
        guard.assert_called_once()
        service.update_control_status.assert_called_once_with(10001, ["30003"], action="PAUSE", before_send=ANY)
        service.transport.assert_called_once_with(10001, ["30003"], action="PAUSE")
        enqueue.assert_called_once()

    def test_rate_limit_has_no_sleep_or_automatic_second_post(self):
        service = Mock()
        service.update_control_status.side_effect = ApiRateLimitError("rate limited", retry_after=60)
        with patch("services.official_api_execution.asyncio.sleep") as sleep:
            result, _finish, enqueue = self.run_stop(service, Mock(return_value=""))
        self.assertFalse(result.success)
        service.update_control_status.assert_called_once()
        sleep.assert_not_called()
        enqueue.assert_not_called()

    def test_pre_submit_rechecks_cycle_not_just_metrics(self):
        for state in ({"blocked": True, "cycle_key": "cycle-one"},
                      {"blocked": False, "cycle_key": "cycle-two"}):
            with self.subTest(state=state), patch(
                "services.regulation_rule_runner._revalidate_stop_candidate",
                return_value=({}, {"assist_task_id": "30003"}, "global", ""),
            ), patch("services.regulation_rule_runner.stop_cycle_state", return_value=state):
                reason = _pre_submit_stop_check(Mock(), expected_cycle_key="cycle-one",
                                               target_uid="target-one", assist_task_id="30003")
                self.assertIn("周期", reason)

    def test_fresh_plan_sync_does_not_hide_stale_task_metrics(self):
        strategy = {"id": "stop-one", "target_uid": "target-one"}
        target = dict(target_uid="target-one", account_uid="account-one", enabled=1,
                      monitor_eligible=1, stop_eligible=1, capacity_state="active", last_status="ok",
                      aadvid="10001", ad_id="20002", promotion_scene="live", plan_system="global")
        for updated_at in ("", (datetime.now() - timedelta(hours=1)).isoformat()):
            with self.subTest(updated_at=updated_at):
                rows = {"promotion_target": target,
                        "qianchuan_account": {"owner_username": "owner-a", "enabled": 1},
                        "pmc_roi2_assist_task": {**target, "updated_at": updated_at}}
                db = Mock()
                db.select_one.side_effect = lambda table, **_: rows[table]
                with patch("services.qianchuan_session.current_session_owner", return_value="owner-a"), patch(
                    "services.qianchuan_session.automation_session_ready", return_value={"ready": True, "session_epoch": 1}
                ), patch("services.regulation_rule_runner.load_rule_regulation_config", return_value={
                    "enabled": True, "strategies": [strategy],
                }), patch("services.regulation_rule_runner._target_assist_sync_ready", return_value=(True, "")):
                    *_, error = _revalidate_stop_candidate(
                        db, original_strategy=strategy, expected_owner="owner-a", expected_session_epoch=1,
                        target_uid="target-one", assist_task_id="30003", aavid="10001", ad_id="20002",
                        promotion_scene="live", trigger={}, max_age_minutes=30,
                    )
                self.assertIn("指标", error)


class StopSaveSafetyTests(unittest.TestCase):
    def test_save_invalidates_old_cards_before_requesting_new_rule_pass(self):
        api = Api.__new__(Api)
        api.db = Mock()
        config = {"enabled": False, "strategies": []}
        calls = []
        with ExitStack() as stack:
            stack.enter_context(patch("api.views.QIANCHUAN_BACKEND", "official_api"))
            for name in ("preview_merge", "merge_and_save"):
                stack.enter_context(patch("api.rule_regulation_config." + name, return_value=config))
            for name in ("validate_rule_regulation_config", "bind_and_validate_strategy_targets"):
                stack.enter_context(patch("api.rule_regulation_config." + name, return_value=(True, "")))
            stack.enter_context(patch("services.qianchuan_accounts.list_qianchuan_accounts", return_value=[]))
            stack.enter_context(patch("services.qianchuan_open_api.runtime_settings.enable_execution_for_saved_rules", return_value={}))
            stack.enter_context(patch("services.qianchuan_session.current_session_owner", return_value="owner-a"))
            invalidate = stack.enter_context(patch("services.local_feishu_bridge.invalidate_obsolete_local_stop_tasks",
                side_effect=lambda *args: calls.append("invalidate") or {"count": 2}))
            wake = stack.enter_context(patch("services.regulation_rule_runner.request_regulation_rule_evaluation",
                side_effect=lambda *args: calls.append("wake")))
            result = api.setRuleRegulationConfig(config)
        self.assertTrue(result["success"])
        self.assertEqual(2, result["invalidatedCardCount"])
        self.assertEqual(["invalidate", "wake"], calls)
        invalidate.assert_called_once_with("owner-a", config)
        wake.assert_called_once_with("rule_saved")


if __name__ == "__main__":
    unittest.main()
