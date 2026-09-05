# Watchdog 深度 review：程序辨識與歷史資料容錯

基準：`0a2feb8356171ebd6024283829e9dcf828765e44`。
修改於獨立 `codex/watchdog-review-20260906` 工作樹，不干擾上一批正在執行的
完整本機 CI，也不包含原 main 的既有未提交修改。

## 已查證與修正

1. **P1：資料引數被當作終止程序的身分證據。**
   `python.exe repair.py sample.pyw` 原本通過 `sample` 的精確比對，
   經生產 `kill_pids_verified` 路徑呼叫模擬 kill。改為辨識 Python 的 script
   operand，不再遍歷後續資料引數；拒絕 -c、-m、stdin、未知選項，保留常見
   Python 短選項、-W/-X 的值、-- 分隔與既有 script-only 觀測。
   僅接受無副檔名、.py、.pyw，不將同名 .backup 視為同一程式。
   此判準仍須搭配既有 Python PID 驗證及 handle pin；不是單獨的 kill 授權。
2. **P2：合法 JSON 的非物件根節點會中斷重啟授權。**
   歷史內容為非空陣列等值時，`.get` 拋出 AttributeError；假值則把已知世代
   清為 0。現在先檢查根型別，警告並保留記憶體中的歷史、暫停與世代。
   不更改既有持鎖、保存成功後授權、寫失敗降級出口或 crash-loop 門檻。
3. **P2：異常時間戳造成例外或永久暫停。**
   極大整數轉 float 會溢位，Infinity 暫停時間永遠不會到期。
   時間戳只接受有限數值，略過布林、溢位與非有限值；同份歷史內有效的
   計數及暫停紀錄照常載入。先重現 6 failed / 1 passed，再修正。

Python script 與後續 args 的區別依
[Python 3.13 命令列文件](https://docs.python.org/3.13/using/cmdline.html)。
不試圖完整支援未知執行模式；無法辨識時不授權破壞性動作。

## 驗證與界線

- 原碼先重現 18 failed / 12 passed，修正後再加入 attached option、help/version、
  組合選項，以及暫存磁碟上的歷史復原／多次全新 watchdog 計數案例。
- 根型別與 argv 修正時，新檔 39 個案例，連同三份既有核心／辨識／重啟鎖
  測試共 120 passed；再加入七個異常時間戳案例，最終須重新驗證。
- 最終定向驗證：10 份 watchdog 測試共 261 passed；Ruff 通過、Pyright
  0 errors / 0 warnings。這不是全套 CI 的替代。
  所有 PID / kill 動作皆模擬；歷史寫入僅在 pytest 暫存目錄。
- 沒有實際終止程序、重開機、登入、打卡、寄信或更動臨床收件設定。
- 程序辨識並非可對抗任意惡意 argv 偽裝的信任邊界；仍保留既有 PID 檔驗證。
- 不宣稱其他 watchdog 設定 schema、stale lock 接管競態，
  或整個專案的人工 review 已全部完成。效能調整仍待既定實機量測基準。

## 交付

正式程式變更須更新版本／manifest。提交後對最終 SHA 跑完整本機 CI、
full type-debt 與相依檢查，成功才能 push；再確認同 SHA 的 GitHub CI 與
Security 全綠。結果與 run ID 留在本機驗證紀錄，不能沿用父提交結果。

Claude Code 精確 Opus 5 / high 先前回覆 quota exhausted，沒有模型通過證據。
本批以 pending / high 提交，連同完整未審範圍由既有 05:55 排程補審；
電腦須開機且 Codex 執行。模型審查 pending 不豁免本機及遠端 CI。
