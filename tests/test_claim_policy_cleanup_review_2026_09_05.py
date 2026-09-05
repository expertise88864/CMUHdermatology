"""Regression cases for exception boundaries and update artifact ownership."""
import builtins
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import bootstrap_recovery as recovery  # noqa: E402
from cmuh_common import cache_cleanup, cross_process_claim as claim, update_policy  # noqa: E402


@pytest.mark.parametrize("busy", [False, True])
def test_file_lock_preserves_body_exception(tmp_path, monkeypatch, busy):
    if busy:
        def lock_busy(_fd):
            raise OSError("busy")
        monkeypatch.setattr(claim, "_lock_fd", lock_busy)
    error = ValueError("original caller failure")
    with pytest.raises(ValueError) as caught:
        with claim._os_file_lock(str(tmp_path / "claim.lock"), deadline_sec=0) as acquired:
            assert acquired is not busy
            raise error
    assert caught.value is error
    if not busy:
        with claim._os_file_lock(str(tmp_path / "claim.lock"), deadline_sec=0) as acquired:
            assert acquired


def test_claim_timeout_preserves_body_exception_and_releases_local_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(claim, "_claims_dir", lambda: str(tmp_path))

    @contextmanager
    def no_lock(_path):
        yield False

    monkeypatch.setattr(claim, "_os_file_lock", no_lock)
    error = ValueError("skip callback failed")
    with pytest.raises(ValueError) as caught:
        with claim.exclusive_claim("review-timeout") as acquired:
            assert not acquired
            raise error
    assert caught.value is error
    local = claim._local_lock("review-timeout")
    assert local.acquire(blocking=False)
    local.release()


def test_failed_claim_write_remains_fail_open_without_generator_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(claim, "_claims_dir", lambda: str(tmp_path))

    def cannot_write(*_args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(claim, "_write_record", cannot_write)
    error = ValueError("caller failed after fallback")
    with pytest.raises(ValueError) as caught:
        with claim.exclusive_claim("review-write-failure") as acquired:
            assert acquired
            raise error
    assert caught.value is error
    assert "generator didn't stop" not in caplog.text


def test_corrupt_flag_reader_does_not_delete_concurrent_replacement(tmp_path, monkeypatch):
    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    flag = tmp_path / update_policy.AUTO_UPDATE_SUSPEND_FILENAME
    flag.write_text("bad timestamp\n", encoding="utf-8")
    original_open = builtins.open

    @contextmanager
    def replace_after_read(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as fh:
            yield fh
        update_policy.suspend_auto_updates("new suspension", now=1000, duration_sec=600)

    monkeypatch.setattr(update_policy, "open", replace_after_read, raising=False)
    assert update_policy.get_auto_update_suspend_until(now=1000) == 0.0
    monkeypatch.delattr(update_policy, "open")
    assert flag.exists(), "old reader removed a newly published suspension"
    assert update_policy.get_auto_update_suspend_until(now=1000) == 1600


@pytest.fixture
def cleanup_tree(tmp_path, monkeypatch):
    app = tmp_path / "app"
    shared = app / "src" / "cmuh_common"
    shared.mkdir(parents=True)
    settings = app / "settings"
    settings.mkdir()
    monkeypatch.setattr(cache_cleanup, "get_app_dir", lambda: str(app))
    monkeypatch.setattr(cache_cleanup, "get_settings_dir", lambda: str(settings))
    old = cache_cleanup.time.time() - 40 * cache_cleanup.DAY
    bak = shared / "sample.py.bak"
    tmp = shared / "sample.py.bak.tmp"
    for path in (bak, tmp):
        path.write_text("required recovery bytes", encoding="utf-8")
        os.utime(path, (old, old))
    return app, bak, tmp


@pytest.mark.parametrize("marker", [recovery.JOURNAL_FILENAME,
                                  recovery.JOURNAL_FILENAME + recovery.FAILED_JOURNAL_SUFFIX])
def test_cleanup_preserves_backups_with_unresolved_journal(cleanup_tree, marker):
    app, bak, tmp = cleanup_tree
    (app / marker).write_text("pending", encoding="utf-8")
    cache_cleanup.cleanup_old_files()
    assert bak.exists(), "cleanup destroyed a backup still owned by recovery"
    assert tmp.exists()


def test_cleanup_preserves_artifacts_when_update_lock_is_unavailable(cleanup_tree, monkeypatch):
    _app, bak, tmp = cleanup_tree

    @contextmanager
    def locked(_app_dir, timeout_sec=10.0):
        yield False

    monkeypatch.setattr(recovery, "_write_lock", locked)
    cache_cleanup.cleanup_old_files()
    assert bak.exists()
    assert tmp.exists()


def test_cleanup_still_removes_old_unowned_artifacts(cleanup_tree):
    _app, bak, tmp = cleanup_tree
    stats = cache_cleanup.cleanup_old_files()
    assert not bak.exists()
    assert not tmp.exists()
    assert stats["bak_files"] == 1
    assert stats["tmp_files"] == 1


def test_cleanup_uses_same_lock_as_running_updater(cleanup_tree, monkeypatch):
    from cmuh_common import updater

    app, bak, tmp = cleanup_tree
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(app))
    # Backup copy2 retains the old source mtime; age cannot establish ownership.
    with updater._updater_write_lock(timeout_sec=1) as acquired:
        assert acquired
        cache_cleanup.cleanup_old_files()
        assert bak.exists()
        assert tmp.exists()
    # Once the writer is gone and no journal remains, normal pruning resumes.
    cache_cleanup.cleanup_old_files()
    assert not bak.exists()


def test_cleanup_preserves_artifacts_if_journal_stat_is_denied(cleanup_tree, monkeypatch):
    app, bak, tmp = cleanup_tree
    original_stat = Path.stat

    def denied_stat(path, *args, **kwargs):
        if path == app / recovery.JOURNAL_FILENAME:
            raise PermissionError("cannot determine recovery state")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    cache_cleanup.cleanup_old_files()
    assert bak.exists()
    assert tmp.exists()
