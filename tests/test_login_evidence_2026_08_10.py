# -*- coding: utf-8 -*-
"""[批次SG] 登入失敗時要留下【分辨得出原因】的證據。

★實機 2026-08-10 A01-11106-001★ 會診查詢連續失敗六小時，告警信裡只有：

    「期間按了 1 次「確認」(不同通知視窗 0 個,最後一個 hwnd=None);
      當下看到的視窗:… TFrmLogin(vis=1,en=1) …」

`clicks=1` 但 `distinct_notices=0`、`last_notice_hwnd=None` → 那一下按的不是
「登入後訊息通知」，而是 `_dismiss_blocking_modals` 在登入途中攔到、按掉之後
就消失的一個對話框。★那個對話框寫了什麼是整件事最有價值的一行字，而我們把它
丟掉了★ —— 於是只能在「密碼被改」「帳號被鎖」「字沒打進欄位」之間用猜的。

這一批補上兩半證據：
  ① 登入階段對話框的文字（HIS 拒絕時會跳的那個）；
  ② 帳號/密碼欄位的焦點有沒有被確認（字有沒有真的打進去）。

★隱私邊界（結構上保證）★ 只有 `_wait_main_window_after_login` 傳
`record_text=True`，而那條路在【主畫面交出來之前】就返回 —— 此刻 HIS 還沒給
主畫面、更沒送出任何查詢，畫面上不可能有病人資料。其餘所有呼叫端維持
「不記任何視窗文字」的既有原則。
"""
import ast
import importlib
import io
import os
import re
import sys

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

cq = importlib.import_module("consult_query")

SRC = io.open(os.path.join(REPO_ROOT, "src", "consult_query.py"),
              encoding="utf-8").read()


def _fn_src(name):
    for n in ast.walk(ast.parse(SRC)):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(SRC, n) or ""
    raise AssertionError(f"找不到 {name}")


def _strip_comments(text):
    """★負向斷言先剝註解★（說明「為什麼不可以」的那句話裡就有那個字面）。"""
    return NL.join(re.sub(r"#.*$", "", ln) for ln in text.splitlines())


@pytest.fixture(autouse=True)
def _clean():
    cq._login_dialog_texts.clear()
    cq._login_focus_report.clear()
    cq._login_stealth_mode[0] = False
    yield
    cq._login_dialog_texts.clear()
    cq._login_focus_report.clear()
    cq._login_stealth_mode[0] = False


class TestTheEvidenceSurvivesTheAlertTruncation:
    """★[外審 SG 第 1 輪 P1]★ 告警信只保留前 300 字。

    實機那封信光是「前言 + 視窗清單」就已經逼近上限 —— 證據若接在視窗清單
    後面,正好會被剪掉,整個修正等於沒做。而原本那批測試都只直接檢查
    `_describe_windows_for_diag()` 的回傳值,★沒有一條把它送進真正的告警
    格式裡量過★。
    """

    @staticmethod
    def _realistic_diag(monkeypatch, windows=12):
        monkeypatch.setattr(cq, "_window_texts",
                            lambda h: ["登入失敗", "密碼錯誤，請重新輸入"])
        cq._note_login_dialog(1, "TFMShowMessage", ["確認"])
        cq._note_login_focus("帳號", True)
        cq._note_login_focus("密碼", False)
        monkeypatch.setattr(cq, "find_windows",
                            lambda *a, **k: list(range(windows)))
        monkeypatch.setattr(cq.win32gui, "GetClassName",
                            lambda h: f"TSomeDelphiWindowClass{h}")
        monkeypatch.setattr(cq.win32gui, "IsWindowVisible", lambda h: 1)
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled", lambda h: 1)
        return cq._describe_windows_for_diag({1}, 1, None, set())

    def test_the_evidence_survives_the_300_char_cut(self, monkeypatch):
        diag = self._realistic_diag(monkeypatch)
        msg = str(cq.LoginNotCompleted(
            "登入沒有完成(登入視窗仍在畫面上)—— 請確認帳號密碼是否被院方"
            "改過/停用,以及 HIS 是否連得上。★本次不再重試登入★"
            "(避免同一組帳密被連續送出而逼近鎖定門檻)。" + diag))
        assert "密碼錯誤" in msg[:300], (
            "★證據被 300 字上限剪掉了★:" + msg[:300])
        assert "密碼=★未確認★" in msg[:300]

    def test_the_window_list_is_the_part_that_gets_cut(self, monkeypatch):
        """視窗清單是這串裡最不重要的部分 —— 讓它去當被剪掉的那一段。"""
        diag = self._realistic_diag(monkeypatch)
        assert diag.index("密碼錯誤") < diag.index("當下看到的視窗")
        assert diag.index("帳號=確認") < diag.index("當下看到的視窗")


