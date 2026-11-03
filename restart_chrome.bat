@echo off
REM Force-restart the dedicated scraper Chrome (the one bound to CDP port
REM 9222). Use this when the scraper complains "Can't reach Chrome at
REM http://localhost:9222" or "Connection closed while reading from the
REM driver" — that usually means an older Chrome is bound to 9222 without
REM the --remote-allow-origins=* flag the new CDP handshake needs.
REM
REM Your personal Chrome is NOT touched. We only kill processes whose
REM --user-data-dir matches the scraper's dedicated profile (user_data/
REM inside this folder), so any other Chrome windows you have open stay
REM exactly where they were.
REM
REM Safe to double-click any time. Idempotent: if no scraper Chrome is
REM running we just launch a fresh one.

cd /d "%~dp0"

if exist .venv\Scripts\python.exe (
  ".venv\Scripts\python.exe" -c "import login; n = login.kill_scraper_chrome(); print(f'killed {n} scraper Chrome process(es)'); ok = login.launch_chrome(); print('Chrome ready' if ok else 'ERROR: relaunch failed')"
) else (
  python -c "import login; n = login.kill_scraper_chrome(); print(f'killed {n} scraper Chrome process(es)'); ok = login.launch_chrome(); print('Chrome ready' if ok else 'ERROR: relaunch failed')"
)

echo.
pause
