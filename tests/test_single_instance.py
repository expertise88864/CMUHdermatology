# -*- coding: utf-8 -*-
"""single_instance helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import single_instance as si  # noqa: E402


class _FakeCreateMutex:
    argtypes = None
    restype = None

    def __init__(self, kernel):
        self.kernel = kernel

    def __call__(self, *_args):
        return self.kernel.handle


class _FakeKernel32:
    def __init__(self, handle):
        self.handle = handle
        self.closed = []
        self.CreateMutexW = _FakeCreateMutex(self)

    def CloseHandle(self, handle):
        self.closed.append(handle)


def _patch_mutex(monkeypatch, *, handle, last_error):
    fake = _FakeKernel32(handle)
    monkeypatch.setattr(si, "_kernel32", lambda: fake)
    monkeypatch.setattr(si, "_set_last_error", lambda value: None)
    monkeypatch.setattr(si, "_last_error", lambda: last_error)
    si._instance_mutex_handles.clear()
    return fake


def test_ensure_single_instance_rejects_existing_mutex(monkeypatch):
    fake = _patch_mutex(monkeypatch, handle=100, last_error=183)

    # retry_sec=0：不重試，立即判定（測即時拒絕邏輯，避免單元測試空等）
    assert si.ensure_single_instance("Local\\TestMutex", retry_sec=0) is False
    assert fake.closed == [100]
    assert si._instance_mutex_handles == {}


def test_ensure_single_instance_retries_until_mutex_released(monkeypatch):
    """重啟競態：前兩次 ALREADY_EXISTS（舊 instance 還沒釋放），之後成功取得。"""
    fake = _FakeKernel32(300)
    seq = iter([183, 183, 0])

    def next_err():
        try:
            return next(seq)
        except StopIteration:
            return 0

    monkeypatch.setattr(si, "_kernel32", lambda: fake)
    monkeypatch.setattr(si, "_set_last_error", lambda value: None)
    monkeypatch.setattr(si, "_last_error", next_err)
    monkeypatch.setattr(si.time, "sleep", lambda _s: None)  # 不真的睡，加速測試
    si._instance_mutex_handles.clear()

    assert si.ensure_single_instance("Local\\TestMutex", retry_sec=5) is True
    # 前兩次 ALREADY_EXISTS 各關一次 handle，第三次成功保留
    assert fake.closed == [300, 300]
    assert si._instance_mutex_handles == {"Local\\TestMutex": 300}

    si._instance_mutex_handles.clear()


def test_ensure_single_instance_access_denied_does_not_retry(monkeypatch):
    """ACCESS_DENIED 不重試：sleep 不該被呼叫，立即回 False。"""
    _patch_mutex(monkeypatch, handle=0, last_error=5)
    slept = []
    monkeypatch.setattr(si.time, "sleep", lambda s: slept.append(s))

    assert si.ensure_single_instance("Local\\TestMutex", retry_sec=5) is False
    assert slept == []


def test_ensure_single_instance_rejects_access_denied_mutex(monkeypatch):
    fake = _patch_mutex(monkeypatch, handle=0, last_error=5)

    assert si.ensure_single_instance("Local\\TestMutex") is False
    assert fake.closed == []
    assert si._instance_mutex_handles == {}


def test_ensure_single_instance_keeps_new_mutex_until_release(monkeypatch):
    fake = _patch_mutex(monkeypatch, handle=200, last_error=0)

    assert si.ensure_single_instance("Local\\TestMutex") is True
    assert si.ensure_single_instance("Local\\TestMutex") is True
    assert si._instance_mutex_handles == {"Local\\TestMutex": 200}

    si.release_single_instance()

    assert fake.closed == [200]
    assert si._instance_mutex_handles == {}


def test_is_instance_running_treats_access_denied_as_running(monkeypatch):
    fake = _patch_mutex(monkeypatch, handle=0, last_error=5)

    assert si.is_instance_running("Local\\TestMutex") is True
    assert fake.closed == []


def test_mutex_handle_state_is_guarded_for_ensure_and_release(monkeypatch):
    fake = _patch_mutex(monkeypatch, handle=200, last_error=0)
    enters = []

    class RecordingLock:
        def __enter__(self):
            enters.append("enter")

        def __exit__(self, *_args):
            enters.append("exit")

    monkeypatch.setattr(si, "_instance_mutex_lock", RecordingLock())

    assert si.ensure_single_instance("Local\\TestMutex") is True
    si.release_single_instance()

    assert fake.closed == [200]
    assert enters == ["enter", "exit", "enter", "exit"]
