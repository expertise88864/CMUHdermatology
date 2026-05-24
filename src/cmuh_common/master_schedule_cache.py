# -*- coding: utf-8 -*-
"""Master schedule cache loading and refresh helpers."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Callable

from cmuh_common.config_io import load_json_dict
from cmuh_common.ui_messages import UiMasterScheduleMessage, put_ui_message

MASTER_SCHEDULE_DISK_TTL_SECONDS = 24 * 60 * 60
MASTER_SCHEDULE_INCREMENTAL_FRESH_SECONDS = 30 * 60


def normalize_master_schedule(raw: Any) -> dict:
    """Normalize cached master schedule rows and skip malformed weekday keys."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for doctor_name, days in raw.items():
        if not isinstance(days, dict):
            continue
        normalized_days = {}
        for weekday_key, sessions in days.items():
            try:
                weekday_idx = int(weekday_key)
            except (TypeError, ValueError):
                logging.debug(
                    "Skipping invalid master schedule weekday key: %r/%r",
                    doctor_name,
                    weekday_key,
                )
                continue
            normalized_days[weekday_idx] = sessions
        if normalized_days:
            out[str(doctor_name)] = normalized_days
    return out


def load_master_schedule_cache(path: str) -> dict:
    """Load cached master schedule with corruption-safe JSON handling."""
    return normalize_master_schedule(load_json_dict(path, {}, merge_defaults=False))


def _canonical_schedule(schedule: dict) -> dict:
    return {
        str(doc): {str(day): sessions for day, sessions in days.items()}
        for doc, days in (schedule or {}).items()
        if isinstance(days, dict)
    }


def _schedule_hash(schedule: dict) -> str:
    data = json.dumps(
        _canonical_schedule(schedule),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha1(data.encode("utf-8")).hexdigest()


def refresh_master_schedule_if_needed(
    ui_queue,
    fetch_schedule: Callable[[], dict],
    cache_path: str,
    *,
    force: bool = False,
    fresh_seconds: int = MASTER_SCHEDULE_INCREMENTAL_FRESH_SECONDS,
    ttl_seconds: int = MASTER_SCHEDULE_DISK_TTL_SECONDS,
) -> str:
    """Fetch master schedule only when the disk cache is stale enough.

    Returns a status string for logging/tests: fresh, unchanged, updated,
    fetched, or fetch_failed.
    """
    cache_age = None
    if not force and os.path.exists(cache_path):
        try:
            cache_age = time.time() - os.path.getmtime(cache_path)
        except OSError:
            cache_age = None

    if cache_age is not None and cache_age < fresh_seconds:
        logging.info(
            "[master_schedule] cache fresh (%.1f minutes), skip fetch",
            cache_age / 60.0,
        )
        return "fresh"

    try:
        new_schedule = normalize_master_schedule(fetch_schedule())
    except Exception as exc:
        logging.warning("[master_schedule] fetch failed; keep cache: %s", exc)
        return "fetch_failed"

    if cache_age is not None and cache_age < ttl_seconds:
        old_schedule = load_master_schedule_cache(cache_path)
        if old_schedule and _schedule_hash(old_schedule) == _schedule_hash(new_schedule):
            logging.info("[master_schedule] unchanged; keep local cache")
            try:
                os.utime(cache_path, None)
            except OSError:
                pass
            return "unchanged"
        status = "updated"
    else:
        status = "fetched"

    put_ui_message(ui_queue, UiMasterScheduleMessage(schedule=new_schedule))
    return status
