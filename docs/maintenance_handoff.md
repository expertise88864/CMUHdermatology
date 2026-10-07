# 維護與交接手冊

適用於既有六支 Windows 程式。功能以目前程式及使用者定案為準；本手冊不授權操作真實 HIS、寄信、打卡或變更臨床規則。基準見 [風險清單](maintenance_risk_plan_2026-09-30.md)。

## 安全開發與候選發佈（2026-10-07）

本節是開發操作入口；下方日期章節保留歷史證據。每次接手重新 fetch、記錄完整 SHA、
版本及工作樹，不把本文中的歷史 SHA 當永遠有效的最新版。審查／交付完成狀態以
實際模型證據、精確 trailer／audit 與 exact-SHA CI 為準，本節不能替代批准。

### 建立開發環境

保留既有 Desktop 副本；新任務從最新 main 建立另一個完整副本，再切本批候選分支。
下例不複製 settings，也不修改全域 Git 設定；分支名稱須換成本批唯一名稱：

```powershell
$devClone = Join-Path $env:TEMP ('cmuh-dev-' + [guid]::NewGuid().ToString('N'))
git clone --branch main --single-branch https://github.com/expertise88864/CMUHdermatology.git $devClone
if ($LASTEXITCODE -ne 0) { throw '無法取得新的開發副本' }
Set-Location $devClone
git switch -c codex/your-development-batch
if ($LASTEXITCODE -ne 0) { throw '候選分支未建立' }
git config --local core.hooksPath .githooks
if ($LASTEXITCODE -ne 0) { throw '專案 hook 未設定' }
git rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw '無法確認來源 SHA' }
git status --short
if ($LASTEXITCODE -ne 0) { throw '無法確認來源狀態' }
```

在完整、隔離的 Windows 開發 checkout 使用 Python 3.13（與正式 CI 對照）。
`tools/dev-env-setup.py` 無參數或 `check` 只讀取環境中繼資料、hook 與 CLI 是否存在，
不匯入應用程式、不安裝、不修改全域設定。`--auth` 才額外查詢登入狀態，只輸出判定，
不顯示帳戶、token 或供應商原始訊息。CLI 找到與模型額度可用是不同條件。
`not_checked`／`manual` 不能當已驗證；任何 failed 都是非零退出碼。

```powershell
python -X utf8 tools/dev-env-setup.py check
# 初次沒有 venv 預期非零；先查看缺口，再明確建立：
python -X utf8 tools/dev-env-setup.py apply --venv .venv
if ($LASTEXITCODE -ne 0) { throw '開發環境未完成；依列出的 failed 項目處理' }
$devPython = (Resolve-Path '.venv/Scripts/python.exe').Path
& $devPython -X utf8 tools/dev-env-setup.py check --auth
if ($LASTEXITCODE -ne 0) { throw '開發環境或登入狀態尚未確認' }
```

apply 沿用兩份 requirements 與既有 runtime constraints，開發工具另裝，僅寫入新建立的
`.venv` 或 `.venv-名稱`。現有環境只檢查，不原地修復；失敗保留未完成環境，
使用新的 `.venv-rebuild` 等名稱重建，再把 `$devPython` 指向該環境。這讓舊環境可繼續使用。
新目錄以互斥建立保留名稱；同名建立競爭會停止，不覆寫另一個程序的環境。
唯讀套件查詢明確停用 bytecode 寫入，不靠會被隔離模式忽略的環境變數。
不自動安裝全域 npm／pip、不改模型、effort、MCP、全域 CLAUDE.md 或帳戶設定。
缺少 CLI／登入或 hook 時按當前官方工具與既有 hook 安裝流程處理，不能繞過檢查。
不要用此工具搬遷院內正式環境；正式機器的狀態／設定搬遷另依下方整包回退與院內驗收程序。

測試沿用 `tests/` 的匿名 fixtures，自建暫存 settings／SQLite／時鐘與假 HIS；
不需要複製正式 settings，不開啟 `.pyw` 或直接執行六支來源入口作 smoke。

### 發佈工具契約

`push.bat` 與 `scripts/push_helper.py` 共用契約：

- 無參數／`--help`：顯示說明；不存取 Git、不生成版本。
- `check`：唯讀核對安全、index 版本與 manifest 雜湊。它不是 lint、完整 CI 或 review。
- `publish`：必須指定提交訊息及精確來源範圍，才準備候選並推送 `codex/*`。
- 未知參數、錯字、裸提交訊息、缺漏或互斥參數直接拒絕；舊 `--sanity-only` 不支援。

