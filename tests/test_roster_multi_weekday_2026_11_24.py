# -*- coding: utf-8 -*-
"""[2026-11-24 使用者] R 值班星期可複選:兩天以上【盡量】排同一人,硬規則與點數平衡
優先;只選一天維持原本的硬性固定。匿名代號、暫存資料夾。"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
pytest.importorskip("ortools", reason="ortools 未安裝（自動排班引擎）")

from cmuh_common.roster.calendar_colors import week_colors_for_year  # noqa: E402
from cmuh_common.roster.model import Member, SolveContext  # noqa: E402
from cmuh_common.roster.report import build_report  # noqa: E402
from cmuh_common.roster.rules import (  # noqa: E402
    ConsecutiveDutyRule, DutyCountBalanceRule, PreferredWeekdaysRule)
from cmuh_common.roster.solve_rvs import rvs_input_fingerprint, solve_duty  # noqa: E402
from test_roster_solve import make_ctx  # noqa: E402

WED_THU = [d for d in (date(2026, 8, n) for n in range(1, 32)) if d.weekday() in (2, 3)]


def members(first=(2, 3), **kw):
    return [Member("r1", "甲", "R1", fixed_weekdays=first, **kw),
            Member("r2", "乙", "R2"), Member("r3", "丙", "R3")]


def dev(ctx, res):
    return sum(abs(res.points_by_person[m.id] - res.targets[m.id]) for m in ctx.members)


def got(res, mid="r1", days=WED_THU):
    return sum(res.assignments[d] == mid for d in days)


def test_preferred_weekdays_fill_first_without_hurting_points():
    base_ctx = make_ctx(members=[Member(i, i, lv) for i, lv in
                                 (("r1", "R1"), ("r2", "R2"), ("r3", "R3"))])
    base = solve_duty(base_ctx)
    ctx = make_ctx(members=members())
    res = solve_duty(ctx)
    assert base.status == res.status == "ok"
    assert dev(ctx, res) == pytest.approx(dev(base_ctx, base))   # 點數一樣平
    assert got(res) == len(WED_THU) == 8 and got(base) < 8
    assert all(9 <= res.duty_counts[m.id] <= 11 for m in ctx.members)
    assert all(1 <= sum(res.assignments[date(2026, 8, s)] == m.id
                        for s in (1, 8, 15, 22, 29)) <= 2 for m in ctx.members)
    assert res.duty_counts["r1"] > 8                  # 點數不夠 → 再排其他天
    assert any(res.reasons[d] == "指定星期" for d in WED_THU)


def test_preference_outranks_points_and_the_gap_goes_to_the_ledger():
    """[2026-11-26 使用者選 (A)] 指定星期優先於點數:帳本已超前(+6)也照樣排滿,
    點數差額進帳本;硬規則(班數上限 12、每人 1–2 個週末)照舊。"""
    ledger = {"r1": 6.0, "r2": -3.0, "r3": -3.0}
    ctx = make_ctx(members=members(), ledger=ledger)
    free = make_ctx(members=[Member(i, i, lv) for i, lv in
                             (("r1", "R1"), ("r2", "R2"), ("r3", "R3"))], ledger=ledger)
    res, base = solve_duty(ctx), solve_duty(free)
    assert res.status == base.status == "ok"
    assert got(res) == len(WED_THU) and got(base) < len(WED_THU)
    assert dev(ctx, res) > dev(free, base)            # 點數讓步,差額進帳本
    assert res.duty_counts["r1"] <= 12
    assert all(1 <= sum(res.assignments[date(2026, 8, s)] == m.id
                        for s in (1, 8, 15, 22, 29)) <= 2 for m in ctx.members)


def test_preference_never_breaks_the_hard_duty_cap():
    """五天全選、帳本超前:指定星期多於上限 → 停在硬上限 12 班,不會更多。"""
    ctx = make_ctx(members=members(first=(0, 1, 2, 3, 4)),
                   ledger={"r1": 6.0, "r2": -3.0, "r3": -3.0})
    res = solve_duty(ctx)
    assert res.status == "ok"
    assert res.duty_counts["r1"] == 12
    assert all(9 <= res.duty_counts[m.id] for m in ctx.members)


def test_leave_and_must_duty_still_win():
    ctx = make_ctx(members=members(), leaves={"r1": {date(2026, 8, 5)}},
                   must={"r2": {date(2026, 8, 6)}})
    res = solve_duty(ctx)
    assert res.status == "ok"
    assert res.assignments[date(2026, 8, 5)] != "r1"
    assert res.assignments[date(2026, 8, 6)] == "r2"
    msgs = [c.msg for c in res.prechecks if c.rule_id == "preferred_weekdays"]
    assert any("8/5" in m and "請假" in m for m in msgs) and any("8/6" in m for m in msgs)


def test_a_single_fixed_weekday_of_someone_else_stays_hard():
    ms = members()
    ms[1] = Member("r2", "乙", "R2", fixed_weekday=3)
    res = solve_duty(make_ctx(members=ms))
    assert res.status == "ok"
    assert all(res.assignments[d] == "r2" for d in WED_THU if d.weekday() == 3)
    assert all(res.assignments[d] == "r1" for d in WED_THU if d.weekday() == 2)
    assert any("固定值班日" in c.msg for c in res.prechecks
               if c.rule_id == "preferred_weekdays")
    # [PR #29 審查] 別人的固定日不算「可排」;對方請假那天才算
    assert "可排 4 天，實排 4 天" in build_report(make_ctx(members=ms), res, "R 排班")
    away = make_ctx(members=ms, leaves={"r2": {date(2026, 8, 6)}})
    assert "可排 5 天" in build_report(away, solve_duty(away), "R 排班")


def test_report_shows_how_many_preferred_days_were_met():
    ctx = make_ctx(members=members())
    assert "甲 指定星期（三、四）：可排 8 天，實排 8 天" in build_report(
        ctx, solve_duty(ctx), "R 排班")


def test_the_choice_is_part_of_the_input_fingerprint():
    assert (rvs_input_fingerprint(make_ctx(members=members((2, 3))))
            != rvs_input_fingerprint(make_ctx(members=members((1, 3)))))


def test_weight_ranking():
    from cmuh_common.roster.rules import POINT_WEIGHT, WeekendCapRule
    from cmuh_common.roster.rules import counted_blocks
    ctx = make_ctx(members=members())
    w = PreferredWeekdaysRule.day_weight(ctx)
    assert w > DutyCountBalanceRule.RANGE_WEIGHT
    assert w > 2 * ctx.params.weekday_point * 100 * POINT_WEIGHT   # 一天換人的點數偏差
    blocks = counted_blocks(ctx.blocks, ctx.weekend_exempt, ctx.holidays)
    assert WeekendCapRule.over_weight(ctx, blocks) > len(WED_THU) * w   # 週末上限仍最優先


@pytest.mark.parametrize("raw, single, multi", [
    ({"fixed_weekday": 2}, 2, ()),
    ({"fixed_weekdays": [3, 2, 3]}, None, (2, 3)),
    ({"fixed_weekday": 1, "fixed_weekdays": [2, 3]}, None, (2, 3)),
    ({"fixed_weekdays": [2]}, 2, ()),
    ({"fixed_weekdays": None}, None, ()),
    ({"fixed_weekdays": ["x", 9, True]}, None, ()),
    ({"fixed_weekday": 2, "fixed_weekdays": "壞"}, 2, ()),
])
def test_member_parsing(raw, single, multi):
    m = Member.from_dict({"id": "a", "name": "a", **raw})
    assert (m.fixed_weekday, m.fixed_weekdays) == (single, multi)
    assert Member.from_dict(m.to_dict()) == m


# ── 設定頁(真的 Tk)─────────────────────────────────────────────────────
def _click(dlg, i):
    """真的按畫面上的勾選框(不是直接改變數)。"""
    def walk(w):
        for c in w.winfo_children():
            if c.winfo_class() == "TCheckbutton" and c.cget("text") == "一二三四五六日"[i]:
                c.invoke()
            walk(c)
    walk(dlg)


shown: list = []


def _dialog(tk_root, monkeypatch, initial, toggles):
    from cmuh_common.roster.ui import settings as st

    def drive(dlg, _win=None):
        for i in toggles:
            _click(dlg, i)
        shown.append([k for k, var in dlg._wd.items() if var.get()])
        dlg._ok()
    monkeypatch.setattr(st._MemberDialog, "wait_window", drive)
    return st._MemberDialog(tk_root, "編輯", initial, True, True, id_locked=False).result


@pytest.mark.parametrize("initial, toggles, single, multi", [
    ({"fixed_weekday": 2}, [3], None, [2, 3]),
    ({"fixed_weekdays": [2, 3]}, [], None, [2, 3]),
    ({"fixed_weekdays": [2, 3]}, [2], 3, None),
    ({"fixed_weekday": 2}, [2], None, None),
])
def test_dialog_multi_select(tk_root, monkeypatch, initial, toggles, single, multi):
    out = _dialog(tk_root, monkeypatch, {"id": "A", "name": "甲", **initial}, toggles)
    assert (out["fixed_weekday"], out["fixed_weekdays"]) == (single, multi)


def test_settings_page_saves_and_lists_the_choice(tk_root, tmp_path, monkeypatch):
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage
    from cmuh_common.roster.ui import settings as st
    storage = RosterStorage(str(tmp_path))
    storage.save_config({"r_members": [{"id": "A", "name": "甲", "level": "R1",
                                        "fixed_weekday": 2}], "vs_members": []})
    tab = st.SettingsTab(tk_root, RosterService(storage))
    tree = tab._member_trees["r"][0]
    assert tree.item("A", "values")[-1] == "三"
    tree.selection_set("A")

    def drive(dlg, _win=None):
        dlg._wd[3].set(True)
        dlg._ok()
    monkeypatch.setattr(st._MemberDialog, "wait_window", drive)
    tab._member_edit("r")
    saved = storage.load_config()["r_members"][0]
    assert saved["fixed_weekdays"] == [2, 3] and saved["fixed_weekday"] is None
    assert tree.item("A", "values")[-1] == "三、四"
    tab.destroy()


def test_past_days_are_not_prechecked():
    """[PR #29 審查] 今天以前是事實,不可以說「由其他人代」。"""
    ctx = make_ctx(members=members(), leaves={"r1": {date(2026, 8, 5), date(2026, 8, 26)}})
    ctx.past_cutoff = date(2026, 8, 10)
    msgs = [c.msg for c in PreferredWeekdaysRule().precheck(ctx)]
    assert any("8/26" in m for m in msgs) and not any("8/5 " in m for m in msgs)


@pytest.mark.parametrize("toggles, single", [([], 5), ([0], 0), ([5], None)])
def test_a_legacy_weekend_choice_is_kept_until_changed(tk_root, monkeypatch, toggles, single):
    """[PR #29 審查] 舊設定選過週六:只改姓名不可以把它清掉;可以換掉或取消。"""
    out = _dialog(tk_root, monkeypatch, {"id": "A", "name": "甲", "fixed_weekday": 5}, toggles)
    assert shown[-1] == ([] if single is None else [single])    # 看到的＝會存的
    assert (out["fixed_weekday"], out["fixed_weekdays"]) == (single, None)


def test_a_concurrent_multi_choice_does_not_survive_beside_a_single_one(
        tk_root, tmp_path, monkeypatch):
    """[PR #29 審查] 視窗開著時他機存了三、四;本機改成只選一 → 整組以本機為準。"""
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage
    from cmuh_common.roster.ui import settings as st
    storage = RosterStorage(str(tmp_path))
    storage.save_config({"r_members": [{"id": "A", "name": "甲", "level": "R1"}],
                         "vs_members": []})
    tab = st.SettingsTab(tk_root, RosterService(storage))
    tab._member_trees["r"][0].selection_set("A")

    def drive(dlg, _win=None):
        cfg = storage.load_config()
        cfg["r_members"][0]["fixed_weekdays"] = [2, 3]
        storage.save_config(cfg)
        dlg._wd[0].set(True)
        dlg._ok()
    monkeypatch.setattr(st._MemberDialog, "wait_window", drive)
    tab._member_edit("r")
    m = Member.from_dict(storage.load_config()["r_members"][0])
    assert (m.fixed_weekday, m.fixed_weekdays) == (0, ())
    tab.destroy()


# ── [2026-11-24 使用者「選2」] 指定星期優先於避免連值 4、5 天;上限 12 ──────────
def _december(off, weekdays=(2, 3)):
    def D(n):
        return date(2026, 12, n)
    ctx = SolveContext(
        "r", 2026, 12, holidays={D(25)}, annual_holiday={D(25): "K"},
        members=[Member("K", "K", "R3", fixed_weekdays=weekdays),
                 Member("C", "C", "R2", fixed_weekday=1),
                 Member("Z", "Z", "R1", fixed_weekday=0)],
        leaves={"K": {D(n) for n in off}, "C": {D(n) for n in (11, 12, 13)},
                "Z": {D(n) for n in (19, 20, 26, 27)}},
        ledger={"C": .33, "K": -.67, "Z": .33},
        week_colors=week_colors_for_year(2026),
        weekend_prior={"K": 6, "C": 6, "Z": 6},
        weekend_weights={"K": 16.4, "C": 17.5, "Z": 19.1}).prepare()
    res = solve_duty(ctx)
    assert res.status == "ok"
    return [d.day for d in sorted(res.assignments) if res.assignments[d] == "K"], res


@pytest.mark.parametrize("off, want", [
    ((31,), [2, 3, 9, 10, 16, 17, 23, 24, 30]),
    ((2, 3, 31), [9, 10, 16, 17, 23, 24, 30])])
def test_every_available_preferred_day_goes_to_them(off, want):
    days, res = _december(off)
    assert [n for n in days if date(2026, 12, n).weekday() in (2, 3)] == want
    assert {25, 26, 27} <= set(days) and res.duty_counts["K"] <= 12
    assert sum(abs(res.points_by_person[p] - res.targets[p]) for p in "KCZ") < 1.4
    assert all(res.duty_counts[p] <= 11 for p in "CZ")


def test_rules_are_unchanged_without_multiple_weekdays():
    days, res = _december((31,), ())
    assert res.duty_counts["K"] <= 11
    assert not any(all(n + i in days for i in range(5)) for n in range(1, 28))


def test_one_five_day_run_costs_less_than_one_preferred_day():
    c = ConsecutiveDutyRule
    assert (3 * c.RUN3_WEIGHT + 2 * c.PREFERRED_RUN4_WEIGHT + c.PREFERRED_RUN5_WEIGHT
            < PreferredWeekdaysRule.day_weight(make_ctx(members=members())))


@pytest.mark.parametrize("configured, want", [(11, 12), (12, 12), (13, 13)])
def test_the_cap_is_loosened_to_twelve_but_never_below_the_setting(configured, want):
    from cmuh_common.roster.rules import DutyRangeRule
    ctx = make_ctx(members=members())
    ctx.params.duty_max = configured
    assert [DutyRangeRule._max(ctx, m) for m in ctx.members] == [want, configured, configured]
