# -*- coding: utf-8 -*-
"""v0.1.41 account catalog refresh regression tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services.promotion_readonly_probe import PromotionReadOnlyProbe
from services.qianchuan_accounts import ensure_qianchuan_account
from services.qianchuan_catalog import finalize_catalog_sync
from services.run_services import ServiceController
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class CatalogRefreshV0141Tests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
