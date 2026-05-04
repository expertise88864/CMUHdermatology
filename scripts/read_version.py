# -*- coding: utf-8 -*-
"""讀 src/cmuh_common/version.py 的 CURRENT_VERSION 並印出。"""
import re
import sys
from pathlib import Path

f = Path(__file__).resolve().parent.parent / "src" / "cmuh_common" / "version.py"
if not f.exists():
    sys.exit(1)
m = re.search(r'CURRENT_VERSION\s*=\s*["\']([\d.]+)["\']', f.read_text(encoding="utf-8"))
if not m:
    sys.exit(1)
print(m.group(1))