class TestStealthModeCanStillSeeTheDialog:
    """★[外審 SG 第 2 輪 P2]★ 我第一版只讓診斷「承認可能被藏起來」——
    那沒有解決確認過的缺陷:隱形執行緒照樣每 80ms 把新視窗 SW_HIDE,
    而 `_blocking_dialogs` 照樣只認可見的視窗。認證錯誤訊息既不會被記下來、
    也不會被按掉,登入還是空等滿 120 秒。★把診斷改誠實不等於把證據拿回來。★

    修法選了最小、且【不新增任何點擊面】的那一種:讓隱形執行緒【放過】
    擋路的對話框(判準與偵測共用 `_is_blocking_dialog`,兩邊不可能漂移;
    隱形執行緒本來就只掃我們自己 spawn 的那個 pid)。
    """

    def test_the_stealth_thread_leaves_blocking_dialogs_alone(self):
        body = _strip_comments(_fn_src("_run_with_sw_hide"))
        assert "_is_blocking_dialog(h)" in body, (
            "★隱形執行緒把偵測要找的對話框藏掉了★")
        i = body.index("_is_blocking_dialog(h)")
        j = body.index("hide_window(h)")
        assert i < j, "要先判斷再決定藏不藏"

    def test_the_two_sides_share_one_predicate(self):
        """判準各寫一份的話,改了一邊另一邊就靜默失效。"""
        assert "_is_blocking_dialog(hwnd)" in _strip_comments(
            _fn_src("_blocking_dialogs"))

    def test_a_content_window_is_still_hidden(self):
        """登入視窗/主畫面【要】藏起來 —— 那才是隱形模式存在的理由。"""
        for cls in (cq.LOGIN_CLASS, cq.MAIN_CLASS, cq.CONSULT_CLASS,
                    "TApplication"):
            assert cls in cq._CONTENT_CLASSES

    def test_a_disabled_window_is_not_treated_as_a_dialog(self, monkeypatch):
        """自己都被擋住的視窗不是擋路的那個(2026-08-05 實機的教訓)。"""
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled", lambda h: 0)
        monkeypatch.setattr(cq.win32gui, "GetClassName", lambda h: "TFMTimeOut_1")
        assert cq._is_blocking_dialog(1) is False

    def test_an_enabled_non_content_window_is_a_dialog(self, monkeypatch):
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled", lambda h: 1)
        monkeypatch.setattr(cq.win32gui, "GetClassName", lambda h: "TFMTimeOut_1")
        assert cq._is_blocking_dialog(1) is True

    def test_the_mode_is_stated_in_the_diagnosis(self, monkeypatch):
        """後備模式是每輪完整登入、節奏也不同 —— 判讀時要知道是哪一種。"""
        cq._reset_login_dialog_texts(stealth=True)
        monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [])
        assert "模式=SW_HIDE後備" in cq._describe_windows_for_diag(
            set(), 0, None, set())
        cq._reset_login_dialog_texts(stealth=False)
        assert "模式=SW_HIDE後備" not in cq._describe_windows_for_diag(
            set(), 0, None, set())

    def test_the_mode_comes_from_the_login_wait_itself(self):
        """★不可以另外猜★ `visible_only=False` 就是 SW_HIDE 那條路的簽名。"""
        body = _strip_comments(_fn_src("_wait_main_window_after_login"))
        assert "_reset_login_dialog_texts(stealth=not visible_only)" in body


