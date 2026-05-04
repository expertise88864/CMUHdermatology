# -*- coding: utf-8 -*-
"""
push.bat 在 commit 前的自檢：
  1. settings/ 不可被 git 追蹤（含密碼）
  2. .gitignore 必須排除 settings/、_originals/、安裝包/、*.log、.deps_cache、python_embed/
  3. cmuh_common.version 可正常 import 並讀到 CURRENT_VERSION
"""
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GITIGNORE = REPO_ROOT / ".gitignore"
VERSION_FILE = REPO_ROOT / "src" / "cmuh_common" / "version.py"

REQUIRED_IGNORE = [
    "settings/", "_originals/", "安裝包/", "*.log",
    ".deps_cache", "python_embed/", "__pycache__/",
    "*.bak", "build/", "dist/",
]

def fail(msg: str) -> None:
    print(f"[sanity_check 失敗] {msg}", file=sys.stderr)
    sys.exit(1)

def check_gitignore() -> None:
    if not GITIGNORE.exists():
        fail(".gitignore 不存在")
    content = GITIGNORE.read_text(encoding="utf-8")
    missing = [p for p in REQUIRED_IGNORE if p not in content]
    if missing:
        fail(f".gitignore 缺少: {', '.join(missing)}")

def check_settings_not_tracked() -> None:
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "settings/"], cwd=str(REPO_ROOT),
            text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return
    if out.strip():
        fail(f"settings/ 已被追蹤（會把密碼推上 Public repo）：\n{out}")

def check_version_import() -> None:
    if not VERSION_FILE.exists():
        fail("找不到 src/cmuh_common/version.py")
    m = re.search(r'CURRENT_VERSION\s*=\s*["\']([\d.]+)["\']',
                  VERSION_FILE.read_text(encoding="utf-8"))
    if not m:
        fail("version.py 缺少 CURRENT_VERSION")
    print(f"[sanity_check] version={m.group(1)} OK")

if __name__ == "__main__":
    check_gitignore()
    check_settings_not_tracked()
    check_version_import()
    print("[sanity_check] 全部通過")
