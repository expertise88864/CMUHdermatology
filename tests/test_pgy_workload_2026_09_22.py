from collections import Counter
from copy import deepcopy
from datetime import date

import pytest

from cmuh_common.roster.pgy_balance import balance_pgy
from cmuh_common.roster.pgy_workload import availability_weights
from cmuh_common.roster.solve_day import DaySolveInput, PHOTO, TREATMENT, REST, is_follow_slot


def example():
    people = list("ABCD")
    days = [date(2026, 9, n) for n in (7, 8, 10, 11, 14, 15, 17, 18)]
    grid = {d: {s: ["101"] for s in ("上午", "下午")} for d in days}
    inp = DaySolveInput("2026-09", grid, people)
    slots = {}
    for i, (d, s) in enumerate((d, s) for d, ss in grid.items() for s in ss):
        slots.setdefault(d.isoformat(), {})[s] = {
            r: [people[(i + j) % 4]] for j, r in enumerate((PHOTO, TREATMENT, "101", REST))}
    return inp, slots


def counts(slots):
    out = Counter()
    for ss in slots.values():
        for cells in ss.values():
            for room, members in cells.items():
                for p in members:
                    out[p, room] += 1
                    if room in (PHOTO, TREATMENT):
                        out[p, "necessary"] += 1
                    if room in (PHOTO, TREATMENT) or is_follow_slot(room):
                        out[p, "total"] += 1
    return out


@pytest.mark.parametrize("offset", [-1, -2])
def test_manual_reduction_reduces_photo_and_actual_total(offset):
    inp, slots = example()
    before = deepcopy(slots)
    inp.pgy_photo_offsets = {"A": offset}
    balance_pgy(inp, slots, [], [])
    actual = counts(slots)
    assert actual["A", PHOTO] == 4 + offset
    assert actual["A", "necessary"] == 8 + offset
    assert actual["A", "total"] == 12 + offset
    assert actual["A", TREATMENT] == 4
    assert actual["A", "101"] == 4
    for iso, ss in slots.items():
        for s, cells in ss.items():
            assert {r: len(ps) for r, ps in cells.items()} == {
                r: len(ps) for r, ps in before[iso][s].items()}
            assert sorted(p for ps in cells.values() for p in ps) == list("ABCD")


def test_availability_counts_closed_sessions_but_not_leave_or_holidays():
    inp, _ = example()
    days = sorted(inp.grid)
    inp.grid[days[0]]["下午"] = []
    inp.holidays = {days[-1]}
    inp.leaves = {"pgy": {"A": set(days[:3])}}
    assert availability_weights(inp, list("ABCD")) == {"A": 4, "B": 7, "C": 7, "D": 7}


def test_all_nonzero_adjustments_keep_absolute_zero_baseline():
    inp, slots = example()
    inp.pgy_photo_offsets = {"A": -1, "B": -1, "C": 1, "D": 1}
    balance_pgy(inp, slots, [], [])
    actual = counts(slots)
    for p, shift in inp.pgy_photo_offsets.items():
        assert actual[p, PHOTO] == 4 + shift
        assert actual[p, "necessary"] == 8 + shift
        assert actual[p, "total"] == 12 + shift


def test_extreme_uniform_requests_report_shortfall_without_invalid_model():
    inp, slots = example()
    inp.pgy_photo_offsets = dict.fromkeys("ABCD", -99)
    warnings = []
    balance_pgy(inp, slots, [], warnings)
    assert not any("尚未證明最佳" in w for w in warnings)
    assert any("減量目標未達" in w for w in warnings)
    assert sum(counts(slots)[p, "necessary"] for p in "ABCD") == 32


def test_locked_imbalance_is_compensated_with_clinics():
    inp, slots = example()
    # Fix the necessary workers; leave clinic/rest exchange available on every
    # half-day. A has two more necessary duties than B. Equal total work must
    # therefore give A two fewer clinics, retaining all clinic seats.
    from cmuh_common.roster.pgy_workload import workload_goals
    from ortools.sat.python import cp_model
    from collections import defaultdict
    model = cp_model.CpModel()
    totals, original = defaultdict(list), Counter()
    for p, photo, tx, follow in (("A", 5, 4, 4), ("B", 3, 4, 4),
                                ("C", 4, 4, 4), ("D", 4, 4, 4)):
        clinic = model.new_int_var(1, 7, p)
        for kind, value in (("photo", photo), ("regular", photo), ("tx", tx),
                            ("wed", 0), ("necessary", photo + tx)):
            totals[p, kind] = [value]
            original[p, kind] = value
        totals[p, "follow"] = [clinic]
        totals[p, "all"] = [photo + tx, clinic]
        original[p, "follow"] = follow
        original[p, "all"] = photo + tx + follow
    model.add(sum(sum(totals[p, "follow"]) for p in "ABCD") == 16)
    phases, _, _ = workload_goals(model, list("ABCD"), totals, original, dict.fromkeys("ABCD", 1), {})
    solver = cp_model.CpSolver()
    for terms in phases:
        model.minimize(sum(terms))
        assert solver.solve(model) == cp_model.OPTIMAL
        model.add(sum(terms) == solver.value(sum(terms)))
    assert [solver.value(sum(totals[p, "follow"])) for p in "ABCD"] == [3, 5, 4, 4]
    assert {solver.value(sum(totals[p, "all"])) for p in "ABCD"} == {12}


def test_wednesday_photo_is_not_counted_twice():
    inp, slots = example()
    # One complete Wednesday with photo-only PM. Total necessary seats=33,
    # so four equally available people must receive 8/8/8/9, not a double
    # charge to the person doing Wednesday afternoon.
    d = date(2026, 9, 16)
    inp.grid[d] = {"下午": []}
    slots[d.isoformat()] = {"下午": {PHOTO: ["A"], REST: list("BCD")}}
    balance_pgy(inp, slots, [], [])
    actual = counts(slots)
    assert sorted(actual[p, "necessary"] for p in "ABCD") == [8, 8, 8, 9]


def test_impossible_reduction_keeps_coverage_and_reports_shortfall():
    inp, slots = example()
    inp.locked = deepcopy(slots)
    inp.pgy_photo_offsets = {"A": -2}
    # One movable rest/clinic half-day keeps the optimizer active, while all
    # necessary duties remain locked.
    iso = sorted(slots)[-1]
    inp.locked[iso].pop("下午")
    before = deepcopy(slots)
    warnings = []
    balance_pgy(inp, slots, [], warnings)
    assert sum(len(c[PHOTO]) for ss in slots.values() for c in ss.values()) == 16
    assert all(slots[d][s] == before[d][s] for d, ss in inp.locked.items() for s in ss)
    assert any("減量目標未達" in w for w in warnings)
