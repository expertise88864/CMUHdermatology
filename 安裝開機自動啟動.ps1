# -*- coding: utf-8 -*-
# 安裝開機自動啟動 — 三個程式可勾選哪些要在登入時自動啟動。
# 必須存成 UTF-8 with BOM。

$ErrorActionPreference = 'Stop'
chcp 65001 > $null
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$scriptDir = Split-Path -Parent $PSCommandPath
$user      = "$env:USERDOMAIN\$env:USERNAME"

# === 三個目標程式定義 ===
# defaultChecked：主程式預設勾選（每台電腦都要）；打卡、會診查詢預設不勾
# （只需要在一台電腦執行）
$programs = @(
    [pscustomobject]@{
        Key='main';     TaskName='CMUH皮膚科主程式自動啟動';
        Pyw='中國醫皮膚科主程式.pyw';
        Display='中國醫皮膚科主程式';
        Hint='掛號監控／熱鍵自動化（每台電腦都建議勾）';
        DefaultChecked=$true },
    [pscustomobject]@{
        Key='clock';    TaskName='CMUH皮膚科打卡自動啟動';
        Pyw='中國醫皮膚科打卡程式.pyw';
        Display='中國醫皮膚科打卡程式';
        Hint='排班自動打卡（只需要一台電腦執行）';
        DefaultChecked=$false },
    [pscustomobject]@{
        Key='consult';  TaskName='CMUH皮膚科會診查詢自動啟動';
        Pyw='中國醫皮膚科會診查詢程式.pyw';
        Display='中國醫皮膚科會診查詢程式';
        Hint='每日 12:30 / 17:00 擷取會診單寄信（只需要一台電腦執行）';
        DefaultChecked=$false },
    [pscustomobject]@{
        Key='watchdog'; TaskName='CMUH皮膚科守護程式_每2分鐘';
        Pyw='中國醫皮膚科守護程式.pyw';
        Display='中國醫皮膚科守護程式 (外層備援，每 2 分鐘)';
        Hint='主程式內已有內層 watchdog 每 30s 巡邏；外層備援 schtasks 每 2 分鐘觸發跑一次 (RAM ≈ 0)，主程式掛了會接手';
        DefaultChecked=$true;
        Periodic=$true;
        ScriptRelPath='src\watchdog_runner.py';
        ScriptArgs='--once' }
)

# === 檢查 .pyw 存在 ===
foreach ($p in $programs) {
    $pywPath = Join-Path $scriptDir $p.Pyw
    if (-not (Test-Path $pywPath)) {
        [System.Windows.Forms.MessageBox]::Show(
            "找不到主程式檔案：`n$pywPath`n`n請把這個 .cmd / .ps1 放在跟 .pyw 同一目錄。",
            '錯誤', 'OK', 'Error') | Out-Null
        exit 1
    }
    $p | Add-Member -NotePropertyName PywPath -NotePropertyValue $pywPath
}

# === 找 pythonw.exe ===
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $embed = Join-Path $scriptDir 'python_embed\pythonw.exe'
    if (Test-Path $embed) { $pythonw = $embed }
}
if (-not $pythonw) {
    [System.Windows.Forms.MessageBox]::Show(
        "找不到 pythonw.exe`n`n請先安裝 Python 3.10+ 或執行 deploy\installer.bat。",
        '錯誤', 'OK', 'Error') | Out-Null
    exit 1
}

# === 偵測現有排程，prefill 勾選狀態 ===
# 若某個排程已存在，預設勾選那一格；不存在則按 DefaultChecked
foreach ($p in $programs) {
    $existing = Get-ScheduledTask -TaskName $p.TaskName -ErrorAction SilentlyContinue
    $checked = if ($existing) { $true } else { $p.DefaultChecked }
    $p | Add-Member -NotePropertyName ExistingTask -NotePropertyValue ([bool]$existing)
    $p | Add-Member -NotePropertyName InitialChecked -NotePropertyValue ([bool]$checked)
}

# === 建立 GUI ===
# 注意：之前 Size=560x420 在某些 DPI / Chinese font 下會把按鈕擠出可視範圍
# （bug 報告：2026-05-18 1280x1024 + admin cmd 視窗下，套用設定按鈕看不到）。
# 改用 AutoSize=false 但給足空間（580x560）+ 按鈕 anchor bottom-right，
# 並用 Padding 而不是手算 y 座標。
$form = New-Object System.Windows.Forms.Form
$form.Text = '中國醫皮膚科 — 開機自動啟動設定'
$form.ClientSize = New-Object System.Drawing.Size(580, 610)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.Font = New-Object System.Drawing.Font('Segoe UI', 10)

