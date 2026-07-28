[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'qcsckp-test-runtime'),
    [switch]$WithTunnel
)

$ErrorActionPreference = 'Stop'
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$apiRoot = Join-Path $repoRoot 'qcsckp-api-services'
$stateFile = Join-Path $RuntimeRoot 'state.json'
$secretsFile = Join-Path $RuntimeRoot 'secrets.local.json'
$logsDir = Join-Path $RuntimeRoot 'logs'
$dbData = Join-Path $RuntimeRoot 'mariadb-data'
$phpExe = Join-Path $RuntimeRoot 'php\php.exe'
$mariaHome = Join-Path $RuntimeRoot 'mariadb'
$mariaInstall = Join-Path $mariaHome 'bin\mariadb-install-db.exe'
$mariaServer = Join-Path $mariaHome 'bin\mariadbd.exe'
$mariaClient = Join-Path $mariaHome 'bin\mariadb.exe'
$cloudflared = Join-Path $RuntimeRoot 'cloudflared.exe'
$pythonExe = Join-Path $repoRoot 'qcsckp-desktop\.venv\Scripts\python.exe'

& (Join-Path $PSScriptRoot 'bootstrap-runtime.ps1') -RuntimeRoot $RuntimeRoot
& (Join-Path $PSScriptRoot 'stop-local.ps1') -RuntimeRoot $RuntimeRoot | Out-Null
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

function New-HexSecret([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($buffer)).Replace('-', '').ToLowerInvariant()
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Wait-TcpPort([int]$Port, [int]$Seconds = 30) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $client = New-Object Net.Sockets.TcpClient
        try {
            $client.Connect('127.0.0.1', $Port)
            return
        } catch {
            Start-Sleep -Milliseconds 300
        } finally {
            $client.Dispose()
        }
    }
    throw "Local port $Port did not start in time."
}

if (-not (Test-Path -LiteralPath $secretsFile)) {
    $secrets = [ordered]@{
        db_root_password = New-HexSecret 24
        db = [ordered]@{
            host = '127.0.0.1'
            port = 3307
            name = 'qcsckp_local_test'
            user = 'qcsckp_local'
            pass = New-HexSecret 24
            charset = 'utf8mb4'
        }
        test_account = [ordered]@{
            username = 'local_test'
            password = New-HexSecret 12
        }
        feishu_app = [ordered]@{
            enabled = $true
            mock = $true
            app_id = ''
            app_secret = ''
            verification_token = New-HexSecret 24
            encrypt_key = New-HexSecret 24
            authorized_open_id = 'mock_authorized_open_id'
            chat_ids = @('mock_test_chat')
            open_ids = @('mock_test_person')
        }
    }
    Write-Utf8NoBom $secretsFile ($secrets | ConvertTo-Json -Depth 8)
    & icacls $secretsFile /inheritance:r /grant:r "$($env:USERNAME):(F)" 2>&1 | Out-Null
}
$secrets = Get-Content -LiteralPath $secretsFile -Raw -Encoding UTF8 | ConvertFrom-Json

