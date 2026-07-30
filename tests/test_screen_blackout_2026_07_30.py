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
    """可調的輸入狀態：閒置秒數（None＝查不到）+ last-input tick。

    ★tick 是【事件身分】★：輪詢靠它分辨「新的輸入事件」與「同一事件的後續輪」。
    只改 `value` 不改 `tick` 就是「同一次輸入過了一點時間」；`input()` 才是新事件。
    """

    def __init__(self, value=9999.0):
        self.value = value
        self.tick = 100000

    def __call__(self):
        return self.value

    def input(self, seconds: float = 0.1):
        """模擬【一次新的】鍵鼠輸入。"""
        self.tick += 17
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
                           rect_fn=lambda: RECT,
                           last_input_tick_fn=lambda: idle.tick)
    yield bo, idle, busy
    bo.hide()


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
    bo, _idle, _busy = made
    bo.show()
    bo._on_wake()
    assert bo.active is False


# ─── ★外審第 2 輪★ 飄移門檻:兩條路徑用【同一個】 ─────────────────────────
# 第 1 輪抓到「Tk 綁定有門檻、輪詢沒有 → 門檻永遠輪不到」;我第 2 版乾脆把門檻
# 整個拿掉,那是【改掉需求】而不是修矛盾。現在兩條路徑都拿實際游標位置比對同一個
# 門檻與同一個原點,鍵盤/按鍵則以「有新輸入但游標一格都沒動」辨識。
def _at(pos):
    return type("E", (), {"x_root": pos[0], "y_root": pos[1]})()


def test_mouse_drift_below_the_threshold_does_not_take_it_down(made,
                                                              monkeypatch):
    """桌面震動/光學滑鼠飄移不該喚醒（否則黑幕根本蓋不住三秒）。"""
    bo, _idle, _busy = made
    monkeypatch.setattr(sb, "cursor_pos", lambda: (500, 500))
    bo.show()
    bo._on_motion(_at((500 + sb.MOTION_TOLERANCE_PX, 500)))
    assert bo.active is True


def test_mouse_movement_beyond_the_threshold_takes_it_down(made, monkeypatch):
    bo, _idle, _busy = made
    monkeypatch.setattr(sb, "cursor_pos", lambda: (500, 500))
    bo.show()
    bo._on_motion(_at((500 + sb.MOTION_TOLERANCE_PX + 1, 500)))
    assert bo.active is False


def test_the_poll_uses_the_same_threshold_as_the_motion_binding(made,
                                                               monkeypatch):
    """★兩條路徑一致★ 輪詢看到「有新輸入」時，也要拿游標位移比對同一個門檻。"""
    bo, idle, _busy = made
    cursor = {"pos": (500, 500)}
    monkeypatch.setattr(sb, "cursor_pos", lambda: cursor["pos"])
    bo.show()
    idle.input(0.5)                                         # 新輸入事件 #1
    cursor["pos"] = (500 + sb.MOTION_TOLERANCE_PX, 501)     # 但只飄了一點
    bo._poll()
    assert bo.active is True, "門檻內的飄移不可收黑幕（跟 <Motion> 同一個規則）"

    idle.input(0.2)                                         # tick 變了＝新輸入事件 #2
    cursor["pos"] = (500 + sb.MOTION_TOLERANCE_PX + 5, 501)
    bo._poll()
    assert bo.active is False


def test_one_sub_threshold_drift_does_not_merely_delay_dismissal(made,
                                                                monkeypatch):
    """★[外審第 3 輪] 門檻不可只是把收黑幕延後 250ms★

    `GetLastInputInfo` 在一次輸入之後會【持續兩秒都算 fresh】。舊版按輪數計，所以
    一次 1px 飄移在下一輪就被當成「游標沒動 ⇒ 鍵盤輸入」而收掉黑幕 —— 門檻完全白費。
    現在以【原始 last-input tick】辨認新事件，同一個事件不重複判斷。
    """
    bo, idle, _busy = made
    cursor = {"pos": (500, 500)}
    monkeypatch.setattr(sb, "cursor_pos", lambda: cursor["pos"])
    bo.show()
    idle.input(0.1)                         # 一次輸入事件
    cursor["pos"] = (501, 500)              # 1px 飄移
    bo._poll()
    assert bo.active is True
    # 同一個輸入事件的後續輪：閒置秒數遞增（250ms 一輪），游標不再動
    for extra in (0.35, 0.60, 0.85, 1.10, 1.35, 1.60, 1.85):
        idle.value = extra
        bo._poll()
        assert bo.active is True, (
            f"閒置 {extra}s 仍是同一個輸入事件，不可把它重新判成鍵盤輸入")


