# -*- coding: utf-8 -*-
"""GUI-free parsing and lookup for runtime dependency manifests."""
from __future__ import annotations

import logging
import os
import re

from cmuh_common.paths import get_app_dir


_REQ_SPECS_CACHE: dict[str, str] | None = None
_REQUIREMENTS_FILES = ("requirements.txt", "requirements-lazy.txt")


def _normalize_pkg(name: str) -> str:
    """Normalize a pip distribution name using PEP 503-style rules."""
    return re.sub(r"[-_.]+", "-", str(name).strip().lower())


def _load_requirement_specs() -> dict[str, str]:
    """Read runtime manifests as ``normalized_name -> full requirement``."""
    global _REQ_SPECS_CACHE
    if _REQ_SPECS_CACHE is not None:
        return _REQ_SPECS_CACHE

    specs: dict[str, str] = {}
    sources: dict[str, str] = {}
    for filename in _REQUIREMENTS_FILES:
        req_path = os.path.join(get_app_dir(), filename)
        if not os.path.exists(req_path):
            continue
        try:
            with open(req_path, "r", encoding="utf-8") as req_file:
                for raw in req_file:
                    line = raw.split("#", 1)[0].strip()
                    if not line:
                        continue
                    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
                    if not match:
                        continue
                    key = _normalize_pkg(match.group(1))
                    previous = specs.get(key)
                    if previous is not None:
                        if previous != line:
                            logging.warning(
                                "[deps] conflicting requirements for %s: %s (%s) vs %s (%s); "
                                "keeping the first declaration",
                                key, previous, sources[key], line, filename,
                            )
                        continue
                    specs[key] = line
                    sources[key] = filename
        except Exception:
            logging.debug("[deps] failed to read %s", req_path, exc_info=True)

    _REQ_SPECS_CACHE = specs
    return specs


def _resolve_pip_spec(pkg_name: str) -> str:
    """Return the manifest constraint for a package, or its input unchanged."""
    specs = _load_requirement_specs()
    return specs.get(_normalize_pkg(pkg_name), pkg_name)
