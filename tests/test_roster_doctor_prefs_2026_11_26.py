# -*- coding: utf-8 -*-
"""[2026-11-26 使用者] 指定某人多跟某位醫師(匿名代號與虛構醫師 A／B)。"""
from collections import Counter
from copy import deepcopy
from datetime import date

import pytest

from cmuh_common.roster import doctor_prefs as dp
from cmuh_common.roster.course_balance import balance_rooms
from cmuh_common.roster.doctor_follow import DoctorFollowRow
from cmuh_common.roster.doctor_prefs import DoctorPref
from cmuh_common.roster.model import ClerkBatch
from cmuh_common.roster.solve_day import BIOPSY, REST, DaySolveInput, day_input_fingerprint, is_follow_slot
from cmuh_common.roster.storage import validate_authoritative_month

DAYS = [date(2026, 9, n) for n in (7, 8, 9, 10)]


def doctor_counts(inp, slots, person):
    return Counter(inp.clinic_doctors[date.fromisoformat(iso)][s][r]
                   for iso, ss in slots.items() for s, cells in ss.items()
                   for r, people in cells.items() if is_follow_slot(r) and person in people)


def follows(slots, person):
    return sum(person in ps for ss in slots.values() for cells in ss.values()
               for r, ps in cells.items() if is_follow_slot(r))


def one_person(scope, rooms):
    """X 一人;101/102 是 A 醫師、103 是 B 醫師;rooms[i] 是第 i 天 X 的診間。"""
    inp = DaySolveInput("2026-09", {d: {"上午": ["101", "102", "103"]} for d in DAYS}, [])
    period = "B1" if scope == "clerk" else "2026-09"
    if scope == "clerk":
        inp.clerk_batches = [ClerkBatch("B1", DAYS[0], ["X"])]
    else:
        setattr(inp, scope + "_roster", ["X"])
    inp.family_follow = {"X": {(d, "上午") for d in DAYS}} if scope == "family" else {}
    inp.clinic_doctors = {d: {"上午": {"101": "A", "102": "A", "103": "B"}} for d in DAYS}
    slots = {d.isoformat(): {"上午": {r: ["X"]}} for d, r in zip(DAYS, rooms, strict=True)}
    return inp, slots, period


@pytest.mark.parametrize("scope", ["pgy", "clerk", "external", "family"])
def test_a_blank_target_keeps_the_preferred_doctor(scope):
    inp, slots, period = one_person(scope, ["101", "102", "101", "102"])
    plain = deepcopy(slots)
    balance_rooms(inp, plain)
    assert doctor_counts(inp, plain, "X") == {"A": 2, "B": 2}, "沒有指定 → 原本的平均醫師"
    inp.doctor_prefs = (DoctorPref(scope, period, "X", "A"),)
    balance_rooms(inp, slots)
    assert doctor_counts(inp, slots, "X") == {"A": 4}
    assert follows(slots, "X") == 4


@pytest.mark.parametrize("scope", ["pgy", "external"])
def test_someone_who_followed_others_keeps_one(scope):
    inp, slots, period = one_person(scope, ["101", "103", "101", "103"])
    inp.doctor_prefs = (DoctorPref(scope, period, "X", "A"),)
    balance_rooms(inp, slots)
    assert doctor_counts(inp, slots, "X") == {"A": 3, "B": 1}


def test_a_target_is_enough_and_never_undercut():
    inp, slots, period = one_person("external", ["101", "102", "101", "102"])
    inp.doctor_prefs = (DoctorPref("external", period, "X", "A", 3),)
    balance_rooms(inp, slots)
    assert doctor_counts(inp, slots, "X") == {"A": 3, "B": 1}
    inp, slots, period = one_person("external", ["103", "103", "103", "103"])
    inp.doctor_prefs = (DoctorPref("external", period, "X", "A", 2),)
    balance_rooms(inp, slots)
    assert doctor_counts(inp, slots, "X") == {"A": 2, "B": 2}, "做到目標就停"


