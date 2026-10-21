@echo off
REM ============================================================================
REM ASCII-only wrapper. Self-elevates via UAC.
REM (The probe must run as admin: UIPI silently blocks WM_COMMAND sent to the
REM  elevated HIS window, so an un-elevated probe looks like "buttons do
REM  nothing".)
REM
REM 用途：HIS 改版讓熱鍵打到別的功能時(例:F9/F10 開成診斷書),雙擊本檔:
REM   1. 開啟「HIS熱鍵ID修正工具」(西醫門診醫師作業 要開著+掛入患者)
REM   2. 照畫面步驟 1~4:選功能 → 送出測試 → 畫面正確就按「就是這個編號」
REM   3. 按「套用修正」→ 確認 → 寫入 settings\his_menu_override.json
REM   4. 重啟中國醫皮膚科主程式,下次啟動直接套用修正(不用等版本更新)
REM 詳細步驟: HIS熱鍵ID修正工具_使用說明.txt (同資料夾)
REM 之後正式校正版本推送上來時,快速修正檔會自動過期失效,不會互相蓋。
REM ============================================================================

REM Admin check via fltmc (no dependency on the LanmanServer service --
REM `net session` fails even when elevated if that service is disabled,
REM which would loop UAC forever). The /elevated marker is a second guard:
REM the relaunched copy never tries to elevate again.
fltmc >nul 2>nul
if not errorlevel 1 goto :elevated
if "%~1"=="/elevated" (
    echo [ERROR] Could not obtain administrator rights.
    goto :hold
)
REM The path goes through an environment variable, NOT interpolated into the
REM PowerShell source: a path containing an apostrophe (C:\Users\O'Connor\...)
REM would otherwise terminate the single-quoted string early and the emergency
REM repair tool could no longer elevate at all. Env var = data, not source.
set "CMUH_ELEVATE_TARGET=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath $env:CMUH_ELEVATE_TARGET -ArgumentList '/elevated' -Verb RunAs"
exit /b 0

:elevated
chcp 65001 >nul
cd /d "%~dp0"
REM The tool's file name is Chinese (HIS熱鍵ID修正工具.py); command lines stay
REM ASCII so the batch parser never depends on the console code page: the
REM wildcard below matches it (it is the only HIS*ID*.py in the app folder).
set "SCRIPT="
for %%F in ("%~dp0HIS*ID*.py") do set "SCRIPT=%%~fF"

if not defined SCRIPT (
    echo [ERROR] HIS hotkey ID fix tool HIS*ID*.py not found. Update first:
    echo   restart any CMUH program to auto-update, then run this again.
    goto :hold
)

REM Python resolution: app-local embedded python first (安裝Python.bat layout),
REM then the py launcher (only present with real installs), then bare python
REM last -- on machines with the App Execution Alias enabled, bare `python`
REM can be the Microsoft Store stub that never runs anything.
if exist "%~dp0python_embed\python.exe" (
    "%~dp0python_embed\python.exe" "%SCRIPT%"
    goto :after
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%SCRIPT%"
    goto :after
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%SCRIPT%"
    goto :after
)
echo [ERROR] Python not found. Run 安裝Python.bat first.
goto :hold

:after
set "RC=%errorlevel%"
if "%RC%"=="0" exit /b 0
echo.
echo [ERROR] HIS hotkey ID fix tool exited with code %RC%.

:hold
echo.
echo Press any key to close...
pause >nul
exit /b 1
