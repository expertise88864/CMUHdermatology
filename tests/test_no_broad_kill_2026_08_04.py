# -*- coding: utf-8 -*-
"""重試清理不可以用全機 PID 差集當 kill 授權（2026-08-04 外審第 3 輪 P1-06）。

【問題】
`_kill_systemftp(before_pids)` 算出 `目前 systemftp PID − 任務開始前快照`，然後對
整個差集執行 `taskkill /F`。它完全繞過這幾輪建立的 ownership 驗證：醫師若在本次
任務【執行期間】手動開住院系統，他的行程不在 before 快照裡 → 落進差集 → 被強殺。

而它會在任何可重試錯誤前執行，包括【寄信失敗】：

    會診查完 → 醫師手動開 HIS → SMTP timeout → 醫師的 HIS 被 taskkill

【修法】改成收掉【我們自己的 session】（對確切主畫面送 WM_CLOSE 並回讀確認），
差集只留作證據。真的清不掉時，寧可讓下一次 attempt 撞上「最多兩個」而★明確失敗★，
也不要無聲強殺別人的程式 —— 前者看得見、修得了；後者醫師只會看到自己的系統消失。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def test_the_module_never_invokes_taskkill():
    """★不得再有任何一條路【呼叫】taskkill★

    ★判準是「有沒有呼叫」，不是「有沒有提到」★（第一版就是這樣紅的）：
    `_kill_systemftp` 的 docstring 本身就寫著「絕不再 taskkill /IM 全機掃殺」，
    所以「掃所有字串常數」會被自己的說明文字餵飽 —— 本 session 第三次踩到
    「掃原始碼的斷言被自己的散文餵飽」。

    改成看實際的呼叫引數：任何 `f([... "taskkill" ...])` 形式的參數列都算。
    """
    import ast
    import pathlib

    src_path = (pathlib.Path(__file__).resolve().parent.parent
                / "src" / "consult_query.py")
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            elts = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else []
            for e in elts:
                if (isinstance(e, ast.Constant)
                        and isinstance(e.value, str)
                        and "taskkill" in e.value.lower()):
                    offenders.append(node.lineno)
    assert offenders == [], (
        f"★consult_query 仍會呼叫 taskkill（行 {offenders}）★ "
        "會強殺醫師在任務期間開的 HIS")


def test_cleanup_closes_our_own_session_instead(monkeypatch):
    """清理要走「收掉自己的 session」那條有身分依據的路。"""
    closed = []
    monkeypatch.setattr(cq, "_session_close", lambda why: closed.append(why))
    monkeypatch.setattr(cq, "_systemftp_pids", lambda: {1, 2})

    cq._kill_systemftp({1})

    assert closed, "沒有收掉自己的 session"
    assert "重試" in closed[0], f"理由說不清楚：{closed[0]}"


def test_a_process_that_appeared_is_only_logged_not_killed(monkeypatch,
                                                           caplog):
    """★差集只留作證據★ 出現了要記下來，但不動它。"""
    import logging as _lg
    monkeypatch.setattr(cq, "_session_close", lambda _w: None)
    monkeypatch.setattr(cq, "_systemftp_pids", lambda: {1, 777})

    with caplog.at_level(_lg.INFO):
        cq._kill_systemftp({1})

    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "777" in msgs, f"沒有留下證據：{msgs}"
    assert "不強殺" in msgs, "沒有說明它不會被殺掉"


def test_no_snapshot_still_closes_the_session(monkeypatch):
    """★反方向:沒有快照不代表什麼都不做★ 自己的 session 仍要收。

    舊版在 before_pids=None 時直接 return（那時是為了避免全機掃殺）。現在收自己的
    session 不需要任何快照，沒有理由跳過。
    """
    closed = []
    monkeypatch.setattr(cq, "_session_close", lambda why: closed.append(why))
    cq._kill_systemftp(None)
    assert closed, "沒有快照就什麼都不做 → 重試會撞上自己上一輪的 wedged session"


def test_the_retry_path_still_calls_cleanup():
    """★接線本身也要被測到★（本 session 這個形狀第九次）"""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._do_full_job)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_kill_systemftp" in called, "重試前不再做任何清理"
