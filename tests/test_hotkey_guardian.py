# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.hotkey_guardian import (  # noqa: E402
    is_hook_probe_failure_confirmed,
    should_auto_restart_for_dead_hook,
    should_bypass_foreground_guard,
    should_emit_interrupt,
    should_emit_idle_status,
    should_probe_hook_health,
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


def test_interrupt_emits_only_when_automation_is_running():
    assert should_emit_interrupt(True) is True
    assert should_emit_interrupt(False) is False
    assert should_emit_interrupt(True, stop_already_requested=True) is False


def test_idle_status_emits_only_for_latest_finished_worker():
    assert should_emit_idle_status(
        3, 3, subsystem_running=False,
    ) is True
    assert should_emit_idle_status(
        4, 3, subsystem_running=False,
    ) is False
    assert should_emit_idle_status(
        3, 3, subsystem_running=True,
    ) is False


# ─── 健康監看：安靜夠久才探針 ──────────────────────────────────────────────

def test_probe_skipped_while_recent_key_events():
    assert should_probe_hook_health(5.0) is False
    assert should_probe_hook_health(149.0) is False


def test_probe_runs_after_quiet_window():
    assert should_probe_hook_health(150.0) is True
    assert should_probe_hook_health(600.0) is True


def test_probe_custom_threshold_and_bad_input():
    assert should_probe_hook_health(40.0, quiet_threshold_sec=30.0) is True
    assert should_probe_hook_health(20.0, quiet_threshold_sec=30.0) is False
    assert should_probe_hook_health(None) is True  # type: ignore[arg-type]


# ─── 健康監看：需連續多次探針未回應才算確認失效 ─────────────────────────────

def test_failure_needs_consecutive_misses():
    assert is_hook_probe_failure_confirmed(0) is False
    assert is_hook_probe_failure_confirmed(1) is False
    assert is_hook_probe_failure_confirmed(2) is True
    assert is_hook_probe_failure_confirmed(5) is True


def test_failure_threshold_floor_is_one():
    assert is_hook_probe_failure_confirmed(1, threshold=0) is True
    assert is_hook_probe_failure_confirmed(0, threshold=0) is False


# ─── 健康監看：自動重啟的所有 guard ───────────────────────────────────────

def _restart_kwargs(**over):
    base = dict(
        hook_dead=True,
        shutting_down=False,
        subsystem_running=False,
        modules_ready=True,
        system_idle_sec=10.0,
        seconds_since_last_restart=1e9,
        restarts_this_session=0,
    )
    base.update(over)
    return base


def test_restart_allowed_when_idle_and_dead():
    assert should_auto_restart_for_dead_hook(**_restart_kwargs()) is True


def test_restart_blocked_by_each_guard():
    assert should_auto_restart_for_dead_hook(**_restart_kwargs(hook_dead=False)) is False
    assert should_auto_restart_for_dead_hook(**_restart_kwargs(modules_ready=False)) is False
    assert should_auto_restart_for_dead_hook(**_restart_kwargs(shutting_down=True)) is False
    # 自動化執行中不可被重啟打斷
    assert should_auto_restart_for_dead_hook(**_restart_kwargs(subsystem_running=True)) is False


def test_restart_waits_for_idle():
    assert should_auto_restart_for_dead_hook(**_restart_kwargs(system_idle_sec=1.0)) is False
    assert should_auto_restart_for_dead_hook(**_restart_kwargs(system_idle_sec=3.0)) is True


def test_restart_respects_cooldown_and_session_cap():
    assert should_auto_restart_for_dead_hook(
        **_restart_kwargs(seconds_since_last_restart=120.0)) is False
    assert should_auto_restart_for_dead_hook(
        **_restart_kwargs(seconds_since_last_restart=300.0)) is True
    assert should_auto_restart_for_dead_hook(**_restart_kwargs(restarts_this_session=3)) is False
    assert should_auto_restart_for_dead_hook(**_restart_kwargs(restarts_this_session=2)) is True


def test_restart_bad_numeric_input_is_safe():
    assert should_auto_restart_for_dead_hook(
        **_restart_kwargs(system_idle_sec=None)) is False  # type: ignore[arg-type]
