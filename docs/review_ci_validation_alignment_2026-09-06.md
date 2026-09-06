# 完整 CI 回饋：測試政策與子行程編碼

`0d8c3a45fbe0f200ce10a8a14c8a242f02ffe48e` 的完整 pytest 實跑為
1 failed、6682 passed、0 skipped、4 warnings（740.99 秒），因此沒有 push。

失敗項 `test_push_helper_actually_runs_the_ratchet` 還要求一定傳 `--fast`。
2026-09-05 使用者已要求完整適用本機 CI，發佈工具也已改為與 GitHub 相同的
逐規則 full 路徑；本次保留「棘輪存在且會阻擋 push」測試，並把模式要求收緊為
不能用 --fast 代替 full，不改型別債基線、不取消任何關卡。

四則警告出自 mail-quota 真子行程測試的 pipe reader：子行程繼承 UTF-8
輸出設定，父行程卻用 Windows 本地編碼解碼，中文診斷會使背景讀取執行緒拋錯。
新增專用案例在舊 helper 上出現 1 failed / 1 teardown error；修正為父子雙方
明確 UTF-8。新案例將背景執行緒警告當作錯誤，並核對中文 stderr 未遺失；
沒有隱藏警告、降低檢查要求，也沒有修改正式寄信配額或使用正式信箱。

這是前述三批未推修改的驗證補正。須在新的最終 SHA 重跑完整 CI 後才可
push，再核對該 SHA 的 GitHub CI/Security。原失敗紀錄保留，不視為通過。
Claude Opus 5/high 仍 pending，既有 16:45 台北時間補審排程涵蓋更新後全範圍。
