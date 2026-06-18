@echo off
REM ASCII-only launcher: identify the 身份/部分負擔 field (shows 40). Read-only.
REM
REM Steps:
REM   1. Load a patient in the HIS main window (the top-left field shows "40").
REM   2. Double-click this file.
REM   3. Switch back to the HIS and CLICK INSIDE the 身份 field (the "40" box),
REM      caret blinking in it, BEFORE the countdown ends.
REM   4. Send Claude the whole file: settings\_identity_field_probe.txt

cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
if not exist settings mkdir settings

where python >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=python"
    goto :run
)
where py >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py -3"
    goto :run
)
echo [ERROR] Python not found on PATH.
echo.
pause
exit /b 1

:run
REM No output redirect: you must SEE the countdown so you know when to click.
%PYEXE% "%~dp0scripts\probe_identity_field.py"
echo.
echo (Result saved to settings\_identity_field_probe.txt -- send it to Claude.)
echo.
echo Press any key to close...
pause >nul
exit /b 0
