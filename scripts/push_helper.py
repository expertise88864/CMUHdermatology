# -*- coding: utf-8 -*-
"""push.bat 的核心邏輯（用 Python 寫，避免 BAT 在 UTF-8 環境下解析錯誤）。

流程：
  1. sanity check：settings/ 不可被追蹤、.gitignore 完整、version.py 可讀
  2. 確認有 git 變更
  3. 品質關卡：ruff + pyright + pytest + skip 守衛 + 覆蓋率門檻
     紅燈或【工具沒裝】就中止（壞 build 推不出去；尚未 bump/commit）
  4. bump 版本（YYYY.MM.DD.serial）
  5. 同步 manifest.json（含 SHA256）
  6. git add -A → commit → push

用法：
  python scripts/push_helper.py "commit 訊息"
  python scripts/push_helper.py "commit 訊息" --emergency "為什麼非繞過不可"

★[2026-07-30 第二輪外審 P2-08] 這個關卡是【最後一道】，不是第一道★
push 是直推 main，而診間電腦約 5 分鐘內就會自動拉新版 —— GitHub CI 是推上去
之後才跑的，它紅燈的時候壞版本已經在診間了。所以本機關卡缺工具時必須中止，
不能像舊版那樣印一句「CI 仍會把關」就放行。
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


GATE_ARTIFACTS = ("junit.xml", "cov.json")


def _clean_gate_artifacts() -> None:
    for name in GATE_ARTIFACTS:
        try:
            (REPO_ROOT / name).unlink()
        except OSError:
            pass


def step_quality_gate(emergency_reason: str = "") -> None:
    """本機品質關卡。任一紅燈即中止推送（此時尚未 bump 版本、未 commit）。

    ★[2026-07-30 第二輪外審 P2-08] 工具沒裝 →【中止】，不是略過★
    舊版的寫法是：`find_spec(module) is None` 就印一行「已略過，CI 仍會把關」
    然後繼續推。那句話在這個專案是錯的：

      * push 是【直推 main】，CI 是推上去之後才跑的；而診間電腦的自動更新
        大約 5 分鐘內就把新版拉下去。CI 紅燈的時候，壞版本已經在診間了。
      * 而【工具沒裝】正是最可能發生在新機器／重灌後的情境 —— 也就是最需要
        關卡的時候。那一刻退回「不檢查」，等於這把鎖只在不需要它的時候有效
        （跟 P1-06 更新鎖犯過的錯完全一樣）。

    真的遇到緊急狀況（診間壞掉、本機環境壞掉）用
    `--emergency "理由"` —— 要寫理由、會大字印出來、而且會寫進 commit
    訊息裡永久可查。有意識的繞過可以；默默地繞過不行。
    """
    print("\n=== [3/7] 品質關卡（ruff + pyright + pytest + 棘輪）===")
    if emergency_reason:
        print("  " + "!" * 56)
        print("  !! 緊急模式：本次【跳過所有本機品質關卡】")
        print(f"  !! 理由：{emergency_reason}")
        print("  !! 這個理由會寫進 commit 訊息。推完請盡快回頭補跑關卡。")
        print("  " + "!" * 56)
        return

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
             "  ★不能因為「CI 會把關」就放行★：push 是直推 main，CI 是推上去之後\n"
             "  才跑的，而診間電腦約 5 分鐘內就會自動拉新版 —— CI 紅燈時壞版本\n"
             "  已經在診間了。\n"
             '  真的緊急：python scripts/push_helper.py "訊息" '
             '--emergency "為什麼不能先裝工具"')

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

    # 型別債棘輪【刻意只在 CI 跑】：它要把 11 條規則各跑一次 pyright（本機實測約
    # 10 分鐘），每次 push 都等太久 → 會逼人想辦法繞過關卡。而它守的是「被關掉的
    # 規則有沒有新增診斷」，不是正確性；推上去之後 CI 才發現可以接受。
    _clean_gate_artifacts()
    if failed:
        fail("品質關卡未通過（" + ", ".join(failed) + " 紅燈），已中止推送。\n"
             "  尚未 bump 版本、未 commit；請修正上面紅燈後再 push。")


VERSION_REL = "src/cmuh_common/version.py"
MANIFEST_REL = "manifest.json"
# git add -A 會收進來、且值得防還原的範圍
SCAN_PATHS = ["src", "scripts", "tests", MANIFEST_REL]


def _git_bytes(args: list, stdin: bytes = b"") -> bytes:
    cp = subprocess.run(["git", *args], input=stdin, cwd=REPO_ROOT,
                        capture_output=True, text=False)
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
    if not commit_msg or commit_msg.strip() in ("", "1"):
        commit_msg = f"Update v{new_version}"
    if emergency_reason:
        # ★繞過關卡要留下永久紀錄★ —— 只在終端印一行，關掉視窗就沒了。
        # 寫進 commit 訊息後，`git log --grep` 一查就知道哪幾版是未經本機關卡的。
        commit_msg = "\n".join([
            commit_msg,
            "",
            "[緊急推送] 本次跳過本機品質關卡（ruff/pyright/pytest/棘輪）",
            f"理由：{emergency_reason}",
        ])
    # 用 UTF-8 暫存檔 + `git commit -F`：Windows 上 subprocess 會以系統 ANSI(cp936/gbk)
    # 編碼參數，commit message 含 emoji/特殊符號(例 U+232B 退格符)時會 UnicodeEncodeError
    # 而中斷整個推送。改寫成 UTF-8 檔讓 git 自行讀取，與系統 codepage 無關，穩定不踩雷。
    msg_path = REPO_ROOT / ".git" / "PUSH_COMMIT_MSG.txt"
    msg_path.write_text(commit_msg, encoding="utf-8")
    try:
        cp = run(["git", "commit", "-F", str(msg_path)], check=False)
    finally:
        try:
            msg_path.unlink()
        except OSError:
            pass
    if cp.returncode != 0:
        fail("git commit 失敗（可能無實際變更或 hook 阻擋）")


def step6_push() -> None:
    print("\n=== [9/9] Push ===")
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


def parse_args(argv: list) -> tuple:
    """→ (commit_msg, emergency_reason)。

    ★[2026-07-30 外審 P2-08] `--emergency` 一定要帶理由★
    一個不用寫理由的旁路開關，用起來跟「預設就跳過」沒兩樣 —— 幾次之後就變成
    習慣動作。要求打字寫理由，而且理由會進 commit 訊息，讓每一次繞過都留下
    可以被回頭質問的紀錄。
    """
    args = list(argv[1:])
    emergency = ""
    if "--emergency" in args:
        idx = args.index("--emergency")
        rest = args[idx + 1:]
        reason = rest[0].strip() if rest else ""
        if not reason or reason.startswith("--"):
            fail('--emergency 一定要接理由，例如：\n'
                 '  python scripts/push_helper.py "hotfix" '
                 '--emergency "診間全掛，pytest 環境同時壞掉，先推修正"')
        emergency = reason
        args = args[:idx] + rest[1:]
    return " ".join(args), emergency


def main(argv: list) -> int:
    commit_msg, emergency_reason = parse_args(argv)

    print("=" * 60)
    print("  CMUHdermatology 一鍵推送")
    print("=" * 60)

    # 環境檢查
    if not (REPO_ROOT / "src" / "cmuh_common" / "version.py").exists():
        fail(f"請在 repo 根目錄執行（目前: {REPO_ROOT}）")

    step1_sanity()
    if not step2_check_changes():
        return 0
    # [2026-08-02 補審 P1] 指紋要在【品質關卡之前】取。原本在關卡返回【之後】才取,
    # 若 OneDrive 剛好在那個瞬間還原,舊版就成為合法基準、檢查必過 —— 基準本身被
    # 污染,後面驗什麼都沒用。關卡(ruff/pytest)不會改動 src/scripts/tests,提前取樣安全。
    fingerprint = snapshot_tracked_sources()
    step_quality_gate(emergency_reason)
    verify_unchanged_since_tests(fingerprint)
    new_ver = step3_bump_version()
    step4_sync_manifest(new_ver)
    # bump/sync_manifest 合法改寫 version.py 與 manifest.json → 取它們【當下】的內容
    # 當作預期值,一併納入 index 比對。★不可像原本那樣永久排除 version.py★:
    # 若 bump 後被還原,commit 進去的是舊 CURRENT_VERSION、manifest 卻記著新版本與
    # 新雜湊 → 所有機器下載後 SHA256 對不上、更新 fail-closed 全面停更。
    expected = dict(fingerprint)
    expected.update({k: v for k, v in worktree_blob_ids(include_version=True).items()
                     if k in (VERSION_REL, MANIFEST_REL)})
    step5_stage()
    verify_index_matches(expected)
    verify_staged_version_consistency(new_ver)
    step5_commit(commit_msg, new_ver, emergency_reason)
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
