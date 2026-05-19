@echo off
REM ASCII-only wrapper. Self-elevates (requires admin to manage schtasks).
REM Logic in 清除所有皮膚科排程.ps1 (UTF-8 with BOM).

net session >nul 2>nul
if not errorlevel 1 goto :elevated

powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:elevated
cd /d "%~dp0"
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0清除所有皮膚科排程.ps1"
echo.
echo Press any key to close...
pause >nul
exit /b %errorlevel%
