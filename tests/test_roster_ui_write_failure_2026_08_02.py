# -*- coding: utf-8 -*-
"""[2026-08-02 補審] 排班 UI 的寫入失敗完全無聲,而請假編輯器還會寫到【錯的人】身上。

`cmuh_common.tk_exception` 刻意只把 Tk callback 例外寫進 log、不跳窗。於是每一條
沒有自己攔例外的寫入路徑,失敗時在畫面上就是「按了沒反應」。會拋的情況都不罕見:

  * 月檔被防毒/同步軟體鎖住(roster 目錄本身就是 git repo,GitSync 背景在跑 git)
  * 另一台電腦剛把該月定案 → FinalizedMonthError
  * 月檔壞掉 → storage 的 schema 守門拋 ValueError

同一份檔案裡的對照很清楚:大動作(套用排班/清除/重算帳本/定案/匯出/停診)每一個
都有 try + messagebox;而【最常按的那幾個互動】(點格改人、鎖定、當月 PGY 名單、
手動編輯時段、請假編輯器)一個都沒有。這條教訓 2026-07-25 已經在 `_save_cfg`
寫下過("Tk callback 例外只會進 log → 使用者以為改好了"),但只修了設定分頁。

★最嚴重的是 LeaveEditor★:切換成員時先 `_commit_current()` 落檔、再 `_load_member()`
換畫面。落檔一拋例外,`_load_member()` 就不會執行 —— 下拉已經顯示 B,格子與
`_loaded_mid` 卻還是 A。使用者以為在編輯 B,按下儲存卻是把 B 的內容寫進 A。
請假直接餵給求解器,寫錯人＝該休的人被排班、該上班的人被跳過。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from roster_edit_helpers import ui_flip_lock  # noqa: E402

from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_day import PHOTO  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402
from cmuh_common.roster.ui import day_tab as day_mod  # noqa: E402
from cmuh_common.roster.ui import duty as duty_mod  # noqa: E402
from cmuh_common.roster.ui.duty import LeaveEditor  # noqa: E402

YM = "2026-08"


@pytest.fixture
def root(tk_root):
    return tk_root


@pytest.fixture(autouse=True)
def noblock(monkeypatch):
    """訊息框改成記錄呼叫,測試才能斷言「有沒有告訴使用者」。"""
    seen = {"error": [], "warning": [], "info": []}
    for mod in (duty_mod, day_mod):
        monkeypatch.setattr(mod.messagebox, "showerror",
                            lambda *a, **k: seen["error"].append(a))
        monkeypatch.setattr(mod.messagebox, "showwarning",
                            lambda *a, **k: seen["warning"].append(a))
        monkeypatch.setattr(mod.messagebox, "showinfo",
                            lambda *a, **k: seen["info"].append(a))
        monkeypatch.setattr(mod.messagebox, "askyesno", lambda *a, **k: True)
    return seen


def _svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": "A", "name": "甲"}, {"id": "B", "name": "乙"}],
        "vs_members": [{"id": "D", "name": "D"}],
        "pgy_members": [{"id": "P1"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
    })
    st.save_holiday_duty({"r": {}, "vs": {}})
    st.save_clinic_template({"template": {"0": {"上午": [{"room": "101"}]}}})
    return RosterService(st)


def _as_tk_would(fn):
    """照 Tk 的方式呼叫:callback 拋出的例外會被 report_callback_exception 吞掉
    (只進 log、不跳窗),使用者毫無所覺地繼續操作。測試必須忠實重現這一點,
    否則例外炸在測試裡,反而看不到「使用者接下來會怎麼被害」。"""
    try:
        fn()
    except Exception:                       # noqa: BLE001,S110
        pass


def _break_saving(monkeypatch, svc, exc=None):
    """讓月檔存檔一律失敗(模擬鎖檔/壞檔/他機剛定案)。"""
    def _boom(*_a, **_k):
        raise exc or OSError("模擬：月檔被同步軟體鎖住")
    monkeypatch.setattr(svc.storage, "save_month", _boom)


# ─── LeaveEditor:寫到錯的人 ────────────────────────────────────────────────
def test_leave_editor_does_not_switch_member_when_the_save_failed(
        root, tmp_path, monkeypatch, noblock):
    """★核心★ 落檔失敗時,畫面【不可】切到下一位成員。

    切過去而資料沒切,就會變成「下拉顯示 B、實際還在編輯 A」。
    """
    svc = _svc(tmp_path)
    ed = LeaveEditor(root, svc, "r", YM, "leave")
    root.update()
    assert ed._loaded_mid == "A"                       # 前提:一開始載入 A

    ed._toggle(date(2026, 8, 5))                       # A 改了東西(尚未落檔)
    _break_saving(monkeypatch, svc)
    ed._combo.current(1)                               # 使用者把下拉切到 B
    _as_tk_would(ed._on_member_change)

    assert ed._loaded_mid == "A", "資料還是 A 的"
    assert ed._combo.current() == 0, "★下拉必須跟著留在 A,不可顯示 B★"
    assert noblock["error"], "★而且要告訴使用者存檔失敗,不能默不作聲★"


def test_leave_editor_screen_and_target_member_never_disagree(
        root, tmp_path, monkeypatch, noblock):
    """★真正的後果:畫面上的人 與 會被寫入的人 不可以是兩個人★

    落檔失敗而畫面照切,下拉就會顯示 B、`_loaded_mid` 卻還是 A。使用者接著替
    「B」勾的每一天,最後都會寫進 A 的請假 —— 而且兩邊都錯:A 被塞了不該有的假,
    B 該有的假一天也沒進去。請假直接餵給求解器,寫錯人＝該休的人被排班。

    這裡釘的是那條不變式本身(而不是某一次的資料結果):任何時候
    下拉顯示的成員都必須就是 `_loaded_mid`,失敗路徑也不例外。
    """
    svc = _svc(tmp_path)
    ed = LeaveEditor(root, svc, "r", YM, "leave")
    root.update()
    ed._toggle(date(2026, 8, 5))

    _break_saving(monkeypatch, svc)
    ed._combo.current(1)
    _as_tk_would(ed._on_member_change)                 # 失敗,但使用者看不出來

    shown = ed._combo.get().split()[0]
    assert shown == ed._loaded_mid, (
        f"★畫面顯示 {shown}、實際會寫進 {ed._loaded_mid}★")

    monkeypatch.undo()                                 # 鎖檔的情況排除了
    ed._toggle(date(2026, 8, 20))                      # 使用者繼續勾
    _as_tk_would(ed._save)

    leaves = (svc.storage.load_month(YM).get("leaves") or {}).get("r") or {}
    assert leaves.get(shown) == ["2026-08-05", "2026-08-20"], (
        f"勾的日期要落在畫面上那位({shown})身上;實際={leaves}")


def test_leave_editor_save_button_reports_failure_and_stays_open(
        root, tmp_path, monkeypatch, noblock):
    """按「儲存」寫入失敗 → 要說出來,而且不可以關窗(關了使用者就以為存好了)。"""
    svc = _svc(tmp_path)
    ed = LeaveEditor(root, svc, "r", YM, "leave")
    root.update()
    ed._toggle(date(2026, 8, 5))
    _break_saving(monkeypatch, svc)

    _as_tk_would(ed._save)

    assert noblock["error"], "存檔失敗必須跳訊息"
    assert ed.winfo_exists(), "★不可關窗★ 關了等於告訴使用者「存好了」"


def test_leave_editor_normal_switch_still_commits(root, tmp_path, noblock):
    """★不可矯枉過正★ 一切正常時,切換成員仍要把上一位的變更落檔並載入下一位。"""
    svc = _svc(tmp_path)
    ed = LeaveEditor(root, svc, "r", YM, "leave")
    root.update()
    ed._toggle(date(2026, 8, 5))
    ed._combo.current(1)
    ed._on_member_change()

    assert ed._loaded_mid == "B"
    leaves = (svc.storage.load_month(YM).get("leaves") or {}).get("r") or {}
    assert leaves.get("A") == ["2026-08-05"]


# ─── 其餘互動:失敗不可無聲 ─────────────────────────────────────────────────
def test_cell_click_reports_a_failed_write(root, tmp_path, monkeypatch, noblock):
    """點月曆格輪換值班者是最常按的互動;寫不進去卻沒有任何提示,
    使用者只會看到「點了沒反應」。"""
    svc = _svc(tmp_path)
    tab = duty_mod.CalendarDutyTab(root, svc, _App())
    root.update()
    _break_saving(monkeypatch, svc)

    _as_tk_would(lambda: tab._set_cell_and_refresh(date(2026, 8, 5), "A", "r"))

    assert noblock["error"], "寫入失敗要跳訊息"


def test_toggle_lock_reports_a_failed_write(root, tmp_path, monkeypatch, noblock):
    svc = _svc(tmp_path)
    svc.set_cell("r", YM, date(2026, 8, 5), "A")
    tab = duty_mod.CalendarDutyTab(root, svc, _App())
    root.update()
    _break_saving(monkeypatch, svc)

    _as_tk_would(lambda: ui_flip_lock(tab, date(2026, 8, 5), "r"))

    assert noblock["error"]


def test_day_edit_dialog_reports_a_failed_write(root, tmp_path, monkeypatch,
                                                noblock):
    """手動編輯某時段:寫不進去時視窗會留著(destroy 在拋出之後),
    但沒有任何訊息 —— 使用者不知道是當掉還是自己按錯。"""
    svc = _svc(tmp_path)
    dlg = day_mod._DayEditDialog(root, svc, YM, date(2026, 8, 3), "上午",
                                 lambda: None)
    root.update()
    dlg._set_slot_text(PHOTO, ["P1"])      # 真的改了東西——沒改就不會存檔(前提)
    _break_saving(monkeypatch, svc)

    _as_tk_would(dlg._save)

    assert noblock["error"], "寫入失敗要跳訊息"
    assert dlg.winfo_exists(), "失敗不可關窗"


class _App:
    ym = YM
