# -*- coding: utf-8 -*-
"""[2026-07-30 使用者] 閒置 15 分鐘後螢幕還是不關 → 改用可回讀的「全黑螢幕保護」。

原本有兩層,兩層都【送出去就當成功】:
  1. `powercfg /change monitor-timeout-*` —— 被任何 DISPLAY wake lock 壓住就不會關。
  2. 廣播 `SC_MONITORPOWER=2` —— 只要系統上還有 DISPLAY request,Windows 收到後會
     立刻把螢幕點回來;而一般行程沒有簡單的 API 查得到螢幕現在是開還是關。
     舊版送完就 log「已強制關閉螢幕」→ 實機螢幕沒關、log 卻一直說關了,
     這個問題因此查了兩次都查不出來。

使用者定案:「或是 15 分鐘後進入全黑的螢幕保護程式也可以」。黑幕是我們自己畫的視窗,
不受電源管理影響,而且【可以回讀】(`winfo_ismapped()`)。

★這一檔最重要的不是「黑幕會不會出現」,而是「黑幕一定收得掉」★
這是診間機:一個蓋滿全螢幕又收不掉的視窗會讓醫師無法看病歷。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import screen_blackout as sb  # noqa: E402


class _Idle:
    """可調的閒置秒數來源（None＝查不到）。"""

    def __init__(self, value=9999.0):
        self.value = value

    def __call__(self):
        return self.value

    def input(self, seconds: float = 0.1):
        """模擬「使用者碰了鍵鼠」。"""
        self.value = seconds


class _Busy:
    def __init__(self, value=False):
        self.value = value

    def __call__(self):
        return self.value


RECT = (0, 0, 800, 600)


@pytest.fixture
def made(tk_root):
    """(blackout, idle, busy) —— 用注入的假閒置/忙碌來源，不碰真的 Win32。"""
    idle, busy = _Idle(), _Busy()
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=idle, busy_fn=busy,
                           rects_fn=lambda: [RECT])
    yield bo, idle, busy
    bo.hide()


def _arm(bo, idle):
    """跑一輪輪詢讓黑幕進入待命 —— ★這就是生產路徑★

    [2026-07-31] 黑幕改成由設定頁的按鈕觸發。按鈕是用滑鼠按的，所以黑幕出現的
    瞬間 `GetLastInputInfo` 的閒置是 0、游標又停在黑幕上 —— 沒有待命期的話，
    黑幕會被自己的開啟動作立刻收掉，按鈕等於沒用。
    待命之後的規則【一字未改】：任何輸入都馬上收。
    """
    idle.value = 9999.0
    bo._poll()
    assert bo.armed, "跑過一輪輪詢就該待命了"


# ─── 顯示：回傳的是回讀結果 ───────────────────────────────────────────────
def test_show_reports_the_readback_not_the_intent(made):
    bo, _idle, _busy = made
    assert bo.show() is True, "show() 回的是【回讀 ismapped】，不是「我送出去了」"
    assert bo.active is True


def test_show_is_idempotent(made):
    bo, _idle, _busy = made
    assert bo.show() is True
    assert bo.show() is True, "已經黑著再叫一次不可重建視窗"
    assert bo.active is True


def test_it_covers_the_monitor_it_was_given(made):
    """★診間有雙螢幕機器★ 只用 -fullscreen 會只蓋一個螢幕，另一台仍亮著顯示病歷。"""
    bo, _idle, _busy = made
    bo.show()
    win = bo._wins[0]
    win.update_idletasks()
    assert (win.winfo_width(), win.winfo_height()) == (800, 600)


# ─── ★[2026-07-31 使用者回報] 一個螢幕一片，不是一片蓋全部★ ────────────
# 使用者實機回報：「現在只有副螢幕跟主螢幕 1/3 有黑屏，其他都沒有」。
# 原本是【一個】視窗蓋整個虛擬桌面，有兩個獨立的坑會讓它蓋不滿：
#   1. Tk 的 `wm maxsize` 預設是【主螢幕】大小 —— 超過就被夾掉，而虛擬桌面比主螢幕
#      寬正是雙螢幕的常態。
#   2. 本程式刻意是 system-DPI-aware（Tk 不處理 WM_DPICHANGED）→ 兩台螢幕縮放不同時，
#      Windows 對「與系統 DPI 不同」的那台做座標虛擬化，一個大矩形就會對不上。
def test_one_panel_per_monitor(tk_root):
    """每個螢幕都要有自己的一片，而且各自蓋滿【自己那台】。"""
    rects = [(0, 0, 800, 600), (800, 0, 640, 480)]
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: rects)
    try:
        assert bo.show() is True
        assert bo.panel_count == 2, "兩台螢幕就該有兩片"
        got = []
        for w in bo._wins:
            w.update_idletasks()
            got.append((w.winfo_width(), w.winfo_height()))
        assert got == [(800, 600), (640, 480)]
    finally:
        bo.hide()


def test_a_secondary_monitor_larger_than_the_primary_is_not_clipped(tk_root):
    """★`wm maxsize` 的坑★ Tk 預設把視窗尺寸夾到主螢幕大小。

    副螢幕比主螢幕大時（4K 副 + HD 主），沒有解除上限的話那一片會被截短 ——
    正是使用者看到的「蓋不滿」。這裡用一個比 tk_root 所在螢幕還大的矩形來逼出它。
    """
    big = (0, 0, tk_root.winfo_screenwidth() + 600,
           tk_root.winfo_screenheight() + 400)
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [big])
    try:
        assert bo.show() is True
        win = bo._wins[0]
        win.update_idletasks()
        assert (win.winfo_width(), win.winfo_height()) == (big[2], big[3]), \
            "視窗被夾到主螢幕大小了（wm maxsize 沒解除）"
    finally:
        bo.hide()


def test_all_panels_come_down_together(tk_root):
    """★收黑幕必須整組收★ 留一片在螢幕上就是「收不掉的黑幕」。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 400, 300),
                                             (400, 0, 400, 300),
                                             (800, 0, 400, 300)])
    try:
        assert bo.show() is True and bo.panel_count == 3
        bo.hide()
        assert bo.panel_count == 0 and bo.active is False
        assert bo.active_from_any_thread() is False
    finally:
        bo.hide()


