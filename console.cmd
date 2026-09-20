@echo off
rem Run MonitorPad in a console window instead of the tray, so you can watch
rem the log and read the pairing PIN. Useful when something is not working.
rem Ctrl+C to stop.
setlocal
cd /d "%~dp0agent"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install Python 3.9 or newer from https://python.org and tick
  echo "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

python server.py %*
echo.
echo MonitorPad has stopped.
pause
