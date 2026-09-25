"""Capacity explanations are based on anonymous course inputs."""

from datetime import date, timedelta

import pytest

from cmuh_common.roster import service as service_module
from cmuh_common.roster.day_diagnostics import diagnose_day, format_day_feasibility
from cmuh_common.roster.day_explanation import explain_day_courses
from cmuh_common.roster.model import ClerkBatch
from cmuh_common.roster.service import RosterService
from cmuh_common.roster.solve_day import BIOPSY, PHOTO, TREATMENT, DaySolveInput
from cmuh_common.roster.storage import RosterStorage
from cmuh_common.roster.solve_day import month_solve_day, person_course_stats
from cmuh_common.roster.training_bands import available_slots
from scripts.benchmark_day_roster import make_case


def _batch_case(seat_count=4, *, locked=None):
    start = date(2026, 10, 5)
    members = [f"C{i}" for i in range(1, 6)]
    batch = ClerkBatch("B1", start, members)
    days = [start + timedelta(days=i) for i in range(5)]
    grid = {d: {"上午": ["101", "102"], "下午": ["103"]} for d in days}
    open_slots = {d.isoformat(): {"上午": True}
                  for d in days[:seat_count]}
    inp = DaySolveInput(
        "2026-10", grid, ["P1", "P2", "P3", "P4"],
        clerk_batches=[batch], biopsy_open={"B1": open_slots},
        course_days={d.isoformat() for d in days},
        course_clinic_days={d.isoformat() for d in days},
        locked=locked or {},
    )
    available = {(d, s) for d in days for s in ("上午", "下午")}
    data = {
        "pgy": {"roster": inp.pgy_roster, "stats": {}},
        "external": {"roster": [], "stats": {}},
        "family": {"roster": [], "stats": {}},
        "batches": [{"id": "B1", "start": start.isoformat(),
                     "end": (start + timedelta(days=13)).isoformat(),
                     "members": members, "stats": {}, "slots": {},
                     "available_slots": {c: available for c in members}}],
    }
    explanation = explain_day_courses(inp, data, {})
    return diagnose_day(inp, explanation, data)


def test_five_clerks_four_single_person_biopsy_seats_proves_one_missing():
    report = _batch_case()
    text = format_day_feasibility(report)
    assert "5 人各需至少 1 席" in text
    assert "最多涵蓋 4 人" in text
    assert "至少缺 1 席" in text
    assert "2026-10-05 上午" in text


def test_fifth_biopsy_slot_removes_the_aggregate_capacity_shortage():
    report = _batch_case(seat_count=5)
    assert not any("切片席位容量不足" in issue
                   for issue in report.proven_shortages)


def test_locked_empty_biopsy_slot_is_not_counted_as_available():
    report = _batch_case(
        locked={"2026-10-05": {"上午": {}}})
    assert any("最多涵蓋 3 人" in issue and "至少缺 2 席" in issue
               for issue in report.proven_shortages)


def test_locked_assigned_biopsy_remains_one_seat_not_two():
    report = _batch_case(
        locked={"2026-10-05": {"上午": {BIOPSY: ["C1"]}}})
    assert any("最多涵蓋 4 人" in issue and "至少缺 1 席" in issue
               for issue in report.proven_shortages)
    c1 = next(r for r in report.rows if r.code == "C1" and r.kind == "切片")
    assert c1.possible_sessions == 4


@pytest.mark.parametrize("prior_person, expected", [(None, 4), ("C1", 5)])
def test_previous_month_biopsy_opening_counts_only_when_already_assigned(
        prior_person, expected):
    start = date(2026, 9, 28)
    members = [f"C{i}" for i in range(1, 6)]
    days = [start] + [date(2026, 10, n) for n in (1, 2, 5, 6)]
    prior = {start.isoformat(): {"上午": {BIOPSY: [prior_person]}
                                     if prior_person else {}}}
    inp = DaySolveInput(
        "2026-10", {d: {"上午": ["101"]} for d in days[1:]},
        ["P1", "P2", "P3", "P4"],
        clerk_batches=[ClerkBatch("B1", start, members)],
        biopsy_open={"B1": {d.isoformat(): {"上午": True} for d in days}},
        course_days={d.isoformat() for d in days},
        course_clinic_days={d.isoformat() for d in days},
        prior_sessions=prior)
    available = {(d, "上午") for d in days}
    data = {
        "pgy": {"roster": inp.pgy_roster, "stats": {}},
        "family": {"roster": [], "stats": {}},
        "external": {"roster": [], "stats": {}},
        "batches": [{"id": "B1", "start": start.isoformat(),
                     "end": (start + timedelta(days=13)).isoformat(),
                     "members": members, "stats": {}, "slots": prior,
                     "available_slots": {c: available for c in members}}],
    }
    report = diagnose_day(inp, explain_day_courses(inp, data, prior), data,
                          adjacent_grid={start: {"上午": ["101"]}})
    seats = next(r for r in report.rows if r.code == "C1" and r.kind == "切片")
    assert seats.shared_seats == expected
    assert seats.possible_sessions == expected
    assert any("切片席位容量不足" in issue for issue in report.proven_shortages) \
        is (prior_person is None)