def test_the_gate_still_holds_if_only_one_panel_survives(tk_root):
    """★偏保守★ 只剩一片沒收掉時，熱鍵閘門仍要當成「黑幕還在」——
    否則那一下按鍵會打在使用者看不見的畫面後面。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 400, 300),
                                             (400, 0, 400, 300)])
    try:
        assert bo.show() is True
        bo._wins[0].destroy()          # 模擬其中一片不見了
        assert bo.active is True, "還有一片蓋著就不算收掉"
    finally:
        bo.hide()


# ─── ★[2026-07-31 第二次回報] 查與擺要走同一個座標空間，而且要回讀★ ───────
# 使用者：「現在變成主螢幕有黑 但是副螢幕沒有黑 或是只黑三分之一」。
# 逐螢幕之後主螢幕對了，副螢幕還是 1/3 —— 那個比例就是縮放比。螢幕矩形是
# `GetMonitorInfo` 給的（Win32 空間），視窗卻用 Tk 的 `wm geometry` 擺；本程式是
# system-DPI-aware，兩台螢幕縮放不同時這兩者不是同一個空間。
def test_each_panel_is_placed_with_win32_not_just_tk():
    """查與擺必須走同一個 API —— 否則跨螢幕縮放時對不上。"""
    import inspect
    src = inspect.getsource(sb.ScreenBlackout._place_and_verify)
    assert "SetWindowPos" in src
    assert "GetWindowRect" in inspect.getsource(sb.ScreenBlackout._window_rect)


def test_the_panel_geometry_is_read_back_and_verified(tk_root, caplog):
    """★送出去就當成功，正是這個 repo 反覆出事的形狀★
    擺完要回讀；對不上要留下【兩組數字】，下次才判斷得出是算錯還是被改掉。"""
    import logging as _lg
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    try:
        with caplog.at_level(_lg.INFO):
            assert bo.show() is True
        assert bo.panel_rects() == [(0, 0, 500, 400)], \
            "Win32 回讀到的矩形要等於要求的矩形"
        assert any("要求" in r.getMessage() and "實際" in r.getMessage()
                   for r in caplog.records), "log 要同時留下要求與實際"
    finally:
        bo.hide()


def test_a_panel_that_lands_wrong_is_reported_not_swallowed(tk_root,
                                                            monkeypatch,
                                                            caplog):
    """★蓋不滿要大聲★ 這正是使用者連續回報兩次的情況 ——
    程式必須自己說得出「我要的是什麼、實際是什麼」。"""
    import logging as _lg
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    monkeypatch.setattr(sb.ScreenBlackout, "_window_rect",
                        staticmethod(lambda _h: (0, 0, 166, 400)))  # 只有 1/3
    try:
        with caplog.at_level(_lg.WARNING):
            bo.show()
        msgs = [r.getMessage() for r in caplog.records]
        assert any("沒有蓋到要求的範圍" in m for m in msgs)
        assert any("(0, 0, 500, 400)" in m and "(0, 0, 166, 400)" in m
                   for m in msgs), "要把要求與實際兩組數字都寫出來"
    finally:
        bo.hide()


def test_a_readback_failure_is_reported_rather_than_assumed_fine(tk_root,
                                                                 monkeypatch,
                                                                 caplog):
    """讀不到 ≠ 沒問題（本 repo 的老病灶）。"""
    import logging as _lg
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    monkeypatch.setattr(sb.ScreenBlackout, "_window_rect",
                        staticmethod(lambda _h: None))
    try:
        with caplog.at_level(_lg.WARNING):
            bo.show()
        assert any("無法確認蓋滿" in r.getMessage() for r in caplog.records)
    finally:
        bo.hide()


def test_placement_failure_does_not_prevent_the_blackout(tk_root, monkeypatch,
                                                         caplog):
    """★擺不動也不可以變成「完全沒有黑幕」★

    Tk 已經先用 `wm geometry` 擺過一次了；Win32 那一步只是把它校正到正確的座標
    空間。校正失敗時退回 Tk 的擺法（蓋一部分）遠優於什麼都沒有 —— 但要留 warning。
    """
    import logging as _lg
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    monkeypatch.setattr(sb.ctypes.windll.user32, "SetWindowPos",
                        lambda *_a: (_ for _ in ()).throw(OSError("掛了")),
                        raising=False)
    try:
        with caplog.at_level(_lg.WARNING):
            assert bo.show() is True, "校正失敗不可以退化成「沒有黑幕」"
        assert any("SetWindowPos 失敗" in r.getMessage()
                   for r in caplog.records)
    finally:
        bo.hide()


def test_monitor_rects_falls_back_instead_of_returning_nothing(monkeypatch):
    """★列舉不到螢幕不可以回空清單★ 那會讓 `_create` 直接放棄，等於沒有黑幕。"""
    import cmuh_common.platform_win as pw
    monkeypatch.setattr(pw, "get_active_physical_monitors", lambda: [])
    rects = sb.monitor_rects()
    assert len(rects) == 1
    assert rects[0][2] > 0 and rects[0][3] > 0


def test_monitor_rects_survives_an_enumeration_failure(monkeypatch):
    import cmuh_common.platform_win as pw
    monkeypatch.setattr(pw, "get_active_physical_monitors",
                        lambda: (_ for _ in ()).throw(OSError("boom")))
    rects = sb.monitor_rects()
    assert len(rects) == 1 and rects[0][2] > 0


def test_the_default_rect_never_reports_a_zero_sized_desktop():
    """取不到虛擬桌面尺寸時要退回一個【能蓋住東西】的大小，不可回 0（等於沒黑幕）。"""
    x, y, w, h = sb.virtual_screen_rect()
    assert w > 0 and h > 0


# ─── 不可干擾自動化 ────────────────────────────────────────────────────────
def test_it_refuses_while_this_process_is_running_automation(made):
    """★主程式有「螢幕擷取 + OCR」路徑（F2/F3 照光卡號）★
    一個 topmost 黑視窗會讓它擷到全黑 → 自動化跑著就不黑屏。"""
    bo, _idle, busy = made
    busy.value = True
    assert bo.show() is False
    assert bo.active is False


def test_the_poll_takes_it_down_when_automation_starts(made):
    bo, _idle, busy = made
    bo.show()
    busy.value = True
    bo._poll()
    assert bo.active is False, "自動化開始跑就要立刻收黑幕"


# ─── ★收得掉★：三條退場路徑 ──────────────────────────────────────────────
def test_a_keypress_takes_it_down(made):
    bo, idle, _busy = made
    bo.show()
    _arm(bo, idle)
    bo._on_wake()
    assert bo.active is False


# ─── ★★使用者定案：任何人在電腦前的行為都馬上收★★ ─────────────
# 原話：「全黑的螢幕要能在使用者一移動滑鼠或是一動到任何鍵盤
# (有任何使用者有在電腦前的行為)」。
#
# ★這推翻了外審第 1/2/3 輪的 P3 finding★：外審堅持要有 25px 飄移門檻（把我第
# 一版自己發明的東西當成需求），並因此衍生出游標位移比對、last-input tick
# 事件身分、硬上限……一堆複雜度。使用者明確要求相反的行為，而這是他的診間機。
def test_any_mouse_movement_takes_it_down_however_small(made):
    """★使用者定案★ 不問移了幾 px，滑鼠一動就收。"""
    bo, idle, _busy = made
    bo.show()
    _arm(bo, idle)
    bo._on_wake(type("E", (), {"x_root": 501, "y_root": 500})())   # 移了 1px
    assert bo.active is False


def test_there_is_no_drift_tolerance_left(made):
    """門檻要真的不存在 —— 不只是調小。"""
    assert not hasattr(sb, "MOTION_TOLERANCE_PX")
    assert not hasattr(sb, "FRESH_INPUT_EVENTS_BEFORE_FORCE_HIDE")
    assert not hasattr(sb, "cursor_pos"), "不再需要游標位置"
    assert not hasattr(sb, "last_input_tick"), "不再需要事件身分推論"


def test_every_input_binding_goes_straight_to_wake():
    """四種 Tk 輸入事件都直接收黑幕，沒有任何中間判斷。"""
    import inspect
    src = inspect.getsource(sb.ScreenBlackout._create)
    assert '("<Key>", "<Button>", "<MouseWheel>", "<Motion>")' in src
    assert "_on_motion" not in src, "不可再有另一條有門檻的滑鼠路徑"


def test_the_poll_takes_it_down_on_any_fresh_input(made):
    """失效保險輪詢也一樣：idle 一掉下來就收，不再分辨是滑鼠還是鍵盤。"""
    bo, idle, _busy = made
    bo.show()
    _arm(bo, idle)
    idle.input(0.5)
    bo._poll()
    assert bo.active is False


def test_the_poll_does_not_second_guess_repeated_input(made):
    """連續有輸入時也不需要任何「硬上限」—— 第一下就收了。"""
    bo, idle, _busy = made
    bo.show()
    _arm(bo, idle)
    for v in (0.9, 0.8, 0.7):
        idle.value = v
        if bo.active:
            bo._poll()
    assert bo.active is False


# ─── ★[2026-07-31] 待命：按鈕的那一下不算「有人回來了」★ ──────────────
def test_the_click_that_opened_it_does_not_close_it(made):
    """★這條是「按鈕能不能用」的關鍵★

    黑幕改成設定頁按鈕觸發。按鈕是用滑鼠按的 → 出現的瞬間 `GetLastInputInfo`
    的閒置是 0，游標又停在黑幕上隨時會送 `<Motion>`。沒有待命期的話，黑幕會被
    自己的開啟動作立刻收掉 —— 按下去畫面閃一下就沒了。
    """
    bo, idle, _busy = made
    idle.value = 0.0                      # 剛剛才按了按鈕
    bo.show()
    assert bo.armed is False
    bo._on_wake()                         # 游標停在黑幕上送出的 <Motion>
    assert bo.active is True, "開啟黑幕的那一下操作不可以把它收掉"
    bo._poll()                            # idle 仍是 0 → 還不待命
    assert bo.active is True and bo.armed is False


def test_it_arms_once_the_user_lets_go(made):
    bo, idle, _busy = made
    idle.value = 0.0
    bo.show()
    bo._poll()
    assert bo.armed is False
    idle.value = sb.ARM_IDLE_SEC          # 放手了
    bo._poll()
    assert bo.armed is True and bo.active is True
    bo._on_wake()
    assert bo.active is False, "待命之後任何輸入都要馬上收"


def test_the_arming_threshold_cannot_be_below_the_dismiss_threshold():
    """★這是我第一版寫錯的地方，釘住它★

    收黑幕的條件是 `idle < INPUT_FRESH_SEC`。待命門檻若比它小（我寫過 0.8），
    同一輪輪詢會先待命、再馬上收 —— 而且無論如何都留不住（輪詢間隔 250ms，
    閒置時間不可能從 <0.8 直接跳到 ≥2.0）。按鈕會完全沒用。
    """
    assert sb.ARM_IDLE_SEC >= sb.INPUT_FRESH_SEC


def test_the_blackout_survives_a_normal_button_press(made):
    """★端到端：按下按鈕、把手放開 → 黑幕留著★

    這支測的是整條路徑而不是單一門檻 —— 上面那個門檻寫錯時，這支也會紅。
    """
    bo, idle, _busy = made
    idle.value = 0.0                       # 剛按下按鈕
    assert bo.show() is True
    for t in (0.25, 0.5, 1.0, 1.5):        # 手放開，閒置時間往上爬
        idle.value = t
        bo._poll()
        assert bo.active is True, f"閒置 {t}s 時不該收（使用者還沒回來）"
    idle.value = 30.0                      # 真的離開了
    bo._poll()
    assert bo.active is True and bo.armed is True
    idle.value = 0.1                       # 回來碰了一下
    bo._poll()
    assert bo.active is False


def test_it_arms_anyway_after_the_hard_cap(made, monkeypatch):
    """★硬上限★ 手一直在動也不可以變成收不掉的全螢幕黑窗。"""
    bo, idle, _busy = made
    clock = {"t": 1000.0}
    monkeypatch.setattr(sb.time, "monotonic", lambda: clock["t"])
    idle.value = 0.0                      # 從頭到尾都有輸入
    bo.show()
    bo._poll()
    assert bo.armed is False
    clock["t"] += sb.ARM_MAX_SEC
    bo._poll()
    assert bo.armed is True, "超過硬上限就要待命，不管有沒有人一直碰"
    assert bo.active is False, "待命的同時 idle 也很新 → 這一輪就收掉"


def test_arming_resets_for_the_next_blackout(made):
    """每次按按鈕都要重新走一次待命 —— 不可以沿用上一次的待命狀態。"""
    bo, idle, _busy = made
    bo.show()
    _arm(bo, idle)
    bo.hide()
    idle.value = 0.0
    bo.show()
    assert bo.armed is False


def test_an_unknown_idle_time_still_takes_it_down_before_arming(made):
    """★待命不可以變成新的卡死路徑★ 查不到閒置時間時照樣收 —— 那是
    「我確定沒人在用」的依據沒了，繼續蓋著＝醫師回來卻看不到病歷。"""
    bo, idle, _busy = made
    idle.value = 0.0
    bo.show()
    assert bo.armed is False
    idle.value = None
    bo._poll()
    assert bo.active is False


def test_automation_still_takes_it_down_before_arming(made):
    """自動化開始跑也一樣：待命與否都要立刻收（否則 OCR 會擷到全黑）。"""
    bo, idle, busy = made
    idle.value = 0.0
    bo.show()
    assert bo.armed is False
    busy.value = True
    bo._poll()
    assert bo.active is False


# ─── ★外審第 1 輪★ 負原點的雙螢幕排列 ────────────────────────
@pytest.mark.parametrize("rect", [
    (-1920, 0, 3840, 1080),        # 副螢幕在主螢幕左邊
    (0, -1080, 1920, 2160),        # 副螢幕在主螢幕上方
    (-1920, -1080, 3840, 2160),    # 左上
])
def test_it_still_appears_when_the_virtual_desktop_origin_is_negative(
        tk_root, rect):
    """`f"...+{x}+{y}"` 會組出 `3840x1080+-1920+0` 這種不合法的幾何字串 →
    拋例外 → 黑幕在【雙螢幕常見排列】下永遠不會出現。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [rect])
    try:
        assert bo.show() is True, f"虛擬桌面原點 {rect[:2]} 時黑幕沒出現"
    finally:
        bo.hide()


