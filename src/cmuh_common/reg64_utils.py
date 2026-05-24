# -*- coding: utf-8 -*-
"""reg64（門診動態）時段工具 — main.py / scheduler.py 共用。

【重構 2026-05-21】兩支入口各有一份 byte-identical 實作，抽出避免不同步。
全部純函式，只依賴 stdlib datetime。

API:
  - reg64_time_code_from_local_clock(when=None) -> "1"/"2"/"3"
  - reg64_slot_cn(time_code) -> "早上" / "下午" / "晚上"
  - reg64_slot_label_color(time_code) -> "#XXXXXX"
  - resolve_clinic_reg64_time_code(mode, when=None) -> "1"/"2"/"3"
  - _reg64_tc_to_session_cn(time_code) -> "上午" / "下午" / "晚上"（與 reg64_slot_cn 略異）
"""
from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Optional


def reg64_time_code_from_local_clock(when: Optional[datetime] = None) -> str:
    """依本機時鐘：00:00–13:29→1，13:30–17:59→2，18:00–23:59→3。"""
    if when is None:
        when = datetime.now()
    cur = when.time()
    if cur <= dt_time(13, 29, 59):
        return "1"
    if cur <= dt_time(17, 59, 59):
        return "2"
    return "3"


def reg64_slot_cn(time_code) -> str:
    """TimeCode → 早上／下午／晚上（與門診統計 session 用語一致）。"""
    return {"1": "早上", "2": "下午", "3": "晚上"}.get(str(time_code), "")


def reg64_slot_label_color(time_code) -> str:
    """診間代號後方時段字色：早綠、午藍、晚深藍。"""
    return {"1": "#2E7D32", "2": "#1565C0", "3": "#0D47A1"}.get(str(time_code), "#78909C")


# 門診動態「顯示時段」選項（UI 僅中文，不顯示 Code 數字）
CLINIC_DISPLAY_MODE_OPTIONS = (
    ("auto", "自動（依電腦時間）"),
    ("1", "早上"),
    ("2", "下午"),
    ("3", "晚上"),
)


def _normalize_clinic_display_mode(val) -> str:
    v = str(val).strip().lower() if val is not None else "auto"
    if v in ("1", "2", "3", "auto"):
        return v
    return "auto"


def _clinic_display_mode_label(mode_key: str) -> str:
    for k, lab in CLINIC_DISPLAY_MODE_OPTIONS:
        if k == mode_key:
            return lab
    return CLINIC_DISPLAY_MODE_OPTIONS[0][1]


def _clinic_display_mode_from_label(label: str) -> str:
    for k, lab in CLINIC_DISPLAY_MODE_OPTIONS:
        if lab == label:
            return k
    return "auto"


def resolve_clinic_reg64_time_code(mode: str,
                                    when: Optional[datetime] = None) -> str:
    """自動 → 依本機時鐘；早上/下午/晚上 → 固定對應 reg64 TimeCode。"""
    m = _normalize_clinic_display_mode(mode)
    if m in ("1", "2", "3"):
        return m
    return reg64_time_code_from_local_clock(when)


def _reg64_tc_to_session_cn(time_code) -> str:
    """TimeCode → 上午／下午／晚上（注意：與 reg64_slot_cn 的「早上」略異，
    這個用「上午」是因為門診 metadata 內部就用這套字串）。"""
    return {"1": "上午", "2": "下午", "3": "晚上"}.get(str(time_code), "")


def canonical_clinic_session_str(value) -> str:
    """Normalize clinic session labels to 早上|下午|晚上."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    aliases = {
        "上午": "早上",
        "早診": "早上",
        "早": "早上",
        "早上": "早上",
        "下午": "下午",
        "午診": "下午",
        "午": "下午",
        "晚上": "晚上",
        "晚診": "晚上",
        "晚": "晚上",
    }
    return aliases.get(text, text)


def session_boundary_datetime(session_cn: str, now_dt: datetime) -> datetime:
    """Earliest same-day time when close detection should start for a session."""
    session = canonical_clinic_session_str(session_cn)
    if session == "早上":
        hour, minute = 12, 0
    elif session == "下午":
        hour, minute = 17, 0
    else:
        hour, minute = 21, 0
    return now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def prev_session_cn(session_cn: str) -> str | None:
    """Return the previous clinic session label using canonical terms."""
    session = canonical_clinic_session_str(session_cn)
    if session == "下午":
        return "早上"
    if session == "晚上":
        return "下午"
    return None


def clinic_int_count(value, default: int = 0) -> int:
    """Coerce reg64 numeric counts while rejecting blanks, booleans and fractions."""
    if value is None or value == "" or value == "-":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else default
    try:
        text = str(value).strip()
        if text in ("-", "--", ""):
            return default
        return int(text)
    except (TypeError, ValueError):
        return default


def reg64_clinic_quiet_hours(when: Optional[datetime] = None) -> bool:
    """Return true when reg64 HTTP polling should stay quiet."""
    current = when or datetime.now()
    return current.hour < 8


def reg64_next_allowed_fetch_time(when: Optional[datetime] = None) -> datetime:
    """Return the next 08:00 boundary, or when if polling is already allowed."""
    current = when or datetime.now()
    start = current.replace(hour=8, minute=0, second=0, microsecond=0)
    if current < start:
        return start
    return current
