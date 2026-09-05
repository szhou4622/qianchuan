param([Parameter(Mandatory=$true)][string]$ContextFile,[switch]$SkipRestart)
$ErrorActionPreference = 'Stop'
$ctx = Get-Content -LiteralPath $ContextFile -Raw -Encoding UTF8 | ConvertFrom-Json
$installRoot = [IO.Path]::GetFullPath([string]$ctx.root)
$stageRoot = [IO.Path]::GetFullPath([string]$ctx.stage)
$payloadRoot = [IO.Path]::GetFullPath([string]$ctx.payload)
$allowedStage = $installRoot.TrimEnd('\') + '\.qcsckp-update\'
if (!$stageRoot.StartsWith($allowedStage,[StringComparison]::OrdinalIgnoreCase) -or !$payloadRoot.StartsWith($stageRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe update paths' }
if (!(Test-Path -LiteralPath (Join-Path $installRoot 'QCSCKP.exe')) -or !(Test-Path -LiteralPath (Join-Path $payloadRoot 'PACKAGE-MANIFEST.json'))) { throw 'Invalid installation roots' }
$manifest = Get-Content -LiteralPath (Join-Path $payloadRoot 'PACKAGE-MANIFEST.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$manifest.app_name -ne 'QCSCKP' -or [string]$manifest.channel -notin @('production','development','stable')) { throw 'Invalid update manifest identity' }
if ([int]$ctx.old_pid -gt 0) {
    $deadline = (Get-Date).AddSeconds(60)
    while (Get-Process -Id ([int]$ctx.old_pid) -ErrorAction SilentlyContinue) {
        if ((Get-Date) -ge $deadline) { throw 'Original process did not exit; no files replaced' }
        Start-Sleep -Milliseconds 250
    }
}
$backup = Join-Path $stageRoot 'previous-version'
$failed = Join-Path $stageRoot 'failed-version'
New-Item -ItemType Directory -Path $backup,$failed | Out-Null
$names = @('QCSCKP.exe','bin','runtime','PACKAGE-MANIFEST.json','VERSION.txt','README-Windows.txt','QCSCKP-Startup-Diagnostics.cmd','QCSCKP-License-Repair.cmd')
$moved = [Collections.Generic.List[string]]::new()
$installed = [Collections.Generic.List[string]]::new()
$newProcess = $null
try {
    foreach ($name in $names) {
        $old = Join-Path $installRoot $name
        $next = Join-Path $payloadRoot $name
        if (!(Test-Path -LiteralPath $next)) { continue }
        if (Test-Path -LiteralPath $old) { Move-Item -LiteralPath $old -Destination (Join-Path $backup $name); $moved.Add($name) }
        Move-Item -LiteralPath $next -Destination $old
        $installed.Add($name)
    }
    if (!$SkipRestart) {
        $launchStartedUnix = ([DateTime]::UtcNow - [DateTime]::new(1970,1,1,0,0,0,[DateTimeKind]::Utc)).TotalSeconds
        $newProcess = Start-Process -FilePath (Join-Path $installRoot 'QCSCKP.exe') -WorkingDirectory $installRoot -WindowStyle Hidden -PassThru
        # PowerShell variables are case-insensitive; $HOME is read-only.
        $runtimeHome = if ($env:QCSCKP_HOME) { [IO.Path]::GetFullPath($env:QCSCKP_HOME) } else { Join-Path $env:LOCALAPPDATA 'QCSCKP' }
        $stateFile = Join-Path $runtimeHome ("channels\{0}\startup-state\{1}.json" -f [string]$manifest.channel,$newProcess.Id)
        $expectedExe = [IO.Path]::GetFullPath((Join-Path $installRoot 'QCSCKP.exe'))
        $ready = $false
        $readyDeadline = (Get-Date).AddSeconds(40)
        while ((Get-Date) -lt $readyDeadline) {
            if (!(Get-Process -Id $newProcess.Id -ErrorAction SilentlyContinue)) { throw 'New version exited before ready' }
            if (Test-Path -LiteralPath $stateFile) {
                try { $state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $state = $null }
                $currentAttempt = $state -and [int]$state.pid -eq $newProcess.Id -and [double]$state.updated_unix -ge $launchStartedUnix
                if ($currentAttempt -and [string]$state.phase -in @('failed','window_timeout')) { throw ("New version startup failed: {0}" -f [string]$state.phase) }
                if ($state -and [string]$state.phase -eq 'ready') {
                    $stateExe = [IO.Path]::GetFullPath([string]$state.executable)
                    $identityMatches = (
                        [int]$state.pid -eq $newProcess.Id -and
                        $stateExe.Equals($expectedExe,[StringComparison]::OrdinalIgnoreCase) -and
                        [string]$state.version -eq [string]$manifest.version -and
                        [string]$state.channel -eq [string]$manifest.channel -and
                        [int]$state.build_revision -eq [int]$manifest.build_revision -and
                        [double]$state.updated_unix -ge $launchStartedUnix
                    )
                    if ($identityMatches) { $ready = $true; break }
                }
            }
            Start-Sleep -Milliseconds 250
        }
        if (!$ready) { throw 'New version did not reach ready state; rolling back' }
    }
    'Update completed. previous-version retained for recovery.' | Set-Content -LiteralPath (Join-Path $stageRoot 'result.txt') -Encoding UTF8
} catch {
    if ($newProcess -and (Get-Process -Id $newProcess.Id -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $newProcess.Id -Force -ErrorAction SilentlyContinue
        $newProcess.WaitForExit(10000) | Out-Null
    }
    foreach ($name in $installed) {
        $item = Join-Path $installRoot $name
        if (Test-Path -LiteralPath $item) { Move-Item -LiteralPath $item -Destination (Join-Path $failed $name) }
    }
    foreach ($name in $moved) { Move-Item -LiteralPath (Join-Path $backup $name) -Destination (Join-Path $installRoot $name) }
    'Update failed; previous files restored.' | Set-Content -LiteralPath (Join-Path $stageRoot 'result.txt') -Encoding UTF8
    if (!$SkipRestart) { Start-Process -FilePath (Join-Path $installRoot 'QCSCKP.exe') -WorkingDirectory $installRoot -WindowStyle Hidden }
    throw
}
