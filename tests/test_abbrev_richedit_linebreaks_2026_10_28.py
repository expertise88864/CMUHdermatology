# -*- coding: utf-8 -*-
"""[2026-10-28] 縮寫在 HIS 展得開、在 Windows 11 記事本(第二行起)展不開。

記事本的編輯區是 RichEditD2DPT(RichEdit 2.0+):EM_GETSEL 把換行算 1 格,WM_GETTEXT
卻回 `\\r\\n`(2 格)。實機量到:三行文字 caret=33、全文長 35 → 核對到的是 'd ne'
而不是 'nev ' → 輪詢到逾時 → ABORT,安靜地不展開。HIS 的 Edit/TMemo 兩邊都算 2。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import abbrev_engine as ae  # noqa: E402

NOTEPAD_TEXT = "first line\r\nsecond line\r\nthird nev "   # 實機 WM_GETTEXT 原樣


@pytest.mark.parametrize("class_name, one", [
    ("RichEditD2DPT", True),     # Windows 11 記事本
    ("RichEdit20W", True),
    ("RICHEDIT50W", True),       # WordPad
    ("TRichEdit", True),         # Delphi TRichEdit(包 RichEdit 2.0)
    ("RichEdit", False),         # RichEdit 1.0 仍是 CRLF 算 2
    ("Edit", False),
    ("TMemo", False),            # HIS 欄位
    ("TEditExt", False),
    ("", False),
])
def test_which_controls_count_a_line_break_as_one(monkeypatch, class_name, one):
    monkeypatch.setattr(ae, "_get_window_class_name", lambda h: class_name)
    assert ae._line_break_is_one_position(1) is one


def _fake_control(monkeypatch, class_name, caret, text):
    monkeypatch.setattr(ae, "_get_focused_window_handle", lambda: 7)
    monkeypatch.setattr(ae, "_get_window_class_name", lambda h: class_name)
    monkeypatch.setattr(ae, "_get_edit_selection", lambda h: (caret, caret))
    monkeypatch.setattr(ae, "_read_window_text", lambda h: text)
    replaced, setsel = [], []
    monkeypatch.setattr(ae, "_replace_edit_selection",
                        lambda h, s, e, t: replaced.append((s, e, t)) or True)

    def fake_smt(hwnd, msg, w=0, lparam=0, timeout_ms=80):
        if msg == ae._EM_SETSEL:
            setsel.append((w, lparam))
        return True, 0
    monkeypatch.setattr(ae, "_send_message_timeout", fake_smt)
    monkeypatch.setattr(ae.time, "sleep", lambda s: None)
    return replaced, setsel


def test_notepad_third_line_expands(monkeypatch):
    """實機數字:caret=33(換行算 1)對上 35 字的 WM_GETTEXT。"""
    replaced, _ = _fake_control(monkeypatch, "RichEditD2DPT", 33, NOTEPAD_TEXT)
    assert ae._replace_native_edit_suffix("nev ", "nevus ", 0) == ae._NATIVE_REPLACED
    assert replaced == [(29, 33, "nevus ")]


def test_multiline_expansion_uses_cr_and_places_the_caret(monkeypatch):
    """RichEdit 插入用 \\r(一格);游標標記在 A 之後 → caret = start + 1。"""
    replaced, setsel = _fake_control(monkeypatch, "RichEditD2DPT", 33, NOTEPAD_TEXT)
    res = ae._replace_native_edit_suffix("nev ", "A\nB", 0, cursor_left=2)
    assert res == ae._NATIVE_REPLACED
    assert replaced == [(29, 33, "A\rB")]
    assert setsel == [(30, 30)]
    replaced.clear(); setsel.clear()
    res = ae._replace_native_edit_suffix("nev ", "A\r\nB", 0, cursor_left=3)
    assert res == ae._NATIVE_REPLACED and replaced == [(29, 33, "A\rB")]
    assert setsel == [(30, 30)]


def test_word_boundary_check_uses_the_same_positions(monkeypatch):
    """字邊界看的是換算後的前一字元:'xnev ' 在第三行 → 前一字元 'x' → 不展開。"""
    text = "first line\r\nsecond line\r\nthirdxnev "
    replaced, _ = _fake_control(monkeypatch, "RichEditD2DPT", 33, text)
    assert ae._replace_native_edit_suffix("nev ", "X", 0) == ae._NATIVE_ABORT
    assert replaced == []


def test_standard_edit_is_unchanged(monkeypatch):
    """HIS 的 TMemo:位置與全文都算 2 → 照舊直接索引,插入仍用 \\r\\n。"""
    replaced, setsel = _fake_control(monkeypatch, "TMemo", 35, NOTEPAD_TEXT)
    res = ae._replace_native_edit_suffix("nev ", "A\nB", 0, cursor_left=2)
    assert res == ae._NATIVE_REPLACED
    assert replaced == [(31, 35, "A\r\nB")]
    assert setsel == [(32, 32)]
