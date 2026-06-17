@echo off
REM ASCII-only wrapper for the 醫師上次 UIA probe.
REM
REM IMPORTANT: this does NOT self-elevate. UIA must run at the SAME privilege
REM level as the HIS window, or it gets blocked. If the HIS runs as normal user
REM (usual case), just double-click this. If the probe says it cannot read and
REM your HIS runs "as administrator", re-run this as administrator too.
REM
REM Usage:
REM   1. In the HIS: DITTO -> 醫師上次, leave that list window OPEN.
REM   2. Double-click this file.
REM   3. Result saves to settings\_ditto_uia_probe.txt -- send it to Claude.

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    pause
    exit /b 1
)

chcp 65001 >nul
set PYTHONIOENCODING=utf-8

python scripts\probe_ditto_uia.py
echo.
echo Press any key to close...
pause >nul
exit /b %errorlevel%
