@echo off
REM Launches Kartis as a single user-facing action:
REM   1. Spawns the dedicated scraper Chrome with Lysted/Viagogo/CrowdVolt
REM      tabs (idempotent — login.py exits cleanly if it's already running
REM      on :9222).
REM   2. Starts Flask (python app.py) in a separate minimized window if
REM      it's not already responding on :5000. Idempotent.
REM   3. Opens the dashboard at http://localhost:5000 as a Chrome --app
REM      window (no tabs, no address bar — looks native).
REM
REM Wired to the "Kartis" Start menu shortcut. WindowStyle=7 (minimized)
REM on the .lnk hides the brief console flash from this script. The Flask
REM window itself stays minimized in your taskbar — close it to stop the
REM dashboard.

cd /d "%~dp0"

REM --- 1. Login Chrome (dedicated CDP profile) ----------------------------
if exist .venv\Scripts\pythonw.exe (
  start "" ".venv\Scripts\pythonw.exe" login.py --no-prompt
) else (
  start "" pythonw login.py --no-prompt
)

REM --- 2. Flask — only start if not already responding on :5000 -----------
powershell -NoProfile -Command "try { Invoke-WebRequest http://localhost:5000 -TimeoutSec 2 -UseBasicParsing | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  REM Flask is down — launch in its own minimized window so user can see
  REM logs / Ctrl+C to stop. cmd /k keeps the window open if app.py crashes.
  if exist .venv\Scripts\python.exe (
    start "Kartis Flask" /MIN cmd /k ".venv\Scripts\python.exe app.py"
  ) else (
    start "Kartis Flask" /MIN cmd /k "python app.py"
  )
  REM Wait up to 20s for Flask to come up before we try to open the dashboard.
  powershell -NoProfile -Command "for ($i = 0; $i -lt 20; $i++) { try { Invoke-WebRequest http://localhost:5000 -TimeoutSec 1 -UseBasicParsing | Out-Null; exit 0 } catch { Start-Sleep -Milliseconds 800 } }; exit 1"
)

REM --- 3. Dashboard --app window (user's normal Chrome profile) -----------
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo ERROR: chrome.exe not found.
  exit /b 1
)
start "" "%CHROME%" --app=http://localhost:5000
