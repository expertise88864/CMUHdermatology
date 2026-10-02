# 維護與交接手冊

適用於既有六支 Windows 程式。功能以目前程式及使用者定案為準；本手冊不授權操作真實 HIS、寄信、打卡或變更臨床規則。基準見 [風險清單](maintenance_risk_plan_2026-09-30.md)。

## 接手先核對

1. 讀取最新使用者定案、`AGENTS.md`、`REMOTE_CI_DELIVERY.md`、`_delivery_policy.json`；較早文件作歷史證據。
2. `git fetch origin` 後記錄完整 main SHA；查看 `git status --short`、版本、manifest、工作樹及未完成 review。保留他人修改，勿用 `git add -A` 混入交付。
3. pending 只有在後續 audit 列出**該完整 SHA**且附真正審查證據才結清；不能只查最近一次 passed。
4. 使用隔離 `codex/*` 分支。先有匿名重現及會失敗的回歸，再最小修正；無確認缺陷就停止改碼。

版本核對 `src/cmuh_common/version.py`、`manifest.json`。院內另記錄 `current.txt` 指向的版本及畫面版本；根目錄 `src/` 未必是本次啟動來源。HIS 版本是另一欄，不能當成程式版本。

## 可重跑的風險回歸

在隔離開發工作樹根目錄，用已安裝 `requirements.txt`、`requirements-lazy.txt` 的開發 Python 執行。不要在醫院正式資料夾測試或啟動六支程式作 smoke。現有 pytest 暫存／假物件隔離不代替人工核對新測試是否呼叫真實外部系統。

| 組別 | 應守住的行為 | 實機仍需確認 |
| --- | --- | --- |
| A 主程式 | 熱鍵註冊復原；F1–F3 目標、讀回、取消與部分完成；病人由醫師確認 | 權限、焦點、座標、HIS 控制項 |
| B 排班 | 照光／治療室硬限制、請假／鎖定、負荷、course 分散與不同醫師；保存不蓋新版本 | 真正月份可行性與人工核對 |
| C 會診 | 姓名／病歷號去識別化、寄送結果、重觸發與帳本去重 | HIS、郵件系統與收件情況 |
| D 打卡 | 點擊前檢查、已點擊未確認不重複、跨日與多帳號 | 官方網站、權限及網路 |
| E 啟動／更新 | 復原順序、版本指標、更新鎖失敗保守處理、守護競態 | 防毒、捷徑、排程與重啟 |
| F 診斷 | 舊失敗不被新摘要蓋掉；帳本、時間及存取異常保留處理指引 | 摘要與實際狀態一致 |

以下 PowerShell 命令可逐組重跑，每條完成後檢查 `$LASTEXITCODE`；非零就停止該批交付。清單是相關回歸，**不是完整 CI**；後續改檔名需同步維護。

```powershell
# A
python -m pytest -q -p no:cacheprovider tests/test_hotkey_registration_transaction_2026_09_03.py tests/test_hotkey_guardian.py tests/test_main_his_write_transaction_2026_09_25.py tests/test_main_his_memo_port_2026_09_24.py
# B
python -m pytest -q -p no:cacheprovider tests/test_pgy_workload_2026_09_22.py tests/test_pgy_category_fairness_2026_09_23.py tests/test_training_bands_2026_09_21.py tests/test_roster_course_spread_2026_09_08.py tests/test_roster_family_2026_09_07.py tests/test_roster_external_course_2026_09_07.py tests/test_roster_doctor_diversity_2026_09_08.py tests/test_roster_stale_overwrite_2026_08_19.py tests/test_roster_solve_day.py tests/test_roster_two_pgy_mode_2026_08_21.py tests/test_roster_two_pgy_week_rotation_2026_08_27.py
# C
python -m pytest -q -p no:cacheprovider tests/test_consult_privacy_2026_09_21.py tests/test_consult_typed_delivery_2026_09_24.py tests/test_consult_retrigger.py tests/test_delivery_ledger_2026_08_07.py tests/test_baseline_loss_2026_08_04.py
# D
python -m pytest -q -p no:cacheprovider tests/test_clock_action_state_2026_09_24.py tests/test_clock_click_confirmation_2026_09_23.py tests/test_autoclock_clock_safety.py
# E
python -m pytest -q -p no:cacheprovider tests/test_launcher_recovery_order_2026_08_12.py tests/test_version_pointer_l1_2026_08_09.py tests/test_updater_lock_failclosed_2026_07_30.py tests/test_watchdog_action_claim_race_2026_09_06.py
# F
python -m pytest -q -p no:cacheprovider tests/test_runtime_diagnostics_2026_09_28.py tests/test_debug_privacy_2026_07_30.py
```

