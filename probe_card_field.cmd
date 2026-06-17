@echo off
REM ASCII-only launcher: identify the 卡號 input field by focus. Read-only.
REM
REM Steps:
REM   1. Make sure a patient is loaded in the HIS main window.
REM   2. Double-click this file.
REM   3. Switch back to the HIS and CLICK INSIDE the 卡號 field (caret blinking in it)
REM      BEFORE the countdown ends.
REM   4. Send Claude: settings\_card_field_probe.txt

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
%PYEXE% "%~dp0scripts\probe_card_field.py"
echo.
echo (Result saved to settings\_card_field_probe.txt -- send it to Claude.)
echo.
echo Press any key to close...
pause >nul
exit /b 0
