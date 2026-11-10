"""人員固定(held):某人在某天某半天的位置固定不動,同一格的其他人照常排。

[2026-11-10 使用者] 「只排未鎖定人員」與「局部重排(請假)」兩個按鈕共用這一層:
兩者差別只在【哪些人×半天】被固定,求解器看到的都是同一種輸入。

    held        {iso: {時段: {格: [代號]}}}   被固定的人在那一格的實際位置
    held_people {iso: {時段: [代號]}}         被固定的人(含那一格沒有位置的人)

與整格鎖定(`locked`)的關係:
  * 整格鎖定 = 那一格全部的人都固定;它優先,同一格同時出現在兩邊時以鎖定為準。
  * 被固定的人★不可以★被任何階段移動、刪除,也不可以被排到別的位置;
    其餘的人在扣掉他們占用的名額之後照常排。
  * 被固定的人照常計入公平計數與配額(與鎖定格的回放同一套規則)。

★所有階段都經由這裡判斷★,不各自解讀兩個欄位 —— 規則只有一份,
`enforce_held` 再於求解結束時逐一核對,任何階段漏改都會在這裡被擋下,
不會讓「固定的人被移動過」的班表流出去。
"""
from __future__ import annotations

from datetime import date as _date

from .model import STUDENT_SESSIONS


def _locked(inp, iso: str, session: str) -> bool:
    return session in ((inp.locked or {}).get(iso) or {})


def held_cell(inp, iso: str, session: str) -> dict:
    """這一格被固定的位置 {格: [代號]};整格鎖定、不排班的日子回空。"""
    if _locked(inp, iso, session) or not _scheduled(inp, iso):
        return {}
    cell = ((getattr(inp, "held", None) or {}).get(iso) or {}).get(session)
    if not isinstance(cell, dict):
        return {}
    return {str(slot): [str(p) for p in (people or [])]
            for slot, people in cell.items() if people}


def held_codes(inp, iso: str, session: str) -> frozenset:
    """這一格被固定的人(有位置的 + 沒有位置的)。"""
    if _locked(inp, iso, session) or not _scheduled(inp, iso):
        return frozenset()
    codes = {p for people in held_cell(inp, iso, session).values() for p in people}
    extra = ((getattr(inp, "held_people", None) or {}).get(iso) or {}).get(session)
    codes.update(str(p) for p in (extra or ()))
    return frozenset(codes)


def is_held(inp, iso: str, session: str, code) -> bool:
    return str(code) in held_codes(inp, iso, session)


def has_held(inp) -> bool:
    return any(held_codes(inp, iso, s)
               for iso, sessions in (getattr(inp, "held", None) or {}).items()
               for s in (sessions or {})) or any(
        held_codes(inp, iso, s)
        for iso, sessions in (getattr(inp, "held_people", None) or {}).items()
        for s in (sessions or {}))


def _scheduled(inp, iso: str) -> bool:
    """這一天本月有排班(在開診格網裡)。後來變成假日/停診的日子整天不排,
    那天的固定紀錄沒有東西可以固定 —— 忽略,不算衝突也不算被移動。"""
    try:
        d = _date.fromisoformat(iso)
    except (TypeError, ValueError):
        return False
    return iso[:7] == inp.ym and d in (inp.grid or {})


def held_keys(inp) -> list:
    """所有有固定人員的 (iso, 時段),排序後回傳(整格鎖定、不排班的日子除外)。"""
    keys = set()
    for table in (getattr(inp, "held", None) or {},
                  getattr(inp, "held_people", None) or {}):
        for iso, sessions in table.items():
            if not _scheduled(inp, iso):
                continue
            for s in (sessions or {}):
                if s in STUDENT_SESSIONS and held_codes(inp, iso, s):
                    keys.add((iso, s))
    return sorted(keys)


def held_violations(inp, slots: dict) -> list:
    """逐格核對:被固定的人是否還在原位置、而且沒有被排到別的位置。"""
    problems = []
    for iso, s in held_keys(inp):
        cell = ((slots or {}).get(iso) or {}).get(s) or {}
        expected = held_cell(inp, iso, s)
        for code in sorted(held_codes(inp, iso, s)):
            # 逐次比對(不是只看在不在):同一格出現兩次也算被改動。
            want = sorted(slot for slot, people in expected.items()
                          for p in people if p == code)
            got = sorted(slot for slot, people in cell.items()
                         for p in (people or ()) if p == code)
            if want != got:
                problems.append(f"{iso} {s} {code}：應為 {want or '無'}，實際 {got or '無'}")
    return problems


def enforce_held(inp, slots: dict) -> None:
    """求解結束的總檢查。不符 = 程式缺陷,寧可不出結果也不交出被改動的固定人員。"""
    problems = held_violations(inp, slots)
    if problems:
        raise RuntimeError("固定人員被排班流程移動（程式缺陷，未產生結果）："
                           + "；".join(problems[:5]))


