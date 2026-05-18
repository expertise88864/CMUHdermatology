@echo off
REM ============================================================================
REM ASCII-only wrapper. Real logic is in the .ps1 of the same base name.
REM
REM Why ASCII only:
REM   cmd.exe on Traditional-Chinese Windows (CP950) reads .cmd file content
REM   using the active code page BEFORE any chcp 65001 line takes effect.
REM   UTF-8 Chinese bytes in a .cmd file become garbage, causing silent
REM   failures (the previous .bat "flashed and disappeared" for this reason).
REM   PowerShell handles UTF-8 BOM .ps1 files correctly, so we delegate.
REM ============================================================================

REM Self-elevate via UAC. The elevated process re-enters this file.
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
