# -*- coding: utf-8 -*-
"""排班年度週期（2026-10-19 使用者定案）。

排班一年一個週期：每年 8 月 ～ 隔年 7 月。每年 8 月起，所有【公平性累計】
重新計算：R/VS 點數帳本、週六切片次數、VS 週末計數、R 年度週末次數。
★銜接規則不重置★：上月最後週末的人接本月連休、月初連值、色塊連週，
這些是安全與交接，7/31 → 8/1 照常接續。

★本週期的值怎麼算★（帳本檔不改寫，純推導，不需要遷移）
  累計值 = history 各月分錄的總和 + 「史外餘額」(history 解釋不了的部分)。
  史外餘額有兩種來源，處理方式不同：
    * 使用者直接改 ledger.json / biopsy.json（文件記載的應急手段）→ 屬於現在，
      要算進本週期；
    * 舊分錄被 `_trim_history` 修剪掉（舊版修剪時沒留下總和，外審 PR #9）→
      只會發生在 history 已滿 HISTORY_KEEP_MONTHS（24）個月時，而被丟掉的必然是
      最舊的月份 → 必在本週期之前，不可帶進新週期。
  所以：history【不可能被修剪過】(不足 24 個月) → 本週期＝累計 − 週期之外的分錄；
        【可能被修剪過】→ 本週期＝只加總落在本週期的分錄(24 個月必含整個週期)。
  「可能被修剪過」要在求解端於記憶體回滾【之前】判定(回滾會讓月數變少)。
  人員改代號/歸零/離開名單會同步改寫累計值與分錄，兩種算法都跟著正確。
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


def ledger_in_cycle(ledger: dict, scope: str, ym: str, *,
                    trimmed: "bool | None" = None) -> dict:
    """本週期的帳本餘額 {person: 值}（見模組說明）。

    trimmed: 呼叫端在回滾之前判定的「可能被修剪過」；None → 就地判定。"""
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
    if trimmed:
        return {pid: round(v, 4) for pid, v in inside.items()}
    return {pid: round(float(v or 0.0) - outside.get(pid, 0.0), 4)
            for pid, v in book.items()}


def biopsy_in_cycle(book: dict, ym: str, *, trimmed: "bool | None" = None) -> dict:
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
    if trimmed:
        return inside
    return {mid: max(0, int(v or 0) - outside.get(mid, 0)) for mid, v in counts.items()}
