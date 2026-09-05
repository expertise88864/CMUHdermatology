"""Deterministic shutdown interleavings; no network or clinical data."""
import subprocess
import threading

import pytest

from cmuh_common.roster.gitsync_storage import GitSyncStorage


class ObservedRLock:
    def __init__(self, entered):
        self.lock = threading.RLock()
        self.entered = entered
        self.owner = threading.current_thread()

    def __enter__(self):
        if threading.current_thread() is not self.owner:
            self.entered.set()
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


@pytest.mark.parametrize("resume", [False, True])
def test_started_timer_cannot_sync_after_handover(tmp_path, monkeypatch, resume):
    storage = GitSyncStorage(str(tmp_path), remote_sync=False, pull_interval_sec=0,
                             push_debounce_sec=0)
    storage._git_ok = storage._remote_sync = True
    calls = []
    monkeypatch.setattr(storage, "_commit", lambda label: True)
    monkeypatch.setattr(storage, "_gitignore_ready", lambda: True)
    monkeypatch.setattr(storage, "_remote_name", lambda: calls.append("remote"))
    entered = threading.Event()
    storage._git_lock = ObservedRLock(entered)
    timer = None
    try:
        with storage._git_lock:
            storage._schedule_push()
            timer = storage._push_timer
            assert entered.wait(5), "timer must have started and be waiting for git lock"
            storage.quiesce_local()
            if resume:
                storage._debounce = 600
                storage.resume_sync()
        timer.join(5)
        assert not timer.is_alive()
        assert calls == [], "old timer performed Git work after quiesce returned"
    finally:
        storage.quiesce_local()
        if timer is not None:
            timer.join(5)


def test_save_cannot_rearm_background_push_while_quiesced(tmp_path, monkeypatch):
    storage = GitSyncStorage(str(tmp_path), remote_sync=False, pull_interval_sec=0,
                             push_debounce_sec=600)
    storage._git_ok = storage._remote_sync = True
    monkeypatch.setattr(storage, "_commit", lambda label: True)
    try:
        storage.quiesce_local()
        storage._schedule_push()
        assert storage._push_timer is None
    finally:
        storage.quiesce_local()


@pytest.mark.parametrize("resume", [False, True])
def test_queued_periodic_pull_cannot_sync_after_handover(tmp_path, monkeypatch, resume):
    storage = GitSyncStorage(str(tmp_path), remote_sync=False, pull_interval_sec=0,
                             push_debounce_sec=600)
    storage._git_ok = storage._remote_sync = True
    calls = []
    monkeypatch.setattr(storage, "_commit", lambda label: True)
    monkeypatch.setattr(storage, "_remote_name", lambda: calls.append("remote"))
    entered = threading.Event()
    storage._git_lock = ObservedRLock(entered)
    worker = threading.Thread(target=storage._periodic_pull,
                              kwargs={"_epoch": storage._sync_epoch}, daemon=True)
    try:
        with storage._git_lock:
            worker.start()
            assert entered.wait(5)
            storage.quiesce_local()
            if resume:
                storage.resume_sync()
        worker.join(5)
        assert not worker.is_alive()
        assert calls == []
    finally:
        storage.quiesce_local()
        worker.join(5)


def test_resume_starts_a_working_new_timer(tmp_path, monkeypatch):
    storage = GitSyncStorage(str(tmp_path), remote_sync=False, pull_interval_sec=0,
                             push_debounce_sec=0)
    storage._git_ok = storage._remote_sync = True
    reached = threading.Event()
    monkeypatch.setattr(storage, "_commit", lambda label: True)
    monkeypatch.setattr(storage, "_gitignore_ready", lambda: True)
    monkeypatch.setattr(storage, "_remote_name", lambda: reached.set())
    try:
        old_epoch = storage._sync_epoch
        storage.quiesce_local()
        storage.resume_sync()
        timer = storage._push_timer
        assert reached.wait(5), "new generation must still synchronize"
        timer.join(5)
        assert not timer.is_alive()
        assert storage._sync_epoch != old_epoch
    finally:
        storage.quiesce_local()


def test_old_push_does_not_notify_a_resumed_generation(tmp_path, monkeypatch):
    notified = []
    storage = GitSyncStorage(str(tmp_path), remote_sync=False, pull_interval_sec=0,
                             push_debounce_sec=600,
                             on_remote_change=lambda: notified.append(1))
    storage._git_ok = storage._remote_sync = True
    monkeypatch.setattr(storage, "_commit", lambda label: True)

    def finish_old_sync(*, _epoch):
        storage.quiesce_local()
        storage.resume_sync()
        return True

    try:
        monkeypatch.setattr(storage, "_push_locked_body", finish_old_sync)
        storage._push(_epoch=storage._sync_epoch)
        assert notified == []
        monkeypatch.setattr(storage, "_push_locked_body", lambda **kw: True)
        storage._push(_epoch=storage._sync_epoch)
        assert notified == [1], "active-generation notification must be preserved"
    finally:
        storage.quiesce_local()


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("abort_result", ["success", "failure", "timeout"])
def test_rebase_failure_aborts_before_releasing_tree_lock(
        tmp_path, monkeypatch, timeout, abort_result):
    storage = GitSyncStorage(str(tmp_path), remote_sync=False, pull_interval_sec=0)
    monkeypatch.setattr(storage, "_commit", lambda label: True)
    monkeypatch.setattr(storage, "_remote_name", lambda: "origin")
    monkeypatch.setattr(storage, "_current_branch", lambda: "main")
    monkeypatch.setattr(storage, "_rev_parse", lambda ref: "old")
    calls = []
    depth = []

    class TreeLock:
        def __enter__(self):
            depth.append(1)

        def __exit__(self, *exc):
            depth.pop()
            calls.append(("release",))

    storage._tree_lock = TreeLock()

    def on_state(state, detail):
        calls.append(("notify", bool(depth)))

    storage._on_sync_state = on_state

    def git(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("pull", "--rebase"):
            if timeout:
                raise subprocess.TimeoutExpired(args, 30)
            return subprocess.CompletedProcess(args, 1, "", "conflict")
        if args == ("rebase", "--abort"):
            assert depth, "abort must exclude local saves"
            if abort_result == "timeout":
                raise subprocess.TimeoutExpired(args, 30)
            if abort_result == "failure":
                return subprocess.CompletedProcess(args, 1, "", "abort failed")
        return subprocess.CompletedProcess(args, int(args[0] == "merge"), "head", "")

    monkeypatch.setattr(storage, "_git", git)
    assert storage._push_locked_body() is False
    assert ("rebase", "--abort") in calls
    assert calls.index(("rebase", "--abort")) < calls.index(("release",))
    assert not any(args[0] == "push" for args in calls)
    assert ("notify", False) in calls
    assert ("notify", True) not in calls
    assert storage.sync_state == ("diverged" if abort_result == "success" else "error")
