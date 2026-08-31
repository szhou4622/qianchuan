import io
import json
import os
import socket
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from services.license_client import LicenseHttpClient, LicenseNetworkError, LicenseServiceError
from services.license_manager import LicenseManager
from services.license_transport import (
    LicenseTransport, TransportFailure, WindowsHttpsOpener, _NoRedirect,
    _python_opener, _curl_proxy_options, describe_network_error,
)


BASE = "https://license.example/api/license"


def missing_session(request, timeout=None):
    body = json.dumps({"ok": False, "message": "缺少设备会话。"}).encode()
    raise HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(body))


def certificate_failure(code):
    exc = ssl.SSLCertVerificationError(1, "private-proxy:password@host")
    exc.verify_code = code
    return URLError(exc)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qcsckp-license-transport-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transport = self.new_transport()

    def new_transport(self):
        return LicenseTransport(base_url=BASE, settings_file=self.root / "license_transport.json", log_file=self.root / "license-network.log")

    def test_classification_never_exposes_exception_text(self):
        cases = [(socket.gaierror(-2, "private"), "dns"), (TimeoutError("private"), "timeout"), (ConnectionResetError("private"), "connection_reset"), (ConnectionRefusedError("private"), "connection_refused"), (URLError("proxy http://user:secret@private"), "proxy"), (ssl.SSLError("private"), "tls")]
        for exc, kind in cases:
            with self.subTest(kind=kind):
                details = describe_network_error(exc)
                self.assertEqual(kind, details["kind"])
                self.assertNotIn("private", json.dumps(details))
                self.assertNotIn("secret", json.dumps(details))

    def test_certificate_errors_distinguish_clock_name_and_trust(self):
        for code, kind in [(10, "certificate_time"), (9, "certificate_time"), (62, "certificate_identity"), (20, "certificate_chain"), (23, "certificate_invalid")]:
            self.assertEqual(kind, describe_network_error(certificate_failure(code))["kind"])

    def test_probe_401_is_connectivity_not_authorization(self):
        captured = []

        def opener(request, timeout):
            captured.append(request)
            return missing_session(request)

        with patch.object(self.transport, "_opener", return_value=opener):
            result = self.transport.diagnose_and_repair()
        self.assertTrue(result["success"])
        self.assertNotIn("authorized", result)
        self.assertEqual(1, len(captured))
        self.assertEqual("GET", captured[0].get_method())
        self.assertIsNone(captured[0].data)
        self.assertEqual(BASE + "/device/status", captured[0].full_url)
        self.assertIsNone(captured[0].get_header("Authorization"))
        self.assertIsNone(captured[0].get_header("X-device-credential"))
        self.assertNotIn("machine", captured[0].full_url)

    def test_cert_chain_repair_is_verified_then_persisted(self):
        def choose(mode):
            if mode == "default":
                raise certificate_failure(20)
            return missing_session

        with patch.object(self.transport, "_opener", side_effect=choose):
            result = self.transport.diagnose_and_repair()
        self.assertTrue(result["success"])
        self.assertEqual("bundled_ca", self.transport.mode)
        self.assertEqual("bundled_ca", self.new_transport().mode)
        self.assertEqual(["certificate_chain", "reachable"], [step["kind"] for step in result["steps"]])
        saved = self.transport.settings_file.read_text()
        self.assertEqual({"schema", "mode"}, set(json.loads(saved)))

    def test_windows_fallback_only_after_verified_probe(self):
        def choose(mode):
            if mode != "windows_https":
                raise URLError(TimeoutError())
            return missing_session

        with patch("services.license_transport.sys.platform", "win32"), patch.object(self.transport, "_opener", side_effect=choose):
            result = self.transport.diagnose_and_repair()
        self.assertTrue(result["success"])
        self.assertEqual("windows_https", self.transport.mode)
        self.assertEqual("windows_https", self.new_transport().mode)

    def test_certificate_identity_time_revocation_do_not_fallback(self):
        for code in [9, 10, 62, 23]:
            with self.subTest(code=code), patch.object(self.transport, "_opener", side_effect=certificate_failure(code)) as mock:
                result = self.transport.diagnose_and_repair()
                self.assertFalse(result["success"])
                self.assertEqual(1, mock.call_count)
                self.assertFalse(self.transport.settings_file.exists())

    def test_proxy_is_not_silently_bypassed(self):
        with patch("services.license_transport.sys.platform", "darwin"), patch("services.license_transport.getproxies", return_value={"https": "http://user:SECRET@proxy:1234"}), patch("services.license_transport.proxy_bypass", return_value=False):
            options = _curl_proxy_options("license.example")
        self.assertIn('proxy = "http://user:SECRET@proxy:1234"', options)

    def test_http_errors_do_not_attempt_to_bypass_server(self):
        for status in [400, 403, 409, 429, 503]:
            def opener(request, timeout):
                raise HTTPError(request.full_url, status, "error", {}, io.BytesIO(b'{"ok":false}'))

            with self.subTest(status=status), patch.object(self.transport, "_opener", return_value=opener) as mock:
                result = self.transport.diagnose_and_repair()
                self.assertFalse(result["success"])
                self.assertEqual(1, mock.call_count)
                self.assertEqual(status, result["steps"][0]["http_status"])

    def test_html_401_is_not_proof_of_license_endpoint(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 401, "error", {}, io.BytesIO(b'<html>proxy login</html>'))

        with patch.object(self.transport, "_opener", return_value=opener):
            self.assertFalse(self.transport.diagnose_and_repair()["success"])

    def test_failure_retains_existing_mode_and_files(self):
        self.transport._save_mode("bundled_ca")
        self.transport.mode = "bundled_ca"
        credential = self.root / "license_credentials.dpapi"
        credential.write_bytes(b"pretend-encrypted-test-fixture")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with patch.object(self.transport, "_opener", side_effect=TimeoutError()):
            result = self.transport.diagnose_and_repair()
        self.assertFalse(result["success"])
        for name, data in before.items():
            self.assertEqual(data, (self.root / name).read_bytes())
        self.assertEqual("bundled_ca", self.transport.mode)

    def test_double_click_does_not_start_second_probe(self):
        self.transport._repair_lock.acquire()
        try:
            with patch.object(self.transport, "_probe") as mock:
                result = self.transport.diagnose_and_repair()
                self.assertFalse(result["success"])
                mock.assert_not_called()
        finally:
            self.transport._repair_lock.release()

    def test_setting_file_cannot_inject_endpoint_or_executable(self):
        self.transport.settings_file.write_text(json.dumps({"mode": "bad.exe", "endpoint": "http://evil"}))
        fresh = self.new_transport()
        self.assertEqual("default", fresh.mode)
        self.assertEqual(BASE, fresh.base_url)

    def test_only_three_secure_routes_are_allowed(self):
        for url, method in [("http://license.example/api/license/activate", "POST"), ("https://evil/activate", "POST"), (BASE + "/admin/edit", "POST"), (BASE + "/device/status", "POST")]:
            with self.subTest(url=url), self.assertRaises(TransportFailure):
                self.transport(Request(url, method=method), 5)

    def test_safe_mode_persistence_failure_does_not_claim_durable_repair(self):
        with patch.object(self.transport, "_opener", return_value=missing_session), patch.object(self.transport, "_save_mode", return_value=False):
            result = self.transport.diagnose_and_repair()
        self.assertTrue(result["success"])
        self.assertFalse(result["saved"])
        self.assertIn("保存失败", result["message"])

    def test_default_opener_never_forwards_credentials_on_redirect(self):
        handler = _NoRedirect()
        request = Request(BASE + "/activate", data=b"secret", headers={"Authorization": "Bearer token"})
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, "https://evil"))

    def test_ca_repair_keeps_tls_and_hostname_verification(self):
        for bundled in [False, True]:
            opener = _python_opener(bundled).__self__
            https = next(handler for handler in opener.handlers if hasattr(handler, "_context"))
            self.assertEqual(ssl.CERT_REQUIRED, https._context.verify_mode)
            self.assertTrue(https._context.check_hostname)

    def test_request_diagnostics_are_secret_free_and_post_sent_once(self):
        captured = []

        def fail(request, timeout):
            captured.append(request)
            raise URLError("proxy password=SENSITIVE https://user:secret@proxy")

        with patch("services.license_client.LicenseTransport", return_value=self.transport), patch.object(self.transport, "_opener", return_value=fail):
            client = LicenseHttpClient(base_url=BASE, sleeper=lambda _: None)
            with self.assertRaises(LicenseNetworkError):
                client.activate("FULL-CODE-SECRET", "PRIVATE-MACHINE", credentials={"device_session": "PRIVATE-SESSION", "device_credential": "PRIVATE-CREDENTIAL"})
        self.assertEqual(1, len(captured))
        log = self.transport.log_file.read_text(encoding="utf-8")
        for secret in ["SENSITIVE", "FULL-CODE", "PRIVATE", "user:secret"]:
            self.assertNotIn(secret, log)
        self.assertEqual("proxy", json.loads(log)["kind"])
        self.assertEqual("POST", json.loads(log)["method"])

    def test_no_network_failure_deletes_credentials(self):
        # Use the existing manager/storage doubles so no real user data loads.
        from test_online_license import FakeStore

        store = FakeStore()
        store.credentials = {"device_session": "TEST-SESSION", "device_credential": "TEST-CREDENTIAL"}
        client = LicenseHttpClient(opener=lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()), sleeper=lambda _: None)
        manager = LicenseManager(client=client, store=store)
        state = manager.startup_check()
        self.assertFalse(state["authorized"])
        self.assertTrue(state["network_error"])
        self.assertEqual("TEST-SESSION", store.credentials["device_session"])


