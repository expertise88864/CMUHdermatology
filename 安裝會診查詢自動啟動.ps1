# -*- coding: utf-8 -*-
# 安裝會診查詢自動啟動 — 實際邏輯。由同名 .cmd wrapper 提權後呼叫。
# 必須存成 UTF-8 with BOM（檔首 EF BB BF），Windows PowerShell 5.1 才會正確讀中文。

$ErrorActionPreference = 'Stop'
chcp 65001 > $null
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

$scriptDir = Split-Path -Parent $PSCommandPath
$pyw       = Join-Path $scriptDir '中國醫皮膚科會診查詢程式.pyw'
$taskName  = 'CMUH皮膚科會診查詢自動啟動'
$user      = "$env:USERDOMAIN\$env:USERNAME"

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  安裝會診查詢自動啟動排程' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''

# --- 找 .pyw ---
if (-not (Test-Path $pyw)) {
    Write-Host "[錯誤] 找不到會診查詢主程式：" -ForegroundColor Red
    Write-Host "       $pyw"
    Write-Host '請把這對 .cmd/.ps1 放在跟 .pyw 同一目錄。'
    exit 1
}

# --- 找 pythonw.exe（先 PATH，再 python_embed）---
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $embed = Join-Path $scriptDir 'python_embed\pythonw.exe'
    if (Test-Path $embed) { $pythonw = $embed }
}
if (-not $pythonw) {
    Write-Host '[錯誤] 找不到 pythonw.exe' -ForegroundColor Red
    Write-Host '       請先安裝 Python 3.10+ 或執行 deploy\installer.bat'
    exit 1
}

Write-Host '排程設定：'
Write-Host "  名稱      ：$taskName"
Write-Host "  pythonw   ：$pythonw"
Write-Host "  .pyw      ：$pyw"
Write-Host "  使用者    ：$user"
Write-Host '  觸發      ：每次此使用者登入 Windows'
Write-Host '  執行權限  ：最高權限（admin，不跳 UAC）'
Write-Host ''

# --- 偵測舊的 shell:startup 捷徑並提示移除（避免雙開）---
$oldLnk = Join-Path ([Environment]::GetFolderPath('Startup')) '中國醫皮膚科會診查詢程式.lnk'
if (Test-Path $oldLnk) {
    Write-Host "偵測到舊的開機捷徑：" -ForegroundColor Yellow
    Write-Host "  $oldLnk"
    Write-Host '建議移除，否則登入後會跳 UAC 又雙開兩個實例。' -ForegroundColor Yellow
    $ans = Read-Host '現在移除？(Y/N)'
    if ($ans -match '^[Yy]') {
        Remove-Item $oldLnk -Force
        Write-Host '  已移除。' -ForegroundColor Green
    }
    Write-Host ''
}

# --- 建立排程 ---
Write-Host '建立排程中...' -ForegroundColor DarkGray
try {
    $action = New-ScheduledTaskAction `
        -Execute $pythonw `
        -Argument ('"' + $pyw + '"') `
        -WorkingDirectory $scriptDir

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user

    $principal = New-ScheduledTaskPrincipal `
        -UserId $user `
        -LogonType Interactive `
        -RunLevel Highest

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Force | Out-Null

    Write-Host '排程已建立 ✓' -ForegroundColor Green
} catch {
    Write-Host "[失敗] 建立排程失敗：$_" -ForegroundColor Red
    exit 1
}

# --- 立即啟動一次測試 ---
Write-Host ''
Write-Host '立即啟動一次測試...' -ForegroundColor DarkGray
try {
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 2
    Write-Host '已觸發。請檢查系統匣是否出現會診查詢圖示。' -ForegroundColor Green
} catch {
    Write-Host "（無法立即啟動：$_，但下次登入會自動執行）" -ForegroundColor Yellow
}

# --- 驗證 ---
Write-Host ''
Write-Host '驗證設定：' -ForegroundColor DarkGray
$t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($t) {
    Write-Host "  狀態     ：$($t.State)"
    Write-Host "  RunLevel ：$($t.Principal.RunLevel)  (應為 Highest)"
    Write-Host "  LogonType：$($t.Principal.LogonType) (應為 Interactive)"
    Write-Host "  Trigger  ：$($t.Triggers[0].CimClass.CimClassName) (應為 MSFT_TaskLogonTrigger)"
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '完成！' -ForegroundColor Green
Write-Host '  - 下次登入此 Windows 帳號 → 自動以 admin 啟動會診查詢'
Write-Host '  - 不會跳 UAC（排程器直接給 elevated token）'
Write-Host '  - 查看排程：開始 → 工作排程器 → 工作排程器程式庫'
Write-Host "            → 找 [$taskName]"
Write-Host '  - 移除排程：雙擊 移除會診查詢自動啟動.cmd'
Write-Host '============================================================' -ForegroundColor Cyan
exit 0
