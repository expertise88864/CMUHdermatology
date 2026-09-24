"""Reassign existing PGY seats without losing coverage or weekly clinic minima."""
from collections import Counter, defaultdict
from datetime import date

from .pgy_workload import availability_weights, workload_goals
from .session_leave import on_leave
from .solve_day import PHOTO, REST, TREATMENT, TWO_PGY_PHOTO_ONLY, is_follow_slot


def _manual_target(counts, people, offsets, p, kind):
    shifts = {q: offsets.get(q, 0) for q in people}
    pool = sum(counts[q, kind] - shifts[q] for q in people)
    floor = pool // len(people)
    peers = [counts[q, kind] - shifts[q] for q in people
             if q != p and counts[q, kind] - shifts[q] >= floor]
    return max(0, min(peers, default=floor) + shifts[p])


def _reduce_manual_pgy_follow(inp, slots, people, log):
    """Leave only surplus, unlocked PGY clinic seats empty for manual cuts.

    Necessary duties and the solver's achieved weekly two-clinic credit stay
    intact. This runs after the existing category balance, so it cannot take a
    clinic seat away from a Clerk, family physician, or external trainee.
    """
    offsets = inp.pgy_photo_offsets
    if len(people) < 2 or len({offsets.get(p, 0) for p in people}) == 1:
        return
    dropped_weeks = Counter()
    for p in people:
        if offsets.get(p, 0) >= 0:
            continue
        while True:
            counts, weekly, daily, doctors = Counter(), Counter(), Counter(), Counter()
            candidates = []
            for iso, sessions in slots.items():
                if iso[:7] != inp.ym:
                    continue
                try:
                    d = date.fromisoformat(iso)
                except (TypeError, ValueError):
                    continue
                week = d.isocalendar()[:2]
                for session, cells in sessions.items():
                    for room, members in cells.items():
                        for person in members:
                            if person not in people or room == REST:
                                continue
                            counts[person, "all"] += 1
                            daily[d, person] += 1
                            if room == PHOTO:
                                counts[person, "photo"] += 1
                            if is_follow_slot(room):
                                weekly[week, person] += 1
                                doctor = inp.clinic_doctors.get(d, {}).get(
                                    session, {}).get(room)
                                if doctor:
                                    doctors[person, doctor] += 1
                                if (person == p and d in inp.grid
                                        and session not in inp.locked.get(iso, {})
                                        and not on_leave(inp, "pgy", p, d, session)
                                        and not (len(people) == 2 and
                                                 (d.weekday(), session)
                                                 in TWO_PGY_PHOTO_ONLY)):
                                    candidates.append((d, session, cells, room, doctor))

            if (counts[p, "photo"] > _manual_target(
                    counts, people, offsets, p, "photo")
                    or counts[p, "all"] <= _manual_target(
                        counts, people, offsets, p, "all")):
                break
            eligible = [(d, session, cells, room, doctor)
                        for d, session, cells, room, doctor in candidates
                        if weekly[d.isocalendar()[:2], p] >= 3
                        and daily[d, p] >= 2]
            if not eligible:
                break
            d, session, cells, room, doctor = max(
                eligible,
                key=lambda item: (
                    weekly[item[0].isocalendar()[:2], p],
                    -dropped_weeks[item[0].isocalendar()[:2], p],
                    doctors[p, item[4]] if item[4] else 0,
                    -item[0].toordinal(), item[1],
                ),
            )
            cells[room].remove(p)
            cells.setdefault(REST, []).append(p)
            dropped_weeks[d.isocalendar()[:2], p] += 1
            log.append(f"PGY {p} 手動照光減量：{d.isoformat()} {session} "
                       f"保留必要人力與每週跟診，減少一個非必要跟診時段")