class TestTheLoginDialogTextIsKept:
    def test_the_text_reaches_the_diagnosis(self, monkeypatch):
        """★核心★ 沒有這行字，「密碼錯誤」與「字沒打進去」分不出來。"""
        monkeypatch.setattr(cq, "_window_texts",
                            lambda h: ["登入失敗", "密碼錯誤，請重新輸入"])
        cq._note_login_dialog(1, "TFMShowMessage", ["確認"])
        monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [])
        diag = cq._describe_windows_for_diag(set(), 1, None, set())
        assert "密碼錯誤" in diag
        assert "TFMShowMessage" in diag

    def test_the_same_message_is_only_recorded_once(self, monkeypatch):
        """那個迴圈每 0.4 秒跑一次 —— 不去重的話同一句話會把 log 洗掉。"""
        monkeypatch.setattr(cq, "_window_texts", lambda h: ["密碼錯誤"])
        for _ in range(50):
            cq._note_login_dialog(1, "TFMShowMessage", ["確認"])
        assert len(cq._login_dialog_texts) == 1

    def test_it_is_bounded_in_count_and_length(self, monkeypatch):
        monkeypatch.setattr(cq, "_window_texts", lambda h: ["x" * 5000])
        for i in range(20):
            monkeypatch.setattr(cq, "_window_texts",
                                lambda h, i=i: [f"訊息{i}" + "x" * 5000])
            cq._note_login_dialog(i, f"C{i}", ["確認"])
        assert len(cq._login_dialog_texts) <= cq._LOGIN_DIALOG_KEEP
        for _c, t in cq._login_dialog_texts:
            assert len(t) <= cq._LOGIN_DIALOG_TEXT_MAX

    def test_an_empty_dialog_is_not_recorded(self, monkeypatch):
        monkeypatch.setattr(cq, "_window_texts", lambda h: ["", "   "])
        cq._note_login_dialog(1, "C", [])
        assert not cq._login_dialog_texts

    def test_a_blocked_read_does_not_break_the_login_loop(self, monkeypatch):
        """讀視窗文字是 raw `GetWindowText` —— 凍結的 HIS 會讓它永不返回。
        這條路在登入的 120 秒預算裡，不可以把預算燒光。"""
        body = _strip_comments(_fn_src("_note_login_dialog"))
        assert "call_with_timeout(" in body, "讀文字要走有界呼叫"

    def test_absence_of_any_dialog_is_stated_explicitly(self, monkeypatch):
        """★「什麼都沒攔到」本身就是證據★ —— 要說出來，不是留白。"""
        monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [])
        diag = cq._describe_windows_for_diag(set(), 0, None, set())
        assert "沒有攔到任何對話框" in diag


class TestTheRecorderIsActuallyWiredIn:
    """★沒有呼叫端＝那個宣稱是假的★

    突變驗證量出來的:把 `_dismiss_blocking_modals` 裡的記錄整段關掉,
    原本那批測試【全綠】—— 它們只驗了「呼叫端傳了 record_text=True」
    與「helper 自己會做事」,沒有一條證明那兩件事真的接在一起。
    """

    @staticmethod
    def _fake_dialog(monkeypatch, order, buttons):
        monkeypatch.setattr(cq, "_blocking_dialogs",
                            lambda pids: [(11, "TFMShowMessage")])
        monkeypatch.setattr(
            cq, "enum_children",
            lambda h: [(21 + i, "TButton", t, (0, 0, 10, 10))
                       for i, t in enumerate(buttons)])
        monkeypatch.setattr(cq, "_note_login_dialog",
                            lambda h, c, b: order.append("record"))
        monkeypatch.setattr(cq, "click_button",
                            lambda h: order.append("click"))

    def test_the_text_is_recorded_before_the_button_is_pressed(self,
                                                               monkeypatch):
        """★順序就是重點★ 按下去之後那個視窗通常就消失了 ——
        記在後面等於什麼都沒記到(2026-08-10 實機就是這樣丟掉證據的)。"""
        order = []
        self._fake_dialog(monkeypatch, order, ["確認"])
        assert cq._dismiss_blocking_modals(pids={1}, record_text=True) == 1
        assert order == ["record", "click"], order

    def test_nothing_is_recorded_when_not_asked(self, monkeypatch):
        order = []
        self._fake_dialog(monkeypatch, order, ["確認"])
        assert cq._dismiss_blocking_modals(pids={1}) == 1
        assert order == ["click"], order

    def test_a_dialog_we_dare_not_click_is_still_recorded(self, monkeypatch):
        """不認得的對話框我們不出手 —— 但它寫了什麼【更】需要留下來。"""
        order = []
        self._fake_dialog(monkeypatch, order, ["離開系統"])
        assert cq._dismiss_blocking_modals(pids={1}, record_text=True) == 0
        assert order == ["record"], order


