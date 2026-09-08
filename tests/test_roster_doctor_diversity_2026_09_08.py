from collections import Counter
from copy import deepcopy
from datetime import date

import pytest

from cmuh_common.roster.clinic_grid import grid_doctors, month_grid
from cmuh_common.roster.course_balance import balance_rooms
from cmuh_common.roster.model import ClerkBatch
from cmuh_common.roster.service import RosterService
from cmuh_common.roster.solve_day import (
    BIOPSY, REST, DaySolveInput, day_input_fingerprint, is_follow_slot,
)
from cmuh_common.roster.storage import RosterStorage


def doctor_counts(inp, slots, person):
    return Counter(inp.clinic_doctors[date.fromisoformat(iso)][s][r]
                   for iso, ss in slots.items() for s, cells in ss.items()
                   for r, people in cells.items() if is_follow_slot(r) and person in people)


@pytest.mark.parametrize("scope", ["pgy", "clerk", "external", "family"])
def test_actual_doctors_not_room_numbers_define_diversity(scope):
    days = [date(2026, 9, n) for n in (7, 8, 9, 10)]
    inp = DaySolveInput("2026-09", {d: {"上午": ["101", "102", "103"]} for d in days}, [])
    if scope == "clerk":
        inp.clerk_batches = [ClerkBatch("B", days[0], ["X"])]
    else:
        setattr(inp, scope + "_roster", ["X"])
    inp.family_follow = {"X": {(d, "上午") for d in days}} if scope == "family" else {}
    inp.clinic_doctors = {d: {"上午": {"101": "A", "102": "A", "103": "B"}} for d in days}
    slots = {d.isoformat(): {"上午": {"101" if i % 2 == 0 else "102": ["X"]}}
             for i, d in enumerate(days)}
    balance_rooms(inp, slots)
    assert doctor_counts(inp, slots, "X") == {"A": 2, "B": 2}
    assert all(sum("X" in ps for ps in ss["上午"].values()) == 1 for ss in slots.values())
    again = deepcopy(slots)
    balance_rooms(inp, slots)
    assert slots == again


def test_doctor_changes_in_the_same_room_and_full_room_swaps():
    days = [date(2026, 9, n) for n in (7, 8, 9, 10)]
    inp = DaySolveInput("2026-09", {d: {"上午": ["101", "102"]} for d in days}, ["P1", "P2"], capacity=1)
    inp.clinic_doctors = {d: {"上午": {"101": "A" if i % 2 == 0 else "B",
                                      "102": "B" if i % 2 == 0 else "A"}} for i, d in enumerate(days)}
    slots = {d.isoformat(): {"上午": {r: ["P1" if doctor == "A" else "P2"]
                                      for r, doctor in inp.clinic_doctors[d]["上午"].items()}} for d in days}
    balance_rooms(inp, slots)
    for p in inp.pgy_roster:
        assert doctor_counts(inp, slots, p) == {"A": 2, "B": 2}
    assert all(len(ps) == 1 for ss in slots.values() for ps in ss["上午"].values())


def test_same_day_am_pm_swap_preserves_daily_counts_and_special_duties():
    days = [date(2026, 9, n) for n in (7, 8)]
    inp = DaySolveInput("2026-09", {d: {"上午": ["101"], "下午": ["101"]} for d in days}, [],
                        clerk_batches=[ClerkBatch("B", days[0], ["C"])], external_roster=["E"], capacity=1)
    inp.clinic_doctors = {d: {"上午": {"101": "A"}, "下午": {"101": "B"}} for d in days}
    slots = {d.isoformat(): {"上午": {"101": ["C"], REST: ["E"], BIOPSY: ["Other"]},
                            "下午": {"101": ["E"], REST: ["C"]}} for d in days}
    balance_rooms(inp, slots)
    for p in ("C", "E"):
        assert doctor_counts(inp, slots, p) == {"A": 1, "B": 1}
        for ss in slots.values():
            assert sum(p in ps for cells in ss.values() for r, ps in cells.items() if is_follow_slot(r)) == 1
    assert all(ss["上午"][BIOPSY] == ["Other"] for ss in slots.values())


