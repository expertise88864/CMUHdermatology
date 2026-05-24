# -*- coding: utf-8 -*-
"""clinic_light_history helpers."""
from datetime import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.clinic_light_history import (  # noqa: E402
    historical_light_average,
    light_bucket_label,
    light_bucket_minute,
    light_history_key,
    record_light_sample,
)


def test_light_bucket_helpers():
    when = datetime(2026, 5, 24, 10, 5)

    assert light_bucket_minute(when) == 603
    assert light_bucket_label(603) == "10:03"
    assert light_history_key("王小明", "181", "早上", 603) == "王小明|181|早上|10:03"


def test_record_light_sample_replaces_same_day_and_prunes_key_rows():
    when = datetime(2026, 5, 24, 10, 5)
    key = light_history_key("王小明", "181", "早上", 603)
    data = {
        key: [
            {"date": "2026/05/24", "light": 2},
            {"date": "2026/03/01", "light": 9},
            {"date": "2026/05/20", "light": 4},
            "bad",
        ]
    }

    updated, changed = record_light_sample(
        data, room_code="181", doc_name="王小明", session_key="早上",
        light_val="5", when=when, retain_days=60)

    assert changed is True
    assert updated[key] == [
        {"date": "2026/05/20", "light": 4},
        {"date": "2026/05/24", "light": 5},
    ]


def test_record_light_sample_rejects_bad_light_value():
    data = {}
    updated, changed = record_light_sample(
        data, room_code="181", doc_name="王小明", session_key="早上",
        light_val="bad", when=datetime(2026, 5, 24, 10, 5), retain_days=60)

    assert changed is False
    assert updated is data


def test_historical_light_average_prefers_same_weekday_samples():
    when = datetime(2026, 5, 24, 10, 5)  # Sunday
    key = light_history_key("王小明", "181", "早上", 603)
    data = {
        key: [
            {"date": "2026/05/17", "light": 6},
            {"date": "2026/05/10", "light": 9},
            {"date": "2026/05/03", "light": 12},
            {"date": "2026/05/20", "light": 99},
            {"date": "2026/05/24", "light": 1},
        ]
    }

    assert historical_light_average(
        data, room_code="181", doc_name="王小明", session_key="早上",
        when=when, history_days=30, window_minutes=9) == "~9"


def test_historical_light_average_trims_when_enough_samples():
    when = datetime(2026, 5, 24, 10, 5)
    key = light_history_key("王小明", "181", "早上", 603)
    rows = [
        {"date": "2026/05/20", "light": val}
        for val in [1, 10, 10, 10, 10, 10, 10, 10, 10, 100]
    ]

    assert historical_light_average(
        {key: rows}, room_code="181", doc_name="王小明",
        session_key="早上", when=when, history_days=30,
        window_minutes=9) == "~10"
