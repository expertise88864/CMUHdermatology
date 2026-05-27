# -*- coding: utf-8 -*-
import os
import sys
import threading
from concurrent.futures import Future

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import cache_cleanup  # noqa: E402


def test_schedule_cleanup_falls_back_when_executor_returns_failed_future(monkeypatch):
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
