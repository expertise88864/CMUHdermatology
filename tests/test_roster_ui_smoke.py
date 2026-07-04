# -*- coding: utf-8 -*-
"""roster UI 冒煙測試：實際建立 Tk 元件、跑 refresh 與手動改格路徑，抓接線錯誤。
無顯示器（Tk 建立失敗）→ 整檔跳過。所有 messagebox 皆 monkeypatch 掉不阻塞。"""
import os
import sys
import types
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

try:
    import tkinter as tk
    from tkinter import ttk
    _r = tk.Tk()
    ttk.Spinbox(_r)            # 破損的 tk 安裝缺 ttk/spinbox.tcl → 在此失敗
    ttk.Treeview(_r)
    _r.destroy()
    _HAS_TK = True
except Exception:
    _HAS_TK = False

pytestmark = pytest.mark.skipif(not _HAS_TK, reason="無可用顯示器/或 tk 安裝不完整")

from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402
from cmuh_common.roster.ui import day_tab as day_mod  # noqa: E402
from cmuh_common.roster.ui import duty as duty_mod  # noqa: E402
from cmuh_common.roster.ui import settings as settings_mod  # noqa: E402
from cmuh_common.roster.ui.day_tab import DayScheduleTab  # noqa: E402
from cmuh_common.roster.ui.duty import CalendarDutyTab  # noqa: E402
from cmuh_common.roster.ui.settings import SettingsTab  # noqa: E402

YM = "2026-08"


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as e:            # 破損 tk 的偶發失敗 → 跳過而非 error
        pytest.skip(f"tk 建立失敗：{e}")
    r.geometry("1000x700")
    yield r
    r.destroy()


@pytest.fixture(autouse=True)
def noblock(monkeypatch):
    for mod in (duty_mod, settings_mod, day_mod):
        monkeypatch.setattr(mod.messagebox, "askyesno", lambda *a, **k: True)
        monkeypatch.setattr(mod.messagebox, "showwarning", lambda *a, **k: None)
        monkeypatch.setattr(mod.messagebox, "showerror", lambda *a, **k: None)
        monkeypatch.setattr(mod.messagebox, "showinfo", lambda *a, **k: None)


def _svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": "A", "name": "甲", "fixed_weekday": 2},
                      {"id": "B", "name": "乙"}],
        "vs_members": [{"id": "D", "name": "D"}],
        "pgy_members": [{"id": "A"}, {"id": "B"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
        "duty_range_soft": [9, 11],
    })
    st.save_holiday_duty({"r": {date(2026, 8, 15): "A"}, "vs": {}})
    st.save_clinic_template({"template": {"0": {"上午": [{"room": "101"}]}}})
    return RosterService(st)


def _app():
    return types.SimpleNamespace(ym=YM)


def test_settings_tab_builds_and_reloads(root, tmp_path):
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    tab.pack(fill="both", expand=True)
    root.update()
    # 名單樹已載入 config 成員
    assert set(tab._member_trees["r"][0].get_children()) == {"A", "B"}
    # 新增假日 → 存檔並反映
    tab._hol_date.insert(0, "2026-08-25")
    tab._hol_r.insert(0, "B")
    tab._holiday_put()
    assert date(2026, 8, 25) in svc.storage.load_holiday_duty()["r"]
    # 參數存檔不炸
    tab._save_params()
    assert svc.storage.load_config()["duty_range_soft"] == [9, 11]


def test_settings_phase3_blocks(root, tmp_path):
    svc = _svc(tmp_path)                                # _svc 已存門診模板(101)
    svc.storage.save_clerk_batches(
        [{"id": "b1", "start_monday": "2026-08-03", "members": ["1", "2"]}])
    tab = SettingsTab(root, svc)
    tab.pack(fill="both", expand=True)
    root.update()
    assert tab._tpl_tree.get_children()                # 門診模板有 101 那筆
    assert "b1" in tab._batch_tree.get_children()       # Clerk 梯次有 b1
    # PGY 預設代號存檔（頓號分隔）
    tab._pgy_entry.delete(0, "end")
    tab._pgy_entry.insert(0, "X、Y")
    tab._save_pgy_defaults()
    assert [m["id"] for m in svc.storage.load_config()["pgy_members"]] == ["X", "Y"]
    # 刪除門診模板列
    tab._tpl_tree.selection_set(tab._tpl_tree.get_children()[0])
    tab._template_del()
    assert not tab._tpl_tree.get_children()


