# -*- coding: utf-8 -*-
"""Shared auto-update suspension policy tests."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import update_policy  # noqa: E402


def test_auto_update_checks_run_three_fixed_times_per_day():
    # 【2026-06-03】改為每天固定 3 次（07:00 / 13:00 / 18:00），少打 GitHub 避免限流。
    times = update_policy.AUTO_UPDATE_CHECK_TIMES

    assert times == ("07:00", "13:00", "18:00")
    assert len(times) == 3


def test_suspend_auto_updates_round_trips_active_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))

    path = update_policy.suspend_auto_updates(
        "test crash loop", duration_sec=120, now=1000)

    assert path == str(tmp_path / ".auto_update_suspended_until")
    assert update_policy.get_auto_update_suspend_until(now=1050) == 1120
    assert "reason: test crash loop" in Path(path).read_text(encoding="utf-8")


def test_stale_auto_update_suspend_flag_expires_but_is_kept(tmp_path, monkeypatch):
    # [IE-03 2026-07-10] 已過期的旗標【不再刪除】—— 避免「讀到過期→準備刪」與 watchdog「同時
    # 寫新旗標」之間的 TOCTOU 把新旗標刪掉。過期本來就不生效(回 0),留著無害,下次 suspend 會覆寫。
    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    flag = tmp_path / update_policy.AUTO_UPDATE_SUSPEND_FILENAME
    flag.write_text("1000\nreason: old\n", encoding="utf-8")

    assert update_policy.get_auto_update_suspend_until(now=1001) == 0.0
    assert flag.exists(), "過期旗標不再被刪(TOCTOU 防護)"


def test_bad_auto_update_suspend_flag_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    flag = tmp_path / update_policy.AUTO_UPDATE_SUSPEND_FILENAME
    flag.write_text("not-a-timestamp\n", encoding="utf-8")

    assert update_policy.get_auto_update_suspend_until(now=1001) == 0.0
    assert flag.exists(), "corrupt readers must not delete a concurrent replacement"


def test_crash_loop_does_not_shorten_a_longer_maintenance_window(tmp_path, monkeypatch):
    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    path = Path(update_policy.suspend_auto_updates(
        "maintenance", duration_sec=24 * 3600, now=1000))
    original = path.read_bytes()

    update_policy.suspend_auto_updates("watchdog crash loop", duration_sec=3600, now=1001)

    assert update_policy.get_auto_update_suspend_until(now=5000) == 87400
    assert path.read_bytes() == original


def test_a_later_suspension_can_extend_the_window_and_still_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    update_policy.suspend_auto_updates("first", duration_sec=120, now=1000)
    path = Path(update_policy.suspend_auto_updates("second", duration_sec=240, now=1100))
    assert update_policy.get_auto_update_suspend_until(now=1200) == 1340
    assert "reason: second" in path.read_text(encoding="utf-8")
    assert update_policy.get_auto_update_suspend_until(now=1340) == 0


def test_unreadable_existing_suspension_is_not_overwritten(tmp_path, monkeypatch):
    import builtins
    import pytest

    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    path = Path(update_policy.suspend_auto_updates("maintenance", duration_sec=86400, now=1000))
    original = path.read_bytes()
    real_open = builtins.open

    def denied(file, *args, **kwargs):
        if os.fspath(file) == str(path):
            raise PermissionError("synthetic read denial")
        return real_open(file, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(builtins, "open", denied)
        with pytest.raises(OSError):
            update_policy.suspend_auto_updates("watchdog", now=1001)
    assert path.read_bytes() == original


def test_concurrent_suspensions_keep_the_longest_deadline(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    ready = Barrier(2)

    def suspend(duration):
        ready.wait(timeout=5)
        return update_policy.suspend_auto_updates("concurrent", duration_sec=duration, now=1000)

    with ThreadPoolExecutor(max_workers=2) as executor:
        short = executor.submit(suspend, 3600)
        long = executor.submit(suspend, 86400)
        assert short.result(timeout=10) == long.result(timeout=10)
    assert update_policy.get_auto_update_suspend_until(now=5000) == 87400


def test_busy_suspension_writer_does_not_touch_the_flag(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    path = Path(update_policy.suspend_auto_updates("maintenance", duration_sec=86400, now=1000))
    original = path.read_bytes()
    monkeypatch.setattr(update_policy, "acquire", lambda path: None)
    ticks = iter([0.0, 3.0])
    monkeypatch.setattr(update_policy, "time", SimpleNamespace(
        monotonic=lambda: next(ticks), sleep=update_policy.time.sleep, time=update_policy.time.time,
    ))
    with pytest.raises(TimeoutError):
        update_policy.suspend_auto_updates("watchdog", now=1001)
    assert path.read_bytes() == original


def test_nonfinite_suspension_is_repaired_to_an_expiring_window(tmp_path, monkeypatch):
    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    flag = tmp_path / update_policy.AUTO_UPDATE_SUSPEND_FILENAME
    for invalid in ("inf", "-inf", "nan", "1e999"):
        flag.write_text(invalid + "\n", encoding="utf-8")
        assert update_policy.get_auto_update_suspend_until(now=1000) == 0
        update_policy.suspend_auto_updates("repair", duration_sec=120, now=1000)
        assert update_policy.get_auto_update_suspend_until(now=1050) == 1120
        assert update_policy.get_auto_update_suspend_until(now=1120) == 0
