"""Attendance bands use available half-days, not occupied clinic seats."""
from collections import defaultdict
from datetime import date
from math import gcd
from collections import Counter

from .session_leave import on_leave
from .solve_day import BIOPSY, STUDENT_SESSIONS, is_follow_slot


def available_training_slots(inp, scope, person):
    return {(d, s) for d in inp.grid if d.isoformat()[:7] == inp.ym
            and d.weekday() < 5 and d not in inp.holidays
            for s in STUDENT_SESSIONS
            if not on_leave(inp, scope, person, d, s)
            and (scope != "family" or (d, s) in inp.family_follow.get(person, set()))}


def available_slots(inp, scope, person):
    """User-confirmed denominator: work availability, including clinic closures."""
    return available_training_slots(inp, scope, person)


def band(n, percentages=(30, 50, 70)):
    """Round the target to nearest half-up, without exceeding the upper cap."""
    minimum, target, maximum = percentages
    high = n * maximum // 100
    low = min((n * minimum + 99) // 100, high)
    return low, min(high, max(low, (n * target + 50) // 100)), high


def week_slots(slots):
    weeks = defaultdict(set)
    for d, s in sorted(slots):
        weeks[d.isocalendar()[:2]].add((d, s))
    return weeks


def _fixed_table(inp):
    """整格鎖定 + 人員固定:兩者都是「這次求解改不動」的位置(見 `held.py`)。"""
    from .held import held_cell, held_keys
    table = {iso: dict(ss) for iso, ss in (inp.locked or {}).items()
             if isinstance(ss, dict)}
    for iso, s in held_keys(inp):
        table.setdefault(iso, {})[s] = held_cell(inp, iso, s)
    return table


def fixed_count(inp, p, kind, within=None):
    return sum(p in (ps or []) for iso, ss in _fixed_table(inp).items()
               if isinstance(iso, str) and iso[:7] == inp.ym and isinstance(ss, dict)
               for s, cells in ss.items() if isinstance(cells, dict)
               and s in STUDENT_SESSIONS
               and (within is None or (iso, s) in within)
               for r, ps in cells.items()
               if (r == BIOPSY if kind == "biopsy" else is_follow_slot(r)))


def fixed_week_slots(inp, week):
    within = set()
    for iso, sessions in _fixed_table(inp).items():
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        if d.isocalendar()[:2] == week and isinstance(sessions, dict):
            within.update((iso, s) for s in sessions)
    return within


def pgy_week_counts(inp, slots):
    counts = Counter()
    for iso, sessions in slots.items():
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        if iso[:7] != inp.ym or not isinstance(sessions, dict):
            continue
        for s, cells in sessions.items():
            if s not in STUDENT_SESSIONS or not isinstance(cells, dict):
                continue
            for r, people in cells.items():
                if is_follow_slot(r):
                    counts.update((d.isocalendar()[:2], p) for p in (people or []) if p in inp.pgy_roster)
    return counts


def verify_pgy_caps(inp, slots, *, report_only=False):
    """A timed-out fallback must not silently bypass configured upper limits.

    `report_only`(有固定人員時):不拒絕,改回傳未滿足條件讓預覽列出。"""
    fixed = pgy_week_counts(inp, _fixed_table(inp))
    excess = sorted((week, p, count) for (week, p), count in pgy_week_counts(inp, slots).items()
                    if count > max(inp.training.pgy_high, fixed[week, p]))
    if excess and not report_only:
        raise ValueError("無法產生符合 PGY 每週跟診上限的班表；原班表未變更，請調整條件後重試")
    return [f"PGY {p} 第 {week[1]} 週跟診 {count} 次，超過上限 {inp.training.pgy_high} 次"
            for week, p, count in excess]


def add_training_objectives(inp, model, choices, deviation, external, clerk_minima=()):
    """Protect minimums jointly, then family/external targets, then PGY targets."""
    basics, family, other, pgy, spread = [], [], [], [], []
    worst = model.new_int_var(0, 1000, "training_minimum_shortfall")

    def count(person, slots, kind="follow"):
        within = {(d.isoformat(), s) for d, s in slots}
        return (sum(v for (d, s, p, k), v in choices.items()
                    if p == person and k == kind and (d, s) in slots)
                + fixed_count(inp, person, kind, within))

    def minimum(actual, required, name):
        if not required:
            return
        deficit = model.new_int_var(0, required, name)
        model.add(deficit >= required - actual)
        model.add(1000 * deficit <= required * worst)
        basics.append(1000 * deficit)

    for bid, person, actual, required in clerk_minima:
        minimum(actual, required, f"clerk_min/{bid}/{person}")

    for scope, people, objective in (("family", inp.family_roster, family),
                                      ("external", external, other)):
        peer_gap = model.new_int_var(0, 1000, f"peer_target_gap/{scope}")
        objective.append(10000 * peer_gap)
        for person in people:
            slots = available_slots(inp, scope, person)
            training_slots = available_training_slots(inp, scope, person)
            low, target, high = band(len(slots), inp.training.follow_band(scope))
            # Count every retained clinic, even if a newly entered leave makes
            # its date unavailable. Never create further excess beyond the cap.
            fixed = fixed_count(inp, person, "follow")
            new = sum(v for (_, _, p, k), v in choices.items() if p == person and k == "follow")
            model.add(new <= max(0, high - fixed))
            minimum(new + fixed, low, f"minimum/{person}")
            gap = deviation(new + fixed - target, 1000, f"target/{person}")
            model.add(peer_gap >= gap)
            objective.append(gap)
            for week, ws in week_slots(slots).items():
                # Preserve the historical objective scale for the default 50%.
                percent = inp.training.follow_band(scope)[1]
                divisor = gcd(percent, 100)
                spread.append(30 * deviation((100 // divisor) * count(person, ws)
                                              - (percent // divisor) * len(ws), 10000,
                                              f"weekly/{person}/{week}"))
            for half in (0, 1):
                half_slots = {(d, s) for d, s in training_slots if (d.day > 14) == bool(half)}
                fixed_half = fixed_count(inp, person, "biopsy", {
                    (d.isoformat(), s) for d in inp.grid if (d.day > 14) == bool(half)
                    for s in STUDENT_SESSIONS})
                new_half = sum(v for (d, _, p, k), v in choices.items()
                               if p == person and k == "biopsy" and (d.day > 14) == bool(half))
                biopsy_target = getattr(inp.training, f"{scope}_biopsy")
                model.add(new_half <= max(0, biopsy_target - fixed_half))
                minimum(new_half + fixed_half, biopsy_target if half_slots else 0,
                        f"biopsy/{person}/{half}")
                if half_slots:
                    center = min(d.toordinal() for d, _ in half_slots) + max(d.toordinal() for d, _ in half_slots)
                    spread.extend(abs(2 * d.toordinal() - center) * v
                                  for (d, _, p, k), v in choices.items()
                                  if p == person and k == "biopsy" and (d.day > 14) == bool(half))
    for person in inp.pgy_roster:
        for week, ws in week_slots(available_slots(inp, "pgy", person)).items():
            fixed = fixed_count(inp, person, "follow", fixed_week_slots(inp, week))
            actual = fixed + sum(v for (d, _, p, k), v in choices.items()
                                 if p == person and k == "follow" and d.isocalendar()[:2] == week)
            model.add(actual <= max(inp.training.pgy_high, fixed))
            minimum(actual, inp.training.pgy_low, f"pgy_min/{person}/{week}")
            missing = model.new_int_var(0, inp.training.pgy_target, f"pgy_target/{person}/{week}")
            model.add(missing >= min(inp.training.pgy_target, len(ws)) - actual)
            pgy.append(missing)
    # A unit improvement in worst proportional deficit dominates all totals.
    return [[worst], basics, family, other, pgy], spread


def training_warnings(inp, slots, scopes=("external", "family", "pgy")):
    out = []
    for scope, roster, label in (("external", inp.external_roster, "外訓"),
                                  ("family", inp.family_roster, "家醫科"),
                                  ("pgy", inp.pgy_roster, "PGY")):
        if scope not in scopes:
            continue
        for p in roster:
            available = available_slots(inp, scope, p)
            follows, biopsy = [], []
            for iso, ss in slots.items():
                try:
                    d = date.fromisoformat(iso)
                except (TypeError, ValueError):
                    continue
                if iso[:7] != inp.ym or not isinstance(ss, dict):
                    continue
                for s, cells in ss.items():
                    if not isinstance(cells, dict) or s not in STUDENT_SESSIONS:
                        continue
                    for r, ps in cells.items():
                        if p in (ps or []):
                            if is_follow_slot(r):
                                follows.append((d, s))
                            elif r == BIOPSY:
                                biopsy.append((d, s))
            if scope == "pgy":
                weeks = week_slots(available)
                for week in sorted(set(weeks) | {d.isocalendar()[:2] for d, _ in follows}):
                    n = sum(d.isocalendar()[:2] == week for d, _ in follows)
                    low, target, high = inp.training.follow_band("pgy")
                    if (weeks.get(week) and n < target) or n > high:
                        out.append(f"PGY {p} {week[0]}-W{week[1]:02d} 跟診 {n} 次（最低 {low}、目標 {target}、上限 {high}；請確認診間、請假與保留排班）")
                continue
            low, target, high = band(len(available), inp.training.follow_band(scope))
            n = len(follows)
            if n != target:
                out.append(f"{label} {p} 全月跟診 {n} 次（可排 {len(available)} 時段；最低 {low}、目標 {target}、上限 {high}；請確認請假、容量與保留排班）")
            for half in (0, 1):
                n = sum((d.day > 14) == bool(half) for d, _ in biopsy)
                target = getattr(inp.training, f"{scope}_biopsy")
                if n != target and any((d.day > 14) == bool(half)
                                  for d, _ in available_training_slots(inp, scope, p)):
                    out.append(f"{label} {p} {'1–14 日' if half == 0 else '15 日至月底'} 切片室 {n} 次（目標 {target}；請確認開放時段、請假與保留排班）")
    return out
