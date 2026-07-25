# -*- coding: utf-8 -*-
"""[2026-07-26 審查] 代碼輸入的焦點守衛:兩個分支的假設必須一致。

本函式存在的理由就是「選單命令沒生效時,焦點可能仍停在醫師的病歷 TMemo/TRichEdit」。
但「有前焦點」的分支原本連 memo/rich 也收 —— 焦點從甲 memo 移到乙 memo 一樣通過
`focus != previous_focus`,代碼(51019 等)就被 key 進病歷內文。
嚴格分支(前焦點未知)早就假設代碼輸入欄不是 memo/rich,兩邊必須一致。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


@pytest.fixture
def focus_env(monkeypatch):
    state = {"focus": 0, "cls": ""}
    monkeypatch.setattr(main, "_get_thread_focus", lambda _h: state["focus"])
    monkeypatch.setattr(main, "_get_class_name_of", lambda _h: state["cls"])
    monkeypatch.setattr(main, "_sleep_interruptible", lambda _s: None)
    return state


@pytest.mark.parametrize("cls", ["TMemo", "TRichEdit", "TDBRichEdit"])
def test_chart_text_controls_rejected_even_when_focus_moved(focus_env, caplog, cls):
    """★病歷污染★ 焦點從甲 memo 移到乙 memo 一樣通過 `focus != previous_focus`,
    接著就是 WM_CHAR 打入 51019 等代碼 + Enter → 直接寫進病歷內文。事後 log 只留
    證據、擋不住污染,必須當場拒絕。"""
    focus_env.update(focus=555, cls=cls)
    with caplog.at_level("WARNING"):
        got = main._wait_for_code_input_focus(1, previous_focus=444, timeout=0.05)
    assert got == 0, f"{cls} 不可被當成代碼輸入欄"
    assert any("病歷內文類控件" in r.getMessage() for r in caplog.records),         "拒絕時要說明原因(多半是選單命令沒生效),否則現場無從判斷"


def test_rejection_must_not_use_a_naive_whitelist():
    """踩過的坑:"TRichEdit"/"TDBRichEdit" 的 class 名裡就含 "edit" —— 只是把 memo/rich
    從白名單拿掉【擋不住】它們,必須明確排除。用這個測試把坑釘住。"""
    for cls in ("trichedit", "tdbrichedit"):
        assert any(s in cls for s in ("edit", "grid")),             "白名單擋不住 rich edit,必須明確排除 memo/rich"


@pytest.mark.parametrize("cls", ["TInplaceEdit", "TStringGrid", "TEdit"])
def test_code_editors_still_accepted(focus_env, cls):
    """不可誤擋:真的代碼輸入欄(格線內嵌編輯器 / 單行 edit)照常通過。"""
    focus_env.update(focus=555, cls=cls)
    assert main._wait_for_code_input_focus(
        1, previous_focus=444, timeout=0.05) == 555


def test_focus_unchanged_is_rejected(focus_env):
    """焦點根本沒移動 → 選單命令沒生效,不可通過(既有行為)。"""
    focus_env.update(focus=444, cls="TInplaceEdit")
    assert main._wait_for_code_input_focus(
        1, previous_focus=444, timeout=0.05) == 0


@pytest.mark.parametrize("cls,expect", [
    ("TInplaceEdit", 555), ("TStringGrid", 555),
    ("TEdit", 0), ("TMemo", 0), ("TRichEdit", 0),
])
def test_strict_branch_unchanged(focus_env, cls, expect):
    """前焦點未知的嚴格分支行為不可被這次改動影響:只收正面辨識的格線內嵌編輯器。"""
    focus_env.update(focus=555, cls=cls)
    assert main._wait_for_code_input_focus(
        1, previous_focus=0, timeout=0.05) == expect


def test_non_grid_acceptance_is_logged(focus_env, caplog):
    """收下非格線編輯器時要留證據 —— 沒有實機日誌就沒有依據把這分支也收緊,
    而盲目收緊會誤擋 F1-F5、讓診間停擺。"""
    focus_env.update(focus=555, cls="TEdit")
    with caplog.at_level("WARNING"):
        main._wait_for_code_input_focus(1, previous_focus=444, timeout=0.05)
    assert any("不是格線內嵌代碼編輯器" in r.getMessage() for r in caplog.records)
