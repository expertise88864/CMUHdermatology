# -*- coding: utf-8 -*-
"""Pure policy helpers for main hotkey guardian re-hook decisions."""
from __future__ import annotations


def should_rehook_hotkeys(
    has_profile: bool,
    *,
    shutting_down: bool,
    subsystem_running: bool,
    modules_ready: bool,
) -> bool:
    """Return True when the guardian may safely refresh hotkey hooks."""
    return (
        bool(has_profile)
        and not shutting_down
        and not subsystem_running
        and bool(modules_ready)
    )


def should_bypass_foreground_guard(key_name: str, *, subsystem_running: bool) -> bool:
    """Return True for rescue hotkeys that must work while automation is active."""
    return key_name.upper() == "F12" and bool(subsystem_running)


def should_show_busy_notice(
    now: float,
    last_notice_at: float,
    *,
    min_interval_sec: float = 2.5,
) -> bool:
    """Throttle repeated busy notices while users hold or re-press hotkeys."""
    try:
        return float(now) - float(last_notice_at) >= float(min_interval_sec)
    except (TypeError, ValueError):
        return True


def should_emit_interrupt(
    subsystem_running: bool,
    *,
    stop_already_requested: bool = False,
) -> bool:
    """Return True when F12 has an active automation flow to interrupt."""
    return bool(subsystem_running) and not bool(stop_already_requested)


def should_emit_idle_status(
    current_token: int,
    completed_token: int,
    *,
    subsystem_running: bool,
) -> bool:
    """Return True if a finished worker may publish the final idle status."""
    return current_token == completed_token and not subsystem_running
