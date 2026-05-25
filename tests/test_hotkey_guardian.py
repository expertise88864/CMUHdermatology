# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.hotkey_guardian import should_rehook_hotkeys  # noqa: E402


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
