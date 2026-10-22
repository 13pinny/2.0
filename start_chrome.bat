@echo off
REM Open the logged-in Chrome with Lysted + Viagogo + CrowdVolt as tabs.
REM Sessions persist via user_data/. The scraper attaches via CDP on :9222.
REM
REM Idempotent — if Chrome is already running on :9222, this exits quietly.
REM
REM Double-click to launch. To autostart on boot, run install_autostart.bat
REM once and Windows will run this on every login.

cd /d "%~dp0"

if exist .venv\Scripts\pythonw.exe (
  REM pythonw runs without a console window — best for autostart
  start "" ".venv\Scripts\pythonw.exe" login.py --no-prompt
) else (
  REM Fall back to system Python in a hidden window if .venv isn't there
  start "" pythonw login.py --no-prompt
)
