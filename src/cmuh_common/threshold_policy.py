# -*- coding: utf-8 -*-
"""Doctor alert threshold policy helpers."""
from __future__ import annotations

import re
from typing import Any

# ★[2026-08-05 使用者定案] 止掛提醒對象改為 陳駿升 + 沈冠宇★
#   張廖年峰不再做止掛提醒(整套止掛邏輯保留,只是不再有他的門檻)。
#   沈冠宇的一早/一午/三午【刻意不給預設值】—— 沒有預設就不會有門檻,
#   `build_doctor_threshold_map` 會跳過那些診次(int(None)/int("") 都落到 except),
#   等使用者自己在設定頁填上數字才開始提醒。只有三晚預設 100 人。
DEFAULT_THRESHOLDS = {
    "chen_mon_afternoon": 69,
    "chen_tue_night": 59,
    "chen_thu_morning": 54,
    "chen_thu_afternoon": 69,
    "shen_wed_night": 100,
}

_DOCTOR_THRESHOLD_KEYS = {
    "陳駿升": (
        ((0, "下午"), "chen_mon_afternoon"),
        ((1, "晚上"), "chen_tue_night"),
        ((3, "上午"), "chen_thu_morning"),
        ((3, "下午"), "chen_thu_afternoon"),
    ),
    "沈冠宇": (
        ((0, "上午"), "shen_mon_morning"),      # 一早：無預設
        ((0, "下午"), "shen_mon_afternoon"),    # 一午：無預設
        ((2, "下午"), "shen_wed_afternoon"),    # 三午：無預設
        ((2, "晚上"), "shen_wed_night"),        # 三晚：預設 100
    ),
}

_COUNT_DIGIT_RE = re.compile(r"(\d+)")


def normalize_threshold_entry(cfg_key: str, raw: Any):
    """設定頁一格門檻要存進 threshold_settings.json 的值。

    ★留空 = 這個診次【沒有門檻】,不是 0★
      門檻 0 會讓 `is_near_alert_threshold`(count >= 0 - margin)恆真 ——
      「還沒設定」會變成「每一診都提醒」,方向完全相反。這一格原本的寫法是
      `except: DEFAULT_THRESHOLDS.get(key, 0)`,對沈冠宇那三個【刻意沒有預設】
      的診次就會存下 0。故:空字串一律存 ""(build_doctor_threshold_map 會跳過)。

    看不懂的輸入(打錯字)則沿用該鍵的原廠預設;沒有原廠預設的鍵同樣退成 ""。
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return ""
    try:
        return int(text)
    except (TypeError, ValueError):
        fallback = DEFAULT_THRESHOLDS.get(cfg_key)
        return "" if fallback is None else int(fallback)


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
