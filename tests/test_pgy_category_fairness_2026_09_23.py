"""The October report's PGY counts must favor equal duty types first."""
from collections import Counter, defaultdict
from datetime import date

import pytest
from ortools.sat.python import cp_model

from cmuh_common.roster.pgy_workload import workload_goals
from cmuh_common.roster.model import ClerkBatch
from cmuh_common.roster.solve_day import (DaySolveInput, PHOTO, STAT_KEYS, TREATMENT,
                                          format_course_stats, month_solve_day,
                                          person_course_stats)


def test_october_duty_counts_are_equalized_before_clinic_compensation():
    people = "ABCD"
    # Counts from the user's October report. C had 12 photo / 10 treatment,
    # while B and D had 10 photo / 9 treatment. The report also shows 36,
    # 36, 40, 36 counted half-days, respectively.
    reported = {"A": (8, 8, 1, 6), "B": (10, 9, 1, 6),
                "C": (12, 10, 1, 5), "D": (10, 9, 1, 6)}
    model = cp_model.CpModel()
    totals, original = defaultdict(list), Counter()
    selected = {}
    for p, (photo, tx, wed, follow) in reported.items():
        variables = {kind: model.new_int_var(0, cap, f"{p}/{kind}")
                     for kind, cap in (("photo", 40), ("tx", 36), ("wed", 4),
                                       ("follow", 23), ("regular", 40))}
        model.add(variables["regular"] + variables["wed"] == variables["photo"])
        selected[p] = variables
        totals[p, "photo"] = [variables["photo"]]
        totals[p, "tx"] = [variables["tx"]]
        totals[p, "wed"] = [variables["wed"]]
        totals[p, "regular"] = [variables["regular"]]
        totals[p, "follow"] = [variables["follow"]]
        totals[p, "necessary"] = [variables["photo"], variables["tx"]]
        totals[p, "all"] = [variables["photo"], variables["tx"], variables["follow"]]
        model.add(variables["follow"] >= 5)  # Retain each achieved weekly minimum.
        for kind, value in (("photo", photo), ("tx", tx), ("wed", wed),
                            ("regular", photo - wed), ("follow", follow),
                            ("necessary", photo + tx), ("all", photo + tx + follow)):
            original[p, kind] = value
    for kind in ("photo", "tx", "wed", "follow"):
        model.add(sum(selected[p][kind] for p in people) == sum(original[p, kind] for p in people))
    phases, _, _ = workload_goals(
        model, list(people), totals, original,
        {"A": 36, "B": 36, "C": 40, "D": 36}, {"A": -2})
    solver = cp_model.CpSolver()
    for terms in phases:
        expression = sum(terms)
        model.minimize(expression)
        assert solver.solve(model) == cp_model.OPTIMAL
        model.add(expression == solver.value(expression))
    photo = {p: solver.value(selected[p]["photo"]) for p in people}
    tx = {p: solver.value(selected[p]["tx"]) for p in people}
    follow = {p: solver.value(selected[p]["follow"]) for p in people}
    work = {p: photo[p] + tx[p] + follow[p] for p in people}
    assert photo["A"] == 8
    assert max(photo[p] for p in "BCD") - min(photo[p] for p in "BCD") <= 1
    assert list(tx.values()) == [9, 9, 9, 9]
    assert max(work[p] - (-2 if p == "A" else 0) for p in people) - min(
        work[p] - (-2 if p == "A" else 0) for p in people) <= 1
    assert work["C"] < sum(reported["C"][i] for i in (0, 1, 3))


def test_report_total_does_not_count_wednesday_twice():
    stats = dict.fromkeys(STAT_KEYS, 0)
    stats.update(photo=8, photo_wed_pm=1, tx=8, follow=6, rest=14)
    report = format_course_stats({"A": stats}, ["A"], [])
    row = next(line for line in report.splitlines() if line.strip().startswith("A "))
    assert row.split() == ["A", "8", "1", "8", "6", "22", "14"]
    assert "週三下午照光已包含在照光次數內" in report


