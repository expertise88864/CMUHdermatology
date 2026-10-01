# 維護風險與施工清單（2026-09-30）

這是盤點時點的證據，不是永久完成狀態。基準 origin/main：`853f1336568c84599eccaaa4cbdfee9eabea5960`，版本 `2026.09.29.3`。後續重新核對 main、pending audit 及 CI。

範圍：剩餘約一年維護期，穩定既有六支程式、驗證與交接。不新增功能、不改臨床／排班規則、不全案重寫；無確認缺陷就停止改碼。

## 證據與排序

| 優先級 | 已核對風險／證據 | 小批次與停止條件 |
| --- | --- | --- |
| 1 | Phase 4 診斷、Phase 5 F1–F3 已在 main，但最終 Opus 全範圍補審與精確 audit 未結清 | 指定模型唯讀補審；重現 findings 才最小修復，新 diff 重審；逐 SHA audit、候選／正式 CI、fake smoke。quota 留 pending 並做獨立工作。 |
| 2 | 舊 README 的直接執行 src、push／review 命令與固定測試數不符現行啟動器／delivery | 文件批次整理啟動、發佈及整包回退；核對來源與命令，不改執行程式。 |
| 3 | 回歸分散在 tests／分期文件，缺簡短交接入口與院內模板 | A–F 可重跑清單、匿名回報及人工流程；實跑清單；模板保持未驗收。 |
| 4 | 大型 Tk／HIS／solver 模組耦合，但已有可測試邊界；拆模組本身無使用改善證據 | 只因重現缺陷局部修改，保持格式／行為；無量化收益不性能重構。 |

來源：[架構](第一期架構與驗收_2026-09-23.md)、[可測試邊界](第二期可測試邊界與排班量測_2026-09-24.md)、[性能品質](第三期排班效能與品質守門_2026-09-24.md)。歷史合成量測不能當目前院內速度。

## 基準 CI 與未結 review

