import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production_v1a.migration import LegacyMigrationService
from services.qianchuan_open_api import runtime_settings
from services.qianchuan_open_api import token_provider


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


if __name__ == "__main__":
    unittest.main()
