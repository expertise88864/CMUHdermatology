# -*- coding: utf-8 -*-
"""[2026-11-17 使用者] Clerk 請假要指定【哪一梯的哪一位】。

代號在梯次之間重複(每梯都有 1~6 號),而請假以「代號＋日期」記錄。舊的請假視窗
只列代號、整月都能點、「每週批次」套到全月 → 一次就替每一梯的同代號都請了假。
現在:選項是「梯次 · 代號」,只有該梯次的上課日可點,每週批次也只在那個範圍。
用真的 Tk 視窗操作(匿名代號、暫存資料夾)。"""
import os
import sys
import types
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    import tkinter as tk  # noqa: F401
except ImportError:  # pragma: no cover
    pass

from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402
from cmuh_common.roster.ui import day_tab as day_mod  # noqa: E402
from cmuh_common.roster.ui.day_tab import DayScheduleTab  # noqa: E402
from cmuh_common.roster.ui.session_leave import SessionLeaveEditor  # noqa: E402

YM = "2026-08"


@pytest.fixture
def root(tk_root):
    tk_root.geometry("1000x700")
    return tk_root


@pytest.fixture(autouse=True)
def noblock(monkeypatch):
    for name in ("askyesno", "showwarning", "showerror", "showinfo"):
        monkeypatch.setattr(day_mod.messagebox, name,
                            (lambda *a, **k: True) if name == "askyesno"
                            else (lambda *a, **k: None))
    monkeypatch.setattr(day_mod, "archive_finalize_pdf_async", lambda *a, **k: None)


def _svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [], "vs_members": [],
                    "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": [{"room": "101"}]} for w in range(5)}})
    st.save_clerk_batches([
        {"id": "b1", "start_monday": "2026-08-03", "members": ["1", "2", "3"]},
        {"id": "b2", "start_monday": "2026-08-17", "members": ["1", "2", "3"]}])
    st.save_biopsy_grid({})
    st.save_month(YM, {"day_slots": {}})
    return RosterService(st)


def _editor(root, tmp_path):
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, types.SimpleNamespace(ym=YM))
    members = tab._clerk_leave_members()
    ed = SessionLeaveEditor(root, svc, "clerk", YM, "leave", members=members)
    root.update()
    return svc, tab, ed, members


def _pick(ed, members, label):
    ed._combo.current([m["label"] for m in members].index(label))
    ed._on_member_change()


def test_each_option_names_the_batch_and_the_person(root, tmp_path):
    _svc_, tab, ed, members = _editor(root, tmp_path)
    labels = [m["label"] for m in members]
    assert labels == ["8/3–8/16 梯 · 1", "8/3–8/16 梯 · 2", "8/3–8/16 梯 · 3",
                      "8/17–8/30 梯 · 1", "8/17–8/30 梯 · 2", "8/17–8/30 梯 · 3"]
    assert list(ed._combo["values"]) == labels
    ed.destroy()
    tab.destroy()


def test_only_that_batchs_days_can_be_clicked(root, tmp_path):
    svc, tab, ed, members = _editor(root, tmp_path)
    _pick(ed, members, "8/3–8/16 梯 · 3")
    ed._toggle(date(2026, 8, 5))                 # 第一梯的日子 → 可以
    ed._toggle(date(2026, 8, 19))                # 第二梯的日子 → 不理
    assert str(ed._buttons[date(2026, 8, 19)]["state"]) == "disabled"
    assert str(ed._buttons[date(2026, 8, 5)]["state"]) == "normal"
    ed._save()
    assert svc.get_trainee_leave_slots("clerk", YM, "3") == {
        (date(2026, 8, 5), "上午"), (date(2026, 8, 5), "下午")}
    tab.destroy()


def test_weekly_batch_stays_inside_the_chosen_batch(root, tmp_path):
    """舊行為:「本月每週三」會把兩梯的 3 號都請假。"""
    svc, tab, ed, members = _editor(root, tmp_path)
    _pick(ed, members, "8/17–8/30 梯 · 3")
    ed._period.set("上午")
    ed._weekday.set("三")
    ed._weekly(True)
    ed._save()
    assert svc.get_trainee_leave_slots("clerk", YM, "3") == {
        (date(2026, 8, 19), "上午"), (date(2026, 8, 26), "上午")}
    tab.destroy()


def test_other_batchs_leave_is_kept_when_editing_this_batch(root, tmp_path):
    """同代號在另一梯已請的假,編輯這一梯時不會被清掉。"""
    svc, tab, ed, members = _editor(root, tmp_path)
    _pick(ed, members, "8/3–8/16 梯 · 3")
    ed._toggle(date(2026, 8, 6))
    _pick(ed, members, "8/17–8/30 梯 · 3")        # 切換時先存上一位
    assert ed._combo.get() == "8/17–8/30 梯 · 3"
    ed._toggle(date(2026, 8, 20))
    ed._save()
    days = {d for d, _s in svc.get_trainee_leave_slots("clerk", YM, "3")}
    assert days == {date(2026, 8, 6), date(2026, 8, 20)}
    tab.destroy()


def test_a_failed_switch_returns_to_the_same_batch_entry(root, tmp_path, monkeypatch):
    """兩梯同代號:存檔失敗退回時,要回到原本那一梯的那一位(不是同代號的第一項)。"""
    svc, tab, ed, members = _editor(root, tmp_path)
    _pick(ed, members, "8/17–8/30 梯 · 3")
    ed._toggle(date(2026, 8, 20))
    monkeypatch.setattr(svc, "set_trainee_leave_slots",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("鎖住")))
    from cmuh_common.roster.ui import common as common_mod
    monkeypatch.setattr(common_mod.messagebox, "showerror", lambda *a, **k: None)
    ed._combo.current(0)
    ed._on_member_change()
    assert ed._combo.get() == "8/17–8/30 梯 · 3"
    ed.destroy()
    tab.destroy()


def test_overlapping_batches_only_offer_the_days_each_one_owns(root, tmp_path):
    """[PR #24 審查] 梯次日期重疊(設定打錯)時,重疊日歸設定順序較前的梯次
    (與排班程式相同);後一梯不可以點到那幾天。"""
    svc = _svc(tmp_path)
    svc.storage.save_clerk_batches([
        {"id": "A", "start_monday": "2026-08-03", "members": ["3"]},
        {"id": "B", "start_monday": "2026-08-10", "members": ["3"]}])
    tab = DayScheduleTab(root, svc, types.SimpleNamespace(ym=YM))
    members = {m["label"]: m["days"] for m in tab._clerk_leave_members()}
    a, b = members["8/3–8/16 梯 · 3"], members["8/10–8/23 梯 · 3"]
    assert date(2026, 8, 12) in a and date(2026, 8, 12) not in b
    assert date(2026, 8, 19) in b and not (a & b)
    tab.destroy()
