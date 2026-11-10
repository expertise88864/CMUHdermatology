# -*- coding: utf-8 -*-
"""[2026-11-10 使用者] 人員固定第 3 步:Clerk 半天假、鎖定名單存檔、兩個按鈕。
匿名代號、暫存資料夾。"""
import ast
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.clinic_grid import month_grid  # noqa: E402
from cmuh_common.roster.model import ClerkBatch  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.session_leave import (  # noqa: E402
    clerk_leave_days, parse_session_leaves,
)
from cmuh_common.roster.solve_day import (  # noqa: E402
    REST, DaySolveInput, month_solve_day,
)
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"
DAY = date(2026, 8, 5)
TUE = date(2026, 8, 4)
_TEMPLATE = {str(w): {"上午": [{"room": "101"}, {"room": "102"}],
                      "下午": [{"room": "101"}, {"room": "102"}]} for w in range(5)}
_ROOT = os.path.join(os.path.dirname(__file__), "..", "src", "cmuh_common", "roster")


def _work(slots, d, s, code):
    cell = (slots.get(d.isoformat()) or {}).get(s) or {}
    return [slot for slot, ps in cell.items() if code in ps and slot != REST]


# ── Clerk 半天假 ─────────────────────────────────────────────────────────
def test_clerk_half_day_leave_parses():
    out = parse_session_leaves(YM, {"session_leaves": {"clerk": {"C1": ["2026-08-05|上午"]}}})
    assert out["clerk"]["C1"] == {(DAY, "上午")}


def test_solver_skips_only_the_clerks_leave_half_day():
    grid = month_grid(YM, _TEMPLATE, set())
    inp = DaySolveInput(
        ym=YM, grid=grid, pgy_roster=["P1", "P2"],
        clerk_batches=[ClerkBatch("b1", date(2026, 8, 3), ["C1", "C2", "C3"])],
        session_leaves={"clerk": {"C1": {(TUE, "上午")}}})
    slots, _log, _warnings = month_solve_day(inp)
    assert _work(slots, TUE, "上午", "C1") == []
    assert not any("C1" in ps for ps in slots[TUE.isoformat()]["上午"].values())
    # 只請上午:下午仍是可排的人(有位置或放假),不是整天請假
    assert any("C1" in ps for ps in slots[TUE.isoformat()]["下午"].values())


def test_half_day_leave_counts_as_not_full_attendance():
    grid = month_grid(YM, _TEMPLATE, set())
    inp = DaySolveInput(ym=YM, grid=grid, pgy_roster=["P1"],
                        leaves={"clerk": {"C2": {date(2026, 8, 6)}}},
                        session_leaves={"clerk": {"C1": {(DAY, "下午")}}})
    assert clerk_leave_days(inp) == {"C1": {DAY}, "C2": {date(2026, 8, 6)}}


# ── service:半天假寫入、鎖定名單、候選人 ─────────────────────────────────
def _svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": c} for c in ("P1", "P2")],
                    "r_members": [], "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": _TEMPLATE})
    st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03",
                            "members": ["C1", "C2", "C3"]}])
    st.save_biopsy_grid({})
    st.save_month(YM, {"day_slots": {}})
    return RosterService(st), st


def test_service_saves_a_clerk_half_day_leave(tmp_path):
    svc, st = _svc(tmp_path)
    svc.set_trainee_leave_slots("clerk", YM, "C1", {(DAY, "上午")}, baseline=set())
    assert svc.get_trainee_leave_slots("clerk", YM, "C1") == {(DAY, "上午")}
    inp = svc.build_day_input(YM)
    assert inp.session_leaves["clerk"]["C1"] == {(DAY, "上午")}


def test_person_locks_are_saved_and_merged(tmp_path):
    svc, _st = _svc(tmp_path)
    assert svc.get_person_locks(YM) == []
    assert svc.set_person_locks(YM, ["C1", "P1"], baseline=[]) == ["C1", "P1"]
    # 他機在這段期間加了 C2;本機只拿掉 P1 → 合併後保留 C2
    svc.set_person_locks(YM, ["C1", "C2", "P1"], baseline=["C1", "P1"])
    assert svc.set_person_locks(YM, ["C1"], baseline=["C1", "P1"]) == ["C1", "C2"]


def test_person_locks_refuse_a_finalized_month(tmp_path):
    svc, st = _svc(tmp_path)
    month = st.load_month(YM)
    month["finalized"] = True
    st.save_month(YM, month)
    from cmuh_common.roster.service import FinalizedMonthError
    with pytest.raises(FinalizedMonthError):
        svc.set_person_locks(YM, ["C1"], baseline=[])


def test_held_candidates_group_people_and_find_future_leave(tmp_path):
    svc, _st = _svc(tmp_path)
    svc.set_trainee_leave_slots("clerk", YM, "C2", {(date(2026, 8, 12), "下午")},
                                baseline=set())
    cand = svc.held_candidates(YM, today=DAY)
    groups = dict(cand["groups"])
    assert groups["PGY"] == ["P1", "P2"] and groups["Clerk"] == ["C1", "C2", "C3"]
    assert cand["on_leave"] == ["C2"]
    assert svc.held_candidates(YM, today=date(2026, 8, 20))["on_leave"] == []


# ── 介面接線(原始碼守門;Tk 視窗本身不在 CI 開)────────────────────────
def _fn(name):
    src = open(os.path.join(_ROOT, "ui", "day_tab.py"), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(name)


def test_both_buttons_run_the_solver_with_their_spec():
    assert "HeldSpec(HELD_PEOPLE" in _fn("_on_held_people")
    assert "set_person_locks" in _fn("_on_held_people")
    assert "HeldSpec(HELD_LOCAL" in _fn("_on_held_local")
    assert '{"held": held}' in _fn("_solve_day_async")
    assert '"clerk", "external", "family"' in _fn("_on_leave")


def test_plain_auto_schedule_ignores_the_saved_locks():
    """一般「自動排班」不看勾選名單(使用者定案:只有專用按鈕才生效)。"""
    assert "person_locks" not in _fn("_on_auto")
    assert "held=None" in _fn("_on_auto")


def test_the_attendance_stage_itself_skips_the_leave_half_day():
    """後段 CP-SAT 會逐半天看請假、把結果修回來 —— 前段若照樣排他,公平計數
    就會算進一次不存在的工作。前段本身也要避開。"""
    from cmuh_common.roster.solve_day import _month_solve_attendance
    grid = month_grid(YM, _TEMPLATE, set())
    inp = DaySolveInput(
        ym=YM, grid=grid, pgy_roster=["P1", "P2"],
        clerk_batches=[ClerkBatch("b1", date(2026, 8, 3), ["C1", "C2", "C3"])],
        session_leaves={"clerk": {"C1": {(d, s) for d in grid if d.weekday() == 1
                                         for s in ("上午",)}}})
    slots, _log, _warnings = _month_solve_attendance(inp)
    for d in grid:
        if d.weekday() == 1:
            assert not any("C1" in ps for ps in slots[d.isoformat()]["上午"].values()), d
