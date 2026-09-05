# CI #509：flush 通知測試的時序隔離

基準：`dab10b085797840db4022e77e729aa2ab6ce8877`。使用者提供的失敗 run
針對較早的 `5fdbe52d8c0c9037720b528ba986d90eca9a5e9d`；新一次 CI 成功
不能證明舊的間歇性失敗已消除。

## 已確認原因

`GitSyncStorage.__init__` 即使設定 `pull_interval_sec=0`，仍會呼叫
`_schedule_push()`，排入預設三秒的 startup push。原測試從 B 建構開始
累計通知，卻在 B.flush 完成後要求整份通知清單為空。

當 A 存檔／flush 耗時較久，B 的 startup push 可在 B.flush **之前**拉進
A 的資料並合法通知。這不是 flush 本身通知，原斷言卻將兩條路徑混在一起。
以手控 timer 強制「A.flush 完成 → B startup push → B.save／flush」順序，
原測試確實重現 `AssertionError: [1] != []`，完全不需要依賴隨機 sleep。
既有 CI annotation 本身不含執行緒時序，因此這是已確認可重現的成因，
不宣稱從該 annotation 已排除所有其他並行路徑。

## 最小修正與不變範圍

- 本測試模組的 push timer 改由 fixture 手動觸發；保留真實本地 bare Git
  remote、clone、commit、fetch、merge/rebase、push、資料與通知斷言。
- 保留 `test_flush_does_not_notify` 的零通知與資料合併檢查，不刪測試、
  不標 skip、不放寬 expected；新增 startup push 先通知、flush 不追加
  通知的明確交錯案例。
- 既有真實 timer 整合案例仍在 `test_roster_p2_batch_2026_08_19.py` 與
  `test_roster_gitsync_uncommitted_2026_08_22.py` 執行，不受本檔 fixture 影響。
- 本批只修改測試與此報告；正式程式、排班資料、帳密、臨床收件者及 CI
  工作流程均未修改，不發布新的應用程式版本，也不改 manifest。
- Node.js 20 deprecation 是另項 action runtime 警告；本次 exit 1 的明確
  錯誤是 pytest assertion，不藉此擴大修改 Actions 版本。

## 驗證證據與交付限制

- 上述三個 GitSyncStorage 測試檔合跑：**44 passed，89.27 秒**。
- 反向驗證：僅在獨立測試行程中故意將 flush 的 push 改成通知模式，
  修正後測試仍以相同 `[1] != []` 失敗（1 failed），確認檢查沒有被削弱。
  該故障注入沒有寫入正式檔案。
- Ruff、pip check 與四項 lazy CI 相依匯入檢查成功。
- 推送前仍須對最終內容完成全套本機 CI（包含完整型別債檢查）；推送後
  必須核對該 SHA 的 GitHub CI 與 Security 結果，不能沿用先前 SHA 的成功。
- Opus 5 / high 仍待額度恢復補審，提交必須標記 pending；本報告不是外審
  通過紀錄，也不表示整個專案人工 review 已完成。
- 原工作區另有五個既有程式／測試修改及兩份未追蹤規則文件，不納入本批。
