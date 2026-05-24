# -*- coding: utf-8 -*-
"""Helpers for settings backup snapshots."""
from __future__ import annotations

import os
import shutil
from datetime import date, datetime
from typing import Iterable

DEFAULT_SETTINGS_BACKUP_FILES = (
    "r_doctor_settings.json",
    "threshold_settings.json",
    "doctors.json",
    "auto_reboot_settings.json",
    "clinic_light_settings.json",
)


def normalize_hhmm(text: object, fallback: str) -> str:
    """Normalize a HH:MM string, preserving the historical clamp behavior."""
    value = str(text).strip()
    if ":" not in value:
        return fallback
    hh, mm = value.split(":", 1)
    try:
        hour = max(0, min(24, int(hh)))
        minute = max(0, min(59, int(mm)))
    except (TypeError, ValueError):
        return fallback
    if hour == 24:
        minute = 0
    return f"{hour:02d}:{minute:02d}"


def create_settings_snapshot(
    settings_dir: str,
    files: Iterable[str] = DEFAULT_SETTINGS_BACKUP_FILES,
    *,
    when: datetime | None = None,
) -> tuple[str, int]:
    """Copy existing settings files into a timestamped versions snapshot."""
    snap_name = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    snap_dir = os.path.join(settings_dir, "versions", snap_name)
    os.makedirs(snap_dir, exist_ok=True)

    copied = 0
    for filename in files:
        src = os.path.join(settings_dir, filename)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(snap_dir, filename))
            copied += 1
    return snap_name, copied


def find_latest_snapshot_for_date(
    settings_dir: str,
    target_date: date,
    *,
    versions_dir_name: str = "versions",
) -> str | None:
    """Return the latest snapshot name for target_date, if one exists."""
    versions_dir = os.path.join(settings_dir, versions_dir_name)
    if not os.path.isdir(versions_dir):
        return None

    ymd = target_date.strftime("%Y%m%d")
    candidates = [
        name for name in os.listdir(versions_dir)
        if name.startswith(ymd)
        and os.path.isdir(os.path.join(versions_dir, name))
    ]
    if not candidates:
        return None
    return sorted(candidates)[-1]


def restore_settings_snapshot(
    settings_dir: str,
    snap_name: str,
    files: Iterable[str] = DEFAULT_SETTINGS_BACKUP_FILES,
) -> int:
    """Restore settings files from an existing snapshot directory."""
    snap_dir = os.path.join(settings_dir, "versions", snap_name)
    restored = 0
    for filename in files:
        src = os.path.join(snap_dir, filename)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(settings_dir, filename))
            restored += 1
    return restored
