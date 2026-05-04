@echo off
REM =============================================================================
REM migrate.bat - 把整包 CMUHdermatology 搬到舊版桌面資料夾
REM 預設目標: C:\Users\calling\Desktop\翊嘉\中國醫皮膚科程式
REM 用法: migrate.bat [自訂目標路徑]
REM =============================================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [Error] python not found in PATH.
    pause
    exit /b 1
)

python "%~dp0scripts\migrate_helper.py" %*
set RC=%ERRORLEVEL%

pause
exit /b %RC%
