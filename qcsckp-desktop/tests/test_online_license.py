import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from config import LICENSE_APP_NAME, LICENSE_PROTOCOL_VERSION, SOFTWARE_CHINESE_NAME
from services.license_client import (
    LicenseHttpClient,
    LicenseNetworkError,
    LicenseServiceError,
)
from services.license_manager import LicenseManager
from services.license_storage import LicenseSecureStore
from services.device_identity import generate_device_code, validate_device_code


ROOT = Path(__file__).resolve().parents[1]
EXPIRES_AT = "2027-08-20T12:00:00+08:00"
TEST_DEVICE_CODE = generate_device_code(
    {
        "BOARD_UUID": "11111111-2222-3333-4444-555555555555",
        "WINDOWS_MACHINE_GUID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }
)


def license_payload(license_type="monthly", **overrides):
    duration_by_type = {
        "weekly": 7,
        "time_7d": 7,
        "monthly": 30,
        "time_30d": 30,
        "yearly": 365,
        "time_365d": 365,
        "permanent": 0,
        "lifetime": 0,
    }
    duration_days = duration_by_type.get(license_type, 30)
    is_permanent = license_type in {"permanent", "lifetime"}
    payload = {
        "app_name": LICENSE_APP_NAME,
        "code_id": "code-test-001",
        "device_session": "session-sensitive-value",
        "device_credential": "credential-sensitive-value",
        "binding_status": "active",
        "license_status": "active",
        "license_type": license_type,
        "duration_days": duration_days,
        "activated_at": "2026-08-20T12:00:00+08:00",
        "expires_at": "" if is_permanent else EXPIRES_AT,
        "remaining_days": None if is_permanent else duration_days,
        "transfer_count": 0,
    }
    payload.update(overrides)
    return payload


def status_payload(license_type="monthly", **overrides):
    payload = license_payload(license_type, **overrides)
    payload.pop("device_session", None)
    payload.pop("device_credential", None)
    return payload


class FakeStore:
    def __init__(self, machine="machine_stable_12345678", device_code=TEST_DEVICE_CODE):
        self.credentials = None
        self.activation_context = {}
        self.license_snapshot = {}
        self.metadata = {}
        self.machine = machine
        self.device_code = device_code
        self.clear_count = 0

    def load_credentials(self):
        return dict(self.credentials) if self.credentials else None

    def save_credentials(self, payload):
        self.credentials = {
            "device_session": payload["device_session"],
            "device_credential": payload["device_credential"],
        }
        for name in ("activation_code", "code_id", "machine_code"):
            if payload.get(name):
                self.activation_context[name] = str(payload[name])

    def clear_credentials(self):
        self.credentials = None
        self.clear_count += 1

    def load_activation_context(self):
        return dict(self.activation_context)

    def load_license_snapshot(self):
        return dict(self.license_snapshot)

    def save_license_snapshot(self, payload):
        self.license_snapshot = dict(payload)

    def get_or_create_machine_code(self):
        return self.machine

    def get_or_create_device_code(self):
        return self.device_code

    def load_metadata(self):
        return dict(self.metadata)

    def save_metadata(self, payload):
        self.metadata = dict(payload)


class FakeClient:
    def __init__(self, *, activation=None, status=None, unbind=None):
        self.activation = activation or license_payload()
        self.status = status or status_payload()
        self.unbind_response = unbind or {
            "binding_status": "unbound",
            "message": "当前设备已解绑",
        }
        self.activate_calls = []
        self.activate_options = []
        self.activate_credentials = []
        self.status_calls = []
        self.unbind_calls = []

    def activate(
        self,
        code,
        machine,
        *,
        current_code_id="",
        credential_refresh=False,
        credentials=None,
    ):
        self.activate_calls.append((code, machine))
        self.activate_options.append(
            {
                "current_code_id": current_code_id,
                "credential_refresh": credential_refresh,
            }
        )
        self.activate_credentials.append(dict(credentials) if credentials else None)
        if isinstance(self.activation, Exception):
            raise self.activation
        return dict(self.activation)

    def device_status(self, credentials, *, machine_code, code_id):
        self.status_calls.append(
            {
                "credentials": dict(credentials),
                "machine_code": machine_code,
                "code_id": code_id,
            }
        )
        if isinstance(self.status, Exception):
            raise self.status
        return dict(self.status)

    def unbind(self, credentials, *, machine_code, code_id):
        self.unbind_calls.append(
            {
                "credentials": dict(credentials),
                "machine_code": machine_code,
                "code_id": code_id,
            }
        )
        if isinstance(self.unbind_response, Exception):
            raise self.unbind_response
        return dict(self.unbind_response)


class FakeHttpResponse:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


class OnlineLicenseManagerTests(unittest.TestCase):
    def test_unactivated_device_cannot_enter(self):
        manager = LicenseManager(client=FakeClient(), store=FakeStore())
        state = manager.startup_check()
        self.assertFalse(state["authorized"])
        self.assertTrue(state["needs_activation"])
        self.assertFalse(manager.is_runtime_authorized())

    def test_plain_metadata_cannot_bypass_missing_secure_credentials(self):
        store = FakeStore()
        store.metadata = status_payload()
        manager = LicenseManager(client=FakeClient(), store=store)
        state = manager.startup_check()
        self.assertFalse(state["authorized"])
        self.assertEqual(0, len(manager.client.status_calls))

    def test_month_card_uses_server_timestamps_without_recalculation(self):
        store = FakeStore()
        client = FakeClient(activation=license_payload("monthly"))
        state = LicenseManager(client=client, store=store).activate("MONTH-TEST")
        self.assertTrue(state["authorized"])
        self.assertEqual("月卡", state["license"]["license_type_label"])
        self.assertEqual(EXPIRES_AT, state["license"]["expires_at"])
        self.assertEqual(30, state["license"]["duration_days"])
        self.assertEqual([("MONTH-TEST", store.device_code)], client.activate_calls)
        self.assertEqual("MONTH-TEST", store.activation_context["activation_code"])
        self.assertEqual("code-test-001", store.activation_context["code_id"])
        self.assertEqual(store.device_code, store.activation_context["machine_code"])
        public_json = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("session-sensitive-value", public_json)
        self.assertNotIn("credential-sensitive-value", public_json)
        self.assertNotIn("MONTH-TEST", public_json)

    def test_week_card_uses_server_seven_day_entitlement(self):
        state = LicenseManager(
            client=FakeClient(activation=license_payload("weekly")),
            store=FakeStore(),
        ).activate("WEEK-TEST")
        self.assertTrue(state["authorized"])
        self.assertEqual("周卡", state["license"]["license_type_label"])
        self.assertEqual(7, state["license"]["duration_days"])
        self.assertFalse(state["license"]["is_permanent"])

    def test_year_card_uses_server_timestamps_without_recalculation(self):
        state = LicenseManager(
            client=FakeClient(activation=license_payload("yearly")),
            store=FakeStore(),
        ).activate("YEAR-TEST")
        self.assertTrue(state["authorized"])
        self.assertEqual("年卡", state["license"]["license_type_label"])
        self.assertEqual(EXPIRES_AT, state["license"]["expires_at"])
        self.assertEqual(365, state["license"]["duration_days"])

    def test_permanent_card_allows_missing_expiry_fields(self):
        state = LicenseManager(
            client=FakeClient(activation=license_payload("permanent")),
            store=FakeStore(),
        ).activate("PERMANENT-TEST")
        self.assertTrue(state["authorized"])
        self.assertEqual("永久授权", state["license"]["license_type_label"])
        self.assertTrue(state["license"]["is_permanent"])
        self.assertEqual(0, state["license"]["duration_days"])
        self.assertEqual("", state["license"]["expires_at"])
        self.assertIsNone(state["license"]["remaining_days"])

    def test_server_standard_zero_day_entitlement_maps_to_permanent(self):
        payload = license_payload("standard")
        payload.update(
            {
                "duration_days": 0,
                "expires_at": "",
                "remaining_days": None,
            }
        )
        state = LicenseManager(
            client=FakeClient(activation=payload),
            store=FakeStore(),
        ).activate("SERVER-PERMANENT")
        self.assertTrue(state["authorized"])
        self.assertEqual("永久授权", state["license"]["license_type_label"])
        self.assertTrue(state["license"]["is_permanent"])

    def test_server_unlimited_type_maps_to_permanent_but_points_variant_does_not(self):
        allowed = license_payload("unlimited")
        allowed.update({"duration_days": 0, "expires_at": "", "remaining_days": None})
        state = LicenseManager(
            client=FakeClient(activation=allowed),
            store=FakeStore(),
        ).activate("SERVER-UNLIMITED")
        self.assertTrue(state["authorized"])
        self.assertEqual("永久授权", state["license"]["license_type_label"])

    def test_unknown_server_type_keeps_issued_credentials_without_authorizing(self):
        store = FakeStore()
        payload = license_payload("future_server_type")
        payload["machine_code"] = store.device_code
        manager = LicenseManager(
            client=FakeClient(activation=payload),
            store=store,
        )
        state = manager.activate("FUTURE-TYPE")
        self.assertFalse(state["authorized"])
        self.assertEqual("unsupported_license_type", state["code"])
        self.assertEqual(
            "session-sensitive-value",
            store.credentials["device_session"],
        )
        self.assertEqual("future_server_type", store.metadata["license_type"])
        self.assertFalse(manager.is_runtime_authorized())

    def test_server_time_duration_license_types_map_to_month_and_year(self):
        month = LicenseManager(
            client=FakeClient(activation=license_payload("time_30d", duration_days=30)),
            store=FakeStore(),
        ).activate("MONTH-TIME")
        year = LicenseManager(
            client=FakeClient(activation=license_payload("time_365d", duration_days=365)),
            store=FakeStore(),
        ).activate("YEAR-TIME")
        self.assertEqual("月卡", month["license"]["license_type_label"])
        self.assertEqual("年卡", year["license"]["license_type_label"])

    def test_restart_uses_device_status_and_never_needs_activation_code(self):
        store = FakeStore()
        first = LicenseManager(client=FakeClient(), store=store)
        self.assertTrue(first.activate("ONCE-ONLY")["authorized"])
        second_client = FakeClient(status=status_payload())
        second = LicenseManager(client=second_client, store=store)
        state = second.startup_check()
        self.assertTrue(state["authorized"])
        self.assertEqual(1, len(second_client.status_calls))
        self.assertEqual(EXPIRES_AT, state["license"]["expires_at"])

    def test_status_uses_server_issued_saved_static_card_fields(self):
        store = FakeStore()
        manager = LicenseManager(client=FakeClient(), store=store)
        self.assertTrue(manager.activate("ONCE")["authorized"])
        status_without_static_fields = status_payload()
        status_without_static_fields.pop("license_type")
        status_without_static_fields.pop("duration_days")
        restarted = LicenseManager(
            client=FakeClient(status=status_without_static_fields),
            store=store,
        ).startup_check()
        self.assertTrue(restarted["authorized"])
        self.assertEqual("月卡", restarted["license"]["license_type_label"])
        self.assertEqual(30, restarted["license"]["duration_days"])

    def test_plain_metadata_tampering_cannot_override_encrypted_snapshot(self):
        store = FakeStore()
        manager = LicenseManager(client=FakeClient(), store=store)
        self.assertTrue(manager.activate("MONTH-SNAPSHOT")["authorized"])
        store.metadata.update(
            {
                "license_type": "permanent",
                "license_type_label": "永久授权",
                "duration_days": 0,
                "expires_at": "",
                "is_permanent": True,
            }
        )
        response = status_payload("monthly")
        response.pop("license_type")
        response.pop("duration_days")
        restarted = LicenseManager(
            client=FakeClient(status=response),
            store=store,
        ).startup_check()
        self.assertTrue(restarted["authorized"])
        self.assertEqual("月卡", restarted["license"]["license_type_label"])
        self.assertFalse(restarted["license"]["is_permanent"])

    def test_matching_legacy_metadata_migrates_once_to_secure_snapshot(self):
        store = FakeStore()
        store.credentials = {
            "device_session": "session",
            "device_credential": "credential",
        }
        store.activation_context = {
            "activation_code": "LEGACY-CODE",
            "code_id": "code-test-001",
            "machine_code": store.machine,
        }
        store.metadata = {
            **status_payload("monthly"),
            "license_type_label": "月卡",
            "is_permanent": False,
        }
        response = status_payload("monthly", machine_code=store.machine)
        response.pop("license_type")
        response.pop("duration_days")
        state = LicenseManager(
            client=FakeClient(status=response),
            store=store,
        ).startup_check()
        self.assertTrue(state["authorized"])
        self.assertEqual("monthly", store.license_snapshot["license_type"])

    def test_legacy_binding_without_secret_context_migrates_when_server_identity_matches(self):
        store = FakeStore()
        store.credentials = {
            "device_session": "session",
            "device_credential": "credential",
        }
        store.metadata = {
            **status_payload("monthly"),
            "license_type_label": "月卡",
            "is_permanent": False,
        }
        state = LicenseManager(
            client=FakeClient(status=status_payload("monthly")),
            store=store,
        ).startup_check()
        self.assertTrue(state["authorized"])
        self.assertEqual("monthly", store.license_snapshot["license_type"])
        self.assertIsNotNone(store.credentials)

    def test_legacy_binding_with_mismatched_server_code_id_is_rejected(self):
        store = FakeStore()
        store.credentials = {
            "device_session": "session",
            "device_credential": "credential",
        }
        store.metadata = {
            **status_payload("monthly"),
            "license_type_label": "月卡",
            "is_permanent": False,
        }
        response = status_payload("monthly", code_id="different-code-id")
        state = LicenseManager(
            client=FakeClient(status=response),
            store=store,
        ).startup_check()
        self.assertFalse(state["authorized"])
        self.assertTrue(state["credential_refresh_required"])

    def test_legacy_binding_reactivation_sends_existing_device_credentials(self):
        store = FakeStore()
        store.credentials = {
            "device_session": "existing-session",
            "device_credential": "existing-credential",
        }
        store.metadata = {
            **status_payload("monthly"),
            "license_type_label": "月卡",
            "is_permanent": False,
        }
        client = FakeClient(activation=license_payload("monthly"))
        manager = LicenseManager(client=client, store=store)
        state = manager.activate("ORIGINAL-CODE")
        self.assertTrue(state["authorized"])
        self.assertEqual(
            {
                "device_session": "existing-session",
                "device_credential": "existing-credential",
            },
            client.activate_credentials[-1],
        )

    def test_duplicate_activation_after_success_is_not_resubmitted(self):
        client = FakeClient()
        manager = LicenseManager(client=client, store=FakeStore())
        self.assertTrue(manager.activate("FIRST-CLICK")["authorized"])
        self.assertTrue(manager.activate("DELAYED-SECOND-CLICK")["authorized"])
        self.assertEqual(1, len(client.activate_calls))

    def test_machine_code_stays_stable_across_manager_and_version_lifecycle(self):
        store = FakeStore(machine="machine_upgrade_stable")
        first_client = FakeClient()
        LicenseManager(client=first_client, store=store).activate("FIRST")
        second_client = FakeClient()
        LicenseManager(client=second_client, store=store).activate("SECOND")
        self.assertEqual(TEST_DEVICE_CODE, first_client.activate_calls[0][1])
        self.assertEqual(TEST_DEVICE_CODE, second_client.activate_calls[0][1])

    def test_active_code_on_other_device_keeps_exact_server_409_message(self):
        message = "该激活码已绑定其他设备，请先在原设备解绑"
        client = FakeClient(
            activation=LicenseServiceError(
                "already_bound",
                message,
                http_status=409,
            )
        )
        store = FakeStore(machine="machine_other")
        state = LicenseManager(client=client, store=store).activate("BOUND-CODE")
        self.assertFalse(state["authorized"])
        self.assertEqual(message, state["message"])
        self.assertIsNone(store.credentials)

    def test_unbind_clears_only_credentials_and_preserves_expiry(self):
        store = FakeStore()
        manager = LicenseManager(client=FakeClient(), store=store)
        self.assertTrue(manager.activate("MONTH")["authorized"])
        result = manager.unbind_current_device()
        self.assertTrue(result["success"])
        self.assertFalse(result["authorized"])
        self.assertIsNone(store.credentials)
        self.assertEqual(EXPIRES_AT, store.metadata["expires_at"])
        self.assertEqual("unbound", store.metadata["binding_status"])

    def test_new_machine_rebind_keeps_server_original_expiry(self):
        original_expiry = "2026-09-19T12:00:00+08:00"
        state = LicenseManager(
            client=FakeClient(
                activation=license_payload(
                    "monthly",
                    expires_at=original_expiry,
                    remaining_days=18,
                )
            ),
            store=FakeStore(machine="machine_new_computer"),
        ).activate("ORIGINAL-CODE")
        self.assertTrue(state["authorized"])
        self.assertEqual(original_expiry, state["license"]["expires_at"])
        self.assertEqual(18, state["license"]["remaining_days"])

    def test_rebound_reports_rebind_success_without_points_logic(self):
        state = LicenseManager(
            client=FakeClient(
                activation=license_payload(
                    "monthly",
                    action="rebound",
                    grant_score=0,
                    remaining_credits=37,
                    transfer_count=1,
                )
            ),
            store=FakeStore(machine="machine_new"),
        ).activate("ORIGINAL-CODE")
        self.assertTrue(state["authorized"])
        self.assertEqual("换机绑定成功", state["message"])
        self.assertNotIn("remaining_credits", state["license"])
        self.assertEqual(0, state["license"]["grant_score"])

    def test_rebound_must_not_grant_initial_entitlement_again(self):
        state = LicenseManager(
            client=FakeClient(
                activation=license_payload(
                    "monthly",
                    action="rebound",
                    grant_score=100,
                )
            ),
            store=FakeStore(machine="machine_new"),
        ).activate("ORIGINAL-CODE")
        self.assertFalse(state["authorized"])
        self.assertEqual("invalid_response", state["code"])

    def test_old_active_binding_without_credentials_requests_controlled_refresh(self):
        store = FakeStore()
        store.metadata = {
            "app_name": LICENSE_APP_NAME,
            "code_id": "code-test-001",
            "binding_status": "active",
        }
        store.activation_context = {
            "activation_code": "ORIGINAL-CODE",
            "code_id": "code-test-001",
            "machine_code": store.machine,
        }
        client = FakeClient(activation=license_payload("monthly"))
        state = LicenseManager(client=client, store=store).activate("ORIGINAL-CODE")
        self.assertTrue(state["authorized"])
        self.assertEqual(
            {
                "current_code_id": "code-test-001",
                "credential_refresh": True,
            },
            client.activate_options[0],
        )

    def test_invalid_same_device_binding_requests_controlled_refresh(self):
        store = FakeStore()
        store.metadata = {
            "app_name": LICENSE_APP_NAME,
            "code_id": "code-test-001",
            "binding_status": "invalid",
        }
        store.activation_context = {
            "activation_code": "ORIGINAL-CODE",
            "code_id": "code-test-001",
            "machine_code": store.machine,
        }
        client = FakeClient(activation=license_payload("monthly"))
        state = LicenseManager(client=client, store=store).activate("ORIGINAL-CODE")
        self.assertTrue(state["authorized"])
        self.assertTrue(client.activate_options[0]["credential_refresh"])

    def test_unbound_code_rebind_does_not_request_credential_refresh(self):
        store = FakeStore(machine="machine_new")
        store.metadata = {
            "app_name": LICENSE_APP_NAME,
            "code_id": "code-test-001",
            "binding_status": "unbound",
        }
        store.activation_context = {
            "activation_code": "ORIGINAL-CODE",
            "code_id": "code-test-001",
            "machine_code": "machine_old",
        }
        client = FakeClient(
            activation=license_payload("monthly", action="rebound", grant_score=0)
        )
        self.assertTrue(
            LicenseManager(client=client, store=store)
            .activate("ORIGINAL-CODE")["authorized"]
        )
        self.assertFalse(client.activate_options[0]["credential_refresh"])

    def test_points_and_unlimited_points_types_are_rejected(self):
        for license_type in ("points", "unlimited_points"):
            with self.subTest(license_type=license_type):
                state = LicenseManager(
                    client=FakeClient(activation=license_payload(license_type)),
                    store=FakeStore(),
                ).activate("NOT-TIME")
                self.assertFalse(state["authorized"])
                self.assertEqual("unsupported_license_type", state["code"])

    def test_expired_license_never_authorizes_runtime(self):
        store = FakeStore()
        store.credentials = {
            "device_session": "session",
            "device_credential": "credential",
        }
        store.activation_context = {
            "activation_code": "EXPIRED-CODE",
            "code_id": "code-test-001",
            "machine_code": store.machine,
        }
        store.license_snapshot = {
            key: value
            for key, value in license_payload("monthly").items()
            if key in {
                "app_name",
                "code_id",
                "license_type",
                "duration_days",
                "activated_at",
                "expires_at",
            }
        }
        manager = LicenseManager(
            client=FakeClient(status=status_payload(license_status="expired", remaining_days=0)),
            store=store,
        )
        state = manager.startup_check()
        self.assertFalse(state["authorized"])
        self.assertEqual(EXPIRES_AT, state["license"]["expires_at"])

    def test_network_failure_does_not_delete_local_credentials(self):
        store = FakeStore()
        store.credentials = {
            "device_session": "session",
            "device_credential": "credential",
        }
        state = LicenseManager(
            client=FakeClient(status=LicenseNetworkError()),
            store=store,
        ).startup_check()
        self.assertTrue(state["network_error"])
        self.assertEqual("授权服务器暂时无法连接，请重试", state["message"])
        self.assertIsNotNone(store.credentials)
        self.assertEqual(0, store.clear_count)

    def test_runtime_network_failure_has_monotonic_thirty_minute_grace(self):
        clock = [100.0]
        store = FakeStore()
        client = FakeClient()
        manager = LicenseManager(
            client=client,
            store=store,
            monotonic=lambda: clock[0],
            runtime_network_grace_seconds=1800,
        )
        self.assertTrue(manager.activate("GRACE-CODE")["authorized"])
        client.status = LicenseNetworkError()
        first = manager.runtime_check()
        self.assertTrue(first["authorized"])
        self.assertTrue(first["network_grace"])
        self.assertEqual(1800, first["network_grace_remaining_seconds"])
        clock[0] += 1799
        self.assertTrue(manager.runtime_check()["authorized"])
        clock[0] += 2
        expired = manager.runtime_check()
        self.assertFalse(expired["authorized"])
        self.assertEqual(0, expired["network_grace_remaining_seconds"])
        self.assertIsNotNone(store.credentials)

    def test_runtime_network_grace_resets_after_server_recovers(self):
        clock = [1.0]
        store = FakeStore()
        client = FakeClient()
        manager = LicenseManager(
            client=client,
            store=store,
            monotonic=lambda: clock[0],
        )
        self.assertTrue(manager.activate("RECOVERY-CODE")["authorized"])
        client.status = LicenseNetworkError()
        self.assertTrue(manager.runtime_check()["network_grace"])
        client.status = status_payload("monthly")
        recovered = manager.runtime_check()
        self.assertTrue(recovered["authorized"])
        self.assertFalse(recovered.get("network_grace", False))

    def test_runtime_401_never_enters_network_grace(self):
        store = FakeStore()
        client = FakeClient()
        manager = LicenseManager(client=client, store=store)
        self.assertTrue(manager.activate("REVOKED-CODE")["authorized"])
        client.status = LicenseServiceError(
            "http_401",
            "设备已失效",
            http_status=401,
        )
        state = manager.runtime_check()
        self.assertFalse(state["authorized"])
        self.assertFalse(state.get("network_grace", False))
        self.assertIsNone(store.credentials)

    def test_runtime_server_5xx_uses_network_grace(self):
        store = FakeStore()
        client = FakeClient()
        manager = LicenseManager(client=client, store=store)
        self.assertTrue(manager.activate("SERVER-ERROR-GRACE")["authorized"])
        client.status = LicenseServiceError(
            "http_503",
            "service unavailable",
            http_status=503,
            retryable=True,
        )
        state = manager.runtime_check()
        self.assertTrue(state["authorized"])
        self.assertTrue(state["network_grace"])

    def test_minimal_unbound_status_clears_invalid_credentials(self):
        store = FakeStore()
        store.credentials = {
            "device_session": "session",
            "device_credential": "credential",
        }
        state = LicenseManager(
            client=FakeClient(status={"binding_status": "unbound"}),
            store=store,
        ).startup_check()
        self.assertFalse(state["authorized"])
        self.assertIsNone(store.credentials)
        self.assertEqual("unbound", store.metadata["binding_status"])

    def test_unbind_requires_explicit_server_unbound_confirmation(self):
        store = FakeStore()
        client = FakeClient(unbind={"message": "request accepted"})
        manager = LicenseManager(client=client, store=store)
        self.assertTrue(manager.activate("KEEP-BOUND")["authorized"])
        state = manager.unbind_current_device()
        self.assertFalse(state["success"])
        self.assertIsNotNone(store.credentials)
        self.assertTrue(manager.is_runtime_authorized())

    def test_unbind_refreshes_server_transfer_counters(self):
        store = FakeStore()
        client = FakeClient(
            unbind={
                "binding_status": "unbound",
                "transfer_count": 2,
                "self_transfers_used_30d": 1,
                "self_transfers_remaining_30d": 2,
                "message": "解绑成功",
            }
        )
        manager = LicenseManager(client=client, store=store)
        self.assertTrue(manager.activate("UNBIND-COUNTERS")["authorized"])
        state = manager.unbind_current_device()
        self.assertTrue(state["success"])
        self.assertEqual(2, store.metadata["transfer_count"])
        self.assertEqual(1, store.metadata["self_transfers_used_30d"])
        self.assertEqual(2, store.metadata["self_transfers_remaining_30d"])

    def test_unbind_401_clears_invalid_device_credentials(self):
        store = FakeStore()
        client = FakeClient(
            unbind=LicenseServiceError(
                "http_401",
                "设备凭证已失效",
                http_status=401,
            )
        )
        manager = LicenseManager(client=client, store=store)
        self.assertTrue(manager.activate("UNBIND-401")["authorized"])
        state = manager.unbind_current_device()
        self.assertFalse(state["authorized"])
        self.assertEqual(401, state["http_status"])
        self.assertIsNone(store.credentials)
        self.assertEqual("invalid", store.metadata["binding_status"])

    def test_duplicate_unbind_is_rejected_before_second_request(self):
        store = FakeStore()
        manager = LicenseManager(client=FakeClient(), store=store)
        self.assertTrue(manager.activate("UNBIND-LOCK")["authorized"])
        manager._unbind_lock.acquire()
        try:
            state = manager.unbind_current_device()
        finally:
            manager._unbind_lock.release()
        self.assertFalse(state["success"])
        self.assertEqual("unbind_in_progress", state["code"])
        self.assertEqual([], manager.client.unbind_calls)

    def test_401_message_is_specific_and_does_not_fake_recovery(self):
        store = FakeStore()
        store.credentials = {
            "device_session": "session",
            "device_credential": "credential",
        }
        state = LicenseManager(
            client=FakeClient(
                status=LicenseServiceError(
                    "http_401",
                    "设备授权已失效，请重新激活或联系管理员",
                    http_status=401,
                )
            ),
            store=store,
        ).startup_check()
        self.assertFalse(state["authorized"])
        self.assertEqual(401, state["http_status"])
        self.assertEqual("当前设备授权已失效，请重新激活", state["message"])
        self.assertIsNone(store.credentials)


class OnlineLicenseHttpTests(unittest.TestCase):
    def test_activation_reads_full_license_object_from_real_server_envelope(self):
        envelope = {
            "ok": True,
            "message": "激活成功",
            "data": {"app_name": LICENSE_APP_NAME},
            "license": license_payload("time_30d", duration_days=30),
        }
        client = LicenseHttpClient(
            opener=lambda _request, timeout=None: FakeHttpResponse(envelope)
        )
        result = client.activate("TEST-CODE", "machine-stable")
        self.assertEqual("time_30d", result["license_type"])
        self.assertEqual(30, result["duration_days"])
        self.assertEqual("session-sensitive-value", result["device_session"])

    def test_activate_payload_is_protocol_v2_and_post_is_not_retried(self):
        captured = []

        def opener(request, timeout):
            captured.append((request, timeout))
            return FakeHttpResponse({"ok": True, "data": license_payload()})

        result = LicenseHttpClient(opener=opener).activate("TEST-CODE", "machine_stable")
        self.assertEqual("code-test-001", result["code_id"])
        self.assertEqual(1, len(captured))
        request = captured[0][0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(LICENSE_APP_NAME, body["app_name"])
        self.assertEqual(LICENSE_PROTOCOL_VERSION, body["license_protocol_version"])
        self.assertEqual("TEST-CODE", body["activation_code"])

    def test_reactivation_can_authenticate_with_existing_device_credentials(self):
        captured = []

        def opener(request, timeout):
            captured.append(request)
            return FakeHttpResponse({"ok": True, "data": license_payload()})

        LicenseHttpClient(opener=opener).activate(
            "ORIGINAL-CODE",
            "machine_stable",
            credentials={
                "device_session": "existing-session",
                "device_credential": "existing-credential",
            },
        )
        self.assertEqual(
            "Bearer existing-session",
            captured[0].get_header("Authorization"),
        )
        self.assertEqual(
            "existing-credential",
            captured[0].get_header("X-device-credential"),
        )

    def test_legacy_credential_refresh_fields_are_sent_only_when_requested(self):
        captured = []

        def opener(request, timeout):
            captured.append(request)
            return FakeHttpResponse({"ok": True, "data": license_payload()})

        LicenseHttpClient(opener=opener).activate(
            "ORIGINAL-CODE",
            "machine_stable",
            current_code_id="code-test-001",
            credential_refresh=True,
        )
        body = json.loads(captured[0].data.decode("utf-8"))
        self.assertEqual("code-test-001", body["current_code_id"])
        self.assertIs(True, body["credential_refresh"])

    def test_status_retries_one_transient_network_failure(self):
        calls = []
        requests = []

        def opener(request, timeout=None):
            calls.append(1)
            requests.append(request)
            if len(calls) == 1:
                raise URLError("temporary")
            return FakeHttpResponse({"ok": True, "data": status_payload()})

        client = LicenseHttpClient(opener=opener, sleeper=lambda _seconds: None)
        result = client.device_status(
            {"device_session": "session", "device_credential": "credential"},
            machine_code="machine-stable",
            code_id="code-001",
        )
        self.assertEqual("active", result["binding_status"])
        self.assertEqual(2, len(calls))
        self.assertEqual("Bearer session", requests[-1].get_header("Authorization"))
        self.assertEqual(
            "credential",
            requests[-1].get_header("X-device-credential"),
        )
        query = parse_qs(urlparse(requests[-1].full_url).query)
        self.assertEqual([LICENSE_APP_NAME], query["app_name"])
        self.assertEqual([str(LICENSE_PROTOCOL_VERSION)], query["license_protocol_version"])
        self.assertEqual(["machine-stable"], query["machine_code"])
        self.assertEqual(["code-001"], query["code_id"])

    def test_status_drops_server_returned_activation_code_and_credentials(self):
        payload = status_payload(
            primary_activation_code="FULL-CODE-MUST-DROP",
            activation_code="OTHER-CODE-MUST-DROP",
            device_session="session-must-drop",
            device_credential="credential-must-drop",
        )
        result = LicenseHttpClient(
            opener=lambda _request, timeout=None: FakeHttpResponse(
                {"ok": True, "data": payload}
            )
        ).device_status(
            {"device_session": "session", "device_credential": "credential"},
            machine_code="machine-stable",
            code_id="code-001",
        )
        for field in (
            "primary_activation_code",
            "activation_code",
            "device_session",
            "device_credential",
        ):
            self.assertNotIn(field, result)

    def test_unbind_sends_complete_protocol_v2_device_identity(self):
        captured = []

        def opener(request, timeout=None):
            captured.append(request)
            return FakeHttpResponse(
                {"ok": True, "binding_status": "unbound"}
            )

        result = LicenseHttpClient(opener=opener).unbind(
            {"device_session": "session", "device_credential": "credential"},
            machine_code="machine-stable",
            code_id="code-001",
        )
        self.assertEqual("unbound", result["binding_status"])
        request = captured[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(LICENSE_APP_NAME, body["app_name"])
        self.assertEqual(LICENSE_PROTOCOL_VERSION, body["license_protocol_version"])
        self.assertEqual("machine-stable", body["machine_code"])
        self.assertEqual("code-001", body["current_code_id"])
        self.assertEqual("Bearer session", request.get_header("Authorization"))

    def test_http_409_keeps_server_business_message(self):
        message = "24小时内不能再次换机"

        def opener(_request, timeout=None):
            body = json.dumps({"message": message, "code": "transfer_limited"}).encode()
            raise HTTPError(
                "https://license.example/device/unbind",
                409,
                "Conflict",
                {},
                io.BytesIO(body),
            )

        with self.assertRaises(LicenseServiceError) as caught:
            LicenseHttpClient(opener=opener).unbind(
                {"device_session": "session", "device_credential": "credential"},
                machine_code="machine-stable",
                code_id="code-001",
            )
        self.assertEqual(409, caught.exception.http_status)
        self.assertEqual(message, caught.exception.message)

    def test_http_429_keeps_server_cooldown_message(self):
        message = "成功换机后 24 小时内不能再次解绑。"

        def opener(_request, timeout=None):
            body = json.dumps({"message": message, "code": "cooldown"}).encode()
            raise HTTPError(
                "https://license.example/device/unbind",
                429,
                "Too Many Requests",
                {},
                io.BytesIO(body),
            )

        with self.assertRaises(LicenseServiceError) as caught:
            LicenseHttpClient(opener=opener).unbind(
                {"device_session": "session", "device_credential": "credential"},
                machine_code="machine-stable",
                code_id="code-001",
            )
        self.assertEqual(429, caught.exception.http_status)
        self.assertEqual(message, caught.exception.message)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
class WindowsLicenseStorageTests(unittest.TestCase):
    def test_dpapi_files_do_not_contain_plain_credentials_and_machine_is_stable(self):
        with tempfile.TemporaryDirectory(prefix="qcsckp-license-store-") as root:
            store = LicenseSecureStore(
                credential_file=str(Path(root) / "credentials.dpapi"),
                machine_code_file=str(Path(root) / "machine.dpapi"),
                device_code_file=str(Path(root) / "device-code.dpapi"),
                metadata_file=str(Path(root) / "metadata.json"),
                platform_name="win32",
            )
            store.save_credentials(
                {
                    "device_session": "plain-session-must-not-appear",
                    "device_credential": "plain-credential-must-not-appear",
                    "activation_code": "ORIGINAL-CODE-MUST-NOT-APPEAR",
                    "code_id": "code-001",
                    "machine_code": "machine-stable",
                }
            )
            store.save_license_snapshot(
                {
                    "app_name": LICENSE_APP_NAME,
                    "code_id": "code-001",
                    "license_type": "monthly",
                    "license_type_label": "月卡",
                    "duration_days": 30,
                    "activated_at": "2026-08-20T12:00:00+08:00",
                    "expires_at": EXPIRES_AT,
                    "is_permanent": False,
                }
            )
            raw = (Path(root) / "credentials.dpapi").read_text(encoding="ascii")
            self.assertNotIn("plain-session", raw)
            self.assertNotIn("plain-credential", raw)
            self.assertNotIn("ORIGINAL-CODE", raw)
            self.assertNotIn("monthly", raw)
            self.assertEqual(
                "plain-session-must-not-appear",
                store.load_credentials()["device_session"],
            )
            first = store.get_or_create_machine_code()
            second = store.get_or_create_machine_code()
            self.assertEqual(first, second)
            self.assertTrue(validate_device_code(first))
            self.assertEqual(
                "ORIGINAL-CODE-MUST-NOT-APPEAR",
                store.load_activation_context()["activation_code"],
            )
            store.clear_credentials()
            self.assertIsNone(store.load_credentials())
            self.assertEqual("monthly", store.load_license_snapshot()["license_type"])
            self.assertEqual(
                "ORIGINAL-CODE-MUST-NOT-APPEAR",
                store.load_activation_context()["activation_code"],
            )


class MacKeychainStorageTests(unittest.TestCase):
    def test_macos_credentials_and_machine_code_use_keychain_backend(self):
        class FakeKeyring:
            def __init__(self):
                self.values = {}

            def get_password(self, service, account):
                return self.values.get((service, account))

            def set_password(self, service, account, value):
                self.values[(service, account)] = value

            def delete_password(self, service, account):
                self.values.pop((service, account), None)

        fake = FakeKeyring()
        with tempfile.TemporaryDirectory(prefix="qcsckp-mac-license-") as root, patch(
            "services.license_storage._mac_keyring",
            return_value=fake,
        ):
            store = LicenseSecureStore(
                credential_file=str(Path(root) / "must-not-exist"),
                machine_code_file=str(Path(root) / "must-not-exist-machine"),
                metadata_file=str(Path(root) / "metadata.json"),
                platform_name="darwin",
            )
            store.save_credentials(
                {
                    "device_session": "session-in-keychain",
                    "device_credential": "credential-in-keychain",
                }
            )
            self.assertEqual(
                "session-in-keychain",
                store.load_credentials()["device_session"],
            )
            first = store.get_or_create_machine_code()
            self.assertEqual(first, store.get_or_create_machine_code())
            self.assertFalse((Path(root) / "must-not-exist").exists())
            self.assertFalse((Path(root) / "must-not-exist-machine").exists())


class LicenseSurfaceTests(unittest.TestCase):
    def test_activation_and_management_pages_have_no_localstorage_or_admin_routes(self):
        activation = (ROOT / "static" / "license.html").read_text(encoding="utf-8")
        management = (ROOT / "static" / "license_management.html").read_text(encoding="utf-8")
        combined = activation + management
        self.assertNotIn("localStorage", combined)
        self.assertNotIn("/api/license/admin/", combined)
        self.assertNotIn("transfer_code", combined)
        self.assertNotIn("remaining_credits", management)
        self.assertIn("正在激活，请勿重复点击", (ROOT / "services" / "license_manager.py").read_text(encoding="utf-8"))
        self.assertGreaterEqual(management.count("confirm("), 2)

    def test_activation_page_displays_machine_code_and_contact_panel_can_collapse(self):
        source = (ROOT / "static" / "license.html").read_text(encoding="utf-8")
        self.assertIn('id="machineCode"', source)
        self.assertIn("showMachineCode(state)", source)
        self.assertIn("contactPanel.classList.remove('show')", source)
        self.assertIn("收起联系方式", source)
        gui = (ROOT / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn('result["machine_code"]', gui)

    def test_main_surface_has_no_username_password_login(self):
        source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="loginForm"', source)
        self.assertNotIn('id="loginUser"', source)
        self.assertNotIn('id="loginPass"', source)
        self.assertNotIn("verifyAccountLogin", source)
        self.assertIn("getLicenseManagementInfo", source)

    def test_service_start_uses_license_identity_not_account_password(self):
        source = (ROOT / "api" / "views.py").read_text(encoding="utf-8")
        start = source[source.index("    def startService"):source.index("    def stopService")]
        self.assertIn("activate_license_runtime_identity", start)
        self.assertNotIn("verify_can_start_service", start)

    def test_client_registers_only_required_three_license_routes(self):
        source = (ROOT / "services" / "license_client.py").read_text(encoding="utf-8")
        self.assertIn('"/activate"', source)
        self.assertIn('"/device/status"', source)
        self.assertIn('"/device/unbind"', source)
        self.assertNotIn("/admin/", source)
        self.assertNotIn("/transfer/", source)
        for word in ("pre_deduct", "consume", "merge_points", "release_points"):
            self.assertNotIn(word, source)

    def test_main_runtime_is_started_only_by_license_entry_method(self):
        source = (ROOT / "gui_app.py").read_text(encoding="utf-8")
        self.assertNotIn("RUNTIME_SUPERVISOR.start(js_api)", source)
        self.assertIn("def enterLicensedApplication", source)
        self.assertIn("url=license_url", source)
        self.assertIn('QCSCKP_LICENSE_RECHECK_SECONDS", "60"', source)
        self.assertIn("state = manager.runtime_check()", source)
        self.assertIn("window.applyLicenseRuntimeState", source)
        self.assertIn(
            "window.applyLicenseRuntimeState = renderSidebarLicense",
            (ROOT / "static" / "index.html").read_text(encoding="utf-8"),
        )

    def test_backend_gate_rejects_business_method_when_not_authorized(self):
        import gui_app

        class Denied:
            @staticmethod
            def is_runtime_authorized():
                return False

        bridge = object.__new__(gui_app.JSApi)
        bridge.license_manager = Denied()
        result = bridge.getAppVersion()
        self.assertFalse(result["success"])
        self.assertEqual("license_required", result["error"])

    def test_fixed_names_are_not_user_editable(self):
        self.assertEqual("QCSCKP", LICENSE_APP_NAME)
        self.assertEqual("千川素材看盘工具", SOFTWARE_CHINESE_NAME)

    def test_packaging_manifests_include_license_modules_without_secret_values(self):
        windows = (ROOT / "packaging" / "windows" / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("services.device_identity", windows)
        macos = (ROOT / "packaging" / "macos" / "build_macos.sh").read_text(encoding="utf-8")
        workflow = (ROOT.parent / ".github" / "workflows" / "build-macos-notarized.yml").read_text(encoding="utf-8")
        for manifest in (windows, macos):
            self.assertIn("services.license_client", manifest)
            self.assertIn("services.license_storage", manifest)
            self.assertIn("services.license_manager", manifest)
        self.assertIn("secrets.APPLE_CERTIFICATE_PASSWORD", workflow)
        self.assertIn("secrets.APPLE_APP_SPECIFIC_PASSWORD", workflow)
        self.assertNotIn("@outlook.com", workflow)
        self.assertNotIn("export APPLE_APP_SPECIFIC_PASSWORD=", workflow)


if __name__ == "__main__":
    unittest.main()
