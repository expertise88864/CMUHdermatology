# -*- coding: utf-8 -*-
# 移除開機自動啟動 — 移除 4 個程式的排程任務。
# 必須存成 UTF-8 with BOM。

$ErrorActionPreference = 'Continue'
chcp 65001 > $null
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

$tasks = @(
    @{TaskName='CMUH皮膚科主程式自動啟動';     Display='中國醫皮膚科主程式'},
    @{TaskName='CMUH皮膚科打卡自動啟動';       Display='中國醫皮膚科打卡程式'},
    @{TaskName='CMUH皮膚科會診查詢自動啟動';   Display='中國醫皮膚科會診查詢程式'},
    @{TaskName='CMUH皮膚科守護程式自動啟動';   Display='中國醫皮膚科守護程式'}
)

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  移除所有開機自動啟動排程' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''

foreach ($t in $tasks) {
    $exists = Get-ScheduledTask -TaskName $t.TaskName -ErrorAction SilentlyContinue
    if ($exists) {
        try {
            Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
            Write-Host "  ✓ 已移除：$($t.Display) ($($t.TaskName))" -ForegroundColor Green
        } catch {
            Write-Host "  ✗ 移除失敗：$($t.Display) → $_" -ForegroundColor Red
        }
    } else {
        Write-Host "  ─ 略過：$($t.Display)（本來就沒設定）" -ForegroundColor Gray
    }
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '完成。下次登入後這 3 個程式都不會自動啟動。' -ForegroundColor Green
Write-Host '（目前已在跑的常駐實例不受影響，要手動退出才會關掉）'
Write-Host '要重新啟用：執行 安裝開機自動啟動.cmd'
Write-Host '============================================================' -ForegroundColor Cyan
exit 0
