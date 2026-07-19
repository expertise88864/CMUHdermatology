# -*- coding: utf-8 -*-
"""醫令代碼送出的兩個 P1 邊界(GPT-5.6 第三輪 → codex deep 查證確認)。

1. 半截醫令:字元中途送失敗(chars_ok=False)後【絕不可按 Enter】—— Enter 會把欄位裡的
   半截代碼(51019 → 510)真的提交進 HIS。
2. 提交點:Delphi TranslateMessage 對 WM_KEYDOWN 轉出 Enter 的 WM_CHAR → keydown 被接受
   = 醫令可能已提交。keyup 失敗不可回 False(呼叫端會誤判「沒送出」→ 跳過療程/記 failed,
   重試還可能重複下醫令)。keydown 失敗才是真的沒送。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


class _FakeUser32:
    """按序回覆 PostMessageW 結果;其餘 API 交回真 user32(IsWindow 恆真)。"""

    def __init__(self, post_results):
        self._results = list(post_results)
        self.calls = []

    def PostMessageW(self, hwnd, msg, w, l):  # noqa: N802
        self.calls.append((msg, w))
        return self._results.pop(0) if self._results else 1

    def IsWindow(self, hwnd):  # noqa: N802
        return 1


def _patch_user32(monkeypatch, post_results):
    fake = _FakeUser32(post_results)
    monkeypatch.setattr(main.ctypes, "windll",
                        type("W", (), {"user32": fake})())
    return fake


WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101


# ── _send_enter_to_window:提交點語意 ────────────────────────────────────────
def test_enter_keydown_fail_returns_false(monkeypatch):
    fake = _patch_user32(monkeypatch, [0])            # keydown 被拒
    assert main._send_enter_to_window(123) is False
    assert [m for m, _ in fake.calls] == [WM_KEYDOWN], "keydown 失敗就不該再送 keyup"


def test_enter_keyup_fail_after_keydown_still_true(monkeypatch):
    # keydown 已被接受 = 提交點已過 → 即使 keyup 失敗也回 True(避免呼叫端誤判沒送出
    # 而中止療程/重試重複下醫令);會補送一次 keyup。
    fake = _patch_user32(monkeypatch, [1, 0, 0])      # down ok, up 失敗, 補送也失敗
    assert main._send_enter_to_window(123) is True
    assert [m for m, _ in fake.calls] == [WM_KEYDOWN, WM_KEYUP, WM_KEYUP]


def test_enter_normal_path_true(monkeypatch):
    _patch_user32(monkeypatch, [1, 1])
    assert main._send_enter_to_window(123) is True


# ── _send_chars_to_window:PostMessage 回 0 → 中止 ──────────────────────────
def test_chars_abort_on_postmessage_zero(monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)
    fake = _patch_user32(monkeypatch, [1, 1, 0])      # 第 3 個字元被拒
    assert main._send_chars_to_window(123, "51019") is False
    # 只送出 3 次嘗試(第 3 次失敗即中止),不會把剩餘字元硬塞
    assert len([1 for m, _ in fake.calls if m == 0x0102]) == 3


# ── 半截醫令:chars 失敗 → 不按 Enter ───────────────────────────────────────
def test_partial_chars_suppresses_enter():
    # [codex P1] chars_ok=False 後按 Enter = 把半截代碼提交進 HIS。原始碼守門:
    # Enter 必須被 chars_ok 條件擋住。
    src = inspect.getsource(main._script_code_input_adaptive)
    assert "_send_enter_to_window(focused) if chars_ok else False" in src, \
        "字元不完整時必須跳過 Enter(避免送出半截醫令)"
