# -*- coding: utf-8 -*-
"""把 C:\\Dev\\CMUHdermatology 整包搬到任意目標目錄（預設舊版桌面資料夾），
並自動把 root 的 config JSON 搬進 settings/，與新代碼相容。

用法：
  python scripts\\migrate_helper.py [目標目錄]
  預設目標：C:\\Users\\calling\\Desktop\\翊嘉\\中國醫皮膚科程式

執行後：
  1. 在目標備份舊 .pyw 為 .pyw.OLD（避免新 launcher 覆蓋）
  2. 把 root 的 doctors.json / threshold_settings.json / r_doctor_settings.json /
     auto_reboot_settings.json 搬進 settings/（若還沒搬過）
  3. 把本 repo（C:\\Dev\\CMUHdermatology）所有檔複製到目標
  4. 不覆寫目標的 settings/ 內容（保留密碼等敏感資料）
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DST = Path(r"C:\Users\calling\Desktop\翊嘉\中國醫皮膚科程式")

# 從目標 root 搬進 settings/ 的 config 檔（舊版放 root，新版放 settings/）
ROOT_CONFIGS_TO_MIGRATE = [
    "doctors.json",
    "threshold_settings.json",
    "r_doctor_settings.json",
    "auto_reboot_settings.json",
]

# 不要覆寫目標已存在的東西（保留使用者資料）
PROTECTED_DIRS = ["settings", ".wdm", "__pycache__", "build", "dist",
                  "deploy/dist", "python_embed", ".ruff_cache"]
PROTECTED_FILES_PATTERNS = ["*.log", "*.log.*", "*.bak"]


def confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} (y/N): ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def step1_backup_old_pyw(dst: Path) -> None:
    """把目標的舊 .pyw 改名為 .pyw.OLD（避免新 launcher 直接覆蓋掉）。"""
    print("\n=== [1/3] 備份舊 .pyw ===")
    pyws = list(dst.glob("中國醫皮膚科*.pyw"))
    if not pyws:
        print("  [略過] 目標沒有舊 .pyw")
        return
    for p in pyws:
        # 判斷是否為舊大檔（>50KB 才備份；新 launcher 才幾百 bytes）
        if p.stat().st_size > 5000:
            backup = p.with_suffix(p.suffix + ".OLD")
            i = 0
            while backup.exists():
                i += 1
                backup = p.with_suffix(f"{p.suffix}.OLD{i}")
            p.rename(backup)
            print(f"  [備份] {p.name} -> {backup.name}")
        else:
            print(f"  [略過] {p.name} (size={p.stat().st_size}, 看起來是新 launcher)")


def step2_move_root_configs_to_settings(dst: Path) -> None:
    """把目標 root 的 config 檔搬進 settings/。"""
    print("\n=== [2/3] 搬移 root config 至 settings/ ===")
    settings_dir = dst / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    for fn in ROOT_CONFIGS_TO_MIGRATE:
        src_file = dst / fn
        dst_file = settings_dir / fn
        if not src_file.exists():
            continue
        if dst_file.exists():
            print(f"  [略過] settings/{fn} 已存在（保留現有）；root 的留作備份")
            continue
        shutil.move(str(src_file), str(dst_file))
        print(f"  [搬移] {fn} -> settings/{fn}")


def step3_copy_repo(dst: Path) -> None:
    """把 REPO_ROOT 所有檔案複製到 dst（不覆寫 PROTECTED_DIRS、不複製 _originals/）。"""
    print(f"\n=== [3/3] 複製 repo 到 {dst} ===")
    skipped = 0
    copied = 0
    for src in REPO_ROOT.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(REPO_ROOT)
        rel_str = str(rel).replace("\\", "/")

        # 排除 _originals/ 與 .git/ 內部物件以加速（.git 也可選擇複製）
        if rel_str.startswith("_originals/"):
            continue
        if rel_str.startswith(".git/"):
            continue
        # 排除 __pycache__
        if "__pycache__" in rel.parts:
            continue
        # 排除 build/ dist/ python_embed/
        if rel.parts and rel.parts[0] in ("build", "dist", "python_embed"):
            continue

        target = dst / rel
        # 受保護目錄底下的檔案不動
        for pd in PROTECTED_DIRS:
            if rel_str.startswith(pd + "/") or rel_str == pd:
                target = None
                break
        if target is None:
            skipped += 1
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1

    # 複製 .git 目錄（保留 git 歷史與 remote）— 整包覆蓋（這是最安全的）
    src_git = REPO_ROOT / ".git"
    dst_git = dst / ".git"
    if src_git.is_dir():
        if dst_git.exists():
            print("  [警告] 目標已有 .git，先備份為 .git.OLD")
            old_git = dst / ".git.OLD"
            if old_git.exists():
                shutil.rmtree(old_git, ignore_errors=True)
            shutil.move(str(dst_git), str(old_git))
        shutil.copytree(src_git, dst_git)
        print("  [複製] .git/")

    print(f"  [統計] 複製 {copied} 個檔，跳過 {skipped} 個（受保護）")


def main(argv: list) -> int:
    dst = Path(argv[1]) if len(argv) > 1 else DEFAULT_DST

    print("=" * 60)
    print("  搬移 CMUHdermatology 至目標資料夾")
    print("=" * 60)
    print(f"  來源: {REPO_ROOT}")
    print(f"  目標: {dst}")

    if not dst.exists():
        if not confirm(f"\n目標不存在，建立 {dst}？"):
            print("[取消]")
            return 0
        dst.mkdir(parents=True, exist_ok=True)
    else:
        print("\n目標已存在；本工具會：")
        print("  - 備份舊 .pyw 為 .pyw.OLD（保留你原本的程式碼以防萬一）")
        print("  - 搬移 root config（doctors.json 等）到 settings/")
        print("  - 複製本 repo 所有檔（保護 settings/ 不覆蓋）")
        if not confirm("\n繼續？"):
            print("[取消]")
            return 0

    step1_backup_old_pyw(dst)
    step2_move_root_configs_to_settings(dst)
    step3_copy_repo(dst)

    print("\n" + "=" * 60)
    print("  搬移完成！")
    print("=" * 60)
    print(f"  下一步：cd \"{dst}\"")
    print("          雙擊 中國醫皮膚科主程式.pyw 確認可正常啟動")
    print("          確認 OK 後，C:\\Dev\\CMUHdermatology 就可以刪除了")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("\n[中斷]")
        sys.exit(130)
