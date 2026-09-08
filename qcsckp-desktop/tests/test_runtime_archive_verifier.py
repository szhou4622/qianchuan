"""Verify runtime packaging through fake readers and the existing r20 EXE only."""
from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging/windows/verify_runtime_archive.py"
SPEC = importlib.util.spec_from_file_location("verify_runtime_archive", SCRIPT)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
R20_EXE = ROOT / "output/windows/v0.1.66/production/r20/dist/QCSCKP-v0.1.66-production-r20-Windows-x64/QCSCKP.exe"


def fake_reader_factory(modules, *, corrupt=None, include_pyz=True, include_parents=True):
    names = set(modules)
    if include_parents:
        names.update(name.rsplit(".", 1)[0] for name in modules if "." in name)
    # If executed, this fixture would fail. The verifier may only decode it.
    inert = compile("raise AssertionError('archived module must not execute')", "<fixture-module>", "exec")
    def extract(name):
        if name == corrupt:
            raise EOFError("broken fixture payload")
        return inert
    pyz = SimpleNamespace(toc={name: (0, 0, 1) for name in names}, extract=extract)
    archive = SimpleNamespace(toc={"PYZ.pyz": (0, 1, 1, 0, "z")} if include_pyz else {},
                              open_embedded_archive=lambda name: pyz)
    return lambda path: archive


class RuntimeArchiveVerifierTests(unittest.TestCase):
    def test_literal_manifest_is_read_without_import_or_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.py"
            path.write_text("raise RuntimeError('source must not execute')\n"
                            "RUNTIME_MODULE_MANIFEST = ('utils.first', 'services.second')\n", encoding="utf-8")
            self.assertEqual(("utils.first", "services.second"), verifier.expected_runtime_modules(path))

    def test_manifest_cannot_be_empty_dynamic_duplicated_or_missing(self):
        for source in ("RUNTIME_MODULE_MANIFEST = ()", "RUNTIME_MODULE_MANIFEST = get_secret_config()",
                       "RUNTIME_MODULE_MANIFEST = ('utils.first','utils.first')", "OTHER = ('utils.first',)"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "registry.py"
                path.write_text(source, encoding="utf-8")
                with self.assertRaises(ValueError):
                    verifier.expected_runtime_modules(path)

    def test_all_expected_modules_are_checked_not_just_prune(self):
        expected = ("utils.first", "services.second", "services.third")
        report = verifier.verify_runtime_archive(Path("fake.exe"), expected,
            reader_factory=fake_reader_factory(("utils.first", "services.third")))
        self.assertFalse(report["success"])
        self.assertEqual(["services.second"], report["missing_modules"])
        self.assertEqual(3, report["expected_module_count"])

    def test_present_modules_decode_without_execution(self):
        expected = ("utils.first", "services.second")
        report = verifier.verify_runtime_archive(Path("fake.exe"), expected,
            reader_factory=fake_reader_factory(expected))
        self.assertTrue(report["success"])
        self.assertEqual([], report["invalid_modules"])

    def test_corrupt_payload_and_missing_parent_package_fail(self):
        expected = ("utils.first", "services.second")
        corrupt = verifier.verify_runtime_archive(Path("fake.exe"), expected,
            reader_factory=fake_reader_factory(expected, corrupt="services.second"))
        self.assertFalse(corrupt["success"])
        self.assertEqual([{"module": "services.second", "error_type": "EOFError"}], corrupt["invalid_modules"])
        no_parent = verifier.verify_runtime_archive(Path("fake.exe"), expected,
            reader_factory=fake_reader_factory(expected, include_parents=False))
        self.assertEqual(["services", "utils"], no_parent["missing_parent_packages"])
        self.assertFalse(no_parent["success"])

    def test_absent_pyz_is_rejected_even_if_an_unrelated_toc_exists(self):
        with self.assertRaises(ValueError):
            verifier.verify_runtime_archive(Path("fake.exe"), ("utils.first",),
                reader_factory=fake_reader_factory(("utils.first",), include_pyz=False))

    def test_cli_missing_and_invalid_archive_have_nonzero_exit(self):
        for report, expected_exit in (({"success": False, "missing_modules": ["utils.first"]}, 1),
                                      ({"success": True, "missing_modules": []}, 0)):
            with self.subTest(report=report), patch.object(verifier, "expected_runtime_modules", return_value=("utils.first",)), \
                 patch.object(verifier, "verify_runtime_archive", return_value=report), redirect_stdout(io.StringIO()) as output:
                code = verifier.main(["fake.exe"])
            self.assertEqual(expected_exit, code)
            self.assertEqual(report, json.loads(output.getvalue()))
        with patch.object(verifier, "expected_runtime_modules", side_effect=ValueError("unsafe source detail")), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(2, verifier.main(["fake.exe"]))
        self.assertNotIn("unsafe source detail", output.getvalue())

    def test_registry_lists_eleven_services_and_build_checks_final_exe_before_zip(self):
        modules = verifier.expected_runtime_modules()
        self.assertEqual(11, len(modules))
        self.assertIn("utils.sqlite_prune_scheduler", modules)
        source = (ROOT / "packaging/windows/build_windows.ps1").read_text(encoding="utf-8-sig")
        call = source.index("& $python -B $runtimeArchiveVerifier")
        self.assertLess(source.index("Move-Item -LiteralPath $builtDir -Destination $releaseDir"), call)
        self.assertLess(call, source.index("Invoke-WebRequest"))
        self.assertLess(call, source.index("if ($SkipArchive)"))
        self.assertLess(call, source.index("& tar.exe"))
        self.assertIn('--manifest $runtimeManifestSource', source)
        self.assertIn('Runtime archive verification failed; this build cannot be released.', source)


@unittest.skipUnless(R20_EXE.is_file() and importlib.util.find_spec("PyInstaller"), "Existing local r20 archive is optional")
class ExistingR20ArchiveReadOnlyTests(unittest.TestCase):
    def test_real_r20_archive_reproduces_exact_registry_omission(self):
        report = verifier.verify_runtime_archive(R20_EXE, verifier.expected_runtime_modules())
        self.assertFalse(report["success"])
        self.assertEqual(11, report["expected_module_count"])
        self.assertEqual(["utils.sqlite_prune_scheduler"], report["missing_modules"])
        self.assertEqual([], report["missing_parent_packages"])
        self.assertEqual([], report["invalid_modules"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(1, verifier.main([str(R20_EXE)]))
        self.assertEqual(["utils.sqlite_prune_scheduler"], json.loads(output.getvalue())["missing_modules"])


if __name__ == "__main__":
    unittest.main()

