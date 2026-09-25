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
    """Verify every line while allowing HIS whitespace changes on edited lines.

    Existing phototherapy read-back checks validate clinical fields. This also
    protects unrelated history from disappearing after a whole-memo write.
    """
    before = original.splitlines()
    intended = proposed.splitlines()
    observed = actual.splitlines()
    if len(before) != len(intended) or len(intended) != len(observed):
        return False
    for old, want, got in zip(before, intended, observed, strict=True):
        if old == want:
            if got != want:
                return False
        elif re.sub(r"\s+", "", got).casefold() != re.sub(
                r"\s+", "", want).casefold():
            return False
    return True
