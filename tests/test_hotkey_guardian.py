# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.hotkey_guardian import (  # noqa: E402
    should_bypass_foreground_guard,
    should_rehook_hotkeys,
    should_show_busy_notice,
)


def test_hotkey_guardian_rehooks_when_ready_and_idle():
    assert should_rehook_hotkeys(
        True,
        shutting_down=False,
        subsystem_running=False,
        modules_ready=True,
    ) is True


def test_hotkey_guardian_skips_unsafe_states():
    assert should_rehook_hotkeys(
        False,
        shutting_down=False,
        subsystem_running=False,
        modules_ready=True,
    ) is False
    assert should_rehook_hotkeys(
        True,
        shutting_down=True,
        subsystem_running=False,
        modules_ready=True,
    ) is False
    assert should_rehook_hotkeys(
        True,
        shutting_down=False,
        subsystem_running=True,
        modules_ready=True,
    ) is False
    assert should_rehook_hotkeys(
        True,
        shutting_down=False,
        subsystem_running=False,
        modules_ready=False,
    ) is False


def test_f12_bypasses_foreground_guard_only_while_automation_runs():
    assert should_bypass_foreground_guard(
        "F12", subsystem_running=True,
    ) is True
    assert should_bypass_foreground_guard(
        "F12", subsystem_running=False,
    ) is False
    assert should_bypass_foreground_guard(
        "F11", subsystem_running=True,
    ) is False


def test_busy_notice_is_throttled():
    assert should_show_busy_notice(100.0, 0.0) is True
    assert should_show_busy_notice(101.0, 100.0) is False
    assert should_show_busy_notice(102.5, 100.0) is True


def test_busy_notice_tolerates_bad_timestamps():
    assert should_show_busy_notice("bad", 100.0) is True
