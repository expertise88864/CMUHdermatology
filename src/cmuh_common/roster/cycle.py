# -*- coding: utf-8 -*-
"""排班年度週期（2026-10-19 使用者定案）。

排班一年一個週期：每年 8 月 ～ 隔年 7 月。每年 8 月起，所有【公平性累計】
重新計算：R/VS 點數帳本、週六切片次數、VS 週末計數、R 年度週末次數。
★銜接規則不重置★：上月最後週末的人接本月連休、月初連值、色塊連週，
這些是安全與交接，7/31 → 8/1 照常接續。

★帳本檔本身不改寫★：`ledger.json` / `biopsy.json` 的累計值照舊一路累加，
「本週期的餘額」是讀取時扣掉【週期開始之前】那些分錄算出來的（純推導，
不需要遷移，也不怕某台電腦在 8 月沒開程式就錯過「歸零」的時機）。
被 `_trim_history` 修剪掉的舊分錄，修剪時會累加到 `trimmed`，一樣扣掉 ——
否則分錄一被修剪，它的值就會在扣除時消失、重新回到本週期的餘額裡。
"""
from __future__ import annotations

#: 週期起始月份（8 月）。
CYCLE_START_MONTH = 8


def cycle_start(ym: str) -> str:
    """ym 所屬週期的起始月 "YYYY-08"。"""
    y, m = int(ym[:4]), int(ym[5:7])
    if m < CYCLE_START_MONTH:
        y -= 1
    return f"{y:04d}-{CYCLE_START_MONTH:02d}"


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


def ledger_pre_cycle(ledger: dict, scope: str, ym: str) -> dict:
    """點數帳本中【ym 所屬週期開始之前】的累計 {person: 值}。

    = 該 scope 早於週期起點的分錄 deltas 總和 + 被修剪掉的舊分錄累計。"""
    start = cycle_start(ym)
    out: dict = {}
    for e in ledger.get("history") or []:
        if e.get("scope") != scope or str(e.get("month") or "") >= start:
            continue
        for pid, delta in (e.get("deltas") or {}).items():
            out[pid] = out.get(pid, 0.0) + float(delta)
    for pid, v in ((ledger.get("trimmed") or {}).get(scope) or {}).items():
        out[pid] = out.get(pid, 0.0) + float(v)
    return out


def ledger_in_cycle(ledger: dict, scope: str, ym: str) -> dict:
    """本週期的帳本餘額 {person: 值}（累計值扣掉週期之前的部分）。"""
    pre = ledger_pre_cycle(ledger, scope, ym)
    return {pid: round(float(v or 0.0) - pre.get(pid, 0.0), 4)
            for pid, v in (ledger.get(scope) or {}).items()}


def biopsy_pre_cycle(book: dict, ym: str) -> dict:
    """切片計數中【ym 所屬週期開始之前】的次數 {person: 次}。"""
    start = cycle_start(ym)
    out: dict = {}
    for e in book.get("history") or []:
        if str(e.get("month") or "") >= start:
            continue
        for mid in (e.get("assign") or {}).values():
            out[mid] = out.get(mid, 0) + 1
    for mid, v in (book.get("trimmed") or {}).items():
        out[mid] = out.get(mid, 0) + int(v)
    return out


def biopsy_in_cycle(counts: dict, book: dict, ym: str) -> dict:
    """本週期的切片次數（counts 扣掉週期之前的部分，不低於 0）。"""
    pre = biopsy_pre_cycle(book, ym)
    return {mid: max(0, int(v or 0) - pre.get(mid, 0))
            for mid, v in (counts or {}).items()}
