from collections import Counter
from datetime import date

import pytest

from cmuh_common.roster.solve_day import DaySolveInput, month_solve_day, BIOPSY, is_follow_slot
from cmuh_common.roster.training_bands import available_slots, band
from cmuh_common.roster.model import ClerkBatch


def october():
    grid = {date(2026, 10, n): {"上午": ["101", "102"],
            "下午": [] if date(2026, 10, n).weekday() == 2 else ["101", "102"]}
            for n in range(1, 32) if date(2026, 10, n).weekday() < 5}
    return DaySolveInput("2026-10", grid, ["P1", "P2", "P3", "P4"],
                         family_roster=["F"], external_roster=["E"], capacity=1,
                         family_follow={"F": {(d, s) for d, ss in grid.items() for s in ss if ss[s]}})


def test_four_pgy_receive_weekly_clinics_while_family_not_filled():
    inp = october()
    slots, _, _ = month_solve_day(inp)
    counts, biopsies, weekly = Counter(), Counter(), Counter()
    for iso, ss in slots.items():
        d = date.fromisoformat(iso)
        for s, cells in ss.items():
            assigned = [p for ps in cells.values() for p in ps]
            assert len(assigned) == len(set(assigned))
            for r, ps in cells.items():
                if r == BIOPSY:
                    assert len(ps) <= 1
                    biopsies.update(ps)
                if is_follow_slot(r):
                    counts.update(ps)
                    weekly.update((d.isocalendar()[:2], p) for p in ps)
                if "F" in ps:
                    assert (d, s) in inp.family_follow["F"]
    weeks = {d.isocalendar()[:2] for d in inp.grid}
    for p in inp.pgy_roster:
        assert all(weekly[w, p] >= 1 for w in weeks)
        assert all(weekly[w, p] >= 2 for w in weeks if w != (2026, 40))
    for scope, p in (("family", "F"), ("external", "E")):
        # External has 22 working days, including closed Wednesday afternoons.
        # Family is limited to its 40 explicitly selected available half-days.
        assert len(available_slots(inp, scope, p)) == (40 if scope == "family" else 44)
        low, target, high = band(len(available_slots(inp, scope, p)))
        assert low <= counts[p] <= high
        assert counts[p] == target == (20 if scope == "family" else 22)
        assert biopsies[p] == 2


def test_family_denominator_excludes_leave_and_uses_only_windows():
    inp = october()
    inp.family_follow["F"] = {(date(2026, 10, 1), "上午"), (date(2026, 10, 1), "下午"),
                              (date(2026, 10, 2), "上午")}
    inp.session_leaves = {"family": {"F": {(date(2026, 10, 1), "上午")}}}
    assert available_slots(inp, "family", "F") == {
        (date(2026, 10, 1), "下午"), (date(2026, 10, 2), "上午")}
    assert band(10) == (3, 5, 7)
    assert band(40) == (12, 20, 28)


def test_closed_clinics_still_count_work_availability_and_report_shortfalls():
    inp = october()
    inp.grid = {d: {s: [] for s in ss} for d, ss in inp.grid.items()}
    slots, _, warnings = month_solve_day(inp)
    assert any("PGY P1" in w and "跟診 0" in w for w in warnings)
    assert any("家醫科 F" in w and "全月跟診 0" in w for w in warnings)
    assert len(available_slots(inp, "external", "E")) == 44
    assert sum("E" in cells.get(BIOPSY, []) for ss in slots.values() for cells in ss.values()) == 2
    assert not any(is_follow_slot(r) for ss in slots.values() for cells in ss.values() for r in cells)


def test_clerk_extra_clinics_never_displace_feasible_pgy_minimums():
    days = [date(2026, 9, n) for n in range(7, 19) if date(2026, 9, n).weekday() < 5]
    grid = {d: {"上午": ["101", "102"], "下午": [] if d.weekday() == 2 else ["101", "102"]}
            for d in days}
    inp = DaySolveInput("2026-09", grid, ["P1", "P2", "P3", "P4"], capacity=1,
                        clerk_batches=[ClerkBatch("B", date(2026, 9, 7), ["C1", "C2", "C3"])],
                        biopsy_open={"B": {d.isoformat(): {s: bool(rooms) for s, rooms in ss.items()}
                                           for d, ss in grid.items()}})
    slots, _, _ = month_solve_day(inp)
    totals, weekly = Counter(), Counter()
    for iso, ss in slots.items():
        for cells in ss.values():
            for room, people in cells.items():
                if is_follow_slot(room):
                    totals.update(people)
                    weekly.update((date.fromisoformat(iso).isocalendar().week, p) for p in people)
    assert sorted(totals[p] for p in ("C1", "C2", "C3")) == [9, 9, 10]
    assert all(weekly[week, p] >= 1 for week in (37, 38) for p in inp.pgy_roster)


@pytest.mark.parametrize("size", [4, 5])
def test_october_two_clerk_batches_share_two_double_capacity_clinics(size):
    inp = october()
    inp.capacity = 2
    inp.clerk_batches = [ClerkBatch(f"B{i}", date(2026, 10, start),
                        [f"C{i}{n}" for n in range(1, size + 1)])
                        for i, start in ((1, 5), (2, 19))]
    inp.biopsy_open = {b.id: {d.isoformat(): {s: bool(rooms) for s, rooms in ss.items()}
                              for d, ss in inp.grid.items() if b.covers(d)} for b in inp.clerk_batches}
    slots, _, _ = month_solve_day(inp)
    clinics, biopsies, weekly = Counter(), Counter(), Counter()
    for iso, ss in slots.items():
        d = date.fromisoformat(iso)
        for cells in ss.values():
            people = [p for members in cells.values() for p in members]
            assert len(people) == len(set(people))
            for room, members in cells.items():
                if room == BIOPSY:
                    assert len(members) <= 1
                    biopsies.update(members)
                elif is_follow_slot(room):
                    assert len(members) <= 2
                    clinics.update(members)
                    weekly.update((d.isocalendar().week, p) for p in members)
    for b in inp.clerk_batches:
        for p in b.members:
            assert 9 <= clinics[p] <= 11
            assert 1 <= biopsies[p] <= 2
    for scope, p in (("family", "F"), ("external", "E")):
        low, _, high = band(len(available_slots(inp, scope, p)))
        assert low <= clinics[p] <= high
        assert biopsies[p] == 2
    assert all(weekly[w, p] >= 1 for w in range(40, 45) for p in inp.pgy_roster)
    counts = [clinics[p] for p in inp.pgy_roster]
    assert max(counts) - min(counts) <= 1
