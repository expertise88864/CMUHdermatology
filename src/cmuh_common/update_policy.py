# -*- coding: utf-8 -*-
"""Shared auto-update suspension policy for watchdog and updater."""
from __future__ import annotations

import logging
import os
import time

from cmuh_common.atomic_io import atomic_write_text
from cmuh_common.paths import get_settings_dir

AUTO_UPDATE_SUSPEND_FILENAME = ".auto_update_suspended_until"
AUTO_UPDATE_CHECK_TIMES = tuple(
    f"{minute // 60:02d}:{minute % 60:02d}"
    for minute in range(8 * 60, 17 * 60 + 1, 15)
)


def get_auto_update_suspend_path() -> str:
    return os.path.join(get_settings_dir(), AUTO_UPDATE_SUSPEND_FILENAME)


def suspend_auto_updates(reason: str, *, duration_sec: float = 3600,
                         now: float | None = None) -> str:
    """Suspend file-writing updates until the timestamp stored in settings."""
    current = time.time() if now is None else float(now)
    suspend_until = int(current + max(0.0, float(duration_sec)))
    path = get_auto_update_suspend_path()
    content = f"{suspend_until}\nreason: {reason}\n"
    if not atomic_write_text(path, content, encoding="utf-8"):
        raise OSError(f"failed to write auto-update suspension flag: {path}")
    return path


def get_auto_update_suspend_until(*, now: float | None = None) -> float:
    """Return an active suspension timestamp and remove stale/bad flags."""
    current = time.time() if now is None else float(now)
    path = get_auto_update_suspend_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        suspend_until = float(first_line)
        if suspend_until > current:
            return suspend_until
    except FileNotFoundError:
        return 0.0
    except (OSError, TypeError, ValueError) as e:
        logging.warning("[update-policy] invalid suspension flag %s: %s", path, e)

    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logging.debug("[update-policy] failed to remove stale flag %s",
                      path, exc_info=True)
    return 0.0
