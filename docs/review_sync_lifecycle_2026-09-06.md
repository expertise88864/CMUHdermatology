# 深度 review 續輪：同步交棒與測試資源生命週期

基準：`c5e71c014980528f6f73d5c021bd0ea2965efc27`。
本批在 `codex/full-review-20260905` 隔離工作樹修改；原 main 工作區的五個
既有程式／測試修改及 AGENTS.md、CLAUDE.md 不包含在本批。

全專案人工 review 與優化仍未完成。本文件只記錄以下已查證範圍；
完整測試通過、檔案盤點、模型補審都不能代替未做的逐路徑審查。

## 已重現並修正

| 優先級 | 已確認問題 | 修正與驗證 |
| --- | --- | --- |
| P1 | Git push timer 已開始等待 `_git_lock` 時，`cancel()` 無法阻止它在 `quiesce_local()` 返回、交出 repo 後繼續工作；resume 清除 Event 也會讓舊世代復活。 | timer 與 pull thread 綁定建立時的 epoch；拿到 Git 鎖後再次檢查。停機使 epoch 失效並禁止重新排 timer，恢復後只有新工作可同步。 |
| P1 | rebase 失敗後先釋放 `_tree_lock`、再重新取鎖 abort；空檔中的存檔可能被回復操作覆蓋。逾時例外則完全略過 abort。 | rebase 失敗／逾時與 abort 留在同一段樹鎖內；回復失敗明確回報 error、不繼續 push；狀態 callback 在樹鎖外執行。 |
| P2 | 八個 delivery 測試檔的 21 處 `with sqlite3.connect(...)` 只結束交易、不關閉 connection，造成 ResourceWarning。 | 外加 `contextlib.closing`，仍保留原本的 connection transaction context，SQL、提交／回滾與斷言不變。 |
| P2 | 同批測試在嚴格資源警告檢查下，另有四個案例暴露五處未關閉檔案讀取。 | 將讀取置於 with context，不修改原本資料比對或原始碼接線斷言。 |

## 回歸證據

- 三個 timer／停機案例先在原碼重現 **3 failed**；修正後擴充正常 resume、
  queued periodic pull、舊世代通知抑制及正常通知保留。
- rebase 失敗與逾時先在原碼重現 **2 failed**；擴充 abort 成功、回傳失敗、
  逾時，以及狀態通知不持樹鎖。目前新測試檔共 **13 passed**。
- 相關四個 roster 測試檔整合 **57 passed**；使用暫存本地 Git remote 或
  fake subprocess，不操作正式排班 repo。最終交付仍須再跑完整 CI。
- 八個 delivery 測試檔開啟 `-W error::ResourceWarning` 與
  `-W error::pytest.PytestUnraisableExceptionWarning`：**134 passed**。
  這只證明該測試集合無資源警告，不宣稱原全套 113 warnings 已全部消除。
- Ruff 全範圍、Pyright（0 errors / 0 warnings）與 diff whitespace 通過。
  上述定向結果不冒充最終 SHA 的全套本機或 GitHub CI。

## 界線與仍待審查

- 不更動打卡帳密、身分證格式、臨床收件設定或寄送政策，不新增敏感資料掃描；
  不執行院內登入、真實打卡、寄信或重開機。
- epoch 守衛防止等待中的舊 Git 工作復活，不宣称能終止任意已開始的原生呼叫，
  也不保證收回已進入使用者 callback 的通知。明確的同步 flush 仍可執行。
- abort 本身失敗時需要人工檢查；不自動刪除 Git lock、不 reset 或改寫歷史。
  repo 中間態跨行程崩潰的全面復原策略尚未完成驗證。
- 其餘 HIS 主程式／autoclock／consult、watchdog、bootstrap 舊世代相容性、
  delivery 狀態機全路徑、排班服務／求解／UI／匯出仍需逐區追完。
  效能尚無完整基準與前後比較，不宣稱整體效能優化已完成。
- 發佈工具仍提供舊 `--emergency` 繞過介面，與最新禁止豁免政策不一致；
  本次不使用該介面，後續需獨立修正並保留拒絕繞過的回歸測試。

## 外審與 CI

本輪 Claude Code 2.1.261 精確指定 `claude-opus-5`、effort `high`、唯讀，
嘗試審查最早 pending parent 至已發布 tip 的完整差異及分列的未提交內容。
session `4ed8ef7b-1c13-4ff9-bceb-c63c83ffa984` 回傳 429、`modelUsage={}`，
額度於 **2026-09-06 05:50 Asia/Taipei** 重置，並無模型通過證據。
既有補審排程 `cmuh-opus-5-pending-2` 已排 **05:55**，需電腦開機且 Codex 執行。
這次新 diff 亦維持 pending，須連同所有未解決的已發布 SHA 一起補審。

正式程式有變動，須以版本產生器更新版本與 manifest，核對 index 中全部雜湊。
任何 push 前都須對最終提交完成完整本機品質關卡（含 full type-debt），
push 後核對該 SHA 的 GitHub CI 與既有 Security jobs；沒有結果或未通過均
不能宣告交付完成。最終 SHA、命令退出碼與 hosted run ID 留在本機驗證紀錄，
不得用較早 `c5e71c0` 的成功沿用為本批批准。
