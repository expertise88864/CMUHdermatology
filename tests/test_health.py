# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import health  # noqa: E402


def test_normalize_health_monitor_args_falls_back_for_invalid_values():
    assert health._normalize_health_monitor_args(
        "bad", None, object(), "bad"
    ) == (400.0, 800.0, 300, 6)


def test_normalize_health_monitor_args_enforces_safe_lower_bounds():
    assert health._normalize_health_monitor_args(
        500, 100, 0, -2
    ) == (500.0, 500.0, 5, 1)