def test_biopsy_seed_from_prev_batch(root, tmp_path):
    """新梯次切片格網預設複製前一梯次（依相對週幾對齊）。"""
    svc = _svc(tmp_path)
    svc.storage.save_clerk_batches(
        [{"id": "b1", "start_monday": "2026-08-03", "members": ["1"]}])
    svc.storage.save_biopsy_grid({"b1": {"2026-08-03": {"上午": True}}})  # 梯1 週一早開
    tab = SettingsTab(svc and root, svc)
    root.update()
    b2 = {"id": "b2", "start_monday": "2026-08-17", "members": ["2"]}
    tab._seed_biopsy_from_prev(
        b2, svc.storage.load_clerk_batches() + [b2])
    g = svc.storage.load_biopsy_grid()
    assert g["b2"]["2026-08-17"]["上午"] is True     # 梯2 週一(8/17)早對齊複製


def test_shift_biopsy_grid_on_start_change(root, tmp_path):
    """改梯次起始日 → 切片格網整組平移，不遺失（codex P2）。"""
    svc = _svc(tmp_path)
    svc.storage.save_biopsy_grid({"b1": {"2026-08-03": {"上午": True}}})
    tab = SettingsTab(root, svc)
    root.update()
    tab._shift_biopsy_grid("b1", "2026-08-03", "2026-08-10")   # 後移一週
    g = svc.storage.load_biopsy_grid()
    assert "2026-08-03" not in g["b1"] and g["b1"]["2026-08-10"]["上午"] is True


def test_duty_tab_manual_edit_and_lock(root, tmp_path):
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, "r", _app())
    tab.pack(fill="both", expand=True)
    root.update()

    d = date(2026, 8, 3)                       # 週一
    tab._on_cell_left(d)                       # None → A（名單首位）
    cell = svc.storage.load_month(YM)["r_duty"]["2026-08-03"]
    assert cell["person"] == "A" and cell["locked"] is False

    tab._toggle_lock(d)
    assert svc.storage.load_month(YM)["r_duty"]["2026-08-03"]["locked"] is True
    assert svc.build_context("r", YM).locks == {d: "A"}

    tab._set_cell_and_refresh(date(2026, 8, 4), "B")
    assert svc.storage.load_month(YM)["r_duty"]["2026-08-04"]["person"] == "B"


def test_duty_tab_clear_unlocked_keeps_locked(root, tmp_path):
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, "r", _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab._set_cell_and_refresh(date(2026, 8, 5), "A")
    tab._set_cell_and_refresh(date(2026, 8, 6), "B")
    tab._toggle_lock(date(2026, 8, 5))         # 鎖定 8/5
    tab._on_clear_unlocked()                    # 清未鎖定（askyesno→True）
    duty = svc.storage.load_month(YM)["r_duty"]
    assert "2026-08-05" in duty                 # 鎖定保留
    assert "2026-08-06" not in duty             # 未鎖定被清


def test_duty_tab_finalize_disables_editing(root, tmp_path):
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, "r", _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab._final_var.set(True)
    tab._on_finalize()
    assert svc.storage.load_month(YM)["finalized"] is True
    assert str(tab._auto_btn["state"]) == "disabled"
    tab._final_var.set(False)
    tab._on_finalize()
    assert svc.storage.load_month(YM)["finalized"] is False


def test_vs_tab_reuses_same_class(root, tmp_path):
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, "vs", _app())   # scope="vs" 直接重用
    tab.pack(fill="both", expand=True)
    root.update()
    tab._on_cell_left(date(2026, 8, 3))
    assert svc.storage.load_month(YM)["vs_duty"]["2026-08-03"]["person"] == "D"