本機快速交付檢查：

```powershell
python -m ruff check src scripts tests
python scripts/sanity_check.py
python -m unittest _test_delivery
# 在已暫存的最終內容上核對；不沿用修改前的結果。
$env:PYTHONPATH = 'src'
$env:PYTHONIOENCODING = 'utf-8'
python -c "from scripts import push_helper as p; from cmuh_common.version import CURRENT_VERSION; p.verify_staged_version_consistency(CURRENT_VERSION); p.verify_staged_manifest_hashes()"
```

必要時完整本機檢查，以 `.github/workflows/ci.yml` 當前命令為準：

```powershell
python -m pyright
python -m pytest -q -p no:cacheprovider --junitxml=junit.xml --cov=src --cov-report=json:cov.json --cov-report=term:skip-covered
python scripts/check_skips.py junit.xml
python scripts/check_coverage.py cov.json
python scripts/type_debt.py
```

恰兩位 PGY 月維持既有特殊模式：週二上午、週四下午、週五上午不排治療室，並保留週別輪替；逐時段初排由 Clerk 先入座，再用剩餘診位安排釋出的 PGY。完整月份還會執行 course 平衡：在必要工作、請假、鎖定與容量允許時，PGY 每週至少一次跟診與 Clerk／家醫／外訓最低需求共同受保護，之後才依 Clerk > 家醫 > 外訓 > PGY 追求目標次數。不能因 Clerk 尚未達目標就移除可行的 PGY 最低跟診；既有回歸為 `test_clerk_extra_clinics_never_displace_feasible_pgy_minimums`。判準是該月名單，不是請假後當日剩兩人。一般月仍先滿足照光＋治療室，週三下午僅照光。不要把特殊模式誤判為漏班，也不要沿用早期註解中的 PGY 優先於 Clerk；現行管線及回歸測試已採較新定案。

保存平台、Python／依賴版本及 exit codes，不修改門檻製造全綠。性能改動先按 [第三期量測](第三期排班效能與品質守門_2026-09-24.md) 與 `scripts/compare_day_roster.py` 對照基準／候選；記錄 SHA、環境、樣本與品質。沒有可量化收益，或硬限制／品質退步，就不採用重構。

## 發佈與補審

`scripts/push_helper.py` 會生成版本、manifest、暫存、commit 並 push，**不代替完整 review，且會暫存整個工作樹**；使用前確認只有本批修改。手動分步提交仍須相同生成及一致性守門，不繞 hook。

1. 相關回歸、Ruff、sanity、delivery unit tests 成功；程式改動生成版本與 manifest，再核對最終 index。純文件未改 manifest 涵蓋檔案可保持版本不變，但記錄理由。
2. 獨立 Codex review；Claude Code 固定 `claude-opus-5-5`／`high`／唯讀 `Read,Glob,Grep`，覆蓋 committed、staged、unstaged、相關 untracked。保存 diff 雜湊及 machine `modelUsage`；修 findings 後是新 diff，要重審。
3. 只有確定的供應商額度限制可留 pending；每個未審 commit 加 pending／high trailers，安排回復後補審。其他審查失敗不能當額度豁免。
4. `git config --get core.hooksPath` 應為本專案已追蹤 `.githooks`；使用既有安裝機制，不關閉 hook。
5. 固定完整 SHA 推候選，完整候選 CI 成功才發佈同一 SHA。下列每一步失敗停止；分支示例需換成本批分支。

```powershell
$candidateSha = git rev-parse HEAD
git push origin "${candidateSha}:refs/heads/codex/your-maintenance-batch"
python _delivery.py verify $candidateSha --phase candidate --wait 2700
git fetch origin
git merge-base --is-ancestor origin/main $candidateSha
# 上一步 exit code 必須為 0；main 前進則先整合並驗證新的 SHA。
git status --short
# 工作樹須乾淨且 HEAD 仍是 candidateSha，才能執行下一步。
git push origin "${candidateSha}:refs/heads/main"
python _delivery.py verify $candidateSha --phase main --wait 2700
```

