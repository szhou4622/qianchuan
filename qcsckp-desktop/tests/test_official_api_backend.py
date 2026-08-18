import json
import asyncio
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.error import URLError
from unittest.mock import patch

from services.qianchuan_open_api.client import ApiResponse, QianchuanOpenApiClient
from services.qianchuan_open_api.errors import (
    ApiRateLimitError,
    ApiRequestError,
    ApiWriteOutcomeUnknown,
    OfficialApiWriteDisabled,
)
from services.qianchuan_open_api.normalizers import (
    normalize_account,
    normalize_control_task,
    normalize_material,
    normalize_plan,
    stable_material_set,
)
from api.promotion_targets import target_eligibility
from services.qianchuan_open_api.service import QianchuanOfficialApiService
from services.qianchuan_open_api.token_provider import (
    AccessTokenBundle,
    DefaultTokenProvider,
    DpapiTokenProvider,
    InjectedTokenProvider,
    api_configuration_status,
    begin_api_authorization,
    exchange_authorization_code,
    _oauth_callback_query,
    _relay_json_request,
    poll_api_authorization,
    save_api_credentials,
)
from services.promotion_capability import check_target_capability
from services.official_api_collection import (
    _adaptive_worker_limit,
    _collect_target_safely,
    _collection_phase_plan,
    _fair_order_targets,
    _observe_collection_results,
    _reset_adaptive_collection_state_for_tests,
    _target_is_due,
    collect_target,
    run_collection_cycle,
    _supported_material_metrics,
    request_official_api_collection,
)
from services.official_api_execution import (
    OfficialApiRetargetingService,
    _configured_control_task_base_name,
    _material_is_writable,
    _unique_control_task_name,
    _verify_control_task,
    _verify_control_task_eventually,
)
from unittest.mock import MagicMock, Mock


class _PagedClient(QianchuanOpenApiClient):
    def __init__(self, pages):
        super().__init__(InjectedTokenProvider(AccessTokenBundle("token")))
        self.pages = list(pages)

    def get(self, endpoint, query=None, *, advertiser_id=""):
        page = int((query or {}).get("page") or 1)
        return self.pages[page - 1]


class _CaptureClient:
    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])
        self.posts = []

    def get_all_pages(self, endpoint, query, **kwargs):
        self.last_pages = (endpoint, dict(query or {}), dict(kwargs or {}))
        if "control_task/list" in endpoint:
            return self.tasks, ["req-list"]
        return [], []

    def post(self, endpoint, body, *, advertiser_id=""):
        self.posts.append((endpoint, body, str(advertiser_id)))
        return ApiResponse(
            data={"task_id": 9876543210123456789},
            raw={"code": 0},
            request_id="req-write",
        )


class _ReportConfigClient(_CaptureClient):
    def get(self, endpoint, query=None, *, advertiser_id=""):
        self.last_get = (endpoint, dict(query or {}), str(advertiser_id))
        return ApiResponse(
            data={
                "custom_config_datas": [
                    {
                        "data_topic": (query or {}).get("data_topics", [""])[0],
                        "metrics": [{"field": "stat_cost_for_roi2", "unit": 3}],
                    }
                ]
            },
            raw={"code": 0},
            request_id="req-report-config",
        )


