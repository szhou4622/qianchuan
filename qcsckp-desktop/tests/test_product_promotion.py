# -*- coding: utf-8 -*-
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from api.promotion_targets import (
    detect_confirmed_detail_scene,
    detect_plan_system,
    detect_promotion_scene,
    extract_plan_name,
    extract_target_ids,
    list_target_products,
    make_target_uid,
    normalize_plan_system,
    replace_material_product_links,
    sanitize_target_url,
    set_promotion_target_enabled,
    upsert_products,
    upsert_promotion_target,
)
from api.rule_regulation_config import validate_rule_regulation_config
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
)
from services.retargeting_service import QianChuanRetargetingService
from services.regulation_rule_runner import has_completed_stop
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


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
            )
        )
        self.assertFalse(result.success)
        self.assertEqual("validate", result.step)
        self.assertIn("最多支持20条素材", result.message)

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
        service._select_product_material = AsyncMock(return_value=None)
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
                )
            )

        self.assertTrue(result.success)
        self.assertEqual("capability_probe", result.step)
        self.assertIn("未点击提交", result.message)
        service._open_product_retarget_dialog.assert_awaited_once()
        service._select_product_material.assert_awaited_once_with(
            service.page,
            "m1",
        )
        service._click_submit_and_wait_assist.assert_not_awaited()
        service.close.assert_awaited_once()

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
        url = (
            "https://qianchuan.jinritemai.com/ad/api/pmc/v1/"
            "uni-promotion/material/list-required?aavid=10001"
        )
        self.assertTrue(
            fetcher._is_target_api(
                url,
                {"reqFrom": "uni-prom-creative-tab-list"},
            )
        )
        self.assertTrue(
            fetcher._is_target_api(
                url + "&reqFrom=uni-prom-creative-tab-list",
            )
        )
        self.assertFalse(fetcher._is_target_api(url, {"reqFrom": "other"}))
        self.assertFalse(
            fetcher._is_target_api(
                url.replace("aavid=10001", "aavid=10002"),
                {"reqFrom": "uni-prom-creative-tab-list"},
            )
        )

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
    def test_maximum_ten_enabled_targets_is_enforced_for_create_and_reenable(self):
        for i in range(10):
            self._target(i)
        with self.assertRaisesRegex(ValueError, "最多同时启用 10"):
            self._target(10)

        set_promotion_target_enabled(make_target_uid("10001", "20000"), False, db=self.db)
        target_11 = self._target(10)
        self.assertTrue(target_11["enabled"])
        with self.assertRaisesRegex(ValueError, "最多同时启用 10"):
            upsert_promotion_target(
                {
                    "aavid": "10001",
                    "ad_id": "20000",
                    "promotion_scene": "product",
                    "enabled": True,
                },
                db=self.db,
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
                "capability": {
                    "material_read": True,
                    "product_relation": True,
                    "retarget_execute": True,
                    "regulation_execute": False,
                },
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
