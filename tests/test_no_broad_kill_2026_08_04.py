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


# ── [2026-08-05 外審第 5 輪 P1-08] SW_HIDE 後備不可以借用醫師的 HIS ──────────
def test_the_sw_hide_fallback_never_borrows_an_existing_login():
    """★這條路跑在【使用者自己的桌面】上★

    舊寫法 `pick = fresh or cands` —— 找不到新的就撿一個既有的登入視窗。
    那極可能就是醫師剛打開、正要自己登入的住院系統。接下來我們會：
        把自動化帳密打進去 → 把他的視窗移到螢幕外 → 開會診單
        → 擷取全院病人資料 → 最後試著還原
    「收尾不關掉它」只把傷害縮小到「不關窗」，前面那一串照做不誤。

    ★隱藏桌面那條路徑不同★：`find_windows` 只列舉呼叫緒所在桌面，而那條已經
    SetThreadDesktop 到我們自己建的隱藏桌面 —— 撿到的必然是我們前幾輪留下的
    孤兒，重用它反而是對的。所以這一支只釘後備模式。
    """
    import ast
    import inspect
    import textwrap

    import consult_query as cq

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._run_with_sw_hide)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "pick" for t in node.targets):
            continue
        # `pick = fresh or cands` 就是那個借用寫法
        v = node.value
        assert not (isinstance(v, ast.BoolOp) and isinstance(v.op, ast.Or)), (
            "★後備模式又用 `fresh or cands` 借用既有登入視窗★ "
            "會對醫師自己開的住院系統輸入帳密")


def test_the_hidden_desktop_path_may_still_reuse_an_orphan():
    """★反方向：隱藏桌面那條不可以一起改成 fail-closed★

    那裡撿到的必然是我們自己前幾輪留下的孤兒（醫師的 HIS 在互動桌面，
    不可能被列舉到）。改成放棄只會讓「有孤兒佔位時永遠查不到」。
    """
    import inspect

    import consult_query as cq

    src = inspect.getsource(cq._cold_start_session_impl)
    assert "fresh or cands" in src, (
        "隱藏桌面那條被一起改成 fail-closed → 有孤兒佔位時會永遠查不到")


def test_a_borrowed_pid_is_refused_not_just_logged():
    """★第二道門也要 fail-closed★

    `fresh` 的定義已經排除了「啟動前就存在的 pid」，但 pid 可能在等待期間被
    回收（我們選中的視窗，其 pid 剛好等於某個 before 裡的死 pid）。那時身分
    無法確定，一樣不可以拿它輸入帳密 —— 不是印一行 warning 就繼續。

    突變驗證抓到的洞：把那個 raise 換成 `logging.warning(...)`，上面那支
    「不可以用 fresh or cands」照樣全綠。
    """
    import ast
    import inspect
    import textwrap

    import consult_query as cq

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._run_with_sw_hide)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if "borrowed" not in names:
            continue
        assert any(isinstance(n, ast.Raise) for n in ast.walk(node)), (
            "★偵測到借用卻只寫 log 就繼續★ 會對醫師的住院系統輸入帳密")
        return
    raise AssertionError("找不到 borrowed 的判斷")
