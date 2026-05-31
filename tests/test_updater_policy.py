# -*- coding: utf-8 -*-
"""Updater integration tests for the shared write-suspension policy."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import updater  # noqa: E402


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
