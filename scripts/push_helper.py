# -*- coding: utf-8 -*-
"""push.bat 的核心邏輯（用 Python 寫，避免 BAT 在 UTF-8 環境下解析錯誤）。

流程：
  1. sanity check：settings/ 不可被追蹤、.gitignore 完整、version.py 可讀
  2. 確認有 git 變更
  3. bump 版本、同步 manifest.json、核對 index 後建立本機 commit
  4. 對最終 commit 跑 Ruff 與 delivery contract 快速回歸
  5. 再確認 SHA 與整個工作樹未變動，正常推送 codex/* 候選分支
  6. 完整 CI 在候選分支遠端執行；exact-SHA 全綠後才可快轉 main

用法：
  python scripts/push_helper.py check
  python scripts/push_helper.py publish --path scripts/example.py --message-file message.txt
  發佈在隔離副本內進行，原工作樹、index 及分支不會被升版或提交。
  --emergency 已停用；不可豁免候選與正式 exact-SHA 遠端 CI。

`step_quality_gate` 保留為人工診斷／舊測試可直接呼叫的完整本機檢查，正式
remote-first 發佈流程不呼叫它；權威流程見 REMOTE_CI_DELIVERY.md。
"""
from __future__ import annotations

import hashlib
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """執行子命令，輸出直接連到 console。"""
    private_remote = cmd[:3] == ["git", "remote", "set-url"]
    shown = [*cmd[:4], "<remote URL>"] if private_remote else cmd
    print(f"  $ {' '.join(shown)}")
    if private_remote:
        # Git may include the URL in errors; neither arguments nor raw diagnostics
        # should expose credentials stored in the user's local remote config.
        cp = subprocess.run(cmd, cwd=REPO_ROOT, check=False, text=True,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                            capture_output=True, encoding='utf-8', errors='replace')
        if check and cp.returncode:
            raise subprocess.CalledProcessError(cp.returncode, shown)
        return cp
    if capture:
        return subprocess.run(cmd, cwd=REPO_ROOT, check=check, text=True,
                              env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                              capture_output=True, encoding='utf-8', errors='replace')
    return subprocess.run(cmd, cwd=REPO_ROOT, check=check,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})


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


def _unpushed_commit_count() -> int:
    """本地分支【領先 upstream】幾個 commit。查不到 upstream 時回 0。"""
    cp = run(["git", "rev-list", "--count", "@{u}..HEAD"],
             check=False, capture=True)
    if cp.returncode != 0:
        return 0                       # 沒有 upstream（新分支等）→ 不做推測
    try:
        return int(cp.stdout.strip() or "0")
    except ValueError:
        return 0


def step2_check_changes() -> bool:
    """→ 還有沒有東西要發佈。

    ★[2026-08-05 補審 P1] 工作區乾淨【不等於】沒有東西要推★
    原本只看 `git status --porcelain`：一乾淨就 return False 直接結束。可是
    「先在本機 commit 幾次、最後再推」是很正常的做法（這次補審就是這樣做的：
    每修好一輪外審 finding 就 commit 一次）。那種情況下：

      * 跑 `push.bat` → 這裡直接返回 → **不 bump 版本、不同步 manifest、不推**，
        而且畫面上寫的是「沒有變更，無需推送」，看起來像一切正常；
      * 忍不住改用 `git push` → 原始碼上去了，manifest.json 卻還是舊的雜湊 →
        既有機器比對後認為「檔案沒變」而【跳過下載】，等於修正根本沒送到診間；
        全新 checkout 則會拿到與 manifest 對不上的檔案，SHA 驗證失敗、整批停更。

    所以工作區乾淨時還要看「有沒有還沒推上去的 commit」。有的話照樣要走完
    bump → 同步 manifest → commit → push（那個 release commit 只會動
    version.py 與 manifest.json）。
    """
    print("\n=== [2/6] Git 狀態 ===")
    cp = run(["git", "status", "--porcelain"], check=False, capture=True)
    if cp.stdout.strip():
        for line in cp.stdout.splitlines()[:20]:
            print(f"  {line}")
        return True

    ahead = _unpushed_commit_count()
    if ahead:
        print(f"  工作區乾淨，但有 {ahead} 個 commit 還沒推上去 → "
              f"仍要 bump 版本並同步 manifest 才能發佈")
        run(["git", "log", "--oneline", "@{u}..HEAD"], check=False)
        return True

    print("\n[提示] 沒有變更，無需推送。")
    return False


GATE_ARTIFACTS = ("junit.xml", "cov.json")