@pytest.mark.parametrize("prior_people", [["C5"], ["C4", "C5"]])
def test_previous_month_saved_biopsy_outside_open_grid_counts_as_done(
        prior_people):
    start = date(2026, 9, 28)
    members = [f"C{i}" for i in range(1, 6)]
    earlier = date(2026, 9, 29)
    current = [date(2026, 10, n) for n in (1, 2, 5, 6)]
    saved = {earlier.isoformat(): {"上午": {BIOPSY: prior_people}}}
    inp = DaySolveInput(
        "2026-10", {d: {"上午": ["101"]} for d in current},
        ["P1", "P2", "P3", "P4"],
        clerk_batches=[ClerkBatch("B1", start, members)],
        biopsy_open={"B1": {d.isoformat(): {"上午": True} for d in current}},
        course_days={earlier.isoformat(), *(d.isoformat() for d in current)},
        course_clinic_days={earlier.isoformat(), *(d.isoformat() for d in current)},
        prior_sessions=saved)
    available = {(d, "上午") for d in [earlier, *current]}
    data = {
        "pgy": {"roster": inp.pgy_roster, "stats": {}},
        "family": {"roster": [], "stats": {}},
        "external": {"roster": [], "stats": {}},
        "batches": [{"id": "B1", "start": start.isoformat(),
                     "end": (start + timedelta(days=13)).isoformat(),
                     "members": members,
                     "stats": person_course_stats(saved, include=set(members)),
                     "slots": saved,
                     "available_slots": {c: available for c in members}}],
    }
    report = diagnose_day(inp, explain_day_courses(inp, data, saved), data,
                          adjacent_grid={earlier: {"上午": ["101"]}})
    assert not any("切片席位容量不足" in issue
                   for issue in report.proven_shortages)


@pytest.mark.parametrize("lock_duplicate, shortage", [(False, False),
                                                     (True, True)])
def test_current_month_unlocked_duplicate_biopsy_can_be_reassigned(
        lock_duplicate, shortage):
    start = date(2026, 10, 5)
    days = [start + timedelta(days=i) for i in range(5)]
    members = [f"C{i}" for i in range(1, 6)]
    assignments = ["C1", "C1", "C2", "C3", "C4"]
    saved = {d.isoformat(): {"上午": {BIOPSY: [person]}}
             for d, person in zip(days, assignments, strict=True)}
    locked = ({d.isoformat(): saved[d.isoformat()] for d in days[:2]}
              if lock_duplicate else {})
    inp = DaySolveInput(
        "2026-10", {d: {"上午": ["101"]} for d in days},
        ["P1", "P2", "P3", "P4"],
        clerk_batches=[ClerkBatch("B1", start, members)],
        biopsy_open={"B1": {d.isoformat(): {"上午": True} for d in days}},
        course_days={d.isoformat() for d in days},
        course_clinic_days={d.isoformat() for d in days},
        locked=locked)
    available = {(d, "上午") for d in days}
    data = {
        "pgy": {"roster": inp.pgy_roster, "stats": {}},
        "family": {"roster": [], "stats": {}},
        "external": {"roster": [], "stats": {}},
        "batches": [{"id": "B1", "start": start.isoformat(),
                     "end": (start + timedelta(days=13)).isoformat(),
                     "members": members,
                     "stats": person_course_stats(saved, include=set(members)),
                     "slots": saved,
                     "available_slots": {c: available for c in members}}],
    }
    report = diagnose_day(inp, explain_day_courses(inp, data, saved), data,
                          saved)
    assert any("切片席位容量不足" in issue for issue in report.proven_shortages) \
        is shortage


