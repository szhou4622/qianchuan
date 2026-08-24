param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$ZipPath,
    [Parameter(Mandatory = $true)][string]$NotesFile,
    [string]$MinSupportedVersion = "0.1.58",
    [switch]$Force,
    [string]$ServerHost = "124.174.46.12",
    [string]$ServerUser = "root",
    [int]$ServerPort = 22,
    [string]$IdentityFile = "$HOME\.ssh\ProductOperationReport-update-server-ed25519"
)

$ErrorActionPreference = "Stop"
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid version: $Version" }
$zip = (Resolve-Path -LiteralPath $ZipPath).Path
$notes = (Resolve-Path -LiteralPath $NotesFile).Path
$key = (Resolve-Path -LiteralPath $IdentityFile).Path
$sha256 = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
$fileName = "QCSCKP-v$Version-Windows-x64.zip"
if ([IO.Path]::GetFileName($zip) -ne $fileName) {
    throw "Unexpected ZIP name: $([IO.Path]::GetFileName($zip))"
}

$noteLines = @(
    Get-Content -LiteralPath $notes -Encoding UTF8 |
        Where-Object { $_ -match '^\-\s+(.+)$' } |
        ForEach-Object { $Matches[1].Trim() }
)
if ($noteLines.Count -eq 0) { throw "Release notes contain no bullet items" }

$manifest = [ordered]@{
    app_name = "QCSCKP"
    version = $Version
    min_supported_version = $MinSupportedVersion
    download_url = [ordered]@{
        windows_x64 = "https://update.dadaozixun.com/downloads/QCSCKP/$Version/$fileName"
    }
    sha256 = [ordered]@{ windows_x64 = $sha256 }
    notes = $noteLines
    force = [bool]$Force
}

$outputDir = Split-Path -Parent $zip
$manifestPath = Join-Path $outputDir "QCSCKP-v$Version-update-manifest.json"
$manifestJson = $manifest | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText(
    $manifestPath,
    $manifestJson,
    [Text.UTF8Encoding]::new($false)
)

$scp = "$env:SystemRoot\System32\OpenSSH\scp.exe"
$ssh = "$env:SystemRoot\System32\OpenSSH\ssh.exe"
foreach ($tool in @($scp, $ssh)) {
    if (-not (Test-Path -LiteralPath $tool)) { throw "Windows OpenSSH is required" }
}

$remotePrefix = "/tmp/qcsckp-update-$Version"
$remoteZip = "$remotePrefix.zip"
$remoteManifest = "$remotePrefix.json"
$remoteDeploy = "$remotePrefix.sh"
$deployScript = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\packaging\windows\deploy_update_release.sh")).Path

foreach ($pair in @(
    @($zip, $remoteZip),
    @($manifestPath, $remoteManifest),
    @($deployScript, $remoteDeploy)
)) {
    & $scp -i $key -o IdentitiesOnly=yes -o BatchMode=yes -P $ServerPort $pair[0] "${ServerUser}@${ServerHost}:$($pair[1])"
    if ($LASTEXITCODE -ne 0) { throw "Upload failed: $($pair[0])" }
}

$remoteCommand = "set +e; chmod 700 '$remoteDeploy'; bash '$remoteDeploy' '$Version' '$remoteZip' '$remoteManifest' '$sha256'; rc=`$?; rm -f -- '$remoteZip' '$remoteManifest' '$remoteDeploy'; exit `$rc"
& $ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes -p $ServerPort "${ServerUser}@${ServerHost}" $remoteCommand
if ($LASTEXITCODE -ne 0) { throw "Server deployment failed" }

$endpoint = "https://update.dadaozixun.com/api/update/latest?app_name=QCSCKP"
$published = Invoke-RestMethod -Uri $endpoint -Method Get -TimeoutSec 30 -Headers @{ "Cache-Control" = "no-store" }
if ([string]$published.app_name -ne "QCSCKP" -or [string]$published.version -ne $Version) {
    throw "Published manifest mismatch"
}
if ([string]$published.sha256.windows_x64 -ne $sha256) {
    throw "Published SHA256 mismatch"
}
& curl.exe -sS -f -I --max-time 30 ([string]$published.download_url.windows_x64) | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Published ZIP is unavailable" }

Write-Output "MANIFEST_PATH=$manifestPath"
Write-Output "DOWNLOAD_URL=$($published.download_url.windows_x64)"
Write-Output "SHA256=$sha256"
