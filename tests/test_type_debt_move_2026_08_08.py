# -*- coding: utf-8 -*-
"""[任務 #52] 型別債棘輪不該把「抽出函式」當成新的技術債。

指紋是 `檔案|所在函式|訊息|那一行的原始碼`。`所在函式` 是為了分辨「同一個檔案裡
兩行字面完全相同的程式碼」而加的（基線裡真的有計數 2–5 的重複行）。

但它有一個副作用：把一段程式碼搬進新函式時（extract function），裡面每一筆**既有
的**診斷 scope 都變了 → 棘輪同時報「消失 N 筆 + 新增 N 筆」→ 紅燈。
2026-08-08 就發生過：`_final` 抽成模組層的 `_collect_final_uids` 之後 CI 連紅兩版。

所以 `diff_counts` 會把「消失側找得到同檔案/同訊息/同一行」的新增對消掉。
下面逐條測「對消得夠、但沒有對消過頭」—— 過頭的話棘輪就再也擋不住真正的新債。
"""
import io
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import type_debt  # noqa: E402

_MSG = "Type of \"x\" is partially unknown"
_SRC = "cells = row.find_all(td)"


def _fp(scope: str, msg: str = _MSG, src: str = _SRC, path: str = "src/a.py") -> str:
    return "|".join([path, scope, msg, src])


def test_a_diagnostic_that_only_moved_into_a_new_function_is_not_new_debt():
    """★核心★ 一模一樣的診斷，只是所在函式改了 → 不算新增。"""
    baseline = {_fp("old_caller"): 1}
    current = {_fp("_extracted_helper"): 1}
    added, gone = type_debt.diff_counts(baseline, current)
    assert added == {}, f"純搬家被當成新債 → 棘輪誤紅：{added}"
    # 「消失」那一側仍然照實回報（它不擋 CI，只印出來請人確認）。
    assert gone == {_fp("old_caller"): 1}


def test_a_second_copy_of_the_same_line_is_still_caught():
    """搬家容忍不可以吃掉「修一個、別處又寫一個一模一樣的」。

    舊的位置還在（計數沒少），新的位置多出來 → 那是真的多了一筆。
    """
    baseline = {_fp("f1"): 1}
    current = {_fp("f1"): 1, _fp("f2"): 1}
    added, gone = type_debt.diff_counts(baseline, current)
    assert added == {_fp("f2"): 1}, "同一行在別處又寫一個，棘輪必須看得到"
    assert gone == {}


def test_only_as_many_as_vanished_get_offset():
    """一筆消失、兩筆出現 → 只能對消一筆，剩下那筆仍是新債。"""
    baseline = {_fp("f1"): 1}
    current = {_fp("f2"): 2}
    added, gone = type_debt.diff_counts(baseline, current)
    assert added == {_fp("f2"): 1}, f"對消過頭了：{added}"


def test_a_different_message_is_not_a_move():
    baseline = {_fp("f1"): 1}
    current = {_fp("f2", msg="Import \"foo\" could not be resolved"): 1}
    added, _gone = type_debt.diff_counts(baseline, current)
    assert added, "訊息不同就是不同的診斷，不可以被當成搬家"


def test_a_different_source_line_is_not_a_move():
    baseline = {_fp("f1"): 1}
    current = {_fp("f2", src="rows = soup.select(tr)"): 1}
    added, _gone = type_debt.diff_counts(baseline, current)
    assert added, "出錯的那一行不同就是不同的診斷"


def test_a_different_file_is_not_a_move():
    baseline = {_fp("f1", path="src/a.py"): 1}
    current = {_fp("f1", path="src/b.py"): 1}
    added, _gone = type_debt.diff_counts(baseline, current)
    assert added, "搬到別的檔案不是同一筆診斷"


def test_nothing_vanished_means_nothing_can_be_offset():
    """沒有任何診斷消失時，新增就是新增（對消池是空的）。"""
    baseline = {}
    current = {_fp("f1"): 3}
    added, gone = type_debt.diff_counts(baseline, current)
    assert added == {_fp("f1"): 3}
    assert gone == {}


def test_a_malformed_fingerprint_does_not_crash_the_gate():
    """★關卡自己不可以炸★

    指紋格式若異常（欄位不足），例外會變成 CI 紅燈，而原因跟型別債無關 ——
    看到紅燈的人會去查型別債，查不到東西，最後把關卡關掉。
    """
    assert type_debt._without_scope("src/a.py") == "src/a.py"
    assert type_debt._without_scope("a|b") == "a|b"
    added, gone = type_debt.diff_counts({"a|b": 1}, {"a|c": 1})
    assert isinstance(added, dict) and isinstance(gone, dict)


def test_without_scope_keeps_file_message_and_source():
    assert type_debt._without_scope(_fp("whatever")) == "src/a.py|%s|%s" % (
        _MSG, _SRC)


def test_the_real_regression_extract_function(capsys):
    """重現 2026-08-08 那次：一批診斷整團從 `_final` 搬到 `_collect_final_uids`。"""
    lines = ["uid = uid_map.get(a)", "out.append(uid)", "return sorted(out)"]
    baseline = {_fp("_final", src=s): 1 for s in lines}
    current = {_fp("_collect_final_uids", src=s): 1 for s in lines}
    added, gone = type_debt.diff_counts(baseline, current)
    assert added == {}, f"整團搬家仍然紅燈 → 就是 v.4/v.5 那兩次 CI 失敗：{added}"
    assert len(gone) == 3
    assert "不算新債" in capsys.readouterr().out, "對消了就要說出來，不可以靜默"