def test_clerk_leave_shortage_is_personal_not_shared_room_shortage():
    start = date(2026, 10, 5)
    days = [start + timedelta(days=i) for i in range(14)
            if (start + timedelta(days=i)).weekday() < 5]
    grid = {d: {"上午": ["101"]} for d in days}
    inp = DaySolveInput(
        "2026-10", grid, ["P1", "P2", "P3", "P4"],
        clerk_batches=[ClerkBatch("B1", start, ["C1"])],
        course_days={d.isoformat() for d in days},
        course_clinic_days={d.isoformat() for d in days},
        session_leaves={"clerk": {"C1": {(d, "上午") for d in days[4:]}}})
    available = {(d, "上午") for d in days[:4]}
    data = {
        "pgy": {"roster": inp.pgy_roster, "stats": {}},
        "family": {"roster": [], "stats": {}},
        "external": {"roster": [], "stats": {}},
        "batches": [{"id": "B1", "start": start.isoformat(),
                     "end": (start + timedelta(days=13)).isoformat(),
                     "members": ["C1"], "stats": {}, "slots": {},
                     "available_slots": {"C1": available}}],
    }
    report = diagnose_day(inp, explain_day_courses(inp, data, {}), data)
    assert any("C1 跟診：最低 9" in issue for issue in report.proven_shortages)
    assert not any("全梯跟診總席位不足" in issue
                   for issue in report.proven_shortages)


def test_retained_clerk_follow_after_leave_does_not_prove_aggregate_shortage():
    start = date(2026, 10, 5)
    days = [start + timedelta(days=i) for i in range(11)
            if (start + timedelta(days=i)).weekday() < 5][:9]
    grid = {d: {"上午": ["101"]} for d in days}
    saved = {d.isoformat(): {"上午": {"101": ["C1"]}} for d in days}
    inp = DaySolveInput(
        "2026-10", grid, ["P1", "P2", "P3", "P4"],
        clerk_batches=[ClerkBatch("B1", start, ["C1"])],
        course_days={d.isoformat() for d in days},
        course_clinic_days={d.isoformat() for d in days},
        locked={days[0].isoformat(): saved[days[0].isoformat()]})
    available = {(d, "上午") for d in days[1:]}
    data = {
        "pgy": {"roster": inp.pgy_roster, "stats": {}},
        "family": {"roster": [], "stats": {}},
        "external": {"roster": [], "stats": {}},
        "batches": [{"id": "B1", "start": start.isoformat(),
                     "end": (start + timedelta(days=13)).isoformat(),
                     "members": ["C1"],
                     "stats": person_course_stats(saved, include={"C1"}),
                     "slots": saved, "available_slots": {"C1": available}}],
    }
    report = diagnose_day(inp, explain_day_courses(inp, data, saved), data,
                          saved)
    assert not any("全梯跟診總席位不足" in issue
                   for issue in report.proven_shortages)
    assert any("2026-10-05 上午 C1 已排跟診但目前請假" in note
               for note in report.manual_notes)
    only_retained = {days[0].isoformat(): saved[days[0].isoformat()]}
    data["batches"][0]["slots"] = {
        **only_retained, days[1].isoformat(): ["legacy-corrupt"]}
    data["batches"][0]["stats"] = person_course_stats(
        only_retained, include={"C1"})
    report = diagnose_day(inp, explain_day_courses(inp, data, only_retained),
                          data, only_retained)
    assert not any("C1 跟診：最低" in issue for issue in report.proven_shortages)


@pytest.mark.parametrize("scope, label", [("family", "家醫科"),
                                           ("external", "外訓")])
