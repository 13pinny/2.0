@echo off
REM One-time setup: drop a "Kartis" shortcut into the Windows Start Menu
REM so you can search "Kartis" from Start and open BOTH:
REM   - the dashboard (frameless Chrome --app window)
REM   - the scraper Chrome with Lysted/Viagogo/CrowdVolt logins (idempotent)
REM
REM The shortcut targets kartis_open.bat (in this folder) which handles
REM both. Console flash is suppressed via WindowStyle=7 (minimized).
REM
REM Run this once. Then type "Kartis" in Start and hit Enter.
REM Delete %APPDATA%\Microsoft\Windows\Start Menu\Programs\Kartis.lnk to undo.

setlocal
cd /d "%~dp0"

if not exist "%~dp0kartis_open.bat" (
  echo ERROR: kartis_open.bat not found in %~dp0
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'; if (-not (Test-Path $chrome)) { $chrome = 'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe' }; if (-not (Test-Path $chrome)) { $chrome = \"$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe\" }; $ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut(\"$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Kartis.lnk\"); $sc.TargetPath = '%~dp0kartis_open.bat'; $sc.Arguments = ''; $sc.WorkingDirectory = '%~dp0'; $sc.WindowStyle = 7; $sc.Description = 'Kartis dashboard + login Chrome'; if (Test-Path $chrome) { $sc.IconLocation = \"$chrome,0\" }; $sc.Save(); Write-Host 'Installed. Search Kartis in Windows Start to launch.'"

if errorlevel 1 (
  echo Failed to create shortcut.
  exit /b 1
)

echo.
echo Make sure Flask is running (start.bat or python app.py) before
echo searching Kartis in Start.
echo.
pause
