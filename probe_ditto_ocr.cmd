@echo off
REM ASCII-only launcher for the medical-history-grid OCR probe.
REM Read-only: screenshots the grid (PrintWindow) + OCR. No clicks, no data changes.
REM Logs EVERYTHING to settings\_ditto_ocr_run.log so output survives even if the
REM window closes. Always pauses at the end.
REM
REM Steps:
REM   1. In the HIS: DITTO -> medical-history list, leave it OPEN with the top rows visible.
REM   2. Double-click this file. (May auto-install the OCR package "winsdk" once.)
REM   3. Send Claude: settings\_ditto_ocr_probe.txt  (and settings\_ditto_grid.png).

cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
if not exist settings mkdir settings
set "LOG=%~dp0settings\_ditto_ocr_run.log"

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
echo [ERROR] Python not found on PATH (tried: python, py).> "%LOG%"
type "%LOG%"
echo.
echo Press any key to close...
pause >nul
exit /b 1

:run
echo Running probe with %PYEXE% ...  (full log: %LOG%)
echo.
%PYEXE% "%~dp0scripts\probe_ditto_ocr.py" --show > "%LOG%" 2>&1
echo ------------------------- OUTPUT -------------------------
type "%LOG%"
echo ----------------------------------------------------------
echo.
echo If anything failed, send Claude this file: %LOG%
echo.
echo Press any key to close...
pause >nul
exit /b 0
