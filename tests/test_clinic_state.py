# -*- coding: utf-8 -*-
"""clinic_state helpers."""
from datetime import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.clinic_state import (  # noqa: E402
    DEFAULT_CLINIC_ROOMS,
    build_dynamic_state,
    clinic_dynamic_state_key,
    int_set,
    matching_state_keys,
    new_clinic_tracker,
    normalize_clinic_rooms,
    prune_states_for_today,
    restore_tracker_from_state,
    state_matches,
)


def test_default_clinic_rooms_are_101_102_103():
    assert DEFAULT_CLINIC_ROOMS == ("101", "102", "103")


def test_normalize_clinic_rooms_migrates_legacy_defaults():
    assert normalize_clinic_rooms(["181", "182"]) == (["101", "102", "103"], True)


def test_normalize_clinic_rooms_preserves_custom_rooms_and_repairs_bad_values():
    # 自訂前兩格 → 保留並用對應預設補上第三格(103)；舊版 2 格設定一律升級成 3 格
    assert normalize_clinic_rooms([" 201 ", "202"]) == (["201", "202", "103"], True)
    assert normalize_clinic_rooms(["201", "202"]) == (["201", "202", "103"], True)
    # 已是完整三格 → 不變動
    assert normalize_clinic_rooms(["201", "202", "203"]) == (["201", "202", "203"], False)
    assert normalize_clinic_rooms("bad") == (["101", "102", "103"], True)


def _canon(value):
    return {"上午": "早上"}.get(str(value), str(value or ""))


def test_state_key_and_numeric_coercion_helpers():
    assert clinic_dynamic_state_key(" 181 ", " 1 ") == "181/1"
    assert int_set(["1", "bad", 2]) == {1, 2}


def test_restore_tracker_from_state_filters_bad_values():
    state = {
        "doctor": "王小明",
        "session": "上午",
        "last_completed_set": ["1", "x"],
        "durations": ["60", "bad"],
        "patient_checkin_times": {"3": "12.5", "x": "bad"},
        "last_monitor_pair": ["4", "5"],
        "actual_closing_dt": "2026-05-24T12:30:00",
    }

    tracker = restore_tracker_from_state(state, "早上", 100.0, _canon)

    assert tracker["doc_name"] == "王小明"
    assert tracker["session_period"] == "早上"
    assert tracker["last_completed_set"] == {1}
    assert tracker["durations"] == [60.0]
    assert tracker["patient_checkin_times"] == {3: 12.5}
    assert tracker["last_monitor_pair"] == (4, 5)
    assert tracker["actual_closing_dt"] == datetime(2026, 5, 24, 12, 30)


def test_build_dynamic_state_round_trip_core_fields():
    tracker = new_clinic_tracker("早上", 100.0)
    tracker.update({
        "doc_name": "王小明",
        "last_completed_set": {2, 1},
        "patient_checkin_times": {7: 12.0},
        "actual_closing_dt": datetime(2026, 5, 24, 12, 30),
    })

    state = build_dynamic_state(
        "2026/05/24", "2026-05-24T12:31:00", "181", "1", "早上",
        "王小明", tracker, {"light": "綠", "completed": 2},
        current_timestamp=200.0, est_remain="-")

    assert state["last_completed_set"] == [1, 2]
    assert state["patient_checkin_times"] == {"7": 12.0}
    assert state["actual_closing_dt"] == "2026-05-24T12:30:00"
    assert state["last_display"]["est_remain"] == "—"
    assert state["last_result"]["completed"] == 2


def test_state_matching_and_cache_selection():
    state = {
        "date": "2026/05/24", "room": "181", "time_code": "1",
        "session": "上午", "doctor": "王小明",
    }
    assert state_matches(state, "181", "1", "2026/05/24", _canon,
                         doc_name="王小明", session_cn="早上")
    assert not state_matches(state, "182", "1", "2026/05/24", _canon)

    states = {"a": state, "b": {"date": "2026/05/23"}, "c": "bad"}
    assert prune_states_for_today(states, "2026/05/24") == {"a": state}
    assert matching_state_keys(states, "181", "1", "王小明") == ["a"]


def test_new_clinic_tracker_has_date_field():
    """[2026-06-22] tracker 需有 date 欄位供主程式跨日重置;新建時為 None。"""
    t = new_clinic_tracker("早上", 1000.0)
    assert "date" in t
    assert t["date"] is None


def test_restore_tracker_carries_date_from_state():
    """還原時帶入 state 的 date(state 只在=今日才會被還原),避免主程式誤判跨日而重置。"""
    def _canon(v):
        return {"上午": "早上"}.get(str(v or "").strip(), str(v or "").strip())
    state = {"date": "2026/06/22", "doctor": "沈冠宇", "session": "早上"}
    t = restore_tracker_from_state(state, "早上", 1000.0, _canon)
    assert t["date"] == "2026/06/22"
    # 壞 state(非 dict)→ 回新 tracker(date=None)
    assert restore_tracker_from_state(None, "早上", 1000.0, _canon)["date"] is None


def test_is_ended_persists_round_trip():
    """[2026-06-22] is_ended 需與 actual_closing_dt 一起持久化(原本漏存,跨重啟會掉)。"""
    t = new_clinic_tracker("早上", 1000.0)
    assert t["is_ended"] is False
    t["is_ended"] = True
    state = build_dynamic_state(
        "2026/06/22", "2026-06-22T11:00:00", "101", "1", "早上", "沈冠宇",
        t, {"light": "5"}, current_timestamp=1000.0)
    assert state["is_ended"] is True

    def _canon(v):
        return {"上午": "早上"}.get(str(v or "").strip(), str(v or "").strip())
    restored = restore_tracker_from_state(state, "早上", 1000.0, _canon)
    assert restored["is_ended"] is True