def test_retained_training_follow_after_leave_is_not_a_proven_shortage(
        scope, label):
    start = date(2026, 10, 5)
    days = [start + timedelta(days=i) for i in range(5)]
    all_slots = {(d, s) for d in days for s in ("上午", "下午")}
    first = (days[0], "上午")
    grid = {d: {"上午": ["101"] if d in days[:3] else [],
                "下午": []} for d in days}
    saved = {days[0].isoformat(): {"上午": {"101": ["F1"]}}}
    kwargs = {"family_roster" if scope == "family" else "external_roster":
              ["F1"]}
    if scope == "family":
        kwargs["family_follow"] = {"F1": all_slots}
    inp = DaySolveInput(
        "2026-10", grid, ["P1", "P2", "P3", "P4"],
        session_leaves={scope: {"F1": {first}}}, **kwargs)
    data = {
        "pgy": {"roster": inp.pgy_roster, "stats": {}},
        "family": {"roster": inp.family_roster,
                   "stats": person_course_stats(saved, include={"F1"})},
        "external": {"roster": inp.external_roster,
                     "stats": person_course_stats(saved, include={"F1"})},
        "batches": [],
    }
    with_malformed = {**saved, days[4].isoformat(): ["legacy-corrupt"]}
    report = diagnose_day(inp, explain_day_courses(inp, data, saved), data,
                          with_malformed)
    follow = next(r for r in report.rows if r.scope == label and r.code == "F1"
                  and r.kind == "跟診")
    assert follow.actual == 1
    assert follow.possible_sessions < follow.minimum
    assert not any(f"{label} 2026-10 F1 跟診" in issue
                   for issue in report.proven_shortages)
    assert any("已排跟診但目前請假" in note for note in report.manual_notes)


def test_retained_pgy_follow_after_leave_satisfies_weekly_minimum():
    d = date(2026, 10, 1)
    saved = {d.isoformat(): {"上午": {"101": ["P1"]}}}
    inp = DaySolveInput(
        "2026-10", {d: {"上午": ["101"]}}, ["P1", "P2"],
        locked=saved,
        session_leaves={"pgy": {"P1": {(d, "上午")}}})
    data = {"pgy": {"roster": inp.pgy_roster,
                    "stats": person_course_stats(saved, include={"P1", "P2"})},
            "family": {"roster": [], "stats": {}},
            "external": {"roster": [], "stats": {}}, "batches": []}
    report = diagnose_day(inp, explain_day_courses(inp, data, saved), data, saved)
    assert not any("PGY P1" in issue and "沒有可用門診半日" in issue
                   for issue in report.proven_shortages)
    assert any("PGY P1 已排跟診但目前請假" in note
               for note in report.manual_notes)


def test_adjacent_arbitration_batch_fixed_biopsy_is_not_current_batch():
    earlier = ClerkBatch("B1", date(2026, 9, 14), ["X1"])
    current = ClerkBatch("B2", date(2026, 9, 21), ["C1"])
    fixed = {"2026-09-24": {"上午": {BIOPSY: ["X1"]}}}
    d = date(2026, 10, 1)
    inp = DaySolveInput(
        "2026-10", {d: {"上午": ["101"]}},
        ["P1", "P2", "P3", "P4"],
        clerk_batches=[current], batch_order=[earlier, current],
        course_days={"2026-09-24", d.isoformat()},
        course_clinic_days={"2026-09-24", d.isoformat()},
        course_fixed=fixed,
        prior_sessions=fixed,
        biopsy_open={"B2": {d.isoformat(): {"上午": True}}},
    )
    available = {(d, "上午")}
    data = {
        "pgy": {"roster": inp.pgy_roster, "stats": {}},
        "family": {"roster": [], "stats": {}},
        "external": {"roster": [], "stats": {}},
        "batches": [{"id": "B2", "start": current.start_monday.isoformat(),
                     "end": "2026-10-04", "members": ["C1"],
                     "stats": {}, "slots": fixed,
                     "available_slots": {"C1": available}}],
    }
    report = diagnose_day(inp, explain_day_courses(inp, data, {}), data)
    biopsy = next(r for r in report.rows if r.code == "C1" and r.kind == "切片")
    assert biopsy.shared_seats == 1


def test_family_marked_slots_are_availability_not_mandatory_assignment():
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
    inp = DaySolveInput(
        "2026-10", {d1: {"上午": ["101"], "下午": []},
                    d2: {"上午": ["102"], "下午": []}},
        ["P1", "P2"], family_roster=["F1"],
        family_follow={"F1": {(d1, "上午"), (d1, "下午")}},
    )
    data = {"pgy": {"roster": inp.pgy_roster, "stats": {}},
            "family": {"roster": ["F1"], "stats": {}},
            "external": {"roster": [], "stats": {}}, "batches": []}
    report = diagnose_day(inp, explain_day_courses(inp, data, {}), data)
    follow = next(r for r in report.rows if r.code == "F1" and r.kind == "跟診")
    assert (follow.available_half_days, follow.possible_sessions,
            follow.actual) == (2, 1, 0)
    assert "不代表每格必排" in format_day_feasibility(report)


