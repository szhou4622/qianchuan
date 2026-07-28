[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'qcsckp-test-runtime'),
    [switch]$Reset
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Assert-RuntimePath([string]$Path) {
    $root = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\') + '\'
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside runtime root: $resolved"
    }
}

function Remove-RuntimeItem([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Assert-RuntimePath $Path
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Download-Verified(
    [string]$Url,
    [string]$Destination,
    [string]$Sha256
) {
    if (Test-Path -LiteralPath $Destination) {
        $existing = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($existing -eq $Sha256.ToLowerInvariant()) { return }
        Remove-Item -LiteralPath $Destination -Force
    }
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
    $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        Remove-Item -LiteralPath $Destination -Force
        throw "Download checksum mismatch: $Url"
    }
}

function Export-WindowsRootCaBundle([string]$Destination) {
    $certificates = @(
        Get-ChildItem -Path Cert:\CurrentUser\Root -ErrorAction SilentlyContinue
        Get-ChildItem -Path Cert:\LocalMachine\Root -ErrorAction SilentlyContinue
    ) |
        Where-Object { $_.HasPrivateKey -eq $false } |
        Sort-Object Thumbprint -Unique
    if (-not $certificates) {
        throw 'No trusted Windows root certificates were found.'
    }
    $blocks = foreach ($certificate in $certificates) {
        $base64 = [Convert]::ToBase64String(
            $certificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Cert),
            [Base64FormattingOptions]::InsertLineBreaks
        )
        "-----BEGIN CERTIFICATE-----`r`n$base64`r`n-----END CERTIFICATE-----"
    }
    [IO.File]::WriteAllText(
        $Destination,
        ($blocks -join "`r`n") + "`r`n",
        (New-Object Text.UTF8Encoding($false))
    )
}

$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
if ($Reset) {
    Assert-RuntimePath (Join-Path $RuntimeRoot 'php')
    foreach ($name in @('php', 'mariadb', 'cloudflared.exe', 'downloads')) {
        Remove-RuntimeItem (Join-Path $RuntimeRoot $name)
    }
}
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
$downloads = Join-Path $RuntimeRoot 'downloads'
New-Item -ItemType Directory -Path $downloads -Force | Out-Null

$phpHome = Join-Path $RuntimeRoot 'php'
if (-not (Test-Path -LiteralPath (Join-Path $phpHome 'php.exe'))) {
    $phpReleases = Invoke-RestMethod -Uri 'https://windows.php.net/downloads/releases/releases.json' -UseBasicParsing
    $phpVersionProperty = $phpReleases.PSObject.Properties |
        Where-Object { $_.Name -match '^8\.[0-9]+$' } |
        Sort-Object { [version]$_.Value.version } -Descending |
        Select-Object -First 1
    if (-not $phpVersionProperty) { throw 'Unable to resolve a PHP Windows release.' }
    $phpBuildProperty = $phpVersionProperty.Value.PSObject.Properties |
        Where-Object { $_.Name -match '^nts-.*-x64$' } |
        Select-Object -First 1
    if (-not $phpBuildProperty) { throw 'PHP x64 NTS archive was not found.' }
    $phpArchive = $phpBuildProperty.Value.zip.path
    $phpSha = $phpBuildProperty.Value.zip.sha256
    $phpZip = Join-Path $downloads $phpArchive
    Download-Verified "https://windows.php.net/downloads/releases/$phpArchive" $phpZip $phpSha
    Remove-RuntimeItem $phpHome
    New-Item -ItemType Directory -Path $phpHome -Force | Out-Null
    Expand-Archive -LiteralPath $phpZip -DestinationPath $phpHome -Force
}

$extensionDir = (Join-Path $phpHome 'ext').Replace('\', '/')
$caBundle = Join-Path $RuntimeRoot 'windows-root-ca.pem'
Export-WindowsRootCaBundle $caBundle
$caBundleIni = $caBundle.Replace('\', '/')
$phpIni = @"
[PHP]
extension_dir="$extensionDir"
extension=curl
extension=mbstring
extension=openssl
extension=pdo_mysql
curl.cainfo="$caBundleIni"
openssl.cafile="$caBundleIni"
date.timezone=Asia/Shanghai
display_errors=Off
log_errors=On
error_log="$((Join-Path $RuntimeRoot 'logs/php-errors.log').Replace('\', '/'))"
session.save_path="$((Join-Path $RuntimeRoot 'php-sessions').Replace('\', '/'))"
"@
New-Item -ItemType Directory -Path (Join-Path $RuntimeRoot 'logs') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $RuntimeRoot 'php-sessions') -Force | Out-Null
[IO.File]::WriteAllText(
    (Join-Path $phpHome 'php.ini'),
    $phpIni,
    (New-Object Text.UTF8Encoding($false))
)

$mariaHome = Join-Path $RuntimeRoot 'mariadb'
if (-not (Test-Path -LiteralPath (Join-Path $mariaHome 'bin\mariadb.exe'))) {
    $mariaMeta = Invoke-RestMethod -Uri 'https://downloads.mariadb.org/rest-api/mariadb/10.11/latest/' -UseBasicParsing
    $releaseProperty = $mariaMeta.releases.PSObject.Properties | Select-Object -First 1
    if (-not $releaseProperty) { throw 'Unable to resolve a MariaDB release.' }
    $mariaFile = $releaseProperty.Value.files |
        Where-Object {
            $_.os -eq 'Windows' -and $_.cpu -eq 'x86_64' -and
            $_.package_type -eq 'ZIP file' -and $_.file_name -notmatch 'debug'
        } |
        Select-Object -First 1
    if (-not $mariaFile) { throw 'MariaDB Windows x64 ZIP archive was not found.' }
    $mariaZip = Join-Path $downloads $mariaFile.file_name
    $mariaUrl = ([string]$mariaFile.file_download_url).Replace('http://', 'https://')
    Download-Verified $mariaUrl $mariaZip ([string]$mariaFile.checksum.sha256sum)
    $unpack = Join-Path $RuntimeRoot 'mariadb-unpack'
    Remove-RuntimeItem $unpack
    Remove-RuntimeItem $mariaHome
    Expand-Archive -LiteralPath $mariaZip -DestinationPath $unpack -Force
    $inner = Get-ChildItem -LiteralPath $unpack -Directory | Select-Object -First 1
    if (-not $inner) { throw 'Invalid MariaDB archive layout.' }
    Move-Item -LiteralPath $inner.FullName -Destination $mariaHome
    Remove-RuntimeItem $unpack
}

$cloudflared = Join-Path $RuntimeRoot 'cloudflared.exe'
if (-not (Test-Path -LiteralPath $cloudflared)) {
    $release = Invoke-RestMethod `
        -Uri 'https://api.github.com/repos/cloudflare/cloudflared/releases/latest' `
        -Headers @{ 'User-Agent' = 'qcsckp-local-test' } `
        -UseBasicParsing
    $asset = $release.assets |
        Where-Object { $_.name -eq 'cloudflared-windows-amd64.exe' } |
        Select-Object -First 1
    if (-not $asset -or [string]$asset.digest -notmatch '^sha256:([a-fA-F0-9]{64})$') {
        throw 'A cloudflared Windows asset with SHA256 digest was not found.'
    }
    Download-Verified ([string]$asset.browser_download_url) $cloudflared $Matches[1]
}

Write-Output "Portable runtime is ready: $RuntimeRoot"
