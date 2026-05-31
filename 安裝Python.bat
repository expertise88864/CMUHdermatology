@echo off
REM =============================================================================
REM Run this once on a new PC. After it finishes you can double-click any
REM .pyw file in this folder to launch the program.
REM
REM What this does (no admin / UAC needed):
REM   1. Detects existing Python >= 3.10. If found, jumps to dependency install.
REM   2. If not found, downloads python-3.12.7-amd64.exe from python.org
REM      and runs it in per-user silent mode:
REM        /quiet InstallAllUsers=0 PrependPath=1 AssociateFiles=1
REM      -> Python installed to %LOCALAPPDATA%\Programs\Python\Python312\
REM      -> .pyw / .py file associations registered (so double-click works)
REM      -> Python added to user PATH (so `python` works in new cmd sessions)
REM      -> py.exe launcher + pip + tkinter all installed
REM   3. Installs all required packages via pip from requirements.txt.
REM
REM Filename in chinese is OK; the BODY of this .bat must stay pure ASCII
REM because cmd.exe under cp950 (zh-TW Windows) mis-parses non-ASCII source.
REM =============================================================================

setlocal
cd /d "%~dp0"

set "PYTHON_VERSION=3.12.7"
set "PYTHON_INSTALLER_URL=https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
set "PER_USER_PY_DIR=%LOCALAPPDATA%\Programs\Python\Python312"

echo.
echo ============================================================
echo   CMUH dermatology - one-time Python setup
echo ============================================================
echo.

REM ---- [1/3] check existing Python ------------------------------------------
echo [1/3] Checking for existing Python ^>= 3.10 ...
set "PYEXE="

where python >nul 2>nul
if errorlevel 1 goto :check_per_user

set "PY_OK=0"
for /f "usebackq delims=" %%v in (`python -c "import sys; print(1 if sys.version_info >= (3,10) else 0)" 2^>nul`) do set "PY_OK=%%v"
if not "%PY_OK%"=="1" goto :check_per_user

for /f "usebackq delims=" %%p in (`where python 2^>nul`) do if not defined PYEXE set "PYEXE=%%p"
echo       found in PATH: %PYEXE%
goto :install_deps

:check_per_user
if not exist "%PER_USER_PY_DIR%\python.exe" goto :install_python
set "PYEXE=%PER_USER_PY_DIR%\python.exe"
echo       found per-user Python: %PYEXE%
goto :install_deps

REM ---- [2/3] download + install Python --------------------------------------
:install_python
echo       no Python detected.
echo.
echo [2/3] Downloading Python %PYTHON_VERSION% installer (~30 MB) ...
set "INSTALLER=%TEMP%\cmuh_python_installer.exe"
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYTHON_INSTALLER_URL%' -OutFile '%INSTALLER%' -UseBasicParsing"
if errorlevel 1 (
    echo       [error] download failed. Check internet / firewall.
    pause
    exit /b 1
)

echo       installing silently (per-user; no admin required) ...
echo       this takes ~30-60 seconds. Please wait...
"%INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 AssociateFiles=1 Include_test=0 Include_doc=0 Include_launcher=1 Include_pip=1 Include_tcltk=1 SimpleInstall=1
set "RC=%ERRORLEVEL%"
del /q "%INSTALLER%" 2>nul

if not "%RC%"=="0" (
    echo       [error] installer returned exit code %RC%
    echo       Common causes: anti-virus blocked, existing Python conflict.
    pause
    exit /b 1
)

if not exist "%PER_USER_PY_DIR%\python.exe" (
    echo       [error] install completed but %PER_USER_PY_DIR%\python.exe not found
    echo       Try opening %LOCALAPPDATA%\Programs\Python\ to see what was installed.
    pause
    exit /b 1
)

set "PYEXE=%PER_USER_PY_DIR%\python.exe"
echo       Python %PYTHON_VERSION% installed at: %PER_USER_PY_DIR%
echo       File associations (.pyw, .py) registered automatically.
echo       PATH updated (effective in NEW cmd sessions).

REM ---- [3/3] install requirements -------------------------------------------
:install_deps
echo.
echo [3/3] Installing Python packages from requirements.txt ...
echo       (this takes 1-3 minutes on first run)
echo       interpreter:
"%PYEXE%" -c "import sys; print('      ' + sys.executable)"
if not exist "%~dp0settings" mkdir "%~dp0settings"
set "SETUP_LOG=%~dp0settings\python_setup.log"
> "%SETUP_LOG%" echo [setup] interpreter=%PYEXE%
"%PYEXE%" -m pip install --upgrade --no-input --disable-pip-version-check --prefer-binary --no-warn-script-location -r "%~dp0requirements.txt" >> "%SETUP_LOG%" 2>&1
if errorlevel 1 (
    echo       [error] pip install failed
    echo       Details: %SETUP_LOG%
    type "%SETUP_LOG%"
    pause
    exit /b 1
)

echo       verifying imports ...
"%PYEXE%" "%~dp0scripts\verify_dependencies.py"
if errorlevel 1 (
    echo       [error] package verification failed
    echo       The Python used above may differ from the Python associated with .pyw files.
    echo       Details: %SETUP_LOG%
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   DONE. Setup complete.
echo ============================================================
echo.
echo Next: just double-click any .pyw file in this folder:
echo   - main program      ^(zhu cheng shi^)
echo   - autoclock         ^(da ka^)
echo   - consult query     ^(hui zhen cha xun^)
echo   - scheduler         ^(pai ban^)
echo.
echo Python is now available system-wide for this user account.
echo You can also run other .pyw / .py files on this machine.
echo Setup log: %SETUP_LOG%
echo.
pause
endlocal
