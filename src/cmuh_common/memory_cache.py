# -*- coding: utf-8 -*-
"""Small helpers for bounding long-lived in-memory state maps."""
from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import Any


def trim_oldest_entries(
    store: MutableMapping[Any, Any],
    max_entries: int,
    *,
    timestamp_of: Callable[[Any], float] | None = None,
) -> int:
    """Remove oldest rows until ``store`` fits within ``max_entries``."""
    limit = max(1, int(max_entries))
    excess = len(store) - limit
    if excess <= 0:
        return 0

    def sort_key(item) -> float:
        value = item[1]
        try:
            stamp = timestamp_of(value) if timestamp_of else value[0]
            return float(stamp)
        except (IndexError, KeyError, TypeError, ValueError):
            return float("-inf")

    oldest = sorted(store.items(), key=sort_key)[:excess]
    for key, _value in oldest:
        store.pop(key, None)
    return len(oldest)
