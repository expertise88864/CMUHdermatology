@echo off
REM Lists python/pythonw processes that look like the CMUH autoclock app.
cd /d "%~dp0"
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0診斷打卡重複執行.ps1"
echo.
echo Press any key to close...
pause >nul
exit /b %errorlevel%
