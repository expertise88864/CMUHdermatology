"""Deterministic races for status-driver takeover; no real browser/network."""
import threading
import time

import pytest

import main


class Driver:
    window_handles = ["tab"]

    def __init__(self):
        self.closed = threading.Event()

    def quit(self):
        self.closed.set()


@pytest.fixture
def pool(monkeypatch):
    value = {"driver": None, "last_used": 0.0, "epoch": 0,
             "lock": threading.Lock(), "init_lock": threading.Lock()}
    monkeypatch.setattr(main, "_status_driver_pool", value)
    return value


def test_late_health_failure_cannot_borrow_replacement(pool):
    entered, release = threading.Event(), threading.Event()

    class OldDriver(Driver):
        @property
        def window_handles(self):
            entered.set()
            assert release.wait(3)
            raise RuntimeError("old session failed after takeover")

    old, replacement = OldDriver(), Driver()
    pool.update(driver=old, last_used=time.time())
    result = []
    worker = threading.Thread(
        target=lambda: result.append(main._get_or_create_status_driver()), daemon=True)
    worker.start()
    try:
        assert entered.wait(1)
        main._discard_status_driver()
        with pool["lock"]:
            pool.update(driver=replacement, last_used=time.time())
        release.set()
        worker.join(2)
        assert result == [None], "expired caller must never borrow the new session"
        assert pool["driver"] is replacement
        assert not replacement.closed.is_set()
    finally:
        release.set()
        worker.join(3)


def test_takeover_initializes_without_waiting_for_hung_initializer(pool, monkeypatch):
    entered, release, replacement_ready = (threading.Event() for _ in range(3))
    old, replacement = Driver(), Driver()

    def initialize():
        if threading.current_thread().name == "OldInitializer":
            entered.set()
            assert release.wait(4)
            return old
        replacement_ready.set()
        return replacement

    monkeypatch.setattr(main, "_initialize_status_driver", initialize)
    old_result, new_result = [], []
    old_worker = threading.Thread(name="OldInitializer", daemon=True,
        target=lambda: old_result.append(main._get_or_create_status_driver()))
    new_worker = threading.Thread(daemon=True,
        target=lambda: new_result.append(main._get_or_create_status_driver()))
    old_worker.start()
    try:
        assert entered.wait(1)
        main._discard_status_driver()
        new_worker.start()
        assert replacement_ready.wait(1), "takeover is blocked by the expired init lock"
        new_worker.join(1)
        assert new_result == [replacement]
        release.set()
        old_worker.join(2)
        assert old_result == [None]
        assert old.closed.wait(1)
        assert pool["driver"] is replacement
    finally:
        release.set()
        old_worker.join(3)
        if new_worker.ident is not None:
            new_worker.join(3)


def test_dead_current_driver_can_still_be_rebuilt(pool, monkeypatch):
    class DeadDriver(Driver):
        @property
        def window_handles(self):
            raise RuntimeError("dead current session")

    old, replacement = DeadDriver(), Driver()
    pool.update(driver=old, last_used=time.time())
    monkeypatch.setattr(main, "_initialize_status_driver", lambda: replacement)
    assert main._get_or_create_status_driver() is replacement
    assert old.closed.wait(1)


def test_caller_queued_on_old_init_lock_cannot_borrow_replacement(pool):
    entered = threading.Event()
    gate = threading.Lock()

    class ObservedLock:
        def __enter__(self):
            entered.set()
            gate.acquire()

        def __exit__(self, *_exc):
            gate.release()

    pool["init_lock"] = ObservedLock()
    replacement = Driver()
    result = []
    gate.acquire()
    worker = threading.Thread(daemon=True,
        target=lambda: result.append(main._get_or_create_status_driver()))
    worker.start()
    try:
        assert entered.wait(1)
        main._discard_status_driver()
        with pool["lock"]:
            pool.update(driver=replacement, last_used=time.time())
    finally:
        gate.release()
        worker.join(2)
    assert result == [None]
    assert pool["driver"] is replacement


def test_idle_driver_can_still_be_rebuilt(pool, monkeypatch):
    old, replacement = Driver(), Driver()
    pool.update(driver=old, last_used=time.time() - main._STATUS_DRIVER_IDLE_TIMEOUT - 1)
    monkeypatch.setattr(main, "_initialize_status_driver", lambda: replacement)
    assert main._get_or_create_status_driver() is replacement
    assert old.closed.wait(1)
