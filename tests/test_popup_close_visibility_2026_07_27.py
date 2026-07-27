# -*- coding: utf-8 -*-
"""[2026-07-27 實機故障修正] 判定 popup「已關閉」必須看可見性,不是視窗物件是否被銷毀。

實機症狀(2026-07-27 08:49 診間):F10/F9 每次都停在
  「片語 popup(hwnd=70170)未在 5 秒內關閉 → 片語可能沒帶回」
  「Round 3 失敗(片語沒選成)→ 不自動送出,交醫師手動確認」
片語 popup 明明有開、帶回也有點,但 Delphi 的 modal form 關閉時多半只是 **Hide**
(表單物件留著重用)→ `IsWindow(hwnd)` 永遠是真 → 迴圈必定跑滿 5 秒 → 一律判失敗。

根因是我 2026-07-26 (v2026.07.26.5) 把等待條件從
`_find_window_by_class_title(...)`(它內部會跳過不可見視窗)改成只看 `IsWindow(hwnd)`,
弄丟了「不可見 = 已關閉」這個語意。保留「只看這一個 hwnd」的改進,條件改回可見性。
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def test_phrase_popup_close_uses_visibility():
    code = _code_only(inspect.getsource(main._select_phrase_and_return))
    tail = code[code.index("帶回"):]
    assert "IsWindowVisible(phrase_popup)" in tail, \
        "要用可見性判定關閉(Delphi modal form 只是 Hide,IsWindow 永遠為真)"
    assert "IsWindow(phrase_popup)" in tail, "仍要針對這一個 hwnd(不可退回 class 全域找)"


def test_consent_popup_close_uses_visibility():
    code = _code_only(inspect.getsource(main._f9_f10_wait_consent_popup_closed))
    assert "IsWindowVisible(popup_hwnd)" in code, \
        "同意書 popup 同一個地雷:只看 IsWindow 會每次都假警報『未送出』"


def test_dialog_close_checks_use_visibility():
    """★外審★ Round 4 的兩個對話框:【輪詢迴圈的離開條件】也要用可見性,不能只在
    逾時之後才補檢查 —— 否則每次都白等滿 5 秒(兩個對話框就是 10 秒),
    而且第一個還會被記成「server 寫入時間 5000ms」把等待誤植成伺服器耗時。"""
    code = _code_only(inspect.getsource(main._f9_f10_round4_submit_and_confirm))
    for handle in ("dlg", "dlg2", "popup_hwnd"):
        # 每一處 while 迴圈的離開條件都必須同時含 IsWindow 與 IsWindowVisible
        pat = f"not _u32.IsWindow({handle}) or not _u32.IsWindowVisible({handle})"
        assert pat in code, f"{handle} 的迴圈離開條件沒有用可見性"
    # 逾時後的最終判定也要一致
    assert "IsWindowVisible(dlg)" in code and "IsWindowVisible(dlg2)" in code


def test_class_lookup_skips_invisible_windows():
    """釘住根因事實:舊路徑之所以能通過,是因為 class 查詢會跳過不可見視窗。
    這條測試說明「為什麼可見性才是正確語意」,日後有人想改回 IsWindow 會先看到它。"""
    code = _code_only(inspect.getsource(main._find_window_by_class_title))
    assert "IsWindowVisible(hwnd)" in code
    i_vis = code.index("IsWindowVisible(hwnd)")
    assert "continue" in code[i_vis:i_vis + 120], "不可見就跳過 = 視同不存在"
