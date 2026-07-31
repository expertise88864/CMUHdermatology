# -*- coding: utf-8 -*-
"""[2026-07-31 使用者] 「院內系統捷徑」左右兩排按鈕高度沒對齊。

原話：「為什麼院內系統捷徑 左右兩邊按鈕高度沒對齊 就是排檢程式跟新版住院系統
電子簽章跟值班查詢上下高度不一樣」。

★成因★ 左右兩邊用不同的排版管理員、不同的垂直間距，而且沒有任何東西把兩邊的
【列】綁在一起：
  * 左：`sticky="nw"` 的容器 + 內部 pack，每顆按鈕 pady=1，第二列的 frame 再多
    一個 pady=(1,0) → 第一列差 1px、第二列累積差到 3-4px。
  * 右：`sticky="nsew"` 的容器 + 內部 grid，pady=0；而且值班資訊放在它自己容器裡的
    `col5, rowspan=2`，會反過來撐開右側列高，按鈕（`sticky='ew'`）在變高的列裡被
    垂直置中再位移一次 —— 位移量還會隨字體縮放而變。

★這一檔量的是【按鈕真的在螢幕上的 y 座標】，不是 padding 數字★
對齊是視覺結果，斷言 padding 只能證明「我照著我以為的方式設定了」。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


@pytest.fixture
def links(tk_root):
    """用【生產的】骨架與填充函式建一份捷徑列 → (frame, 各排的按鈕)。"""
    import tkinter as tk
    from tkinter import ttk

    app = object.__new__(main.AutomationApp)
    for name in ("duty_row1_prefix_var", "duty_row1_name_var",
                 "duty_row1_vs_lbl_var", "duty_row1_vs_name_var",
                 "duty_row2_prefix_var", "duty_row2_name_var",
                 "duty_row2_vs_lbl_var", "duty_row2_vs_name_var"):
        setattr(app, name, tk.StringVar(master=tk_root, value="值班 某某"))

    frame = ttk.LabelFrame(tk_root, text="院內系統捷徑")
    frame.pack(fill="x")
    rows = main.AutomationApp._build_links_grid(frame)
    app._populate_link_buttons(*rows)
    tk_root.update_idletasks()
    local1, local2, web1, web2, _duty = rows
    yield frame, local1, local2, web1, web2
    frame.destroy()


def _buttons(container):
    from tkinter import ttk
    return [w for w in container.winfo_children() if isinstance(w, ttk.Button)]


def _text_of(container, label):
    for b in _buttons(container):
        if b.cget("text") == label:
            return b
    raise AssertionError(f"找不到按鈕：{label}")


def _top_in_frame(widget, frame) -> int:
    """widget 相對於 links_frame 的 y 座標（跨中間那層 row frame）。"""
    return widget.winfo_rooty() - frame.winfo_rooty()


def test_the_two_buttons_the_user_named_are_aligned(links):
    """★使用者指名的第一組：排檢程式（左排一）vs 新版住院系統（右排一）★"""
    frame, local1, _local2, web1, _web2 = links
    left = _top_in_frame(_text_of(local1, "排檢程式"), frame)
    right = _top_in_frame(_text_of(web1, "新版住院系統"), frame)
    assert left == right, f"第一排沒對齊：左 y={left} 右 y={right}"


def test_the_second_pair_the_user_named_is_aligned(links):
    """★使用者指名的第二組：電子簽章（左排二）vs 值班查詢（右排二）★
    這一組原本差最多（左邊第二列的 frame 還多一層 pady）。"""
    frame, _local1, local2, _web1, web2 = links
    left = _top_in_frame(_text_of(local2, "電子簽章"), frame)
    right = _top_in_frame(_text_of(web2, "值班查詢"), frame)
    assert left == right, f"第二排沒對齊：左 y={left} 右 y={right}"


def test_every_button_in_a_row_shares_the_same_top(links):
    """不只使用者指名的那兩顆 —— 整排都要齊。"""
    frame, local1, local2, web1, web2 = links
    for row_no, (left_box, right_box) in enumerate(
            ((local1, web1), (local2, web2)), start=1):
        tops = {b.cget("text"): _top_in_frame(b, frame)
                for b in _buttons(left_box) + _buttons(right_box)}
        assert len(set(tops.values())) == 1, f"第 {row_no} 排高度不一致：{tops}"


def test_the_two_rows_are_actually_different_rows(links):
    """★防空洞★ 若兩排疊在一起，上面每一支都會「通過」。"""
    frame, local1, local2, _web1, _web2 = links
    assert (_top_in_frame(_buttons(local2)[0], frame)
            > _top_in_frame(_buttons(local1)[0], frame))


def test_both_sides_use_the_same_vertical_padding():
    """兩邊的 pady 綁在同一個常數上 —— 沒有「只改了一邊」這種可能。"""
    import ast
    import inspect
    import textwrap

    # ★用 AST 去掉註解與 docstring★ 解釋「原本 pady=1 才會歪」的那段註解會
    # 命中比對字串（本輪已經被自己的說明騙過好幾次）。這裡只看真的會執行的部分。
    src = inspect.getsource(main.AutomationApp._populate_link_buttons)
    code = ast.unparse(ast.parse(textwrap.dedent(src)))
    assert code.count("pady=self._LINK_BTN_PADY") == 4, "四排都要用同一個常數"
    assert "pady=1" not in code and "pady=(1," not in code, \
        "不可以再出現寫死的 pady（那正是原本歪掉的原因）"


def test_the_duty_block_no_longer_lives_inside_the_web_container():
    """值班資訊移到自己的欄：它撐高時左右一起受影響，不會只把右邊推歪。"""
    import inspect
    src = inspect.getsource(main.AutomationApp._build_links_grid)
    assert "column=3, rowspan=2" in src
    populate = inspect.getsource(main.AutomationApp._populate_link_buttons)
    assert "rowspan=2" not in populate, "填充函式不該再自己安排跨列的東西"


def test_the_duty_block_growing_does_not_break_the_alignment(links, tk_root):
    """★會隨字體/內容而變的那一項★ 值班文字變多時，兩排按鈕仍要齊。"""
    frame, local1, local2, web1, web2 = links
    app_vars = [w for w in frame.winfo_children()]
    del app_vars
    for child in frame.winfo_children():
        for lbl in child.winfo_children():
            try:
                lbl.configure(text="值" * 30)
            except Exception:
                pass
    tk_root.update_idletasks()
    for left_box, right_box in ((local1, web1), (local2, web2)):
        tops = {_top_in_frame(b, frame)
                for b in _buttons(left_box) + _buttons(right_box)}
        assert len(tops) == 1, f"值班區變高之後又歪了：{tops}"
