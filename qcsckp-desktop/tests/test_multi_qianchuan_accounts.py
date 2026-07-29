# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from api.promotion_targets import upsert_promotion_target
from services import promotion_browser_lock, qianchuan_session, rc23_rollback
from services.qianchuan_accounts import (
    capacity_snapshot,
    ensure_qianchuan_account,
    list_qianchuan_accounts,
    resolve_account_feishu_targets,
    save_qianchuan_account_settings,
    schedulable_promotion_targets,
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
        return upsert_promotion_target(
            {
                "aavid": aavid,
                "ad_id": str(20000 + index),
                "plan_name": f"计划{index}",
                "promotion_scene": "live" if index % 2 else "product",
                "plan_system": "global" if index % 3 else "chengfang",
                "enabled": True,
            },
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

    def test_disabled_account_disables_all_its_automation_targets(self):
        target = self._target("10001", 1)
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

    def test_account_specific_feishu_route_only_uses_selected_bound_targets(self):
        account = ensure_qianchuan_account("10001", db=self.db)
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