# ─── ★外審第 1 輪★ 熱鍵閘門在非 Tk 緒上查狀態 ───────────────
def test_the_state_can_be_queried_from_a_non_tk_thread(made):
    """★這條就是「測試給假信心」的那一條★

    熱鍵閘門是在 `keyboard` 的 hook 緒上跑的。`active` 會呼叫 `winfo_*`，
    從別的緒呼叫 tkinter 會拋 `RuntimeError: main thread is not in main loop`
    → 閘門把例外當成「沒黑幕」 → 整個閘門在正式環境完全不生效。
    而在 Tk 緒上呼叫的測試全綠。
    """
    import threading

    bo, _idle, _busy = made
    bo.show()
    out = {}

    def _hook_thread():
        out["blacked"] = bo.active_from_any_thread()

    t = threading.Thread(target=_hook_thread, name="FakeHookThread")
    t.start()
    t.join(timeout=5)
    assert out.get("blacked") is True,         "從非 Tk 緒查不到黑幕 → 熱鍵閘門在正式環境不生效"


def _cross_thread_state(bo) -> bool:
    """從別的緒問閘門（與熱鍵回呼同一條路徑，會消耗一次性 token）。"""
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.update(
        blacked=bo.consume_wake_gate()))
    t.start()
    t.join(timeout=5)
    return out.get("blacked")


