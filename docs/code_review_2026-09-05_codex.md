# Code review 追蹤（2026-09-05）

## 狀態與界線

全專案人工審查尚未完成；完整 pytest 通過也不代表所有路徑已人工審完。
本批以 `171d37eac1fb2ae367be009bad3ce63082c81e2f` 為基準，在獨立工作樹修正。
不修改可用打卡帳密、身分證格式或臨床收件設定，不新增敏感資料掃描。

基準版本追蹤檔案：`src/` 120 個、`scripts/` 22 個、`tests/` 337 個；
主要入口為 main、autoclock、scheduler、consult_query、watchdog_runner、coord_detector。
本批另新增 2 個測試檔。以上是檔案盤點，不是人工審查完成率。

Claude Code 外審要求：精確 `claude-opus-5`、effort `high`、唯讀，必須有
machine-readable modelUsage 證據。本批尚未取得修正後完整 diff 的通過證據，
已驗證的提交依使用者指示標記 `pending`，不可解讀為外審通過。

## 本批已重現並修正

| 優先級 | 位置 | 可觀察問題 | 修正與回歸測試 |
| --- | --- | --- | --- |
| P1 | `src/main.py` status driver pool | 舊 `window_handles` RPC 在接管後才失敗，原重試迴圈會借走新 worker 的 driver，造成兩輪操作同一個 Selenium session。 | caller 綁定 epoch；自己的正常淘汰可繼續重建，被別人接管則返回。事件同步測試重現舊版失敗。 |
| P1 | 同上 | 舊 `_initialize_status_driver` 卡住時持有共用 init lock，180 秒接管雖清空 pool，新 initializer 仍被舊鎖擋住。 | 接管換用新世代的 init lock，舊候選完成後只能回收；測試在不釋放舊 initializer 的情況下要求新一輪完成。另驗證排在舊鎖後的 caller 不得借用新 session。 |
| P2 | `scripts/push_helper.py` | 把 `.git` 當資料夾寫 commit message；linked worktree 的 `.git` 是檔案，因此品質關卡後仍無法 commit。 | 使用各次獨立暫存目錄，保留 UTF-8 訊息與 pending trailers；測試成功、失敗兩條路徑的清理和 `.git` 不變。 |
| P2 | `scripts/sync_manifest.py` | runtime 讀取兩份 requirements，但 updater 清單只有第一份；第二份版本規格無法由更新管道取得。 | 把 `requirements-lazy.txt` 納入發佈清單；測試同時核對產生器與實際 manifest.json。 |

新增測試：`tests/test_status_pool_review_2026_09_05.py`（5 條）與
`tests/test_delivery_review_2026_09_05.py`（3 條）。先在原碼重現失敗，再驗證修正。
原有正常 driver 重建、idle 淘汰、非同步清理與發佈檢查仍保留。

## 覆蓋追蹤與下一步

- 本批逐段檢查：status pool 取得／淘汰／初始化、查詢 worker 接管與 UI 世代閘門、
  發佈工具品質關卡／版本／index 雜湊／commit／push、manifest 收集器。
- 已讀相關實作但未宣告整個子系統完成：依賴 runtime／installer／manifest、
  `task_gate.py`、`bounded_executor.py`、`config_io.py`、`atomic_io.py`、
  SQLite 的初始化／損壞處理、寄信 quota／alert 去重、cross-process claim、
  updater 的備份／提交流程及 cache cleanup。亦檢查排班的共用匯出函式、
  XLSX／DOCX／PDF 輸出程式碼、fingerprint、clinic grid、週色與日期工具；
  尚未做實機輸出視覺驗收或逐一追完 service 的所有呼叫路徑。
- 原工作區另有 5 個既有未提交檔案（deps_installer、deps_runtime、paths 及其兩份
  測試），屬另一輪 bootstrap/重啟修正，不納入本批。其舊父行程相容性與 installer
  修復前可能間接載入舊套件的路徑仍需繼續驗證，不能把它們當成已發布／已通過。
- 尚需逐區完成：其餘主程式與 HIS 自動化、autoclock／consult／scheduler／watchdog、
  updater／版本切換／bootstrap、原子儲存與 SQLite、通知與去重、排班求解與所有匯出路徑。
