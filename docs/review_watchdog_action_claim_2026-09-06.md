# Watchdog 動作鎖 review：接管時的競爭窗口

基準：`0d8c3a45fbe0f200ce10a8a14c8a242f02ffe48e`。
只修改動作鎖接管／撤回的同步、測試、文件及生成版本／manifest。

## 確認的問題

`claim_action_lock` 的「讀取過期 mtime → unlink → O_EXCL 建立」不是一個
不可分割的操作。A 看見舊鎖後，B 可以先刪掉舊鎖並成功建立新鎖；A 隨後
刪掉的就變成 B 的新鎖。兩者都回傳 True，內、外層 watchdog 同輪可能
重複啟動程式。新增受控交錯測試在旧實作得到 `[True, True]`，明確失敗。

## 修正邊界

- 永久保留獨立 `.lock.guard` sidecar，以 Windows 非阻塞 byte lock 保護
  讀取／接管／撤回整段；程式退出時由作業系統釋鎖，guard 本身不刪除。
- 同一行程也以每路徑的 threading.Lock 防止重入；不同程式的鎖互不阻擋。
- 取不到 guard 或 I/O 失敗時略過本輪，不退回無鎖操作。
- 保留既有 `.lock` payload、PID 撤回條件、90 秒冷卻與重啟授權流程；
  沒有改輪詢頻率、crash-loop 門檻或正式程式的 kill/start 行為。
- 這個保護需要各 watchdog 使用新版；舊版程序不認識 guard，混合版本的
  過渡期不能宣稱具備完整跨版本互斥。這不是整段 kill/start 的長時間交易鎖。

## 測試

新增 7 個案例，含受控接管交錯、實際 `ensure_program` 啟動要求只產生一次、
真正子行程的互斥／不同 key 並行、子行程未執行 finally 即退出後 OS 釋鎖、
guard I/O 失敗保留舊紀錄、body exception 傳遞與 descriptor 清理。
11 個 watchdog 測試檔合計 268 passed；Ruff、Pyright 全部通過。
所有啟動動作都用假函式，真正子行程只執行鎖測試，沒有操作正式應用程式。

定向通過不是 push 許可；最終 commit 必須重新通過完整本機 CI，push 後
核對相同 SHA 的 GitHub CI 與 Security。Claude Opus 5/high 仍 pending；
最近實際請求回報 2026-09-06 16:40（Asia/Taipei）恢復，既有補審排程
設為 16:45，會審完整未審範圍而不是只審之前快照。電腦及 Codex 須保持執行。
本紀錄不表示整個專案人工深度 review 或效能驗收已完成。
