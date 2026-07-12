# -*- coding: utf-8 -*-
"""UVB批次5(P3)安全子集 回歸測試(2026-07-12)。

UD-08 read-back 空值保守中止;UD-12 逐字 IsWindow 把關;UD-13 警告框納 awaiting scope。
(UD-07/09/10/11/14 涉確認迴圈重構/文案 threading/core stub,緩修;UC-10/UC-12/UD-15 安全方向/BY-DESIGN。)
"""
import os


def _main_src():
    p = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    with open(p, encoding="utf-8") as f:
        return f.read()


def _func(src, name):
    start = src.find(name)
    assert start != -1, f"找不到 {name}"
    nxt = src.find("\ndef ", start + 1)
    return src[start:nxt if nxt != -1 else len(src)]


# ── UD-12 _send_chars_to_window 逐字 IsWindow 把關 ───────────────────────────
def test_ud12_send_chars_checks_iswindow():
    body = _func(_main_src(), "def _send_chars_to_window")
    assert "IsWindow(hwnd)" in body, "UD-12 未逐字確認視窗仍在"
    # IsWindow 檢查須在 PostMessageW 之前
    assert body.index("IsWindow(hwnd)") < body.index("PostMessageW(hwnd, WM_CHAR"), \
        "UD-12 IsWindow 未置於送字之前"


# ── UD-13 _show_uvb_warning 的 MessageBoxW 納 awaiting scope ─────────────────
def test_ud13_warning_uses_awaiting_scope():
    body = _func(_main_src(), "def _show_uvb_warning")
    assert "_hotkey_awaiting_user_scope()" in body and "MessageBoxW" in body, \
        "UD-13 警告框未納 awaiting scope"
    assert body.index("_hotkey_awaiting_user_scope()") < body.index("MessageBoxW(main_hwnd"), \
        "UD-13 scope 未包住 MessageBoxW"


# ── UD-08 read-back 空字串保守中止 ───────────────────────────────────────────
def test_ud08_empty_readback_aborts():
    src = _main_src()
    lines = src.splitlines()
    idx = next((i for i, ln in enumerate(lines)
                if "actual_text = _read_tmemo_text(memo_hwnd)" in ln), None)
    assert idx is not None, "找不到 read-back 段"
    window = "\n".join(lines[idx:idx + 12])
    assert "if not actual_text:" in window and "return False" in window, \
        "UD-08 read-back 空字串未保守中止"
