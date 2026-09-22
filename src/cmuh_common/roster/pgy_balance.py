"""Reassign existing PGY seats without losing coverage or weekly clinic minima."""
from collections import Counter, defaultdict
from datetime import date

from .pgy_workload import availability_weights, workload_goals
from .session_leave import on_leave
from .solve_day import PHOTO, REST, TREATMENT, TWO_PGY_PHOTO_ONLY, is_follow_slot


def balance_pgy(inp, slots, log, warnings):
    from ortools.sat.python import cp_model

    people = sorted(set(inp.pgy_roster))
    if not people:
        return
    offsets = inp.pgy_photo_offsets
    if any(type(v) is not int or not -99 <= v <= 99 for v in offsets.values()):
        raise ValueError("PGY 照光調整須為 -99 至 99 的整數")
    model = cp_model.CpModel()
    totals, weekly, previous = defaultdict(list), defaultdict(list), Counter()
    choices, changes = [], []
    original_counts = Counter()
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
                if room == PHOTO:
                    kinds = ["photo", "necessary", "all",
                             "wed" if d.weekday() == 2 and s == "下午" else "regular"]
                elif room == TREATMENT:
                    kinds = ["tx", "necessary", "all"]
                else:
                    kinds = ["follow", "all"] if is_follow_slot(room) else []
                if "follow" in kinds:
                    previous[week, original] += 1
                original_counts.update((original, kind) for kind in kinds)
                if (room, index, original) not in movable:
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
                    for kind in kinds:
                        totals[p, kind].append(var)
                    if "follow" in kinds:
                        weekly[week, p].append(var)
                model.add(sum(seat) == 1)
            for variables in assignments.values():
                model.add(sum(variables) == 1)
    for key, count in previous.items():
        model.add(sum(weekly[key]) >= min(1, count))
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
        expression = sum(terms)
        model.add(expression <= best_score[tier])
        model.add(expression >= lower_bounds[tier])
        if best_score[tier] == lower_bounds[tier]:
            continue
        model.minimize(expression)
        status = solver.solve(model)
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
    log.append("PGY 次數平衡：依可工作時段比例，先平衡必要工作合計（照光含週三下午只計一次），"
               "再平衡各類工作，最後用跟診平衡總工作量；照光個人調整同步調整總工作量。"
               "保留請假、鎖定、必要人力、個人每週最低跟診及每週整體目標達成次數")
    actual = Counter()
    for iso, ss in slots.items():
        if iso[:7] != inp.ym:
            continue
        for cells in ss.values():
            for room, members in cells.items():
                for p in members:
                    if room in (PHOTO, TREATMENT):
                        actual[p, "necessary"] += 1
                        actual[p, "all"] += 1
                    elif is_follow_slot(room):
                        actual[p, "all"] += 1
                    if room == PHOTO:
                        actual[p, "photo"] += 1
    scale = sum(weights.values()) or len(people)
    offset_values = [offsets.get(p, 0) for p in people]
    for kind, title in (("necessary", "必要工作"), ("all", "總工作量"), ("photo", "照光")):
        pool = sum(actual[p, kind] - offsets.get(p, 0) for p in people)
        targets = {p: pool * weights[p] / scale + offsets.get(p, 0) for p in people}
        if any(abs(actual[p, kind] - targets[p]) > 1 for p in people):
            warnings.append(f"PGY {title}未完全平衡（請假、鎖定、必要人力與每週跟診優先）："
                            + "、".join(f"{p} {actual[p, kind]} 次／目標 {targets[p]:.1f}" for p in people))
        for p in people:
            requested = (round(sum(actual[q, kind] for q in people) * weights[p] / scale)
                         + offsets.get(p, 0))
            if offsets.get(p, 0) < 0 and actual[p, kind] > requested:
                warnings.append(f"PGY {p} {title}減量目標未達：實排 {actual[p, kind]} 次，"
                                f"調整後目標 {max(0, requested)} 次；必要人力、鎖定與每週最低跟診優先")
    if len(set(offset_values)) == 1 and offset_values[0]:
        warnings.append("PGY 全員設定相同照光調整：必要工作總人力固定，無法讓全員同時增減；仍依可工作時段比例分配")
