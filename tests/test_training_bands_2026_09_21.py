from collections import Counter
from datetime import date

from cmuh_common.roster.solve_day import DaySolveInput, month_solve_day, BIOPSY, is_follow_slot
from cmuh_common.roster.training_bands import available_slots, band


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
        # October has 22 weekdays minus four closed Wednesday afternoons.
        assert len(available_slots(inp, scope, p)) == 40
        low, target, high = band(len(available_slots(inp, scope, p)))
        assert low <= counts[p] <= high
        assert counts[p] == target == 20
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


def test_closed_clinics_do_not_generate_false_follow_shortfalls():
    inp = october()
    inp.grid = {d: {s: [] for s in ss} for d, ss in inp.grid.items()}
    slots, _, warnings = month_solve_day(inp)
    assert not any("PGY P1" in w and "跟診 0" in w for w in warnings)
    assert not any("家醫科 F" in w and "全月跟診 0" in w for w in warnings)
    assert len(available_slots(inp, "external", "E")) == 0
    assert sum("E" in cells.get(BIOPSY, []) for ss in slots.values() for cells in ss.values()) == 2
    assert not any(is_follow_slot(r) for ss in slots.values() for cells in ss.values() for r in cells)
