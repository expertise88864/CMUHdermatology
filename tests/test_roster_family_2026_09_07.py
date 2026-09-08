from copy import deepcopy
from datetime import date

import pytest

from cmuh_common.roster.model import ClerkBatch
from cmuh_common.roster.solve_day import (
    DaySolveInput, month_solve_day, is_follow_slot, day_input_fingerprint,
)
from cmuh_common.roster.storage import RosterStorage, family_month_codes, family_follow_slots
from cmuh_common.roster.service import RosterService
from cmuh_common.roster.follow_priority import family_requirement_warnings


def make_input():
    grid = {date(2026, 9, n): {"上午": ["101", "102", "103"], "下午": ["101", "102", "103"]}
            for n in range(1, 31) if date(2026, 9, n).weekday() < 5}
    return DaySolveInput("2026-09", grid, ["P1", "P2"],
                         family_roster=["F1", "F2"], external_roster=["E1"],
                         family_follow={"F1": {(date(2026, 9, n), "下午") for n in (1, 8, 15, 22)},
                                        "F2": {(date(2026, 9, 22), "上午"), (date(2026, 9, 22), "下午")}})


@pytest.fixture
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [], "vs_members": []})
    return RosterService(st)


@pytest.mark.parametrize("n", [0, 1, 2])
def test_month_roster_defaults_and_isolation(svc, n):
    assert svc.build_day_input("2026-09").family_roster == []
    codes = [f"F{i}" for i in range(n)]
    svc.set_family_month_roster("2026-09", codes, baseline=[])
    assert svc.build_day_input("2026-09").family_roster == codes
    assert svc.build_day_input("2026-10").family_roster == []


def test_roster_count_after_concurrent_merge(svc):
    svc.set_family_month_roster("2026-09", ["F1", "F2"], baseline=[])
    with pytest.raises(ValueError, match="0–2"):
        svc.set_family_month_roster("2026-09", ["F3"], baseline=[])
    assert svc.build_day_input("2026-09").family_roster == ["F1", "F2"]


def test_specified_delta_and_fingerprint(svc):
    svc.set_family_month_roster("2026-09", ["F1"], baseline=[])
    before = day_input_fingerprint(svc.build_day_input("2026-09"))
    a, b = (date(2026, 9, 1), "上午"), (date(2026, 9, 8), "下午")
    svc.set_family_follow("2026-09", {"F1": {a}}, baseline={})
    svc.set_family_follow("2026-09", {"F1": {b}}, baseline={})
    inp = svc.build_day_input("2026-09")
    assert inp.family_follow["F1"] == {a, b}
    assert day_input_fingerprint(inp) != before
    assert any("家醫科" in w for w in svc.quick_validate_day("2026-09"))
    svc.set_family_month_roster("2026-09", [], baseline=["F1"])
    with pytest.raises(ValueError, match="已不在"):
        svc.set_family_follow("2026-09", {"F1": {a}}, baseline={})


@pytest.mark.parametrize("value", [None, "F1", [""], ["F1", "F1"], ["A", "B", "C"]])
def test_malformed_roster(value):
    with pytest.raises(ValueError):
        family_month_codes({"family_month_roster": value})


@pytest.mark.parametrize("key", ["2026-10-01|上午", "2026-09-31|下午", "2026-09-01|夜間", "bad"])
def test_invalid_session_rejected(key):
    with pytest.raises(ValueError):
        family_follow_slots("2026-09", {"family_follow": {"F1": [key]}})


def test_identity_both_directions(svc):
    with pytest.raises(ValueError):
        svc.set_family_month_roster("2026-09", ["P1"], baseline=[])
    svc.set_family_month_roster("2026-09", ["F1"], baseline=[])
    with pytest.raises(ValueError):
        svc.set_external_month_roster("2026-09", ["F1"], baseline=[])
    with pytest.raises(ValueError):
        svc.set_pgy_month_roster("2026-09", ["F1"], baseline=["P1"])
    with pytest.raises(ValueError):
        svc.set_pgy_default_members(["F1"], baseline=["P1"])
    with pytest.raises(ValueError):
        svc.update_clerk_batches(lambda bs: bs.append(
            {"id": "b", "start_monday": "2026-09-07", "members": ["F1"]}))


def test_exact_required_slots_and_monthly_room_balance():
    inp = make_input()
    original = deepcopy(inp)
    slots, _, _ = month_solve_day(inp)
    assert inp == original
    assert not family_requirement_warnings(inp, slots)
    for p in inp.family_roster:
        actual = {(date.fromisoformat(iso), s) for iso, ss in slots.items()
                  for s, cells in ss.items() for r, ps in cells.items() if p in ps and is_follow_slot(r)}
        assert actual == inp.family_follow[p]
        assert all(is_follow_slot(r) for ss in slots.values() for cells in ss.values()
                   for r, ps in cells.items() if p in ps)
    rooms = {r for ss in slots.values() for cells in ss.values()
             for r, ps in cells.items() if "F1" in ps}
    assert rooms == {"101", "102", "103"}


def test_family_priority_with_scarce_seats():
    inp = make_input()
    inp.grid = {d: {s: ["101"] for s in ss} for d, ss in inp.grid.items()}
    inp.capacity = 1
    inp.family_roster = ["F1"]
    inp.family_follow = {"F1": {(d, s) for d, ss in inp.grid.items() for s in ss}}
    inp.clerk_batches = [ClerkBatch("B", date(2026, 8, 31), ["C1", "C2"])]
    slots, _, warnings = month_solve_day(inp)
    assert not family_requirement_warnings(inp, slots)
    assert all(cells.get("101") == ["F1"] for ss in slots.values() for cells in ss.values())
    assert any("外訓" in w for w in warnings)
    assert any("跟診時段偏少" in w and "C1×0" in w and "C2×0" in w for w in warnings)


