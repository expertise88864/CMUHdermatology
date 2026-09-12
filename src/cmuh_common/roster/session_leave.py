"""Monthly half-day leave for external and family trainees."""
from datetime import date

SESSIONS = ("上午", "下午")
SCOPES = ("external", "family")


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


def monthly_person_slots(ym, month, scope, person):
    slots = set(parse_session_leaves(ym, month).get(scope, {}).get(person, set()))
    for iso in ((month.get("leaves") or {}).get(scope) or {}).get(person) or []:
        d = date.fromisoformat(iso)
        slots.update((d, s) for s in SESSIONS)
    return slots
