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
    patch_target_sync_state,
    record_target_verification_failure,
    set_target_automation_write_block,
    update_target_catalog_evidence,
    upsert_promotion_target,
)
from services.fetcher import QianChuanFetcher
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
    _qianchuan_authenticated_shell_visible,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class Rc28SafetyHotfixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "rc28.db")
        init_sqlite_schema(database=self.db_path)
        self.db = SQLiteStore(database=self.db_path)
        self.owner = "rc28-owner"

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

            async def _init_browser(self):
                return None

            async def close(self):
                return None

        class FakeProbe:
            def __init__(self, *_args, **_kwargs):
                pass

            def attach(self, _page):
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
