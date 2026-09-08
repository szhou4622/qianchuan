"""Independent control snapshots use real SQLite and mock API responses only."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from services import collection_lifecycle as lifecycle
from services import control_metric_collection as controls
from services import official_api_collection as collection
from services import regulation_rule_runner as rules
from services.control_task_cycle import stop_cycle_state
from services.qianchuan_open_api.client import ApiResponse
from services.qianchuan_open_api.collection_context import CollectionContext, use_collection_context
from services.qianchuan_open_api.errors import ApiRequestError, CollectionCancelledError
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class IndependentControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qcsckp-r20-controls-")
        self.db = SQLiteStore(database=str(Path(self.temp.name) / "test.db"))
        init_sqlite_schema(database=self.db.config["database"])
        self.owner, self.uid = "control-owner", "control-target"
        self.now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.units = {"stat_cost_for_roi2": "3", "stat_cost_for_roi2_assist": "3",
                      "total_pay_order_count_for_roi2_assist": "4"}
        self.cap = {"regulation_execute": True, "marketing_goal": "LIVE_PROM_GOODS",
                    "report_metric_units": self.units, "report_config_synced_at": self.now,
                    "plan_detail_synced_at": self.now, "control_history_synced_at": self.now,
                    "material_report_filter_context": {"ad_id": "2001"},
                    "material_marker": "must_survive"}
        self.db.insert("qianchuan_account", {"account_uid": "control-account", "owner_username": self.owner,
                        "aavid": "1001", "enabled": 1, "directory_selected": 1})
        self.db.insert("promotion_target", {"target_uid": self.uid, "account_uid": "control-account",
                        "aadvid": "1001", "ad_id": "2001", "promotion_scene": "live", "plan_system": "global",
                        "platform_status": "active", "verification_state": "verified", "enabled": 1,
                        "monitor_eligible": 1, "stop_eligible": 1, "capacity_state": "active", "last_status": "ok",
                        "capability_json": json.dumps(self.cap)})
        self.trigger = {"group_combine": "and", "groups": [{"join": "and", "conditions": [
            {"metric": "stat_cost_for_roi2_assist", "op": "gt", "value": 10}]}]}
        self.strategy = {"id": "stop-control", "title": "Stop", "target_uid": self.uid,
                         "account_uid": "control-account", "aavid": "1001", "trigger": self.trigger}
        self.service = Mock()
        self.service.get_plan_detail.return_value = ({"aavid": "1001", "ad_id": "2001",
            "marketing_goal": "LIVE_PROM_GOODS", "adlab_scene": "UNI_PROJECT", "platform_status": "active"},
            ApiResponse(data={}, raw={}, request_id="detail-request"))
        self.service.list_control_tasks.side_effect = self.control_response
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(collection, "_owner_key", return_value=self.owner))
        self.stack.enter_context(patch.object(collection, "get_official_api_service", return_value=self.service))
        self.stack.enter_context(patch.object(lifecycle, "resource_pressure", return_value={"critical": False}))
        self.stack.enter_context(patch.object(collection, "_account_backoff_remaining", return_value=0))
        self.wake = self.stack.enter_context(patch.object(rules, "request_regulation_rule_evaluation"))
        self.stack.enter_context(patch("services.qianchuan_session.current_session_owner", return_value=self.owner))
        self.stack.enter_context(patch("services.qianchuan_session.automation_session_ready", return_value={"ready": True, "session_epoch": 1}))
        self.stack.enter_context(patch.object(rules, "load_rule_regulation_config", return_value={"enabled": True, "strategies": [self.strategy]}))
        self.auth_held = False
        @contextmanager
        def guard(expected):
            self.assertFalse(self.auth_held)
            self.auth_held = True
            try:
                yield
            finally:
                self.auth_held = False
        self.stack.enter_context(patch("services.qianchuan_open_api.token_provider.authorization_identity_guard", side_effect=guard))
        self.stack.enter_context(patch("services.qianchuan_open_api.token_provider.authorization_identity_is_current", return_value=True))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def target(self):
        return self.db.select_one("promotion_target", where={"target_uid": self.uid})

    def capability(self):
        return json.loads(self.target()["capability_json"])

    def row(self):
        return self.db.select_one("pmc_roi2_assist_task", where={"target_uid": self.uid, "assist_task_id": "3001"})

    def context(self):
        context = CollectionContext(300, generation=lifecycle.generation())
        context.authorization_identity = {"owner_username": self.owner, "app_id": "test-app", "auth_generation": "test-generation"}
        context.target_identity = {"target_uid": self.uid, "owner_username": self.owner,
            **{key: str(self.target()[key]) for key in ("account_uid", "aadvid", "ad_id", "promotion_scene", "plan_system")}}
        return context

    def control_response(self, *args, **kwargs):
        self.assertFalse(self.auth_held, "Never hold the authorization guard during network reads")
        self.assertTrue(self.capability().get("assist_sync_in_progress"))
        return ([{"task_id": "3001", "ad_id": "2001", "task_name": "Control", "budget": 100,
                  "duration": 24, "scene": "MATERIAL_ADD_BUDGET", "status": "PROCESSING",
                  "status_source": "api_filtered" if kwargs.get("active_only") else "api",
                  "material_ids": ["4001"], "metrics": {"stat_cost_for_roi2_assist": 100},
                  "raw": {"advertiser_id": "1001", "ad_id": "2001", "scene": "MATERIAL_ADD_BUDGET"}}],
                ["control-request"])

    def collect(self):
        with use_collection_context(self.context()):
            return controls.collect_control_metrics(self.target(), db=self.db)

    def revalidate(self):
        return rules._revalidate_stop_candidate(self.db, original_strategy=self.strategy,
            expected_owner=self.owner, expected_session_epoch=1, target_uid=self.uid, assist_task_id="3001",
            aavid="1001", ad_id="2001", promotion_scene="live", trigger=self.trigger, max_age_minutes=30)

    def test_material_pagination_failure_keeps_committed_control_usable_for_stop(self):
        self.service.list_plan_materials.side_effect = ApiRequestError("素材分页不完整", code="bad-page")
        with use_collection_context(self.context()):
            outcome = collection._collect_target_safely(self.target(), db=self.db, interval_seconds=300,
                                                          independent_controls=True)
        self.assertFalse(outcome["success"])
        self.service.list_plan_materials.assert_called_once()
        self.assertEqual(100, self.row()["stat_cost_for_roi2_assist"])
        self.assertTrue(self.capability()["assist_sync_ok"])
        self.assertEqual(1, self.capability()["active_control_task_count"])
        self.assertFalse(self.capability()["assist_sync_in_progress"])
        self.assertEqual("", self.revalidate()[-1])
        self.assertEqual("must_survive", self.capability()["material_marker"])

    def test_commit_takes_authorization_before_db_lock(self):
        original = self.db._get_connection
        connections = []
        lock_observations = []
        def connect():
            connection = original()
            connection.set_trace_callback(lambda sql: lock_observations.append(self.auth_held) if sql == "BEGIN IMMEDIATE" else None)
            connections.append(connection)
            return connection
        with patch.object(self.db, "_get_connection", side_effect=connect):
            result = self.collect()
        self.assertTrue(result["independently_committed"])
        self.assertTrue(connections)
        self.assertEqual([True, True], lock_observations)

    def test_wakeup_failure_does_not_invalidate_already_committed_controls(self):
        self.wake.side_effect = RuntimeError("synthetic wake failure")
        result = self.collect()
        self.assertTrue(result["independently_committed"])
        self.assertTrue(self.capability()["assist_sync_ok"])
        self.assertEqual(100, self.row()["stat_cost_for_roi2_assist"])

    def test_in_progress_is_visible_before_first_plan_api_read(self):
        response = self.service.get_plan_detail.return_value
        def detail(*args, **kwargs):
            self.assertFalse(self.auth_held)
            self.assertTrue(self.capability()["assist_sync_in_progress"])
            return response
        self.service.get_plan_detail.side_effect = detail
        self.collect()

    def test_raw_account_or_plan_echo_mismatch_never_commits_rows(self):
        for key, value in (("ad_id", "9999"), ("advertiser_id", "9999"), ("adId", "9999"), ("aavid", "9999")):
            with self.subTest(key=key):
                def bad(*args, **kwargs):
                    rows, ids = self.control_response(*args, **kwargs)
                    rows[0]["raw"][key] = value
                    return rows, ids
                self.service.list_control_tasks.side_effect = bad
                with self.assertRaisesRegex(RuntimeError, "归属"):
                    self.collect()
                self.assertIsNone(self.row())

    def test_plan_raw_identity_conflict_is_not_hidden_by_normalized_defaults(self):
        detail, response = self.service.get_plan_detail.return_value
        self.service.get_plan_detail.return_value = ({**detail, "raw": {"aavid": "9999"}}, response)
        with self.assertRaisesRegex(RuntimeError, "归属"):
            self.collect()
        self.service.list_control_tasks.assert_not_called()
        self.assertIsNone(self.row())

    def test_wrong_scene_is_not_accepted_as_a_successful_empty_control_list(self):
        def wrong(*args, **kwargs):
            rows, ids = self.control_response(*args, **kwargs)
            rows[0]["scene"] = rows[0]["raw"]["scene"] = "OTHER_SCENE"
            return rows, ids
        self.service.list_control_tasks.side_effect = wrong
        with self.assertRaisesRegex(RuntimeError, "Scene-2"):
            self.collect()
        self.assertIsNone(self.row())

    def test_suspicious_empty_preserves_previous_values_but_blocks_rules(self):
        self.collect()
        original = self.row()
        self.service.list_control_tasks.side_effect = None
        self.service.list_control_tasks.return_value = ([], ["empty-request"])
        result = self.collect()
        self.assertTrue(result["control_suspicious_empty"])
        self.assertEqual(original["stat_cost_for_roi2_assist"], self.row()["stat_cost_for_roi2_assist"])
        self.assertEqual(original["metrics_observed_at"], self.row()["metrics_observed_at"])
        self.assertFalse(self.capability()["assist_sync_ok"])
        self.assertNotEqual("", self.revalidate()[-1])

    def test_metric_age_is_not_repaired_by_recent_other_update(self):
        self.collect()
        self.db.update("pmc_roi2_assist_task", {"metrics_observed_at": "2000-01-01 00:00:00"},
                       where={"target_uid": self.uid})
        self.assertIn("指标已过期", self.revalidate()[-1])

    def test_omitted_metric_stays_unknown_instead_of_reusing_previous_spend(self):
        self.collect()
        def missing(*args, **kwargs):
            rows, ids = self.control_response(*args, **kwargs)
            rows[0]["metrics"] = {}
            return rows, ids
        self.service.list_control_tasks.side_effect = missing
        self.collect()
        self.assertIsNone(self.row()["stat_cost_for_roi2_assist"])
        self.assertIn("不满足停投策略", self.revalidate()[-1])

    def test_independent_flags_never_bypass_verification_error_or_scope_change(self):
        self.collect()
        self.db.update("promotion_target", {"last_status": "verification_error"}, where={"target_uid": self.uid})
        self.assertNotEqual("", self.revalidate()[-1])
        self.db.update("promotion_target", {"last_status": "pagination_error", "verification_state": "error"}, where={"target_uid": self.uid})
        self.assertIn("身份", self.revalidate()[-1])
        self.db.update("promotion_target", {"verification_state": "verified", "ad_id": "9999"}, where={"target_uid": self.uid})
        self.assertNotEqual("", self.revalidate()[-1])

    def test_new_authorization_or_collection_generation_blocks_old_snapshot(self):
        self.collect()
        with patch("services.qianchuan_open_api.token_provider.authorization_identity_is_current", return_value=False):
            self.assertIn("授权代次", self.revalidate()[-1])
        with patch.object(lifecycle, "generation", return_value="new-generation"):
            self.assertIn("采集代次", self.revalidate()[-1])

    def test_confirmed_stop_cycle_cannot_be_reactivated_by_filtered_old_list(self):
        self.collect()
        ended = (datetime.now() - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
        self.db.insert("pmc_regulation_run", {"aavid": "1001", "ad_id": "2001", "target_uid": self.uid,
            "assist_task_id": "3001", "started_at": ended, "ended_at": ended, "status": 1,
            "execution_state": "confirmed_succeeded", "execution_uid": "old-stop"})
        self.collect()
        self.assertEqual(1, self.row()["ad_delivery_type"])
        self.assertNotEqual("", self.revalidate()[-1])

    def test_context_cancelled_during_read_cannot_publish_late_metrics(self):
        context = self.context()
        def cancel(*args, **kwargs):
            result = self.control_response(*args, **kwargs)
            context.cancel("old generation")
            return result
        self.service.list_control_tasks.side_effect = cancel
        with use_collection_context(context), self.assertRaises(CollectionCancelledError):
            controls.collect_control_metrics(self.target(), db=self.db)
        self.assertIsNone(self.row())

    def test_scope_changed_during_read_cannot_commit_old_target_metrics(self):
        def changed(*args, **kwargs):
            result = self.control_response(*args, **kwargs)
            self.db.update("promotion_target", {"ad_id": "9999"}, where={"target_uid": self.uid})
            return result
        self.service.list_control_tasks.side_effect = changed
        with self.assertRaises(CollectionCancelledError):
            self.collect()
        self.assertIsNone(self.row())

    def test_resume_needs_fresh_explicit_observation_and_invalidates_old_cycle_key(self):
        self.collect()
        original_key = stop_cycle_state(self.db, self.uid, "3001", assist_row=self.row())["cycle_key"]
        ended = (datetime.now() - timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
        resumed = (datetime.now() - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
        self.db.insert("pmc_regulation_run", {"aavid": "1001", "ad_id": "2001", "target_uid": self.uid,
            "assist_task_id": "3001", "started_at": ended, "ended_at": ended, "status": 1,
            "execution_state": "confirmed_succeeded", "execution_uid": "old-stop"})
        self.db.insert("account_operation_event", {"event_uid": "resume-event", "platform_event_id": "platform-resume",
            "source": "qianchuan_open_api", "account_uid": "control-account", "aavid": "1001", "ad_id": "2001",
            "target_uid": self.uid, "action_type": "control_resume", "object_type": "assist_task",
            "object_id": "3001", "regulate_task_id": "3001", "status": "success", "occurred_at": resumed})
        self.collect()
        self.assertEqual("api", self.row()["task_status_source"])
        self.assertEqual(0, self.row()["ad_delivery_type"])
        self.assertEqual("", self.revalidate()[-1])
        reason = rules._pre_submit_stop_check(self.db, expected_cycle_key=original_key,
            original_strategy=self.strategy, expected_owner=self.owner, expected_session_epoch=1,
            target_uid=self.uid, assist_task_id="3001", aavid="1001", ad_id="2001", promotion_scene="live",
            trigger=self.trigger, max_age_minutes=30)
        self.assertIn("周期", reason)