def balance_pgy(inp, slots, log, warnings, *, control=None):
    from ortools.sat.python import cp_model

    if control is not None:
        control.checkpoint("平衡 PGY 工作量")

    people = sorted(set(inp.pgy_roster))
    if not people:
        return
    offsets = inp.pgy_photo_offsets
    if any(type(v) is not int or not -99 <= v <= 99 for v in offsets.values()):
        raise ValueError("PGY 照光調整須為 -99 至 99 的整數")
    model = cp_model.CpModel()
    totals, weekly, previous = defaultdict(list), defaultdict(list), Counter()
    daily_work = defaultdict(list)
    choices, changes = [], []
    original_counts, original_daily_work = Counter(), Counter()
    for iso, ss in sorted(slots.items()):
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if iso[:7] != inp.ym:
            continue
        week = d.isocalendar()[:2]
        for s, cells in ss.items():
            positions = [(r, i, p) for r, ps in cells.items() for i, p in enumerate(ps) if p in people]
            movable = [(r, i, p) for r, i, p in positions
                       if (r in (PHOTO, TREATMENT, REST) or is_follow_slot(r))
                       and not on_leave(inp, "pgy", p, d, s)
                       # The existing two-PGY weekly rotation outranks monthly
                       # fairness and requested photo offsets.
                       and not (len(people) == 2 and (d.weekday(), s) in TWO_PGY_PHOTO_ONLY)
                       and s not in inp.locked.get(iso, {}) and d in inp.grid]
            candidates = [p for _, _, p in movable]
            assignments = defaultdict(list)
            for room, index, original in positions:
                if room != REST:
                    original_daily_work[d, original] += 1
                if room == PHOTO:
                    kinds = ["photo", "necessary", "all"]
                    if d.weekday() == 2 and s == "下午":
                        kinds.append("wed")
                elif room == TREATMENT:
                    kinds = ["tx", "necessary", "all"]
                else:
                    kinds = ["follow", "all"] if is_follow_slot(room) else []
                if "follow" in kinds:
                    previous[week, original] += 1
                original_counts.update((original, kind) for kind in kinds)
                if (room, index, original) not in movable:
                    if room != REST:
                        daily_work[d, original].append(1)
                    for kind in kinds:
                        totals[original, kind].append(1)
                    if "follow" in kinds:
                        weekly[week, original].append(1)
                    continue
                seat = []
                for p in candidates:
                    var = model.new_bool_var(f"{iso}/{s}/{room}/{index}/{p}")
                    seat.append(var)
                    assignments[p].append(var)
                    model.add_hint(var, int(p == original))
                    choices.append((cells, room, index, p, var))
                    if p != original:
                        changes.append(var)
                    if room != REST:
                        daily_work[d, p].append(var)
                    for kind in kinds:
                        totals[p, kind].append(var)
                    if "follow" in kinds:
                        weekly[week, p].append(var)
                model.add(sum(seat) == 1)
            for variables in assignments.values():
                model.add(sum(variables) == 1)
    for key, count in previous.items():
        model.add(sum(weekly[key]) >= min(1, count))
    # Fairness may exchange work and rest seats within a half-day, but it must
    # not turn somebody who originally worked that day into a full-day rest.
    # Existing leave/lock-driven full-day rests remain untouched.
    for key, count in original_daily_work.items():
        if count:
            model.add(sum(daily_work[key]) >= 1)
    # Protect each achieved weekly minimum and the week's total target credit.
    # A second clinic can move between PGYs to spread monthly shortages fairly;
    # pinning every person's second clinic stranded one person at 8 vs 10/10/10.
    for week in {w for w, _ in previous}:
        credit = []
        for p in people:
            achieved = model.new_int_var(0, 2, f"weekly_target_credit/{week}/{p}")
            model.add_min_equality(achieved, [sum(weekly[week, p]), 2])
            credit.append(achieved)
        model.add(sum(credit) >= sum(min(2, previous[week, p]) for p in people))
    weights = availability_weights(inp, people)
    phases, original_score, lower_bounds = workload_goals(
        model, people, totals, original_counts, weights, offsets)
    phases.append(changes)
    original_score.append(0)
    lower_bounds.append(0)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    solver.parameters.max_deterministic_time = 5
    saved = [int(cells[room][index] == p) for cells, room, index, p, _ in choices]
    best_score = tuple(original_score)
    status = cp_model.OPTIMAL
    all_optimal = True
    for tier, terms in enumerate(phases):
        if control is not None:
            control.checkpoint(f"平衡 PGY 工作量 {tier + 1}/{len(phases)}")
        expression = sum(terms)
        model.add(expression <= best_score[tier])
        model.add(expression >= lower_bounds[tier])
        if best_score[tier] == lower_bounds[tier]:
            continue
        model.minimize(expression)
        status = control.solve(solver, model) if control is not None else solver.solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            all_optimal = False
            break
        score = tuple(solver.value(sum(terms)) for terms in phases)
        if score <= best_score:
            best_score = score
            saved = [solver.value(v) for _, _, _, _, v in choices]
        if status != cp_model.OPTIMAL:
            all_optimal = False
        model.add(expression == best_score[tier])
        model.clear_hints()
        for (_, _, _, _, variable), value in zip(choices, saved, strict=True):
            model.add_hint(variable, value)
    if not all_optimal:
        warnings.append("PGY 次數平衡尚未證明最佳，保留已知最佳可行班表")
    for (cells, room, index, p, _), selected in zip(choices, saved, strict=True):
        if selected:
            cells[room][index] = p
    _reduce_manual_pgy_follow(inp, slots, people, log)
    log.append("PGY 次數平衡：先平衡照光（個人減量除外）、治療室及週三下午，"
               "再由跟診調整總工作量；各人總次數盡量一致，個人減量除外。"
               "週三下午照光已包含在照光次數，照光個人調整同步調整總工作量。"
               "保留請假、鎖定、必要人力、個人每週最低跟診及每週整體目標達成次數")
    actual = Counter()
    for iso, ss in slots.items():
        if iso[:7] != inp.ym:
            continue
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        for s, cells in ss.items():
            for room, members in cells.items():
                for p in members:
                    if room in (PHOTO, TREATMENT):
                        actual[p, "necessary"] += 1
                        actual[p, "all"] += 1
                    elif is_follow_slot(room):
                        actual[p, "all"] += 1
                    if room == PHOTO:
                        actual[p, "photo"] += 1
                        if d.weekday() == 2 and s == "下午":
                            actual[p, "wed"] += 1
                    elif room == TREATMENT:
                        actual[p, "tx"] += 1
    offset_values = [offsets.get(p, 0) for p in people]
    uniform_adjustment = len(set(offset_values)) == 1 and bool(offset_values[0])
    for kind, title in (("photo", "照光"), ("tx", "治療室"), ("wed", "週三下午"),
                        ("necessary", "必要工作"), ("all", "總工作量")):
        shares = dict.fromkeys(people, 1)
        scale = sum(shares.values())
        shifts = {p: offsets.get(p, 0) if kind in ("photo", "necessary", "all") else 0
                  for p in people}
        pool = sum(actual[p, kind] - shifts[p] for p in people)
        targets = {p: pool * shares[p] / scale + shifts[p] for p in people}
        spread = (max(actual[p, kind] - shifts[p] for p in people)
                  - min(actual[p, kind] - shifts[p] for p in people))
        if (any(abs(actual[p, kind] - targets[p]) > 1 for p in people)
                or spread > 1):
            warnings.append(f"PGY {title}未完全平衡（請假、鎖定、必要人力與每週跟診優先）："
                            + "、".join(f"{p} {actual[p, kind]} 次／目標 {targets[p]:.1f}" for p in people))
        if kind not in ("photo", "necessary", "all"):
            continue
        for p in people:
            requested = (round(sum(actual[q, kind] for q in people) * shares[p] / scale)
                         + offsets.get(p, 0))
            if not uniform_adjustment and len(people) > 1:
                # An absolute rounded request only breaks fairness ties. A
                # peer far below the shared target (e.g. long leave) cannot
                # define whether this person's reduction succeeded.
                fair_floor = pool // scale
                comparable = [actual[q, kind] - shifts[q] for q in people
                              if q != p and actual[q, kind] - shifts[q] >= fair_floor]
                requested = min(comparable, default=fair_floor) + shifts[p]
            requested = max(0, requested)
            if offsets.get(p, 0) < 0 and actual[p, kind] > requested:
                warnings.append(f"PGY {p} {title}減量目標未達：實排 {actual[p, kind]} 次，"
                                f"調整後目標 {requested} 次；必要人力、鎖定與每週最低跟診優先")
    if uniform_adjustment:
        warnings.append("PGY 全員設定相同照光調整：必要工作總人力固定，無法讓全員同時增減；仍盡量平均分配")