基準候選：[test](https://github.com/expertise88864/CMUHdermatology/actions/runs/36510759123)、[Security](https://github.com/expertise88864/CMUHdermatology/actions/runs/36510759165)、[Delivery](https://github.com/expertise88864/CMUHdermatology/actions/runs/36510759115)；正式 main：[test](https://github.com/expertise88864/CMUHdermatology/actions/runs/36514023587)、[Security](https://github.com/expertise88864/CMUHdermatology/actions/runs/36514023602)、[Delivery](https://github.com/expertise88864/CMUHdermatology/actions/runs/36514023586)，盤點時成功。不替代後續 commit CI、Opus 或院內驗收。

Phase 4 review 基準 `509dafc131010f01181061bf60b93bc5f8348e8e`；未由精確 audit 結清十一筆：

```text
9f9cfdc340ed5f0fac3ab0b44ef14f4fb59c6e47
6bdd2e9973db01aa532abd0c1280eea3bb3d2662
71dc54a19909e3f130597c5a90104e49c4cac626
be44c4dea0851870dd2f4d9819eddaca76504f8d
e5d71ff2f6d3fa54391c242c0c7fa22079eceeb1
8a425e4b17f126379b6a910ffa6f3fc930a5dba5
a10757914fa3d97820446fc0c6d4a420e1a45025
5e6349d95d8cb8db51a167d9b192126a9b53408f
4abcb091b7a63fcd0aa50932c9ec13b72f741462
8744c7e57210cbfe6f2a1e08e52fee1ec3db617b
853f1336568c84599eccaaa4cbdfee9eabea5960
```

Phase 5 review 基準 `7c66bd7aa70d4b0fea50d4aa6f73e3d1cb7564da`；八筆：

```text
4ae49237f5b10456954d33794bdd5846d74863c2
04cba8ddf6a9bc680ef9c5a358439a7d118d0ec1
6b4365bcb4d9560fedf7deca3800792dd43ced9c
e88edec2448ce320094888e5ad631cf9f923c0dc
1a8913bfa4a54ee149a6d3c87b17b822c931fb4f
2748a74dbd817e29af5be5798721eb639f1b2a51
e219ea71b6c3d5a25321bcc8ca33461db67bea9d
7161844509832e04f1edad670b526209b81fe542
```

以上均在基準 main，不重複發佈／改寫 pending。模型固定 `claude-opus-5-5`／high／Read,Glob,Grep；quota 不算批准。避免 Phase 4／5 共享 Claude session 重疊；本機補審需電腦及 Codex 保持開啟。

## 交付與保留條件

本文件批次只整理 README、[手冊](maintenance_handoff.md)、[院內模板](hospital_acceptance_template.md) 及本清單。沿用匿名回歸，無新執行功能；測試數以當次輸出為準。未改 manifest 涵蓋檔案，版本／manifest 保持一致。

後續優先結清十九筆補審；若有缺陷另開最小批次，保存紅轉綠重現、review／pending、候選／正式 exact-SHA CI、fake smoke、版本／manifest、整包回退及實機待辦。不能因外部證據缺失而改守門。

維持一般月 PGY 照光＋治療室（週三下午僅照光）先於跟診，跟診 Clerk > 家醫 > 外訓 > PGY。恰兩位 PGY 月保留二早／四下／五早不排治療室及週別輪替；釋出 PGY 在 Clerk 入座後有剩餘診位才跟診（solve_day.py 管線／two_pgy 回歸，較新定案取代早期優先註解）。家醫指定是可跟診時段；照光負調整真的降低總負荷。醫囑／Excimer 及待確認寄送／打卡處理維持定案。

本輪尚未確認新的執行程式缺陷。院內驗收由使用者按模板完成；CI、fake smoke、文件整理不自動滿足此項。

## 2026-10-01 後續穩定性與效能批次

上方是 9 月 30 日盤點時點；本節記錄後續施工。新任務基準為 `40f33f8edcfb31201b4ab8b821fdc2dbd24806b6`，執行程式基準版本仍為 `2026.09.29.3`。候選修正版為 `2026.10.01.1`。是否已發佈及補審結清，必須查各完整 SHA 的候選／正式 CI、smoke、pending 與精確 audit；本文件不是通過證明。

| 採納批次 | 可重現問題與修改 | 本機證據及限制 |
| --- | --- | --- |
| 測試成本 | 三檔重複解析大型 source、重複 `inspect.getsource`，假 HTTP 重試卻真的睡 2／4／6 秒。以 source 內容鍵快取 AST、單次取 source、局部假 sleep 記錄延遲。 | 同機三組交錯，原 137 案例全部保留，新增 1 案例；中位數 92.858 → 6.687 秒。原斷言無刪除／改寫，故意繞過啟動器與寄送紀錄保護仍會轉紅。只代表這組測試，不能宣稱整輪 GitHub CI 也快 93%。 |
| 圖示穩定性 | 缺失／過期圖示讓 Tk 同步請求四個 URL；檔案 stat 權限錯誤可中斷視窗建立。另在本機 Windows 多層 ICO 的 Tk `iconbitmap` 路徑，反覆開關累積 GUI 物件。改 Tk 僅讀有效快取，Windows 僅走原有受控 native 圖示路徑。 | 假網路 4×0.2 秒 timeout 的三組交錯，中位數約 0.802 秒 → 0.000032 秒，候選零下載。有效快取讀取約 0.11ms，差異不足以主張一般啟動加速。圖示錯誤與 Windows 分支都有修改前失敗、修改後成功的測試。 |
| 量測與交接 | 補可重跑的離線 UI／資源、設定／匯出、真 executor 故障恢復工具，沿用原測試 harness 與排班 benchmark。 | 原始樣本／source hashes 保留；手冊明確分開程式本身與工具清理、真 Tk 與假 widget、正式啟動器與隔離建構。無新增使用者功能或正式背景排程。 |

Windows 11、Python 3.13.1 的 100 次獨立 Toplevel 圖示隔離測試：不套圖示 GDI 26 → 26；僅 Tk `iconbitmap` 53 → 2726；原雙路徑 59 → 3110；僅既有 native 路徑及修正版 32 → 410，後段停在既有 64 視窗 owned-handle 登記表上限，USER 停在 147。銷毀自身 icon 呼叫均成功；500ms 後 `WM_GETICON` 確认大小圖仍等於程式持有的圖示。這定位到本機 Tk ICO 載入路徑，不推論所有 Tk／Windows 版本都有同樣數值。

完整主 UI 重建 100 輪，修正後最後 20 輪固定 GDI 411、USER 147、handles 397、RSS 95,748,096 bytes、Python thread 1、child process 0；64-entry 圖示快取有界，不宣稱每個關閉視窗都立刻歸零。實際 executor 另跑 100 輪：滿載釋放後在同一 executor 恢復、去重／交接、舊世代晚回傳、停止後晚回傳各 25 輪，結果及佇列均符合斷言；末段沒有執行緒／子程序累積。

三組新 process 的畫面建構至首輪 Tk 事件，基準約 0.394–0.406 秒、候選約 0.391–0.399 秒；同 process 重開約 0.146–0.164 秒。工具 process 至首次 UI 約 1.15–1.48 秒，含隔離工具成本，沒有足夠依據宣稱一般冷啟動顯著加速。初次安裝依賴未量測、未混入上述時間；院內啟動器完整啟動仍須驗收。

匿名小案例的暖機後中位數：100 欄設定 JSON 0.18ms、排班設定／月份各約 0.09ms、匯出資料組裝 1.15ms、Excel 11.95ms、Word 63.02ms、PDF 2.89ms、假 widget 套用 300 筆門診訊息 0.75ms。各操作保留首筆和五筆暖樣本，只作基準，不報 p95。不能把小案例套用到所有月份、真表格重畫或網路環境。

沿用排班 benchmark：`pgy4_clerk5_mix1` 三筆約 17.20–17.25 秒，`pgy2` 約 0.026 秒，無 hard issues。這不表示所有軟目標已滿足：混合案例仍有切片／每週跟診目標警告，兩 PGY 案例總負荷為 43／41；保留原規則與完整警告，未為測速調整必要工作、容量或公平性。排班來源沒有修改，也未以本輪測速替代規則討論。

未採納：全面平行化測試、以 `type_debt --fast` 取代正式逐條檢查、縮短 solver 限時或降低品質門檻、大型模組拆分、另建非必要圖示背景下載／輪詢。已有可重現收益的小修正足夠；其餘未證明收益，不在僅餘一年維護期增加風險。SQLite 鎖定／寫入失敗、寄送 unknown／pending、打卡重啟／跨日與關閉後結果，沿用手冊既有回歸及本輪實跑證據，不複製新的業務狀態機。

新任務開始時未結清記錄為 Phase 4 十一筆、Phase 5 八筆、文件兩筆，共 21 筆；本輪新提交另計。Claude 固定完整模型 `claude-opus-5-5`／high／Read,Glob,Grep，額度錯誤或空 `modelUsage` 不算批准。所有未審提交保持 pending/high，依精確 SHA 補審與空 audit；舊文中的十九筆為更早盤點數，不可當目前總數。不得在補審與正式交付門檻未完成時宣告整體 goal 完成。

院內待驗收仍包括正式啟動器、不同解析度／DPI 下圖示與畫面、原熱鍵、真 HIS 版本、會診寄送及官方打卡紀錄；使用既有模板並保留病人／出勤資料在院內。本輪沒有代替醫師執行真實操作。
