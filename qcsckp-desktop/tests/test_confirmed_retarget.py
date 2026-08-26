# -*- coding: utf-8 -*-
import asyncio
import copy
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from api.operation_events import (
    _normalize_occurred_at,
    export_operation_events_csv,
    get_operation_event,
    ingest_platform_log_rows,
    list_operation_accounts,
    normalize_action_type,
    prune_operation_events,
    query_operation_events_page,
    update_platform_sync_state,
    upsert_operation_event,
)
from api.rule_retargeting_config import (
    _normalize_full,
    validate_rule_retargeting_config,
    validate_strategy_target_compatibility,
)
from services.cloud_retarget_client import report_retarget_task
from services import local_test_guard
from services import retarget_task_worker
from services.promotion_capability import RETARGET_FORM_PROBE_VERSION
from services.run_services import (
    ServiceConfig,
    _choose_startup_target,
    _is_qianchuan_login_url,
    _known_promotion_target_keys,
    _load_last_target,
    _promotion_target_key,
    _reuse_last_target_enabled,
    _save_last_target,
    _target_is_excluded,
    _trusted_startup_discovery,
)
from services.operation_log_monitor import (
    _classify_write,
    _extract_platform_rows,
    _explicit_has_more,
    _paginate_body,
    _paginate_url,
    _prepare_replay_body,
    _replay_has_explicit_30_day_range,
    _thirty_day_coverage_window,
    _with_30_day_body,
    _with_30_day_range,
)
from services.retarget_task_worker import (
    _snapshot_hash,
    _strategy_hash,
    _strategy_matches_task_snapshot,
    _strategy_snapshot,
)
from services import retargeting_rule_runner
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


TEST_CAPABILITY_VERIFIED_AT = datetime.now().isoformat(timespec="seconds")


def _retarget_capability_json(
    *,
    target_uid,
    aavid,
    ad_id,
    scene,
    plan_system,
    batch=False,
):
    capability = {
        "retarget_execute": True,
        "retarget_scene": scene,
        "retarget_plan_system": plan_system,
        "retarget_probe_version": RETARGET_FORM_PROBE_VERSION,
        "retarget_verified_at": TEST_CAPABILITY_VERIFIED_AT,
        "retarget_target_uid": target_uid,
        "retarget_aavid": aavid,
        "retarget_ad_id": ad_id,
        "report_metric_units": {
            "stat_cost_for_roi2": "3",
            "total_order_settle_amount_for_roi2_1h": "3",
            "total_prepay_and_pay_settle_roi2_1h": "3",
            "total_order_settle_count_for_roi2_1h": "0",
            "total_pay_order_gmv_include_coupon_for_roi2": "3",
            "total_prepay_and_pay_order_roi2": "3",
        },
    }
    if batch:
        capability.update(
            {
                "retarget_batch_execute": True,
                "retarget_batch_probe_version": RETARGET_FORM_PROBE_VERSION,
                "retarget_batch_verified_at": TEST_CAPABILITY_VERIFIED_AT,
            }
        )
    return json.dumps(capability)


