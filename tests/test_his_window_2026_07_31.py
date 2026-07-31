# -*- coding: utf-8 -*-
"""[2026-07-31 P2-06 分層第一刀] cmuh_common/his_window.py。

★這 15 個函式在 main.py 裡的時候，判斷邏輯一行都沒有測試守著★
而它們正是本 repo 反覆出事的那一層：

  * 2026-07-27 診間事故：`IsWindow` 與 `IsWindowVisible` 混用 —— Delphi modal form
    關閉時只是 Hide，「還在不在」永遠為真，F9/F10 全面卡住整個早上。
  * 同 class(#32770) 對話框誤判：別的程式跳出的標準對話框被當成 HIS 警告框自動按「是」。
  * 殘留舊視窗：`_wait_for_window` 找到就回傳 → 整段操作打在上一次沒關的視窗上，
    而且表面完全正常（視窗有、按鈕點得到、log 一路綠）。

三個都是「找錯視窗」，三個都測得到 —— 只要有辦法餵一棵假的視窗樹進去。
搬家時加的 `_user32()` 取得器就是為了這件事。
"""
import ctypes
import sys
from ctypes import wintypes

import pytest

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from cmuh_common import his_window as hw  # noqa: E402


class FakeWindow:
    def __init__(self, hwnd, cls, title="", visible=True, pid=0, parent=0,
                 rect=(0, 0, 10, 10), client=(0, 0, 40, 20), text=""):
        self.hwnd = hwnd
        self.cls = cls
        self.title = title
        self.visible = visible
        self.pid = pid
        self.parent = parent
        self.rect = rect              # (left, top, right, bottom)
        self.client = client
        self.text = text or title


class FakeUser32:
    """一棵假的視窗樹。

    ★`byref(r)._obj`★：被測程式用 `wintypes.RECT()` + `ctypes.byref(r)` 讓 Win32
    回填欄位。假的 API 要模擬回填，只能從 CArgObject 取回原物件 —— CPython 提供
    `._obj`。這是測試專用的細節，寫在這裡以免日後有人以為是誤用。
    """

    def __init__(self, windows, *, post_returns=1, top_level=()):
        self.win = {w.hwnd: w for w in windows}
        self.top_level = list(top_level) or [w.hwnd for w in windows]
        self.posted = []
        self.post_returns = post_returns
        self.calls = []
        self.foreground = 0
        self.focus = 0
        self.attach_calls = []

    # -- 供 find_window_by_class_title 用（它會設 argtypes/restype） ----------
    @property
    def FindWindowExW(self):
        fake = self

        class _F:
            argtypes = None
            restype = None

            def __call__(self, parent, after, cls, title):
                started = not after
                for h in fake.top_level:
                    if not started:
                        if h == after:
                            started = True
                        continue
                    if fake.win[h].cls == cls:
                        return h
                return 0
        if not hasattr(self, "_find_obj"):
            self._find_obj = _F()
        return self._find_obj

    def IsWindowVisible(self, hwnd):
        self.calls.append(("IsWindowVisible", hwnd))
        return 1 if self.win[hwnd].visible else 0

    def IsWindow(self, hwnd):
        self.calls.append(("IsWindow", hwnd))
        return 1 if hwnd in self.win else 0

    def GetWindowThreadProcessId(self, hwnd, out_pid):
        if out_pid is not None:
            out_pid._obj.value = self.win[hwnd].pid
        return 1000 + hwnd            # 假的 thread id

    def GetWindowTextLengthW(self, hwnd):
        return len(self.win[hwnd].text)

    def GetWindowTextW(self, hwnd, buf, _n):
        buf.value = self.win[hwnd].text
        return len(buf.value)

    def GetClassNameW(self, hwnd, buf, _n):
        buf.value = self.win[hwnd].cls
        return len(buf.value)

    def GetWindowRect(self, hwnd, ref):
        left, top, right, bottom = self.win[hwnd].rect
        r = ref._obj
        r.left, r.top, r.right, r.bottom = left, top, right, bottom
        return 1

    def GetClientRect(self, hwnd, ref):
        left, top, right, bottom = self.win[hwnd].client
        r = ref._obj
        r.left, r.top, r.right, r.bottom = left, top, right, bottom
        return 1

    def EnumChildWindows(self, parent, cb, _lparam):
        for w in self.win.values():
            if w.parent == parent and not cb(w.hwnd, 0):
                return 1
        return 1

    def GetWindow(self, hwnd, which):
        GW_CHILD, GW_HWNDNEXT = 5, 2
        if which == GW_CHILD:
            kids = [w.hwnd for w in self.win.values() if w.parent == hwnd]
            return kids[0] if kids else 0
        if which == GW_HWNDNEXT:
            sibs = [w.hwnd for w in self.win.values()
                    if w.parent == self.win[hwnd].parent]
            i = sibs.index(hwnd)
            return sibs[i + 1] if i + 1 < len(sibs) else 0
        return 0

    def GetAncestor(self, hwnd, _ga_parent):
        return self.win[hwnd].parent if hwnd in self.win else 0

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        self.posted.append((hwnd, msg, wparam, lparam))
        rv = self.post_returns
        return rv(len(self.posted)) if callable(rv) else rv

    def GetForegroundWindow(self):
        return self.foreground

    def GetFocus(self):
        return self.focus

    def AttachThreadInput(self, a, b, on):
        self.attach_calls.append((a, b, bool(on)))
        return 1

    def IsIconic(self, _h):
        return 0

    def ShowWindow(self, *_a):
        return 1

    def SetWindowPos(self, *args):
        self.calls.append(("SetWindowPos", args))
        return 1

    def BringWindowToTop(self, h):
        self.calls.append(("BringWindowToTop", h))
        return 1

    def SetForegroundWindow(self, h):
        self.foreground = h
        return 1


