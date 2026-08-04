# -*- coding: utf-8 -*-
"""session 判死要說出【哪一個】原因（2026-08-04 實機 log）。

診間 consult_query.log 顯示每一輪都走「session 已死」→ 冷啟動 → 重新送帳密
（13:44→14:29 共 15 輪無一例外）。而 `_cold_start_session_impl` 的 docstring
明文寫著「3 分鐘 keepalive 節奏絕不可把同一組帳密每 3 分鐘送一次」——
實際行為與設計意圖直接相反，有帳號鎖定風險。

要修它就得先知道是哪個條件在觸發。舊訊息把兩個成因寫成同一句：

    session 已死(行程結束或主畫面不見了)

★兩個成因完全不同、修法也完全不同★，寫成同一句等於 45 分鐘的 log 什麼都沒說。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


class _Sess:
    def __init__(self):
        self.hproc = object()
        self.our_pids = {123}


def _proc_state(monkeypatch, *, exited):
    class _E:
        WAIT_TIMEOUT = 258
        WAIT_OBJECT_0 = 0

        @staticmethod
        def WaitForSingleObject(_h, _ms):
            return 0 if exited else 258
    monkeypatch.setattr(cq, "win32event", _E)


def test_a_dead_launcher_with_a_live_main_window_is_still_usable(monkeypatch):
    """★這就是修正本身★（2026-08-04 診間 log 證實）

    ★這支測試原本斷言相反的事★ —— 它要求「我們 spawn 的行程結束了就判死」，
    而那正是 bug：實機 log 顯示 21 次判死【100% 都是】這個原因、「找不到主畫面」
    零次，也就是 systemftp.exe 是【啟動器型行程】：起來、把工作交給既有實例、
    自己立刻結束。舊判定於是每 3 分鐘判死一次 → 冷啟動 → 重新送一次帳密。

    session 能不能用，取決於主畫面在不在，不取決於那個啟動器活著沒有。
    """
    _proc_state(monkeypatch, exited=True)
    monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [1])

    assert cq._session_death_reason(_Sess()) == "", (
        "★啟動器結束就判死 → 每 3 分鐘重新送一次帳密★")
    assert cq._session_is_alive(_Sess()) is True


def test_a_missing_main_window_says_so(monkeypatch):
    _proc_state(monkeypatch, exited=False)
    monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [])
    assert "找不到主畫面" in cq._session_death_reason(_Sess())


def test_both_the_process_and_the_window_gone_says_so(monkeypatch):
    """行程也結束、主畫面也不在 → 這才是真的死了。"""
    _proc_state(monkeypatch, exited=True)
    monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [])
    reason = cq._session_death_reason(_Sess())
    assert "行程已結束" in reason and "主畫面" in reason, reason


def test_the_two_reasons_are_not_the_same_sentence(monkeypatch):
    """★這就是實機 log 判斷不出來的原因★ 兩個成因要講得出差別。"""
    monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [])
    _proc_state(monkeypatch, exited=True)
    dead_proc = cq._session_death_reason(_Sess())
    _proc_state(monkeypatch, exited=False)
    no_window = cq._session_death_reason(_Sess())

    assert dead_proc and no_window and dead_proc != no_window, (
        f"兩個成因共用同一句話：{dead_proc!r} / {no_window!r}")


def test_a_healthy_session_reports_nothing(monkeypatch):
    """★反方向:不可以把活著的 session 判死★ 那正是每 3 分鐘重送帳密的來源。"""
    _proc_state(monkeypatch, exited=False)
    monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [1])
    assert cq._session_death_reason(_Sess()) == ""
    assert cq._session_is_alive(_Sess()) is True


def test_an_unqueryable_process_is_not_reported_as_exited(monkeypatch):
    """★措辭鐵律★ 查不到狀態 ≠ 行程結束。處置相同，說法必須不同。"""
    class _E:
        WAIT_TIMEOUT = 258

        @staticmethod
        def WaitForSingleObject(_h, _ms):
            raise OSError("handle 無效")
    monkeypatch.setattr(cq, "win32event", _E)
    # 主畫面不在，才會走到「查行程狀態」那一步
    monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [])

    reason = cq._session_death_reason(_Sess())
    assert "無法查詢" in reason and "已結束" not in reason, reason


def test_acquire_logs_the_specific_reason():
    """接線:判死的訊息要帶上原因，否則診斷等於沒做。"""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._acquire_session)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_session_death_reason" in called, (
        "_acquire_session 沒有取用原因 → log 仍分不出是哪一個成因")
