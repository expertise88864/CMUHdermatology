# -*- coding: utf-8 -*-
"""reg64_utils helpers."""
from datetime import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.reg64_utils import (  # noqa: E402
    canonical_clinic_session_str,
    clinic_int_count,
    prev_session_cn,
    reg64_clinic_quiet_hours,
    reg64_next_allowed_fetch_time,
    reg64_time_code_from_local_clock,
    session_boundary_datetime,
)


def test_time_code_split_points_13_00_and_17_30():
    """[2026-06-19 user] 切換點:13:00 轉下午、17:30 轉晚上(原 13:30 / 18:00)。"""
    d = lambda h, m: datetime(2026, 6, 19, h, m)  # noqa: E731
    assert reg64_time_code_from_local_clock(d(8, 0)) == "1"    # 早上
    assert reg64_time_code_from_local_clock(d(12, 59)) == "1"  # 12:59 仍早上
    assert reg64_time_code_from_local_clock(d(13, 0)) == "2"   # 13:00 起下午
    assert reg64_time_code_from_local_clock(d(13, 29)) == "2"  # 舊邊界內仍下午
    assert reg64_time_code_from_local_clock(d(17, 29)) == "2"  # 17:29 仍下午
    assert reg64_time_code_from_local_clock(d(17, 30)) == "3"  # 17:30 起晚上
    assert reg64_time_code_from_local_clock(d(18, 0)) == "3"   # 晚上


def test_canonical_clinic_session_str_and_previous_session():
    assert canonical_clinic_session_str("上午") == "早上"
    assert canonical_clinic_session_str("早診") == "早上"
    assert canonical_clinic_session_str("午診") == "下午"
    assert canonical_clinic_session_str("晚診") == "晚上"
    assert prev_session_cn("下午") == "早上"
    assert prev_session_cn("晚上") == "下午"
    assert prev_session_cn("早上") is None


def test_session_boundary_accepts_morning_aliases():
    now = datetime(2026, 5, 25, 9, 30)

    assert session_boundary_datetime("上午", now) == datetime(2026, 5, 25, 12, 0)
    assert session_boundary_datetime("早上", now) == datetime(2026, 5, 25, 12, 0)
    assert session_boundary_datetime("下午", now) == datetime(2026, 5, 25, 17, 0)
    assert session_boundary_datetime("晚上", now) == datetime(2026, 5, 25, 21, 0)


def test_clinic_int_count_rejects_non_integral_values():
    assert clinic_int_count("12") == 12
    assert clinic_int_count(12.0) == 12
    assert clinic_int_count(12.5, -1) == -1
    assert clinic_int_count(True, -1) == -1
    assert clinic_int_count("--", -1) == -1


def test_reg64_quiet_hours_and_next_allowed_time():
    early = datetime(2026, 5, 25, 7, 59)
    allowed = datetime(2026, 5, 25, 8, 1)

    assert reg64_clinic_quiet_hours(early)
    assert not reg64_clinic_quiet_hours(allowed)
    assert reg64_next_allowed_fetch_time(early) == datetime(2026, 5, 25, 8, 0)
    assert reg64_next_allowed_fetch_time(allowed) == allowed
