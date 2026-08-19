import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from services.official_api_operation_logs import sync_official_operation_logs
from services.qianchuan_accounts import ensure_qianchuan_account
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class OfficialOperationLogWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temp.name, "official-logs.db")
        init_sqlite_schema(database=self.database)
        self.db = SQLiteStore(database=self.database)
        self.owner = patch.dict(os.environ, {"QCSCKP_SESSION_OWNER": "log-owner"})
        self.owner.start()
        ensure_qianchuan_account(
            "1001",
            owner_username="log-owner",
            account_name="日志账户",
            enabled=True,
            seen=True,
            db=self.db,
        )

    def tearDown(self):
        self.owner.stop()
        self.temp.cleanup()

    def test_initial_thirty_day_backfill_advances_in_bounded_windows(self):
        service = MagicMock()
        service.OPERATION_LOGS = "/operation/logs"
        service.list_operation_logs.return_value = ([], ["req-log"])
        with patch(
            "services.official_api_operation_logs.get_official_api_service",
            return_value=service,
        ):
            first = sync_official_operation_logs("1001", days=30, db=self.db)
            second = sync_official_operation_logs("1001", days=30, db=self.db)
        first_start = datetime.strptime(first["sync_window_from"], "%Y-%m-%d %H:%M:%S")
        first_end = datetime.strptime(first["coverage_to"], "%Y-%m-%d %H:%M:%S")
        second_end = datetime.strptime(second["coverage_to"], "%Y-%m-%d %H:%M:%S")
        self.assertLessEqual((first_end - first_start).total_seconds(), 24 * 3600)
        self.assertTrue(first["backfill_remaining"])
        self.assertGreater(second_end, first_end)
        state = self.db.select_one(
            "platform_log_sync_state", where={"aavid": "1001"}
        )
        self.assertEqual("backfilling", state["last_status"])


if __name__ == "__main__":
    unittest.main()
