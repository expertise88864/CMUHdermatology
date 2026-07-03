# -*- coding: utf-8 -*-
"""W1(2026-07-03):熱鍵硬逾時看門狗的決策 —— worker thread 還活著就絕不解鎖
(否則第二支熱鍵會與卡住的第一支並行寫同一 HIS 病歷/醫令 → billing 錯亂)。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main as m  # noqa: E402


def test_stuck_but_alive_never_unlocks():
    """卡住(未在等醫師)但 thread 仍活著 → keep_stuck(維持鎖定,不解鎖)。"""
    assert m._hotkey_watchdog_action(
        still_ours=True, alive=True, awaiting=False) == "keep_stuck"


def test_awaiting_user_keeps_locked():
    """正在等醫師回應對話框 → keep_awaiting(維持鎖定、再等一週期)。"""
    assert m._hotkey_watchdog_action(
        still_ours=True, alive=True, awaiting=True) == "keep_awaiting"


def test_only_clears_when_thread_dead():
    """旗標殘留但 thread 已死(finally 沒清到)→ clear_dead(才可代清)。"""
    assert m._hotkey_watchdog_action(
        still_ours=True, alive=False, awaiting=False) == "clear_dead"


def test_gone_when_not_ours():
    """流程已正常結束或被後續熱鍵取代 → gone(看門狗退出)。"""
    assert m._hotkey_watchdog_action(False, True, False) == "gone"
    assert m._hotkey_watchdog_action(False, False, False) == "gone"


def test_unlock_requires_thread_dead_invariant():
    """核心不變量:唯一會導致清旗標的決策(clear_dead)必然 alive=False。
    任何 alive=True 的情況都不得回 clear_dead(=不得解鎖)。"""
    for still_ours in (True, False):
        for awaiting in (True, False):
            assert m._hotkey_watchdog_action(still_ours, True, awaiting) != "clear_dead"
