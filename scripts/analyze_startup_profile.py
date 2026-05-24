# -*- coding: utf-8 -*-
r"""把 `python -X importtime` 的 stderr 解析，列出最慢的 20 個 import。

用法：python scripts\analyze_startup_profile.py settings\startup_profile.txt
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LINE_RE = re.compile(r'^import time:\s+(\d+)\s*\|\s+(\d+)\s*\|\s+(.*)$')


def main(argv: list) -> int:
    if len(argv) < 2:
        print("usage: analyze_startup_profile.py <path-to-importtime-log>")
        return 2

    path = Path(argv[1])
    if not path.exists():
        print(f"[error] file not found: {path}")
        return 1

    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE_RE.match(line)
            if not m:
                continue
            self_us, cum_us, name = m.groups()
            rows.append((int(cum_us), int(self_us), name.rstrip()))

    if not rows:
        print("[error] no importtime rows recognized — was main.py run with `python -X importtime`?")
        return 1

    rows.sort(reverse=True)
    total_cum = sum(s for _, s, _ in rows)  # sum of self_us is total wall
    print(f"\nTotal import wall time (sum of self): {total_cum/1000:,.0f} ms ({total_cum/1_000_000:.2f} s)")
    print()
    print(f"{'cum_ms':>10} {'self_ms':>10}  module")
    print("-" * 80)
    for c, s, n in rows[:30]:
        print(f"{c/1000:>10,.1f} {s/1000:>10,.1f}  {n}")

    print()
    print(f"shown: top 30 of {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
