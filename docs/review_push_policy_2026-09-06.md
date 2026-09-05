# 發佈工具 review：最終版本與不可豁免的本機 CI

基準：`0f2a6138fa890b09d325eafc31be825a4605b375`。
本批只調整 push_helper、相關測試／文件及生成版本與 manifest；
不包含原 main 的五個既有未提交修改，也不編輯其他專案的全域規則。

## 已確認問題與修正

- **P1：舊 --emergency 仍可直接繞過品質關卡。** 依使用者 2026-09-05
  最新定案，CLI（含等號形式）、直接 quality-gate 與 commit 呼叫一律拒絕
  emergency 理由。沒有停用檢查、降低門檻或用新名稱重建旁路。
- **P1：原本先測試，再生成版本／manifest。** 改為先生成、核對 index 並
  建立本機 commit，再跑完整 CI。失敗保留本機成果，不 push。
  最終 SHA／工作樹／暫存區必須保持不變；文件、設定、新檔也納入乾淨狀態檢查。
- 型別債改為與 GitHub 同一條逐規則 full 路徑，不用 --fast 替代。
  另外檢查 pip 相依一致性及四項 lazy import。
- 主流程 push 固定已驗證完整 SHA 的 refspec，不用可能在最後檢查後被移動的
  分支作來源；保留一般 fast-forward push，沒有 force 或 no-verify。
- 印出完整 SHA，明確標示「尚待 GitHub CI」；工具成功推送不冒充遠端 CI
  或模型審查批准。操作者／排程仍須核對此 SHA 的 GitHub CI 與 Security。

## 回歸驗證

先新增 10 個案例，在原流程全部失敗，修正後全部通過。再新增 main／codex
分支使用固定 SHA、最後一刻 HEAD 變動阻擋的三個案例。
相關 push 測試修改中執行：52 passed、2 skipped；兩個是既有 clean-index
條件檢查，最終提交後的完整 CI 必須再跑，不把這次定向結果當作 push 許可。

舊測試明確要求「emergency 可繞過」，現在改為驗證「即使提供理由也拒絕」，
並補上實際 main 流程順序與變動阻擋行為。這是依最新使用者政策收緊要求，
不是為了全綠而削弱 CI。所有 Git push／commit 單元測試均使用假 subprocess。

## 驗證與外審狀態

本批須在最終提交上跑完整本機 CI、相依稽核、manifest／版本核對，通過才推；
其後追蹤該完整 SHA 的 GitHub CI / Security。精確結果另記本機驗證紀錄。
Claude Opus 5/high 仍因額度不足保持 pending，由既有 05:55 排程補審完整
未審範圍，電腦須開機且 Codex 執行。不宣稱整個專案人工 review 已全部完成。
