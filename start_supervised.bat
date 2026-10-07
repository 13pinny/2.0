@echo off
cd /d "%~dp0"

echo.
echo ==============================================
echo   Kartis Drop-Checker — supervised + auto-update
echo ==============================================
echo.
echo Pulls from GitHub every few minutes, restarts the
echo watcher only when there are actual changes.
echo Close this window to stop.
echo.

if not exist .venv\Scripts\activate.bat (
  echo .venv not found. Run setup first:
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

if not exist .env (
  echo .env not found. Copy .env.example to .env and fill in:
  echo   DISCORD_WEBHOOK_URL, GMAIL_USER, GMAIL_APP_PASSWORD, NOTIFY_EMAIL_TO
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat

REM Tee output to a rolling log file so unattended runs leave a trail
python supervisor.py >> supervisor.log 2>&1

pause
