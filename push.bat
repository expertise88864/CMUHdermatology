@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
REM =============================================================================
REM push.bat — 一鍵推送本機變更到 GitHub
REM 流程：
REM   1) 自檢（git/python 在 PATH、檔案結構正確、settings 沒漏）
REM   2) bump src/cmuh_common/version.py 的 CURRENT_VERSION
REM   3) 同步 manifest.json（含 SHA256）
REM   4) git add -A && commit && push
REM 用法：
REM   雙擊本檔，或 push.bat "commit message"
REM =============================================================================

cd /d "%~dp0"
set REPO_ROOT=%~dp0
if "%REPO_ROOT:~-1%"=="\" set REPO_ROOT=%REPO_ROOT:~0,-1%

echo.
echo ========================================
echo   CMUHdermatology 一鍵推送
echo ========================================
echo.

REM ====== 0. 前置檢查 ======
where git >nul 2>nul || ( echo [錯誤] 找不到 git，請先裝 Git for Windows: https://git-scm.com/download/win & pause & exit /b 1 )
where python >nul 2>nul || ( echo [錯誤] 找不到 python，本腳本需要 python 來 bump 版本與算 SHA256 & pause & exit /b 1 )

if not exist "%REPO_ROOT%\src\cmuh_common\version.py" (
    echo [錯誤] 找不到 src\cmuh_common\version.py
    echo 請確認當前目錄為 CMUHdermatology repo 根
    pause & exit /b 1
)
if not exist "%REPO_ROOT%\scripts\bump_version.py" (
    echo [錯誤] 找不到 scripts\bump_version.py
    pause & exit /b 1
)

REM ====== 1. 安全自檢：確保 settings/ 沒被加入 staging ======
echo === [1/6] 安全自檢 ===
git ls-files --error-unmatch settings/ >nul 2>nul
if not errorlevel 1 (
    echo [安全錯誤] settings/ 目錄已被 git 追蹤！這會把密碼推上 Public repo
    echo 請執行：git rm -r --cached settings/ ^&^& 確認 .gitignore 有排除 settings/
    pause & exit /b 1
)
python "%REPO_ROOT%\scripts\sanity_check.py"
if errorlevel 1 ( echo [錯誤] sanity_check 失敗 & pause & exit /b 1 )
echo.

REM ====== 2. 顯示 git 狀態 ======
echo === [2/6] Git 狀態 ===
git status --short
set HAS_CHANGES=
for /f "delims=" %%i in ('git status --porcelain') do set HAS_CHANGES=1
if not defined HAS_CHANGES (
    echo [提示] 沒有變更，無需推送
    pause & exit /b 0
)
echo.

REM ====== 3. Bump 版本 ======
echo === [3/6] Bump 版本號 ===
python "%REPO_ROOT%\scripts\bump_version.py" || ( echo [錯誤] bump_version 失敗 & pause & exit /b 1 )

for /f "usebackq delims=" %%v in (`python "%REPO_ROOT%\scripts\read_version.py"`) do set NEW_VERSION=%%v
if "!NEW_VERSION!"=="" ( echo [錯誤] 讀版本失敗 & pause & exit /b 1 )
echo     新版本: !NEW_VERSION!
echo.

REM ====== 4. 同步 manifest.json + SHA256 ======
echo === [4/6] 同步 manifest.json（含 SHA256）===
python "%REPO_ROOT%\scripts\sync_manifest.py" "!NEW_VERSION!" || ( echo [錯誤] sync_manifest 失敗 & pause & exit /b 1 )
echo.

REM ====== 5. Commit ======
echo === [5/6] Commit ===
set COMMIT_MSG=%~1
if "!COMMIT_MSG!"=="" set COMMIT_MSG=Update v!NEW_VERSION!

git add -A
git commit -m "!COMMIT_MSG!" || ( echo [警告] commit 失敗 & pause & exit /b 1 )
echo.

REM ====== 6. Push ======
echo === [6/6] Push ===
for /f "usebackq delims=" %%b in (`git rev-parse --abbrev-ref HEAD`) do set CUR_BRANCH=%%b
echo     推送至 origin/!CUR_BRANCH! ...
git push origin !CUR_BRANCH! || ( echo [錯誤] push 失敗 & pause & exit /b 1 )

echo.
echo ============================================================
echo   推送完成！v!NEW_VERSION!
echo   其他電腦下次啟動會自動更新（manifest.json 5 分鐘 CDN 快取）
echo ============================================================
pause
endlocal