def contest(c_scope, e_target=2):
    """兩人各一天一診,101＝A、102＝B,容量 1;開始時 C 在 B、E 在 A。"""
    days = [date(2026, 9, n) for n in (7, 8, 9)]
    inp = DaySolveInput("2026-09", {d: {"上午": ["101", "102"]} for d in days}, [], capacity=1)
    inp.external_roster = ["E"]
    period = "2026-09"
    if c_scope == "clerk":
        inp.clerk_batches = [ClerkBatch("B1", days[0], ["C"])]
        period = "B1"
    else:
        inp.external_roster = ["C", "E"]
    inp.clinic_doctors = {d: {"上午": {"101": "A", "102": "B"}} for d in days}
    slots = {d.isoformat(): {"上午": {"101": ["E"], "102": ["C"]}} for d in days}
    inp.doctor_prefs = (DoctorPref(c_scope, period, "C", "A", 2),
                        DoctorPref("external", "2026-09", "E", "A", e_target))
    return inp, slots


def test_higher_priority_wins_but_the_other_keeps_the_doctor():
    inp, slots = contest("clerk", None)
    balance_rooms(inp, slots)
    assert doctor_counts(inp, slots, "C")["A"] == 2, "Clerk ＞ 外訓"
    assert doctor_counts(inp, slots, "E")["A"] == 1, "被換走的人不可少跟一位不同醫師"
    inp, slots = contest("clerk", 2)
    balance_rooms(inp, slots)
    assert doctor_counts(inp, slots, "E")["A"] == 2, "外訓已達標 2 次,優先順序也不能換到目標以下"
    assert doctor_counts(inp, slots, "C")["A"] == 1
    inp, slots = contest("external")
    balance_rooms(inp, slots)
    assert sorted(doctor_counts(inp, slots, p)["A"] for p in "CE") == [1, 2], \
        "同類別:平方和最小 → 兩人一起靠近目標,不是一人全拿"


def test_same_day_am_pm_swap_and_family_stays():
    days = [date(2026, 9, 7), date(2026, 9, 8)]
    inp = DaySolveInput("2026-09", {d: {"上午": ["101"], "下午": ["101"]} for d in days}, [],
                        capacity=1)
    inp.clinic_doctors = {d: {"上午": {"101": "A"}, "下午": {"101": "B"}} for d in days}
    for scope in ("external", "family"):
        setattr(inp, scope + "_roster", ["X"])
        inp.family_follow = {"X": {(d, "下午") for d in days}} if scope == "family" else {}
        slots = {d.isoformat(): {"上午": {REST: ["X"]}, "下午": {"101": ["X"]}} for d in days}
        inp.doctor_prefs = (DoctorPref(scope, "2026-09", "X", "A"),)
        balance_rooms(inp, slots)
        # 外訓:一天改上午跟 A,另一天保留 B(至少留一位其他醫師);家醫科不能換半天
        want = {"A": 1, "B": 1} if scope == "external" else {"B": 2}
        assert doctor_counts(inp, slots, "X") == want
        setattr(inp, scope + "_roster", [])


@pytest.mark.parametrize("guard", ["locked", "held", "apply"])
def test_locked_held_and_apply_seats_do_not_move(guard):
    inp, slots, period = one_person("pgy", ["103", "103", "103", "103"])
    inp.pgy_roster = ["X"]
    if guard == "locked":
        inp.locked = deepcopy(slots)
    elif guard == "held":
        inp.held = deepcopy(slots)
        inp.held_people = {iso: {"上午": ["X"]} for iso in slots}
    else:
        inp.clinic_doctors = {d: {"上午": {"101": "A", "102": "A", "103": "B"}} for d in DAYS}
        days = [date(2026, 9, 8), date(2026, 9, 11)]   # 週二、週五
        inp.grid = {d: {"上午": ["101", "102"]} for d in days}
        inp.clinic_doctors = {d: {"上午": {"101": "B", "102": "A"}} for d in days}
        inp.apply_pref = {"X"}
        slots = {d.isoformat(): {"上午": {"101": ["X"]}} for d in days}
    before = deepcopy(slots)
    inp.doctor_prefs = (DoctorPref("pgy", period, "X", "A"),)
    balance_rooms(inp, slots)
    assert slots == before