- 已知限制：Python thread 接管不會終止卡住的原生呼叫；本批消除跨世代共用 session
  與 init-lock 阻塞，不宣稱已解決任意永久卡死或完成實機院內端到端驗證。

後續優先驗證（候選問題，不當成已確認／已修正）：背景清理與更新交易備份
是否共用足夠的鎖定與保留策略；損壞的更新暫停旗標在刪除前被另一行程換新時
是否可能誤刪；claim context manager 對呼叫端例外是否會二次 yield 而掩蓋原始錯誤。
須補確定性的重現與呼叫路徑確認，才列入下一批修正。

以上三個候選已在同日續輪完成重現，修正與驗證記於下節；其餘未完成範圍不變。

## 驗證與外審紀錄

交付前跑既有完整品質關卡（ruff、pyright、pytest + coverage、skip 守衛、type debt），
更新版本與 manifest，再比對 staged 內容及 SHA256。實際測試結果以執行輸出為準。
只有關卡通過才推送，不使用 `--no-verify` 或 force-push。

本批完整關卡結果：ruff 通過；pyright 0 errors；pytest **6558 passed、2 skipped、
113 warnings**（909.07 秒）。兩個跳過是工作樹未提交時的 clean-index 守衛測試，
skip 守衛已通過。Coverage：入口 39.0%、共用模組 80.5%、總計 63.5%，皆高於
既有門檻；type-debt 守衛無新增債務。另 `pip check`、compileall、diff whitespace
檢查通過；測試期間 481 個交付相關檔案的 blob 快照比對一致。

警告不能視為已處理：含 SQLite `ResourceWarning`（未關閉連線）；GC 顯示位置
不一定是連線建立位置，尚需追蹤配置端以區分測試清理缺漏與正式程式生命週期問題。
此項列入後續查核，本批不宣稱測試零警告或所有資料庫路徑皆已審完。

未審提交由專案排程 `CMUH Opus 5 pending 補審` 收集，範圍包括先前的 `171d37e`。
排程依額度 reset 重試；本機需開機且 App 執行。補審完成不改寫舊提交，而以新的
audit commit 記錄每筆被審查的完整 SHA；若發現缺陷則驗證、修正、重審完整範圍。

本輪實際外審嘗試 session `1a4e06cf-5147-4dcc-b585-314bb8dde495`：
`api_error_status=429`、`modelUsage={}`，回報 Asia/Taipei 00:50 重置。
因此未取得外審通過；已更新既有排程於 9/6 00:55 處理全部 pending，而非只處理單筆。

## 同日續輪：例外邊界與更新救援檔

基準 `5fdbe52d8c0c9037720b528ba986d90eca9a5e9d`；仍在隔離工作樹修改，
原工作區既有 5 個未提交檔案不納入。依使用者指示不等待 Opus 額度，提交保持 pending。

| 優先級 | 確認問題 | 最小修正 |
| --- | --- | --- |
| P1 | `cache_cleanup.py` 無更新鎖、無 journal 檢查就刪舊 `.bak`／`.tmp`。updater 的 `copy2` 保留來源 mtime，所以剛建立、仍供回滾使用的備份也會立即符合 TTL；崩潰後的待恢復備份亦可能被刪。 | 使用 bootstrap recovery 的相同 OS 位元組鎖；journal、failed marker 尚在，或狀態讀不到、鎖拿不到時保留 bak/tmp。一般 log/pyc 清理照常。 |
| P2 | `update_policy.py` 對損壞旗標讀後刪除，會誤刪讀取之後另一行程原子寫入的新 suspend。 | 與過期旗標一致：損壞內容仍回 0，但不刪檔，後續 suspend 可覆寫；不改既有有效旗標或 IO 錯誤政策。 |
| P2 | `cross_process_claim.py` 在涵蓋 yield 的 except 內再次 yield，會把呼叫端 `ValueError` 等改成 `RuntimeError: generator didn't stop after throw()`；timeout 路徑亦有同因。 | 取鎖／claim 的錯誤處理不涵蓋交給呼叫端的 yield；保留原始例外與清鎖行為，不更改原 fail-open／timeout 略過政策。 |
| P1 | `smtp_mail.py` 的 smtplib context exit 在 QUIT 逾時／異常回覆時，覆蓋 DATA 已確認成功或原始例外；已寄出的信會被重送，UNKNOWN 的階段標記與認證失敗也會遺失。 | 兩種 SMTP transport 共用 outcome-preserving session wrapper；保留成功時逐位拒收 dict，失敗時保留原始例外身分／階段，QUIT 僅盡力收尾，不改寄送結果。 |

