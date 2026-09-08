"""Release-policy and packaging helpers only; never run PyInstaller or an app."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_windows_release_privacy import privacy
import release_configuration as policy

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "packaging/windows/build_windows.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
REQUIRED = (
    "QCSCKP.exe", "VERSION.txt", "bin/python312.dll", "bin/release.json",
    "bin/DEFAULT-CONFIG.json", "bin/apply_channel_update.ps1", "bin/static/index.html",
    "bin/static/license.html", "bin/pythonnet/runtime/Python.Runtime.dll",
    "bin/clr_loader/ffi/dlls/amd64/ClrLoader.dll",
    "bin/webview/lib/Microsoft.Web.WebView2.Core.dll",
    "bin/webview/lib/Microsoft.Web.WebView2.WinForms.dll",
    "bin/webview/lib/WebBrowserInterop.x64.dll",
    "bin/webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
    "runtime/MicrosoftEdgeWebview2Setup.exe",
)


class UnifiedWindowsPrivacyTests(unittest.TestCase):
    def test_production_qcsckp_control_names_require_explicit_policy_review(self):
        paths = list(ROOT.glob("*.py"))
        for folder in ("services", "api", "production_v1a", "packaging/windows"):
            paths.extend(path for path in (ROOT / folder).rglob("*") if path.suffix in {".py", ".ps1", ".cmd"})
        names = set()
        for path in paths:
            names.update(re.findall(r"QCSCKP_[A-Z][A-Z0-9_]+", path.read_text(encoding="utf-8-sig")))
        # HTTP_STATUS is curl's stdout delimiter, not an environment variable.
        reviewed = set(policy.DEVELOPMENT_ENVIRONMENT_KEYS) | set(policy.DIRECTORY_ENVIRONMENT_KEYS) | {"QCSCKP_HTTP_STATUS"}
        self.assertEqual(set(), names - reviewed, "Review a new internal protocol variable before blocking it")

    def test_nested_user_state_and_temp_credentials_are_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            forbidden = ("bin/data/profiles/owner-test/innocent.bin", "bin/shared-v1/identity/activation.bin",
                         "custom-device.dpapi", "bin/qianchuan_open_api_token.json.uuid.tmp",
                         "bin/.license_device_code.dpapi.uuid.tmp", "activation_codes.csv",
                         "bin/private-signing.key", "bin/archive/client.pfx", "bin/history.sqlite3-wal")
            for relative in forbidden:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"test fixture only")
            self.assertEqual(len(forbidden), len(privacy.private_artifacts(root)))
            self.assertEqual(len(forbidden), len(privacy.sanitize_release(root)))
            privacy.verify_release(root)

    def test_public_tls_roots_and_dependency_profiles_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, payload in (
                ("bin/certifi/cacert.pem", b"-----BEGIN CERTIFICATE-----\npublic-test-root\n-----END CERTIFICATE-----"),
                ("bin/sdk/profiles/schema.json", b'{"properties":{"app_secret":{"type":"string"}}}'),
                ("bin/static/feishu_binding.html", b'<input name="app_secret">'),
                ("bin/DEFAULT-CONFIG.json", b'{"defaults":{"execution_permission_source":"owner_settings_and_saved_rules"}}'),
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            self.assertEqual([], privacy.sanitize_release(root))
            self.assertTrue((root / "bin/certifi/cacert.pem").is_file())

    def test_private_key_cannot_masquerade_as_certifi_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = root / "bin/certifi/cacert.pem"
            key.parent.mkdir(parents=True)
            key.write_bytes(b"-----BEGIN RSA PRIVATE KEY-----\nfake-test-key\n-----END RSA PRIVATE KEY-----")
            self.assertEqual(1, len(privacy.private_artifacts(root)))

    def test_renamed_token_json_and_private_pem_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "innocent.json").write_text('{"refresh_token":"test-not-a-real-token"}')
            (root / "renamed.json").write_text('{"format":"qcsckp-oceanengine-token-dpapi-v1","ciphertext":"fake"}')
            (root / "private-cert.pem").write_text("-----BEGIN CERTIFICATE-----\nfake-client-cert")
            self.assertEqual(3, len(privacy.private_artifacts(root)))

    def test_privacy_is_checked_before_hashes_and_archive(self):
        source = BUILD_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertLess(source.index("& $python $privacyVerifier --sanitize"),
                        source.index("$criticalFiles = @(Get-CriticalReleaseHashes"))
        self.assertLess(source.index("$criticalFiles = @(Get-CriticalReleaseHashes"), source.index("& tar.exe"))
        self.assertIn("enforce_packaged_configuration(for_build=True)", source)
        self.assertIn('"--collect-binaries", "pythonnet"', source)
        self.assertIn('"--collect-binaries", "clr_loader"', source)
        self.assertIn('"--add-data", "$defaultConfigPath;."', source)


@unittest.skipUnless(POWERSHELL and os.name == "nt", "Windows PowerShell helper simulation")
class UnifiedWindowsManifestTests(unittest.TestCase):
    def run_helper(self, root):
        # Parse then evaluate function definitions only. No top-level build,
        # signing, download, process start, archive or publication is executed.
        script = """
$ErrorActionPreference = 'Stop'
$tokens=$null
$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:TEST_RELEASE_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Build script has PowerShell parse errors' }
$names=@('Get-CriticalReleasePaths','Get-CriticalReleaseHashes','Get-ReleaseSha256')
$functions=$ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names},$true)
foreach($function in $functions) { Invoke-Expression $function.Extent.Text }
@(Get-CriticalReleaseHashes -ReleaseDir $env:TEST_RELEASE_ROOT) | ConvertTo-Json -Depth 5 -Compress
"""
        command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        environment = {**os.environ, "TEST_RELEASE_SCRIPT": str(BUILD_SCRIPT), "TEST_RELEASE_ROOT": str(root)}
        # A pwsh host's module path is incompatible with Windows PowerShell's
        # built-in utility module; let this isolated child restore its default.
        environment.pop("PSModulePath", None)
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                              env=environment,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def seed(self, root):
        for relative in REQUIRED:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("mock-dependency:" + relative).encode())

    def test_required_paths_optional_wpf_and_duplicate_load_paths_all_hashed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            extras = ("bin/Python.Runtime.dll", "bin/Microsoft.Web.WebView2.Core.dll",
                      "bin/webview/lib/Microsoft.Web.WebView2.Wpf.dll")
            for relative in extras:
                (root / relative).write_bytes(b"mock-optional-copy")
            result = self.run_helper(root)
            self.assertEqual(0, result.returncode, result.stderr)
            rows = json.loads(result.stdout)
            indexed = {row["path"]: row for row in rows}
            self.assertEqual(set(REQUIRED) | set(extras), set(indexed))
            for relative, row in indexed.items():
                payload = (root / relative).read_bytes()
                self.assertEqual(len(payload), row["size"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), row["sha256"])

    def test_missing_canonical_managed_dll_fails_even_when_root_copy_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            (root / "bin/pythonnet/runtime/Python.Runtime.dll").unlink()
            (root / "bin/Python.Runtime.dll").write_bytes(b"root-copy-is-not-enough")
            result = self.run_helper(root)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Critical release file is missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
