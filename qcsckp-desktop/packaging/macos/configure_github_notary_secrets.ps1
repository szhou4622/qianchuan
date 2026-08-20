#requires -Version 7.0

param(
    [Parameter(Mandatory = $true)]
    [string]$CertificateZip,
    [string]$Repository = "szhou4622/qianchuan"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Set-GitHubSecretFromMemory {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = "gh"
    $start.ArgumentList.Add("secret")
    $start.ArgumentList.Add("set")
    $start.ArgumentList.Add($Name)
    $start.ArgumentList.Add("--repo")
    $start.ArgumentList.Add($Repository)
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($start)
    try {
        $process.StandardInput.Write($Value)
        $process.StandardInput.Close()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            $errorText = $process.StandardError.ReadToEnd()
            throw "GitHub Secret $Name 写入失败：$errorText"
        }
    }
    finally {
        $process.Dispose()
    }
}

$resolvedZip = (Resolve-Path -LiteralPath $CertificateZip).Path
$zip = [System.IO.Compression.ZipFile]::OpenRead($resolvedZip)
try {
    $p12Entry = $zip.Entries |
        Where-Object { $_.FullName.EndsWith(".p12") -and $_.Length -gt 1000 } |
        Select-Object -First 1
    $passwordEntry = $zip.Entries |
        Where-Object { $_.FullName.EndsWith(".md") -and $_.Length -gt 0 -and $_.Length -lt 100 } |
        Select-Object -First 1
    if (-not $p12Entry -or -not $passwordEntry) {
        throw "证书压缩包中未找到有效的 .p12 或对应密码文件。"
    }

    $certificateStream = $p12Entry.Open()
    $certificateMemory = [System.IO.MemoryStream]::new()
    try {
        $certificateStream.CopyTo($certificateMemory)
    }
    finally {
        $certificateStream.Dispose()
    }
    $passwordReader = [System.IO.StreamReader]::new($passwordEntry.Open())
    try {
        $certificatePassword = $passwordReader.ReadToEnd().Trim()
    }
    finally {
        $passwordReader.Dispose()
    }
    if (-not $certificatePassword) {
        throw "证书密码文件为空。"
    }

    $certificateBase64 = [Convert]::ToBase64String($certificateMemory.ToArray())
    Set-GitHubSecretFromMemory -Name "APPLE_CERTIFICATE_P12_BASE64" -Value $certificateBase64
    Set-GitHubSecretFromMemory -Name "APPLE_CERTIFICATE_PASSWORD" -Value $certificatePassword

    $appleId = Read-Host "Apple ID"
    $teamId = Read-Host "Apple Team ID"
    $securePassword = Read-Host "Apple App-Specific Password" -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    try {
        $appPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        Set-GitHubSecretFromMemory -Name "APPLE_ID" -Value $appleId.Trim()
        Set-GitHubSecretFromMemory -Name "APPLE_TEAM_ID" -Value $teamId.Trim()
        Set-GitHubSecretFromMemory -Name "APPLE_APP_SPECIFIC_PASSWORD" -Value $appPassword
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $appPassword = $null
    }
}
finally {
    $zip.Dispose()
}

Write-Host "GitHub 公证 Secret 已写入（仅显示名称，不显示值）："
gh secret list --repo $Repository |
    Select-String "APPLE_CERTIFICATE_P12_BASE64|APPLE_CERTIFICATE_PASSWORD|APPLE_ID|APPLE_TEAM_ID|APPLE_APP_SPECIFIC_PASSWORD"
