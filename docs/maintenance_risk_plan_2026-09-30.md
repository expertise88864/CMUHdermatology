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
