# -*- coding: utf-8 -*-
"""platform_win helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.platform_win import (  # noqa: E402
    _admin_relaunch_params,
    _shell_execute_succeeded,
    foreground_window_on_primary,
    get_monitor_count,
    get_primary_monitor_size,
    get_virtual_screen_rect,
)


def test_admin_relaunch_params_quotes_paths_with_spaces():
    params = _admin_relaunch_params([
        r"C:\Program Files\CMUH App\main.pyw",
        "--mode",
        "clock config",
    ])

    assert '"C:\\Program Files\\CMUH App\\main.pyw"' in params
    assert '--mode' in params
    assert '"clock config"' in params


def test_shell_execute_requires_success_code_above_32():
    assert _shell_execute_succeeded(33) is True
    assert _shell_execute_succeeded(32) is False
    assert _shell_execute_succeeded(5) is False
    assert _shell_execute_succeeded(None) is False


def test_get_monitor_count_is_positive_int():
    n = get_monitor_count()
    assert isinstance(n, int)
    assert n >= 1


def test_get_primary_monitor_size_returns_pair():
    w, h = get_primary_monitor_size()
    assert isinstance(w, int) and isinstance(h, int)
    assert w >= 0 and h >= 0


def test_get_virtual_screen_rect_has_positive_extent():
    x, y, w, h = get_virtual_screen_rect()
    assert all(isinstance(v, int) for v in (x, y, w, h))
    # 虛擬桌面寬高一定為正；left/top 可能為負(副螢幕在左/上)
    assert w > 0 and h > 0


def test_virtual_screen_covers_primary():
    pw, ph = get_primary_monitor_size()
    x, y, w, h = get_virtual_screen_rect()
    if pw > 0 and ph > 0:
        # 主螢幕 (0,0)-(pw,ph) 必須落在虛擬桌面範圍內
        assert x <= 0 and y <= 0
        assert x + w >= pw and y + h >= ph


def test_foreground_window_on_primary_returns_bool():
    # fail-open：單螢幕/非 Windows/偵測不到時應為 True，但永遠是 bool
    assert isinstance(foreground_window_on_primary(), bool)
