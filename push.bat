@echo off
REM =============================================================================
REM push.bat - 一鍵推送（核心邏輯在 scripts\push_helper.py，避免 BAT 在 UTF-8 環境下解析雷）
REM Usage: push.bat check
REM Publish: push.bat publish --path scripts/example.py --message-file message.txt
REM No arguments show help. Legacy positional commit messages are rejected.
REM =============================================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [Error] python not found in PATH.
    echo Install Python from https://python.org
    pause
    exit /b 1
)

where git >nul 2>nul
if errorlevel 1 (
    echo [Error] git not found in PATH.
    echo Install Git from https://git-scm.com/download/win
    pause
    exit /b 1
)

python "%~dp0scripts\push_helper.py" %*
set RC=%ERRORLEVEL%

pause
exit /b %RC%