class FakeKernel32:
    def __init__(self, tid=999):
        self.tid = tid

    def GetCurrentThreadId(self):
        return self.tid


@pytest.fixture
def fake(monkeypatch):
    def _install(windows, **kw):
        u = FakeUser32(windows, **kw)
        monkeypatch.setattr(hw, "_user32", lambda: u)
        monkeypatch.setattr(hw, "_kernel32", lambda: FakeKernel32())
        return u
    return _install


# ══ 事故 1：IsWindow vs IsWindowVisible（2026-07-27 診間）═══════════════════
def test_a_hidden_window_is_not_found(fake):
    """★Delphi 的 modal form 關閉時只是 Hide，視窗物件還在★

    這一條就是 2026-07-27 讓 F9/F10 整早卡住的形狀：只要判斷用的是「視窗還在嗎」
    而不是「看得見嗎」，關掉的對話框會被永遠當成還開著。
    """
    fake([FakeWindow(1, "#32770", "警告", visible=False)])
    assert hw.find_window_by_class_title("#32770") == 0


def test_a_visible_window_is_found(fake):
    fake([FakeWindow(1, "#32770", "警告")])
    assert hw.find_window_by_class_title("#32770") == 1


def test_it_asks_about_visibility_not_existence(fake):
    """釘住「問的是哪一個」—— 回歸成 IsWindow 時這支會紅。"""
    u = fake([FakeWindow(1, "#32770")])
    hw.find_window_by_class_title("#32770")
    asked = {name for name, *_ in u.calls}
    assert "IsWindowVisible" in asked
    assert "IsWindow" not in asked


# ══ 事故 2：同 class 對話框誤判（require_pid）════════════════════════════════
def test_another_programs_dialog_is_skipped_when_pid_is_required(fake):
    """★別的程式跳出的 #32770 被當成 HIS 警告框去自動按「是」★

    #32770 是 Windows 標準對話框的 class —— 任何程式都可能跳一個出來。
    """
    fake([FakeWindow(1, "#32770", "其他程式", pid=5),
          FakeWindow(2, "#32770", "HIS 的", pid=9)])
    assert hw.find_window_by_class_title("#32770", require_pid=9) == 2


def test_without_require_pid_the_first_one_wins(fake):
    """對照組：不指定 pid 時就是先找到誰算誰 —— 這正是當初的漏洞。"""
    fake([FakeWindow(1, "#32770", "其他程式", pid=5),
          FakeWindow(2, "#32770", "HIS 的", pid=9)])
    assert hw.find_window_by_class_title("#32770") == 1