def test_pgy_clinic_capacity_reserves_mandatory_photo_and_treatment():
    d = date(2026, 10, 1)
    inp = DaySolveInput("2026-10", {d: {"上午": ["101"], "下午": []}},
                        ["P1", "P2"])
    data = {"pgy": {"roster": inp.pgy_roster, "stats": {}},
            "family": {"roster": [], "stats": {}},
            "external": {"roster": [], "stats": {}}, "batches": []}
    report = diagnose_day(inp, explain_day_courses(inp, data, {}), data)
    p1 = next(r for r in report.rows if r.code == "P1" and r.kind == "跟診")
    assert p1.possible_sessions == 0
    assert any("必要照光／治療室" in item for item in p1.blockers)
    assert any("PGY P1" in issue and "沒有可用門診半日" in issue
               for issue in report.proven_shortages)


def test_manual_cut_explains_locked_mandatory_duties_and_leave():
    d = date(2026, 10, 1)
    cells = {PHOTO: ["P1"], TREATMENT: ["P2"]}
    inp = DaySolveInput(
        "2026-10", {d: {"上午": ["101"], "下午": []}},
        ["P1", "P2"], pgy_photo_offsets={"P1": -2},
        locked={d.isoformat(): {"上午": cells}},
        session_leaves={"pgy": {"P2": {(d, "下午")}}},
    )
    slots = {d.isoformat(): {"上午": cells}}
    data = {"pgy": {"roster": inp.pgy_roster,
                    "stats": person_course_stats(slots,
                                                 include=set(inp.pgy_roster))},
            "family": {"roster": [], "stats": {}},
            "external": {"roster": [], "stats": {}}, "batches": []}
    report = diagnose_day(inp, explain_day_courses(inp, data, slots), data, slots)
    text = format_day_feasibility(report)
    assert "PGY P1 設定 -2" in text
    assert "2026-10-01 上午 照光 鎖定" in text
    assert "2026-10-01 下午 請假" in text


def test_cross_month_unknown_room_counts_do_not_prove_clerk_follow_shortage():
    inp = DaySolveInput(
        "2026-10", {date(2026, 10, 26): {"上午": ["101"], "下午": []}},
        ["P1", "P2"],
        clerk_batches=[ClerkBatch("B1", date(2026, 10, 26), ["C1"])],
        course_days={"2026-10-26", "2026-11-02"},
        course_clinic_days={"2026-10-26", "2026-11-02"},
    )
    available = {(date(2026, 10, 26), "上午"),
                 (date(2026, 11, 2), "上午")}
    data = {"pgy": {"roster": inp.pgy_roster, "stats": {}},
            "family": {"roster": [], "stats": {}},
            "external": {"roster": [], "stats": {}},
            "batches": [{"id": "B1", "start": "2026-10-26", "end": "2026-11-08",
                         "members": ["C1"], "stats": {}, "slots": {},
                         "available_slots": {"C1": available}}]}
    report = diagnose_day(inp, explain_day_courses(inp, data, {}), data)
    follow = next(r for r in report.rows if r.code == "C1" and r.kind == "跟診")
    assert follow.unquantified_sessions == 1
    assert not any("C1 跟診" in issue for issue in report.proven_shortages)