publish 使用 HEAD 為基底，`--path` 可重複，每項是一般檔案的精確 repo 相對路徑，
不接受目錄、glob、忽略檔、符號連結、路徑越界或手動指定的版本／manifest。
預設 `--source index`，只取指定檔案已暫存的內容，包括部分暫存；
明確指定 `--source worktree` 才取指定檔案的完整現況，包含指定的新檔／刪除。
`--committed` 則只使用 HEAD，不能和 `--path`／`--source` 混用。
HEAD 已有的提交都在候選祖先範圍內，也必須納入任務與 review 的核對。

候選在 repo 外的新本機副本生成版本、manifest、提交並驗證；原工作樹的檔案、index、
分支與本機 refs 保留，無關 staged／unstaged／untracked 不會加入候選。
候選套用選定內容後會再次核對 settings 與 `.gitignore`，來源通過不代表候選已通過。
來源 HEAD／分支／指定內容在驗證期間改變會停止推送。
工具會保留候選與 `release.json`（階段、SHA、選定路徑及 patch 雜湊）；
失敗／中止不自動 reset 或清理原修改，依 failed_at 核對已完成的步驟。
不可因發生錯誤就盲目重跑 publish：若已建立候選 commit，先在保留副本修正／重新驗證，
沿用下方分步交付規則；若推送回應不明，先核對遠端 exact SHA。

```powershell
& $devPython -X utf8 scripts/push_helper.py check
if ($LASTEXITCODE -ne 0) { throw 'index metadata 不一致；先確認實際交付內容' }
# 先完成相關回歸、快速檢查與完整 review，message.txt 含實際 review trailers。
# 下例只發佈明確暫存的 helper 與相關測試，檔案清單須換成本批實際範圍。
$releaseDir = Join-Path $env:TEMP ('cmuh-release-' + [guid]::NewGuid().ToString('N'))
& $devPython -X utf8 scripts/push_helper.py publish --path scripts/push_helper.py --path tests/test_push_cli_contract_2026_10_07.py --message-file message.txt --output $releaseDir
if ($LASTEXITCODE -ne 0) { throw '候選未完成；查看保留副本與 release.json' }
$candidateReceipt = Get-Content -LiteralPath (Join-Path $releaseDir 'release.json') -Raw | ConvertFrom-Json
$candidateSha = $candidateReceipt.sha
Set-Location (Join-Path $releaseDir 'candidate')
& $devPython _delivery.py verify $candidateSha --phase candidate --wait 2700
if ($LASTEXITCODE -ne 0) { throw '候選 CI 未通過；停止正式發佈' }
# 後續依本手冊「發佈與補審」：重新 fetch、核對祖先與乾淨狀態、同 SHA 快轉 main。
```

原來源分支仍指向原 HEAD；從保留候選副本完成本次交付，下一批用最新已驗證 main 建立
新的工作副本，勿 force push 舊來源分支蓋掉遠端候選。候選推送成功不代表 main 已交付。
副本保留 hook、版本／manifest／index／來源指紋及 final-SHA 守門，仍須正式 CI 與匿名 smoke。
本輪只修改開發工具與文件，未修改 runtime／manifest 涵蓋檔案時可保留程式版本，記錄理由。
manifest 子程序明確使用 UTF-8 輸出，避免英文 Windows 的 cp1252 在印中文時中斷生成。
這項設定只套用該子程序，不改父程序、全域環境或 CI pytest 子程序的既有 Python 模式。

相關回歸：

```powershell
& $devPython -m pytest -q -p no:cacheprovider tests/test_push_cli_contract_2026_10_07.py tests/test_dev_setup_contract_2026_10_07.py tests/test_push_policy_2026_09_06.py tests/test_push_helper_antirevert.py tests/test_push_gate_failclosed_2026_07_31.py tests/test_delivery_review_2026_09_05.py
if ($LASTEXITCODE -ne 0) { throw '開發工具回歸失敗' }
```

回退開發工具需一起核對 helper、BAT／CMD、測試與交接說明，以新提交走相同候選／正式流程；
不能把舊版危險的參數用法當作新的操作入口。院內實機驗收仍未由這組匿名測試證明。

