# 收信驗證 review：結果與身分必須屬於同一段

基準：`518c8cc1e7d6b03c9181d28172402b886677237d`。
本批僅修改 Authentication-Results 解析、回歸測試、此紀錄及生成版本／manifest。
不更動正式帳密、臨床收件人、白名單、網域對齊政策或輪詢頻率。

## 已確認問題

原解析以 `find("dkim=pass")` 等字串與後方固定長度片段找身分，會將
前一段 pass 與下一段 fail 的對齊網域拼接，誤判寄件人已驗證。
例如 `dkim=pass; dkim=fail header.d=trusted.example` 不應驗證
`doctor@trusted.example`。結果關鍵字前後綴、註解與 reason 文字也可能被誤採信。

依 [RFC 8601 §2.2](https://www.rfc-editor.org/rfc/rfc8601.html#section-2.2)
的 method/result/property 分段關係，修正為辨識註解、引號與跳脫字元後再切分。
只採用完整 pass 結果自身的屬性，不向其他結果借用身分。
保留一般引號值、帶引號的郵件 local-part、CFWS、方法版本 1 與既有對齊政策。
不平衡引號／註解及同段重複屬性不作為驗證證據；這不是通用完整 RFC parser。

## 回歸證據

- 初版新增案例在舊實作呈現 19 failed / 11 passed，包含一個真正走到
  `check_trigger` UID 驗證結果的反例。
- 補入 quoted local-part 相容案例後，共 31 個新增案例；與原收信、trigger
  授權測試合跑為 110 passed。Ruff、Pyright、diff whitespace 檢查通過。
- 測試使用保留的 example 網域與假 IMAP；沒有連線正式信箱、標記真實郵件
  已讀、發送郵件或執行遠端指令。

定向測試不等於完整 CI。須在最終 commit 上重跑全部適用本機關卡，通過才推送；
再核對相同完整 SHA 的 GitHub CI 與 Security。外部 Opus 5/high 仍須提供
機器可核對的實際模型證據，未取得前保留 pending，不宣稱全專案 review 完成。
