# -*- coding: utf-8 -*-
# Diagnose duplicate CMUH autoclock processes without killing anything.

$ErrorActionPreference = 'Continue'
chcp 65001 > $null
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

$keywords = @(
    '中國醫皮膚科打卡程式.pyw',
    'src/autoclock.py',
    'src\autoclock.py'
)

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  診斷打卡程式是否重複執行' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan

$rows = @()
try {
    $rows = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object {
            $cmd = [string]$_.CommandLine
            foreach ($kw in $keywords) {
                if ($cmd -like "*$kw*") { return $true }
            }
            return $false
        } |
        Sort-Object CreationDate |
        Select-Object ProcessId, Name, CreationDate, CommandLine
} catch {
    Write-Host "讀取 process 清單失敗：$_" -ForegroundColor Red
    exit 1
}

if (-not $rows -or $rows.Count -eq 0) {
    Write-Host ''
    Write-Host '✓ 目前沒有偵測到打卡程式 process。' -ForegroundColor Green
    exit 0
}

Write-Host ''
Write-Host ("找到 {0} 個疑似打卡程式 process：" -f $rows.Count) -ForegroundColor Yellow
foreach ($r in $rows) {
    $started = if ($r.CreationDate) {
        [Management.ManagementDateTimeConverter]::ToDateTime($r.CreationDate).ToString('yyyy-MM-dd HH:mm:ss')
    } else {
        '?'
    }
    Write-Host ''
    Write-Host ("PID: {0}  Name: {1}  Start: {2}" -f $r.ProcessId, $r.Name, $started)
    Write-Host ("  {0}" -f $r.CommandLine)
}

Write-Host ''
if ($rows.Count -gt 1) {
    Write-Host '⚠ 偵測到多個打卡 process。更新到 v2026.05.25.7+ 後，建議重開機一次清掉舊 process/tray 殘影。' -ForegroundColor Yellow
    Write-Host '  如需手動處理，請在工作管理員依上方 PID 結束舊版重複 process。'
} else {
    Write-Host '✓ 只有一個打卡 process，屬於正常狀態。' -ForegroundColor Green
}
exit 0
