# 中國醫皮膚科常用程式

中國醫藥大學附設醫院皮膚部的 Windows 診間與行政工具。剩餘維護期以既有功能穩定化與交接為主，不因整理架構而更改臨床或排班規則。

## 安裝與啟動

取得完整程式資料夾後，執行根目錄 `第一次執行先點我.bat`。安裝問題先查 `settings/python_setup.log`、`settings/dependency_install.log`；開機啟動使用 `安裝開機自動啟動.cmd`。

平常雙擊根目錄 `.pyw` 啟動器或其捷徑。啟動器先處理更新復原，再依 `current.txt` 選擇完整版本；直接執行 `src/*.py` 會略過這些步驟，不適合作為正式啟動方法。

| 啟動器 | 原始入口 | 用途 |
| --- | --- | --- |
| 中國醫皮膚科主程式.pyw | `src/main.py` | HIS 熱鍵、門診動態與通知；F7 浮動門診、F8 快速文字、F12 中止 |
| 中國醫皮膚科排班程式.pyw | `src/scheduler.py` | R／VS、PGY／Clerk／外訓／家醫排班、定案與匯出 |
| 中國醫皮膚科會診查詢程式.pyw | `src/consult_query.py` | 會診查詢、去識別化郵件與寄送核對 |
| 中國醫皮膚科打卡程式.pyw | `src/autoclock.py` | 排程打卡、官方紀錄確認與待確認保護 |
| 中國醫皮膚科守護程式.pyw | `src/watchdog_runner.py` | 監看與受保護的重啟 |
| 中國醫皮膚科點座標偵測程式.pyw | `src/coord_detector.py` | 座標／顏色校正；使用時避免與主程式熱鍵衝突 |

## 維護入口

- [維護與交接手冊](docs/maintenance_handoff.md)：回歸指令、發佈、整包回退、匿名回報與人工處理。
- [院內驗收紀錄模板](docs/hospital_acceptance_template.md)：實機待填，自動測試不能代替實機結果。
- [維護風險與施工清單](docs/maintenance_risk_plan_2026-09-30.md)：指定 SHA 的盤點與批次範圍。
- [架構與責任邊界](docs/第一期架構與驗收_2026-09-23.md)、[可測試邊界](docs/第二期可測試邊界與排班量測_2026-09-24.md)、[排班效能與品質](docs/第三期排班效能與品質守門_2026-09-24.md)。
- [會診／打卡執行摘要](docs/runtime_diagnostics_2026-09-28.md)、[F1–F3 HIS 寫入流程](docs/第五期主程式HIS寫入流程與驗收_2026-09-25.md)。

## 修改與交付

以使用者最新定案、`AGENTS.md`、[REMOTE_CI_DELIVERY.md](REMOTE_CI_DELIVERY.md) 及 `_delivery_policy.json` 為準。隔離 `codex/*` 分支：相關本機回歸與快速檢查 → 完整 diff 的獨立 Codex／Claude review → 候選 exact-SHA 遠端 CI → **同一 SHA** 正常快轉進 main → 正式 exact-SHA CI 與適用假環境 smoke。文件與空 audit commit 也遵守此流程。

Claude 固定完整 ID `claude-opus-5-5`、effort `high`、唯讀工具，核對機器回傳 `modelUsage`。額度不足只代表 pending，不能當成核准或省略 CI；較早文件不取代最新使用者定案。

GitHub CI 包含 Windows／Python 3.13 完整 pytest、Ruff、Pyright、skip／coverage／type-debt、Security 及 Delivery contract。指令見手冊；測試數量以當次輸出為準。

`settings/`、病人資料、帳密、正式排班、寄送／打卡帳本及原始日誌不得提交或上傳。診斷匯出須人工檢查後才分享；manifest SHA256 校驗不代表院內驗收完成。
