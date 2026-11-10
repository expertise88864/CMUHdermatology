# -*- coding: utf-8 -*-
"""[2026-11-10 使用者] 人員固定接到 service:兩個按鈕的固定範圍、預覽變動清單、
指紋一致(求解/預覽/套用用同一份範圍重建輸入)。匿名代號、暫存資料夾。"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.held import (  # noqa: E402
    HELD_LOCAL, HELD_PEOPLE, describe_changes, held_from_spec,
)
from cmuh_common.roster.service import HeldSpec, RosterService  # noqa: E402
from cmuh_common.roster.solve_day import REST, STUDENT_SESSIONS  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"
TODAY = date(2026, 8, 5)
LEAVE_DAY = date(2026, 8, 12)


def _svc(tmp_path, *, leaves=None):
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": c} for c in ("P1", "P2", "P3")],
                    "r_members": [], "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}, {"room": "102"}],
                 "下午": [{"room": "101"}, {"room": "102"}]} for w in range(5)}})
    st.save_clerk_batches([
        {"id": "b1", "start_monday": "2026-08-03", "members": ["C1", "C2", "C3"]},
        {"id": "b2", "start_monday": "2026-08-17", "members": ["C4", "C5", "C6"]}])
    st.save_biopsy_grid({})
    month = {"day_slots": {}}
    if leaves:
        month["leaves"] = leaves
    st.save_month(YM, month)
    svc = RosterService(st)
    first = svc.run_day_solve(YM, today=TODAY)
    svc.accept_day_solution(YM, first.day_slots, "", expect=first, today=TODAY)
    return svc, st


def _future_keys(svc):
    inp = svc.build_day_input(YM, today=TODAY)
    return [(d.isoformat(), s) for d in sorted(inp.grid) if d > TODAY
            for s in STUDENT_SESSIONS]


def _positions(slots, iso, s, code):
    return sorted(slot for slot, ps in ((slots.get(iso) or {}).get(s) or {}).items()
                  if code in ps)


def test_only_unlocked_people_keeps_the_ticked_ones(tmp_path):
    svc, st = _svc(tmp_path)
    before = st.load_month(YM)["day_slots"]
    spec = HeldSpec(HELD_PEOPLE, ("C1", "P1"))
    res = svc.run_day_solve(YM, today=TODAY, held=spec)
    assert res.held == spec
    for iso, s in _future_keys(svc):
        for code in ("C1", "P1"):
            assert _positions(res.day_slots, iso, s, code) == _positions(before, iso, s, code)
    assert svc.day_solution_is_current(YM, res, today=TODAY)
    analysis = svc.day_preview_analysis(YM, res.day_slots, expect=res, today=TODAY)
    assert isinstance(analysis["changes"], list)
    assert not any(" C1：" in c or " P1：" in c for c in analysis["changes"])
    svc.accept_day_solution(YM, res.day_slots, "", expect=res, today=TODAY)


def test_editing_a_held_persons_shift_makes_the_preview_stale(tmp_path):
    svc, st = _svc(tmp_path)
    res = svc.run_day_solve(YM, today=TODAY, held=HeldSpec(HELD_PEOPLE, ("C1",)))
    month = st.load_month(YM)
    iso, s = next((i, ss) for i, ss in _future_keys(svc)
                  if _positions(month["day_slots"], i, ss, "C1"))
    cell = month["day_slots"][iso][s]
    for slot in list(cell):
        cell[slot] = [p for p in cell[slot] if p != "C1"]
    cell.setdefault(REST, []).append("C1")
    st.save_month(YM, month)
    assert not svc.day_solution_is_current(YM, res, today=TODAY)
    with pytest.raises(ValueError):
        svc.accept_day_solution(YM, res.day_slots, "", expect=res, today=TODAY)


def test_local_resolve_only_moves_the_leave_days(tmp_path):
    svc, st = _svc(tmp_path)
    before = st.load_month(YM)["day_slots"]
    month = st.load_month(YM)
    month["leaves"] = {"clerk": {"C2": [LEAVE_DAY.isoformat()]}}
    st.save_month(YM, month)
    res = svc.run_day_solve(YM, today=TODAY, held=HeldSpec(HELD_LOCAL, ("C2",)))
    everyone = {"P1", "P2", "P3", "C1", "C3", "C4", "C5", "C6"}
    for iso, s in _future_keys(svc):
        if iso == LEAVE_DAY.isoformat():
            assert [x for x in _positions(res.day_slots, iso, s, "C2") if x != REST] == []
            continue
        for code in everyone:
            assert _positions(res.day_slots, iso, s, code) == _positions(before, iso, s, code)


def test_held_spec_validation(tmp_path):
    svc, _st = _svc(tmp_path)
    inp = svc.build_day_input(YM, today=TODAY)
    with pytest.raises(ValueError):
        held_from_spec(inp, {}, HELD_PEOPLE, ("NOBODY",))
    with pytest.raises(ValueError):
        held_from_spec(inp, {}, HELD_PEOPLE, ())
    with pytest.raises(ValueError):
        held_from_spec(inp, {}, "whatever", ("C1",))


def test_held_never_covers_past_or_locked_sessions(tmp_path):
    svc, st = _svc(tmp_path)
    inp = svc.build_day_input(YM, today=TODAY)
    held, people = held_from_spec(inp, st.load_month(YM)["day_slots"],
                                  HELD_PEOPLE, ("C1",))
    assert all(iso > TODAY.isoformat() for iso in people)
    assert all(iso > TODAY.isoformat() for iso in held)


def test_describe_changes_lists_only_people_who_moved():
    before = {"2026-08-12": {"上午": {"101": ["C1", "C2"], REST: ["C3"]}}}
    after = {"2026-08-12": {"上午": {"101": ["C1"], "102": ["C3"], REST: ["C2"]}}}
    lines = describe_changes(before, after)
    assert lines == ["8/12(三) 上午 C2：101 → 放假",
                     "8/12(三) 上午 C3：放假 → 102"]
