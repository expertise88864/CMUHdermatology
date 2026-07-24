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
    # 定案 PDF 走背景執行緒 + Tk after，測試無 mainloop → 換成 no-op（PDF 另檔測）
    for mod in (duty_mod, day_mod):
        monkeypatch.setattr(mod, "archive_finalize_pdf_async", lambda *a, **k: None)


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


def test_rf18_duplicate_member_id_no_startup_crash(root, tmp_path):
    """RF-18：config 名單重複/空代號 → SettingsTab 建構不得拋 TclError。"""
    svc = _svc(tmp_path)
    cfg = svc.storage.load_config()
    cfg["r_members"] = [{"id": "A"}, {"id": "A"}, {"id": ""}]   # 重複 + 空代號
    svc.storage.save_config(cfg)
    tab = SettingsTab(root, svc)                    # 不應拋例外
    tab.pack(fill="both", expand=True)
    root.update()
    assert tab._member_trees["r"][0].get_children() == ("A",)   # 只顯示一筆 A


def test_rf18_duplicate_batch_key_no_startup_crash(root, tmp_path):
    """RF-18：兩筆無 id 同起始日的舊 Clerk 梯次 → 不得撞 Treeview iid 崩潰。"""
    svc = _svc(tmp_path)
    svc.storage.save_clerk_batches([
        {"start_monday": "2026-08-03", "members": ["1"]},
        {"start_monday": "2026-08-03", "members": ["2"]}])       # 無 id 同起始日
    tab = SettingsTab(root, svc)                    # 不應拋例外
    tab.pack(fill="both", expand=True)
    root.update()
    assert len(tab._batch_tree.get_children()) == 1


def test_rf19_on_shown_reloads_ledger(root, tmp_path):
    """RF-19：R/VS 分頁改帳本後切回設定頁 → on_shown 重讀顯示新餘額。"""
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    tab.pack(fill="both", expand=True)
    root.update()
    led = svc.storage.load_ledger()                 # 模擬 R 分頁 accept/重算改帳本
    led["r"] = {"A": 3.5}
    svc.storage.save_ledger(led)
    tab.on_shown()
    assert tab._led_tree.item("r:A", "values")[2] == "+3.50"


def test_rf21_param_save_is_debounced(root, tmp_path, monkeypatch):
    """RF-21：連續三次鍵擊 → 800ms 內 0 次 save_config，去抖後恰 1 次。"""
    import time
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    tab.pack(fill="both", expand=True)
    root.update()
    calls = []
    monkeypatch.setattr(svc.storage, "save_config", lambda cfg: calls.append(1))
    tab._p_wd.set(2)
    tab._p_wd.set(3)
    tab._p_wd.set(4)                                # 模擬三次鍵擊
    root.update()
    assert calls == []                             # 800ms 內尚未存
    deadline = time.time() + 3
    while time.time() < deadline and not calls:    # 等去抖觸發
        root.update()
        time.sleep(0.05)
    assert len(calls) == 1                          # 恰 1 次


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
    tab = CalendarDutyTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()

    d = date(2026, 8, 3)                       # 週一
    tab._on_cell_left(d, "r")                       # None → A（名單首位）
    cell = svc.storage.load_month(YM)["r_duty"]["2026-08-03"]
    assert cell["person"] == "A" and cell["locked"] is False

    tab._toggle_lock(d, "r")
    assert svc.storage.load_month(YM)["r_duty"]["2026-08-03"]["locked"] is True
    assert svc.build_context("r", YM).locks == {d: "A"}

    tab._set_cell_and_refresh(date(2026, 8, 4), "B", "r")
    assert svc.storage.load_month(YM)["r_duty"]["2026-08-04"]["person"] == "B"


def test_duty_tab_clear_unlocked_keeps_locked(root, tmp_path):
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab._set_cell_and_refresh(date(2026, 8, 5), "A", "r")
    tab._set_cell_and_refresh(date(2026, 8, 6), "B", "r")
    tab._toggle_lock(date(2026, 8, 5), "r")         # 鎖定 8/5
    m = svc.storage.load_month(YM)             # RF-20：塞舊報告，清除後應一併清空
    m["report_r"] = "OLD"
    svc.storage.save_month(YM, m)
    tab._on_clear_unlocked("r")                    # 清未鎖定（askyesno→True）
    duty = svc.storage.load_month(YM)["r_duty"]
    assert "2026-08-05" in duty                 # 鎖定保留
    assert "2026-08-06" not in duty             # 未鎖定被清
    assert svc.storage.load_month(YM).get("report_r") == ""   # RF-20：舊報告清空


def test_rf05_discards_result_when_month_changed(root, tmp_path):
    """RF-05：求解期間切月 → 結果屬別的月 → 捨棄不預覽/不套用。"""
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab.app.ym = "2026-09"                       # 求解期間切走月份
    tab._on_solved(types.SimpleNamespace(status="ok"), "2026-08", "r")
    assert not [w for w in tab.winfo_children() if isinstance(w, tk.Toplevel)]
    assert svc.storage.load_month("2026-09").get("r_duty") == {}


