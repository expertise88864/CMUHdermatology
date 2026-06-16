# -*- coding: utf-8 -*-
"""autoclock clock_lock(RLock)回歸防護。

v43 對 RLock 呼叫了不存在的 .locked() → 每次排程觸發 process_clock_task 就 crash;
因為沒有 end-to-end 測試跑排程迴圈,這個災難性 bug 直到實機才被發現。v45 移除該呼叫
修好,但沒有測試防止再被引入。本檔以原始碼層級固定:clock_lock 仍是 RLock,且全檔
不得對任何 *_lock 呼叫 .locked()(RLock 沒有此方法)。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTOCLOCK_SRC = ROOT / "src" / "autoclock.py"


def test_clock_lock_is_rlock():
    src = AUTOCLOCK_SRC.read_text(encoding="utf-8")
    assert "clock_lock = threading.RLock()" in src


def test_no_locked_method_call_on_locks():
    """RLock 沒有 .locked();禁止對 *_lock 呼叫(會像 v43 一樣 AttributeError 崩潰)。"""
    src = AUTOCLOCK_SRC.read_text(encoding="utf-8")
    assert "_lock.locked(" not in src, (
        "偵測到對某個 *_lock 呼叫 .locked() —— RLock 無此方法(v43 因此崩潰);"
        "請改用其他方式判斷鎖狀態")
