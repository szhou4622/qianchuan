"""Run the native script against disposable files and process-command stubs only."""
import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell 5.1 regression')
class WindowsUpdaterReadyTests(unittest.TestCase):
    def _run(self, outcome):
        with tempfile.TemporaryDirectory(prefix='qcsckp-updater-') as temp:
            root = Path(temp) / '中文 安装目录'
            stage = root / '.qcsckp-update' / 'isolated'
            payload = stage / 'unpacked' / '新版目录'
            home = Path(temp) / '中文 用户数据'
            (root / 'bin').mkdir(parents=True)
            (payload / 'bin').mkdir(parents=True)
            (root / 'QCSCKP.exe').write_bytes(b'old-exe')
            (root / 'bin' / 'value').write_bytes(b'old-bin')
            (payload / 'QCSCKP.exe').write_bytes(b'new-exe')
            (payload / 'bin' / 'value').write_bytes(b'new-bin')
            manifest = {'app_name': 'QCSCKP', 'channel': 'production', 'version': '0.1.66', 'build_revision': 18}
            (payload / 'PACKAGE-MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
            context = stage / 'context.json'
            context.write_text(json.dumps({'root': str(root), 'stage': str(stage), 'payload': str(payload), 'old_pid': 0}, ensure_ascii=False), encoding='utf-8')
            helper = Path(__file__).resolve().parents[1] / 'packaging/windows/apply_channel_update.ps1'
            # All process operations are shadowed. No fixture executable runs.
            script = r'''
$ErrorActionPreference = 'Stop'
$global:starts = 0
$global:checks = 0
$global:stopped = $false
function global:Start-Process {
    param($FilePath,$WorkingDirectory,$WindowStyle,[switch]$PassThru)
    if ($WindowStyle -ne 'Hidden') { throw 'Expected hidden launch' }
    $global:starts++
    if (!$PassThru) { return }
    $fake = [pscustomobject]@{Id=424242}
    $fake | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($timeout) return $true }
    $stateRoot = Join-Path $env:QCSCKP_HOME 'channels\production\startup-state'
    New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
    $phase = if ($env:QCSCKP_TEST_OUTCOME -eq 'failed') { 'failed' } else { 'ready' }
    $stamp = ([DateTime]::UtcNow - [DateTime]::new(1970,1,1,0,0,0,[DateTimeKind]::Utc)).TotalSeconds
    if ($env:QCSCKP_TEST_OUTCOME -eq 'stale') { $stamp -= 600 }
    $revision = if ($env:QCSCKP_TEST_OUTCOME -eq 'wrong_identity') { 17 } else { 18 }
    $state = @{pid=424242; executable=$FilePath; phase=$phase; version='0.1.66'; channel='production'; build_revision=$revision; updated_unix=$stamp}
    # Bootstrap writes BOM-less UTF-8, including the Chinese executable path.
    [IO.File]::WriteAllText((Join-Path $stateRoot '424242.json'),($state | ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    return $fake
}
function global:Get-Process {
    param($Id,$ErrorAction)
    $global:checks++
    if (!$global:stopped -and $global:checks -le 2) { return [pscustomobject]@{Id=$Id} }
}
function global:Stop-Process { param($Id,[switch]$Force,$ErrorAction) $global:stopped=$true }
function global:Start-Sleep { param($Milliseconds) }
try {
    & $env:QCSCKP_TEST_HELPER -ContextFile $env:QCSCKP_TEST_CONTEXT
    exit 0
} catch {
    Write-Output $_.Exception.Message
    exit 7
}
'''
            env = dict(os.environ, QCSCKP_HOME=str(home), QCSCKP_TEST_OUTCOME=outcome,
                       QCSCKP_TEST_HELPER=str(helper), QCSCKP_TEST_CONTEXT=str(context))
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                 '-EncodedCommand', base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
                env=env, capture_output=True, timeout=20,
            )
            if outcome == 'ready':
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(b'new-exe', (root / 'QCSCKP.exe').read_bytes())
                self.assertEqual(b'old-exe', (stage / 'previous-version' / 'QCSCKP.exe').read_bytes())
                self.assertIn('Update completed', (stage / 'result.txt').read_text(encoding='utf-8-sig'))
            else:
                self.assertEqual(7, result.returncode, result.stdout + result.stderr)
                self.assertEqual(b'old-exe', (root / 'QCSCKP.exe').read_bytes())
                self.assertEqual(b'old-bin', (root / 'bin' / 'value').read_bytes())
                self.assertEqual(b'new-exe', (stage / 'failed-version' / 'QCSCKP.exe').read_bytes())
                self.assertIn('previous files restored', (stage / 'result.txt').read_text(encoding='utf-8-sig'))

    def test_current_ready_succeeds_in_chinese_paths(self):
        self._run('ready')

    def test_startup_failure_restores_previous_version(self):
        self._run('failed')

    def test_stale_ready_does_not_accept_reused_pid(self):
        self._run('stale')

    def test_wrong_release_identity_rolls_back(self):
        self._run('wrong_identity')
