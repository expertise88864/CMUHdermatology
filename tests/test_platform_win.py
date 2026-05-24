# -*- coding: utf-8 -*-
"""platform_win helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.platform_win import _admin_relaunch_params  # noqa: E402


def test_admin_relaunch_params_quotes_paths_with_spaces():
    params = _admin_relaunch_params([
        r"C:\Program Files\CMUH App\main.pyw",
        "--mode",
        "clock config",
    ])

    assert '"C:\\Program Files\\CMUH App\\main.pyw"' in params
    assert '--mode' in params
    assert '"clock config"' in params
