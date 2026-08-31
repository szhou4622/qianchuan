param([Parameter(Mandatory=$true)][string]$ContextFile,[switch]$SkipRestart)
$ErrorActionPreference = 'Stop'
$ctx = Get-Content -LiteralPath $ContextFile -Raw | ConvertFrom-Json
$installRoot = [IO.Path]::GetFullPath([string]$ctx.root)
$stageRoot = [IO.Path]::GetFullPath([string]$ctx.stage)
$payloadRoot = [IO.Path]::GetFullPath([string]$ctx.payload)
$allowedStage = $installRoot.TrimEnd('\') + '\.qcsckp-update\'
if (!$stageRoot.StartsWith($allowedStage,[StringComparison]::OrdinalIgnoreCase) -or !$payloadRoot.StartsWith($stageRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe update paths' }
if (!(Test-Path -LiteralPath (Join-Path $installRoot 'QCSCKP.exe')) -or !(Test-Path -LiteralPath (Join-Path $payloadRoot 'PACKAGE-MANIFEST.json'))) { throw 'Invalid installation roots' }
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
        $newProcess = Start-Process -FilePath (Join-Path $installRoot 'QCSCKP.exe') -WorkingDirectory $installRoot -PassThru
        if ($newProcess.WaitForExit(15000) -and $newProcess.ExitCode -ne 0) { throw 'New version failed at startup' }
    }
    'Update completed. previous-version retained for recovery.' | Set-Content -LiteralPath (Join-Path $stageRoot 'result.txt')
} catch {
    foreach ($name in $installed) {
        $item = Join-Path $installRoot $name
        if (Test-Path -LiteralPath $item) { Move-Item -LiteralPath $item -Destination (Join-Path $failed $name) }
    }
    foreach ($name in $moved) { Move-Item -LiteralPath (Join-Path $backup $name) -Destination (Join-Path $installRoot $name) }
    'Update failed; previous files restored.' | Set-Content -LiteralPath (Join-Path $stageRoot 'result.txt')
    if (!$SkipRestart) { Start-Process -FilePath (Join-Path $installRoot 'QCSCKP.exe') -WorkingDirectory $installRoot }
    throw
}
