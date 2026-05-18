@echo off
REM 雙擊執行 → 探測「中國醫藥大學附設醫院西醫門診醫師作業」的視窗 + 選單結構。
REM 使用前：先開啟主程式並切到有患者掛入、看得到「醫令」選單列的畫面。

chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ============================================================
echo   探測主程式視窗結構
echo ============================================================
echo.
echo 使用前確認：
echo   1. 「中國醫藥大學附設醫院西醫門診醫師作業」視窗已開啟
echo   2. 已掛入患者（畫面顯示像截圖那樣，能看到「醫令」選單）
echo.
pause

where python >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 找不到 python，請先安裝。
    pause
    exit /b 1
)

python scripts\probe_main_app_menu.py

echo.
echo ============================================================
echo   結束。輸出已存到 settings\main_app_menu_probe.txt
echo   請把該檔內容貼給 Claude。
echo ============================================================
pause
