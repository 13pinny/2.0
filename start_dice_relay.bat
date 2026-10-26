@echo off
REM Desktop relay for DICE prices: api.dice.fm blocks the server's IP, so
REM this fetches every tracked DICE event's ticket_types from here and
REM pushes it to kartis.homes every 60s. Runs windowless; logs to
REM logs\dice_relay.log. Needs KARTIS_CVAUTH_SECRET / KARTIS_BASE_URL /
REM KARTIS_WEB_USER / KARTIS_WEB_PASS in .env.

cd /d "%~dp0"

if exist .venv\Scripts\pythonw.exe (
  start "" ".venv\Scripts\pythonw.exe" dice_relay.py --loop
) else (
  start "" pythonw dice_relay.py --loop
)
