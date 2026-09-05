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
