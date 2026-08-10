# -*- coding: utf-8 -*-
"""v0.1.41 account catalog refresh regression tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from services.promotion_readonly_probe import PromotionReadOnlyProbe
from services.qianchuan_accounts import ensure_qianchuan_account
from services.qianchuan_catalog import finalize_catalog_sync
from services.qianchuan_catalog import (
    catalog_sync_status,
    clear_catalog_login_failure,
    mark_catalog_sync_started,
)
from services.run_services import ServiceController
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class CatalogRefreshV0141Tests(unittest.TestCase):
    def test_status_reconciles_persisted_result_after_running_flag_stalls(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "catalog.db")
            init_sqlite_schema(database=db_path)
            db = SQLiteStore(database=db_path)
            owner = "catalog-persisted-finish-user"
            account = ensure_qianchuan_account(
                "10003", owner_username=owner, account_name="账户三", db=db
            )
            started = mark_catalog_sync_started(
                owner_username=owner,
                account_uid=account["account_uid"],
            )
            db.update(
                "qianchuan_account",
                {
                    "catalog_status": "complete",
                    "catalog_error": "",
                    "catalog_last_sync_at": started["started_at"],
                },
                where={"account_uid": account["account_uid"]},
            )

            result = catalog_sync_status(owner_username=owner, db=db)

            self.assertFalse(result["running"])
            self.assertTrue(result["success"])
            self.assertTrue(result["complete"])
            self.assertEqual("complete", result["status"])
            self.assertEqual("账户计划目录同步完成", result["message"])

    def test_session_save_does_not_interrupt_running_catalog_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "catalog.db")
            init_sqlite_schema(database=db_path)
            db = SQLiteStore(database=db_path)
            owner = "catalog-refresh-session-user"
            account = ensure_qianchuan_account(
                "10001", owner_username=owner, account_name="账户一", db=db
            )
            db.update(
                "qianchuan_account",
                {"catalog_status": "partial", "catalog_error": "旧错误"},
                where={"account_uid": account["account_uid"]},
            )

            mark_catalog_sync_started(
                owner_username=owner,
                account_uid=account["account_uid"],
            )
            state = clear_catalog_login_failure(
                owner_username=owner,
                db=db,
            )

            persisted = db.select_one(
                "qianchuan_account",
                where={"account_uid": account["account_uid"]},
            )
            self.assertTrue(state["running"])
            self.assertEqual("syncing", state["status"])
            self.assertEqual("partial", persisted["catalog_status"])
            self.assertEqual("旧错误", persisted["catalog_error"])

    def test_probe_instances_can_write_same_file_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "promotion_readonly_probe.json")

            def write_many(worker: int) -> None:
                probe = PromotionReadOnlyProbe(path)
                for sequence in range(12):
                    probe._write({"worker": worker, "sequence": sequence})

            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(write_many, item) for item in range(8)]
                for future in futures:
                    future.result()

            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertIn("worker", payload)
            self.assertFalse(os.path.exists(path + ".tmp"))
            self.assertEqual(
                [],
                [name for name in os.listdir(directory) if name.endswith(".tmp")],
            )

    def test_targeted_finalize_does_not_change_other_account_status(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "catalog.db")
            init_sqlite_schema(database=db_path)
            db = SQLiteStore(database=db_path)
            owner = "catalog-refresh-user"
            first = ensure_qianchuan_account(
                "10001", owner_username=owner, account_name="账户一", db=db
            )
            second = ensure_qianchuan_account(
                "10002", owner_username=owner, account_name="账户二", db=db
            )
            db.update(
                "qianchuan_account",
                {"catalog_status": "partial", "catalog_error": "保留原状态"},
                where={"account_uid": first["account_uid"]},
            )
            db.update(
                "qianchuan_account",
                {"catalog_status": "not_synced", "catalog_error": ""},
                where={"account_uid": second["account_uid"]},
            )

            result = finalize_catalog_sync(
                owner_username=owner,
                account_results={second["account_uid"]: {"complete": True}},
                refreshed_account_uids=[second["account_uid"]],
                db=db,
            )

            kept = db.select_one(
                "qianchuan_account", where={"account_uid": first["account_uid"]}
            )
            refreshed = db.select_one(
                "qianchuan_account", where={"account_uid": second["account_uid"]}
            )
            self.assertEqual("partial", kept["catalog_status"])
            self.assertEqual("保留原状态", kept["catalog_error"])
            self.assertEqual("complete", refreshed["catalog_status"])
            self.assertEqual("complete", result["status"])
            self.assertEqual(1, result["processed_accounts"])
            self.assertEqual(1, result["total_accounts"])

    def test_monitor_service_does_not_start_during_catalog_refresh(self):
        controller = ServiceController()
        controller._catalog_sync_thread = Mock()
        controller._catalog_sync_thread.is_alive.return_value = True

        result = controller.start()

        self.assertFalse(result["success"])
        self.assertEqual("waiting_catalog_sync", result["phase"])
        self.assertIsNone(controller._thread)

    def test_save_account_setup_starts_monitoring_from_saved_session(self):
        controller = ServiceController()
        controller.start = Mock(
            return_value={"success": True, "running": True, "phase": "starting"}
        )
        with (
            patch("services.run_services.current_session_owner", return_value="owner"),
            patch(
                "services.run_services.automation_session_ready",
                return_value={"ready": True},
            ),
            patch(
                "services.run_services.schedulable_promotion_targets",
                return_value=[{"target_uid": "target_one"}],
            ),
        ):
            result = controller.start_from_saved_session()

        self.assertTrue(result["success"])
        self.assertTrue(result["running"])
        self.assertEqual("设置已保存，正在启动首次后台采集", result["message"])
        self.assertTrue(controller._saved_session_bootstrap)
        controller.start.assert_called_once_with()

    def test_save_account_setup_defers_monitoring_until_catalog_finishes(self):
        controller = ServiceController()
        controller._catalog_sync_thread = Mock()
        controller._catalog_sync_thread.is_alive.return_value = True
        captured = {}

        class FakeThread:
            def __init__(self, *, target, args, name, daemon):
                captured.update(
                    {"target": target, "args": args, "name": name, "daemon": daemon}
                )

            def start(self):
                captured["started"] = True

            def is_alive(self):
                return False

        with (
            patch("services.run_services.current_session_owner", return_value="owner"),
            patch(
                "services.run_services.automation_session_ready",
                return_value={"ready": True},
            ),
            patch(
                "services.run_services.schedulable_promotion_targets",
                return_value=[{"target_uid": "target_one"}],
            ),
            patch("services.run_services.threading.Thread", FakeThread),
        ):
            result = controller.start_from_saved_session()

        self.assertTrue(result["success"])
        self.assertEqual("waiting_catalog_sync", result["phase"])
        self.assertTrue(captured["started"])
        self.assertEqual("qianchuan-monitor-auto-start", captured["name"])
        self.assertEqual(("owner",), captured["args"])

    def test_start_returns_status_when_service_is_already_running(self):
        controller = ServiceController()
        controller._thread = Mock()
        controller._thread.is_alive.return_value = True

        result = controller.start()

        self.assertTrue(result["success"])
        self.assertTrue(result["running"])
        self.assertEqual("服务已在运行", result["message"])

    def test_targeted_refresh_normalizes_aavid_to_account_uid(self):
        controller = ServiceController()
        captured = {}

        class FakeThread:
            def __init__(self, *, target, args, name, daemon):
                captured.update(
                    {"target": target, "args": args, "name": name, "daemon": daemon}
                )

            def start(self):
                captured["started"] = True

            def is_alive(self):
                return False

        accounts = [
            {
                "account_uid": "account_one",
                "aavid": "10001",
                "account_name": "账户一",
            },
            {
                "account_uid": "account_two",
                "aavid": "10002",
                "account_name": "账户二",
            },
        ]
        cfg = SimpleNamespace(db_path=":memory:")
        cfg.normalize_paths = lambda: cfg
        with (
            patch("services.run_services.current_session_owner", return_value="owner"),
            patch(
                "services.run_services.automation_session_ready",
                return_value={"ready": True},
            ),
            patch("services.run_services.ServiceConfig", return_value=cfg),
            patch(
                "services.run_services.list_qianchuan_accounts",
                return_value=accounts,
            ),
            patch(
                "services.qianchuan_catalog.mark_catalog_sync_started",
                return_value={},
            ) as marked,
            patch(
                "services.qianchuan_catalog.catalog_sync_status",
                return_value={"success": True, "running": True},
            ),
            patch("services.run_services.threading.Thread", FakeThread),
        ):
            result = controller.start_catalog_sync("10002")

        self.assertTrue(result["success"])
        self.assertTrue(captured["started"])
        self.assertEqual(("owner", "account_two"), captured["args"])
        marked.assert_called_once_with(
            owner_username="owner", account_uid="account_two"
        )

    def test_catalog_completion_rechecks_saved_monitor_selection(self):
        controller = ServiceController()
        controller._catalog_sync_async = AsyncMock(return_value=None)
        controller.start_from_saved_session = Mock(
            return_value={"success": True, "running": True}
        )

        with patch(
            "services.run_services.current_session_owner",
            return_value="owner",
        ):
            controller._catalog_sync_entry("owner", "account_one")

        controller._catalog_sync_async.assert_awaited_once_with(
            owner_username="owner",
            account_uid="account_one",
        )
        controller.start_from_saved_session.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
