# -*- coding: utf-8 -*-
"""Updater integration tests for the shared write-suspension policy."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import updater  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


def test_background_helpers_restart_immediately_after_update():
    autoclock_src = (ROOT / "src" / "autoclock.py").read_text(encoding="utf-8")
    consult_src = (ROOT / "src" / "consult_query.py").read_text(encoding="utf-8")

    assert "打卡程式偵測到新版，立即重新啟動" in autoclock_src
    assert "restart_program()" in autoclock_src
    assert "會診查詢程式偵測到新版，立即重新啟動" in consult_src
    assert "release_single_instance()" in consult_src
    assert "perform_restart()" in consult_src


def test_check_and_update_honors_active_write_suspension(monkeypatch):
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    monkeypatch.setattr(
        updater, "get_auto_update_suspend_until", lambda: 12345.0)
    monkeypatch.setattr(
        updater, "_fetch_manifest",
        lambda: pytest.fail("suspended updater must not fetch manifest"),
    )

    result = updater.check_and_update(write_files=True)

    assert result.checked is False
    assert result.suspended_until == 12345.0


def test_read_only_update_check_ignores_write_suspension(monkeypatch):
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(
        updater, "get_auto_update_suspend_until",
        lambda: pytest.fail("read-only check must ignore write suspension"),
    )
    monkeypatch.setattr(
        updater, "_fetch_manifest",
        lambda: {"app_version": updater.CURRENT_VERSION, "files": []},
    )

    result = updater.check_and_update(write_files=False)

    assert result.checked is True
    assert result.suspended_until == 0.0
