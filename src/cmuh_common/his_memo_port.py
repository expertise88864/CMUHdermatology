"""Narrow HIS memo boundary for the main program's phototherapy workflow.

The protocol contains I/O only. Dose parsing, clinical decisions, warnings, and
the required read-back verification remain in the caller.
"""

import re
from typing import Protocol


class HisMemoPort(Protocol):
    def find_main_window(self) -> int: ...

    def locate_phototherapy(self, main_hwnd: int) -> tuple[int, str]: ...

    def read_memo(self, memo_hwnd: int) -> str: ...

    def write_memo(self, memo_hwnd: int, text: str) -> bool: ...

def memo_written_back_intact(original: str, proposed: str, actual: str) -> bool:
    """Verify every line while allowing spacing around edited-line tokens.

    Numeric and word tokens must remain intact: ``850`` must not compare equal
    to ``8 50``. Unedited history lines must match exactly.
    """
    def tokens(line: str) -> list[str]:
        return re.findall(r"\d+(?:[.,]\d+)*|[^\W\d_]+|_+|[^\w\s]",
                          line.casefold())

    before = original.splitlines()
    intended = proposed.splitlines()
    observed = actual.splitlines()
    if len(before) != len(intended) or len(intended) != len(observed):
        return False
    for old, want, got in zip(before, intended, observed, strict=True):
        if old == want:
            if got != want:
                return False
        elif tokens(got) != tokens(want):
            return False
    return True
