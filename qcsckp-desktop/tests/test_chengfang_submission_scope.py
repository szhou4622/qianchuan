"""Submission uses the collected Chengfang scope and the existing fenced gate."""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from services import official_api_reconciliation as reconciliation
from services.official_api_execution import OfficialApiRetargetingService
from services.official_api_execution import prepare_submission_gate
from services.qianchuan_open_api import token_provider as tokens
from services.qianchuan_open_api.client import QianchuanOpenApiClient
from services.qianchuan_open_api.errors import ApiRequestError
from services.qianchuan_open_api.service import QianchuanOfficialApiService
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class Reply:
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"code": 0, "data": {"task_id": "9001"}, "request_id": "fake-create"}).encode()


class ChengfangSubmissionScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qcsckp-chengfang-send-")
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteStore(database=str(Path(self.temp.name) / "state.db"))
        init_sqlite_schema(database=self.store.config["database"])
        self.owner = "chengfang-test-owner"
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        self.scope = {"metric_scope": "chengfang_anchor_material", "attribution": "unique_plan_in_complete_catalog",
                      "advertiser_id": "1001", "ad_id": "2001", "anchor_id": "8001", "ecp_app_id": "1",
                      "data_period": "ALL_DATA", "start_date": today, "end_date": today}
        self.capability = {"material_metric_source": "chengfang_anchor_material_report", "material_sync_complete": True,
                           "material_metric_contract": "chengfang_anchor_report_v1:scope-a",
                           "material_metric_scope": dict(self.scope),
                           "material_metric_evidence": {"source": "chengfang_anchor_material_report", "stat_date": today,
                                                        "observed_at": now.strftime("%Y-%m-%d %H:%M:%S")}}
        self.store.insert("qianchuan_account", {"account_uid": "account-a", "owner_username": self.owner,
                         "aavid": "1001", "directory_selected": 1, "enabled": 1})
        self.store.insert("promotion_target", {"target_uid": "target-a", "account_uid": "account-a", "aadvid": "1001",
                         "ad_id": "2001", "promotion_scene": "live", "plan_system": "chengfang", "enabled": 1,
                         "monitor_eligible": 1, "retarget_eligible": 1, "capability_json": json.dumps(self.capability)})
        self.claim = {"task_uid": "scope-card", "account_username": self.owner, "claim_token": "fake-claim", "fencing_token": 4}
        self.store.insert("local_retarget_task", {**self.claim, "action_nonce": "fake-nonce", "action_type": "retarget",
                         "status": "executing", "payload_json": "{}",
                         "expires_at": (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                         "claim_expires_at": (now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for item in (patch("services.qianchuan_session.current_session_owner", return_value=self.owner),
                     patch.object(reconciliation, "current_session_owner", return_value=self.owner),
                     patch.object(reconciliation, "SQLiteStore", return_value=self.store),
                     patch("services.official_api_execution.SQLiteStore", return_value=self.store),
                     patch.object(reconciliation, "start_official_api_reconciliation_background_thread"),
                     patch("services.official_api_execution._existing_reconciliation", return_value=None),
                     patch("services.official_api_execution._check_plan", return_value={})):
            self.stack.enter_context(item)
        self.token = Mock()
        self.token.get_token.return_value = SimpleNamespace(access_token="fake-not-a-secret")
        self.client = QianchuanOpenApiClient(self.token, rate_limiter=Mock())
        self.service = QianchuanOfficialApiService(self.client, allow_writes=True)
        self.stack.enter_context(patch.object(self.service, "find_duplicate_control_task", return_value=None))
        self.stack.enter_context(patch("services.official_api_execution.get_official_api_service", return_value=self.service))
        self.proof = self.stack.enter_context(patch.object(self.service, "list_chengfang_live_material_report",
                                  return_value=([], ["fake-proof"], dict(self.scope))))
        self.materials = self.stack.enter_context(patch.object(self.service, "list_plan_materials", return_value=([
            {"material_id": "3001", "material_status": "DELIVERY_OK", "audit_status": "PASS"}], ["fake-member"])))
        self.http = self.stack.enter_context(patch("services.qianchuan_open_api.client.urlopen", return_value=Reply()))
        self.audit = self.stack.enter_context(patch("services.official_api_execution.OfficialApiAuditStore"))

    def run_retarget(self, **overrides):
        arguments = dict(aavid=1001, ad_id=2001, target_uid="target-a", material_id="3001",
                         promotion_scene="live", plan_system="chengfang", execution_uid="scope-intent",
                         reconciliation_task_uid=self.claim["task_uid"], submission_claim=self.claim,
                         retargeting={"method": "volume", "volume": {"total_budget_yuan": 100, "duration_hours": 24}})
        arguments.update(overrides)
        return asyncio.run(OfficialApiRetargetingService().run(**arguments))

    def save_capability(self):
        self.store.update("promotion_target", {"capability_json": json.dumps(self.capability)}, where={"target_uid": "target-a"})

    def assert_not_submitted(self, result):
        self.assertFalse(result.success)
        self.assertEqual("not_sent", json.loads(result.detail)["submission_phase"])
        self.http.assert_not_called()
        self.assertEqual([], self.store.select("execution_reconciliation"))

    def test_fresh_same_scope_reaches_exactly_one_real_fenced_post(self):
        result = self.run_retarget()
        self.assertTrue(result.success, result.detail)
        self.assertEqual("submitted_verifying", result.step)
        self.assertEqual(1, self.http.call_count)
        self.assertEqual(["stat_cost_for_roi2"], self.proof.call_args.kwargs["metrics"])
        row = self.store.select("execution_reconciliation")[0]
        self.assertEqual("accepted", json.loads(row["payload_json"])["submission_phase"])
        self.assertEqual(4, json.loads(row["payload_json"])["submission_claim"]["fencing_token"])

    def test_fresh_official_anchor_change_blocks_without_post(self):
        self.proof.return_value = ([], [], {**self.scope, "anchor_id": "8002"})
        self.assert_not_submitted(self.run_retarget())

    def test_competing_same_anchor_plan_blocks_without_post(self):
        self.proof.side_effect = ApiRequestError("同一抖音号存在多个乘方直播计划", code="client_metric_scope")
        self.assert_not_submitted(self.run_retarget())

    def test_scope_change_during_material_precheck_blocks_without_post(self):
        def members(*args, **kwargs):
            self.capability["material_metric_contract"] = "chengfang_anchor_report_v1:scope-b"
            self.save_capability()
            return [{"material_id": "3001", "material_status": "DELIVERY_OK", "audit_status": "PASS"}], []
        self.materials.side_effect = members
        self.assert_not_submitted(self.run_retarget())

    def test_gate_rechecks_scope_after_token_wait(self):
        def token(**kwargs):
            self.capability["material_metric_scope"]["anchor_id"] = "8002"
            self.save_capability()
            return SimpleNamespace(access_token="fake")
        self.token.get_token.side_effect = token
        self.assert_not_submitted(self.run_retarget())

    def test_gate_still_rejects_a_lost_claim_fence(self):
        def token(**kwargs):
            self.store.update("local_retarget_task", {"fencing_token": 5}, where={"task_uid": self.claim["task_uid"]})
            return SimpleNamespace(access_token="fake")
        self.token.get_token.side_effect = token
        self.assert_not_submitted(self.run_retarget())

    def test_stale_missing_or_foreign_metric_evidence_blocks_before_proof(self):
        for mode in ("stale", "missing", "owner"):
            with self.subTest(mode=mode):
                self.capability["material_metric_evidence"]["observed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.store.update("qianchuan_account", {"owner_username": self.owner}, where={"account_uid": "account-a"})
                if mode == "stale":
                    self.capability["material_metric_evidence"]["observed_at"] = (datetime.now()-timedelta(minutes=11)).strftime("%Y-%m-%d %H:%M:%S")
                elif mode == "missing":
                    self.capability["material_metric_evidence"]["observed_at"] = ""
                else:
                    self.store.update("qianchuan_account", {"owner_username": "another-owner"}, where={"account_uid": "account-a"})
                self.save_capability()
                self.assert_not_submitted(self.run_retarget())
        self.proof.assert_not_called()

    def test_manual_run_without_target_retains_existing_path(self):
        result = self.run_retarget(target_uid=None)
        self.assertTrue(result.success, result.detail)
        self.assertEqual(1, self.http.call_count)
        self.proof.assert_not_called()

    def install_test_grant(self):
        for item in (patch.object(tokens, "DATA_DIR", self.temp.name),
                     patch.object(tokens, "QIANCHUAN_API_TOKEN_FILE", str(Path(self.temp.name) / "absent-legacy.json")),
                     patch.object(tokens, "_protect", side_effect=lambda value: value),
                     patch.object(tokens, "_unprotect", side_effect=lambda value: value)):
            self.stack.enter_context(item)
        tokens.save_api_credentials("123001", "synthetic-secret-one")

    def test_clear_in_initial_preflight_cannot_capture_a_new_grant_later(self):
        self.install_test_grant()
        def preflight(*args, **kwargs):
            tokens.clear_api_configuration()
            tokens.save_api_credentials("123002", "synthetic-secret-two")
            return {}
        with patch("services.official_api_execution._check_plan", side_effect=preflight):
            self.assert_not_submitted(self.run_retarget())

    def test_new_grant_during_token_wait_blocks_post_even_with_unchanged_local_data(self):
        self.install_test_grant()
        def token(**kwargs):
            tokens.save_api_credentials("123001", "synthetic-secret-two")
            return SimpleNamespace(access_token="fake")
        self.token.get_token.side_effect = token
        self.assert_not_submitted(self.run_retarget())

    def test_account_detached_during_token_wait_blocks_post(self):
        self.install_test_grant()
        def token(**kwargs):
            self.store.update("qianchuan_account", {"directory_selected": 0}, where={"account_uid": "account-a"})
            return SimpleNamespace(access_token="fake")
        self.token.get_token.side_effect = token
        self.assert_not_submitted(self.run_retarget())

    def test_grant_changed_by_submission_callback_blocks_before_intent(self):
        self.install_test_grant()
        called = 0
        def guard():
            nonlocal called
            called += 1
            # First call is the end-of-preflight check. The second occurs
            # within the short authorization/intent transaction boundary.
            if called == 2:
                tokens.clear_api_configuration()
            return ""
        self.assert_not_submitted(self.run_retarget(pre_submit_check=guard))
        self.assertEqual(2, called)

    def test_send_gate_and_clear_have_a_single_order_and_retain_reserved_intent(self):
        self.install_test_grant()
        started, finished = threading.Event(), threading.Event()
        def clear():
            started.set()
            tokens.clear_api_configuration()
            finished.set()
        thread = threading.Thread(target=clear)
        def guard():
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.05))
            return ""
        gate = prepare_submission_gate(task_uid=self.claim["task_uid"], action_type="retarget", aavid=1001,
            ad_id=2001, intent_key="ordered-clear", verify_payload={"target_uid": "target-a"},
            submission_claim=self.claim, pre_submit_check=guard)
        try:
            gate.before_send()
        finally:
            thread.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual("sending", gate.phase)
        row = self.store.select("execution_reconciliation")[0]
        self.assertEqual("submitting", row["status"])
        with self.assertRaises(tokens.AuthorizationContextChanged):
            gate.before_followup("duration")
        self.assertEqual("submitting", self.store.select("execution_reconciliation")[0]["status"])
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