def test_a_new_keypress_is_caught_even_when_the_idle_reading_went_up(made,
                                                                    monkeypatch):
    """★[外審第 4 輪] 不可用「閒置秒數變小」推測新事件★

    上一輪量到 idle=0.05；下一個 250ms 區間【初】有人敲了鍵盤；這一輪量到 idle=0.20。
    0.20 不小於 0.05 → 用秒數推測就會把那下鍵盤漏掉，而 Tk 拿不到焦點正是這條輪詢
    存在的理由 → 黑幕收不掉。拿原始 `dwTime` 就不會誤判。
    """
    bo, idle, _busy = made
    cursor = {"pos": (500, 500)}
    monkeypatch.setattr(sb, "cursor_pos", lambda: cursor["pos"])
    bo.show()
    # 第一輪：同一個舊事件，只是建立 _last_tick 基準（idle 很小但 tick 沒變）
    idle.value = 0.05
    bo._poll()
    assert bo.active is True, "測試前提：同一個事件的後續輪不該收黑幕"
    # ★新事件，但量到的閒置秒數【比上一輪大】★——用秒數推測就會漏掉它
    idle.input(0.20)
    bo._poll()
    assert bo.active is False, "★閒置秒數變大的新輸入事件也必須被抓到★"


def test_an_unavailable_last_input_tick_errs_towards_taking_it_down(made,
                                                                   monkeypatch):
    """拿不到 tick 就不推測：有新輸入直接收（寧可多收，不可收不掉）。"""
    bo, idle, _busy = made
    monkeypatch.setattr(sb, "cursor_pos", lambda: (500, 500))
    bo.show()
    bo._last_input_tick_fn = lambda: None
    idle.value = 0.5
    bo._poll()
    assert bo.active is False


def test_the_poll_treats_input_without_cursor_movement_as_keyboard(made,
                                                                  monkeypatch):
    """★游標一格都沒動卻有新輸入 → 那是鍵盤/按鍵，一定要收★

    `GetLastInputInfo` 分不出鍵盤和滑鼠，但「游標完全沒動」就足以判定不是滑鼠。
    這條路徑很重要：`overrideredirect` 視窗可能拿不到焦點，Tk 的 <Key> 綁定收不到。
    """
    bo, idle, _busy = made
    monkeypatch.setattr(sb, "cursor_pos", lambda: (500, 500))
    bo.show()
    bo._poll()                       # 先跑一輪，讓 _last_cursor 有值
    idle.input(0.5)
    bo._poll()
    assert bo.active is False


def test_the_poll_gives_up_guessing_after_the_hard_cap(made, monkeypatch):
    """★硬上限★ 判不出來就不判了 —— 絕不可出現「黑幕收不掉」。

    這裡讓游標【每一輪都飄一點點但都在門檻內】：既不算移動、也不算「完全沒動」，
    正是兩個判斷都落空的情形。撐過上限就必須無條件收起來。
    """
    bo, idle, _busy = made
    pos = {"n": 0}

    def _drifting():
        pos["n"] += 1
        return (500 + (pos["n"] % 3), 500)     # 永遠在 25px 門檻內來回

    monkeypatch.setattr(sb, "cursor_pos", _drifting)
    bo.show()
    cap = sb.FRESH_INPUT_EVENTS_BEFORE_FORCE_HIDE
    for i in range(cap):
        if not bo.active:
            break
        idle.input(1.0 - i * 0.1)       # tick 每輪都變＝一個【新的】輸入事件
        bo._poll()
    assert bo.active is False, (
        f"連續 {cap} 個不同輸入事件都判不出結果卻還黑著 → 收不掉")


def test_an_unreadable_cursor_position_errs_towards_taking_it_down(made,
                                                                   monkeypatch):
    """`GetCursorPos` 失敗時偏向「有移動」＝偏向收黑幕。
    反過來（查不到就當沒動）會讓一次 Win32 失敗變成「黑幕收不掉」。"""
    bo, idle, _busy = made
    monkeypatch.setattr(sb, "cursor_pos", lambda: (500, 500))
    bo.show()
    monkeypatch.setattr(sb, "cursor_pos", lambda: None)
    idle.input(0.5)
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