$lblHeader = New-Object System.Windows.Forms.Label
$lblHeader.Text = "勾選要在此使用者登入時【自動啟動】的程式：`n（會以 admin 身份啟動，不跳 UAC）"
$lblHeader.Location = New-Object System.Drawing.Point(15, 12)
$lblHeader.Size = New-Object System.Drawing.Size(520, 40)
$lblHeader.ForeColor = [System.Drawing.Color]::DarkSlateGray
$form.Controls.Add($lblHeader)

$lblPython = New-Object System.Windows.Forms.Label
$lblPython.Text = "pythonw：$pythonw"
$lblPython.Location = New-Object System.Drawing.Point(15, 55)
$lblPython.Size = New-Object System.Drawing.Size(520, 18)
$lblPython.ForeColor = [System.Drawing.Color]::Gray
$lblPython.Font = New-Object System.Drawing.Font('Consolas', 8)
$form.Controls.Add($lblPython)

$lblUser = New-Object System.Windows.Forms.Label
$lblUser.Text = "使用者：$user"
$lblUser.Location = New-Object System.Drawing.Point(15, 73)
$lblUser.Size = New-Object System.Drawing.Size(520, 18)
$lblUser.ForeColor = [System.Drawing.Color]::Gray
$lblUser.Font = New-Object System.Drawing.Font('Consolas', 8)
$form.Controls.Add($lblUser)

# 勾選框
$checkboxes = @{}
$y = 100
foreach ($p in $programs) {
    $cb = New-Object System.Windows.Forms.CheckBox
    $cb.Text = $p.Display + $(if ($p.ExistingTask) { '  (目前已設定)' } else { '' })
    $cb.Location = New-Object System.Drawing.Point(25, $y)
    $cb.Size = New-Object System.Drawing.Size(500, 22)
    $cb.Checked = $p.InitialChecked
    $cb.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
    $form.Controls.Add($cb)
    $checkboxes[$p.Key] = $cb

    $hint = New-Object System.Windows.Forms.Label
    $hint.Text = "    $($p.Hint)"
    $hint.Location = New-Object System.Drawing.Point(45, ($y + 22))
    $hint.Size = New-Object System.Drawing.Size(490, 18)
    $hint.ForeColor = [System.Drawing.Color]::DimGray
    $hint.Font = New-Object System.Drawing.Font('Microsoft JhengHei UI', 9)
    $form.Controls.Add($hint)

    $y += 60
}

# 說明區
$lblNote = New-Object System.Windows.Forms.Label
$lblNote.Text = (
    "備註：" +
    "`n  • 勾選 → 建立排程（登入時自動以 admin 啟動，不跳 UAC）" +
    "`n  • 取消勾選 → 移除排程（不再自動啟動）" +
    "`n  • 大部分電腦只需要勾【主程式】；打卡/會診查詢全皮膚科只需一台電腦執行")
$lblNote.Location = New-Object System.Drawing.Point(15, ($y + 5))
$lblNote.Size = New-Object System.Drawing.Size(550, 90)
$lblNote.ForeColor = [System.Drawing.Color]::DarkSlateGray
$form.Controls.Add($lblNote)

# OK / Cancel 按鈕（anchor 到右下角，無論 form 多高都看得到）
$formH = $form.ClientSize.Height
$formW = $form.ClientSize.Width

$btnOk = New-Object System.Windows.Forms.Button
$btnOk.Text = '套用設定'
$btnOk.Location = New-Object System.Drawing.Point(($formW - 230), ($formH - 50))
$btnOk.Size = New-Object System.Drawing.Size(105, 36)
$btnOk.Anchor = [System.Windows.Forms.AnchorStyles]::Bottom -bor [System.Windows.Forms.AnchorStyles]::Right
$btnOk.DialogResult = [System.Windows.Forms.DialogResult]::OK
$btnOk.BackColor = [System.Drawing.Color]::FromArgb(0, 120, 215)
$btnOk.ForeColor = [System.Drawing.Color]::White
$btnOk.FlatStyle = 'Flat'
$form.Controls.Add($btnOk)
$form.AcceptButton = $btnOk

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text = '取消'
$btnCancel.Location = New-Object System.Drawing.Point(($formW - 120), ($formH - 50))
$btnCancel.Size = New-Object System.Drawing.Size(105, 36)
$btnCancel.Anchor = [System.Windows.Forms.AnchorStyles]::Bottom -bor [System.Windows.Forms.AnchorStyles]::Right
$btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.Controls.Add($btnCancel)
$form.CancelButton = $btnCancel

