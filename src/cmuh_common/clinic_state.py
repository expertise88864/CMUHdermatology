# -*- coding: utf-8 -*-
"""Clinic dynamic-state helpers."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable


DEFAULT_CLINIC_ROOMS = ("101", "102")
LEGACY_DEFAULT_CLINIC_ROOMS = ("181", "182")


def normalize_clinic_rooms(value: Any) -> tuple[list[str], bool]:
    """Normalize two clinic rooms and migrate the historical 181/182 default."""
    if not isinstance(value, list):
        return list(DEFAULT_CLINIC_ROOMS), True
    rooms = [str(room or "").strip() for room in value[:2]]
    while len(rooms) < 2:
        rooms.append("")
    if tuple(rooms) == LEGACY_DEFAULT_CLINIC_ROOMS:
        return list(DEFAULT_CLINIC_ROOMS), True
    return rooms, rooms != value


def clinic_dynamic_today_str() -> str:
    return date.today().strftime("%Y/%m/%d")


def clinic_dynamic_state_key(room_code: Any, time_code: Any) -> str:
    return f"{str(room_code).strip()}/{str(time_code).strip()}"


def new_clinic_tracker(curr_session_i: str, current_timestamp: float) -> dict:
    return {
        'last_completed_set': set(),
        'last_waiting_set': set(),
        'last_valid_completion_time': current_timestamp,
        'durations': [],
        'waiting_durations': [],
        'is_saved': False,
        'doc_name': '',
        'actual_closing_dt': None,
        'phototherapy_count': 0,
        'patient_checkin_times': {},
        'session_period': curr_session_i,
        'is_first_run': True,
        'first_valid_skipped': False,
        'had_any_activity': False,
        'stable_since_ts': None,
        'last_monitor_pair': None,
        'last_activity_ts': None,
    }


def int_set(value: Any) -> set[int]:
    out = set()
    for item in value or []:
        try:
            out.add(int(item))
        except (TypeError, ValueError):
            pass
    return out


def float_list(value: Any) -> list[float]:
    out = []
    for item in value or []:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            pass
    return out


def state_matches(state: Any, room_code: Any, time_code: Any, today_str: str,
                  canonical_session: Callable[[Any], str],
                  doc_name: Any = None, session_cn: Any = None) -> bool:
    if not isinstance(state, dict):
        return False
    if state.get("date") != today_str:
        return False
    if str(state.get("room", "")).strip() != str(room_code).strip():
        return False
    if str(state.get("time_code", "")).strip() != str(time_code).strip():
        return False
    if session_cn and canonical_session(state.get("session")) != canonical_session(session_cn):
        return False
    cached_doc = (state.get("doctor") or "").strip()
    if doc_name and cached_doc and cached_doc != str(doc_name).strip():
        return False
    return True


def restore_tracker_from_state(state: Any, curr_session_i: str,
                               current_timestamp: float,
                               canonical_session: Callable[[Any], str]) -> dict:
    tracker = new_clinic_tracker(curr_session_i, current_timestamp)
    if not isinstance(state, dict):
        return tracker
    tracker["doc_name"] = (state.get("doctor") or "").strip()
    tracker["session_period"] = canonical_session(state.get("session")) or curr_session_i
    tracker["last_completed_set"] = int_set(state.get("last_completed_set"))
    tracker["last_waiting_set"] = int_set(state.get("last_waiting_set"))
    tracker["durations"] = float_list(state.get("durations"))
    tracker["waiting_durations"] = float_list(state.get("waiting_durations"))
    try:
        tracker["phototherapy_count"] = int(state.get("phototherapy_count", 0))
    except (TypeError, ValueError):
        tracker["phototherapy_count"] = 0
    try:
        tracker["last_valid_completion_time"] = float(
            state.get("last_valid_completion_time", current_timestamp))
    except (TypeError, ValueError):
        tracker["last_valid_completion_time"] = current_timestamp
    tracker["is_saved"] = bool(state.get("is_saved", False))
    tracker["first_valid_skipped"] = bool(
        state.get("first_valid_skipped", bool(tracker["durations"])))
    tracker["had_any_activity"] = bool(
        state.get("had_any_activity")
        or tracker["last_completed_set"]
        or tracker["last_waiting_set"]
        or tracker["durations"]
    )
    tracker["is_first_run"] = (
        bool(state.get("is_first_run", False)) and not tracker["had_any_activity"])

    patient_times = {}
    for key, value in (state.get("patient_checkin_times") or {}).items():
        try:
            patient_times[int(key)] = float(value)
        except (TypeError, ValueError):
            pass
    tracker["patient_checkin_times"] = patient_times

    for key in ("stable_since_ts",):
        try:
            tracker[key] = float(state[key]) if state.get(key) is not None else None
        except (TypeError, ValueError):
            tracker[key] = None

    pair = state.get("last_monitor_pair")
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        try:
            tracker["last_monitor_pair"] = (int(pair[0]), int(pair[1]))
        except (TypeError, ValueError):
            tracker["last_monitor_pair"] = None

    close_iso = state.get("actual_closing_dt") or ""
    if close_iso:
        try:
            tracker["actual_closing_dt"] = datetime.fromisoformat(close_iso)
        except (TypeError, ValueError):
            tracker["actual_closing_dt"] = None
    return tracker


def build_dynamic_state(today_str: str, saved_at: str, room_code: Any,
                        time_code: Any, session_cn: str, doc_name: str,
                        tracker: dict, result: dict, *,
                        current_timestamp: float,
                        curr_avg: str = "-", est_remain: str = "—",
                        hist_light: str = "—", prev_close: str = "—") -> dict:
    actual_dt = tracker.get("actual_closing_dt")
    if isinstance(actual_dt, datetime):
        actual_dt = actual_dt.isoformat(timespec="seconds")
    else:
        actual_dt = ""
    return {
        "date": today_str,
        "saved_at": saved_at,
        "room": str(room_code),
        "time_code": str(time_code),
        "session": session_cn,
        "doctor": doc_name,
        "last_completed_set": sorted(int(x) for x in tracker.get("last_completed_set", set())),
        "last_waiting_set": sorted(int(x) for x in tracker.get("last_waiting_set", set())),
        "last_valid_completion_time": float(
            tracker.get("last_valid_completion_time", current_timestamp)),
        "durations": [float(x) for x in tracker.get("durations", [])],
        "waiting_durations": [float(x) for x in tracker.get("waiting_durations", [])],
        "is_saved": bool(tracker.get("is_saved", False)),
        "actual_closing_dt": actual_dt,
        "phototherapy_count": int(tracker.get("phototherapy_count", 0)),
        "patient_checkin_times": {
            str(int(k)): float(v)
            for k, v in (tracker.get("patient_checkin_times") or {}).items()
        },
        "session_period": session_cn,
        "is_first_run": bool(tracker.get("is_first_run", False)),
        "first_valid_skipped": bool(tracker.get("first_valid_skipped", False)),
        "had_any_activity": bool(tracker.get("had_any_activity", False)),
        "stable_since_ts": tracker.get("stable_since_ts"),
        "last_monitor_pair": (
            list(tracker.get("last_monitor_pair"))
            if tracker.get("last_monitor_pair") else None
        ),
        "last_result": {
            "doc_name": doc_name,
            "reg64_time_code": str(time_code),
            "light": result.get("light", "--"),
            "total": result.get("total", "-"),
            "waiting": result.get("waiting", "-"),
            "completed": result.get("completed", 0),
            "status": result.get("status", ""),
            "is_closed": bool(result.get("is_closed", False)),
            "is_stopped": bool(result.get("is_stopped", False)),
            "close_time": result.get("close_time", ""),
        },
        "last_display": {
            "curr_avg": curr_avg,
            "est_remain": est_remain if est_remain and est_remain != "-" else "—",
            "hist_light": hist_light,
            "prev_close": prev_close,
        },
    }


def prune_states_for_today(states: dict, today_str: str) -> dict:
    return {
        key: value for key, value in states.items()
        if isinstance(value, dict) and value.get("date") == today_str
    }


def matching_state_keys(states: dict, room_code: Any, time_code: Any = None,
                        doc_name: Any = None) -> list:
    keys = []
    for key, state in states.items():
        if not isinstance(state, dict):
            continue
        if str(state.get("room", "")) != str(room_code):
            continue
        if time_code is not None and str(state.get("time_code", "")) != str(time_code):
            continue
        if doc_name and state.get("doctor") != doc_name:
            continue
        keys.append(key)
    return keys
