[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'qcsckp-test-runtime'),
    [int]$ApiPort = 8791,
    [int]$CallbackPort = 8792
)

$ErrorActionPreference = 'Stop'
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ciRoot = [IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'ci-runtime'))
if (-not $ciRoot.StartsWith($RuntimeRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'CI runtime directory escaped the local test runtime.'
}
$phpExe = Join-Path $RuntimeRoot 'php\php.exe'
$mariaClient = Join-Path $RuntimeRoot 'mariadb\bin\mariadb.exe'
$rootClientIni = Join-Path $RuntimeRoot 'root-client.ini'
$pythonExe = Join-Path $repoRoot 'qcsckp-desktop\.venv\Scripts\python.exe'
$baseSecretsPath = Join-Path $RuntimeRoot 'secrets.local.json'
$ciSecretsPath = Join-Path $ciRoot 'secrets.local.json'
$apiRoot = Join-Path $repoRoot 'qcsckp-api-services'
$configPath = Join-Path $PSScriptRoot 'config.local.php'
$schemaPath = Join-Path $PSScriptRoot 'schema.sql'
$apiBase = "http://127.0.0.1:$ApiPort"
$callbackUrl = "http://127.0.0.1:$CallbackPort/api/feishu/card_callback.php"
$phpProcess = $null
$proxyProcess = $null
$exitCode = 1

function New-HexSecret([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($buffer)).Replace('-', '').ToLowerInvariant()
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Wait-TcpPort([int]$Port, [int]$Seconds = 20) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $client = New-Object Net.Sockets.TcpClient
        try {
            $client.Connect('127.0.0.1', $Port)
            return
        } catch {
            Start-Sleep -Milliseconds 200
        } finally {
            $client.Dispose()
        }
    }
    throw "Local integration port $Port did not start."
}

if (-not (Test-Path -LiteralPath $baseSecretsPath)) {
    throw 'Start the isolated local runtime once before running integration tests.'
}
if (-not (Test-Path -LiteralPath $phpExe) -or -not (Test-Path -LiteralPath $mariaClient)) {
    throw 'Portable PHP or MariaDB is missing from the local test runtime.'
}
if (Test-Path -LiteralPath $ciRoot) {
    Remove-Item -LiteralPath $ciRoot -Recurse -Force
}
New-Item -ItemType Directory -Path (Join-Path $ciRoot 'logs') -Force | Out-Null

$baseSecrets = Get-Content -LiteralPath $baseSecretsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$ciSecrets = [ordered]@{
    db = [ordered]@{
        host = [string]$baseSecrets.db.host
        port = [int]$baseSecrets.db.port
        name = 'qcsckp_local_ci'
        user = [string]$baseSecrets.db.user
        pass = [string]$baseSecrets.db.pass
        charset = 'utf8mb4'
    }
    test_account = [ordered]@{
        username = 'local_test'
        password = [string]$baseSecrets.test_account.password
    }
    feishu_app = [ordered]@{
        enabled = $true
        mock = $true
        app_id = 'cli_local_ci'
        app_secret = New-HexSecret 24
        verification_token = New-HexSecret 24
        encrypt_key = New-HexSecret 24
        authorized_open_id = 'mock_authorized_open_id'
        chat_ids = @('mock_test_chat')
        open_ids = @('mock_test_person')
    }
}
Write-Utf8NoBom $ciSecretsPath ($ciSecrets | ConvertTo-Json -Depth 8)

$oldSeed = $env:QCSCKP_SEED_PASSWORD
$oldServerConfig = $env:QCSCKP_SERVER_CONFIG
$oldSecrets = $env:QCSCKP_LOCAL_SECRETS
$oldServerTest = $env:QCSCKP_SERVER_TEST_MODE
try {
    $env:QCSCKP_SEED_PASSWORD = [string]$ciSecrets.test_account.password
    $passwordHash = & $phpExe (Join-Path $PSScriptRoot 'hash-password.php')
    if (-not $passwordHash) { throw 'Unable to hash the isolated test password.' }
    $schema = Get-Content -LiteralPath $schemaPath -Raw -Encoding UTF8
    $schema = $schema.Replace('{{DB_NAME}}', [string]$ciSecrets.db.name)
    $schema = $schema.Replace('{{DB_PASSWORD}}', [string]$ciSecrets.db.pass)
    $schema = $schema.Replace('{{ACCOUNT_PASSWORD_HASH}}', [string]$passwordHash)
    $bootstrapSql = Join-Path $ciRoot 'bootstrap.sql'
    Write-Utf8NoBom $bootstrapSql $schema
    Get-Content -LiteralPath $bootstrapSql -Raw -Encoding UTF8 |
        & $mariaClient "--defaults-extra-file=$rootClientIni"
    if ($LASTEXITCODE -ne 0) { throw 'Isolated test database schema creation failed.' }
    Remove-Item -LiteralPath $bootstrapSql -Force

    $env:QCSCKP_SERVER_CONFIG = $configPath
    $env:QCSCKP_LOCAL_SECRETS = $ciSecretsPath
    $env:QCSCKP_SERVER_TEST_MODE = '1'

    $phpProcess = Start-Process -FilePath $phpExe `
        -ArgumentList '-S', "127.0.0.1:$ApiPort", '-t', $apiRoot `
        -WorkingDirectory $apiRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $ciRoot 'logs\php.out.log') `
        -RedirectStandardError (Join-Path $ciRoot 'logs\php.err.log')
    Wait-TcpPort $ApiPort
    $proxyProcess = Start-Process -FilePath $pythonExe `
        -ArgumentList (Join-Path $PSScriptRoot 'callback_proxy.py'), '--listen-port', $CallbackPort, '--upstream-port', $ApiPort `
        -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $ciRoot 'logs\proxy.out.log') `
        -RedirectStandardError (Join-Path $ciRoot 'logs\proxy.err.log')
    Wait-TcpPort $CallbackPort

    & $pythonExe (Join-Path $PSScriptRoot 'run-integration-tests.py') `
        '--runtime-root' $ciRoot '--api-base' $apiBase '--callback-url' $callbackUrl
    if ($LASTEXITCODE -ne 0) { throw 'Isolated local integration suite failed.' }

    & $phpExe (Join-Path $apiRoot 'cron\expire_retarget_tasks.php') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Card update queue worker failed.' }
    $remaining = & $mariaClient "--defaults-extra-file=$rootClientIni" --batch --skip-column-names `
        -e "SELECT COUNT(*) FROM qcsckp_local_ci.retarget_card_update_jobs"
    if ([int]$remaining -ne 0) { throw 'Card update queue did not drain in mock mode.' }

    Invoke-WebRequest -Uri "$apiBase/api/version.php?current_version=0.1.6" -UseBasicParsing | Out-Null
    Write-Output 'Isolated API, callback, queue, database, and version checks passed.'
    $exitCode = 0
} finally {
    $env:QCSCKP_SEED_PASSWORD = $oldSeed
    $env:QCSCKP_SERVER_CONFIG = $oldServerConfig
    $env:QCSCKP_LOCAL_SECRETS = $oldSecrets
    $env:QCSCKP_SERVER_TEST_MODE = $oldServerTest
    foreach ($process in @($proxyProcess, $phpProcess)) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    & $mariaClient "--defaults-extra-file=$rootClientIni" -e "DROP DATABASE IF EXISTS qcsckp_local_ci" 2>&1 | Out-Null
    if (Test-Path -LiteralPath $ciRoot) {
        Remove-Item -LiteralPath $ciRoot -Recurse -Force
    }
}
exit $exitCode
