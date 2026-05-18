@echo off
REM ASCII-only wrapper. See sibling 安裝會診查詢自動啟動.cmd for full explanation.

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
