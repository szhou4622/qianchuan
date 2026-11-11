@echo off
setlocal
chcp 65001 >nul
set "PACKAGE_UNPACK_ROOT=%~dp0"
set "PACKAGE_UNPACK_COMMAND=%~f0"
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [AppContext]::SetSwitch('Switch.System.IO.UseLegacyPathHandling',$false); [AppContext]::SetSwitch('Switch.System.IO.BlockLongPaths',$false); $p=[IO.Path]::GetFullPath($env:PACKAGE_UNPACK_COMMAND); if(!$p.StartsWith('\\?\')){ if($p.StartsWith('\\')){$p='\\?\UNC\'+$p.Substring(2)}else{$p='\\?\'+$p} }; $text=[IO.File]::ReadAllText($p); $marker='# POWERSHELL PAYLOAD BELOW'; & ([ScriptBlock]::Create($text.Substring($text.LastIndexOf($marker)+$marker.Length)))"
set "UNPACK_RESULT=%errorlevel%"
if errorlevel 1 (
  echo Installation failed. Existing program and user data were not overwritten.
) else (
  echo Installation completed. Open the directory printed above and run QCSCKP.exe.
)
pause
exit /b %UNPACK_RESULT%
# POWERSHELL PAYLOAD BELOW
@@POWERSHELL_PAYLOAD@@