## 歷史 CI 階段定位與驗證成本（2026-10-04）

本輪基準為 `6855a37bd2941e36a9efcef9d9305cc342d65fa5`，修改開發驗證工具、CI 與相關測試／文件。啟動器、requirements 及臨床／排班來源保持不變；發佈 helper 已將版本中繼資料升為 `2026.10.04.1` 並同步 manifest，164 個配套檔案中只有 `src/cmuh_common/version.py` 的內容雜湊改變。最新狀態仍須按精確 SHA 核對。舊 pending 沿原排程，不用本輪 review 代替舊完整範圍。

候選 `e2e6f5ad68f0e918b0b6684b4f5dc1e1b94c0b67` 因誤將不存在的 `--sanity-only` 當成檢查參數而由 helper 建立並推送，提交訊息缺少 review trailers；其完整 diff **仍為 pending，沒有審查批准**。保留已發佈歷史，由後續提交補記本項缺口；最終補審及 audit 必須另列這個完整 SHA，不能只掃既有 pending trailers 而漏掉。此紀錄不表示已推 main 或 CI 通過。helper 主入口會升版、暫存與推送，不可用猜測的參數作檢查；純 sanity 應使用既有 `python scripts/sanity_check.py`。CLI 防誤操作另列後續範圍，不在本輪擴大修正。

`c5ed246` 的正式 CI attempt 1 曾在 73% 後逾時取消，當時只有百分比，JUnit／coverage 未產生；同 SHA attempt 2 完成。這是定位證據缺口，不能說已找到或修復卡住的根因。最新基準候選／正式 pytest 各為 1335／1462 秒，逐條型別債各為 153／203 秒；只是不同 runner 的單次樣本。

CI 沿用同一組完整 pytest／coverage 參數，透過 `scripts/run_ci_pytest.py` 啟動一次新 pytest 子程序，明確載入 `ci_pytest_progress`；一般 `python -m pytest` 不載入這個工具。每個收集器與 setup／call／teardown 開始時，安全代號寫入逐行 flush 的 JSONL 及 job console；完成階段另記 outcome、耗時，終端紀錄有 selected／finished 及 pytest 退出碼。參數值與例外內容不進進度紀錄；絕對／上層路徑以代號代替。環境僅記 Python、OS、程式 SHA、dirty 與 run／attempt。

2026-10-06 補審重現 wrapper 強制 UTF-8 造成的測試環境差異，已改為保留同環境 plain Python 子程序的編碼模式，不傳入 `-X utf8` 或 `-u`；wrapper 自身的 UTF-8 輸出不影響子程序旗標。加入 progress plugin 所需的 scripts 路徑，但原 PYTHONPATH 為空時不再加空項目。證據負向案例先確認完整匿名基準可通過，再分別破壞 dirty、狀態、退出碼、診斷錯誤、終端事件、零案例及報告雜湊；報告雜湊案例先還原前項偽造計數，避免被其他檢查提前擋住。這是原驗證能力修正，沒有降低 CI 門檻或宣稱加速。

wrapper 要求新的 evidence 目錄，拒絕重用；先移除隔離 checkout 的舊 `junit.xml`／`cov.json`。pytest 的失敗退出碼原樣保留；收尾診斷失敗不改成成功。`result.json` 的 `finished` 只表示 pytest 正常結束，**不表示通過**，仍須核對 exit code。子程序被終止、少了 sessionfinish、進度截斷或收尾未完成，都不是完整證據。`Validate pytest evidence` 另外核對完整 SHA、乾淨來源、run／attempt、終端與完成計數及報告雜湊；它不能取代 skip／coverage／型別／安全或 `_delivery.py`。

`Preserve pytest evidence` 在正常或失敗後嘗試上傳，artifact 名稱包含 SHA、run ID、attempt，保留 14 天。GitHub 強制終止或 runner 消失可能來不及上傳；console 的最後開始階段提供有限線索，不能保證任何中止都會留下完整 artifact。`running` 狀態或單獨檔案不能證明程序仍活著，須查實際 handle／GitHub run。

artifact 同時保留原始 `junit.xml`（含失敗 traceback 及參數識別）與 `cov.json`；逐行 progress 的遮罩不會遮罩這些原始報告。測試與保存的 artifact 仍須遵守既有匿名資料規則，不得包含患者、帳密或正式設定。

