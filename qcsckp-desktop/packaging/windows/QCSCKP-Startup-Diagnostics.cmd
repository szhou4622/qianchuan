@echo off
setlocal
cd /d "%~dp0"
if not exist "QCSCKP.exe" (
  echo QCSCKP.exe is missing from this folder.
  pause
  exit /b 2
)
if not exist "bin\python312.dll" (
  echo bin\python312.dll is missing. Re-extract the complete ZIP or check antivirus quarantine.
  pause
  exit /b 3
)
start "" "QCSCKP.exe" --diagnose-startup
endlocal