def test_report_explains_a_missed_target():
    inp, slots, period = one_person("external", ["103", "103", "103", "103"])
    inp.clinic_doctors = {d: {"上午": {"101": "A" if i == 0 else "B", "102": "B", "103": "B"}}
                          for i, d in enumerate(DAYS)}
    inp.doctor_prefs = (DoctorPref("external", period, "X", "A", 3),
                        DoctorPref("external", period, "Y", "A"),
                        DoctorPref("external", period, "X", "Z", 1))
    balance_rooms(inp, slots)
    text = "\n".join(dp.pref_warnings(inp, slots))
    assert "目標 3、實際 1" in text and "最多只能換到 1 次" in text
    assert "Y 已不在本月名單" in text and "Z 在這段期間的門診表沒有門診" in text


def test_no_preferences_means_no_change_and_the_field_is_fingerprinted():
    inp, slots, _ = one_person("pgy", ["101", "102", "101", "102"])
    assert dp.pref_warnings(inp, slots) == []
    before = day_input_fingerprint(inp)
    inp.doctor_prefs = (DoctorPref("pgy", "2026-09", "X", "A"),)
    assert day_input_fingerprint(inp) != before


# ── 月檔格式、Clerk 沿用 ──
def month(rows=(), clerk=None):
    block = {}
    if rows:
        block["month"] = list(rows)
    if clerk is not None:
        block["clerk"] = clerk
    return {"doctor_prefs": block} if block else {}


def test_clerk_follows_the_batch_across_months():
    prev = month(clerk={"B1": [{"code": "C", "doctor": "A", "target": 2}],
                        "B0": [{"code": "D", "doctor": "A", "target": None}]})
    cur = month([{"scope": "external", "code": "E", "doctor": "B", "target": None}])
    got = dp.effective_prefs("2026-10", cur, prev, ["B1"])
    assert got == (DoctorPref("clerk", "B1", "C", "A", 2),
                   DoctorPref("external", "2026-10", "E", "B"))
    cleared = month(clerk={"B1": []})
    assert dp.effective_prefs("2026-10", cleared, prev, ["B1"]) == (), "本月清空 → 不再沿用"


def test_editing_writes_only_what_changed():
    prev_clerk = (DoctorPref("clerk", "B1", "C", "A", 2),)
    m = {}
    dp.apply_edit(m, "2026-10", prev_clerk, prev_clerk, ["B1"])
    assert m == {}, "沒改的 Clerk 設定照樣跟著上個月,不寫鍵"
    dp.apply_edit(m, "2026-10", (DoctorPref("pgy", "2026-10", "P", "A", 3),), prev_clerk, ["B1"])
    assert m["doctor_prefs"] == {
        "month": [{"scope": "pgy", "code": "P", "doctor": "A", "target": 3}],
        "clerk": {"B1": []}}
    dp.apply_edit(m, "2026-10", (), prev_clerk, ["B1"])
    assert m["doctor_prefs"] == {"clerk": {"B1": []}}


@pytest.mark.parametrize("bad", [
    {"doctor_prefs": []},
    {"doctor_prefs": {"other": []}},
    {"doctor_prefs": {"month": [{"scope": "clerk", "code": "X", "doctor": "A", "target": None}]}},
    {"doctor_prefs": {"month": [{"scope": "pgy", "code": " X", "doctor": "A", "target": None}]}},
    {"doctor_prefs": {"month": [{"scope": "pgy", "code": "X", "doctor": "A", "target": 0}]}},
    {"doctor_prefs": {"month": [{"scope": "pgy", "code": "X", "doctor": "A", "target": True}]}},
    {"doctor_prefs": {"month": [{"scope": "pgy", "code": "X", "doctor": "A", "target": 1}] * 2}},
    {"doctor_prefs": {"clerk": {"B1": [{"code": "X", "doctor": ""}]}}},
])
def test_a_bad_shape_is_rejected_before_scheduling(bad):
    with pytest.raises(ValueError):
        dp.parse_doctor_prefs(bad)
    with pytest.raises(ValueError):
        validate_authoritative_month("2026-09", bad)


