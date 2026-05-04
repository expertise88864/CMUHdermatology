# -*- coding: utf-8 -*-
"""
sync_manifest.py — 自動掃描 src/ 下所有 .py 並寫入 manifest.json
   - app_version 與所有 entry version 同步
   - 每個 entry 計算 sha256（防下載中斷/MITM）
   - entry 路徑保留 src/ 前綴，方便 updater 直接拼 raw URL
用法: python sync_manifest.py <new_version>
"""
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
MANIFEST = REPO_ROOT / "manifest.json"
GITHUB = "https://github.com/expertise88864/CMUHdermatology"

def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

# 入口檔的 key 對應（其餘子模組以路徑當 key）
ENTRY_KEYS = {
    "src/main.py": "main",
    "src/scheduler.py": "scheduler",
    "src/autoclock.py": "autoclock",
    "src/coord_detector.py": "coord",
}

def collect_entries(version: str) -> list:
    entries = []
    for py in sorted(SRC_DIR.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).as_posix()  # 例: src/cmuh_common/version.py
        key = ENTRY_KEYS.get(rel) or rel.replace("src/", "").replace("/", ".").replace(".py", "")
        entries.append({
            "key": key,
            "remote_path": rel,                    # GitHub 上的路徑
            "local_filename": rel,                 # 本地相對 app_dir 的路徑
            "version": version,
            "sha256": sha256_of(py),
        })
    return entries

def main() -> int:
    if len(sys.argv) < 2:
        print("用法: sync_manifest.py <new_version>", file=sys.stderr)
        return 1
    new_version = sys.argv[1]
    if not SRC_DIR.exists():
        print(f"[錯誤] 找不到 {SRC_DIR}", file=sys.stderr)
        return 1

    data = {
        "manifest_version": 2,
        "app_version": new_version,
        "min_supported_local_version": "2026.04.01.0",
        "release_url": f"{GITHUB}/releases/latest",
        "files": collect_entries(new_version),
    }
    MANIFEST.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"[manifest] 已寫入 {len(data['files'])} 個檔案，app_version={new_version}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
