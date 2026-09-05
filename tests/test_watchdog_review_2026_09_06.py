"""Watchdog identity and malformed-history regressions; no real process actions."""
import contextlib
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cmuh_common import pidfile as pf, watchdog_core as wc  # noqa: E402


@pytest.mark.parametrize("argv", [
    ["python.exe", "repair.py", "sample.pyw"],
    ["python.exe", "repair.py", "--file", "sample.pyw"],
    ["python.exe", "-c", "pass", "sample.pyw"],
    ["python.exe", "-m", "repair", "sample.pyw"],
    ["python.exe", "-", "sample.pyw"],
    ["python.exe", "-W", "sample.pyw", "repair.py"],
    ["python.exe", "-X", "sample.pyw", "repair.py"],
    ["python.exe", "--check-hash-based-pycs", "sample.pyw", "repair.py"],
    ["python.exe", "--unknown", "sample.pyw"],
    ["python.exe", "sample.backup"],
    ["python.exe", "--", "repair.py", "sample.pyw"],
    ["python.exe", "-Wsample.pyw", "repair.py"],
    ["python.exe", "-Xsample.pyw", "repair.py"],
    ["python.exe", "-cpass", "sample.pyw"],
    ["python.exe", "-mrepair", "sample.pyw"],
    ["python.exe", "-h", "sample.pyw"],
    ["python.exe", "-V", "sample.pyw"],
])
def test_data_argument_cannot_authorize_kill(monkeypatch, argv):
    killed = []

    @contextlib.contextmanager
    def pin(pid, predicate):
        yield pid if predicate(pid) else None

    monkeypatch.setattr(pf, "pinned_matching_pid", pin)
    monkeypatch.setattr(pf, "pid_looks_like_python", lambda pid: True)
    monkeypatch.setattr(wc, "_cmdline_tokens_of_pid_now", lambda pid: argv)
    monkeypatch.setattr(wc, "_PID_FROM_PIDFILE", {})
    monkeypatch.setattr(wc, "kill_pid", lambda pid: killed.append(pid) or True)
    assert wc.kill_pids_verified([1234], "sample", "sample") == []
    assert killed == []


@pytest.mark.parametrize("argv", [
    ["pythonw.exe", "C:/My Apps/sample.pyw", "--background"],
    ["python.exe", "-u", "sample.py"],
    ["python.exe", "-IB", "sample.pyw"],
    ["python.exe", "-W", "ignore", "sample.pyw"],
    ["python.exe", "-Wignore", "sample.pyw"],
    ["python.exe", "-X", "utf8", "sample.pyw"],
    ["python.exe", "-Xutf8", "sample.pyw"],
    ["python.exe", "-bWignore", "sample.pyw"],
    ["python.exe", "--check-hash-based-pycs", "always", "sample.pyw"],
    ["python.exe", "--", "sample.pyw"],
    ["sample.PYW"],
    ["pythonw.exe", "sample"],
])
def test_actual_script_is_still_recognized(argv):
    assert wc._tokens_are_target(argv, "sample") is True


@pytest.fixture
def history_seams(monkeypatch):
    now = time.time()
    monkeypatch.setattr(wc, "_RESTART_HISTORY", {"sample": [now]})
    monkeypatch.setattr(wc, "_SUSPENDED_UNTIL", {"paused": now + 600})
    monkeypatch.setattr(wc, "_HISTORY_GENERATION", [9])
    monkeypatch.setattr(wc, "_restart_history_lock", contextlib.nullcontext)
    monkeypatch.setattr(wc, "_save_restart_history", lambda: None)
    monkeypatch.setattr(wc, "_unsaved_since_clear", lambda name: None)
    return now


@pytest.mark.parametrize("data", [[1], "invalid", 42, True, False, [], None])
def test_wrong_root_preserves_history_and_does_not_abort_tick(
        monkeypatch, history_seams, data, caplog):
    monkeypatch.setattr(wc, "safe_load_json", lambda *args: data)
    assert wc._authorize_restart("paused") == wc.RESTART_AUTH_CRASH_LOOP
    assert wc._RESTART_HISTORY == {"sample": [history_seams]}
    assert wc._SUSPENDED_UNTIL == {"paused": history_seams + 600}
    assert wc._HISTORY_GENERATION == [9]
    assert any("啟動歷史格式" in record.getMessage() for record in caplog.records)
    assert wc._authorize_restart("sample") == wc.RESTART_AUTH_OK
    assert len(wc._RESTART_HISTORY["sample"]) == 2


def test_valid_history_still_replaces_cached_state(monkeypatch, history_seams):
    monkeypatch.setattr(wc, "safe_load_json", lambda *args: {
        "generation": 12, "history": {"other": [history_seams]},
        "suspended_until": {},
    })
    wc._load_restart_history()
    assert wc._RESTART_HISTORY == {"other": [history_seams]}
    assert wc._SUSPENDED_UNTIL == {}
    assert wc._HISTORY_GENERATION == [12]


@pytest.mark.parametrize("invalid", [10 ** 400, float("inf"), float("-inf"),
                                    float("nan"), True, False, "123"])
def test_invalid_timestamps_cannot_break_or_permanently_suspend_recovery(
        monkeypatch, history_seams, invalid):
    monkeypatch.setattr(wc, "safe_load_json", lambda *args: {
        "generation": 12,
        "history": {"sample": [history_seams, invalid]},
        "suspended_until": {"sample": invalid, "paused": history_seams + 600},
    })
    wc._load_restart_history()
    assert wc._RESTART_HISTORY == {"sample": [history_seams]}
    assert wc._SUSPENDED_UNTIL == {"paused": history_seams + 600}
    assert wc._authorize_restart("sample") == wc.RESTART_AUTH_OK


@pytest.mark.parametrize("invalid_root", [[1], False])
def test_repaired_history_accumulates_across_fresh_watchdogs(
        tmp_path, monkeypatch, invalid_root):
    path = tmp_path / "history.json"
    path.write_text(json.dumps(invalid_root), encoding="utf-8")
    monkeypatch.setattr(wc, "_restart_history_path", lambda: str(path))
    monkeypatch.setattr(wc, "_unsaved_since_clear", lambda name: None)
    monkeypatch.setattr(wc, "suspend_auto_updates", lambda *args, **kwargs: "")
    for _ in range(wc.CRASH_LOOP_MAX_RESTARTS):
        # --once starts with empty memory; the production load/lock/save must
        # carry the count from the preceding invocation, not just a local dict.
        monkeypatch.setattr(wc, "_RESTART_HISTORY", {})
        monkeypatch.setattr(wc, "_SUSPENDED_UNTIL", {})
        monkeypatch.setattr(wc, "_HISTORY_GENERATION", [0])
        assert wc._authorize_restart("sample") == wc.RESTART_AUTH_OK
    monkeypatch.setattr(wc, "_RESTART_HISTORY", {})
    assert wc._authorize_restart("sample") == wc.RESTART_AUTH_CRASH_LOOP
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["suspended_until"]["sample"] > time.time()
    assert saved["generation"] == wc.CRASH_LOOP_MAX_RESTARTS + 1
    assert not path.with_suffix(".json.lock").exists()