# ══ 事故 3：上一次流程殘留的舊視窗（exclude / collect）══════════════════════
def test_excluded_hwnds_are_skipped(fake):
    fake([FakeWindow(1, "TFormA"), FakeWindow(2, "TFormA")])
    assert hw.find_window_by_class_title("TFormA", exclude_hwnd=1) == 2
    assert hw.find_window_by_class_title("TFormA", exclude_hwnds=(1, 2)) == 0


def test_collect_snapshots_every_pre_existing_window(fake):
    """★先拍快照、之後排除★ 這是「操作打在上一次沒關的視窗上」的解法。"""
    fake([FakeWindow(1, "TFormA"), FakeWindow(2, "TFormA"),
          FakeWindow(3, "TOther")])
    assert hw.collect_windows_by_class("TFormA") == (1, 2)


def test_collect_returns_empty_when_nothing_is_open(fake):
    fake([FakeWindow(3, "TOther")])
    assert hw.collect_windows_by_class("TFormA") == ()


def test_collect_is_bounded_so_a_broken_finder_cannot_hang(fake, monkeypatch):
    """★上限防禦要真的有上限★ 若 find 永遠回同一個 hwnd（排除失效），
    這個迴圈必須停下來，不能無限長 —— 它跑在熱鍵路徑上。"""
    fake([FakeWindow(1, "TFormA")])
    monkeypatch.setattr(hw, "find_window_by_class_title",
                        lambda *a, **k: 7)      # 永遠回同一個
    assert len(hw.collect_windows_by_class("TFormA")) == 32


# ══ title 過濾 ═══════════════════════════════════════════════════════════════
def test_title_keyword_filters(fake):
    fake([FakeWindow(1, "TForm", "存檔完成"), FakeWindow(2, "TForm", "刪除確認")])
    assert hw.find_window_by_class_title("TForm", "刪除") == 2
    assert hw.find_window_by_class_title("TForm", "不存在的字") == 0


def test_an_empty_title_is_skipped_when_a_keyword_is_required(fake):
    fake([FakeWindow(1, "TForm", ""), FakeWindow(2, "TForm", "刪除確認")])
    assert hw.find_window_by_class_title("TForm", "刪除") == 2


def test_a_win32_exception_returns_zero_not_a_crash(fake, monkeypatch):
    """★稽核/自動化不可以因為 Win32 抽風就炸掉熱鍵緒★"""
    u = fake([FakeWindow(1, "TForm")])

    def boom(hwnd):
        raise OSError("access denied")
    monkeypatch.setattr(u, "IsWindowVisible", boom)
    assert hw.find_window_by_class_title("TForm") == 0


# ══ 走子孫樹 ════════════════════════════════════════════════════════════════
def test_enum_class_in_window_sorts_by_top_then_left(fake):
    fake([FakeWindow(1, "TForm"),
          FakeWindow(2, "TEdit", parent=1, rect=(50, 20, 60, 30)),
          FakeWindow(3, "TEdit", parent=1, rect=(10, 20, 20, 30)),
          FakeWindow(4, "TEdit", parent=1, rect=(0, 5, 10, 15)),
          FakeWindow(5, "TLabel", parent=1)])
    got = hw.enum_class_in_window(1, "TEdit")
    assert [h for h, _t, _left in got] == [4, 3, 2], "先 top 後 left"


def test_enum_class_in_window_ignores_other_classes(fake):
    fake([FakeWindow(1, "TForm"), FakeWindow(2, "TLabel", parent=1)])
    assert hw.enum_class_in_window(1, "TEdit") == []


def test_enum_direct_children_does_not_recurse(fake):
    fake([FakeWindow(1, "TForm"),
          FakeWindow(2, "TPanel", parent=1),
          FakeWindow(3, "TEdit", parent=2)])     # 孫子
    assert hw.enum_direct_children(1) == [2]
    assert hw.enum_direct_children(1, "TEdit") == []
    assert hw.enum_direct_children(2, "TEdit") == [3]


def test_find_first_descendant_by_class(fake):
    fake([FakeWindow(1, "TForm"), FakeWindow(2, "TLabel", parent=1),
          FakeWindow(3, "TEdit", parent=1)])
    assert hw.find_first_descendant_by_class(1, "TEdit") == 3
    assert hw.find_first_descendant_by_class(1, "TMemo") == 0


