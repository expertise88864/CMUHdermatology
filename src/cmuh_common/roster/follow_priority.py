"""Monthly mandatory family sessions and ordered trainee allocation."""
from collections import Counter
from copy import deepcopy
from calendar import monthrange
from datetime import date, timedelta

from .session_leave import on_leave
from .solve_day import REST, STUDENT_SESSIONS, arbitration_order, day_owner_batch, is_follow_slot


def clerk_month_caps(inp, batch):
    """Reserve a proportional share of a cross-month course for its later days.

    Include future fixed attendance in the cap because the greedy solver also
    includes those seats in its running level. Holidays and known leave are not
    future opportunities. Ended courses retain the ordinary 11-session ceiling.
    """
    from .model import CLERK_COURSE_DAYS
    from .solve_day import CLERK_SEAT_TARGET_MAX
    out = {}
    for p in batch.members:
        days = [batch.start_monday + timedelta(days=i) for i in range(CLERK_COURSE_DAYS)]
        days = [d for d in days if d.weekday() < 5 and d not in inp.holidays
                and d not in inp.leaves.get("clerk", {}).get(p, set())
                and (not inp.course_clinic_days or d.isoformat() in inp.course_clinic_days)
                and day_owner_batch(arbitration_order(inp), d) == batch]
        future = [d for d in days if d.isoformat()[:7] > inp.ym]
        if not future:
            continue
        fixed = sum(p in (ps or []) for d in future
                    for cells in (inp.course_fixed.get(d.isoformat()) or {}).values()
                    if isinstance(cells, dict) for r, ps in cells.items() if is_follow_slot(r))
        elapsed = len(days) - len(future)
        out[p] = min(CLERK_SEAT_TARGET_MAX,
                     (CLERK_SEAT_TARGET_MAX * elapsed + len(days) // 2) // len(days) + fixed)
    return out


def refresh_clerk_warnings(inp, slots, warnings):
    """Report final joint-solver attendance, not the preliminary Clerk plan."""
    from .solve_day import (
        clerk_seat_band_warnings, clerk_seat_uneven_warnings,
        clerk_batches_ended_by, clerk_full_attendance,
    )
    prefixes = ("跟診時段偏少（梯次", "跟診時段偏多（梯次", "跟診次數相差超過 ")
    warnings[:] = [w for w in warnings if not w.startswith(prefixes)]
    counts = Counter()
    order = arbitration_order(inp)
    solved = {b.id for d in inp.grid if d.isoformat()[:7] == inp.ym and d.weekday() < 5
              if (b := day_owner_batch(order, d))}
    sources = {iso: ss for iso, ss in inp.prior_sessions.items() if iso[:7] < inp.ym}
    sources.update(slots)
    for iso, sessions in sources.items():
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        b = day_owner_batch(order, d)
        if not b or d.weekday() >= 5 or iso[:7] > inp.ym:
            continue
        if iso[:7] == inp.ym and d not in inp.grid:
            continue
        excluded = inp.pgy_roster if iso[:7] == inp.ym else inp.prior_pgy
        if not isinstance(sessions, dict):
            continue
        for s, cells in sessions.items():
            if not isinstance(cells, dict):
                continue
            if s not in STUDENT_SESSIONS:
                continue
            for r, people in cells.items():
                if not is_follow_slot(r) or (iso[:7] == inp.ym and r not in inp.grid[d].get(s, [])):
                    continue
                for p in (people or []):
                    if p in b.members and p not in excluded:
                        counts[b.id, p] += 1
    y, m = map(int, inp.ym.split("-"))
    ended = clerk_batches_ended_by(inp.clerk_batches, date(y, m, monthrange(y, m)[1]))
    warnings.extend(clerk_seat_band_warnings(inp.clerk_batches, counts, only_ids=solved, ended_ids=ended))
    warnings.extend(clerk_seat_uneven_warnings(
        inp.clerk_batches, counts, only_ids=solved & ended,
        base_ids={(b.id, p) for b in inp.clerk_batches
                  for p in clerk_full_attendance(inp, b, inp.leaves.get("clerk", {}))}))


def family_requirement_warnings(inp, slots):
    out = [f"家醫科 {p} 已不在當月名單，仍有指定跟診設定，請確認名單"
           for p, required in inp.family_follow.items() if required and p not in inp.family_roster]
    for iso, sessions in slots.items():
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        if not isinstance(sessions, dict):
            continue
        for s, cells in sessions.items():
            if not isinstance(cells, dict):
                continue
            for p in set(inp.family_roster) & {p for ps in cells.values() for p in (ps or [])}:
                if (d, s) not in inp.family_follow.get(p, set()):
                    out.append(f"家醫科 {p} {d:%m/%d} {s} 未指定此時段，卻有排班；請確認指定跟診設定")
    for p in inp.family_roster:
        for d, s in sorted(inp.family_follow.get(p, set())):
            sessions = slots.get(d.isoformat())
            cells = sessions.get(s) if isinstance(sessions, dict) else None
            cells = cells if isinstance(cells, dict) else {}
            rooms = inp.grid.get(d, {}).get(s, [])
            if on_leave(inp, "family", p, d, s):
                reason = "與請假衝突"
            elif d.weekday() >= 5 or not rooms:
                reason = "無開放診間"
            elif any(p in (cells.get(r) or []) for r in rooms):
                continue
            elif s in inp.locked.get(d.isoformat(), {}):
                reason = "鎖定內容未安排跟診"
            else:
                reason = "未排到跟診（請檢查診間容量）"
            out.append(f"家醫科 {p} {d:%m/%d} {s} 指定跟診：{reason}")
    return out


def prepare_priority(inp, slots):
    """Remove movable follow/rest for Clerk-first joint allocation.

    Existing Clerk attendance is the course budget. This retains the original
    cross-month quota calculation. External trainees use the remaining seats
    after Clerk goals. Special duties and entire locks are immutable.
    """
    occupied = set(inp.pgy_roster) | set(inp.external_roster)
    occupied.update(p for b in inp.clerk_batches for p in b.members)
    if occupied & set(inp.family_roster):
        raise ValueError("家醫科與 PGY／Clerk／外訓代號重複，請先修正名單")
    targets = Counter()
    originals = {}
    order = arbitration_order(inp)
    for d in sorted(inp.grid):
        if d.isoformat()[:7] != inp.ym or d.weekday() >= 5:
            continue
        owner = day_owner_batch(order, d)
        for s in STUDENT_SESSIONS:
            cells = slots.setdefault(d.isoformat(), {}).setdefault(s, {})
            if s in inp.locked.get(d.isoformat(), {}):
                continue
            originals[d, s] = deepcopy(cells)
            for r in list(cells):
                if is_follow_slot(r) or r == REST:
                    if is_follow_slot(r) and owner:
                        for p in cells[r]:
                            if p in owner.members and p not in inp.pgy_roster:
                                targets[owner.id, p] += 1
                    del cells[r]
    return targets, originals


def restore_pgy_and_rest(inp, slots, originals):
    counts = Counter(p for ss in slots.values() if isinstance(ss, dict)
                     for cells in ss.values() if isinstance(cells, dict)
                     for r, ps in cells.items() if is_follow_slot(r) for p in (ps or []))
    order = arbitration_order(inp)
    for (d, s), before in sorted(originals.items()):
        cells = slots[d.isoformat()][s]
        assigned = {p for ps in cells.values() for p in (ps or [])}
        available = [p for p in inp.pgy_roster if p not in assigned
                     and d not in inp.leaves.get("pgy", {}).get(p, set())]
        original_follow = {p for r, ps in before.items() if is_follow_slot(r) for p in ps}
        for r in inp.grid[d].get(s, []):
            while available and len(cells.get(r, [])) < inp.capacity:
                p = min(available, key=lambda p: (
                    counts[p], not (p in inp.apply_pref and r == "101" and d.weekday() in (1, 4)),
                    p not in original_follow, p))
                cells.setdefault(r, []).append(p)
                available.remove(p)
                counts[p] += 1
        owner = day_owner_batch(order, d)
        scopes = [("pgy", inp.pgy_roster), ("external", inp.external_roster),
                  ("clerk", owner.members if owner else [])]
        assigned = {p for ps in cells.values() for p in (ps or [])}
        for scope, people in scopes:
            for p in people:
                if p not in assigned and not on_leave(inp, scope, p, d, s):
                    cells.setdefault(REST, []).append(p)
                    assigned.add(p)


def spread_clerk_days(inp, slots):
    """Move a second daily follow to an idle day in the same course.

    Earlier Clerk priority must not consume an entire course budget in its first
    week. Each move decreases idle Clerk days; it never changes course totals,
    special duties, family/external attendance, leave or locked sessions.
    """
    order = arbitration_order(inp)
    days = [d for d in sorted(inp.grid) if d.isoformat()[:7] == inp.ym and d.weekday() < 5]
    def works(d, p):
        return sum(p in ps for s, cells in slots.get(d.isoformat(), {}).items()
                   if s in STUDENT_SESSIONS for r, ps in cells.items() if r != REST)
    for d in sorted(days, key=lambda d: (not inp.grid[d].get("下午"), d)):
        owner = day_owner_batch(order, d)
        if not owner or not any(inp.grid[d].get(s) for s in STUDENT_SESSIONS):
            continue
        for p in sorted(set(owner.members) - set(inp.pgy_roster)):
            if works(d, p) or d in inp.leaves.get("clerk", {}).get(p, set()):
                continue
            donors = [(dd, s, r) for dd in days
                      if day_owner_batch(order, dd) == owner and works(dd, p) > 1
                      for s, cells in slots.get(dd.isoformat(), {}).items()
                      if s in STUDENT_SESSIONS and s not in inp.locked.get(dd.isoformat(), {})
                      for r, ps in cells.items() if is_follow_slot(r) and p in ps]
            if not donors:
                continue
            placed = False
            for s in STUDENT_SESSIONS:
                if s in inp.locked.get(d.isoformat(), {}):
                    continue
                cells = slots.get(d.isoformat(), {}).get(s, {})
                for r in inp.grid[d].get(s, []):
                    displaced = None
                    if len(cells.get(r, [])) >= inp.capacity:
                        candidates = [q for q in cells.get(r, []) if q in inp.pgy_roster]
                        displaced = max(candidates, key=lambda q: works(d, q), default=None)
                        if displaced is None:
                            continue
                    dd, ss, rr = donors[0]
                    source = slots[dd.isoformat()][ss]
                    source[rr].remove(p)
                    if not source[rr]:
                        del source[rr]
                    source.setdefault(REST, []).append(p)
                    if p in cells.get(REST, []):
                        cells[REST].remove(p)
                        if not cells[REST]:
                            del cells[REST]
                    if displaced:
                        cells[r].remove(displaced)
                        cells.setdefault(REST, []).append(displaced)
                    cells.setdefault(r, []).append(p)
                    # Fill the released seat using an available PGY only.
                    candidates = [q for q in source.get(REST, []) if q in inp.pgy_roster
                                  and dd not in inp.leaves.get("pgy", {}).get(q, set())]
                    if candidates:
                        q = displaced if displaced in candidates else candidates[0]
                        source[REST].remove(q)
                        source.setdefault(rr, []).append(q)
                    placed = True
                    break
                if placed:
                    break
