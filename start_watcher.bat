@echo off
cd /d "%~dp0"

echo.
echo ==============================================
echo   Kartis Drop-Checker (watcher-only mode)
echo ==============================================
echo.
echo No Chrome login required. Polls Ticketmaster
echo every 60s and pings Discord + email on drops.
echo Close this window to stop.
echo.

if not exist .venv\Scripts\activate.bat (
  echo .venv not found. Run setup first:
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat

if not exist .env (
  echo .env not found. Copy .env.example to .env and fill in:
  echo   DISCORD_WEBHOOK_URL, GMAIL_USER, GMAIL_APP_PASSWORD, NOTIFY_EMAIL_TO
  pause
  exit /b 1
)

python watcher_only.py

pause