@pytest.mark.parametrize(("people", "total", "original_counts", "expected", "kind"), [
    ("ABCD", 42, (8, 11, 11, 12), (9, 11, 11, 11), "photo"),
    ("AB", 40, (18, 22), (19, 21), "photo"),
    ("ABCD", 98, (22, 25, 25, 26), (23, 25, 25, 25), "all"),
])
def test_manual_offset_uses_shifted_pool(people, total, original_counts, expected, kind):
    model = cp_model.CpModel()
    assignments = {p: model.new_int_var(0, total, p) for p in people}
    model.add(sum(assignments.values()) == total)
    totals = defaultdict(list)
    original = Counter()
    for p, n in zip(people, original_counts, strict=True):
        for duty in (("photo", "necessary", "all") if kind == "photo" else ("all",)):
            totals[p, duty] = [assignments[p]]
            original[p, duty] = n
    phases, _, _ = workload_goals(model, list(people), totals, original,
                                  dict.fromkeys(people, 1), {"A": -2})
    solver = cp_model.CpSolver()
    for terms in phases:
        expression = sum(terms)
        model.minimize(expression)
        assert solver.solve(model) == cp_model.OPTIMAL
        model.add(expression == solver.value(expression))
    assert tuple(solver.value(assignments[p]) for p in people) == expected


def test_relative_fair_offset_does_not_warn_of_absolute_rounding_shortfall():
    from cmuh_common.roster.pgy_balance import balance_pgy

    days = [date(2026, 10, n) for n in range(1, 31) if date(2026, 10, n).weekday() < 5][:21]
    grid = {d: {s: ["101"] for s in ("上午", "下午")} for d in days}
    members = list("A" * 9 + "B" * 11 + "C" * 11 + "D" * 11)
    slots = {d.isoformat(): {s: {PHOTO: [members[2 * i + j]]}
                                for j, s in enumerate(("上午", "下午"))}
             for i, d in enumerate(days)}
    inp = DaySolveInput("2026-10", grid, list("ABCD"), pgy_photo_offsets={"A": -2},
                        locked={iso: ss for iso, ss in slots.items()})
    warnings = []
    balance_pgy(inp, slots, [], warnings)
    assert not any("A 照光減量目標未達" in warning for warning in warnings)


def test_relative_reduction_shortfall_still_warns():
    from cmuh_common.roster.pgy_balance import balance_pgy

    day = date(2026, 10, 1)
    members = list("A" * 10 + "B" * 10 + "C" * 10 + "D" * 11)
    slots = {day.isoformat(): {"上午": {PHOTO: members}}}
    inp = DaySolveInput("2026-10", {day: {"上午": ["101"]}}, list("ABCD"),
                        pgy_photo_offsets={"A": -1}, locked={day.isoformat(): slots[day.isoformat()]})
    warnings = []
    balance_pgy(inp, slots, [], warnings)
    assert any("A 照光減量目標未達" in warning for warning in warnings)


def test_peer_with_long_leave_does_not_define_reduction_target():
    from cmuh_common.roster.pgy_balance import balance_pgy

    days = [date(2026, 10, n) for n in range(1, 31) if date(2026, 10, n).weekday() < 5][:20]
    grid = {d: {s: ["101"] for s in ("上午", "下午")} for d in days}
    members = list("A" * 10 + "B" * 5 + "C" * 12 + "D" * 13)
    slots = {d.isoformat(): {s: {PHOTO: [members[2 * i + j]]}
                                for j, s in enumerate(("上午", "下午"))}
             for i, d in enumerate(days)}
    inp = DaySolveInput("2026-10", grid, list("ABCD"), pgy_photo_offsets={"A": -2},
                        locked={iso: ss for iso, ss in slots.items()})
    inp.leaves = {"pgy": {"B": set(days[8:])}}
    warnings = []
    balance_pgy(inp, slots, [], warnings)
    assert any("照光未完全平衡" in warning for warning in warnings)
    assert not any("A 照光減量目標未達" in warning for warning in warnings)


def test_zero_assignments_do_not_report_an_impossible_negative_target():
    from cmuh_common.roster.pgy_balance import balance_pgy

    inp = DaySolveInput("2026-10", {}, list("AB"), pgy_photo_offsets={"A": -2})
    warnings = []
    balance_pgy(inp, {}, [], warnings)
    assert not any("減量目標未達" in warning for warning in warnings)


def test_unlocked_42_photo_assignments_rebalance_end_to_end():
    from cmuh_common.roster.pgy_balance import balance_pgy

    days = [date(2026, 10, n) for n in range(1, 31) if date(2026, 10, n).weekday() < 5][:21]
    grid = {d: {s: ["101", "102"] for s in ("上午", "下午")} for d in days}
    owners = list("A" * 8 + "B" * 11 + "C" * 11 + "D" * 12)
    slots = {}
    for i, d in enumerate(days):
        for j, s in enumerate(("上午", "下午")):
            p = owners[2 * i + j]
            rotated = list("ABCD")
            rotated.remove(p)
            slots.setdefault(d.isoformat(), {})[s] = {
                PHOTO: [p], TREATMENT: [rotated[0]],
                "101": [rotated[1]], "102": [rotated[2]]}
    inp = DaySolveInput("2026-10", grid, list("ABCD"), pgy_photo_offsets={"A": -2})
    balance_pgy(inp, slots, [], [])
    photo = Counter(p for ss in slots.values() for cells in ss.values() for p in cells[PHOTO])
    assert tuple(photo[p] for p in "ABCD") == (9, 11, 11, 11)


