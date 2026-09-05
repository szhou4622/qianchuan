from __future__ import annotations

import hashlib
import json
import tempfile
import sys
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import startup_bootstrap as bootstrap


class StartupBootstrapTests(unittest.TestCase):
    def test_diagnostic_failure_cannot_hide_unhandled_exception(self):
        def fail_diagnostics(*args, **kwargs):
            raise RuntimeError('diagnostics broken')

        with tempfile.TemporaryDirectory(prefix='qcsckp-bootstrap-hooks-') as temp:
            with (
                patch.object(bootstrap, '_runtime_root', return_value=Path(temp)),
                patch.object(bootstrap, '_STATE_FILE', None),
                patch.object(bootstrap, '_HOOKS_INSTALLED', False),
                patch.object(bootstrap, '_FAULT_HANDLE', None),
                patch.object(bootstrap.faulthandler, 'enable'),
                patch.object(sys, 'excepthook'),
                patch.object(threading, 'excepthook'),
                patch.dict(sys.modules, {'services.diagnostics': SimpleNamespace(record_event=fail_diagnostics)}),
                patch.object(bootstrap, '_show_fatal_error') as show,
            ):
                try:
                    bootstrap.install_exception_hooks()
                    error = ValueError('app_secret=never-export-this')
                    sys.excepthook(ValueError, error, None)
                    text = bootstrap.startup_log_path().read_text(encoding='utf-8')
                    self.assertIn('unhandled_exception', text)
                    self.assertIn('ValueError', text)
                    self.assertIn('diagnostic_event_failed', text)
                    self.assertNotIn('never-export-this', text)
                    show.assert_called_once()
                finally:
                    if bootstrap._FAULT_HANDLE:
                        bootstrap._FAULT_HANDLE.close()

    def _release_fixture(self, root: Path) -> Path:
        files = {
            "QCSCKP.exe": b"exe",
            "VERSION.txt": b"version",
            "bin/python312.dll": b"python",
            "bin/static/index.html": b"index",
            "bin/static/license.html": b"license",
            "bin/webview/lib/runtimes/win-x64/native/WebView2Loader.dll": b"loader",
            "runtime/MicrosoftEdgeWebview2Setup.exe": b"microsoft",
        }
        manifest = {"app_name": "QCSCKP", "version": "0.1.64", "critical_files": []}
        for relative, payload in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            manifest["critical_files"].append(
                {
                    "path": relative,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        (root / "PACKAGE-MANIFEST.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return root / "QCSCKP.exe"

    def test_package_manifest_detects_missing_or_changed_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = self._release_fixture(root)
            with (
                patch.object(bootstrap.sys, "frozen", True, create=True),
                patch.object(bootstrap.sys, "platform", "win32"),
                patch.object(bootstrap.sys, "executable", str(executable)),
            ):
                self.assertEqual([], bootstrap.validate_package_integrity())
                (root / "bin/static/index.html").write_bytes(b"changed")
                issues = bootstrap.validate_package_integrity()
                self.assertTrue(any("index.html" in item for item in issues))

    def test_missing_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "QCSCKP.exe"
            executable.write_bytes(b"exe")
            with (
                patch.object(bootstrap.sys, "frozen", True, create=True),
                patch.object(bootstrap.sys, "platform", "win32"),
                patch.object(bootstrap.sys, "executable", str(executable)),
            ):
                self.assertIn(
                    "PACKAGE-MANIFEST.json 缺失",
                    bootstrap.validate_package_integrity(),
                )

    def test_webview2_install_only_runs_when_runtime_is_missing(self):
        with (
            patch.object(bootstrap.sys, "frozen", True, create=True),
            patch.object(bootstrap.sys, "platform", "win32"),
            patch.object(
                bootstrap,
                "detect_webview2_version",
                side_effect=["", "151.0.4129.107"],
            ),
            patch.object(bootstrap, "_install_webview2", return_value=True) as install,
        ):
            self.assertEqual("151.0.4129.107", bootstrap.ensure_webview2())
            install.assert_called_once_with()

    def test_startup_diagnostic_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            with (
                patch.object(bootstrap, "_runtime_root", return_value=runtime),
                patch.object(bootstrap, "detect_webview2_version", return_value="151.0"),
                patch.object(bootstrap, "validate_package_integrity", return_value=[]),
                patch.object(bootstrap, "_recent_windows_application_errors", return_value=[]),
                patch.object(bootstrap, "_STATE_FILE", None),
            ):
                bootstrap.startup_log("app_secret=top-secret access_token=token-value")
                report = bootstrap.generate_diagnostic_report()
                text = report.read_text(encoding="utf-8")
                self.assertNotIn("top-secret", text)
                self.assertNotIn("token-value", text)
                self.assertIn("<redacted>", text)

    def test_startup_redaction_removes_paths_urls_cookies_and_business_ids(self):
        raw = (
            r"C:\Users\RealName\AppData\Local\QCSCKP\logs\startup.log "
            "https://example.test/path?token=private Cookie: sessionid=private-value\n"
            "advertiser 1862251436023940"
        )
        safe = bootstrap._redact(raw)
        for private in (
            "RealName",
            "example.test",
            "private-value",
            "1862251436023940",
        ):
            self.assertNotIn(private, safe)
        self.assertIn("<local-path>", safe)
        self.assertIn("<url>", safe)
        self.assertIn("<business-id>", safe)

    def test_startup_state_contains_release_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "release.json").write_text(
                json.dumps(
                    {
                        "version": "0.1.66",
                        "channel": "production",
                        "build_revision": 16,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(bootstrap, "_app_root", return_value=root):
                state = bootstrap._state_payload("ready")
            self.assertEqual("0.1.66", state["version"])
            self.assertEqual("production", state["channel"])
            self.assertEqual(16, state["build_revision"])

    def test_windows_build_uses_bootstrap_and_signed_webview_installer(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "packaging/windows/build_windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('startup_bootstrap.py', script)
        self.assertIn('LinkId=2124703', script)
        self.assertIn('Get-AuthenticodeSignature', script)
        self.assertIn('PACKAGE-MANIFEST.json', script)

    def test_native_updater_requires_ready_handshake_and_rolls_back(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "packaging/windows/apply_channel_update.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("startup-state", script)
        self.assertIn("phase -eq 'ready'", script)
        self.assertIn("build_revision", script)
        self.assertIn("New version did not reach ready state", script)
        self.assertIn("Stop-Process -Id $newProcess.Id", script)

    def test_gui_startup_exception_is_reraised_for_bootstrap(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn('except Exception as e:', source)
        self.assertIn('traceback.print_exc()\n        raise', source)


if __name__ == "__main__":
    unittest.main()
