# -*- coding: utf-8 -*-
"""週六切片輪排（2026-07-13 使用者需求）。

R2/R3 兩位住院醫師輪流負責每個週六的切片時段：
  - 該週六【值班】者是兩人其中一位 → 切片＝該人（值班連動）。
  - 兩人該週六都沒值班 → 取「累計次數」較少者（次數平衡；同數 → 與上一次
    切片不同者優先輪替，再同 → R2 在前）。
  - 目標＝一整年下來兩人次數盡量平均（每月 4-5 個週六 → 一人約 2-3 次）。
    counts 永續累計於 biopsy.json；同月重排先回滾該月舊分錄再重記（仿 ledger）。

本模組只有純函式與資料結構操作，無檔案 IO：
  IO → RosterStorage.load_biopsy()/save_biopsy()；編排 → RosterService。
與 Clerk 平日「切片室」（solve_day.BIOPSY）無關，勿混用。
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Optional

BIOPSY_LEVELS = ("R2", "R3")

# 與 ledger.HISTORY_KEEP_MONTHS 同理：history 只供「同月重排回滾」與「跨月輪替
# 決勝」用，修剪避免無限膨脹。
HISTORY_KEEP_MONTHS = 24


def biopsy_pair(members) -> tuple:
    """從成員名單取 (pair, notes)。pair=[R2 成員, R3 成員]（各級第一位）。

    缺任一級 → pair 回空 list ＋人話 note（保守：不硬指派單人包全年）。
    同級多人 → 取名單順序第一位並 note 提醒。
    """
    notes: list = []
    pair: list = []
    for lvl in BIOPSY_LEVELS:
        cands = [m for m in members
                 if (m.level or "").strip().upper() == lvl]
        if not cands:
            notes.append(f"名單缺 {lvl} 級住院醫師 → 本月不自動排週六切片，"
                         f"請手動安排")
            return [], notes
        if len(cands) > 1:
            notes.append(f"{lvl} 級有 {len(cands)} 位，週六切片取名單第一位 "
                         f"{cands[0].name or cands[0].id}")
        pair.append(cands[0])
    return pair, notes


def month_saturdays(year: int, month: int) -> list:
    _, last = calendar.monthrange(year, month)
    return [date(year, month, d) for d in range(1, last + 1)
            if date(year, month, d).weekday() == 5]


def assign_saturday_biopsy(*, year: int, month: int, members, duty: dict,
                           leaves: dict, counts: dict,
                           last_person: Optional[str] = None) -> tuple:
    """排該月每個週六的切片 → (assign, notes)。決定性（同輸入同輸出）。

    duty:   {date: member_id} 該月值班（只看週六的鍵）
    leaves: {member_id: set[date]} 請假
    counts: {member_id: int} 累計切片次數（【不含】本月 —— 呼叫端先回滾本月）
    last_person: 本月之前最近一次切片的人（跨月「同數輪替」決勝；None 可）

    assign: {date: {"person": mid, "reason": "值班連動"|"次數平衡"}}
    notes:  人話清單（缺級、兩人皆請假等）
    """
    pair, notes = biopsy_pair(members)
    if not pair:
        return {}, notes
    pair_ids = [m.id for m in pair]
    run = {mid: int(counts.get(mid, 0)) for mid in pair_ids}
    last = last_person if last_person in pair_ids else None
    assign: dict = {}
    for sat in month_saturdays(year, month):
        duty_p = duty.get(sat)
        on_leave = {mid for mid in pair_ids
                    if sat in (leaves.get(mid) or set())}
        # [codex P2] 請假最高優先(全系統 R4 原則):值班連動也不得把「當日請假」
        # 的人排切片——手動改格可造成「值班=請假者」的矛盾班表(驗證層警告但不擋
        # 存),此時切片退回次數平衡並附註,不放大矛盾。
        if duty_p in pair_ids and duty_p not in on_leave:
            pick, reason = duty_p, "值班連動"
        else:
            if duty_p in pair_ids and duty_p in on_leave:
                notes.append(f"{sat.month}/{sat.day}(六) 值班 {duty_p} 當日"
                             f"請假（班表矛盾，請假優先）→ 切片改按次數平衡")
            cands = [mid for mid in pair_ids if mid not in on_leave]
            if not cands:
                notes.append(f"{sat.month}/{sat.day}(六) R2/R3 皆請假 → "
                             f"切片未排，請手動安排")
                continue
            # 次數平衡：(累計次數, 是否為上次切片者, 名單序) —— 同數時與上次
            # 不同者優先（自然輪替）、再同取 R2 在前。
            pick = min(cands, key=lambda mid: (run[mid], mid == last,
                                               pair_ids.index(mid)))
            reason = "次數平衡"
        assign[sat] = {"person": pick, "reason": reason}
        run[pick] += 1
        last = pick
    return assign, notes


# ─── 計數帳本（biopsy.json；仿 ledger 的回滾語意）────────────────────────────
def _trim_history(book: dict, keep_months: int = HISTORY_KEEP_MONTHS) -> None:
    hist = book.get("history") or []
    months = sorted({e.get("month") for e in hist if e.get("month")},
                    reverse=True)
    if len(months) <= keep_months:
        return
    keep = set(months[:keep_months])
    book["history"] = [e for e in hist if e.get("month") in keep]


def rollback_biopsy(book: dict, ym: str) -> bool:
    """移除該月分錄影響（重排前呼叫）。回傳是否有回滾。"""
    hist = book.setdefault("history", [])
    counts = book.setdefault("counts", {})
    rolled = False
    kept = []
    for entry in hist:
        if entry.get("month") == ym:
            for mid in (entry.get("assign") or {}).values():
                if mid in counts:
                    counts[mid] = max(0, int(counts[mid]) - 1)
            rolled = True
        else:
            kept.append(entry)
    book["history"] = kept
    return rolled


def settle_biopsy(book: dict, ym: str, assign: dict) -> dict:
    """把該月切片結果記入計數帳本（先自動回滾同月舊分錄）。

    assign: {date|iso: {"person": mid, ...}}。就地修改並回傳，呼叫端負責 save。
    """
    rollback_biopsy(book, ym)
    counts = book.setdefault("counts", {})
    iso_assign: dict = {}
    for d, cell in assign.items():
        iso = d.isoformat() if isinstance(d, date) else str(d)
        mid = cell["person"] if isinstance(cell, dict) else str(cell)
        iso_assign[iso] = mid
        counts[mid] = int(counts.get(mid, 0)) + 1
    book.setdefault("history", []).append({"month": ym, "assign": iso_assign})
    _trim_history(book)
    return book


def last_assigned_before(book: dict, ym: str) -> Optional[str]:
    """ym【之前】最近一次切片的人（跨月輪替決勝用）；無 → None。"""
    best: Optional[tuple] = None
    for entry in (book.get("history") or []):
        if (entry.get("month") or "") >= ym:
            continue
        for iso, mid in (entry.get("assign") or {}).items():
            if best is None or iso > best[0]:
                best = (iso, mid)
    return best[1] if best else None


def format_biopsy_section(assign: dict, notes: list, counts_after: dict,
                          pair, names: dict) -> str:
    """報告用「週六切片」段落（monospace 純字串；空 assign 也給出說明）。"""
    lines = ["[週六切片]（R2/R3 輪排：值班連動優先，否則次數平衡）"]
    for m in pair:
        lines.append(f"  {names.get(m.id, m.id)}（{m.level}）累計 "
                     f"{int(counts_after.get(m.id, 0))} 次")
    for d in sorted(assign):
        cell = assign[d]
        lines.append(f"  {d.month}/{d.day}(六) → "
                     f"{names.get(cell['person'], cell['person'])}"
                     f"（{cell['reason']}）")
    if not assign:
        lines.append("  （本月未排）")
    for n in notes:
        lines.append(f"  ⚠ {n}")
    return "\n".join(lines)
