# -*- coding: utf-8 -*-
"""watchdog_runner entry-point behavior."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import watchdog_runner  # noqa: E402


def test_once_mode_bypasses_daemon_mutex(monkeypatch):
    calls = []

    monkeypatch.setattr(sys, "argv", ["watchdog_runner.py", "--once"])
    monkeypatch.setattr(watchdog_runner, "_run_once_via_core",
                        lambda: calls.append("once") or 0)
    monkeypatch.setattr(watchdog_runner, "ensure_single_instance",
                        lambda name: calls.append(("ensure", name)) or True)

    assert watchdog_runner.main() == 0
    assert calls == ["once"]


def test_daemon_duplicate_exits_without_loop(monkeypatch):
    calls = []

    monkeypatch.setattr(sys, "argv", ["watchdog_runner.py"])
    monkeypatch.setattr(watchdog_runner, "_setup_logging",
                        lambda: calls.append("logging"))
    monkeypatch.setattr(watchdog_runner, "ensure_single_instance",
                        lambda name: calls.append(("ensure", name)) or False)

    assert watchdog_runner.main() == 0
    assert calls == [
        "logging",
        ("ensure", watchdog_runner.WATCHDOG_DAEMON_MUTEX_NAME),
    ]


def test_daemon_releases_mutex_when_loop_exits(monkeypatch):
    calls = []
    fake_core = types.SimpleNamespace(
        load_config=lambda: {},
        run_one_tick=lambda mode: calls.append(("tick", mode)) or [],
        get_loop_timing=lambda cfg: (300, 1),
    )

    def fake_import(name, *args, **kwargs):
        if name == "cmuh_common":
            return types.SimpleNamespace(watchdog_core=fake_core)
        return original_import(name, *args, **kwargs)

    def stop_loop(_interval):
        raise KeyboardInterrupt

    original_import = __import__
    monkeypatch.setattr(sys, "argv", ["watchdog_runner.py"])
    monkeypatch.setattr(watchdog_runner, "_setup_logging",
                        lambda: calls.append("logging"))
    monkeypatch.setattr(watchdog_runner, "ensure_single_instance",
                        lambda name: calls.append(("ensure", name)) or True)
    monkeypatch.setattr(watchdog_runner, "release_single_instance",
                        lambda: calls.append("release"))
    monkeypatch.setattr(watchdog_runner.time, "sleep", stop_loop)
    monkeypatch.setattr("builtins.__import__", fake_import)

    try:
        watchdog_runner.main()
    except KeyboardInterrupt:
        pass

    assert calls == [
        "logging",
        ("ensure", watchdog_runner.WATCHDOG_DAEMON_MUTEX_NAME),
        ("tick", "outer"),
        "release",
    ]


def test_daemon_heartbeat_uses_monotonic_clock():
    import inspect

    src = inspect.getsource(watchdog_runner.main)

    assert "time.monotonic()" in src
    assert "time.time() - last_heartbeat" not in src