def test_fixed_prior_follow_counts_even_if_room_was_removed_from_template():
    start = date(2026, 9, 28)
    prior_days = [start, start + timedelta(days=1)]
    current_days = [date(2026, 10, n) for n in (1, 2, 5)]
    prior = {d.isoformat(): {"上午": {"101": ["C1"]},
                             "下午": {"102": ["C1"]}}
             for d in prior_days}
    grid = {d: {"上午": ["101"], "下午": ["101"]} for d in current_days}
    inp = DaySolveInput(
        "2026-10", grid, ["P1", "P2", "P3", "P4"],
        clerk_batches=[ClerkBatch("B1", start, ["C1"])],
        prior_sessions=prior,
        course_days={d.isoformat() for d in [*prior_days, *current_days]},
        course_clinic_days={d.isoformat() for d in [*prior_days, *current_days]})
    available = {(d, s) for d in [*prior_days, *current_days]
                 for s in ("上午", "下午")}
    data = {
        "pgy": {"roster": inp.pgy_roster, "stats": {}},
        "family": {"roster": [], "stats": {}},
        "external": {"roster": [], "stats": {}},
        "batches": [{"id": "B1", "start": start.isoformat(),
                     "end": (start + timedelta(days=13)).isoformat(),
                     "members": ["C1"],
                     "stats": person_course_stats(prior, include={"C1"}),
                     "slots": prior,
                     "available_slots": {"C1": available}}],
    }
    # The current template no longer contains September's 102 room.
    adjacent = {d: {"上午": ["101"], "下午": []} for d in prior_days}
    report = diagnose_day(inp, explain_day_courses(inp, data, prior), data,
                          adjacent_grid=adjacent)
    follow = next(r for r in report.rows if r.code == "C1" and r.kind == "跟診")
    assert follow.actual == 4
    assert follow.possible_sessions == 10
    assert not any("C1 跟診" in issue or "全梯跟診總席位不足" in issue
                   for issue in report.proven_shortages)


def test_locked_follow_counts_after_current_room_is_removed():
    d = date(2026, 10, 5)
    saved = {d.isoformat(): {"上午": {"102": ["C1"]}}}
    inp = DaySolveInput(
        "2026-10", {d: {"上午": []}}, ["P1", "P2", "P3", "P4"],
        clerk_batches=[ClerkBatch("B1", d, ["C1"])],
        locked=saved, course_days={d.isoformat()},
        course_clinic_days={d.isoformat()})
    data = {
        "pgy": {"roster": inp.pgy_roster, "stats": {}},
        "family": {"roster": [], "stats": {}},
        "external": {"roster": [], "stats": {}},
        "batches": [{"id": "B1", "start": d.isoformat(),
                     "end": (d + timedelta(days=13)).isoformat(),
                     "members": ["C1"],
                     "stats": person_course_stats(saved, include={"C1"}),
                     "slots": saved,
                     "available_slots": {"C1": {(d, "上午")}}}],
    }
    report = diagnose_day(inp, explain_day_courses(inp, data, saved), data,
                          saved)
    follow = next(r for r in report.rows if r.code == "C1" and r.kind == "跟診")
    assert follow.possible_sessions == follow.actual == 1


def test_losing_overlapping_clerk_batch_has_no_personal_follow_seat():
    d = date(2026, 10, 5)
    winner = ClerkBatch("B1", d, ["C1"])
    loser = ClerkBatch("B2", d, ["C2"])
    inp = DaySolveInput(
        "2026-10", {d: {"上午": ["101"], "下午": ["102"]}},
        ["P1", "P2", "P3", "P4"],
        clerk_batches=[winner, loser], batch_order=[winner, loser],
        course_days={d.isoformat()}, course_clinic_days={d.isoformat()},
    )
    available = {(d, "上午"), (d, "下午")}
    data = {"pgy": {"roster": inp.pgy_roster, "stats": {}},
            "family": {"roster": [], "stats": {}},
            "external": {"roster": [], "stats": {}},
            "batches": [
                {"id": b.id, "start": d.isoformat(),
                 "end": (d + timedelta(days=13)).isoformat(),
                 "members": b.members, "stats": {}, "slots": {},
                 "available_slots": {b.members[0]: available}}
                for b in (winner, loser)]}
    report = diagnose_day(inp, explain_day_courses(inp, data, {}), data)
    c2 = next(r for r in report.rows if r.code == "C2" and r.kind == "跟診")
    assert c2.available_half_days == c2.possible_sessions == 0
    assert any("梯次重疊，B1 優先" in b for b in c2.blockers)


def test_zero_offset_comparison_uses_two_read_only_solves(tmp_path, monkeypatch):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": "P1"}, {"id": "P2"}]})
    storage.save_month("2026-10", {"pgy_photo_offsets": {"P1": -1}})
    service = RosterService(storage)
    seen = []

    def fake_solve(inp, *, control=None):
        seen.append(dict(inp.pgy_photo_offsets))
        code = "P1" if inp.pgy_photo_offsets.get("P1") == 0 else "P2"
        return {"2026-10-01": {"上午": {PHOTO: [code]}}}, [], []

    monkeypatch.setattr(service_module, "month_solve_day", fake_solve)
    result = service.compare_pgy_zero_offset("2026-10", "P1")
    assert seen == [{"P1": -1}, {"P1": 0}]
    assert result["variants"]["目前設定"]["photo"] == 0
    assert result["variants"]["本人設為 0"]["photo"] == 1
    assert storage.load_month("2026-10")["pgy_photo_offsets"] == {"P1": -1}
    assert storage.load_month("2026-10").get("day_slots") == {}