`_delivery.py` 核對 policy 的 workflows、jobs、steps；不能以一個綠勾替代。缺失、應跑卻跳過、取消、逾時、讀不到都不通過。main 驗證後跑適用假環境 smoke，附證據才宣告交付；等待可分次查看，不更改門檻。

`--wait 2700` 是本機最多觀察 45 分鐘，不會修改 GitHub 工作流程的逾時限制。觀察期結束且回報 `in_progress` 時，該步仍未通過，停止後續發佈；先在 GitHub 核對原 run ID／完整 SHA 是否仍在執行，再對同一 SHA、同一 phase 重跑上面的 `verify` 命令繼續觀察。此命令只查證據，不會重啟 CI。不要只因本機等待結束就取消或重啟原工作流程。若 GitHub 已回報 failure、cancelled 或 timed_out，按真正失敗診斷；API 讀不到也不能推定仍在執行或已通過。

pending 獲乾淨全範圍 review 後，用**新空 audit commit**列出每個精確 SHA，不重寫既有 pending。audit 也走候選／正式 CI：

```text
Claude-Opus-5-Review: passed
Claude-Opus-5-Review-Effort: high
Claude-Opus-5-Reviewed-Commit: <被補審的完整 SHA>
```

每批保存：重現與風險、行為變化、SHA／版本、review／pending、本機結果、候選／正式 CI links、fake smoke、整包回退來源與實機待辦。合成測試不能寫成院內成功。

## 整包回退

更新切換 `versions/<版本>/src`、`current.txt`，也更新根目錄啟動器、`version_pointer.py` 等 extras。**只改指標、刪指標、只還原單檔或舊 src 都不是整包回退。** 操作狀態也不能倒退，否則可能重複寄信／打卡。

1. 維護者安排停機窗口，記錄版本及待確認寄送、打卡、HIS 操作；停止本專案六支程式、守護與其自動啟動／排程。確認已停止，勿終止其他專案的 Python／瀏覽器。
2. 在院內受控位置備份當下完整資料夾，以及最新設定、排班、帳本、待確認狀態；私有備份不進 Git 或診斷回報。保留權限及原安裝路徑。
3. 取得已驗證、資料格式相容的整包快照，包含程式、manifest、六支啟動器、版本解析器、版本目錄、指標與 extras。無配套快照／相容證據時，由維護者準備向前修正版，不臨時拼裝。
4. 暫時阻止更新立即換回新版，再還原配套程式；**保留最新設定、帳本、排班與待確認狀態**，不由舊快照覆蓋。核對捷徑／排程路徑。現有更新抑制在 `cmuh_common/update_policy.py`，是 `settings/.auto_update_suspended_until` 保存的絕對到期時間，並非永久停用。watchdog 偵測 crash loop 會以當下加一小時覆寫此期限，可能縮短手動設定的較長暫停；過期後 updater 仍可能重新拉回 main 的較高問題版本，防降版不會阻止。維護者先於隔離副本驗證：修復版尚未在 main 驗證完成前，保持 watchdog 及其自動啟動／排程停止，或在受控維護期間持續核對並延長有效期限，確保涵蓋整個修復窗口；無法維持這項保護就不恢復會觸發更新的工作／排程。不可只設定一次旗標便假設回退會永久維持，也不任意刪狀態。
5. 隔離副本跑相容性及上述回歸；院內以根目錄啟動器核對版本／畫面，依 [模板](hospital_acceptance_template.md) 驗收再恢復工作及排程。
6. 由醫師／院方核對停機與「可能已執行」操作。回退不撤回醫囑、郵件、官方打卡，勿為測試恢復而重做；記錄處理者、恢復時間及待辦。

遠端修復用新的較高版本交付完整相容程式，不倒推 main、不 force push、不降安全門檻。恢復更新前核對修復版；防降版設計使「把遠端 manifest 改低」不能作為回退方法。

## 匿名回報與人工處理

記錄版本／入口、平台、合成重現、錯誤分類及是否可人工工作。優先安全診斷匯出，分享前打開檢查；不傳病人姓名、病歷號、帳號、內文、帳密、token、原始畫面／log 或整個 settings。真實處理紀錄留在院內。

