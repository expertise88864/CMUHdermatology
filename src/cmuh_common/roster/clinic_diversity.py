"""Course-local doctor diversity, subordinate to attendance and special duties."""
from collections import Counter
from datetime import date

from .solve_day import (
    REST, STUDENT_SESSIONS, TWO_PGY_PHOTO_ONLY,
    arbitration_order, day_owner_batch, is_follow_slot,
)


def balance_clinics(inp, slots):
    """Improve distinct doctors, then repeated-doctor balance, then room balance.

    Same-session moves/swaps preserve attendance exactly. A morning/afternoon
    move is allowed only within the same day, through existing REST positions,
    and only for a strict doctor improvement. This preserves individual daily,
    weekly and course counts, all special duties, and family requirements.
    The lexicographically decreasing integer potential ensures termination.
    """
    order = arbitration_order(inp)

    def key(d, p):
        for scope, people in (("pgy", inp.pgy_roster), ("family", inp.family_roster),
                              ("external", inp.external_roster)):
            if p in people:
                return scope, inp.ym, p
        batch = day_owner_batch(order, d)
        return ("clerk", batch.id, p) if batch and p in batch.members else None

    def doctor(d, s, r):
        return inp.clinic_doctors.get(d, {}).get(s, {}).get(r, "")

    def pinned(d, r, p):
        return p in inp.apply_pref and r == "101" and d.weekday() in (1, 4)

    def can_change_session(d, s, t, p):
        if p in inp.family_roster:
            return False
        if p in inp.pgy_roster and len(set(inp.pgy_roster)) == 2:
            return ((d.weekday(), s) in TWO_PGY_PHOTO_ONLY) == ((d.weekday(), t) in TWO_PGY_PHOTO_ONLY)
        return True

    rooms_count, doctors_count = Counter(), Counter()
    sources = {iso: ss for iso, ss in inp.prior_sessions.items() if iso[:7] < inp.ym}
    sources.update({iso: ss for iso, ss in inp.course_fixed.items() if iso[:7] > inp.ym})
    sources.update({iso: ss for iso, ss in slots.items() if iso[:7] == inp.ym})
    for iso, sessions in sorted(sources.items()):
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if d.weekday() >= 5 or (iso[:7] == inp.ym and d not in inp.grid):
            continue
        if not isinstance(sessions, dict):
            continue
        for s, cells in sessions.items():
            if s not in STUDENT_SESSIONS or not isinstance(cells, dict):
                continue
            for r, people in cells.items():
                if not is_follow_slot(r) or (iso[:7] == inp.ym and r not in inp.grid[d].get(s, [])):
                    continue
                for p in people or []:
                    k = key(d, p)
                    if k is None or (iso[:7] != inp.ym and k[0] != "clerk"):
                        continue
                    if iso[:7] < inp.ym and p in inp.prior_pgy:
                        continue
                    rooms_count[k, r] += 1
                    if (name := doctor(d, s, r)):
                        doctors_count[k, name] += 1

    def delta(counter, changes):
        return sum(2 * counter[k] * n + n * n for k, n in changes.items())

    while True:
        best, best_score = None, (0, 0, 0)
        for d in sorted(inp.grid):
            iso = d.isoformat()
            if iso[:7] != inp.ym or d.weekday() >= 5:
                continue
            for s in STUDENT_SESSIONS:
                if s in inp.locked.get(iso, {}):
                    continue
                source = slots.get(iso, {}).get(s, {})
                for r in sorted(set(inp.grid[d].get(s, []))):
                    for p in sorted(source.get(r, [])):
                        kp = key(d, p)
                        if kp is None or pinned(d, r, p):
                            continue
                        for t in (s,) + tuple(ss for ss in STUDENT_SESSIONS if ss != s):
                            if t in inp.locked.get(iso, {}):
                                continue
                            dest = slots.get(iso, {}).get(t, {})
                            cross = s != t
                            if cross and (not can_change_session(d, s, t, p) or p not in dest.get(REST, [])):
                                continue
                            for target in sorted(set(inp.grid[d].get(t, []))):
                                if not cross and r == target:
                                    continue
                                a, b = doctor(d, s, r), doctor(d, t, target)
                                if (a and not b) or (cross and a == b):
                                    continue  # Do not reward replacing a known doctor with an unknown one.
                                others = list(sorted(dest.get(target, [])))
                                if len(others) < inp.capacity:
                                    others.append(None)
                                for q in others:
                                    if q is not None and b and not a:
                                        continue  # A swap must not send the other person to an unknown doctor.
                                    kq = key(d, q) if q is not None else None
                                    if q == p or (q is not None and (kq is None or pinned(d, target, q))):
                                        continue
                                    if cross and q is not None and (not can_change_session(d, t, s, q) or q not in source.get(REST, [])):
                                        continue
                                    rc, dc = Counter(), Counter()
                                    for k, old_r, new_r, old_doc, new_doc in (
                                            (kp, r, target, a, b), (kq, target, r, b, a)):
                                        if k is None:
                                            continue
                                        rc[k, old_r] -= 1
                                        rc[k, new_r] += 1
                                        if old_doc:
                                            dc[k, old_doc] -= 1
                                        if new_doc:
                                            dc[k, new_doc] += 1
                                    unique = Counter()
                                    for (k, name), n in dc.items():
                                        unique[k] += int(doctors_count[k, name] + n > 0) - int(doctors_count[k, name] > 0)
                                    if any(n < 0 for n in unique.values()):
                                        continue  # No person's distinct-doctor coverage is sacrificed.
                                    score = (-sum(unique.values()), delta(doctors_count, dc), delta(rooms_count, rc))
                                    if cross and score[:2] >= (0, 0):
                                        continue
                                    if score < best_score:
                                        best_score = score
                                        best = source, dest, r, target, p, q, cross, rc, dc
        if best is None:
            return
        source, dest, r, target, p, q, cross, rc, dc = best
        source[r].remove(p)
        dest.setdefault(target, []).append(p)
        if cross:
            dest[REST].remove(p)
            source.setdefault(REST, []).append(p)
        if q is not None:
            dest[target].remove(q)
            source.setdefault(r, []).append(q)
            if cross:
                source[REST].remove(q)
                dest.setdefault(REST, []).append(q)
        for cells in (source, dest):
            for room in list(cells):
                if not cells[room]:
                    del cells[room]
        rooms_count.update(rc)
        doctors_count.update(dc)
