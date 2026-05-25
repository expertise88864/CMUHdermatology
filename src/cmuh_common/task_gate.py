# -*- coding: utf-8 -*-
"""Small helpers to prevent duplicate background tasks from piling up."""
from __future__ import annotations

import threading
import time
from collections.abc import Hashable
from typing import Callable


class ActiveTaskGate:
    """Track active task keys across background workers.

    The gate is intentionally tiny: callers acquire before starting a worker and
    release in the worker's finally block. If the worker hangs, later ticks skip
    instead of creating an unbounded queue of blocked threads.
    """

    def __init__(
        self,
        stale_after_sec: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._active: dict[Hashable, float] = {}
        self._lock = threading.Lock()
        self._stale_after_sec = stale_after_sec
        self._clock = clock or time.monotonic

    def _is_stale(self, started_at: float, now: float) -> bool:
        return (
            self._stale_after_sec is not None
            and self._stale_after_sec > 0
            and now - started_at >= self._stale_after_sec
        )

    def acquire(self, key: Hashable) -> bool:
        now = self._clock()
        with self._lock:
            started_at = self._active.get(key)
            if started_at is not None and not self._is_stale(started_at, now):
                return False
            self._active[key] = now
            return True

    def release(self, key: Hashable) -> None:
        with self._lock:
            self._active.pop(key, None)

    def is_active(self, key: Hashable) -> bool:
        now = self._clock()
        with self._lock:
            started_at = self._active.get(key)
            if started_at is None:
                return False
            if self._is_stale(started_at, now):
                self._active.pop(key, None)
                return False
            return True

    def active_age_sec(self, key: Hashable) -> float | None:
        now = self._clock()
        with self._lock:
            started_at = self._active.get(key)
            if started_at is None:
                return None
            if self._is_stale(started_at, now):
                self._active.pop(key, None)
                return None
            return max(0.0, now - started_at)
