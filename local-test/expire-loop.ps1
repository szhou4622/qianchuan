[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PhpExe,
    [Parameter(Mandatory = $true)][string]$CronFile,
    [Parameter(Mandatory = $true)][string]$ServerConfig,
    [Parameter(Mandatory = $true)][string]$LocalSecrets,
    [switch]$ServerTestMode
)

$ErrorActionPreference = 'Continue'
$env:QCSCKP_SERVER_CONFIG = $ServerConfig
$env:QCSCKP_LOCAL_SECRETS = $LocalSecrets
$env:QCSCKP_SERVER_TEST_MODE = if ($ServerTestMode) { '1' } else { '0' }
while ($true) {
    & $PhpExe $CronFile 2>&1 | Out-Null
    Start-Sleep -Seconds 5
}
