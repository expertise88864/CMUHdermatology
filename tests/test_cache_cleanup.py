# -*- coding: utf-8 -*-
import os
import sys
import threading
from concurrent.futures import Future

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import cache_cleanup  # noqa: E402


def _reset_cleanup_state():
    with cache_cleanup._cleanup_state_lock:
        cache_cleanup._cleanup_scheduled = False
        cache_cleanup._cleanup_running = False


def test_schedule_cleanup_falls_back_when_executor_returns_failed_future(monkeypatch):
    _reset_cleanup_state()
    ran = threading.Event()

    def fake_cleanup_old_files():
        ran.set()
        return {}

    class RejectingExecutor:
        def submit(self, _fn):
            future = Future()
            future.set_exception(RuntimeError("executor saturated"))
            return future

    monkeypatch.setattr(cache_cleanup, "cleanup_old_files", fake_cleanup_old_files)

    cache_cleanup.schedule_cleanup_in_background(RejectingExecutor(), delay_seconds=0)

    assert ran.wait(timeout=1)


def test_schedule_cleanup_skips_duplicate_timer(monkeypatch):
    _reset_cleanup_state()
    timers = []

    class FakeTimer:
        def __init__(self, delay_seconds, callback):
            timers.append((delay_seconds, callback))
            self.daemon = False

        def start(self):
            return None

    monkeypatch.setattr(cache_cleanup.threading, "Timer", FakeTimer)

    assert cache_cleanup.schedule_cleanup_in_background(object(), delay_seconds=30) is True
    assert cache_cleanup.schedule_cleanup_in_background(object(), delay_seconds=30) is False
    assert len(timers) == 1
    _reset_cleanup_state()


def test_schedule_cleanup_falls_back_when_executor_future_fails_later(monkeypatch):
    _reset_cleanup_state()
    ran = threading.Event()
    pending = Future()

    def fake_cleanup_old_files():
        ran.set()
        return {}

    class AsyncRejectingExecutor:
        def submit(self, _fn):
            return pending

    monkeypatch.setattr(cache_cleanup, "cleanup_old_files", fake_cleanup_old_files)

    cache_cleanup.schedule_cleanup_in_background(
        AsyncRejectingExecutor(), delay_seconds=0,
    )
    pending.set_exception(RuntimeError("executor shut down"))

    assert ran.wait(timeout=1)