def _clean_gate_artifacts() -> None:
    for name in GATE_ARTIFACTS:
        try:
            (REPO_ROOT / name).unlink()
        except OSError:
            pass


def step_quality_gate(emergency_reason: str = "") -> None:
    """可選的完整本機診斷；remote-first 候選推送流程不會呼叫。

    工具沒裝仍然【中止】，不是略過；這個函式若被人工或測試呼叫，就必須
    完整執行其宣告的本機診斷。正式 remote-first 流程則由下方快速關卡建立
    codex/* 候選，再以 exact-SHA 遠端 CI 作為 main 發佈依據。

    緊急理由與 Opus pending 均不豁免候選／正式 CI。保留舊參數只為明確
    拒絕舊呼叫，不提供任何旁路。
    """
    print("\n=== [3/7] 品質關卡（ruff + pyright + pytest + 棘輪）===")
    if emergency_reason:
        fail("--emergency 已停用：不得繞過候選／正式 exact-SHA 遠端 CI。")

    # ★工具齊全性先檢★：缺任何一個都直接中止，不進入部分檢查
    needed = {"ruff": "ruff", "pyright": "pyright", "pytest": "pytest",
              "pytest-cov": "pytest_cov"}
    missing = [name for name, module in needed.items()
               if importlib.util.find_spec(module) is None]
    if missing:
        # ★安裝指令要指名【這個】解釋器★
        #   工具有沒有裝是用 `importlib.util.find_spec`（＝跑這支腳本的直譯器）判斷的，
        #   檢查也全都走 `sys.executable`。本機常裝了好幾個 Python，裸的 `pip` 很可能
        #   屬於另一個 —— 使用者照著裝完，這裡依舊查不到，於是每次 push 都被擋而且
        #   不知道為什麼。（repo 裡 probe_ditto_ocr.py 早就是這樣寫的。）
        fail("本機品質關卡的工具沒裝齊，【中止推送】：" + ", ".join(missing) + "\n"
             '  請先裝："' + sys.executable + '" -m pip install '
             + " ".join(missing) + "\n"
             "  （要用這個解釋器的 pip：本機可能裝了好幾個 Python，裸的 `pip` 很可能\n"
             "    屬於另一個 —— 裝完這裡依舊查不到，推不出去且不知道為什麼）\n"
             "  這次人工完整診斷不可標成通過；請先修復環境。正式發佈仍須走\n"
             "  codex/* 候選與候選／正式 exact-SHA 遠端 CI，不得略過。")

    _clean_gate_artifacts()
    failed = []

    def _step(label: str, cmd: list) -> bool:
        print(f"  $ {' '.join(cmd)}")
        ok = subprocess.run(cmd, cwd=REPO_ROOT).returncode == 0
        print(f"  [{'OK' if ok else 'FAIL'}] {label}")
        if not ok:
            failed.append(label)
        return ok

    _step("ruff", [sys.executable, "-m", "ruff", "check", "src", "scripts", "tests"])
    # pyright 以前只在 CI 跑 —— 型別錯誤因此都是【推上去之後】才發現。
    _step("pyright", [sys.executable, "-m", "pyright"])
    _step("相依一致性", [sys.executable, "-m", "pip", "check"])
    _step("lazy 相依匯入", [sys.executable, "-c",
                              "import ortools, openpyxl, docx, reportlab"])
    # 一次 pytest 同時產出 junit.xml 與 cov.json，下面兩道棘輪直接吃（不多跑一次）
    pytest_ok = _step("pytest", [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "--junitxml=junit.xml", "--cov=src", "--cov-report=json:cov.json",
        "--cov-report="])
    if pytest_ok:
        _step("skip 數量守衛",
              [sys.executable, "scripts/check_skips.py", "junit.xml"])
        _step("分層覆蓋率門檻",
              [sys.executable, "scripts/check_coverage.py", "cov.json"])
    else:
        # pytest 紅燈時報告不完整，拿不完整的報告去判只會誤導
        print("  [略過] skip 守衛與覆蓋率門檻（pytest 已紅燈，報告不完整）")

    # 與 GitHub CI 同樣逐條執行；--fast 不代替最終交付的完整檢查。
    _step("型別債棘輪(full)", [sys.executable, "scripts/type_debt.py"])
    _clean_gate_artifacts()
    if failed:
        fail("品質關卡未通過（" + ", ".join(failed) + " 紅燈），已中止推送。\n"
             "  本機 commit 保留；請修正紅燈並對最終版本重新驗證後再 push。")


VERSION_REL = "src/cmuh_common/version.py"
MANIFEST_REL = "manifest.json"
# git add -A 會收進來、且值得防還原的範圍
SCAN_PATHS = ["."]  # Include docs, launchers, policy and every explicit candidate file.


