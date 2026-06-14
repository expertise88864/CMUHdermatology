@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
REM =============================================================================
REM pull_update.bat — 手動拉取最新版（給其他電腦使用）
REM
REM 為何不用普通 git pull：執行中的程式有「線上自動更新」，會把追蹤檔(src/*.py…)
REM 內容覆寫成新版、但 git HEAD 仍停在舊 commit → git 把這些新內容當成「本地未提交
REM 的修改」→ git pull(merge)被擋「local changes would be overwritten」→ 永遠拉不
REM 下來。故改為「強制對齊遠端、放棄本地改動」(= 既定慣例)。
REM settings\ 等被 .gitignore 的檔不受影響，帳密保留。
REM =============================================================================
cd /d "%~dp0"

set GITHUB_OWNER=expertise88864
set GITHUB_REPO=CMUHdermatology
set GITHUB_BRANCH=main

echo === 手動拉取最新版 ===
echo 建議先「完全關閉皮膚科程式」再更新，避免和自動更新搶寫檔案。
echo.

if not exist ".git" goto zipmode

REM ---------- git 模式：強制同步遠端 ----------
where git >nul 2>nul || ( echo [錯誤] 找不到 git & pause & exit /b 1 )
echo 從 GitHub 取得最新版...
git fetch origin %GITHUB_BRANCH%
if errorlevel 1 goto neterr
git reset --hard origin/%GITHUB_BRANCH%
if errorlevel 1 goto reseterr
REM reset --hard 已把檔案對齊最新；checkout -B 只是把分支切回 main(非必要)。
REM 失敗不算更新失敗(內容已最新),但要明講、不可悄悄當成功。
git checkout -B %GITHUB_BRANCH% origin/%GITHUB_BRANCH%
if errorlevel 1 echo [提醒] 檔案已是最新，但切回 %GITHUB_BRANCH% 分支失敗(不影響使用，下次更新仍會強制對齊)。
for /f "delims=" %%v in ('git rev-parse --short HEAD') do echo 已同步到遠端 commit %%v
goto done

:zipmode
echo [zip 模式] 從 zip 重新下載...
set ZIP_URL=https://github.com/%GITHUB_OWNER%/%GITHUB_REPO%/archive/refs/heads/%GITHUB_BRANCH%.zip
set ZIP_FILE=%TEMP%\cmuh_pull.zip
powershell -NoProfile -Command "Invoke-WebRequest -Uri '!ZIP_URL!' -OutFile '!ZIP_FILE!' -UseBasicParsing"
if errorlevel 1 goto neterr
powershell -NoProfile -Command "Expand-Archive -Path '!ZIP_FILE!' -DestinationPath '%TEMP%\cmuh_pull_extract' -Force"
xcopy /e /i /y "%TEMP%\cmuh_pull_extract\%GITHUB_REPO%-%GITHUB_BRANCH%\*" ".\" >nul
del /f /q "!ZIP_FILE!" & rmdir /s /q "%TEMP%\cmuh_pull_extract"
goto done

:neterr
echo.
echo [錯誤] 連不到 GitHub。多半是網路或醫院防火牆擋了 github.com。
echo        先用瀏覽器確認這台能開：https://github.com/%GITHUB_OWNER%/%GITHUB_REPO%
pause
exit /b 1

:reseterr
echo.
echo [錯誤] 強制同步失敗。請先「完全關閉皮膚科程式」（含背景打卡/常駐）再重跑本檔。
pause
exit /b 1

:done
echo.
echo ============================================================
echo   拉取完成！下次啟動程式即會使用新版
echo ============================================================
pause
endlocal