獨立審查找到中止測試的 PID checkpoint 競態：父程序可在 PID 檔建立後、寫完前讀到空內容，清理時只終止 wrapper 而留下 pytest 子程序。受控暫停該寫入後重現逾時及存活子程序；改成先完整寫暫存 PID 檔，再 `os.replace` 原子發布。相關回歸另重現 Windows checkpoint 短暫讀取拒絕。父程序在原有 15 秒期限內只重試 FileNotFoundError／PermissionError，損壞 PID 仍失敗；例外清理在結束 wrapper 前只終止其直接子程序中 cwd 為該匿名暫存目錄且 argv 為 pytest 的程序，避免遺留繼承的 pipe。新增一次讀取拒絕及損壞 PID 清理的匿名整合案例。這是測試同步與清理修正，沒有改正式程序的終止規則。

在**隔離開發 checkout**（必要依賴已安裝）重跑：

```powershell
$ciEvidenceDir = Join-Path $env:TEMP ("cmuh-pytest-" + [guid]::NewGuid().ToString("N"))
$ciSourceSha = git rev-parse HEAD
python -X utf8 scripts/run_ci_pytest.py --output $ciEvidenceDir -- -q -p no:cacheprovider --junitxml=junit.xml --cov=src --cov-report=json:cov.json --cov-report=term:skip-covered
$ciPytestExit = $LASTEXITCODE
python -X utf8 scripts/run_ci_pytest.py --check --output $ciEvidenceDir --sha $ciSourceSha
# 只有 pytest 退出碼、evidence 檢查及原有所有守門都成功才算完整本機驗證。
Get-Content -LiteralPath (Join-Path $ciEvidenceDir "result.json")
Get-Content -LiteralPath (Join-Path $ciEvidenceDir "events.jsonl") -Tail 3
# 匿名正常／斷言失敗／collection error／四階段中止及完整性負向案例
python -m pytest -q -p no:cacheprovider tests/test_ci_progress_2026_10_04.py tests/test_ci_annotations_2026_08_09.py tests/test_ci_gates_2026_07_30.py tests/test_delivery_review_2026_09_05.py
```

fixture 快取候選未採納：249 案例的 cProfile 指出共用 fixture 約 4 秒累積時間，SQLite 實際交易／連線占較多；profiling 自身有成本。僅試快取絕對模組路徑正規化，保留每次模組／屬性／帳本掃描。113 原案例三組交錯新程序，基準 10.887／10.134／10.639 秒，候選 9.865／9.532／10.625 秒，全部案例與結果一致。雖達到預先設定的中位數門檻，第三組僅差 0.014 秒，候選波動大於中位数收益，因此恢復原 fixture。不修改 SQLite 測試以替身繞過真正保留／順序守門，也不以 `type_debt --fast` 代替逐條檢查。

診斷工具本身也有成本。同一組 113 原案例、六個新程序、未開 coverage 的交錯量測，plain 為 11.206／10.160／10.172 秒，diagnostic 為 10.626／11.208／10.847 秒；兩邊中位數為 10.172／10.847 秒，差約 0.675 秒，全部案例及結果相同。樣本有明顯波動，只記錄本機小組的額外成本，不推算整輪 CI 或院內效能，不宣稱加速或 p95。

重跑此成本案例時保持 Python／套件、來源及案例不變，交錯 plain→diagnostic、diagnostic→plain、plain→diagnostic；每次用外層 monotonic 計時記錄完整新程序及退出碼，保存各次 JUnit 並比較 testcase 身分、結果及 skip，不能只比較總數。固定案例為 `test_config_io.py`、`test_paths.py`、`test_task_gate.py`、`test_config_loss_guards_2026_07_25.py`、`test_main_launch_guards.py`（皆在 `tests/`）。plain 用 `python -m pytest -q -p no:cacheprovider --junitxml=junit.xml` 加上述案例；diagnostic 使用前述 wrapper 加相同 pytest 參數與案例、每次新的 evidence 目錄。此量測刻意沒有 coverage，不執行要求完整 coverage 的 evidence 通過檢查，也不是完整 CI。fixture profiling 另以 `python -m cProfile -o .pytest_cache/ci-fixture.prof -m pytest` 執行，將上述第四檔換成 `tests/test_runtime_diagnostics_2026_09_28.py`；profile 時間不當成正常耗時。