def _git_bytes(args: list, stdin: bytes = b"") -> bytes:
    cp = subprocess.run(["git", *args], input=stdin, cwd=REPO_ROOT,
                        capture_output=True, text=False,
                        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if cp.returncode != 0:
        fail(f"git {' '.join(args)} 失敗，為安全起見中止推送。\n"
             f"{cp.stderr.decode('utf-8', 'replace')[:400]}")
    return cp.stdout


def worktree_blob_ids(*, include_version: bool = False) -> dict:
    """{相對路徑: git blob id}——「這些檔案現在 git add 進去會變成什麼」。

    ★[2026-08-02 補審] 一定要讓 git 自己算,不可自行 sha256(檔案原始 bytes)★
    本機 core.autocrlf=true 且 .gitattributes 是 `* text=auto eol=lf`,git 在
    add 時會把 CRLF 正規化成 LF。更致命的是 push_helper **自己** bump 出來的
    version.py 在磁碟上就是 CRLF(Path.write_text 在 Windows 把 \n 轉成 \r\n)——
    自算的 hash 與 index 裡的必然不同,會變成【每一次推送都誤報】。
    `git hash-object` 會依 .gitattributes 套用同一組 filter(實測其輸出等於
    `git ls-files -s` 的 blob id),binary 檔也由 git 自行判定,正確且不必自己猜。

    include_version:bump 會在關卡之後合法改寫 version.py → 關卡前後的比對要排除它;
    index 比對則必須納入(見 main())。
    """
    # --others --exclude-standard：連「尚未追蹤但即將被 git add -A 收進去」的新檔
    # 也納入（事故當次就有新測試檔；只驗已追蹤檔會漏掉新檔被還原/刪除的情形）。
    listed = _git_bytes(["ls-files", "--cached", "--others", "--exclude-standard",
                         "-z", *SCAN_PATHS])
    rels = [x.decode("utf-8", "surrogateescape")
            for x in listed.split(b"\0") if x]
    if not include_version:
        rels = [r for r in rels if r.replace("\\", "/") != VERSION_REL]
    existing = [r for r in rels if (REPO_ROOT / r).exists()]
    out = {r: "" for r in rels}          # 不存在(已刪/讀不到)→ 空字串
    if existing:
        ids = _git_bytes(
            ["hash-object", "--stdin-paths"],
            ("\n".join(existing) + "\n").encode("utf-8")).split()
        if len(ids) != len(existing):
            fail("git hash-object 回傳筆數與檔案數不符，為安全起見中止推送。")
        for rel, bid in zip(existing, ids, strict=True):
            out[rel] = bid.decode("ascii")
    return out


def index_blob_ids() -> dict:
    """{相對路徑: blob id}——index 裡真的要 commit 的內容。"""
    raw = _git_bytes(["ls-files", "-s", "-z", *SCAN_PATHS])
    out = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        parts = meta.split()
        if len(parts) >= 2:
            out[path.decode("utf-8", "surrogateescape")] = parts[1].decode("ascii")
    return out


# 舊名保留:既有測試與呼叫端沿用(語意由 sha256 改為 git blob id)
snapshot_tracked_sources = worktree_blob_ids


def verify_index_matches(expected: dict) -> None:
    """★真正的防線★ `git add -A` 之後,比對【index 裡真的要 commit 的內容】。

    [2026-08-02 補審 P1] 原本只比對「工作目錄的兩次快照」,留下兩個空窗:
      (1) 品質關卡返回 → 取基準之間被還原 → 舊版直接成為合法基準,檢查必過。
      (2) 檢查通過 → `git add -A` 之間被還原 → 被還原的內容照樣進 commit。
    這正是本防護聲稱要消除的事故類型。改成對 index 驗證後,「測過的內容」與
    「commit 進去的內容」之間不再有任何空窗 —— commit 的就是這個 index。

    空字串 = 取樣當下該檔就不在(使用者刻意刪除)→ 它【本來就該】從 index 消失。
    第一版把「index 沒有這個項目」一律當成異常,結果刪掉一個 source 檔就再也
    push 不出去(補審第 2 輪抓到,是我引進的迴歸)。兩個方向現在都覆蓋:
    該在卻不在、以及該不在卻還在(刪掉後又被還原回來)。
    """
    print("")
    print("=== [7/9] 防還原檢查（index 內容 == 測過的內容）===")
    actual = index_blob_ids()
    # ★[2026-08-02 補審第 4 輪] 比對【聯集】,不可只比 expected 的鍵★
    #   取樣之後、git add -A 之前才出現的新檔,只比 expected 就完全看不到 ——
    #   它會被 staged 並 commit 出去,而那正是「commit 的內容 == 測過的內容」
    #   要保證的事。空字串仍代表「預期不存在」,刻意刪檔的情形不受影響。
    changed = sorted(r for r in {*expected, *actual}
                     if actual.get(r, "") != expected.get(r, ""))
    if changed:
        listing = "\n".join(f"    - {c}" for c in changed[:20])
        more = f"\n    …等共 {len(changed)} 個檔案" if len(changed) > 20 else ""
        fail("【即將 commit 的內容與測過的內容不符】已中止推送（尚未 commit）：\n"
             f"{listing}{more}\n"
             "  最可能是 OneDrive 把未提交的檔案還原成舊版（本 repo 已發生兩次）。\n"
             "  請確認上列檔案內容是否為你要的版本（必要時自 %TEMP% 備份還原），\n"
             "  再重新執行 push_helper。")
    print(f"  [OK] {len(expected)} 個檔案的 index 內容與測過的一致")


def verify_staged_version_consistency(new_version: str) -> None:
    """★[2026-08-02 補審] 檢查【真正要 commit 的】version.py 與 manifest.json 一致★

    比「盯著微秒級的還原時窗」更可靠的做法,是直接驗那個【會造成危害的不變量】:
    committed version.py 的 CURRENT_VERSION 必須等於 manifest.json 的 app_version。
    兩者不一致時,所有機器下載後 SHA256 對不上 → 更新 fail-closed 全面停更。
    不管中間發生過什麼還原/競態,這條檢查都成立。
    """
    print("")
    print("=== [7.5/9] 版本一致性（index 內的 version.py == manifest）===")
    ver_blob = _git_bytes(["cat-file", "blob", f":{VERSION_REL}"]).decode(
        "utf-8", "replace")
    man_blob = _git_bytes(["cat-file", "blob", f":{MANIFEST_REL}"]).decode(
        "utf-8", "replace")
    m = re.search(r'CURRENT_VERSION\s*=\s*["\']([^"\']+)["\']', ver_blob)
    staged_ver = m.group(1) if m else "(解析不到)"
    m2 = re.search(r'"app_version"\s*:\s*"([^"]+)"', man_blob)
    staged_man = m2.group(1) if m2 else "(解析不到)"
    if staged_ver != new_version or staged_man != new_version:
        fail("【即將 commit 的版本不一致】已中止推送（尚未 commit）：\n"
             f"    本次 bump 版本      = {new_version}\n"
             f"    index 內 version.py = {staged_ver}\n"
             f"    index 內 manifest   = {staged_man}\n"
             "  若放行，其他機器下載後 SHA256 會對不上而讓更新全面 fail-closed。")
    print(f"  [OK] version.py 與 manifest 皆為 {new_version}")


def verify_staged_manifest_hashes() -> None:
    """讀 index 內的 manifest，逐檔比對雜湊。★必須在 sync_manifest 之後才有意義★

    （刻意【不】掛在 `verify_staged_version_consistency` 裡面：那支在品質關卡的
      情境下也會被直接呼叫，而那個時間點 manifest 本來就還是上一版的 —— 它要到
      `step4_sync_manifest` 才會重算。掛在一起會讓一道正確的檢查在錯誤的時機誤報。）
    """
    _verify_staged_manifest_hashes(
        _git_bytes(["cat-file", "blob", f":{MANIFEST_REL}"]).decode(
            "utf-8", "replace"))


def _verify_staged_manifest_hashes(man_blob: str) -> None:
    """★[2026-08-05 補審 P1] manifest 裡的每個 SHA256 要對得上【index 內的】檔案★

    版本號一致還不夠。真正會傷到人的是「manifest 記的雜湊 ≠ 實際被 commit 的檔案」：

      * 雜湊還是舊的 → 既有機器比對後認為「這個檔沒變」而【跳過下載】，
        修正等於沒送到診間，而且畫面上一切正常；
      * 雜湊對不上 → 下載後 SHA 驗證失敗 → 整批 fail-closed，全機停更。

    這道檢查在【已經 staged、還沒 commit】時跑，所以中止的代價只是「這次沒推成」。
    """
    print("")
    print("=== [7.6/9] manifest 雜湊 vs index 內的檔案 ===")
    try:
        manifest = json.loads(man_blob)
    except ValueError as e:
        fail(f"index 內的 manifest.json 解析不了：{e}")
        return
    bad, checked = [], 0
    for entry in manifest.get("files", []):
        # manifest 的欄位是 remote_path（repo 內路徑）/ local_filename（安裝後路徑）
        rel = str(entry.get("remote_path")
                  or entry.get("local_filename") or "").strip()
        want = str(entry.get("sha256") or "").strip().lower()
        if not rel or not want:
            continue
        rel_git = rel.replace("\\", "/")
        try:
            blob = _git_bytes(["cat-file", "blob", f":{rel_git}"])
        except SystemExit:
            raise
        except Exception as e:
            bad.append(f"{rel_git}: 讀不到 index 內的內容（{e}）")
            continue
        got = hashlib.sha256(blob).hexdigest()
        checked += 1
        if got != want:
            bad.append(f"{rel_git}: manifest={want[:12]}… 實際={got[:12]}…")
    if not checked:
        fail("manifest 裡沒有任何可檢查的檔案雜湊 —— "
             "★空集合不算通過★（欄位名改了的話這道檢查會靜默失效）")
    if bad:
        fail("【manifest 雜湊與即將 commit 的檔案對不上】已中止推送："
             + "\n    " + "\n    ".join(bad[:10])
             + "\n  多半是 sync_manifest 沒跑到，或跑完之後檔案又被改動過。")
    print(f"  [OK] {checked} 個檔案的 SHA256 都對得上")


def verify_unchanged_since_tests(before: dict) -> None:
    """commit 前重算指紋：與品質關卡當下不一致 → 中止推送並點名檔案。

    絕不自動覆蓋/還原檔案——無法判斷哪一份才是使用者要的，只中止並要求人工確認。
    """
    print("\n=== [5/9] 防還原檢查（品質關卡期間檔案未被竄改）===")
    after = snapshot_tracked_sources()
    changed = sorted(k for k in set(before) | set(after)
                     if before.get(k) != after.get(k))
    if changed:
        listing = "\n".join(f"    - {c}" for c in changed[:20])
        more = f"\n    …等共 {len(changed)} 個檔案" if len(changed) > 20 else ""
        fail("【檔案在測試通過後被改動】已中止推送（尚未 commit）：\n"
             f"{listing}{more}\n"
             "  最可能是 OneDrive 把未提交的檔案還原成舊版（本 repo 已發生兩次）。\n"
             "  請確認上列檔案內容是否為你要的版本（必要時自 %TEMP% 備份還原），\n"
             "  再重新執行 push_helper。")
    print(f"  [OK] {len(after)} 個追蹤檔案內容一致")


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


def step5_stage() -> None:
    """把變更放進 index。★與 commit 分開★:分開之後才能在 commit 之前比對
    「index 裡真的要提交的內容」;原本 add 與 commit 綁在一起,驗證只能驗
    工作目錄,add 與 commit 之間仍有空窗。"""
    print("")
    print("=== [6/9] git add ===")
    run(["git", "add", "-A"])


def step5_commit(commit_msg: str, new_version: str,
                 emergency_reason: str = "") -> None:
    print("")
    print("=== [8/9] Commit ===")
    if not commit_msg or not commit_msg.strip():
        commit_msg = f"Update v{new_version}"
    if emergency_reason:
        fail("--emergency 已停用：不能建立豁免 CI 的交付 commit。")
    # 用 UTF-8 暫存檔 + `git commit -F`：Windows 上 subprocess 會以系統 ANSI(cp936/gbk)
    # 編碼參數，commit message 含 emoji/特殊符號(例 U+232B 退格符)時會 UnicodeEncodeError
    # 而中斷整個推送。改寫成 UTF-8 檔讓 git 自行讀取，與系統 codepage 無關，穩定不踩雷。
    # Linked worktree 的 .git 是檔案；獨立暫存目錄也避免多工作樹共用訊息檔。
    with tempfile.TemporaryDirectory(prefix="cmuh_push_commit_") as message_dir:
        msg_path = Path(message_dir) / "message.txt"
        msg_path.write_text(commit_msg, encoding="utf-8")
        cp = run(["git", "commit", "-F", str(msg_path)], check=False)
    if cp.returncode != 0:
        fail("git commit 失敗（可能無實際變更或 hook 阻擋）")


def step6_push(expected_sha: str = "") -> None:
    print("\n=== [9/9] Push ===")
    # 取當前分支
    cp = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False, capture=True)
    branch = cp.stdout.strip() or "main"
    if not branch.startswith("codex/"):
        fail("請在 codex/* 候選分支建立版本；遠端完整 CI 通過後才可發佈同一 SHA 至 main。")
    if expected_sha:
        if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
            fail("待推 SHA 格式無效，已中止推送。")
        if _git_bytes(["rev-parse", "HEAD"]).decode("ascii").strip() != expected_sha:
            fail("待推 SHA 已改變，已中止推送。")
        if branch == "HEAD":
            fail("detached HEAD 沒有明確的交付分支，已中止推送。")
    print(f"  推送至 origin/{branch} ...")
    # 固定來源 SHA，避免最後檢查後分支被移動而把未驗證的新 commit 推出去。
    source = f"{expected_sha}:refs/heads/{branch}" if expected_sha else branch
    cp = run(["git", "push", "origin", source], check=False)
    if cp.returncode != 0:
        # 可能還沒設 remote 或第一次推
        print("\n[提示] git push 失敗。可能原因：")
        print("  - 還沒設定 remote：git remote add origin https://github.com/expertise88864/CMUHdermatology.git")
        print("  - 第一次推送：請確認 codex/* 候選分支與 origin 權限")
        sys.exit(1)


