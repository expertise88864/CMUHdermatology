# -*- coding: utf-8 -*-
"""bump src/cmuh_common/version.py 的 CURRENT_VERSION（YYYY.MM.DD.serial）。"""
import re
import sys
from datetime import datetime
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "src" / "cmuh_common" / "version.py"

def main() -> int:
    if not VERSION_FILE.exists():
        print(f"[錯誤] 找不到 {VERSION_FILE}", file=sys.stderr)
        return 1
    content = VERSION_FILE.read_text(encoding="utf-8")
    m = re.search(r'CURRENT_VERSION\s*=\s*["\']([\d.]+)["\']', content)
    if not m:
        print("[錯誤] 找不到 CURRENT_VERSION", file=sys.stderr)
        return 1
    old = m.group(1)
    parts = old.split(".")
    today = datetime.now().strftime("%Y.%m.%d")
    if len(parts) >= 4 and ".".join(parts[:3]) == today:
        try:
            new_serial = int(parts[3]) + 1
        except ValueError:
            new_serial = 1
        new = f"{today}.{new_serial}"
    else:
        new = f"{today}.1"
    new_content = re.sub(
        r'(CURRENT_VERSION\s*=\s*["\'])([\d.]+)(["\'])',
        rf'\g<1>{new}\g<3>', content, count=1)
    VERSION_FILE.write_text(new_content, encoding="utf-8")
    print(f"[bump] {old} -> {new}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
