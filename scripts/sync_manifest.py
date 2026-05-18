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

_BINARY_EXTS = {
    ".ico", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".ttf", ".otf", ".pdf", ".exe", ".dll", ".zip", ".bin",
}


def is_binary_file(filename: str) -> bool:
    import os
    return os.path.splitext(filename.lower())[1] in _BINARY_EXTS


def sha256_of(p: Path) -> str:
    """計算檔案 SHA256。

    text 檔（.py/.cmd/.ps1/.txt/.json…）：LF normalize 再 hash（因為 GitHub
    raw 服務的是 LF，updater 端會把下載的內容當 text 處理）。
    binary 檔（.ico/.png 等）：直接 hash raw bytes（updater 走 binary 路徑
    不會做 LF normalize）。

    【重要】Windows git 預設 autocrlf=true，本機磁碟上是 CRLF，但 git 儲存與
    GitHub raw 服務的都是 LF。若直接 hash 磁碟 bytes，會與 updater 從 GitHub
    下載的 bytes 不符，導致 SHA256 校驗永遠失敗。
    """
    with p.open("rb") as f:
        content = f.read()
    if not is_binary_file(p.name):
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
    # 自動同步到所有電腦的 extras：
    # - .pyw 啟動 shim（萬一啟動邏輯改了，舊版會壞）
    # - 開機自動啟動相關腳本
    # - requirements.txt（pip 依賴清單）
    # - assets 圖示（binary，updater 會自動走 binary 路徑）
    # - hotkey_overrides.json（[O34] 多台電腦同步熱鍵覆寫）
    extra_files = [
        # 啟動 shim（5 個 .pyw）
        "中國醫皮膚科主程式.pyw",
        "中國醫皮膚科打卡程式.pyw",
        "中國醫皮膚科排班程式.pyw",
        "中國醫皮膚科會診查詢程式.pyw",
        "中國醫皮膚科點座標偵測程式.pyw",
        # 自動啟動排程相關
        "安裝開機自動啟動.cmd",
        "安裝開機自動啟動.ps1",
        "移除開機自動啟動.cmd",
        "移除開機自動啟動.ps1",
        # 設定/資源檔
        "hotkey_overrides.json",
        "requirements.txt",
        # 圖示 (binary — updater 會走 atomic_write_bytes)
        "assets/cmuh_app.ico",
        "assets/AutoClockIcon.png",
        "assets/cmuh_icon_version.txt",
    ]
    for fn in extra_files:
        p = REPO_ROOT / fn
        if p.is_file():
            # key：dot/slash 改底線，特殊副檔名加後綴避免衝突
            key = (fn.replace("/", "_")
                     .replace(".json", "")
                     .replace(".cmd", "_cmd")
                     .replace(".ps1", "_ps1")
                     .replace(".pyw", "_pyw")
                     .replace(".txt", "_txt")
                     .replace(".ico", "_ico")
                     .replace(".png", "_png")
                     .replace(".", "_"))
            entries.append({
                "key": key,
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