def parse_args(argv: list) -> argparse.Namespace:
    """Parse the complete contract before reading or changing a repository."""
    parser = argparse.ArgumentParser(
        description="安全候選發佈：check 唯讀；publish 使用明確範圍及隔離副本。",
        allow_abbrev=False,
        epilog="舊的裸提交訊息／--sanity-only 不支援；請使用 check 或 publish。")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("check", help="唯讀核對安全、index 版本及 manifest 雜湊",
                        allow_abbrev=False)
    publish = commands.add_parser("publish", help="準備並推送隔離 codex/* 候選",
                                  allow_abbrev=False)
    selection = publish.add_mutually_exclusive_group(required=True)
    selection.add_argument("--path", action="append", help="精確 repo 相對檔名，可重複")
    selection.add_argument("--committed", action="store_true", help="只使用目前 HEAD 的內容")
    publish.add_argument("--source", choices=("index", "worktree"),
                         help="--path 的內容來源，預設 index；worktree 須明確指定")
    publish.add_argument("--output", type=Path, help="repo 外部的新目錄，保留候選與復原紀錄")
    message = publish.add_mutually_exclusive_group(required=True)
    message.add_argument("--message", help="完整提交訊息（含必要 review trailers）")
    message.add_argument("--message-file", type=Path, help="UTF-8 提交訊息檔")
    parsed = parser.parse_args(argv[1:])
    if parsed.command is None:
        parser.print_help()
    elif parsed.command == "publish":
        if parsed.committed and parsed.source:
            parser.error("--source 只能搭配 --path")
        if parsed.message is not None and not parsed.message.strip():
            parser.error("提交訊息不能為空")
    return parsed


