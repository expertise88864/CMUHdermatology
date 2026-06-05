# -*- coding: utf-8 -*-
"""push.bat 的核心邏輯（用 Python 寫，避免 BAT 在 UTF-8 環境下解析錯誤）。

流程：
  1. sanity check：settings/ 不可被追蹤、.gitignore 完整、version.py 可讀
  2. 確認有 git 變更
  3. 品質關卡：ruff + pytest，紅燈就中止（壞 build 推不出去；尚未 bump/commit）
  4. bump 版本（YYYY.MM.DD.serial）
  5. 同步 manifest.json（含 SHA256）
  6. git add -A → commit → push
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """執行子命令，輸出直接連到 console。"""
    print(f"  $ {' '.join(cmd)}")
    if capture:
        return subprocess.run(cmd, cwd=REPO_ROOT, check=check, text=True,
                              capture_output=True, encoding='utf-8', errors='replace')
    return subprocess.run(cmd, cwd=REPO_ROOT, check=check)


def fail(msg: str, code: int = 1) -> None:
    print(f"\n[錯誤] {msg}\n")
    sys.exit(code)


def step1_sanity() -> None:
    print("\n=== [1/6] 安全自檢 ===")
    # 1a. settings/ 不可被追蹤
    cp = run(["git", "ls-files", "settings/"], check=False, capture=True)
    if cp.stdout.strip():
        fail(f"settings/ 已被追蹤（會把密碼推上 Public repo）：\n{cp.stdout}\n"
             f"請執行：git rm -r --cached settings/")
    # 1b. .gitignore 必含這些
    gi = REPO_ROOT / ".gitignore"
    if not gi.exists():
        fail(".gitignore 不存在")
    content = gi.read_text(encoding='utf-8')
    required = ["settings/", "_originals/", "*.log", ".deps_cache",
                "python_embed/", "__pycache__/"]
    missing = [p for p in required if p not in content]
    if missing:
        fail(f".gitignore 缺少: {', '.join(missing)}")
    # 1c. version.py 可讀
    ver_file = REPO_ROOT / "src" / "cmuh_common" / "version.py"
    if not ver_file.exists():
        fail(f"找不到 {ver_file}")
    print("  [OK] 安全自檢通過")


def step2_check_changes() -> bool:
    print("\n=== [2/6] Git 狀態 ===")
    cp = run(["git", "status", "--porcelain"], check=False, capture=True)
    if not cp.stdout.strip():
        print("\n[提示] 沒有變更，無需推送。")
        return False
    # 顯示簡短狀態
    for line in cp.stdout.splitlines()[:20]:
        print(f"  {line}")
    return True


def step_quality_gate() -> None:
    """ruff + pytest 品質關卡。任一紅燈即中止推送（此時尚未 bump 版本、未 commit）。

    工具未安裝時警示並略過該項（讓沒裝 dev 工具的機器仍能推），但 CI 仍會把關。
    """
    print("\n=== [3/7] 品質關卡（ruff + pytest）===")
    checks = [
        ("ruff", "ruff",
         [sys.executable, "-m", "ruff", "check", "src", "scripts", "tests"]),
        ("pytest", "pytest",
         [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]),
    ]
    failed = []
    for label, module, cmd in checks:
        if importlib.util.find_spec(module) is None:
            print(f"  [略過] {label} 未安裝，跳過（建議 pip install {label}）。CI 仍會把關。")
            continue
        print(f"  $ {' '.join(cmd)}")
        if subprocess.run(cmd, cwd=REPO_ROOT).returncode != 0:
            failed.append(label)
        else:
            print(f"  [OK] {label} 通過")
    if failed:
        fail(f"品質關卡未通過（{', '.join(failed)} 紅燈），已中止推送。\n"
             f"  尚未 bump 版本、未 commit；請修正上面紅燈後再 push。")


def step3_bump_version() -> str:
    print("\n=== [4/7] Bump 版本號 ===")
    ver_file = REPO_ROOT / "src" / "cmuh_common" / "version.py"
    text = ver_file.read_text(encoding='utf-8')
    m = re.search(r'CURRENT_VERSION\s*=\s*["\']([\d.]+)["\']', text)
    if not m:
        fail("找不到 CURRENT_VERSION")
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
    new_text = re.sub(
        r'(CURRENT_VERSION\s*=\s*["\'])([\d.]+)(["\'])',
        rf'\g<1>{new}\g<3>', text, count=1)
    ver_file.write_text(new_text, encoding='utf-8')
    print(f"  [bump] {old} -> {new}")
    return new


def step4_sync_manifest(new_version: str) -> None:
    print("\n=== [5/7] 同步 manifest.json（含 SHA256）===")
    # 不 capture（避免 cp950 console 解碼 utf-8 中文輸出失敗）；讓子程序直接印
    cp = run([sys.executable, str(REPO_ROOT / "scripts" / "sync_manifest.py"), new_version],
             check=False)
    if cp.returncode != 0:
        fail("sync_manifest.py 失敗")


def step5_commit(commit_msg: str, new_version: str) -> None:
    print("\n=== [6/7] Commit ===")
    if not commit_msg or commit_msg.strip() in ("", "1"):
        commit_msg = f"Update v{new_version}"
    run(["git", "add", "-A"])
    cp = run(["git", "commit", "-m", commit_msg], check=False)
    if cp.returncode != 0:
        fail("git commit 失敗（可能無實際變更或 hook 阻擋）")


def step6_push() -> None:
    print("\n=== [7/7] Push ===")
    # 取當前分支
    cp = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False, capture=True)
    branch = cp.stdout.strip() or "main"
    print(f"  推送至 origin/{branch} ...")
    cp = run(["git", "push", "origin", branch], check=False)
    if cp.returncode != 0:
        # 可能還沒設 remote 或第一次推
        print("\n[提示] git push 失敗。可能原因：")
        print("  - 還沒設定 remote：git remote add origin https://github.com/expertise88864/CMUHdermatology.git")
        print("  - 第一次推送：git push -u origin main")
        sys.exit(1)


def main(argv: list) -> int:
    commit_msg = " ".join(argv[1:]) if len(argv) > 1 else ""

    print("=" * 60)
    print("  CMUHdermatology 一鍵推送")
    print("=" * 60)

    # 環境檢查
    if not (REPO_ROOT / "src" / "cmuh_common" / "version.py").exists():
        fail(f"請在 repo 根目錄執行（目前: {REPO_ROOT}）")

    step1_sanity()
    if not step2_check_changes():
        return 0
    step_quality_gate()
    new_ver = step3_bump_version()
    step4_sync_manifest(new_ver)
    step5_commit(commit_msg, new_ver)
    step6_push()

    print("\n" + "=" * 60)
    print(f"  推送完成！v{new_ver}")
    print("  其他電腦下次啟動時會自動拉新版（CDN 快取約 5 分鐘）")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("\n[中斷]")
        sys.exit(130)
