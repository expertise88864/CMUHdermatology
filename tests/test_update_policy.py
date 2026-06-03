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


def test_stale_auto_update_suspend_flag_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    flag = tmp_path / update_policy.AUTO_UPDATE_SUSPEND_FILENAME
    flag.write_text("1000\nreason: old\n", encoding="utf-8")

    assert update_policy.get_auto_update_suspend_until(now=1001) == 0.0
    assert not flag.exists()


def test_bad_auto_update_suspend_flag_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(tmp_path))
    flag = tmp_path / update_policy.AUTO_UPDATE_SUSPEND_FILENAME
    flag.write_text("not-a-timestamp\n", encoding="utf-8")

    assert update_policy.get_auto_update_suspend_until(now=1001) == 0.0
    assert not flag.exists()