| 異常 | 人工處理 |
| --- | --- |
| F1–F3 目標不明、讀回不符或中止 | 醫師核對病人／醫囑，不盲目重按。依 2026-10-01 使用者決策，熱鍵不再辨識病人資訊列；病人由醫師確認。純 Excimer 的 F2／F3：原 HIS 主視窗／處置目標無法確認時不改身分 01；目標可確認但劑量不明，保留 01 嘗試並回報未完整完成。相同視窗與文字不能證明病人未切換。F1 不改身分 01。 |
| 排班無解／次數不一 | 匿名保留輸入／診斷，核對請假、鎖定、容量及硬限制；不移除必要照光／治療室人力湊公平。手動照光負調整代表總負荷真的減少。 |
| 會診查詢／名單未知 | 人工核對 HIS，不能當成確定空名單。 |
| 寄送待確認／部分失敗 | 核對寄件備份、帳本及拒收分類；寄送端接受不等於收件匣送達，不直接重寄。 |
| 打卡讀取不明／點擊待確認 | 核對官方紀錄，保留持久化保護，不重複點擊／刪狀態解鎖。 |
| 摘要時間異常／資料庫鎖定、損壞 | 核對時鐘／儲存並保留資料；診斷不取代寄送帳本／官方紀錄。 |

詳細定義見 [執行摘要](runtime_diagnostics_2026-09-28.md)。院內驗收由使用者安排；CI 不代替醫療或出勤確認。

## 離線穩定性與效能量測（2026-10-01）

這組工具供開發副本使用，須先安裝既有開發依賴；不要在院內正式程式資料夾執行。工具先隔離暫存設定，再匯入程式；禁止網路與外部程序，使用匿名資料，不啟動 HIS／寄信／打卡。結果 JSON 保留原始樣本及來源雜湊。工具失敗或遭到外部操作攔截時不能當通過；比較前先核對 `status: completed` 及空的錯誤清單。

2026-10-02 補正量測工具的重啟隔離：三支工具均在任何應用程式匯入前，清除繼承的 `CMUH_RESTART_HANDSHAKE`、`CMUH_RESTART_READY_EVENT`、`CMUH_RESTART_PARENT_CAPS`。禁止網路／外部程序仍不足以阻止依賴檢查經環境指定路徑改寫交握檔或存取 READY event。既有 launcher 隔離回歸增加兩支工具實際入口與 `boot`／`repair-only` 能力組合，使用匿名暫存 sentinel 與替身 event；原版四案失敗、補正後通過。較早沒有這組繼承環境的量測，不代表已驗證此保護；重跑時核對工具本身的雜湊。此補正不改正式重啟流程、業務狀態或臨床規則。

```powershell
# 真 Tk 畫面建構、重開、Windows GUI 物件與合成設定／匯出操作
python -X utf8 scripts/benchmark_runtime_offline.py --cycles 100 --operations --output "$env:TEMP/cmuh-runtime.json"
# 圖示檔遺失：不得為開視窗發出 HTTP 請求
python -X utf8 scripts/benchmark_runtime_offline.py --cycles 2 --missing-icon --output "$env:TEMP/cmuh-no-icon.json"
# 真正受限 executor，既有假 UI／來源；每四輪各測一種故障／恢復
python -X utf8 scripts/soak_outpatient_refresh.py --cycles 100 --output "$env:TEMP/cmuh-refresh-soak.json"
# 沿用既有排班案例，保留品質、警告與硬限制資料
python scripts/benchmark_day_roster.py --samples 3 --warmups 1 --case pgy4_clerk5_mix1 --case pgy2 --output "$env:TEMP/cmuh-roster.json"
```

兩支新工具皆可用 `--root <另一份完整開發 checkout>` 指向舊版；比較時使用相同工具、依賴、案例及電腦，交錯執行至少三組。新 process 的畫面時間須每組另啟動工具，不能拿同一 process 重建視窗冒充冷啟動。100 輪用來觀察趨勢，不代表連續運行一整天。資源先暖機，檢查最後數段是否仍上升；Python RSS 不必每輪精確歸零。GUI 物件須看 `gdi`／`user_objects`，一般 `handles` 與 RSS 平穩不能證明它們沒有累積；`null` 表示未知。

`benchmark_runtime_offline.py` 的主視窗保持隱藏，停用 deferred 業務啟動、熱鍵掛鉤、螢幕配置與正式程序清理。`first_idle_s` 是建構至處理首輪 Tk 事件，`probe_process_to_first_idle_s` 另包含量測工具匯入與隔離成本；都不是正式啟動器、依賴首次安裝、OS 冷磁碟快取或院內完整啟動時間。圖示與本地設定讀取是實作路徑，匯出是既有匿名小案例，300 筆門診訊息使用假 widget，不含真表格重畫。

