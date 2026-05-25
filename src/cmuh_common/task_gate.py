# -*- coding: utf-8 -*-
"""Small helpers to prevent duplicate background tasks from piling up."""
from __future__ import annotations

import threading
from collections.abc import Hashable


class ActiveTaskGate:
    """Track active task keys across background workers.

    The gate is intentionally tiny: callers acquire before starting a worker and
    release in the worker's finally block. If the worker hangs, later ticks skip
    instead of creating an unbounded queue of blocked threads.
    """

    def __init__(self) -> None:
        self._active: set[Hashable] = set()
        self._lock = threading.Lock()

    def acquire(self, key: Hashable) -> bool:
        with self._lock:
            if key in self._active:
                return False
            self._active.add(key)
            return True

    def release(self, key: Hashable) -> None:
        with self._lock:
            self._active.discard(key)

    def is_active(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._active