def _selection_paths(paths: list[str]) -> list[str]:
    """Only literal, individual files; never directories, ignored data or traversal."""
    result = []
    for value in paths:
        rel = value.replace("\\", "/")
        parts = rel.split("/")
        if (not rel or Path(rel).is_absolute() or any(p in ("", ".", "..", ".git") for p in parts)
                or any(c in rel for c in "\n\r\t:*?\0")
                or rel in (VERSION_REL, MANIFEST_REL)):
            fail(f"--path 必須是精確 repo 相對檔名，版本／manifest 由工具生成：{value}")
        path = REPO_ROOT / rel
        if not path.resolve().is_relative_to(REPO_ROOT.resolve()) or path.is_dir() or path.is_symlink():
            fail(f"不能選擇目錄、符號連結或 repo 外部路徑：{value}")
        ignored = run(["git", "check-ignore", "--no-index", "--", rel],
                      check=False, capture=True)
        if ignored.returncode != 1:
            fail(f"不能選擇忽略檔案，或無法確認 ignore 規則：{value}")
        if rel not in result:
            result.append(rel)
    return result


def _selection_patch(paths: list[str], source: str) -> bytes:
    """Export an exact snapshot without writing the user's index."""
    if source == "index":
        staged = _git_bytes(["--literal-pathspecs", "ls-files", "--stage", "-z", "--", *paths])
        entries = [e.split(b"\t", 1)[0].split() for e in staged.split(b"\0") if e]
        if any(e[2] != b"0" or e[0] not in (b"100644", b"100755") for e in entries):
            fail("選定 index 有衝突或非一般檔案，已中止。")
        return _git_bytes(["--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--cached", "--binary",
                           "--full-index", "--no-renames", "HEAD", "--", *paths])
    # A separate index starts at HEAD, so unrelated staged changes never enter the patch.
    with tempfile.TemporaryDirectory(prefix="cmuh_selected_index_") as directory:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index"),
               "GIT_OPTIONAL_LOCKS": "0"}
        for args in (["read-tree", "HEAD"], ["--literal-pathspecs", "add", "-A", "--", *paths]):
            subprocess.run(["git", *args], cwd=REPO_ROOT, env=env, check=True,
                           capture_output=True)
        cp = subprocess.run(["git", "diff", "--no-ext-diff", "--no-textconv", "--cached", "--binary", "--full-index",
                             "--no-renames", "HEAD"], cwd=REPO_ROOT, env=env,
                            check=True, capture_output=True)
        return cp.stdout