class OfficialApiCollectionMetricTests(unittest.TestCase):
    def test_adaptive_concurrency_backs_off_on_429_and_recovers_gradually(self):
        _reset_adaptive_collection_state_for_tests()
        try:
            self.assertEqual(3, _adaptive_worker_limit(3))
            self.assertEqual(
                2,
                _observe_collection_results(
                    [{"success": False, "error_kind": "rate_limit"}]
                ),
            )
            for _ in range(2):
                self.assertEqual(
                    2,
                    _observe_collection_results([{"success": True}]),
                )
            self.assertEqual(
                3,
                _observe_collection_results([{"success": True}]),
            )
        finally:
            _reset_adaptive_collection_state_for_tests()

    def test_account_fair_order_prevents_one_large_account_from_starving_others(self):
        targets = [
            {"target_uid": "a-1", "aadvid": "account-a"},
            {"target_uid": "a-2", "aadvid": "account-a"},
            {"target_uid": "a-3", "aadvid": "account-a"},
            {"target_uid": "b-1", "aadvid": "account-b"},
            {"target_uid": "c-1", "aadvid": "account-c"},
            {"target_uid": "c-2", "aadvid": "account-c"},
        ]
        self.assertEqual(
            ["a-1", "b-1", "c-1", "a-2", "c-2", "a-3"],
            [row["target_uid"] for row in _fair_order_targets(targets)],
        )

    def test_same_account_targets_are_serialized_while_other_accounts_can_parallelize(self):
        _reset_adaptive_collection_state_for_tests()
        active = 0
        max_active = 0
        lock = threading.Lock()

        def collect_one(target, *, db):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                return {"success": True, "target_uid": target["target_uid"]}
            finally:
                with lock:
                    active -= 1

        store = Mock(config={"database": ":memory:"})
        try:
            with patch(
                "services.official_api_collection._ACTIVE_TARGET_UIDS", set()
            ), patch(
                "services.official_api_collection.collect_target",
                side_effect=collect_one,
            ), patch(
                "services.official_api_collection.patch_target_sync_state"
            ), patch(
                "services.official_api_collection.record_target_duration"
            ):
                threads = [
                    threading.Thread(
                        target=_collect_target_safely,
                        kwargs={
                            "target": {
                                "target_uid": f"same-account-{index}",
                                "aadvid": "account-1",
                            },
                            "db": store,
                            "interval_seconds": 300,
                        },
                    )
                    for index in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)
            self.assertEqual(1, max_active)
        finally:
            _reset_adaptive_collection_state_for_tests()

    def test_low_frequency_phase_cache_keeps_five_minute_metrics_lightweight(self):
        now = datetime(2026, 8, 18, 21, 0, 0)
        target = {
            "promotion_scene": "product",
            "capability": {
                "report_metric_units": {"stat_cost_for_roi2": "3"},
                "report_config_synced_at": "2026-08-18 20:45:00",
                "product_catalog_synced_at": "2026-08-18 20:45:00",
            },
        }
        cached = _collection_phase_plan(target, now=now)
        self.assertFalse(cached["refresh_report_config"])
        self.assertFalse(cached["refresh_products"])
        expired = _collection_phase_plan(
            target,
            now=now + timedelta(minutes=16),
        )
        self.assertTrue(expired["refresh_report_config"])
        self.assertTrue(expired["refresh_products"])
        self.assertFalse(
            _collection_phase_plan(
                {**target, "promotion_scene": "live"},
                now=now + timedelta(hours=1),
            )["refresh_products"]
        )

    def test_cached_low_frequency_phases_still_collect_metrics_and_controls(self):
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        target = {
            "target_uid": "target-product",
            "account_uid": "account-product",
            "aadvid": "1001",
            "ad_id": "2001",
            "promotion_scene": "product",
            "plan_system": "global",
            "capability": {
                "report_metric_units": {"stat_cost_for_roi2": "3"},
                "report_config_synced_at": now_text,
                "product_catalog_synced_at": now_text,
                "product_count": 7,
            },
        }
        service = MagicMock()
        service.get_plan_detail.return_value = (
            {
                "aavid": "1001",
                "ad_id": "2001",
                "marketing_goal": "VIDEO_PROM_GOODS",
                "adlab_scene": "UNI_PROJECT",
                "platform_status": "active",
            },
            ApiResponse(data={}, raw={"code": 0}, request_id="detail-request"),
        )
        service.list_plan_materials.return_value = ([], ["material-request"])
        service.list_control_tasks.return_value = ([], ["control-request"])
        store = MagicMock(config={"database": ":memory:"})
        store.transaction.return_value.__enter__.return_value = MagicMock()
        with patch(
            "services.official_api_collection.get_official_api_service",
            return_value=service,
        ), patch(
            "services.official_api_collection.init_sqlite_schema"
        ), patch(
            "services.official_api_collection.update_target_catalog_evidence"
        ), patch(
            "services.official_api_collection.patch_target_sync_state"
        ), patch(
            "services.retargeting_rule_runner.request_retargeting_rule_evaluation"
        ):
            result = collect_target(target, db=store)
        service.get_report_config.assert_not_called()
        service.list_plan_products.assert_not_called()
        service.list_plan_materials.assert_called_once()
        service.list_control_tasks.assert_called_once()
        self.assertEqual(7, result["product_count"])
        self.assertFalse(result["product_catalog_refreshed"])

    def test_official_delivery_ok_material_is_writable(self):
        self.assertTrue(
            _material_is_writable(
                {"material_status": "DELIVERY_OK", "audit_status": "PASS"}
            )
        )

    def test_unknown_material_status_still_fails_closed(self):
        self.assertFalse(
            _material_is_writable(
                {"material_status": "", "audit_status": "PASS"}
            )
        )

    def test_control_task_verification_accepts_omitted_scene_from_filtered_list(self):
        service = Mock()
        service.list_control_tasks.return_value = (
            [
                {
                    "task_id": "1873324404257064",
                    "ad_id": "",
                    "scene": "",
                    "material_ids": ["7643772216392564762"],
                    "budget": 100,
                    "duration": 24,
                }
            ],
            ["req-list"],
        )
        task = _verify_control_task(
            service,
            aavid="1795110974060618",
            ad_id="1859333122962634",
            promotion_scene="product",
            task_id="1873324404257064",
            material_ids=["7643772216392564762"],
            budget=100,
            duration=24,
        )
        self.assertEqual("1873324404257064", task["task_id"])

    def test_control_task_verification_rejects_explicit_wrong_scene(self):
        service = Mock()
        service.list_control_tasks.return_value = (
            [
                {
                    "task_id": "1873324404257064",
                    "scene": "OTHER_SCENE",
                    "material_ids": ["7643772216392564762"],
                    "budget": 100,
                    "duration": 24,
                }
            ],
            ["req-list"],
        )
        with self.assertRaisesRegex(RuntimeError, "不是素材追投"):
            _verify_control_task(
                service,
                aavid="1795110974060618",
                ad_id="1859333122962634",
                promotion_scene="product",
                task_id="1873324404257064",
                material_ids=["7643772216392564762"],
                budget=100,
                duration=24,
            )

    def test_product_topic_drops_live_only_material_metrics(self):
        units = {
            "stat_cost_for_roi2": "0",
            "total_prepay_and_pay_order_roi2": "0",
            "live_show_count_for_roi2_v2": "0",
        }
        self.assertEqual(
            (
                "stat_cost_for_roi2",
                "total_prepay_and_pay_order_roi2",
                "live_show_count_for_roi2_v2",
            ),
            _supported_material_metrics(units),
        )

    def test_unknown_metrics_are_not_sent_to_material_endpoint(self):
        self.assertEqual(
            ("stat_cost_for_roi2",),
            _supported_material_metrics({"stat_cost_for_roi2": "0", "other": "0"}),
        )

    def test_immediate_collection_request_is_deduplicated_and_starts_now(self):
        store = Mock()
        pending = set()
        wake = Mock()
        with patch(
            "services.official_api_collection.start_official_api_collection_background_thread"
        ), patch(
            "services.official_api_collection.patch_target_sync_state"
        ) as patch_state, patch(
            "services.official_api_collection._ACTIVE_TARGET_UIDS", set()
        ), patch(
            "services.official_api_collection._PENDING_TARGET_UIDS", pending
        ), patch(
            "services.official_api_collection._WAKE", wake
        ):
            result = request_official_api_collection(
                ["target-1", "target-1", ""],
                db=store,
            )
        self.assertEqual(0, result["queued_count"])
        self.assertEqual(1, result["started_count"])
        self.assertEqual(["target-1"], result["target_uids"])
        self.assertEqual({"target-1"}, pending)
        patch_state.assert_called_once()
        wake.set.assert_called_once_with()

    def test_immediate_collection_does_not_duplicate_active_target(self):
        with patch(
            "services.official_api_collection.start_official_api_collection_background_thread"
        ), patch(
            "services.official_api_collection.patch_target_sync_state"
        ) as patch_state, patch(
            "services.official_api_collection._ACTIVE_TARGET_UIDS", {"target-1"}
        ), patch(
            "services.official_api_collection._PENDING_TARGET_UIDS", set()
        ):
            result = request_official_api_collection(["target-1"], db=Mock())
        self.assertEqual(0, result["started_count"])
        self.assertEqual(1, result["already_collecting_count"])
        patch_state.assert_not_called()

    def test_target_due_time_is_per_target_not_per_whole_cycle(self):
        now = datetime.now().replace(microsecond=0)
        self.assertTrue(
            _target_is_due(
                {"next_due_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")},
                now=now,
            )
        )
        self.assertFalse(
            _target_is_due(
                {"next_due_at": (now + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")},
                now=now,
            )
        )

    def test_collection_cycle_runs_accounts_concurrently_and_contains_failure(self):
        targets = [{"target_uid": f"target-{index}"} for index in range(4)]
        active = 0
        max_active = 0
        lock = threading.Lock()

        def collect_one(target, *, db, interval_seconds):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                if target["target_uid"] == "target-2":
                    raise RuntimeError("isolated target failure")
                return {"success": True, "target_uid": target["target_uid"]}
            finally:
                with lock:
                    active -= 1

        with patch(
            "services.official_api_collection.schedulable_promotion_targets",
            return_value=targets,
        ), patch(
            "services.official_api_collection._collect_target_safely",
            side_effect=collect_one,
        ), patch(
            "services.official_api_collection.refresh_monitor_capacity"
        ):
            result = run_collection_cycle(
                db=Mock(config={"database": ":memory:"}),
                target_uids=[row["target_uid"] for row in targets],
                max_workers=2,
            )

        self.assertFalse(result["success"])
        self.assertEqual(4, result["target_count"])
        self.assertEqual(2, result["worker_count"])
        self.assertEqual(2, max_active)
        self.assertEqual(
            {"target-0", "target-1", "target-2", "target-3"},
            {row["target_uid"] for row in result["results"]},
        )

    def test_periodic_collection_submits_only_due_targets(self):
        now = datetime.now().replace(microsecond=0)
        targets = [
            {
                "target_uid": "due",
                "next_due_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            },
            {
                "target_uid": "later",
                "next_due_at": (now + timedelta(minutes=4)).strftime("%Y-%m-%d %H:%M:%S"),
            },
        ]
        with patch(
            "services.official_api_collection.schedulable_promotion_targets",
            return_value=targets,
        ), patch(
            "services.official_api_collection._collect_target_safely",
            return_value={"success": True, "target_uid": "due"},
        ) as collect_one, patch(
            "services.official_api_collection.refresh_monitor_capacity"
        ):
            result = run_collection_cycle(
                db=Mock(config={"database": ":memory:"}),
                max_workers=3,
            )
        self.assertEqual(1, result["target_count"])
        self.assertEqual("due", collect_one.call_args.args[0]["target_uid"])

    def test_periodic_collection_uses_one_wave_batches_for_prompt_new_work(self):
        targets = [
            {"target_uid": "account-a-1", "aadvid": "account-a"},
            {"target_uid": "account-a-2", "aadvid": "account-a"},
            {"target_uid": "account-a-3", "aadvid": "account-a"},
            {"target_uid": "account-b-1", "aadvid": "account-b"},
            {"target_uid": "account-c-1", "aadvid": "account-c"},
        ]
        with patch(
            "services.official_api_collection.schedulable_promotion_targets",
            return_value=targets,
        ), patch(
            "services.official_api_collection._target_is_due",
            return_value=True,
        ), patch(
            "services.official_api_collection._collect_target_safely",
            side_effect=lambda target, **_: {
                "success": True,
                "target_uid": target["target_uid"],
            },
        ) as collect_one, patch(
            "services.official_api_collection.refresh_monitor_capacity"
        ):
            result = run_collection_cycle(
                db=Mock(config={"database": ":memory:"}),
                max_workers=3,
                max_batch_size=3,
            )
        self.assertEqual(3, result["target_count"])
        self.assertEqual(3, collect_one.call_count)
        self.assertEqual(
            ["account-a-1", "account-b-1", "account-c-1"],
            [row["target_uid"] for row in result["results"]],
        )

    def test_rate_limit_failure_is_isolated_and_retried_early(self):
        store = Mock()
        _reset_adaptive_collection_state_for_tests()
        try:
            with patch(
                "services.official_api_collection._ACTIVE_TARGET_UIDS", set()
            ), patch(
                "services.official_api_collection.collect_target",
                side_effect=ApiRateLimitError("rate limited"),
            ) as collect_one, patch(
                "services.official_api_collection.patch_target_sync_state"
            ) as patch_state, patch(
                "services.official_api_collection._set_retry_due"
            ) as set_retry:
                result = _collect_target_safely(
                    {
                        "target_uid": "rate-limited-target",
                        "aadvid": "account-rate-limited",
                    },
                    db=store,
                    interval_seconds=300,
                )
                deferred = _collect_target_safely(
                    {
                        "target_uid": "same-account-next-target",
                        "aadvid": "account-rate-limited",
                    },
                    db=store,
                    interval_seconds=300,
                )
            self.assertFalse(result["success"])
            self.assertEqual(120, result["retry_seconds"])
            self.assertEqual("error", patch_state.call_args_list[1].kwargs["status"])
            self.assertTrue(deferred["deferred"])
            self.assertEqual("account_backoff", deferred["error_kind"])
            self.assertEqual(1, collect_one.call_count)
            self.assertEqual(2, set_retry.call_count)
            set_retry.assert_any_call(
                "rate-limited-target",
                delay_seconds=120,
                db=store,
            )
        finally:
            _reset_adaptive_collection_state_for_tests()

    def test_repeated_rate_limit_uses_exponential_account_backoff(self):
        store = Mock()
        _reset_adaptive_collection_state_for_tests()
        try:
            with patch(
                "services.official_api_collection._ACTIVE_TARGET_UIDS", set()
            ), patch(
                "services.official_api_collection.collect_target",
                side_effect=ApiRateLimitError("rate limited again"),
            ), patch(
                "services.official_api_collection.patch_target_sync_state"
            ), patch(
                "services.official_api_collection._set_retry_due"
            ):
                result = _collect_target_safely(
                    {
                        "target_uid": "repeated-rate-limit",
                        "aadvid": "account-repeated-rate-limit",
                        "capability": {"collection_consecutive_failures": 1},
                    },
                    db=store,
                    interval_seconds=300,
                )
            self.assertEqual(240, result["retry_seconds"])
        finally:
            _reset_adaptive_collection_state_for_tests()


class _PublicInfoClient(_CaptureClient):
    def get(self, endpoint, query=None, *, advertiser_id=""):
        self.last_get = (endpoint, dict(query or {}), str(advertiser_id))
        return ApiResponse(
            data=[
                {
                    "advertiser_id": "1782685702496260",
                    "advertiser_name": "松之选专卖店",
                }
            ],
            raw={"code": 0},
            request_id="req-public-info",
        )


class OfficialApiBackendTests(unittest.TestCase):
    def test_successful_create_is_not_reported_failed_when_list_reconciliation_lags(self):
        service = Mock()
        service.list_plan_materials.return_value = (
            [
                {
                    "material_id": "7643772216392564762",
                    "material_status": "DELIVERY_OK",
                    "audit_status": "PASS",
                }
            ],
            ["req-material"],
        )
        response = ApiResponse(
            data={"task_id": "1873333931211978"},
            raw={"code": 0},
            request_id="req-create",
            request_uid="write-uid",
        )
        service.create_material_control_task.return_value = response
        reconcile = Mock()
        runner = OfficialApiRetargetingService()
        with patch(
            "services.official_api_execution.get_official_api_service",
            return_value=service,
        ), patch(
            "services.official_api_execution._check_plan",
            return_value={},
        ), patch(
            "services.official_api_execution._start_control_task_reconciliation",
            reconcile,
        ), patch(
            "services.official_api_execution._existing_reconciliation",
            return_value=None,
        ), patch(
            "services.official_api_reconciliation.reserve_execution_intent",
            return_value=({}, True),
        ):
            result = asyncio.run(
                runner.run(
                    aavid=1795110974060618,
                    ad_id=1859333122962634,
                    material_id="7643772216392564762",
                    retargeting={
                        "method": "volume",
                        "volume": {"total_budget_yuan": 100, "duration_hours": 24},
                    },
                    strategy_title="策略 1",
                    promotion_scene="product",
                    plan_system="global",
                    execution_uid="abdcf523-b1d0-45e5-992e-f023ffdc13e9",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual("1873333931211978", result.regulate_task_id)
        self.assertEqual("submitted_verifying", result.step)
        self.assertIn("正在核验", result.message)
        service.create_material_control_task.assert_called_once()
        reconcile.assert_called_once_with(
            service,
            request_uid="write-uid",
            request_id="req-create",
            task_id="1873333931211978",
            task_uid="abdcf523-b1d0-45e5-992e-f023ffdc13e9",
            idempotency_key="abdcf523-b1d0-45e5-992e-f023ffdc13e9",
            verify_kwargs={
                "aavid": 1795110974060618,
                "ad_id": 1859333122962634,
                "promotion_scene": "product",
                "task_id": "1873333931211978",
                "material_ids": ["7643772216392564762"],
                "budget": Decimal("100"),
                "duration": Decimal("24"),
                "execution_uid": "abdcf523-b1d0-45e5-992e-f023ffdc13e9",
            },
        )

    def test_create_reconciliation_retries_incomplete_material_list_without_resubmitting(self):
        expected = {
            "task_id": "88",
            "ad_id": "456",
            "scene": "MATERIAL_ADD_BUDGET",
            "material_ids": ["1"],
            "budget": "100",
            "duration": "24",
        }
        with patch(
            "services.official_api_execution._verify_control_task",
            side_effect=[RuntimeError("material list incomplete"), expected],
        ) as verify:
            slept = []
            task = _verify_control_task_eventually(
                object(),
                aavid="123",
                ad_id="456",
                promotion_scene="product",
                task_id="88",
                material_ids=["1"],
                budget="100",
                duration="24",
                retry_delays=(0.25,),
                sleep=slept.append,
            )
        self.assertEqual("88", task["task_id"])
        self.assertEqual([0.25], slept)
        self.assertEqual(2, verify.call_count)

    def test_create_reconciliation_fails_after_bounded_read_only_retries(self):
        with patch(
            "services.official_api_execution._verify_control_task",
            side_effect=RuntimeError("still incomplete"),
        ) as verify:
            with self.assertRaisesRegex(RuntimeError, "still incomplete"):
                _verify_control_task_eventually(
                    object(),
                    retry_delays=(0, 0),
                    sleep=lambda _: None,
                )
        self.assertEqual(3, verify.call_count)

    def test_control_task_name_is_unique_per_feishu_execution_and_stable_for_retry(self):
        fixed = datetime(2026, 8, 12, 23, 36, 8)
        first = _unique_control_task_name(
            "策略 1", "66fda617-8e5e-4cc6-80d7-d206d53a0f79", now=fixed
        )
        retry = _unique_control_task_name(
            "策略 1", "66fda617-8e5e-4cc6-80d7-d206d53a0f79", now=fixed
        )
        second = _unique_control_task_name(
            "策略 1", "dec2bf2b-1e49-415b-96ed-b0bd891c09ac", now=fixed
        )
        self.assertEqual(first, retry)
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"-[0-9a-f]{8}$")
        self.assertLessEqual(len(first), 50)

    def test_control_task_name_uses_user_configured_name_before_unique_marker(self):
        base = _configured_control_task_base_name(
            {"task_name_suffix": "晚间高ROI素材追投"},
            "策略 1",
        )
        task_name = _unique_control_task_name(
            base,
            "66fda617-8e5e-4cc6-80d7-d206d53a0f79",
        )
        self.assertRegex(task_name, r"^晚间高ROI素材追投-[0-9a-f]{8}$")

    def test_control_task_name_falls_back_to_strategy_for_legacy_payload(self):
        self.assertEqual(
            "策略 1",
            _configured_control_task_base_name({}, "策略 1"),
        )

    def test_control_task_name_is_distinct_for_groups_of_same_card(self):
        first = _unique_control_task_name("策略 1", "card-uid:group:1")
        second = _unique_control_task_name("策略 1", "card-uid:group:2")
        self.assertNotEqual(first, second)

    def test_control_task_name_respects_platform_length_limit(self):
        name = _unique_control_task_name(
            "长" * 150, "66fda617-8e5e-4cc6-80d7-d206d53a0f79"
        )
        self.assertEqual(50, len(name))

    def test_control_task_name_without_execution_uid_also_stays_within_limit(self):
        name = _unique_control_task_name(
            "长" * 50,
            None,
            now=datetime(2026, 8, 13, 21, 45, 59, 123456),
        )
        self.assertEqual(50, len(name))
        self.assertRegex(name, r"-[0-9a-f]{8}$")

    def test_report_config_uses_official_data_topics_for_all_four_plan_classes(self):
        cases = {
            ("chengfang", "live"): "OVERALL_ROI_LIVE_MATERIAL_VIDEO",
            ("chengfang", "product"): "OVERALL_ROI_PRODUCT_MATERIAL",
            ("global", "live"): "SITE_PROMOTION_POST_DATA_VIDEO",
            ("global", "product"): "SITE_PROMOTION_PRODUCT_POST_DATA_VIDEO",
        }
        for plan_class, expected_topic in cases.items():
            client = _ReportConfigClient()
            service = QianchuanOfficialApiService(client)
            units, response = service.get_report_config(
                "1854823495704009",
                plan_system=plan_class[0],
                promotion_scene=plan_class[1],
            )
            self.assertEqual([expected_topic], client.last_get[1]["data_topics"])
            self.assertNotIn("marketing_goal", client.last_get[1])
            self.assertEqual("3", units["stat_cost_for_roi2"])
            self.assertEqual("req-report-config", response.request_id)

    @patch("services.qianchuan_open_api.token_provider.urlopen")
    def test_oauth_relay_request_uses_explicit_desktop_user_agent(self, mocked_urlopen):
        class _Response:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"success":true,"status":"pending"}'

        mocked_urlopen.return_value = _Response()
        status, result = _relay_json_request(
            "/oauth/session",
            {"state": "state-value", "poll_secret": "poll-secret"},
            base_url="https://callback.example.test",
        )
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(status, 201)
        self.assertTrue(result["success"])
        self.assertEqual(request.get_header("User-agent"), "QCSCKP-Desktop/0.1.49")
        self.assertEqual(request.get_header("Accept"), "application/json")

    @patch("services.qianchuan_open_api.token_provider._protect", side_effect=lambda raw: raw)
    @patch("services.qianchuan_open_api.token_provider._unprotect", side_effect=lambda raw: raw)
    def test_api_credentials_are_saved_without_plaintext_secret(self, _unprotect, _protect):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "official-api.json")
            status = save_api_credentials("1869344049893595", "secret-value", path)
            self.assertTrue(status["configured"])
            self.assertFalse(status["authorized"])
            self.assertTrue(status["app_secret_saved"])
            with open(path, "r", encoding="utf-8") as handle:
                stored = handle.read()
            self.assertNotIn("secret-value", stored)
            self.assertNotIn("1869344049893595", stored)
            self.assertNotIn("app_secret", stored)

    @patch("services.qianchuan_open_api.token_provider._protect", side_effect=lambda raw: raw)
    def test_new_secret_can_replace_configuration_that_current_user_cannot_decrypt(
        self, _protect
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "official-api.json")
            with patch(
                "services.qianchuan_open_api.token_provider._unprotect",
                side_effect=lambda raw: raw,
            ):
                save_api_credentials("1869344049893595", "old-secret", path)

            calls = 0

            def fail_once(raw):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ValueError("foreign DPAPI ciphertext")
                return raw

            with patch(
                "services.qianchuan_open_api.token_provider._unprotect",
                side_effect=fail_once,
            ):
                status = save_api_credentials(
                    "1869344049893595", "new-secret", path
                )

            self.assertTrue(status["configured"])
            self.assertTrue(status["app_secret_saved"])

    def test_unreadable_foreign_configuration_returns_reentry_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "official-api.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "format": "qcsckp-oceanengine-token-dpapi-v1",
                        "ciphertext": "Zm9yZWlnbi1jaXBoZXJ0ZXh0",
                    },
                    handle,
                )
            with patch(
                "services.qianchuan_open_api.token_provider._unprotect",
                side_effect=ValueError("foreign DPAPI ciphertext"),
            ):
                status = api_configuration_status(path)

            self.assertFalse(status["configured"])
            self.assertFalse(status["authorized"])
            self.assertTrue(status["requires_reentry"])
            self.assertEqual(
                "unreadable_local_encryption", status["configuration_error"]
            )

    @patch("services.qianchuan_open_api.token_provider._protect", side_effect=lambda raw: raw)
    @patch("services.qianchuan_open_api.token_provider._unprotect", side_effect=lambda raw: raw)
    def test_begin_authorization_uses_saved_app_and_random_state(self, _unprotect, _protect):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "official-api.json")
            save_api_credentials("1869344049893595", "secret-value", path)
            relay = lambda _endpoint, _payload: (201, {"success": True})
            first = begin_api_authorization(path, relay_request=relay)
            second = begin_api_authorization(path, relay_request=relay)
            self.assertIn("app_id=1869344049893595", first["url"])
            self.assertIn("material_auth=1", first["url"])
            self.assertNotEqual(first["url"], second["url"])
            self.assertTrue(api_configuration_status(path)["authorization_pending"])

    @patch("services.qianchuan_open_api.token_provider._protect", side_effect=lambda raw: raw)
    @patch("services.qianchuan_open_api.token_provider._unprotect", side_effect=lambda raw: raw)
    def test_default_authorization_uses_browser_capture_without_fixed_relay(
        self, _unprotect, _protect
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "official-api.json")
            save_api_credentials("1869344049893595", "secret-value", path)
            auth = begin_api_authorization(path)
            self.assertTrue(auth["state"])
            status = api_configuration_status(path)
            self.assertTrue(status["authorization_pending"])
            self.assertEqual("", status["oauth_callback_url"])
            self.assertEqual("browser_navigation", status["oauth_capture_mode"])
            self.assertTrue(status["oauth_callback_managed_by_platform"])

    @patch("services.qianchuan_open_api.token_provider._protect", side_effect=lambda raw: raw)
    @patch("services.qianchuan_open_api.token_provider._unprotect", side_effect=lambda raw: raw)
    @patch("services.qianchuan_open_api.token_provider._discard_oauth_browser_callback")
    @patch(
        "services.qianchuan_open_api.token_provider._peek_oauth_browser_callback",
        return_value="auth_code=one-time-code&state=state-from-bundle",
    )
    @patch("services.qianchuan_open_api.token_provider.exchange_authorization_code")
    def test_poll_reads_callback_captured_from_authorization_browser(
        self,
        exchange,
        peek,
        discard,
        _unprotect,
        _protect,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "official-api.json")
            save_api_credentials("1869344049893595", "secret-value", path)
            from services.qianchuan_open_api.token_provider import (
                _load_saved_bundle,
                save_token_bundle,
            )

            bundle = _load_saved_bundle(path)
            save_token_bundle(
                AccessTokenBundle(
                    access_token="",
                    app_id=bundle.app_id,
                    app_secret=bundle.app_secret,
                    oauth_state="state-from-bundle",
                    oauth_started_at=time.time(),
                ),
                path,
            )
            result = poll_api_authorization(path)
            self.assertTrue(result["completed"])
            peek.assert_called_once_with("state-from-bundle")
            exchange.assert_called_once_with(
                "auth_code=one-time-code&state=state-from-bundle", path
            )
            discard.assert_called_once_with("state-from-bundle")

    def test_browser_capture_only_accepts_matching_state(self):
        valid = _oauth_callback_query(
            "expected-state",
            "https://user.example/callback?auth_code=one-time-code&state=expected-state",
        )
        wrong = _oauth_callback_query(
            "expected-state",
            "https://user.example/callback?auth_code=one-time-code&state=wrong-state",
        )
        self.assertIn("auth_code=one-time-code", valid)
        self.assertEqual("", wrong)

    @patch("services.qianchuan_open_api.token_provider._protect", side_effect=lambda raw: raw)
    @patch("services.qianchuan_open_api.token_provider._unprotect", side_effect=lambda raw: raw)
    def test_saved_configuration_takes_priority_over_development_token(self, _unprotect, _protect):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"QCSCKP_OE_ACCESS_TOKEN": "old-development-token"}
        ):
            path = os.path.join(tmp, "official-api.json")
            save_api_credentials("1869344049893595", "secret-value", path)
            provider = DefaultTokenProvider()
            provider._dpapi = DpapiTokenProvider(path)
            with self.assertRaises(Exception):
                provider.get_token()

    @patch("services.qianchuan_open_api.token_provider.urlopen")
    @patch("services.qianchuan_open_api.token_provider._protect", side_effect=lambda raw: raw)
    @patch("services.qianchuan_open_api.token_provider._unprotect", side_effect=lambda raw: raw)
    def test_authorization_callback_requires_matching_state(
        self, _unprotect, _protect, mocked_urlopen
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "official-api.json")
            save_api_credentials("1869344049893595", "secret-value", path)
            auth = begin_api_authorization(
                path,
                relay_request=lambda _endpoint, _payload: (201, {"success": True}),
            )
            state = auth["url"].split("state=", 1)[1].split("&", 1)[0]
            with self.assertRaises(Exception):
                exchange_authorization_code(
                    "https://callback.invalid/?auth_code=valid-code&state=wrong",
                    path,
                )
            mocked_urlopen.assert_not_called()

    @patch("services.qianchuan_open_api.token_provider._protect", side_effect=lambda raw: raw)
    @patch("services.qianchuan_open_api.token_provider._unprotect", side_effect=lambda raw: raw)
    def test_authorization_code_exchange_uses_official_form_fields(
        self, _unprotect, _protect
    ):
        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "access_token": "access-value",
                            "refresh_token": "refresh-value",
                            "expires_in": 86400,
                        },
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "official-api.json")
            save_api_credentials("1869344049893595", "secret-value", path)
            auth = begin_api_authorization(
                path,
                relay_request=lambda _endpoint, _payload: (201, {"success": True}),
            )
            state = auth["url"].split("state=", 1)[1].split("&", 1)[0]
            with patch(
                "services.qianchuan_open_api.token_provider.urlopen",
                return_value=_Response(),
            ) as mocked:
                exchange_authorization_code(
                    f"auth_code=one-time-code&state={state}",
                    path,
                )
            request = mocked.call_args.args[0]
            form = request.data.decode("utf-8")
            self.assertIn("auth_code=one-time-code", form)
            self.assertIn("app_id=1869344049893595", form)
            self.assertIn("secret=secret-value", form)
            self.assertNotIn("grant_type", form)

    @patch("services.qianchuan_open_api.token_provider._protect", side_effect=lambda raw: raw)
    @patch("services.qianchuan_open_api.token_provider._unprotect", side_effect=lambda raw: raw)
    def test_poll_authorization_waits_without_exposing_credentials(self, _unprotect, _protect):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "official-api.json")
            save_api_credentials("1869344049893595", "secret-value", path)
            captured = []

            def relay(endpoint, payload):
                captured.append((endpoint, dict(payload)))
                if endpoint == "/oauth/session":
                    return 201, {"success": True}
                return 202, {"success": True, "status": "pending"}

            begin_api_authorization(path, relay_request=relay)
            result = poll_api_authorization(path, relay_request=relay)
            self.assertFalse(result["completed"])
            poll_payload = captured[-1][1]
            self.assertNotIn("app_id", poll_payload)
            self.assertNotIn("app_secret", poll_payload)
            self.assertNotIn("secret-value", json.dumps(captured))

    @patch("services.qianchuan_open_api.token_provider.exchange_authorization_code")
    @patch("services.qianchuan_open_api.token_provider._protect", side_effect=lambda raw: raw)
    @patch("services.qianchuan_open_api.token_provider._unprotect", side_effect=lambda raw: raw)
    def test_poll_authorization_exchanges_ready_code_once(
        self, _unprotect, _protect, mocked_exchange
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "official-api.json")
            save_api_credentials("1869344049893595", "secret-value", path)
            state_box = {}

            def relay(endpoint, payload):
                if endpoint == "/oauth/session":
                    state_box["state"] = payload["state"]
                    return 201, {"success": True}
                return 200, {
                    "success": True,
                    "status": "ready",
                    "state": state_box["state"],
                    "auth_code": "one-time-code",
                }

            begin_api_authorization(path, relay_request=relay)
            result = poll_api_authorization(path, relay_request=relay)
            self.assertTrue(result["completed"])
            mocked_exchange.assert_called_once()
            callback = mocked_exchange.call_args.args[0]
            self.assertIn("auth_code=one-time-code", callback)
            self.assertIn("state=", callback)

    def test_public_info_fills_missing_account_name(self):
        service = QianchuanOfficialApiService(_PublicInfoClient())
        rows = service.list_advertiser_public_info(["1782685702496260"])
        self.assertEqual("松之选专卖店", rows[0]["advertiser_name"])
        self.assertEqual(
            "/open_api/2/advertiser/public_info/",
            service.client.last_get[0],
        )
        self.assertEqual(
            ["1782685702496260"],
            service.client.last_get[1]["advertiser_ids"],
        )

    def test_multi_account_shop_names_are_enriched_from_public_info(self):
        service = QianchuanOfficialApiService(_CaptureClient())
        with patch.object(
            service,
            "list_authorized_accounts",
            return_value=[
                {
                    "advertiser_id": "55192491",
                    "advertiser_name": "店铺主体",
                    "role": "SHOP",
                    "shop_id": "55192491",
                }
            ],
        ), patch.object(
            service,
            "list_shop_advertisers",
            return_value=[
                {"advertiser_id": "10001", "advertiser_name": ""},
                {"advertiser_id": "10002", "advertiser_name": ""},
            ],
        ), patch.object(
            service,
            "list_advertiser_public_info",
            return_value=[
                {"advertiser_id": "10001", "advertiser_name": "账户甲"},
                {"advertiser_id": "10002", "advertiser_name": "账户乙"},
            ],
        ):
            rows, evidence = service.list_business_accounts()
        self.assertEqual(
            {"10001": "账户甲", "10002": "账户乙"},
            {row["advertiser_id"]: row["advertiser_name"] for row in rows},
        )
        self.assertTrue(evidence["account_names_complete"])

    def test_account_refresh_is_queued_while_catalog_worker_is_running(self):
        from services import official_api_catalog as catalog

        worker = unittest.mock.Mock()
        worker.is_alive.return_value = True
        with patch.object(catalog, "_THREAD", worker), patch.object(
            catalog, "_PENDING_ACCOUNT_UIDS", set()
        ), patch.object(catalog, "_PENDING_ALL", False):
            result = catalog.start_official_api_catalog_sync("account-uid-2")
            self.assertTrue(result["queued"])
            self.assertIn("account-uid-2", catalog._PENDING_ACCOUNT_UIDS)

    def test_unknown_detail_system_does_not_replace_list_classification(self):
        from services import official_api_catalog as catalog

        fake_service = unittest.mock.Mock()
        fake_service.list_business_accounts.return_value = (
            [{"advertiser_id": "10001", "advertiser_name": "测试账户"}],
            {"complete": True},
        )
        fake_service.list_all_plans.return_value = (
            [
                {
                    "aavid": "10001",
                    "ad_id": "20001",
                    "plan_name": "直播全域",
                    "promotion_scene": "live",
                    "plan_system": "global",
                    "marketing_goal": "LIVE_PROM_GOODS",
                    "adlab_scene": "UNI_PROJECT",
                    "platform_status": "active",
                }
            ],
            {"complete": True, "classes": {}},
        )
        fake_service.get_plan_detail.return_value = (
            {
                "aavid": "10001",
                "ad_id": "20001",
                "plan_name": "直播全域",
                "promotion_scene": "live",
                "plan_system": "unknown",
                "marketing_goal": "LIVE_PROM_GOODS",
                "adlab_scene": "0",
                "platform_status": "active",
            },
            ApiResponse(data={}, raw={"code": 0}, request_id="req-detail"),
        )
        account = {
            "aavid": "10001",
            "account_uid": "account-1",
            "account_name": "测试账户",
            "owner_username": "owner",
        }
        captured = []
        with patch.object(catalog, "get_official_api_service", return_value=fake_service), patch.object(
            catalog, "ensure_qianchuan_account"
        ), patch.object(catalog, "list_promotion_targets", return_value=[]), patch.object(
            catalog, "upsert_promotion_target", side_effect=lambda payload, **_kwargs: captured.append(payload) or {"target_uid": "target-1"}
        ), patch.object(catalog, "patch_target_sync_state") as patch_sync:
            result = catalog._sync_account(account, unittest.mock.Mock())
        self.assertTrue(result["complete"])
        self.assertEqual("global", captured[0]["plan_system"])
        self.assertEqual("live", captured[0]["promotion_scene"])
        self.assertIsNone(patch_sync.call_args.kwargs["status"])
        self.assertIsNone(patch_sync.call_args.kwargs["error"])

    def test_unknown_detail_status_does_not_replace_list_status(self):
        from services import official_api_catalog as catalog

        fake_service = unittest.mock.Mock()
        fake_service.list_business_accounts.return_value = (
            [{"advertiser_id": "10001", "advertiser_name": "测试账户"}],
            {"complete": True},
        )
        fake_service.list_all_plans.return_value = (
            [{
                "aavid": "10001",
                "ad_id": "20001",
                "plan_name": "商品全域",
                "promotion_scene": "product",
                "plan_system": "global",
                "marketing_goal": "VIDEO_PROM_GOODS",
                "adlab_scene": "UNI_PROJECT",
                "platform_status": "active",
            }],
            {"complete": True, "classes": {}},
        )
        fake_service.get_plan_detail.return_value = (
            {
                "aavid": "10001",
                "ad_id": "20001",
                "plan_name": "商品全域",
                "promotion_scene": "product",
                "plan_system": "global",
                "marketing_goal": "VIDEO_PROM_GOODS",
                "adlab_scene": "UNI_PROJECT",
                "platform_status": "unknown",
            },
            ApiResponse(data={}, raw={"code": 0}, request_id="req-detail"),
        )
        account = {
            "aavid": "10001",
            "account_uid": "account-1",
            "account_name": "测试账户",
            "owner_username": "owner",
        }
        captured = []
        with patch.object(catalog, "get_official_api_service", return_value=fake_service), patch.object(
            catalog, "ensure_qianchuan_account"
        ), patch.object(catalog, "list_promotion_targets", return_value=[]), patch.object(
            catalog,
            "upsert_promotion_target",
            side_effect=lambda payload, **_kwargs: captured.append(payload) or {"target_uid": "target-1"},
        ), patch.object(catalog, "patch_target_sync_state"):
            result = catalog._sync_account(account, unittest.mock.Mock())
        self.assertTrue(result["complete"])
        self.assertEqual("active", captured[0]["platform_status"])

    def test_single_shop_account_inherits_official_shop_name(self):
        service = QianchuanOfficialApiService(_CaptureClient())
        with patch.object(
            service,
            "list_authorized_accounts",
            return_value=[
                {
                    "advertiser_id": "55192491",
                    "advertiser_name": "松鲜鲜松之选专卖店",
                    "role": "SHOP",
                    "shop_id": "55192491",
                }
            ],
        ), patch.object(
            service,
            "list_shop_advertisers",
            return_value=[{"advertiser_id": "1782685702496260", "advertiser_name": ""}],
        ):
            rows, evidence = service.list_business_accounts()
        self.assertTrue(evidence["complete"])
        self.assertEqual(rows[0]["advertiser_name"], "松鲜鲜松之选专卖店")

    def test_user_selected_official_account_is_added_and_warmed(self):
        from services.official_api_catalog import add_authorized_account

        fake_service = unittest.mock.Mock()
        fake_service.list_business_accounts.return_value = (
            [
                {
                    "advertiser_id": "1782685702496260",
                    "advertiser_name": "松鲜鲜松之选专卖店",
                }
            ],
            {"complete": True},
        )
        saved = {
            "account_uid": "account-uid-1",
            "aavid": "1782685702496260",
            "account_name": "松鲜鲜松之选专卖店",
        }
        with patch(
            "services.official_api_catalog.get_official_api_service",
            return_value=fake_service,
        ), patch(
            "services.official_api_catalog.ensure_qianchuan_account",
            return_value=saved,
        ) as ensure, patch(
            "services.official_api_catalog.start_official_api_catalog_sync",
            return_value={"success": True, "running": True},
        ) as start_sync:
            result = add_authorized_account("1782685702496260")
        self.assertTrue(result["success"])
        ensure.assert_called_once()
        start_sync.assert_called_once_with("account-uid-1")

    def test_real_oceanengine_collection_keys_are_extracted(self):
        cases = {
            "adv_id_list": [{"adv_id": "123"}],
            "account_list": [{"account_id": "234"}],
            "ad_list": [{"ad_id": "456"}],
            "ad_material_infos": [{"material_info": {"material_type": "VIDEO"}}],
            "material_list": [{"material_id": "789"}],
            "product_list": [{"product_id": "321"}],
            "task_list": [{"task_id": "654"}],
            "log_list": [{"log_id": "987"}],
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                self.assertEqual(
                    QianchuanOpenApiClient.extract_items({key: expected}),
                    expected,
                )

    def test_real_material_wrapper_is_normalized(self):
        row = {
            "audit_status": "PASS",
            "material_status": "DELIVERY_OK",
            "material_select_type": "CUSTOM",
            "material_info": {
                "material_type": "VIDEO",
                "video_material": {
                    "material_id": 7673039307986386995,
                    "video_id": "video-1",
                    "title": "sample.mp4",
                },
            },
            "product_info": [{"product_id": 1234567890123456789}],
            "stats_info": {"stat_cost_for_roi2": 12.5},
        }
        material = normalize_material(row)
        self.assertEqual("7673039307986386995", material["material_id"])
        self.assertEqual("sample.mp4", material["material_name"])
        self.assertEqual("VIDEO", material["material_type"])
        self.assertEqual(["1234567890123456789"], material["product_ids"])
        self.assertEqual(12.5, material["stats_info"]["stat_cost_for_roi2"])

    def test_shop_advertiser_object_list_is_not_hidden_by_numeric_list(self):
        data = {
            "adv_id_list": [{"adv_id": "1782685702496260", "extra_permission": []}],
            "list": [1782685702496260],
        }
        self.assertEqual(
            QianchuanOpenApiClient.extract_items(data),
            data["adv_id_list"],
        )

    def test_enterprise_operator_is_expanded_to_qianchuan_accounts(self):
        service = QianchuanOfficialApiService(_CaptureClient())
        with patch.object(
            service,
            "list_authorized_accounts",
            return_value=[
                {
                    "advertiser_id": "1858078536393860",
                    "advertiser_name": "企业操作主体",
                    "role": "PLATFORM_ROLE_ENTERPRISE_BP_OPERATOR",
                    "shop_id": "",
                }
            ],
        ), patch.object(
            service,
            "list_enterprise_advertisers",
            return_value=[
                {
                    "advertiser_id": "1854823495704009",
                    "advertiser_name": "企业下千川账户",
                    "role": "QIANCHUAN",
                }
            ],
        ):
            rows, evidence = service.list_business_accounts()
        self.assertEqual(["1854823495704009"], [row["advertiser_id"] for row in rows])
        self.assertTrue(evidence["complete"])
        self.assertEqual("enterprise", evidence["subjects"][0]["type"])
        self.assertEqual(1, evidence["subjects"][0]["resolved"])

    def test_enterprise_account_list_fields_are_normalized(self):
        account = normalize_account(
            {
                "account_id": 1854823495704009,
                "account_name": "扬-YB-6050-直播-唐造女装旗舰店-F6.0",
                "account_type": "QIANCHUAN",
            }
        )
        self.assertEqual("1854823495704009", account["advertiser_id"])
        self.assertEqual(
            "扬-YB-6050-直播-唐造女装旗舰店-F6.0",
            account["advertiser_name"],
        )
        self.assertEqual("QIANCHUAN", account["role"])

    def test_api_code_40100_is_treated_as_rate_limit(self):
        client = QianchuanOpenApiClient(
            InjectedTokenProvider(AccessTokenBundle("token")),
            sleep=lambda _seconds: None,
        )
        with self.assertRaises(ApiRateLimitError):
            client._raise_api_error(
                {"code": 40100, "message": "System request frequency exceeded"},
                endpoint="/read/",
                http_status=200,
            )

    def test_shop_advertiser_adv_id_is_normalized_as_long_string(self):
        account = normalize_account({"adv_id": 1782685702496260})
        self.assertEqual(account["advertiser_id"], "1782685702496260")
        self.assertEqual(account["aavid"], "1782685702496260")

    def test_four_plan_classes_are_normalized_from_official_fields(self):
        matrix = {
            ("OVERALL_PROJECT", "LIVE_PROM_GOODS"): ("chengfang", "live"),
            ("OVERALL_PROJECT", "VIDEO_PROM_GOODS"): ("chengfang", "product"),
            ("UNI_PROJECT", "LIVE_PROM_GOODS"): ("global", "live"),
            ("UNI_PROJECT", "VIDEO_PROM_GOODS"): ("global", "product"),
        }
        for (adlab_scene, marketing_goal), expected in matrix.items():
            with self.subTest(adlab_scene=adlab_scene, marketing_goal=marketing_goal):
                plan = normalize_plan(
                    {
                        "ad_id": "9876543210123456789",
                        "adlab_scene": adlab_scene,
                        "marketing_goal": marketing_goal,
                        "status": "ENABLE",
                    },
                    advertiser_id="1234567890123456789",
                )
                self.assertEqual((plan["plan_system"], plan["promotion_scene"]), expected)

    def test_numeric_detail_scene_matches_official_list_scene(self):
        matrix = {
            (1, "LIVE_PROM_GOODS"): ("chengfang", "live"),
            (0, "VIDEO_PROM_GOODS"): ("global", "product"),
        }
        for (adlab_scene, marketing_goal), expected in matrix.items():
            with self.subTest(adlab_scene=adlab_scene):
                plan = normalize_plan(
                    {
                        "ad_id": "9876543210123456789",
                        "adlab_scene": adlab_scene,
                        "marketing_goal": marketing_goal,
                        "status": "DELIVERY_OK",
                    },
                    advertiser_id="1234567890123456789",
                )
                self.assertEqual(expected, (plan["plan_system"], plan["promotion_scene"]))

    def test_official_delivery_status_enums_drive_monitor_eligibility(self):
        expected = {
            "DELIVERY_OK": "active",
            "DISABLE": "paused",
            "SYSTEM_DISABLE": "paused",
            "ROI2_DISABLE": "paused",
            "TIME_DONE": "ended",
        }
        for status, normalized in expected.items():
            with self.subTest(status=status):
                plan = normalize_plan(
                    {
                        "ad_id": "9876543210123456789",
                        "adlab_scene": "UNI_PROJECT",
                        "marketing_goal": "VIDEO_PROM_GOODS",
                        "status": status,
                        "opt_status": "ENABLE",
                    },
                    advertiser_id="1234567890123456789",
                )
                self.assertEqual(normalized, plan["platform_status"])

    def test_enabled_live_plan_can_be_monitored_before_broadcast(self):
        plan = normalize_plan(
            {
                "ad_id": "9876543210123456789",
                "adlab_scene": "OVERALL_PROJECT",
                "marketing_goal": "LIVE_PROM_GOODS",
                "status": "LIVE_ROOM_OFF",
                "opt_status": "ENABLE",
            },
            advertiser_id="1234567890123456789",
        )
        self.assertEqual("waiting_live", plan["platform_status"])
        eligibility = target_eligibility(
            promotion_scene="live",
            plan_system="chengfang",
            platform_status=plan["platform_status"],
            verification_state="verified",
            capability={"retarget_supported": True, "stop_supported": True},
        )
        self.assertTrue(eligibility["monitor_eligible"])
        self.assertFalse(eligibility["retarget_eligible"])
        self.assertFalse(eligibility["stop_eligible"])

    def test_disabled_offline_live_plan_stays_ineligible(self):
        plan = normalize_plan(
            {
                "ad_id": "9876543210123456789",
                "adlab_scene": "UNI_PROJECT",
                "marketing_goal": "LIVE_PROM_GOODS",
                "status": "LIVE_ROOM_OFF",
                "opt_status": "DISABLE",
            },
            advertiser_id="1234567890123456789",
        )
        self.assertEqual("paused", plan["platform_status"])

    def test_plan_list_forwards_explicit_chengfang_scene(self):
        client = _CaptureClient()
        service = QianchuanOfficialApiService(client)
        service.list_plans(
            "1854823495704009",
            marketing_goal="LIVE_PROM_GOODS",
            adlab_scene="OVERALL_PROJECT",
        )
        self.assertEqual("OVERALL_PROJECT", client.last_pages[1]["adlab_scene"])

    def test_all_plan_catalog_queries_four_explicit_classes(self):
        service = QianchuanOfficialApiService(_CaptureClient())
        calls = []

        def fake_list(_advertiser_id, *, marketing_goal, adlab_scene, **_kwargs):
            calls.append((marketing_goal, adlab_scene))
            return [], [f"req-{len(calls)}"]

        with patch.object(service, "list_plans", side_effect=fake_list):
            rows, evidence = service.list_all_plans("1854823495704009")

        self.assertEqual([], rows)
        self.assertEqual(
            {
                ("LIVE_PROM_GOODS", "OVERALL_PROJECT"),
                ("VIDEO_PROM_GOODS", "OVERALL_PROJECT"),
                ("LIVE_PROM_GOODS", "UNI_PROJECT"),
                ("VIDEO_PROM_GOODS", "UNI_PROJECT"),
            },
            set(calls),
        )
        self.assertEqual(
            {
                "chengfang_live",
                "chengfang_product",
                "global_live",
                "global_product",
            },
            set(evidence["classes"]),
        )
        self.assertTrue(evidence["complete"])

    def test_live_plan_list_ad_info_wrapper_is_normalized(self):
        plan = normalize_plan(
            {
                "ad_info": {
                    "id": 1804998056156307,
                    "name": "直播全域计划",
                    "adlab_scene": "UNI_PROJECT",
                    "marketing_goal": "LIVE_PROM_GOODS",
                    "status": "ENABLE",
                },
                "product_info": [{"id": 999}],
                "room_info": [{"id": 888}],
            },
            advertiser_id="1782685702496260",
        )
        self.assertEqual("1804998056156307", plan["ad_id"])
        self.assertEqual("直播全域计划", plan["plan_name"])
        self.assertEqual("global", plan["plan_system"])
        self.assertEqual("live", plan["promotion_scene"])
        self.assertEqual("active", plan["platform_status"])

    def test_official_api_capability_is_valid_for_batch_retarget(self):
        verified_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        capability = {
            "source": "qianchuan_open_api",
            "retarget_execute": True,
            "retarget_scene": "product",
            "retarget_plan_system": "chengfang",
            "retarget_probe_version": "official-open-api-v1",
            "retarget_verified_at": verified_at,
            "retarget_target_uid": "target-1",
            "retarget_aavid": "123",
            "retarget_ad_id": "456",
            "retarget_batch_execute": True,
            "retarget_batch_probe_version": "official-open-api-v1",
            "retarget_batch_verified_at": verified_at,
        }
        ok, reason = check_target_capability(
            {
                "target_uid": "target-1",
                "aadvid": "123",
                "ad_id": "456",
                "capability_json": json.dumps(capability),
            },
            action="retarget",
            promotion_scene="product",
            plan_system="chengfang",
            require_batch=True,
        )
        self.assertTrue(ok, reason)

    def test_long_ids_are_strings_in_normalized_models(self):
        plan = normalize_plan(
            {
                "ad_id": 9876543210123456789,
                "marketing_goal": "LIVE_PROM_GOODS",
                "adlab_scene": "OVERALL_PROJECT",
                "status": "ENABLE",
            },
            advertiser_id=1234567890123456789,
        )
        self.assertEqual(plan["aavid"], "1234567890123456789")
        self.assertEqual(plan["ad_id"], "9876543210123456789")
        self.assertEqual(plan["plan_system"], "chengfang")
        self.assertEqual(plan["promotion_scene"], "live")

    def test_object_arrays_normalize_material_and_product_ids(self):
        material = normalize_material(
            {
                "material_id": "90071992547409931",
                "material_type": "VIDEO",
                "products": [{"product_id": 90071992547409933}],
            }
        )
        task = normalize_control_task(
            {
                "task_id": 90071992547409935,
                "scene": "MATERIAL_ADD_BUDGET",
                "materials": [
                    {"material_id": 90071992547409931},
                    {"material_id": "90071992547409932"},
                ],
            }
        )
        self.assertEqual(material["product_ids"], ["90071992547409933"])
        self.assertEqual(
            task["material_ids"],
            ["90071992547409931", "90071992547409932"],
        )

    def test_pagination_rejects_repeated_pages(self):
        page = ApiResponse(
            data={"list": [{"id": "1"}], "page_info": {"has_more": True}},
            raw={},
            request_id="r1",
        )
        client = _PagedClient([page, page])
        with self.assertRaises(ApiRequestError):
            client.get_all_pages("/open_api/v1.0/test/", {}, page_size=1)

    def test_parallel_pagination_preserves_page_order_and_completeness(self):
        class _ParallelClient(QianchuanOpenApiClient):
            def __init__(self):
                super().__init__(InjectedTokenProvider(AccessTokenBundle("token")))

            def get(self, endpoint, query=None, *, advertiser_id=""):
                page = int((query or {}).get("page") or 1)
                return ApiResponse(
                    data={
                        "list": [{"id": str(page)}],
                        "page_info": {"total_page": 4, "total_number": 4},
                    },
                    raw={},
                    request_id=f"r{page}",
                )

        rows, request_ids = _ParallelClient().get_all_pages(
            "/open_api/v1.0/test/",
            {},
            page_size=1,
            parallel_workers=3,
        )
        self.assertEqual(["1", "2", "3", "4"], [row["id"] for row in rows])
        self.assertEqual(["r1", "r2", "r3", "r4"], request_ids)

    def test_post_network_failure_is_not_retried(self):
        client = QianchuanOpenApiClient(
            InjectedTokenProvider(AccessTokenBundle("token")),
            max_get_attempts=4,
            sleep=lambda _: None,
        )
        with patch(
            "services.qianchuan_open_api.client.urlopen",
            side_effect=URLError("offline"),
        ) as mocked:
            with self.assertRaises(ApiWriteOutcomeUnknown):
                client.post(
                    "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/create/",
                    {"advertiser_id": 123},
                    advertiser_id="123",
                )
        self.assertEqual(mocked.call_count, 1)

    def test_get_transient_business_error_is_retried(self):
        class _Response:
            status = 200
            headers = {}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

        client = QianchuanOpenApiClient(
            InjectedTokenProvider(AccessTokenBundle("token")),
            max_get_attempts=3,
            sleep=lambda _: None,
        )
        responses = [
            _Response({"code": 40000, "message": "系统开小差，请稍后重试一下"}),
            _Response({"code": 0, "message": "OK", "data": {"id": "1"}}),
        ]
        with patch(
            "services.qianchuan_open_api.client.urlopen",
            side_effect=responses,
        ) as mocked:
            response = client.get("/open_api/v1.0/test/")
        self.assertEqual(response.data["id"], "1")
        self.assertEqual(mocked.call_count, 2)

    def test_post_transient_business_error_is_not_retried(self):
        class _Response:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps(
                    {"code": 40000, "message": "系统开小差，请稍后重试一下"},
                    ensure_ascii=False,
                ).encode("utf-8")

        client = QianchuanOpenApiClient(
            InjectedTokenProvider(AccessTokenBundle("token")),
            max_get_attempts=4,
            sleep=lambda _: None,
        )
        with patch(
            "services.qianchuan_open_api.client.urlopen",
            return_value=_Response(),
        ) as mocked:
            with self.assertRaises(ApiRequestError):
                client.post("/open_api/v1.0/test/", {"advertiser_id": 1})
        self.assertEqual(mocked.call_count, 1)

    def test_real_writes_are_disabled_by_default(self):
        service = QianchuanOfficialApiService(_CaptureClient(), allow_writes=False)
        with self.assertRaises(OfficialApiWriteDisabled):
            service.update_control_status("123", ["456"], action="PAUSE")

    def test_create_uses_exact_integer_json_ids(self):
        client = _CaptureClient()
        service = QianchuanOfficialApiService(client, allow_writes=True)
        response = service.create_material_control_task(
            "1234567890123456789",
            ad_id="9876543210123456789",
            marketing_goal="VIDEO_PROM_GOODS",
            name="test",
            budget=Decimal("100"),
            duration=Decimal("24"),
            material_ids=["90071992547409931", "90071992547409932"],
        )
        body = client.posts[0][1]
        self.assertIsInstance(body["advertiser_id"], int)
        self.assertEqual(body["advertiser_id"], 1234567890123456789)
        self.assertEqual(body["ad_id"], 9876543210123456789)
        self.assertEqual(body["material_ids"][0], 90071992547409931)
        self.assertNotIn('"1234567890123456789"', json.dumps(body))
        self.assertEqual(str(response.data["task_id"]), "9876543210123456789")

    def test_overlapping_groups_are_not_treated_as_duplicates(self):
        client = _CaptureClient(
            tasks=[
                {
                    "task_id": "88",
                    "material_ids": ["1", "2", "3"],
                    "budget": "100",
                    "duration": "24",
                    "status": "ENABLE",
                }
            ]
        )
        service = QianchuanOfficialApiService(client, allow_writes=True)
        duplicate = service.find_duplicate_control_task(
            "123",
            ad_id="456",
            marketing_goal="VIDEO_PROM_GOODS",
            task_name="new-execution",
            budget="100",
            duration="24",
            material_ids=["1", "2"],
        )
        self.assertIsNone(duplicate)

    def test_closed_identical_task_does_not_block_new_retarget(self):
        for status in (
            "FROZEN",
            "PAUSE",
            "PAUSED",
            "DISABLE",
            "DISABLED",
            "FINISHED",
        ):
            with self.subTest(status=status):
                client = _CaptureClient(tasks=[{
                    "task_id": "88",
                    "material_ids": ["1"],
                    "budget": "100",
                    "duration": "24",
                    "status": status,
                }])
                service = QianchuanOfficialApiService(client, allow_writes=True)
                duplicate = service.find_duplicate_control_task(
                    "123",
                    ad_id="456",
                    marketing_goal="VIDEO_PROM_GOODS",
                    task_name="same-execution",
                    budget="100",
                    duration="24",
                    material_ids=["1"],
                )
                self.assertIsNone(duplicate)

    def test_processing_identical_business_params_from_another_execution_are_allowed(self):
        client = _CaptureClient(tasks=[{
            "task_id": "88",
            "task_name": "older-execution",
            "material_ids": ["1"],
            "budget": "100",
            "duration": "24",
            "status": "PROCESSING",
        }])
        service = QianchuanOfficialApiService(client, allow_writes=True)
        duplicate = service.find_duplicate_control_task(
            "123",
            ad_id="456",
            marketing_goal="VIDEO_PROM_GOODS",
            task_name="new-execution",
            budget="100",
            duration="24",
            material_ids=["1"],
        )
        self.assertIsNone(duplicate)

    def test_same_execution_name_and_params_are_reconciled_without_second_post(self):
        client = _CaptureClient(tasks=[{
            "task_id": "88",
            "task_name": "same-execution",
            "material_ids": ["1"],
            "budget": "100",
            "duration": "24",
            "status": "PROCESSING",
        }])
        service = QianchuanOfficialApiService(client, allow_writes=True)

        response = service.create_material_control_task(
            "123",
            ad_id="456",
            marketing_goal="VIDEO_PROM_GOODS",
            name="same-execution",
            budget="100",
            duration="24",
            material_ids=["1"],
        )

        self.assertEqual("88", str(response.data["task_id"]))
        self.assertTrue(response.raw["reconciled_existing"])
        self.assertEqual([], client.posts)

    def test_same_execution_is_reconciled_even_when_existing_task_is_closed(self):
        client = _CaptureClient(tasks=[{
            "task_id": "88",
            "task_name": "same-execution",
            "material_ids": ["1"],
            "budget": "100",
            "duration": "24",
            "status": "DISABLE",
        }])
        service = QianchuanOfficialApiService(client, allow_writes=True)

        response = service.create_material_control_task(
            "123",
            ad_id="456",
            marketing_goal="VIDEO_PROM_GOODS",
            name="same-execution",
            budget="100",
            duration="24",
            material_ids=["1"],
        )

        self.assertEqual("88", str(response.data["task_id"]))
        self.assertEqual([], client.posts)

    def test_delete_control_action_is_forbidden(self):
        service = QianchuanOfficialApiService(_CaptureClient(), allow_writes=True)
        with self.assertRaises(ValueError):
            service.update_control_status("123", ["456"], action="DELETE")

    def test_stable_material_set_rejects_non_digit_ids(self):
        with self.assertRaises(ValueError):
            stable_material_set(["123", "not-an-id"])


if __name__ == "__main__":
    unittest.main()
