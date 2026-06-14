@echo off
REM Dev-env migration launcher. Double-click on a new machine after syncing the repo.
REM Runs dev-env-setup.py under UTF-8 so the Chinese console output is not garbled.
chcp 65001 >nul
set PYTHONUTF8=1
set "PYEXE="
REM Prefer the py launcher (reliable). Verify it actually runs Python, not a stub.
py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3"
REM Fall back to python, but only if it really executes (rejects the Windows Store alias).
if not defined PYEXE (
    python -c "import sys" >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] Python 3 not found. Install Python 3.10+ from https://python.org
    echo         and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)
%PYEXE% "%~dp0dev-env-setup.py" %*
pause
