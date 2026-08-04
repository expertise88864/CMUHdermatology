# -*- coding: utf-8 -*-
"""收掉 session 必須真的把主畫面關掉（2026-08-04 外審第 3 輪 P1-05）。

★這是我自己造成的迴歸★
批次 P 把 teardown 收緊成「只終止驗證過的自有行程」，而實機證實 systemftp 是
啟動器型行程 —— 我們 spawn 的 root 立刻結束。兩件事加在一起：

    _verified_owned_pids(已死的 root, ...)  → 只剩 {已死的 root}
    close_pids({已死的 pid})                → 什麼都關不到
    WaitForSingleObject(hproc)              → 早已 signaled → 不執行 Terminate

淨結果：teardown 只清掉 Python 這邊的參照，真正的 HIS UI 還留在隱藏桌面登入中。
「6 小時定期重啟」「休息時段收掉」「接管舊 worker」全都只是說說而已。

行程層面關不掉，就從【視窗層面】關：對確切的主畫面送 WM_CLOSE 並★回讀確認★。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


class _Sess:
    """our_pids 刻意含一個外來 pid（15056）—— 實機就是這樣。"""

    pid = 1860
    our_pids = {1860, 15056}

    def __init__(self, main_hwnd=111):
        self.main_hwnd = main_hwnd


class TestOnlyOurOwnLoggedInWindowIsClosed:
    """★[2026-08-04 自查 P0] 不可以用 `our_pids` 決定「哪個視窗是我的」★

    第一版寫成 `find_windows(MAIN_CLASS, pids=sess.our_pids)`。`our_pids` 是全機
    PID 差集，實機已證實會混進外來的 systemftp（log：「pid 10928 已非 systemftp」、
    「收尾時排除 1 個不屬於本次啟動的」）。拿它當授權去送 WM_CLOSE，等於把批次 P
    擋掉的傷害從【視窗】這道門放回來 —— 醫師的住院系統會被關掉。

    改成只關 `sess.main_hwnd`：`_wait_main_window_after_login()` 回傳的、我們
    【確切登入進去】的那一個。身分由「我們自己登入它」保證，不靠 PID 猜。
    """

    def test_it_closes_the_exact_window_and_confirms(self):
        closed = []
        state = {"gone": False}

        def _close(h):
            closed.append(h)
            state["gone"] = True

        ok = cq._close_session_windows(
            _Sess(main_hwnd=111), close=_close,
            gone=lambda _h: state["gone"], sleep=lambda _s: None)

        assert ok is True
        assert closed == [111], f"沒有關到我們登入的那個視窗：{closed}"

    def test_it_never_enumerates_by_pid(self):
        """★核心★ 就算 our_pids 裡有醫師的行程，也不可以碰到它的視窗。

        用「`find_windows` 一旦被呼叫就炸」來證明這條路根本沒被走。
        """
        def _boom(*_a, **_k):
            raise AssertionError("★又用 PID 集合去找視窗了★ 會關到醫師的 HIS")

        import unittest.mock as _m
        with _m.patch.object(cq, "find_windows", _boom):
            ok = cq._close_session_windows(
                _Sess(main_hwnd=111), close=lambda _h: None,
                gone=lambda _h: True, sleep=lambda _s: None)
        assert ok is True

    def test_a_session_that_never_logged_in_has_nothing_to_close(self):
        """`main_hwnd is None` = 從未登入成功 → 沒有我們的主畫面可關。

        `_wait_main_window_after_login()` 只有一條成功出口、失敗一律拋例外，
        所以成功建立的 session 必然帶著 hwnd（不變式由下面的接線測試守著）。
        ★重點是它【不會】退回用 PID 差集去找★——那個集合含醫師的行程。
        """
        closed = []
        ok = cq._close_session_windows(
            _Sess(main_hwnd=None), close=closed.append,
            gone=lambda _h: False, sleep=lambda _s: None)

        assert ok is True
        assert closed == [], f"沒有 hwnd 卻還是關了東西：{closed}"

    def test_a_window_that_refuses_to_close_is_reported(self, caplog):
        """★送了不回讀就是開迴路★ 關不掉要說出來，不可以假裝收乾淨了。"""
        import logging as _lg
        with caplog.at_level(_lg.ERROR):
            ok = cq._close_session_windows(
                _Sess(main_hwnd=111), close=lambda _h: None,
                gone=lambda _h: False, sleep=lambda _s: None)

        assert ok is False, "關不掉卻回報成功"
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "關不掉" in msgs and "仍然登入" in msgs, msgs

    def test_a_window_already_gone_is_not_a_failure(self):
        """本來就不在了 → 沒東西要關，不算失敗。"""
        assert cq._close_session_windows(
            _Sess(main_hwnd=111), close=lambda _h: None,
            gone=lambda _h: True, sleep=lambda _s: None) is True

    def test_a_failed_post_is_not_reported_as_closed(self):
        """★措辭鐵律★ 送不出去 ≠ 已經關掉。"""
        def _boom(_h):
            raise OSError("PostMessage 失敗")
        assert cq._close_session_windows(
            _Sess(main_hwnd=111), close=_boom,
            gone=lambda _h: False, sleep=lambda _s: None) is False


class TestAHiddenWindowCountsAsGone:
    """Delphi 表單關閉後常常只是 Hide、handle 仍有效（2026-07-27 診間事故）。"""

    def test_a_destroyed_window_is_gone(self, monkeypatch):
        monkeypatch.setattr(cq.win32gui, "IsWindow", lambda _h: False)
        assert cq._window_is_gone(111) is True

    def test_a_hidden_but_alive_window_is_also_gone(self, monkeypatch):
        monkeypatch.setattr(cq.win32gui, "IsWindow", lambda _h: True)
        monkeypatch.setattr(cq.win32gui, "IsWindowVisible", lambda _h: False)
        assert cq._window_is_gone(111) is True, (
            "★只看 IsWindow★ Delphi 關閉後只是 Hide，會被當成還開著")

    def test_a_live_visible_window_is_not_gone(self, monkeypatch):
        monkeypatch.setattr(cq.win32gui, "IsWindow", lambda _h: True)
        monkeypatch.setattr(cq.win32gui, "IsWindowVisible", lambda _h: True)
        assert cq._window_is_gone(111) is False

    def test_an_api_failure_counts_as_gone(self, monkeypatch):
        def _boom(_h):
            raise OSError("handle 無效")
        monkeypatch.setattr(cq.win32gui, "IsWindow", _boom)
        assert cq._window_is_gone(111) is True


def test_teardown_actually_calls_the_window_close():
    """★接線本身也要被測到★（本 session 這個形狀第八次）

    上面幾支直接呼叫 `_close_session_windows`。若 `_terminate_session_process`
    沒有接上它，真正的 HIS UI 照樣關不掉 —— 而那些測試會全綠。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._terminate_session_process)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_close_session_windows" in called, (
        "★teardown 沒有關主畫面★ 只清掉 Python 參照，HIS 仍登入中")


