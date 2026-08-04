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
    pid = 1860
    our_pids = {1860, 15056}


class TestTheMainWindowIsActuallyClosed:

    def test_it_sends_close_and_confirms_the_window_is_gone(self):
        seen = {"n": 0}
        closed = []

        def _find():
            seen["n"] += 1
            return [] if closed else [111]

        ok = cq._close_session_windows(
            _Sess(), find=_find, close=closed.append, sleep=lambda _s: None)

        assert ok is True
        assert closed == [111], f"沒有對主畫面送關閉：{closed}"

    def test_a_window_that_refuses_to_close_is_reported(self, caplog):
        """★送了不回讀就是開迴路★ 關不掉要說出來，不可以假裝收乾淨了。"""
        import logging as _lg
        with caplog.at_level(_lg.ERROR):
            ok = cq._close_session_windows(
                _Sess(), find=lambda: [111], close=lambda _h: None,
                sleep=lambda _s: None)

        assert ok is False, "關不掉卻回報成功"
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "關不掉" in msgs and "仍然登入" in msgs, (
            f"沒有留下『這個 session 還登入著』的痕跡：{msgs}")

    def test_no_main_window_is_not_a_failure(self):
        """本來就沒有主畫面 → 沒東西要關，不算失敗。"""
        assert cq._close_session_windows(
            _Sess(), find=lambda: [], close=lambda _h: None,
            sleep=lambda _s: None) is True

    def test_an_enumeration_failure_is_not_reported_as_closed(self):
        """★措辭鐵律★ 列舉不到 ≠ 已經關掉。"""
        def _boom():
            raise OSError("列舉失敗")
        assert cq._close_session_windows(
            _Sess(), find=_boom, close=lambda _h: None,
            sleep=lambda _s: None) is False

    def test_every_main_window_gets_a_close(self):
        """多個主畫面(接管殘留)都要送到，不能只關第一個。"""
        closed = []
        state = {"open": [111, 222]}

        def _find():
            return list(state["open"])

        def _close(h):
            closed.append(h)
            state["open"] = [x for x in state["open"] if x not in closed]

        ok = cq._close_session_windows(
            _Sess(), find=_find, close=_close, sleep=lambda _s: None)
        assert ok is True and sorted(closed) == [111, 222]


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