def test_zero_offset_warning_filter_does_not_mix_prefix_codes(tmp_path,
                                                              monkeypatch):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": "P1"}, {"id": "P10"},
                                         {"id": "XP1"}, {"id": "1"}]})
    storage.save_month("2026-10", {"pgy_photo_offsets": {"P1": -1, "1": -1}})
    service = RosterService(storage)

    def fake_solve(_inp, *, control=None):
        return {}, [], ["PGY P10 照光減量目標未達",
                        "PGY XP1 照光減量目標未達",
                        "PGY 1 照光減量目標未達",
                        "PGY P1 照光減量目標未達",
                        "PGY 總工作量未完全平衡：P10 10 次",
                        "學員門診最佳化尚未證明最優"]

    monkeypatch.setattr(service_module, "month_solve_day", fake_solve)
    warnings = service.compare_pgy_zero_offset(
        "2026-10", "P1")["variants"]["目前設定"]["warnings"]
    assert "PGY P1 照光減量目標未達" in warnings
    assert "學員門診最佳化尚未證明最優" in warnings
    assert all(other not in item for item in warnings
               for other in ("PGY P10 ", "PGY XP1 ", "PGY 1 "))
    numeric = service.compare_pgy_zero_offset(
        "2026-10", "1")["variants"]["目前設定"]["warnings"]
    assert "PGY 1 照光減量目標未達" in numeric
    assert all("PGY P1 " not in item for item in numeric)


def test_zero_offset_comparison_rejects_changed_source(tmp_path, monkeypatch):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": "P1"}, {"id": "P2"}]})
    storage.save_month("2026-10", {"pgy_photo_offsets": {"P1": -1}})
    service = RosterService(storage)
    calls = []

    def fake_solve(inp, *, control=None):
        calls.append(inp.pgy_photo_offsets.get("P1"))
        if len(calls) == 2:
            month = storage.load_month("2026-10")
            month["pgy_photo_offsets"] = {"P1": -2}
            storage.save_month("2026-10", month)
        return {}, [], []

    monkeypatch.setattr(service_module, "month_solve_day", fake_solve)
    with pytest.raises(ValueError, match="資料已變動"):
        service.compare_pgy_zero_offset("2026-10", "P1")


def test_zero_offset_comparison_rejects_stale_settings(tmp_path):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": "P1"}, {"id": "P2"}]})
    storage.save_month("2026-10", {"pgy_photo_offsets": {"P1": -2}})
    service = RosterService(storage)
    with pytest.raises(ValueError, match="設定已變動"):
        service.compare_pgy_zero_offset("2026-10", "P1",
                                        expected_offset=-1)


def test_preflight_capacity_treats_elapsed_empty_session_as_fixed(tmp_path):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": f"P{i}"} for i in range(1, 5)]})
    storage.save_clinic_template({"template": {
        "3": {"上午": [{"room": "101"}]}}})
    service = RosterService(storage)
    future = service.day_current_course_stats("2026-10")["diagnostics"]
    past = service.day_current_course_stats(
        "2026-10", today=date(2026, 10, 1))["diagnostics"]
    future_row = next(r for r in future.rows if r.code == "P1"
                      and r.course == "2026-W40")
    past_row = next(r for r in past.rows if r.code == "P1"
                    and r.course == "2026-W40")
    assert future_row.possible_sessions >= 1
    assert past_row.possible_sessions == 0
    assert any("2026-10-01 上午 已鎖定" in b for b in past_row.blockers)