def test_the_wake_keystroke_is_still_gated_right_after_the_blackout_is_gone(
        made):
    """★[外審第 2 輪] 這一下按鍵的競態★

    熱鍵回呼（`keyboard` hook 緒）與 Tk 的 <Key> 處理是並行的：同一下按鍵，Tk 可能
    先把黑幕拆掉、HWND 清掉，熱鍵回呼才來問閘門 → 只看 HWND 就會放行，那一下 F1/F9
    就打進【已經回到前景的 HIS】，而醫師還沒看到畫面。
    故閘門還要看「剛剛才黑過嗎」（`WAKE_GRACE_SEC`）。
    """
    bo, _idle, _busy = made
    bo.show()
    bo.hide()                                    # 模擬 Tk 先收掉
    assert bo._hwnds == (), "測試前提：HWND 已經清掉了"
    assert _cross_thread_state(bo) is True, (
        "★黑幕剛收掉的那一瞬，熱鍵仍必須被吃掉★")


def test_only_the_wake_keystroke_is_eaten_not_the_next_one(made):
    """★[外審第 3 輪] 一次性 token，不是時間窗★

    舊版只看「1.5 秒內黑過」→ 醫師按 F1 喚醒、馬上再按一次 F1，第二下也被吃掉，
    變成「要等一下才有反應」。喚醒的那一下只有【一下】。
    """
    bo, _idle, _busy = made
    bo.show()
    bo.hide()
    assert _cross_thread_state(bo) is True, "喚醒的那一下要被吃掉"
    assert _cross_thread_state(bo) is False, (
        "★第二下就必須正常動作★（不可把 1.5 秒內的每一下都吃掉）")


def test_a_hotkey_already_eaten_while_black_does_not_earn_a_second_token(made):
    """★[外審第 4 輪] hook 緒先跑的順序不可吃掉兩下★

    第一下 F1 看到黑幕還在 → 擋下（這就是「喚醒的那一下」）；Tk 接著拆窗。
    若拆窗又發一張新 token，下一下 F1 也會被吃掉 —— 医師按了兩下都沒反應。
    """
    bo, _idle, _busy = made
    bo.show()
    assert _cross_thread_state(bo) is True, "黑幕還蓋著 → 擋下（喚醒的那一下）"
    bo.hide()
    assert _cross_thread_state(bo) is False, (
        "★黑幕期間已經吃掉一下了 → 拆窗不可再發新 token★")


def test_a_destroy_racing_the_gate_check_leaves_no_orphan_token(made):
    """★[外審第 5 輪] `active` 是在鎖【外面】問的★

    hook 緒問完「黑幕還在嗎」→ 得到 True → 正要去拿鎖；`_destroy()` 剛好卡在這中間
    跑完，那時 `_eaten_this_blackout` 還沒設起來，所以它照樣發了一張 token。
    若只設旗標而不清 token，那張孤兒 token 會把【下一下】熱鍵也吃掉。
    """
    bo, _idle, _busy = made
    bo.show()

    real_check = bo.active_from_any_thread
    fired = {"n": 0}

    def _racing_check():
        got = real_check()
        if got and fired["n"] == 0:
            fired["n"] = 1
            bo._destroy()       # ★就卡在「問完」與「拿鎖」之間★
        return got

    bo.active_from_any_thread = _racing_check      # type: ignore[method-assign]
    assert bo.consume_wake_gate() is True, "這一下就是喚醒的那一下，要吃掉"
    bo.active_from_any_thread = real_check         # type: ignore[method-assign]

    assert fired["n"] == 1, "測試前提不成立：沒有製造出交錯"
    assert bo.consume_wake_gate() is False, (
        "★下一下熱鍵必須正常動作（不可被孤兒 token 吃掉）★")


def test_multiple_presses_while_black_are_all_blocked(made):
    """黑幕蓋著時每一下都要擋（不是只擋第一下）。"""
    bo, _idle, _busy = made
    bo.show()
    assert _cross_thread_state(bo) is True
    assert _cross_thread_state(bo) is True
    assert _cross_thread_state(bo) is True


def test_the_wake_grace_expires_and_cannot_wedge_the_hotkeys(made,
                                                             monkeypatch):
    """★寬限是時間界定的★ 過了就一定放行 —— 不可能卡住所有 F1-F12。"""
    bo, _idle, _busy = made
    bo.show()
    bo.hide()
    base = sb.time.monotonic()
    monkeypatch.setattr(sb.time, "monotonic",
                        lambda: base + sb.WAKE_GRACE_SEC + 0.1)
    assert _cross_thread_state(bo) is False


def test_a_blackout_that_never_appeared_does_not_gate_hotkeys(tk_root,
                                                             monkeypatch):
    """沒真的黑過就不該有寬限（否則每次「本輪不黑屏」都白白吃掉一次熱鍵）。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(),
                           busy_fn=_Busy(True), rects_fn=lambda: [RECT])
    assert bo.show() is False
    assert _cross_thread_state(bo) is False


def _body_without_docstring(fn) -> str:
    """回傳函式的【可執行程式碼】，剝掉 docstring 與註解。

    ★用 ast 而不是字串處理★：這個函式的 docstring 本身就在解釋「不可以用
    `winfo_*`」，只剝 `#` 註解會被自己的 docstring 騙過去（同一個坑本檔已踩過一次）。
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    body = getattr(node, "body", [])
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body.pop(0)
    return ast.unparse(node)


def test_the_cross_thread_state_does_not_touch_tk():
    """閘門的查詢路徑不可出現任何 winfo_* —— 那就是跨緒碰 Tk。"""
    code = _body_without_docstring(sb.ScreenBlackout.active_from_any_thread)
    assert "winfo" not in code
    assert "IsWindowVisible" in code,         "要看 IsWindowVisible 而不是只看 IsWindow（2026-07-27 診間事故）"


