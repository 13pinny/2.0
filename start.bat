@echo off
cd /d "%~dp0"

echo.
echo ==============================================
echo   Kartis Dashboard - starting up
echo ==============================================
echo.

call .venv\Scripts\activate.bat

echo Launching Chrome. Log into Lysted (and Viagogo in a 2nd tab)
echo if it asks, then come back and press Enter in this window.
echo.

python login.py
if errorlevel 1 goto :end

echo.
echo Opening dashboard in your browser...
start "" http://localhost:5000

echo.
echo ==============================================
echo   Dashboard running at http://localhost:5000
echo   Close this window to stop the scraper.
echo ==============================================
echo.

python app.py

:end
pause
