"""Authorization changes invalidate only access state; all I/O is isolated and mocked."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from services import official_api_collection as collection
from services import official_api_authorization as state
from services import official_api_catalog as catalog
from services import collection_lifecycle
from services.qianchuan_open_api import token_provider as tokens
from services.qianchuan_open_api.collection_context import CollectionContext
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class AuthorizationCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.owner = "owner-a"
        root = Path(self.temp.name)
        for item in (
            patch.object(tokens, "DATA_DIR", str(root)),
            patch.object(tokens, "QIANCHUAN_API_TOKEN_FILE", str(root / "no-legacy.json")),
            patch.object(tokens, "_current_owner", side_effect=lambda: self.owner),
            patch.object(tokens, "_protect", side_effect=lambda value: value),
            patch.object(tokens, "_unprotect", side_effect=lambda value: value),
            patch.object(collection, "_owner_key", side_effect=lambda: self.owner),
            patch.object(catalog, "_owner_key", side_effect=lambda: self.owner),
        ):
            self.stack.enter_context(item)
        path = str(root / "state.db")
        init_sqlite_schema(database=path)
        self.db = SQLiteStore(database=path)
        tokens.save_api_credentials("100001", "test-only-secret-a")
        self.identity = tokens.get_authorization_identity()
        collection._reset_adaptive_collection_state_for_tests()
        self.addCleanup(collection._reset_adaptive_collection_state_for_tests)
        self.db.insert_or_update("qianchuan_account", {
            "account_uid": "account-a", "owner_username": self.owner, "aavid": "2001",
            "directory_selected": 1, "enabled": 1,
        }, unique_fields=["account_uid"])
        self.db.insert_or_update("promotion_target", {
            "target_uid": "target-a", "account_uid": "account-a", "aadvid": "2001", "ad_id": "3001",
            "promotion_scene": "product", "plan_system": "global", "platform_status": "active",
            "verification_state": "verified", "monitor_eligible": 1, "retarget_eligible": 1,
            "stop_eligible": 1, "enabled": 1, "last_sync_at": "2026-08-30 12:00:00",
            "capability_json": json.dumps({"retarget_supported": True, "collection_committed_at": "2026-08-30 12:00:00",
                "material_backfill_state": {"2026-08-29": {"status": "succeeded"}}}),
        }, unique_fields=["target_uid"])

    def key(self, account="2001"):
        return collection._target_account_key({"aadvid": account})

    def rotate(self, app="100001"):
        tokens.save_api_credentials(app, "test-only-secret-b")
        return tokens.get_authorization_identity()

    def legacy(self, error, seconds=180):
        from datetime import datetime, timedelta
        self.db.insert_or_update("api_quota_state", {
            "scope_key": state.scope_hash(self.owner, "account", "2001"),
            "owner_username": self.owner, "scope_type": "account", "scope_id": "2001",
            "backoff_until": (datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S"),
            "last_error": error,
        }, unique_fields=["scope_key"])

    def test_token_backoff_is_not_rate_limit_or_degraded_lane(self):
        key = self.key()
        collection._set_account_backoff(key, 120, db=self.db, reason="token", error="41013")
        result = collection._account_backoff_state(key, db=self.db)
        self.assertEqual("token", result["reason"])
        self.assertEqual(0, result["rate_limit_seconds"])
        self.assertNotIn(key, collection._ACCOUNT_RATE_LIMITED)
        self.assertEqual("account_auth", self.db.select("api_quota_state")[0]["scope_type"])

    def test_same_app_new_grant_clears_auth_but_preserves_true_throttle(self):
        key = self.key()
        collection._set_account_backoff(key, 350, db=self.db, reason="rate_limit", include_application=True)
        collection._set_account_backoff(key, 120, db=self.db, reason="permission")
        current = self.rotate()
        result = collection.handle_authorization_change(self.identity, current, db=self.db, notify_catalog=Mock())
        self.assertEqual(1, result["auth_backoffs_cleared"])
        self.assertGreater(collection._account_backoff_state(key, db=self.db)["rate_limit_seconds"], 300)
        self.assertEqual({"account", "application"}, {r["scope_type"] for r in self.db.select("api_quota_state")})

    def test_shorter_rate_limit_never_shortens_durable_deadline(self):
        key = self.key()
        collection._set_account_backoff(key, 350, db=self.db, reason="rate_limit")
        first = self.db.select("api_quota_state")[0]["backoff_until"]
        collection._set_account_backoff(key, 35, db=self.db, reason="rate_limit")
        self.assertEqual(first, self.db.select("api_quota_state")[0]["backoff_until"])

    def test_same_advertiser_different_app_and_owner_are_isolated(self):
        old_key = self.key()
        collection._set_account_backoff(old_key, 350, db=self.db, reason="rate_limit", include_application=True)
        self.rotate("100002")
        self.assertNotEqual(old_key, self.key())
        self.assertEqual(0, collection._account_backoff_remaining(self.key(), db=self.db))
        self.owner = "owner-b"
        tokens.save_api_credentials("100001", "test-only-secret-c")
        self.assertEqual(0, collection._account_backoff_remaining(self.key(), db=self.db))

    def test_application_escalation_does_not_mix_apps(self):
        self.assertFalse(collection._should_escalate_application_backoff(self.key(), now_monotonic=100))
        self.rotate("100002")
        self.assertFalse(collection._should_escalate_application_backoff(self.key("2002"), now_monotonic=101))
        self.assertTrue(collection._should_escalate_application_backoff(self.key("2003"), now_monotonic=102))

    def test_legacy_41013_is_recognized_as_auth(self):
        self.legacy("41013 refresh_token 因用户重新授权失效")
        result = collection._account_backoff_state(self.key(), db=self.db)
        self.assertEqual("token", result["reason"])
        self.assertNotIn(self.key(), collection._ACCOUNT_RATE_LIMITED)
        self.assertEqual("account_auth", self.db.select("api_quota_state")[0]["scope_type"])

    def test_legacy_real_throttle_is_bound_to_previous_app_on_switch(self):
        self.legacy("40110 rate_limit")
        current = self.rotate("100002")
        collection.handle_authorization_change(self.identity, current, db=self.db, notify_catalog=Mock())
        self.assertEqual(0, collection._account_backoff_remaining(self.key(), db=self.db))
        row = self.db.select("api_quota_state")[0]
        self.assertEqual("100001", state.split_scope(row["scope_id"], {})[1])
        self.assertEqual("rate_limit", state.backoff_reason(row["last_error"]))

    def test_legacy_auth_cleared_after_authorization(self):
        self.legacy("41013 refresh_token失效")
        current = self.rotate()
        collection.handle_authorization_change(self.identity, current, db=self.db, notify_catalog=Mock())
        self.assertEqual([], self.db.select("api_quota_state"))

    def test_handler_revokes_old_claim_preserves_history_settings_and_queues_readonly(self):
        collection._enqueue_collection_jobs(["target-a"], db=self.db, priority=90)
        job = self.db.select("collection_job")[0]
        self.db.update("collection_job", {"status": "leased", "lease_owner": "worker-old", "fencing_token": 4},
                       where={"id": job["id"]})
        context = CollectionContext(300, generation=collection_lifecycle.generation())
        context.authorization_identity = self.identity
        context.database = self.db
        context.job_claim = {**job, "lease_owner": "worker-old", "fencing_token": 4}
        collection_lifecycle.register(context)
        self.addCleanup(lambda: collection_lifecycle.release(context))
        current = self.rotate()
        notified = Mock()
        result = collection.handle_authorization_change(self.identity, current, db=self.db, notify_catalog=notified)
        self.assertEqual(1, result["cancelled_batches"])
        self.assertTrue(context._cancel.is_set())
        self.assertEqual(5, self.db.select("collection_job")[0]["fencing_token"])
        target = self.db.select_one("promotion_target", where={"target_uid": "target-a"})
        self.assertEqual(1, target["enabled"])
        self.assertEqual("2026-08-30 12:00:00", target["last_sync_at"])
        self.assertEqual(0, target["monitor_eligible"])
        cap = json.loads(target["capability_json"])
        self.assertNotIn("retarget_supported", cap)
        self.assertEqual("succeeded", cap["material_backfill_state"]["2026-08-29"]["status"])
        notified.assert_called_once_with()

    def test_superseded_callback_never_revokes_new_context_or_clears_new_state(self):
        current = self.rotate()
        context = CollectionContext(300, generation=collection_lifecycle.generation())
        context.authorization_identity = current
        collection_lifecycle.register(context)
        self.addCleanup(lambda: collection_lifecycle.release(context))
        result = collection.handle_authorization_change({}, self.identity, db=self.db, notify_catalog=Mock())
        self.assertEqual("superseded", result["status"])
        self.assertFalse(context._cancel.is_set())
        self.assertEqual("verified", self.db.select("promotion_target")[0]["verification_state"])

    def test_credentials_save_does_not_schedule_network_before_oauth(self):
        current = self.rotate()
        notified = Mock()
        collection.handle_authorization_change(self.identity, current, event="credentials_saved", db=self.db, notify_catalog=notified)
        notified.assert_not_called()

    def test_clear_detaches_directory_revokes_unsent_work_and_preserves_history(self):
        from services.qianchuan_accounts import list_qianchuan_accounts, save_qianchuan_account_settings
        self.db.insert_or_update("qianchuan_account", {
            "account_uid": "account-other", "owner_username": "owner-b", "aavid": "2002",
            "directory_selected": 1, "enabled": 1,
        }, unique_fields=["account_uid"])
        collection._enqueue_collection_jobs(["target-a"], db=self.db, priority=90)
        self.db.insert_or_update("operation_log_sync_window", {
            "window_uid": "old-log", "owner_username": self.owner, "account_uid": "account-a", "aavid": "2001",
            "window_start": "2026-08-30 00:00:00", "window_end": "2026-08-31 00:00:00",
            "status": "running", "lease_owner": "old-worker", "fencing_token": 7,
        }, unique_fields=["window_uid"])
        for status in ("pending", "approved_queued", "claimed", "executing", "verifying", "succeeded"):
            self.db.insert_or_update("local_retarget_task", {
                "task_uid": status, "account_username": self.owner, "qianchuan_account_uid": "account-a",
                "status": status, "active_dedupe_key": status, "action_nonce": status,
                "payload_json": "{}", "expires_at": "2030-01-01 00:00:00", "claim_token": "old-claim",
                "fencing_token": 3,
            }, unique_fields=["task_uid"])
        self.db.insert_or_update("execution_reconciliation", {
            "reconciliation_uid": "submitted", "account_username": self.owner,
            "task_uid": "executing:group:1", "idempotency_key": "sent-once", "status": "submitted",
        }, unique_fields=["reconciliation_uid"])
        self.db.insert_or_update("feishu_outbox", {
            "outbox_uid": "unsent", "account_username": self.owner, "task_uid": "pending",
            "operation": "send", "status": "queued",
        }, unique_fields=["outbox_uid"])
        tokens.clear_api_configuration()
        current = tokens.get_authorization_identity()
        result = collection.handle_authorization_change(self.identity, current, event="disconnected", db=self.db)
        self.assertEqual(1, result["accounts_detached"])
        self.assertEqual(3, result["cards_cancelled"])
        self.assertEqual([], list_qianchuan_accounts(owner_username=self.owner, db=self.db))
        self.assertEqual(1, self.db.select_one("qianchuan_account", where={"account_uid": "account-other"})["enabled"])
        account = self.db.select_one("qianchuan_account", where={"account_uid": "account-a"})
        self.assertEqual((0, 0, "removed"), (account["directory_selected"], account["enabled"], account["last_status"]))
        target = self.db.select_one("promotion_target", where={"target_uid": "target-a"})
        self.assertEqual((0, "disabled"), (target["enabled"], target["capacity_state"]))
        self.assertEqual("2026-08-30 12:00:00", target["last_sync_at"])
        self.assertEqual("succeeded", json.loads(target["capability_json"])["material_backfill_state"]["2026-08-29"]["status"])
        self.assertEqual("cancelled", self.db.select("collection_job")[0]["status"])
        window = self.db.select("operation_log_sync_window")[0]
        self.assertEqual(("cancelled", None, 8), (window["status"], window["lease_owner"], window["fencing_token"]))
        for status in ("pending", "approved_queued", "claimed"):
            row = self.db.select_one("local_retarget_task", where={"task_uid": status})
            self.assertEqual(("cancelled", None, None, 4),
                (row["status"], row["active_dedupe_key"], row["claim_token"], row["fencing_token"]))
        for status in ("executing", "verifying", "succeeded"):
            self.assertEqual(status, self.db.select_one("local_retarget_task", where={"task_uid": status})["status"])
        self.assertEqual("submitted", self.db.select("execution_reconciliation")[0]["status"])
        self.assertEqual("cancelled", self.db.select("feishu_outbox")[0]["status"])
        with self.assertRaisesRegex(ValueError, "已移除"):
            save_qianchuan_account_settings("2001", {"enabled": True}, owner_username=self.owner, db=self.db)

    def test_app_switch_preserves_selection_made_under_new_grant_and_blocks_old_picker(self):
        from services.qianchuan_accounts import list_qianchuan_accounts
        current = self.rotate("100002")
        self.db.insert_or_update("qianchuan_account", {
            "account_uid": "fresh-account", "owner_username": self.owner, "aavid": "2003",
            "directory_selected": 1, "enabled": 1,
            "selection_authorization_json": json.dumps(current),
        }, unique_fields=["account_uid"])
        self.db.insert_or_update("promotion_target", {
            "target_uid": "fresh-target", "account_uid": "fresh-account", "aadvid": "2003", "ad_id": "3003",
            "promotion_scene": "live", "plan_system": "chengfang", "verification_state": "verified",
            "enabled": 1, "monitor_eligible": 1,
        }, unique_fields=["target_uid"])
        result = collection.handle_authorization_change(self.identity, current, event="credentials_saved", db=self.db)
        self.assertEqual(1, result["accounts_detached"])
        self.assertEqual(["2003"], [r["aavid"] for r in list_qianchuan_accounts(owner_username=self.owner, db=self.db)])
        self.assertEqual("verified", self.db.select_one("promotion_target", where={"target_uid": "fresh-target"})["verification_state"])
        service = Mock()
        def old_picker():
            self.rotate("100003")
            return [{"advertiser_id": "2001"}], {"complete": True}
        service.list_business_accounts.side_effect = old_picker
        with patch.object(catalog, "get_official_api_service", return_value=service), \
             patch.object(catalog, "ensure_qianchuan_account") as ensure:
            with self.assertRaises(tokens.AuthorizationContextChanged):
                catalog.add_authorized_account("2001")
        ensure.assert_not_called()

    def test_same_app_new_grant_retains_current_account_selection(self):
        current = self.rotate()
        result = collection.handle_authorization_change(self.identity, current, db=self.db, notify_catalog=Mock())
        self.assertEqual(0, result["accounts_detached"])
        self.assertEqual(1, self.db.select("qianchuan_account")[0]["directory_selected"])
        self.assertEqual(1, self.db.select("promotion_target")[0]["enabled"])

    def test_clear_again_repairs_previously_failed_detachment(self):
        from services.qianchuan_open_api import configuration
        tokens.clear_api_configuration()
        with patch.object(configuration, "get_official_api_service", return_value=Mock()), \
             patch.object(collection, "SQLiteStore", return_value=self.db):
            result = configuration.disconnect_configuration()
        self.assertTrue(result["success"])
        self.assertEqual(0, self.db.select("qianchuan_account")[0]["directory_selected"])

    def test_existing_directory_repair_requires_complete_current_authorized_list(self):
        from services.qianchuan_accounts import list_qianchuan_accounts
        self.db.insert_or_update("qianchuan_account", {
            "account_uid": "fresh-account", "owner_username": self.owner, "aavid": "2003",
            "directory_selected": 1, "enabled": 1,
        }, unique_fields=["account_uid"])
        accounts = [{"advertiser_id": "2003"}]
        result = state.reconcile_selected_authorized_accounts(self.identity, accounts, {"complete": False}, db=self.db)
        self.assertEqual(0, result["accounts_detached"])
        self.assertEqual(2, len(list_qianchuan_accounts(owner_username=self.owner, db=self.db)))
        result = state.reconcile_selected_authorized_accounts(self.identity, accounts, {"complete": True}, db=self.db)
        self.assertEqual(1, result["accounts_detached"])
        self.assertEqual(["2003"], [r["aavid"] for r in list_qianchuan_accounts(owner_username=self.owner, db=self.db)])

    def test_explicit_readd_records_current_grant_but_discovery_cannot_resurrect_removed_account(self):
        from services.qianchuan_accounts import ensure_qianchuan_account, make_account_uid
        # Use a real account UID, as production always does.
        uid = make_account_uid("2001", self.owner)
        self.db.update("qianchuan_account", {"account_uid": uid}, where={"aavid": "2001"})
        self.db.update("promotion_target", {"account_uid": uid}, where={"target_uid": "target-a"})
        current = self.rotate("100002")
        collection.handle_authorization_change(self.identity, current, event="credentials_saved", db=self.db)
        account = ensure_qianchuan_account("2001", owner_username=self.owner, directory_selected=True, seen=True, db=self.db)
        self.assertFalse(account["directory_selected"])
        account = ensure_qianchuan_account("2001", owner_username=self.owner, directory_selected=True, seen=True,
            allow_reactivate_removed=True, selection_authorization=current, db=self.db)
        self.assertTrue(account["directory_selected"])
        self.assertEqual(current, json.loads(account["selection_authorization_json"]))
        self.assertEqual(0, self.db.select("promotion_target")[0]["enabled"])

    def test_new_generation_auth_error_arriving_before_notification_is_not_cleared(self):
        current = self.rotate()
        collection._set_account_backoff(self.key(), 120, db=self.db, reason="permission")
        collection.handle_authorization_change(self.identity, current, db=self.db, notify_catalog=Mock())
        self.assertEqual("permission", collection._account_backoff_state(self.key(), db=self.db)["reason"])
        self.assertEqual(1, len(self.db.select("api_quota_state")))

    def test_normal_configuration_save_notifies_handler_without_warning(self):
        from services.qianchuan_open_api import configuration
        with patch.object(configuration, "get_official_api_service", return_value=Mock()), \
             patch.object(collection, "SQLiteStore", return_value=self.db):
            result = configuration.save_configuration("100001", "test-only-secret-b")
        self.assertTrue(result["success"])
        self.assertNotIn("authorization_notification_warning", result)
        self.assertEqual("authorization_required", self.db.select("promotion_target")[0]["last_status"])

    def test_catalog_timeout_can_finish_but_does_not_inherit_parent_budget(self):
        from services.qianchuan_open_api.collection_context import use_collection_context, current_collection_context
        captured = []
        def expired(*args, **kwargs):
            context = current_collection_context()
            captured.append(context.remaining_seconds())
            context.deadline = 0
            context.check_active()
        with use_collection_context(CollectionContext(0)), \
             patch.object(catalog, "_run_catalog_sync", side_effect=expired), \
             patch.object(catalog, "finalize_catalog_sync", return_value={"status": "failed"}) as finish:
            result = catalog.run_catalog_sync(db=self.db)
        self.assertGreater(captured[0], 290)
        self.assertEqual("failed", result["status"])
        finish.assert_called_once()

    def test_old_catalog_response_cannot_restore_eligibility_after_new_grant(self):
        service = Mock()
        service.list_business_accounts.return_value = ([{"advertiser_id": "2001"}], {"complete": True})
        def response(*args, **kwargs):
            current = self.rotate()
            collection.handle_authorization_change(self.identity, current, db=self.db, notify_catalog=Mock())
            return ([{"ad_id": "3001", "promotion_scene": "product", "plan_system": "global"}], {"complete": True})
        service.list_all_plans.side_effect = response
        account = self.db.select("qianchuan_account")[0]
        with patch.object(catalog, "get_official_api_service", return_value=service), \
             patch.object(catalog, "list_qianchuan_accounts", return_value=[account]), \
             patch.object(catalog, "mark_catalog_sync_progress"), \
             patch.object(catalog, "ensure_qianchuan_account"), \
             patch.object(catalog, "upsert_promotion_target") as upsert, \
             patch.object(catalog, "finalize_catalog_sync") as finish:
            result = catalog.run_catalog_sync(db=self.db)
        self.assertEqual("authorization_superseded", result["status"])
        upsert.assert_not_called()
        finish.assert_not_called()
        self.assertEqual(0, self.db.select("promotion_target")[0]["monitor_eligible"])


if __name__ == "__main__":
    unittest.main()
