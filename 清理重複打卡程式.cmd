@echo off
REM Self-elevates because duplicate autoclock processes may run as Highest.
cd /d "%~dp0"

net session >nul 2>nul
if not errorlevel 1 goto :elevated

powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:elevated
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dpn0.ps1"
echo.
echo Press any key to close...
pause >nul
exit /b %errorlevel%
