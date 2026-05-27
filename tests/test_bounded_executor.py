# -*- coding: utf-8 -*-
import pytest

import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.bounded_executor import (  # noqa: E402
    BoundedThreadPoolExecutor,
    RejectedExecutionError,
)


def test_bounded_executor_rejects_when_pending_budget_is_full():
    blocker = threading.Event()
    started = threading.Event()

    def wait_forever():
        started.set()
        blocker.wait(timeout=5)

    executor = BoundedThreadPoolExecutor(max_workers=1, max_pending=1)
    try:
        running = executor.submit(wait_forever)
        assert started.wait(timeout=1)

        rejected = executor.submit(lambda: "too many")

        with pytest.raises(RejectedExecutionError):
            rejected.result()
        assert not running.done()
    finally:
        blocker.set()
        executor.shutdown(wait=True)


def test_bounded_executor_releases_budget_after_task_finishes():
    executor = BoundedThreadPoolExecutor(max_workers=1, max_pending=1)
    try:
        assert executor.submit(lambda: "one").result(timeout=1) == "one"
        assert executor.submit(lambda: "two").result(timeout=1) == "two"
    finally:
        executor.shutdown(wait=True)


def test_bounded_executor_logs_background_task_exceptions(caplog):
    def fail_task():
        raise ValueError("boom")

    executor = BoundedThreadPoolExecutor(max_workers=1, max_pending=1)
    try:
        future = executor.submit(fail_task)

        with pytest.raises(ValueError):
            future.result(timeout=1)
    finally:
        executor.shutdown(wait=True)

    assert "background task failed:" in caplog.text
    assert "fail_task" in caplog.text
    assert "boom" in caplog.text
