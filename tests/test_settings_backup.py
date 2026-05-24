# -*- coding: utf-8 -*-
"""settings_backup helpers."""
import os
import sys
import tempfile
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.settings_backup import (  # noqa: E402
    create_settings_snapshot,
    find_latest_snapshot_for_date,
    normalize_hhmm,
    restore_settings_snapshot,
)


def test_normalize_hhmm_clamps_and_falls_back():
    assert normalize_hhmm("7:5", "00:00") == "07:05"
    assert normalize_hhmm("25:99", "00:00") == "24:00"
    assert normalize_hhmm("24:30", "00:00") == "24:00"
    assert normalize_hhmm("bad", "08:00") == "08:00"
    assert normalize_hhmm(None, "08:00") == "08:00"


def test_create_snapshot_copies_existing_files_only():
    with tempfile.TemporaryDirectory() as tmp:
        first = os.path.join(tmp, "threshold_settings.json")
        with open(first, "w", encoding="utf-8") as handle:
            handle.write('{"a": 1}')

        snap_name, copied = create_settings_snapshot(
            tmp,
            files=("threshold_settings.json", "missing.json"),
            when=datetime(2026, 5, 25, 9, 8, 7),
        )

        assert snap_name == "20260525_090807"
        assert copied == 1
        assert os.path.exists(os.path.join(
            tmp, "versions", snap_name, "threshold_settings.json"))
        assert not os.path.exists(os.path.join(
            tmp, "versions", snap_name, "missing.json"))


def test_find_latest_snapshot_for_date_ignores_files():
    with tempfile.TemporaryDirectory() as tmp:
        versions = os.path.join(tmp, "versions")
        os.makedirs(os.path.join(versions, "20260524_080000"))
        os.makedirs(os.path.join(versions, "20260524_120000"))
        with open(os.path.join(versions, "20260524_235959"), "w", encoding="utf-8") as handle:
            handle.write("not a snapshot directory")

        assert find_latest_snapshot_for_date(tmp, date(2026, 5, 24)) == "20260524_120000"
        assert find_latest_snapshot_for_date(tmp, date(2026, 5, 23)) is None


def test_restore_settings_snapshot_copies_back_existing_files():
    with tempfile.TemporaryDirectory() as tmp:
        snap_name = "20260524_090000"
        snap_dir = os.path.join(tmp, "versions", snap_name)
        os.makedirs(snap_dir)
        target = os.path.join(tmp, "threshold_settings.json")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("old")
        with open(os.path.join(snap_dir, "threshold_settings.json"), "w", encoding="utf-8") as handle:
            handle.write("new")

        restored = restore_settings_snapshot(
            tmp,
            snap_name,
            files=("threshold_settings.json", "missing.json"),
        )

        with open(target, encoding="utf-8") as handle:
            content = handle.read()

        assert restored == 1
        assert content == "new"
