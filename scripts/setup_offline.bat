@echo off
REM Windows wrapper for scripts\setup_offline.py.
REM Lets users double-click from Explorer instead of opening a terminal.
setlocal
cd /d "%~dp0\.."
python scripts\setup_offline.py
if errorlevel 1 (
    echo.
    echo Setup failed. Press any key to close...
    pause >nul
    exit /b 1
)
echo.
echo Press any key to close...
pause >nul
