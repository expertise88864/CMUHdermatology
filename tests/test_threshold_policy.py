# -*- coding: utf-8 -*-
"""threshold_policy helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.threshold_policy import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    appt_item_session_and_count_text,
    build_doctor_threshold_map,
    is_near_alert_threshold,
)


def test_build_doctor_threshold_map_uses_defaults_and_overrides():
    thresholds = build_doctor_threshold_map("張廖年峰", {"chang_mon_night": "130"})

    assert thresholds[(0, "晚上")] == 130
    assert thresholds[(3, "上午")] == DEFAULT_THRESHOLDS["chang_thu_morning"]
    assert build_doctor_threshold_map("其他醫師", {}) == {}


def test_appt_item_session_and_count_text_handles_dict_and_legacy_text():
    assert appt_item_session_and_count_text({"session": "上午", "count": 12}) == ("上午", "12人")
    assert appt_item_session_and_count_text("下午: 55人|room=1") == ("下午", "55人")
    assert appt_item_session_and_count_text("bad") == ("", "bad")


def test_is_near_alert_threshold_skips_dayoff_and_bad_rows():
    threshold_map = {(0, "晚上"): 100, (0, "下午"): 50}

    assert is_near_alert_threshold(
        ["晚上: 90人", "上午: 休診", {"session": "下午", "count": "停診"}],
        0,
        threshold_map,
        margin=10,
    )
    assert not is_near_alert_threshold(["晚上: 89人", "bad"], 0, threshold_map, margin=10)
    assert not is_near_alert_threshold(["晚上: 100人"], "bad", threshold_map)
