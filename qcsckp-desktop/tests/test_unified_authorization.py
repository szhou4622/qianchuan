"""Deterministic authorization interleavings, isolated fake credentials only."""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services.qianchuan_open_api import token_provider as tokens
from services.qianchuan_open_api import configuration, runtime_settings
from services.qianchuan_open_api.errors import ApiTokenError, OfficialApiNotConfigured
from services.qianchuan_open_api.service import QianchuanOfficialApiService


class AuthorizationConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.owner = "owner-a"
        self.root = Path(self.temp.name)
        for item in (
            patch.object(tokens, "DATA_DIR", str(self.root)),
            patch.object(tokens, "QIANCHUAN_API_TOKEN_FILE", str(self.root / "absent-legacy.json")),
            patch.object(tokens, "_current_owner", side_effect=lambda: self.owner),
            patch.object(tokens, "_protect", side_effect=lambda data: data),
            patch.object(tokens, "_unprotect", side_effect=lambda data: data),
            patch.object(configuration, "_notify_authorization_changed", return_value=""),
            patch.object(configuration, "persist_official_api_runtime", return_value={}),
        ):
            item.start()
            self.addCleanup(item.stop)

    def seed(self, *, expired=True):
        path = tokens.resolve_token_path()
        tokens.save_token_bundle(tokens.AccessTokenBundle(
            access_token="test-old-access", refresh_token="test-old-refresh", app_id="100001",
            app_secret="test-secret-a", expires_at=time.time() + (-10 if expired else 3600),
        ), path)
        return path

    def background(self, action):
        outcomes = []
        def run():
            try:
                outcomes.append(action())
            except BaseException as exc:
                outcomes.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        return thread, outcomes

    def grant(self, path):
        auth = tokens.begin_api_authorization(path)
        body = {"code": 0, "data": {"access_token": "test-new-access", "refresh_token": "test-new-refresh", "expires_in": 3600}}
        with patch.object(tokens, "urlopen", return_value=io.BytesIO(json.dumps(body).encode())):
            return tokens.exchange_authorization_code("auth_code=test-code&state=" + auth["state"], path)

    def test_late_refresh_cannot_restore_old_app_or_secret(self):
        path = self.seed()
        provider = tokens.DpapiTokenProvider(path)
        entered, release = threading.Event(), threading.Event()
        def refresh(old):
            entered.set()
            self.assertTrue(release.wait(5))
            return replace(old, access_token="test-refreshed-old-app", expires_at=time.time() + 3600)
        with patch.object(provider, "_refresh", side_effect=refresh):
            thread, result = self.background(provider.get_token)
            self.assertTrue(entered.wait(5))
            changed = tokens.save_api_credentials("200002", "test-secret-b", path)
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result[0], tokens.AuthorizationContextChanged)
        saved = tokens._load_saved_bundle(path)
        self.assertEqual("200002", saved.app_id)
        self.assertEqual("test-secret-b", saved.app_secret)
        self.assertEqual("", saved.access_token)
        self.assertEqual(changed["authorization_identity"]["auth_generation"], saved.auth_generation)

    def test_old_refresh_error_does_not_poison_completed_new_grant(self):
        for failure in (False, True):
            with self.subTest(old_refresh_failed=failure):
                path = self.seed()
                provider = tokens.DpapiTokenProvider(path)
                entered, release = threading.Event(), threading.Event()
                def refresh(old):
                    entered.set()
                    self.assertTrue(release.wait(5))
                    if failure:
                        raise ApiTokenError("test old generation revoked", code="41013")
                    return replace(old, access_token="test-stale-refresh", expires_at=time.time() + 3600)
                with patch.object(provider, "_refresh", side_effect=refresh):
                    thread, result = self.background(provider.get_token)
                    self.assertTrue(entered.wait(5))
                    granted = self.grant(path)
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual("test-new-access", result[0].access_token)
                self.assertEqual(granted.token_revision, tokens._load_saved_bundle(path).token_revision)

    def test_refresh_cannot_erase_pending_oauth_state(self):
        path = self.seed()
        provider = tokens.DpapiTokenProvider(path)
        entered, release = threading.Event(), threading.Event()
        def refresh(old):
            entered.set()
            self.assertTrue(release.wait(5))
            return replace(old, access_token="test-refreshed", expires_at=time.time() + 3600)
        with patch.object(provider, "_refresh", side_effect=refresh):
            thread, result = self.background(provider.get_token)
            self.assertTrue(entered.wait(5))
            auth = tokens.begin_api_authorization(path)
            release.set()
            thread.join(5)
        self.assertIsInstance(result[0], tokens.AuthorizationContextChanged)
        self.assertEqual(auth["state"], tokens._load_saved_bundle(path).oauth_state)
        with patch.object(provider, "_refresh", side_effect=AssertionError("pending OAuth must not refresh")):
            with self.assertRaises(ApiTokenError):
                provider.get_token()

    def test_identical_credentials_save_preserves_pending_authorization(self):
        path = self.seed(expired=False)
        auth = tokens.begin_api_authorization(path)
        before = tokens._load_saved_bundle(path)
        saved = tokens.save_api_credentials("100001", "", path)
        after = tokens._load_saved_bundle(path)
        self.assertFalse(saved["authorization_changed"])
        self.assertEqual(auth["state"], after.oauth_state)
        self.assertEqual(before.token_revision, after.token_revision)

    def test_owner_switch_during_refresh_never_redirects_write(self):
        path_a = self.seed()
        provider = tokens.DpapiTokenProvider()
        entered, release = threading.Event(), threading.Event()
        def refresh(old):
            entered.set()
            self.assertTrue(release.wait(5))
            return replace(old, access_token="test-a-refreshed", expires_at=time.time() + 3600)
        with patch.object(provider, "_refresh", side_effect=refresh):
            thread, result = self.background(provider.get_token)
            self.assertTrue(entered.wait(5))
            self.owner = "owner-b"
            path_b = self.seed(expired=False)
            previous_b = Path(path_b).read_bytes()
            release.set()
            thread.join(5)
        self.assertIsInstance(result[0], tokens.AuthorizationContextChanged)
        self.assertEqual(previous_b, Path(path_b).read_bytes())
        self.assertEqual("owner-a", tokens._load_saved_bundle(path_a).owner_username)
        self.assertEqual("test-a-refreshed", tokens._load_saved_bundle(path_a).access_token)

    def test_multiple_providers_coalesce_refresh_for_one_path(self):
        path = self.seed()
        one, two = tokens.DpapiTokenProvider(path), tokens.DpapiTokenProvider(path)
        entered, observed_two, release = threading.Event(), threading.Event(), threading.Event()
        original_load = tokens._load_saved_bundle
        def load(target):
            result = original_load(target)
            if threading.current_thread().name == "second-provider":
                observed_two.set()
            return result
        def refresh(old):
            entered.set()
            self.assertTrue(release.wait(5))
            return replace(old, access_token="test-refreshed-once", expires_at=time.time() + 3600)
        with patch.object(tokens, "_load_saved_bundle", side_effect=load), \
             patch.object(tokens.DpapiTokenProvider, "_refresh", side_effect=refresh) as refreshed:
            first, first_result = self.background(lambda: one.get_token(force_refresh=True))
            self.assertTrue(entered.wait(5))
            result = []
            second = threading.Thread(name="second-provider", target=lambda: result.append(two.get_token(force_refresh=True)))
            second.start()
            self.assertTrue(observed_two.wait(5))
            release.set()
            first.join(5)
            second.join(5)
            self.assertFalse(first.is_alive() or second.is_alive())
            refreshed.assert_called_once()
        self.assertEqual(first_result[0].token_revision, result[0].token_revision)

    def test_late_oauth_response_cannot_overwrite_changed_credentials(self):
        path = self.seed()
        auth = tokens.begin_api_authorization(path)
        entered, release = threading.Event(), threading.Event()
        def request(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return io.BytesIO(json.dumps({"code": 0, "data": {"access_token": "test-outdated-grant", "refresh_token": "test-r", "expires_in": 3600}}).encode())
        with patch.object(tokens, "urlopen", side_effect=request):
            thread, result = self.background(lambda: tokens.exchange_authorization_code("auth_code=test-code&state=" + auth["state"], path))
            self.assertTrue(entered.wait(5))
            tokens.save_api_credentials("200002", "test-secret-b", path)
            release.set()
            thread.join(5)
        self.assertIsInstance(result[0], tokens.AuthorizationContextChanged)
        self.assertEqual("200002", tokens._load_saved_bundle(path).app_id)

    def test_failed_atomic_save_keeps_previous_file_and_notifies_nobody(self):
        path = self.seed(expired=False)
        original = Path(path).read_bytes()
        with patch.object(tokens.os, "replace", side_effect=OSError("test replace failed")), \
             patch.object(configuration, "_notify_authorization_changed") as notify:
            result = configuration.save_configuration("200002", "test-secret-b")
        self.assertFalse(result["success"])
        self.assertEqual(original, Path(path).read_bytes())
        self.assertEqual([], list(Path(path).parent.glob("*.tmp")))
        notify.assert_not_called()

    def test_explicit_disconnect_can_remove_unreadable_old_configuration(self):
        path = self.seed()
        with patch.object(tokens, "_unprotect", side_effect=OSError("test other Windows user")):
            result = configuration.disconnect_configuration()
        self.assertTrue(result["success"])
        self.assertFalse(Path(path).exists())
        self.assertEqual("unconfigured", result["authorization_identity"]["auth_generation"])

    def test_legacy_schema_identity_is_stable_and_upgrades_only_on_write(self):
        path = Path(tokens.resolve_token_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy = {"access_token": "test-old", "refresh_token": "test-refresh", "app_id": "100001", "app_secret": "test-secret-a", "expires_at": time.time() + 3600}
        path.write_text(json.dumps({"format": "qcsckp-oceanengine-token-dpapi-v1", "ciphertext": base64.b64encode(json.dumps(legacy).encode()).decode()}), encoding="utf-8")
        original = path.read_bytes()
        first = tokens.get_authorization_identity(str(path))
        self.assertEqual(first, tokens.get_authorization_identity(str(path)))
        self.assertEqual(original, path.read_bytes())
        result = tokens.save_api_credentials("100001", "", str(path))
        self.assertFalse(result["authorization_changed"])
        self.assertEqual(first, result["authorization_identity"])
        self.assertEqual("test-old", tokens._load_saved_bundle(str(path)).access_token)

    def test_disconnect_never_resurrects_legacy_rollback_token(self):
        legacy = self.root / "legacy.json"
        data = {"access_token": "test-legacy", "app_id": "100001", "app_secret": "test-secret-a", "expires_at": time.time() + 3600}
        legacy.write_text(json.dumps({"ciphertext": base64.b64encode(json.dumps(data).encode()).decode()}))
        with patch.object(tokens, "QIANCHUAN_API_TOKEN_FILE", str(legacy)):
            path = tokens.resolve_token_path()
            self.assertTrue(Path(path).exists())
            tokens.clear_api_configuration()
            self.assertFalse(Path(tokens.resolve_token_path()).exists())
            with self.assertRaises(OfficialApiNotConfigured):
                tokens.DpapiTokenProvider().get_token()
        self.assertTrue(legacy.exists())

    def test_captured_owner_cannot_be_reinterpreted_between_configuration_steps(self):
        path = self.seed(expired=False)
        before = Path(path).read_bytes()
        real_save = tokens.save_api_credentials
        def switched_save(*args, **kwargs):
            self.owner = "owner-b"
            return real_save(*args, **kwargs)
        with patch.object(configuration, "save_api_credentials", side_effect=switched_save):
            result = configuration.start_authorization("200002", "test-secret-b")
        self.assertFalse(result["success"])
        self.assertEqual(before, Path(path).read_bytes())

    def test_real_refresh_preserves_auth_generation_but_changes_revision(self):
        path = self.seed()
        before = tokens._load_saved_bundle(path)
        response = {"code": 0, "data": {"access_token": "test-refreshed", "refresh_token": "test-rotated", "expires_in": 3600}}
        with patch.object(tokens, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            after = tokens.DpapiTokenProvider(path).get_token()
        self.assertEqual(before.auth_generation, after.auth_generation)
        self.assertNotEqual(before.token_revision, after.token_revision)
        with patch.object(tokens.DpapiTokenProvider, "_refresh", side_effect=AssertionError("must reuse new revision")):
            reused = tokens.DpapiTokenProvider(path).get_token(force_refresh=True, rejected_token_revision=before.token_revision)
        self.assertEqual(after.token_revision, reused.token_revision)

    def test_success_callback_runs_after_credential_lock_release(self):
        path = self.seed(expired=False)
        def notify(*args, **kwargs):
            acquired = threading.Event()
            def check():
                with tokens.credential_file_guard(path):
                    acquired.set()
            thread = threading.Thread(target=check)
            thread.start()
            self.assertTrue(acquired.wait(2))
            thread.join(2)
            return ""
        with patch.object(configuration, "_notify_authorization_changed", side_effect=notify):
            result = configuration.save_configuration("200002", "test-secret-b")
        self.assertTrue(result["success"])

    def test_identity_guard_blocks_old_generation_and_supports_nested_reads(self):
        path = self.seed(expired=False)
        identity = tokens.get_authorization_identity(path)
        with tokens.authorization_identity_guard(identity, path):
            self.assertTrue(tokens.authorization_identity_is_current(identity, path))
        tokens.save_api_credentials("200002", "test-secret-b", path)
        self.assertFalse(tokens.authorization_identity_is_current(identity, path))

    def test_public_writer_rejects_stale_snapshot_and_cannot_undo_disconnect(self):
        path = self.seed(expired=False)
        old = tokens._load_saved_bundle(path)
        tokens.save_api_credentials("200002", "test-secret-b", path)
        with self.assertRaises(tokens.AuthorizationContextChanged):
            tokens.save_token_bundle(old, path)
        tokens.clear_api_configuration(path)
        with self.assertRaises(tokens.AuthorizationContextChanged):
            tokens.save_token_bundle(old, path)
        self.assertFalse(Path(path).exists())

    def test_finish_oauth_validates_new_token_with_forced_account_request(self):
        path = self.seed()
        auth = tokens.begin_api_authorization(path)
        service = Mock()
        service.list_business_accounts.return_value = ([{"advertiser_id": "123"}], {"complete": True})
        body = {"code": 0, "data": {"access_token": "test-new", "refresh_token": "test-new-r", "expires_in": 3600}}
        with patch.object(tokens, "urlopen", return_value=io.BytesIO(json.dumps(body).encode())), \
             patch.object(configuration, "get_official_api_service", return_value=service), \
             patch.object(configuration, "_notify_authorization_changed", return_value="") as notify:
            result = configuration.finish_authorization("auth_code=test-code&state=" + auth["state"])
        self.assertTrue(result["success"])
        self.assertTrue(result["account_check_success"])
        service.list_business_accounts.assert_called_once_with(force_refresh=True)
        notify.assert_called_once()
        public = json.dumps(result)
        self.assertNotIn("test-secret", public)
        self.assertNotIn("test-new-r", public)

    def test_packaged_environment_never_uses_late_injected_token(self):
        with patch.dict(os.environ, {"QCSCKP_OE_ACCESS_TOKEN": "test-hidden", "QCSCKP_OE_EXPIRES_AT": str(time.time() + 3600)}), \
             patch("release_configuration.packaged_policy_active", return_value=True):
            with self.assertRaises(OfficialApiNotConfigured):
                tokens.DefaultTokenProvider().get_token()

    def test_runtime_profiles_merge_atomically_and_do_not_apply_another_owner(self):
        target = str(self.root / "runtime.json")
        with patch.object(runtime_settings, "QIANCHUAN_RUNTIME_SETTINGS_FILE", target), \
             patch.object(runtime_settings, "_current_owner", return_value="owner-b"), \
             patch("services.qianchuan_open_api.runtime.apply_live_write_permission") as apply:
            threads = [threading.Thread(target=runtime_settings.persist_official_api_runtime,
                        kwargs={"owner_username": owner, "allow_live_writes": allowed, "apply_runtime": False})
                       for owner, allowed in (("owner-a", True), ("owner-b", False))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
            runtime_settings.persist_official_api_runtime(owner_username="owner-a", allow_live_writes=True)
            apply.assert_not_called()
        data = json.loads(Path(target).read_text())
        self.assertTrue(data["profiles"]["owner-a"]["allow_live_api_writes"])
        self.assertFalse(data["profiles"]["owner-b"]["allow_live_api_writes"])


class AuthorizationCacheTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"owner_username": "owner-a", "app_id": "100001", "auth_generation": "g1"}
        provider = SimpleNamespace(get_identity=lambda: dict(self.identity))
        self.service = QianchuanOfficialApiService(SimpleNamespace(token_provider=provider), allow_writes=False)
        self.accounts = [{"advertiser_id": "101", "advertiser_name": "test-name", "role": "ADVERTISER"}]

    def test_cache_isolated_by_owner_app_and_authorization_generation(self):
        with patch.object(self.service, "list_authorized_accounts", side_effect=lambda: list(self.accounts)) as fetch:
            self.service.list_business_accounts()
            self.service.list_business_accounts()
            self.assertEqual(1, fetch.call_count)
            for field in ("owner_username", "app_id", "auth_generation"):
                self.identity[field] += "-changed"
                self.service.list_business_accounts()
            self.assertEqual(4, fetch.call_count)

    def test_clear_during_old_request_prevents_late_cache_repopulation(self):
        entered, release = threading.Event(), threading.Event()
        result = []
        def fetch():
            entered.set()
            self.assertTrue(release.wait(5))
            return self.accounts
        def load():
            try:
                result.append(self.service.list_business_accounts())
            except Exception as exc:
                result.append(exc)
        with patch.object(self.service, "list_authorized_accounts", side_effect=fetch):
            thread = threading.Thread(target=load)
            thread.start()
            self.assertTrue(entered.wait(5))
            self.service.clear_business_account_cache()
            release.set()
            thread.join(5)
        self.assertIsInstance(result[0], tokens.AuthorizationContextChanged)
        self.assertIsNone(self.service._business_account_cache)
