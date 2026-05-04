@echo off
REM =============================================================================
REM push.bat - 一鍵推送（核心邏輯在 scripts\push_helper.py，避免 BAT 在 UTF-8 環境下解析雷）
REM 用法：push.bat [commit message]
REM 範例：push.bat "修正 F11 在 1280x1024 偶發失效"
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
