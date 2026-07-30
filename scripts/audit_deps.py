# -*- coding: utf-8 -*-
"""相依套件弱點掃描（P2-07）——★不做無聲過濾★

`pip-audit` 本身很好用，但直接丟進 CI 有兩個實務問題：

1. **`-r requirements.txt` 會炸**：`pip_requirements_parser` 用 locale 編碼讀檔，
   而 requirements 檔只要有一個非 ASCII 位元組 → 在 cp936/cp1252 的機器上
   `UnicodeDecodeError`。所以改成掃【已安裝的環境】：CI 是照 requirements.txt 裝的，
   而且這樣連間接相依也一起涵蓋（比只看直接相依更接近實際出貨內容）。

2. **雜訊會讓關卡失去意義**：環境裡有 pip / setuptools / pytest / ruff 這些
   **不會出貨**的開發工具。它們的 CVE 混進來，關卡就會長期紅燈 → 被忽略 →
   等於沒有。

★但「過濾」必須是看得見的★
所以這支腳本把每一筆被略過的東西【都印出來並說明理由】。這個 repo 的教訓是
「靜默跳過的守衛比沒有守衛更糟」——一個安靜地把 CVE 濾掉的掃描器正是那種東西。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

def _make_stdout_robust() -> None:
    """★關卡不可死在「印不出自己的輸出」★

    這些關卡會把外部工具的訊息（pyright 診斷、pytest 的 skip 理由、
    pip-audit 的套件名）原樣印出來，而那些可能含任何 Unicode（例如 pyright
    用的 U+2022 項目符號）。控制台是 cp936/cp1252 時，`print` 會拋
    `UnicodeEncodeError` —— 關卡於是因為【印不出來】而失敗或中斷，
    而不是因為它要擋的事情。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_make_stdout_robust()


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWLIST_FILE = os.path.join(REPO_ROOT, "security", "pip_audit_allowlist.json")


def _load_allowlist() -> dict:
    with open(ALLOWLIST_FILE, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {
        "dev_only_packages": {p.lower(): why for p, why
                              in raw.get("dev_only_packages", {}).items()},
        "accepted_vulns": {v.upper(): why for v, why
                           in raw.get("accepted_vulns", {}).items()},
    }


def _run_pip_audit() -> dict:
    """跑 pip-audit 並回完整報告。★任何「沒跑完」都視為失敗★

    ★[2026-07-30 外審第 1 輪] 掃不完不可當成「沒有弱點」★
    我上一版只看 stdout 能不能解成 JSON：
      * 沒檢 returncode → pip-audit 自己死掉但吐了部分 JSON 也算過。
      * 沒驗 schema → 未釘版的新 pip-audit 改了格式，`dependencies` 不見了，
        `.get(..., [])` 直接變成「零個弱點」的綠燈。
      * `--strict` 沒加 → 某些套件收集失敗時會被靜默跳過。
    一個會安靜回綠的弱點掃描器，比沒有掃描器更糟。
    """
    cp = subprocess.run(
        [sys.executable, "-m", "pip_audit", "--format", "json",
         "--progress-spinner", "off", "--strict"],
        capture_output=True, text=True, check=False)

    def _die(msg: str) -> "None":
        raise SystemExit("\n".join([
            f"[deps] {msg}",
            "  ★掃不完不等於沒有弱點★ —— 這道關卡沒跑成，視為失敗。",
            f"  rc={cp.returncode}",
            f"  stdout 前 800 字：{cp.stdout[:800]!r}",
            f"  stderr 前 800 字：{cp.stderr[:800]!r}",
        ]))

    # pip-audit 的離開碼：0=沒找到弱點、1=找到弱點。其他一律是執行錯誤。
    if cp.returncode not in (0, 1):
        _die(f"pip-audit 以異常狀態碼結束（{cp.returncode}）")
    if not cp.stdout.strip():
        _die("pip-audit 沒有輸出")
    try:
        report = json.loads(cp.stdout)
    except ValueError as e:
        _die(f"pip-audit 輸出無法解析：{e}")
    if not isinstance(report, dict) or "dependencies" not in report:
        _die("pip-audit 報告沒有 `dependencies` 欄位（格式可能改了）")
    deps = report["dependencies"]
    if not isinstance(deps, list) or not deps:
        _die("pip-audit 一個相依套件都沒掃到")
    # `--strict` 下本來就不該有 skipped；真的出現代表有套件沒掃到。
    skipped = report.get("skipped") or []
    if skipped:
        _die(f"pip-audit 跳過了 {len(skipped)} 個套件：{skipped[:5]}")
    return report


def main() -> int:
    allow = _load_allowlist()
    report = _run_pip_audit()

    blocking: list = []
    skipped_dev: list = []
    skipped_accepted: list = []

    for dep in report.get("dependencies", []):
        name = str(dep.get("name", "")).lower()
        version = dep.get("version", "?")
        for vuln in dep.get("vulns", []):
            vid = str(vuln.get("id", "?")).upper()
            fix = ", ".join(vuln.get("fix_versions") or []) or "（尚無修正版）"
            row = (name, version, vid, fix)
            if name in allow["dev_only_packages"]:
                skipped_dev.append(row)
            elif vid in allow["accepted_vulns"]:
                skipped_accepted.append(row)
            else:
                blocking.append(row)

    def _dump(title: str, rows: list, why_of=None) -> None:
        if not rows:
            return
        print(f"\n{title}（{len(rows)} 筆）")
        for name, version, vid, fix in sorted(rows):
            why = f"  ← {why_of(name, vid)}" if why_of else ""
            print(f"  {name} {version}  {vid}  修正版: {fix}{why}")

    # ★被略過的一律列出來★ —— 安靜地濾掉 CVE 的掃描器比沒有掃描器更糟
    _dump("[deps] 已略過（開發工具，不隨程式出貨）", skipped_dev,
          lambda n, _v: allow["dev_only_packages"][n])
    _dump("[deps] 已略過（明列接受，見 security/pip_audit_allowlist.json）",
          skipped_accepted, lambda _n, v: allow["accepted_vulns"][v])
    _dump("[deps] ★需要處理★", blocking)

    if blocking:
        print(f"\n[deps] {len(blocking)} 個弱點需要處理："
              "升級相依套件，或在 security/pip_audit_allowlist.json 明列接受"
              "（要寫理由與複查日期）。")
        return 1
    print(f"\n[deps] 沒有待處理的弱點"
          f"（略過 {len(skipped_dev)} 開發工具 / {len(skipped_accepted)} 明列接受）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
