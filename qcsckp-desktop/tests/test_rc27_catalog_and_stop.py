# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