class TestTheFocusEvidenceIsKept:
    def test_the_focus_result_reaches_the_diagnosis(self, monkeypatch):
        cq._note_login_focus("帳號", True)
        cq._note_login_focus("密碼", False)
        monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [])
        diag = cq._describe_windows_for_diag(set(), 0, None, set())
        assert "帳號=確認" in diag
        assert "密碼=★未確認★" in diag

    def test_typing_reports_whether_focus_was_confirmed(self):
        """`type_via_focus` 要把結果交出來，不然沒有人拿得到它。"""
        assert _fn_src("type_via_focus").splitlines()[0].endswith("-> bool:")
        body = _strip_comments(_fn_src("type_via_focus"))
        assert "return focus_ok" in body

    def test_both_login_paths_record_it(self):
        """★接線★ 兩條登入路徑（隱藏桌面 / SW_HIDE 後備）都要記。"""
        for fn in ("_cold_start_session_impl", "_run_with_sw_hide"):
            body = _strip_comments(_fn_src(fn))
            assert body.count("_note_login_focus(") == 2, fn

    def test_a_later_login_overwrites_rather_than_accumulates(self):
        for _ in range(5):
            cq._note_login_focus("帳號", True)
        assert len(cq._login_focus_report) == 1

    def test_the_dialog_reset_does_not_wipe_the_focus_evidence(self):
        """★順序陷阱★ 焦點是在 `_wait_main_window_after_login` 之【前】打的；
        那個 reset 若連焦點一起清掉，這半邊證據永遠是空的。"""
        cq._note_login_focus("密碼", False)
        cq._reset_login_dialog_texts()
        assert cq._login_focus_report, "reset 把焦點證據一起清掉了"


class TestThePrivacyBoundaryIsStructural:
    def test_only_the_login_wait_asks_for_text(self):
        """★邊界不是靠紀律,是靠只有一個呼叫端傳 True★

        登入完成之後畫面上就是病人清單 —— 那些路徑一律不得記視窗文字。
        """
        callers = [n for n in ast.walk(ast.parse(SRC))
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name)
                   and n.func.id == "_dismiss_blocking_modals"]
        assert callers, "找不到呼叫端(守衛會空集合真空通過)"
        with_text = [c for c in callers
                     if any(k.arg == "record_text" for k in c.keywords)]
        assert len(with_text) == 1, (
            f"只能有一個呼叫端要求記文字,現在有 {len(with_text)} 個")
        wait_src = _strip_comments(_fn_src("_wait_main_window_after_login"))
        assert "record_text=True" in wait_src, (
            "★唯一允許的呼叫端必須是登入等待★")

    def test_it_defaults_to_not_recording(self):
        import inspect
        sig = inspect.signature(cq._dismiss_blocking_modals)
        assert sig.parameters["record_text"].default is False

    def test_the_consult_cycle_never_records_text(self):
        """會診單那條路上有病人資料 —— 一個字都不可以記。"""
        for fn in ("_query_cycle", "_close_session_windows"):
            assert "record_text" not in _strip_comments(_fn_src(fn)), fn


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
