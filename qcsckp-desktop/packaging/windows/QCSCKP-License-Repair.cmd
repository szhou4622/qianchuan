@echo off
setlocal
cd /d "%~dp0"
if not exist "QCSCKP.exe" (
  echo QCSCKP.exe is missing. Extract the complete ZIP first.
  pause
  exit /b 1
)
start "" "QCSCKP.exe" --repair-license-connection
