[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'qcsckp-test-runtime'),
    [switch]$ArmLiveRetarget,
    [switch]$AutoStartService,
    [switch]$SelectNewTarget
)

$ErrorActionPreference = 'Stop'
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$desktopRoot = Join-Path $repoRoot 'qcsckp-desktop'
$pythonw = Join-Path $desktopRoot '.venv\Scripts\pythonw.exe'
$python = Join-Path $desktopRoot '.venv\Scripts\python.exe'
$stateFile = Join-Path $RuntimeRoot 'state.json'
$targetFile = Join-Path $RuntimeRoot 'test-target.json'
$dataDir = Join-Path $RuntimeRoot 'desktop-data'
$pidFile = Join-Path $RuntimeRoot 'desktop-test.pid'

function Stop-ManagedProcessTree([int]$RootPid) {
    $pending = New-Object 'System.Collections.Generic.Queue[int]'
    $descendants = New-Object 'System.Collections.Generic.List[int]'
    $pending.Enqueue($RootPid)
    while ($pending.Count -gt 0) {
        $parentPid = $pending.Dequeue()
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$parentPid" -ErrorAction SilentlyContinue
        foreach ($child in $children) {
            $childPid = [int]$child.ProcessId
            $descendants.Add($childPid)
            $pending.Enqueue($childPid)
        }
    }
    for ($i = $descendants.Count - 1; $i -ge 0; $i--) {
        Stop-Process -Id $descendants[$i] -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $RootPid -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path -LiteralPath $stateFile)) {
    throw 'Start the local test services first.'
}
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw 'Desktop Python environment is missing.'
}
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Desktop Python environment is missing.'
}

$aavid = ''
$materialId = ''
if (Test-Path -LiteralPath $targetFile) {
    $target = Get-Content -LiteralPath $targetFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $aavid = [string]$target.aavid
    $materialId = [string]$target.material_id
}
if ($ArmLiveRetarget -and ($aavid -eq '' -or $materialId -eq '')) {
    throw 'Lock a test account and material before arming live retargeting.'
}

$env:QCSCKP_API_BASE_URL = 'http://127.0.0.1:8787'
$env:QCSCKP_DATA_DIR = $dataDir
$env:QCSCKP_TEST_MODE = '1'
$env:QCSCKP_TEST_AAVID = $aavid
$env:QCSCKP_TEST_MATERIAL_ID = $materialId
$env:QCSCKP_ALLOW_LIVE_RETARGET = if ($ArmLiveRetarget) { '1' } else { '0' }
$env:QCSCKP_LOCAL_TEST_SECRETS_FILE = Join-Path $RuntimeRoot 'secrets.local.json'
$env:QCSCKP_AUTO_START_SERVICE = if ($AutoStartService) { '1' } else { '0' }
$env:QCSCKP_AUTO_START_INTERVAL = '600'
$env:QCSCKP_FORCE_TARGET_RESELECT = if ($SelectNewTarget) { '1' } else { '0' }
if ($ArmLiveRetarget) {
    $preflightScript = Join-Path $PSScriptRoot 'check-live-preflight.py'
    # Windows PowerShell 会把原生程序写入 stderr 的普通日志包装成
    # ErrorRecord；在全局 Stop 模式下会提前中断，导致实际上已通过的
    # 预检无法启动桌面端。这里只按进程退出码判断预检结果。
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $preflightOutput = @(& $python $preflightScript 2>&1)
    $preflightExitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldErrorActionPreference
    if ($preflightExitCode -ne 0) {
        throw ("Live retarget preflight rejected:`n" + ($preflightOutput -join [Environment]::NewLine))
    }
    $preflightOutput | Write-Output
    $consumed = Join-Path $dataDir 'live_retarget_consumed.json'
    if (Test-Path -LiteralPath $consumed) {
        Remove-Item -LiteralPath $consumed -Force
    }
}

if (Test-Path -LiteralPath $pidFile) {
    $oldPid = 0
    [void][int]::TryParse((Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue), [ref]$oldPid)
    if ($oldPid -gt 0) {
        $oldProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$oldPid" -ErrorAction SilentlyContinue
        if ($oldProcess -and ([string]$oldProcess.CommandLine).Contains($desktopRoot)) {
            Stop-ManagedProcessTree $oldPid
        }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

$process = Start-Process -FilePath $pythonw `
    -ArgumentList (Join-Path $desktopRoot 'gui_app.py') `
    -WorkingDirectory $desktopRoot -PassThru
[IO.File]::WriteAllText(
    $pidFile,
    [string]$process.Id,
    (New-Object Text.UTF8Encoding($false))
)
Write-Output "Test desktop started. PID=$($process.Id) live_retarget=$($ArmLiveRetarget.IsPresent) auto_start_service=$($AutoStartService.IsPresent) select_new_target=$($SelectNewTarget.IsPresent)"