def test_find_descendant_by_class_text_matches_a_substring(fake):
    fake([FakeWindow(1, "TForm"),
          FakeWindow(2, "TButton", text="全部完成", parent=1)])
    assert hw.find_descendant_by_class_text(1, "TButton", "完成") == 2
    assert hw.find_descendant_by_class_text(1, "TButton", "取消") == 0


def test_exact_text_distinguishes_phrase_from_single_phrase(fake):
    """★這就是它跟 by_class_text 的差別★ 「片語」與「單張片語」同 class，
    用「含子字串」會抓錯（'片語' 是 '單張片語' 的子字串）。"""
    fake([FakeWindow(1, "TForm"),
          FakeWindow(2, "TLabel", text=" 單張片語 ", parent=1,
                     rect=(0, 0, 5, 5)),
          FakeWindow(3, "TLabel", text=" 片語 ", parent=1, rect=(0, 9, 5, 14))])
    assert hw.find_descendant_by_class_text(1, "TLabel", "片語") == 2, \
        "含子字串會先抓到『單張片語』"
    got = hw.find_descendants_by_exact_text(1, "TLabel", "片語")
    assert [h for h, _t, _left in got] == [3], "精確比對只該回『片語』"


def test_exact_text_returns_nothing_when_no_match(fake):
    fake([FakeWindow(1, "TForm"), FakeWindow(2, "TLabel", text="別的", parent=1)])
    assert hw.find_descendants_by_exact_text(1, "TLabel", "片語") == []


# ══ 祖先判定 ════════════════════════════════════════════════════════════════
def test_window_is_ancestor_of_itself_and_of_descendants(fake):
    fake([FakeWindow(1, "TForm"), FakeWindow(2, "TPanel", parent=1),
          FakeWindow(3, "TEdit", parent=2)])
    assert hw.window_is_ancestor(1, 1) is True
    assert hw.window_is_ancestor(1, 3) is True
    assert hw.window_is_ancestor(2, 1) is False


def test_window_is_ancestor_rejects_zero(fake):
    fake([FakeWindow(1, "TForm")])
    assert hw.window_is_ancestor(0, 1) is False
    assert hw.window_is_ancestor(1, 0) is False


def test_window_is_ancestor_stops_on_a_parent_cycle(fake, monkeypatch):
    """★防環要真的防得住★ parent 鏈成環時不可以無限迴圈（熱鍵緒會整個卡死）。"""
    u = fake([FakeWindow(1, "TForm"), FakeWindow(2, "TPanel")])
    monkeypatch.setattr(u, "GetAncestor", lambda h, _g: 2 if h == 2 else 2)
    assert hw.window_is_ancestor(1, 2) is False


# ══ 送訊息 ══════════════════════════════════════════════════════════════════
def test_post_click_uses_the_client_centre_by_default(fake):
    u = fake([FakeWindow(1, "TButton", client=(0, 0, 40, 20))])
    assert hw.post_click_to_control(1) is True
    (_h, down_msg, _w, lparam), (_h2, up_msg, _w2, _l2) = u.posted
    assert (down_msg, up_msg) == (0x0201, 0x0202)
    assert (lparam & 0xFFFF, lparam >> 16) == (20, 10), "client 中心"


def test_post_click_honours_explicit_coordinates(fake):
    u = fake([FakeWindow(1, "TButton")])
    assert hw.post_click_to_control(1, 3, 4) is True
    lparam = u.posted[0][3]
    assert (lparam & 0xFFFF, lparam >> 16) == (3, 4)


def test_post_click_reports_failure_when_windows_refuses(fake):
    """★PostMessage 回 0 = Windows 根本沒收★ 回 True 會讓稽核記成假成功。"""
    fake([FakeWindow(1, "TButton")], post_returns=0)
    assert hw.post_click_to_control(1) is False


def test_post_click_rejects_a_null_hwnd(fake):
    fake([])
    assert hw.post_click_to_control(0) is False


def test_send_chars_aborts_when_the_window_disappears(fake, monkeypatch):
    """★[UD-12] 編輯器中途被關 → 中止並回 False★
    原本不論如何都回 True，代碼欄會殘留半截醫令而沒人知道。"""
    u = fake([FakeWindow(1, "TEdit")])
    monkeypatch.setattr(hw.time, "sleep", lambda _s: None)
    seen = {"n": 0}

    def is_window(_h):
        seen["n"] += 1
        return 1 if seen["n"] <= 2 else 0
    monkeypatch.setattr(u, "IsWindow", is_window)
    assert hw.send_chars_to_window(1, "51017") is False
    assert len(u.posted) == 2, "已送出的字元不會回收 —— 只能中止"