@unittest.skipUnless(os.name == "nt", "Windows system HTTPS adapter")
class WindowsTransportTests(unittest.TestCase):
    def opener(self):
        # Construction is tested separately; no actual program is run here.
        opener = object.__new__(WindowsHttpsOpener)
        opener.path = Path(r"C:\Windows\System32\curl.exe")
        return opener

    def test_payload_only_in_stdin_no_curlrc_no_shell_no_retry(self):
        request = Request(BASE + "/activate", data=json.dumps({"activation_code": 'SECRET"\n-CODE'}).encode(), headers={"Authorization": "Bearer PRIVATE-SESSION", "X-Device-Credential": "PRIVATE-CREDENTIAL"}, method="POST")
        response = SimpleNamespace(returncode=0, stdout=b'{"ok":true}\nQCSCKP_HTTP_STATUS:200', stderr=b"")
        with patch("services.license_transport._curl_proxy_options", return_value=[]), patch("services.license_transport.subprocess.run", return_value=response) as run:
            with self.opener()(request, 8) as result:
                self.assertEqual(200, result.status)
        args, kwargs = run.call_args
        self.assertEqual([str(self.opener().path), "-q", "--config", "-"], args[0])
        self.assertNotIn("SECRET", str(args))
        self.assertNotIn("PRIVATE", str(args))
        config = kwargs["input"].decode()
        self.assertIn("PRIVATE-SESSION", config)
        self.assertIn('retry = "0"', config)
        self.assertIn('proto = "=https"', config)
        for option in ["insecure", "location", "ssl-no-revoke", "trace", "verbose"]:
            self.assertNotIn(option, config)
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(subprocess.CREATE_NO_WINDOW, kwargs["creationflags"])
        self.assertEqual(1, run.call_count)

    def test_http_401_is_passed_to_normal_authorization_handler(self):
        response = SimpleNamespace(returncode=0, stdout=b'{"ok":false}\nQCSCKP_HTTP_STATUS:401', stderr=b"")
        with patch("services.license_transport._curl_proxy_options", return_value=[]), patch("services.license_transport.subprocess.run", return_value=response):
            with self.assertRaises(HTTPError) as caught:
                self.opener()(Request(BASE + "/device/status"), 8)
        self.assertEqual(401, caught.exception.code)
        caught.exception.close()

    def test_curl_errors_sanitized_not_retried(self):
        for code, kind in [(5, "proxy"), (6, "dns"), (28, "timeout"), (35, "tls"), (60, "certificate_invalid")]:
            response = SimpleNamespace(returncode=code, stdout=b"", stderr=b"PRIVATE-TOKEN https://user:password@proxy")
            with self.subTest(code=code), patch("services.license_transport._curl_proxy_options", return_value=[]), patch("services.license_transport.subprocess.run", return_value=response) as run:
                with self.assertRaises(TransportFailure) as caught:
                    self.opener()(Request(BASE + "/activate", data=b"{}"), 8)
                self.assertEqual(kind, caught.exception.kind)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(1, run.call_count)

    def test_pac_blocks_previously_saved_windows_route(self):
        with patch("services.license_transport._curl_proxy_options", side_effect=TransportFailure("proxy_policy")), patch("services.license_transport.subprocess.run") as run:
            with self.assertRaises(TransportFailure):
                self.opener()(Request(BASE + "/device/status"), 8)
            run.assert_not_called()

    def test_schannel_is_required_and_path_is_fixed(self):
        with patch("services.license_transport._system_curl_path", return_value=self.opener().path), patch("services.license_transport.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b"curl OpenSSL")):
            with self.assertRaises(TransportFailure):
                WindowsHttpsOpener()

    def test_curl_timeout_never_repeats_post(self):
        with patch("services.license_transport._curl_proxy_options", return_value=[]), patch("services.license_transport.subprocess.run", side_effect=subprocess.TimeoutExpired("curl", 10)) as run:
            with self.assertRaises(TransportFailure) as caught:
                self.opener()(Request(BASE + "/activate", data=b"{}"), 8)
            self.assertEqual("timeout", caught.exception.kind)
            self.assertEqual(1, run.call_count)


class PreLoginTests(unittest.TestCase):
    def test_repair_bridge_works_before_license_but_does_not_enter(self):
        import gui_app

        calls = []
        class Denied:
            client = SimpleNamespace(diagnose_and_repair=lambda: calls.append("GET probe") or {"success": True})

            def is_runtime_authorized(self):
                return False

        bridge = object.__new__(gui_app.JSApi)
        bridge.license_manager = Denied()
        self.assertTrue(bridge.diagnoseLicenseConnection()["success"])
        self.assertEqual(["GET probe"], calls)
        self.assertFalse(bridge.enterLicensedApplication()["success"])
        self.assertEqual("license_required", bridge.getAppVersion()["error"])

    def test_standalone_entry_precedes_webview_and_manager_import(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "startup_bootstrap.py").read_text(encoding="utf-8")
        main = source[source.index("def main()") :]
        self.assertLess(main.index('"--repair-license-connection"'), main.index("from channel_runtime"))
        helper = source[source.index("def _repair_license_connection()"):source.index("def _main_impl()")]
        self.assertNotIn("LicenseManager", helper)
        self.assertNotIn("load_credentials", helper)
        page = (root / "static/license.html").read_text(encoding="utf-8")
        self.assertIn("if (recheck) await checkAuthorization()", page)
        self.assertIn("repairConnectionButton.disabled = busy", page)
        self.assertIn("connectionReport.textContent = lines.join", page)

    def test_standalone_repair_never_switches_user_profile(self):
        import startup_bootstrap
        with patch("sys.argv", ["QCSCKP.exe", "--repair-license-connection"]), patch.object(startup_bootstrap, "install_exception_hooks"), patch.object(startup_bootstrap, "_repair_license_connection", return_value=0) as repair, patch("channel_runtime.prepare_profile") as prepare, patch("channel_runtime.InstanceLease") as lease:
            self.assertEqual(0, startup_bootstrap.main())
            repair.assert_called_once()
            prepare.assert_not_called()
            lease.assert_not_called()


if __name__ == "__main__":
    unittest.main()