# ───────────────────────────────────────────────────────────────────────────
# --fast：本機也跑得起這道關卡（#52 的另一半）
#
# v2026.08.08.4/.5 連兩版 CI 紅在型別債棘輪 —— 本機四道關卡全綠，唯一沒跑的
# 那一道就是紅的那一道。原因是 push_helper 刻意不跑它（註解寫「本機約 10 分鐘」）。
# 2026-08-08 實測：逐條 70.5 秒、一次跑完 6.3 秒，而且結果逐一比對完全一致。
# ───────────────────────────────────────────────────────────────────────────
_REPORT = {"generalDiagnostics": [
    {"severity": "error", "rule": "reportUnknownMemberType",
     "file": "src/a.py", "message": "m1", "range": {"start": {"line": 0}}},
    {"severity": "error", "rule": "reportUnknownMemberType",
     "file": "src/a.py", "message": "m1", "range": {"start": {"line": 0}}},
    {"severity": "error", "rule": "reportReturnType",
     "file": "src/a.py", "message": "m2", "range": {"start": {"line": 0}}},
    # 底下三筆都【不該】被算進去
    {"severity": "warning", "rule": "reportUnknownMemberType",
     "file": "src/a.py", "message": "m3", "range": {"start": {"line": 0}}},
    {"severity": "error", "rule": "reportGeneralTypeIssues",
     "file": "src/a.py", "message": "m4", "range": {"start": {"line": 0}}},
    {"severity": "error", "rule": None,
     "file": "src/a.py", "message": "m5", "range": {"start": {"line": 0}}},
]}
_RULES = ["reportUnknownMemberType", "reportReturnType", "reportIndexIssue"]


def test_fast_mode_splits_one_report_back_into_rules():
    got = type_debt._count_by_rule(_REPORT, _RULES)
    assert sum(got["reportUnknownMemberType"].values()) == 2
    assert sum(got["reportReturnType"].values()) == 1


def test_fast_mode_ignores_warnings_and_unrequested_rules():
    got = type_debt._count_by_rule(_REPORT, _RULES)
    total = sum(sum(v.values()) for v in got.values())
    assert total == 3, (
        "warning、沒被要求的規則、rule=None 都不屬於這道棘輪，"
        f"卻被算進來了：{got}")


def test_fast_mode_gives_every_requested_rule_a_bucket():
    """★沒有診斷的規則也要有桶子★

    少一個 key 的話 `main()` 會 KeyError → 關卡自己炸掉 → CI 紅在跟型別債
    無關的地方。0 筆是正常結果，不是缺席。
    """
    got = type_debt._count_by_rule(_REPORT, _RULES)
    assert set(got) == set(_RULES)
    assert got["reportIndexIssue"] == {}


def test_fast_mode_enables_every_baseline_rule(monkeypatch):
    """一次跑完必須把【每一條】規則都打開，漏掉一條就是安靜地少守一條。"""
    seen = {}

    def _fake(cfg, what):
        seen.update(cfg)
        return {"generalDiagnostics": []}

    monkeypatch.setattr(type_debt, "_run_pyright", _fake)
    rules = ["reportUnknownMemberType", "reportReturnType"]
    type_debt._all_rule_diagnostics(rules)
    assert [seen.get(r) for r in rules] == ["error", "error"], seen


def test_fast_may_not_write_the_baseline(monkeypatch, capsys):
    """★--fast 不可以拿來 --update★

    基線是 CI 判定的依據。若哪天「一次打開」真的因為規則互相遮蔽而少報，
    用它寫基線就會把那個削弱【固化】下來，而且從此看不見。
    """
    called = []
    monkeypatch.setattr(type_debt, "_load_baseline",
                        lambda: {"reportReturnType": {}})
    monkeypatch.setattr(type_debt, "_all_rule_diagnostics",
                        lambda r: called.append("fast") or {})
    monkeypatch.setattr(type_debt, "_rule_diagnostics",
                        lambda r: called.append("slow") or {})
    rc = type_debt.main(["--fast", "--update"])
    assert rc == 2, "--fast --update 必須被拒絕"
    assert called == [], "被拒絕之後不該還去跑 pyright"
    assert "--update" in capsys.readouterr().out


def test_push_helper_actually_runs_the_ratchet():
    """★這才是 #52 真正要防的事★

    v.4/v.5 之所以推上去才紅，是因為本機的推送關卡根本沒跑這道棘輪。
    用 AST 找 `subprocess` 參數列裡有沒有 `scripts/type_debt.py`（不掃註解 ——
    註解裡本來就寫滿了 type_debt.py，掃字串會被自己的散文餵飽）。
    """
    import ast
    path = os.path.join(REPO_ROOT, "scripts", "push_helper.py")
    with io.open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        parts = [e.value for e in node.elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if any("type_debt.py" in p for p in parts):
            found.append(parts)
    assert found, "push_helper 沒有把型別債棘輪納入推送前的關卡"
    assert any("--fast" in p for p in found), (
        f"跑了棘輪但沒用 --fast（逐條 70 秒 vs 一次 6 秒）：{found}")


def test_push_helper_counts_the_ratchet_as_a_blocking_gate():
    """跑了但不擋 = 沒跑。棘輪要走 `_step`（失敗會進 `failed` 清單）。"""
    import ast
    path = os.path.join(REPO_ROOT, "scripts", "push_helper.py")
    with io.open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    ok = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_step"):
            src = ast.dump(node)
            if "type_debt.py" in src:
                ok = True
    assert ok, "棘輪不是用 _step 跑的 → 紅燈不會擋下 push"
