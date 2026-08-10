# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from api.promotion_targets import (
    set_promotion_target_enabled,
    upsert_promotion_target,
)
from api.rule_regulation_config import (
    _normalize_full as normalize_stop_config,
    load_rule_regulation_config,
)
from services.qianchuan_accounts import (
    ensure_qianchuan_account,
    get_qianchuan_account,
    list_qianchuan_accounts,
    save_qianchuan_account_automation_setup,
)
from services.qianchuan_catalog import finalize_catalog_sync
from services.plan_system import detect_plan_system
from services.promotion_readonly_probe import (
    PromotionReadOnlyProbe,
    summarize_json,
)
from services.run_services import ServiceController, _can_reuse_startup_target
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class Rc27CatalogAndStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "rc27.db")
        init_sqlite_schema(database=self.db_path)
        self.db = SQLiteStore(database=self.db_path)
        self.owner = "tool-owner"

    def tearDown(self):
        self.temp.cleanup()

    def _target(
        self,
        *,
        aavid: str = "10001",
        ad_id: str,
        scene: str,
        system: str,
        status: str = "active",
        verification: str = "verified",
    ):
        return upsert_promotion_target(
            {
                "aavid": aavid,
                "ad_id": ad_id,
                "plan_name": f"{system}-{scene}-{ad_id}",
                "promotion_scene": scene,
                "plan_system": system,
                "platform_status": status,
                "verification_state": verification,
                "enabled": False,
            },
            owner_username=self.owner,
            trusted_catalog=True,
            db=self.db,
        )

    def test_schema_contains_catalog_eligibility_and_stop_action_columns(self):
        target_columns = {
            row["name"]
            for row in self.db.execute(
                "PRAGMA table_info(promotion_target)", fetch=True
            )
        }
        task_columns = {
            row["name"]
            for row in self.db.execute(
                "PRAGMA table_info(local_retarget_task)", fetch=True
            )
        }
        self.assertTrue(
            {
                "platform_status",
                "verification_state",
                "catalog_seen_at",
                "last_verified_at",
                "last_verification_error",
                "write_block_origin",
                "monitor_eligible",
                "retarget_eligible",
                "stop_eligible",
                "ineligible_reason",
            }.issubset(target_columns)
        )
        self.assertIn("action_type", task_columns)

    def test_four_classes_are_monitorable_only_with_explicit_active_verification(self):
        combinations = [
            ("30001", "live", "global"),
            ("30002", "product", "global"),
            ("30003", "live", "chengfang"),
            ("30004", "product", "chengfang"),
        ]
        for ad_id, scene, system in combinations:
            target = self._target(
                ad_id=ad_id,
                scene=scene,
                system=system,
            )
            self.assertTrue(target["monitor_eligible"], (scene, system))

        paused = self._target(
            ad_id="30005",
            scene="live",
            system="global",
            status="paused",
        )
        candidate = self._target(
            ad_id="30006",
            scene="product",
            system="global",
            verification="candidate",
        )
        self.assertFalse(paused["monitor_eligible"])
        self.assertFalse(candidate["monitor_eligible"])
        with self.assertRaises(ValueError):
            set_promotion_target_enabled(
                candidate["target_uid"], True, db=self.db
            )

    def test_click_visible_exact_skips_hidden_duplicate_text_node(self):
        clicked = []

        class Candidate:
            def __init__(self, index):
                self.index = index

            async def is_visible(self):
                return self.index == 0

            async def click(self, timeout=None):
                clicked.append(self.index)

        class Locator:
            async def count(self):
                return 2

            def nth(self, index):
                return Candidate(index)

        class Page:
            def get_by_text(self, _text, exact=None):
                return Locator()

            async def wait_for_timeout(self, _milliseconds):
                return None

        self.assertTrue(
            asyncio.run(
                ServiceController._click_visible_exact(
                    Page(), ["推商品"], timeout_ms=100
                )
            )
        )
        self.assertEqual([0], clicked)

    def test_click_visible_exact_retries_after_stale_visible_node(self):
        attempts = []

        class Candidate:
            def __init__(self, index):
                self.index = index

            async def is_visible(self):
                return True

            async def click(self, timeout=None):
                attempts.append((self.index, timeout))
                if self.index == 1:
                    raise RuntimeError("detached during redraw")

            async def evaluate(self, _script):
                raise RuntimeError("stale node is detached")

        class Locator:
            async def count(self):
                return 2

            def nth(self, index):
                return Candidate(index)

        class Page:
            def get_by_text(self, _text, exact=None):
                return Locator()

            async def wait_for_timeout(self, _milliseconds):
                return None

        self.assertTrue(
            asyncio.run(
                ServiceController._click_visible_exact(
                    Page(), ["推直播间"], timeout_ms=2_000
                )
            )
        )
        self.assertEqual([1, 0], [item[0] for item in attempts])
        self.assertTrue(all(0 < int(item[1]) <= 1_500 for item in attempts))

    def test_product_catalog_visits_self_selected_and_managed_subtabs(self):
        clicked = []

        class Candidate:
            async def is_visible(self):
                return True

            async def click(self, timeout=None):
                clicked.append(page.current_label)

        class Locator:
            async def count(self):
                return 1

            def nth(self, _index):
                return Candidate()

        class Page:
            current_label = ""

            def get_by_text(self, text, exact=None):
                self.current_label = text
                return Locator()

            async def wait_for_timeout(self, _milliseconds):
                return None

        page = Page()
        opened = asyncio.run(
            ServiceController._open_all_product_subcatalogs(page)
        )
        self.assertEqual(2, opened)
        self.assertEqual(["商品自选", "全店托管"], clicked)

    def test_chengfang_promotional_copy_is_not_treated_as_catalog_entry(self):
        class EmptyLocator:
            async def count(self):
                return 0

        class VisibleCopyLocator:
            async def count(self):
                return 1

            def nth(self, _index):
                return self

            async def is_visible(self):
                return True

        class Page:
            def get_by_role(self, _role, name=None, exact=None):
                return EmptyLocator()

            def get_by_text(self, _text, exact=None):
                return VisibleCopyLocator()

        page = Page()
        self.assertFalse(
            asyncio.run(
                ServiceController._has_visible_action_exact(
                    page, ["千川乘方"]
                )
            )
        )
        self.assertFalse(
            asyncio.run(
                ServiceController._open_explicit_chengfang_catalog(
                    page,
                    aavid="10001",
                )
            )
        )

    def test_chengfang_badged_text_navigation_can_open_catalog(self):
        class EmptyLocator:
            async def count(self):
                return 0

        class Candidate:
            async def is_visible(self):
                return True

            async def click(self, timeout=None):
                page.active = True

        class TextLocator:
            async def count(self):
                return 1

            def nth(self, _index):
                return Candidate()

        class Page:
            active = False

            def get_by_role(self, _role, name=None, exact=None):
                return EmptyLocator()

            def get_by_text(self, _text, exact=None):
                return TextLocator()

            async def wait_for_load_state(self, *_args, **_kwargs):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

            async def evaluate(self, _script):
                return "chengfang" if self.active else "unknown"

        page = Page()
        self.assertTrue(
            asyncio.run(
                ServiceController._open_explicit_chengfang_catalog(
                    page,
                    aavid="10001",
                )
            )
        )

    def test_chengfang_nested_span_navigation_uses_interactive_ancestor(self):
        class EmptyLocator:
            async def count(self):
                return 0

        class Page:
            def get_by_role(self, _role, name=None, exact=None):
                return EmptyLocator()

            def get_by_text(self, _text, exact=None):
                return EmptyLocator()

            async def evaluate(self, script):
                return "closest(" in script

            async def wait_for_load_state(self, *_args, **_kwargs):
                return None

            async def wait_for_timeout(self, _milliseconds):
                return None

        self.assertTrue(
            asyncio.run(
                ServiceController._open_explicit_chengfang_catalog(
                    Page(),
                    aavid="10001",
                )
            )
        )

    def test_new_account_and_new_plan_are_disabled_until_user_saves_setup(self):
        account = ensure_qianchuan_account(
            "10001",
            owner_username=self.owner,
            seen=True,
            db=self.db,
        )
        target = self._target(
            ad_id="30001",
            scene="live",
            system="global",
        )
        self.assertFalse(account["enabled"])
        self.assertFalse(target["enabled"])

    def test_account_and_plan_setup_validates_all_rows_before_transaction(self):
        account = ensure_qianchuan_account(
            "10001",
            owner_username=self.owner,
            seen=True,
            db=self.db,
        )
        eligible = self._target(
            ad_id="30001",
            scene="live",
            system="global",
        )
        blocked = self._target(
            ad_id="30002",
            scene="product",
            system="global",
            verification="candidate",
        )
        feishu = {
            "connected": True,
            "profile": {"authorized_open_id": "ou_owner"},
        }
        with patch(
            "services.local_feishu_bridge.get_local_feishu_status",
            return_value=feishu,
        ):
            with self.assertRaises(ValueError):
                save_qianchuan_account_automation_setup(
                    account["account_uid"],
                    {
                        "enabled": True,
                        "report_enabled": True,
                        "route_mode": "default",
                    },
                    [
                        {"target_uid": eligible["target_uid"], "enabled": True},
                        {"target_uid": blocked["target_uid"], "enabled": True},
                    ],
                    owner_username=self.owner,
                    db=self.db,
                )
        saved_account = get_qianchuan_account(
            account["account_uid"],
            owner_username=self.owner,
            db=self.db,
        )
        self.assertFalse(saved_account["enabled"])
        self.assertFalse(
            self.db.select_one(
                "promotion_target",
                where={"target_uid": eligible["target_uid"]},
            )["enabled"]
        )

    def test_catalog_never_claims_complete_without_explicit_page_evidence(self):
        account = ensure_qianchuan_account(
            "10001",
            owner_username=self.owner,
            seen=True,
            db=self.db,
        )
        self._target(ad_id="30001", scene="live", system="global")
        partial = finalize_catalog_sync(
            owner_username=self.owner,
            complete_account_uids=[],
            db=self.db,
        )
        self.assertFalse(partial["complete"])
        saved = get_qianchuan_account(
            account["account_uid"],
            owner_username=self.owner,
            db=self.db,
        )
        self.assertEqual("partial", saved["catalog_status"])

        complete = finalize_catalog_sync(
            owner_username=self.owner,
            complete_account_uids=[account["account_uid"]],
            db=self.db,
        )
        self.assertTrue(complete["complete"])

    def test_new_stop_rules_default_to_card_and_legacy_rules_keep_auto(self):
        new_config = normalize_stop_config(None)
        self.assertEqual(
            "card_confirm", new_config["strategies"][0]["action_mode"]
        )
        legacy = normalize_stop_config(
            {
                "enabled": True,
                "strategies": [
                    {
                        "id": "legacy-stop",
                        "title": "旧停投策略",
                        "target_uid": "target-1",
                        "trigger": {
                            "group_combine": "or",
                            "groups": [],
                        },
                    }
                ],
            }
        )
        self.assertEqual(
            "auto_execute", legacy["strategies"][0]["action_mode"]
        )

    def test_legacy_stop_action_mode_is_persisted_on_first_load(self):
        path = os.path.join(self.temp.name, "rule_regulation.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "enabled": False,
                    "strategies": [
                        {
                            "id": "legacy-stop",
                            "title": "旧停投策略",
                            "trigger": {
                                "group_combine": "or",
                                "groups": [],
                            },
                        }
                    ],
                },
                handle,
                ensure_ascii=False,
            )
        with patch(
            "api.rule_regulation_config.config_path",
            return_value=path,
        ):
            loaded = load_rule_regulation_config()
        self.assertEqual(
            "auto_execute", loaded["strategies"][0]["action_mode"]
        )
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(
            "auto_execute", saved["strategies"][0]["action_mode"]
        )

    def test_account_switch_payload_uses_exact_account_names_not_login_alias(self):
        summary = summarize_json(
            {
                "data": {
                    "userAccountInfos": [
                        {
                            "id": "1772402168278157",
                            "name": "口袋文化",
                            "qcVersion": "Prof",
                        },
                        {
                            "id": "1782685702496260",
                            "name": "松之选专卖店",
                            "qcVersion": "Prof",
                        },
                    ]
                }
            }
        )
        self.assertEqual(
            [
                ("1772402168278157", "口袋文化"),
                ("1782685702496260", "松之选专卖店"),
            ],
            [
                (item["aavid"], item["account_name"])
                for item in summary["account_candidates"]
            ],
        )

    def test_authorized_account_completeness_uses_full_ui_scroll_result(self):
        path = os.path.join(self.temp.name, "account-probe.json")
        probe = PromotionReadOnlyProbe(path)
        probe._authorized_account_total = 2
        probe._authorized_account_pages_seen = {1}
        probe._authorized_accounts_ui = {
            "1772402168278157": {
                "aavid": "1772402168278157",
                "account_name": "口袋文化",
            },
            "1782685702496260": {
                "aavid": "1782685702496260",
                "account_name": "松之选专卖店",
            },
        }
        # The response cache may retain only the last GET page. The fully
        # scrolled UI result remains authoritative for observed account count.
        probe._apis["/ad/api/v1/account/user-list"] = {
            "account_candidates": [
                {
                    "aavid": "1782685702496260",
                    "account_name": "松之选专卖店",
                }
            ]
        }
        status = probe.authorized_account_catalog_status()
        self.assertTrue(status["complete"])
        self.assertEqual(2, status["observed"])
        self.assertEqual(
            {"1772402168278157", "1782685702496260"},
            {item["aavid"] for item in probe.authorized_accounts()},
        )

    def test_catalog_requires_full_status_variant_and_all_pages(self):
        path = os.path.join(self.temp.name, "probe.json")
        probe = PromotionReadOnlyProbe(path)
        prefix = ("10001", "product", "global")
        probe._catalog_variants[(*prefix, "filtered")] = {
            "total_pages": 2,
            "seen_pages": [1, 2],
            "full_catalog": False,
            "error": "",
        }
        self.assertFalse(
            probe.catalog_class_status(
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
            )["complete"]
        )
        probe._catalog_variants[(*prefix, "full")] = {
            "total_pages": 2,
            "seen_pages": [1],
            "full_catalog": True,
            "error": "",
        }
        self.assertFalse(
            probe.catalog_class_status(
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
            )["complete"]
        )
        probe._catalog_variants[(*prefix, "full")]["seen_pages"] = [1, 2]
        self.assertTrue(
            probe.catalog_class_status(
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
            )["complete"]
        )

    def test_direct_all_status_replay_keeps_its_original_catalog_context(self):
        path = os.path.join(self.temp.name, "direct-replay-probe.json")
        probe = PromotionReadOnlyProbe(path)
        request_path = "/ad/api/pmc/v1/uni-promotion/ad/list-required"
        body = {
            "Params": {
                "AdFilter": {"MarGoal": 1},
                "PageParams": {"Page": 1, "PageSize": 100},
            }
        }
        probe._record_catalog_payload(
            path=request_path,
            body=body,
            http_status=200,
            payload={
                "status_code": 0,
                "data": {
                    "adInfos": [
                        {
                            "id": "20001",
                            "name": "商品计划一",
                            "adDeliveryName": "投放中",
                        },
                        {
                            "id": "20002",
                            "name": "商品计划二",
                            "adDeliveryName": "已暂停",
                        },
                    ]
                },
            },
            aavid="10001",
            promotion_scene="product",
            plan_system="global",
            full_catalog=True,
        )

        self.assertTrue(
            probe.catalog_class_status(
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
            )["complete"]
        )
        self.assertEqual(
            {"20001", "20002"},
            {
                row["ad_id"]
                for row in probe.catalog_rows(
                    aavid="10001",
                    promotion_scene="product",
                    plan_system="global",
                )
            },
        )

    def test_deferred_all_status_replay_runs_after_navigation_settles(self):
        path = os.path.join(self.temp.name, "deferred-replay-probe.json")
        probe = PromotionReadOnlyProbe(path)
        request_path = "/ad/api/pmc/v1/uni-promotion/ad/list-required"
        body = {
            "Params": {
                "AdFilter": {"MarGoal": 1},
                "PageParams": {"Page": 1, "PageSize": 100},
            }
        }
        marker = (request_path, "10001", "product", "global")
        probe._catalog_replay_templates[marker] = {
            "url": "https://qianchuan.test" + request_path,
            "body": body,
        }

        class StablePage:
            async def evaluate(self, _script, _arguments):
                return {
                    "status": 200,
                    "payload": {
                        "status_code": 0,
                        "data": {
                            "adInfos": [
                                {
                                    "id": "20003",
                                    "name": "稳定后取得的商品计划",
                                    "adDeliveryName": "投放中",
                                }
                            ]
                        },
                    },
                }

        complete = asyncio.run(
            probe.replay_full_catalog(
                StablePage(),
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
            )
        )
        self.assertTrue(complete)
        self.assertEqual(
            ["20003"],
            [
                row["ad_id"]
                for row in probe.catalog_rows(
                    aavid="10001",
                    promotion_scene="product",
                    plan_system="global",
                )
            ],
        )

    def test_flat_and_nested_catalog_pagination_are_supported(self):
        flat = {"page": 1, "page_size": 10}
        nested = {"Params": {"PageParams": {"Page": 1, "PageSize": 10}}}
        self.assertTrue(PromotionReadOnlyProbe._set_request_page(flat, 3))
        self.assertTrue(PromotionReadOnlyProbe._set_request_page(nested, 4))
        self.assertEqual(3, flat["page"])
        self.assertEqual(4, nested["Params"]["PageParams"]["Page"])
        self.assertEqual(
            3,
            PromotionReadOnlyProbe._response_total_pages(
                {"data": {"pagination": {"totalPages": 3}}}
            ),
        )

    def test_backend_dataset_is_authoritative_for_all_four_catalog_classes(self):
        expected = {
            "overall_roi_promotion_list_for_product": ("product", "global"),
            "site_promotion_list": ("live", "global"),
            "overall_roi_promotion_list_for_product_v2": (
                "product",
                "chengfang",
            ),
            "overall_roi_promotion_list_for_live_v2": ("live", "chengfang"),
        }
        for dataset, catalog_class in expected.items():
            with self.subTest(dataset=dataset):
                self.assertEqual(
                    catalog_class,
                    PromotionReadOnlyProbe._request_catalog_class(
                        {
                            "aavid": "10001",
                            "mar_goal": 1,
                            "dataSetKey": dataset,
                        }
                    ),
                )

    def test_backend_catalog_fetch_does_not_require_dom_navigation(self):
        path = os.path.join(self.temp.name, "backend-catalog-probe.json")
        probe = PromotionReadOnlyProbe(path)
        probe._catalog_base_templates["10001"] = {
            "url": (
                "https://qianchuan.jinritemai.com"
                "/ad/api/pmc/v1/uni-promotion/ad/list-required"
            ),
            "body": {
                "aavid": "10001",
                "mar_goal": 1,
                "dataSetKey": "overall_roi_promotion_list_for_product",
                "adlabScene": 0,
                "smartBidType": 0,
                "page": 1,
                "page_size": 100,
            },
        }

        class BackendOnlyPage:
            def __init__(self):
                self.requests = []

            async def evaluate(self, _script, arguments):
                body = dict(arguments["body"])
                self.requests.append(body)
                dataset = str(body["dataSetKey"])
                return {
                    "status": 200,
                    "payload": {
                        "status_code": 0,
                        "data": {
                            "totalPage": 1,
                            "adInfos": [
                                {
                                    "id": str(30000 + len(self.requests)),
                                    "name": dataset,
                                    "adDeliveryName": "投放中",
                                }
                            ],
                        },
                    },
                }

        page = BackendOnlyPage()
        classes = (
            ("product", "global"),
            ("live", "global"),
            ("product", "chengfang"),
            ("live", "chengfang"),
        )
        for scene, system in classes:
            complete = asyncio.run(
                probe.fetch_catalog_class_from_backend(
                    page,
                    aavid="10001",
                    promotion_scene=scene,
                    plan_system=system,
                )
            )
            self.assertTrue(complete, (scene, system))
            rows = probe.catalog_rows(
                aavid="10001",
                promotion_scene=scene,
                plan_system=system,
            )
            self.assertEqual(1, len(rows), (scene, system))
            self.assertEqual(system, rows[0]["plan_system"])
            self.assertEqual(scene, rows[0]["promotion_scene"])

        self.assertEqual(
            [
                "overall_roi_promotion_list_for_product",
                "site_promotion_list",
                "overall_roi_promotion_list_for_product_v2",
                "overall_roi_promotion_list_for_live_v2",
            ],
            [item["dataSetKey"] for item in page.requests],
        )

    def test_backend_catalog_prefers_the_exact_scene_request_shape(self):
        path = os.path.join(self.temp.name, "exact-template-probe.json")
        probe = PromotionReadOnlyProbe(path)
        probe._catalog_base_templates[("10001", "live", "global")] = {
            "url": "https://qianchuan.test/live",
            "body": {
                "aavid": "10001",
                "mar_goal": 2,
                "dataSetKey": "site_promotion_list",
                "page": 1,
                "page_size": 100,
                "requestShape": "live",
            },
        }
        probe._catalog_base_templates[("10001", "product", "global")] = {
            "url": "https://qianchuan.test/product",
            "body": {
                "aavid": "10001",
                "mar_goal": 1,
                "dataSetKey": "overall_roi_promotion_list_for_product",
                "page": 1,
                "page_size": 100,
                "requestShape": "product",
            },
        }

        class Page:
            def __init__(self):
                self.calls = []

            async def evaluate(self, _script, arguments):
                self.calls.append(dict(arguments["body"]))
                return {
                    "status": 200,
                    "payload": {
                        "status_code": 0,
                        "data": {
                            "totalPage": 1,
                            "adInfos": [
                                {
                                    "id": "30001",
                                    "name": "商品计划",
                                    "adDeliveryName": "投放中",
                                }
                            ],
                        },
                    },
                }

        page = Page()
        complete = asyncio.run(
            probe.fetch_catalog_class_from_backend(
                page,
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
            )
        )
        self.assertTrue(complete)
        self.assertEqual("product", page.calls[0]["requestShape"])
        self.assertEqual(
            "overall_roi_promotion_list_for_product",
            page.calls[0]["dataSetKey"],
        )

    def test_backend_catalog_replays_every_native_subcatalog_template(self):
        path = os.path.join(self.temp.name, "multi-template-probe.json")
        probe = PromotionReadOnlyProbe(path)
        datasets = {
            "self_selected": "product_roi2_promotion",
            "all_shop": "site_promotion_allshop_list",
        }
        for subcatalog, dataset in datasets.items():
            probe._remember_catalog_template(
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
                template={
                    "url": "https://qianchuan.test/catalog",
                    "body": {
                        "aavid": "10001",
                        "mar_goal": 1,
                        "dataSetKey": dataset,
                        "page": 1,
                        "page_size": 20,
                        "subCatalog": subcatalog,
                    },
                },
            )

        class Page:
            def __init__(self):
                self.calls = []

            async def evaluate(self, _script, arguments):
                body = dict(arguments["body"])
                self.calls.append(body)
                suffix = "1" if body["subCatalog"] == "self_selected" else "2"
                return {
                    "status": 200,
                    "payload": {
                        "status_code": 0,
                        "data": {
                            "totalPage": 1,
                            "adInfos": [
                                {
                                    "id": f"3000{suffix}",
                                    "name": body["subCatalog"],
                                    "adDeliveryName": "投放中",
                                }
                            ],
                        },
                    },
                }

        page = Page()
        complete = asyncio.run(
            probe.fetch_catalog_class_from_backend(
                page,
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
            )
        )

        self.assertTrue(complete)
        self.assertEqual(
            ["all_shop", "self_selected"],
            sorted(body["subCatalog"] for body in page.calls),
        )
        self.assertEqual(
            sorted(datasets.values()),
            sorted(body["dataSetKey"] for body in page.calls),
        )
        self.assertEqual(
            ["30001", "30002"],
            sorted(
                row["ad_id"]
                for row in probe.catalog_rows(
                    aavid="10001",
                    promotion_scene="product",
                    plan_system="global",
                )
            ),
        )

    def test_catalog_template_cache_collapses_status_filters_per_dataset(self):
        probe = PromotionReadOnlyProbe(
            os.path.join(self.temp.name, "filtered-template-probe.json")
        )
        for status_filter in ("active", "paused"):
            probe._remember_catalog_template(
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
                template={
                    "url": "https://qianchuan.test/catalog",
                    "body": {
                        "aavid": "10001",
                        "dataSetKey": "product_roi2_promotion",
                        "mar_goal": 1,
                        "page": 1,
                        "page_size": 20,
                        "ad_status_filter_type": status_filter,
                        "ad_cost_status": status_filter,
                    },
                },
            )

        templates = probe._catalog_templates_for_scope(
            aavid="10001",
            promotion_scene="product",
            plan_system="global",
        )
        self.assertEqual(1, len(templates))
        self.assertNotIn("ad_status_filter_type", templates[0]["body"])
        self.assertNotIn("ad_cost_status", templates[0]["body"])

    def test_current_product_datasets_supersede_legacy_catalog_contract(self):
        probe = PromotionReadOnlyProbe(
            os.path.join(self.temp.name, "preferred-template-probe.json")
        )
        for dataset in (
            "overall_roi_promotion_list_for_product",
            "product_roi2_promotion",
            "site_promotion_allshop_list",
        ):
            probe._remember_catalog_template(
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
                template={
                    "url": "https://qianchuan.test/catalog",
                    "body": {
                        "aavid": "10001",
                        "dataSetKey": dataset,
                        "mar_goal": 1,
                        "page": 1,
                        "page_size": 100,
                    },
                },
            )

        preferred = probe._preferred_catalog_templates_for_scope(
            aavid="10001",
            promotion_scene="product",
            plan_system="global",
        )
        self.assertEqual(
            ["product_roi2_promotion", "site_promotion_allshop_list"],
            sorted(
                probe._catalog_template_dataset(template["body"])
                for template in preferred
            ),
        )

    def test_backend_catalog_never_reuses_another_accounts_generic_template(self):
        path = os.path.join(self.temp.name, "account-scoped-template.json")
        probe = PromotionReadOnlyProbe(path)
        probe._catalog_base_templates[("product", "global")] = {
            "url": "https://qianchuan.test/old-account",
            "body": {
                "aavid": "20002",
                "mar_goal": 1,
                "dataSetKey": "overall_roi_promotion_list_for_product",
                "page": 1,
                "page_size": 100,
            },
        }
        probe._catalog_base_templates["10001"] = {
            "url": "https://qianchuan.test/current-account",
            "body": {
                "aavid": "10001",
                "mar_goal": 2,
                "dataSetKey": "site_promotion_list",
                "page": 1,
                "page_size": 100,
            },
        }

        class Page:
            def __init__(self):
                self.calls = []

            async def evaluate(self, _script, arguments):
                self.calls.append(dict(arguments))
                return {
                    "status": 200,
                    "payload": {
                        "status_code": 0,
                        "data": {
                            "totalPage": 1,
                            "adInfos": [
                                {
                                    "id": "30001",
                                    "name": "当前账户商品计划",
                                    "adDeliveryName": "投放中",
                                }
                            ],
                        },
                    },
                }

        page = Page()
        complete = asyncio.run(
            probe.fetch_catalog_class_from_backend(
                page,
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
            )
        )
        self.assertTrue(complete)
        self.assertTrue(probe.has_backend_catalog_template("10001"))
        self.assertFalse(probe.has_backend_catalog_template("30003"))
        self.assertEqual(
            "https://qianchuan.test/current-account",
            page.calls[0]["url"],
        )
        self.assertEqual("10001", page.calls[0]["body"]["aavid"])
        self.assertEqual(
            "overall_roi_promotion_list_for_product",
            page.calls[0]["body"]["dataSetKey"],
        )

    def test_backend_catalog_derives_sibling_scene_only_for_same_account_system(self):
        path = os.path.join(self.temp.name, "same-account-sibling-probe.json")
        probe = PromotionReadOnlyProbe(path)
        probe._catalog_base_templates[("10001", "live", "chengfang")] = {
            "url": "https://qianchuan.test/current-account",
            "body": {
                "aavid": "10001",
                "Params": {
                    "SophonxDataSetKey": "overall_roi_promotion_list_for_live_v2",
                    "AdFilter": {
                        "MarGoal": 2,
                        "AdlabScene": 1,
                        "IsOverallRoi": 1,
                    },
                    "PageParams": {"Page": 1, "PageSize": 10},
                },
            },
        }

        class Page:
            def __init__(self):
                self.calls = []

            async def evaluate(self, _script, arguments):
                body = dict(arguments["body"])
                self.calls.append(body)
                return {
                    "status": 200,
                    "payload": {
                        "status_code": 0,
                        "data": {"totalPage": 1, "adInfos": []},
                    },
                }

        page = Page()
        complete = asyncio.run(
            probe.fetch_catalog_class_from_backend(
                page,
                aavid="10001",
                promotion_scene="product",
                plan_system="chengfang",
            )
        )

        self.assertTrue(complete)
        sent = page.calls[0]
        self.assertEqual("10001", sent["aavid"])
        self.assertEqual(
            "overall_roi_promotion_list_for_product_v2",
            sent["Params"]["SophonxDataSetKey"],
        )
        self.assertEqual(1, sent["Params"]["AdFilter"]["MarGoal"])

    def test_backend_catalog_falls_back_to_canonical_read_contract(self):
        path = os.path.join(self.temp.name, "canonical-fallback-probe.json")
        probe = PromotionReadOnlyProbe(path)
        probe._catalog_base_templates[("10001", "product", "global")] = {
            "url": "https://qianchuan.test/catalog",
            "body": {
                "aavid": "10001",
                "mar_goal": 1,
                "dataSetKey": "overall_roi_promotion_list_for_product",
                "page": 1,
                "page_size": 100,
                "requestShape": "page-specific",
            },
        }

        class Page:
            def __init__(self):
                self.calls = []

            async def evaluate(self, _script, arguments):
                body = dict(arguments["body"])
                self.calls.append(body)
                if body.get("requestShape"):
                    return {
                        "status": 200,
                        "payload": {
                            "status_code": 40001,
                            "message": "request shape rejected",
                        },
                    }
                return {
                    "status": 200,
                    "payload": {
                        "status_code": 0,
                        "data": {
                            "totalPage": 1,
                            "adInfos": [
                                {
                                    "id": "30002",
                                    "name": "标准只读契约计划",
                                    "adDeliveryName": "投放中",
                                }
                            ],
                        },
                    },
                }

        page = Page()
        complete = asyncio.run(
            probe.fetch_catalog_class_from_backend(
                page,
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
            )
        )
        self.assertTrue(complete)
        self.assertEqual(2, len(page.calls))
        self.assertNotIn("requestShape", page.calls[1])
        self.assertEqual(100, page.calls[1]["PageSize"])
        self.assertEqual(
            ["30002"],
            [
                row["ad_id"]
                for row in probe.catalog_rows(
                    aavid="10001",
                    promotion_scene="product",
                    plan_system="global",
                )
            ],
        )

    def test_partial_backend_catalog_recovers_through_native_class_requests(self):
        ensure_qianchuan_account(
            "10001",
            account_name="测试账户",
            owner_username=self.owner,
            seen=True,
            db=self.db,
        )

        class Page:
            url = "https://qianchuan.jinritemai.com/uni-prom?aavid=10001"

            async def goto(self, url, **_kwargs):
                self.url = url

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Probe:
            def reset_catalog_class(self, **_kwargs):
                return None

            def set_catalog_context(self, **_kwargs):
                return None

            async def fetch_catalog_class_from_backend(self, _page, **kwargs):
                # Reproduce the live-only signed template observed in the
                # field: derived product contracts fail, while live works.
                return kwargs["promotion_scene"] == "live"

            def has_backend_catalog_template(self, _aavid):
                return True

        controller = ServiceController.__new__(ServiceController)
        scanned = []

        async def scan_class(**kwargs):
            scanned.append(
                (kwargs["plan_system"], kwargs["promotion_scene"])
            )
            return {
                "complete": True,
                "seen": 0,
                "seen_ids": [],
                "verified": 0,
                "candidates": 0,
                "message": "",
            }

        controller._scan_catalog_class = scan_class
        controller._open_explicit_global_catalog = AsyncMock(
            return_value=Page.url
        )
        controller._open_all_product_subcatalogs = AsyncMock()
        controller._click_visible_exact = AsyncMock(return_value=True)
        controller._has_visible_action_exact = AsyncMock(return_value=True)
        controller._open_explicit_chengfang_catalog = AsyncMock(
            return_value=True
        )

        with patch(
            "services.run_services.current_session_owner",
            return_value=self.owner,
        ):
            result = asyncio.run(
                controller._scan_global_account_catalog(
                    fetcher=SimpleNamespace(page=Page()),
                    probe=Probe(),
                    db=self.db,
                    owner_username=self.owner,
                    account={
                        "aavid": "10001",
                        "account_name": "测试账户",
                    },
                )
            )

        self.assertTrue(result["complete"])
        self.assertEqual(
            [
                ("global", "product"),
                ("global", "live"),
                ("chengfang", "product"),
                ("chengfang", "live"),
            ],
            scanned[-4:],
        )

    def test_known_chengfang_plan_prevents_false_empty_complete_catalog(self):
        ensure_qianchuan_account(
            "10001",
            account_name="测试账户",
            owner_username=self.owner,
            seen=True,
            db=self.db,
        )
        self._target(
            ad_id="39991",
            scene="live",
            system="chengfang",
        )

        class Page:
            url = "https://qianchuan.jinritemai.com/uni-prom?aavid=10001"

            async def goto(self, url, **_kwargs):
                self.url = url

            async def wait_for_timeout(self, _milliseconds):
                return None

        class Probe:
            def reset_catalog_class(self, **_kwargs):
                return None

            def set_catalog_context(self, **_kwargs):
                return None

            async def fetch_catalog_class_from_backend(self, _page, **kwargs):
                return kwargs["promotion_scene"] == "live"

            def has_backend_catalog_template(self, _aavid):
                return True

        controller = ServiceController.__new__(ServiceController)

        async def scan_class(**_kwargs):
            return {
                "complete": True,
                "seen": 0,
                "seen_ids": [],
                "verified": 0,
                "candidates": 0,
                "message": "",
            }

        controller._scan_catalog_class = scan_class
        controller._open_explicit_global_catalog = AsyncMock(
            return_value=Page.url
        )
        controller._open_all_product_subcatalogs = AsyncMock()
        controller._click_visible_exact = AsyncMock(return_value=True)
        controller._has_visible_action_exact = AsyncMock(return_value=False)
        controller._open_explicit_chengfang_catalog = AsyncMock(
            return_value=False
        )

        with patch(
            "services.run_services.current_session_owner",
            return_value=self.owner,
        ):
            result = asyncio.run(
                controller._scan_global_account_catalog(
                    fetcher=SimpleNamespace(page=Page()),
                    probe=Probe(),
                    db=self.db,
                    owner_username=self.owner,
                    account={
                        "aavid": "10001",
                        "account_name": "测试账户",
                    },
                )
            )

        self.assertFalse(result["complete"])
        self.assertFalse(result["classes"]["chengfang_live"]["complete"])
        self.assertIn(
            "已登记乘方·推直播计划",
            result["classes"]["chengfang_live"]["message"],
        )

    def test_exact_detail_verification_can_limit_to_new_plan_ids(self):
        path = os.path.join(self.temp.name, "incremental-verify-probe.json")
        probe = PromotionReadOnlyProbe(path)
        for ad_id in ("30001", "30002"):
            probe._catalog_rows[("10001", "product", "global", ad_id)] = {
                "aavid": "10001",
                "ad_id": ad_id,
                "plan_name": ad_id,
                "promotion_scene": "product",
                "plan_system": "global",
                "platform_status": "active",
                "product_ids": [],
            }

        class Page:
            def __init__(self):
                self.calls = []

            async def evaluate(self, _script, arguments):
                self.calls.append(arguments["adId"])
                return {
                    "status": 200,
                    "payload": {
                        "status_code": 0,
                        "data": {
                            "adDetailInfo": {
                                "id": arguments["adId"],
                                "name": arguments["adId"],
                                "advId": "10001",
                                "marGoal": 1,
                            }
                        },
                    },
                }

        page = Page()
        result = asyncio.run(
            probe.verify_catalog_plans(
                page,
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
                ad_ids=["30002"],
            )
        )
        self.assertEqual(["30002"], page.calls)
        self.assertEqual(1, result["candidate_count"])

    def test_exact_detail_verification_uses_bounded_concurrency(self):
        path = os.path.join(self.temp.name, "concurrent-verify-probe.json")
        probe = PromotionReadOnlyProbe(path)
        for offset in range(8):
            ad_id = str(31000 + offset)
            probe._catalog_rows[("10001", "product", "global", ad_id)] = {
                "aavid": "10001",
                "ad_id": ad_id,
                "plan_name": ad_id,
                "promotion_scene": "product",
                "plan_system": "global",
                "platform_status": "active",
                "product_ids": [],
            }

        class Page:
            def __init__(self):
                self.active = 0
                self.max_active = 0

            async def evaluate(self, _script, arguments):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.02)
                self.active -= 1
                return {
                    "status": 200,
                    "payload": {
                        "status_code": 0,
                        "data": {
                            "adDetailInfo": {
                                "id": arguments["adId"],
                                "name": arguments["adId"],
                                "advId": "10001",
                                "marGoal": 1,
                            }
                        },
                    },
                }

        page = Page()
        result = asyncio.run(
            probe.verify_catalog_plans(
                page,
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
            )
        )
        self.assertEqual(8, len(result["verified"]))
        self.assertGreater(page.max_active, 1)
        self.assertLessEqual(page.max_active, 6)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_catalog_request_template_is_dpapi_persisted(self):
        path = os.path.join(self.temp.name, "template-probe.json")
        probe = PromotionReadOnlyProbe(path)
        probe._catalog_base_templates[("product", "global")] = {
            "url": "/ad/api/pmc/v1/uni-promotion/ad/list-required",
            "body": {
                "aavid": "10001",
                "Params": {
                    "SophonxDataSetKey": "overall_roi_promotion_list_for_product",
                    "AdFilter": {"MarGoal": 1},
                    "PageParams": {"Page": 1, "PageSize": 100},
                },
            },
        }
        probe._catalog_templates_protected = probe._protect_catalog_templates(
            probe._serializable_catalog_templates()
        )
        probe._flush()
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertIn('"catalog_templates_protected": "dpapi:', raw)
        self.assertNotIn("overall_roi_promotion_list_for_product", raw)

        restored = PromotionReadOnlyProbe(path)
        self.assertIn(("product", "global"), restored._catalog_base_templates)
        self.assertIn(
            ("10001", "product", "global"),
            restored._catalog_base_templates,
        )

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_all_catalog_subtemplates_are_dpapi_persisted(self):
        path = os.path.join(self.temp.name, "multi-template-persisted-probe.json")
        probe = PromotionReadOnlyProbe(path)
        for subcatalog, dataset in {
            "self_selected": "overall_roi_promotion_list_for_product",
            "all_shop": "site_promotion_allshop_list",
        }.items():
            probe._remember_catalog_template(
                aavid="10001",
                promotion_scene="product",
                plan_system="global",
                template={
                    "url": "/ad/api/pmc/v1/uni-promotion/ad/list-required",
                    "body": {
                        "aavid": "10001",
                        "mar_goal": 1,
                        "dataSetKey": dataset,
                        "page": 1,
                        "page_size": 20,
                        "subCatalog": subcatalog,
                    },
                },
            )
        probe._catalog_templates_protected = probe._protect_catalog_templates(
            probe._serializable_catalog_templates()
        )
        probe._flush()

        restored = PromotionReadOnlyProbe(path)
        templates = restored._catalog_templates_for_scope(
            aavid="10001",
            promotion_scene="product",
            plan_system="global",
        )
        self.assertEqual(2, len(templates))
        self.assertEqual(
            ["all_shop", "self_selected"],
            sorted(template["body"]["subCatalog"] for template in templates),
        )

    def test_generated_catalog_replay_cannot_overwrite_observed_template(self):
        path = os.path.join(self.temp.name, "generated-template-probe.json")
        probe = PromotionReadOnlyProbe(path)
        good_body = {
            "aavid": "10001",
            "Params": {
                "SophonxDataSetKey": "observed_product_contract",
                "AdFilter": {"MarGoal": 1},
                "PageParams": {"Page": 1, "PageSize": 100},
            },
        }
        generated_body = {
            "aavid": "10001",
            "Params": {
                "SophonxDataSetKey": "generated_replay_contract",
                "AdFilter": {"MarGoal": 1},
                "PageParams": {"Page": 1, "PageSize": 100},
            },
        }
        probe._catalog_base_templates[("product", "global")] = {
            "url": "/ad/api/pmc/v1/uni-promotion/ad/list-required",
            "body": good_body,
        }
        probe._generated_catalog_request_variants.add(
            probe._catalog_variant_key(
                "/ad/api/pmc/v1/uni-promotion/ad/list-required",
                generated_body,
            )
        )

        class Request:
            method = "POST"
            post_data = json.dumps(generated_body)
            frame = SimpleNamespace(page=SimpleNamespace())

        class Response:
            status = 200
            url = (
                "https://qianchuan.jinritemai.com"
                "/ad/api/pmc/v1/uni-promotion/ad/list-required"
            )
            request = Request()

            async def json(self):
                return {"status_code": 0, "data": {"list": []}}

        with (
            patch.object(
                probe,
                "_replay_all_status_variant",
                AsyncMock(),
            ),
            patch.object(
                probe,
                "_replay_remaining_product_pages",
                AsyncMock(),
            ),
        ):
            asyncio.run(probe._on_response(Response()))

        retained = probe._catalog_base_templates[("product", "global")]
        self.assertEqual("observed_product_contract", retained["body"]["Params"]["SophonxDataSetKey"])

    def test_unfiltered_flat_plan_list_is_full_catalog_evidence(self):
        flat = {
            "aavid": "10001",
            "mar_goal": 1,
            "page": 1,
            "page_size": 100,
            "start_time": "2026-08-01",
            "end_time": "2026-08-02",
        }
        self.assertTrue(PromotionReadOnlyProbe._is_full_catalog_request(flat))
        flat["not_in_ecp_ad_statuses"] = ["PAUSED"]
        self.assertFalse(PromotionReadOnlyProbe._is_full_catalog_request(flat))

    def test_optional_catalog_segment_uses_required_response_session_context(self):
        path = os.path.join(self.temp.name, "session-context-probe.json")
        probe = PromotionReadOnlyProbe(path)
        probe.set_catalog_context(
            aavid="10001",
            promotion_scene="product",
            plan_system="global",
        )
        primary = probe._catalog_response_context(
            {
                "aavid": "10001",
                "mar_goal": 1,
                "page": 1,
                "page_size": 100,
            },
            {"data": {"sessionId": "transient-session"}},
        )
        probe.set_catalog_context(
            aavid="10001",
            promotion_scene="live",
            plan_system="global",
        )
        optional = probe._catalog_response_context(
            {"aavid": "10001", "SessionID": "transient-session"},
            {"data": {}},
        )

        self.assertEqual(("10001", "product", "global"), primary)
        self.assertEqual(primary, optional)

    def test_optional_segment_arriving_first_is_replayed_after_required_response(self):
        path = os.path.join(self.temp.name, "pending-session-probe.json")
        probe = PromotionReadOnlyProbe(path)
        probe.set_catalog_context(
            aavid="10001",
            promotion_scene="product",
            plan_system="global",
        )
        request_path = "/ad/api/pmc/v1/uni-promotion/ad/list-optional"
        probe._record_catalog_response_segment(
            path=request_path,
            body={"aavid": "10001", "SessionID": "late-primary"},
            http_status=200,
            payload={
                "status_code": 0,
                "data": {
                    "adInfos": [
                        {"id": "20002", "name": "后到上下文计划二"},
                        {"id": "20003", "name": "后到上下文计划三"},
                    ]
                },
            },
        )
        self.assertEqual([], probe.catalog_rows(
            aavid="10001", promotion_scene="product", plan_system="global"
        ))
        probe._record_catalog_response_segment(
            path="/ad/api/pmc/v1/uni-promotion/ad/list-required",
            body={
                "aavid": "10001",
                "mar_goal": 1,
                "page": 1,
                "page_size": 100,
            },
            http_status=200,
            payload={
                "status_code": 0,
                "data": {
                    "sessionId": "late-primary",
                    "adInfos": [{"id": "20001", "name": "主段计划一"}],
                },
            },
        )
        self.assertEqual(
            {"20001", "20002", "20003"},
            {
                row["ad_id"]
                for row in probe.catalog_rows(
                    aavid="10001",
                    promotion_scene="product",
                    plan_system="global",
                )
            },
        )
        self.assertEqual(
            3,
            PromotionReadOnlyProbe._response_total_pages(
                {
                    "data": {
                        "pagination": {
                            "totalCount": 201,
                            "pageSize": 100,
                        }
                    }
                }
            ),
        )

    def test_catalog_class_keeps_responses_captured_during_navigation(self):
        class CapturedProbe:
            def catalog_rows(self, **_kwargs):
                return []

            async def verify_catalog_plans(self, _page, **_kwargs):
                return {
                    "verified": [],
                    "rejected": [],
                    "complete": True,
                }

        controller = ServiceController.__new__(ServiceController)

        async def wait_for_existing_capture(_probe, **_kwargs):
            return {"complete": True, "message": ""}

        controller._wait_catalog_class = wait_for_existing_capture
        with patch(
            "services.run_services.current_session_owner",
            return_value=self.owner,
        ):
            result = asyncio.run(
                controller._scan_catalog_class(
                    fetcher=SimpleNamespace(page=object()),
                    probe=CapturedProbe(),
                    db=self.db,
                    owner_username=self.owner,
                    account={"aavid": "10001", "account_name": "测试账户"},
                    promotion_scene="product",
                    plan_system="global",
                    page_url="https://qianchuan.jinritemai.com/uni-prom?aavid=10001",
                )
            )
        self.assertTrue(result["complete"])

    def test_complete_pagination_is_not_downgraded_by_inactive_unverified_plan(self):
        class CapturedProbe:
            def catalog_rows(self, **_kwargs):
                return [
                    {
                        "ad_id": "30001",
                        "plan_name": "historical",
                        "platform_status": "paused",
                    }
                ]

            async def verify_catalog_plans(self, _page, **kwargs):
                self.requested_ids = list(kwargs.get("ad_ids") or [])
                return {"verified": [], "rejected": [], "complete": True}

        probe = CapturedProbe()
        controller = ServiceController.__new__(ServiceController)

        async def wait_for_complete(_probe, **_kwargs):
            return {"complete": True, "message": ""}

        controller._wait_catalog_class = wait_for_complete
        with patch(
            "services.run_services.current_session_owner",
            return_value=self.owner,
        ):
            result = asyncio.run(
                controller._scan_catalog_class(
                    fetcher=SimpleNamespace(page=object()),
                    probe=probe,
                    db=self.db,
                    owner_username=self.owner,
                    account={"aavid": "10001", "account_name": "account"},
                    promotion_scene="product",
                    plan_system="global",
                    page_url="https://qianchuan.jinritemai.com/uni-prom?aavid=10001",
                )
            )
        self.assertTrue(result["complete"])
        self.assertEqual([], probe.requested_ids)
        target = self.db.select_one(
            "promotion_target",
            where={"aadvid": "10001", "ad_id": "30001"},
        )
        self.assertEqual("candidate", target["verification_state"])
        self.assertEqual(0, target["monitor_eligible"])

    def test_complete_empty_class_preserves_previously_verified_plan(self):
        target = self._target(
            ad_id="30009",
            scene="live",
            system="chengfang",
        )
        self.db.update(
            "promotion_target",
            {
                "verification_state": "missing",
                "monitor_eligible": 0,
                "retarget_eligible": 0,
                "stop_eligible": 0,
            },
            where={"target_uid": target["target_uid"]},
        )

        class CapturedProbe:
            def catalog_rows(self, **_kwargs):
                return []

            async def verify_catalog_plans(self, _page, **_kwargs):
                return {"verified": [], "rejected": [], "complete": True}

        class Page:
            async def wait_for_timeout(self, _milliseconds):
                return None

        controller = ServiceController.__new__(ServiceController)

        async def wait_for_complete(_probe, **_kwargs):
            return {"complete": True, "message": ""}

        controller._wait_catalog_class = wait_for_complete
        with patch(
            "services.run_services.current_session_owner",
            return_value=self.owner,
        ):
            result = asyncio.run(
                controller._scan_catalog_class(
                    fetcher=SimpleNamespace(page=Page()),
                    probe=CapturedProbe(),
                    db=self.db,
                    owner_username=self.owner,
                    account={"aavid": "10001", "account_name": "account"},
                    promotion_scene="live",
                    plan_system="chengfang",
                    page_url=(
                        "https://qianchuan.jinritemai.com/uni-prom?aavid=10001"
                    ),
                )
            )
        self.assertFalse(result["complete"])
        self.assertIn("本轮返回0条", result["message"])
        saved = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )
        self.assertEqual("verified", saved["verification_state"])
        self.assertEqual(1, saved["monitor_eligible"])

    def test_same_plan_returned_by_two_classes_cannot_overwrite_first_class(self):
        misplaced_global = self._target(
            ad_id="30021",
            scene="live",
            system="chengfang",
        )
        real_chengfang = self._target(
            ad_id="30022",
            scene="live",
            system="chengfang",
        )
        self.db.update(
            "promotion_target",
            {
                "verification_state": "missing",
                "monitor_eligible": 0,
                "retarget_eligible": 0,
                "stop_eligible": 0,
            },
            where={"target_uid": real_chengfang["target_uid"]},
        )

        class CapturedProbe:
            def catalog_rows(self, **kwargs):
                if kwargs.get("promotion_scene") != "live":
                    return []
                return [
                    {
                        "ad_id": "30021",
                        "plan_name": "同一条接口返回计划",
                        "platform_status": "active",
                    }
                ]

            async def verify_catalog_plans(self, _page, **kwargs):
                verified = [
                    {
                        "ad_id": value,
                        "plan_name": "同一条接口返回计划",
                        "platform_status": "active",
                    }
                    for value in kwargs.get("ad_ids") or []
                ]
                return {
                    "verified": verified,
                    "rejected": [],
                    "complete": True,
                }

        class Page:
            async def wait_for_timeout(self, _milliseconds):
                return None

        controller = ServiceController.__new__(ServiceController)

        async def wait_for_complete(_probe, **_kwargs):
            return {"complete": True, "message": ""}

        controller._wait_catalog_class = wait_for_complete
        claims = {}
        common = {
            "fetcher": SimpleNamespace(page=Page()),
            "probe": CapturedProbe(),
            "db": self.db,
            "owner_username": self.owner,
            "account": {"aavid": "10001", "account_name": "account"},
            "promotion_scene": "live",
            "page_url": "https://qianchuan.jinritemai.com/uni-prom?aavid=10001",
            "claimed_plan_classes": claims,
        }
        with patch(
            "services.run_services.current_session_owner",
            return_value=self.owner,
        ):
            global_result = asyncio.run(
                controller._scan_catalog_class(
                    **common,
                    plan_system="global",
                )
            )
            chengfang_result = asyncio.run(
                controller._scan_catalog_class(
                    **common,
                    plan_system="chengfang",
                )
            )

        self.assertTrue(global_result["complete"])
        self.assertFalse(chengfang_result["complete"])
        self.assertIn("同时返回到多个分类", chengfang_result["message"])
        saved_global = self.db.select_one(
            "promotion_target",
            where={"target_uid": misplaced_global["target_uid"]},
        )
        saved_chengfang = self.db.select_one(
            "promotion_target",
            where={"target_uid": real_chengfang["target_uid"]},
        )
        self.assertEqual("global", saved_global["plan_system"])
        self.assertEqual("verified", saved_global["verification_state"])
        self.assertEqual("chengfang", saved_chengfang["plan_system"])
        self.assertEqual("verified", saved_chengfang["verification_state"])

    def test_existing_partial_empty_damage_is_repaired_on_account_load(self):
        target = self._target(
            ad_id="30010",
            scene="live",
            system="chengfang",
        )
        account = get_qianchuan_account(
            "10001",
            owner_username=self.owner,
            db=self.db,
        )
        self.db.update(
            "promotion_target",
            {
                "verification_state": "missing",
                "monitor_eligible": 0,
                "retarget_eligible": 0,
                "stop_eligible": 0,
            },
            where={"target_uid": target["target_uid"]},
        )
        self.db.update(
            "qianchuan_account",
            {
                "catalog_status": "partial",
                "catalog_counts_json": json.dumps(
                    {
                        "class_status": {
                            "chengfang_live": {
                                "complete": True,
                                "seen": 0,
                            }
                        }
                    }
                ),
            },
            where={"account_uid": account["account_uid"]},
        )

        list_qianchuan_accounts(
            owner_username=self.owner,
            db=self.db,
        )
        saved = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )
        self.assertEqual("verified", saved["verification_state"])
        self.assertEqual(1, saved["monitor_eligible"])

    def test_existing_cross_class_damage_is_repaired_on_account_load(self):
        misplaced_global = self._target(
            ad_id="30031",
            scene="live",
            system="chengfang",
        )
        real_chengfang = self._target(
            ad_id="30032",
            scene="live",
            system="chengfang",
        )
        account = get_qianchuan_account(
            "10001",
            owner_username=self.owner,
            db=self.db,
        )
        self.db.update(
            "promotion_target",
            {
                "verification_state": "missing",
                "monitor_eligible": 0,
                "retarget_eligible": 0,
                "stop_eligible": 0,
            },
            where={"target_uid": real_chengfang["target_uid"]},
        )
        self.db.update(
            "qianchuan_account",
            {
                "catalog_status": "partial",
                "catalog_counts_json": json.dumps(
                    {
                        "class_status": {
                            "global_live": {
                                "complete": True,
                                "seen": 1,
                                "seen_ids": ["30031"],
                            },
                            "chengfang_live": {
                                "complete": True,
                                "seen": 1,
                                "seen_ids": ["30031"],
                            },
                        }
                    }
                ),
            },
            where={"account_uid": account["account_uid"]},
        )

        list_qianchuan_accounts(
            owner_username=self.owner,
            db=self.db,
        )

        saved_global = self.db.select_one(
            "promotion_target",
            where={"target_uid": misplaced_global["target_uid"]},
        )
        saved_chengfang = self.db.select_one(
            "promotion_target",
            where={"target_uid": real_chengfang["target_uid"]},
        )
        saved_account = self.db.select_one(
            "qianchuan_account",
            where={"account_uid": account["account_uid"]},
        )
        self.assertEqual("global", saved_global["plan_system"])
        self.assertEqual("verified", saved_global["verification_state"])
        self.assertEqual("chengfang", saved_chengfang["plan_system"])
        self.assertEqual("verified", saved_chengfang["verification_state"])
        self.assertEqual("partial", saved_account["catalog_status"])
        self.assertIn("跨分类重复计划", saved_account["catalog_error"])

    def test_expired_cookie_never_bypasses_visible_login(self):
        target = {
            "aadvid": "10001",
            "ad_id": "30001",
            "promotion_scene": "product",
        }
        self.assertFalse(
            _can_reuse_startup_target(
                storage_state_available=True,
                reuse_last_target=True,
                startup_target=target,
                current_url="https://qianchuan.jinritemai.com/login",
            )
        )
        self.assertTrue(
            _can_reuse_startup_target(
                storage_state_available=True,
                reuse_last_target=True,
                startup_target=target,
                current_url=(
                    "https://qianchuan.jinritemai.com/uni-prom/detail"
                    "?aavid=10001&adId=30001"
                ),
            )
        )

    def test_exact_global_page_title_is_system_evidence_but_promo_copy_is_not(self):
        self.assertEqual(
            "global",
            detect_plan_system(page_text="首页\n全域投放\n推商品"),
        )
        self.assertEqual(
            "unknown",
            detect_plan_system(page_text="商品全域投放产品手册"),
        )


if __name__ == "__main__":
    unittest.main()