def test_the_poll_takes_it_down_even_when_no_tk_event_arrives(made):
    """★核心失效保險★

    `overrideredirect` 視窗在某些情況拿不到焦點 → 只靠 <Key> 綁定會收不掉。
    輪詢看的是 `GetLastInputInfo`（全 session 層級），不管按鍵送到哪個視窗。
    """
    bo, idle, _busy = made
    bo.show()
    _arm(bo, idle)
    idle.input(0.5)                      # 有人碰了鍵鼠，但事件沒送到我們
    bo._poll()
    assert bo.active is False


def test_an_unknown_idle_time_takes_it_down(made):
    """★查不到閒置時間就收起來★

    黑幕是「我確定沒人在用」才該蓋著的東西；失去那個依據還繼續蓋，
    就是醫師回來卻看不到病歷。收起來最壞只是「螢幕沒關」＝修好之前的既有狀態。
    """
    bo, idle, _busy = made
    bo.show()
    idle.value = None
    bo._poll()
    assert bo.active is False


def test_the_poll_keeps_it_up_while_still_idle(made):
    bo, idle, _busy = made
    bo.show()
    idle.value = 9999.0
    bo._poll()
    assert bo.active is True, "還在閒置就不該自己收掉"


def test_hide_when_not_shown_is_harmless(made):
    bo, _idle, _busy = made
    bo.hide()
    bo.hide(reason="再一次")
    assert bo.active is False


# ─── ★閘門絕不可卡在「黑著」★ ─────────────────────────────────────────────
def test_active_is_false_once_the_window_is_gone(made):
    """★這條守的是所有 F1-F12★

    熱鍵閘門看的就是 `active`。若它是一個「記下來的旗標」，任何一條例外路徑都可能
    把它留在 True，結果所有熱鍵從此失效而沒人知道原因。所以它必須是【從視窗狀態
    算出來的】—— 視窗被別人銷毀掉也要立刻回 False。
    """
    bo, _idle, _busy = made
    bo.show()
    for w in bo._wins:                   # 繞過 hide()，模擬視窗被別人弄掉
        w.destroy()
    assert bo.active is False


def test_active_is_false_when_the_window_object_misbehaves(made):
    bo, _idle, _busy = made
    bo.show()

    class _Broken:
        def winfo_exists(self):
            raise RuntimeError("Tcl 沒了")

    bo._wins = [_Broken()]
    assert bo.active is False, "問視窗狀態時炸掉也必須回 False（不可卡住熱鍵）"


def test_it_refuses_to_stay_up_without_a_failsafe_poll(tk_root, monkeypatch):
    """★排不到 after 就不黑屏★ 沒有失效保險輪詢的黑幕是收不掉的黑幕。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [RECT])
    monkeypatch.setattr(tk_root, "after",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            RuntimeError("排不到")))
    assert bo.show() is False
    assert bo.active is False


def test_it_does_not_grab_input():
    """★不可 grab_set★ 抓住輸入的黑幕若沒收乾淨會把整台機器鎖死。

    ★要先剝掉註解才比對★：模組裡有一行註解在【解釋為什麼不用 grab_set】，
    直接對整份原始碼比對會被自己的註解騙過去（這個坑之前踩過一次）。
    """
    import inspect
    code = "\n".join(
        line.split("#", 1)[0] for line in inspect.getsource(sb).splitlines())
    assert "grab_set" not in code


# ─── ★[2026-08-01 外審 P1] 擺錯視窗:winfo_id() 不是最外層★ ────────────────
# 使用者連續兩次回報「副螢幕沒黑／只黑三分之一」,前兩次都改錯了地方。真正的成因是
# `SetWindowPos` 下在 Tk 的【子】視窗上,而使用者看到的是外面那層 wrapper ——
# 於是那一整段 Win32 擺放對畫面【毫無作用】,黑幕就停在 Tk 自己擺的位置。
#
# 本機實測(overrideredirect Toplevel,先 geometry 200x150+50+50,
# 再對 winfo_id() 下 SetWindowPos 到 700,300,400,250):
#     子視窗 rect = (750, 350, 400, 250)   ← 座標相對父視窗,所以整整差了 (50,50)
#     最外層 rect = ( 50,  50, 200, 150)   ← 使用者看到的這個動都沒動
#
# ★而且回讀也讀錯視窗★ 原本回讀的同樣是 winfo_id(),它只差 50px,對真正的失敗
# (外層完全沒動)毫無察覺 —— 守衛讀錯對象時比沒有守衛更危險,因為它讓人以為驗過了。
@pytest.mark.skipif(os.name != "nt", reason="Tk 的 wrapper 階層是 Windows 專有")
def test_the_outermost_hwnd_is_not_the_one_winfo_id_returns(tk_root):
    """★這就是前兩次都沒修對的那件事★"""
    import tkinter as tk
    win = tk.Toplevel(tk_root)
    try:
        win.overrideredirect(True)
        win.geometry("200x150+50+50")
        win.update_idletasks()
        outer = sb._toplevel_hwnd(win)
        assert outer, "拿不到最外層 HWND"
        assert outer != int(win.winfo_id()), \
            "winfo_id() 若真的就是最外層,這個修正才會是多餘的"
        import ctypes
        assert int(ctypes.windll.user32.GetParent(outer) or 0) == 0, \
            "最外層再往上不該還有父視窗"
    finally:
        win.destroy()


@pytest.mark.skipif(os.name != "nt", reason="需要真的 Win32 視窗")
def test_a_panel_at_a_nonzero_origin_actually_lands_there(tk_root, caplog):
    """非零原點(副螢幕的原點永遠不是 0)時仍要落在要求的矩形上。

    ★誠實記下這支【抓不到】上面那個 P1★
    量過了:突變回 `winfo_id()` 之後這支照樣綠。因為建立流程跑完時,Tk 會把子視窗
    重新鋪滿 wrapper 的 client 區,子視窗與最外層完全重合 —— 單螢幕開發機上
    Tk 的 `wm geometry` 本來就擺對了,錯的程式與對的程式看起來一模一樣。
    真正釘住 P1 的是下面兩支(`..._holds_the_outermost_hwnds`／
    `..._uses_the_resolved_toplevel_hwnd`),它們比對的是「動到哪個 HWND」。

    那這支留著做什麼:它涵蓋的是非零原點這條路本身(副螢幕、負座標),
    跟 P1 是兩件事,不該因為 P1 有別人釘就把它拿掉。
    """
    import logging as _lg
    want = (300, 200, 500, 400)
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [want])
    try:
        with caplog.at_level(_lg.WARNING):
            assert bo.show() is True
        assert bo.panel_rects() == [want], \
            "非零原點時仍要落在要求的矩形上(擺錯視窗的話會差一個父視窗的位移)"
        assert not any("沒有蓋到要求的範圍" in r.getMessage()
                       for r in caplog.records)
    finally:
        bo.hide()


@pytest.mark.skipif(os.name != "nt", reason="需要真的 Win32 視窗")
def test_the_hotkey_gate_holds_the_outermost_hwnds(tk_root):
    """★閘門也要拿對視窗★ `_hwnds` 是給【非 Tk 緒】用 IsWindowVisible 判斷
    「現在黑著沒有」的唯一依據。存成子視窗的話,問的就不是使用者看到的那個。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(300, 200, 500, 400)])
    try:
        assert bo.show() is True
        assert bo._hwnds, "要記得住 HWND,否則熱鍵閘門會失效"
        # strict=True：一片黑幕對一個 HWND，長度對不上本身就是 bug
        for win, hwnd in zip(bo._wins, bo._hwnds, strict=True):
            assert hwnd == sb._toplevel_hwnd(win)
            assert hwnd != int(win.winfo_id())
    finally:
        bo.hide()


