# -*- coding: utf-8 -*-
"""Cache data helpers shared by main.py and scheduler.py."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from cmuh_common.atomic_io import atomic_write_json


def date_key_encoder(obj: Any) -> str:
    """Encode date-like keys as ISO strings for JSON output."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def decode_date_keys(dct: dict) -> dict:
    """Decode ISO date-string keys back to date objects when possible."""
    new_dct = {}
    for key, value in dct.items():
        try:
            new_key = date.fromisoformat(key)
        except (TypeError, ValueError):
            new_key = key
        new_dct[new_key] = value
    return new_dct


def convert_keys_to_str(data: Any) -> Any:
    """Recursively convert mapping keys to strings for JSON cache files."""
    if isinstance(data, dict):
        new_dict = {}
        for key, value in data.items():
            if isinstance(key, (date, datetime)):
                key_str = key.isoformat()
            else:
                key_str = str(key)
            new_dict[key_str] = convert_keys_to_str(value)
        return new_dict
    if isinstance(data, list):
        return [convert_keys_to_str(item) for item in data]
    return data


def save_json_cache(path: str, data: Any) -> None:
    """Write cache JSON with normalized keys and atomic replace semantics."""
    atomic_write_json(path, convert_keys_to_str(data), default=date_key_encoder)


def build_master_schedule_index(master_schedule: dict) -> tuple[defaultdict, dict]:
    """Build lookup indexes for master schedule queries."""
    by_weekday = defaultdict(list)
    self_paid_map = {}
    for doctor_name, weekday_map in master_schedule.items():
        if not isinstance(weekday_map, dict):
            continue
        for weekday_idx, sessions in weekday_map.items():
            try:
                normalized_weekday = int(weekday_idx)
            except (TypeError, ValueError):
                continue
            if not isinstance(sessions, list):
                continue
            for session_info in sessions:
                if not isinstance(session_info, dict):
                    continue
                session_name = session_info.get('session')
                if not session_name:
                    continue
                is_self_paid = bool(session_info.get('is_self_paid'))
                by_weekday[normalized_weekday].append(
                    (doctor_name, session_name, is_self_paid))
                self_paid_map[
                    (doctor_name, normalized_weekday, session_name)
                ] = is_self_paid
    return by_weekday, self_paid_map
