"""Monthly half-day leave for Clerk, external and family trainees.

[2026-11-10 使用者] Clerk 也可以請半天假(原本只有整天)。"""
from datetime import date

SESSIONS = ("上午", "下午")
SCOPES = ("clerk", "external", "family")


def parse_session_leaves(ym, month):
    block = month.get("session_leaves", {})
    if not isinstance(block, dict):
        raise ValueError("時段請假必須是物件")
    out = {}
    for scope, members in block.items():
        if scope not in SCOPES or not isinstance(members, dict):
            raise ValueError("時段請假身分或人員格式錯誤")
        out[scope] = {}
        for p, values in members.items():
            if not isinstance(p, str) or not isinstance(values, list):
                raise ValueError("時段請假人員或清單格式錯誤")
            slots = set()
            for value in values:
                if not isinstance(value, str) or value.count("|") != 1:
                    raise ValueError("時段請假格式錯誤")
                iso, session = value.split("|")
                d = date.fromisoformat(iso)
                if d.isoformat() != iso or iso[:7] != ym or session not in SESSIONS:
                    raise ValueError("時段請假日期或早下午錯誤")
                slots.add((d, session))
            out[scope][p] = slots
    return out


def on_leave(inp, scope, person, d, session):
    return (d in inp.leaves.get(scope, {}).get(person, set())
            or (d, session) in inp.session_leaves.get(scope, {}).get(person, set()))


def clerk_leave_days(inp) -> dict:
    """Clerk 有請假(整天或半天)的日子 {代號: {日期}}。

    ★只用在「全勤/可排天數」這類以【天】為單位的判斷★(半天假也算沒全勤,
    偏保守);誰能排進某一格要用 `on_leave` 逐半天判斷。"""
    out = {str(p): set(ds) for p, ds in (inp.leaves.get("clerk") or {}).items()}
    for p, slots in (inp.session_leaves.get("clerk") or {}).items():
        out.setdefault(str(p), set()).update(d for d, _s in slots)
    return out


def monthly_person_slots(ym, month, scope, person):
    slots = set(parse_session_leaves(ym, month).get(scope, {}).get(person, set()))
    for iso in ((month.get("leaves") or {}).get(scope) or {}).get(person) or []:
        d = date.fromisoformat(iso)
        slots.update((d, s) for s in SESSIONS)
    return slots