@pytest.mark.parametrize("guard", ["family", "locked", "two_pgy", "special", "unknown"])
def test_higher_priority_requirements_prevent_session_moves(guard):
    days = [date(2026, 9, n) for n in (8, 11)]  # protected two-PGY mornings
    inp = DaySolveInput("2026-09", {d: {"上午": ["101"], "下午": ["101"]} for d in days}, ["P", "Q"])
    inp.clinic_doctors = {d: {"上午": {"101": "A"}, "下午": {"101": "B"}} for d in days}
    slots = {d.isoformat(): {"上午": {"101": ["P"]}, "下午": {REST: ["P"]}} for d in days}
    if guard != "two_pgy":
        inp.pgy_roster = ["P"]
    if guard == "family":
        inp.pgy_roster, inp.family_roster = [], ["P"]
        inp.family_follow = {"P": {(d, "上午") for d in days}}
    if guard == "locked":
        inp.locked = deepcopy(slots)
    if guard == "special":
        for ss in slots.values():
            ss["下午"] = {BIOPSY: ["P"]}
    if guard == "unknown":
        for d in days:
            inp.clinic_doctors[d]["下午"] = {}
    before = deepcopy(slots)
    balance_rooms(inp, slots)
    assert slots == before


def test_clerk_history_and_future_fixed_count_in_its_course_only():
    d = date(2026, 9, 1)
    inp = DaySolveInput("2026-09", {d: {"上午": ["101", "102"]}}, [],
                        clerk_batches=[ClerkBatch("B", date(2026, 8, 31), ["C"])])
    inp.prior_sessions = {"2026-08-31": {"上午": {"101": ["C"]}}}
    inp.clinic_doctors = {date(2026, 8, 31): {"上午": {"101": "A"}},
                          d: {"上午": {"101": "A", "102": "B"}}}
    slots = {d.isoformat(): {"上午": {"101": ["C"]}}}
    balance_rooms(inp, slots)
    assert slots[d.isoformat()]["上午"] == {"102": ["C"]}
    assert inp.prior_sessions["2026-08-31"]["上午"] == {"101": ["C"]}


def test_template_mapping_excludes_unknown_conflicting_closed_and_paid_rooms():
    template = {"0": {"上午": [
        {"room": 101, "doctor": " A "}, {"room": "102", "doctor": "A"},
        {"room": "102", "doctor": "B"}, {"room": "103", "doctor": ""},
        {"room": "104", "doctor": "C", "is_self_paid": True}]}}
    grid = month_grid("2026-09", template, set(), {
        "2026-09-14": {"上午": {"closed_rooms": ["101"], "added_rooms": ["105"]}}})
    mapping = grid_doctors(grid, template)
    assert mapping[date(2026, 9, 7)]["上午"] == {"101": "A"}
    assert date(2026, 9, 14) not in mapping


def test_doctor_only_setting_change_invalidates_solution_fingerprint(tmp_path):
    svc = RosterService(RosterStorage(str(tmp_path)))
    svc.storage.save_config({"pgy_members": [{"id": "P"}], "r_members": [], "vs_members": []})
    svc.add_clinic_template_entry(0, "上午", "101", "A")
    before = svc.build_day_input("2026-09")
    preview = svc.run_day_solve("2026-09")
    svc.update_clinic_template(lambda data: data["template"]["0"]["上午"][0].update(doctor="B"))
    after = svc.build_day_input("2026-09")
    assert before.grid == after.grid
    assert before.clinic_doctors[date(2026, 9, 7)]["上午"]["101"] == "A"
    assert after.clinic_doctors[date(2026, 9, 7)]["上午"]["101"] == "B"
    assert day_input_fingerprint(before) != day_input_fingerprint(after)
    with pytest.raises(ValueError, match="過期"):
        svc.accept_day_solution("2026-09", preview.day_slots, "", expect=preview)


def test_future_fixed_clerk_physician_influences_current_month():
    d, future = date(2026, 9, 30), date(2026, 10, 1)
    inp = DaySolveInput("2026-09", {d: {"上午": ["101", "102"]}}, [],
                        clerk_batches=[ClerkBatch("B", date(2026, 9, 28), ["C"])])
    inp.course_fixed = {future.isoformat(): {"上午": {"101": ["C"]}}}
    inp.clinic_doctors = {future: {"上午": {"101": "A"}},
                          d: {"上午": {"101": "A", "102": "B"}}}
    slots = {d.isoformat(): {"上午": {"101": ["C"]}}}
    balance_rooms(inp, slots)
    assert slots == {d.isoformat(): {"上午": {"102": ["C"]}}}
    assert future.isoformat() not in slots


