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
from datetime import date, timedelta
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
                           last_person: Optional[str] = None,
                           overrides: Optional[dict] = None) -> tuple:
    """排該月每個週六的切片 → (assign, notes)。決定性（同輸入同輸出）。

    duty:   {date: member_id} 該月值班（只看週六的鍵）
    leaves: {member_id: set[date]} 請假
    counts: {member_id: int} 累計切片次數（【不含】本月 —— 呼叫端先回滾本月）
    last_person: 本月之前最近一次切片的人（跨月「同數輪替」決勝；None 可）
    overrides: {date: member_id} [2026-07-27 使用者] 手動右鍵指定的切片人選——
        最高優先，蓋過值班連動與次數平衡；名單外代號忽略並附註；當日請假仍照排
        （與鎖定格同語意：使用者明確指定為準，只附註提醒）。指定者照樣累計次數，
        後續週六的次數平衡會把它算進去。
        ★值為空字串 "" ＝ 這個週六【不切片】★(2026-08-20 使用者:不是每個
        週六早上都要切片)——該週不排人、不累計任何人的次數、也不影響輪替
        (run/last 都不動,之後的次數平衡就當這週不存在)。

    assign: {date: {"person": mid,
                    "reason": "手動指定"|"值班連動"|"次數平衡"}}
    notes:  人話清單（缺級、兩人皆請假等）
    """
    pair, notes = biopsy_pair(members)
    if not pair:
        return {}, notes
    pair_ids = [m.id for m in pair]
    run = {mid: int(counts.get(mid, 0)) for mid in pair_ids}
    last = last_person if last_person in pair_ids else None
    ov_map = dict(overrides or {})
    assign: dict = {}
    for sat in month_saturdays(year, month):
        duty_p = duty.get(sat)
        on_leave = {mid for mid in pair_ids
                    if sat in (leaves.get(mid) or set())}
        ov = ov_map.get(sat)
        if ov == "":                          # ★手動指定:這個週六不切片★
            notes.append(f"{sat.month}/{sat.day}(六) 手動指定不切片")
            continue
        if ov is not None and ov not in pair_ids:
            notes.append(f"{sat.month}/{sat.day}(六) 手動指定的切片人選 "
                         f"'{ov}' 不是本月 R2/R3 → 忽略，改自動排")
            ov = None
        if ov is not None:                    # ★手動指定最高優先
            if ov in on_leave:
                notes.append(f"{sat.month}/{sat.day}(六) 手動指定切片 {ov} "
                             f"當日請假，仍照指定排入——請確認或改回自動")
            pick, reason = ov, "手動指定"
            assign[sat] = {"person": pick, "reason": reason}
            run[pick] += 1
            last = pick
            continue
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
            # 次數平衡：(累計次數, 沒值週五, 是否為上次切片者, 名單序)。
            # ★[2026-07-27 使用者] 週五連動排在【累計次數之後】★
            #   使用者要「切片的人盡量也值週五」，但同一句話裡把它定為【最後條件】、
            #   且另外明確要求「R2/R3 週六切片次數一整年下來一樣」→ 次數平衡不可讓位，
            #   週五連動只在次數平手時決勝。
            #   它取代的是原本的「與上次不同者優先」輪替：輪替本來就只是次數平衡的
            #   弱化版（挑完 run[pick]+=1，下個週六自然換人，全距仍 ≤1），
            #   讓位給一個使用者明講的需求是划算的；輪替仍留在後面當次要決勝。
            #   週六值班若是 R2/R3 本來就走「值班連動」不到這裡；這條專門處理
            #   「週六值班是 R1（或兩人都沒值）」時該挑誰。
            fri_person = duty.get(sat - timedelta(days=1))
            pick = min(cands, key=lambda mid: (run[mid], mid != fri_person,
                                               mid == last,
                                               pair_ids.index(mid)))
            reason = "次數平衡"
            # 只有在「真的有得選」而且週五連動確實是決勝因素時才這樣標，
            # 否則報告會宣稱一件程式並不確知的事。
            tied = [mid for mid in cands if run[mid] == run[pick]]
            if len(tied) > 1 and pick == fri_person:
                reason = "次數平衡·週五連動"
        assign[sat] = {"person": pick, "reason": reason}
        run[pick] += 1
        last = pick
    return assign, notes


# ─── 計數帳本（biopsy.json；仿 ledger 的回滾語意）────────────────────────────
def can_rollback(book: dict, ym: str,
                 keep_months: int = HISTORY_KEEP_MONTHS) -> bool:
    """該月的切片分錄還在不在（或根本沒有過）。推導規則與 ledger.can_rollback 相同
    （見那裡的說明：不可存水位線，否則對既有的 biopsy.json 完全無效）。"""
    hist = book.get("history") or []
    months = {e.get("month") for e in hist if e.get("month")}
    months.discard(None)
    if ym in months:
        return True
    if len(months) < keep_months:
        return True
    return ym > min(months)


def _trim_history(book: dict, keep_months: int = HISTORY_KEEP_MONTHS) -> None:
    """修剪過舊的分錄（[OPT-4] 限制檔案大小）。

    ★[2026-08-02 補審] 被修剪掉的月份必須留下水位線★
    修剪本身沒問題，但它讓「重算同一個月」從冪等變成【會重複計入】：
    `rollback_*` 找不到舊分錄 → 回滾不到任何東西 → 接著又加一次 delta，
    累計次數就這樣悄悄翻倍（實測：2024-01 讓 某人 +1 次，隔 30 個月後回頭重算，
    變成 +2 次）。而累計次數正是R2/R3 全年次數平均的基準，錯了之後每一個月都跟著錯。
    原註解只寫「舊分錄不再被回滾」，低估了後果——不是「無法復原」，是「會算錯」。

    故：記下【被丟掉的最新月份】當水位線，settle 時據此擋下（見 settle_* 的說明）。
    """
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

    ★[2026-08-02 補審] 分錄已被修剪的月份一律拒絕結算★（同 ledger.settle_month）
    切片累計次數就是「R2/R3 全年下來次數要一樣」的依據，重複計入會讓某人永遠
    被判定成「已經切很多次」而輪不到。
    """
    if not can_rollback(book, ym):
        raise ValueError(
            f"{ym} 比切片帳本保留的最舊月份還早（上限 {HISTORY_KEEP_MONTHS} 個月），"
            f"無法確認它的舊分錄是否已被修剪掉——若已被修剪，重記會在已計入的次數上"
            f"再加一次。如確實需要調整該月，請直接修改 biopsy.json 的 counts。")
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
