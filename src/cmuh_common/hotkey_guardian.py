# -*- coding: utf-8 -*-
"""Hotkey guardian helpers.

Mostly pure policy decisions (testable on any platform). Also hosts a small
Win32 idle probe (`system_idle_seconds`) used to tell whether the user is
actually active before the guardian auto-restarts to recover a dead hook —
`ctypes` imports fine everywhere; `windll` is only touched at call time.
"""
from __future__ import annotations

import ctypes

# 守護執行緒輪詢間隔（秒）。比舊版 600s 短，讓失效偵測夠即時。
GUARDIAN_INTERVAL_SEC = 60
# 健康探針注入的虛擬鍵：VK_F24（0x87）。一般應用程式不會對 F24 反應，注入無副作用。
PROBE_VK = 0x87


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def system_idle_seconds() -> float:
    """系統層級閒置秒數（Windows GetLastInputInfo）。

    此值由 OS 維護、與本程式的鍵盤 hook 完全無關，可用來判斷「使用者實際上
    有沒有在操作」：守護程式據此只在使用者閒置時才自動重啟，避免打斷。
    取得失敗 / 非 Windows 一律回 0.0（保守視為剛操作過，不會誤判為閒置）。
    """
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        user32 = ctypes.windll.user32          # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32      # type: ignore[attr-defined]
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        # GetTickCount 與 dwTime 同為 32-bit ms；&0xFFFFFFFF 處理 49.7 天回繞。
        tick = kernel32.GetTickCount() & 0xFFFFFFFF
        idle_ms = (tick - (info.dwTime & 0xFFFFFFFF)) & 0xFFFFFFFF
        return max(0.0, idle_ms / 1000.0)
    except Exception:
        return 0.0


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


def should_probe_hook_health(
    hook_silent_sec: float, *, quiet_threshold_sec: float = 150.0,
) -> bool:
    """Return True only once the global keyboard hook has been silent (no real
    key events seen) for a while, so we avoid injecting probe keys while the
    user is actively typing. Recent real events already prove the hook lives.
    """
    try:
        return float(hook_silent_sec) >= float(quiet_threshold_sec)
    except (TypeError, ValueError):
        return True


def is_hook_probe_failure_confirmed(
    consecutive_failed_probes: int, *, threshold: int = 2,
) -> bool:
    """Return True once enough back-to-back health probes have gone unanswered
    that the hook can be treated as dead. A single miss can be a transient
    race; requiring N consecutive misses avoids false positives. Only a process
    restart recovers a Windows LowLevelHooks-timeout removal, so this gates the
    decision to restart.
    """
    try:
        return int(consecutive_failed_probes) >= max(1, int(threshold))
    except (TypeError, ValueError):
        return False


def should_auto_restart_for_dead_hook(
    *,
    hook_dead: bool,
    shutting_down: bool,
    subsystem_running: bool,
    modules_ready: bool,
    system_idle_sec: float,
    seconds_since_last_restart: float,
    restarts_this_session: int,
    idle_required_sec: float = 3.0,
    restart_cooldown_sec: float = 300.0,
    max_restarts: int = 3,
) -> bool:
    """Decide whether the guardian may auto-restart the process to recover a
    confirmed-dead hotkey hook.

    Guards (all must hold):
      - hook confirmed dead and hotkey modules are loaded;
      - not shutting down and no automation currently running;
      - the user is idle (>= idle_required_sec of no system input) so the
        restart is non-disruptive;
      - a cooldown has elapsed since the previous auto-restart;
      - the per-session restart cap has not been reached (avoid restart loops
        when the root cause is something a restart cannot fix).
    """
    if not (hook_dead and modules_ready):
        return False
    if shutting_down or subsystem_running:
        return False
    try:
        if float(system_idle_sec) < float(idle_required_sec):
            return False
        if float(seconds_since_last_restart) < float(restart_cooldown_sec):
            return False
        return int(restarts_this_session) < int(max_restarts)
    except (TypeError, ValueError):
        return False


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