def test_full_solver_keeps_daily_quantities_and_special_assignments():
    from cmuh_common.roster.solve_day import month_solve_day
    grid = {date(2026, 9, n): {"上午": ["101", "102", "103"],
                              "下午": [] if date(2026, 9, n).weekday() == 2 else ["101", "102", "103"]}
            for n in range(1, 31) if date(2026, 9, n).weekday() < 5}
    inp = DaySolveInput("2026-09", grid, ["P1", "P2"], external_roster=["E1"], family_roster=["F1"],
                        family_follow={"F1": {(date(2026, 9, 7), "上午"), (date(2026, 9, 14), "下午")}},
                        clerk_batches=[ClerkBatch("B", date(2026, 9, 7), ["C1", "C2"])],
                        leaves={"clerk": {"C1": {date(2026, 9, 8)}}},
                        locked={"2026-09-15": {"上午": {"101": ["P1", "C1"]}}})
    baseline = month_solve_day(inp)[0]
    inp.clinic_doctors = {d: {s: {r: ("A" if r in ("101", "102") else "B") for r in rooms}
                              for s, rooms in ss.items()} for d, ss in grid.items()}
    slots = month_solve_day(inp)[0]
    def daily(schedule):
        return Counter((iso, p) for iso, ss in schedule.items() for cells in ss.values()
                       for r, people in cells.items() if is_follow_slot(r) for p in people)
    assert daily(slots) == daily(baseline)
    for iso, ss in slots.items():
        for s, cells in ss.items():
            assert {r: ps for r, ps in cells.items() if r != REST and not is_follow_slot(r)} == {
                r: ps for r, ps in baseline[iso][s].items() if r != REST and not is_follow_slot(r)}
            assert all(len(ps) <= inp.capacity for r, ps in cells.items() if is_follow_slot(r))
            assert sum(map(len, cells.values())) == len({p for ps in cells.values() for p in ps})
            assert (any("F1" in ps for ps in cells.values()) ==
                    ((date.fromisoformat(iso), s) in inp.family_follow["F1"]))
    assert slots["2026-09-15"]["上午"] == inp.locked["2026-09-15"]["上午"]


@pytest.mark.parametrize("cross", [False, True])
@pytest.mark.parametrize("occupied", [False, True])
def test_unknown_to_known_vacancy_improves_without_harming_swap_partner(cross, occupied):
    d = date(2026, 9, 7)
    target_session = "下午" if cross else "上午"
    grid = {d: {"上午": ["101"], "下午": ["102"]}} if cross else {d: {"上午": ["101", "102"]}}
    inp = DaySolveInput("2026-09", grid, ["P", "Q"], capacity=1)
    inp.clinic_doctors = {d: {target_session: {"102": "A"}}}
    sessions = {"上午": {"101": ["P"]}}
    dest = sessions.setdefault(target_session, {})
    if cross:
        dest[REST] = ["P"]
    if occupied:
        dest["102"] = ["Q"]
        if cross:
            sessions["上午"][REST] = ["Q"]
    slots = {d.isoformat(): sessions}
    before = deepcopy(slots)
    balance_rooms(inp, slots)
    if occupied:
        assert slots == before
    else:
        assert slots[d.isoformat()][target_session]["102"] == ["P"]
        assert "101" not in slots[d.isoformat()]["上午"]
        again = deepcopy(slots)
        balance_rooms(inp, slots)
        assert slots == again


@pytest.mark.parametrize("invalid", [123, True, ["A"], {"name": "A"}, None])
def test_malformed_doctor_values_are_unknown(invalid):
    d = date(2026, 9, 7)
    template = {"0": {"上午": [{"room": "101", "doctor": invalid},
                                   {"room": "102", "doctor": "A"},
                                   {"room": "102", "doctor": invalid}]}}
    assert grid_doctors({d: {"上午": ["101", "102"]}}, template) == {}
