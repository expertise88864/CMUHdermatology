# -*- coding: utf-8 -*-
import os
import sys
import inspect
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import health  # noqa: E402


def test_normalize_health_monitor_args_falls_back_for_invalid_values():
    assert health._normalize_health_monitor_args(
        "bad", None, object(), "bad"
    ) == (400.0, 800.0, 300, 6)


def test_normalize_health_monitor_args_enforces_safe_lower_bounds():
    assert health._normalize_health_monitor_args(
        500, 100, 0, -2
    ) == (500.0, 500.0, 5, 1)


def test_self_process_is_cached_for_rss_and_stats(monkeypatch):
    class FakeMem:
        rss = 12 * 1024 * 1024

    class FakeProcess:
        created = 0

        def __init__(self):
            FakeProcess.created += 1
            self.cpu_calls = 0

        def cpu_percent(self, interval=None):
            self.cpu_calls += 1
            return 7.5

        def memory_info(self):
            return FakeMem()

        def num_threads(self):
            return 4

        def oneshot(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(health, "_self_process", None)
    monkeypatch.setitem(sys.modules, "psutil",
                        SimpleNamespace(Process=FakeProcess))

    assert health._get_rss_mb() == 12
    assert health._get_self_stats() == {
        "rss_mb": 12,
        "cpu_pct": 7.5,
        "threads": 4,
    }
    assert FakeProcess.created == 1
    assert health._self_process.cpu_calls == 2


def test_self_process_cache_recovers_after_read_failure(monkeypatch):
    class FakeMem:
        rss = 21 * 1024 * 1024

    class FakeProcess:
        created = 0

        def __init__(self):
            FakeProcess.created += 1
            self.sequence = FakeProcess.created

        def cpu_percent(self, interval=None):
            return 0.0

        def memory_info(self):
            if self.sequence == 1:
                raise RuntimeError("stale psutil process handle")
            return FakeMem()

    monkeypatch.setattr(health, "_self_process", None)
    monkeypatch.setitem(sys.modules, "psutil",
                        SimpleNamespace(Process=FakeProcess))

    assert health._get_rss_mb() is None
    assert health._self_process is None
    assert health._get_rss_mb() == 21
    assert FakeProcess.created == 2


def test_health_loop_continues_checks_when_rss_unavailable():
    src = inspect.getsource(health._health_loop)

    assert "continuing network/disk checks" in src
    assert "rss_mb = 0.0" not in src
    assert "time.sleep(interval_sec * 6)" not in src


def test_health_stats_interval_uses_monotonic_clock():
    src = inspect.getsource(health._health_loop)

    assert "now_stats = time.monotonic()" in src
    assert "now_stats = time.time()" not in src
