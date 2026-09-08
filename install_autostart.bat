@echo off
REM One-time setup: drop a shortcut to start_chrome.bat into the Windows
REM Startup folder so the logged-in Chrome opens on every Windows login.
REM Delete the shortcut from shell:startup to undo.

setlocal
cd /d "%~dp0"

set "TARGET=%~dp0start_chrome.bat"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP%\Kartis Chrome Login.lnk"
set "ICON=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if not exist "%TARGET%" (
  echo ERROR: start_chrome.bat not found at %TARGET%
  exit /b 1
)

REM Use PowerShell to create a proper .lnk shortcut. WScript.Shell is the
REM standard Windows way to do this — no third-party tools needed.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$sc = $ws.CreateShortcut('%SHORTCUT%');" ^
  "$sc.TargetPath = '%TARGET%';" ^
  "$sc.WorkingDirectory = '%~dp0';" ^
  "$sc.WindowStyle = 7;" ^
  "$sc.Description = 'Open the Lysted / Viagogo / CrowdVolt logged-in Chrome';" ^
  "if (Test-Path '%ICON%') { $sc.IconLocation = '%ICON%' };" ^
  "$sc.Save()"

if errorlevel 1 (
  echo Failed to create shortcut.
  exit /b 1
)

echo.
echo Installed.
echo   Shortcut: %SHORTCUT%
echo   Will run on every Windows login until you delete it.
echo.
echo To uninstall: open the Startup folder via Win+R, type "shell:startup",
echo then delete "Kartis Chrome Login.lnk".
echo.
pause
