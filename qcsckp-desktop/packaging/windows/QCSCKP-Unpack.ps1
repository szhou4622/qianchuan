[CmdletBinding()]
param([string]$ArchivePath, [string]$Destination, [string]$ExpectedSha256)
$ErrorActionPreference = 'Stop'
[AppContext]::SetSwitch('Switch.System.IO.UseLegacyPathHandling',$false)
[AppContext]::SetSwitch('Switch.System.IO.BlockLongPaths',$false)
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-NativePath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path.Replace('/','\'))
    if ($full.StartsWith('\\?\')) { return $full }
    if ($full.StartsWith('\\')) { return '\\?\UNC\' + $full.Substring(2) }
    return '\\?\' + $full
}
function Get-FileSha256([string]$Path) {
    $stream = [IO.File]::OpenRead((Get-NativePath $Path))
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}
if (!$ArchivePath) {
    $scriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { $env:PACKAGE_UNPACK_ROOT }
    $archives = @([IO.Directory]::GetFiles((Get-NativePath $scriptRoot),'QCSCKP-v*-production-r*-Windows-x64.zip'))
    if ($archives.Count -ne 1) { throw 'Place exactly one production ZIP beside this script, or pass -ArchivePath.' }
    $ArchivePath = $archives[0]
}
$ArchivePath = Get-NativePath $ArchivePath
if (!$ExpectedSha256 -and [IO.File]::Exists($ArchivePath+'.sha256.txt')) {
    $ExpectedSha256 = ([IO.File]::ReadAllText($ArchivePath+'.sha256.txt').Trim() -split '\s+')[0]
}
if ($ExpectedSha256) {
    if ($ExpectedSha256 -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileSha256 $ArchivePath) -ne $ExpectedSha256.ToLowerInvariant()) {
        throw 'ZIP SHA256 verification failed. No installation files were changed.'
    }
}
$zip = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
$stage = $null
try {
    $manifestEntries = @($zip.Entries | Where-Object { $_.FullName -match '^[^/]+/PACKAGE-MANIFEST\.json$' })
    if ($manifestEntries.Count -ne 1) { throw 'Invalid package root or manifest.' }
    $prefix = $manifestEntries[0].FullName.Substring(0,$manifestEntries[0].FullName.IndexOf('/')+1)
    $reader = [IO.StreamReader]::new($manifestEntries[0].Open(),[Text.Encoding]::UTF8)
    try { $manifest = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
    if ($manifest.app_name -ne 'QCSCKP' -or $manifest.channel -ne 'production' -or
        [string]$manifest.version -notmatch '^\d+\.\d+\.\d+$' -or [int]$manifest.build_revision -lt 1) {
        throw 'Invalid production package identity.'
    }
    if (!$Destination) {
        $Destination = [IO.Path]::Combine($env:LOCALAPPDATA,('QCSCKP\programs\production\v{0}-r{1}' -f $manifest.version,$manifest.build_revision))
    }
    $Destination = Get-NativePath $Destination
    if ([IO.Directory]::Exists($Destination) -or [IO.File]::Exists($Destination)) {
        throw "Destination already exists; choose a new directory with -Destination. Nothing was overwritten: $Destination"
    }
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    [long]$expandedBytes = 0
    foreach ($entry in $zip.Entries) {
        $name = $entry.FullName.Replace('\','/')
        if (!$name.StartsWith($prefix,[StringComparison]::Ordinal) -or $name.Contains([char]0)) { throw 'ZIP entry is outside the package root.' }
        $relative = $name.Substring($prefix.Length).TrimEnd('/')
        if (!$relative) { continue }
        foreach ($part in $relative.Split('/')) {
            if (!$part -or $part -eq '.' -or $part -eq '..' -or $part -match '[<>:"|?*]' -or $part.TrimEnd(' ','.') -ne $part) {
                throw 'ZIP contains an invalid or escaping path.'
            }
        }
        if ((($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960) { throw 'ZIP links are not supported.' }
        if (!$seen.Add($relative)) { throw 'ZIP contains duplicate paths.' }
        $expandedBytes += $entry.Length
        if ($expandedBytes -gt 3GB) { throw 'Expanded package exceeds the size limit.' }
    }
    $parent = [IO.Path]::GetDirectoryName($Destination)
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $stage = $Destination + '.unpack-' + [Guid]::NewGuid().ToString('N')
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    foreach ($entry in $zip.Entries) {
        $relative = $entry.FullName.Replace('\','/').Substring($prefix.Length).TrimEnd('/')
        if (!$relative) { continue }
        $file = Get-NativePath ([IO.Path]::Combine($stage,$relative.Replace('/','\')))
        if (!$file.StartsWith($stage+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'ZIP entry escapes staging.' }
        if ($entry.FullName.EndsWith('/')) { [IO.Directory]::CreateDirectory($file) | Out-Null; continue }
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($file)) | Out-Null
        $inputStream = $entry.Open()
        $outputStream = [IO.File]::Open($file,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
        try { $inputStream.CopyTo($outputStream) } finally { $outputStream.Dispose(); $inputStream.Dispose() }
    }
    $required = @('QCSCKP.exe','bin/python312.dll','bin/release.json','bin/static/index.html','bin/static/license.html')
    foreach ($name in $required) {
        if ($name -notin @($manifest.critical_files | ForEach-Object { $_.path })) { throw 'Manifest is missing a required file.' }
    }
    foreach ($item in $manifest.critical_files) {
        if (!$seen.Contains(([string]$item.path).Replace('\','/'))) { throw 'Manifest refers to an invalid package path.' }
        $file = Get-NativePath ([IO.Path]::Combine($stage,[string]$item.path))
        if (!$file.StartsWith($stage+'\',[StringComparison]::OrdinalIgnoreCase) -or ![IO.File]::Exists($file) -or
            [IO.FileInfo]::new($file).Length -ne [long]$item.size -or (Get-FileSha256 $file) -ne [string]$item.sha256) {
            throw 'Extracted package integrity verification failed.'
        }
    }
    $identity = [IO.File]::ReadAllText([IO.Path]::Combine($stage,'bin\release.json')) | ConvertFrom-Json
    foreach ($key in @('app_name','channel','version','build_revision')) {
        if ([string]$identity.$key -ne [string]$manifest.$key) { throw 'Package identity mismatch.' }
    }
    [IO.Directory]::Move($stage,$Destination)
    $stage = $null
    Write-Host "Installed: $Destination"
    Write-Host 'Run QCSCKP.exe in that directory. Existing channel data and encrypted credentials were not changed.'
} finally {
    $zip.Dispose()
    if ($stage -and $stage.StartsWith($Destination+'.unpack-',[StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetDirectoryName($stage) -eq [IO.Path]::GetDirectoryName($Destination) -and [IO.Directory]::Exists($stage)) {
        [IO.Directory]::Delete($stage,$true)
    }
}
