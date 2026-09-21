"""Monthly external trainees and course-local clinic diversity.

Special duties and locked sessions are preserved. Clinic seats follow monthly
trainee priorities; room balancing only moves attendance between open rooms.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date

from .session_leave import on_leave
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
    occupied = set(inp.pgy_roster) | set(inp.family_roster)
    occupied.update(c for b in inp.clerk_batches for c in b.members)
    return sorted(set(inp.external_roster) - occupied)


def _weeks(inp):
    weeks = defaultdict(list)
    for d in sorted(inp.grid):
        if d.isoformat()[:7] == inp.ym and d.weekday() < 5 and d not in inp.holidays:
            weeks[d.isocalendar()[:2]].append(d)
    return weeks


def _in_month_half(iso, ym, half):
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return False
    return d.isoformat() == iso and iso[:7] == ym and (d.day > 14) == bool(half)


def external_quota_warnings(inp, slots):
    from .training_bands import training_warnings
    return training_warnings(inp, slots, scopes=("external",))


def _fixed_clerk_follows(inp, slots):
    """Course credit outside the movable monthly clinic budget."""
    counts = defaultdict(int)
    sources = {iso: ss for iso, ss in inp.prior_sessions.items() if iso[:7] < inp.ym}
    sources.update({iso: ss for iso, ss in inp.course_fixed.items() if iso[:7] > inp.ym})
    sources.update(slots)
    order = arbitration_order(inp)
    for iso, sessions in sources.items():
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        owner = day_owner_batch(order, d)
        if not owner or d.weekday() >= 5 or not isinstance(sessions, dict):
            continue
        if iso[:7] == inp.ym and d not in inp.grid:
            continue
        excluded = inp.prior_pgy if iso[:7] < inp.ym else inp.pgy_roster
        for s, cells in sessions.items():
            if s not in STUDENT_SESSIONS or not isinstance(cells, dict):
                continue
            for room, members in cells.items():
                if is_follow_slot(room) and (iso[:7] != inp.ym or room in inp.grid[d].get(s, [])):
                    for p in members or []:
                        if p in owner.members and p not in excluded:
                            counts[owner.id, p] += 1
    return counts


def add_external(inp, slots, log, warnings):
    people = external_roster(inp)
    conflicts = sorted(set(inp.external_roster) - set(people))
    if conflicts:
        warnings.append(f"外訓代號與 PGY/Clerk 重複：{'、'.join(conflicts)}；未重複排班")
    from .follow_priority import prepare_priority, restore_pgy_and_rest, family_requirement_warnings
    for iso, sessions in inp.locked.items():
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        if iso[:7] != inp.ym or not isinstance(sessions, dict):
            continue
        for s, cells in sessions.items():
            if not isinstance(cells, dict):
                continue
            for scope, roster in (("external", people), ("family", inp.family_roster)):
                for p in roster:
                    if on_leave(inp, scope, p, d, s) and any(p in (ps or []) for ps in cells.values()):
                        warnings.append(f"{p} {iso} {s} 請假與既有保留排班衝突；原紀錄未更動，請手動確認該時段")
    targets, originals = prepare_priority(inp, slots)
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    choices = {}
    clerk_objective, spread_objective = [], []
    leaves = {**inp.leaves.get("external", {}), **inp.leaves.get("clerk", {})}
    max_shortfall = model.new_int_var(0, 1000, "clerk_shortfall")
    clerk_objective.append(100000 * max_shortfall)
    def shortage(new, target):
        if target <= 0:
            return
        deficit = model.new_int_var(0, target, "shortfall")
        model.add(deficit >= target - new)
        model.add(1000 * deficit <= max_shortfall * target)
        clerk_objective.append(1000 * deficit)
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
            owner = day_owner_batch(arbitration_order(inp), d)
            clerks = [p for p in (owner.members if owner else [])
                      if owner is not None and (owner.id, p) in targets and p not in inp.pgy_roster]
            assigned = {p for ps in cells.values() for p in ps}
            family = [p for p in inp.family_roster if (d, s) in inp.family_follow.get(p, set())]
            for p in sorted(set(people + clerks + family + inp.pgy_roster)):
                scope = "family" if p in family else "external" if p in people else "pgy" if p in inp.pgy_roster else "clerk"
                if p in assigned or on_leave(inp, scope, p, d, s):
                    continue
                pair = []
                for kind, available in (("follow", spare > 0),
                                        ("biopsy", p in people + family and external_biopsy_open(inp, d, s)
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
    def deviation(expression, bound, name):
        value = model.new_int_var(0, bound, name)
        model.add_abs_equality(value, expression)
        return value

    fixed_clerk = _fixed_clerk_follows(inp, slots)
    clerk_minima, clerk_extra = [], []
    for (bid, p), target in targets.items():
        new = sum(v for (d, _, pp, k), v in choices.items()
                  if pp == p and k == "follow"
                  and (owner := day_owner_batch(arbitration_order(inp), d)) is not None
                  and owner.id == bid)
        model.add(new <= target)
        fixed = fixed_clerk[bid, p]
        clerk_minima.append((bid, p, new, min(target, max(0, 9 - fixed))))
        shortage(new, min(target, max(0, 10 - fixed)))
        clerk_extra.append(-new)
    from .training_bands import add_training_objectives
    training_phases, training_spread = add_training_objectives(
        inp, model, choices, deviation, people, clerk_minima)
    spread_objective.extend(training_spread)
    idle_days = []
    for d in sorted(inp.grid):
        owner = day_owner_batch(arbitration_order(inp), d)
        if (not owner or d.isoformat()[:7] != inp.ym or d.weekday() >= 5
                or not any(inp.grid[d].get(s) for s in STUDENT_SESSIONS)):
            continue
        for p in owner.members:
            if p in inp.pgy_roster or d in leaves.get(p, set()):
                continue
            fixed_work = any(p in ps for s, cells in slots.get(d.isoformat(), {}).items()
                             if s in STUDENT_SESSIONS for r, ps in cells.items()
                             if r != REST)
            if fixed_work:
                continue
            attendance = sum(v for (dd, _, pp, k), v in choices.items()
                             if dd == d and pp == p and k == "follow")
            idle = model.new_bool_var(f"idle/{d}/{p}")
            model.add(attendance + idle >= 1)
            idle_days.append(idle)
    spread_objective.extend(100 * idle for idle in idle_days)
    # Prefix balance distributes each person's actual total along their available
    # days, even when leave or scarce rooms prevent reaching the nominal quota.
    groups = [(p, [d for ds in _weeks(inp).values() for d in ds]) for p in people]
    groups += [(p, [d for d in sorted(inp.grid) if d.isoformat()[:7] == inp.ym
                     and (b := day_owner_batch(arbitration_order(inp), d)) and b.id == bid])
               for bid, p in targets]
    for p, days in groups:
        days = [d for d in days if d.weekday() < 5 and d not in leaves.get(p, set())
                and any(inp.grid[d].get(s) for s in STUDENT_SESSIONS)]
        daily = [sum(v for (dd, _, pp, k), v in choices.items()
                     if dd == d and pp == p and k == "follow")
                 + sum(p in ps for cells in slots.get(d.isoformat(), {}).values()
                       for r, ps in cells.items() if is_follow_slot(r)) for d in days]
        for i in range(1, len(days)):
            spread_objective.append(deviation(
                len(days) * sum(daily[:i]) - i * sum(daily), 10000, f"prefix/{p}/{days[i]}"))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    solver.parameters.linearization_level = 2
    solver.parameters.max_deterministic_time = 10
    # Freeze each achieved priority objective before considering the next tier.
    # Joint minimums first, then ordered targets. Clerk's optional 11th clinic
    # uses capacity left after everyone's targets, never a PGY's weekly minimum.
    phases = (*training_phases[:2], clerk_objective, *training_phases[2:],
              clerk_extra, spread_objective)
    for index, objective in enumerate(phases):
        expression = sum(objective)
        model.minimize(expression)
        status = solver.solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            raise RuntimeError("家醫科／Clerk／外訓排班未取得可行解；原班表未變更")
        if index < len(phases) - 1 and status != cp_model.OPTIMAL:
            raise RuntimeError("排班逾時，尚未確認跟診優先順序與配額的最佳解；原班表未變更")
        model.add(expression == solver.value(expression))
    for (d, s, p, kind), v in choices.items():
        if not solver.value(v):
            continue
        cells = slots[d.isoformat()][s]
        room = BIOPSY if kind == "biopsy" else next(
            r for r in inp.grid[d][s] if len(cells.get(r, [])) < inp.capacity)
        cells.setdefault(room, []).append(p)
    restore_pgy_and_rest(inp, slots, originals)
    from .follow_priority import spread_clerk_days
    spread_clerk_days(inp, slots)
    warnings.extend(family_requirement_warnings(inp, slots))
    for (bid, p), target in targets.items():
        actual = sum(p in ps for (d, ss) in originals
                     if (owner := day_owner_batch(arbitration_order(inp), d)) and owner.id == bid
                     for r, ps in slots[d.isoformat()][ss].items() if is_follow_slot(r))
        desired = min(target, max(0, 10 - fixed_clerk[bid, p]))
        if actual < desired:
            warnings.append(f"Clerk {p}（{bid}）可調整跟診 {actual}/{desired} 次；先保障所有人的最低需求，再依 Clerk、家醫科、外訓、PGY 安排目標")
    from .follow_priority import refresh_clerk_warnings
    refresh_clerk_warnings(inp, slots, warnings)
    from .training_bands import training_warnings
    warnings.extend(training_warnings(inp, slots, scopes=("external", "family")))
    log.append("基本需求先保障；額外跟診依 Clerk ＞ 家醫科 ＞ 外訓 ＞ PGY；家醫／外訓跟診最低 30%、目標 50%、上限 70%；PGY 每週最低 1、目標 2 診；家醫／外訓每半月一次切片室")


def balance_rooms(inp, slots):
    from .clinic_diversity import balance_clinics
    balance_clinics(inp, slots)


def finish_courses(inp, slots, log, warnings):
    slots, log, warnings = deepcopy(slots), list(log), list(warnings)
    add_external(inp, slots, log, warnings)
    from .pgy_balance import balance_pgy
    balance_pgy(inp, slots, log, warnings)
    balance_rooms(inp, slots)
    from .training_bands import training_warnings
    warnings.extend(training_warnings(inp, slots, scopes=("pgy",)))
    log.append("依 course 優先平衡實際跟診醫師，再平衡診間；保留各人跟診次數、特殊工作、家醫可參與時段與鎖定內容")
    return slots, log, warnings
