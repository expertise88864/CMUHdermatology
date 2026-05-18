@echo off
REM ============================================================================
REM 啟動會診查詢程式.cmd
REM
REM 雙擊執行：觸發已建立的工作排程「CMUH皮膚科會診查詢自動啟動」
REM 排程器直接給 admin token，【不跳 UAC】。
REM
REM 前提：必須先跑過 安裝會診查詢自動啟動.cmd（建立排程）。
REM ============================================================================

set "TASK_NAME=CMUH皮膚科會診查詢自動啟動"

REM 是否已經有 consult_query 在跑？單例 mutex 會自己擋掉重複啟動，
REM 這邊先用 schtasks /Query 簡單檢查任務狀態，給使用者一點訊息。

schtasks /Query /TN "%TASK_NAME%" >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 找不到排程 "%TASK_NAME%"
    echo        請先雙擊 安裝會診查詢自動啟動.cmd 建立排程。
    echo.
    pause
    exit /b 1
)

schtasks /Run /TN "%TASK_NAME%" >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 啟動排程失敗。
    echo.
    pause
    exit /b 1
)

REM 成功就直接關，不留視窗
exit /b 0
