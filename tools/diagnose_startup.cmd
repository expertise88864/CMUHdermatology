@echo off
REM =============================================================================
REM diagnose_startup.cmd - measure where main.py spends startup time
REM
REM Requires Python to be installed (run the install bat in this folder first).
REM
REM Runs main.py under `python -X importtime`. Each imported module's load
REM time is printed to stderr. We capture it to settings\startup_profile.txt
REM and the analyzer script lists the slowest 30 imports for quick triage.
REM
REM Usage:
REM   1. Double-click this file
REM   2. Wait for the main program window to open
REM   3. Close the main program normally (X button)
REM   4. The slowest 30 imports will print; full log at settings\startup_profile.txt
REM =============================================================================

setlocal
cd /d "%~dp0.."

set "PYEXE="
set "PER_USER_PY_DIR=%LOCALAPPDATA%\Programs\Python\Python312"

REM Prefer PATH python (system or per-user with PATH update applied);
REM otherwise fall back to the known per-user install dir.
where python >nul 2>nul
if not errorlevel 1 (
    for /f "usebackq delims=" %%p in (`where python 2^>nul`) do if not defined PYEXE set "PYEXE=%%p"
) else (
    if exist "%PER_USER_PY_DIR%\python.exe" set "PYEXE=%PER_USER_PY_DIR%\python.exe"
)

if not defined PYEXE (
    echo [error] Python not found. Run the install .bat in this folder first.
    pause
    exit /b 1
)

if not exist "settings" mkdir "settings"
set "OUT=%~dp0..\settings\startup_profile.txt"

echo.
echo === Profiling main.py startup imports ===
echo === Python: %PYEXE%
echo === Output: %OUT%
echo.
echo The main program will open. Close it normally when ready; then this
echo window will summarize the slowest imports.
echo.

"%PYEXE%" -X importtime "%~dp0..\src\main.py" 2> "%OUT%"

echo.
echo === Slowest imports ===
"%PYEXE%" "%~dp0..\scripts\analyze_startup_profile.py" "%OUT%"

echo.
echo Full log:  %OUT%
echo.
pause
endlocal
