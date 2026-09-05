# 測試資源清理續輪

基準：`59a19734cfb78b82bb3499a06d9684859ed72052`。
此批只修改測試與本記錄，不變更正式程式、應用版本或 manifest。

## 已確認並修正

- `test_delivery_phases_2026_08_08.py` 六處 SQLite 連線（七次執行）
  原本只結束交易、未關閉 connection；外加 closing，保留原 transaction context。
- 同檔圖片讀取與兩處 AST 原始碼讀取改為 with context，斷言不變。
- `test_sqlite_cache.py` 四個記憶體連線交由 ExitStack fixture 清理，
  保留原本的 isolation_level，包含斷言失敗時也會關閉。

三個相關測試檔使用 `-W error::ResourceWarning` 與
`-W error::pytest.PytestUnraisableExceptionWarning`：73 passed。
Ruff 與 diff whitespace 檢查通過。這些是定向驗證，不代表整個專案已審完，
也不代替本批最終 SHA 的全套 CI。未降低警告設定、覆蓋率或 skip 門檻。

## 交付條件

提交後對乾淨工作樹重跑全套本機 CI 等效檢查及完整 type-debt、相依檢查，
全部通過才能 push；其後須確認同一完整 SHA 的 GitHub CI / Security 全綠。
命令結果、最終 SHA 與 hosted run ID 存放於本機驗證紀錄。

Claude Code 已回報額度不足，本批保持 `Claude-Opus-5-Review: pending`、
effort high；由既有 05:55 補審排程連同所有尚未有精確 SHA audit 記錄的
提交補審，不將 quota 回覆視為模型批准。電腦須開機且 Codex 正在執行。