@contextmanager
def _candidate_copy(options, branch: str):
    """Retain a recoverable local clone; never checkout/reset the source worktree."""
    global REPO_ROOT
    original = REPO_ROOT
    head = _git_bytes(["rev-parse", "HEAD"]).decode("ascii").strip()
    paths = _selection_paths(options.path or [])
    source = options.source or "index"
    patch = b"" if options.committed else _selection_patch(paths, source)
    if not options.committed and not patch:
        fail("選定範圍沒有相對 HEAD 的變更；index 模式須先暫存指定檔案。")
    remote = _git_bytes(["remote", "get-url", "origin"]).decode("utf-8").strip()
    identity = {}
    for key in ("user.name", "user.email"):
        cp = run(["git", "config", "--get", key], check=False, capture=True)
        if cp.returncode == 0:
            identity[key] = cp.stdout.strip()
    if options.output:
        receipt_dir = options.output.resolve()
        if receipt_dir.is_relative_to(original.resolve()) or original.resolve().is_relative_to(receipt_dir):
            fail("--output 必須是 repo 外部的新目錄，不能是 repo 的父目錄。")
        if receipt_dir.exists():
            fail("--output 已存在，請選擇新的候選目錄；不覆蓋既有內容。")
        receipt_dir.mkdir(parents=True)
    else:
        receipt_dir = Path(tempfile.mkdtemp(prefix="cmuh_release_"))
    candidate = receipt_dir / "candidate"
    state = {"branch": branch, "source_sha": head, "phase": "prepare",
             "source": "committed" if options.committed else source,
             "paths": paths, "patch_sha256": hashlib.sha256(patch).hexdigest()}
    print(f"[候選副本] {candidate}\n[復原紀錄] {receipt_dir / 'release.json'}")
    def record():
        (receipt_dir / "release.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    record()
    try:
        run(["git", "clone", "--no-hardlinks", "--no-checkout", "--local",
             str(original), str(candidate)])
        REPO_ROOT = candidate
        run(["git", "checkout", "-B", branch, head])
        run(["git", "remote", "set-url", "origin", remote])
        run(["git", "config", "core.hooksPath", ".githooks"])
        for key, value in identity.items():
            run(["git", "config", key, value])
        if patch:
            subprocess.run(["git", "apply", "--index", "--binary", "-"],
                           input=patch, cwd=candidate, check=True, capture_output=True)
        state["phase"] = "candidate_sanity"
        record()
        step1_sanity()
        state["phase"] = "prepared"
        record()
        def verify_source():
            global REPO_ROOT
            REPO_ROOT = original
            try:
                if _git_bytes(["rev-parse", "HEAD"]).decode("ascii").strip() != head:
                    fail("來源 HEAD 已改變；候選保留，請重新確認範圍後驗證。")
                if _git_bytes(["branch", "--show-current"]).decode("utf-8").strip() != branch:
                    fail("來源分支已改變；候選保留，已中止推送。")
                if not options.committed and _selection_patch(paths, source) != patch:
                    fail("選定來源在驗證期間已改變；候選保留，已中止推送。")
            finally:
                REPO_ROOT = candidate
        yield state, record, verify_source
        state["phase"] = "candidate_pushed_awaiting_remote_ci"
    except BaseException:
        state["failed_at"] = state["phase"]
        state["phase"] = "incomplete"
        print(f"[未完成] 候選及紀錄保留於 {receipt_dir}；原工作樹未被提交或還原。")
        raise
    finally:
        REPO_ROOT = original
        record()


def _check_metadata() -> None:
    step1_sanity()
    ver_blob = _git_bytes(["cat-file", "blob", f":{VERSION_REL}"]).decode("utf-8")
    match = re.search(r'CURRENT_VERSION\s*=\s*["\']([^"\']+)["\']', ver_blob)
    if not match:
        fail("index 版本不可讀")
    verify_staged_version_consistency(match.group(1))
    verify_staged_manifest_hashes()
    print("[通過] 唯讀 metadata 檢查；不代表 review、本機完整 CI 或遠端 CI 通過。")


def verify_clean_revision(expected_sha: str) -> None:
    """CI applies only to this unchanged commit, including docs/config/index."""
    actual_sha = _git_bytes(["rev-parse", "HEAD"]).decode("ascii").strip()
    dirty = _git_bytes(["status", "--porcelain", "--untracked-files=all"])
    if actual_sha != expected_sha or dirty.strip():
        fail("最終 SHA／工作樹在驗證期間變動，已中止推送；請重新驗證實際待推版本。")


def step_candidate_gate(emergency_reason: str = "") -> None:
    """Cheap local checks; complete verification runs on the remote candidate."""
    if emergency_reason:
        fail("緊急參數不能繞過候選驗證。")
    run([sys.executable, "-m", "ruff", "check", "src", "scripts", "tests"])
    run([sys.executable, "-m", "unittest", "_test_delivery"])


def main(argv: list) -> int:
    options = parse_args(argv)
    if options.command is None:
        return 0
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        if os.environ.get(key):
            fail(f"偵測到 {key}，請在明確工作副本中執行，避免讀寫另一個 repository。")
    if options.command == "check":
        _check_metadata()
        return 0
    try:
        commit_msg = (options.message_file.read_text(encoding="utf-8-sig")
                      if options.message_file else options.message)
    except (OSError, UnicodeError):
        fail("提交訊息檔不可讀，須為 UTF-8；尚未準備候選。")
    if not commit_msg or not commit_msg.strip() or "\0" in commit_msg:
        fail("提交訊息不能為空或含 NUL；尚未準備候選。")
    emergency_reason = ""
    branch = _git_bytes(["branch", "--show-current"]).decode("utf-8").strip()
    if not branch.startswith("codex/"):
        fail("遠端 CI 流程：請先建立 codex/* 候選分支，不直接修改／推送 main。")

    print("=" * 60)
    print("  CMUHdermatology 一鍵推送")
    print("=" * 60)

    # 環境檢查
    if not (REPO_ROOT / "src" / "cmuh_common" / "version.py").exists():
        fail(f"請在 repo 根目錄執行（目前: {REPO_ROOT}）")

    step1_sanity()
    with _candidate_copy(options, branch) as (state, record, verify_source):
        # Every mutation below targets only the selected, retained candidate copy.
        state["phase"] = "generate"
        record()
    # 先保存使用者欲交付的來源，再生成版本/manifest 與核對 index。
    # 完整 CI 在 commit 之後執行，不能沿用 bump 之前的測試結果。
        fingerprint = snapshot_tracked_sources()
        new_ver = step3_bump_version()
        step4_sync_manifest(new_ver)
    # bump/sync_manifest 合法改寫 version.py 與 manifest.json → 取它們【當下】的內容
    # 當作預期值,一併納入 index 比對。★不可像原本那樣永久排除 version.py★:
    # 若 bump 後被還原,commit 進去的是舊 CURRENT_VERSION、manifest 卻記著新版本與
    # 新雜湊 → 所有機器下載後 SHA256 對不上、更新 fail-closed 全面停更。
        expected = dict(fingerprint)
        expected.update({k: v for k, v in worktree_blob_ids(include_version=True).items()
                     if k in (VERSION_REL, MANIFEST_REL)})
        state["phase"] = "stage_candidate"
        record()
        step5_stage()
        state["phase"] = "verify_candidate_index"
        record()
        verify_index_matches(expected)
        verify_staged_version_consistency(new_ver)
        verify_staged_manifest_hashes()
        state["phase"] = "commit"
        record()
        step5_commit(commit_msg, new_ver, emergency_reason)
        final_sha = _git_bytes(["rev-parse", "HEAD"]).decode("ascii").strip()
        state.update(sha=final_sha, version=new_ver, phase="local_candidate_checks")
        record()
        verify_clean_revision(final_sha)
    # Full CI now runs on the candidate in GitHub. Keep cheap local bug checks.
        step_candidate_gate(emergency_reason)
        verify_index_matches(expected)
        verify_staged_version_consistency(new_ver)
        verify_staged_manifest_hashes()
        verify_clean_revision(final_sha)
        verify_source()
        state["phase"] = "push_candidate"
        record()
        step6_push(final_sha)

    print("\n" + "=" * 60)
    print(f"  已推送 v{new_ver}，SHA {final_sha}；尚待 GitHub CI 全綠核對。")
    print("  本機通過不等於 GitHub CI 成功；請追蹤此完整 SHA 的所有適用檢查。")
    print("  候選分支不供使用端更新；請以 _delivery.py 核對後再發佈同一 SHA 至 main。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("\n[中斷]")
        sys.exit(130)
