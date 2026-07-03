# -*- coding: utf-8 -*-
"""排班程式核心邏輯套件（純函式、無 UI import、全可 pytest）。

規格唯一來源：docs/排班程式設計文件.md（v1.1）。
模組分工：
    model.py      資料模型 + 值班區塊/週色/月曆工具（純計算）
    storage.py    唯一檔案 IO 層（原子寫入 + 快照 + schema_version）
    ledger.py     點數帳本（月結 / 回滾 / 人員異動歸零）
    rules.py      規則註冊表 + R/VS 規則宣告（precheck + CP-SAT 約束）
    solve_rvs.py  R/VS 值班求解器（CP-SAT + 放寬階梯 L0-L3）
    report.py     四段式決策報告產生器

重依賴 ortools 僅 solve_rvs 內 lazy import（UI 按「自動排班」時才需要安裝）。
釘選版本：ortools==9.15.6755（不同版本可能給出不同但皆合法的解，
升版視為有意識的決策 — 設計文件 §16.5）。
"""

ORTOOLS_PINNED_VERSION = "9.15.6755"