def test_rf05_selector_disabled_while_busy(root, tmp_path):
    """RF-05：求解中 MonthSelector 的 ◀▶ 停用；結束恢復。"""
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab._busy("x")
    assert str(tab._selector._prev_btn["state"]) == "disabled"
    assert str(tab._selector._next_btn["state"]) == "disabled"
    tab._unbusy()
    assert str(tab._selector._prev_btn["state"]) == "normal"


def test_rf16_unbusy_on_finalized_month_keeps_report_and_final(root, tmp_path):
    """RF-16：求解結束停在已定案月，報告鈕/定案勾選不得被永久停用。"""
    svc = _svc(tmp_path)
    m = svc.storage.load_month("2026-09")
    m["finalized"] = True
    svc.storage.save_month("2026-09", m)
    tab = CalendarDutyTab(root, svc, _app())    # app.ym=2026-08（未定案）
    tab.pack(fill="both", expand=True)
    root.update()
    tab._busy("x")
    tab._on_month_change("2026-09")                   # 求解中切到已定案月
    tab._unbusy()                                     # 求解結束
    assert str(tab._report_btns["r"]["state"]) == "normal"
    assert str(tab._final_chk["state"]) == "normal"
    tab._on_month_change("2026-08")                   # 切回未定案月
    assert str(tab._auto_btns["r"]["state"]) == "normal"


def test_rf17_refresh_during_busy_keeps_disabled_and_no_edit(root, tmp_path):
    """RF-17：求解中 refresh 不得重新啟用編輯鈕；手排格為 no-op。"""
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab._busy("x")
    tab.refresh()                                     # 切月/設定變更會觸發
    assert str(tab._auto_btns["r"]["state"]) == "disabled"
    assert str(tab._clear_btns["r"]["state"]) == "disabled"
    assert str(tab._resettle_btns["r"]["state"]) == "disabled"
    tab._on_cell_left(date(2026, 8, 3), "r")               # 求解中手排 → no-op
    assert "2026-08-03" not in svc.storage.load_month(YM).get("r_duty", {})


def test_duty_tab_finalize_disables_editing(root, tmp_path):
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab._final_var.set(True)
    tab._on_finalize()
    assert svc.storage.load_month(YM)["finalized"] is True
    assert str(tab._auto_btns["r"]["state"]) == "disabled"
    tab._final_var.set(False)
    tab._on_finalize()
    assert svc.storage.load_month(YM)["finalized"] is False


def test_vs_row_edits_vs_scope_in_merged_tab(root, tmp_path):
    """[2026-07-23 整合] 同一分頁點 VS（三線）列 → 只改 vs_duty，不碰 r_duty。"""
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab._on_cell_left(date(2026, 8, 3), "vs")
    m = svc.storage.load_month(YM)
    assert m["vs_duty"]["2026-08-03"]["person"] == "D"
    assert "2026-08-03" not in (m.get("r_duty") or {})     # 一線不受影響


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
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    assert "2026-08-03|上午" in tab._tree.get_children()   # 週一有工作日列
    assert tab._roster_members("pgy") == [{"id": "A", "name": ""},
                                     {"id": "B", "name": ""}]


def test_clerk_day_tab_builds(root, tmp_path):
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    assert tab._tree.get_children()


def test_day_tab_auto_accept_flow(root, tmp_path):
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    ds, _log, _w = svc.run_day_solve(YM)
    svc.accept_day_solution(YM, ds)
    tab.refresh()                                          # 重繪不炸
    mon = svc.storage.load_month(YM)["day_slots"]["2026-08-03"]["上午"]
    assert mon.get("治療室")                               # 週一早有治療室 PGY


def test_clinic_closure_dialog_closes_room(root, tmp_path):
    """本月停診對話框：選診間+日期+時段→停診，寫進 grid_overrides。"""
    svc = _svc(tmp_path)                                    # 模板 週一 上午 101
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    dlg = day_mod._ClinicClosureDialog(tab, svc, YM)
    root.update()
    dlg._room.set("101")
    dlg._start.delete(0, "end")
    dlg._start.insert(0, "2026-08-03")
    dlg._end.delete(0, "end")
    dlg._end.insert(0, "2026-08-03")
    dlg._pm.set(False)                                     # 只停上午
    dlg._apply(True)
    assert svc.clinic_closures(YM)["2026-08-03"]["上午"] == ["101"]
    assert "101" not in svc.build_day_input(YM).grid[date(2026, 8, 3)]["上午"]


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


def test_day_tab_lock_toggle(root, tmp_path):
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    svc.accept_day_solution(YM, svc.run_day_solve(YM)[0])
    tab.refresh()
    tab._tree.selection_set("2026-08-03|上午")
    tab._on_toggle_lock()
    assert svc.is_day_locked(YM, date(2026, 8, 3), "上午")
    assert "🔒" in tab._tree.item("2026-08-03|上午", "values")   # 鎖列顯示


