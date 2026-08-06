# -*- coding: utf-8 -*-
"""[2026-08-06 深度穩定] 依近三日實機 log 統計(150+49 次擋登入、12 次 IMAP 砍
socket、#32770 按鈕列舉不到)修的三個穩定性缺陷;④ 為同日 15:23 實機事件
(慢載入誤判+全黑截圖)追加的兩個修正。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


class _Sess:
    def __init__(self, hwnd=111, pid=222, cls="TFMNewMain"):
        self.main_hwnd = hwnd
        self.main_pid = pid
        self.main_class = cls
        self.pid = pid


# ─── ① hwnd 被回收 → session 視為已結束(不再永久掛帳) ──────────────────────
def test_recycled_hwnd_counts_as_closed(monkeypatch):
    """身分【結論性】不符(讀得到、確定換人) = 我們的視窗早已銷毀 → True。
    舊行為回 False → 永遠掛帳、每 6 小時假告警、每 15 分鐘擋一次登入。"""
    monkeypatch.setattr(cq, "_is_same_window", lambda s: False)
    monkeypatch.setattr(cq, "_window_identity",
                        lambda h: (999, "SomeoneElsesWindow"))
    sent = []
    ok = cq._close_session_windows(
        _Sess(), close=sent.append, gone=lambda h: False, sleep=lambda s: None)
    assert ok is True, "結論性身分不符應視為已結束"
    assert sent == [], "絕不可對別人的視窗送 WM_CLOSE"


def test_unreadable_identity_stays_on_the_ledger(monkeypatch):
    """讀不到現任身分 → 「不知道」不可以當「已結束」→ 保守留帳,下一輪再驗。"""
    monkeypatch.setattr(cq, "_is_same_window", lambda s: False)
    monkeypatch.setattr(cq, "_window_identity", lambda h: (None, None))
    sent = []
    ok = cq._close_session_windows(
        _Sess(), close=sent.append, gone=lambda h: False, sleep=lambda s: None)
    assert ok is False and sent == []


def test_unrecorded_own_identity_stays_on_the_ledger(monkeypatch):
    """當初沒記到自己的身分(舊 session)→ 無從比對 → 同樣保守留帳。"""
    monkeypatch.setattr(cq, "_is_same_window", lambda s: False)
    monkeypatch.setattr(cq, "_window_identity", lambda h: (999, "X"))
    sess = _Sess()
    sess.main_pid = None
    sent = []
    ok = cq._close_session_windows(
        sess, close=sent.append, gone=lambda h: False, sleep=lambda s: None)
    assert ok is False and sent == []


def test_matching_window_still_gets_closed_normally(monkeypatch):
    """反面:身分相符的自家視窗照常送 WM_CLOSE(既有行為不變)。"""
    monkeypatch.setattr(cq, "_is_same_window", lambda s: True)
    monkeypatch.setattr(cq, "_dismiss_blocking_modals", lambda s: 0)
    sent = []
    state = {"gone": False}

    def _close(h):
        sent.append(h)
        state["gone"] = True
    ok = cq._close_session_windows(
        _Sess(), close=_close, gone=lambda h: state["gone"],
        sleep=lambda s: None)
    assert ok is True and sent == [111]


# ─── ② 原生對話框(#32770)的 "Button" 也要列舉 ───────────────────────────────
def test_native_dialog_buttons_are_clickable(monkeypatch):
    """實機 09:06:class=#32770 按鈕=[]——原生按鈕 class 是 Button 不是 TButton,
    只列 TButton 等於原生對話框永遠按不掉、只能等 15 分鐘放行。"""
    clicked = []
    monkeypatch.setattr(cq, "_blocking_dialogs", lambda pids: [(5, "#32770")])
    monkeypatch.setattr(cq, "enum_children", lambda hwnd: [
        (41, "Static", "發生錯誤", (0, 0, 0, 0)),
        (42, "Button", "確定", (0, 0, 0, 0)),
    ])
    monkeypatch.setattr(cq, "click_button", lambda h: clicked.append(h))
    assert cq._dismiss_blocking_modals(pids={999}) == 1
    assert clicked == [42]


def test_native_dialog_still_never_blind_clicks(monkeypatch):
    """原生 class 放進列舉【不放寬】字樣白名單:非肯定字樣照樣不按。"""
    clicked = []
    monkeypatch.setattr(cq, "_blocking_dialogs", lambda pids: [(5, "#32770")])
    monkeypatch.setattr(cq, "enum_children", lambda hwnd: [
        (43, "Button", "中止", (0, 0, 0, 0)),
        (44, "Button", "重試", (0, 0, 0, 0)),
    ])
    monkeypatch.setattr(cq, "click_button", lambda h: clicked.append(h))
    cq._reported_unknown_dialogs.discard("#32770")
    assert cq._dismiss_blocking_modals(pids={999}) == 0
    assert clicked == []


# ─── ④ 15:23 實機:慢載入誤判 + 全黑截圖 ────────────────────────────────────
# 時序:視窗開了但後端 ~6 秒才回資料 → settle 在 0 列時判「穩定」→ 截圖全黑
# → 回讀才看到 2 列 → 直接 fail-open 寄了一封附全黑截圖的信;3 分鐘後下一輪
# 完全正常。修法:(a) 回讀對不上=資料剛到 → 整輪重來一次;(b) 單色截圖重截。
def _rgb(color):
    from PIL import Image
    return Image.new("RGB", (8, 6), color)


def test_a_black_image_is_recognized_as_blank():
    from PIL import Image
    assert cq._image_is_blank(_rgb((0, 0, 0))) is True
    assert cq._image_is_blank(_rgb((118, 218, 41))) is True  # 單色=零資訊
    real = _rgb((0, 0, 0))
    real.putpixel((3, 3), (255, 255, 255))
    assert cq._image_is_blank(real) is False
    assert cq._image_is_blank(Image.new("L", (4, 4), 0)) is True  # 單色版模式
    assert cq._image_is_blank("IMG") is False, "非影像(測試樁)不可誤判"


def test_a_blank_capture_is_retried_until_the_window_paints():
    shots = [_rgb((0, 0, 0)), _rgb((0, 0, 0))]
    good = _rgb((0, 0, 0))
    good.putpixel((1, 1), (255, 0, 0))
    shots.append(good)
    img = cq._capture_nonblank(1, lambda h: shots.pop(0),
                               sleep=lambda s: None)
    assert img is good, "重截後拿到有內容的那張"


def test_a_stubbornly_blank_capture_is_kept_after_bounded_retries():
    calls = {"n": 0}

    def _cap(_h):
        calls["n"] += 1
        return _rgb((0, 0, 0))
    img = cq._capture_nonblank(1, _cap, sleep=lambda s: None)
    assert cq._image_is_blank(img), "重截失敗仍要有圖可寄(fail-open)"
    assert calls["n"] == 1 + cq._CAPTURE_BLANK_RETRIES, "重截必須有上限"


def test_late_loading_roster_gets_one_full_retry(monkeypatch):
    """15:23 劇本:settle 判空清單穩定,回讀才看到 2 列 → 重來一輪就正常,
    不該直接 fail-open 寄信。"""
    two = ["張01234567", "林07654321"]
    settles = [cq._RosterSnapshot([], True, [], []),
               cq._RosterSnapshot(list(two), True, [], [])]
    img, snap = cq._capture_with_settled_roster(
        1, capture=lambda h: "IMG",
        settle=lambda h: settles.pop(0),
        read=lambda h: ([], [], list(two)))
    assert snap.stable is True and list(snap.texts) == two
    assert cq._may_update_baseline(snap.texts) is True, "重來成功=正常一輪"


def test_endless_churn_still_fails_open_after_bounded_retries():
    """清單真的一直在變 → 重試有上限,最後仍走 fail-open(既有語意不變)。"""
    settle_calls = {"n": 0}

    def _settle(_h):
        settle_calls["n"] += 1
        return cq._RosterSnapshot(["甲1111111"], True, [], [])
    _img, snap = cq._capture_with_settled_roster(
        1, capture=lambda h: "IMG", settle=_settle,
        read=lambda h: ([], [], ["甲1111111", "乙2222222"]))
    assert snap.stable is False, "重試耗盡仍對不上 → fail-open"
    assert settle_calls["n"] == 1 + cq._SETTLED_RETRY_ROUNDS, "重試必須有上限"


# ─── ③ IMAP 單操作逾時要遠小於外層 60s 砍 socket ────────────────────────────
def test_imap_per_op_timeout_fails_fast():
    """3 天 12 次「超過 60s 強制砍 socket」:單操作 30s × 一次檢查多個操作,
    兩三個停滯就撞 watchdog。單操作逾時必須 ≤15s,讓單點卡住走正常重試路徑。"""
    import inspect

    from cmuh_common import imap_reader
    sig = inspect.signature(imap_reader.check_trigger_emails) \
        if hasattr(imap_reader, "check_trigger_emails") else None
    if sig is not None and "timeout" in sig.parameters:
        assert sig.parameters["timeout"].default <= 15
    else:                              # 函式名不同 → 直接掃源碼的預設值
        src = inspect.getsource(imap_reader)
        assert "timeout: float = 12.0" in src
        assert "timeout: float = 30.0" not in src
