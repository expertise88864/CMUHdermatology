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
