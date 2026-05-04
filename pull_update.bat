@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
REM =============================================================================
REM pull_update.bat — 手動拉取最新版（給其他電腦使用）
REM =============================================================================
cd /d "%~dp0"

set GITHUB_OWNER=expertise88864
set GITHUB_REPO=CMUHdermatology
set GITHUB_BRANCH=main

echo === 手動拉取最新版 ===
echo.

if exist ".git" (
    where git >nul 2>nul || ( echo [錯誤] 找不到 git & pause & exit /b 1 )
    git pull origin %GITHUB_BRANCH% || ( echo [錯誤] git pull 失敗 & pause & exit /b 1 )
) else (
    echo [zip 模式] 從 zip 重新下載...
    set ZIP_URL=https://github.com/%GITHUB_OWNER%/%GITHUB_REPO%/archive/refs/heads/%GITHUB_BRANCH%.zip
    set ZIP_FILE=%TEMP%\cmuh_pull.zip
    powershell -NoProfile -Command "Invoke-WebRequest -Uri '!ZIP_URL!' -OutFile '!ZIP_FILE!' -UseBasicParsing"
    if errorlevel 1 ( echo [錯誤] 下載失敗 & pause & exit /b 1 )
    powershell -NoProfile -Command "Expand-Archive -Path '!ZIP_FILE!' -DestinationPath '%TEMP%\cmuh_pull_extract' -Force"
    xcopy /e /i /y "%TEMP%\cmuh_pull_extract\%GITHUB_REPO%-%GITHUB_BRANCH%\*" ".\" >nul
    del /f /q "!ZIP_FILE!" & rmdir /s /q "%TEMP%\cmuh_pull_extract"
)

echo.
echo ============================================================
echo   拉取完成！下次啟動程式即會使用新版
echo ============================================================
pause
endlocal
