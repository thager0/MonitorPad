@echo off
rem Launch MonitorPad into the notification area (no console window).
rem Right-click its tray icon to pair a phone or change displays.
setlocal
cd /d "%~dp0agent"

where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw.exe monitorpad.pyw %*
  exit /b 0
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install Python 3.9 or newer from https://python.org and tick
  echo "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

start "" python.exe monitorpad.pyw %*
