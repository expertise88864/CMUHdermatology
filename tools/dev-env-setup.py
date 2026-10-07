"""Read-only development check; explicit setup creates a project-local venv.

No global model, effort, MCP, rules, package or production settings edits.
Existing environments are checked, never repaired in place.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV_PACKAGES = ("ruff", "pytest", "pytest-cov", "pyright")
BASELINE = "docs/dependency_baseline_2026-10-03.txt"


def run(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    """Capture diagnostics without echoing credentials or provider messages."""
    executable = shutil.which(args[0]) or args[0]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    env.update(PYTHONDONTWRITEBYTECODE="1", GIT_OPTIONAL_LOCKS="0", PIP_CONFIG_FILE=os.devnull)
    return subprocess.run([executable, *args[1:]], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)


def _venv_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if path.parent != ROOT.resolve() or not re.fullmatch(r"\.venv(?:-[A-Za-z0-9_-]+)?", path.name):
        raise ValueError("使用 repo 根目錄的 .venv 或 .venv-名稱；不得使用全域／正式環境。")
    return path


def _python(path: Path) -> Path:
    windows = path / "Scripts/python.exe"
    return windows if windows.exists() else path / "bin/python"


def _row(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _probe_dependencies(python: Path) -> dict:
    # Metadata only: importing keyboard, pyautogui or launchers can touch the desktop.
    code = (
        "import importlib.metadata as m,json,sys; from packaging.requirements import Requirement; "
        "packages={d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()}; "
        "requirements=[Requirement(r) for r in json.loads(sys.argv[1])]; "
        "missing=[r.name for r in requirements if r.name.lower().replace('_','-') not in packages "
        "or not r.specifier.contains(packages[r.name.lower().replace('_','-')])]; "
        "print(json.dumps({'python':list(sys.version_info[:2]), 'venv':sys.prefix != sys.base_prefix, "
        "'missing':missing}))"
    )
    cp = run([str(python), "-I", "-B", "-c", code, json.dumps(_requirements())])
    if cp.returncode:
        raise ValueError("無法查詢開發 Python 的套件中繼資料")
    data = json.loads(cp.stdout)
    if not data.get("venv"):
        raise ValueError("指定 Python 不是隔離 venv")
    return data


def _requirements() -> list[str]:
    result = []
    for filename in ("requirements.txt", "requirements-lazy.txt"):
        for line in (ROOT / filename).read_text(encoding="ascii").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                result.append(line)
    return result + list(DEV_PACKAGES)


def _auth_status(tool: str) -> bool:
    args = ([tool, "auth", "status", "--json"] if tool == "claude" else
            [tool, "login", "status"])
    cp = run(args)
    if cp.returncode:
        return False
    if tool == "claude":
        return json.loads(cp.stdout).get("loggedIn") is True
    return re.search(r"(?im)^Logged in\b", cp.stdout + "\n" + cp.stderr) is not None


def check(environment: Path, auth: bool = False) -> list[dict]:
    rows = [_row("platform", "passed" if sys.platform == "win32" else "failed",
                 "開發完整驗證使用 Windows；GitHub CI 使用 Python 3.13")]
    interpreter = _python(environment)
    if not interpreter.exists():
        rows.append(_row("venv", "failed", "開發環境不存在；使用明確 apply 建立"))
        rows.append(_row("dependencies", "not_checked", "尚無可查詢的開發環境"))
    else:
        try:
            data = _probe_dependencies(interpreter)
            missing = data["missing"]
            rows.append(_row("venv", "passed" if data["python"] == [3, 13] else "failed",
                             "以 Python 3.13 與正式 CI 對照；此限制僅適用開發流程"))
            rows.append(_row("dependencies", "failed" if missing else "passed",
                             "缺少或版本不符：" + ", ".join(missing) if missing else "必要 runtime、lazy 與開發工具齊全"))
        except (OSError, ValueError, KeyError, ImportError, subprocess.SubprocessError):
            rows.append(_row("dependencies", "failed", "環境中繼資料不可讀；未修改環境"))
    try:
        cp = run(["git", "config", "--get", "core.hooksPath"])
        good = cp.returncode == 0 and cp.stdout.strip() == ".githooks" and (ROOT / ".githooks/pre-push").is_file()
        rows.append(_row("git_hook", "passed" if good else "failed",
                         "依既有交付流程核對 .githooks；本工具不修改 Git 設定"))
    except (OSError, subprocess.SubprocessError):
        rows.append(_row("git_hook", "failed", "無法核對 Git hook"))
    for tool in ("claude", "codex"):
        exists = shutil.which(tool) is not None
        rows.append(_row(tool, "passed" if exists else "failed",
                         "CLI 已找到" if exists else "需依官方方式手動安裝；本工具不做全域安裝"))
        if auth and exists:
            try:
                logged_in = _auth_status(tool)
                rows.append(_row(tool + "_auth", "passed" if logged_in else "failed",
                                 "登入狀態已核對" if logged_in else "未登入或狀態無法確認，請手動登入"))
            except (OSError, ValueError, subprocess.SubprocessError):
                rows.append(_row(tool + "_auth", "failed", "狀態無法確認；未顯示原始供應商輸出"))
        else:
            rows.append(_row(tool + "_auth", "not_checked", "需 --auth 唯讀核對；未檢查不代表已登入"))
    rows.append(_row("production_settings", "manual" if (ROOT / "settings").exists() else "passed",
                     "開發測試沿用匿名 pytest fixtures；不複製或讀取正式 settings"))
    return rows


def apply(environment: Path) -> bool:
    if environment.exists():
        print("[保留] 已有環境只檢查；需重建時指定新的 .venv-名稱。")
        return True
    if sys.platform != "win32" or sys.version_info[:2] != (3, 13):
        print("[失敗] 請以 Windows Python 3.13 建立開發環境。")
        return False
    try:
        # venv reuses existing directories. Reserve this name exclusively before
        # invoking it, so a concurrent apply cannot overwrite another creator.
        environment.mkdir()
    except FileExistsError:
        print("[未完成] 環境名稱剛被其他程序建立；未初始化該目錄，請重新檢查或使用新名稱。")
        return False
    except OSError:
        print("[未完成] 無法建立新的環境目錄；未初始化或修改既有環境。")
        return False
    try:
        (environment / ".cmuh-dev-setup.json").write_text(
            json.dumps({"creator_pid": os.getpid(), "purpose": "CMUH local development"}), encoding="utf-8")
    except OSError:
        print("[未完成] 無法記錄新環境的建立者；保留新目錄，未開始初始化。")
        return False
    commands = [
        [sys.executable, "-I", "-m", "venv", str(environment)],
        [str(environment / "Scripts/python.exe"), "-I", "-m", "pip", "--isolated", "install", "--no-user",
         "-r", "requirements.txt", "-r", "requirements-lazy.txt", "-c", BASELINE,
         *DEV_PACKAGES],
        [str(environment / "Scripts/python.exe"), "-I", "-m", "pip", "--isolated", "check"],
    ]
    for number, args in enumerate(commands, 1):
        print(f"[套用 {number}/3] 僅操作 {environment.name}")
        try:
            cp = run(args, timeout=1200)
            if cp.returncode:
                print(f"[未完成] 步驟 {number} 失敗（exit {cp.returncode}）；保留未完成環境，請用新名稱重建。")
                return False
        except (OSError, subprocess.SubprocessError):
            print(f"[未完成] 步驟 {number} 無法完成；未變更既有環境或全域設定。")
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="開發環境檢查（預設唯讀）；apply 僅建立新的專案 venv。",
                                     allow_abbrev=False)
    parser.add_argument("action", nargs="?", choices=("check", "apply"), default="check")
    parser.add_argument("--venv", default=".venv", help="repo 根目錄的 .venv 或 .venv-名稱")
    parser.add_argument("--auth", action="store_true", help="唯讀查詢 Claude/Codex 登入狀態，不輸出憑證")
    args = parser.parse_args(argv)
    try:
        environment = _venv_path(args.venv)
    except ValueError as error:
        parser.error(str(error))
    if args.action == "apply" and not apply(environment):
        return 1
    rows = check(environment, args.auth)
    for row in rows:
        print(f"[{row['status']}] {row['name']}: {row['detail']}")
    failed = any(row["status"] == "failed" for row in rows)
    print("[未完成] 請處理 failed 項目後重查。" if failed else
          "[檢查通過] 僅代表已檢查項目；not_checked／manual、完整 CI 與院內驗收另行處理。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