def test_the_session_records_the_window_it_logged_into():
    """★接線★ 冷啟動必須接住 `_wait_main_window_after_login()` 的回傳值。

    那行以前寫成不接回傳值，於是 session 不知道自己登入了哪個視窗，收尾只好用
    PID 差集猜 —— 而那個集合含醫師的行程。這裡確認回傳值有被接住並傳進 session。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._cold_start_session_impl)))

    # `_wait_main_window_after_login(...)` 的結果必須被指派給某個名字
    assigned = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        v = node.value
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == "_wait_main_window_after_login"):
            assigned = node.targets[0].id
    assert assigned, (
        "★主畫面 hwnd 的回傳值沒被接住★ session 不知道自己登入了哪個視窗")

    # 而且要當成 main_hwnd 傳進 _PersistentSession
    # ★any 不是 last★：這個函式有兩處建構 —— 成功路徑（要帶 hwnd）與冷啟動失敗
    #   後為了收行程而臨時建的那個（本來就沒有主畫面，不該帶）。第一版寫成迴圈
    #   覆蓋，結果被【錯誤路徑那個】決定了結果而誤紅。
    passed = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_PersistentSession"
        and any(kw.arg == "main_hwnd" for kw in n.keywords)
        for n in ast.walk(tree))
    assert passed, "hwnd 接住了卻沒傳進 session"


def test_the_persistent_session_defaults_to_unknown():
    """沒指定就是 None —— 不可以預設成某個「看起來合理」的值。"""
    s = cq._PersistentSession(None, None, 123, {123})
    assert s.main_hwnd is None
