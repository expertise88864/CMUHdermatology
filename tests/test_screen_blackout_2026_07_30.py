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
                           rect_fn=lambda: RECT)
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


def test_it_covers_the_whole_virtual_desktop_not_just_one_monitor(made):
    """★診間有雙螢幕機器★ 只用 -fullscreen 會只蓋一個螢幕，另一台仍亮著顯示病歷。"""
    bo, _idle, _busy = made
    bo.show()
    bo._win.update_idletasks()
    assert (bo._win.winfo_width(), bo._win.winfo_height()) == (800, 600)


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
                           rect_fn=lambda: rect)
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
    assert bo._hwnd == 0, "測試前提：HWND 已經清掉了"
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
                           busy_fn=_Busy(True), rect_fn=lambda: RECT)
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
    bo._win.destroy()                    # 繞過 hide()，模擬視窗被別人弄掉
    assert bo.active is False


def test_active_is_false_when_the_window_object_misbehaves(made):
    bo, _idle, _busy = made
    bo.show()

    class _Broken:
        def winfo_exists(self):
            raise RuntimeError("Tcl 沒了")

    bo._win = _Broken()
    assert bo.active is False, "問視窗狀態時炸掉也必須回 False（不可卡住熱鍵）"


def test_it_refuses_to_stay_up_without_a_failsafe_poll(tk_root, monkeypatch):
    """★排不到 after 就不黑屏★ 沒有失效保險輪詢的黑幕是收不掉的黑幕。"""
    bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(), busy_fn=_Busy(),
                           rect_fn=lambda: RECT)
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