def test_the_stats_page_shows_target_and_actual():
    row = DoctorFollowRow("外訓", "2026-09", "2026-09", "E", 5, 0, 0, 0, 0,
                          (("A", 3), ("B", 2)), 0, (), prefs=(("A", 4), ("B", None)))
    assert row.pref_text() == "A 3/4、B 2（盡量多）"


# ── 存檔、跨月沿用、整套排班 ──
def service(tmp_path):
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage
    svc = RosterService(RosterStorage(str(tmp_path)))
    svc.storage.save_config({"pgy_members": [], "r_members": [], "vs_members": []})
    for wd in range(5):
        svc.add_clinic_template_entry(wd, "上午", "101", "A")
        svc.add_clinic_template_entry(wd, "上午", "103", "B")
    svc.storage.save_clerk_batches([{"id": "B1", "start_monday": "2026-09-28",
                                     "members": ["C"]}])
    svc.set_external_month_roster("2026-09", ["X"], baseline=[])
    return svc


def test_saved_preferences_reach_the_solver_and_clerk_carries_over(tmp_path):
    svc = service(tmp_path)
    ctx = svc.doctor_pref_context("2026-09")
    assert ctx["baseline"] is None and ctx["inherited"] == ()
    prefs = (DoctorPref("external", "2026-09", "X", "A", 3), DoctorPref("clerk", "B1", "C", "B"))
    svc.set_doctor_prefs("2026-09", prefs, baseline=None, inherited=(), batch_ids=["B1"])
    assert svc.build_day_input("2026-09").doctor_prefs == (
        DoctorPref("clerk", "B1", "C", "B"), DoctorPref("external", "2026-09", "X", "A", 3))
    october = svc.doctor_pref_context("2026-10")
    assert october["inp"].doctor_prefs == (DoctorPref("clerk", "B1", "C", "B"),)
    assert october["inherited"] == (DoctorPref("clerk", "B1", "C", "B"),)
    with pytest.raises(ValueError, match="其他電腦"):
        svc.set_doctor_prefs("2026-09", (), baseline=None, inherited=(), batch_ids=["B1"])
    preview = svc.run_day_solve("2026-09")
    got = Counter(r for ss in preview.day_slots.values() for cells in ss.values()
                  for r, ps in cells.items() if "X" in ps and is_follow_slot(r))
    assert got["101"] >= 3, got
    assert not any("指定多跟醫師：外訓 X" in w for w in preview.warnings)


def test_the_dialog_lists_doctors_with_counts(tmp_path):
    from cmuh_common.roster.ui import doctor_prefs_dialog as dlg
    inp = service(tmp_path).build_day_input("2026-09")
    days = dlg.period_days(inp, "external", "2026-09")
    assert {d.month for d in days} == {9}
    sessions = dlg.doctor_sessions(inp, days)
    assert sessions["A"] == sessions["B"] == len([d for d in inp.grid if d.month == 9
                                                  and inp.grid[d].get("上午")])
    clerk_days = dlg.period_days(inp, "clerk", "B1")
    assert min(clerk_days) == date(2026, 9, 28) and max(clerk_days).month == 10
    assert dlg.people_of(inp, "clerk", "B1") == ["C"] and dlg.people_of(inp, "external", "") == ["X"]
    assert dlg.parse_target("") == (None, "") and dlg.parse_target("4") == (4, "")
    assert dlg.parse_target("0")[1] and dlg.parse_target("x")[1]


