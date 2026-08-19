param(
    [string]$Version = "0.1.54"
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "The Windows release must be built on Windows."
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $scriptDir "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pyinstaller = Join-Path $projectRoot ".venv\Scripts\pyinstaller.exe"
$entry = Join-Path $projectRoot "gui_app.py"
$icon = Join-Path $projectRoot "logo.ico"
$staticDir = Join-Path $projectRoot "static"
$usageFile = Join-Path $scriptDir "README-Windows.txt"
$privacyVerifier = Join-Path $scriptDir "verify_release_privacy.py"

foreach ($required in @($python, $pyinstaller, $entry, $icon, $staticDir, $usageFile, $privacyVerifier)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required build input does not exist: $required"
    }
}

$outputRoot = Join-Path $projectRoot "output\windows\v$Version"
$distRoot = Join-Path $outputRoot "dist"
$workRoot = Join-Path $outputRoot "build"
$specRoot = Join-Path $outputRoot "spec"

$safeOutputParent = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "output\windows"))
$resolvedOutput = [System.IO.Path]::GetFullPath($outputRoot)
if (-not $resolvedOutput.StartsWith($safeOutputParent, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to clean an unexpected path: $resolvedOutput"
}
if (Test-Path -LiteralPath $outputRoot) {
    Remove-Item -LiteralPath $outputRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $distRoot, $workRoot, $specRoot -Force | Out-Null

$appName = "QCSCKP"
$pyinstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onedir",
    "--contents-directory", "bin",
    "--name", $appName,
    "--icon", $icon,
    "--add-data", "$staticDir;static",
    "--collect-all", "playwright",
    "--collect-all", "webview",
    "--collect-all", "lark_oapi",
    "--collect-all", "baseopensdk",
    "--collect-all", "pystray",
    "--collect-all", "PIL",
    "--hidden-import", "webview.platforms.edgechromium",
    "--distpath", $distRoot,
    "--workpath", $workRoot,
    "--specpath", $specRoot,
    $entry
)

Push-Location $projectRoot
try {
    & $pyinstaller @pyinstallerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$builtDir = Join-Path $distRoot $appName
$releaseName = "$appName-v$Version-Windows-x64"
$releaseDir = Join-Path $distRoot $releaseName
if (-not (Test-Path -LiteralPath $builtDir)) {
    throw "PyInstaller output directory was not found: $builtDir"
}
if (Test-Path -LiteralPath $releaseDir) {
    Remove-Item -LiteralPath $releaseDir -Recurse -Force
}
Move-Item -LiteralPath $builtDir -Destination $releaseDir
Copy-Item -LiteralPath $usageFile -Destination (Join-Path $releaseDir "README-Windows.txt") -Force

foreach ($writableDir in @("data", "logs", "temp")) {
    New-Item -ItemType Directory -Path (Join-Path $releaseDir $writableDir) -Force | Out-Null
}

# A public package must start with a completely blank local runtime. In
# particular, never ship another Windows user's DPAPI ciphertext: it cannot be
# decrypted on the recipient's computer and would make API setup appear broken.
# Sanitize the fully staged release before zipping. Local Feishu profiles and
# bindings, DPAPI blobs, tokens, cookies, databases, logs, and history are
# removed automatically. The command then verifies that none remain.
& $python $privacyVerifier --sanitize $releaseDir
if ($LASTEXITCODE -ne 0) {
    throw "Release privacy cleanup or verification failed with exit code $LASTEXITCODE"
}

$repoRoot = Split-Path -Parent $projectRoot
$commit = (& git -C $repoRoot rev-parse --short HEAD 2>$null)
$branch = (& git -C $repoRoot branch --show-current 2>$null)
$versionInfo = @(
    "Application version: $Version",
    "Git branch: $branch",
    "Git commit: $commit",
    "Build time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')",
    "Target: Windows x64"
)
Set-Content -LiteralPath (Join-Path $releaseDir "VERSION.txt") -Value $versionInfo -Encoding UTF8

$exePath = Join-Path $releaseDir "$appName.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "The built executable was not found: $exePath"
}

$zipPath = Join-Path $outputRoot "$releaseName.zip"
Push-Location $distRoot
try {
    & tar.exe -a -c -f $zipPath $releaseName
    if ($LASTEXITCODE -ne 0) {
        throw "ZIP creation failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumPath = "$zipPath.sha256.txt"
Set-Content -LiteralPath $checksumPath -Value "$zipHash  $([System.IO.Path]::GetFileName($zipPath))" -Encoding ASCII

Write-Output "RELEASE_DIR=$releaseDir"
Write-Output "EXE_PATH=$exePath"
Write-Output "ZIP_PATH=$zipPath"
Write-Output "ZIP_SHA256=$zipHash"
Write-Output "CHECKSUM_PATH=$checksumPath"
