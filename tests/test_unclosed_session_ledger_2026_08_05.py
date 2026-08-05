# -*- coding: utf-8 -*-
"""關不掉的 session 要掛帳重試，不可以連參照一起丟（外審第 4 輪 P1-03）。

【為什麼這是 P1】
`_session_close` 是先 `_psession = None`、再去關窗。關失敗的時候（HIS 不回應
WM_CLOSE、hwnd 身分對不上、modal 擋著），我們已經把**唯一**認得那個 session 的
參照丟掉了。而且底下那幾條後路對它都無效：

  * systemftp 是啟動器型行程（實機證實）→ `sess.hproc` 早已 signaled →
    `TerminateProcess` 那一段根本不執行
  * `_verified_owned_pids` 只剩一個已死的 root → `close_pids` 關不到東西
  * `_kill_systemftp` 在 2026-08-04 已經改成不再 taskkill

淨結果：一個【仍然登入中】的 HIS 留在隱藏桌面上，沒有任何程式碼認得它；
下一輪看到「沒有 session」就再冷啟動登入一次。每失敗一次就多一個。

所以關不掉時把 session 掛在帳上，每輪查詢前重試，並且告警。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


class _Sess:
    def __init__(self, pid=1860, hwnd=111):
        self.pid = pid
        self.main_hwnd = hwnd
        self.main_pid = pid
        self.main_class = cq.MAIN_CLASS
        self.our_pids = {pid}


def _clear_ledger():
    with cq._unclosed_lock:
        cq._unclosed_sessions.clear()


def test_a_failed_close_is_recorded(caplog):
    import logging as _lg
    _clear_ledger()
    s = _Sess()
    with caplog.at_level(_lg.ERROR):
        cq._note_unclosed_session(s, "測試")
    assert cq._unclosed_sessions == [s], "關不掉卻沒掛帳 → 這個 session 從此無人認領"
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "關不掉" in msgs and "仍登入中" in msgs, msgs
    _clear_ledger()


def test_the_same_session_is_not_recorded_twice():
    _clear_ledger()
    s = _Sess()
    cq._note_unclosed_session(s, "第一次")
    cq._note_unclosed_session(s, "第二次")
    assert len(cq._unclosed_sessions) == 1
    _clear_ledger()


def test_retry_closes_and_removes_it(monkeypatch):
    _clear_ledger()
    s = _Sess()
    cq._note_unclosed_session(s, "測試")
    monkeypatch.setattr(cq, "_close_session_windows", lambda _s: True)

    assert cq._retry_unclosed_sessions() == 0
    assert cq._unclosed_sessions == [], "關掉了卻還留在帳上"
    _clear_ledger()


def test_a_still_failing_session_stays_on_the_ledger(monkeypatch):
    """★關鍵★ 還是關不掉 → 留在帳上，下一輪繼續試。不可以「試過一次就算了」。"""
    _clear_ledger()
    s = _Sess()
    cq._note_unclosed_session(s, "測試")
    monkeypatch.setattr(cq, "_close_session_windows", lambda _s: False)

    assert cq._retry_unclosed_sessions() == 1
    assert cq._unclosed_sessions == [s]
    _clear_ledger()


def test_a_raising_close_does_not_drop_it(monkeypatch):
    """重試時炸掉 ≠ 關掉了。例外不可以變成「從帳上消失」。"""
    _clear_ledger()
    s = _Sess()
    cq._note_unclosed_session(s, "測試")

    def _boom(_s):
        raise OSError("PostMessage 失敗")
    monkeypatch.setattr(cq, "_close_session_windows", _boom)

    assert cq._retry_unclosed_sessions() == 1
    assert cq._unclosed_sessions == [s]
    _clear_ledger()


def test_only_the_ones_that_closed_are_removed(monkeypatch):
    """一批裡有成功有失敗 → 只移除成功的那些。"""
    _clear_ledger()
    good, bad = _Sess(pid=1), _Sess(pid=2)
    cq._note_unclosed_session(good, "a")
    cq._note_unclosed_session(bad, "b")
    monkeypatch.setattr(cq, "_close_session_windows",
                        lambda s: s.pid == 1)

    assert cq._retry_unclosed_sessions() == 1
    assert cq._unclosed_sessions == [bad]
    _clear_ledger()


def test_hitting_the_cap_does_not_discard_anything(caplog):
    """★上限是防爆，不是丟棄的理由★

    丟掉任何一筆就等於回到「沒人認得它」—— 那正是這整個機制要擋的事。
    達到上限時：不再增加、並且告警，但帳上原有的一筆都不能少。
    """
    import logging as _lg
    _clear_ledger()
    kept = [_Sess(pid=i) for i in range(cq._MAX_UNCLOSED)]
    for s in kept:
        cq._note_unclosed_session(s, "填滿")
    with caplog.at_level(_lg.ERROR):
        cq._note_unclosed_session(_Sess(pid=999), "溢位")

    assert cq._unclosed_sessions == kept, "★把舊的擠掉了★ 那個 HIS 從此無人認領"
    assert "上限" in " ".join(r.getMessage() for r in caplog.records)
    _clear_ledger()


# ── 接線（這個 session 反覆出事的形狀）─────────────────────────────────────
def test_teardown_records_a_failed_close():
    """★接線★ `_terminate_session_process` 必須把失敗結果掛帳。

    上面幾支都直接呼叫 `_note_unclosed_session`。若 teardown 只是呼叫
    `_close_session_windows(sess)` 而不看回傳值（原本就是這樣寫的），
    那些測試照樣全綠，實機卻一個都不會掛帳。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._terminate_session_process)))
    # 必須存在「if not _close_session_windows(...): _note_unclosed_session(...)」
    ok = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_calls = {n.func.id for n in ast.walk(node.test)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        body_calls = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name)}
        if ("_close_session_windows" in test_calls
                and "_note_unclosed_session" in body_calls):
            ok = True
    assert ok, "★關窗的回傳值沒被檢查★ 關不掉不會掛帳，那個 HIS 無人認領"


def test_every_cycle_retries_the_ledger():
    """★接線★ 掛帳了但沒有人重試 = 只是換一個地方遺忘它。"""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._acquire_session)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_retry_unclosed_sessions" in called, (
        "每輪取用 session 之前沒有重試掛帳的 session")