def test_leave_closed_and_locked_conflicts_preserve_slots():
    inp = make_input()
    inp.leaves = {"family": {"F1": {date(2026, 9, 1)}}}
    inp.grid[date(2026, 9, 8)]["下午"] = []
    inp.locked = {"2026-09-15": {"下午": {"101": ["P1"]}}}
    slots, _, warnings = month_solve_day(inp)
    assert slots["2026-09-15"]["下午"] == inp.locked["2026-09-15"]["下午"]
    for phrase in ("請假衝突", "無開放診間", "鎖定內容"):
        assert any(phrase in w for w in warnings)


def test_clerk_precedes_external_with_scarcity_and_preserves_course_cap():
    inp = make_input()
    inp.family_roster, inp.family_follow = [], {}
    inp.capacity = 1
    inp.grid = {d: {s: ["101"] for s in ss} for d, ss in inp.grid.items()}
    inp.clerk_batches = [ClerkBatch("B", date(2026, 8, 31), ["C1", "C2"])]
    slots, _, _ = month_solve_day(inp)
    counts = {p: sum(p in ps for iso, ss in slots.items()
                     if inp.clerk_batches[0].covers(date.fromisoformat(iso))
                     for cells in ss.values() for r, ps in cells.items() if is_follow_slot(r))
              for p in ("C1", "C2", "E1")}
    assert 0 < counts["C1"] <= 11 and 0 < counts["C2"] <= 11
    assert counts["C1"] == counts["C2"] == 9
    assert counts["E1"] == 0  # All 18 course seats are needed by higher-priority Clerks.
    assert abs(counts["C1"] - counts["C2"]) <= 1


def test_family_duplicate_solver_input_is_refused():
    inp = make_input()
    inp.family_roster = ["P1"]
    with pytest.raises(ValueError, match="代號重複"):
        month_solve_day(inp)


def test_manual_candidates_and_unspecified_assignment_warning(svc):
    from types import SimpleNamespace
    from cmuh_common.roster.ui.day_tab import _DayEditDialog
    svc.set_family_month_roster("2026-09", ["F1"], baseline=[])
    d = date(2026, 9, 7)
    editor = SimpleNamespace(service=svc, ym="2026-09", d=d, session="上午")
    assert "F1" not in _DayEditDialog._load_candidates(editor)[0]
    svc.update_month("2026-09", lambda m: m.update(day_slots={d.isoformat(): {"上午": {"101": ["F1"]}}}))
    assert any("未指定此時段" in w for w in svc.quick_validate_day("2026-09"))
    svc.set_family_follow("2026-09", {"F1": {(d, "上午")}}, baseline={})
    assert "F1" in _DayEditDialog._load_candidates(editor)[0]
    editor.session = "下午"
    assert "F1" not in _DayEditDialog._load_candidates(editor)[0]


def test_joint_solver_spreads_clerk_days_with_same_tier_external():
    from cmuh_common.roster.course_balance import add_external
    grid = {date(2026, 9, n): {"上午": ["101"], "下午": ["101"]} for n in (7, 8, 9, 10)}
    inp = DaySolveInput("2026-09", grid, [], capacity=1, external_roster=["E1"],
                        clerk_batches=[ClerkBatch("B", date(2026, 9, 7), ["C1", "C2"])])
    slots = {d.isoformat(): {s: {"101": ["C1"] if d.day == 7 else ["C2"] if d.day == 8 else []}
                            for s in ("上午", "下午")} for d in grid}
    add_external(inp, slots, [], [])
    for p in ("C1", "C2"):
        worked = [iso for iso, ss in slots.items() for cells in ss.values()
                  for r, ps in cells.items() if is_follow_slot(r) and p in ps]
        assert len(worked) == 2 and len(set(worked)) == 2
    assert sum("E1" in ps for ss in slots.values() for cells in ss.values()
               for r, ps in cells.items() if is_follow_slot(r)) == 4


@pytest.mark.parametrize("history", [None, [], {"上午": None, "下午": {"101": None}}])
def test_family_preserves_main_null_history_compatibility(history):
    inp = make_input()
    inp.clerk_batches = [ClerkBatch("B", date(2026, 8, 31), ["C1"])]
    inp.prior_sessions = {"2026-08-31": history}
    slots, _, _ = month_solve_day(inp)
    assert not family_requirement_warnings(inp, slots)
    warnings = family_requirement_warnings(inp, {"2026-09-01": history})
    assert any("09/01" in w for w in warnings)


def test_family_conflict_in_unchanged_batch_does_not_block_other_edit(svc):
    svc.set_family_month_roster("2026-09", ["X"], baseline=[])
    svc.storage.save_clerk_batches([
        {"id": "A", "start_monday": "2026-09-07", "members": ["X"]},
        {"id": "B", "start_monday": "2026-09-21", "members": ["B1"]},
    ])
    svc.update_clerk_batches(lambda bs: bs[1].update(members=["B2"]))
    assert svc.storage.load_clerk_batches()[1]["members"] == ["B2"]
    assert any("家醫科代號與其他名單重複" in w for w in svc.validate_roster_identity_invariants())
