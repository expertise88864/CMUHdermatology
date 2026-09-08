from collections import Counter
from datetime import date

import pytest

from cmuh_common.roster.model import ClerkBatch
from cmuh_common.roster.solve_day import BIOPSY, DaySolveInput, is_follow_slot, month_solve_day


def make_input(ym="2026-09"):
    from calendar import monthrange
    y, m = map(int, ym.split("-"))
    grid = {date(y, m, n): {"上午": ["101", "102"], "下午": ["101", "102"]}
            for n in range(1, monthrange(y, m)[1] + 1) if date(y, m, n).weekday() < 5}
    batch = ClerkBatch("B", date(2026, 9, 7), ["C1", "C2"])
    return DaySolveInput(ym, grid, ["P1", "P2"], clerk_batches=[batch],
                         external_roster=["E1", "E2"], capacity=2,
                         biopsy_open={"B": {d.isoformat(): {"上午": True, "下午": True}
                                             for d in grid if batch.covers(d)}})


def counts(slots, p, room_test=is_follow_slot):
    return Counter({date.fromisoformat(iso): sum(p in ps for cells in ss.values()
                                               for r, ps in cells.items() if room_test(r))
                    for iso, ss in slots.items()})


@pytest.mark.parametrize("externals", [[], ["E1", "E2"]])
def test_monthly_half_and_course_totals_are_spread_across_all_weeks(externals):
    inp = make_input()
    inp.external_roster = externals
    slots, _, _ = month_solve_day(inp)
    for p in ("C1", "C2"):
        follow = counts(slots, p)
        assert 9 <= sum(follow.values()) <= 11
        assert 1 <= sum(counts(slots, p, lambda r: r == BIOPSY).values()) <= 2
        halves = [sum(n for d, n in follow.items() if low <= d.day <= high)
                  for low, high in ((7, 11), (14, 18))]
        assert min(halves) >= 4 and abs(halves[0] - halves[1]) <= 2
    for p in externals:
        follow = counts(slots, p)
        assert sum(follow.values()) == len(inp.grid)
        for week in {d.isocalendar().week for d in inp.grid}:
            days = [d for d in inp.grid if d.isocalendar().week == week]
            assert abs(sum(follow[d] for d in days) - len(days)) <= 1
        halves = [sum(n for d, n in counts(slots, p, lambda r: r == BIOPSY).items()
                      if (d.day > 14) == half) for half in (False, True)]
        assert halves == [1, 1]


def test_family_has_no_implicit_attendance_and_leave_locks_are_preserved():
    inp = make_input()
    inp.family_roster = ["F1", "F2"]
    inp.family_follow = {"F1": {(date(2026, 9, 7), "下午")}}
    inp.leaves = {"external": {"E1": {date(2026, 9, 8)}}}
    inp.locked = {"2026-09-14": {"上午": {"101": ["E1"]}}}
    slots, _, _ = month_solve_day(inp)
    assert sum(counts(slots, "F1").values()) == 1
    assert not any("F2" in ps for ss in slots.values() for cells in ss.values() for ps in cells.values())
    assert not any("E1" in ps for cells in slots["2026-09-08"].values() for ps in cells.values())
    assert slots["2026-09-14"]["上午"] == inp.locked["2026-09-14"]["上午"]


def test_cross_month_clerk_reserves_later_course_attendance():
    inp = make_input()
    inp.clerk_batches = [ClerkBatch("B", date(2026, 9, 28), ["C1", "C2"])]
    inp.biopsy_open = {}
    first, _, _ = month_solve_day(inp)
    assert all(2 <= sum(counts(first, p).values()) <= 4 for p in ("C1", "C2"))
    later = make_input("2026-10")
    later.clerk_batches = inp.clerk_batches
    later.prior_sessions = first
    later.biopsy_open = {}
    second, _, _ = month_solve_day(later)
    for p in ("C1", "C2"):
        a, b = sum(counts(first, p).values()), sum(counts(second, p).values())
        assert 9 <= a + b <= 11 and b >= 5


def test_holidays_reduce_external_month_target():
    inp = make_input()
    inp.clerk_batches, inp.biopsy_open = [], {}
    holiday = date(2026, 9, 7)
    del inp.grid[holiday]
    inp.holidays = {holiday}
    slots, _, _ = month_solve_day(inp)
    assert all(sum(counts(slots, p).values()) == len(inp.grid) for p in inp.external_roster)


def test_external_peers_share_scarcity_before_temporal_spreading():
    inp = make_input()
    inp.clerk_batches, inp.biopsy_open, inp.pgy_roster = [], {}, []
    inp.capacity = 1
    inp.grid = {d: {"上午": ["101"], "下午": ["101"]} for d in inp.grid}
    inp.family_roster = ["F1"]
    inp.family_follow = {"F1": {(d, "上午") for d in inp.grid}}
    slots, _, _ = month_solve_day(inp)
    totals = [sum(counts(slots, p).values()) for p in inp.external_roster]
    assert min(totals) >= 10 and abs(totals[0] - totals[1]) <= 1


def test_optional_second_biopsy_does_not_displace_ninth_follow():
    inp = make_input()
    inp.external_roster = []
    inp.clerk_batches[0].members = ["C1"]
    inp.leaves = {"clerk": {"C1": {date(2026, 9, n) for n in range(14, 19)}}}
    slots, _, _ = month_solve_day(inp)
    assert sum(counts(slots, "C1").values()) == 9
    assert sum(counts(slots, "C1", lambda r: r == BIOPSY).values()) == 1


def test_unproven_priority_phase_does_not_return_schedule(monkeypatch):
    from copy import deepcopy
    from ortools.sat.python import cp_model
    inp = make_input()
    original = deepcopy(inp)
    monkeypatch.setattr(cp_model.CpSolver, "solve", lambda *args, **kwargs: cp_model.FEASIBLE)
    with pytest.raises(RuntimeError, match="尚未確認跟診優先順序"):
        month_solve_day(inp)
    assert inp == original


def test_cross_month_defers_optional_biopsy_until_follow_budget_is_safe():
    from datetime import timedelta
    batch = ClerkBatch("B", date(2026, 9, 28), ["C1"])
    course = [batch.start_monday + timedelta(days=n) for n in range(14)]
    available = {date(2026, 9, 29), date(2026, 9, 30),
                 date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)}
    first = make_input()
    first.clerk_batches, first.external_roster = [batch], []
    first.biopsy_open = {"B": {d.isoformat(): {"上午": True, "下午": True} for d in course}}
    first.leaves = {"clerk": {"C1": set(course) - available}}
    september, _, _ = month_solve_day(first)
    assert sum(counts(september, "C1").values()) == 3
    assert sum(counts(september, "C1", lambda r: r == BIOPSY).values()) == 1
    later = make_input("2026-10")
    later.clerk_batches, later.external_roster = [batch], []
    later.biopsy_open, later.leaves = first.biopsy_open, first.leaves
    later.prior_sessions = september
    october, _, _ = month_solve_day(later)
    assert sum(counts(october, "C1").values()) == 6
    assert sum(counts(october, "C1", lambda r: r == BIOPSY).values()) == 0