分開成本時，GitHub job step 的起迄時間提供依賴安裝及正式逐條型別債耗時；已驗證 artifact 的 `collection_finished.duration_s` 與終端 `phase_seconds` 分別提供收集、setup、call、teardown 時間。setup／teardown 包含 fixture 與 pytest hooks，不能直接稱為某一個 fixture 的時間，也不能把 report.duration 加總當成整個 job 耗時。coverage 的追蹤成本混在測試階段，報告成本另以原工具定位：

```powershell
python -X utf8 -m cProfile -o .pytest_cache/ci-coverage-report-cost.prof -m pytest -q -p no:cacheprovider tests/test_config_io.py tests/test_paths.py tests/test_task_gate.py tests/test_config_loss_guards_2026_07_25.py tests/test_main_launch_guards.py --junitxml=.pytest_cache/ci-coverage-profile.xml --cov=src --cov-report=json:.pytest_cache/ci-coverage-profile.json --cov-report=term:skip-covered
python -X utf8 -c "import pstats; pstats.Stats('.pytest_cache/ci-coverage-report-cost.prof').strip_dirs().sort_stats('cumulative').print_stats('summary|finish|start')"
```

該本機 113 案例 profile 通過，總 profile 時間 31.94 秒；pytest-cov 的 coverage 啟動、finish、summary 累積時間各約 0.34、0.09、16.83 秒。這些是含 profiling 成本的函式定位資料，互有包含關係，不能直接相加，也不是純 coverage 額外成本、正常 CI 基準或加速成果。沒有據此改 coverage 設定、增加 skip 或降低正式門檻。

回退本輪開發工具須配套還原 workflow、delivery policy、兩支 scripts 及相關測試／文件，再走正常候選／正式 CI；不能只還原 wrapper 留下不一致 workflow。正式執行程式僅變更版本中繼資料；若回退已發佈套件，仍遵守下方整包回退及保留最新業務狀態的方法。本輪沒有執行院內實機驗收，不宣稱診斷解決了歷史逾時或縮短了整輪 CI。

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

必要時完整本機檢查，以 `.github/workflows/ci.yml` 當前命令為準。先執行上方「CI 階段定位與驗證成本」的完整 wrapper 與 evidence 檢查命令（含 coverage），兩者退出碼皆須為 0，再執行下列原有守門；plain pytest 或只有部分報告不能代替完整 evidence 檢查：

```powershell
python -m pyright
python scripts/check_skips.py junit.xml
python scripts/check_coverage.py cov.json
python scripts/type_debt.py
```

恰兩位 PGY 月維持既有特殊模式：週二上午、週四下午、週五上午不排治療室，並保留週別輪替；逐時段初排由 Clerk 先入座，再用剩餘診位安排釋出的 PGY。完整月份還會執行 course 平衡：在必要工作、請假、鎖定與容量允許時，PGY 每週至少一次跟診與 Clerk／家醫／外訓最低需求共同受保護，之後才依 Clerk > 家醫 > 外訓 > PGY 追求目標次數。不能因 Clerk 尚未達目標就移除可行的 PGY 最低跟診；既有回歸為 `test_clerk_extra_clinics_never_displace_feasible_pgy_minimums`。判準是該月名單，不是請假後當日剩兩人。一般月仍先滿足照光＋治療室，週三下午僅照光。不要把特殊模式誤判為漏班，也不要沿用早期註解中的 PGY 優先於 Clerk；現行管線及回歸測試已採較新定案。

保存平台、Python／依賴版本及 exit codes，不修改門檻製造全綠。性能改動先按 [第三期量測](第三期排班效能與品質守門_2026-09-24.md) 與 `scripts/compare_day_roster.py` 對照基準／候選；記錄 SHA、環境、樣本與品質。沒有可量化收益，或硬限制／品質退步，就不採用重構。

## 發佈與補審

`scripts/push_helper.py publish` 依上方明確範圍在隔離副本生成版本、manifest、commit 並推候選，
不代替完整 review。手動分步提交仍須相同生成及一致性守門，不繞 hook。