def test_leave_editor_saves(root, tmp_path):
    svc = _svc(tmp_path)
    ed = duty_mod.LeaveEditor(root, svc, "r", YM, "leave")
    root.update()
    ed._combo.current(0)                        # 成員 A
    ed._load_member()
    ed._toggle(date(2026, 8, 10))
    ed._toggle(date(2026, 8, 11))
    ed._save()
    assert svc.build_context("r", YM).leaves["A"] == {
        date(2026, 8, 10), date(2026, 8, 11)}


# ─── PGY/Clerk 日排班分頁 ────────────────────────────────────────────────────
def test_pgy_day_tab_builds_and_roster(root, tmp_path):
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, "pgy", _app())
    tab.pack(fill="both", expand=True)
    root.update()
    assert "2026-08-03|上午" in tab._tree.get_children()   # 週一有工作日列
    assert tab._roster_members() == [{"id": "A", "name": ""},
                                     {"id": "B", "name": ""}]


def test_clerk_day_tab_builds(root, tmp_path):
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, "clerk", _app())
    tab.pack(fill="both", expand=True)
    root.update()
    assert tab._tree.get_children()


def test_day_tab_auto_accept_flow(root, tmp_path):
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, "pgy", _app())
    tab.pack(fill="both", expand=True)
    root.update()
    ds, _log, _w = svc.run_day_solve(YM)
    svc.accept_day_solution(YM, ds)
    tab.refresh()                                          # 重繪不炸
    mon = svc.storage.load_month(YM)["day_slots"]["2026-08-03"]["上午"]
    assert mon.get("治療室")                               # 週一早有治療室 PGY


def test_day_edit_dialog_saves(root, tmp_path):
    svc = _svc(tmp_path)
    dlg = day_mod._DayEditDialog(root, svc, YM, date(2026, 8, 3), "上午",
                                 lambda: None)
    root.update()
    dlg._entries["治療室"].delete(0, "end")
    dlg._entries["治療室"].insert(0, "A")
    dlg._save()
    got = svc.storage.load_month(YM)["day_slots"]["2026-08-03"]["上午"]["治療室"]
    assert got == ["A"]


def test_day_tab_finalize_disables_edit_controls(root, tmp_path):
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, "pgy", _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab._final_var.set(True)
    tab._on_finalize()
    assert str(tab._auto_btn["state"]) == "disabled"
    assert str(tab._leave_btn["state"]) == "disabled"
    tab._edit_pgy_roster()                                 # 定案後為 no-op，不炸
    tab._on_leave()
    assert svc.storage.load_month(YM)["finalized"] is True


def test_day_edit_preserves_multi_person_on_unchanged_save(root, tmp_path):
    """開啟含多人的格、原封不動存 → 不得被 `、` 併成單一 id（codex P2）。"""
    svc = _svc(tmp_path)
    svc.set_day_slot(YM, date(2026, 8, 3), "上午", "101", ["A", "B"])
    dlg = day_mod._DayEditDialog(root, svc, YM, date(2026, 8, 3), "上午",
                                 lambda: None)
    root.update()
    dlg._save()                                            # 不改直接存
    got = svc.storage.load_month(YM)["day_slots"]["2026-08-03"]["上午"]["101"]
    assert got == ["A", "B"]


def test_day_edit_dialog_can_clear_stale_room(root, tmp_path):
    """已從模板移除/關閉的房號殘留指派 → 編輯視窗仍要能清除（codex P2）。"""
    svc = _svc(tmp_path)
    svc.set_day_slot(YM, date(2026, 8, 3), "上午", "999", ["A"])   # 非模板房
    dlg = day_mod._DayEditDialog(root, svc, YM, date(2026, 8, 3), "上午",
                                 lambda: None)
    root.update()
    assert "999" in dlg._entries                           # 殘留房也有欄位
    dlg._entries["999"].delete(0, "end")                   # 清空
    dlg._save()
    slots = svc.storage.load_month(YM)["day_slots"]["2026-08-03"]["上午"]
    assert "999" not in slots
