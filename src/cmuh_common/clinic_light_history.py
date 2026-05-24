# -*- coding: utf-8 -*-
"""Clinic light history helpers."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


EMPTY_DISPLAY = "\u2014"


def light_bucket_minute(when: datetime) -> int:
    """Return the 3-minute bucket start minute-of-day."""
    return (when.hour * 60 + when.minute) // 3 * 3


def light_bucket_label(bucket_min: int) -> str:
    return f"{bucket_min // 60:02d}:{bucket_min % 60:02d}"


def light_history_key(doc_name: str, room_code: Any, session_key: str,
                      bucket_min: int) -> str:
    return f"{doc_name}|{room_code}|{session_key}|{light_bucket_label(bucket_min)}"


def record_light_sample(data: dict, *, room_code: Any, doc_name: str,
                        session_key: str, light_val: Any, when: datetime,
                        retain_days: int) -> tuple[dict, bool]:
    """Record one light sample in a 3-minute bucket.

    Returns (new_data, changed). One sample is kept per key per date, and
    entries older than retain_days are dropped from that key.
    """
    if not room_code or not doc_name or light_val in (None, ""):
        return data, False
    try:
        light_num = int(light_val)
    except (TypeError, ValueError):
        return data, False

    bucket_min = light_bucket_minute(when)
    key = light_history_key(doc_name, room_code, session_key, bucket_min)
    cutoff_date = (when.date() - timedelta(days=retain_days)).strftime("%Y/%m/%d")
    today_str = when.strftime("%Y/%m/%d")
    entry = {"date": today_str, "light": light_num}

    new_data = dict(data or {})
    rows = new_data.get(key, [])
    if not isinstance(rows, list):
        rows = []
    new_data[key] = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("date") != today_str
        and row.get("date", "") >= cutoff_date
    ]
    new_data[key].append(entry)
    return new_data, True


def historical_light_average(data: dict, *, room_code: Any, doc_name: str,
                             session_key: str, when: datetime,
                             history_days: int,
                             window_minutes: int) -> str:
    """Return near-time historical light average text."""
    if not room_code or not doc_name or not data:
        return EMPTY_DISPLAY

    target_min = when.hour * 60 + when.minute
    cutoff = when.date() - timedelta(days=history_days)
    today = when.date()
    all_values = []
    same_weekday_values = []
    bucket_starts = range(
        max(0, target_min - int(window_minutes)),
        min(24 * 60 - 1, target_min + int(window_minutes)) + 1,
        3,
    )
    for bucket_min in bucket_starts:
        bucket_min = (bucket_min // 3) * 3
        key = light_history_key(doc_name, room_code, session_key, bucket_min)
        rows = data.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                row_date = datetime.strptime(
                    row.get("date", ""), "%Y/%m/%d").date()
                if row_date < cutoff or row_date >= today:
                    continue
                light_num = int(row["light"])
            except (KeyError, ValueError, TypeError):
                continue
            all_values.append(light_num)
            if row_date.weekday() == when.weekday():
                same_weekday_values.append(light_num)

    values = same_weekday_values if len(same_weekday_values) >= 3 else all_values
    if not values:
        return EMPTY_DISPLAY
    sorted_vals = sorted(values)
    trim = max(0, int(len(sorted_vals) * 0.1))
    if trim and len(sorted_vals) >= 10:
        sorted_vals = sorted_vals[trim:-trim]
    avg_val = sum(sorted_vals) / len(sorted_vals)
    return f"~{int(round(avg_val))}"
