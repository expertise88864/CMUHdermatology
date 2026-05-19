# -*- coding: utf-8 -*-
# 一鍵清除所有「皮膚科」相關 Windows 工作排程 (schtasks)。
# 必須存成 UTF-8 with BOM (檔頭 EF BB BF) 才能正確處理中文。
#
# 用途：
#   等同手動跑 `schtasks /Query /FO TABLE | findstr 皮膚科` 看一遍，
#   確認後一次 Unregister 所有匹配的排程。
#
# 適用情境：
#   - 整理「不該跑」的電腦上殘留的 ONLOGON / 周期 排程
#   - 重新規劃哪些電腦要跑哪些自動啟動
#   - 排程命名混亂、想砍乾淨重來

$ErrorActionPreference = 'Continue'
chcp 65001 > $null
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  一鍵清除所有「皮膚科」工作排程' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan

# 抓所有 TaskName 含「皮膚科」或「CMUH」的工作 (涵蓋 v9-v29 各版命名)
# 例如：CMUH皮膚科主程式自動啟動 / CMUH皮膚科守護程式_每2分鐘 / 等
$tasks = @()
try {
    $tasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
        $_.TaskName -like "*皮膚科*" -or $_.TaskName -like "CMUH*"
    }
} catch {
    Write-Host "Get-ScheduledTask 失敗: $_" -ForegroundColor Red
    exit 1
}

if (-not $tasks -or $tasks.Count -eq 0) {
    Write-Host ''
    Write-Host '✓ 沒有任何「皮膚科」相關排程，不需清除。' -ForegroundColor Green
    Write-Host ''
    Write-Host '提示：若仍有打卡/會診程式在跑，那不是 schtasks 觸發的，'
    Write-Host '可能是主程式的內層 watchdog。請去主程式設定頁取消勾選'
    Write-Host '「啟用 watchdog 監看背景程式」。'
    Write-Host ''
    exit 0
}

Write-Host ''
Write-Host '找到以下排程：' -ForegroundColor Yellow
$i = 0
foreach ($t in $tasks) {
    $i++
    $state = if ($t.State) { $t.State } else { '?' }
    Write-Host ("  [{0}] {1}  (State={2})" -f $i, $t.TaskName, $state)
}

Write-Host ''
Write-Host '⚠ 全部刪除後，下次登入這些排程不會再自動觸發。' -ForegroundColor Yellow
Write-Host '   已在跑的 process 不會被一併關閉。'
Write-Host ''
$confirm = Read-Host '輸入 y 確認刪除全部，其他鍵取消'
if ($confirm -ne 'y' -and $confirm -ne 'Y') {
    Write-Host ''
    Write-Host '已取消，未刪除任何排程。' -ForegroundColor Yellow
    exit 0
}

Write-Host ''
$ok = 0
$fail = 0
foreach ($t in $tasks) {
    try {
        Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false -ErrorAction Stop
        Write-Host ("  ✓ 已刪除: {0}" -f $t.TaskName) -ForegroundColor Green
        $ok++
    } catch {
        Write-Host ("  ✗ 刪除失敗: {0} → {1}" -f $t.TaskName, $_.Exception.Message) -ForegroundColor Red
        $fail++
    }
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ("結果：成功 {0} 個，失敗 {1} 個。" -f $ok, $fail)

# 驗證：再 query 一次，確認都清乾淨
$remaining = @()
try {
    $remaining = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
        $_.TaskName -like "*皮膚科*" -or $_.TaskName -like "CMUH*"
    }
} catch { }

if ($remaining -and $remaining.Count -gt 0) {
    Write-Host ''
    Write-Host '仍有殘留：' -ForegroundColor Yellow
    foreach ($t in $remaining) {
        Write-Host ("  - {0}" -f $t.TaskName)
    }
} else {
    Write-Host ''
    Write-Host '✓ 已完全清除，沒有任何「皮膚科」相關排程殘留。' -ForegroundColor Green
}

Write-Host ''
Write-Host '如要立刻關掉正在跑的 pythonw 進程：'
Write-Host '  taskkill /F /IM pythonw.exe   (注意：會把所有 pythonw 一起砍'
Write-Host '                                  包括主程式 — 請先儲存資料)'
Write-Host '或工作管理員 → 找 pythonw.exe → 結束特定 PID。'
Write-Host '============================================================' -ForegroundColor Cyan
exit 0