def held_conflicts(inp) -> list:
    """被固定的人後來請假、而那一格有工作 → 保留原班,列為衝突讓使用者確認
    (使用者定案 2026-11-10)。同一人在同一格出現兩個位置也一併點名。"""
    from .session_leave import on_leave
    from .solve_day import REST, is_follow_slot

    clerks = {str(p) for b in (inp.clerk_batches or []) for p in b.members}
    scopes = (("pgy", {str(p) for p in inp.pgy_roster}),
              ("external", {str(p) for p in inp.external_roster}),
              ("family", {str(p) for p in inp.family_roster}),
              ("clerk", clerks))
    out = []
    for iso, s in held_keys(inp):
        try:
            d = _date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        places: dict = {}
        for slot, people in held_cell(inp, iso, s).items():
            for p in people:
                places.setdefault(p, []).append(slot)
        for p, slots in sorted(places.items()):
            if len(slots) > 1:
                out.append(f"{d.month}/{d.day} {s} 📌固定人員 {p} 同時在 "
                           f"{'、'.join(slots)}——同一時段只能做一件事，請修正")
            work = [x for x in slots if x != REST]
            if not work:
                continue
            rooms = (inp.grid.get(d) or {}).get(s) or []
            closed = [x for x in work if is_follow_slot(x) and x not in rooms]
            if closed:
                out.append(f"{d.month}/{d.day} {s} 📌固定人員 {p} 的診間 "
                           f"{'、'.join(closed)} 本時段未開診，仍保留原班——請確認")
            for scope, people in scopes:
                if p in people and on_leave(inp, scope, p, d, s):
                    out.append(f"{d.month}/{d.day} {s} 📌固定人員 {p} 當天已請假，"
                               f"仍保留原班（{'、'.join(work)}）——請確認")
                    break
    return out


# ─── 兩個按鈕 → 固定範圍(service 用;純函式) ─────────────────────────────
#: 只排未鎖定人員:勾選的人在可排的每一格都固定。
HELD_PEOPLE = "people"
#: 局部重排(請假):請假者請假的那幾個半天全體可動;請假者本人其他日子可動
#:   (只能補進空位 —— 其他人都固定,不會被擠掉);其餘全部固定。
HELD_LOCAL = "local"


def _roster_codes(inp) -> set:
    codes = {str(p) for p in inp.pgy_roster}
    codes |= {str(p) for b in (inp.clerk_batches or []) for p in b.members}
    codes |= {str(p) for p in inp.external_roster}
    codes |= {str(p) for p in inp.family_roster}
    return codes


def _absent_at(inp, code: str, d, session: str) -> bool:
    from .session_leave import on_leave
    return any(on_leave(inp, scope, code, d, session)
               for scope in ("pgy", "clerk", "external", "family"))


def held_from_spec(inp, day_slots: dict, mode: str, codes) -> tuple:
    """(held, held_people):把按鈕的選擇換成求解器的固定範圍。

    只處理【可排】的格(在格網裡、沒有整格鎖定 —— 今天以前已由 RS-32 併入
    鎖定);固定的位置一律取自目前存檔的班表 `day_slots`。"""
    codes = {str(c) for c in (codes or ())}
    everyone = _roster_codes(inp)
    unknown = sorted(codes - everyone)
    if unknown:
        raise ValueError(f"不在本月名單：{'、'.join(unknown)}")
    if not codes:
        raise ValueError("請至少選擇一位人員")
    if mode not in (HELD_PEOPLE, HELD_LOCAL):
        raise ValueError(f"未知的固定方式：{mode}")
    held: dict = {}
    people: dict = {}
    for d in sorted(inp.grid):
        iso = d.isoformat()
        if iso[:7] != inp.ym or d.weekday() >= 5:
            continue
        for s in STUDENT_SESSIONS:
            if _locked(inp, iso, s):
                continue
            if mode == HELD_PEOPLE:
                fixed = set(codes)
            elif any(_absent_at(inp, c, d, s) for c in codes):
                continue                     # 請假的半天:全體可動
            else:
                fixed = everyone - codes     # 請假者本人可動,其他人固定
            cell = ((day_slots or {}).get(iso) or {}).get(s) or {}
            part = {str(slot): [str(p) for p in ps if str(p) in fixed]
                    for slot, ps in cell.items() if isinstance(ps, list)}
            part = {slot: ps for slot, ps in part.items() if ps}
            if part:
                held.setdefault(iso, {})[s] = part
            people.setdefault(iso, {})[s] = sorted(fixed)
    return held, people


def describe_changes(before: dict, after: dict) -> list:
    """逐人逐格的變動 → 人話清單(預覽用)。只列位置真的不同的人。"""
    lines = []
    keys = sorted({(iso, s) for table in (before or {}, after or {})
                   for iso, ss in table.items() if isinstance(ss, dict)
                   for s in ss if s in STUDENT_SESSIONS})
    for iso, s in keys:
        def where(table, iso=iso, s=s):
            cell = ((table or {}).get(iso) or {}).get(s) or {}
            out: dict = {}
            for slot, ps in cell.items():
                for p in (ps or []):
                    out.setdefault(str(p), []).append(str(slot))
            return out
        b, a = where(before), where(after)
        try:
            d = _date.fromisoformat(iso)
            label = f"{d.month}/{d.day}({'一二三四五六日'[d.weekday()]}) {s}"
        except (TypeError, ValueError):
            label = f"{iso} {s}"
        for p in sorted(set(b) | set(a)):
            if sorted(b.get(p, [])) != sorted(a.get(p, [])):
                lines.append(f"{label} {p}：{'、'.join(sorted(b.get(p, []))) or '（無）'}"
                             f" → {'、'.join(sorted(a.get(p, []))) or '（無）'}")
    return lines
