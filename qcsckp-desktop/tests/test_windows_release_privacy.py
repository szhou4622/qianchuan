from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "packaging"
    / "windows"
    / "verify_release_privacy.py"
)
SPEC = importlib.util.spec_from_file_location("verify_release_privacy", MODULE_PATH)
assert SPEC and SPEC.loader
privacy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(privacy)


class WindowsReleasePrivacyTests(unittest.TestCase):
    def test_blank_release_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("data", "logs", "temp"):
                (root / name).mkdir()
            (root / "QCSCKP.exe").write_bytes(b"test executable")
            (root / "bin" / "static").mkdir(parents=True)
            (root / "bin" / "static" / "feishu_binding.html").write_text(
                "<input name='app_secret'>", encoding="utf-8"
            )
            self.assertEqual([], privacy.privacy_violations(root))

    def test_feishu_profile_is_rejected_even_outside_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            (root / "bin" / "feishu_local_profiles.json").write_text(
                "{}", encoding="utf-8"
            )
            violations = privacy.privacy_violations(root)
            self.assertTrue(any("Feishu" in item or "runtime" in item for item in violations))

    def test_any_file_in_runtime_directories_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "data").mkdir()
            (root / "data" / "innocent.txt").write_text("x", encoding="utf-8")
            violations = privacy.privacy_violations(root)
            self.assertTrue(any("not empty" in item for item in violations))

    def test_database_and_logs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            (root / "bin" / "runtime.sqlite3").write_bytes(b"")
            (root / "worker.log").write_text("", encoding="utf-8")
            violations = privacy.privacy_violations(root)
            self.assertEqual(2, len(violations))

    def test_license_credentials_machine_code_and_metadata_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            for name in (
                "license_credentials.dpapi",
                "license_device_code.dpapi",
                "license_machine_code.dpapi",
                "license_metadata.json",
            ):
                (root / "bin" / name).write_text("private", encoding="utf-8")
            violations = privacy.privacy_violations(root)
            self.assertEqual(4, len(violations))

    def test_dependency_source_names_are_not_false_positives(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin" / "sdk").mkdir(parents=True)
            (root / "bin" / "sdk" / "token_store.py").write_text("", encoding="utf-8")
            (root / "bin" / "sdk" / "config.py").write_text("", encoding="utf-8")
            self.assertEqual([], privacy.privacy_violations(root))

    def test_sanitize_removes_private_files_and_keeps_runtime_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "data").mkdir()
            (root / "data" / "feishu_local_profiles.json").write_text(
                '{"encrypted": "ciphertext"}', encoding="utf-8"
            )
            (root / "logs").mkdir()
            (root / "logs" / "worker.log").write_text("private", encoding="utf-8")
            removed = privacy.sanitize_release(root)
            self.assertEqual(2, len(removed))
            self.assertTrue((root / "data").is_dir())
            self.assertTrue((root / "logs").is_dir())
            self.assertEqual([], privacy.privacy_violations(root))


if __name__ == "__main__":
    unittest.main()
