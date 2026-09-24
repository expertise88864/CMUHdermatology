"""Narrow HIS memo boundary for the main program's phototherapy workflow.

The protocol contains I/O only. Dose parsing, clinical decisions, warnings, and
the required read-back verification remain in the caller.
"""

from typing import Protocol


class HisMemoPort(Protocol):
    def find_main_window(self) -> int: ...

    def locate_phototherapy(self, main_hwnd: int) -> tuple[int, str]: ...

    def read_memo(self, memo_hwnd: int) -> str: ...

    def write_memo(self, memo_hwnd: int, text: str) -> bool: ...

    def update_excimer(
        self, main_hwnd: int, memo_hwnd: int, text: str, label: str
    ) -> object: ...
