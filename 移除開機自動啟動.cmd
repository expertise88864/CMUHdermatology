@echo off
REM ASCII-only wrapper for 移除開機自動啟動.ps1
REM 移除所有 3 個程式的自動啟動排程（主程式 / 打卡 / 會診查詢）。

net session >nul 2>nul
if not errorlevel 1 goto :elevated

powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:elevated
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dpn0.ps1"
set "RC=%errorlevel%"
echo.
echo Press any key to close...
pause >nul
exit /b %RC%
