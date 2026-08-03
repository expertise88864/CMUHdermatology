# -*- coding: utf-8 -*-
"""放假是【殘量】，不做平均 —— 把這個宣稱與實作綁在一起。

（2026-08-03 使用者實測提問後更正文件；見 solve_day 模組 docstring）

原本 docstring 寫「7 RestStep 還沒位子 → 放假（放假次數輪平均）」，但
`fc.rest` / `fc.last_rest` 只被 RestStep 寫入，沒有任何選人 key 讀它。
更根本的是：

    放假 = 可排時段 − 工作時段

工作名額每時段固定，而照光/治療室/跟診各自已按次數平均 → 「工作次數平均」
與「放假次數平均」在每人可排時段不同時互斥。所以這是【刻意不做】。

★這份測試不是要禁止未來實作放假平均★，而是要求：真的要做的時候，
必須連同 docstring 一起改 —— 不可以讓文件又變成一句沒有實作的宣稱。
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from cmuh_common.roster import solve_day


def _code_of(func) -> str:
    """取原始碼並剝掉 docstring（掃原始碼的斷言不可被說明文字餵飽）。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    body = node.body[1:] if (
        node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)) else node.body
    return "\n".join(ast.unparse(n) for n in body)


def test_the_docstring_no_longer_claims_rest_is_balanced():
    """★宣稱要對得上實作★"""
    doc = solve_day.__doc__ or ""
    # ★不可以斷言「舊說法完全不出現」★ 新文件會【引用】那句話來說明它錯在哪，
    #   那是有價值的（下一個人才知道踩過什麼）。所以只看流程第 7 步那一行。
    step7 = next((ln for ln in doc.splitlines() if "RestStep" in ln), "")
    assert step7, "找不到 RestStep 那一行（測試失效了）"
    assert "輪平均" not in step7, (
        f"第 7 步又宣稱放假會被平均：{step7.strip()}")
    assert "殘量" in step7, f"第 7 步應該說明放假是殘量：{step7.strip()}"
    assert "互斥" in doc, "文件應該說明為什麼做不到（與工作次數平均互斥）"


def test_rest_counters_are_written_but_never_used_to_choose():
    """目前沒有任何選人 key 讀 `rest`／`last_rest`。

    ★這一支轉紅不代表「壞了」★ —— 它代表有人開始把放假次數納入輪選。
    那是可以的，但請同時把模組 docstring 的「刻意不做平均」改掉，
    再更新這支測試的預期。
    """
    readers = []
    for name in ("_seat", "PhotoStep", "TreatmentStep", "_pick"):
        obj = getattr(solve_day, name, None)
        if obj is None:
            continue
        src = textwrap.dedent(inspect.getsource(obj))
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # fc.rest.get(...) / fc.last_rest.get(...) 之類的「讀取」
            if (isinstance(node, ast.Attribute)
                    and node.attr in ("rest", "last_rest")
                    and isinstance(node.ctx, ast.Load)):
                readers.append(f"{name}:{node.lineno}")
    assert readers == [], (
        f"選人邏輯開始讀放假次數了（{readers}）—— 請一併更新模組 docstring")


def test_rest_is_the_last_step_of_the_pipeline():
    """放假是殘量的結構前提：它必須是最後一步，前面每一步都先搶位子。"""
    names = [type(step).__name__ for step in solve_day.PIPELINE]
    assert names[-1] == "RestStep", f"放假不再是最後一步：{names}"
    assert names.count("RestStep") == 1


def test_rest_step_does_not_choose_anyone():
    """RestStep 只是把「剩下的人」記下來，不做任何挑選。"""
    code = _code_of(solve_day.RestStep.run)
    # `sorted(...)` 只是為了輸出順序決定性，不是挑人 —— 判準要看有沒有
    # 「依 key 取最小」或「把人從 pool 移走」這種真正的選擇動作。
    tree = ast.parse(textwrap.dedent(code))
    chooses = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and ((isinstance(n.func, ast.Name) and n.func.id == "min")
                    or (isinstance(n.func, ast.Attribute)
                        and n.func.attr == "remove"))]
    assert chooses == [], "RestStep 開始挑人了 —— 那表示放假不再是純殘量"
    assert "ctx.pgy + ctx.clerk" in code, "應該是把剩下的人整批記為放假"
