# -*- coding: utf-8 -*-
"""P2-06 第五刀(a) 第二批：勿擾窗、已寄止掛信紀錄、Tk 重繪小工具。

★這一批不只是搬家★ 搬 `_load_alert_email_sent` 的時候發現它用的是
`load_json_dict`（分不出「檔案不存在」與「被鎖住讀不到」），而寫回時會把
記憶體那份原子性地覆蓋上去 —— 開機那一刻被防毒鎖住，使用者先前所有
「已寄過」的紀錄就永久消失。修正與測試都在這裡。
"""
from __future__ import annotations

import ast
import inspect
import io
import json
import os
from datetime import date, datetime, timedelta

import pytest

from cmuh_common import alert_state as als
from cmuh_common import tk_widgets as tw

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 勿擾窗 ────────────────────────────────────────────────────────────────
class TestDndWindow:

    @pytest.mark.parametrize("hour,minute,suppressed", [
        (0, 0, True), (3, 30, True), (7, 59, True),
        (8, 0, False),          # ★結束時刻不含在內★
        (12, 0, False), (23, 59, False),
    ])
    def test_the_clinic_window_is_midnight_to_eight(self, hour, minute,
                                                    suppressed):
        now = datetime(2026, 8, 2, hour, minute)
        assert als.is_within_dnd_window(now, 0, 8) is suppressed

    @pytest.mark.parametrize("hour,suppressed", [
        (22, True), (23, True), (0, True), (5, True),
        (6, False), (12, False), (21, False),
    ])
    def test_a_window_that_crosses_midnight(self, hour, suppressed):
        """★跨午夜要用 or 不是 and★ 22→6 的區間橫跨兩個日期。"""
        now = datetime(2026, 8, 2, hour, 0)
        assert als.is_within_dnd_window(now, 22, 6) is suppressed

    def test_a_zero_length_window_means_all_day(self):
        """start == end 沿用主程式原本的選擇：整天勿擾。

        勿擾只抑制彈窗、不影響寄信，所以往「安靜」的方向解讀是安全的。
        """
        for hour in (0, 9, 17, 23):
            assert als.is_within_dnd_window(datetime(2026, 8, 2, hour, 0),
                                            9, 9) is True

    def test_the_moved_logic_matches_what_the_app_asks(self):
        """★搬家不可以改行為★ main 那支問句要用同一個判準。"""
        import main
        src = inspect.getsource(
            main.AutomationApp._is_notification_suppressed_now)
        assert "_is_within_dnd_window" in src
        assert "NOTIFY_DO_NOT_DISTURB_START_HOUR" in src
        assert "NOTIFY_DO_NOT_DISTURB_END_HOUR" in src
        # 舊的可調設定不可以復活（test_settings_forced_20260713 的同款守衛）
        assert "notify_dnd_start_time" not in src