def test_placement_uses_the_resolved_toplevel_hwnd():
    """釘住呼叫端:hwnd 由 `_toplevel_hwnd()` 解析後傳進去,不是在裡面自己抓。

    (原本 `_place_and_verify` 自己呼叫 `win.winfo_id()` —— 抓錯對象的地方就在那。)
    """
    import ast
    import inspect
    import textwrap

    def _code_only(fn) -> str:
        """只看【會執行的程式碼】—— docstring 與註解不算數。

        (這一支第一次就踩到:`_place_and_verify` 的 docstring 裡本來就寫著
         「不可以是 win.winfo_id()」,於是斷言比對到自己的說明文字。
         這個 repo 反覆出現的形狀,所以用 AST 剝掉而不是靠眼睛。)
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    node.body = body[1:] or [ast.Pass()]
        return ast.unparse(ast.fix_missing_locations(tree))

    assert "_toplevel_hwnd(win)" in _code_only(sb.ScreenBlackout._create)
    assert "winfo_id" not in _code_only(sb.ScreenBlackout._place_and_verify), \
        "擺放不可以再自己去拿 winfo_id()"


# ══════════════════════════════════════════════════════════════════════════
# [2026-08-01 外部 review P1-04] 成功判準 + destroy 回讀
# ══════════════════════════════════════════════════════════════════════════
def test_a_partially_covered_screen_is_not_success(tk_root, monkeypatch,
                                                   caplog):
    """★核心★ 「主螢幕全黑、副螢幕只黑 1/3」不可以回 True。

    原本 `show()` 用 `self.active`，而那是 `any(...)` —— 兩片都 mapped 就成立。
    對一個用來遮住病歷的東西，那等於宣告成功卻沒有遮住：醫師以為螢幕關了，
    離開診間，而另一台螢幕還亮著上一個病人的病歷。
    """
    import logging as _lg
    want = [(0, 0, 400, 300), (400, 0, 400, 300)]
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: want)
    real = sb.ScreenBlackout._window_rect

    def _second_panel_is_short(hwnd):
        got = real(hwnd)
        # 第二片只蓋 1/3（就是使用者實機回報的那個比例）
        if got and got[0] == 400:
            return (400, 0, 133, 300)
        return got
    monkeypatch.setattr(sb.ScreenBlackout, "_window_rect",
                        staticmethod(_second_panel_is_short))
    try:
        with caplog.at_level(_lg.WARNING):
            assert bo.show() is False, "★蓋不滿就不是成功★"
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "沒有蓋滿" in msgs and "geometry" in msgs
    finally:
        bo.hide()


def test_a_failed_batch_leaves_no_panel_behind(tk_root, monkeypatch):
    """★不可以留下部分黑幕★ 那是最糟的狀態：

    醫師看不到一半的畫面（以為關了），而熱鍵閘門又因為「還有一片蓋著」
    把 F1-F12 全部擋住 —— 兩邊都壞掉。
    """
    real = sb.ScreenBlackout._window_rect
    monkeypatch.setattr(
        sb.ScreenBlackout, "_window_rect",
        staticmethod(lambda h: (0, 0, 1, 1) if real(h) else None))
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 400, 300),
                                             (400, 0, 400, 300)])
    try:
        assert bo.show() is False
        assert bo.active is False, "整批都要收掉"
        assert bo._hwnds == (), "HWND 也要清乾淨（否則熱鍵永遠被擋）"
        assert bo._wins == []
    finally:
        bo.hide()


def test_a_fully_covered_screen_is_success(tk_root):
    """反方向：真的蓋滿就要回 True（不可矯枉過正把功能弄壞）。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    try:
        assert bo.show() is True
        assert all(p.fully_verified for p in bo._panels)
    finally:
        bo.hide()


def test_setpos_failure_alone_does_not_fail_the_panel(tk_root, monkeypatch):
    """★判準是【結果】不是【呼叫成不成功】★

    Tk 的 `wm geometry` 已經先擺過一次，Win32 那步只是校正到正確座標空間。
    校正呼叫失敗、但回讀顯示確實蓋滿了 —— 那就是成功，沒有理由拆掉。
    （單螢幕機器最常見的情況；把它判成失敗會讓按鈕在那些機器上完全沒用。）
    """
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    monkeypatch.setattr(sb._u32(), "SetWindowPos",
                        lambda *_a: 0, raising=False)   # 回 0＝失敗
    try:
        assert bo.show() is True, "回讀蓋滿了就算成功"
        assert bo._panels[0].setpos_ok is False, "但要如實記下來（診斷用）"
        assert bo._panels[0].fully_verified is True
    finally:
        bo.hide()


def test_the_hotkey_gate_stays_conservative(tk_root):
    """★`active` 必須維持 any(...) 的保守語意★

    它回答的是「可不可以放行熱鍵」。只要還有一片蓋著就不可以放行 ——
    這一刀改的是 `show()` 的成功判準，【不可以】把 `active` 一起改成 all(...)，
    否則「剩一片沒收掉」就會放行熱鍵，那一下就打在看不見的畫面上。
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(sb.ScreenBlackout.active.fget)))
    calls = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "any" in calls, "熱鍵閘門要保守：任一片還在就算黑著"
    assert "all" not in calls


def test_the_coverage_state_distinguishes_partial(tk_root, monkeypatch):
    """★bool 表達不了「蓋了一半」★ 診斷與告警要看得出 PARTIAL。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    assert bo.coverage_state() == sb.COVERAGE_HIDDEN
    try:
        bo.show()
        assert bo.coverage_state() == sb.COVERAGE_FULLY_VISIBLE
        # 假裝應該有兩片、實際只有一片 → PARTIAL
        bo._expected_panels = 2
        assert bo.coverage_state() == sb.COVERAGE_PARTIAL
    finally:
        bo.hide()
    assert bo.coverage_state() == sb.COVERAGE_HIDDEN


