# -*- coding: utf-8 -*-
"""appt_utils helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.appt_utils import appointment_data_count  # noqa: E402


def test_appointment_data_count_ignores_error_payloads_and_bad_rows():
    assert appointment_data_count({"error": "timeout"}) == 0
    assert appointment_data_count(["bad"]) == 0
    assert appointment_data_count({
        "2026-05-24": [{"session": "上午"}, {"session": "下午"}],
        "bad": "not list",
    }) == 2