# ── 已寄止掛信紀錄：讀 ────────────────────────────────────────────────────
class TestLoadingTheSentRecord:

    def _write(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_a_missing_file_is_not_a_failure(self, tmp_path):
        """第一次跑：檔案不存在 → 空紀錄，但★不是讀取失敗★。"""
        got = als.load_alert_email_sent(str(tmp_path / "nope.json"), 21)
        assert got.records == {}
        assert got.unreadable is False

    def test_recent_records_survive_and_old_ones_are_pruned(self, tmp_path):
        today = date(2026, 8, 2)
        p = tmp_path / "sent.json"
        self._write(p, {
            "keep": (today - timedelta(days=5)).isoformat(),
            "drop": (today - timedelta(days=40)).isoformat(),
        })
        got = als.load_alert_email_sent(str(p), 21, today=today)
        assert set(got.records) == {"keep"}
        assert got.unreadable is False

    def test_an_unreadable_file_is_reported_not_swallowed(self, tmp_path,
                                                          monkeypatch):
        """★這是整批最重要的一支★

        `load_json_dict` 對「不存在」與「被鎖住」都回預設值 —— 呼叫端因此
        分不出來，然後拿空紀錄去覆蓋磁碟。
        """
        p = tmp_path / "sent.json"
        self._write(p, {"a": date.today().isoformat()})
        monkeypatch.setattr(als, "load_json_dict_ex",
                            lambda *a, **k: ({}, "error"))

        got = als.load_alert_email_sent(str(p), 21)

        assert got.unreadable is True
        assert got.records == {}

    @pytest.mark.parametrize("status,unreadable", [
        ("ok", False), ("missing", False), ("corrupt", False),
        ("error", True),
    ])
    def test_only_error_counts_as_unreadable(self, tmp_path, monkeypatch,
                                             status, unreadable):
        """★missing/corrupt 不算讀不到★ 那兩種磁碟上本來就沒有可用內容，
        用空紀錄接手是合理的修復；只有「檔案還在卻讀不到」才要保護。"""
        monkeypatch.setattr(als, "load_json_dict_ex",
                            lambda *a, **k: ({}, status))
        got = als.load_alert_email_sent(str(tmp_path / "x.json"), 21)
        assert got.unreadable is unreadable


# ── 已寄止掛信紀錄：寫 ────────────────────────────────────────────────────
class TestSavingTheSentRecord:

    def _write(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_a_normal_run_just_writes_what_it_has(self, tmp_path):
        got = als.records_for_save(str(tmp_path / "sent.json"), {"a": "2026-08-02"},
                                   load_failed=False, retain_days=21)
        assert got.status == als.SAVE_WRITE
        assert got.should_write is True
        assert got.payload == {"a": "2026-08-02"}

    def test_a_run_that_started_blind_merges_instead_of_overwriting(self,
                                                                    tmp_path):
        """★這就是那個 bug★

        開機時讀不到 → 記憶體是空的 → 寄出一封 → 直接寫回就只剩那一封，
        使用者先前所有「已寄過」的紀錄消失 → 止掛提醒整批重寄。
        """
        today = date(2026, 8, 2)
        p = tmp_path / "sent.json"
        self._write(p, {"舊紀錄一": (today - timedelta(days=3)).isoformat(),
                        "舊紀錄二": (today - timedelta(days=1)).isoformat()})

        got = als.records_for_save(str(p), {"這次新寄的": today.isoformat()},
                                   load_failed=True, retain_days=21,
                                   today=today)

        assert got.status == als.SAVE_MERGED
        assert got.should_write is True
        assert set(got.payload) == {"舊紀錄一", "舊紀錄二", "這次新寄的"}

    def test_the_merge_still_applies_the_retention_window(self, tmp_path):
        """★合併不可以讓過期紀錄復活★ 磁碟上那份沒有被過濾過。"""
        today = date(2026, 8, 2)
        p = tmp_path / "sent.json"
        self._write(p, {"太舊": (today - timedelta(days=90)).isoformat()})

        got = als.records_for_save(str(p), {"新": today.isoformat()},
                                   load_failed=True, retain_days=21,
                                   today=today)

        assert set(got.payload) == {"新"}

    def test_still_unreadable_means_do_not_write_at_all(self, tmp_path,
                                                        monkeypatch):
        """★寧可這次不落地，也不要抹掉磁碟上的紀錄★

        代價是重啟後可能重寄一封；相對地覆蓋掉的是全部歷史。
        """
        monkeypatch.setattr(als, "load_json_dict_ex",
                            lambda *a, **k: ({}, "error"))
        got = als.records_for_save(str(tmp_path / "sent.json"), {"新": "2026-08-02"},
                                   load_failed=True, retain_days=21)
        assert got.status == als.SAVE_SKIP
        assert got.should_write is False
        assert got.payload == {}

    def test_every_outcome_has_its_own_wording(self):
        said = {als.AlertSentSave(s, {"a": "b"}).describe()
                for s in (als.SAVE_WRITE, als.SAVE_MERGED, als.SAVE_SKIP)}
        assert len(said) == 3


# ── main 的接線 ──────────────────────────────────────────────────────────
class TestTheAppIsWiredToTheGuard:

    def test_the_writer_asks_before_overwriting(self):
        """★守衛要在寫入咽喉上，不是只存在於模組裡★"""
        import main
        src = inspect.getsource(main.AutomationApp._mark_alert_email_sent)
        assert "_alert_records_for_save" in src, "寫回前沒有問過磁碟現況"
        assert "should_write" in src, "沒有理會「不要寫」的判定"

    def test_the_load_failure_flag_comes_from_the_loader_not_a_constant(self):
        """★[突變驗證抓到] 第一版只斷言字串出現過，那證明不了任何事★

        把 `self._alert_sent_load_failed = _alert_load.unreadable` 改成
        `= False`，兩個字串都還在，測試照樣綠 —— 而守衛已經永遠不會生效。
        改成看那個賦值的【右邊到底是什麼】。
        """
        import textwrap

        import main
        # `getsource` 取 method 會帶著類別的縮排 → 直接 parse 會 IndentationError。
        # ★這一步漏掉時整支測試會【永遠失敗】，而突變驗證會因此全部「轉紅」——
        #   看起來很漂亮，其實什麼都沒驗到。★
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(main.AutomationApp.__init__)))
        assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Attribute)
                           and t.attr == "_alert_sent_load_failed"
                           for t in n.targets)]
        assert assigns, "__init__ 沒有設定 _alert_sent_load_failed"
        for node in assigns:
            assert not isinstance(node.value, ast.Constant), (
                "旗標被寫死成常數 → 守衛永遠不會生效")
            attrs = {n.attr for n in ast.walk(node.value)
                     if isinstance(n, ast.Attribute)}
            assert "unreadable" in attrs, (
                f"旗標不是取自載入結果：{ast.unparse(node.value)}")

    def test_no_write_path_bypasses_the_guard(self):
        """★機械化地掃★ 任何寫 ALERT_EMAIL_SENT_FILENAME 的地方都要走守衛。

        以後有人新增第二條寫入路徑（例如「清除紀錄」按鈕）會在這裡轉紅。
        """
        import main
        tree = ast.parse(inspect.getsource(main))
        # 判準：函式裡同時出現「原子寫入」與「那個檔名常數」就算一條寫入路徑。
        # （不比對 Call 的參數 —— 現行寫法先把路徑存進區域變數，比參數會漏掉。）
        writers = [fn for fn in ast.walk(tree)
                   if isinstance(fn, ast.FunctionDef)
                   and any(isinstance(n, ast.Name)
                           and n.id == "ALERT_EMAIL_SENT_FILENAME"
                           for n in ast.walk(fn))
                   and any(isinstance(n, ast.Name)
                           and n.id == "_atomic_write_json"
                           for n in ast.walk(fn))]
        assert writers, "找不到任何寫入路徑（測試失效了）"
        missing = sorted(fn.name for fn in writers
                         if not any(isinstance(n, ast.Name)
                                    and n.id == "_alert_records_for_save"
                                    for n in ast.walk(fn)))
        assert missing == [], f"這些地方寫了已寄紀錄卻沒過守衛：{missing}"