def test_saved_day_report_recomputes_diagnostics_after_manual_change(
        tmp_path, monkeypatch):
    class ReportDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 1)

    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": f"P{i}"}
                                         for i in range(1, 5)]})
    storage.save_clinic_template({"template": {
        "3": {"上午": [{"room": "101"}]}}})
    month = storage.load_month("2026-10")
    month["day_report"] = ("【逐日過程】\n原求解\n\n"
                           "【需求缺口與容量診斷】\n舊席位 999")
    storage.save_month("2026-10", month)
    monkeypatch.setattr(service_module, "date", ReportDate)

    displayed = RosterService(storage).report_for_display("day", "2026-10")
    assert displayed.count("【需求缺口與容量診斷】") == 1
    assert "舊席位 999" not in displayed
    assert "原求解" in displayed
    assert "2026-10-01 上午 已鎖定" in displayed


def test_service_uses_adjacent_month_rooms_and_previous_fixed_work(tmp_path):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": "P1"}, {"id": "P2"},
                                         {"id": "P3"}, {"id": "P4"}]})
    storage.save_clinic_template({"template": {
        "0": {"上午": [{"room": "101"}]},
        "3": {"上午": [{"room": "102"}]}}})
    storage.save_clerk_batches([{"id": "B1", "start_monday": "2026-09-28",
                                "members": ["C1"]}])
    storage.save_month("2026-09", {"day_slots": {
        "2026-09-28": {"上午": {"101": ["C1"]}}}})
    service = RosterService(storage)
    report = service.day_current_course_stats("2026-10")["diagnostics"]
    follow = next(r for r in report.rows if r.scope == "Clerk"
                  and r.code == "C1" and r.kind == "跟診")
    assert follow.unquantified_sessions == 0
    assert follow.actual == 1
    assert follow.possible_sessions >= 2  # fixed September seat + October room


def _synthetic_data(inp, slots):
    batches = []
    for b in inp.clerk_batches:
        end = b.start_monday + timedelta(days=13)
        available = {(d, s) for i in range(14)
                     if (d := b.start_monday + timedelta(days=i)).isoformat()
                     in inp.course_days for s in ("上午", "下午")}
        batches.append({
            "id": b.id, "start": b.start_monday.isoformat(),
            "end": end.isoformat(), "members": b.members,
            "slots": {**inp.prior_sessions, **slots},
            "stats": person_course_stats({**inp.prior_sessions, **slots},
                                         include=set(b.members),
                                         start=b.start_monday, end=end),
            "available_slots": {p: available for p in b.members},
        })
    return {
        "pgy": {"roster": inp.pgy_roster,
                "stats": person_course_stats(slots, include=set(inp.pgy_roster))},
        "family": {"roster": inp.family_roster,
                   "stats": person_course_stats(slots, include=set(inp.family_roster))},
        "external": {"roster": inp.external_roster,
                     "stats": person_course_stats(slots, include=set(inp.external_roster))},
        "batches": batches,
    }


@pytest.mark.parametrize("case", ["pgy4_clerk5_mix1", "pgy4_clerk4_cross",
                                  "pgy2", "pgy4_offset"])
def test_diagnostics_with_actual_synthetic_solve(case):
    inp = make_case(case)
    slots, _log, _warnings = month_solve_day(inp)
    data = _synthetic_data(inp, slots)
    report = diagnose_day(inp, explain_day_courses(inp, data, slots), data, slots)
    text = format_day_feasibility(report)
    assert "【需求缺口與容量診斷】" in text
    for p in inp.pgy_roster:
        assert any(r.scope == "PGY" and r.code == p and r.kind == "跟診"
                   for r in report.rows)
    for b in inp.clerk_batches:
        for p in b.members:
            assert f"Clerk {b.id} {p} 跟診" in text
            assert f"Clerk {b.id} {p} 切片" in text
    for p in inp.family_roster:
        assert f"家醫科 {inp.ym} {p} 跟診" in text
        assert (next(r for r in report.rows if r.code == p and r.kind == "跟診")
                .available_half_days == len(available_slots(inp, "family", p)))
    for p in inp.external_roster:
        assert f"外訓 {inp.ym} {p} 跟診" in text
    if case == "pgy4_offset":
        assert "PGY P1 設定 -1" in text


def test_minus_two_offset_diagnostic_and_holiday_case():
    inp = make_case("pgy4_offset")
    inp.pgy_photo_offsets = {"P1": -2}
    slots, _log, _warnings = month_solve_day(inp)
    data = _synthetic_data(inp, slots)
    report = diagnose_day(inp, explain_day_courses(inp, data, slots), data, slots)
    assert "PGY P1 設定 -2" in format_day_feasibility(report)
    assert date(2026, 10, 9) not in inp.grid