class RetargetConfigTests(unittest.TestCase):
    def test_strategy_normalization_preserves_account_scope(self):
        normalized = _normalize_full(
            {
                "strategies": [
                    {
                        "id": "strategy-account",
                        "account_uid": "account-1",
                        "target_uid": "target-1",
                    }
                ]
            }
        )
        strategy = normalized["strategies"][0]
        self.assertEqual("account-1", strategy["account_uid"])
        self.assertEqual("target-1", strategy["target_uid"])

    def test_strategy_target_compatibility_rejects_cross_account_plan(self):
        config = {
            "enabled": True,
            "strategies": [
                {
                    "title": "跨账户策略",
                    "account_uid": "account-left",
                    "target_uid": "target-right",
                    "retargeting": {"method": "volume"},
                }
            ],
        }
        ok, message = validate_strategy_target_compatibility(
            config,
            {
                "target-right": {
                    "account_uid": "account-right",
                    "account_enabled": True,
                    "enabled": True,
                    "retarget_eligible": True,
                    "promotion_scene": "live",
                }
            },
        )
        self.assertFalse(ok)
        self.assertIn("监控账户与计划不一致", message)

    def test_waiting_live_plan_can_save_enabled_retarget_strategy(self):
        config = {
            "enabled": True,
            "strategies": [
                {
                    "title": "开播后追投",
                    "account_uid": "account-live",
                    "target_uid": "target-live",
                    "retargeting": {"method": "volume"},
                }
            ],
        }
        ok, message = validate_strategy_target_compatibility(
            config,
            {
                "target-live": {
                    "account_uid": "account-live",
                    "account_enabled": True,
                    "enabled": True,
                    "monitor_eligible": True,
                    "retarget_eligible": False,
                    "promotion_scene": "live",
                    "platform_status": "waiting_live",
                    "verification_state": "verified",
                }
            },
        )
        self.assertTrue(ok, message)

    def test_strategy_hash_includes_account_scope(self):
        base = {
            "id": "strategy-1",
            "target_uid": "target-1",
            "trigger": {},
            "retargeting": {},
        }
        left = _strategy_hash({**base, "account_uid": "account-left"})
        right = _strategy_hash({**base, "account_uid": "account-right"})
        self.assertNotEqual(left, right)

    def test_card_account_name_is_resolved_per_target(self):
        class FakeStore:
            def select_one(self, table, **kwargs):
                self.calls.append((table, kwargs.get("where")))
                if table == "qianchuan_account":
                    return {"account_name": "目标千川账户"}
                return None

            def __init__(self):
                self.calls = []

        store = FakeStore()
        name = retargeting_rule_runner._account_name_for_target(
            store,
            {
                "account_uid": "account-1",
                "aadvid": "10001",
                "ad_id": "30001",
            },
            "全局标签",
        )
        self.assertEqual("目标千川账户", name)
        self.assertEqual(
            [("qianchuan_account", {"account_uid": "account-1"})],
            store.calls,
        )

    def test_legacy_card_snapshot_without_default_task_action_is_accepted(self):
        strategy = {
            "id": "strategy-1",
            "title": "策略1",
            "account_uid": "account-1",
            "target_uid": "target-1",
            "trigger_level": "material",
            "candidate_limit": 1,
            "action_mode": "card_confirm",
            "trigger": {"groups": []},
            "retargeting": {"method": "volume"},
        }
        legacy_snapshot = _strategy_snapshot(strategy)
        legacy_snapshot.pop("task_action")
        self.assertTrue(
            _strategy_matches_task_snapshot(
                strategy,
                legacy_snapshot,
                _snapshot_hash(legacy_snapshot),
            )
        )

    def test_legacy_card_snapshot_without_grouping_mode_is_accepted(self):
        strategy = {
            "id": "strategy-1",
            "title": "策略1",
            "account_uid": "account-1",
            "target_uid": "target-1",
            "trigger_level": "material",
            "candidate_limit": 1,
            "action_mode": "card_confirm",
            "trigger": {"groups": []},
            "retargeting": {"method": "volume"},
        }
        legacy_snapshot = _strategy_snapshot(strategy)
        legacy_snapshot.pop("material_grouping_mode")
        self.assertTrue(
            _strategy_matches_task_snapshot(
                strategy,
                legacy_snapshot,
                _snapshot_hash(legacy_snapshot),
            )
        )

    def test_legacy_card_bridge_does_not_hide_real_strategy_change(self):
        strategy = {
            "id": "strategy-1",
            "title": "策略1",
            "target_uid": "target-1",
            "trigger": {"groups": []},
            "retargeting": {"method": "volume"},
        }
        legacy_snapshot = _strategy_snapshot(strategy)
        legacy_snapshot.pop("task_action")
        changed = copy.deepcopy(strategy)
        changed["retargeting"] = {"method": "volume", "volume": {"total_budget_yuan": 200}}
        self.assertFalse(
            _strategy_matches_task_snapshot(
                changed,
                legacy_snapshot,
                _snapshot_hash(legacy_snapshot),
            )
        )

    def test_existing_strategy_defaults_to_card_confirmation(self):
        cfg = _normalize_full(
            {
                "enabled": True,
                "strategies": [
                    {
                        "id": "s1",
                        "title": "旧策略",
                        "trigger": {"group_combine": "or", "groups": []},
                        "retargeting": {"method": "volume", "volume": {"total_budget_yuan": 100}},
                    }
                ],
            }
        )
        self.assertEqual("card_confirm", cfg["strategies"][0]["action_mode"])

    def test_auto_execute_is_preserved(self):
        cfg = _normalize_full(
            {
                "strategies": [
                    {
                        "id": "s1",
                        "action_mode": "auto_execute",
                        "trigger": {},
                        "retargeting": {},
                    }
                ]
            }
        )
        self.assertEqual("auto_execute", cfg["strategies"][0]["action_mode"])

    def test_material_grouping_mode_defaults_to_separate_and_preserves_merged(self):
        legacy = _normalize_full({"strategies": [{"id": "s1", "trigger": {}, "retargeting": {}}]})
        self.assertEqual("separate", legacy["strategies"][0]["material_grouping_mode"])

        merged = _normalize_full(
            {
                "strategies": [
                    {
                        "id": "s1",
                        "material_grouping_mode": "merged",
                        "candidate_limit": 20,
                        "trigger": {},
                        "retargeting": {},
                    }
                ]
            }
        )
        self.assertEqual("merged", merged["strategies"][0]["material_grouping_mode"])
        self.assertEqual(20, merged["strategies"][0]["candidate_limit"])

    def test_invalid_material_grouping_mode_is_rejected_when_enabled(self):
        config = _normalize_full(
            {
                "enabled": True,
                "strategies": [
                    {
                        "id": "s1",
                        "target_uid": "target-1",
                        "trigger": {},
                        "retargeting": {},
                    }
                ],
            }
        )
        config["strategies"][0]["material_grouping_mode"] = "unsupported"
        ok, message = validate_rule_retargeting_config(config)
        self.assertFalse(ok)
        self.assertIn("分组方式无效", message)

    def test_local_test_environment_blocks_auto_execute(self):
        with patch.object(retargeting_rule_runner, "TEST_MODE", True):
            self.assertFalse(retargeting_rule_runner.auto_execute_allowed_in_current_environment())
        with patch.object(retargeting_rule_runner, "TEST_MODE", False):
            self.assertTrue(retargeting_rule_runner.auto_execute_allowed_in_current_environment())

    def test_transient_collection_state_keeps_fresh_completed_snapshot_usable(self):
        fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stale = (datetime.now() - timedelta(minutes=11)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        self.assertTrue(
            retargeting_rule_runner.target_has_usable_collection_snapshot(
                {"last_status": "collecting", "last_sync_at": fresh}
            )
        )
        self.assertTrue(
            retargeting_rule_runner.target_has_usable_collection_snapshot(
                {"last_status": "queued", "last_sync_at": fresh}
            )
        )
        self.assertFalse(
            retargeting_rule_runner.target_has_usable_collection_snapshot(
                {"last_status": "collecting", "last_sync_at": stale}
            )
        )
        self.assertFalse(
            retargeting_rule_runner.target_has_usable_collection_snapshot(
                {"last_status": "queued", "last_sync_at": ""}
            )
        )

    def test_rule_runner_wake_request_is_consumed_without_waiting_for_interval(self):
        while retargeting_rule_runner._wait_for_next_rule_cycle(0.01):
            pass
        self.assertTrue(
            retargeting_rule_runner.request_retargeting_rule_evaluation(
                "test"
            )
        )
        started = datetime.now()
        self.assertTrue(
            retargeting_rule_runner._wait_for_next_rule_cycle(2)
        )
        self.assertLess((datetime.now() - started).total_seconds(), 0.5)

    def test_auto_revalidation_allows_collecting_with_fresh_snapshot(self):
        strategy = {
            "id": "auto-collecting",
            "title": "自动追投",
            "target_uid": "target-live",
            "trigger_level": "material",
            "action_mode": "auto_execute",
            "trigger": {"groups": []},
            "retargeting": {
                "method": "volume",
                "volume": {"total_budget_yuan": 100, "duration_hours": 1},
            },
        }
        cfg = {
            "enabled": True,
            "trigger_query_period": "1h",
            "per_strategy_rate_limit": False,
            "interval": {"window_seconds": 3600, "max_count": 1},
            "strategies": [copy.deepcopy(strategy)],
        }
        target = {
            "target_uid": "target-live",
            "aadvid": "10001",
            "ad_id": "30001",
            "promotion_scene": "live",
            "plan_system": "global",
            "last_status": "collecting",
            "last_sync_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "enabled": 1,
            "monitor_eligible": 1,
            "retarget_eligible": 1,
            "capability_json": _retarget_capability_json(
                target_uid="target-live",
                aavid="10001",
                ad_id="30001",
                scene="live",
                plan_system="global",
            ),
        }

        class FakeStore:
            def select(self, _table, **_kwargs):
                return []

        class FakeDashboard:
            def get_table_data(self, **_kwargs):
                return {
                    "success": True,
                    "data": [
                        {
                            "targetUid": "target-live",
                            "aadvid": "10001",
                            "id": "m1",
                            "periodEndTime": datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                        }
                    ],
                }

        with patch(
            "services.retargeting_rule_runner.current_session_owner",
            return_value="tool-owner",
        ), patch(
            "services.retargeting_rule_runner.automation_session_ready",
            return_value={"ready": True},
        ), patch(
            "services.retargeting_rule_runner.load_rule_retargeting_config",
            return_value=cfg,
        ), patch(
            "services.retargeting_rule_runner.schedulable_promotion_targets",
            return_value=[target],
        ), patch(
            "services.retargeting_rule_runner.DashboardApi",
            return_value=FakeDashboard(),
        ), patch(
            "services.retargeting_rule_runner.evaluate_trigger",
            return_value=True,
        ), patch(
            "services.retargeting_rule_runner.rate_limit_should_skip",
            return_value=False,
        ):
            _, _, current_target = (
                retargeting_rule_runner._revalidate_auto_retarget_under_lock(
                    FakeStore(),
                    original_strategy=strategy,
                    target_uid="target-live",
                    aavid="10001",
                    ad_id="30001",
                    promotion_scene="live",
                    plan_system="global",
                    material_id="m1",
                    product_id="",
                )
            )

        self.assertEqual("collecting", current_target["last_status"])

    def test_auto_execute_revalidates_strategy_and_material_under_browser_lock(self):
        strategy = {
            "id": "auto-s1",
            "title": "自动追投",
            "target_uid": "target-live",
            "trigger_level": "material",
            "action_mode": "auto_execute",
            "trigger": {"groups": []},
            "retargeting": {
                "method": "volume",
                "volume": {"total_budget_yuan": 100, "duration_hours": 1},
            },
        }
        target = {
            "target_uid": "target-live",
            "aadvid": "10001",
            "ad_id": "30001",
            "promotion_scene": "live",
            "plan_system": "global",
            "last_status": "ok",
            "last_sync_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "enabled": 1,
            "monitor_eligible": 1,
            "retarget_eligible": 1,
            "capability_json": _retarget_capability_json(
                target_uid="target-live",
                aavid="10001",
                ad_id="30001",
                scene="live",
                plan_system="global",
            ),
        }

        class FakeStore:
            def select(self, _table, **_kwargs):
                return []

        class FakeDashboard:
            def get_table_data(self, **_kwargs):
                return {
                    "success": True,
                    "data": [
                        {
                            "targetUid": "target-live",
                            "aadvid": "10001",
                            "id": "m1",
                            "periodEndTime": datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                        }
                    ],
                }

        cfg = {
            "enabled": True,
            "trigger_query_period": "1h",
            "per_strategy_rate_limit": False,
            "interval": {"window_seconds": 3600, "max_count": 1},
            "strategies": [copy.deepcopy(strategy)],
        }
        with patch(
            "services.retargeting_rule_runner.current_session_owner",
            return_value="tool-owner",
        ), patch(
            "services.retargeting_rule_runner.automation_session_ready",
            return_value={"ready": True},
        ), patch(
            "services.retargeting_rule_runner.load_rule_retargeting_config",
            return_value=cfg,
        ), patch(
            "services.retargeting_rule_runner.schedulable_promotion_targets",
            return_value=[target],
        ), patch(
            "services.retargeting_rule_runner.DashboardApi",
            return_value=FakeDashboard(),
        ), patch(
            "services.retargeting_rule_runner.evaluate_trigger",
            return_value=True,
        ), patch(
            "services.retargeting_rule_runner.rate_limit_should_skip",
            return_value=False,
        ):
            current_cfg, current_strategy, current_target = (
                retargeting_rule_runner._revalidate_auto_retarget_under_lock(
                    FakeStore(),
                    original_strategy=strategy,
                    target_uid="target-live",
                    aavid="10001",
                    ad_id="30001",
                    promotion_scene="live",
                    plan_system="global",
                    material_id="m1",
                    product_id="",
                )
            )
        self.assertIs(cfg, current_cfg)
        self.assertEqual("auto-s1", current_strategy["id"])
        self.assertEqual("target-live", current_target["target_uid"])

        stale_target = copy.deepcopy(target)
        stale_target["last_sync_at"] = (
            datetime.now() - timedelta(minutes=11)
        ).strftime("%Y-%m-%d %H:%M:%S")
        with patch(
            "services.retargeting_rule_runner.current_session_owner",
            return_value="tool-owner",
        ), patch(
            "services.retargeting_rule_runner.automation_session_ready",
            return_value={"ready": True},
        ), patch(
            "services.retargeting_rule_runner.load_rule_retargeting_config",
            return_value=cfg,
        ), patch(
            "services.retargeting_rule_runner.schedulable_promotion_targets",
            return_value=[stale_target],
        ):
            with self.assertRaisesRegex(RuntimeError, "超过10分钟"):
                retargeting_rule_runner._revalidate_auto_retarget_under_lock(
                    FakeStore(),
                    original_strategy=strategy,
                    target_uid="target-live",
                    aavid="10001",
                    ad_id="30001",
                    promotion_scene="live",
                    plan_system="global",
                    material_id="m1",
                    product_id="",
                )

        class FakeStaleDashboard:
            def get_table_data(self, **_kwargs):
                return {
                    "success": True,
                    "data": [
                        {
                            "targetUid": "target-live",
                            "aadvid": "10001",
                            "id": "m1",
                            "periodEndTime": (
                                datetime.now() - timedelta(minutes=11)
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    ],
                }

        with patch(
            "services.retargeting_rule_runner.current_session_owner",
            return_value="tool-owner",
        ), patch(
            "services.retargeting_rule_runner.automation_session_ready",
            return_value={"ready": True},
        ), patch(
            "services.retargeting_rule_runner.load_rule_retargeting_config",
            return_value=cfg,
        ), patch(
            "services.retargeting_rule_runner.schedulable_promotion_targets",
            return_value=[target],
        ), patch(
            "services.retargeting_rule_runner.DashboardApi",
            return_value=FakeStaleDashboard(),
        ):
            with self.assertRaisesRegex(RuntimeError, "素材实时数据已超过10分钟"):
                retargeting_rule_runner._revalidate_auto_retarget_under_lock(
                    FakeStore(),
                    original_strategy=strategy,
                    target_uid="target-live",
                    aavid="10001",
                    ad_id="30001",
                    promotion_scene="live",
                    plan_system="global",
                    material_id="m1",
                    product_id="",
                )

        changed = copy.deepcopy(cfg)
        changed["strategies"][0]["retargeting"]["volume"][
            "total_budget_yuan"
        ] = 200
        with patch(
            "services.retargeting_rule_runner.current_session_owner",
            return_value="tool-owner",
        ), patch(
            "services.retargeting_rule_runner.automation_session_ready",
            return_value={"ready": True},
        ), patch(
            "services.retargeting_rule_runner.load_rule_retargeting_config",
            return_value=changed,
        ):
            with self.assertRaisesRegex(RuntimeError, "策略参数已经变更"):
                retargeting_rule_runner._revalidate_auto_retarget_under_lock(
                    FakeStore(),
                    original_strategy=strategy,
                    target_uid="target-live",
                    aavid="10001",
                    ad_id="30001",
                    promotion_scene="live",
                    plan_system="global",
                    material_id="m1",
                    product_id="",
                )

    def test_strategy_snapshot_hash_detects_parameter_tampering(self):
        strategy = {
            "id": "s1",
            "title": "策略",
            "action_mode": "card_confirm",
            "trigger": {"groups": []},
            "retargeting": {"method": "volume", "volume": {"total_budget_yuan": 100}},
        }
        snapshot = copy.deepcopy(_strategy_snapshot(strategy))
        self.assertEqual(_strategy_hash(strategy), _snapshot_hash(snapshot))
        snapshot["retargeting"]["volume"]["total_budget_yuan"] = 999
        self.assertNotEqual(_strategy_hash(strategy), _snapshot_hash(snapshot))

    def test_one_strategy_cycle_creates_one_card_with_all_product_materials(self):
        strategy = {
            "id": "batch-strategy",
            "title": "批量商品追投",
            "target_uid": "target-product",
            "trigger_level": "material",
            "action_mode": "card_confirm",
            "trigger": {"groups": []},
            "retargeting": {
                "method": "volume",
                "volume": {
                    "total_budget_yuan": 100,
                    "duration_hours": 1,
                },
            },
        }
        target = {
            "target_uid": "target-product",
            "aadvid": "10001",
            "ad_id": "30001",
            "plan_name": "商品全域计划",
            "promotion_scene": "product",
            "plan_system": "global",
            "last_status": "ok",
            "enabled": 1,
            "monitor_eligible": 1,
            "retarget_eligible": 1,
            "capability_json": _retarget_capability_json(
                target_uid="target-product",
                aavid="10001",
                ad_id="30001",
                scene="product",
                plan_system="global",
            ),
        }
        rows = [
            {
                "targetUid": "target-product",
                "aadvid": "10001",
                "id": "m1",
                "videoName": "素材1",
            },
            {
                "targetUid": "target-product",
                "aadvid": "10001",
                "id": "m2",
                "videoName": "素材2",
            },
        ]

        class FakeDashboard:
            def get_table_data(self, **_kwargs):
                return {
                    "success": True,
                    "data": rows,
                    "total": len(rows),
                    "period": "近1小时",
                }

            def get_dashboard_account_label(self):
                return {"label": "测试账户"}

        class FakeStore:
            def select(self, table, **_kwargs):
                if table == "promotion_target":
                    return [target]
                return []

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
            "services.retargeting_rule_runner.row_is_in_test_scope",
            side_effect=lambda row: str(row.get("id") or "") == "m1",
        ), patch(
            "services.retargeting_rule_runner.evaluate_trigger",
            return_value=True,
        ), patch(
            "services.retargeting_rule_runner.build_trigger_evaluation_snapshot",
            return_value={"passed": True},
        ), patch(
            "services.retargeting_rule_runner.rate_limit_should_skip",
            return_value=False,
        ), patch(
            "services.retargeting_rule_runner.create_retarget_task",
            return_value={
                "success": True,
                "duplicate": False,
                "data": {"task_uid": "task-batch"},
            },
        ) as create_task:
            asyncio.run(
                retargeting_rule_runner.run_one_cycle(FakeStore())
            )

        self.assertEqual(1, create_task.call_count)
        payload = create_task.call_args.args[0]
        self.assertEqual(
            ["m1", "m2"],
            [item["material_id"] for item in payload["materials"]],
        )
        self.assertEqual(300, payload["evaluation_interval_seconds"])
        self.assertEqual(
            {"window_seconds": 86400, "max_count": 1, "scope": "global"},
            payload["effective_rate_limit"],
        )

    def test_auto_merged_mode_submits_one_task_up_to_candidate_limit(self):
        strategy = {
            "id": "auto-merged",
            "title": "自动合并追投",
            "target_uid": "target-product",
            "trigger_level": "material",
            "action_mode": "auto_execute",
            "material_grouping_mode": "merged",
            "candidate_limit": 2,
            "trigger": {"groups": []},
            "retargeting": {
                "method": "volume",
                "volume": {"total_budget_yuan": 100, "duration_hours": 1},
            },
        }
        config = {
            "enabled": True,
            "trigger_query_period": "1h",
            "per_strategy_rate_limit": False,
            "interval": {"window_seconds": 3600, "max_count": 1},
            "strategies": [strategy],
        }
        target = {
            "target_uid": "target-product",
            "aadvid": "10001",
            "ad_id": "30001",
            "plan_name": "商品全域计划",
            "promotion_scene": "product",
            "plan_system": "global",
            # 模拟官方 API 采集线程正好开始下一轮；上一轮完整快照仍新鲜。
            "last_status": "collecting",
            "last_sync_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "enabled": 1,
            "monitor_eligible": 1,
            "retarget_eligible": 1,
            "automation_write_blocked": 0,
            "capability_json": _retarget_capability_json(
                target_uid="target-product",
                aavid="10001",
                ad_id="30001",
                scene="product",
                plan_system="global",
                batch=True,
            ),
        }
        rows = [
            {"targetUid": "target-product", "aadvid": "10001", "id": "m1", "netRoi": 3, "currentCost": 10},
            {"targetUid": "target-product", "aadvid": "10001", "id": "m2", "netRoi": 2, "currentCost": 10},
            {"targetUid": "target-product", "aadvid": "10001", "id": "m3", "netRoi": 1, "currentCost": 10},
        ]

        class FakeDashboard:
            def get_table_data(self, **_kwargs):
                return {"success": True, "data": rows, "total": len(rows), "period": "近1小时"}

            def get_dashboard_account_label(self):
                return {"label": "测试账户"}

        class FakeStore:
            def select(self, table, **_kwargs):
                return [target] if table == "promotion_target" else []

            def select_one(self, *_args, **_kwargs):
                return None

        class FakeService:
            def __init__(self):
                self.calls = []

            async def run(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    success=True,
                    aavid="10001",
                    ad_id="30001",
                    material_id=kwargs["material_id"],
                    regulate_task_id="task-merged",
                    step="done_pending_verification",
                    message="成功",
                    detail="",
                    headless=True,
                )

            async def close(self):
                return None

        @asynccontextmanager
        async def fake_operation(*_args, **_kwargs):
            yield

        service = FakeService()
        with patch(
            "services.retargeting_rule_runner.load_rule_retargeting_config",
            return_value=config,
        ), patch(
            "services.retargeting_rule_runner.DashboardApi",
            return_value=FakeDashboard(),
        ), patch(
            "services.retargeting_rule_runner.row_is_in_test_scope",
            return_value=True,
        ), patch(
            "services.retargeting_rule_runner.evaluate_trigger",
            return_value=True,
        ), patch(
            "services.retargeting_rule_runner.check_target_capability",
            return_value=(True, "ok"),
        ), patch(
            "services.retargeting_rule_runner.retarget_capability_matches",
            return_value=True,
        ), patch(
            "services.retargeting_rule_runner.rate_limit_should_skip",
            return_value=False,
        ), patch(
            "services.retargeting_rule_runner.auto_execute_allowed_in_current_environment",
            return_value=True,
        ), patch(
            "services.retargeting_rule_runner.exclusive_browser_operation",
            side_effect=fake_operation,
        ), patch(
            "services.retargeting_rule_runner._revalidate_auto_retarget_under_lock",
            return_value=(config, strategy, target),
        ), patch(
            "services.retargeting_rule_runner.QianChuanRetargetingService.from_rule_file_dict",
            return_value=service,
        ), patch(
            "services.retargeting_rule_runner.record_retarget_rate_success_safely",
        ) as record_rate, patch(
            "services.retargeting_rule_runner._insert_run",
        ) as insert_run:
            asyncio.run(retargeting_rule_runner.run_one_cycle(FakeStore()))

        self.assertEqual(1, len(service.calls))
        self.assertEqual(["m1", "m2"], service.calls[0]["material_ids"])
        record_rate.assert_called_once()
        self.assertEqual(["m1", "m2"], record_rate.call_args.kwargs["material_ids"])
        insert_run.assert_called_once()
        self.assertEqual(2, len(insert_run.call_args.kwargs["materials"]))


class OperationEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        init_sqlite_schema(database=self.db_path)
        self.db = SQLiteStore(database=self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_operation_account_options_include_only_enabled_monitored_account(self):
        from services.qianchuan_accounts import ensure_qianchuan_account

        account = ensure_qianchuan_account(
            "1001",
            account_name="松之选专卖店",
            enabled=True,
            db=self.db,
        )
        self.db.insert(
            "promotion_target",
            {
                "target_uid": "target-operation-list",
                "account_uid": account["account_uid"],
                "aadvid": "1001",
                "ad_id": "2001",
                "promotion_scene": "product",
                "plan_system": "global",
                "platform_status": "active",
                "verification_state": "verified",
                "monitor_eligible": 1,
                "enabled": 1,
            },
        )
        ensure_qianchuan_account(
            "1002",
            account_name="未选择监控的账户",
            enabled=True,
            db=self.db,
        )
        items = list_operation_accounts(db=self.db)
        self.assertEqual(1, len(items))
        self.assertEqual("松之选专卖店", items[0]["account_name"])
        self.assertEqual("1001", items[0]["aavid"])

    def test_event_uid_is_idempotent_and_accounts_are_separate(self):
        event = {
            "event_uid": "task:1",
            "aavid": "1001",
            "source": "tool_direct",
            "action_type": "retarget",
            "status": "requested",
            "summary": "第一次",
            "occurred_at": "2026-07-22 10:00:00",
        }
        upsert_operation_event(event, self.db)
        event.update({"status": "success", "summary": "已完成"})
        upsert_operation_event(event, self.db)
        upsert_operation_event({**event, "event_uid": "task:2", "aavid": "1002"}, self.db)
        rows = self.db.select("account_operation_event", where={"aavid": "1001"})
        self.assertEqual(1, len(rows))
        self.assertEqual("success", rows[0]["status"])
        self.assertEqual("已完成", rows[0]["summary"])

    def test_action_normalization(self):
        self.assertEqual("retarget", normalize_action_type("创建素材追投任务"))
        self.assertEqual(
            "stop",
            normalize_action_type(
                "操作内容：素材追投，调控状态：调控中 -> 调控手动关闭"
            ),
        )
        self.assertEqual("plan_create", normalize_action_type("新建计划"))

    def test_batch_retarget_writes_one_operation_with_all_materials(self):
        materials = [
            {"material_id": "m1", "material_name": "素材1"},
            {"material_id": "m2", "material_name": "素材2"},
        ]
        retargeting_rule_runner._insert_run(
            self.db,
            aavid="1001",
            ad_id="plan1",
            target_uid="target1",
            promotion_scene="product",
            plan_system="global",
            trigger_level="material",
            material_id="m1",
            material_name="素材1",
            strategy_name="批量策略",
            regulate_task_id="regulate1",
            started_at="2026-07-28 10:00:00",
            ended_at="2026-07-28 10:00:01",
            duration_ms=1000,
            status=1,
            step="done",
            message="追投成功（2条素材）",
            detail="",
            retargeting={"method": "volume"},
            rule_full_json="{}",
            trigger_snapshot_json="{}",
            query_snapshot_json="{}",
            headless=True,
            browser_headless_rule=True,
            cloud_task_id="cloud-batch",
            materials=materials,
        )
        runs = self.db.select("pmc_retargeting_run")
        events = self.db.select("account_operation_event")
        self.assertEqual(1, len(runs))
        self.assertEqual(1, len(events))
        self.assertEqual(materials, json.loads(runs[0]["materials_json"]))
        self.assertEqual("assist_task", events[0]["object_type"])
        self.assertEqual(
            materials,
            json.loads(events[0]["raw_json"])["materials"],
        )
        self.assertEqual("budget_update", normalize_action_type("调整日预算"))
        self.assertEqual("other", normalize_action_type("查看报表"))

    def test_platform_timestamp_accepts_milliseconds(self):
        self.assertEqual("2023-11-15 06:13:20", _normalize_occurred_at(1700000000000))

    def test_filters_csv_and_detail_keep_account_isolation(self):
        base = {
            "source": "tool_direct",
            "action_type": "retarget",
            "status": "success",
            "operator_name": "审批人甲",
            "object_name": "素材A",
            "occurred_at": "2026-07-22 10:00:00",
        }
        upsert_operation_event({**base, "event_uid": "one", "aavid": "1001"}, self.db)
        upsert_operation_event({**base, "event_uid": "two", "aavid": "1002"}, self.db)
        with patch("api.operation_events.init_sqlite_schema"), patch(
            "api.operation_events.SQLiteStore", return_value=self.db
        ), patch("api.operation_events.migrate_legacy_operation_runs", return_value=0):
            total, rows = query_operation_events_page(
                aavid="1001", source="tool_direct", action_type="retarget", status="success", operator="审批人", q="素材A"
            )
            csv_text = export_operation_events_csv(aavid="1001")
            own = get_operation_event(rows[0]["id"], "1001")
            foreign = get_operation_event(rows[0]["id"], "1002")
        self.assertEqual(1, total)
        self.assertEqual("1001", rows[0]["aavid"])
        self.assertTrue(csv_text.startswith("\ufeff"))
        self.assertEqual(2, len(csv_text.splitlines()))
        self.assertIsNotNone(own)
        self.assertIsNone(foreign)

    def test_date_filter_possible_duplicate_and_180_day_cleanup(self):
        base = {
            "aavid": "1001",
            "source": "tool_direct",
            "action_type": "budget_update",
            "status": "success",
            "object_id": "plan-1",
            "occurred_at": "2026-07-22 10:00:00",
        }
        upsert_operation_event({**base, "event_uid": "direct-1"}, self.db)
        upsert_operation_event({**base, "event_uid": "direct-2"}, self.db)
        with patch("api.operation_events.init_sqlite_schema"), patch(
            "api.operation_events.SQLiteStore", return_value=self.db
        ), patch("api.operation_events.migrate_legacy_operation_runs", return_value=0):
            ingest_platform_log_rows(
                "1001",
                [
                    {
                        "id": "platform-1",
                        "operation_name": "修改预算",
                        "object_id": "plan-1",
                        "operation_time": "2026-07-22 10:00:30",
                    }
                ],
            )
            total, rows = query_operation_events_page(
                aavid="1001", date_from="2026-07-22", date_to="2026-07-22"
            )
        self.assertEqual(3, total)
        platform = next(row for row in rows if row["source"] == "platform_log")
        self.assertEqual(1, platform["possible_duplicate"])
        direct_rows = [row for row in rows if row["source"] == "tool_direct"]
        self.assertTrue(all(row["possible_duplicate"] == 1 for row in direct_rows))

        old_time = (datetime.now() - timedelta(days=181)).strftime("%Y-%m-%d %H:%M:%S")
        upsert_operation_event(
            {
                **base,
                "event_uid": "too-old",
                "object_id": "plan-old",
                "occurred_at": old_time,
            },
            self.db,
        )
        with patch("api.operation_events.init_sqlite_schema"), patch(
            "api.operation_events.SQLiteStore", return_value=self.db
        ):
            deleted = prune_operation_events(180)
        self.assertEqual(1, deleted)
        self.assertIsNone(self.db.select_one("account_operation_event", where={"event_uid": "too-old"}))

    def test_clear_platform_match_merges_into_one_effective_operation(self):
        upsert_operation_event(
            {
                "event_uid": "direct-clear",
                "aavid": "1001",
                "source": "tool_direct",
                "action_type": "retarget",
                "object_type": "material",
                "object_id": "material-1",
                "object_name": "素材一",
                "material_id": "material-1",
                "material_name": "素材一",
                "regulate_task_id": "task-1",
                "status": "success",
                "occurred_at": "2026-07-22 10:00:00",
            },
            self.db,
        )
        with patch("api.operation_events.init_sqlite_schema"), patch(
            "api.operation_events.SQLiteStore", return_value=self.db
        ), patch("api.operation_events.migrate_legacy_operation_runs", return_value=0):
            ingest_platform_log_rows(
                "1001",
                [
                    {
                        "id": "platform-clear",
                        "operation_name": "创建素材追投任务",
                        "material_id": "material-1",
                        "operation_time": "2026-07-22 10:00:20",
                        "operator_name": "审批人",
                    }
                ],
            )
            total, rows = query_operation_events_page(aavid="1001")
            csv_text = export_operation_events_csv(aavid="1001")
        self.assertEqual(1, total)
        self.assertEqual("direct-clear", rows[0]["event_uid"])
        self.assertEqual("platform-clear", rows[0]["platform_event_id"])
        self.assertEqual("material-1", rows[0]["material_id"])
        self.assertEqual("task-1", rows[0]["regulate_task_id"])
        self.assertIn("调控任务ID", csv_text)


    def test_completed_query_window_expands_and_never_shrinks_coverage(self):
        update_platform_sync_state(
            "1001",
            db=self.db,
            coverage_from="2026-07-01 00:00:00",
            coverage_to="2026-07-30 23:59:59",
            last_status="ok",
        )
        update_platform_sync_state(
            "1001",
            db=self.db,
            coverage_from="2026-07-10 00:00:00",
            coverage_to="2026-07-20 23:59:59",
            last_status="ok",
        )
        row = self.db.select_one(
            "platform_log_sync_state",
            where={"aavid": "1001"},
        )
        self.assertEqual("2026-07-01 00:00:00", row["coverage_from"])
        self.assertEqual("2026-07-30 23:59:59", row["coverage_to"])


class PlatformLogMonitorTests(unittest.TestCase):
    def test_extracts_log_rows_and_classifies_plan_writes(self):
        rows = [
            {"operation_name": "修改预算", "operation_time": "2026-07-22 10:00:00", "operator_name": "张三"}
        ]
        self.assertEqual(rows, _extract_platform_rows({"data": {"list": rows}}))
        self.assertEqual("plan_copy", _classify_write("https://x.test/campaign/copy", {}))
        self.assertEqual("plan_disable", _classify_write("https://x.test/update", {"opt_status": "disable"}))

    def test_30_day_replay_only_changes_existing_date_fields(self):
        original = {"page": 1, "start_time": 1700000000000, "end_time": 1700100000000, "nested": {"name": "x"}}
        changed = _with_30_day_body(original)
        self.assertEqual(1, changed["page"])
        self.assertEqual("x", changed["nested"]["name"])
        self.assertIsInstance(changed["start_time"], int)
        self.assertLess(changed["start_time"], changed["end_time"])
        body, headers = _prepare_replay_body('{"start_date":"2026-07-01","end_date":"2026-07-02"}')
        self.assertIsInstance(body, dict)
        self.assertIn("application/json", headers["Content-Type"])

    def test_log_backfill_paginates_only_existing_fields(self):
        url, supported = _paginate_url(
            "https://example.test/log/list?page=1&page_size=20&account=1001", 3
        )
        self.assertTrue(supported)
        self.assertIn("page=3", url)
        self.assertIn("page_size=100", url)
        unchanged, unsupported = _paginate_url(
            "https://example.test/log/list?account=1001", 3
        )
        self.assertFalse(unsupported)
        self.assertEqual("https://example.test/log/list?account=1001", unchanged)
        body, body_supported = _paginate_body(
            {"filters": {"date": "2026-07-01"}, "page_no": 1, "limit": 20},
            "application/json",
            4,
        )
        self.assertTrue(body_supported)
        self.assertEqual(4, body["page_no"])
        self.assertEqual(100, body["limit"])
        self.assertFalse(_explicit_has_more({"data": {"has_more": False}}))

    def test_complete_window_requires_explicit_date_fields(self):
        self.assertTrue(
            _replay_has_explicit_30_day_range(
                "https://example.test/log?start_date=old&end_date=old",
                "",
            )
        )
        self.assertTrue(
            _replay_has_explicit_30_day_range(
                "https://example.test/log",
                '{"filters":{"start_time":1,"end_time":2}}',
            )
        )
        self.assertFalse(
            _replay_has_explicit_30_day_range(
                "https://example.test/log?page=1",
                '{"filters":{"operator":"owner"}}',
            )
        )
        self.assertFalse(
            _replay_has_explicit_30_day_range(
                "https://example.test/log?start_date=old",
                "",
            )
        )
        rewritten = _with_30_day_range(
            "https://example.test/log?start_time=1700000000000"
            "&end_time=1700100000000"
        )
        self.assertNotIn("1700000000000", rewritten)
        self.assertNotIn("1700100000000", rewritten)
        coverage_from, coverage_to = _thirty_day_coverage_window()
        self.assertTrue(coverage_from.endswith("00:00:00"))
        self.assertTrue(coverage_to.endswith("23:59:59"))


class CloudClientTests(unittest.TestCase):
    def test_result_report_includes_claim_token(self):
        with patch.dict(os.environ, {"QCSCKP_RETARGET_TASK_BACKEND": "cloud_http"}), patch(
            "services.cloud_retarget_client._token", return_value="device-token"
        ), patch(
            "services.cloud_retarget_client._request", return_value={"success": True}
        ) as request:
            response = report_retarget_task("task-1", "a" * 64, "executing", message="执行中")
        self.assertTrue(response["success"])
        payload = request.call_args.kwargs["payload"]
        self.assertEqual("a" * 64, payload["claim_token"])
        self.assertEqual("executing", payload["status"])


class RetargetWorkerValidationTests(unittest.TestCase):
    def setUp(self):
        self.strategy = {
            "id": "s1",
            "title": "策略1",
            "action_mode": "card_confirm",
            "trigger": {"groups": []},
            "retargeting": {
                "method": "volume",
                "volume": {"total_budget_yuan": 100, "duration_hours": 0.5},
            },
        }
        snapshot = retarget_task_worker._strategy_snapshot(self.strategy)
        self.task = {
            "strategy_id": "s1",
            "strategy_hash": retarget_task_worker._snapshot_hash(snapshot),
            "rule_snapshot": snapshot,
            "aavid": "10001",
            "ad_id": "30001",
            "target_uid": "target-live",
            "promotion_scene": "live",
            "plan_system": "global",
            "material_id": "20001",
            "retargeting": copy.deepcopy(snapshot["retargeting"]),
        }

    def validate(
        self,
        *,
        current_strategy=None,
        current_ad_id="30001",
        trigger_passed=True,
        rate_limited=False,
        last_status="ok",
    ):
        strategy = current_strategy or copy.deepcopy(self.strategy)
        cfg = {
            "enabled": True,
            "trigger_query_period": "1h",
            "per_strategy_rate_limit": False,
            "strategies": [strategy],
        }
        class FakeStore:
            def select_one(self, table, **_kwargs):
                if table == "promotion_target":
                    return {
                        "target_uid": "target-live",
                        "aadvid": "10001",
                        "ad_id": current_ad_id,
                        "promotion_scene": "live",
                        "plan_system": "global",
                        "enabled": 1,
                        "last_status": last_status,
                        "capability_json": '{"retarget_execute":true}',
                    }
                return None

        with patch(
            "services.retarget_task_worker.load_rule_retargeting_config",
            return_value=cfg,
        ), patch(
            "services.retarget_task_worker.resolve_ad_id_for_aavid",
            return_value=current_ad_id,
        ), patch(
            "services.retarget_task_worker.os.path.isfile",
            return_value=True,
        ), patch(
            "services.retarget_task_worker._latest_target_rows",
            return_value=[{"aadvid": "10001", "id": "20001"}],
        ), patch(
            "services.retarget_task_worker.evaluate_trigger",
            return_value=trigger_passed,
        ), patch(
            "services.retarget_task_worker._interval_from_root_cfg",
            return_value=(3600, 1),
        ), patch(
            "services.retarget_task_worker.rate_limit_should_skip",
            return_value=rate_limited,
        ), patch(
            "services.retarget_task_worker.assert_test_task_scope",
        ):
            return retarget_task_worker._validate_task(self.task, FakeStore())

    def test_strategy_change_blocks_execution(self):
        changed = copy.deepcopy(self.strategy)
        changed["retargeting"]["volume"]["total_budget_yuan"] = 200
        with self.assertRaisesRegex(RuntimeError, "策略参数已经变更"):
            self.validate(current_strategy=changed)

    def test_account_mismatch_blocks_execution(self):
        with self.assertRaisesRegex(RuntimeError, "账户.*广告ID"):
            self.validate(current_ad_id="99999")

    def test_material_no_longer_matches_blocks_execution(self):
        with self.assertRaisesRegex(RuntimeError, "不满足追投规则"):
            self.validate(trigger_passed=False)

    def test_rate_limit_blocks_execution(self):
        with self.assertRaisesRegex(RuntimeError, "次数上限"):
            self.validate(rate_limited=True)

    def test_collecting_state_keeps_last_verified_snapshot_usable(self):
        _cfg, _strategy, rows = self.validate(last_status="collecting")
        self.assertEqual("20001", str(rows[0]["id"]))

    def test_product_batch_revalidates_every_material_in_one_task(self):
        task = {
            **self.task,
            "target_uid": "target-product",
            "promotion_scene": "product",
            "plan_system": "global",
            "materials": [
                {"material_id": "20001", "material_name": "素材1"},
                {"material_id": "20002", "material_name": "素材2"},
            ],
        }
        cfg = {
            "enabled": True,
            "trigger_query_period": "1h",
            "per_strategy_rate_limit": False,
            "strategies": [copy.deepcopy(self.strategy)],
        }

        class FakeStore:
            def select_one(self, table, **_kwargs):
                if table == "promotion_target":
                    return {
                        "target_uid": "target-product",
                        "aadvid": "10001",
                        "ad_id": "30001",
                        "promotion_scene": "product",
                        "plan_system": "global",
                        "enabled": 1,
                        "last_status": "ok",
                        "capability_json": _retarget_capability_json(
                            target_uid="target-product",
                            aavid="10001",
                            ad_id="30001",
                            scene="product",
                            plan_system="global",
                            batch=True,
                        ),
                    }
                return None

        with patch(
            "services.retarget_task_worker.load_rule_retargeting_config",
            return_value=cfg,
        ), patch(
            "services.retarget_task_worker.resolve_ad_id_for_aavid",
            return_value="30001",
        ), patch(
            "services.retarget_task_worker.os.path.isfile",
            return_value=True,
        ), patch(
            "services.retarget_task_worker._latest_target_rows",
            return_value=[
                {"aadvid": "10001", "id": "20001"},
                {"aadvid": "10001", "id": "20002"},
            ],
        ), patch(
            "services.retarget_task_worker.evaluate_trigger",
            return_value=True,
        ) as evaluate, patch(
            "services.retarget_task_worker._interval_from_root_cfg",
            return_value=(3600, 1),
        ), patch(
            "services.retarget_task_worker.rate_limit_should_skip",
            return_value=False,
        ) as rate_limit, patch(
            "services.retarget_task_worker.assert_test_task_scope",
        ):
            _cfg, _strategy, rows = retarget_task_worker._validate_task(
                task,
                FakeStore(),
            )

        self.assertEqual(["20001", "20002"], [row["id"] for row in rows])
        self.assertEqual(2, evaluate.call_count)
        self.assertEqual(2, rate_limit.call_count)

    def test_task_materials_deduplicate_and_preserve_order(self):
        materials = retarget_task_worker._task_materials(
            {
                "materials": [
                    {"material_id": "m2", "material_name": "第二条"},
                    {"material_id": "m1", "material_name": "第一条"},
                    {"material_id": "m2", "material_name": "重复"},
                ]
            }
        )
        self.assertEqual(["m2", "m1"], [item["material_id"] for item in materials])

    def test_batch_execution_submits_once_and_records_each_material_limit(self):
        task = {
            **self.task,
            "task_uid": "task-batch",
            "target_uid": "target-product",
            "promotion_scene": "product",
            "plan_system": "global",
            "materials": [
                {"material_id": "m1", "material_name": "素材1"},
                {"material_id": "m2", "material_name": "素材2"},
            ],
            "trigger_snapshot": {},
            "query_snapshot": {},
        }
        cfg = {
            "enabled": True,
            "browser_headless": True,
            "per_strategy_rate_limit": False,
        }

        class FakeResult:
            success = True
            regulate_task_id = "regulate-batch"

            def asdict(self):
                return {
                    "success": True,
                    "message": "追投成功",
                    "detail": "",
                    "step": "done",
                    "headless": True,
                }

        class FakeService:
            def __init__(self):
                self.calls = []

            async def run(self, **kwargs):
                self.calls.append(kwargs)
                return FakeResult()

            async def close(self):
                return None

        class FakeStore:
            def select_one(self, table, **_kwargs):
                if table == "promotion_target":
                    return {"sanitized_page_url": "https://example.test/product"}
                return None

        service = FakeService()
        with patch(
            "services.retarget_task_worker._validate_task",
            return_value=(cfg, copy.deepcopy(self.strategy), [{"id": "m1"}, {"id": "m2"}]),
        ), patch(
            "services.retarget_task_worker.consume_live_retarget_batch_once",
        ) as consume, patch(
            "services.retarget_task_worker.QianChuanRetargetingService.from_rule_file_dict",
            return_value=service,
        ), patch(
            "services.retarget_task_worker._insert_run",
        ) as insert_run, patch(
            "services.retarget_task_worker._interval_from_root_cfg",
            return_value=(3600, 5),
        ), patch(
            "services.retarget_task_worker.rate_limit_record_success",
        ) as record_limit:
            result = asyncio.run(
                retarget_task_worker._execute_task(task, FakeStore())
            )

        self.assertTrue(result["success"])
        self.assertEqual("追投成功（2条素材）", result["message"])
        consume.assert_called_once_with(
            "task-batch",
            "10001",
            ["m1", "m2"],
            ["m1", "m2"],
        )
        self.assertEqual(1, len(service.calls))
        self.assertEqual(["m1", "m2"], service.calls[0]["material_ids"])
        self.assertEqual(1, insert_run.call_count)
        self.assertEqual(
            ["m1", "m2"],
            [call.args[1] for call in record_limit.call_args_list],
        )

    def test_retryable_explicit_failure_retries_at_most_three_total_attempts(self):
        task = {
            **self.task,
            "task_uid": "task-retryable",
            "claim_token": "claim-retryable",
            "target_uid": "target-product",
            "promotion_scene": "product",
            "plan_system": "global",
            "materials": [{"material_id": "m1", "material_name": "素材1"}],
            "trigger_snapshot": {},
            "query_snapshot": {},
        }
        cfg = {
            "enabled": True,
            "browser_headless": True,
            "per_strategy_rate_limit": False,
        }

        class FakeResult:
            def __init__(self, success, attempt):
                self.success = success
                self.regulate_task_id = "regulate-ok" if success else ""
                self.retryable = not success
                self.retry_after_seconds = 0
                self.message = "追投成功" if success else "官方明确拒绝且允许重试"
                self.step = "done" if success else "official_api"

            def asdict(self):
                return {
                    "success": self.success,
                    "message": self.message,
                    "detail": "",
                    "step": self.step,
                    "headless": True,
                    "retryable": self.retryable,
                    "retry_after_seconds": self.retry_after_seconds,
                }

        class FakeService:
            def __init__(self):
                self.calls = []

            async def run(self, **kwargs):
                self.calls.append(kwargs)
                return FakeResult(len(self.calls) == 3, len(self.calls))

            async def close(self):
                return None

        class FakeStore:
            def select_one(self, table, **_kwargs):
                if table == "promotion_target":
                    return {"sanitized_page_url": "https://example.test/product"}
                return None

        service = FakeService()
        sleeper = AsyncMock()
        with patch(
            "services.retarget_task_worker._validate_task",
            return_value=(cfg, copy.deepcopy(self.strategy), [{"id": "m1"}]),
        ), patch(
            "services.retarget_task_worker.consume_live_retarget_batch_once",
        ), patch(
            "services.retarget_task_worker.QianChuanRetargetingService.from_rule_file_dict",
            return_value=service,
        ), patch(
            "services.retarget_task_worker._insert_run",
        ), patch(
            "services.retarget_task_worker._interval_from_root_cfg",
            return_value=(3600, 5),
        ), patch(
            "services.retarget_task_worker.rate_limit_record_success",
        ), patch(
            "services.retarget_task_worker.report_retarget_task",
        ) as report, patch(
            "services.retarget_task_worker.asyncio.sleep",
            new=sleeper,
        ):
            result = asyncio.run(
                retarget_task_worker._execute_task(task, FakeStore())
            )

        self.assertTrue(result["success"])
        self.assertEqual(3, result["attempt_count"])
        self.assertEqual(3, len(service.calls))
        self.assertEqual(
            [
                "task-retryable:attempt:1",
                "task-retryable:attempt:2",
                "task-retryable:attempt:3",
            ],
            [call["execution_uid"] for call in service.calls],
        )
        self.assertEqual([60, 120], [call.args[0] for call in sleeper.await_args_list])
        self.assertEqual(2, report.call_count)

    def test_nonretryable_failure_is_submitted_only_once(self):
        task = {
            **self.task,
            "task_uid": "task-nonretryable",
            "target_uid": "target-product",
            "promotion_scene": "product",
            "plan_system": "global",
            "materials": [{"material_id": "m1", "material_name": "素材1"}],
            "trigger_snapshot": {},
            "query_snapshot": {},
        }
        cfg = {"enabled": True, "browser_headless": True, "per_strategy_rate_limit": False}

        class FakeResult:
            success = False
            regulate_task_id = ""
            retryable = False
            retry_after_seconds = 0
            message = "参数错误"
            step = "official_api"

            def asdict(self):
                return {
                    "success": False,
                    "message": self.message,
                    "detail": "",
                    "step": self.step,
                    "headless": True,
                    "retryable": False,
                    "retry_after_seconds": 0,
                }

        class FakeService:
            def __init__(self):
                self.calls = []

            async def run(self, **kwargs):
                self.calls.append(kwargs)
                return FakeResult()

            async def close(self):
                return None

        class FakeStore:
            def select_one(self, table, **_kwargs):
                return {"sanitized_page_url": ""} if table == "promotion_target" else None

        service = FakeService()
        sleeper = AsyncMock()
        with patch(
            "services.retarget_task_worker._validate_task",
            return_value=(cfg, copy.deepcopy(self.strategy), [{"id": "m1"}]),
        ), patch(
            "services.retarget_task_worker.consume_live_retarget_batch_once",
        ), patch(
            "services.retarget_task_worker.QianChuanRetargetingService.from_rule_file_dict",
            return_value=service,
        ), patch(
            "services.retarget_task_worker._insert_run",
        ), patch(
            "services.retarget_task_worker.asyncio.sleep",
            new=sleeper,
        ):
            result = asyncio.run(retarget_task_worker._execute_task(task, FakeStore()))

        self.assertFalse(result["success"])
        self.assertEqual(1, result["attempt_count"])
        self.assertEqual(1, len(service.calls))
        sleeper.assert_not_awaited()

    def test_three_overlapping_groups_submit_three_product_retargets(self):
        material_map = {
            f"m{index}": {"material_id": f"m{index}", "material_name": f"素材{index}"}
            for index in range(1, 11)
        }
        groups = [
            {"group_uid": "g1", "materials": [material_map[f"m{i}"] for i in (1, 2, 3)]},
            {"group_uid": "g2", "materials": [material_map[f"m{i}"] for i in (1, 4, 5, 6, 7)]},
            {"group_uid": "g3", "materials": list(material_map.values())},
        ]
        task = {
            **self.task,
            "task_uid": "task-three-groups",
            "target_uid": "target-product",
            "promotion_scene": "product",
            "plan_system": "global",
            "materials": list(material_map.values()),
            "retarget_groups": groups,
            "trigger_snapshot": {},
            "query_snapshot": {},
        }
        cfg = {
            "enabled": True,
            "browser_headless": True,
            "per_strategy_rate_limit": False,
        }

        class FakeResult:
            def __init__(self, regulate_task_id):
                self.success = True
                self.regulate_task_id = regulate_task_id

            def asdict(self):
                return {
                    "success": True,
                    "message": "追投成功",
                    "detail": "",
                    "step": "done",
                    "headless": True,
                }

        class FakeService:
            def __init__(self):
                self.calls = []

            async def run(self, **kwargs):
                self.calls.append(kwargs)
                return FakeResult(f"regulate-{len(self.calls)}")

            async def close(self):
                return None

        class FakeStore:
            def select_one(self, table, **_kwargs):
                if table == "promotion_target":
                    return {"sanitized_page_url": "https://example.test/product"}
                return None

        service = FakeService()
        with patch(
            "services.retarget_task_worker._validate_task",
            return_value=(cfg, copy.deepcopy(self.strategy), [{"id": "m1"}]),
        ) as validate, patch(
            "services.retarget_task_worker.rate_limit_remaining_capacity",
            return_value=10,
        ), patch(
            "services.retarget_task_worker.consume_live_retarget_batch_once",
        ) as consume, patch(
            "services.retarget_task_worker.QianChuanRetargetingService.from_rule_file_dict",
            return_value=service,
        ), patch(
            "services.retarget_task_worker._insert_run",
        ) as insert_run, patch(
            "services.retarget_task_worker._interval_from_root_cfg",
            return_value=(3600, 10),
        ), patch(
            "services.retarget_task_worker.rate_limit_record_success",
        ):
            result = asyncio.run(
                retarget_task_worker._execute_task(task, FakeStore())
            )

        self.assertTrue(result["success"])
        self.assertEqual(3, result["group_count"])
        self.assertEqual(18, result["material_count"])
        self.assertEqual(10, result["unique_material_count"])
        self.assertEqual(
            [
                ["m1", "m2", "m3"],
                ["m1", "m4", "m5", "m6", "m7"],
                [f"m{i}" for i in range(1, 11)],
            ],
            [call["material_ids"] for call in service.calls],
        )
        self.assertEqual(6, validate.call_count)
        self.assertEqual(3, insert_run.call_count)
        consume.assert_called_once_with(
            "task-three-groups",
            "10001",
            [f"m{i}" for i in range(1, 11)],
            [f"m{i}" for i in range(1, 11)],
        )
        self.assertEqual(
            ["regulate-1", "regulate-2", "regulate-3"],
            result["regulate_task_ids"],
        )

    def test_overlapping_groups_respect_remaining_rate_limit_capacity(self):
        task = {
            **self.task,
            "task_uid": "task-rate-capacity",
            "target_uid": "target-product",
            "promotion_scene": "product",
            "plan_system": "global",
            "materials": [
                {"material_id": "m1", "material_name": "素材1"},
                {"material_id": "m2", "material_name": "素材2"},
            ],
            "retarget_groups": [
                {
                    "group_uid": "g1",
                    "materials": [{"material_id": "m1", "material_name": "素材1"}],
                },
                {
                    "group_uid": "g2",
                    "materials": [
                        {"material_id": "m1", "material_name": "素材1"},
                        {"material_id": "m2", "material_name": "素材2"},
                    ],
                },
            ],
        }
        cfg = {
            "enabled": True,
            "browser_headless": True,
            "per_strategy_rate_limit": False,
        }
        with patch(
            "services.retarget_task_worker._validate_task",
            return_value=(cfg, copy.deepcopy(self.strategy), [{"id": "m1"}]),
        ), patch(
            "services.retarget_task_worker._interval_from_root_cfg",
            return_value=(3600, 1),
        ), patch(
            "services.retarget_task_worker.rate_limit_remaining_capacity",
            return_value=1,
        ), patch(
            "services.retarget_task_worker.consume_live_retarget_batch_once",
        ) as consume:
            result = asyncio.run(
                retarget_task_worker._execute_task(task, object())
            )
        self.assertFalse(result["success"])
        self.assertEqual("group_revalidate", result["step"])
        self.assertIn("本批次被安排2次", result["message"])
        consume.assert_not_called()

    def test_live_groups_keep_single_and_multi_material_retarget_modes(self):
        groups = [
            {
                "group_uid": "single",
                "materials": [{"material_id": "m1", "material_name": "素材1"}],
            },
            {
                "group_uid": "multi",
                "materials": [
                    {"material_id": "m2", "material_name": "素材2"},
                    {"material_id": "m3", "material_name": "素材3"},
                ],
            },
        ]
        task = {
            **self.task,
            "task_uid": "task-live-mixed-groups",
            "target_uid": "target-live",
            "promotion_scene": "live",
            "plan_system": "global",
            "materials": [
                {"material_id": "m1", "material_name": "素材1"},
                {"material_id": "m2", "material_name": "素材2"},
                {"material_id": "m3", "material_name": "素材3"},
            ],
            "retarget_groups": groups,
            "trigger_snapshot": {},
            "query_snapshot": {},
        }
        cfg = {
            "enabled": True,
            "browser_headless": True,
            "per_strategy_rate_limit": False,
        }

        class FakeResult:
            def __init__(self, rid):
                self.success = True
                self.regulate_task_id = rid

            def asdict(self):
                return {
                    "success": True,
                    "message": "追投成功",
                    "detail": "",
                    "step": "done",
                    "headless": True,
                }

        class FakeService:
            def __init__(self):
                self.calls = []

            async def run(self, **kwargs):
                self.calls.append(kwargs)
                return FakeResult(f"live-{len(self.calls)}")

            async def close(self):
                return None

        class FakeStore:
            def select_one(self, table, **_kwargs):
                if table == "promotion_target":
                    return {"sanitized_page_url": "https://example.test/live"}
                return None

        service = FakeService()
        with patch(
            "services.retarget_task_worker._validate_task",
            return_value=(cfg, copy.deepcopy(self.strategy), [{"id": "m1"}]),
        ), patch(
            "services.retarget_task_worker.rate_limit_remaining_capacity",
            return_value=10,
        ), patch(
            "services.retarget_task_worker.consume_live_retarget_batch_once",
        ) as consume, patch(
            "services.retarget_task_worker.QianChuanRetargetingService.from_rule_file_dict",
            return_value=service,
        ), patch(
            "services.retarget_task_worker._insert_run",
        ), patch(
            "services.retarget_task_worker._interval_from_root_cfg",
            return_value=(3600, 10),
        ), patch(
            "services.retarget_task_worker.rate_limit_record_success",
        ):
            result = asyncio.run(
                retarget_task_worker._execute_task(task, FakeStore())
            )

        self.assertTrue(result["success"])
        self.assertEqual([["m1"], ["m2", "m3"]], [
            call["material_ids"] for call in service.calls
        ])
        self.assertTrue(all(call["promotion_scene"] == "live" for call in service.calls))
        consume.assert_called_once_with(
            "task-live-mixed-groups",
            "10001",
            ["m1", "m2", "m3"],
            ["m1", "m2", "m3"],
        )

    def validate_target_plan_system(self, plan_system):
        strategy = copy.deepcopy(self.strategy)
        strategy["target_uid"] = "target-1"
        snapshot = retarget_task_worker._strategy_snapshot(strategy)
        task = {
            **self.task,
            "target_uid": "target-1",
            "promotion_scene": "live",
            "plan_system": plan_system,
            "strategy_hash": retarget_task_worker._snapshot_hash(snapshot),
            "rule_snapshot": snapshot,
        }

        class FakeStore:
            def select_one(self, table, **_kwargs):
                if table == "promotion_target":
                    return {
                        "target_uid": "target-1",
                        "aadvid": "10001",
                        "ad_id": "30001",
                        "promotion_scene": "live",
                        "plan_system": plan_system,
                        "last_status": "ok",
                        "enabled": 1,
                    }
                return None

        with patch(
            "services.retarget_task_worker.load_rule_retargeting_config",
            return_value={
                "enabled": True,
                "trigger_query_period": "1h",
                "strategies": [strategy],
            },
        ), patch("services.retarget_task_worker.assert_test_task_scope"):
            return retarget_task_worker._validate_task(task, FakeStore())

    def test_unknown_plan_system_blocks_execution(self):
        with self.assertRaisesRegex(RuntimeError, "计划体系尚未确认"):
            self.validate_target_plan_system("unknown")

    def test_chengfang_plan_system_blocks_unverified_adapter(self):
        with self.assertRaisesRegex(RuntimeError, "追投能力证据无效"):
            self.validate_target_plan_system("chengfang")


class LocalTestGuardTests(unittest.TestCase):
    def test_scraper_cookie_uses_the_isolated_data_directory(self):
        self.assertEqual(
            os.path.join(local_test_guard.DATA_DIR, "qcookie.json"),
            ServiceConfig().cookie_path,
        )

    def test_last_crawl_target_is_saved_without_the_full_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_file = os.path.join(tmp, "last_crawl_target.json")
            self.assertTrue(_save_last_target("1001", "2002", target_file))
            self.assertEqual(
                {"aavid": "1001", "adId": "2002"},
                _load_last_target(target_file),
            )
            self.assertFalse(_save_last_target("not-an-account", "2002", target_file))

    def test_last_crawl_target_is_isolated_by_tool_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_file = os.path.join(tmp, "last_crawl_target.json")
            self.assertTrue(
                _save_last_target(
                    "1001",
                    "2001",
                    target_file,
                    owner_username="tool-a",
                )
            )
            self.assertTrue(
                _save_last_target(
                    "1002",
                    "2002",
                    target_file,
                    owner_username="tool-b",
                )
            )
            self.assertEqual(
                {"aavid": "1001", "adId": "2001"},
                _load_last_target(
                    target_file,
                    owner_username="tool-a",
                ),
            )
            self.assertEqual(
                {"aavid": "1002", "adId": "2002"},
                _load_last_target(
                    target_file,
                    owner_username="tool-b",
                ),
            )
            self.assertIsNone(
                _load_last_target(
                    target_file,
                    owner_username="tool-c",
                )
            )

    def test_target_reselection_disables_remembered_target_for_one_launch(self):
        with patch.dict(os.environ, {"QCSCKP_FORCE_TARGET_RESELECT": "1"}):
            self.assertFalse(_reuse_last_target_enabled())
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(_reuse_last_target_enabled())

    def test_target_reselection_excludes_only_the_previous_target(self):
        previous = {"aavid": "1001", "adId": "2002"}
        self.assertTrue(_target_is_excluded("1001", "2002", previous))
        self.assertFalse(_target_is_excluded("1001", "2003", previous))
        self.assertFalse(_target_is_excluded("1002", "2002", previous))
        self.assertFalse(_target_is_excluded("1001", "2002", None))

    def test_add_target_discovery_recognizes_existing_account_plan_pair(self):
        known = _known_promotion_target_keys(
            [
                {"aadvid": "1001", "ad_id": "2001"},
                {"aavid": "1001", "adId": "2002"},
            ]
        )
        self.assertIn(_promotion_target_key("1001", "2001"), known)
        self.assertIn(_promotion_target_key("1001", "2002"), known)
        self.assertNotIn(_promotion_target_key("1001", "2003"), known)

    def test_add_target_discovery_labels_login_urls(self):
        self.assertTrue(
            _is_qianchuan_login_url(
                "https://login.jinritemai.com/passport/web/login/"
            )
        )
        self.assertTrue(
            _is_qianchuan_login_url(
                "https://qianchuan.jinritemai.com/login"
            )
        )
        self.assertFalse(
            _is_qianchuan_login_url(
                "https://qianchuan.jinritemai.com/uni-prom"
            )
        )
        self.assertFalse(_is_qianchuan_login_url("about:blank"))

    def test_last_selected_target_takes_priority_on_restart(self):
        targets = [
            {"aadvid": "1001", "ad_id": "2001", "promotion_scene": "live"},
            {"aadvid": "1001", "ad_id": "2002", "promotion_scene": "product"},
        ]
        selected = _choose_startup_target(
            targets,
            {"aavid": "1001", "adId": "2002"},
        )
        self.assertEqual("2002", selected["ad_id"])
        self.assertEqual("product", selected["promotion_scene"])

    def test_product_restart_trusts_stored_target_not_page_default(self):
        discovered = _trusted_startup_discovery(
            {
                "aadvid": "1001",
                "ad_id": "2002",
                "promotion_scene": "product",
                "plan_name": "商品全域计划",
            },
            "https://qianchuan.jinritemai.com/uni-prom?aavid=1001&adId=2002",
        )
        self.assertEqual("1001", discovered["aavid"])
        self.assertEqual("2002", discovered["ad_id"])
        self.assertEqual("product", discovered["promotion_scene"])
        self.assertEqual("商品全域计划", discovered["plan_name"])

    def test_local_test_credentials_are_loaded_only_from_external_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_file = os.path.join(tmp, "secrets.local.json")
            with open(secret_file, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"test_account":{"username":"local_test","password":"random-local-password"},'
                    '"feishu_app":{"app_secret":"must-not-be-returned"}}'
                )
            with patch.multiple(
                local_test_guard,
                TEST_MODE=True,
                LOCAL_TEST_SECRETS_FILE=secret_file,
            ):
                result = local_test_guard.load_local_test_login_credentials()
        self.assertTrue(result["success"])
        self.assertEqual("local_test", result["username"])
        self.assertEqual("random-local-password", result["password"])
        self.assertNotIn("feishu_app", result)

        with patch.multiple(
            local_test_guard,
            TEST_MODE=False,
            LOCAL_TEST_SECRETS_FILE=secret_file,
        ):
            disabled = local_test_guard.load_local_test_login_credentials()
        self.assertFalse(disabled["success"])

    def test_scope_filters_to_one_account_and_material(self):
        with patch.multiple(
            local_test_guard,
            TEST_MODE=True,
            TEST_AAVID="1001",
            TEST_MATERIAL_ID="2001",
        ):
            local_test_guard.assert_test_scope("1001", "2001")
            self.assertTrue(
                local_test_guard.row_is_in_test_scope({"aadvid": "1001", "id": "2001"})
            )
            self.assertFalse(
                local_test_guard.row_is_in_test_scope({"aadvid": "1002", "id": "2001"})
            )
            with self.assertRaisesRegex(RuntimeError, "账户"):
                local_test_guard.assert_test_scope("1002", "2001")

    def test_card_scope_allows_any_selected_material_from_candidate_snapshot(self):
        with patch.multiple(
            local_test_guard,
            TEST_MODE=True,
            TEST_AAVID="1001",
            TEST_MATERIAL_ID="2001",
        ):
            local_test_guard.assert_test_task_scope(
                "1001",
                ["2002", "2003"],
                ["2001", "2002", "2003"],
            )
            with self.assertRaisesRegex(RuntimeError, "不属于本次飞书提醒候选"):
                local_test_guard.assert_test_task_scope(
                    "1001",
                    ["9999"],
                    ["2001", "2002", "2003"],
                )
            with self.assertRaisesRegex(RuntimeError, "账户"):
                local_test_guard.assert_test_task_scope(
                    "1002",
                    ["2002"],
                    ["2001", "2002", "2003"],
                )

    def test_live_permission_can_only_be_consumed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            consumed = os.path.join(tmp, "live_retarget_consumed.json")
            with patch.multiple(
                local_test_guard,
                TEST_MODE=True,
                TEST_AAVID="1001",
                TEST_MATERIAL_ID="2001",
                ALLOW_LIVE_RETARGET=True,
                DATA_DIR=tmp,
                CONSUMED_FILE=consumed,
            ):
                local_test_guard.consume_live_retarget_once("task-1", "1001", "2001")
                self.assertTrue(os.path.isfile(consumed))
                with self.assertRaisesRegex(RuntimeError, "已被消费"):
                    local_test_guard.consume_live_retarget_once("task-1", "1001", "2001")

    def test_live_permission_accepts_multiple_selected_card_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            consumed = os.path.join(tmp, "live_retarget_consumed.json")
            with patch.multiple(
                local_test_guard,
                TEST_MODE=True,
                TEST_AAVID="1001",
                TEST_MATERIAL_ID="2001",
                ALLOW_LIVE_RETARGET=True,
                DATA_DIR=tmp,
                CONSUMED_FILE=consumed,
            ):
                local_test_guard.consume_live_retarget_batch_once(
                    "task-batch",
                    "1001",
                    ["2002", "2003"],
                    ["2001", "2002", "2003"],
                )
                with open(consumed, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                self.assertEqual(["2002", "2003"], payload["material_ids"])

    def test_live_permission_defaults_to_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.multiple(
                local_test_guard,
                TEST_MODE=True,
                TEST_AAVID="1001",
                TEST_MATERIAL_ID="2001",
                ALLOW_LIVE_RETARGET=False,
                DATA_DIR=tmp,
                CONSUMED_FILE=os.path.join(tmp, "gate.json"),
            ):
                with self.assertRaisesRegex(RuntimeError, "未开启"):
                    local_test_guard.consume_live_retarget_once("task-1", "1001", "2001")

    def test_preflight_is_ready_before_final_live_authorization(self):
        config = {
            "enabled": True,
            "strategies": [
                {
                    "id": "s1",
                    "title": "受控验收策略",
                    "target_uid": "target-test",
                    "action_mode": "card_confirm",
                    "trigger": {"group_combine": "or", "groups": []},
                    "retargeting": {
                        "method": "volume",
                        "volume": {
                            "total_budget_yuan": 100,
                            "duration_hours": 0.5,
                        },
                    },
                }
            ],
        }

        class FakeStore:
            def select_one(self, table, **_kwargs):
                if table == "promotion_target":
                    return {
                        "target_uid": "target-test",
                        "aadvid": "1001",
                        "ad_id": "3001",
                        "promotion_scene": "product",
                        "plan_system": "global",
                        "plan_name": "测试全域计划",
                        "enabled": 1,
                        "last_status": "ok",
                        "capability_json": _retarget_capability_json(
                            target_uid="target-test",
                            aavid="1001",
                            ad_id="3001",
                            scene="product",
                            plan_system="global",
                        ),
                    }
                if table == "pmc_ad_detail_basic":
                    return {"user_info_name": "测试账户"}
                if table == "pmc_promotion_material":
                    return {"video_name": "测试素材"}
                return None

            def select(self, *_args, **_kwargs):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "qcookie.json"), "w", encoding="utf-8") as handle:
                handle.write("{}")
            with patch.multiple(
                local_test_guard,
                TEST_MODE=True,
                TEST_AAVID="1001",
                TEST_MATERIAL_ID="2001",
                ALLOW_LIVE_RETARGET=False,
                DATA_DIR=tmp,
                CONSUMED_FILE=os.path.join(tmp, "live_retarget_consumed.json"),
            ), patch(
                "api.rule_retargeting_config.load_rule_retargeting_config",
                return_value=config,
            ), patch(
                "api.rule_retargeting_config.validate_rule_retargeting_config",
                return_value=(True, ""),
            ), patch(
                "services.cloud_retarget_client.load_device_session",
                return_value={"username": "tester", "token": "device-token"},
            ), patch(
                "services.retargeting_rule_runner.resolve_ad_id_for_aavid",
                return_value="3001",
            ), patch(
                "utils.sqlite_store.init_sqlite_schema",
            ), patch(
                "utils.sqlite_store.SQLiteStore",
                return_value=FakeStore(),
            ):
                result = local_test_guard.build_live_retarget_preflight()
                with patch(
                    "services.retargeting_rule_runner.rate_limit_should_skip",
                    return_value=True,
                ):
                    limited_result = local_test_guard.build_live_retarget_preflight()

        self.assertTrue(result["ready_to_arm"])
        self.assertFalse(result["ready_to_execute"])
        self.assertEqual("测试账户", result["account_name"])
        self.assertEqual("测试素材", result["material_name"])
        self.assertIn("预算 100 元", result["strategies"][0]["summary"])
        rate_limit_check = next(
            item
            for item in result["checks"]
            if item["key"] == "retarget_rate_limit"
        )
        self.assertTrue(rate_limit_check["ok"])
        limited_rate_check = next(
            item
            for item in limited_result["checks"]
            if item["key"] == "retarget_rate_limit"
        )
        self.assertFalse(limited_result["ready_to_arm"])
        self.assertFalse(limited_rate_check["ok"])
        self.assertIn("达到全局限频", limited_rate_check["detail"])

    def test_preflight_rejects_pending_monitor_target(self):
        config = {
            "enabled": True,
            "strategies": [
                {
                    "id": "s1",
                    "title": "受控验收策略",
                    "target_uid": "target-test",
                    "action_mode": "card_confirm",
                    "trigger": {"group_combine": "or", "groups": []},
                    "retargeting": {
                        "method": "volume",
                        "volume": {
                            "total_budget_yuan": 100,
                            "duration_hours": 0.5,
                        },
                    },
                }
            ],
        }

        class FakeStore:
            def select_one(self, table, **_kwargs):
                if table == "promotion_target":
                    return {
                        "target_uid": "target-test",
                        "aadvid": "1001",
                        "ad_id": "3001",
                        "promotion_scene": "product",
                        "plan_system": "global",
                        "plan_name": "测试全域计划",
                        "enabled": 1,
                        "last_status": "pending",
                        "capability_json": _retarget_capability_json(
                            target_uid="target-test",
                            aavid="1001",
                            ad_id="3001",
                            scene="product",
                            plan_system="global",
                        ),
                    }
                if table == "pmc_ad_detail_basic":
                    return {"user_info_name": "测试账户"}
                if table == "pmc_promotion_material":
                    return {"video_name": "测试素材"}
                return None

            def select(self, *_args, **_kwargs):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "qcookie.json"), "w", encoding="utf-8") as handle:
                handle.write("{}")
            with patch.multiple(
                local_test_guard,
                TEST_MODE=True,
                TEST_AAVID="1001",
                TEST_MATERIAL_ID="2001",
                ALLOW_LIVE_RETARGET=False,
                DATA_DIR=tmp,
                CONSUMED_FILE=os.path.join(tmp, "live_retarget_consumed.json"),
            ), patch(
                "api.rule_retargeting_config.load_rule_retargeting_config",
                return_value=config,
            ), patch(
                "api.rule_retargeting_config.validate_rule_retargeting_config",
                return_value=(True, ""),
            ), patch(
                "services.cloud_retarget_client.load_device_session",
                return_value={"username": "tester", "token": "device-token"},
            ), patch(
                "services.retargeting_rule_runner.resolve_ad_id_for_aavid",
                return_value="3001",
            ), patch(
                "utils.sqlite_store.init_sqlite_schema",
            ), patch(
                "utils.sqlite_store.SQLiteStore",
                return_value=FakeStore(),
            ):
                result = local_test_guard.build_live_retarget_preflight()

        self.assertFalse(result["ready_to_arm"])
        status_check = next(
            item
            for item in result["checks"]
            if item["key"] == "monitor_target_status"
        )
        self.assertFalse(status_check["ok"])
        self.assertIn("pending", status_check["detail"])


if __name__ == "__main__":
    unittest.main()
