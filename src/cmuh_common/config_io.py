# -*- coding: utf-8 -*-
"""Settings JSON helpers.

Small wrapper around atomic_io so large entry scripts do not each reimplement
the same "load JSON, validate type, merge defaults" pattern.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

from cmuh_common.atomic_io import safe_load_json


def clone_default(default: Any) -> Any:
    """Return an independent copy of a default config object."""
    return copy.deepcopy(default)


def load_json_dict(path: str, default: dict | None = None, *,
                   merge_defaults: bool = True) -> dict:
    """Load a JSON object from path, falling back to default.

    Corrupt JSON is handled by safe_load_json, which backs up the bad file.
    If merge_defaults is true, missing top-level keys are filled from default.
    """
    base = clone_default(default or {})
    data = safe_load_json(path, default=None)
    if not isinstance(data, dict):
        return base
    if not merge_defaults:
        return data
    base.update(data)
    return base


def load_json_list(path: str, default: list | None = None) -> list:
    """Load a JSON list from path, falling back to a copied default list."""
    data = safe_load_json(path, default=None)
    if isinstance(data, list):
        return data
    if data is not None:
        logging.warning("[config_io] %s 不是 list，改用預設值", path)
    return clone_default(default or [])


def normalize_doctor_rows(rows: list, default: list | None = None) -> tuple[list, bool]:
    """Normalize doctor settings rows.

    Returns (rows, changed). Repairs the historical name/doc_no swap where
    doc_no contains Chinese text or name looks like a D-number.
    """
    normalized = []
    changed = False
    for row in rows:
        if not isinstance(row, dict):
            changed = True
            continue
        name = str(row.get('name', '')).strip()
        doc_no = str(row.get('doc_no', '')).strip()
        if (any('\u4e00' <= char <= '\u9fff' for char in doc_no)) or \
           (name.startswith('D') and name[1:].isdigit()):
            logging.warning(
                "Data corruption detected for %s/%s. Swapping back.",
                name, doc_no)
            name, doc_no = doc_no, name
            changed = True
        fixed = dict(row)
        if fixed.get('name') != name or fixed.get('doc_no') != doc_no:
            changed = True
        fixed['name'] = name
        fixed['doc_no'] = doc_no
        normalized.append(fixed)
    if normalized:
        return normalized, changed
    return clone_default(default or []), True
