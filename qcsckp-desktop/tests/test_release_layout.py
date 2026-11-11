import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_layout_verifier', ROOT / 'packaging/windows/verify_release_layout.py')
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)


class ReleaseLayoutTests(unittest.TestCase):
    def test_short_layout_keeps_runtime_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'QCSCKP-r36'
            file = root / 'bin/playwright/driver/package/cli.js'
            file.parent.mkdir(parents=True)
            file.write_bytes(b'public driver resource')
            result = layout.verify_layout(root, modules=layout.SDK_MODULES)
            self.assertTrue(result['success'])
            self.assertEqual(b'public driver resource', file.read_bytes())
            self.assertLessEqual(result['longest_archive_path_characters'], 110)

    def test_duplicate_sdk_source_fails_even_when_modules_are_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'QCSCKP-r36'
            source = root / 'bin/lark_oapi/api/model.py'
            source.parent.mkdir(parents=True)
            source.touch()
            with self.assertRaisesRegex(ValueError, 'Redundant SDK source'):
                layout.verify_layout(root, modules=layout.SDK_MODULES)

    def test_missing_archived_sdk_is_not_hidden_by_source_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'QCSCKP-r36'
            root.mkdir()
            (root / 'QCSCKP.exe').touch()
            with self.assertRaisesRegex(ValueError, 'Required archived SDK modules missing'):
                layout.verify_layout(root, modules={'lark_oapi'})

    def test_new_long_resource_names_block_a_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'QCSCKP-r36'
            file = root / 'bin/resources' / ('x' * 100 + '.dat')
            file.parent.mkdir(parents=True)
            file.touch()
            with self.assertRaisesRegex(ValueError, 'Archive path exceeds'):
                layout.verify_layout(root, modules=layout.SDK_MODULES)

    def test_probe_does_not_replace_existing_files(self):
        from services import packaged_runtime_probe as probe
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'existing.json'
            report.write_text('keep', encoding='utf-8')
            with patch.object(probe, '_probe') as execute:
                self.assertEqual(2, probe.main([str(report)]))
            execute.assert_not_called()
            self.assertEqual('keep', report.read_text(encoding='utf-8'))

    def test_probe_reports_failure_and_isolates_user_data(self):
        from services import packaged_runtime_probe as probe
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'probe.json'
            with patch.object(probe, '_probe', side_effect=RuntimeError('synthetic failure')):
                self.assertEqual(1, probe.main([str(report)]))
            self.assertEqual('RuntimeError', json.loads(report.read_text())['error_type'])

    def test_normal_entrypoint_keeps_activation_and_profile_flow(self):
        import startup_bootstrap
        with patch.object(sys, 'argv', ['QCSCKP.exe']), patch.object(startup_bootstrap, 'main', return_value=7) as normal:
            self.assertEqual(7, startup_bootstrap._entrypoint())
        normal.assert_called_once()

    def test_explicit_probe_does_not_enter_the_business_runtime(self):
        import startup_bootstrap
        with patch.object(sys, 'argv', ['QCSCKP.exe', '--verify-package-runtime', 'synthetic-report.json']), patch.object(
            startup_bootstrap, 'main'
        ) as normal, patch('services.packaged_runtime_probe.main', return_value=0) as probe:
            self.assertEqual(0, startup_bootstrap._entrypoint())
        normal.assert_not_called()
        probe.assert_called_once_with(['synthetic-report.json'])
