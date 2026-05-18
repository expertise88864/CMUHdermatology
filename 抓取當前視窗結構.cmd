@echo off
REM ASCII-only wrapper. Self-elevates so it can enum admin window structures.
REM
REM Usage:
REM   1. Double-click.
REM   2. UAC prompt -> Yes.
REM   3. Click into the target window you want to snapshot.
REM   4. Wait 5-second countdown (give yourself time to switch).
REM   5. Snapshot saves to settings\snapshot_<timestamp>.txt — send to Claude.
REM
REM For F9/F10 workflow:
REM   - Run once with the 同意書開立作業 main window foreground
REM   - Run once with the 開立電子 popup window foreground
REM   - Run once with the 片語選擇 popup window foreground

net session >nul 2>nul
if not errorlevel 1 goto :elevated
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:elevated
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not in PATH.
    pause
    exit /b 1
)

chcp 65001 >nul
set PYTHONIOENCODING=utf-8

python scripts\snapshot_foreground_window.py
echo.
echo Press any key to close...
pause >nul
exit /b %errorlevel%