def test_unequal_total_with_leave_is_reported_as_unbalanced():
    from cmuh_common.roster.pgy_balance import balance_pgy

    first, second = date(2026, 9, 1), date(2026, 9, 2)
    inp = DaySolveInput("2026-09", {d: {s: ["101"] for s in ("上午", "下午")}
                                    for d in (first, second)}, ["A", "B"])
    inp.leaves = {"pgy": {"A": {second}}}
    slots = {first.isoformat(): {s: {PHOTO: ["A"], TREATMENT: ["B"]}
                                for s in ("上午", "下午")},
             second.isoformat(): {s: {PHOTO: ["B"]} for s in ("上午", "下午")}}
    warnings = []
    balance_pgy(inp, slots, [], warnings)
    assert any("總工作量未完全平衡" in warning for warning in warnings)


def test_zero_availability_does_not_crash_workload_report():
    from cmuh_common.roster.pgy_balance import balance_pgy

    inp = DaySolveInput("2026-09", {}, ["A", "B"])
    slots = {"2026-09-01": {"上午": {PHOTO: ["A"], TREATMENT: ["B"]}}}
    balance_pgy(inp, slots, [], [])


def test_october_with_leave_and_two_clerk_courses_keeps_duties_even():
    holidays = {date(2026, 10, 9), date(2026, 10, 26)}
    grid = {d: {"上午": ["101", "102"],
                "下午": [] if d.weekday() == 2 else ["101", "102"]}
            for n in range(1, 32) if (d := date(2026, 10, n)).weekday() < 5
            and d not in holidays}
    batches = [ClerkBatch(f"B{i}", date(2026, 10, start),
                           [f"C{i}{n}" for n in range(1, 5)])
               for i, start in ((1, 5), (2, 19))]
    inp = DaySolveInput("2026-10", grid, list("ABCD"), capacity=2,
                        family_roster=["F"], external_roster=["E"],
                        family_follow={"F": {(d, s) for d, ss in grid.items()
                                             for s, rooms in ss.items() if rooms}},
                        clerk_batches=batches, holidays=holidays,
                        pgy_photo_offsets={"A": -2})
    inp.leaves = {"pgy": {"A": {date(2026, 10, 1), date(2026, 10, 2)},
                          "B": {date(2026, 10, 5), date(2026, 10, 6)},
                          "D": {date(2026, 10, 7), date(2026, 10, 8)}}}
    inp.session_leaves = {"external": {"E": {(d, s) for d, ss in grid.items()
                                                    for s in ss if (d.weekday(), s) in
                                                    ((0, "上午"), (3, "上午"), (3, "下午"))}}}
    inp.biopsy_open = {b.id: {d.isoformat(): {s: bool(rooms) for s, rooms in ss.items()}
                              for d, ss in grid.items() if b.covers(d)} for b in batches}
    slots, _, _ = month_solve_day(inp)
    stats = person_course_stats(slots, include=set("ABCD"))
    assert sum(stats[p]["photo"] for p in "ABCD") == 40
    assert sum(stats[p]["tx"] for p in "ABCD") == 36
    assert stats["A"]["photo"] == 8
    assert max(stats[p]["photo"] for p in "BCD") - min(stats[p]["photo"] for p in "BCD") <= 1
    assert {stats[p]["tx"] for p in "ABCD"} == {9}
    assert max(stats[p]["photo_wed_pm"] for p in "ABCD") - min(
        stats[p]["photo_wed_pm"] for p in "ABCD") <= 1
    assert stats["A"]["photo"] < min(stats[p]["photo"] for p in "BCD")
    work = {p: stats[p]["photo"] + stats[p]["tx"] + stats[p]["follow"] for p in "ABCD"}
    adjusted = {p: work[p] + (2 if p == "A" else 0) for p in "ABCD"}
    assert max(adjusted.values()) - min(adjusted.values()) <= 1, work
    assert all(len(cells.get(PHOTO, [])) == 1 for ss in slots.values() for cells in ss.values())
    assert all(len(cells.get(TREATMENT, [])) == (d.weekday() != 2 or s != "下午")
               for iso, ss in slots.items() for s, cells in ss.items()
               if (d := date.fromisoformat(iso)))