def test_send_chars_aborts_when_post_returns_zero(fake, monkeypatch):
    u = fake([FakeWindow(1, "TEdit")],
             post_returns=lambda n: 0 if n >= 3 else 1)
    monkeypatch.setattr(hw.time, "sleep", lambda _s: None)
    assert hw.send_chars_to_window(1, "51017") is False
    assert len(u.posted) == 3


def test_send_chars_sends_every_character(fake, monkeypatch):
    u = fake([FakeWindow(1, "TEdit")])
    monkeypatch.setattr(hw.time, "sleep", lambda _s: None)
    assert hw.send_chars_to_window(1, "51017") is True
    assert [chr(w) for _h, _m, w, _l in u.posted] == list("51017")


def test_send_chars_rejects_empty_input(fake):
    fake([FakeWindow(1, "TEdit")])
    assert hw.send_chars_to_window(1, "") is False
    assert hw.send_chars_to_window(0, "51017") is False


def test_send_enter_returns_false_only_when_keydown_fails(fake):
    """★提交點★ Delphi 是對 WM_KEYDOWN 做 TranslateMessage —— keydown 一旦被接受，
    醫令【可能已經提交】。此時因 keyup 失敗回 False，呼叫端會誤判「什麼都沒送」→
    重試可能把已提交的醫令再下一次。"""
    fake([FakeWindow(1, "TEdit")], post_returns=0)
    assert hw.send_enter_to_window(1) is False


def test_send_enter_retries_keyup_but_still_reports_submitted(fake):
    u = fake([FakeWindow(1, "TEdit")],
             post_returns=lambda n: 1 if n == 1 else 0)
    assert hw.send_enter_to_window(1) is True, "提交點已過，不可走『沒送出』的路徑"
    assert len(u.posted) == 3, "keydown + keyup 首送 + 補送一次"


def test_send_enter_normal_path_posts_exactly_twice(fake):
    u = fake([FakeWindow(1, "TEdit")])
    assert hw.send_enter_to_window(1) is True
    assert [m for _h, m, _w, _l in u.posted] == [0x0100, 0x0101]


def test_send_enter_rejects_a_null_hwnd(fake):
    fake([])
    assert hw.send_enter_to_window(0) is False


# ══ z-order / 焦點 / IME ════════════════════════════════════════════════════
def test_send_window_to_back_uses_hwnd_bottom_without_activating(fake):
    u = fake([FakeWindow(1, "TForm")])
    assert hw.send_window_to_back(1) is True
    (_name, args), = [c for c in u.calls if c[0] == "SetWindowPos"]
    assert args[1] == 1, "HWND_BOTTOM"
    assert args[6] & 0x0010, "SWP_NOACTIVATE —— 推到底層不可以順便搶焦點"


def test_send_window_to_back_rejects_a_null_hwnd(fake):
    fake([])
    assert hw.send_window_to_back(0) is False


def test_bring_window_front_detaches_the_thread_input_it_attached(fake):
    """★AttachThreadInput 一定要成對★ 只 attach 不 detach 會把兩個 thread 的
    輸入佇列永久綁在一起。"""
    u = fake([FakeWindow(1, "TForm"), FakeWindow(2, "TOther")])
    u.foreground = 2
    hw.bring_window_front(1)
    ons = [c for c in u.attach_calls if c[2]]
    offs = [c for c in u.attach_calls if not c[2]]
    assert len(ons) == len(offs) == 1


def test_bring_window_front_survives_a_win32_failure(fake, monkeypatch):
    u = fake([FakeWindow(1, "TForm")])
    monkeypatch.setattr(u, "SetWindowPos",
                        lambda *_a: (_ for _ in ()).throw(OSError("nope")))
    hw.bring_window_front(1)          # 不可拋


def test_get_thread_focus_attaches_only_across_threads(fake):
    u = fake([FakeWindow(1, "TEdit")])
    u.focus = 42
    assert hw.get_thread_focus(1) == 42
    assert u.attach_calls, "跨 thread 才讀得到 GetFocus"