def test_an_unknown_coverage_is_not_reported_as_hidden(tk_root, monkeypatch):
    """★查不到 ≠ 沒事★ 查詢炸掉時要回 UNKNOWN，不可以回 HIDDEN。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    try:
        bo.show()
        monkeypatch.setattr(sb, "_u32",
                            lambda: (_ for _ in ()).throw(OSError("掛了")))
        assert bo.coverage_state() == sb.COVERAGE_UNKNOWN
    finally:
        monkeypatch.undo()
        bo.hide()


# ─── destroy：關不掉的黑幕要繼續擋熱鍵，而不是假裝收好了 ──────────────────
def test_a_window_that_survives_destroy_keeps_the_gate_closed(tk_root,
                                                              monkeypatch,
                                                              caplog):
    """★這是本項最危險的分支★

    原本 destroy 失敗也無條件清 `_hwnds`，理由是「不然閘門會卡在黑著而熱鍵全死」。
    那個理由沒錯，但代價選錯了：清掉之後如果視窗其實還在螢幕上，就變成
    **醫師看不到 HIS，而 F1-F12 全部放行** —— 自動化在一個看不見的畫面上動作。
    熱鍵全死是看得見、而且安全的故障；在看不見的畫面上動作不是。
    """
    import logging as _lg
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    bo.show()

    class _Stuck:
        """怎麼關都關不掉的視窗。"""
        @staticmethod
        def IsWindow(_h):
            return 1

        @staticmethod
        def IsWindowVisible(_h):
            return 1

        @staticmethod
        def ShowWindow(_h, _c):
            return 0

        @staticmethod
        def DestroyWindow(_h):
            return 0
    monkeypatch.setattr(sb, "_u32", lambda: _Stuck())
    with caplog.at_level(_lg.ERROR):
        bo.hide()
    assert bo._hwnds, "★關不掉就要保留 HWND，讓熱鍵閘門繼續擋★"
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "關不掉" in msgs and "繼續被擋住" in msgs, "而且要大聲說出來"


def test_a_window_that_hides_on_escalation_releases_the_gate(tk_root,
                                                             monkeypatch):
    """★逐級升高就是為了讓大多數情況能收乾淨★

    Tk destroy 失敗但 `ShowWindow(SW_HIDE)` 成功 → 使用者看得到 HIS 了 →
    危險解除 → 閘門要放行（不可以因為「視窗物件還在」就把熱鍵鎖死）。
    """
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    bo.show()
    state = {"visible": True}

    class _HidesEventually:
        @staticmethod
        def IsWindow(_h):
            return 1

        @staticmethod
        def IsWindowVisible(_h):
            return 1 if state["visible"] else 0

        @staticmethod
        def ShowWindow(_h, _c):
            state["visible"] = False       # SW_HIDE 生效
            return 1

        @staticmethod
        def DestroyWindow(_h):
            return 1
    monkeypatch.setattr(sb, "_u32", lambda: _HidesEventually())
    bo.hide()
    assert bo._hwnds == (), "已經看不見了 → 閘門要放行"


def test_the_escalation_reads_back_instead_of_assuming(tk_root):
    """★送出去就當成功★ 是這個 repo 反覆出事的形狀 —— 強制關閉要回讀。"""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(sb.ScreenBlackout._force_close_survivors)))
    src = ast.unparse(tree)
    for api in ("ShowWindow", "DestroyWindow", "IsWindowVisible"):
        assert api in src, f"少了 {api}"
    # DestroyWindow 之後必須再問一次 IsWindowVisible
    assert src.index("DestroyWindow") < src.rindex("IsWindowVisible"), \
        "拆完要再回讀一次才知道到底關掉了沒"


# ─── ★[2026-08-01 外審第 2 輪] 兩個 CONFIRMED★ ───────────────────────────
def test_a_second_teardown_does_not_forget_a_stuck_window(tk_root,
                                                          monkeypatch):
    """★P1：第二次 teardown 會把閘門打開，而黑幕還在螢幕上★

    第一次 hide() 留下關不掉的 survivor 之後，`_wins` 是空的、`_hwnds` 還有東西。
    原本 `_destroy()` 開頭寫「wins 是空的 → 清掉 _hwnds 就返回」，於是：
      * 第二次 hide()，或
      * 殘留視窗上的 <Key>/<Motion> 綁定被觸發
    就會在【完全沒有回讀】的情況下把閘門打開 —— 正好把這一刀的安全保證反轉回去。
    """
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    bo.show()

    class _Stuck:
        @staticmethod
        def IsWindow(_h):
            return 1

        @staticmethod
        def IsWindowVisible(_h):
            return 1

        @staticmethod
        def ShowWindow(_h, _c):
            return 0

        @staticmethod
        def DestroyWindow(_h):
            return 0
    monkeypatch.setattr(sb, "_u32", lambda: _Stuck())

    bo.hide()
    assert bo._hwnds, "前提：第一次就留下了關不掉的 survivor"
    first = bo._hwnds

    bo.hide()                     # ★第二次★
    assert bo._hwnds == first, \
        "★第二次 teardown 不可以在沒回讀的情況下把閘門打開★（黑幕還在螢幕上）"
    assert bo.active_from_any_thread() is True, "閘門必須繼續擋"


def test_a_second_teardown_releases_once_the_window_is_really_gone(tk_root,
                                                                   monkeypatch):
    """反方向：殘留視窗後來真的不見了 → 第二次 teardown 要放行（不可永久鎖死）。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    bo.show()
    state = {"alive": True}

    class _Fake:
        @staticmethod
        def IsWindow(_h):
            return 1 if state["alive"] else 0

        @staticmethod
        def IsWindowVisible(_h):
            return 1 if state["alive"] else 0

        @staticmethod
        def ShowWindow(_h, _c):
            return 0

        @staticmethod
        def DestroyWindow(_h):
            return 0
    monkeypatch.setattr(sb, "_u32", lambda: _Fake())
    bo.hide()
    assert bo._hwnds, "前提：第一次卡住"
    state["alive"] = False        # 視窗後來自己不見了（例如行程回收）
    bo.hide()
    assert bo._hwnds == (), "確定不見了就要放行，不可以永久鎖住熱鍵"


def test_a_verification_rollback_does_not_eat_the_next_hotkey(tk_root,
                                                              monkeypatch):
    """★P2：沒有人按鍵，卻吃掉下一個熱鍵★

    wake token 存在的理由是「造成黑幕收起的那一下按鍵，不可以又落進 HIS」——
    那個競態只發生在【使用者輸入】觸發的退場。
    驗證失敗的 rollback（蓋不滿 → 整批拆掉）根本沒有人按任何鍵；這時發 token
    只會讓接下來 1.5 秒內第一下 F1-F11 被靜默吃掉：黑幕沒蓋成、熱鍵還少一下。
    """
    real = sb.ScreenBlackout._window_rect
    monkeypatch.setattr(
        sb.ScreenBlackout, "_window_rect",
        staticmethod(lambda h: (0, 0, 1, 1) if real(h) else None))
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    try:
        assert bo.show() is False, "前提：驗證失敗而回滾"
        assert bo.consume_wake_gate() is False, \
            "★沒有人按鍵，不該發喚醒 token★"
    finally:
        bo.hide()


def test_a_real_wake_still_eats_the_hotkey(tk_root, made):
    """不可矯枉過正：真的由使用者輸入收起黑幕時，那一下仍要被吃掉。"""
    bo, idle, _busy = made
    bo.show()
    _arm(bo, idle)
    idle.input()                  # 使用者碰了鍵鼠
    bo._poll()                    # → 收黑幕
    assert bo.active is False
    assert bo.consume_wake_gate() is True, "喚醒的那一下仍必須被吃掉"


