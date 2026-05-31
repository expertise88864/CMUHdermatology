# -*- coding: utf-8 -*-
"""Consult-query pending re-trigger worker coalescing tests."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query  # noqa: E402


def test_pending_retrigger_drain_uses_single_delayed_worker(monkeypatch):
    targets = []
    triggered = []

    class DeferredThread:
        def __init__(self, *, target, name=None, daemon=None):
            targets.append(target)

        def start(self):
            pass

    with consult_query._pending_retriggers_lock:
        consult_query._pending_retriggers.clear()
        consult_query._pending_retrigger_drain_running = False
    monkeypatch.setattr(consult_query.threading, "Thread", DeferredThread)
    monkeypatch.setattr(consult_query, "_sleep_while_running", lambda _sec: True)
    monkeypatch.setattr(
        consult_query, "trigger_job_async",
        lambda label, override_recipients=None:
            triggered.append((label, override_recipients)),
    )

    consult_query._enqueue_pending_retrigger("17:00", None)
    consult_query._drain_pending_retriggers()
    consult_query._enqueue_pending_retrigger("email", ["a@example.com"])
    consult_query._drain_pending_retriggers()

    assert len(targets) == 1
    targets[0]()
    assert triggered == [
        ("17:00", None),
        ("email", ["a@example.com"]),
    ]
    assert consult_query._pending_retrigger_drain_running is False
