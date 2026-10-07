# Project agreements

## Claude Code 接手入口

先讀 [AGENTS.md](AGENTS.md)、[交付規則](REMOTE_CI_DELIVERY.md) 與
[維護手冊的開發入口](docs/maintenance_handoff.md#安全開發與候選發佈2026-10-07)。
較早文件中的裸提交訊息、全域環境搬家腳本及直接推 main 指令是歷史流程，
不能代替目前命令。未完成審查依完整 SHA 與後續 audit 核對，不以單一 passed 推定全部結清。

使用新的隔離 `codex/*` 工作副本，先查看 `git status --short`、`git rev-parse HEAD`、
`git rev-parse origin/main` 及 `git config --get core.hooksPath`；保留其他工作副本與使用者修改。
開發測試使用匿名 fixtures，不複製正式 `settings/`，也不啟動六支正式程式作 smoke。
開發環境工具預設只檢查；需建立環境時明確使用 `apply`，不修改全域模型／MCP／規則。

| 修改方向 | 來源入口 | 相關驗證入口 |
| --- | --- | --- |
| HIS 熱鍵、門診刷新 | `src/main.py`、`src/cmuh_common/his_memo_port.py`、`uvb_dose.py` | 手冊 A 組與照光日期換行回歸 |
| 排班 | `src/scheduler.py`、`src/cmuh_common/roster/` | 手冊 B 組與既有品質 benchmark |
| 會診與郵件 | `src/consult_query.py`、`delivery_ledger.py` | 手冊 C 組 |
| 打卡 | `src/autoclock.py`、`src/cmuh_common/action_ledger.py` | 手冊 D 組 |
| 啟動／復原／守護 | 根目錄六支 `.pyw`、`version_pointer.py`、`src/bootstrap_recovery.py`、`watchdog_runner.py` | 手冊 E 組與新程序復原回歸 |
| 開發交付 | `scripts/push_helper.py`、`tools/dev-env-setup.py`、`_delivery.py` | 手冊安全開發入口與 Delivery contract |

詳細模組責任、已定案行為、整包回退及院內待驗收項目留在
[維護與交接手冊](docs/maintenance_handoff.md)；新增功能可以另立明確範圍，
臨床／排班規則的語意變更仍需使用者定案。

## 最新使用者定案：全域 diff review 模型（2026-09-23）

- 新 diff review 與未完成補審固定使用完整模型 ID `claude-opus-5-5`、effort `high`、唯讀工具，核對實際 `modelUsage`；不得以 alias、舊 Opus 5 或其他模型代替。
- 本節取代較早規則中的 `claude-opus-5` 模型指定；完整 diff、獨立審查、CI 與資料保護門檻不變。
- 沿用既有 `Claude-Opus-5-Review`、`Claude-Opus-5-Review-Effort`、`Claude-Opus-5-Reviewed-Commit` trailer 名稱；實際模型以 `modelUsage` 為證。

## 最新使用者定案：遠端 CI 候選驗證與正式發佈（2026-09-06）
本節取代下方／舊任務／舊排程中「每次候選 push 前完整本機 CI」及「直接推 main 再修」的規則；不變更醫療內容核可、獨立 Codex/Claude review 或資料保護要求。
- 四個專案採本機快速檢查與相關回歸測試 → codex/* 候選分支 → 完整遠端 CI → 已驗證同一 SHA 才正常快轉進 main；候選 push 不要求先有完整本機 CI。
- 使用 _delivery.py 與 _delivery_policy.json；正式 push 的 pre-push hook 必須驗證 exact SHA 的候選 push workflows/jobs/steps 成功，並確認最新 main 是候選祖先。新的修改、生成、整合或 rebase 使旧證據失效。
- 網站還須 same-repository PR、exact-SHA Vercel Preview 與瀏覽器檢查；Vercel 正式建置前另驗證候選 CI，未驗證版本不可上線。部署成功不等於 CI 通過。
- HsiaoEye 視覺基準只能由 Ubuntu 產生並人工確認，不自動接受差異；保留 CMS 新修改，衝突停止，不 force-push。CMS 存檔不等於正式發佈完成。
- 晨報候選 CI 不得寄信、寫回正式 state 或觸發正式排程；變更產報/LLM/外部資料關鍵路徑時另做不寄信 dry-run。既有正式寄信排程不得因候選驗證中斷。
- CI 失敗持續診斷並修正可確認缺陷，修正後重跑完整遠端驗證；取消、逾時、缺失、讀不到及應跑卻跳過皆不通過。禁止 skip-ci、降門檻或繞 hook 製造全綠。
- Claude 固定 claude-opus-5-5 / high / read-only；quota pending 只延後模型審查，不豁免正式發佈的遠端 CI。保留精確 pending/passed/Reviewed-Commit trailers 及重置後補審。
- main 發佈後還要驗證 exact-SHA 正式 CI／部署及適用 smoke checks，才可宣告交付；純文件與空 audit commit 也走候選流程。
- 詳細入口、範圍與限制見 REMOTE_CI_DELIVERY.md。不得把本機快速檢查說成完整 CI，也不得把候選 CI 綠燈當作正式部署成功。
