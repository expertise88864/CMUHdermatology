"""Reassign existing PGY seats without losing coverage or weekly clinic minima."""
from collections import Counter, defaultdict
from datetime import date
from itertools import combinations

from .session_leave import on_leave
from .solve_day import PHOTO, REST, TREATMENT, is_follow_slot


def balance_pgy(inp, slots, log, warnings):
    from ortools.sat.python import cp_model

    people = sorted(set(inp.pgy_roster))
    if len(people) < 2:
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
                       and s not in inp.locked.get(iso, {}) and d in inp.grid]
            candidates = [p for _, _, p in movable]
            assignments = defaultdict(list)
            for room, index, original in positions:
                kinds = (["photo"] + (["wed"] if d.weekday() == 2 and s == "下午" else [])
                         if room == PHOTO else ["tx"] if room == TREATMENT
                         else ["follow"] if is_follow_slot(room) else [])
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
    if not choices:
        return
    original_scores, lower_bounds = {}, {}
    for kind in ("photo", "tx", "follow", "wed"):
        values = [original_counts[p, kind] - (offsets.get(p, 0) if kind == "photo" else 0)
                  for p in people]
        original_scores[kind] = sum((a - b) ** 2 for a, b in combinations(values, 2))
        remainder = sum(values) % len(people)
        lower_bounds[kind] = remainder * (len(people) - remainder)
    if original_scores == lower_bounds:
        return  # Already reaches the integer lower bound for all four duties.
    for key, count in previous.items():
        model.add(sum(weekly[key]) >= min(2, count))
    bound = len(choices) + 198
    objectives = defaultdict(list)
    for kind in ("photo", "tx", "follow", "wed"):
        for a, b in combinations(people, 2):
            diff = model.new_int_var(-bound, bound, f"difference/{kind}/{a}/{b}")
            offset = offsets.get(a, 0) - offsets.get(b, 0) if kind == "photo" else 0
            model.add(diff == sum(totals[a, kind]) - sum(totals[b, kind]) - offset)
            squared = model.new_int_var(0, bound * bound, f"square/{kind}/{a}/{b}")
            model.add_multiplication_equality(squared, [diff, diff])
            objectives[kind].append(squared)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    solver.parameters.max_deterministic_time = 5
    saved = [int(cells[room][index] == p) for cells, room, index, p, _ in choices]
    primary = sum(objectives["photo"] + objectives["tx"])
    secondary = sum(objectives["follow"] + objectives["wed"])
    original_score = (original_scores["photo"] + original_scores["tx"],
                      original_scores["follow"] + original_scores["wed"])
    model.add(primary <= original_score[0])
    primary_floor = lower_bounds["photo"] + lower_bounds["tx"]
    model.add(primary >= primary_floor)
    model.add(secondary >= lower_bounds["follow"] + lower_bounds["wed"])
    best_score = (*original_score, 0)
    phases = [(1, objectives["follow"] + objectives["wed"]), (2, changes)]
    if original_score[0] != primary_floor:
        phases.insert(0, (0, objectives["photo"] + objectives["tx"]))
    status = cp_model.UNKNOWN
    for tier, terms in phases:
        expression = sum(terms)
        model.add(expression <= best_score[tier])
        model.minimize(expression)
        status = solver.solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break
        score = (solver.value(primary), solver.value(secondary), solver.value(sum(changes)))
        if score <= best_score:
            best_score = score
            saved = [solver.value(v) for _, _, _, _, v in choices]
        if status != cp_model.OPTIMAL:
            break
        model.add(expression == solver.value(expression))
    if status != cp_model.OPTIMAL:
        warnings.append("PGY 次數平衡尚未證明最佳，保留已知最佳可行班表")
    for (cells, room, index, p, _), selected in zip(choices, saved, strict=True):
        if selected:
            cells[room][index] = p
    log.append("PGY 次數平衡：照光（扣除個人調整）與治療室優先，再平衡跟診與週三下午；保留每週已達成的跟診最低／目標及鎖定工作")
    for kind, title in (("photo", "照光（扣除個人調整）"), ("tx", "治療室"),
                        ("follow", "跟診"), ("wed", "週三下午")):
        # The last solve may time out; count from the retained feasible solution.
        counts = Counter()
        for iso, ss in slots.items():
            try:
                d = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            if iso[:7] != inp.ym:
                continue
            for s, cells in ss.items():
                for room, members in cells.items():
                    matches = (room == PHOTO if kind == "photo" else room == TREATMENT if kind == "tx"
                               else is_follow_slot(room) if kind == "follow"
                               else room == PHOTO and d.weekday() == 2 and s == "下午")
                    if matches:
                        counts.update(members)
        values = {p: counts[p] - (offsets.get(p, 0) if kind == "photo" else 0) for p in people}
        if max(values.values()) - min(values.values()) > 1:
            warnings.append(f"PGY {title}次數仍有差異（請假、鎖定與每週跟診需求優先）："
                            + "、".join(f"{p} {n}" for p, n in values.items()))
