# -*- coding: utf-8 -*-
"""Verify that the interpreter used by setup can import runtime dependencies."""
from __future__ import annotations

import importlib
import sys


REQUIRED_IMPORTS = (
    "requests",
    "bs4",
    "lxml",
    "selenium",
    "keyboard",
    "pyautogui",
    "schedule",
    "psutil",
    "PIL",
    "pystray",
    "win32gui",
    "sv_ttk",
    "webdriver_manager",
    "winotify",
)


def find_import_failures(import_names=REQUIRED_IMPORTS) -> list[tuple[str, str]]:
    failures = []
    for import_name in import_names:
        try:
            importlib.import_module(import_name)
        except Exception as exc:
            failures.append((import_name, f"{type(exc).__name__}: {exc}"))
    return failures


def main() -> int:
    print(f"[verify] interpreter: {sys.executable}")
    print(f"[verify] Python: {sys.version.split()[0]}")
    failures = find_import_failures()
    if failures:
        print("[verify] FAILED imports:")
        for import_name, detail in failures:
            print(f"  - {import_name}: {detail}")
        return 1
    print(f"[verify] OK: {len(REQUIRED_IMPORTS)} imports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