$result = $form.ShowDialog()
if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
    Write-Host '使用者取消，未做任何變更。' -ForegroundColor Yellow
    exit 0
}

# === 套用設定 ===
Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  套用開機自動啟動設定' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''

$summary = @()
foreach ($p in $programs) {
    $cb = $checkboxes[$p.Key]
    $shouldEnable = $cb.Checked
    if ($shouldEnable) {
        try {
            $principal = New-ScheduledTaskPrincipal `
                -UserId $user `
                -LogonType Interactive `
                -RunLevel Highest
            # Periodic 程式 (watchdog 外層 C) 用「每 2 分鐘」觸發跑 --once
            # 其餘程式用 ONLOGON 常駐
            if ($p.PSObject.Properties.Name -contains 'Periodic' -and $p.Periodic) {
                $scriptFullPath = Join-Path $scriptDir $p.ScriptRelPath
                $action = New-ScheduledTaskAction `
                    -Execute $pythonw `
                    -Argument ('"' + $scriptFullPath + '" ' + $p.ScriptArgs) `
                    -WorkingDirectory $scriptDir
                # -Once + RepetitionInterval 是「每 N 分鐘觸發一次跑一遍」
                # RepetitionDuration 空字串 = 無限重複
                $startTime = (Get-Date).AddMinutes(1)
                $trigger = New-ScheduledTaskTrigger -Once -At $startTime
                $trigger.Repetition.Interval = 'PT2M'
                $trigger.Repetition.Duration = ''
                $settings = New-ScheduledTaskSettingsSet `
                    -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries `
                    -StartWhenAvailable `
                    -MultipleInstances IgnoreNew `
                    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
            } else {
                $action = New-ScheduledTaskAction `
                    -Execute $pythonw `
                    -Argument ('"' + $p.PywPath + '"') `
                    -WorkingDirectory $scriptDir
                $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
                $settings = New-ScheduledTaskSettingsSet `
                    -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries `
                    -StartWhenAvailable `
                    -MultipleInstances IgnoreNew `
                    -ExecutionTimeLimit ([TimeSpan]::Zero)
            }
            Register-ScheduledTask `
                -TaskName $p.TaskName `
                -Action $action `
                -Trigger $trigger `
                -Principal $principal `
                -Settings $settings `
                -Force | Out-Null
            Write-Host "  ✓ 已啟用：$($p.Display)" -ForegroundColor Green
            Write-Host "    排程名稱：$($p.TaskName)"
            $summary += [pscustomobject]@{Program=$p.Display; Action='啟用'; Status='OK'}
        } catch {
            Write-Host "  ✗ 啟用失敗：$($p.Display) → $_" -ForegroundColor Red
            $summary += [pscustomobject]@{Program=$p.Display; Action='啟用'; Status="失敗 $_"}
        }
    } else {
        # 沒勾 → 如果排程存在則移除
        if ($p.ExistingTask) {
            try {
                Unregister-ScheduledTask -TaskName $p.TaskName -Confirm:$false
                Write-Host "  ✓ 已移除：$($p.Display)" -ForegroundColor Yellow
                Write-Host "    （原本有排程，現在不再自動啟動）"
                $summary += [pscustomobject]@{Program=$p.Display; Action='移除'; Status='OK'}
            } catch {
                Write-Host "  ✗ 移除失敗：$($p.Display) → $_" -ForegroundColor Red
                $summary += [pscustomobject]@{Program=$p.Display; Action='移除'; Status="失敗 $_"}
            }
        } else {
            Write-Host "  ─ 略過：$($p.Display)（沒勾且原本就沒設）" -ForegroundColor Gray
            $summary += [pscustomobject]@{Program=$p.Display; Action='未啟用'; Status='-'}
        }
    }
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '驗證：' -ForegroundColor DarkGray
foreach ($p in $programs) {
    $t = Get-ScheduledTask -TaskName $p.TaskName -ErrorAction SilentlyContinue
    if ($t) {
        Write-Host ("  [{0}] {1}  RunLevel={2}  State={3}" -f $p.Display, $t.TaskName, $t.Principal.RunLevel, $t.State)
    } else {
        Write-Host "  [$($p.Display)] (未設定排程)" -ForegroundColor Gray
    }
}
Write-Host ''
Write-Host '完成！下次登入時，被勾選的程式會自動以 admin 啟動（不跳 UAC）。' -ForegroundColor Green
Write-Host '要修改：重跑這個安裝器。'
Write-Host '要全部移除：執行 移除開機自動啟動.cmd'
Write-Host '============================================================' -ForegroundColor Cyan
exit 0
