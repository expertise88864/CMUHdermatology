# -*- coding: utf-8 -*-
# 移除會診查詢自動啟動 — 由同名 .cmd wrapper 提權後呼叫。
# 必須存成 UTF-8 with BOM（檔首 EF BB BF）。

$ErrorActionPreference = 'Stop'
chcp 65001 > $null
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

$taskName = 'CMUH皮膚科會診查詢自動啟動'

Write-Host ''
Write-Host "移除排程：$taskName" -ForegroundColor Cyan
Write-Host ''

try {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host '已移除 ✓' -ForegroundColor Green
    Write-Host '下次登入後不會再自動啟動會診查詢程式。'
    Write-Host '（目前系統匣的常駐實例不受影響，要手動退出才會關掉）'
    exit 0
} catch [Microsoft.Management.Infrastructure.CimException] {
    Write-Host "排程 [$taskName] 本來就不存在，無需移除。" -ForegroundColor Yellow
    exit 0
} catch {
    Write-Host "[失敗] 移除排程時發生錯誤：$_" -ForegroundColor Red
    exit 1
}
