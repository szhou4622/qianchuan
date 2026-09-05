"""r19 send-boundary tests: isolated SQLite and fake urlopen only."""
import asyncio
from contextlib import ExitStack, asynccontextmanager, closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from services import official_api_reconciliation as reconciliation
from services.official_api_execution import OfficialApiRegulationStopService, OfficialApiRetargetingService
from services.qianchuan_open_api.client import ApiResponse, QianchuanOpenApiClient
from services.qianchuan_open_api.service import QianchuanOfficialApiService
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class Reply:
    status = 200
    headers = {}

    def __init__(self, payload=None):
        self.payload = payload or {"code": 0, "data": {}, "request_id": "synthetic-request"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class SubmissionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qcsckp-r19-send-")
        self.db = SQLiteStore(database=str(Path(self.temp.name) / "profile.db"))
        init_sqlite_schema(database=self.db.config["database"])
        self.claim = {"task_uid": "card-r19", "account_username": "owner-r19",
                      "claim_token": "synthetic-claim", "fencing_token": 4}
        self.seed_claim(self.db)
        self.stack = ExitStack()
        self.stack.enter_context(patch("services.qianchuan_session.current_session_owner", return_value="owner-r19"))
        self.stack.enter_context(patch.object(reconciliation, "current_session_owner", return_value="owner-r19"))
        self.stack.enter_context(patch.object(reconciliation, "SQLiteStore", return_value=self.db))
        self.start_worker = self.stack.enter_context(patch.object(reconciliation, "start_official_api_reconciliation_background_thread"))
        self.stack.enter_context(patch("services.official_api_execution._check_plan", return_value={}))
        self.stack.enter_context(patch("services.official_api_execution._find_control_task", return_value={
            "task_id": "3001", "scene": "MATERIAL_ADD_BUDGET", "status": "PROCESSING",
        }))
        self.order = []
        limiter = Mock()
        limiter.wait_for_request.side_effect = lambda *args: self.order.append("limiter")
        token = Mock()
        token.get_token.side_effect = lambda **kwargs: self.order.append("token") or SimpleNamespace(access_token="fake-not-real")
        self.client = QianchuanOpenApiClient(token, rate_limiter=limiter)
        self.service = QianchuanOfficialApiService(self.client, allow_writes=True)
        self.stack.enter_context(patch("services.official_api_execution.get_official_api_service", return_value=self.service))
        self.http = self.stack.enter_context(patch("services.qianchuan_open_api.client.urlopen", side_effect=lambda *a, **k: self.order.append("http") or Reply()))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def seed_claim(self, store):
        now = datetime.now()
        store.insert("local_retarget_task", {
            **self.claim, "action_nonce": "nonce", "action_type": "stop", "status": "executing",
            "payload_json": "{}", "expires_at": (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "claim_expires_at": (now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
        })

    def run_stop(self):
        return asyncio.run(OfficialApiRegulationStopService().run(
            aavid=1001, ad_id=2001, assist_task_id="3001", stop_action="pause",
            execution_uid="intent-r19", reconciliation_task_uid=self.claim["task_uid"],
            submission_claim=self.claim, pre_submit_check=lambda: self.order.append("guard") or "",
        ))

    def intent(self):
        return self.db.select_one("execution_reconciliation", where={"idempotency_key": "intent-r19"})

    def test_send_gate_runs_after_limiter_and_token_and_releases_transaction(self):
        def http(*args, **kwargs):
            self.order.append("http")
            with self.db.transaction() as connection:
                connection.execute("BEGIN IMMEDIATE")
            return Reply()
        self.http.side_effect = http
        result = self.run_stop()
        self.assertEqual("submitted_verifying", result.step)
        self.assertEqual(["limiter", "token", "guard", "http"], self.order)
        row = self.intent()
        self.assertEqual("accepted", json.loads(row["payload_json"])["submission_phase"])

    def test_claim_lost_during_token_wait_never_posts_or_reserves(self):
        def token(**kwargs):
            self.db.update("local_retarget_task", {"fencing_token": 5}, where={"task_uid": self.claim["task_uid"]})
            return SimpleNamespace(access_token="fake")
        self.client.token_provider.get_token.side_effect = token
        result = self.run_stop()
        self.assertFalse(result.success)
        self.assertEqual("not_sent", json.loads(result.detail)["submission_phase"])
        self.http.assert_not_called()
        self.assertIsNone(self.intent())

    def test_audit_error_after_acceptance_never_becomes_confirmed_failed(self):
        with patch("services.official_api_execution.OfficialApiAuditStore", side_effect=sqlite3.OperationalError("synthetic audit failure")):
            result = self.run_stop()
        self.assertEqual("submitted_verifying", result.step)
        self.assertEqual("submitted", self.intent()["status"])
        self.run_stop()
        self.assertEqual(1, self.http.call_count)

    def test_worker_start_error_does_not_negate_accepted_post(self):
        self.start_worker.side_effect = RuntimeError("synthetic worker unavailable")
        self.assertEqual("submitted_verifying", self.run_stop().step)
        self.assertEqual("submitted", self.intent()["status"])
        self.assertEqual("accepted", json.loads(self.intent()["payload_json"])["submission_phase"])

    def test_total_post_bookkeeping_failure_keeps_sending_barrier(self):
        with patch.object(reconciliation, "enqueue_execution_reconciliation", side_effect=sqlite3.OperationalError("queue unavailable")), patch.object(
            reconciliation, "record_execution_submission_phase", side_effect=sqlite3.OperationalError("phase unavailable")
        ):
            self.assertEqual("submitted_verifying", self.run_stop().step)
        self.assertEqual("submitting", self.intent()["status"])
        self.assertEqual("sending", json.loads(self.intent()["payload_json"])["submission_phase"])
        self.run_stop()
        self.assertEqual(1, self.http.call_count)

    def test_accepted_create_without_task_id_uses_manifest_for_read_only_recovery(self):
        with patch.object(self.service, "list_plan_materials", return_value=([{
            "material_id": "4001", "material_status": "ENABLE", "audit_status": "PASS",
        }], [])), patch.object(self.service, "find_duplicate_control_task", return_value=None), patch(
            "services.official_api_execution._existing_reconciliation", return_value=None
        ):
            result = asyncio.run(OfficialApiRetargetingService().run(
                aavid=1001, ad_id=2001, material_id="4001", execution_uid="intent-r19",
                reconciliation_task_uid=self.claim["task_uid"], submission_claim=self.claim,
                promotion_scene="product", plan_system="global",
                retargeting={"method": "volume", "volume": {"total_budget_yuan": 100, "duration_hours": 24}},
            ))
        self.assertEqual("submitted_verifying", result.step)
        self.assertEqual("", result.regulate_task_id)
        payload = json.loads(self.intent()["payload_json"])
        self.assertTrue(payload["task_name"])
        self.assertEqual("accepted", payload["submission_phase"])
        self.db.update("execution_reconciliation", {"next_attempt_at": "2000-01-01 00:00:00"}, where={"idempotency_key": "intent-r19"})
        task = reconciliation._claim_one(self.db, "owner-r19")
        with patch.object(reconciliation, "get_official_api_service", return_value=self.service), patch.object(
            self.service, "find_duplicate_control_task", return_value={"task_id": "5001"}
        ) as find, patch("services.official_api_execution._verify_control_task", return_value={"task_id": "5001"}), patch.object(
            reconciliation, "_finish"
        ) as finish:
            reconciliation._verify_one(self.db, task)
        find.assert_called_once()
        finish.assert_called_once()
        self.assertEqual(1, self.http.call_count)

    def test_enqueue_error_preserves_accepted_intent_and_restart_only_verifies(self):
        with patch.object(reconciliation, "enqueue_execution_reconciliation", side_effect=sqlite3.OperationalError("synthetic queue failure")):
            result = self.run_stop()
        self.assertEqual("submitted_verifying", result.step)
        self.assertEqual("accepted", json.loads(self.intent()["payload_json"])["submission_phase"])
        self.db.update("execution_reconciliation", {"updated_at": "2000-01-01 00:00:00"}, where={"idempotency_key": "intent-r19"})
        self.assertEqual(1, reconciliation.recover_interrupted_submissions("owner-r19", db=self.db))
        task = reconciliation._claim_one(self.db, "owner-r19")
        with patch("services.official_api_execution._find_control_task", return_value={"status": "DISABLE"}) as find, patch.object(reconciliation, "_finish") as finish, patch.object(reconciliation, "get_official_api_service", return_value=self.service):
            reconciliation._verify_one(self.db, task)
        find.assert_called_once()
        finish.assert_called_once()
        self.assertEqual(1, self.http.call_count)

    def test_explicit_platform_rejection_is_distinct_from_network_unknown(self):
        self.http.return_value = Reply({"code": 400, "message": "invalid parameter"})
        self.http.side_effect = None
        self.assertFalse(self.run_stop().success)
        self.assertEqual("confirmed_failed", self.intent()["status"])
        self.assertEqual("rejected", json.loads(self.intent()["payload_json"])["submission_phase"])

    def test_network_unknown_is_queued_read_only_and_cannot_repost(self):
        self.http.side_effect = TimeoutError("synthetic transport uncertainty")
        self.assertEqual("submitted_verifying", self.run_stop().step)
        self.assertEqual("unknown", json.loads(self.intent()["payload_json"])["submission_phase"])
        self.run_stop()
        self.assertEqual(1, self.http.call_count)

    def test_stale_sending_intent_recovery_cannot_repost(self):
        row, reserved = reconciliation.reserve_execution_intent(
            task_uid=self.claim["task_uid"], action_type="stop", aavid=1001, ad_id=2001,
            control_task_id="3001", idempotency_key="intent-r19", verify_payload={"expected_status": "PAUSE"},
            submission_claim=self.claim, submission_phase="sending", db=self.db,
        )
        self.assertTrue(reserved)
        self.db.update("execution_reconciliation", {"updated_at": "2000-01-01 00:00:00"}, where={"reconciliation_uid": row["reconciliation_uid"]})
        reconciliation.recover_interrupted_submissions("owner-r19", db=self.db)
        self.run_stop()
        self.http.assert_not_called()
        self.assertEqual("unknown", json.loads(self.intent()["payload_json"])["submission_phase"])

    def test_claim_and_shared_ledger_reservation_use_one_attached_transaction(self):
        base = Path(self.temp.name) / "managed"
        paths = SimpleNamespace(data=base / "data", shared=base / "shared", legacy=base / "legacy")
        with patch("channel_runtime.layout", return_value=paths), patch("services.channel_ledger.layout", return_value=paths):
            store = SQLiteStore(database=str(paths.data / "qianchuan.db"))
            init_sqlite_schema(database=store.config["database"])
            self.seed_claim(store)
            from services.local_feishu_bridge import assert_valid_local_claim
            def inspect(connection, claim, **kwargs):
                self.assertTrue(connection.in_transaction)
                self.assertIn("channel_guard", [item[1] for item in connection.execute("PRAGMA database_list")])
                return assert_valid_local_claim(connection, claim, **kwargs)
            with patch("services.local_feishu_bridge.assert_valid_local_claim", side_effect=inspect):
                _, reserved = reconciliation.reserve_execution_intent(
                    task_uid=self.claim["task_uid"], action_type="stop", aavid=1001, ad_id=2001,
                    idempotency_key="atomic-shared", verify_payload={}, submission_claim=self.claim,
                    submission_phase="sending", db=store,
                )
            self.assertTrue(reserved)
            with closing(sqlite3.connect(paths.shared / "execution.sqlite3")) as shared:
                self.assertEqual(1, shared.execute("SELECT COUNT(*) FROM execution_reconciliation").fetchone()[0])

    def test_concurrent_same_cycle_has_only_one_reserved_sender(self):
        def reserve():
            return reconciliation.reserve_execution_intent(
                task_uid=self.claim["task_uid"], action_type="stop", aavid=1001, ad_id=2001,
                idempotency_key="concurrent-cycle", verify_payload={}, submission_claim=self.claim,
                submission_phase="sending", db=self.db,
            )[1]
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: reserve(), range(2)))
        self.assertEqual([False, True], sorted(outcomes))

    def test_accepted_phase_cannot_be_downgraded_by_late_local_failure(self):
        self.run_stop()
        reconciliation.record_execution_submission_phase("intent-r19", "rejected", db=self.db)
        reconciliation.finish_execution_intent("intent-r19", status="confirmed_failed", db=self.db)
        self.assertEqual("submitted", self.intent()["status"])
        self.assertEqual("accepted", json.loads(self.intent()["payload_json"])["submission_phase"])

    def test_terminal_reconciliation_can_finish_original_expired_claim_without_worker_report(self):
        from services import local_feishu_bridge as bridge
        self.run_stop()
        self.db.update("local_retarget_task", {
            "expires_at": "2000-01-01 00:00:00", "claim_expires_at": "2000-01-01 00:00:00",
        }, where={"task_uid": self.claim["task_uid"]})
        self.db.update("execution_reconciliation", {"next_attempt_at": "2000-01-01 00:00:00"},
                       where={"idempotency_key": "intent-r19"})
        task = reconciliation._claim_one(self.db, "owner-r19")
        with patch.object(bridge, "DB_FILE", self.db.config["database"]), patch.object(bridge._MANAGER, "bridge", return_value=None):
            self.assertTrue(reconciliation._finish(self.db, task, status="confirmed_succeeded", verified={"status": "DISABLE"}))
        local = self.db.select_one("local_retarget_task", where={"task_uid": self.claim["task_uid"]})
        self.assertEqual("succeeded", local["status"])
        self.assertEqual(4, local["fencing_token"])
        self.assertEqual(1, self.http.call_count)

    def run_budget(self, lose_claim_after_budget=False):
        from services import retarget_task_worker as worker
        @asynccontextmanager
        async def lock(*args, **kwargs):
            yield
        task = {"task_uid": self.claim["task_uid"], "target_uid": "target-r19",
                "assist_task_id": "3001", "submission_claim": self.claim}
        target = {"aadvid": "1001", "ad_id": "2001", "promotion_scene": "product"}
        calculation = {"new_budget_yuan": 200, "extend_hours": 2}
        def http(request, **kwargs):
            self.order.append(request.full_url.rsplit('/', 2)[-2])
            if lose_claim_after_budget:
                self.db.update("local_retarget_task", {"fencing_token": 5}, where={"task_uid": self.claim["task_uid"]})
            return Reply()
        self.http.side_effect = http
        with patch.object(worker, "exclusive_browser_operation", side_effect=lock), patch.object(
            worker, "_validate_budget_increase_task", return_value=({}, {}, target, {}, calculation)
        ), patch("config.QIANCHUAN_BACKEND", "official_api"), patch(
            "services.qianchuan_open_api.runtime.get_official_api_service", return_value=self.service
        ), patch.object(self.service, "list_control_tasks", return_value=([{
            "task_id": "3001", "scene": "MATERIAL_ADD_BUDGET", "status": "PROCESSING", "duration": 24,
        }], [])):
            return asyncio.run(worker._execute_budget_increase_task(task, self.db))

    def test_budget_accepted_then_lost_claim_cannot_send_duration_or_report_full_success(self):
        result = self.run_budget(lose_claim_after_budget=True)
        self.assertEqual("submitted_verifying", result["step"])
        self.assertEqual(1, self.http.call_count)
        row = self.db.select_one("execution_reconciliation", where={"action_type": "budget"})
        data = json.loads(row["payload_json"])
        self.assertEqual("accepted", data["submission_phase"])
        self.assertEqual(["budget", "duration"], data["required_steps"])
        self.assertEqual(["budget"], data["attempted_steps"])
        with patch.object(reconciliation, "_finish") as finish, patch.object(reconciliation, "_retry") as retry:
            reconciliation._verify_one(self.db, row)
        finish.assert_not_called()
        retry.assert_called_once()

    def test_budget_and_duration_are_both_attempted_before_overall_verification(self):
        result = self.run_budget()
        self.assertEqual("submitted_verifying", result["step"])
        self.assertEqual(2, self.http.call_count)
        row = self.db.select_one("execution_reconciliation", where={"action_type": "budget"})
        data = json.loads(row["payload_json"])
        self.assertEqual(["budget", "duration"], data["attempted_steps"])
        self.assertEqual(["budget", "duration"], data["completed_steps"])
        self.assertEqual(200, data["budget"])
        self.assertEqual(26, data["duration"])


if __name__ == "__main__":
    unittest.main()
