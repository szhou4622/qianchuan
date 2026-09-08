"""Real SQLite coordinator/ancillary writes; no network or production profile."""
import json
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services import official_api_collection as collection
from services.qianchuan_open_api.collection_context import use_collection_context
from services.qianchuan_open_api.errors import ApiRequestError
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class CommitFencingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qcsckp-r20-fencing-")
        self.db = SQLiteStore(database=str(Path(self.temp.name) / "test.db"))
        init_sqlite_schema(database=self.db.config["database"])
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(collection, "_owner_key", return_value="test-owner"))
        self.stack.enter_context(patch.object(collection, "_capture_authorization_identity", return_value={}))
        self.stack.enter_context(patch.object(collection.collection_lifecycle, "resource_pressure", return_value={"critical": False}))
        collection._STOP.clear()
        self.db.insert("qianchuan_account", {"account_uid": "account", "owner_username": "test-owner", "aavid": "1001", "enabled": 1})
        self.db.insert("promotion_target", {"target_uid": "target", "account_uid": "account", "aadvid": "1001", "ad_id": "2001",
            "promotion_scene": "live", "plan_system": "global", "enabled": 1, "capacity_state": "active", "monitor_eligible": 1,
            "verification_state": "verified", "platform_status": "active", "last_status": "collecting",
            "last_sync_at": "2026-09-01 10:00:00", "capability_json": json.dumps({"assist_sync_ok": True,
                "control_task_sync_complete": True, "assist_sync_in_progress": False, "material_sync_complete": False})})
        self.db.insert("collection_job", {"job_uid": "job", "owner_username": "test-owner", "target_uid": "target", "account_uid": "account", "aavid": "1001",
            "job_kind": "hot_collection", "status": "leased", "lease_owner": "worker", "fencing_token": 1,
            "due_at": "2026-09-01 10:00:00", "last_started_at": "2026-09-01 10:00:00",
            "lease_expires_at": (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")})
        self.job = self.db.select_one("collection_job", where={"job_uid": "job"})
        self.target = self.db.select_one("promotion_target", where={"target_uid": "target"})
        self.ctx = collection._new_collection_context(self.target, db=self.db, job=self.job)

    def tearDown(self):
        self.ctx.cancel("test ended")
        collection.collection_lifecycle.release(self.ctx)
        self.stack.close()
        self.temp.cleanup()

    def test_lost_fence_blocks_all_ancillary_writes(self):
        self.db.update("collection_job", {"fencing_token": 2}, where={"id": self.job["id"]})
        operations = [
            lambda: collection.patch_target_sync_state("target", status="ok", synced=True, db=self.db),
            lambda: collection.upsert_products("target", [{"product_id": "6001"}], db=self.db),
            lambda: collection.replace_material_product_links("target", "4001", ["6001"], db=self.db),
            lambda: collection.record_target_duration("target", 1000, refresh_capacity=False, db=self.db),
            lambda: collection.update_target_catalog_evidence("target", platform_status="active", verification_state="verified", db=self.db),
            lambda: collection.record_target_verification_failure("target", "old error", db=self.db),
        ]
        with use_collection_context(self.ctx):
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaises(ApiRequestError):
                    operation()
        self.assertEqual("collecting", self.db.select_one("promotion_target", where={"target_uid": "target"})["last_status"])
        self.assertEqual(0, self.db.count("promotion_product"))
        self.assertEqual(0, self.db.count("promotion_material_product"))

    def test_valid_ancillary_writes_share_transaction_and_refresh_eligibility(self):
        with use_collection_context(self.ctx):
            collection.patch_target_sync_state("target", status="ok", capability_updates={"material_sync_complete": True}, db=self.db)
            collection.upsert_products("target", [{"product_id": "6001"}], db=self.db)
            collection.replace_material_product_links("target", "4001", ["6001"], db=self.db)
            collection.record_target_duration("target", 1000, refresh_capacity=False, db=self.db)
            collection.update_target_catalog_evidence("target", platform_status="active", verification_state="verified", db=self.db)
            collection._set_retry_due("target", delay_seconds=30, db=self.db)
        self.assertEqual("ok", self.db.select_one("promotion_target", where={"target_uid": "target"})["last_status"])
        self.assertEqual(1, self.db.count("promotion_product"))
        self.assertEqual(1, self.db.count("promotion_material_product"))

    def test_coordinator_timeout_keeps_control_success_and_last_material_snapshot(self):
        self.ctx.cancel("deadline")
        result = {"success": False, "error_kind": "collection_deadline", "message": "deadline", "retry_seconds": 30}
        collection._finish_collection_job(self.job, result, db=self.db)
        row = self.db.select_one("promotion_target", where={"target_uid": "target"})
        self.assertEqual("collection_deadline", row["last_status"])
        self.assertEqual("2026-09-01 10:00:00", row["last_sync_at"])
        self.assertTrue(json.loads(row["capability_json"])["assist_sync_ok"])
        self.assertEqual("queued", self.db.select_one("collection_job", where={"id": self.job["id"]})["status"])
        self.db.update("promotion_target", {"last_status": "ok"}, where={"target_uid": "target"})
        collection._finish_collection_job(self.job, result, db=self.db)
        self.assertEqual("ok", self.db.select_one("promotion_target", where={"target_uid": "target"})["last_status"])

    def test_old_coordinator_result_cannot_release_new_lease_or_mark_error(self):
        self.db.update("collection_job", {"fencing_token": 2, "lease_owner": "new-worker"}, where={"id": self.job["id"]})
        collection._finish_collection_job(self.job, {"success": False, "error_kind": "collection_cancelled"}, db=self.db)
        self.assertEqual("leased", self.db.select_one("collection_job", where={"id": self.job["id"]})["status"])
        self.assertEqual("collecting", self.db.select_one("promotion_target", where={"target_uid": "target"})["last_status"])
