"""Monthly external trainees and course-local clinic diversity.

Existing PGY/Clerk attendance, duties and locked sessions are preserved. Room
balancing only moves existing follow assignments between open rooms.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from calendar import monthrange
from math import ceil

from .solve_day import (
    BIOPSY, REST, STUDENT_SESSIONS, arbitration_order, day_owner_batch,
    is_follow_slot,
)


def external_biopsy_open(inp, d, session):
    if d not in inp.grid or d.weekday() >= 5 or (d.weekday() == 2 and session == "下午"):
        return False
    owner = day_owner_batch(arbitration_order(inp), d)
    # With a Clerk course, share its explicitly maintained biopsy opening grid.
    # Without Clerks, external trainees can use weekday biopsy sessions.
    return owner is None or bool(
        inp.biopsy_open.get(owner.id, {}).get(d.isoformat(), {}).get(session))


def external_roster(inp):
    occupied = set(inp.pgy_roster)
    occupied.update(c for b in inp.clerk_batches for c in b.members)
    return sorted(set(inp.external_roster) - occupied)


def _weeks(inp):
    y, m = map(int, inp.ym.split("-"))
    weeks = defaultdict(list)
    for n in range(1, monthrange(y, m)[1] + 1):
        d = date(y, m, n)
        if d.weekday() < 5:
            weeks[d.isocalendar()[:2]].append(d)
    return weeks


def external_quota_warnings(inp, slots):
    out = []
    for p in external_roster(inp):
        for days in _weeks(inp).values():
            count = sum(p in people for d in days
                        for cells in slots.get(d.isoformat(), {}).values()
                        for room, people in cells.items() if is_follow_slot(room))
            low, high = ceil(len(days) * 4 / 5), len(days)
            if not low <= count <= high:
                out.append(f"外訓 {p} {days[0]:%m/%d}～{days[-1]:%m/%d} 跟診 {count} 次"
                           f"（目標 {low}–{high}；請確認請假、鎖定與診間容量）")
        for half in (0, 1):
            count = sum(p in cells.get(BIOPSY, []) for iso, sessions in slots.items()
                        if iso[:7] == inp.ym and (int(iso[8:]) > 14) == bool(half)
                        for cells in sessions.values())
            if count != 1:
                out.append(f"外訓 {p} {'1–14 日' if half == 0 else '15 日至月底'}"
                           f" 切片室 {count} 次（目標 1 次）")
    return out


def add_external(inp, slots, log, warnings):
    people = external_roster(inp)
    conflicts = sorted(set(inp.external_roster) - set(people))
    if conflicts:
        warnings.append(f"外訓代號與 PGY/Clerk 重複：{'、'.join(conflicts)}；未重複排班")
    if not people:
        return
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    choices = {}
    objective = []
    leaves = inp.leaves.get("external", {})
    for d in sorted(inp.grid):
        if d.isoformat()[:7] != inp.ym or d.weekday() >= 5:
            continue
        iso = d.isoformat()
        for s in STUDENT_SESSIONS:
            if s in inp.locked.get(iso, {}):
                continue
            cells = slots.setdefault(iso, {}).setdefault(s, {})
            rooms = list(dict.fromkeys(inp.grid[d].get(s, [])))
            spare = sum(max(0, inp.capacity - len(cells.get(r, []))) for r in rooms)
            for p in people:
                if d in leaves.get(p, set()):
                    continue
                pair = []
                for kind, available in (("follow", spare > 0),
                                        ("biopsy", external_biopsy_open(inp, d, s)
                                         and not cells.get(BIOPSY))):
                    if available:
                        v = model.new_bool_var(f"{iso}/{s}/{p}/{kind}")
                        choices[d, s, p, kind] = v
                        pair.append(v)
                model.add(sum(pair) <= 1)
            model.add(sum(v for (dd, ss, _, k), v in choices.items()
                          if dd == d and ss == s and k == "follow") <= spare)
            model.add(sum(v for (dd, ss, _, k), v in choices.items()
                          if dd == d and ss == s and k == "biopsy") <= 1)
    for p in people:
        for days in _weeks(inp).values():
            fixed = sum(p in ps for d in days
                        for cells in inp.locked.get(d.isoformat(), {}).values()
                        for r, ps in cells.items() if is_follow_slot(r))
            new = sum(v for (d, _, pp, k), v in choices.items()
                      if pp == p and k == "follow" and d in days)
            model.add(new <= max(0, len(days) - fixed))
            deficit = model.new_int_var(0, 5, "weekly_shortfall")
            model.add(deficit >= ceil(len(days) * 4 / 5) - fixed - new)
            objective.extend([1000 * deficit, -new])
        for half in (0, 1):
            fixed = sum(p in cells.get(BIOPSY, [])
                        for iso, sessions in inp.locked.items()
                        if iso[:7] == inp.ym and (int(iso[8:]) > 14) == bool(half)
                        for cells in sessions.values())
            new = sum(v for (d, _, pp, k), v in choices.items()
                      if pp == p and k == "biopsy" and (d.day > 14) == bool(half))
            model.add(new <= max(0, 1 - fixed))
            objective.append(-10000 * new)
    model.minimize(sum(objective))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    solver.parameters.max_deterministic_time = 2
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        warnings.append("外訓排班未取得可行解；請檢查開診與鎖定設定")
        return
    for (d, s, p, kind), v in choices.items():
        if not solver.value(v):
            continue
        cells = slots[d.isoformat()][s]
        room = BIOPSY if kind == "biopsy" else next(
            r for r in inp.grid[d][s] if len(cells.get(r, [])) < inp.capacity)
        cells.setdefault(room, []).append(p)
    for d in sorted(inp.grid):
        if d.isoformat()[:7] != inp.ym or d.weekday() >= 5:
            continue
        for s in STUDENT_SESSIONS:
            if s in inp.locked.get(d.isoformat(), {}):
                continue
            cells = slots[d.isoformat()][s]
            assigned = {p for ps in cells.values() for p in ps}
            rest = [p for p in people if p not in assigned and d not in leaves.get(p, set())]
            if rest:
                cells.setdefault(REST, []).extend(rest)
    warnings.extend(external_quota_warnings(inp, slots))
    log.append("外訓：每週 4–5 跟診（月界按比例）；1–14 日、15 日至月底各一次切片室")


def balance_rooms(inp, slots):
    """Strictly reduce per-person/course squared room counts by moves/swaps.

    Counts include prior Clerk sessions and future fixed sessions. A strictly
    decreasing integer potential gives deterministic termination. Attendance and
    every special station stay unchanged; locked sessions are never candidates.
    """
    order = arbitration_order(inp)
    def key(d, p):
        if p in inp.pgy_roster:
            return ("pgy", inp.ym, p)
        if p in inp.external_roster:
            return ("external", inp.ym, p)
        b = day_owner_batch(order, d)
        return ("clerk", b.id, p) if b and p in b.members else None

    counts = Counter()
    sources = {iso: sessions for iso, sessions in inp.prior_sessions.items()
               if iso[:7] < inp.ym}
    sources.update({iso: sessions for iso, sessions in inp.course_fixed.items()
                    if iso[:7] > inp.ym})
    sources.update({iso: sessions for iso, sessions in slots.items() if iso[:7] == inp.ym})
    for iso, sessions in sorted(sources.items()):
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if iso[:7] == inp.ym and (d not in inp.grid or d.weekday() >= 5):
            continue  # RF-02: retain grid-external locks, but never count them.
        for session, cells in sessions.items():
            if session not in STUDENT_SESSIONS:
                continue
            for r, ps in cells.items():
                if is_follow_slot(r):
                    if iso[:7] == inp.ym and r not in inp.grid[d].get(session, []):
                        continue
                    for p in ps:
                        k = key(d, p)
                        if k and (iso[:7] == inp.ym or k[0] == "clerk"):
                            counts[k, r] += 1
    while True:
        best = None
        best_delta = 0
        for d in sorted(inp.grid):
            iso = d.isoformat()
            if iso[:7] != inp.ym:
                continue
            for s in STUDENT_SESSIONS:
                if s in inp.locked.get(iso, {}):
                    continue
                cells = slots.get(iso, {}).get(s, {})
                rooms = sorted(set(inp.grid[d].get(s, [])))
                for r in rooms:
                    for p in sorted(cells.get(r, [])):
                        k = key(d, p)
                        if k is None:
                            continue
                        # Preserve existing Apply 本科 seats on preferred days.
                        if p in inp.apply_pref and r == "101" and d.weekday() in (1, 4):
                            continue
                        for target in rooms:
                            if target == r:
                                continue
                            others = list(sorted(cells.get(target, [])))
                            if len(others) < inp.capacity:
                                others.append(None)
                            for q in others:
                                kq = key(d, q) if q is not None else None
                                if q is not None and (kq is None or
                                        (q in inp.apply_pref and target == "101" and d.weekday() in (1, 4))):
                                    continue
                                delta = 2 * (counts[k, target] - counts[k, r] + 1)
                                if kq:
                                    delta += 2 * (counts[kq, r] - counts[kq, target] + 1)
                                if delta < best_delta:
                                    best_delta = delta
                                    best = cells, r, target, p, q, k, kq
        if best is None:
            return
        cells, r, target, p, q, k, kq = best
        cells[r].remove(p)
        cells.setdefault(target, []).append(p)
        counts[k, r] -= 1
        counts[k, target] += 1
        if q is not None:
            cells[target].remove(q)
            cells[r].append(q)
            counts[kq, target] -= 1
            counts[kq, r] += 1
        for room in (r, target):
            if not cells[room]:
                del cells[room]


def finish_courses(inp, slots, log, warnings):
    slots, log, warnings = deepcopy(slots), list(log), list(warnings)
    add_external(inp, slots, log, warnings)
    balance_rooms(inp, slots)
    log.append("依 course 平衡各診間跟診次數；保留每時段出勤、特殊站別及鎖定內容")
    return slots, log, warnings