新增 `test_claim_policy_cleanup_review_2026_09_05.py`：初版 8 個案例在修正前
**7 failed、1 passed**，修正後再擴充為 **11 passed**。涵蓋正常／busy 鎖的例外身分、
timeout 本地鎖釋放、claim 寫入失敗 fallback、旗標讀寫交錯、pending／failed journal、
鎖不可用、journal stat 權限錯誤、與真正 updater 鎖的互斥、解除鎖後正常清理。
原有兩個「損壞旗標必須刪除」測試改成保留，以反映新確認的 TOCTOU。
與既有 claim、update policy、updater safety、cache cleanup 測試整合執行：**54 passed**。

SMTP 新增 `test_smtp_cleanup_review_2026_09_05.py`：使用真的標準庫
`SMTP.__exit__` 配合假的 server、完全不連網。修正前 4 個案例皆失敗，
其中已確認 DATA 的成功信被重送到 3 次後誤報「確定沒有寄出」。修正後與既有
delivery phases／unknown 測試整合 **80 passed**；再擴充新檔到 8 個案例，涵蓋
587／465、QUIT timeout／421／正常 221、部分拒收、UNKNOWN 與認證例外。
擴充後整合測試為 **84 passed**。前次仍執行中的完整關卡已中止，不當成通過；
加入 SMTP 修正後重新啟動全套驗證。

另以 `-X tracemalloc=5` 定向執行 `test_delivery_body_sealed_2026_09_01.py`（11 passed），
定位到該檔 40、75、143、206 行使用 `with sqlite3.connect(...)` 的連線未關閉。
SQLite connection context manager 只收束交易，不會 close；這部分警告確定來自測試輔助
連線，而非警告觸發 GC 當下的正式程式行。尚未據此推論全部 113 個警告同源，亦未在
這個並行安全修正批次修改相關測試；後續需修正其生命週期並再次核對完整警告清單。

本輪另檢視寄送帳本的 schema 初始化／交易 rollback／關閉註冊、排班 ledger 的月結／
rollback／人員異動、排班 storage 的 revision／嚴格讀取邊界；未宣告其整個子系統完成。
亦逐段讀取 GitSyncStorage、fetch_resilience、reg52_contract、punch_status、
consult_keepalive、通知、process_launch／program_launcher、pidfile、health 與
resource_meter；這些閱讀不等於已逐一追完所有呼叫路徑。未執行院內登入、寄信、
打卡或重開機等真實外部操作。

下一輪優先重現：GitSyncStorage 停機後，已啟動但尚在等待 git lock 的 timer callback
是否仍能操作工作樹；rebase 子程序逾時時是否可靠清除中間態。這兩項目前是待驗證
候選，未列入本批已修正項目，也未宣稱相關同步生命週期已全部完成審查。

對 `src/` 118 個 Python 檔案進行 AST 結構檢查，修正後未再找到「同一 try 的 body
與 exception handler 皆含 yield」形狀；這是特定缺陷模式的掃描，不是全專案人工審完
或所有 context manager 都正確的證明。另 compileall、pip check、diff whitespace 通過。

續輪完整關卡實際結果：ruff 通過；pyright **0 errors、0 warnings**；pytest
**6577 passed、2 skipped、113 warnings**（833.33 秒）。兩項 skipped 是未提交
工作樹下的 clean-index 條件檢查，skip 守衛通過，提交後另補跑。Coverage：入口
39.0%（門檻 22.9%）、共用模組 80.8%（73.8%）、總計 63.7%（51.0%）；
type-debt 守衛通過、無新增債務。驗證範圍是上述隔離基準加本批修正，不包含原工作區
5 個既有修改。113 個警告尚未全部消除，不能將完整測試通過解讀為全專案審查完成。
