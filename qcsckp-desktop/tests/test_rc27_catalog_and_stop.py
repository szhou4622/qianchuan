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
