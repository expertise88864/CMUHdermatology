"""Deterministic races for status-driver takeover; no real browser/network."""
import threading
import time

import pytest

import main

# [2026-10-21] 4-vCPU xdist runner 上 worker 執行緒偶爾 1 秒內排不到(CI run
# 39536945386 在第一個 entered 事件逾時,同 SHA 的另一個 run 全綠)。等待改成:
#   SOON — 等「必定會發生」的事件/結束;事件一到就返回,拉長不放寬任何判定。
#   HOLD — 樁卡住多久才自己放手。★必須遠大於 SOON★:否則「接管被舊 init lock
#          擋住」的退化會因為樁先放手而被誤判通過。
SOON = 10.0
HOLD = 30.0
assert HOLD > 2 * SOON


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
            assert release.wait(HOLD)
            raise RuntimeError("old session failed after takeover")

    old, replacement = OldDriver(), Driver()
    pool.update(driver=old, last_used=time.time())
    result = []
    worker = threading.Thread(
        target=lambda: result.append(main._get_or_create_status_driver()), daemon=True)
    worker.start()
    try:
        assert entered.wait(SOON)
        main._discard_status_driver()
        with pool["lock"]:
            pool.update(driver=replacement, last_used=time.time())
        release.set()
        worker.join(SOON)
        assert result == [None], "expired caller must never borrow the new session"
        assert pool["driver"] is replacement
        assert not replacement.closed.is_set()
    finally:
        release.set()
        worker.join(HOLD)


def test_takeover_initializes_without_waiting_for_hung_initializer(pool, monkeypatch):
    entered, release, replacement_ready = (threading.Event() for _ in range(3))
    old, replacement = Driver(), Driver()

    def initialize():
        if threading.current_thread().name == "OldInitializer":
            entered.set()
            assert release.wait(HOLD)
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
        assert entered.wait(SOON)
        main._discard_status_driver()
        new_worker.start()
        assert replacement_ready.wait(SOON), "takeover is blocked by the expired init lock"
        new_worker.join(SOON)
        assert new_result == [replacement]
        release.set()
        old_worker.join(SOON)
        assert old_result == [None]
        assert old.closed.wait(SOON)
        assert pool["driver"] is replacement
    finally:
        release.set()
        old_worker.join(HOLD)
        if new_worker.ident is not None:
            new_worker.join(HOLD)


def test_dead_current_driver_can_still_be_rebuilt(pool, monkeypatch):
    class DeadDriver(Driver):
        @property
        def window_handles(self):
            raise RuntimeError("dead current session")

    old, replacement = DeadDriver(), Driver()
    pool.update(driver=old, last_used=time.time())
    monkeypatch.setattr(main, "_initialize_status_driver", lambda: replacement)
    assert main._get_or_create_status_driver() is replacement
    assert old.closed.wait(SOON)


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
        assert entered.wait(SOON)
        main._discard_status_driver()
        with pool["lock"]:
            pool.update(driver=replacement, last_used=time.time())
    finally:
        gate.release()
        worker.join(SOON)
    assert result == [None]
    assert pool["driver"] is replacement


def test_idle_driver_can_still_be_rebuilt(pool, monkeypatch):
    old, replacement = Driver(), Driver()
    pool.update(driver=old, last_used=time.time() - main._STATUS_DRIVER_IDLE_TIMEOUT - 1)
    monkeypatch.setattr(main, "_initialize_status_driver", lambda: replacement)
    assert main._get_or_create_status_driver() is replacement
    assert old.closed.wait(SOON)
