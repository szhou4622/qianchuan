"""Native long-path regressions, including PowerShell 5.1; never run a fixture EXE."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from services import channel_update as update

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows extended-length paths')
class WindowsLongPathTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='qcsckp-long-path-test-'))
        self.addCleanup(self._cleanup)
        self.long_relative = 'bin/lark_oapi/' + 'a' * 90 + '/' + 'b' * 90 + '/model_' + 'c' * 50 + '.py'

    def _cleanup(self):
        assert self.root.parent.resolve() == Path(tempfile.gettempdir()).resolve()
        assert self.root.name.startswith('qcsckp-long-path-test-')
        shutil.rmtree(update.native_path(self.root))

    def _zip(self, extra=None):
        identity = {'app_name': 'QCSCKP', 'channel': 'production', 'version': '0.1.66', 'build_revision': 35}
        files = {'QCSCKP.exe': b'new-fixture-exe', 'bin/python312.dll': b'fixture-dll',
                 'bin/release.json': json.dumps(identity).encode(), 'bin/static/index.html': b'index',
                 'bin/static/license.html': b'license', self.long_relative: b'public SDK fixture'}
        manifest = {**identity, 'critical_files': [
            {'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for name, data in files.items()]}
        files['PACKAGE-MANIFEST.json'] = json.dumps(manifest).encode()
        if extra:
            files.update(extra)
        archive = self.root / 'package.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            for name, data in files.items():
                package.writestr('QCSCKP-fixture/' + name, data)
        return archive

    def _install(self):
        root = update.native_path(self.root / ('install-' + 'a' * 85) / ('中文-' + 'b' * 85) / "quote's directory")
        (root / 'bin').mkdir(parents=True)
        (root / 'QCSCKP.exe').write_bytes(b'old-fixture-exe')
        (root / 'bin/value').write_bytes(b'old-bin')
        (root / 'data').mkdir()
        (root / 'data/keep.txt').write_bytes(b'never replace user data')
        return root

    def _native_update(self, fail=False):
        install = self._install()
        stage = install / '.qcsckp-update' / ('s' * 32)
        stage.mkdir(parents=True)
        payload = update.safe_extract(self._zip(), stage / 'unpacked')
        context = stage / 'context.json'
        context.write_text(json.dumps({'root': str(install), 'stage': str(stage), 'payload': str(payload), 'old_pid': 0}), encoding='utf-8')
        helper = stage / 'apply.ps1'
        shutil.copy2(ROOT / 'packaging/windows/apply_channel_update.ps1', helper)
        command = update._powershell_helper_command(helper, context, skip_restart=True)
        self.assertGreater(len(str(context)), 260)
        if fail:
            body = base64.b64decode(command[-1]).decode('utf-16-le')
            prefix = "$global:n=0; function global:Move-Item { param($LiteralPath,$Destination) $global:n++; if($global:n -eq 3){throw 'simulated move failure'}; Microsoft.PowerShell.Management\\Move-Item -LiteralPath $LiteralPath -Destination $Destination }; "
            command[-1] = base64.b64encode((prefix + body).encode('utf-16-le')).decode()
        result = subprocess.run(command, capture_output=True, timeout=30)
        if fail:
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b'old-fixture-exe', (install / 'QCSCKP.exe').read_bytes())
            self.assertEqual(b'old-bin', (install / 'bin/value').read_bytes())
            self.assertEqual(b'new-fixture-exe', (stage / 'failed-version/QCSCKP.exe').read_bytes())
        else:
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(b'new-fixture-exe', (install / 'QCSCKP.exe').read_bytes())
            self.assertEqual(b'old-fixture-exe', (stage / 'previous-version/QCSCKP.exe').read_bytes())
            self.assertEqual(b'public SDK fixture', (install / self.long_relative).read_bytes())
        self.assertEqual(b'never replace user data', (install / 'data/keep.txt').read_bytes())

    def test_extract_and_validate_files_beyond_260_characters(self):
        target = self.root / ('x' * 90) / ('y' * 90) / 'unpacked'
        payload = update.safe_extract(self._zip(), target)
        self.assertGreater(len(str(payload / self.long_relative)), 400)
        self.assertEqual(b'public SDK fixture', (payload / self.long_relative).read_bytes())
        self.assertEqual(35, update.validate_payload(payload, 'production')['build_revision'])

    def test_extended_drive_and_unc_paths_remain_canonical(self):
        value = update.native_path(self.root)
        self.assertEqual(value, update.native_path(value))
        self.assertEqual('\\\\?\\UNC\\server\\share\\file', str(update.native_path('\\\\server\\share\\file')))

    def test_unsafe_archive_names_remain_rejected(self):
        for name in ('../../escape', '..\\escape', '/absolute', 'C:/absolute', 'bin/value:stream', 'bin/trailing. '):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    update.safe_extract(self._zip({name: b'bad'}), self.root / 'invalid')
        self.assertFalse((self.root / 'escape').exists())

    def test_real_powershell_update_works_with_deep_unicode_and_quoted_paths(self):
        self._native_update()

    def test_real_powershell_failure_rolls_back_without_touching_data(self):
        self._native_update(fail=True)

    def _unpack(self, destination, archive=None, expected=None):
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                   str(ROOT / 'packaging/windows/QCSCKP-Unpack.ps1'), '-ArchivePath', str(archive or self._zip()),
                   '-Destination', str(destination)]
        if expected:
            command += ['-ExpectedSha256', expected]
        return subprocess.run(command, capture_output=True, timeout=30)

    def test_standalone_unpack_handles_long_names_without_python_on_user_machine(self):
        destination = self.root / ('x' * 90) / ('y' * 90) / '中文 程序'
        archive = self._zip()
        expected = hashlib.sha256(archive.read_bytes()).hexdigest()
        result = self._unpack(destination, archive, expected)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        installed = update.native_path(destination)
        self.assertEqual(b'public SDK fixture', (installed / self.long_relative).read_bytes())
        self.assertEqual(35, update.validate_payload(installed, 'production')['build_revision'])

    def test_standalone_unpack_does_not_overwrite_existing_installation(self):
        install = self._install()
        result = self._unpack(install)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(b'old-fixture-exe', (install / 'QCSCKP.exe').read_bytes())
        self.assertEqual(b'never replace user data', (install / 'data/keep.txt').read_bytes())

    def test_standalone_unpack_rejects_bad_hash_before_installation(self):
        target = self.root / 'bad-hash'
        result = self._unpack(target, expected='0' * 64)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(target.exists())

    def test_standalone_unpack_rejects_traversal(self):
        target = self.root / 'bad-path'
        result = self._unpack(target, self._zip({'../escape': b'bad'}))
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(target.exists())

    def test_cmd_launcher_is_self_contained_without_a_ps1_file(self):
        archive = self._zip()
        archive.rename(self.root / 'QCSCKP-v0.1.66-production-r35-Windows-x64.zip')
        template = (ROOT / 'packaging/windows/QCSCKP-Unpack.cmd').read_text(encoding='utf-8')
        script = (ROOT / 'packaging/windows/QCSCKP-Unpack.ps1').read_text(encoding='utf-8')
        launcher = self.root / 'QCSCKP-Unpack.cmd'
        launcher.write_text(template.replace('@@POWERSHELL_PAYLOAD@@',script),encoding='utf-8')
        env = {**os.environ, 'LOCALAPPDATA': str(self.root / 'isolated-appdata')}
        result = subprocess.run(['cmd.exe','/d','/c',str(launcher)],input=b'\n',env=env,capture_output=True,timeout=30)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        target = update.native_path(self.root / 'isolated-appdata/QCSCKP/programs/production/v0.1.66-r35')
        self.assertEqual(b'public SDK fixture',(target / self.long_relative).read_bytes())


if __name__ == '__main__':
    unittest.main()
