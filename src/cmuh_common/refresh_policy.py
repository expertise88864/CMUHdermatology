# -*- coding: utf-8 -*-
"""Refresh batching policy helpers."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def partition_doctors_for_refresh_batches(
    doctors: Sequence[Any] | None,
    *,
    first_batch_names: Iterable[str],
    second_batch_names: Iterable[str],
) -> list[list[Mapping[str, Any]]]:
    """Partition doctors into priority refresh batches.

    Malformed rows are ignored so one bad config entry does not stop the whole
    refresh cycle.
    """
    if not doctors:
        return []

    valid_doctors = [
        d
        for d in doctors
        if isinstance(d, Mapping) and isinstance(d.get("name"), str) and d.get("name")
    ]
    by_name = {d["name"]: d for d in valid_doctors}

    batch1_names = tuple(first_batch_names)
    batch2_names = tuple(second_batch_names)
    b1 = [by_name[name] for name in batch1_names if name in by_name]
    b2 = [by_name[name] for name in batch2_names if name in by_name]
    fixed = set(batch1_names) | set(batch2_names)
    b3 = [d for d in valid_doctors if d.get("name") not in fixed]
    return [batch for batch in (b1, b2, b3) if batch]
