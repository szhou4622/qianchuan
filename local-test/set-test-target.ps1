[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]+$')][string]$Aavid,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]+$')][string]$MaterialId,
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'qcsckp-test-runtime')
)

$ErrorActionPreference = 'Stop'
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
$targetFile = Join-Path $RuntimeRoot 'test-target.json'
$payload = [ordered]@{
    aavid = $Aavid
    material_id = $MaterialId
    updated_at = (Get-Date).ToString('s')
} | ConvertTo-Json
[IO.File]::WriteAllText($targetFile, $payload, (New-Object Text.UTF8Encoding($false)))
Write-Output "Local test target locked: account=$Aavid material=$MaterialId"
