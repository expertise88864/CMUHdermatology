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

    # [2026-06-03] 會診查詢：背景 daemon thread 偵測到新版後只「標記重啟 + 收掉托盤」，
    # 實際 restart_self 由 main thread 在 tray run() 返回後執行。若回到舊版「在 daemon
    # thread 直接 perform_restart()」會因 sys.exit 只結束本 thread → 舊 process 不退 →
    # 系統列同時出現新舊兩個圖示。這幾條斷言把正確設計鎖住、防回歸。
    assert "會診查詢程式偵測到新版，準備重新啟動" in consult_src
    assert "_request_restart_for_update()" in consult_src
    assert "_restart_after_run" in consult_src
    assert "release_single_instance()" in consult_src


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
