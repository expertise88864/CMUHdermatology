# -*- coding: utf-8 -*-
"""[2026-11-10 使用者] 人員固定:被固定的人原位不動,同一格其他人照常排。

兩個按鈕共用這一層(見 `roster/held.py`):
  * 只排未鎖定人員 —— 勾選的人整月固定;
  * 局部重排(請假)—— 只有請假那幾個半天與請假者本人可動。
本檔用匿名代號跑真的整月求解(含 CP-SAT 階段),釘住:
  1. 固定的人每一格都與原班表一致,也沒有被排到別的位置;
  2. 名額(診間容量、照光/治療室/切片室一人)扣掉固定的人之後仍守住;
  3. 固定的人後來請假 → 保留原班、列為衝突;
  4. 沒有固定人員時,結果與原本完全相同(行為不變);
  5. 有固定人員時,PGY 每週上限超出改列為未滿足條件,不中止;
  6. 總檢查真的會擋下被移動的固定人員。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.clinic_grid import month_grid  # noqa: E402
from cmuh_common.roster.held import (  # noqa: E402
    enforce_held, held_conflicts, held_violations,
)
from cmuh_common.roster.model import ClerkBatch  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, PHOTO, REST, STUDENT_SESSIONS, TREATMENT, DaySolveInput,
    is_follow_slot, month_solve_day,
)
from cmuh_common.roster.training_bands import verify_pgy_caps  # noqa: E402

_ROOMS = [{"room": "101"}, {"room": "102"}, {"room": "103"}]
_TEMPLATE = {str(wd): {"上午": list(_ROOMS), "下午": list(_ROOMS)} for wd in range(5)}
_YM = "2026-08"
_PGY = ["P1", "P2", "P3"]
_B1 = ClerkBatch("b1", date(2026, 8, 3), ["C1", "C2", "C3", "C4"])
_B2 = ClerkBatch("b2", date(2026, 8, 17), ["C5", "C6", "C7", "C8"])
_EXT = ["X1"]
_FAM = ["F1"]


def _grid():
    return month_grid(_YM, _TEMPLATE, set())


def _biopsy_open(grid):
    out = {}
    for b in (_B1, _B2):
        for d in grid:
            if b.covers(d) and d.weekday() in (0, 3):
                out.setdefault(b.id, {})[d.isoformat()] = {"上午": True}
    return out


def _input(**kw):
    grid = _grid()
    family_follow = {"F1": {(d, s) for d in grid if d.weekday() in (1, 3)
                            for s in STUDENT_SESSIONS}}
    base = dict(ym=_YM, grid=grid, pgy_roster=list(_PGY),
                clerk_batches=[_B1, _B2], biopsy_open=_biopsy_open(grid),
                external_roster=list(_EXT), family_roster=list(_FAM),
                family_follow=family_follow)
    base.update(kw)
    return DaySolveInput(**base)


def _keys(grid):
    return [(d.isoformat(), s) for d in sorted(grid) if d.weekday() < 5
            for s in STUDENT_SESSIONS]


def _hold(slots, codes, keys):
    """從一份班表取出 `codes` 在 `keys` 這些格的位置 → (held, held_people)。"""
    held, people = {}, {}
    codes = set(codes)
    for iso, s in keys:
        cell = (slots.get(iso) or {}).get(s) or {}
        part = {slot: [p for p in ps if p in codes]
                for slot, ps in cell.items() if any(p in codes for p in ps)}
        if part:
            held.setdefault(iso, {})[s] = part
        people.setdefault(iso, {})[s] = sorted(codes)
    return held, people


def _positions(slots, iso, s, code):
    cell = (slots.get(iso) or {}).get(s) or {}
    return sorted(slot for slot, ps in cell.items() if code in ps)


@pytest.fixture(scope="module")
def baseline():
    inp = _input()
    slots, _log, _warnings = month_solve_day(inp)
    return inp, slots


def test_held_people_stay_exactly_where_they_were(baseline):
    """只排未鎖定人員:勾選的人整月原位,其他人重排(這裡 C2 新請兩天假)。"""
    inp0, s0 = baseline
    held_codes = {"P1", "C1", "X1", "F1"}
    held, people = _hold(s0, held_codes, _keys(inp0.grid))
    leave = {"clerk": {"C2": {date(2026, 8, 5), date(2026, 8, 6)}}}
    inp = _input(held=held, held_people=people, leaves=leave)
    s1, _log, _warnings = month_solve_day(inp)
    assert held_violations(inp, s1) == []
    for iso, s in _keys(inp.grid):
        for code in held_codes:
            assert _positions(s1, iso, s, code) == _positions(s0, iso, s, code), (iso, s, code)
    # 沒被固定的人確實會被重排:請假那兩天 C2 不再出現在任何工作位置
    for d in (date(2026, 8, 5), date(2026, 8, 6)):
        for s in STUDENT_SESSIONS:
            work = [slot for slot in _positions(s1, d.isoformat(), s, "C2") if slot != REST]
            assert work == []


def test_capacity_and_single_duty_hold_with_held_people(baseline):
    inp0, s0 = baseline
    held, people = _hold(s0, {"P1", "C1", "C5", "X1"}, _keys(inp0.grid))
    inp = _input(held=held, held_people=people)
    s1, _log, _warnings = month_solve_day(inp)
    for iso, sessions in s1.items():
        d = date.fromisoformat(iso)
        for s, cell in sessions.items():
            rooms = (inp.grid.get(d) or {}).get(s) or []
            for r in rooms:
                assert len(cell.get(r, [])) <= inp.capacity, (iso, s, r, cell)
            for slot in (PHOTO, TREATMENT, BIOPSY):
                assert len(cell.get(slot, [])) <= 1, (iso, s, slot, cell)
            everyone = [p for slot, ps in cell.items() for p in ps]
            assert len(everyone) == len(set(everyone)), (iso, s, cell)


def test_a_held_photo_is_not_doubled(baseline):
    """固定人員已在照光 → 那一格不再排第二位照光;治療室也同理。"""
    inp0, s0 = baseline
    iso, s = "2026-08-04", "上午"
    photo = s0[iso][s][PHOTO][0]
    held = {iso: {s: {PHOTO: [photo]}}}
    inp = _input(held=held, held_people={iso: {s: [photo]}})
    s1, _log, _warnings = month_solve_day(inp)
    assert s1[iso][s][PHOTO] == [photo]
    assert _positions(s1, iso, s, photo) == [PHOTO]


def test_a_held_person_on_leave_keeps_the_shift_and_is_reported(baseline):
    inp0, s0 = baseline
    iso, s = "2026-08-04", "上午"
    who = next(p for slot, ps in s0[iso][s].items() if is_follow_slot(slot)
               for p in ps if p in _B1.members)
    held, people = _hold(s0, {who}, _keys(inp0.grid))
    inp = _input(held=held, held_people=people,
                 leaves={"clerk": {who: {date(2026, 8, 4)}}})
    s1, _log, warnings = month_solve_day(inp)
    assert _positions(s1, iso, s, who) == _positions(s0, iso, s, who)
    assert any("📌固定人員" in w and who in w and "請假" in w for w in warnings)
    assert held_conflicts(inp)


def test_no_held_people_means_no_change(baseline):
    inp0, s0 = baseline
    s1, _log, _warnings = month_solve_day(_input(held={}, held_people={}))
    assert s1 == s0


def test_local_resolve_only_changes_the_absent_persons_days(baseline):
    """局部重排:C3 請兩天假 → 只有那兩天的半天可動,其他半天除了 C3 本人
    補次數之外全部不動。"""
    inp0, s0 = baseline
    absent = "C3"
    leave_days = {date(2026, 8, 10), date(2026, 8, 11)}
    free = {(d.isoformat(), s) for d in leave_days for s in STUDENT_SESSIONS}
    everyone = set(_PGY) | set(_B1.members) | set(_B2.members) | set(_EXT) | set(_FAM)
    held, people = {}, {}
    for iso, s in _keys(inp0.grid):
        if (iso, s) in free:
            continue
        h, pp = _hold(s0, everyone - {absent}, [(iso, s)])
        for k, v in h.items():
            held.setdefault(k, {}).update(v)
        for k, v in pp.items():
            people.setdefault(k, {}).update(v)
    inp = _input(held=held, held_people=people,
                 leaves={"clerk": {absent: set(leave_days)}})
    s1, _log, _warnings = month_solve_day(inp)
    assert held_violations(inp, s1) == []
    for iso, s in _keys(inp.grid):
        if (iso, s) in free:
            assert [x for x in _positions(s1, iso, s, absent) if x != REST] == []
            continue
        for code in everyone - {absent}:
            assert _positions(s1, iso, s, code) == _positions(s0, iso, s, code)


def test_pgy_cap_excess_is_reported_not_raised():
    grid = _grid()
    inp = _input()
    week_days = [d for d in sorted(grid) if d.isocalendar()[1] == date(2026, 8, 3).isocalendar()[1]]
    slots = {d.isoformat(): {"上午": {"101": ["P1"]}, "下午": {"102": ["P1"]}}
             for d in week_days}
    with pytest.raises(ValueError):
        verify_pgy_caps(inp, slots)
    problems = verify_pgy_caps(inp, slots, report_only=True)
    assert problems and "P1" in problems[0] and "超過上限" in problems[0]


def test_the_final_check_catches_a_moved_held_person():
    inp = _input(held={"2026-08-04": {"上午": {"101": ["C1"]}}},
                 held_people={"2026-08-04": {"上午": ["C1"]}})
    moved = {"2026-08-04": {"上午": {"102": ["C1"]}}}
    assert held_violations(inp, moved)
    with pytest.raises(RuntimeError):
        enforce_held(inp, moved)
    kept = {"2026-08-04": {"上午": {"101": ["C1", "C2"]}}}
    enforce_held(inp, kept)


def test_a_whole_session_lock_wins_over_held():
    """同一格既整格鎖定又有人員固定 → 以鎖定為準,固定不重複計入。"""
    locked = {"2026-08-04": {"上午": {PHOTO: ["P2"], "101": ["C1"]}}}
    inp = _input(locked=locked,
                 held={"2026-08-04": {"上午": {"103": ["C1"]}}},
                 held_people={"2026-08-04": {"上午": ["C1"]}})
    s1, _log, _warnings = month_solve_day(inp)
    assert s1["2026-08-04"]["上午"] == locked["2026-08-04"]["上午"]
    assert held_violations(inp, s1) == []


def _first_two_weeks(grid):
    return [d for d in sorted(grid) if d.weekday() < 5 and d < date(2026, 8, 17)]


def test_pgy_balance_does_not_undo_a_held_photo():
    """P1 被固定在前兩週所有照光 → PGY 平衡會想把照光分給別人;固定的不可以動。"""
    grid = _grid()
    held, people = {}, {}
    for d in _first_two_weeks(grid):
        for s in STUDENT_SESSIONS:
            held.setdefault(d.isoformat(), {})[s] = {PHOTO: ["P1"]}
            people.setdefault(d.isoformat(), {})[s] = ["P1"]
    inp = _input(held=held, held_people=people)
    s1, _log, _warnings = month_solve_day(inp)
    assert held_violations(inp, s1) == []
    for d in _first_two_weeks(grid):
        for s in STUDENT_SESSIONS:
            assert s1[d.isoformat()][s][PHOTO] == ["P1"]


def test_room_diversity_does_not_move_a_held_clerk():
    """C1 被固定在 101 → 跟診醫師/診間多樣性會想讓他換診;固定的不可以動。"""
    grid = _grid()
    held, people = {}, {}
    for d in _first_two_weeks(grid):
        if not _B1.covers(d):
            continue
        for s in STUDENT_SESSIONS:
            if "101" in (grid.get(d) or {}).get(s, []):
                held.setdefault(d.isoformat(), {})[s] = {"101": ["C1"]}
                people.setdefault(d.isoformat(), {})[s] = ["C1"]
    doctors = {d: {s: {r["room"]: f"Dr{r['room']}" for r in _ROOMS}
                   for s in STUDENT_SESSIONS} for d in grid}
    inp = _input(held=held, held_people=people, clinic_doctors=doctors)
    s1, _log, _warnings = month_solve_day(inp)
    assert held_violations(inp, s1) == []


def test_held_on_a_day_that_is_no_longer_scheduled_is_ignored():
    """固定的日子後來變成假日 → 那天不排班,固定紀錄忽略,不中止也不誤報被移動。"""
    held = {"2026-08-04": {"上午": {"101": ["C1"]}}}
    people = {"2026-08-04": {"上午": ["C1"]}}
    grid = month_grid(_YM, _TEMPLATE, {date(2026, 8, 4)})
    inp = _input(grid=grid, held=held, held_people=people)
    s1, _log, _warnings = month_solve_day(inp)
    assert "2026-08-04" not in s1 or not s1["2026-08-04"]
    assert held_violations(inp, s1) == []


def test_a_held_seat_in_a_closed_room_is_kept_and_reported():
    iso, s = "2026-08-04", "上午"
    held = {iso: {s: {"105": ["C1"]}}}
    inp = _input(held=held, held_people={iso: {s: ["C1"]}})
    s1, _log, warnings = month_solve_day(inp)
    assert s1[iso][s].get("105") == ["C1"]
    assert any("未開診" in w and "C1" in w for w in warnings)


def test_biopsy_lookahead_knows_a_future_held_half_day():
    """[PR #18 審查 P2] 只開兩個切片時段(週一早、週二早),兩人各 1 次;"1" 週二早
    被固定在放假 —— 週一那一格若給了 "6",週二就沒有人能切(6 已用完、1 被固定)。
    抖動偏好 "6",所以前瞻若把被固定的人當成還排得到,就會挑錯人。"""
    from test_roster_clerk_biopsy_quota_2026_08_24 import MON, _counts
    from test_roster_clerk_biopsy_quota_2026_08_24 import _grid as _bgrid
    from datetime import timedelta
    grid, _bio = _bgrid(MON, 12, ["101", "102"], ["101", "102"])
    tue = (MON + timedelta(days=1)).isoformat()
    bio = {"b1": {MON.isoformat(): {"上午": True}, tue: {"上午": True}}}
    day_slots, _log, warns = month_solve_day(DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=["P1"],
        clerk_batches=[ClerkBatch("b1", MON, ["1", "6"])], biopsy_open=bio,
        held={tue: {"上午": {REST: ["1"]}}}, held_people={tue: {"上午": ["1"]}}))
    _seat, bx, _rest = _counts(day_slots)
    assert (bx.get("1", 0), bx.get("6", 0)) == (1, 1), f"{bx} / {warns}"


def test_the_final_check_catches_a_duplicated_held_person():
    """[PR #18 審查 P2] 同一格出現兩次也是被改動,不可以只看「在不在」。"""
    inp = _input(held={"2026-08-04": {"上午": {"101": ["C1"]}}},
                 held_people={"2026-08-04": {"上午": ["C1"]}})
    doubled = {"2026-08-04": {"上午": {"101": ["C1", "C1"]}}}
    with pytest.raises(RuntimeError):
        enforce_held(inp, doubled)
