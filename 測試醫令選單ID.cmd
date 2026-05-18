@echo off
REM ASCII-only wrapper. Self-elevates via UAC.
REM
REM Why admin required:
REM   Windows UIPI blocks non-admin processes from sending WM_COMMAND to
REM   admin process windows. The hospital app runs as admin (TFopdmain).
REM   This test script must also run as admin to poke it.

net session >nul 2>nul
if not errorlevel 1 goto :elevated

powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:elevated
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python.exe not found in PATH.
    pause
    exit /b 1
)

chcp 65001 >nul
set PYTHONIOENCODING=utf-8

python scripts\test_yiling_menu_id.py
echo.
echo Press any key to close...
pause >nul
exit /b %errorlevel%
