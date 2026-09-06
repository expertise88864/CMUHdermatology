"""Stale action-lock takeover must not delete a concurrent fresh claim."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cmuh_common import watchdog_core as wc  # noqa: E402


@pytest.mark.parametrize("through_tick", [False, True])
def test_stale_observer_cannot_replace_another_successful_claim(
        tmp_path, monkeypatch, through_tick):
    monkeypatch.setattr(wc, "LOCK_DIR", tmp_path)
    lock = wc._lock_path_for("sample")
    lock.write_bytes(b"424242 1")
    stale = time.time() - 1000
    os.utime(lock, (stale, stale))
    real_stat = Path.stat
    competing_results = []
    injected = False
    started = []
    if through_tick:
        script = tmp_path / "sample.pyw"
        script.write_text("", encoding="utf-8")
        prog = {"name": "sample", "pyw": str(script), "process_match": "sample"}
        monkeypatch.setattr(wc, "current_session_id", lambda: 1)
        monkeypatch.setattr(wc, "find_matching_pids", lambda *a, **k: [])
        monkeypatch.setattr(wc, "_wmic_find_pids", lambda *a, **k: [])
        monkeypatch.setattr(wc, "_authorize_restart", lambda *a: wc.RESTART_AUTH_OK)
        monkeypatch.setattr(wc, "start_program", lambda *a: started.append(1) or 123)

    def claim():
        if through_tick:
            return wc.ensure_program(prog, "pythonw", [], 1, "outer", {}).startswith("▶")
        return wc.claim_action_lock("sample", 90)

    def stat_then_other_claim(path, *args, **kwargs):
        nonlocal injected
        snapshot = real_stat(path, *args, **kwargs)
        if path == lock and not injected:
            injected = True
            # Interleave B after A observed stale mtime, before A unlinks.
            competing_results.append(claim())
        return snapshot

    monkeypatch.setattr(Path, "stat", stat_then_other_claim)
    first = claim()
    assert injected, "must reach the actual stale-observation seam"
    assert sorted([first, *competing_results]) == [False, True], (
        "both callers were authorized: stale A deleted the newly claimed B lock")
    assert lock.exists()
    if through_tick:
        assert started == [1], "only one real ensure_program path may request a launch"


def _child(tmp_path, code):
    preamble = (
        "import os, sys; from pathlib import Path; "
        "sys.path.insert(0, sys.argv[1]); "
        "from cmuh_common import watchdog_core as wc; "
        "wc.LOCK_DIR = Path(sys.argv[2]); "
    )
    return subprocess.run(
        [sys.executable, "-c", preamble + code,
         str(Path(wc.__file__).resolve().parents[1]), str(tmp_path)],
        capture_output=True, text=True, timeout=15, check=True).stdout.strip()


def test_real_other_process_cannot_claim_or_release_while_guard_held(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "LOCK_DIR", tmp_path)
    lock = wc._lock_path_for("sample")
    lock.write_bytes(f"{os.getpid()} 1".encode())
    stale = time.time() - 1000
    os.utime(lock, (stale, stale))
    with wc._action_claim_guard(lock) as held:
        assert held
        assert _child(tmp_path, "print(wc.claim_action_lock('sample', 90))") == "False"
        assert _child(tmp_path, "print(wc.release_action_lock('sample'))") == "False"
        assert _child(tmp_path, "print(wc.claim_action_lock('other', 90))") == "True"
        assert lock.read_bytes() == f"{os.getpid()} 1".encode()
    assert _child(tmp_path, "print(wc.claim_action_lock('sample', 90))") == "True"
    assert wc.claim_action_lock("sample", 90) is False


def test_guard_is_released_by_os_after_child_exits_without_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "LOCK_DIR", tmp_path)
    code = (
        "guard = wc._action_claim_guard(wc._lock_path_for('sample')); "
        "assert guard.__enter__(); "
        "print('held', flush=True); os._exit(0)"
    )
    assert _child(tmp_path, code) == "held"
    sidecar = Path(str(wc._lock_path_for("sample")) + ".guard")
    assert sidecar.exists(), "sidecar stays; ownership is an OS byte lock"
    assert wc.claim_action_lock("sample", 90) is True


def test_guard_errors_fail_closed_and_do_not_replace_stale_record(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "LOCK_DIR", tmp_path)
    lock = wc._lock_path_for("sample")
    lock.write_bytes(b"424242 1")
    real_open = os.open

    def denied(path, *args, **kwargs):
        if str(path).endswith(".guard"):
            raise PermissionError("guard denied")
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", denied)
        assert wc.claim_action_lock("sample", 0) is False
        assert wc.release_action_lock("sample") is False
    assert lock.read_bytes() == b"424242 1"
    assert wc.claim_action_lock("sample", 0) is True


def test_body_exception_propagates_once_and_releases_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "LOCK_DIR", tmp_path)
    lock = wc._lock_path_for("sample")
    with pytest.raises(ValueError, match="body failed"):
        with wc._action_claim_guard(lock) as held:
            assert held
            raise ValueError("body failed")
    assert _child(tmp_path, "print(wc.claim_action_lock('sample', 90))") == "True"


def test_nonblocking_os_lock_failure_closes_descriptor(tmp_path, monkeypatch):
    import msvcrt

    monkeypatch.setattr(wc, "LOCK_DIR", tmp_path)
    closed = []
    real_close = os.close

    def close(fd):
        closed.append(fd)
        real_close(fd)

    def busy(*args):
        raise OSError("byte already locked")

    with monkeypatch.context() as patch:
        patch.setattr(msvcrt, "locking", busy)
        patch.setattr(os, "close", close)
        assert wc.claim_action_lock("sample", 90) is False
    assert len(closed) == 1
    assert not wc._lock_path_for("sample").exists()
    assert wc.claim_action_lock("sample", 90) is True