# ── Tk 重繪小工具 ────────────────────────────────────────────────────────
class _FakeWidget:
    """★重點就是它不是 Tk★ 這兩支原本只有開得起視窗才驗得到。"""

    def __init__(self, **initial):
        self.values = dict(initial)
        self.config_calls = []

    def cget(self, key):
        if key not in self.values:
            raise KeyError(key)
        return self.values[key]

    def config(self, **kwargs):
        self.config_calls.append(dict(kwargs))
        self.values.update(kwargs)


class TestConfigIfChanged:

    def test_nothing_changed_means_no_redraw(self):
        w = _FakeWidget(text="12", fg="#000")
        assert tw.config_if_changed(w, text="12", fg="#000") is False
        assert w.config_calls == []

    def test_one_difference_reapplies_everything(self):
        """★不是只設有變的那一項★ 一起設才不會出現半新半舊的外觀。"""
        w = _FakeWidget(text="12", fg="#000")
        assert tw.config_if_changed(w, text="13", fg="#000") is True
        assert w.config_calls == [{"text": "13", "fg": "#000"}]

    def test_an_unreadable_option_is_treated_as_changed(self):
        """★讀不到現值就無從比較★ 這時跳過重繪會讓畫面停在舊資料上。"""
        w = _FakeWidget(text="12")          # 沒有 fg → cget 會拋
        assert tw.config_if_changed(w, fg="#111") is True
        assert w.config_calls == [{"fg": "#111"}]


