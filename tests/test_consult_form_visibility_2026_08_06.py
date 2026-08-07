# -*- coding: utf-8 -*-
"""會診視窗採認:可見性是三態（外審第 7 輪 P1-05）。

【問題】批次AC 讓「命令前隱藏、命令後轉可見」的同一個 hwnd 也能被採認
（Delphi 常重用 form）。但那一版有兩個缺口：

1. `_visible()` 的例外回 `False`（＝**明確隱藏**）。於是一張【命令前就可見】
   的舊表單，只要那一刻查詢失敗，就會在命令後被誤判成「hidden→visible」
   而被採認 —— 那可能是醫師自己開的、或上一輪沒退乾淨的舊資料。
   ★「讀不到」被當成某個確定的答案★ 是這幾天一路在修的同一個病灶。

2. 命令後新出現的 hwnd 沒有要求「現在明確可見」就直接採認。Delphi 常
   「先建立 form → 建 children → 載入資料 → 最後才 Show」——在它 Show 之前
   就開始 roster settle／截圖，會拿到一張還沒填好的表單。

【判準】兩種候選都要求 `_visible(h) is True`：
    * 新出現的 hwnd，且現在明確可見
    * 命令前【明確 False】、現在明確可見的同一個 hwnd
命令前已可見、或可見性 UNKNOWN 的，一律不採認。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402

OUR_PID = 16276


class _Sess:
    pid = OUR_PID
    our_pids = {OUR_PID}
    main_hwnd = 5001
    main_pid = OUR_PID
    main_class = cq.MAIN_CLASS
    main_proc_started = None
    hproc = object()


def _drive(monkeypatch, windows_per_call, visibility):
    """windows_per_call: 每次 find_windows(CONSULT_CLASS) 回傳的 hwnd list。
    visibility: {hwnd: [每次查詢回什麼]}，元素 True / False / 'boom'。
    回傳被採認的 hwnd（沒有採認到就是 None）。"""
    monkeypatch.setattr(cq, "_session_death_reason", lambda _s: "")
    monkeypatch.setattr(cq, "resolve_menu_command_id", lambda _h: 42)
    monkeypatch.setattr(cq.win32gui, "PostMessage", lambda *a: None)
    n = {"i": -1}
    vcalls = {}

    def _vis(h):
        seq = visibility[h]
        idx = vcalls.get(h, 0)
        vcalls[h] = idx + 1
        v = seq[min(idx, len(seq) - 1)]
        if v == "boom":
            raise OSError("查不到")
        return v
    monkeypatch.setattr(cq.win32gui, "IsWindowVisible", _vis)

    def _find(cls=None, pids=None, **k):
        if cls != cq.CONSULT_CLASS:
            return []
        n["i"] += 1
        seq = windows_per_call
        return list(seq[min(n["i"], len(seq) - 1)])
    monkeypatch.setattr(cq, "find_windows", _find)
    t = {"v": 1000.0}
    monkeypatch.setattr(cq.time, "time",
                        lambda: t.__setitem__("v", t["v"] + 0.5) or t["v"])
    monkeypatch.setattr(cq.time, "sleep", lambda _s: None)
    got = {}
    monkeypatch.setattr(
        cq, "_capture_with_settled_roster",
        lambda h, **k: (got.__setitem__("c", h),
                        ("IMG", cq._RosterSnapshot([], True, [], [])))[1])
    monkeypatch.setattr(cq, "_extract_consult_text", lambda *a, **k: ("", "", []))
    monkeypatch.setattr(cq, "_return_to_main", lambda *a: None)
    try:
        cq._query_cycle(_Sess(), {}, "今日會診病人")
    except RuntimeError:
        pass                              # 等不到 = 沒有採認到
    return got.get("c")


def test_a_hidden_form_reshown_is_accepted(monkeypatch):
    """★不可以被這次收緊擋掉★ form 重用那條路仍要成立。"""
    picked = _drive(monkeypatch, [[7002]], {7002: [False, True]})
    assert picked == 7002, f"重新顯示的同一張會診單沒被採認(picked={picked})"


def test_unknown_before_state_is_not_treated_as_hidden(monkeypatch):
    """★核心★ 命令前查不到可見性 → 之後可見也不可採認。

    那張表單很可能命令前就是可見的（醫師自己開的／上一輪沒退乾淨），
    採認它就是把【別人的／過期的】病人清單擷取下來寄出去。
    """
    picked = _drive(monkeypatch, [[7002]], {7002: ["boom", True]})
    assert picked is None, (
        "★把『查不到』當成『明確隱藏』★ 會採認一張命令前就在的舊表單")


def test_an_already_visible_form_is_not_accepted(monkeypatch):
    """命令前就可見 → 不是本輪的結果。"""
    picked = _drive(monkeypatch, [[7002]], {7002: [True, True]})
    assert picked is None


def test_a_new_but_still_hidden_form_is_not_accepted(monkeypatch):
    """Delphi 常「先建立 → 載入資料 → 最後才 Show」。

    在它 Show 之前就開始擷取，拿到的是一張還沒填好的表單。
    """
    picked = _drive(monkeypatch, [[], [7003]], {7003: [False, False]})
    assert picked is None, "★新視窗還沒 Show 就被採認★ 會擷取到半成品"


def test_a_new_and_visible_form_is_accepted(monkeypatch):
    """★反方向★ 正常情況（命令後出現且已 Show）仍要採認。"""
    picked = _drive(monkeypatch, [[], [7003]], {7003: [True, True]})
    assert picked == 7003


def test_unknown_current_visibility_is_not_accepted(monkeypatch):
    """現在的可見性查不到 → 不採認（不知道就不要動它）。"""
    picked = _drive(monkeypatch, [[], [7003]], {7003: ["boom", "boom"]})
    assert picked is None


def test_the_visibility_probe_is_tri_state():
    """★判準本身★ 例外要回 None，不是 False。

    直接釘住這一點：上面那些行為測試若哪天被改寫，這一支仍會擋住
    「把讀不到冒充成明確答案」的回歸。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._query_cycle)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_visible":
            continue
        for h in ast.walk(node):
            if not isinstance(h, ast.ExceptHandler):
                continue
            returns = [n.value for n in ast.walk(h) if isinstance(n, ast.Return)]
            assert returns, "例外分支沒有回傳值"
            assert all(isinstance(r, ast.Constant) and r.value is None
                       for r in returns), (
                "★可見性查不到時回了一個確定的答案★ UNKNOWN 必須是 None")
            return
    raise AssertionError("找不到 _visible 的例外處理（測試失效了）")


def test_a_preexisting_window_with_unknown_visibility_is_still_warned(
        monkeypatch, caplog):
    """★三態也要反映在警告上★

    「命令前已有會診視窗」這句警告的判準原本是 `any(before.values())`——
    tri-state 之後 `None` 是 falsy，於是一個【可見性查不到】的既有視窗
    不會被提到。那正是最該讓人知道的狀態：本輪很可能等不到而白跑一輪。
    """
    import logging as _lg
    with caplog.at_level(_lg.WARNING):
        _drive(monkeypatch, [[7002]], {7002: ["boom", "boom"]})
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "送命令前已有" in msgs, (
        "★可見性未知的既有視窗沒被提到★ 這一輪等不到時會查不出原因")


def test_no_warning_when_the_only_window_is_definitely_hidden(
        monkeypatch, caplog):
    """★反方向★ 明確隱藏的殘留 form 是正常狀態（上一輪已退回主畫面）。"""
    import logging as _lg
    with caplog.at_level(_lg.WARNING):
        _drive(monkeypatch, [[7002]], {7002: [False, True]})
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "送命令前已有" not in msgs, f"正常狀態卻發警告:{msgs}"
