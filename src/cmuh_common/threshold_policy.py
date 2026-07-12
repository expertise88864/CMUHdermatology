# -*- coding: utf-8 -*-
"""Doctor alert threshold policy helpers."""
from __future__ import annotations

import re
from typing import Any

DEFAULT_THRESHOLDS = {
    "chang_mon_night": 129,
    "chang_thu_morning": 109,
    "chang_thu_night": 129,
    "chang_fri_afternoon": 89,
    "chen_mon_afternoon": 69,
    "chen_tue_night": 59,
    "chen_thu_morning": 54,
    "chen_thu_afternoon": 69,
}

_DOCTOR_THRESHOLD_KEYS = {
    "張廖年峰": (
        ((0, "晚上"), "chang_mon_night"),
        ((3, "上午"), "chang_thu_morning"),
        ((3, "晚上"), "chang_thu_night"),
        ((4, "下午"), "chang_fri_afternoon"),
    ),
    "陳駿升": (
        ((0, "下午"), "chen_mon_afternoon"),
        ((1, "晚上"), "chen_tue_night"),
        ((3, "上午"), "chen_thu_morning"),
        ((3, "下午"), "chen_thu_afternoon"),
    ),
}

_COUNT_DIGIT_RE = re.compile(r"(\d+)")


def build_doctor_threshold_map(doctor_name: str, threshold_settings: dict | None) -> dict:
    """Build (weekday, session) -> alert threshold for one doctor."""
    ts = threshold_settings if isinstance(threshold_settings, dict) else {}
    pairs = _DOCTOR_THRESHOLD_KEYS.get(doctor_name)
    if not pairs:
        return {}

    out = {}
    for session_key, cfg_key in pairs:
        raw = ts.get(cfg_key, DEFAULT_THRESHOLDS.get(cfg_key))
        try:
            out[session_key] = int(raw)
        except (TypeError, ValueError):
            continue
    return out


def appt_item_session_and_count_text(appt_item: Any) -> tuple[str, str]:
    """Extract session and count/status text from cached appointment item."""
    if isinstance(appt_item, dict):
        session_name = str(appt_item.get("session", ""))
        raw_count = appt_item.get("count", 0)
        status_text = str(raw_count)
        if isinstance(raw_count, int):
            status_text += "人"
        return session_name, status_text

    text = str(appt_item)
    parts = text.split("|", 1)
    status_part = parts[0]
    if ":" not in status_part:
        return "", status_part.strip()
    session_name, status_text = status_part.split(":", 1)
    return session_name, status_text.strip()


def is_near_alert_threshold(
    sessions,
    weekday_idx,
    threshold_map,
    margin: int = 10,
) -> bool:
    """Return true when any session count is within margin of its threshold."""
    if not sessions or not threshold_map:
        return False
    try:
        normalized_weekday = int(weekday_idx)
    except (TypeError, ValueError):
        return False
    try:
        normalized_margin = int(margin)
    except (TypeError, ValueError):
        normalized_margin = 10

    for appt_item in sessions:
        # [CL-04 audit 2026-07-12] 已止掛(is_stopped:不會再增號)不應因既有數接近門檻而誤發「快滿」。
        if isinstance(appt_item, dict) and appt_item.get("is_stopped"):
            continue
        session_name, status_text = appt_item_session_and_count_text(appt_item)
        if "休診" in status_text or "停診" in status_text:
            continue
        match = _COUNT_DIGIT_RE.search(status_text)
        if not match:
            continue
        try:
            count = int(match.group(1))
        except ValueError:
            continue
        threshold = threshold_map.get((normalized_weekday, session_name))
        if isinstance(threshold, int) and count >= threshold - normalized_margin:
            return True
    return False