1. 相關回歸、Ruff、sanity、delivery unit tests 成功；程式改動生成版本與 manifest，再核對最終 index。純文件未改 manifest 涵蓋檔案可保持版本不變，但記錄理由。
2. 獨立 Codex review；Claude Code 固定 `claude-opus-5-5`／`high`／唯讀 `Read,Glob,Grep`，覆蓋 committed、staged、unstaged、相關 untracked。保存 diff 雜湊及 machine `modelUsage`；修 findings 後是新 diff，要重審。
3. 只有確定的供應商額度限制可留 pending；每個未審 commit 加 pending／high trailers，安排回復後補審。其他審查失敗不能當額度豁免。
4. `git config --get core.hooksPath` 應為本專案已追蹤 `.githooks`；使用既有安裝機制，不關閉 hook。
5. 固定完整 SHA 推候選，完整候選 CI 成功才發佈同一 SHA。下列每一步失敗停止；分支示例需換成本批分支。

```powershell
$candidateSha = git rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw '無法取得完整候選 SHA' }
git push origin "${candidateSha}:refs/heads/codex/your-maintenance-batch"
if ($LASTEXITCODE -ne 0) { throw '候選推送未確認；先查遠端 SHA，勿盲目重推' }
python _delivery.py verify $candidateSha --phase candidate --wait 2700
if ($LASTEXITCODE -ne 0) { throw '候選尚未通過完整 CI；停止後續正式發佈' }
git fetch origin
if ($LASTEXITCODE -ne 0) { throw '無法取得最新 main' }
git merge-base --is-ancestor origin/main $candidateSha
if ($LASTEXITCODE -ne 0) { throw 'main 不在候選祖先範圍；先整合並驗證新 SHA' }
# 上一步 exit code 必須為 0；main 前進則先整合並驗證新的 SHA。
$candidateStatus = git status --porcelain
if ($LASTEXITCODE -ne 0 -or $candidateStatus) { throw '候選工作樹不乾淨或狀態不可讀' }
$actualHead = git rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $actualHead -ne $candidateSha) { throw '候選 HEAD 已改變' }
# 工作樹須乾淨且 HEAD 仍是 candidateSha，才能執行下一步。
git push origin "${candidateSha}:refs/heads/main"
if ($LASTEXITCODE -ne 0) { throw '正式推送未確認；先查遠端 SHA' }
python _delivery.py verify $candidateSha --phase main --wait 2700
if ($LASTEXITCODE -ne 0) { throw '正式 CI 尚未通過；不能宣告交付' }
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
4. 暫時阻止更新立即換回新版，再還原配套程式；**保留最新設定、帳本、排班與待確認狀態**，不由舊快照覆蓋。核對捷徑／排程路徑。現有更新抑制在 `cmuh_common/update_policy.py`，是 `settings/.auto_update_suspended_until` 保存的絕對到期時間，並非永久停用。自 `2026.10.03.1` 起，透過 `suspend_auto_updates()` 寫入會取得獨立、最多等待兩秒的跨程序鎖，只延長有效期限；watchdog 的一小時請求不會縮短較長維護期限。讀不到既有旗標或拿不到鎖會回報失敗，不以較短期限覆蓋。直接手動改檔未參與此鎖；設定期限時須先停止所有寫入者並核對內容。過期後 updater 仍可能重新拉回 main 的較高問題版本，防降版不會阻止。維護者先於隔離副本驗證：修復版尚未在 main 驗證完成前，保持 watchdog 及其自動啟動／排程停止，或在受控維護期間持續核對並延長有效期限，確保涵蓋整個修復窗口；無法維持這項保護就不恢復會觸發更新的工作／排程。不可只設定一次旗標便假設回退會永久維持，也不任意刪狀態。
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

## 更新復原與環境重建（2026-10-03）

本輪基準為 `74d58cde6be0293d9fded50ccc7f4e9574131e19`。修正更新抑制期限及非寫入啟動器的復原失敗漏記，另補流程證據及開發工具；正式依賴、更新來源、資料格式及臨床／排班規則不變。無限大、NaN 等旗標內容視為損壞，下一次寫入修成有限期限；不藉此永久停用更新。兩秒鎖等待只在要求寫入抑制旗標時發生，不增加背景輪詢。

使用者已定案：復原未完成時保留**排班、點座標與守護程式**的非寫入工具例外。它們仍可啟動並記錄復原失敗，必要時 resolver 退回根目錄 `src`；這不代表版本一致或復原成功。臨床主程式、會診、打卡仍依各自既有守門，不放寬 HIS 寫入。先核對 `update_recovery.log`、`version_pointer.log`、實際啟動來源與版本；檔案占用解除後再以根目錄啟動器重試。不要為消除錯誤而刪 journal／備份，或直接執行版本樹源碼。

停機備份／程式回退須區分：

| 類型 | 保存及還原方式 |
| --- | --- |
| 配套程式 | 完整 `src`、`versions`、`current.txt`、manifest、啟動器、resolver 及 manifest 列出的 extras 配套還原；置換整棵程式樹，避免留下新版專有模組。舊樹先保留於院內受控位置，確認路徑後再操作。 |
| 最新業務狀態 | 保留整個當下 `settings`，包含設定／校正、排班保存／定案、`clock_state.json`、會診 baseline／notified／trigger／receipts 等；不得拿快照中的舊狀態覆蓋。帳密與臨床資料留在院內。 |
| SQLite | `delivery_ledger.sqlite3` 及診斷資料庫在 WAL 模式下可能另有 `-wal`、`-shm`；先停止所有 writer、關閉連線後備份完整狀態，不能在持續寫入時只複製主檔。無法確認停機一致性時先保留整組原檔，不自行拼接。 |
| 稽核鏈 | `action_ledger.jsonl`、anchor 及輪替代檔成組保存；單獨留最新 JSONL 不能證明完整鏈。 |
| 復原證據 | `.updater_commit.journal`、相應 `.bak`／交易備份、`.multiwrite.manifest.json` 與其備份一起保留，交由正式復原；不任意清除。 |
| 可重建快取 | `.deps_cache`、Python bytecode 可重新建立；不是寄送／打卡去重紀錄。抑制旗標與鎖檔不是業務去重解鎖工具，不以刪檔作為恢復手段。 |

新增整合回歸用同步 checkpoint 終止自建程序，透過新程序的真啟動器／resolver／復原及讀取端驗證四類情境：更新提交中斷、設定多檔交易中斷、程式回退保留較新狀態、真檔案鎖定後重試。省略更新 journal 或設定復原的負向案例會違反一致性斷言。回退比較配套程式雜湊，保留合成狀態全部位元組、真 SQLite 已寄／unknown 去重及 JSONL 鏈；打卡 pending、排班定案的測試資料只證明未被覆蓋，其業務語意沿用現有狀態機回歸。非寫入例外用替代入口驗證，不執行真正排班／HIS／寄信／打卡。這不是斷電、磁碟損壞或院內驗收。

```powershell
python -m pytest -q -p no:cacheprovider tests/test_update_policy.py tests/test_recovery_process_chain_2026_10_03.py tests/test_updater_safety_batch1.py tests/test_update_atomicity_2026_08_01.py tests/test_launcher_recovery_order_2026_08_12.py tests/test_settings_recovery_contract_2026_08_30.py tests/test_watchdog_restart_lock_2026_09_03.py
```

上述歷史測試檔名以目前 `tests` 為準；若命令回報不存在或非零，先修正清單／診斷，不把部分執行當通過。新九個流程案例預期通過，受鎖的臨床入口預期 exit 3，非寫入例外預期 exit 0 且失敗紀錄仍在；鎖解除後 journal 應由復原移除，啟動來源、版本、指標及 manifest 雜湊一致。三支非寫入工具在根目錄復原模組缺失／載入失敗時，也以標準庫嘗試追加 `unknown（復原未完成）`、程式名稱及例外類型，保留 journal 並繼續啟動；紀錄檔也不可寫時不阻擋啟動，因此沒有紀錄不代表復原成功。

開發環境基線 `docs/dependency_baseline_2026-10-03.txt` 是 Windows 11 `10.0.26200`／AMD64、CPython `3.13.1`、Tcl `8.6.15`／Tk ABI `8.6` 的 **54 個必要 runtime 套件**。已在乾淨 venv 重建相同組合，`pip check`、14 個必要匯入、三個新程序離線 launcher 樣本及 107 個相關匿名回歸通過；初次安裝另計，沒有宣稱加速或 p95。這不是院內環境或開發工具鏈的完整凍結：pip、pytest、Ruff、Pyright 不在 runtime constraints 內。

```powershell
# 完整開發 checkout，先選定與基線相同的 Python；不要在正式資料夾操作。
python -m venv .pytest_cache/rebuild-venv
$rebuildPython = '.pytest_cache/rebuild-venv/Scripts/python.exe'
& $rebuildPython -m pip install -r requirements.txt -r requirements-lazy.txt -c docs/dependency_baseline_2026-10-03.txt
& $rebuildPython -m pip check
& $rebuildPython scripts/verify_dependencies.py
& $rebuildPython -X utf8 scripts/dependency_baseline.py --output .pytest_cache/rebuilt-environment.json --constraints .pytest_cache/rebuilt-environment.txt
& $rebuildPython -X utf8 scripts/benchmark_launcher_offline.py --output .pytest_cache/rebuilt-launcher.json
# pytest 是驗證工具，另外安裝，不納入 runtime 套件基線。
& $rebuildPython -m pip install pytest -c docs/dependency_baseline_2026-10-03.txt
& $rebuildPython -m pytest -q -p no:cacheprovider tests/test_deps_runtime.py tests/test_deps_installer.py tests/test_bootstrap_scripts.py tests/test_bootstrap_delivery_2026_09_06.py tests/test_roster_export.py tests/test_update_policy.py tests/test_dependency_baseline_2026_10_03.py tests/test_recovery_process_chain_2026_10_03.py
```

每步核對 exit code，失敗停止，不沿用舊 JSON。環境紀錄只包含 source SHA／dirty 布林、兩份 requirements 雜湊、Python／OS／Tcl／Tk ABI 與必要套件名稱／版本；不讀取 pip 設定、帳號、環境變數值或私有下載來源。比較 baseline 與 rebuilt 的 `packages`、manifest 雜湊並核對來源；dirty 紀錄不是精確提交驗證。CI 使用原安裝宣告及完整既有門檻，另上傳同 SHA／attempt 的 `runtime-environment` artifact，供核對實際 Windows runner／Python patch 與套件；不能拿本機版本推定 CI 版本。

本機 dry-run 的現行安裝宣告有 20 個套件版本不同，沒有確認相容性缺陷，不因差異更改 requirements 或正式延遲安裝。安全掃描仍依現行 CI；日後重建發生解析、wheel／Python ABI 或掃描失敗時保存匿名錯誤，按原流程修正並重新驗證，不放寬門檻或宣稱舊基線永久安全。院內仍需核對 Python／Windows／Tk、檔案權限、防毒鎖定、捷徑／排程、受控停機及人工待確認動作；不在本輪測試正式系統。

## Excimer 舊病史日期換行辨識（2026-10-06）

近期 UVB 與過久的 Excimer 同時存在時，Excimer 的 `on` 與日期若被 HIS 實際換行分開，舊的逐行辨識會把它當成無日期的 Excimer，觸發跨欄位歧義。日期同行的原案例已能正確辨識；本次另修正日期續行，僅在分流副本接回明確相鄰的 `on`／日期，不改病歷的換行與內容。

沿用既有「超過兩個日曆月」的舊照光分流條件；近期、未標日期、無效日期、空行、其他照光項目及不確定的後續治療仍保留原本保護。正常 UVB 劑量與次數更新規則不變，過久 Excimer 與其他病史保持原文。匿名回歸涵蓋日期同行／換行、LF／CRLF／CR、同欄／不同欄及 F1–F3 寫入核心；並驗證近期或未確認的 Excimer 不會被當成過久病史。這不是院內實機驗收。

補審重現並修正兩項邊界：已完整辨識為 OMP 的後續病史日期不再決定 Excimer 新舊；只使用相鄰 Excimer 日期續行判斷。分行邊界與原辨識一致，僅 CR／LF；其他控制字元不能當成空行而隱藏未知續行醫令。回歸也核對 F1 分流及原文保存。未知病史格式仍保守保留歧義，不猜測治療是否重啟。

可重跑：

```powershell
python -m pytest -q tests/test_phototherapy_wrapped_history_2026_10_06.py tests/test_uvb_dose.py tests/test_uvb_golden.py tests/test_uvb_dose_safety_2026_07_25.py tests/test_uvb_excimer_multi.py tests/test_uvb_undated_multiline_2026_09_09.py tests/test_main_his_write_transaction_2026_09_25.py tests/test_main_his_memo_port_2026_09_24.py
```

院內仍須核對實際欄位內容、顯示版本及更新後劑量／次數；遇到真正同時有效的 UVB 與 Excimer，歧義保護仍適用。此修正的獨立 Opus 審查／精確 SHA 交付狀態以提交 trailer、後續 audit 與 CI 證據為準，不由本文宣稱審查結清。回退沿用本文件的整包回退流程，保留最新設定及操作帳本。
