# -*- coding: utf-8 -*-
"""排班年度週期（2026-10-19 使用者定案）。

排班一年一個週期：每年 8 月 ～ 隔年 7 月。每年 8 月起，所有【公平性累計】
重新計算：R/VS 點數帳本、週六切片次數、VS 週末計數、R 年度週末次數。
★銜接規則不重置★：上月最後週末的人接本月連休、月初連值、色塊連週，
這些是安全與交接，7/31 → 8/1 照常接續。

★本週期的值怎麼算★（帳本檔不改寫，純推導）
  累計值 = history 各月分錄 + 被修剪掉的舊分錄 + 使用者直接改的修正。
  本週期 = 累計 − 週期之外的分錄 − 被修剪掉的舊分錄（記在 `trimmed`）。
    * 使用者直接改 ledger.json / biopsy.json（文件記載的應急手段）→ 保留，
      屬於本週期 —— 不可以因為 history 剛好滿了就默默丟掉（外審 PR #9 第 2 輪）。
    * `trimmed` 鍵存在 ＝ 從那時起每次修剪都記了總和（新版在 history 還不可能
      被修剪過時就標上）。沒有這個鍵、history 卻已滿保留月數 ＝ 舊版程式修剪過，
      被丟掉的總和無從得知 → 求解時【明講並停下】（不猜它屬於哪一期）。
  人員改代號/歸零/離開名單會同步改寫累計值、分錄與 `trimmed`。
"""
from __future__ import annotations

from cmuh_common.roster.ledger import HISTORY_KEEP_MONTHS as _LEDGER_KEEP
from cmuh_common.roster.saturday_biopsy import HISTORY_KEEP_MONTHS as _BIOPSY_KEEP

#: 週期起始月份（8 月）。
CYCLE_START_MONTH = 8


def cycle_start(ym: str) -> str:
    """ym 所屬週期的起始月 "YYYY-08"。"""
    y, m = int(ym[:4]), int(ym[5:7])
    if m < CYCLE_START_MONTH:
        y -= 1
    return f"{y:04d}-{CYCLE_START_MONTH:02d}"


def cycle_end_exclusive(ym: str) -> str:
    """ym 所屬週期之後的下一個週期起始月（不含）。"""
    return f"{int(cycle_start(ym)[:4]) + 1:04d}-{CYCLE_START_MONTH:02d}"


def in_cycle(month: str, ym: str) -> bool:
    """month 是否與 ym 屬於同一個週期。"""
    return cycle_start(ym) <= str(month or "") < cycle_end_exclusive(ym)


def cycle_label(ym: str) -> str:
    """報告/畫面用：「2026/8–2027/7」。"""
    y = int(cycle_start(ym)[:4])
    return f"{y}/{CYCLE_START_MONTH}–{y + 1}/{CYCLE_START_MONTH - 1}"


def cycle_months(ym: str) -> list:
    """週期起點到 ym（含）的每個月，升冪。"""
    out = []
    y, m = int(cycle_start(ym)[:4]), CYCLE_START_MONTH
    while f"{y:04d}-{m:02d}" <= ym:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def full_cycle_months(ym: str) -> list:
    """ym 所屬週期的全部 12 個月。"""
    y = int(cycle_start(ym)[:4])
    return [f"{y + (m < CYCLE_START_MONTH):04d}-{m:02d}"
            for m in list(range(CYCLE_START_MONTH, 13))
            + list(range(1, CYCLE_START_MONTH))]


def is_cycle_start(ym: str) -> bool:
    return int(ym[5:7]) == CYCLE_START_MONTH


def possibly_trimmed(history, keep_months: int = _LEDGER_KEEP) -> bool:
    """history 是否可能已被修剪過(已滿保留月數)。與 ledger.can_rollback 同一判準。"""
    months = {e.get("month") for e in (history or []) if e.get("month")}
    return len(months) >= keep_months


class UntrackedTrimError(ValueError):
    """舊版程式修剪過 history、被丟掉的總和無從得知 → 分不出舊餘額與修正。"""


def _untracked(book: dict, trimmed) -> bool:
    return bool(trimmed) and "trimmed" not in book


_UNTRACKED_MSG = (
    "{name} 的歷史已滿 {keep} 個月，且是舊版程式修剪的（沒有記下被修剪掉的總和），"
    "無法分辨帳本裡 history 以外的餘額是「更早的舊值」還是「直接修改的修正」——"
    "猜錯會讓新排班年度帶著上一年度的差額。請確認後在 {name} 加上 \"trimmed\": {{}}"
    "（表示 history 以外的餘額都算本年度的修正），或把舊值直接改掉，再重新排班。")


def ledger_in_cycle(ledger: dict, scope: str, ym: str, *,
                    trimmed: "bool | None" = None, strict: bool = False) -> dict:
    """本週期的帳本餘額 {person: 值}（見模組說明）。

    trimmed: 呼叫端在回滾之前判定的「可能被修剪過」；None → 就地判定。
    strict: 遇到舊版修剪、無從分辨時拋 UntrackedTrimError(求解路徑);否則
        (顯示)退回只加總本週期分錄。"""
    hist = ledger.get("history") or []
    if trimmed is None:
        trimmed = possibly_trimmed(hist)
    book = ledger.get(scope) or {}
    inside: dict = {pid: 0.0 for pid in book}
    outside: dict = {}
    for e in hist:
        if e.get("scope") != scope:
            continue
        target = inside if in_cycle(e.get("month"), ym) else outside
        for pid, delta in (e.get("deltas") or {}).items():
            target[pid] = target.get(pid, 0.0) + float(delta)
    if _untracked(ledger, trimmed):
        if strict:
            raise UntrackedTrimError(_UNTRACKED_MSG.format(
                name="ledger.json", keep=_LEDGER_KEEP))
        return {pid: round(v, 4) for pid, v in inside.items()}
    gone = (ledger.get("trimmed") or {}).get(scope) or {}
    return {pid: round(float(v or 0.0) - outside.get(pid, 0.0)
                       - float(gone.get(pid, 0.0)), 4)
            for pid, v in book.items()}


def biopsy_in_cycle(book: dict, ym: str, *, trimmed: "bool | None" = None,
                    strict: bool = False) -> dict:
    """本週期的週六切片次數 {person: 次}（規則同 ledger_in_cycle，不低於 0）。"""
    hist = book.get("history") or []
    if trimmed is None:
        trimmed = possibly_trimmed(hist, _BIOPSY_KEEP)
    counts = book.get("counts") or {}
    inside: dict = {mid: 0 for mid in counts}
    outside: dict = {}
    for e in hist:
        target = inside if in_cycle(e.get("month"), ym) else outside
        for mid in (e.get("assign") or {}).values():
            target[mid] = target.get(mid, 0) + 1
    if _untracked(book, trimmed):
        if strict:
            raise UntrackedTrimError(_UNTRACKED_MSG.format(
                name="biopsy.json", keep=_BIOPSY_KEEP))
        return inside
    gone = book.get("trimmed") or {}
    return {mid: max(0, int(v or 0) - outside.get(mid, 0) - int(gone.get(mid, 0)))
            for mid, v in counts.items()}
