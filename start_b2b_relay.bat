@echo off
REM Relay laptop: B2B snipes. Opens the dedicated B2B Chrome (port 9334,
REM profile user_data_b2b, signed into Automatiq + TickPick by hand) if it
REM isn't running, then reads every due event's listings from it and pushes
REM them to kartis.homes. Runs windowless; logs to logs\b2b_relay.log.
REM Needs KARTIS_CVAUTH_SECRET / KARTIS_WEB_USER / KARTIS_WEB_PASS in .env.

cd /d "%~dp0"

curl -s -m 3 http://127.0.0.1:9334/json/version >nul 2>&1
if errorlevel 1 (
  start "" "%ProgramFiles%\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9334 --user-data-dir="%~dp0user_data_b2b"
  timeout /t 8 /nobreak >nul
)

if exist .venv\Scripts\pythonw.exe (
  start "" ".venv\Scripts\pythonw.exe" b2b_relay.py --loop
) else (
  start "" pythonw b2b_relay.py --loop
)
