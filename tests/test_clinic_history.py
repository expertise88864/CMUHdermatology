# -*- coding: utf-8 -*-
"""clinic_history helpers."""
from datetime import date
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.clinic_history import (  # noqa: E402
    all_time_average_text,
    duration_stats,
    historical_duration_totals,
    last_closing_time,
    monthly_slot_metric_avgs,
    prev_session_closing_clock,
    remove_doctor_history,
    upsert_session_stat,
)


def _canon(value):
    return {"上午": "早上"}.get(str(value or ""), str(value or ""))


def test_duration_stats_trims_large_outlier():
    durations, valid, avg_min = duration_stats([100, 100, 100, 100, 600])

    assert durations == [100.0, 100.0, 100.0, 100.0, 600.0]
    assert valid == [100.0, 100.0, 100.0, 100.0]
    assert avg_min == 1.7


def test_upsert_session_stat_updates_existing_row_with_empty_closing_sample():
    history = [{
        "date": "2026/05/24", "week": "20", "room": "181",
        "session": "上午", "doctor": "王小明", "closing_time": "",
    }]

    rows, changed = upsert_session_stat(
        history, today_str="2026/05/24", week_str="20", room_code="181",
        doc_name="王小明", completed_count=0, durations=[],
        session="早上", closing_time="12:30", total_reg=None,
        canonical_session=_canon)

    assert changed is True
    assert len(rows) == 1
    assert rows[0]["session"] == "早上"
    assert rows[0]["closing_time"] == "12:30"
    assert rows[0].get("raw_sample_count", 0) == 0


def test_upsert_session_stat_inserts_weighted_sample():
    rows, changed = upsert_session_stat(
        [], today_str="2026/05/24", week_str="20", room_code="181",
        doc_name="王小明", completed_count=3, durations=[60, 120],
        session="早上", total_reg=10, phototherapy=2,
        canonical_session=_canon)

    assert changed is True
    assert rows[0]["avg_time_min"] == 1.5
    assert rows[0]["valid_sample_count"] == 2
    assert rows[0]["total_reg"] == 10
    assert rows[0]["phototherapy"] == 2


def test_history_lookup_helpers():
    history = [
        {"date": "2026/05/17", "room": "181", "session": "早上",
         "doctor": "王小明", "closing_time": "12:00",
         "total_reg": 10, "completed_count": 8, "phototherapy": 2},
        {"date": "2026/05/24", "room": "181", "session": "早上",
         "doctor": "王小明", "closing_time": "12:30",
         "total_reg": 12, "completed_count": 10, "phototherapy": 4},
        "bad",
    ]

    assert last_closing_time(history, "王小明", 6, "上午", _canon) == "12:30"
    assert prev_session_closing_clock(
        history, "181", "王小明", "早上", "2026/05/24", _canon) == "12:30"
    assert monthly_slot_metric_avgs(
        history, "王小明", "181", "上午", date(2026, 5, 1), _canon
    ) == ("11", "9", "3")
    assert remove_doctor_history(history, "王小明") == []


def test_historical_duration_average_text():
    history = [
        {"date": "2026/05/20", "doctor": "王小明",
         "avg_time_min": 2, "valid_sample_count": 2},
        {"date": "2026/04/01", "doctor": "王小明",
         "avg_time_min": 99, "valid_sample_count": 1},
    ]

    totals = historical_duration_totals(history, "王小明", date(2026, 5, 1))
    assert totals == (4.0, 2)
    assert all_time_average_text(totals, [60, 60]) == "1.5"
