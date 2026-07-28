[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'qcsckp-test-runtime')
)

$ErrorActionPreference = 'SilentlyContinue'
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$stateFile = Join-Path $RuntimeRoot 'state.json'
if (-not (Test-Path -LiteralPath $stateFile)) {
    Write-Output 'Local test services are not running.'
    exit 0
}
$state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json

if ($state.mariadb_pid) {
    $admin = Join-Path $RuntimeRoot 'mariadb\bin\mariadb-admin.exe'
    $clientIni = Join-Path $RuntimeRoot 'root-client.ini'
    if ((Test-Path -LiteralPath $admin) -and (Test-Path -LiteralPath $clientIni)) {
        & $admin "--defaults-extra-file=$clientIni" shutdown 2>&1 | Out-Null
        Start-Sleep -Milliseconds 500
    }
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$allowedFragments = @($RuntimeRoot.ToLowerInvariant(), $repoRoot.ToLowerInvariant())
foreach ($field in @('tunnel_pid', 'expiry_pid', 'proxy_pid', 'php_pid', 'mariadb_pid')) {
    $pidValue = [int]($state.$field)
    if ($pidValue -le 0) { continue }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue"
    if (-not $processInfo) { continue }
    $commandLine = ([string]$processInfo.CommandLine).ToLowerInvariant()
    $isOwned = $false
    foreach ($fragment in $allowedFragments) {
        if ($commandLine.Contains($fragment)) { $isOwned = $true; break }
    }
    if ($isOwned) {
        Stop-Process -Id $pidValue -Force
    }
}
foreach ($port in @(8788, 8787, 3307)) {
    $listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $port -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $listener) { continue }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if (-not $processInfo) { continue }
    $commandLine = ([string]$processInfo.CommandLine).ToLowerInvariant()
    $isOwned = $false
    foreach ($fragment in $allowedFragments) {
        if ($commandLine.Contains($fragment)) { $isOwned = $true; break }
    }
    if ($isOwned) {
        Stop-Process -Id $listener.OwningProcess -Force
    }
}
Remove-Item -LiteralPath $stateFile -Force
Write-Output 'Local test services stopped. Data, logs, and secrets were preserved.'
