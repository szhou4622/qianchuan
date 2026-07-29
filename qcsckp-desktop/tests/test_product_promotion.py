# -*- coding: utf-8 -*-
import asyncio
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from api.promotion_targets import (
    detect_confirmed_detail_scene,
    detect_plan_system,
    detect_promotion_scene,
    extract_plan_name,
    extract_target_ids,
    list_promotion_targets,
    list_target_products,
    make_target_uid,
    normalize_plan_system,
    patch_target_sync_state,
    replace_material_product_links,
    sanitize_target_url,
    set_promotion_target_enabled,
    update_target_sync_state,
    upsert_products,
    upsert_promotion_target,
)
from api.views import Api
from api.rule_regulation_config import validate_rule_regulation_config
from api.rule_retargeting_config import validate_strategy_target_compatibility
from services.fetcher import QianChuanFetcher, build_qianchuan_url_by_params
from services.product_rule_engine import (
    aggregate_product_rows,
    evaluate_product_strategy,
    select_product_candidates,
)
from services.promotion_readonly_probe import (
    PromotionReadOnlyProbe,
    summarize_json,
    summarize_page,
)
from services.product_scene_adapter import (
    extract_product_scene_snapshot,
    extract_safe_query_identifiers,
    merge_product_scene_snapshots,
    scope_product_scene_snapshot,
    validate_exact_product_plan_payload,
)
from services.retargeting_rule_runner import (
    rate_limit_record_success,
    rate_limit_should_skip,
    retarget_method_is_supported_for_scene,
)
from services.retargeting_service import (
    QianChuanRetargetingService,
    RETARGET_PROBE_VERSION,
    RetargetingRunResult,
    retarget_capability_matches,
)
from services.regulation_rule_runner import (
    _target_assist_sync_ready,
    has_completed_stop,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


TEST_CAPABILITY_VERIFIED_AT = datetime.now().isoformat(timespec="seconds")


def trigger(metric, op, value):
    return {
        "group_combine": "or",
        "groups": [
            {
                "join": "and",
                "conditions": [{"metric": metric, "op": op, "value": value}],
            }
        ],
    }


class ProductPromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "product.db")
        init_sqlite_schema(database=self.db_path)
        self.db = SQLiteStore(database=self.db_path)
        self.init_patch = patch("api.promotion_targets.init_sqlite_schema")
        self.init_patch.start()

    def tearDown(self):
        self.init_patch.stop()
        self.tmp.cleanup()

    def _target(self, index, *, enabled=True, scene="product"):
        return upsert_promotion_target(
            {
                "aavid": "10001",
                "ad_id": str(20000 + index),
                "plan_name": f"计划{index}",
                "promotion_scene": scene,
                "plan_system": "global",
                "enabled": enabled,
                "page_url": (
                    "https://qianchuan.jinritemai.com/uni-prom/detail"
                    f"?aavid=10001&adId={20000 + index}&token=secret"
                ),
            },
            db=self.db,
        )

    def test_product_probe_uses_latest_observed_plan_not_dict_insertion_order(self):
        probe = PromotionReadOnlyProbe.__new__(PromotionReadOnlyProbe)
        probe._apis = {
            "/ad/api/creation/v1/ad/ad-detail-plus": {
                "path": "/ad/api/creation/v1/ad/ad-detail-plus",
                "observed_at": "2026-07-29 10:04:43",
                "product_snapshot": {
                    "plan": {
                        "ad_id": "new-plan",
                        "plan_name": "新计划",
                    }
                },
            },
            "/ad/api/creation/v1/shop-prom/get-config": {
                "path": "/ad/api/creation/v1/shop-prom/get-config",
                "observed_at": "2026-07-29 10:04:31",
                "product_snapshot": {
                    "plan": {
                        "ad_id": "old-plan",
                        "plan_name": "旧计划",
                    }
                },
            },
        }

        snapshot = probe.latest_product_snapshot()

        self.assertEqual("new-plan", snapshot["plan"]["ad_id"])
        self.assertEqual("新计划", snapshot["plan"]["plan_name"])

    def test_account_snapshot_is_scoped_to_selected_plan(self):
        snapshot = {
            "plan": {"ad_id": "20002", "plan_name": "计划2"},
            "products": [
                {"product_id": "p1", "product_name": "商品1"},
                {"product_id": "p2", "product_name": "商品2"},
            ],
            "ad_rows": [
                {"ad_id": "20001", "product_ids": ["p1"]},
                {"ad_id": "20002", "product_ids": ["p2"]},
            ],
            "materials": [
                {
                    "ad_id": "20001",
                    "material_id": "m1",
                    "product_ids": ["p1"],
                },
                {
                    "ad_id": "20002",
                    "material_id": "m2",
                    "product_ids": ["p2", "p1"],
                },
            ],
        }

        scoped = scope_product_scene_snapshot(snapshot, ad_id="20002")

        self.assertEqual(["p2"], [item["product_id"] for item in scoped["products"]])
        self.assertEqual(["m2"], [item["material_id"] for item in scoped["materials"]])
        self.assertEqual(["p2"], scoped["materials"][0]["product_ids"])

    def test_exact_product_plan_payload_blocks_default_or_paused_plan(self):
        active = {
            "status_code": 0,
            "data": {
                "adDetailInfo": {
                    "id": "20001",
                    "adDeliveryName": "投放中",
                    "adDeliveryType": 0,
                }
            },
        }
        self.assertIsNone(
            validate_exact_product_plan_payload(
                active,
                expected_ad_id="20001",
            )
        )
        self.assertIn(
            "计划不匹配",
            validate_exact_product_plan_payload(
                active,
                expected_ad_id="20002",
            ),
        )
        paused = {
            "status_code": 0,
            "data": {
                "adDetailInfo": {
                    "id": "20001",
                    "adDeliveryName": "已暂停",
                    "adDeliveryType": 5,
                }
            },
        }
        self.assertIn(
            "非投放中",
            validate_exact_product_plan_payload(
                paused,
                expected_ad_id="20001",
            ),
        )

    def test_product_retarget_form_accepts_only_supported_volume_fields(self):
        volume = {
            "method": "volume",
            "volume": {
                "total_budget_yuan": 100,
                "duration_hours": 0.5,
            },
        }
        self.assertIsNone(
            QianChuanRetargetingService._validate_retargeting_payload(
                volume,
                "product",
            )
        )
        self.assertIn(
            "不得低于100元",
            QianChuanRetargetingService._validate_retargeting_payload(
                {
                    "method": "volume",
                    "volume": {
                        "total_budget_yuan": 99,
                        "duration_hours": 1,
                    },
                },
                "product",
            ),
        )
        self.assertIn(
            "暂不支持控成本追投",
            QianChuanRetargetingService._validate_retargeting_payload(
                {
                    "method": "cost_control",
                    "cost_control": {
                        "optimization_goal": "net_roi",
                        "net_roi": {
                            "daily_budget_yuan": 100,
                            "net_roi_target": 3.4,
                        },
                    },
                },
                "product",
            ),
        )

    def test_product_batch_has_twenty_material_limit(self):
        service = QianChuanRetargetingService.from_rule_file_dict(
            {"browser_headless": True}
        )
        result = asyncio.run(
            service.run(
                aavid=10001,
                ad_id=30001,
                material_id="m1",
                material_ids=[f"m{index}" for index in range(21)],
                retargeting={
                    "method": "volume",
                    "volume": {
                        "total_budget_yuan": 100,
                        "duration_hours": 1,
                    },
                },
                promotion_scene="product",
                plan_system="global",
            )
        )
        self.assertFalse(result.success)
        self.assertEqual("validate", result.step)
        self.assertIn("最多支持20条素材", result.message)

    def test_retarget_execution_rejects_unknown_plan_system_before_browser(self):
        service = QianChuanRetargetingService.from_rule_file_dict(
            {"browser_headless": True}
        )
        service._ensure_browser = AsyncMock()
        payload = {
            "method": "volume",
            "volume": {
                "total_budget_yuan": 100,
                "duration_hours": 1,
            },
        }
        result = asyncio.run(
            service.run(
                aavid=10001,
                ad_id=30001,
                material_id="m1",
                retargeting=payload,
                promotion_scene="product",
                plan_system="unknown",
            )
        )
        self.assertFalse(result.success)
        self.assertEqual("validate_plan_system", result.step)
        service._ensure_browser.assert_not_awaited()

        result = asyncio.run(
            service.run_prepare_for_manual_submit(
                aavid=10001,
                ad_id=30001,
                material_id="m1",
                retargeting=payload,
                promotion_scene="product",
                plan_system="unknown",
            )
        )
        self.assertFalse(result.success)
        self.assertEqual("validate_plan_system", result.step)
        service._ensure_browser.assert_not_awaited()

    def test_live_batch_reaches_browser_adapter_instead_of_legacy_rejection(self):
        service = QianChuanRetargetingService.from_rule_file_dict(
            {"browser_headless": True}
        )

        async def no_browser():
            return None

        service._ensure_browser = no_browser
        result = asyncio.run(
            service.run(
                aavid=10001,
                ad_id=30001,
                material_id="m1",
                material_ids=["m1", "m2"],
                retargeting={
                    "method": "volume",
                    "volume": {
                        "total_budget_yuan": 100,
                        "duration_hours": 1,
                    },
                },
                promotion_scene="live",
                plan_system="global",
            )
        )
        self.assertFalse(result.success)
        self.assertEqual("browser", result.step)
        self.assertNotIn("尚不支持", result.message)

    def test_product_capability_probe_never_submits(self):
        service = QianChuanRetargetingService.from_rule_file_dict(
            {"browser_headless": True}
        )

        async def ensure_browser():
            service.page = object()

        service._ensure_browser = ensure_browser
        service._attach_popup_switcher = AsyncMock(return_value=[])
        service._detach_popup_switcher = lambda _handlers: None
        service._open_product_retarget_dialog = AsyncMock(return_value=None)
        service._select_product_materials = AsyncMock(return_value=None)
        service._probe_product_volume_form_structure = AsyncMock(
            return_value=None
        )
        service._click_submit_and_wait_assist = AsyncMock()
        service.close = AsyncMock()

        with patch(
            "services.retargeting_service.goto_and_confirm_product_target",
            new=AsyncMock(return_value=None),
        ):
            result = asyncio.run(
                service.probe_product_retarget_capability(
                    aavid=10001,
                    ad_id=20001,
                    material_id="m1",
                    target_uid="target-1",
                    source_url=(
                        "https://qianchuan.jinritemai.com/uni-prom"
                        "?aavid=10001&adId=20001"
                    ),
                    plan_system="global",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual("capability_probe", result.step)
        self.assertIn("未点击提交", result.message)
        service._open_product_retarget_dialog.assert_awaited_once()
        service._select_product_materials.assert_awaited_once_with(
            service.page,
            ["m1"],
        )
        service._probe_product_volume_form_structure.assert_awaited_once_with(
            service.page,
        )
        service._click_submit_and_wait_assist.assert_not_awaited()
        service.close.assert_awaited_once()

    def test_product_capability_probe_rejects_incomplete_form_without_submit(self):
        service = QianChuanRetargetingService.from_rule_file_dict(
            {"browser_headless": True}
        )

        async def ensure_browser():
            service.page = object()

        service._ensure_browser = ensure_browser
        service._attach_popup_switcher = AsyncMock(return_value=[])
        service._detach_popup_switcher = lambda _handlers: None
        service._open_product_retarget_dialog = AsyncMock(return_value=None)
        service._select_product_materials = AsyncMock(return_value=None)
        service._probe_product_volume_form_structure = AsyncMock(
            return_value="商品追投表单未找到输入框：调控时长"
        )
        service._click_submit_and_wait_assist = AsyncMock()
        service.close = AsyncMock()

        with patch(
            "services.retargeting_service.goto_and_confirm_product_target",
            new=AsyncMock(return_value=None),
        ):
            result = asyncio.run(
                service.probe_product_retarget_capability(
                    aavid=10001,
                    ad_id=20001,
                    material_id="m1",
                    target_uid="target-1",
                    plan_system="global",
                )
            )

        self.assertFalse(result.success)
        self.assertIn("调控时长", result.message)
        service._click_submit_and_wait_assist.assert_not_awaited()

    def test_live_chengfang_capability_probe_is_scoped_and_never_submits(self):
        service = QianChuanRetargetingService.from_rule_file_dict(
            {"browser_headless": True}
        )

        class Page:
            url = ""

            async def goto(self, url, **_kwargs):
                self.url = url

        async def ensure_browser():
            service.page = Page()

        service._ensure_browser = ensure_browser
        service._attach_popup_switcher = AsyncMock(return_value=[])
        service._detach_popup_switcher = lambda _handlers: None
        service._switch_to_video_tab = AsyncMock(return_value=None)
        service._search_material_and_open_dialog = AsyncMock(return_value=None)
        service._probe_live_retarget_form_structure = AsyncMock(
            return_value=None
        )
        service._click_submit_and_wait_assist = AsyncMock()
        service.close = AsyncMock()

        with patch(
            "services.retargeting_service.asyncio.sleep",
            new=AsyncMock(),
        ), patch(
            "services.retargeting_service.confirm_live_page_plan_system",
            new=AsyncMock(return_value=""),
        ):
            result = asyncio.run(
                service.probe_product_retarget_capability(
                    aavid=10001,
                    ad_id=20001,
                    material_id="m1",
                    target_uid="target-live-chengfang",
                    promotion_scene="live",
                    plan_system="chengfang",
                )
            )

        self.assertTrue(result.success)
        self.assertIn("直播放量及控成本", result.message)
        service._switch_to_video_tab.assert_awaited_once_with("live")
        service._search_material_and_open_dialog.assert_awaited_once_with(
            service.page,
            "m1",
        )
        service._probe_live_retarget_form_structure.assert_awaited_once_with(
            service.page,
        )
        service._click_submit_and_wait_assist.assert_not_awaited()
        service.close.assert_awaited_once()

    def test_live_capability_structure_checks_all_supported_fields(self):
        service = QianChuanRetargetingService.from_rule_file_dict(
            {"browser_headless": True}
        )

        class Element:
            async def is_visible(self):
                return True

            async def is_disabled(self):
                return False

        class Page:
            def __init__(self):
                self.selectors = []

            async def query_selector(self, selector):
                self.selectors.append(selector)
                return Element()

        page = Page()
        service._click_radio_option = AsyncMock(return_value=None)
        service._find_visible_button = AsyncMock(return_value=object())
        service._click_submit_and_wait_assist = AsyncMock()
        error = asyncio.run(
            service._probe_live_retarget_form_structure(page)
        )
        self.assertIsNone(error)
        selector_text = "\n".join(page.selectors)
        for label in (
            "调控总预算",
            "调控时长",
            "调控日预算",
            "净成交ROI目标",
            "我的出价",
            "任务名称",
        ):
            self.assertIn(label, selector_text)
        self.assertEqual(4, service._click_radio_option.await_count)
        service._find_visible_button.assert_awaited()
        service._click_submit_and_wait_assist.assert_not_awaited()

    def test_probe_api_records_live_chengfang_scoped_evidence(self):
        target = {
            "target_uid": "target-live-chengfang",
            "aadvid": "10001",
            "ad_id": "20001",
            "plan_name": "乘方直播",
            "promotion_scene": "live",
            "plan_system": "chengfang",
            "enabled": 1,
            "last_status": "ok",
            "capability_json": "{}",
            "sanitized_page_url": (
                "https://qianchuan.jinritemai.com/uni-prom/detail"
                "?aavid=10001&adId=20001"
            ),
        }

        class FakeDb:
            @contextmanager
            def transaction(self):
                yield object()

            def select_one(self, table, **_kwargs):
                if table == "promotion_target":
                    return dict(target)
                if table == "pmc_promotion_material":
                    return {"material_id": "m1"}
                return None

            def update(self, table, values, **_kwargs):
                if table == "promotion_target":
                    target.update(values)
                return 1

            def execute(self, sql, *_args, **_kwargs):
                if str(sql).strip().upper().startswith("BEGIN"):
                    return []
                return [
                    {"material_id": "m1"},
                    {"material_id": "m2"},
                ]

        captured = {}

        class FakeService:
            async def probe_product_retarget_capability(self, **kwargs):
                captured.update(kwargs)
                return RetargetingRunResult(
                    success=True,
                    message="验证通过",
                    step="capability_probe",
                )

        api = Api.__new__(Api)
        api.db = FakeDb()
        with (
            patch(
                "services.retargeting_service."
                "QianChuanRetargetingService.from_rule_file_dict",
                return_value=FakeService(),
            ),
            patch(
                "api.rule_retargeting_config.load_rule_retargeting_config",
                return_value={"browser_headless": True},
            ),
        ):
            result = api.probePromotionTargetRetargetCapability(
                "target-live-chengfang"
            )

        self.assertTrue(result["success"])
        self.assertEqual("live", captured["promotion_scene"])
        self.assertEqual("chengfang", captured["plan_system"])
        capability = json.loads(target["capability_json"])
        self.assertTrue(capability["retarget_execute"])
        self.assertEqual("live", capability["retarget_scene"])
        self.assertEqual("chengfang", capability["retarget_plan_system"])
        self.assertEqual(
            RETARGET_PROBE_VERSION,
            capability["retarget_probe_version"],
        )
        self.assertTrue(capability["retarget_verified_at"])
        self.assertEqual("target-live-chengfang", capability["retarget_target_uid"])
        self.assertEqual("10001", capability["retarget_aavid"])
        self.assertEqual("20001", capability["retarget_ad_id"])
        self.assertTrue(capability["retarget_batch_execute"])

    def test_product_capability_evidence_is_scoped_and_versioned(self):
        evidence = {
            "retarget_execute": True,
            "retarget_scene": "product",
            "retarget_plan_system": "global",
            "retarget_probe_version": RETARGET_PROBE_VERSION,
            "retarget_verified_at": TEST_CAPABILITY_VERIFIED_AT,
            "retarget_target_uid": "target-product",
            "retarget_aavid": "10001",
            "retarget_ad_id": "20001",
        }
        self.assertTrue(
            retarget_capability_matches(
                evidence,
                promotion_scene="product",
                plan_system="global",
            )
        )
        self.assertFalse(
            retarget_capability_matches(
                evidence,
                promotion_scene="live",
                plan_system="global",
            )
        )
        self.assertFalse(
            retarget_capability_matches(
                {**evidence, "retarget_probe_version": "old-version"},
                promotion_scene="product",
                plan_system="global",
            )
        )
        self.assertFalse(
            retarget_capability_matches(
                {"retarget_execute": True},
                promotion_scene="product",
                plan_system="global",
            )
        )
        self.assertFalse(
            retarget_capability_matches(
                {"retarget_execute": True},
                promotion_scene="product",
                plan_system="chengfang",
            )
        )

    def test_product_cost_control_config_is_rejected_but_live_is_allowed(self):
        config = {
            "enabled": True,
            "strategies": [
                {
                    "title": "商品策略",
                    "target_uid": "product-target",
                    "retargeting": {"method": "cost_control"},
                }
            ],
        }
        ok, message = validate_strategy_target_compatibility(
            config,
            {
                "product-target": {
                    "promotion_scene": "product",
                    "enabled": True,
                }
            },
        )
        self.assertFalse(ok)
        self.assertIn("推商品当前仅支持放量追投", message)

        ok, message = validate_strategy_target_compatibility(
            config,
            {
                "product-target": {
                    "promotion_scene": "live",
                    "enabled": True,
                }
            },
        )
        self.assertTrue(ok)
        self.assertEqual("", message)
        self.assertFalse(
            retarget_method_is_supported_for_scene(
                "product",
                {"method": "cost_control"},
            )
        )
        self.assertTrue(
            retarget_method_is_supported_for_scene(
                "live",
                {"method": "cost_control"},
            )
        )

    def test_product_material_selection_passes_wait_argument_by_keyword(self):
        service = QianChuanRetargetingService.from_rule_file_dict(
            {"browser_headless": True}
        )
        wait_arguments = []

        class Locator:
            def __init__(self, kind, page):
                self.kind = kind
                self.page = page

            @property
            def first(self):
                return self

            @property
            def last(self):
                return self

            def nth(self, _index):
                return self

            async def count(self):
                return 1

            async def wait_for(self, **_kwargs):
                return None

            async def fill(self, _value):
                return None

            async def press(self, _key):
                return None

            async def is_visible(self):
                return True

            async def get_attribute(self, _name):
                if self.kind == "label":
                    return "ovui-checkbox ovui-checkbox--md"
                return ""

            async def is_checked(self):
                return self.page.checked

            async def click(self, **_kwargs):
                if self.kind == "label":
                    self.page.checked = True

            async def evaluate(self, _script):
                self.page.checked = True

            async def element_handle(self):
                return object()

            def locator(self, selector):
                if self.kind == "search":
                    return self.page.modal
                if self.kind == "material":
                    return self.page.row
                if self.kind == "row":
                    if selector == 'input[type="checkbox"]':
                        return self.page.checkbox
                    if selector == "label.ovui-checkbox":
                        return self.page.label
                if self.kind == "checkbox":
                    return self.page.label
                return Locator("generic", self.page)

            def get_by_role(self, _role, **_kwargs):
                return Locator("confirm", self.page)

        class Page:
            def __init__(self):
                self.checked = False
                self.modal = Locator("modal", self)
                self.search = Locator("search", self)
                self.row = Locator("row", self)
                self.checkbox = Locator("checkbox", self)
                self.label = Locator("label", self)

            def locator(self, _selector):
                return self.search

            def get_by_text(self, text, **_kwargs):
                if str(text).startswith("素材ID:"):
                    return Locator("material", self)
                return Locator("generic", self)

            async def wait_for_timeout(self, _timeout):
                return None

            async def wait_for_function(
                self,
                _expression,
                *,
                arg=None,
                timeout=None,
            ):
                wait_arguments.append((arg, timeout))
                return True

        page = Page()
        service._click_last_visible = AsyncMock(return_value=True)
        error = asyncio.run(
            service._select_product_materials(page, ["material-1"])
        )

        self.assertIsNone(error)
        self.assertEqual([1, 1], [item[0] for item in wait_arguments])

    def test_product_material_request_accepts_req_from_in_post_body(self):
        fetcher = QianChuanFetcher()
        fetcher._current_aadvid = "10001"
        fetcher._current_adid = "20001"
        url = (
            "https://qianchuan.jinritemai.com/ad/api/pmc/v1/"
            "uni-promotion/material/list-required?aavid=10001"
        )
        matching_body = {
            "reqFrom": "uni-prom-creative-tab-list",
            "Filters": {
                "Conditions": [
                    {"Field": "ad_id", "Values": ["20001"]},
                ]
            },
        }
        self.assertTrue(
            fetcher._is_target_api(
                url,
                matching_body,
            )
        )
        self.assertTrue(
            fetcher._is_target_api(
                url + "&reqFrom=uni-prom-creative-tab-list&adId=20001",
            )
        )
        self.assertFalse(
            fetcher._is_target_api(
                url,
                {
                    **matching_body,
                    "reqFrom": "other",
                },
            )
        )
        self.assertFalse(
            fetcher._is_target_api(
                url.replace("aavid=10001", "aavid=10002"),
                matching_body,
            )
        )

    def test_material_and_assist_requests_reject_other_plan_in_same_account(self):
        fetcher = QianChuanFetcher()
        fetcher._current_aadvid = "10001"
        fetcher._current_adid = "20001"
        material_url = (
            "https://qianchuan.jinritemai.com/ad/api/pmc/v1/"
            "uni-promotion/material/list-required?aavid=10001"
        )
        assist_url = (
            "https://qianchuan.jinritemai.com/ad/api/pmc/v1/"
            "uni-promotion/ad/list-required?aavid=10001"
        )

        def body(ad_id):
            return {
                "reqFrom": "uni-prom-creative-tab-list",
                "Filters": {
                    "Conditions": [
                        {"Field": "ad_id", "Values": [ad_id]},
                    ]
                },
            }

        self.assertTrue(fetcher._is_target_api(material_url, body("20001")))
        self.assertFalse(fetcher._is_target_api(material_url, body("20002")))
        self.assertFalse(
            fetcher._is_target_api(
                material_url,
                {"reqFrom": "uni-prom-creative-tab-list"},
            )
        )
        self.assertTrue(
            fetcher._is_target_assist_api(assist_url, body("20001"))
        )
        self.assertFalse(
            fetcher._is_target_assist_api(assist_url, body("20002"))
        )
        self.assertFalse(fetcher._is_target_assist_api(assist_url))

    def test_response_handlers_ignore_same_account_other_plan(self):
        fetcher = QianChuanFetcher()
        fetcher._current_aadvid = "10001"
        fetcher._current_adid = "20001"
        other_plan_body = {
            "reqFrom": "uni-prom-creative-tab-list",
            "Filters": {
                "Conditions": [
                    {"Field": "ad_id", "Values": ["20002"]},
                ]
            },
        }

        class Request:
            post_data = json.dumps(other_plan_body)

        class Response:
            def __init__(self, url):
                self.url = url
                self.request = Request()
                self.json = AsyncMock(return_value={"status_code": 0})

        fetcher._handle_material_response = AsyncMock()
        material_response = Response(
            "https://qianchuan.jinritemai.com/ad/api/pmc/v1/"
            "uni-promotion/material/list-required?aavid=10001"
        )
        asyncio.run(fetcher._on_response(material_response))
        fetcher._handle_material_response.assert_not_awaited()
        material_response.json.assert_not_awaited()

        fetcher._handle_assist_response = AsyncMock()
        assist_response = Response(
            "https://qianchuan.jinritemai.com/ad/api/pmc/v1/"
            "uni-promotion/ad/list-required?aavid=10001"
        )
        asyncio.run(fetcher._on_response_assist(assist_response))
        fetcher._handle_assist_response.assert_not_awaited()
        assist_response.json.assert_not_awaited()

    def test_assist_sync_marks_seen_only_after_valid_business_response(self):
        fetcher = QianChuanFetcher()
        fetcher._current_aadvid = "10001"
        fetcher._current_adid = "20001"
        fetcher._persist_assist_api_response_to_db = AsyncMock()
        body = {
            "reqFrom": "uni-prom-creative-tab-list",
            "Filters": {
                "Conditions": [
                    {"Field": "ad_id", "Values": ["20001"]},
                ]
            },
        }

        class Request:
            post_data = json.dumps(body)

        class Response:
            url = (
                "https://qianchuan.jinritemai.com/ad/api/pmc/v1/"
                "uni-promotion/ad/list-required?aavid=10001"
            )
            request = Request()

            def __init__(self, result):
                self.json = AsyncMock(return_value=result)

        asyncio.run(
            fetcher._on_response_assist(
                Response({"status_code": 500, "message": "failed"})
            )
        )
        self.assertFalse(fetcher._assist_response_seen)

        asyncio.run(
            fetcher._on_response_assist(
                Response({"status_code": 0, "data": {"adInfos": {}}})
            )
        )
        self.assertFalse(fetcher._assist_response_seen)

        asyncio.run(
            fetcher._on_response_assist(
                Response({"status_code": 0, "data": {}})
            )
        )
        self.assertFalse(fetcher._assist_response_seen)

        fetcher._persist_assist_api_response_to_db.return_value = False
        asyncio.run(
            fetcher._on_response_assist(
                Response(
                    {
                        "status_code": 0,
                        "data": {
                            "adInfos": [{"id": "assist-1"}],
                            "pagination": {"totalNum": 1},
                        },
                    }
                )
            )
        )
        self.assertFalse(fetcher._assist_response_seen)

        fetcher._persist_assist_api_response_to_db.return_value = True
        asyncio.run(
            fetcher._on_response_assist(
                Response(
                    {
                        "status_code": 0,
                        "data": {"adInfos": [{"id": "assist-1"}]},
                    }
                )
            )
        )
        self.assertFalse(fetcher._assist_response_seen)

        asyncio.run(
            fetcher._on_response_assist(
                Response(
                    {
                        "status_code": 0,
                        "data": {
                            "adInfos": [],
                            "pagination": {"totalNum": 0},
                        },
                    }
                )
            )
        )
        self.assertTrue(fetcher._assist_response_seen)

    def test_full_assist_sync_prunes_tasks_removed_from_current_target(self):
        target_uid = make_target_uid("10001", "20001")
        for assist_id in ("keep", "stale"):
            self.db.insert(
                "pmc_roi2_assist_task",
                {
                    "target_uid": target_uid,
                    "assist_task_id": assist_id,
                    "aadvid": "10001",
                    "ad_id": "20001",
                },
            )
        fetcher = QianChuanFetcher()
        fetcher._current_target_uid = target_uid
        fetcher._assist_task_ids = {"keep": True}
        self.assertTrue(
            asyncio.run(
                fetcher._prune_stale_assist_rows_after_full_sync(self.db)
            )
        )
        rows = self.db.select(
            "pmc_roi2_assist_task",
            fields="assist_task_id",
            where={"target_uid": target_uid},
        )
        self.assertEqual(["keep"], [row["assist_task_id"] for row in rows])

    def test_product_material_request_body_is_scoped_to_target_plan(self):
        fetcher = QianChuanFetcher()
        fetcher._current_adid = "20002"
        body = fetcher._build_product_material_request_body(
            offset=100,
            limit=500,
        )
        conditions = {
            item["Field"]: item["Values"]
            for item in body["Filters"]["Conditions"]
        }
        self.assertEqual(["20002"], conditions["ad_id"])
        self.assertEqual(
            {"Limit": 100, "Offset": 100},
            body["PageParams"],
        )
        self.assertEqual(
            "uni-prom-creative-tab-list",
            body["reqFrom"],
        )

    def test_target_identity_scene_and_url_are_stable(self):
        uid = make_target_uid("10001", "20001")
        self.assertEqual(uid, make_target_uid(10001, 20001))
        self.assertEqual(
            ("10001", "20001"),
            extract_target_ids(
                "https://example.test/path?aavid=10001&adId=20001#ignored"
            ),
        )
        self.assertEqual(
            "product",
            detect_promotion_scene(
                "https://qianchuan.jinritemai.com/uni-prom/detail",
                page_text="商品全域推广",
            ),
        )
        self.assertEqual(
            "live",
            detect_promotion_scene(
                "https://qianchuan.jinritemai.com/uni-prom/detail",
                page_text="直播全域",
            ),
        )
        safe = sanitize_target_url(
            "https://example.test/detail?aavid=10001&adId=20001&token=secret"
        )
        self.assertIn("aavid=10001", safe)
        self.assertIn("adId=20001", safe)
        self.assertNotIn("secret", safe)

    def test_plan_system_detection_is_explicit_and_conservative(self):
        self.assertEqual("global", normalize_plan_system("传统全域"))
        self.assertEqual(
            "chengfang",
            detect_plan_system(payload={"data": {"isChengfang": True}}),
        )
        self.assertEqual(
            "global",
            detect_plan_system(payload={"data": {"planSystem": "global"}}),
        )
        self.assertEqual(
            "global",
            detect_plan_system(page_text="推商品 · 传统全域计划"),
        )
        self.assertEqual(
            "chengfang",
            detect_plan_system(page_text="推商品 · 千川乘方计划"),
        )
        self.assertEqual(
            "unknown",
            detect_plan_system(page_text="商品全域推广"),
        )
        self.assertEqual(
            "unknown",
            detect_plan_system(page_text="推直播"),
        )
        for page_text in ("直播全域", "直播间 · 全域计划"):
            with self.subTest(page_text=page_text):
                self.assertEqual(
                    "global",
                    detect_plan_system(page_text=page_text),
                )
        self.assertEqual(
            "chengfang",
            detect_plan_system(page_text="推直播 · 直播全域 · 千川乘方计划"),
        )
        self.assertEqual(
            "chengfang",
            detect_plan_system(
                payload={
                    "data": {
                        "planSystem": "global",
                        "isChengfang": True,
                    }
                }
            ),
        )

    def test_product_fetch_collects_assist_tasks_when_enabled(self):
        fetcher = QianChuanFetcher()
        fetcher.page = AsyncMock()
        fetcher._check_product_delivery_gate = AsyncMock(return_value=True)
        fetcher._fetch_product_material_pages = AsyncMock()
        fetcher._finalize_pending_ad_detail_basic_cloud = AsyncMock()

        async def collect_assist(_db, _timeout):
            fetcher._assist_task_ids = {"assist-1": True, "assist-2": True}
            fetcher._assist_total_count = 2

        fetcher._fetch_roi2_assist_tasks = AsyncMock(side_effect=collect_assist)
        fetcher._raise_if_global_auth_expired = AsyncMock()
        url = (
            "https://qianchuan.jinritemai.com/uni-prom/detail"
            "?aavid=10001&adId=20001"
        )

        with (
            patch(
                "services.fetcher.load_scrape_service_config",
                return_value={"fetch_assist_tasks": True},
            ),
            patch("services.fetcher.asyncio.sleep", new=AsyncMock()),
        ):
            result = asyncio.run(
                fetcher.fetch(
                    url,
                    db=self.db,
                    timeout=30,
                    target_uid=make_target_uid("10001", "20001"),
                    promotion_scene="product",
                    plan_system="global",
                )
            )

        fetcher._fetch_roi2_assist_tasks.assert_awaited_once_with(self.db, 30)
        self.assertEqual(2, result["assist_task_count"])
        self.assertEqual(2, result["assist_task_total_count"])

    def test_discovery_requires_confirmed_detail_context(self):
        generic = (
            "https://qianchuan.jinritemai.com/uni-prom/detail"
            "?aavid=10001&adId=20001"
        )
        self.assertIsNone(
            detect_confirmed_detail_scene(generic, page_text="投放管理 推直播")
        )
        self.assertEqual(
            "product",
            detect_confirmed_detail_scene(
                generic + "#uniDetail=%7B%22edc%22%3A%22productRace%22%7D"
            ),
        )
        self.assertEqual(
            "product",
            detect_confirmed_detail_scene(
                generic,
                page_text="推商品 商品自选 调控工具 素材追投",
            ),
        )
        self.assertIsNone(
            detect_confirmed_detail_scene(
                "https://qianchuan.jinritemai.com/uni-prom"
                "?aavid=10001&adId=20001",
                page_text="推商品 商品自选 调控工具",
            )
        )
        self.assertEqual(
            "商品计划A",
            extract_plan_name(
                page_text="计划名称：商品计划A\n计划ID：20001",
                page_title="投放管理",
                ad_id="20001",
            ),
        )
        self.assertEqual(
            "计划 20001",
            extract_plan_name(
                page_text="投放管理",
                page_title="投放管理",
                ad_id="20001",
            ),
        )

    def test_readonly_probe_redacts_secrets_and_extracts_plan_candidates(self):
        summary = summarize_json(
            {
                "status_code": 0,
                "data": {
                    "adInfos": [
                        {
                            "adId": "20001",
                            "adName": "商品计划A",
                            "promotionScene": "product",
                            "budget": 1000,
                            "accessToken": "must-not-appear",
                        }
                    ],
                    "cookie": "must-not-appear",
                },
            }
        )
        dumped = str(summary)
        self.assertIn("商品计划A", dumped)
        self.assertIn("20001", dumped)
        self.assertNotIn("must-not-appear", dumped)
        self.assertEqual("20001", summary["plan_candidates"][0]["adId"])

        page = summarize_page(
            "https://qianchuan.jinritemai.com/uni-prom"
            "?access_token=must-not-appear",
            "推商品 商品自选 调控工具 素材追投\n"
            "计划ID：1804998056156307\n商品ID：390001",
        )
        self.assertEqual("/uni-prom", page["path"])
        self.assertEqual(["1804998056156307"], page["visible_plan_ids"])
        self.assertEqual(["390001"], page["visible_product_ids"])
        self.assertNotIn("must-not-appear", str(page))

    def test_product_scene_adapter_uses_main_plan_and_maps_products_to_materials(self):
        detail = extract_product_scene_snapshot(
            {
                "data": {
                    "adDetailInfo": {
                        "id": "1855536108315875",
                        "name": "商品全店托管",
                        "creativeType": 2,
                    },
                    "goodsInfos": [
                        {"id": "p1", "name": "商品一"},
                        {"id": "p2", "name": "商品二"},
                    ],
                }
            }
        )
        listing = extract_product_scene_snapshot(
            {
                "data": {
                    "adInfos": [{"id": "sub1", "name": "子计划一"}],
                    "adGoodsMap": {
                        "sub1": [
                            {"id": "p1", "name": "商品一"},
                            {"id": "p2", "name": "商品二"},
                        ]
                    },
                    "adShowMaterialInfoMap": {
                        "sub1": {
                            "promotionVideoMaterial": {
                                "materialId": "m1",
                                "title": "素材一.mp4",
                            }
                        }
                    },
                }
            }
        )
        merged = merge_product_scene_snapshots([detail, listing])
        self.assertEqual("1855536108315875", merged["plan"]["ad_id"])
        self.assertEqual(
            ["p1", "p2"],
            sorted(merged["materials"][0]["product_ids"]),
        )
        self.assertEqual("m1", merged["materials"][0]["material_id"])
        self.assertEqual(
            {"aavid": "1782685702496260", "ad_id": "1855536108315875"},
            extract_safe_query_identifiers(
                "https://qianchuan.jinritemai.com/ad/api/x"
                "?aavid=1782685702496260&adId=1855536108315875&token=secret"
            ),
        )
    def test_enabled_targets_use_dynamic_capacity_instead_of_fixed_ten_limit(self):
        for i in range(13):
            self._target(i)
        rows = list_promotion_targets(enabled=True, db=self.db)
        self.assertEqual(13, len(rows))
        self.assertEqual(
            12,
            sum(1 for item in rows if item.get("capacity_state") == "active"),
        )
        self.assertEqual(
            1,
            sum(
                1
                for item in rows
                if item.get("capacity_state") == "capacity_waiting"
            ),
        )

    def test_discovery_refresh_preserves_verified_capability_and_scope(self):
        original = upsert_promotion_target(
            {
                "aavid": "10001",
                "ad_id": "20001",
                "plan_name": "商品计划",
                "promotion_scene": "product",
                "plan_system": "global",
                "enabled": True,
                "product_filter_mode": "selected",
                "product_ids": ["p1"],
                "page_url": (
                    "https://qianchuan.jinritemai.com/uni-prom/detail"
                    "?aavid=10001&adId=20001"
                ),
            },
            db=self.db,
        )
        update_target_sync_state(
            original["target_uid"],
            status="ok",
            capability={
                "material_read": True,
                "product_relation": True,
                "retarget_execute": True,
                "regulation_execute": False,
            },
            db=self.db,
        )
        refreshed = upsert_promotion_target(
            {
                "aavid": "10001",
                "ad_id": "20001",
                "promotion_scene": "product",
                "plan_system": "unknown",
                "enabled": True,
                "last_status": "pending",
            },
            db=self.db,
        )
        self.assertEqual(original["plan_name"], refreshed["plan_name"])
        self.assertEqual(original["sanitized_page_url"], refreshed["sanitized_page_url"])
        self.assertEqual("selected", refreshed["product_filter_mode"])
        self.assertEqual(["p1"], refreshed["product_ids"])
        self.assertTrue(refreshed["capability"]["retarget_execute"])
        self.assertTrue(refreshed["capability"]["product_relation"])
        self.assertEqual("global", refreshed["plan_system"])

    def test_client_upsert_cannot_forge_write_capability(self):
        target = upsert_promotion_target(
            {
                "aavid": "10001",
                "ad_id": "20001",
                "plan_name": "商品计划",
                "promotion_scene": "product",
                "plan_system": "global",
                "enabled": True,
                "capability": {
                    "retarget_execute": True,
                    "regulation_execute": True,
                },
            },
            db=self.db,
        )
        self.assertFalse(target["capability"].get("retarget_execute", False))
        self.assertFalse(target["capability"].get("regulation_execute", False))

    def test_atomic_sync_patch_preserves_controlled_capability_evidence(self):
        target = upsert_promotion_target(
            {
                "aavid": "10001",
                "ad_id": "20001",
                "promotion_scene": "product",
                "plan_system": "global",
                "enabled": True,
            },
            db=self.db,
        )
        controlled = {
            "retarget_execute": True,
            "retarget_target_uid": target["target_uid"],
            "retarget_aavid": "10001",
            "retarget_ad_id": "20001",
        }
        update_target_sync_state(
            target["target_uid"],
            status="ok",
            capability=controlled,
            db=self.db,
        )
        merged = patch_target_sync_state(
            target["target_uid"],
            status="ok",
            synced=True,
            capability_updates={
                "material_read": True,
                "assist_sync_ok": True,
            },
            db=self.db,
        )
        self.assertTrue(merged["retarget_execute"])
        self.assertEqual(target["target_uid"], merged["retarget_target_uid"])
        self.assertTrue(merged["material_read"])
        self.assertTrue(merged["assist_sync_ok"])

    def test_auto_stop_requires_current_complete_assist_sync(self):
        now = datetime.now()
        ready, _ = _target_assist_sync_ready(
            {
                "capability": {
                    "assist_sync_enabled": True,
                    "assist_sync_ok": True,
                    "assist_synced_at": now.isoformat(timespec="seconds"),
                }
            }
        )
        self.assertTrue(ready)

        incomplete, reason = _target_assist_sync_ready(
            {
                "capability": {
                    "assist_sync_enabled": True,
                    "assist_sync_ok": False,
                    "assist_synced_at": now.isoformat(timespec="seconds"),
                }
            }
        )
        self.assertFalse(incomplete)
        self.assertIn("未完整同步", reason)

        stale, reason = _target_assist_sync_ready(
            {
                "capability": {
                    "assist_sync_enabled": True,
                    "assist_sync_ok": True,
                    "assist_synced_at": (
                        now - timedelta(hours=2)
                    ).isoformat(timespec="seconds"),
                }
            }
        )
        self.assertFalse(stale)
        self.assertIn("过期", reason)

        in_progress, reason = _target_assist_sync_ready(
            {
                "capability": {
                    "assist_sync_enabled": True,
                    "assist_sync_in_progress": True,
                    "assist_sync_ok": True,
                    "assist_synced_at": now.isoformat(timespec="seconds"),
                }
            }
        )
        self.assertFalse(in_progress)
        self.assertIn("正在同步", reason)

    def test_partial_target_refresh_preserves_sync_status_and_error(self):
        original = upsert_promotion_target(
            {
                "aavid": "10001",
                "ad_id": "20001",
                "plan_name": "商品计划",
                "promotion_scene": "product",
                "plan_system": "global",
                "enabled": True,
                "last_status": "ok",
                "last_error": "已核验说明",
            },
            db=self.db,
        )
        refreshed = upsert_promotion_target(
            {
                "aavid": "10001",
                "ad_id": "20001",
                "promotion_scene": "product",
                "enabled": True,
            },
            db=self.db,
        )
        self.assertEqual(original["last_status"], refreshed["last_status"])
        self.assertEqual(original["last_error"], refreshed["last_error"])

    def test_product_and_material_relations_are_isolated_by_target(self):
        left = self._target(1)
        right = self._target(2)
        upsert_products(
            left["target_uid"],
            [{"product_id": "p1", "product_name": "左商品"}],
            db=self.db,
        )
        upsert_products(
            right["target_uid"],
            [{"product_id": "p1", "product_name": "右商品"}],
            db=self.db,
        )
        replace_material_product_links(
            left["target_uid"], "m1", ["p1"], material_name="素材", db=self.db
        )
        replace_material_product_links(
            right["target_uid"], "m1", ["p1"], material_name="素材", db=self.db
        )
        self.assertEqual(
            "左商品", list_target_products(left["target_uid"], db=self.db)[0]["product_name"]
        )
        self.assertEqual(
            "右商品", list_target_products(right["target_uid"], db=self.db)[0]["product_name"]
        )
        links = self.db.select(
            "promotion_material_product", where={"material_id": "m1"}
        )
        self.assertEqual(2, len(links))
        self.assertEqual(2, len({row["target_uid"] for row in links}))

    def test_product_metrics_recompute_from_sums_and_multi_product_links(self):
        rows = [
            {
                "id": "m1",
                "costDiff": 100,
                "netAmount": 300,
                "overallAmount": 400,
                "overallShowCount": 1000,
                "overallClickCount": 100,
                "overallOrderCount": 10,
            },
            {
                "id": "m2",
                "costDiff": 200,
                "netAmount": 300,
                "overallAmount": 600,
                "overallShowCount": 1000,
                "overallClickCount": 50,
                "overallOrderCount": 5,
            },
        ]
        products = aggregate_product_rows(
            rows,
            relation_map={"m1": ["p1", "p2"], "m2": ["p1"]},
            product_names={"p1": "主商品", "p2": "关联商品"},
        )
        p1 = next(item for item in products if item["productId"] == "p1")
        p2 = next(item for item in products if item["productId"] == "p2")
        self.assertEqual(300, p1["costDiff"])
        self.assertEqual(2.0, p1["netRoi"])
        self.assertEqual(0.075, p1["overallCtr"])
        self.assertEqual(0.1, p1["overallConversionRate"])
        self.assertEqual(["m1"], p2["materialIds"])

    def test_candidate_filter_and_net_roi_order(self):
        product = {
            "materials": [
                {"id": "missing", "costDiff": 20, "netRoi": None, "netAmount": 999},
                {"id": "zero", "costDiff": 0, "netRoi": 99, "netAmount": 999},
                {"id": "low", "costDiff": 100, "netRoi": 1.2, "netAmount": 120},
                {"id": "high", "costDiff": 100, "netRoi": 3.5, "netAmount": 350},
            ]
        }
        selected = select_product_candidates(
            product,
            candidate_trigger=trigger("costDiff", "gte", 0),
            limit=4,
        )
        self.assertEqual(["high", "low", "missing", "zero"], [x["id"] for x in selected])

        hits = evaluate_product_strategy(
            product["materials"],
            {
                "trigger": trigger("costDiff", "gt", 100),
                "candidate_trigger": trigger("netRoi", "gt", 2),
                "candidate_limit": 1,
            },
            relation_map={x["id"]: ["p1"] for x in product["materials"]},
        )
        self.assertEqual(1, len(hits))
        self.assertEqual("high", hits[0]["candidates"][0]["id"])

    def test_rate_limits_are_independent_per_plan(self):
        rate_limit_record_success(self.db, "m1", 3600, 1, "target-left")
        self.assertTrue(
            rate_limit_should_skip(self.db, "m1", 3600, 1, "target-left")
        )
        self.assertFalse(
            rate_limit_should_skip(self.db, "m1", 3600, 1, "target-right")
        )

    def test_live_and_product_urls_use_separate_scene_adapter(self):
        live = build_qianchuan_url_by_params(
            "https://qianchuan.jinritemai.com/uni-prom/detail",
            10001,
            20001,
            promotion_scene="live",
        )
        product = build_qianchuan_url_by_params(
            "https://qianchuan.jinritemai.com/uni-prom/detail",
            10001,
            20001,
            promotion_scene="product",
        )
        self.assertIn("liveRace", live)
        self.assertIn("productRace", product)

    def test_successful_stop_is_idempotent_per_target_and_task(self):
        base = {
            "aavid": "10001",
            "ad_id": "20001",
            "promotion_scene": "product",
            "assist_task_id": "assist-1",
            "started_at": "2026-07-27 10:00:00",
            "ended_at": "2026-07-27 10:00:01",
            "status": 1,
            "headless": 1,
        }
        self.db.insert(
            "pmc_regulation_run",
            {**base, "target_uid": "target-left"},
        )
        self.assertTrue(has_completed_stop(self.db, "target-left", "assist-1"))
        self.assertFalse(has_completed_stop(self.db, "target-right", "assist-1"))

    def test_enabled_stop_strategy_requires_target(self):
        ok, message = validate_rule_regulation_config(
            {
                "enabled": True,
                "strategies": [
                    {
                        "target_uid": "",
                        "regulation_stop_action": "pause",
                        "trigger": trigger(
                            "stat_cost_for_roi2_assist", "gt", 10
                        ),
                    }
                ],
            }
        )
        self.assertFalse(ok)
        self.assertIn("监控计划", message)


if __name__ == "__main__":
    unittest.main()