# ─── ★[2026-08-01 外審第 3 輪] token 的判準不是「有沒有 Tk wrapper」★ ─────
def test_a_survivor_closed_on_a_later_teardown_still_issues_the_token(
        tk_root, monkeypatch):
    """★P1：漏發 token，喚醒的那一下 F1 會直接落進剛露出來的 HIS★

    第一次 teardown 留下關不掉的 survivor 之後，`_wins` 已經是空的。
    使用者按 F1（殘留視窗上的 Tk binding 觸發）→ 第二次 teardown 這次成功關掉了
    ——確實有一個可見視窗剛剛消失，正是 token 要防的那個競態。
    但我上一輪用 `not wins` 當「有沒有黑幕」的代理，於是這次不發 token：
    Tk callback 若先於 keyboard hook 跑完，hook 看到 `_hwnds == ()` 就放行同一下 F1。
    """
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    bo.show()
    state = {"alive": True}

    class _Fake:
        @staticmethod
        def IsWindow(_h):
            return 1 if state["alive"] else 0

        @staticmethod
        def IsWindowVisible(_h):
            return 1 if state["alive"] else 0

        @staticmethod
        def ShowWindow(_h, _c):
            return 0

        @staticmethod
        def DestroyWindow(_h):
            return 0
    monkeypatch.setattr(sb, "_u32", lambda: _Fake())

    bo.hide()                                  # 第一次：卡住
    assert bo._hwnds, "前提：留下了 survivor"
    assert bo._wake_token == 0.0, \
        "還卡著時不該發 token（黑幕沒消失，閘門本來就擋著）"
    # ★不可以在這裡呼叫 consume_wake_gate()★ 那會標記「這次黑幕已經吃過一下」，
    #   而本測試要模擬的正是【Tk 先跑完、hook 才來問】的那個順序 —— 那時還沒有
    #   任何熱鍵被吃掉，所以必須發 token。
    state["alive"] = False                     # 視窗這次真的關掉了
    bo.hide()                                  # ★第二次：使用者按鍵觸發★
    assert bo._hwnds == ()
    assert bo.consume_wake_gate() is True, \
        "★可見視窗剛剛消失就要發 token★ 否則那一下 F1 會落進剛露出來的 HIS"


def test_a_stuck_teardown_keeps_blocking_by_itself(tk_root, monkeypatch):
    """反方向：還卡著時，擋熱鍵靠的是【黑幕仍可見】，不是 token。

    `consume_wake_gate()` 在黑幕仍在時本來就回 True（把那一下吃掉）——
    那是正確的，因為畫面確實還被蓋著。這支釘的是「擋得住」，
    以及它不是靠一張 token 撐著（token 有 1.5 秒失效保險，會過期）。
    """
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    bo.show()

    class _Stuck:
        @staticmethod
        def IsWindow(_h):
            return 1

        @staticmethod
        def IsWindowVisible(_h):
            return 1

        @staticmethod
        def ShowWindow(_h, _c):
            return 0

        @staticmethod
        def DestroyWindow(_h):
            return 0
    monkeypatch.setattr(sb, "_u32", lambda: _Stuck())
    bo.hide()
    assert bo._hwnds, "前提：卡住了"
    # ★先看 token 再消費閘門★ 反過來的話 consume_wake_gate() 會把 token 清掉，
    #   於是「有沒有發過 token」就永遠測不出來（突變驗證抓到過這個假綠燈）。
    assert bo._wake_token == 0.0, "★擋住是因為看得見，不是因為有 token★"
    assert bo.active_from_any_thread() is True, "黑幕仍可見"
    assert bo.consume_wake_gate() is True, "仍可見 → 這一下熱鍵要被吃掉"


def test_a_failed_failsafe_schedule_does_not_eat_the_next_hotkey(tk_root,
                                                                 monkeypatch):
    """★P2：排不到 after 也不是使用者按鍵造成的★

    `_schedule_poll()` 失敗時會【自己】呼叫 `_destroy()`；用預設值就會簽發 token，
    而 `show()` 之後那次 `_destroy(issue_wake_token=False)` 撤不掉它（視窗已清空）。
    結果：黑幕根本沒留住，下一下 F1-F11 卻被無故吃掉。
    """
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [RECT])
    monkeypatch.setattr(tk_root, "after",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            RuntimeError("排不到")))
    assert bo.show() is False
    assert bo.consume_wake_gate() is False, \
        "★沒有人按鍵，不該吃掉下一個熱鍵★"


def test_the_eaten_flag_does_not_leak_across_two_wakes(tk_root, monkeypatch):
    """★[2026-08-01 外審第 4 輪] 「已吃過一下」的範圍是一次喚醒，不是一整段黑幕★

    平常沒差：一段黑幕只會被喚醒一次。但 survivor 卡住時黑幕會【持續存在】，
    同一段黑幕就會有第二次喚醒 —— 而 `_eaten_this_blackout` 原本只在 `_create()`
    重設，所以還停在第一次的 True。

    交錯順序：
      第一次 F1：hook 先跑 → 被擋、設 eaten=True；接著 teardown 卡住（survivor）。
      第二次 F1：Tk 先跑 → 這次成功關掉 survivor。
    此時若因為舊的 eaten=True 而不發 token，hook 隨後看到黑幕沒了、也沒有 token，
    就會把那一下 F1 打在剛露出來的 HIS 上。
    """
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rects_fn=lambda: [(0, 0, 500, 400)])
    bo.show()
    state = {"alive": True}

    class _Fake:
        @staticmethod
        def IsWindow(_h):
            return 1 if state["alive"] else 0

        @staticmethod
        def IsWindowVisible(_h):
            return 1 if state["alive"] else 0

        @staticmethod
        def ShowWindow(_h, _c):
            return 0

        @staticmethod
        def DestroyWindow(_h):
            return 0
    monkeypatch.setattr(sb, "_u32", lambda: _Fake())

    # 第一次喚醒：hook 先跑（黑幕仍在 → 擋下並標記 eaten）
    assert bo.consume_wake_gate() is True
    assert bo._eaten_this_blackout is True
    bo.hide()                                   # teardown 卡住
    assert bo._hwnds, "前提：survivor 還在"

    # 第二次喚醒：Tk 先跑，而且這次真的關掉了
    state["alive"] = False
    bo.hide()
    assert bo._hwnds == ()
    assert bo.consume_wake_gate() is True, \
        "★第二次喚醒要有自己的 token★ 舊的 eaten 狀態不可以壓過它"


def test_the_eaten_flag_still_prevents_a_double_eat_within_one_wake(tk_root,
                                                                    made):
    """反方向（外審第 4 輪原本的要求不可回退）：
    同一次喚醒裡 hook 先擋了一下，就不可以再發 token 讓下一下也被吃掉。"""
    bo, idle, _busy = made
    bo.show()
    _arm(bo, idle)
    assert bo.consume_wake_gate() is True       # hook 先跑，擋下這一下
    idle.input()
    bo._poll()                                  # Tk/輪詢接著收黑幕
    assert bo.active is False
    assert bo.consume_wake_gate() is False, \
        "同一次喚醒不可以吃掉兩下（醫師按第二下必須有反應）"