def test_get_thread_focus_skips_attach_on_the_same_thread(fake,
                                                          monkeypatch):
    u = fake([FakeWindow(1, "TEdit")])
    u.focus = 7
    monkeypatch.setattr(hw, "_kernel32", lambda: FakeKernel32(tid=1001))
    assert hw.get_thread_focus(1) == 7
    assert u.attach_calls == []


def test_get_thread_focus_rejects_zero_and_never_raises(fake, monkeypatch):
    u = fake([FakeWindow(1, "TEdit")])
    assert hw.get_thread_focus(0) == 0
    monkeypatch.setattr(u, "GetWindowThreadProcessId",
                        lambda *_a: (_ for _ in ()).throw(OSError("boom")))
    assert hw.get_thread_focus(1) == 0


class FakeImm32:
    def __init__(self, himc=5):
        self.himc = himc
        self.opened = None
        self.released = []

    def ImmGetContext(self, _h):
        return self.himc

    def ImmSetOpenStatus(self, _himc, on):
        self.opened = on
        return 1

    def ImmReleaseContext(self, h, himc):
        self.released.append((h, himc))
        return 1


def test_force_ime_english_closes_and_always_releases(fake, monkeypatch):
    """★ImmReleaseContext 一定要走到★ 漏放會洩漏 IME context。"""
    u = fake([FakeWindow(1, "TEdit")])
    u.foreground = 1
    imm = FakeImm32()
    monkeypatch.setattr(hw, "_imm32", lambda: imm)
    hw.force_ime_english(1)
    assert imm.opened is False
    assert imm.released == [(1, 5)]


def test_force_ime_english_releases_even_when_setting_fails(fake, monkeypatch):
    u = fake([FakeWindow(1, "TEdit")])
    u.foreground = 1
    imm = FakeImm32()
    monkeypatch.setattr(imm, "ImmSetOpenStatus",
                        lambda *_a: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(hw, "_imm32", lambda: imm)
    hw.force_ime_english(1)
    assert imm.released == [(1, 5)], "設定失敗也要放掉 context"


def test_force_ime_english_does_nothing_without_a_target(fake, monkeypatch):
    u = fake([])
    u.foreground = 0
    imm = FakeImm32()
    monkeypatch.setattr(hw, "_imm32", lambda: imm)
    hw.force_ime_english(0)
    assert imm.opened is None and imm.released == []


def test_force_ime_english_ignores_a_missing_ime_context(fake, monkeypatch):
    u = fake([FakeWindow(1, "TEdit")])
    u.foreground = 1
    imm = FakeImm32(himc=0)
    monkeypatch.setattr(hw, "_imm32", lambda: imm)
    hw.force_ime_english(1)
    assert imm.released == []


# ══ 搬家本身 ════════════════════════════════════════════════════════════════
def test_main_still_exposes_the_old_private_names():
    """★這一刀只搬家、不改呼叫端★

    main.py 內部有 100+ 個呼叫點、tests/ 有多支直接用 `main._find_window_...`。
    別名一旦掉了，那些會變成 AttributeError（而且多半只在實機才會炸）。
    """
    import main
    for name in ("_find_window_by_class_title", "_collect_windows_by_class",
                 "_enum_class_in_window", "_enum_direct_children",
                 "_find_descendants_by_exact_text",
                 "_find_descendant_by_class_text",
                 "_find_first_descendant_by_class", "_window_is_ancestor",
                 "_post_click_to_control", "_send_enter_to_window",
                 "_send_chars_to_window", "_bring_window_front",
                 "_force_ime_english", "_get_thread_focus"):
        assert getattr(main, name) is getattr(
            hw, name.lstrip("_")), f"{name} 沒有接到 his_window"


def test_the_real_module_still_talks_to_the_real_user32():
    """接縫不可以把真的 Win32 換掉 —— 測試換的是取得器，生產路徑仍是 ctypes。"""
    assert hw._user32() is ctypes.windll.user32
    assert hw._kernel32() is ctypes.windll.kernel32


def test_rect_is_the_real_win32_struct():
    """假 user32 靠 `byref(r)._obj` 回填欄位；RECT 換成別的型別時這裡會先紅。"""
    r = wintypes.RECT()
    ref = ctypes.byref(r)
    ref._obj.top = 7
    assert r.top == 7
