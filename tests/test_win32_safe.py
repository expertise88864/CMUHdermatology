# -*- coding: utf-8 -*-
"""W2(2026-07-03):Win32 安全逾時呼叫層。callback 阻塞(HIS 凍結)時 fail-open 回
default,不阻塞呼叫緒。"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import win32_safe  # noqa: E402


def test_returns_result_normally():
    assert win32_safe.call_with_timeout(lambda: 42, 1.0, default=0) == 42


def test_exception_returns_default():
    def boom():
        raise RuntimeError("win32 boom")
    assert win32_safe.call_with_timeout(boom, 1.0, default=-1) == -1


def test_timeout_returns_default_fast():
    """fn 卡住 → 在 timeout 內回 default,不等 fn 跑完(不阻塞呼叫緒)。"""
    def slow():
        time.sleep(5)
        return "SLOW_DONE"
    t0 = time.monotonic()
    r = win32_safe.call_with_timeout(slow, 0.1, default="TIMEOUT")
    elapsed = time.monotonic() - t0
    assert r == "TIMEOUT"
    assert elapsed < 2.0   # 沒有等滿 5 秒
