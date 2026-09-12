from datetime import date
from types import SimpleNamespace

import pytest

from cmuh_common.roster.service import RosterService
from cmuh_common.roster.storage import RosterStorage, validate_authoritative_month
from cmuh_common.roster.solve_day import DaySolveInput, month_solve_day, day_input_fingerprint
from cmuh_common.roster.ui.session_leave import SessionLeaveEditor


@pytest.fixture
def svc(tmp_path):
    storage = RosterStorage(str(tmp_path))
    storage.save_config({"pgy_members": [], "r_members": [], "vs_members": []})
    return RosterService(storage)


@pytest.mark.parametrize("scope", ["external", "family"])
def test_legacy_split_merge_and_month_isolation(svc, scope):
    d = date(2026, 9, 3)
    svc.set_leaves(scope, "2026-09", "T1", {d}, baseline=set())
    before = svc.get_trainee_leave_slots(scope, "2026-09", "T1")
    assert before == {(d, "上午"), (d, "下午")}
    fingerprint = day_input_fingerprint(svc.build_day_input("2026-09"))
    svc.set_trainee_leave_slots(scope, "2026-09", "T1", {(d, "上午")}, baseline=before)
    assert svc.get_leaves(scope, "2026-09", "T1") == set()
    assert svc.build_day_input("2026-09").session_leaves[scope]["T1"] == {(d, "上午")}
    assert day_input_fingerprint(svc.build_day_input("2026-09")) != fingerprint
    # A second editor adds a day while the first editor removes its original slot.
    other = (date(2026, 9, 10), "下午")
    svc.set_trainee_leave_slots(scope, "2026-09", "T1", {other}, baseline=set())
    svc.set_trainee_leave_slots(scope, "2026-09", "T1", set(), baseline={(d, "上午")})
    assert svc.get_trainee_leave_slots(scope, "2026-09", "T1") == {other}
    assert svc.get_trainee_leave_slots(scope, "2026-10", "T1") == set()


def test_weekday_batch_changes_only_selected_period():
    days = {date(2026, 9, n): None for n in range(1, 31)}
    editor = SimpleNamespace(_buttons=days, _selected={(date(2026, 9, 3), "下午")},
                             _weekday=SimpleNamespace(get=lambda: "四"),
                             _periods=lambda: ("上午",), _refresh_buttons=lambda: None)
    SessionLeaveEditor._weekly(editor, True)
    assert editor._selected == {(date(2026, 9, n), "上午") for n in (3, 10, 17, 24)} | {
        (date(2026, 9, 3), "下午")}
    SessionLeaveEditor._weekly(editor, False)
    assert editor._selected == {(date(2026, 9, 3), "下午")}


@pytest.mark.parametrize("bad", [None, [], {"pgy": {}}, {"external": {"E": None}},
    {"family": {"F": ["2026-10-01|上午"]}}, {"family": {"F": ["2026-09-03|晚上"]}}])
def test_bad_leave_fails_closed(bad):
    with pytest.raises(ValueError):
        validate_authoritative_month("2026-09", {"session_leaves": bad})


def make_input():
    grid = {date(2026, 9, n): {"上午": ["101", "102"], "下午": ["101", "102"]}
            for n in range(1, 31) if date(2026, 9, n).weekday() < 5}
    return DaySolveInput("2026-09", grid, [], external_roster=["E"], family_roster=["F"],
        family_follow={"F": {(d, s) for d in grid for s in ("上午", "下午")}},
        session_leaves={scope: {p: {(d, "上午") for d in grid if d.weekday() == 3}}
                        for scope, p in (("external", "E"), ("family", "F"))})


def test_solver_excludes_all_work_but_keeps_other_half():
    inp = make_input()
    inp.leaves = {"external": {"E": {date(2026, 9, 4)}},
                  "family": {"F": {date(2026, 9, 4)}}}
    slots, _, warnings = month_solve_day(inp)
    for d in inp.grid:
        for p in ("E", "F"):
            if d.weekday() == 3:
                assert all(p not in ps for ps in slots[d.isoformat()]["上午"].values())
            if d.day == 4:
                assert all(p not in ps for cells in slots[d.isoformat()].values() for ps in cells.values())
        if d.weekday() == 3:
            assert any("F" in ps for ps in slots[d.isoformat()]["下午"].values())
    assert any("請假衝突" in w for w in warnings)
    assert sum("E" in cells.get("切片室", []) for ss in slots.values() for cells in ss.values()) == 2


@pytest.mark.parametrize("person", ["E", "F"])
@pytest.mark.parametrize("slot", ["101", "切片室", "放假"])
def test_locked_leave_conflict_is_explicit_and_not_overwritten(person, slot):
    inp = make_input()
    inp.locked = {"2026-09-03": {"上午": {slot: [person]}}}
    slots, _, warnings = month_solve_day(inp)
    assert slots["2026-09-03"]["上午"] == {slot: [person]}
    assert any("請假與既有保留排班衝突" in w for w in warnings)
    assert inp.locked == {"2026-09-03": {"上午": {slot: [person]}}}


def test_manual_leave_warning_and_candidates(svc):
    from cmuh_common.roster.ui.day_tab import _DayEditDialog
    svc.set_external_month_roster("2026-09", ["E"], baseline=[])
    d = date(2026, 9, 3)
    svc.set_trainee_leave_slots("external", "2026-09", "E", {(d, "上午")}, baseline=set())
    editor = SimpleNamespace(service=svc, ym="2026-09", d=d, session="上午")
    _, leaves = _DayEditDialog._load_candidates(editor)
    assert d in leaves["E"]
    svc.set_day_slot("2026-09", d, "上午", "101", ["E"])
    assert any("E" in w and "請假" in w for w in svc.quick_validate_day("2026-09"))
    editor.session = "下午"
    _, leaves = _DayEditDialog._load_candidates(editor)
    assert d not in leaves.get("E", set())


@pytest.mark.parametrize("scope", ["external", "family"])
@pytest.mark.parametrize("kind", ["top", "scope", "member"])
def test_supported_legacy_null_leave_maps(svc, scope, kind):
    value = None if kind == "top" else {scope: None if kind == "scope" else {"T": None}}
    svc.update_month("2026-09", lambda m: m.update(leaves=value))
    assert svc.get_trainee_leave_slots(scope, "2026-09", "T") == set()
    wanted = {(date(2026, 9, 3), "上午")}
    svc.set_trainee_leave_slots(scope, "2026-09", "T", wanted, baseline=set())
    assert svc.get_trainee_leave_slots(scope, "2026-09", "T") == wanted


def test_retroactive_leave_auto_preserved_session_does_not_block_future(svc):
    svc.set_external_month_roster("2026-09", ["E"], baseline=[])
    d = date(2026, 9, 3)
    svc.set_day_slot("2026-09", d, "上午", "放假", ["E"])
    svc.set_trainee_leave_slots("external", "2026-09", "E", {(d, "上午")}, baseline=set())
    inp = svc.build_day_input("2026-09", today=date(2026, 9, 4))
    assert not svc.storage.load_month("2026-09").get("day_locks")
    slots, _, warnings = month_solve_day(inp)
    assert slots[d.isoformat()]["上午"] == {"放假": ["E"]}
    assert any("請假與既有保留排班衝突" in w for w in warnings)
