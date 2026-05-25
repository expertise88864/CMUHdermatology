# -*- coding: utf-8 -*-
"""task_gate helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.task_gate import ActiveTaskGate  # noqa: E402


def test_active_task_gate_rejects_duplicate_until_release():
    gate = ActiveTaskGate()

    assert gate.acquire("job") is True
    assert gate.acquire("job") is False
    assert gate.is_active("job") is True

    gate.release("job")

    assert gate.is_active("job") is False
    assert gate.acquire("job") is True


def test_active_task_gate_tracks_keys_independently():
    gate = ActiveTaskGate()

    assert gate.acquire("am_in") is True
    assert gate.acquire("pm_out") is True
    assert gate.acquire("am_in") is False

    gate.release("am_in")

    assert gate.acquire("am_in") is True
    assert gate.is_active("pm_out") is True


def test_active_task_gate_releases_stale_key_on_acquire():
    now = [100.0]
    gate = ActiveTaskGate(stale_after_sec=10, clock=lambda: now[0])

    assert gate.acquire("job") is True
    assert gate.acquire("job") is False

    now[0] = 111.0

    assert gate.acquire("job") is True
    assert gate.is_active("job") is True


def test_active_task_gate_active_age_expires_stale_key():
    now = [100.0]
    gate = ActiveTaskGate(stale_after_sec=10, clock=lambda: now[0])

    assert gate.acquire("job") is True

    now[0] = 104.5
    assert gate.active_age_sec("job") == 4.5

    now[0] = 110.0
    assert gate.active_age_sec("job") is None
    assert gate.is_active("job") is False