def test_day_tab_can_unlock_empty_session(root, tmp_path):
    """鎖定後把該時段清空 → 仍能從 UI 解鎖（不被空時段擋，codex P2）。"""
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    svc.accept_day_solution(YM, svc.run_day_solve(YM)[0])
    svc.toggle_day_lock(YM, date(2026, 8, 3), "上午")
    m = svc.storage.load_month(YM)                    # 清空該鎖定時段內容
    m["day_slots"]["2026-08-03"]["上午"] = {}
    svc.storage.save_month(YM, m)
    tab.refresh()
    tab._tree.selection_set("2026-08-03|上午")
    tab._on_toggle_lock()                             # 應能解鎖
    assert not svc.is_day_locked(YM, date(2026, 8, 3), "上午")


def test_day_tab_finalize_disables_edit_controls(root, tmp_path):
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab._final_var.set(True)
    tab._on_finalize()
    assert str(tab._auto_btn["state"]) == "disabled"
    for b in tab._edit_btns:                               # 兩列編輯鈕全停用
        assert str(b["state"]) == "disabled"
    tab._edit_pgy_roster()                                 # 定案後為 no-op，不炸
    tab._on_leave("pgy")
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


def test_rp3_20_leave_editor_keeps_edits_across_member_switch(root, tmp_path):
    """[RP3-20] 為 A 勾兩天 → 切到 B 勾一天 → 儲存：A、B 的請假都要落檔
    （修正前切換成員會靜默丟掉前一位的未存勾選）。"""
    svc = _svc(tmp_path)                       # r_members 至少 A、B
    ed = duty_mod.LeaveEditor(root, svc, "r", YM, "leave")
    root.update()
    ed._combo.current(0)                       # A
    ed._load_member()
    ed._toggle(date(2026, 8, 10))
    ed._toggle(date(2026, 8, 11))
    ed._combo.current(1)                       # 切到 B
    ed._on_member_change()                     # 先 commit A 再載入 B
    ed._toggle(date(2026, 8, 12))
    ed._save()
    leaves = svc.build_context("r", YM).leaves
    assert leaves["A"] == {date(2026, 8, 10), date(2026, 8, 11)}
    assert leaves["B"] == {date(2026, 8, 12)}


def test_rs07_day_tab_refresh_populates_warnings(root, tmp_path):
    """[RS-07] refresh 會呼叫 quick_validate_day 並填警告面板（週三下午治療室有人）。"""
    svc = _svc(tmp_path)
    svc.set_day_slot(YM, date(2026, 8, 5), "下午", "治療室", ["A"])   # 週三下午
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab.refresh()
    items = [tab._warns.get(i) for i in range(tab._warns.size())]
    assert any("週三下午" in it for it in items)


def test_rs01_day_tab_export_cancel_no_crash(root, tmp_path, monkeypatch):
    """[RS-01] 日排班分頁「匯出」鈕：取消存檔對話框 → 直接 return，不起緒、不炸。"""
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    monkeypatch.setattr(day_mod.filedialog, "asksaveasfilename", lambda **k: "")
    tab._on_export()      # 取消 → 不應拋例外


def test_day_tab_default_calendar_view_and_toggle(root, tmp_path):
    """[2026-07-23 使用者] 預設檢視＝月曆總覽（比較少看列表）；按鈕切換列表↔月曆。"""
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    assert tab._view_mode == "cal"                         # 預設月曆
    assert tab._cal_body.winfo_children()                  # 月曆已繪（圖例+表頭+格）
    tab._on_toggle_view()
    assert tab._view_mode == "list"
    tab._on_toggle_view()
    assert tab._view_mode == "cal"


def test_merged_duty_tab_has_both_summaries(root, tmp_path):
    """[2026-07-23 整合] 合併值班分頁：右側同時有 R 與 VS 兩個結算面板，各自統計。"""
    svc = _svc(tmp_path)
    tab = CalendarDutyTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    tab._set_cell_and_refresh(date(2026, 8, 3), "A", "r")
    tab._set_cell_and_refresh(date(2026, 8, 3), "D", "vs")
    r_rows = [tab._sum["r"].item(i)["values"]
              for i in tab._sum["r"].get_children()]
    vs_rows = [tab._sum["vs"].item(i)["values"]
               for i in tab._sum["vs"].get_children()]
    assert any(str(v[0]) == "A" and int(v[2]) == 1 for v in r_rows)   # R 平日1班
    assert any(str(v[0]) == "D" and int(v[2]) == 1 for v in vs_rows)  # VS 平日1班


def test_merged_day_tab_has_both_stats_and_cell_menu(root, tmp_path):
    """[2026-07-23 整合] 合併日排班分頁：PGY/Clerk 兩個統計面板都在；月曆格
    有直接編輯選單掛載（_attach_cell_menu 於 _render_calendar 逐格呼叫）。"""
    import inspect as _ins
    svc = _svc(tmp_path)
    tab = DayScheduleTab(root, svc, _app())
    tab.pack(fill="both", expand=True)
    root.update()
    assert tab._stats_pgy.winfo_exists() and tab._stats_clerk.winfo_exists()
    src = _ins.getsource(day_mod.DayScheduleTab._render_calendar)
    assert "_attach_cell_menu(" in src, "月曆格應可直接點選編輯"
