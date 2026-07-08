# -*- coding: utf-8 -*-
"""platform_win helpers."""
import ctypes
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.platform_win import (  # noqa: E402
    MonitorRect,
    _DISPLAY_DEVICEW,
    _admin_relaunch_params,
    _display_device_has_physical_monitor_id,
    _display_device_is_mirror_driver,
    _shell_execute_succeeded,
    choose_preferred_monitor,
    foreground_window_on_primary,
    get_active_physical_monitors,
    get_monitor_count,
    place_tk_window_on_preferred_monitor,
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


def test_active_physical_monitors_returns_list():
    assert isinstance(get_active_physical_monitors(), list)


def test_choose_preferred_monitor_uses_primary_when_available():
    # [2026-07-08 使用者需求] 改為主螢幕優先（原本偏好副螢幕）。
    primary = MonitorRect(0, 0, 1920, 1080, True)
    secondary = MonitorRect(-1920, 0, 1920, 1080, False)

    assert choose_preferred_monitor([primary, secondary]) == primary


def test_choose_preferred_monitor_falls_back_to_primary():
    primary = MonitorRect(0, 0, 1920, 1080, True)

    assert choose_preferred_monitor([primary]) == primary


def test_choose_preferred_monitor_prefers_primary_over_larger_secondary():
    # 即使副螢幕更大，也一律用主螢幕。
    primary = MonitorRect(0, 0, 1920, 1080, True)
    small = MonitorRect(1920, 0, 1280, 720, False)
    large = MonitorRect(-2560, 0, 2560, 1440, False)

    assert choose_preferred_monitor([small, primary, large]) == primary


def test_choose_preferred_monitor_no_primary_falls_back_to_largest():
    # 極端情形：偵測不到主螢幕旗標 → 退回最大的可用螢幕（不致回 None）。
    a = MonitorRect(0, 0, 1280, 720, False)
    b = MonitorRect(1280, 0, 2560, 1440, False)

    assert choose_preferred_monitor([a, b]) == b


def test_display_device_mirror_driver_is_excluded():
    class FakeUser32:
        @staticmethod
        def EnumDisplayDevicesW(_name, _index, device_ptr, _flags):
            device = ctypes.cast(
                device_ptr, ctypes.POINTER(_DISPLAY_DEVICEW),
            ).contents
            device.StateFlags = 0x00000008
            return 1

    assert _display_device_is_mirror_driver(FakeUser32(), r"\\.\DISPLAY9")


def test_display_device_physical_monitor_requires_monitor_pnp_id():
    class FakeUser32:
        device_id = r"MONITOR\REAL123"

        @classmethod
        def EnumDisplayDevicesW(cls, _name, _index, device_ptr, _flags):
            device = ctypes.cast(
                device_ptr, ctypes.POINTER(_DISPLAY_DEVICEW),
            ).contents
            device.DeviceID = cls.device_id
            return 1

    assert _display_device_has_physical_monitor_id(
        FakeUser32(), r"\\.\DISPLAY1",
    ) is True

    FakeUser32.device_id = r"ROOT\VIRTUALDISPLAY"
    assert _display_device_has_physical_monitor_id(
        FakeUser32(), r"\\.\DISPLAY9",
    ) is False


def test_place_tk_window_moves_and_maximizes_visible_window(monkeypatch):
    target = MonitorRect(1920, 0, 1920, 1080, False)
    moved = []

    class FakeRoot:
        state_value = "normal"

        def geometry(self, _value):
            raise AssertionError("fallback should not be needed")

        def state(self, value=None):
            if value is not None:
                self.state_value = value
            return self.state_value

    root = FakeRoot()
    monkeypatch.setattr(
        "cmuh_common.platform_win.get_preferred_monitor_rect",
        lambda: target,
    )
    monkeypatch.setattr(
        "cmuh_common.platform_win.move_tk_window_to_monitor",
        lambda _root, monitor: moved.append(monitor) or True,
    )

    assert place_tk_window_on_preferred_monitor(root) == target
    assert moved == [target]
    assert root.state_value == "zoomed"


def test_place_tk_window_does_not_unhide_withdrawn_window(monkeypatch):
    target = MonitorRect(-1920, 0, 1920, 1080, False)

    class FakeRoot:
        state_value = "withdrawn"

        def geometry(self, _value):
            raise AssertionError("fallback should not be needed")

        def state(self, value=None):
            if value is not None:
                self.state_value = value
            return self.state_value

    root = FakeRoot()
    monkeypatch.setattr(
        "cmuh_common.platform_win.get_preferred_monitor_rect",
        lambda: target,
    )
    monkeypatch.setattr(
        "cmuh_common.platform_win.move_tk_window_to_monitor",
        lambda _root, _monitor: True,
    )

    place_tk_window_on_preferred_monitor(root)

    assert root.state_value == "withdrawn"


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
