@echo off
REM ============================================================================
REM ASCII-only wrapper for probe_main_app_menu.py.
REM
REM Why ASCII only:
REM   cmd.exe on Traditional-Chinese Windows (CP950) reads .cmd file content
REM   using the active code page BEFORE any chcp 65001 line takes effect.
REM   UTF-8 Chinese bytes in .cmd content become garbage. Keep this wrapper
REM   pure ASCII; the real work is in the .py script (Python handles UTF-8).
REM
REM Usage:
REM   1. Open the hospital main program first
REM      (Title contains "中國醫藥大學附設醫院西醫門診醫師作業")
REM   2. Have a patient loaded (so the "醫令" menu is visible)
REM   3. Double-click this file
REM   4. After it finishes, the output text file is at:
REM      settings\main_app_menu_probe.txt
REM      Paste its content to Claude.
REM ============================================================================

cd /d "%~dp0.."

echo.
echo ============================================================
echo   Probing main hospital app window + menu
echo ============================================================
echo.
echo Make sure:
echo   1. The hospital program window is open and visible.
echo   2. A patient is loaded (so the "Yi Ling" menu shows up).
echo.
echo Press any key to continue...
pause >nul

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] python.exe not found in PATH.
    echo Please install Python 3.10+ first.
    echo.
    pause
    exit /b 1
)

REM Force UTF-8 console output for the python child process so Chinese
REM characters print correctly to the screen (the .txt file is always UTF-8).
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

python scripts\probe_main_app_menu.py
set RC=%errorlevel%

echo.
echo ============================================================
echo   Done.
echo   Output saved to:
echo     settings\main_app_menu_probe.txt
echo   Paste its content to Claude.
echo ============================================================
echo.
pause
exit /b %RC%
