@echo off
REM Desktop agent for the "Re-link CrowdVolt" button on kartis.homes.
REM
REM Polls the server for a parked relink request and, when one lands,
REM harvests cv_refresh_token from the CDP Chrome (start_chrome.bat) and
REM pushes it up. Leave it running; it is idle apart from one small poll
REM every 30s.
REM
REM Needs KARTIS_CVAUTH_SECRET / KARTIS_BASE_URL / KARTIS_WEB_USER /
REM KARTIS_WEB_PASS in .env. Chrome must be signed in at crowdvolt.com.

cd /d "%~dp0"

if exist .venv\Scripts\python.exe (
  ".venv\Scripts\python.exe" cv_agent.py
) else (
  python cv_agent.py
)
pause
