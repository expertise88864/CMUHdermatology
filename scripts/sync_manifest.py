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
    """計算檔案 SHA256（LF normalize）。

    【重要】Windows git 預設 autocrlf=true，本機磁碟上是 CRLF，但 git 儲存與
    GitHub raw 服務的都是 LF。若直接 hash 磁碟 bytes，會與 updater 從 GitHub
    下載的 bytes 不符，導致 SHA256 校驗永遠失敗。
    解法：讀 binary 後 normalize CRLF→LF 再 hash，與 GitHub raw 一致。
    """
    with p.open("rb") as f:
        content = f.read()
    content = content.replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()

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
        rel = py.relative_to(REPO_ROOT).as_posix()
        key = ENTRY_KEYS.get(rel) or rel.replace("src/", "").replace("/", ".").replace(".py", "")
        entries.append({
            "key": key,
            "remote_path": rel,
            "local_filename": rel,
            "version": version,
            "sha256": sha256_of(py),
        })
    # [O34] 也把 repo root 的 hotkey_overrides.json 納入（多台電腦自動同步）
    extra_root_files = ["hotkey_overrides.json"]
    for fn in extra_root_files:
        p = REPO_ROOT / fn
        if p.is_file():
            entries.append({
                "key": fn.replace(".json", "").replace(".", "_"),
                "remote_path": fn,
                "local_filename": fn,
                "version": version,
                "sha256": sha256_of(p),
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
