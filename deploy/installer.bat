@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
REM =============================================================================
REM installer.bat — 中國醫皮膚科常用程式 一鍵部署器（Embedded Python 改良版）
REM
REM 邏輯：
REM   有 Python 3.10+ → 直接用系統 Python，git clone 原始碼，建桌面捷徑
REM   沒 Python      → 下載 Embedded Python 3.12 安裝在 <install_dir>\python_embed\
REM                    然後一樣 git clone 原始碼，捷徑指向 python_embed\pythonw.exe
REM   兩種情況都能線上自動更新（程式碼始終是純 .py）
REM =============================================================================

set GITHUB_OWNER=expertise88864
set GITHUB_REPO=CMUHdermatology
set GITHUB_BRANCH=main
set INSTALL_DIR=%USERPROFILE%\CMUHdermatology
set PY_EMBED_VERSION=3.12.7
set PY_EMBED_URL=https://www.python.org/ftp/python/%PY_EMBED_VERSION%/python-%PY_EMBED_VERSION%-embed-amd64.zip
set GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py

echo.
echo ============================================================
echo   中國醫皮膚科常用程式 安裝器
echo ============================================================
echo.

REM ====== 1. 偵測系統 Python ======
echo [1/5] 偵測 Python 環境...
set USE_SYSTEM_PY=0
where python >nul 2>nul
if not errorlevel 1 (
    for /f "usebackq delims=" %%v in (`python -c "import sys; print(1 if sys.version_info >= (3,10) else 0)"`) do set PY_OK=%%v
    if "!PY_OK!"=="1" set USE_SYSTEM_PY=1
)
if !USE_SYSTEM_PY!==1 (
    echo     [系統 Python] 已偵測到 Python ^>= 3.10，將直接使用
) else (
    echo     [Embedded Python] 未偵測到 Python ^>= 3.10，將下載 Embedded Python %PY_EMBED_VERSION%
)
echo.

REM ====== 2. 確認安裝路徑 ======
echo [2/5] 安裝位置: !INSTALL_DIR!
if exist "!INSTALL_DIR!" (
    echo     [警告] 路徑已存在，將更新內容
    choice /m "繼續？(Y=是 / N=取消)"
    if errorlevel 2 ( echo 取消 & pause & exit /b 0 )
) else (
    mkdir "!INSTALL_DIR!"
)
echo.

REM ====== 3. 下載原始碼 ======
echo [3/5] 下載原始碼（git clone 或 zip）...
where git >nul 2>nul
if errorlevel 1 (
    echo     找不到 git，改用 zip 下載
    set ZIP_URL=https://github.com/%GITHUB_OWNER%/%GITHUB_REPO%/archive/refs/heads/%GITHUB_BRANCH%.zip
    set ZIP_FILE=%TEMP%\cmuh_src.zip
    powershell -NoProfile -Command "Invoke-WebRequest -Uri '!ZIP_URL!' -OutFile '!ZIP_FILE!' -UseBasicParsing"
    if errorlevel 1 ( echo [錯誤] 下載失敗 & pause & exit /b 1 )
    powershell -NoProfile -Command "Expand-Archive -Path '!ZIP_FILE!' -DestinationPath '%TEMP%\cmuh_extract' -Force"
    xcopy /e /i /y "%TEMP%\cmuh_extract\%GITHUB_REPO%-%GITHUB_BRANCH%\*" "!INSTALL_DIR!\" >nul
    del /f /q "!ZIP_FILE!"
    rmdir /s /q "%TEMP%\cmuh_extract"
) else (
    if exist "!INSTALL_DIR!\.git" (
        echo     已存在 .git，執行 git pull
        pushd "!INSTALL_DIR!" & git pull origin %GITHUB_BRANCH% & popd
    ) else (
        if exist "!INSTALL_DIR!\src" (
            echo     [警告] 目錄非空但不是 git repo，先清空
            rmdir /s /q "!INSTALL_DIR!" & mkdir "!INSTALL_DIR!"
        )
        git clone --depth 1 --branch %GITHUB_BRANCH% "https://github.com/%GITHUB_OWNER%/%GITHUB_REPO%.git" "!INSTALL_DIR!"
    )
)
echo     原始碼就緒
echo.

