@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
REM =============================================================================
REM build_all.bat — 一鍵打包 4 個 .exe（離線部署備案）
REM 一般部署用 deploy\installer.bat 走 Embedded Python；本檔是「全離線單檔」備案。
REM =============================================================================

cd /d "%~dp0\.."

echo === [0/6] 環境檢查 ===
where python >nul 2>nul || ( echo [錯誤] 找不到 python & pause & exit /b 1 )
python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [安裝] 正在安裝 PyInstaller ...
    python -m pip install --upgrade pyinstaller || ( echo [錯誤] 安裝失敗 & pause & exit /b 1 )
)
echo.

echo === [1/6] 清空舊 build/dist ===
if exist "build\__pyinstaller_temp__" rmdir /s /q "build\__pyinstaller_temp__"
if exist "dist" rmdir /s /q "dist"
mkdir "build\__pyinstaller_temp__"
echo.

set OPTS=--noconfirm --clean --workpath "build\__pyinstaller_temp__" --distpath "dist"

echo === [2/6] 主程式 ===
python -m PyInstaller %OPTS% deploy\specs\main.spec || ( echo 失敗 & pause & exit /b 1 )

echo === [3/6] 排班程式 ===
python -m PyInstaller %OPTS% deploy\specs\scheduler.spec || ( echo 失敗 & pause & exit /b 1 )

echo === [4/6] 打卡程式 ===
python -m PyInstaller %OPTS% deploy\specs\autoclock.spec || ( echo 失敗 & pause & exit /b 1 )

echo === [5/6] 座標偵測 ===
python -m PyInstaller %OPTS% deploy\specs\coord.spec || ( echo 失敗 & pause & exit /b 1 )

echo === [6/6] 整理 deploy\dist ===
if exist "deploy\dist" rmdir /s /q "deploy\dist"
mkdir "deploy\dist"
xcopy /e /i /y "dist\*" "deploy\dist\" >nul
copy /y "manifest.json" "deploy\dist\" >nul

echo.
echo ============================================================
echo  打包完成！輸出位置：deploy\dist\
echo ============================================================
dir /b /a:d "deploy\dist"
pause
endlocal
