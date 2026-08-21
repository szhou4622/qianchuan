import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from production_v1a.migration import LegacyMigrationService
from services.official_api_collection import (
    _claim_collection_jobs,
    _enqueue_collection_jobs,
    _finish_collection_job,
)
from services.qianchuan_open_api import runtime_settings
from services.qianchuan_open_api import token_provider
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class ToolUserCredentialIsolationTests(unittest.TestCase):
    def test_runtime_write_permission_is_isolated_by_tool_user(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_file = str(Path(directory) / "runtime.json")
            with (
                patch.object(runtime_settings, "QIANCHUAN_RUNTIME_SETTINGS_FILE", settings_file),
                patch.object(runtime_settings, "_current_owner", return_value="user_a"),
            ):
                runtime_settings.persist_official_api_runtime(
                    allow_live_writes=True,
                    apply_runtime=False,
                )
                self.assertTrue(
                    runtime_settings.load_runtime_settings()["allow_live_api_writes"]
                )

            with (
                patch.object(runtime_settings, "QIANCHUAN_RUNTIME_SETTINGS_FILE", settings_file),
                patch.object(runtime_settings, "_current_owner", return_value="user_b"),
            ):
                self.assertFalse(
                    runtime_settings.load_runtime_settings()["allow_live_api_writes"]
                )
                runtime_settings.persist_official_api_runtime(
                    allow_live_writes=False,
                    apply_runtime=False,
                )

            payload = json.loads(Path(settings_file).read_text(encoding="utf-8"))
            self.assertTrue(payload["profiles"]["user_a"]["allow_live_api_writes"])
            self.assertFalse(payload["profiles"]["user_b"]["allow_live_api_writes"])

    def test_legacy_token_is_claimed_by_only_one_tool_user(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            legacy = data_dir / "legacy-token.json"
            legacy.write_text('{"access_token":"encrypted"}', encoding="utf-8")
            with (
                patch.object(token_provider, "DATA_DIR", str(data_dir)),
                patch.object(token_provider, "QIANCHUAN_API_TOKEN_FILE", str(legacy)),
                patch.object(token_provider, "_current_owner", return_value="user_a"),
            ):
                user_a_path = Path(token_provider.resolve_token_path())
            self.assertTrue(user_a_path.is_file())

            with (
                patch.object(token_provider, "DATA_DIR", str(data_dir)),
                patch.object(token_provider, "QIANCHUAN_API_TOKEN_FILE", str(legacy)),
                patch.object(token_provider, "_current_owner", return_value="user_b"),
            ):
                user_b_path = Path(token_provider.resolve_token_path())
            self.assertNotEqual(user_a_path, user_b_path)
            self.assertFalse(user_b_path.exists())


class MigrationIntegrityTests(unittest.TestCase):
    def test_selected_migration_source_gets_deep_integrity_check(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "valid.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
            LegacyMigrationService._validate_selected_source(database)

    def test_corrupt_selected_source_is_rejected_before_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "corrupt.db"
            database.write_bytes(os.urandom(1024))
            with self.assertRaises((sqlite3.DatabaseError, ValueError)):
                LegacyMigrationService._validate_selected_source(database)


class MultiAccountCollectionQueueStressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "stress.db")
        init_sqlite_schema(database=self.database)
        self.db = SQLiteStore(database=self.database)
        self.owner_patch = patch.dict(
            os.environ,
            {"QCSCKP_SESSION_OWNER": "stress-owner"},
        )
        self.owner_patch.start()

    def tearDown(self):
        self.owner_patch.stop()
        self.temp.cleanup()

    def _seed_20_accounts_with_10_targets_each(self):
        target_uids = []
        for account_index in range(20):
            account_uid = f"stress-account-{account_index:02d}"
            aavid = f"9100{account_index:02d}"
            self.db.insert(
                "qianchuan_account",
                {
                    "account_uid": account_uid,
                    "owner_username": "stress-owner",
                    "aavid": aavid,
                    "account_name": f"压力账户{account_index:02d}",
                    "directory_selected": 1,
                    "enabled": 1,
                },
            )
            for plan_index in range(10):
                target_uid = f"stress-target-{account_index:02d}-{plan_index:02d}"
                target_uids.append(target_uid)
                self.db.insert(
                    "promotion_target",
                    {
                        "target_uid": target_uid,
                        "account_uid": account_uid,
                        "aadvid": aavid,
                        "ad_id": f"92{account_index:02d}{plan_index:02d}",
                        "plan_name": f"压力计划{account_index:02d}-{plan_index:02d}",
                        "promotion_scene": "live" if plan_index % 2 == 0 else "product",
                        "plan_system": "chengfang" if plan_index % 3 == 0 else "global",
                        "platform_status": "active",
                        "verification_state": "verified",
                        "monitor_eligible": 1,
                        "enabled": 1,
                    },
                )
        return target_uids

    def test_20_by_10_queue_claims_one_target_per_account_before_second_round(self):
        target_uids = self._seed_20_accounts_with_10_targets_each()
        queued = _enqueue_collection_jobs(
            target_uids,
            db=self.db,
            priority=20,
            due_at=datetime.now() - timedelta(seconds=1),
        )
        self.assertEqual(200, queued)

        claimed = _claim_collection_jobs(db=self.db, limit=20)
        self.assertEqual(20, len(claimed))
        self.assertEqual(20, len({row["account_uid"] for row in claimed}))

    def test_claim_never_leases_more_than_two_plans_for_one_account(self):
        target_uids = self._seed_20_accounts_with_10_targets_each()[:10]
        _enqueue_collection_jobs(
            target_uids,
            db=self.db,
            priority=20,
            due_at=datetime.now() - timedelta(seconds=1),
        )
        claimed = _claim_collection_jobs(db=self.db, limit=6)
        self.assertEqual(2, len(claimed))
        self.assertEqual(1, len({row["account_uid"] for row in claimed}))

    def test_expired_lease_is_recovered_with_a_new_fencing_token(self):
        target_uid = self._seed_20_accounts_with_10_targets_each()[0]
        _enqueue_collection_jobs(
            [target_uid],
            db=self.db,
            priority=100,
            due_at=datetime.now() - timedelta(seconds=1),
        )
        first = _claim_collection_jobs(db=self.db, limit=1)[0]
        self.db.execute(
            "UPDATE collection_job SET lease_expires_at=? WHERE id=?",
            (
                (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S"),
                first["id"],
            ),
        )
        second = _claim_collection_jobs(db=self.db, limit=1)[0]
        self.assertEqual(first["id"], second["id"])
        self.assertGreater(second["fencing_token"], first["fencing_token"])

        _finish_collection_job(first, {"success": True}, db=self.db)
        still_leased = self.db.select_one("collection_job", where={"id": first["id"]})
        self.assertEqual("leased", still_leased["status"])

        _finish_collection_job(second, {"success": True}, db=self.db)
        finished = self.db.select_one("collection_job", where={"id": first["id"]})
        self.assertEqual("queued", finished["status"])

    def test_refresh_during_live_lease_never_revokes_worker_ownership(self):
        target_uid = self._seed_20_accounts_with_10_targets_each()[0]
        _enqueue_collection_jobs(
            [target_uid],
            db=self.db,
            priority=20,
            due_at=datetime.now() - timedelta(seconds=1),
        )
        leased = _claim_collection_jobs(db=self.db, limit=1)[0]
        _enqueue_collection_jobs(
            [target_uid],
            db=self.db,
            priority=100,
            due_at=datetime.now() - timedelta(seconds=1),
        )
        saved = self.db.select_one("collection_job", where={"id": leased["id"]})
        self.assertEqual("leased", saved["status"])
        self.assertEqual(leased["lease_owner"], saved["lease_owner"])
        self.assertEqual(100, saved["priority"])
        _finish_collection_job(leased, {"success": True}, db=self.db)
        rerun = self.db.select_one("collection_job", where={"id": leased["id"]})
        self.assertEqual("queued", rerun["status"])
        self.assertEqual(100, rerun["priority"])
        self.assertLessEqual(
            datetime.strptime(rerun["due_at"], "%Y-%m-%d %H:%M:%S"),
            datetime.now(),
        )

    def test_claim_fills_capacity_across_priority_bands(self):
        target_uids = self._seed_20_accounts_with_10_targets_each()
        _enqueue_collection_jobs(
            [target_uids[0]],
            db=self.db,
            priority=100,
            due_at=datetime.now() - timedelta(seconds=1),
        )
        _enqueue_collection_jobs(
            target_uids[10:13],
            db=self.db,
            priority=20,
            due_at=datetime.now() - timedelta(seconds=1),
        )
        claimed = _claim_collection_jobs(db=self.db, limit=3)
        self.assertEqual(3, len(claimed))
        self.assertEqual(100, claimed[0]["priority"])
        self.assertEqual([20, 20], [row["priority"] for row in claimed[1:]])


if __name__ == "__main__":
    unittest.main()
