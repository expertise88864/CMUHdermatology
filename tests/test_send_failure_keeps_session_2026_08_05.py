# -*- coding: utf-8 -*-
"""寄信失敗不可以收掉健康的 HIS 登入（外審第 4 輪 P1-10）。

`_kill_systemftp` 現在做的事是 `_session_close(...)` —— 收掉常駐登入。
但它掛在**所有**可重試錯誤上，包括組信／截圖落地／SMTP／Outlook：

    會診查完（HIS 一切正常）→ SMTP timeout → 收掉登入
    → 下一次 attempt 冷啟動 → ★再送一次帳密★

帳密重送正是這幾批一路在防的事（2026-08-04 實機：每 3 分鐘送一次），
而這裡的失敗**根本不在 HIS 那一側**。函式自己的註解也已經寫著這個劇本：
「會診查完 → 醫師手動開 HIS → SMTP timeout → 醫師的 HIS 被強殺」——
taskkill 那一半修掉了，session 重置這一半還在。
"""
import ast
import inspect
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402

_SRC = textwrap.dedent(inspect.getsource(cq._do_full_job))
_TREE = ast.parse(_SRC)


def _retry_branch_ifs():
    """重試分支裡所有 if 節點。"""
    return [n for n in ast.walk(_TREE) if isinstance(n, ast.If)]


def test_the_his_stage_is_marked_done_right_after_the_flow():
    """★旗標要立在正確的位置★

    必須是「`run_consult_flow` 回來之後、其他事情之前」。立太早（迴圈外／
    呼叫之前）等於永遠是 True → 連 HIS 真的壞掉都不重置；立太晚（寄信之後）
    等於永遠是 False → 這個修正沒有作用。
    """
    body = None
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.Try):
            continue
        stmts = node.body
        for i, st in enumerate(stmts):
            call_names = {n.func.id for n in ast.walk(st)
                          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if "run_consult_flow" in call_names:
                body = stmts[i + 1] if i + 1 < len(stmts) else None
    assert body is not None, "找不到 run_consult_flow 之後的那一行"
    assert isinstance(body, ast.Assign), ast.dump(body)[:200]
    assert body.targets[0].id == "his_stage_done"
    assert body.value.value is True, "run_consult_flow 之後必須立刻標記 HIS 這段已完成"


def test_the_flag_is_reset_every_attempt():
    """★每個 attempt 都要重設★

    不重設的話：第 1 次 attempt 寄信失敗（旗標 True）→ 第 2 次 attempt 的
    HIS 階段炸掉 → 旗標還是 True → 不重置 session → 一個壞掉的 session
    會被一路重用到放棄。
    """
    for node in ast.walk(_TREE):
        if isinstance(node, ast.For) and getattr(node.target, "id", "") == "attempt":
            first = node.body[0]
            assert isinstance(first, ast.Assign), ast.dump(first)[:200]
            assert first.targets[0].id == "his_stage_done"
            assert first.value.value is False
            return
    raise AssertionError("找不到 attempt 迴圈")


def _kill_runs_when(flag_value: bool) -> bool:
    """`his_stage_done` 是這個值的時候，`_kill_systemftp` 會不會被執行。

    ★用求值、不用比對形狀★ 第一版寫成「kill 必須在 else 那一側」，於是
    `if not his_stage_done: 保留 else: 殺` —— 方向整個相反 —— 照樣全綠
    （kill 確實在 else 側）。突變驗證抓到了。改成把守衛條件在給定旗標值下
    **實際算一次**，`if X` 與 `if not X` 就再也騙不過去。
    """
    for node in _retry_branch_ifs():
        if not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_kill_systemftp" for n in ast.walk(node)):
            continue
        if "his_stage_done" not in {n.id for n in ast.walk(node.test)
                                    if isinstance(n, ast.Name)}:
            continue
        taken = eval(  # noqa: S307 - 受控:只求值這個檔案自己的守衛條件
            compile(ast.Expression(body=node.test), "<guard>", "eval"),
            {"__builtins__": {}}, {"his_stage_done": flag_value})
        branch = node.body if taken else node.orelse
        return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_kill_systemftp"
                   for n in ast.walk(ast.Module(body=branch, type_ignores=[])))
    raise AssertionError(
        "★_kill_systemftp 沒有被 his_stage_done 守著★ 寄信失敗仍會收掉 HIS 登入")


def test_a_send_failure_does_not_reset_the_his_session():
    """★核心★ HIS 那段做完了（＝失敗在寄信）→ 不可以收掉登入。"""
    assert _kill_runs_when(True) is False, (
        "★寄信失敗卻收掉了常駐登入★ 下一次 attempt 會再送一次帳密")


def test_a_his_failure_still_resets():
    """★反方向:不可以修成「永遠不重置」★

    HIS 那一段本來就會卡死／wedged，那時候【必須】重置，否則下一次 attempt
    撞上同一個壞掉的實例。
    """
    assert _kill_runs_when(False) is True, (
        "HIS 階段失敗時仍然必須重置(否則下一次 attempt 撞上壞掉的實例)")


def test_the_message_does_not_claim_we_killed_anything():
    """★措辭鐵律★ 保留 session 那條路徑不可以印「殺 systemftp.exe 後重試」。"""
    i = _SRC.index("his_stage_done:")
    window = _SRC[i:i + 700]
    assert "保留常駐登入" in window, window[:300]
    kept = window[:window.index("else:")]
    assert "殺 systemftp" not in kept, kept
