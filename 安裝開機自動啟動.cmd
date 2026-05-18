@echo off
REM ============================================================================
REM ASCII-only wrapper. Calls the .ps1 of the same base name (handles Unicode).
REM Self-elevates via UAC because schtasks /Create /RL HIGHEST needs admin.
REM
REM 用途：勾選哪些程式要在登入時自動啟動（主程式 / 打卡 / 會診查詢）
REM 每個被勾選的程式 → 建立 schtasks ONLOGON + Highest 排程（不跳 UAC）
REM 沒勾的程式 → 移除對應排程（之前如有勾過）
REM ============================================================================

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
