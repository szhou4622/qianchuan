# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import asyncio
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from api.views import Api
from api.promotion_targets import upsert_promotion_target
from api.promotion_targets import list_promotion_targets
from api.operation_events import upsert_operation_event
from services import promotion_browser_lock, qianchuan_session, rc23_rollback
from services.qianchuan_accounts import (
    capacity_snapshot,
    ensure_qianchuan_account,
    get_qianchuan_account,
    list_qianchuan_accounts,
    migrate_existing_qianchuan_accounts,
    remove_qianchuan_account,
    resolve_account_feishu_targets,
    record_target_duration,
    save_qianchuan_account_settings,
    schedulable_promotion_targets,
    upsert_authorized_accounts,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class MultiQianchuanAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "multi.db")
        init_sqlite_schema(database=self.db_path)
        self.db = SQLiteStore(database=self.db_path)
        self.owner_patch = patch.dict(
            os.environ,
            {"QCSCKP_SESSION_OWNER": "tool-owner"},
        )
        self.owner_patch.start()

    def tearDown(self):
        self.owner_patch.stop()
        self.temp.cleanup()

    def _target(self, aavid: str, index: int):
        ensure_qianchuan_account(
            aavid,
            owner_username="tool-owner",
            enabled=True,
            seen=True,
            db=self.db,
        )
        return upsert_promotion_target(
            {
                "aavid": aavid,
                "ad_id": str(20000 + index),
                "plan_name": f"计划{index}",
                "promotion_scene": "live" if index % 2 else "product",
                "plan_system": "global" if index % 3 else "chengfang",
                "platform_status": "active",
                "verification_state": "verified",
                "enabled": True,
            },
            trusted_catalog=True,
            db=self.db,
        )

    def test_accounts_and_targets_are_isolated_by_aavid(self):
        first = self._target("10001", 1)
        second = self._target("10002", 2)
        accounts = list_qianchuan_accounts(db=self.db)
        self.assertEqual(["10001", "10002"], sorted(x["aavid"] for x in accounts))
        self.assertNotEqual(first["account_uid"], second["account_uid"])

        self.db.insert(
            "account_operation_event",
            {
                "event_uid": "event-a",
                "account_uid": first["account_uid"],
                "aavid": "10001",
                "target_uid": first["target_uid"],
                "material_id": "same-material",
                "source": "tool_direct",
                "action_type": "retarget",
                "status": "success",
                "occurred_at": "2026-07-29 10:00:00",
            },
        )
        self.db.insert(
            "account_operation_event",
            {
                "event_uid": "event-b",
                "account_uid": second["account_uid"],
                "aavid": "10002",
                "target_uid": second["target_uid"],
                "material_id": "same-material",
                "source": "tool_direct",
                "action_type": "stop",
                "status": "success",
                "occurred_at": "2026-07-29 10:01:00",
            },
        )
        rows_a = self.db.select(
            "account_operation_event",
            where={"account_uid": first["account_uid"]},
        )
        rows_b = self.db.select(
            "account_operation_event",
            where={"account_uid": second["account_uid"]},
        )
        self.assertEqual(["retarget"], [row["action_type"] for row in rows_a])
        self.assertEqual(["stop"], [row["action_type"] for row in rows_b])

    def test_authorized_account_catalog_is_not_auto_added_to_user_directory(self):
        upsert_authorized_accounts(
            [
                {"aavid": "10001", "account_name": "授权账户一"},
                {"aavid": "10002", "account_name": "授权账户二"},
            ],
            owner_username="tool-owner",
            db=self.db,
        )
        self.assertEqual(
            list_qianchuan_accounts(
                owner_username="tool-owner",
                db=self.db,
            ),
            [],
        )
        self.assertEqual(
            list_promotion_targets(
                owner_username="tool-owner",
                db=self.db,
            ),
            [],
        )
        ensure_qianchuan_account(
            "10002",
            account_name="用户选择的账户",
            owner_username="tool-owner",
            db=self.db,
        )
        accounts = list_qianchuan_accounts(
            owner_username="tool-owner",
            db=self.db,
        )
        self.assertEqual([item["aavid"] for item in accounts], ["10002"])
        self.assertEqual(accounts[0]["account_name"], "用户选择的账户")

    def test_remove_account_hides_it_and_disables_all_targets(self):
        target = self._target("10001", 1)
        account = get_qianchuan_account(
            "10001",
            owner_username="tool-owner",
            db=self.db,
        )
        result = remove_qianchuan_account(
            account["account_uid"],
            owner_username="tool-owner",
            db=self.db,
        )
        self.assertTrue(result["removed"])
        self.assertEqual(
            list_qianchuan_accounts(
                owner_username="tool-owner",
                db=self.db,
            ),
            [],
        )
        self.assertEqual(
            list_promotion_targets(
                owner_username="tool-owner",
                db=self.db,
            ),
            [],
        )
        removed = self.db.select_one(
            "qianchuan_account",
            where={"account_uid": account["account_uid"]},
        )
        self.assertEqual(int(removed["directory_selected"]), 0)
        self.assertEqual(int(removed["enabled"]), 0)
        saved_target = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )
        self.assertEqual(int(saved_target["enabled"]), 0)
        self.assertEqual(saved_target["capacity_state"], "disabled")
        migrate_existing_qianchuan_accounts(
            owner_username="tool-owner",
            authorized_aavids={"10001"},
            db=self.db,
        )
        upsert_operation_event(
            {
                "event_uid": "removed-account-history",
                "aavid": "10001",
                "source": "platform_log",
                "action_type": "other",
                "status": "success",
                "occurred_at": "2026-07-30 22:00:00",
            },
            db=self.db,
        )
        self.assertEqual(
            list_qianchuan_accounts(
                owner_username="tool-owner",
                db=self.db,
            ),
            [],
        )

    def test_removed_account_is_not_resurrected_by_background_catalog(self):
        self._target("10001", 1)
        account = get_qianchuan_account(
            "10001",
            owner_username="tool-owner",
            db=self.db,
        )
        remove_qianchuan_account(
            account["account_uid"],
            owner_username="tool-owner",
            db=self.db,
        )

        # This mirrors an in-flight catalog persistence that finishes after
        # the user clicked delete. It must update history without restoring
        # the account to the visible directory.
        ensure_qianchuan_account(
            "10001",
            account_name="background refresh",
            owner_username="tool-owner",
            directory_selected=True,
            seen=True,
            db=self.db,
        )
        self.assertEqual(
            [],
            list_qianchuan_accounts(
                owner_username="tool-owner",
                db=self.db,
            ),
        )

        # Only a new explicit account-selection action may clear the tombstone.
        restored = ensure_qianchuan_account(
            "10001",
            account_name="explicitly re-added",
            owner_username="tool-owner",
            directory_selected=True,
            seen=True,
            allow_reactivate_removed=True,
            db=self.db,
        )
        self.assertTrue(restored["directory_selected"])
        self.assertEqual("available", restored["last_status"])
        self.assertEqual(
            ["10001"],
            [
                item["aavid"]
                for item in list_qianchuan_accounts(
                    owner_username="tool-owner",
                    db=self.db,
                )
            ],
        )

    def test_legacy_user_display_name_does_not_overwrite_account_name(self):
        account = ensure_qianchuan_account(
            "10001",
            account_name="权威千川账户名",
            owner_username="tool-owner",
            db=self.db,
        )
        self.db.insert(
            "pmc_ad_detail_basic",
            {
                "aadvid": "10001",
                "account_uid": account["account_uid"],
                "ad_id": "20001",
                "target_uid": "legacy_unscoped",
                "user_info_name": "错误店铺展示名",
            },
        )

        migrate_existing_qianchuan_accounts(
            owner_username="tool-owner",
            authorized_aavids={"10001"},
            db=self.db,
        )

        saved = get_qianchuan_account(
            "10001",
            owner_username="tool-owner",
            db=self.db,
        )
        self.assertEqual("权威千川账户名", saved["account_name"])

    def test_captured_owner_is_used_even_if_current_owner_changes_before_write(self):
        with patch.dict(
            os.environ,
            {"QCSCKP_SESSION_OWNER": "tool-b"},
        ):
            target = upsert_promotion_target(
                {
                    "aavid": "10001",
                    "ad_id": "20001",
                    "plan_name": "captured-owner-plan",
                    "promotion_scene": "product",
                    "plan_system": "global",
                    "enabled": True,
                },
                owner_username="tool-a",
                db=self.db,
            )
        account = self.db.select_one(
            "qianchuan_account",
            where={"account_uid": target["account_uid"]},
        )
        self.assertEqual("tool-a", account["owner_username"])
        self.assertEqual(
            [],
            list_promotion_targets(owner_username="tool-b", db=self.db),
        )
        self.assertEqual(
            [target["target_uid"]],
            [
                row["target_uid"]
                for row in list_promotion_targets(
                    owner_username="tool-a",
                    db=self.db,
                )
            ],
        )

    def test_disabled_account_disables_all_its_automation_targets(self):
        target = self._target("10001", 1)
        self.db.update(
            "promotion_target",
            {"last_lag_seconds": 1200},
            where={"target_uid": target["target_uid"]},
        )
        save_qianchuan_account_settings(
            target["account_uid"],
            {"enabled": False},
            db=self.db,
        )
        saved = self.db.select_one(
            "promotion_target",
            where={"target_uid": target["target_uid"]},
        )
        self.assertEqual("disabled", saved["capacity_state"])
        self.assertEqual(0, saved["last_lag_seconds"])

    def test_dynamic_capacity_marks_excess_targets_waiting(self):
        for index in range(13):
            self._target("10001", index)
        snapshot = capacity_snapshot(db=self.db)
        self.assertEqual(12, snapshot["active_count"])
        self.assertEqual(1, snapshot["waiting_count"])
        self.assertLessEqual(snapshot["estimated_cycle_seconds"], 9 * 60)

    def test_scheduler_only_returns_current_tool_accounts(self):
        current = self._target("10001", 1)
        with patch.dict(
            os.environ,
            {"QCSCKP_SESSION_OWNER": "other-tool-owner"},
        ):
            other = self._target("20001", 2)
        scheduled = schedulable_promotion_targets(db=self.db)
        self.assertEqual(
            [current["target_uid"]],
            [item["target_uid"] for item in scheduled],
        )
        self.assertNotEqual(current["account_uid"], other["account_uid"])

    def test_same_qianchuan_plan_is_isolated_between_tool_accounts(self):
        first = self._target("10001", 1)
        with patch.dict(
            os.environ,
            {"QCSCKP_SESSION_OWNER": "other-tool-owner"},
        ):
            second = self._target("10001", 1)
            self.assertEqual(
                [second["target_uid"]],
                [item["target_uid"] for item in list_promotion_targets(db=self.db)],
            )
        self.assertNotEqual(first["account_uid"], second["account_uid"])
        self.assertNotEqual(first["target_uid"], second["target_uid"])
        self.assertEqual(
            [first["target_uid"]],
            [item["target_uid"] for item in list_promotion_targets(db=self.db)],
        )

    def test_same_external_event_id_never_overwrites_another_tool_account(self):
        first_uid = upsert_operation_event(
            {
                "event_uid": "shared-platform-id",
                "aavid": "10001",
                "source": "platform_log",
                "action_type": "budget_update",
                "status": "success",
            },
            self.db,
        )
        with patch.dict(
            os.environ,
            {"QCSCKP_SESSION_OWNER": "other-tool-owner"},
        ):
            second_uid = upsert_operation_event(
                {
                    "event_uid": "shared-platform-id",
                    "aavid": "10001",
                    "source": "platform_log",
                    "action_type": "roi_update",
                    "status": "success",
                },
                self.db,
            )
        self.assertNotEqual(first_uid, second_uid)
        rows = self.db.select(
            "account_operation_event",
            order_by="id ASC",
        )
        self.assertEqual(2, len(rows))
        self.assertEqual(
            ["budget_update", "roi_update"],
            [row["action_type"] for row in rows],
        )

    def test_single_target_longer_than_capacity_window_waits(self):
        target = self._target("10001", 1)
        record_target_duration(
            target["target_uid"],
            11 * 60_000,
            db=self.db,
        )
        snapshot = capacity_snapshot(db=self.db)
        self.assertEqual(0, snapshot["active_count"])
        self.assertEqual(1, snapshot["waiting_count"])

    def test_account_specific_feishu_route_only_uses_selected_bound_targets(self):
        account = ensure_qianchuan_account("10001", db=self.db)
        with patch(
            "services.local_feishu_bridge.get_local_feishu_status",
            return_value={
                "connected": True,
                "profile": {"authorized_open_id": "ou_owner"},
            },
        ):
            save_qianchuan_account_settings(
                account["account_uid"],
                {
                    "route_mode": "custom",
                    "route_send_personal": True,
                    "route_group_ids": ["oc_selected", "oc_not_bound"],
                },
                db=self.db,
            )
        with patch(
            "services.local_feishu_bridge.list_local_feishu_bound_targets",
            return_value=[("open_id", "ou_owner"), ("chat_id", "oc_selected")],
        ), patch(
            "services.local_feishu_bridge.get_local_feishu_status",
            return_value={
                "profile": {
                    "authorized_open_id": "ou_owner",
                    "groups": [{"chat_id": "oc_selected"}],
                }
            },
        ):
            targets = resolve_account_feishu_targets("10001", db=self.db)
        self.assertEqual(
            [("open_id", "ou_owner"), ("chat_id", "oc_selected")],
            targets,
        )

    def test_account_page_reads_nested_global_daily_report_config(self):
        html = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "qianchuan_accounts.html"
        ).read_text(encoding="utf-8")
        self.assertIn("data.daily_report?.config||{}", html)
        self.assertNotIn("const daily=data.daily_report||{}", html)

    def test_account_page_keeps_equal_cards_and_scrolls_each_plan_catalog(self):
        html = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "qianchuan_accounts.html"
        ).read_text(encoding="utf-8")
        self.assertIn(".card{height:620px", html)
        self.assertIn(".plans{min-height:0", html)
        self.assertIn("overflow-y:auto", html)
        self.assertIn("该账户全部计划（全域/乘方 × 推直播/推商品）", html)
        self.assertIn("请先启用此千川账户，再选择", html)
        self.assertIn('id="diagnostics"', html)
        self.assertIn('data-advanced="${esc(p.target_uid)}"', html)
        self.assertIn("promotion_targets.html?target_uid=", html)
        self.assertIn('class="head-actions"', html)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", html)
        self.assertIn("@media(max-width:560px){.head-actions{grid-template-columns:1fr}}", html)
        self.assertIn('id="catalogActionStatus"', html)
        self.assertIn("已排队，等待采集结束", html)
        self.assertIn("刷新任务已进入共享浏览器队列", html)
        self.assertIn(
            "const late=selected&&accountEnabled&&state==='active'&&!!p.last_sync_at",
            html,
        )
        self.assertIn("firstRun?'等待首次采集':late?'监控延迟'", html)


class QianchuanSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = self.temp.name
        self.paths = [
            patch.object(
                qianchuan_session,
                "SESSION_FILE",
                os.path.join(base, "sessions.json"),
            ),
            patch.object(
                qianchuan_session,
                "LEGACY_COOKIE_FILE",
                os.path.join(base, "qcookie.json"),
            ),
            patch.object(
                qianchuan_session,
                "LEGACY_ROLLBACK_FILE",
                os.path.join(base, "qcookie.legacy.rc23.json"),
            ),
            patch.object(qianchuan_session, "DATA_DIR", base),
            patch.dict(os.environ, {"QCSCKP_SESSION_OWNER": "tool-a"}),
        ]
        for item in self.paths:
            item.start()

    def tearDown(self):
        for item in reversed(self.paths):
            item.stop()
        self.temp.cleanup()

    def test_legacy_cookie_is_encrypted_and_isolated_by_tool_account(self):
        state = {
            "cookies": [
                {
                    "name": "sessionid",
                    "value": "sensitive-cookie-value",
                    "domain": ".jinritemai.com",
                    "path": "/",
                }
            ],
            "origins": [],
        }
        with open(
            qianchuan_session.LEGACY_COOKIE_FILE,
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(state, handle)
        migrated = qianchuan_session.migrate_legacy_qcookie()
        self.assertTrue(migrated["migrated"])
        self.assertFalse(os.path.exists(qianchuan_session.LEGACY_COOKIE_FILE))
        self.assertTrue(os.path.exists(qianchuan_session.LEGACY_ROLLBACK_FILE))
        with open(
            qianchuan_session.SESSION_FILE,
            "r",
            encoding="utf-8",
        ) as handle:
            encrypted_text = handle.read()
        self.assertIn("dpapi:", encrypted_text)
        self.assertNotIn("sensitive-cookie-value", encrypted_text)
        self.assertEqual(
            state,
            qianchuan_session.load_qianchuan_storage_state(),
        )
        with patch.dict(
            os.environ,
            {"QCSCKP_SESSION_OWNER": "tool-b"},
        ):
            self.assertIsNone(qianchuan_session.load_qianchuan_storage_state())

    def test_known_login_failure_closes_the_automation_gate(self):
        state = {"cookies": [], "origins": []}
        qianchuan_session.save_qianchuan_storage_state(state)
        self.assertTrue(qianchuan_session.automation_session_ready()["ready"])
        qianchuan_session.mark_qianchuan_session_invalid("验证码")
        gate = qianchuan_session.automation_session_ready()
        self.assertFalse(gate["ready"])
        self.assertEqual("login_required", gate["status"])

    def test_login_failure_increments_epoch_and_cancels_old_tasks(self):
        state = {"cookies": [], "origins": []}
        qianchuan_session.save_qianchuan_storage_state(state)
        before = qianchuan_session.session_status()["session_epoch"]
        with patch(
            "services.local_feishu_bridge.cancel_active_local_retarget_tasks"
        ) as cancel:
            qianchuan_session.mark_qianchuan_session_invalid("expired")
        after = qianchuan_session.session_status()["session_epoch"]
        self.assertEqual(before + 1, after)
        cancel.assert_called_once()


class ToolAccountSwitchTests(unittest.TestCase):
    def test_save_account_setup_immediately_starts_saved_monitoring(self):
        api = Api.__new__(Api)
        api.db = Mock()
        api.service = Mock()
        api.service.start_from_saved_session.return_value = {
            "success": True,
            "running": True,
            "phase": "starting",
            "message": "设置已保存，正在启动首次后台采集",
        }
        saved = {"account_uid": "account_one", "enabled": True}
        with patch(
            "services.qianchuan_accounts.save_qianchuan_account_automation_setup",
            return_value=saved,
        ) as save:
            result = api.saveQianchuanAccountAutomationSetup(
                "account_one",
                {"enabled": True},
                [{"target_uid": "target_one", "enabled": True}],
            )

        self.assertTrue(result["success"])
        self.assertEqual(saved, result["data"])
        self.assertTrue(result["monitoring"]["running"])
        api.service.start_from_saved_session.assert_called_once_with()
        save.assert_called_once_with(
            "account_one",
            {"enabled": True},
            [{"target_uid": "target_one", "enabled": True}],
            db=api.db,
        )

    def test_monitor_start_failure_does_not_report_saved_settings_rolled_back(self):
        api = Api.__new__(Api)
        api.db = Mock()
        api.service = Mock()
        api.service.start_from_saved_session.side_effect = RuntimeError("boom")
        with patch(
            "services.qianchuan_accounts.save_qianchuan_account_automation_setup",
            return_value={"account_uid": "account_one"},
        ):
            result = api.saveQianchuanAccountAutomationSetup(
                "account_one", {"enabled": True}, []
            )

        self.assertTrue(result["success"])
        self.assertFalse(result["monitoring"]["success"])
        self.assertIn("设置已保存", result["message"])

    def test_start_service_switch_stops_old_owner_before_remote_verification(self):
        api = Api.__new__(Api)
        api.service = Mock()
        api.service.stop_and_wait.return_value = {"running": True}
        api.service.status.return_value = {"running": True}
        api.account_auth = Mock()

        with patch(
            "services.cloud_retarget_client.load_device_session",
            return_value={"username": "tool-a"},
        ):
            result = api.startService(
                username="tool-b",
                password="password",
            )

        self.assertFalse(result["success"])
        self.assertIn("安全退出", result["message"])
        api.service.stop_and_wait.assert_called_once_with(30)
        api.account_auth.verify_can_start_service.assert_not_called()

    def test_login_switch_must_stop_old_service_before_new_session_registration(self):
        api = Api.__new__(Api)
        api.service = Mock()
        api.service.stop_and_wait.return_value = {"running": True}
        api.account_auth = Mock()

        with patch(
            "services.cloud_retarget_client.load_device_session",
            return_value={"username": "tool-a"},
        ):
            result = api.verify_account_login("tool-b", "password")

        self.assertFalse(result["success"])
        self.assertIn("安全退出", result["message"])
        api.service.stop_and_wait.assert_called_once_with(30)
        api.account_auth.verify_login.assert_not_called()


class PromotionBrowserPriorityTests(unittest.TestCase):
    def setUp(self):
        with promotion_browser_lock._CONDITION:
            promotion_browser_lock._WAITERS.clear()
            promotion_browser_lock._ACTIVE = False

    def tearDown(self):
        with promotion_browser_lock._CONDITION:
            promotion_browser_lock._WAITERS.clear()
            promotion_browser_lock._ACTIVE = False
            promotion_browser_lock._CONDITION.notify_all()

    def test_confirmed_retarget_overtakes_log_sync_waiter(self):
        self.assertTrue(promotion_browser_lock._acquire(30, 1))
        order = []

        def worker(name, priority):
            acquired = promotion_browser_lock._acquire(priority, 2)
            if not acquired:
                return
            order.append(name)
            time.sleep(0.02)
            promotion_browser_lock._release()

        low = threading.Thread(target=worker, args=("log", 40))
        high = threading.Thread(target=worker, args=("retarget", 10))
        low.start()
        time.sleep(0.02)
        high.start()
        deadline = time.time() + 1
        while (
            promotion_browser_lock.browser_queue_snapshot()["waiting_count"] < 2
            and time.time() < deadline
        ):
            time.sleep(0.01)
        promotion_browser_lock._release()
        low.join(2)
        high.join(2)
        self.assertEqual(["retarget", "log"], order)

    def test_cancelled_async_waiter_never_leaves_orphan_lock(self):
        async def scenario():
            self.assertTrue(promotion_browser_lock._acquire(30, 1))

            async def waiter():
                async with promotion_browser_lock.exclusive_browser_operation(
                    "cancel-me",
                    priority=10,
                    timeout_seconds=2,
                ):
                    return True

            task = asyncio.create_task(waiter())
            deadline = time.time() + 1
            while (
                promotion_browser_lock.browser_queue_snapshot()["waiting_count"] < 1
                and time.time() < deadline
            ):
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            promotion_browser_lock._release()
            deadline = time.time() + 1
            while (
                promotion_browser_lock.browser_queue_snapshot()["active"]
                or promotion_browser_lock.browser_queue_snapshot()["waiting_count"]
            ) and time.time() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(
                {
                    "active": False,
                    "waiting_count": 0,
                    "waiting_priorities": [],
                    "next_priority": None,
                },
                promotion_browser_lock.browser_queue_snapshot(),
            )

        asyncio.run(scenario())


class Rc23RollbackSnapshotTests(unittest.TestCase):
    def test_snapshot_is_created_once_with_consistent_sqlite_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = os.path.join(temp, "qianchuan.db")
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample(value) VALUES('before-upgrade')")
            connection.commit()
            connection.close()
            with open(
                os.path.join(temp, "qcookie.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump({"cookies": [], "origins": []}, handle)
            marker = os.path.join(temp, "rollback", "rc23_snapshot.json")
            with patch.object(rc23_rollback, "DATA_DIR", temp), patch.object(
                rc23_rollback,
                "DB_FILE",
                db_path,
            ), patch.object(rc23_rollback, "SNAPSHOT_MARKER", marker):
                first = rc23_rollback.ensure_rc23_upgrade_snapshot()
                second = rc23_rollback.ensure_rc23_upgrade_snapshot()
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            backup = sqlite3.connect(
                os.path.join(first["snapshot_dir"], "qianchuan.db")
            )
            try:
                value = backup.execute("SELECT value FROM sample").fetchone()[0]
            finally:
                backup.close()
            self.assertEqual("before-upgrade", value)
            self.assertTrue(
                os.path.isfile(
                    os.path.join(first["snapshot_dir"], "qcookie.json")
                )
            )