def test_the_dialog_adds_and_saves(tmp_path, tk_root, monkeypatch):
    from cmuh_common.roster.ui import doctor_prefs_dialog as dlg
    svc = service(tmp_path)
    monkeypatch.setattr(dlg.tk.Toplevel, "grab_set", lambda self: None)
    w = dlg.DoctorPrefsDialog(tk_root, svc, "2026-09")
    assert w.scope_var.get() == "外訓" and w.code_var.get() == "X"
    label = next(k for k, v in w._doctors.items() if v == "A")
    assert "診" in label and "最多換到" in label
    w.doctor_var.set(label)
    w.target_var.set("3")
    w._add()
    assert w.prefs == [DoctorPref("external", "2026-09", "X", "A", 3)]
    w.scope_var.set("Clerk")
    w._scope_changed()
    assert w.code_var.get() == "C"
    w.doctor_var.set(next(k for k, v in w._doctors.items() if v == "B"))
    w._add()
    assert len(w.tree.get_children()) == 2
    w._save()
    assert svc.build_day_input("2026-09").doctor_prefs == (
        DoctorPref("clerk", "B1", "C", "B"), DoctorPref("external", "2026-09", "X", "A", 3))


# ── PR 審查第 1 輪 ──
def test_a_second_preference_cannot_undercut_a_met_target():
    inp, slots, period = one_person("external", ["101", "102", "101", "102"])
    inp.doctor_prefs = (DoctorPref("external", period, "X", "A", 2),
                        DoctorPref("external", period, "X", "B", 2))
    slots = {d.isoformat(): {"上午": {r: ["X"]}} for d, r in zip(DAYS[:2], ["101", "102"], strict=True)}
    inp.grid = {d: inp.grid[d] for d in DAYS[:2]}
    balance_rooms(inp, slots)
    assert doctor_counts(inp, slots, "X") == {"A": 2}, "A 已達標 2 次,不可為了 B 換到 1 次"


def test_a_cross_month_clerk_doctor_is_not_reported_missing():
    days = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 1)]
    inp = DaySolveInput("2026-09", {d: {"上午": ["101"]} for d in days[:2]}, [],
                        clerk_batches=[ClerkBatch("B1", days[0], ["C"])])
    inp.clinic_doctors = {days[0]: {"上午": {"101": "B"}}, days[1]: {"上午": {"101": "B"}},
                          days[2]: {"上午": {"101": "A"}}}
    slots = {d.isoformat(): {"上午": {"101": ["C"]}} for d in days[:2]}
    inp.doctor_prefs = (DoctorPref("clerk", "B1", "C", "A"),)
    assert dp.pref_warnings(inp, slots) == []
    inp.doctor_prefs = (DoctorPref("clerk", "B1", "C", "Z"),)
    assert "沒有門診" in dp.pref_warnings(inp, slots)[0]


def test_the_upper_bound_needs_a_free_half_day():
    d = DAYS[0]
    inp = DaySolveInput("2026-09", {d: {"上午": ["101"], "下午": ["101"]}}, [], external_roster=["X"])
    inp.clinic_doctors = {d: {"上午": {"101": "B"}, "下午": {"101": "A"}}}
    pref = DoctorPref("external", "2026-09", "X", "A")
    busy = {d.isoformat(): {"上午": {"101": ["X"]}, "下午": {BIOPSY: ["X"]}}}
    free = {d.isoformat(): {"上午": {"101": ["X"]}, "下午": {REST: ["X"]}}}
    assert dp.possible_sessions(inp, busy, pref, {d}) == 0
    assert dp.possible_sessions(inp, free, pref, {d}) == 1


def test_the_dialog_list_and_baseline_come_from_one_read(tmp_path, monkeypatch):
    svc = service(tmp_path)
    svc.set_doctor_prefs("2026-09", (DoctorPref("external", "2026-09", "X", "A"),),
                         baseline=None, inherited=(), batch_ids=["B1"])
    real = svc.build_day_input

    def racing(ym, **kw):
        out = real(ym, **kw)
        if ym == "2026-09" and not racing.done:       # 他機在兩次讀取之間又加了一筆
            racing.done = True
            svc.update_month(ym, lambda m: m["doctor_prefs"]["month"].append(
                {"scope": "external", "code": "X", "doctor": "B", "target": None}))
        return out
    racing.done = False
    monkeypatch.setattr(svc, "build_day_input", racing)
    ctx = svc.doctor_pref_context("2026-09")
    assert {p.doctor for p in ctx["prefs"]} == {"A", "B"}
    assert len(ctx["baseline"]["month"]) == 2