$myIni = Join-Path $RuntimeRoot 'my.ini'
$rootClientIni = Join-Path $RuntimeRoot 'root-client.ini'
$myIniText = @"
[mysqld]
basedir=$($mariaHome.Replace('\', '/'))
datadir=$($dbData.Replace('\', '/'))
port=3307
bind-address=127.0.0.1
skip-name-resolve
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
max_connections=50
"@
$rootClientText = @"
[client]
host=127.0.0.1
port=3307
user=root
password=$($secrets.db_root_password)
protocol=tcp
"@
Write-Utf8NoBom $myIni $myIniText
Write-Utf8NoBom $rootClientIni $rootClientText
& icacls $rootClientIni /inheritance:r /grant:r "$($env:USERNAME):(F)" 2>&1 | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $dbData 'mysql'))) {
    New-Item -ItemType Directory -Path $dbData -Force | Out-Null
    & $mariaInstall "--datadir=$dbData" "--password=$($secrets.db_root_password)" `
        '--port=3307' '--allow-remote-root-access' 2>&1 |
        Out-File -LiteralPath (Join-Path $logsDir 'mariadb-install.log') -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw 'MariaDB initialization failed. Check local logs.' }
}

$state = [ordered]@{
    started_at = (Get-Date).ToString('s')
    runtime_root = $RuntimeRoot
    api_url = 'http://127.0.0.1:8787'
    callback_local_url = 'http://127.0.0.1:8788/api/feishu/card_callback.php'
    callback_https_url = ''
    mariadb_pid = 0
    php_pid = 0
    proxy_pid = 0
    expiry_pid = 0
    tunnel_pid = 0
}
function Save-State {
    Write-Utf8NoBom $stateFile ($state | ConvertTo-Json -Depth 5)
}

try {
    $mariaProcess = Start-Process -FilePath $mariaServer `
        -ArgumentList "--defaults-file=$myIni", '--console' `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logsDir 'mariadb.out.log') `
        -RedirectStandardError (Join-Path $logsDir 'mariadb.err.log')
    $state.mariadb_pid = $mariaProcess.Id
    Save-State
    Wait-TcpPort 3307 45

    $env:QCSCKP_SEED_PASSWORD = [string]$secrets.test_account.password
    $passwordHash = & $phpExe (Join-Path $PSScriptRoot 'hash-password.php')
    Remove-Item Env:QCSCKP_SEED_PASSWORD
    if (-not $passwordHash) { throw 'Unable to hash the local test password.' }
    $schema = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'schema.sql') -Raw -Encoding UTF8
    $schema = $schema.Replace('{{DB_NAME}}', [string]$secrets.db.name)
    $schema = $schema.Replace('{{DB_PASSWORD}}', [string]$secrets.db.pass)
    $schema = $schema.Replace('{{ACCOUNT_PASSWORD_HASH}}', [string]$passwordHash)
    $bootstrapSql = Join-Path $RuntimeRoot 'bootstrap.sql'
    Write-Utf8NoBom $bootstrapSql $schema
    Get-Content -LiteralPath $bootstrapSql -Raw -Encoding UTF8 |
        & $mariaClient "--defaults-extra-file=$rootClientIni"
    $schemaExit = $LASTEXITCODE
    Remove-Item -LiteralPath $bootstrapSql -Force
    if ($schemaExit -ne 0) { throw 'Local test database schema creation failed.' }

    $oldServerConfig = $env:QCSCKP_SERVER_CONFIG
    $oldSecrets = $env:QCSCKP_LOCAL_SECRETS
    $oldServerTest = $env:QCSCKP_SERVER_TEST_MODE
    $env:QCSCKP_SERVER_CONFIG = Join-Path $PSScriptRoot 'config.local.php'
    $env:QCSCKP_LOCAL_SECRETS = $secretsFile
    $env:QCSCKP_SERVER_TEST_MODE = '1'

    $phpProcess = Start-Process -FilePath $phpExe `
        -ArgumentList '-S', '127.0.0.1:8787', '-t', $apiRoot `
        -WorkingDirectory $apiRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logsDir 'php-api.out.log') `
        -RedirectStandardError (Join-Path $logsDir 'php-api.err.log')
    $state.php_pid = $phpProcess.Id
    Save-State
    Wait-TcpPort 8787 30

    if (-not (Test-Path -LiteralPath $pythonExe)) {
        $pythonExe = (Get-Command python).Source
    }
    $proxyProcess = Start-Process -FilePath $pythonExe `
        -ArgumentList (Join-Path $PSScriptRoot 'callback_proxy.py') `
        -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logsDir 'callback-proxy.out.log') `
        -RedirectStandardError (Join-Path $logsDir 'callback-proxy.err.log')
    $state.proxy_pid = $proxyProcess.Id
    Save-State
    Wait-TcpPort 8788 30
    $proxyListener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8788 -State Listen |
        Select-Object -First 1
    if ($proxyListener) {
        $state.proxy_pid = [int]$proxyListener.OwningProcess
        Save-State
    }

    $expiryProcess = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $PSScriptRoot 'expire-loop.ps1'), '-PhpExe', $phpExe,
            '-CronFile', (Join-Path $apiRoot 'cron\expire_retarget_tasks.php'),
            '-ServerConfig', (Join-Path $PSScriptRoot 'config.local.php'),
            '-LocalSecrets', $secretsFile, '-ServerTestMode' `
        -WindowStyle Hidden -PassThru
    $state.expiry_pid = $expiryProcess.Id
    Save-State

    $env:QCSCKP_SERVER_CONFIG = $oldServerConfig
    $env:QCSCKP_LOCAL_SECRETS = $oldSecrets
    $env:QCSCKP_SERVER_TEST_MODE = $oldServerTest

    if ($WithTunnel) {
        $tunnelOut = Join-Path $logsDir 'cloudflared.out.log'
        $tunnelErr = Join-Path $logsDir 'cloudflared.err.log'
        $tunnelProcess = Start-Process -FilePath $cloudflared `
            -ArgumentList 'tunnel', '--url', 'http://127.0.0.1:8788', '--no-autoupdate' `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $tunnelOut `
            -RedirectStandardError $tunnelErr
        $state.tunnel_pid = $tunnelProcess.Id
        Save-State
        $deadline = (Get-Date).AddSeconds(45)
        $tunnelUrl = ''
        while ((Get-Date) -lt $deadline -and $tunnelUrl -eq '') {
            Start-Sleep -Milliseconds 500
            $combined = ''
            foreach ($logFile in @($tunnelOut, $tunnelErr)) {
                if (Test-Path -LiteralPath $logFile) {
                    $combined += Get-Content -LiteralPath $logFile -Raw -Encoding UTF8
                }
            }
            if ($combined -match 'https://[a-zA-Z0-9-]+\.trycloudflare\.com') {
                $tunnelUrl = $Matches[0]
            }
        }
        if ($tunnelUrl -eq '') { throw 'Temporary HTTPS callback URL timed out.' }
        $state.callback_https_url = "$tunnelUrl/api/feishu/card_callback.php"
        Save-State
    }

    $probeBody = @{ username = 'invalid'; password = 'invalid' } | ConvertTo-Json
    try {
        Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/device/session.php' `
            -Method Post -ContentType 'application/json' -Body $probeBody -UseBasicParsing | Out-Null
    } catch {
        if ($_.Exception.Response.StatusCode.value__ -ne 401) { throw }
    }

    Write-Output 'Local API: http://127.0.0.1:8787'
    Write-Output 'Test database: qcsckp_local_test (127.0.0.1:3307 only)'
    if ($state.callback_https_url) {
        Write-Output "Feishu callback: $($state.callback_https_url)"
    } else {
        Write-Output 'HTTPS callback tunnel is off; mock tests do not need it.'
    }
} catch {
    & (Join-Path $PSScriptRoot 'stop-local.ps1') -RuntimeRoot $RuntimeRoot | Out-Null
    throw
}