class TestApplyCalendarSlotState:

    def _slot(self):
        return {"card": _FakeWidget(), "name_lbl": _FakeWidget(),
                "status_lbl": _FakeWidget()}

    def test_the_same_state_twice_only_draws_once(self):
        slot = self._slot()
        args = ("王醫師", "看診中", "#fff", "#000", ("Arial", 10))
        assert tw.apply_calendar_slot_state(slot, *args) is True
        assert tw.apply_calendar_slot_state(slot, *args) is False
        assert len(slot["card"].config_calls) == 1

    def test_all_three_widgets_move_together(self):
        """★整組一起換★ 只換其中一個會出現「新醫師配舊班別底色」的中間狀態。"""
        slot = self._slot()
        tw.apply_calendar_slot_state(slot, "王醫師", "看診中", "#fff", "#000",
                                     ("Arial", 10))
        assert slot["card"].config_calls[0] == {"bg": "#fff"}
        assert slot["name_lbl"].config_calls[0]["text"] == "王醫師"
        assert slot["status_lbl"].config_calls[0]["text"] == "看診中"

    def test_any_single_field_change_counts_as_a_change(self):
        base = ("王醫師", "看診中", "#fff", "#000", ("Arial", 10))
        for i in range(len(base)):
            slot = self._slot()
            tw.apply_calendar_slot_state(slot, *base)
            changed = list(base)
            changed[i] = "★不同★" if i < 4 else ("Arial", 12)
            assert tw.apply_calendar_slot_state(slot, *changed) is True, (
                f"第 {i} 個欄位改了卻沒重繪")


# ── 分層本身 ─────────────────────────────────────────────────────────────
def test_the_moved_modules_do_not_drag_tkinter_in():
    """`tk_widgets` 不 import tkinter —— 那正是它可以用假 widget 測的原因。"""
    with io.open(os.path.join(REPO_ROOT, "src", "cmuh_common", "tk_widgets.py"),
                 encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                for a in n.names]
    imported += [n.module or "" for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom)]
    assert not any("tkinter" in m for m in imported), imported


def test_the_methods_really_left_main():
    """搬走的就不要留一份在 main.py（`_smart_widget_config` 例外，見下）。"""
    import main
    for gone in ("_load_alert_email_sent",):
        assert not hasattr(main.AutomationApp, gone), f"{gone} 還在 AutomationApp"


def test_the_kept_forwarder_is_a_deliberate_choice():
    """★`_smart_widget_config` 刻意留一層轉發★

    全檔 57 個呼叫點；為了拿掉一行轉發而改 57 處，風險與雜訊都大於收穫。
    這支測試把那個決定與理由釘在一起，免得下次有人以為是漏掉的。
    """
    import main
    src = inspect.getsource(main.AutomationApp._smart_widget_config)
    assert "_config_if_changed" in src, "轉發沒有接到搬出去的那支"
    with io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                 encoding="utf-8") as f:
        main_src = f.read()
    assert main_src.count("self._smart_widget_config(") >= 20, (
        "呼叫點變少了 → 當初「不值得改 57 處」的理由要重新評估")
