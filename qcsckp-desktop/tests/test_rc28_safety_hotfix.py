# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from api.promotion_targets import (
    migrate_legacy_target_scope,
    normalize_platform_status,
    patch_target_sync_state,
    record_target_verification_failure,
    set_target_automation_write_block,
    update_target_catalog_evidence,
    upsert_promotion_target,
)
from services.fetcher import QianChuanFetcher
from services.promotion_readonly_probe import PromotionReadOnlyProbe
from services.qianchuan_accounts import (
    ensure_qianchuan_account,
    migrate_existing_qianchuan_accounts,
    save_qianchuan_account_automation_setup,
)
from services.qianchuan_session import save_context_storage_state
from services.retargeting_service import QianChuanRetargetingService
from services.run_services import (
    CatalogLoginRequired,
    ServiceController,
    _page_is_closed,
    _persist_verified_catalog_class,
    _qianchuan_authenticated_shell_visible,
    _resolved_startup_platform_status,
    _trusted_qianchuan_detail_ids,
    _visible_qianchuan_login_failure,
    _visible_plan_detail_ad_id,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class Rc28SafetyHotfixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "rc28.db")
        init_sqlite_schema(database=self.db_path)
        self.db = SQLiteStore(database=self.db_path)
        self.owner = "rc28-owner"

    def test_system_paused_status_is_normalized_and_remains_ineligible(self):
        self.assertEqual("paused", normalize_platform_status("系统暂停"))

    def test_saved_session_keeps_verified_current_plan_status(self):
        # A detail page also contains sibling rows. A paused sibling must not
        # downgrade the exact catalog-verified active target being resumed.
        self.assertEqual(
            "active",
            _resolved_startup_platform_status(
                "已暂停 投放中",
                None,
                existing_status="active",
                preserve_existing=True,
            ),
        )

    def test_exact_payload_status_overrides_saved_catalog_status(self):
        self.assertEqual(
            "paused",
            _resolved_startup_platform_status(
                "投放中",
                "paused",
                existing_status="active",
                preserve_existing=True,
            ),
        )

    def test_resolved_cross_scene_catalog_candidate_is_not_persisted(self):
        result = _persist_verified_catalog_class(
            self.db,
            aavid="10001",
            account_name="账户",
            promotion_scene="product",
            plan_system="global",
            page_url="https://qianchuan.jinritemai.com/uni-prom?aavid=10001",
            candidates=[{"ad_id": "30001", "plan_name": "直播候选"}],
            verification={
                "verified": [],
                "rejected": [
                    {
                        "ad_id": "30001",
                        "reason": "精确详情推广方式不匹配",
                        "resolved": True,
                    }
                ],
                "complete": True,
            },
            owner_username=self.owner,
            class_complete=True,
        )
        self.assertEqual([], result["seen_ids"])
        self.assertIsNone(
            self.db.select_one(
                "promotion_target",
                where={"aadvid": "10001", "ad_id": "30001"},
            )
        )

    def tearDown(self):
        self.temp.cleanup()

    def _target(
        self,
        *,
        ad_id: str,
        status: str,
        verification: str,
        enabled: bool = False,
    ):
        return upsert_promotion_target(
            {
                "aavid": "10001",
                "ad_id": ad_id,
                "plan_name": f"plan-{ad_id}",
                "promotion_scene": "live",
                "plan_system": "global",
                "platform_status": status,
                "verification_state": verification,
                "enabled": enabled,
            },
            owner_username=self.owner,
            trusted_catalog=True,
            db=self.db,
        )

    def test_visible_login_state_captures_session_storage(self):
        class FakePage:
            async def is_closed(self):
                return False

            async def evaluate(self, _script):
                return {
                    "origin": "https://qianchuan.jinritemai.com",
                    "entries": {"session-token": "opaque-value"},
                }

        class FakeContext:
            pages = [FakePage()]

            async def storage_state(self, *, indexed_db=None):
                self.indexed_db = indexed_db
                return {"cookies": [], "origins": []}

        context = FakeContext()
        with patch(
            "services.qianchuan_session.save_qianchuan_storage_state",
            side_effect=lambda state, **_kwargs: state,
        ):
            saved = asyncio.run(
                save_context_storage_state(
                    context,
                    owner_username=self.owner,
                )
            )
        self.assertTrue(context.indexed_db)
        self.assertEqual(
            saved["_qcsckp_session_storage"][
                "https://qianchuan.jinritemai.com"
            ]["session-token"],
            "opaque-value",
        )

    def test_fetcher_restores_encrypted_session_storage_before_navigation(self):
        captured = {}

        class FakeContext:
            async def add_init_script(self, *, script):
                captured["script"] = script

            async def new_page(self):
                return object()

        class FakeBrowser:
            async def new_context(self, **kwargs):
                captured["context_kwargs"] = kwargs
                return FakeContext()

        class FakeChromium:
            async def launch(self, **_kwargs):
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakeStarter:
            async def start(self):
                return FakePlaywright()

        fetcher = QianChuanFetcher(
            headless=True,
            storage_state={
                "cookies": [],
                "origins": [],
                "_qcsckp_session_storage": {
                    "https://qianchuan.jinritemai.com": {
                        "session-token": "opaque-value"
                    }
                },
            },
        )
        with (
            patch(
                "services.fetcher.async_playwright",
                return_value=FakeStarter(),
            ),
            patch(
                "services.fetcher.require_executable_path",
                return_value="chrome.exe",
            ),
        ):
            asyncio.run(fetcher._init_browser())
        self.assertNotIn(
            "_qcsckp_session_storage",
            captured["context_kwargs"]["storage_state"],
        )
        self.assertIn("sessionStorage.setItem", captured["script"])
        self.assertIn("session-token", captured["script"])

    def test_existing_ineligible_selection_no_longer_deadlocks_account_save(self):
        account = ensure_qianchuan_account(
            "10001",
            owner_username=self.owner,
            seen=True,
            db=self.db,
        )
        target = self._target(
            ad_id="30001",
            status="unknown",
            verification="legacy_unverified",
            enabled=True,
        )
        feishu = {
            "connected": True,
            "profile": {"authorized_open_id": "ou_owner"},
        }
        with patch(
            "services.local_feishu_bridge.get_local_feishu_status",
            return_value=feishu,
        ):
            saved = save_qianchuan_account_automation_setup(
                account["account_uid"],
                {
                    "enabled": True,
                    "report_enabled": True,
                    "route_mode": "default",
                },
                [{"target_uid": target["target_uid"], "enabled": True}],
                owner_username=self.owner,
                db=self.db,
            )
            self.assertTrue(saved["enabled"])
            self.assertFalse(
                self.db.select_one(
                    "promotion_target",
                    where={"target_uid": target["target_uid"]},
                )["monitor_eligible"]
            )
            save_qianchuan_account_automation_setup(
                account["account_uid"],
                {
                    "enabled": True,
                    "report_enabled": True,
                    "route_mode": "default",
                },
                [{"target_uid": target["target_uid"], "enabled": False}],
                owner_username=self.owner,
                db=self.db,
            )
        self.assertFalse(
            self.db.select_one(
                "promotion_target",
                where={"target_uid": target["target_uid"]},
            )["enabled"]
        )

    def test_disabling_account_never_requires_feishu_binding(self):
        account = ensure_qianchuan_account(
            "10001",
            owner_username=self.owner,
            enabled=True,
            seen=True,
            db=self.db,
        )
        self.db.update(
            "qianchuan_account",
            {"route_mode": "custom"},
            where={"account_uid": account["account_uid"]},
        )
        with patch(
            "services.local_feishu_bridge.get_local_feishu_status",
            return_value={"connected": False, "profile": {}},
        ):
            saved = save_qianchuan_account_automation_setup(
                account["account_uid"],
                {
                    "enabled": False,
                    "report_enabled": False,
                    "route_mode": "custom",
                    "route_send_personal": False,
                    "route_group_ids": [],
                },
                [],
                owner_username=self.owner,
                db=self.db,
            )
        self.assertFalse(saved["enabled"])

    def test_enabling_account_and_plan_does_not_require_feishu_binding(self):
        account = ensure_qianchuan_account(
            "10001",
            owner_username=self.owner,
            seen=True,
            db=self.db,
        )
        target = self._target(
            ad_id="30001",
            status="active",
            verification="verified",
        )
        saved = save_qianchuan_account_automation_setup(
            account["account_uid"],
            {
                "enabled": True,
                "report_enabled": False,
                "route_mode": "custom",
                "route_send_personal": False,
                "route_group_ids": [],
            },
            [{"target_uid": target["target_uid"], "enabled": True}],
            owner_username=self.owner,
            db=self.db,
        )
        self.assertTrue(saved["enabled"])
        persisted = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )
        self.assertTrue(persisted["enabled"])

    def test_candidate_list_cannot_promote_unknown_verified_target_to_active(self):
        original = self._target(
            ad_id="30002",
            status="unknown",
            verification="verified",
        )
        refreshed = upsert_promotion_target(
            {
                "aavid": "10001",
                "ad_id": "30002",
                "promotion_scene": "live",
                "plan_system": "global",
                "platform_status": "active",
                "verification_state": "candidate",
                "enabled": original["enabled"],
            },
            owner_username=self.owner,
            trusted_catalog=True,
            db=self.db,
        )
        self.assertEqual("verified", refreshed["verification_state"])
        self.assertEqual("unknown", refreshed["platform_status"])
        self.assertFalse(refreshed["monitor_eligible"])

    def test_plain_upsert_does_not_claim_unowned_legacy_target(self):
        self.db.insert(
            "promotion_target",
            {
                "target_uid": "target_legacy",
                "account_uid": "",
                "aadvid": "10001",
                "ad_id": "30003",
                "plan_name": "legacy",
                "promotion_scene": "live",
                "plan_system": "global",
                "platform_status": "active",
                "verification_state": "verified",
                "monitor_eligible": 1,
                "retarget_eligible": 1,
                "stop_eligible": 1,
                "enabled": 1,
                "capability_json": '{"retarget_execute": true}',
            },
        )
        saved = self._target(
            ad_id="30003",
            status="active",
            verification="verified",
        )
        self.assertNotEqual("target_legacy", saved["target_uid"])
        legacy = self.db.select_one(
            "promotion_target", where={"target_uid": "target_legacy"}
        )
        self.assertNotEqual(saved["account_uid"], legacy["account_uid"])
        self.assertTrue(
            str(legacy["account_uid"]).startswith("legacy_quarantined_")
        )
        self.assertFalse(legacy["enabled"])
        self.assertFalse(legacy["monitor_eligible"])
        self.assertFalse(legacy["retarget_eligible"])
        self.assertFalse(legacy["stop_eligible"])
        self.assertTrue(legacy["automation_write_blocked"])
        self.assertEqual("{}", legacy["capability_json"])

    def test_verification_failure_is_atomic_fail_closed_and_explicit_delivery_recovers(
        self,
    ):
        ensure_qianchuan_account(
            "10001",
            owner_username=self.owner,
            enabled=True,
            seen=True,
            db=self.db,
        )
        target = self._target(
            ad_id="30006",
            status="active",
            verification="verified",
            enabled=True,
        )
        verified_at = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )["last_verified_at"]

        failed = record_target_verification_failure(
            target["target_uid"],
            "本轮没有取得明确 delivering 证据",
            db=self.db,
        )
        self.assertEqual(verified_at, failed["last_verified_at"])
        self.assertEqual("error", failed["verification_state"])
        self.assertEqual("unknown", failed["platform_status"])
        self.assertFalse(failed["monitor_eligible"])
        self.assertFalse(failed["retarget_eligible"])
        self.assertFalse(failed["stop_eligible"])
        self.assertTrue(failed["automation_write_blocked"])
        self.assertEqual("disabled", failed["capacity_state"])

        # 真实采集链还会继续写同步状态；该步骤不能重新放开资格。
        patch_target_sync_state(
            target["target_uid"],
            status="verification_error",
            error="本轮没有取得明确 delivering 证据",
            capability_updates={"assist_sync_ok": False},
            db=self.db,
        )
        still_failed = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )
        self.assertFalse(still_failed["monitor_eligible"])
        self.assertTrue(still_failed["automation_write_blocked"])

        recovered = update_target_catalog_evidence(
            target["target_uid"],
            platform_status="active",
            verification_state="verified",
            db=self.db,
        )
        self.assertTrue(recovered["monitor_eligible"])
        self.assertTrue(recovered["retarget_eligible"])
        self.assertTrue(recovered["stop_eligible"])
        self.assertFalse(recovered["automation_write_blocked"])
        self.assertEqual("active", recovered["capacity_state"])
        self.assertFalse(recovered["last_verification_error"])

        # 既有人工安全锁必须穿过“核验失败 -> 再次明确投放中”全过程。
        with patch(
            "api.promotion_targets._owner_key",
            return_value=self.owner,
        ):
            manually_blocked = set_target_automation_write_block(
                target["target_uid"],
                True,
                reason="人工安全封锁",
                db=self.db,
            )
        self.assertEqual("manual", manually_blocked["write_block_origin"])
        failed_under_manual_lock = record_target_verification_failure(
            target["target_uid"],
            "临时没有取得投放中证据",
            db=self.db,
        )
        self.assertTrue(failed_under_manual_lock["automation_write_blocked"])
        self.assertEqual(
            "人工安全封锁",
            failed_under_manual_lock["write_block_reason"],
        )
        self.assertEqual(
            "manual",
            failed_under_manual_lock["write_block_origin"],
        )
        still_manually_blocked = update_target_catalog_evidence(
            target["target_uid"],
            platform_status="active",
            verification_state="verified",
            db=self.db,
        )
        self.assertTrue(
            still_manually_blocked["automation_write_blocked"]
        )
        self.assertEqual(
            "人工安全封锁",
            still_manually_blocked["write_block_reason"],
        )
        self.assertEqual(
            "manual",
            still_manually_blocked["write_block_origin"],
        )

    def test_catalog_fresh_detail_releases_only_transient_verification_lock(self):
        ensure_qianchuan_account(
            "10001",
            owner_username=self.owner,
            enabled=True,
            seen=True,
            db=self.db,
        )
        target = self._target(
            ad_id="30016",
            status="active",
            verification="verified",
            enabled=True,
        )
        failed = record_target_verification_failure(
            target["target_uid"],
            "temporary exact-detail timeout",
            db=self.db,
        )
        verified_at = failed["last_verified_at"]

        common = {
            "db": self.db,
            "aavid": "10001",
            "account_name": "account",
            "promotion_scene": "live",
            "plan_system": "global",
            "page_url": (
                "https://qianchuan.jinritemai.com/uni-prom?aavid=10001"
            ),
            "candidates": [
                {
                    "ad_id": "30016",
                    "plan_name": "plan-30016",
                    "platform_status": "active",
                }
            ],
            "owner_username": self.owner,
            "class_complete": True,
        }

        _persist_verified_catalog_class(
            **common,
            verification={
                "verified": [
                    {
                        "ad_id": "30016",
                        "plan_name": "plan-30016",
                        "platform_status": "active",
                        "verification_evidence_fresh": False,
                    }
                ],
                "rejected": [],
                "complete": True,
            },
        )
        cached = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )
        self.assertTrue(cached["automation_write_blocked"])
        self.assertEqual("verification_failure", cached["write_block_origin"])
        self.assertEqual(verified_at, cached["last_verified_at"])

        _persist_verified_catalog_class(
            **common,
            verification={
                "verified": [
                    {
                        "ad_id": "30016",
                        "plan_name": "plan-30016",
                        "platform_status": "active",
                        "verification_evidence_fresh": True,
                    }
                ],
                "rejected": [],
                "complete": True,
            },
        )
        recovered = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )
        self.assertFalse(recovered["automation_write_blocked"])
        self.assertEqual("", recovered["write_block_origin"])
        self.assertEqual("", recovered["last_verification_error"])
        self.assertEqual(1, recovered["monitor_eligible"])

        with patch(
            "api.promotion_targets._owner_key",
            return_value=self.owner,
        ):
            set_target_automation_write_block(
                target["target_uid"],
                True,
                reason="manual review",
                db=self.db,
            )
        _persist_verified_catalog_class(
            **common,
            verification={
                "verified": [
                    {
                        "ad_id": "30016",
                        "plan_name": "plan-30016",
                        "platform_status": "active",
                        "verification_evidence_fresh": True,
                    }
                ],
                "rejected": [],
                "complete": True,
            },
        )
        manual = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )
        self.assertTrue(manual["automation_write_blocked"])
        self.assertEqual("manual", manual["write_block_origin"])

    def test_legacy_scoped_conflict_migration_prefers_scoped_without_authority_merge(
        self,
    ):
        account = ensure_qianchuan_account(
            "10003",
            owner_username=self.owner,
            enabled=True,
            seen=True,
            db=self.db,
        )
        scoped = self.db.insert(
            "promotion_target",
            {
                "target_uid": "target_scoped_keep",
                "account_uid": account["account_uid"],
                "aadvid": "10003",
                "ad_id": "30007",
                "plan_name": "scoped",
                "promotion_scene": "live",
                "plan_system": "global",
                "platform_status": "unknown",
                "verification_state": "candidate",
                "monitor_eligible": 0,
                "retarget_eligible": 0,
                "stop_eligible": 0,
                "enabled": 0,
                "capability_json": "{}",
                "capacity_state": "disabled",
            },
        )
        self.assertTrue(scoped)
        self.db.insert(
            "promotion_target",
            {
                "target_uid": "target_legacy_conflict",
                "account_uid": "",
                "aadvid": "10003",
                "ad_id": "30007",
                "plan_name": "legacy-powerful",
                "promotion_scene": "live",
                "plan_system": "global",
                "platform_status": "active",
                "verification_state": "verified",
                "monitor_eligible": 1,
                "retarget_eligible": 1,
                "stop_eligible": 1,
                "enabled": 1,
                "capability_json": '{"retarget_execute": true}',
                "capacity_state": "active",
            },
        )

        with (
            patch(
                "api.promotion_targets._owner_key",
                return_value=self.owner,
            ),
            patch(
                "services.qianchuan_accounts.DAILY_CONFIG_FILE",
                os.path.join(self.temp.name, "daily.json"),
            ),
        ):
            migrate_legacy_target_scope(db=self.db)
            # 后续账户迁移不再因 owner+aavid+ad_id 唯一键冲突。
            migrate_existing_qianchuan_accounts(
                owner_username=self.owner,
                authorized_aavids={"10003"},
                db=self.db,
            )

        kept = self.db.select_one(
            "promotion_target",
            where={"target_uid": "target_scoped_keep"},
        )
        legacy = self.db.select_one(
            "promotion_target",
            where={"target_uid": "target_legacy_conflict"},
        )
        self.assertEqual(account["account_uid"], kept["account_uid"])
        self.assertEqual("scoped", kept["plan_name"])
        self.assertFalse(kept["monitor_eligible"])
        self.assertTrue(
            str(legacy["account_uid"]).startswith("legacy_quarantined_")
        )
        self.assertNotEqual(account["account_uid"], legacy["account_uid"])
        self.assertFalse(legacy["enabled"])
        self.assertFalse(legacy["monitor_eligible"])
        self.assertFalse(legacy["retarget_eligible"])
        self.assertFalse(legacy["stop_eligible"])
        self.assertTrue(legacy["automation_write_blocked"])
        self.assertEqual("{}", legacy["capability_json"])

    def test_explicit_legacy_migration_clears_automation_authority(self):
        self.db.insert(
            "promotion_target",
            {
                "target_uid": "target_legacy_migrate",
                "account_uid": "",
                "aadvid": "10002",
                "ad_id": "30004",
                "plan_name": "legacy",
                "promotion_scene": "live",
                "plan_system": "global",
                "platform_status": "active",
                "verification_state": "verified",
                "monitor_eligible": 1,
                "retarget_eligible": 1,
                "stop_eligible": 1,
                "enabled": 1,
                "capability_json": '{"retarget_execute": true}',
                "last_status": "ok",
            },
        )
        with patch(
            "services.qianchuan_accounts.DAILY_CONFIG_FILE",
            os.path.join(self.temp.name, "daily.json"),
        ):
            migrate_existing_qianchuan_accounts(
                owner_username=self.owner,
                authorized_aavids={"10002"},
                db=self.db,
            )
        migrated = self.db.select_one(
            "promotion_target",
            where={"target_uid": "target_legacy_migrate"},
        )
        self.assertTrue(migrated["account_uid"])
        self.assertFalse(migrated["enabled"])
        self.assertEqual("unknown", migrated["platform_status"])
        self.assertEqual("legacy_unverified", migrated["verification_state"])
        self.assertEqual("{}", migrated["capability_json"])
        self.assertFalse(migrated["monitor_eligible"])

    def test_unowned_history_is_not_claimed_without_authorized_account_evidence(
        self,
    ):
        self.db.insert(
            "pmc_ad_detail_basic",
            {
                "aadvid": "10009",
                "ad_id": "30009",
                "user_info_name": "不应被当前工具账号认领",
                "account_uid": "",
            },
        )
        migrate_existing_qianchuan_accounts(
            owner_username=self.owner,
            db=self.db,
        )
        self.assertIsNone(
            self.db.select_one(
                "qianchuan_account",
                where={"owner_username": self.owner, "aavid": "10009"},
            )
        )
        self.assertEqual(
            "",
            self.db.select_one(
                "pmc_ad_detail_basic",
                where={"aadvid": "10009", "ad_id": "30009"},
            )["account_uid"],
        )

    def test_login_only_saves_session_after_authorized_accounts_are_visible(self):
        class FakePage:
            url = "https://qianchuan.jinritemai.com/home"

            async def goto(self, url, **_kwargs):
                self.url = url

            async def bring_to_front(self):
                return None

        class FakeContext:
            def __init__(self, page):
                self.pages = [page]

        class FakeFetcher:
            def __init__(self):
                self.page = FakePage()
                self.context = FakeContext(self.page)
                self.closed = False

            async def _init_browser(self):
                return None

            async def close(self):
                self.closed = True

        class FakeProbe:
            def __init__(self, *_args, **_kwargs):
                pass

            def attach(self, _page):
                return None

            async def observe_page(self, _page):
                return None

            async def discover_authorized_accounts(self, _page, **_kwargs):
                return [{"aavid": "10001", "account_name": "授权账户"}]

        cfg = SimpleNamespace(
            db_path=self.db_path,
            open_url="https://qianchuan.jinritemai.com/home",
        )
        cfg.normalize_paths = lambda: cfg
        controller = ServiceController()
        fetcher = FakeFetcher()
        save_state = AsyncMock()
        mark_available = Mock()
        with (
            patch(
                "services.run_services.current_session_owner",
                return_value=self.owner,
            ),
            patch("services.run_services.ServiceConfig", return_value=cfg),
            patch(
                "services.run_services.load_qianchuan_storage_state",
                return_value=None,
            ),
            patch("services.run_services.migrate_legacy_qcookie"),
            patch(
                "services.run_services.QianChuanFetcher",
                return_value=fetcher,
            ),
            patch(
                "services.run_services.PromotionReadOnlyProbe",
                FakeProbe,
            ),
            patch(
                "services.run_services.save_context_storage_state",
                save_state,
            ),
            patch(
                "services.run_services.mark_qianchuan_session_available",
                mark_available,
            ),
        ):
            asyncio.run(
                controller._target_discovery_async(login_only=True)
            )
        status = controller.target_discovery_status()
        self.assertTrue(status["success"])
        self.assertTrue(status["relogin_complete"])
        self.assertFalse(status["running"])
        save_state.assert_awaited_once()
        mark_available.assert_called_once_with(owner_username=self.owner)
        self.assertTrue(fetcher.closed)

    def test_account_selection_adds_current_account_without_plan_detail(self):
        class FakePage:
            url = "https://qianchuan.jinritemai.com/uni-prom"

            async def goto(self, url, **_kwargs):
                self.url = url

            async def bring_to_front(self):
                return None

        class FakeContext:
            def __init__(self, page):
                self.pages = [page]

        class FakeFetcher:
            def __init__(self):
                self.page = FakePage()
                self.context = FakeContext(self.page)
                self.closed = False

            async def _init_browser(self):
                return None

            async def close(self):
                self.closed = True

        class FakeProbe:
            def __init__(self, *_args, **_kwargs):
                pass

            def attach(self, _page):
                return None

            async def observe_page(self, _page):
                return None

            def latest_observed_aavid(self):
                return "10002"

            async def current_account_name(self, _page):
                return "火"

            def authorized_accounts(self):
                return [
                    {
                        "aavid": "10002",
                        "account_name": "火橙-船奇日化-千川",
                    }
                ]

        cfg = SimpleNamespace(
            db_path=self.db_path,
            open_url="https://qianchuan.jinritemai.com/home",
            wait_url_prefix="https://qianchuan.jinritemai.com/",
        )
        cfg.normalize_paths = lambda: cfg
        controller = ServiceController()
        fetcher = FakeFetcher()
        save_state = AsyncMock()
        mark_available = Mock()
        with (
            patch(
                "services.run_services.current_session_owner",
                return_value=self.owner,
            ),
            patch("services.run_services.ServiceConfig", return_value=cfg),
            patch(
                "services.run_services.load_qianchuan_storage_state",
                return_value={"cookies": []},
            ),
            patch("services.run_services.migrate_legacy_qcookie"),
            patch(
                "services.run_services.QianChuanFetcher",
                return_value=fetcher,
            ),
            patch(
                "services.run_services.PromotionReadOnlyProbe",
                FakeProbe,
            ),
            patch(
                "services.run_services.save_context_storage_state",
                save_state,
            ),
            patch(
                "services.run_services.mark_qianchuan_session_available",
                mark_available,
            ),
        ):
            asyncio.run(
                controller._target_discovery_async(account_only=True)
            )
        status = controller.target_discovery_status()
        self.assertTrue(status["success"])
        self.assertFalse(status["running"])
        self.assertEqual("10002", status["account"]["aavid"])
        self.assertIsNone(status["target"])
        saved = self.db.select_one(
            "qianchuan_account",
            where={"owner_username": self.owner, "aavid": "10002"},
        )
        self.assertEqual(1, int(saved["directory_selected"]))
        self.assertEqual("火橙-船奇日化-千川", saved["account_name"])
        save_state.assert_awaited_once()
        mark_available.assert_called_once_with(owner_username=self.owner)
        self.assertTrue(fetcher.closed)

    def test_account_selection_prefers_current_detail_url_over_stale_probe(self):
        class FakePage:
            url = "about:blank"

            async def goto(self, _url, **_kwargs):
                self.url = (
                    "https://qianchuan.jinritemai.com/uni-prom/detail"
                    "?aavid=10002&adId=30002"
                )

            async def bring_to_front(self):
                return None

            def is_closed(self):
                return False

        class FakeContext:
            def __init__(self, page):
                self.pages = [page]

        class FakeFetcher:
            def __init__(self):
                self.page = FakePage()
                self.context = FakeContext(self.page)
                self.closed = False

            async def _init_browser(self):
                return None

            async def close(self):
                self.closed = True

        class FakeProbe:
            def __init__(self, *_args, **_kwargs):
                pass

            def attach(self, _page):
                return None

            async def observe_page(self, _page):
                return None

            def latest_observed_aavid(self):
                return "10001"

            async def current_account_name(self, _page):
                return "网址中的当前账户"

            def authorized_accounts(self):
                return []

        cfg = SimpleNamespace(
            db_path=self.db_path,
            open_url="https://qianchuan.jinritemai.com/home",
            wait_url_prefix="https://qianchuan.jinritemai.com/",
        )
        cfg.normalize_paths = lambda: cfg
        controller = ServiceController()
        fetcher = FakeFetcher()
        with (
            patch(
                "services.run_services.current_session_owner",
                return_value=self.owner,
            ),
            patch("services.run_services.ServiceConfig", return_value=cfg),
            patch(
                "services.run_services.load_qianchuan_storage_state",
                return_value={"cookies": []},
            ),
            patch("services.run_services.migrate_legacy_qcookie"),
            patch(
                "services.run_services.QianChuanFetcher",
                return_value=fetcher,
            ),
            patch(
                "services.run_services.PromotionReadOnlyProbe",
                FakeProbe,
            ),
            patch(
                "services.run_services.save_context_storage_state",
                AsyncMock(),
            ),
            patch(
                "services.run_services.mark_qianchuan_session_available"
            ),
        ):
            asyncio.run(
                controller._target_discovery_async(account_only=True)
            )
        status = controller.target_discovery_status()
        self.assertFalse(status["running"])
        self.assertEqual("10002", status["account"]["aavid"])
        self.assertEqual(
            "网址中的当前账户", status["account"]["account_name"]
        )
        self.assertTrue(fetcher.closed)

    def test_account_selection_login_failure_releases_running_state(self):
        class FakeLocator:
            async def inner_text(self, **_kwargs):
                return "登录失败，请重新扫码"

        class FakePage:
            url = "about:blank"

            async def goto(self, url, **_kwargs):
                self.url = url

            async def bring_to_front(self):
                return None

            def is_closed(self):
                return False

            def locator(self, _selector):
                return FakeLocator()

        class FakeContext:
            def __init__(self, page):
                self.pages = [page]

        class FakeFetcher:
            def __init__(self):
                self.page = FakePage()
                self.context = FakeContext(self.page)
                self.closed = False

            async def _init_browser(self):
                return None

            async def close(self):
                self.closed = True

        class FakeProbe:
            def __init__(self, *_args, **_kwargs):
                pass

            def attach(self, _page):
                return None

        cfg = SimpleNamespace(
            db_path=self.db_path,
            open_url="https://sso.oceanengine.com/login",
            wait_url_prefix="https://qianchuan.jinritemai.com/",
        )
        cfg.normalize_paths = lambda: cfg
        controller = ServiceController()
        fetcher = FakeFetcher()
        with (
            patch(
                "services.run_services.current_session_owner",
                return_value=self.owner,
            ),
            patch("services.run_services.ServiceConfig", return_value=cfg),
            patch(
                "services.run_services.load_qianchuan_storage_state",
                return_value=None,
            ),
            patch("services.run_services.migrate_legacy_qcookie"),
            patch(
                "services.run_services.QianChuanFetcher",
                return_value=fetcher,
            ),
            patch(
                "services.run_services.PromotionReadOnlyProbe",
                FakeProbe,
            ),
        ):
            controller._target_discovery_entry(account_only=True)
        status = controller.target_discovery_status()
        self.assertFalse(status["success"])
        self.assertFalse(status["running"])
        self.assertIn("登录失败", status["message"])
        self.assertIn("重新点击", status["message"])
        self.assertTrue(fetcher.closed)

    def test_page_close_and_login_failure_helpers_are_fail_fast(self):
        class ClosedPage:
            def is_closed(self):
                return True

        class FailureLocator:
            async def inner_text(self, **_kwargs):
                return "账号或密码错误"

        class FailurePage:
            def locator(self, _selector):
                return FailureLocator()

        self.assertTrue(asyncio.run(_page_is_closed(ClosedPage())))
        self.assertEqual(
            ("10002", "30002"),
            _trusted_qianchuan_detail_ids(
                "https://qianchuan.jinritemai.com/uni-prom/detail"
                "?aavid=10002&adId=30002"
            ),
        )
        self.assertEqual(
            (None, None),
            _trusted_qianchuan_detail_ids(
                "https://example.com/uni-prom/detail"
                "?aavid=10002&adId=30002"
            ),
        )
        self.assertEqual(
            "账号或密码错误",
            asyncio.run(_visible_qianchuan_login_failure(FailurePage())),
        )

    def test_existing_account_plan_detail_confirms_and_closes_browser(self):
        ensure_qianchuan_account(
            "10001",
            account_name="已添加账户",
            owner_username=self.owner,
            directory_selected=True,
            seen=True,
            db=self.db,
        )

        class FakePage:
            url = "https://qianchuan.jinritemai.com/uni-prom"

            async def goto(self, url, **_kwargs):
                self.url = url

            async def bring_to_front(self):
                return None

        class FakeContext:
            def __init__(self, page):
                self.pages = [page]

        class FakeFetcher:
            def __init__(self):
                self.page = FakePage()
                self.context = FakeContext(self.page)
                self.closed = False

            async def _init_browser(self):
                return None

            async def close(self):
                self.closed = True

        class FakeProbe:
            def __init__(self, *_args, **_kwargs):
                pass

            def attach(self, _page):
                return None

            async def observe_page(self, _page):
                return None

            def latest_observed_aavid(self):
                return "10001"

            def latest_observed_detail_ad_id(self, aavid=""):
                return "30001" if aavid == "10001" else ""

        cfg = SimpleNamespace(
            db_path=self.db_path,
            open_url="https://qianchuan.jinritemai.com/home",
            wait_url_prefix="https://qianchuan.jinritemai.com/",
        )
        cfg.normalize_paths = lambda: cfg
        controller = ServiceController()
        fetcher = FakeFetcher()
        save_state = AsyncMock()
        mark_available = Mock()
        with (
            patch(
                "services.run_services.current_session_owner",
                return_value=self.owner,
            ),
            patch("services.run_services.ServiceConfig", return_value=cfg),
            patch(
                "services.run_services.load_qianchuan_storage_state",
                return_value={"cookies": []},
            ),
            patch("services.run_services.migrate_legacy_qcookie"),
            patch(
                "services.run_services.QianChuanFetcher",
                return_value=fetcher,
            ),
            patch(
                "services.run_services.PromotionReadOnlyProbe",
                FakeProbe,
            ),
            patch(
                "services.run_services.save_context_storage_state",
                save_state,
            ),
            patch(
                "services.run_services.mark_qianchuan_session_available",
                mark_available,
            ),
        ):
            asyncio.run(
                controller._target_discovery_async(account_only=True)
            )
        status = controller.target_discovery_status()
        self.assertTrue(status["success"])
        self.assertFalse(status["running"])
        self.assertEqual("10001", status["account"]["aavid"])
        self.assertIn("检测到计划详情", status["message"])
        self.assertIsNone(status["target"])
        rows = self.db.select(
            "qianchuan_account",
            where={"owner_username": self.owner, "aavid": "10001"},
        )
        self.assertEqual(1, len(rows))
        save_state.assert_awaited_once()
        mark_available.assert_called_once_with(owner_username=self.owner)
        self.assertTrue(fetcher.closed)

    def test_detail_probe_ignores_plan_list_and_accepts_detail_request(self):
        probe = PromotionReadOnlyProbe.__new__(PromotionReadOnlyProbe)
        probe._requests = {
            "list": {
                "path": "/ad/api/pmc/v1/uni-promotion/ad/list-required",
                "observed_at": "2026-07-30 23:00:00",
                "identifiers": {"aavid": "10001"},
                "fields": [{"key": "adId", "value": "20001"}],
            },
            "detail": {
                "path": "/ad/api/creation/v1/ad/ad-detail-basic",
                "observed_at": "2026-07-30 23:00:01",
                "identifiers": {"aavid": "10001"},
                "fields": [{"key": "adId", "value": "30001"}],
            },
        }
        probe._apis = {}
        self.assertEqual(
            "30001",
            probe.latest_observed_detail_ad_id("10001"),
        )
        self.assertEqual(
            "",
            probe.latest_observed_detail_ad_id("10002"),
        )

    def test_visible_product_detail_marker_is_fast_fallback(self):
        detail_text = (
            "减咖鲜酱油2.5 投放中 ID：1869678213573940 "
            "预算(元)：每日5,000.00 净成交ROI目标：3.00 "
            "数据 商品 素材 调控 详情 日志 保障历史 "
            "素材ID: 766496681368721450"
        )
        self.assertEqual(
            "1869678213573940",
            _visible_plan_detail_ad_id(detail_text),
        )
        self.assertEqual(
            "",
            _visible_plan_detail_ad_id(
                "计划列表 ID：1869678213573940 预算(元)：每日5,000.00"
            ),
        )

    def test_login_success_can_be_confirmed_by_authenticated_shell(self):
        class FakeLocator:
            @property
            def first(self):
                return self

            async def is_visible(self, **_kwargs):
                return True

        class FakePage:
            url = "https://qianchuan.jinritemai.com/home"

            async def is_closed(self):
                return False

            def locator(self, _selector):
                return FakeLocator()

        self.assertTrue(
            asyncio.run(
                _qianchuan_authenticated_shell_visible(FakePage())
            )
        )

    def test_catalog_sync_waits_while_visible_relogin_is_running(self):
        controller = ServiceController()
        controller._target_discovery_thread = Mock()
        controller._target_discovery_thread.is_alive.return_value = True
        controller._target_discovery_login_only = True
        result = controller.start_catalog_sync()
        self.assertFalse(result["success"])
        self.assertEqual("relogin_in_progress", result["failure_kind"])

    def test_target_discovery_failure_contract_is_fail_closed(self):
        controller = ServiceController()
        with patch.object(
            controller,
            "_target_discovery_async",
            AsyncMock(side_effect=RuntimeError("模拟登录失败")),
        ):
            controller._target_discovery_entry(login_only=True)
        status = controller.target_discovery_status()
        self.assertFalse(status["success"])
        self.assertFalse(status["running"])
        self.assertFalse(status["relogin_complete"])
        self.assertIn("模拟登录失败", status["message"])
        self.assertTrue(controller._target_discovery_launch_event.is_set())

    def test_successful_account_selection_queues_catalog_prefetch(self):
        controller = ServiceController()

        async def finish_selection(*, login_only=False, account_only=False):
            self.assertFalse(login_only)
            self.assertTrue(account_only)
            with controller._lock:
                controller._target_discovery_status = {
                    "success": True,
                    "running": False,
                    "message": "account added",
                    "target": None,
                    "account": {
                        "account_uid": "account-10002",
                        "aavid": "10002",
                        "owner_username": self.owner,
                    },
                    "relogin_complete": False,
                }

        with (
            patch.object(
                controller,
                "_target_discovery_async",
                side_effect=finish_selection,
            ),
            patch.object(
                controller,
                "_queue_catalog_prefetch_after_discovery",
                return_value=True,
            ) as queue_prefetch,
        ):
            controller._target_discovery_entry(account_only=True)

        queue_prefetch.assert_called_once_with(
            owner_username=self.owner,
            account_uid="account-10002",
        )
        self.assertTrue(
            controller.target_discovery_status()["catalog_prefetch_requested"]
        )

    def test_failed_account_selection_does_not_queue_catalog_prefetch(self):
        controller = ServiceController()
        with (
            patch.object(
                controller,
                "_target_discovery_async",
                AsyncMock(side_effect=RuntimeError("selection failed")),
            ),
            patch.object(
                controller,
                "_queue_catalog_prefetch_after_discovery",
            ) as queue_prefetch,
        ):
            controller._target_discovery_entry(account_only=True)
        queue_prefetch.assert_not_called()

    def test_catalog_prefetch_starts_selected_account_and_clears_pending(self):
        controller = ServiceController()
        controller._catalog_prefetch_pending.add("account-10002")
        with (
            patch(
                "services.run_services.current_session_owner",
                return_value=self.owner,
            ),
            patch.object(
                controller,
                "start_catalog_sync",
                return_value={"success": True},
            ) as start_sync,
        ):
            controller._catalog_prefetch_entry(
                self.owner,
                "account-10002",
            )
        start_sync.assert_called_once_with("account-10002")
        self.assertNotIn("account-10002", controller._catalog_prefetch_pending)

    def test_relogin_start_waits_for_real_launch_failure_result(self):
        controller = ServiceController()

        def fail_before_browser(_login_only=False):
            with controller._lock:
                controller._target_discovery_status = {
                    "success": False,
                    "running": False,
                    "message": "识别失败：Chrome程序无法启动",
                    "target": None,
                    "relogin_complete": False,
                }
            controller._target_discovery_launch_event.set()

        with patch.object(
            controller,
            "_target_discovery_entry",
            side_effect=fail_before_browser,
        ):
            result = controller.start_target_discovery(login_only=True)
        self.assertFalse(result["success"])
        self.assertFalse(result["running"])
        self.assertIn("Chrome程序无法启动", result["message"])

    def test_live_delivery_gate_fails_closed_without_ids_or_listener(self):
        fetcher = QianChuanFetcher()
        fetcher._current_aadvid = None
        fetcher._current_adid = None
        self.assertFalse(asyncio.run(fetcher._wait_for_ad_delivery_gate()))
        self.assertEqual("ids_missing", fetcher._delivery_gate_detail["reason"])

        fetcher._current_aadvid = "10001"
        fetcher._current_adid = "30005"
        fetcher._ad_detail_gate_queue = None
        self.assertFalse(asyncio.run(fetcher._wait_for_ad_delivery_gate()))
        self.assertEqual(
            "gate_unavailable", fetcher._delivery_gate_detail["reason"]
        )

    def test_live_delivery_gate_actively_reads_exact_scoped_plan(self):
        fetcher = QianChuanFetcher()
        fetcher._current_aadvid = "10001"
        fetcher._current_adid = "30005"
        fetcher._current_target_uid = "target-live-30005"
        fetcher._current_account_uid = "account-live-10001"
        fetcher._current_plan_name = "live-plan"
        fetcher._current_promotion_scene = "live"
        fetcher._current_plan_system = "chengfang"
        fetcher.page = AsyncMock()
        fetcher.page.evaluate.return_value = {
            "status_code": 0,
            "data": {
                "adDetailInfo": {
                    "id": "30005",
                    "advId": "10001",
                    "adDeliveryName": "投放中",
                    "adDeliveryType": 0,
                }
            },
        }

        self.assertTrue(
            asyncio.run(fetcher._check_live_delivery_gate(self.db))
        )
        self.assertEqual("delivering", fetcher._delivery_gate_detail["reason"])
        saved = self.db.select_one(
            "pmc_ad_detail_basic",
            where={
                "account_uid": "account-live-10001",
                "aadvid": "10001",
                "ad_id": "30005",
            },
        )
        self.assertIsNotNone(saved)

    def test_live_delivery_gate_rejects_account_or_plan_mismatch(self):
        for detail, reason in (
            (
                {
                    "id": "99999",
                    "advId": "10001",
                    "adDeliveryName": "投放中",
                    "adDeliveryType": 0,
                },
                "live_detail_mismatch",
            ),
            (
                {
                    "id": "30005",
                    "advId": "20002",
                    "adDeliveryName": "投放中",
                    "adDeliveryType": 0,
                },
                "live_detail_account_mismatch",
            ),
        ):
            with self.subTest(reason=reason):
                fetcher = QianChuanFetcher()
                fetcher._current_aadvid = "10001"
                fetcher._current_adid = "30005"
                fetcher.page = AsyncMock()
                fetcher.page.evaluate.return_value = {
                    "status_code": 0,
                    "data": {"adDetailInfo": detail},
                }
                self.assertFalse(
                    asyncio.run(fetcher._check_live_delivery_gate(self.db))
                )
                self.assertEqual(
                    reason,
                    fetcher._delivery_gate_detail["reason"],
                )

    def test_delivery_gate_requires_both_name_and_type(self):
        self.assertTrue(
            QianChuanFetcher._ad_detail_is_delivering(
                {"adDeliveryName": "投放中", "adDeliveryType": 0}
            )
        )
        self.assertFalse(
            QianChuanFetcher._ad_detail_is_delivering(
                {"adDeliveryName": "已暂停", "adDeliveryType": 0}
            )
        )
        self.assertFalse(
            QianChuanFetcher._ad_detail_is_delivering(
                {"adDeliveryName": "投放中", "adDeliveryType": 1}
            )
        )

    def test_collection_marks_previous_ok_target_as_verifying(self):
        target = self._target(
            ad_id="30008",
            status="active",
            verification="verified",
            enabled=True,
        )
        self.db.update(
            "promotion_target",
            {"last_status": "ok"},
            where={"target_uid": target["target_uid"]},
        )

        patch_target_sync_state(
            target["target_uid"],
            status="verifying",
            error="本轮采集正在复核投放状态",
            capability_updates={
                "assist_sync_in_progress": True,
                "assist_sync_ok": False,
            },
            db=self.db,
        )

        verifying = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )
        self.assertEqual("verifying", verifying["last_status"])
        self.assertNotEqual("ok", verifying["last_status"])

    def test_live_submit_recheck_requires_exact_identity_and_delivering(self):
        service = QianChuanRetargetingService()

        class FakePage:
            def __init__(self, response):
                self.response = response

            async def evaluate(self, *_args, **_kwargs):
                return self.response

        def response(detail):
            return {
                "httpStatus": 200,
                "payload": {
                    "status_code": 0,
                    "data": {"adDetailInfo": detail},
                },
            }

        active = response(
            {
                "id": "30009",
                "advId": "10001",
                "adDeliveryName": "投放中",
                "adDeliveryType": 0,
            }
        )
        self.assertIsNone(
            asyncio.run(
                service._confirm_live_target_delivering(
                    FakePage(active),
                    expected_aavid="10001",
                    expected_ad_id="30009",
                )
            )
        )

        wrong_plan = response(
            {
                "id": "99999",
                "advId": "10001",
                "adDeliveryName": "投放中",
                "adDeliveryType": 0,
            }
        )
        self.assertIn(
            "计划不匹配",
            asyncio.run(
                service._confirm_live_target_delivering(
                    FakePage(wrong_plan),
                    expected_aavid="10001",
                    expected_ad_id="30009",
                )
            ),
        )

        missing_account = response(
            {
                "id": "30009",
                "adDeliveryName": "投放中",
                "adDeliveryType": 0,
            }
        )
        self.assertIn(
            "账户不匹配",
            asyncio.run(
                service._confirm_live_target_delivering(
                    FakePage(missing_account),
                    expected_aavid="10001",
                    expected_ad_id="30009",
                )
            ),
        )

        paused = response(
            {
                "id": "30009",
                "advId": "10001",
                "adDeliveryName": "已暂停",
                "adDeliveryType": 1,
            }
        )
        self.assertIn(
            "未取得明确投放中证据",
            asyncio.run(
                service._confirm_live_target_delivering(
                    FakePage(paused),
                    expected_aavid="10001",
                    expected_ad_id="30009",
                )
            ),
        )

    def test_catalog_login_redirect_invalidates_session_gate(self):
        class FakePage:
            url = "https://qianchuan.jinritemai.com/login"

            async def goto(self, *_args, **_kwargs):
                return None

        class FakeFetcher:
            def __init__(self):
                self.page = FakePage()
                self.context = object()

            async def _init_browser(self):
                return None

            async def close(self):
                return None

        class FakeProbe:
            def __init__(self, *_args, **_kwargs):
                pass

            def attach(self, _page):
                return None

            def reset_catalog_class(self, **_kwargs):
                return None

            def set_catalog_context(self, **_kwargs):
                return None

        @asynccontextmanager
        async def fake_lock(*_args, **_kwargs):
            yield None

        cfg = SimpleNamespace(db_path=self.db_path)
        cfg.normalize_paths = lambda: cfg
        controller = ServiceController()
        invalid = Mock()
        ensure_qianchuan_account(
            "10001",
            account_name="账户",
            owner_username=self.owner,
            db=self.db,
        )
        with (
            patch(
                "services.run_services.current_session_owner",
                return_value=self.owner,
            ),
            patch(
                "services.run_services.load_qianchuan_storage_state",
                return_value={"cookies": []},
            ),
            patch("services.run_services.ServiceConfig", return_value=cfg),
            patch(
                "services.run_services.QianChuanFetcher",
                return_value=FakeFetcher(),
            ),
            patch(
                "services.run_services.PromotionReadOnlyProbe",
                FakeProbe,
            ),
            patch(
                "services.run_services.exclusive_browser_operation",
                fake_lock,
            ),
            patch(
                "services.run_services.mark_qianchuan_session_invalid",
                invalid,
            ),
            patch(
                "services.qianchuan_catalog.finalize_catalog_sync",
            ),
        ):
            controller._catalog_sync_entry(self.owner)
        invalid.assert_called_once()

    def test_active_catalog_system_uses_selected_navigation_not_page_copy(self):
        class FakePage:
            async def evaluate(self, _script):
                return "global"

        self.assertEqual(
            "global",
            asyncio.run(
                ServiceController._active_catalog_plan_system(FakePage())
            ),
        )

    def test_active_catalog_system_keeps_unknown_without_active_navigation(self):
        class FakePage:
            async def evaluate(self, _script):
                return "unknown"

        self.assertEqual(
            "unknown",
            asyncio.run(
                ServiceController._active_catalog_plan_system(FakePage())
            ),
        )

    def test_stale_catalog_login_failure_cannot_override_visible_relogin(self):
        controller = ServiceController()
        controller._target_discovery_thread = Mock()
        controller._target_discovery_thread.is_alive.return_value = True
        controller._target_discovery_login_only = True
        invalid = Mock()
        finalized = Mock()
        with (
            patch.object(
                controller,
                "_catalog_sync_async",
                AsyncMock(
                    side_effect=CatalogLoginRequired(
                        "旧Cookie已失效"
                    )
                ),
            ),
            patch(
                "services.run_services.mark_qianchuan_session_invalid",
                invalid,
            ),
            patch(
                "services.qianchuan_catalog.finalize_catalog_sync",
                finalized,
            ),
        ):
            controller._catalog_sync_entry(self.owner)
        invalid.assert_not_called()
        finalized.assert_called_once()
        self.assertIn(
            "可见Chrome正在重新登录",
            finalized.call_args.kwargs["error"],
        )

    def test_catalog_account_scan_login_expiry_is_not_downgraded_to_partial(self):
        class FakePage:
            url = "https://qianchuan.jinritemai.com/home"

            async def goto(self, *_args, **_kwargs):
                return None

        class FakeContext:
            pass

        class FakeFetcher:
            def __init__(self):
                self.page = FakePage()
                self.context = FakeContext()

            async def _init_browser(self):
                return None

            async def close(self):
                return None

        class FakeProbe:
            def __init__(self, *_args, **_kwargs):
                pass

            def attach(self, _page):
                return None

            async def discover_authorized_accounts(self, *_args, **_kwargs):
                return [{"aavid": "10001", "account_name": "账户"}]

            def authorized_account_catalog_status(self):
                return {"complete": True, "observed": 1, "total": 1}

        @asynccontextmanager
        async def fake_lock(*_args, **_kwargs):
            yield None

        cfg = SimpleNamespace(db_path=self.db_path)
        cfg.normalize_paths = lambda: cfg
        controller = ServiceController()
        invalid = Mock()
        ensure_qianchuan_account(
            "10001",
            account_name="账户",
            owner_username=self.owner,
            db=self.db,
        )
        with (
            patch(
                "services.run_services.current_session_owner",
                return_value=self.owner,
            ),
            patch(
                "services.run_services.load_qianchuan_storage_state",
                return_value={"cookies": []},
            ),
            patch("services.run_services.ServiceConfig", return_value=cfg),
            patch(
                "services.run_services.QianChuanFetcher",
                return_value=FakeFetcher(),
            ),
            patch(
                "services.run_services.PromotionReadOnlyProbe",
                FakeProbe,
            ),
            patch(
                "services.run_services.exclusive_browser_operation",
                fake_lock,
            ),
            patch.object(
                controller,
                "_scan_global_account_catalog",
                AsyncMock(
                    side_effect=CatalogLoginRequired(
                        "千川登录状态已失效，请重新登录"
                    )
                ),
            ),
            patch(
                "services.run_services.mark_qianchuan_session_invalid",
                invalid,
            ),
            patch(
                "services.qianchuan_catalog.finalize_catalog_sync",
            ),
        ):
            controller._catalog_sync_entry(self.owner)
        invalid.assert_called_once()


if __name__ == "__main__":
    unittest.main()