REM ====== 4. Embedded Python（如需要）======
echo [4/5] 設定 Python 環境...
if !USE_SYSTEM_PY!==1 (
    set PYTHONW=pythonw
    set PIP_CMD=python -m pip
    echo     使用系統 Python
) else (
    set PY_DIR=!INSTALL_DIR!\python_embed
    if not exist "!PY_DIR!\python.exe" (
        echo     下載 Embedded Python %PY_EMBED_VERSION% ...
        set PY_ZIP=%TEMP%\python_embed.zip
        powershell -NoProfile -Command "Invoke-WebRequest -Uri '%PY_EMBED_URL%' -OutFile '!PY_ZIP!' -UseBasicParsing"
        if errorlevel 1 ( echo [錯誤] 下載 Python 失敗 & pause & exit /b 1 )
        mkdir "!PY_DIR!" 2>nul
        powershell -NoProfile -Command "Expand-Archive -Path '!PY_ZIP!' -DestinationPath '!PY_DIR!' -Force"
        del /f /q "!PY_ZIP!"

        REM 啟用 site-packages：把 python3XX._pth 內的 #import site 取消註解
        for %%f in ("!PY_DIR!\python*._pth") do (
            powershell -NoProfile -Command "(Get-Content '%%f') -replace '^#import site','import site' | Set-Content '%%f'"
        )

        echo     安裝 pip ...
        powershell -NoProfile -Command "Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile '%TEMP%\get-pip.py' -UseBasicParsing"
        "!PY_DIR!\python.exe" "%TEMP%\get-pip.py" --no-warn-script-location
        del /f /q "%TEMP%\get-pip.py"
    )
    set PYTHONW=!PY_DIR!\pythonw.exe
    set PIP_CMD="!PY_DIR!\python.exe" -m pip
    echo     使用 Embedded Python: !PY_DIR!
)
echo.

REM ====== 4b. 安裝 requirements ======
if exist "!INSTALL_DIR!\requirements.txt" (
    echo     安裝依賴套件 ...
    !PIP_CMD! install --upgrade -r "!INSTALL_DIR!\requirements.txt"
)
echo.

REM ====== 4c. [O8] 預編譯 .pyc 加速首次啟動 ======
if exist "!INSTALL_DIR!\src" (
    echo     預編譯 .pyc ...
    if !USE_SYSTEM_PY!==1 (
        python -m compileall -q -j 0 "!INSTALL_DIR!\src" 2>nul
    ) else (
        "!PY_DIR!\python.exe" -m compileall -q -j 0 "!INSTALL_DIR!\src" 2>nul
    )
)
echo.

REM ====== 5. 建桌面捷徑 ======
echo [5/5] 建立桌面捷徑...
set DESKTOP=%USERPROFILE%\Desktop

call :make_shortcut "中國醫皮膚科主程式" "!PYTHONW!" "!INSTALL_DIR!\中國醫皮膚科主程式.pyw"
call :make_shortcut "中國醫皮膚科排班程式" "!PYTHONW!" "!INSTALL_DIR!\中國醫皮膚科排班程式.pyw"
call :make_shortcut "中國醫皮膚科打卡程式" "!PYTHONW!" "!INSTALL_DIR!\中國醫皮膚科打卡程式.pyw"
call :make_shortcut "中國醫皮膚科點座標偵測程式" "!PYTHONW!" "!INSTALL_DIR!\中國醫皮膚科點座標偵測程式.pyw"
call :make_shortcut "中國醫皮膚科會診查詢程式" "!PYTHONW!" "!INSTALL_DIR!\中國醫皮膚科會診查詢程式.pyw"

echo.
echo ============================================================
echo   安裝完成！桌面已建立 5 個捷徑
echo   每次啟動會自動檢查 GitHub 上的更新
echo   手動更新：在 !INSTALL_DIR! 執行 pull_update.bat
echo ============================================================
pause & exit /b 0


:make_shortcut
set SHORTCUT_NAME=%~1
set TARGET=%~2
set ARGS=%~3
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%DESKTOP%\%SHORTCUT_NAME%.lnk'); $s.TargetPath = '%TARGET%'; if ('%ARGS%' -ne '') { $s.Arguments = '\"%ARGS%\"' }; $s.WorkingDirectory = '!INSTALL_DIR!'; $s.IconLocation = '!INSTALL_DIR!\assets\cmuh_app.ico'; $s.Save()" 2>nul
exit /b 0