程式關閉流程之後仍可能保留即將隨 process 結束的 timer。JSON 分開列出 `callbacks_after_app_cleanup_before_probe_cleanup` 與工具取消 timer／destroy 後的值，後者為零不代表程式本身取消了所有 timer。`soak_outpatient_refresh.py` 保留真正的外層 bounded executor 和內層 batch workers，但沿用假 root；假 root 的 callback 記錄數不能當成 Tk 洩漏量測。

圖示修正從版本 `2026.10.01.1` 起：Tk 只套用版本符合的本機 `assets/cmuh_app.ico`；圖示遺失、過舊或不可讀時保留預設圖示，畫面不再等候下載，也不增加背景排程。恢復配套安裝包的圖示與版本檔後重開即可。Windows 只使用既有大小圖示的 `WM_SETICON` 與兩次延遲重套，移除重複的 Tk ICO 載入；大小圖示用途見 [Microsoft WM_SETICON](https://learn.microsoft.com/zh-tw/windows/win32/winmsg/wm-seticon)。非 Tk 明確產製入口仍保留原下載行為，因此這不是「全專案不會下載圖示」的承諾。

本輪只採納測試成本與圖示兩項程式改善，量測工具／交接為第三批；不改排班求解、熱鍵、醫囑、寄送帳本、打卡 pending 或資料格式。具體數值、未採納方向及剩餘驗收見 [風險清單的 10 月補充](maintenance_risk_plan_2026-09-30.md#2026-10-01-後續穩定性與效能批次)。回退仍走上方整包流程，保留最新業務狀態；不能只取舊圖示程式碼覆蓋新 manifest。

## 啟動器離線基準補充（2026-10-01）

原有建構工具不能單獨證明根目錄啟動器的時間。第三批量測／交接補上 `scripts/benchmark_launcher_offline.py`：將啟動器、resolver、src、圖示複製到自建暫存目錄，實際執行復原、版本解析及 main 的 `__main__` 區段，直到真 Tk callback 能讀取 notebook。每筆是新的 Python process，依賴須已安裝；首筆無 `.deps_cache`，後兩筆保留同一隔離副本的快取及設定。

```powershell
python -X utf8 scripts/benchmark_launcher_offline.py --output "$env:TEMP/cmuh-launcher.json"
```

工具禁止網路、郵件、外部程序、熱鍵注入與程序終止；worker 必須是自建暫存副本。管理員提升、正式單例 mutex、監控／watchdog、螢幕配置及 deferred 業務啟動使用隔離替身，主視窗保持隱藏。`root_launcher_to_first_dispatch_seconds` 是啟動器入口至首輪有效 Tk callback；`worker_start_to_first_dispatch_seconds` 另含隔離 guard 匯入。這不是院內可見畫面、門診資料取得或熱鍵全部就緒的時間。`process_start_to_clean_exit_seconds` 包含收尾，不能當首屏時間。初次安裝依賴未量測、未混入基準；三筆樣本不報 p95，不用冷／暖差異宣稱本輪加速。成功須同時有 exit 0、`status: completed`、空錯誤清單與來源雜湊相符，不能沿用失敗前的舊 JSON。

以 `5451009`／v2026.10.01.5 的隔離副本測得：首筆約 3.12 秒、保留依賴快取後兩個新 process 約 1.80／1.72 秒（啟動器入口計）。100 次真 Tk 重建最後 20 筆固定 RSS 96,141,312 bytes、handles 392、GDI 411、USER 147、Python thread 1、child process 0；100 次真 executor 情境含 50 次確實發生的 OLD 晚回傳，未蓋掉新狀態。這是版本時點的離線樣本，不是全天運作或院內驗收。本輪仍只有測試成本、圖示修正、量測／交接三類批次。

HIS 基線、會診顯示與 maintain 降量是使用者後續另行指定的修正。較新的醫師定案移除照光病人 banner 自動識別，改由醫師確認病人；原視窗／處置欄／文字與 F12 檢查保留。不要用舊 goal 文字恢復已被取代的病人檢查。剩餘 pending 以精確 SHA／audit 核對，CI、離線量測與空 modelUsage 不代表 Opus 批准。
