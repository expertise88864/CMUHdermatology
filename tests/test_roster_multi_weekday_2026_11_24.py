# -*- coding: utf-8 -*-
"""[2026-11-24 使用者] R 值班星期可複選:兩天以上【盡量】排同一人,硬規則與點數平衡
優先;只選一天維持原本的硬性固定。匿名代號、暫存資料夾。"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
pytest.importorskip("ortools", reason="ortools 未安裝（自動排班引擎）")

from cmuh_common.roster.model import Member  # noqa: E402
from cmuh_common.roster.report import build_report  # noqa: E402
from cmuh_common.roster.rules import (  # noqa: E402
    DutyCountBalanceRule, PreferredWeekdaysRule)
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


def test_points_outrank_the_preference():
    """點數已超前(帳本 +6)→ 不會為了指定星期排滿,班數與點數優先。"""
    ctx = make_ctx(members=members(first=(0, 1, 2, 3, 4)),
                   ledger={"r1": 6.0, "r2": -3.0, "r3": -3.0})
    free = make_ctx(members=[Member(i, i, lv) for i, lv in
                             (("r1", "R1"), ("r2", "R2"), ("r3", "R3"))],
                    ledger={"r1": 6.0, "r2": -3.0, "r3": -3.0})
    res, base = solve_duty(ctx), solve_duty(free)
    assert res.status == "ok"
    assert dev(ctx, res) == pytest.approx(dev(free, base))
    assert res.duty_counts["r1"] <= 11


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


def test_report_shows_how_many_preferred_days_were_met():
    ctx = make_ctx(members=members())
    assert "甲 指定星期（三、四）：可排 8 天，實排 8 天" in build_report(
        ctx, solve_duty(ctx), "R 排班")


def test_the_choice_is_part_of_the_input_fingerprint():
    assert (rvs_input_fingerprint(make_ctx(members=members((2, 3))))
            != rvs_input_fingerprint(make_ctx(members=members((1, 3)))))


def test_weight_ranking():
    assert PreferredWeekdaysRule.DAY_WEIGHT > DutyCountBalanceRule.RANGE_WEIGHT


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
def _dialog(tk_root, monkeypatch, initial, toggles):
    from cmuh_common.roster.ui import settings as st

    def drive(dlg, _win=None):
        for i in toggles:
            dlg._wd[i].set(not dlg._wd[i].get())
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
