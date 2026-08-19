# -*- coding: utf-8 -*-
"""[2026-08-02 第二輪外審 P2-05] GitSync 用 `git add -A`,而 .gitignore 寫失敗只是警告。

兩件事單獨看都還好,合起來就是「把不該進 private repo 的東西整包推出去」:

  * `_ensure_gitignore()` 寫入失敗 → 只記一行 warning 就繼續(OSError:唯讀、
    被防毒鎖住、磁碟滿…)。
  * `_commit()` 用 `git add -A` → working tree 裡【所有沒被忽略的東西】都進 commit。

於是 `.bak-*` 快照、`.corrupt-*` 壞檔備份、`*.tmp` 暫存、`finalized/` 的定案 PDF、
以及使用者為了解衝突手工留下的救援副本,全都會被推到遠端。

★而我自己剛把情況變嚴重★:2026-08-02 我把 `_snapshot` 移進 `RosterStorage._save()`,
讓週色/年度假日表/門診模板/Clerk 梯次/切片格網也開始產生 `.bak-`——
等於把這個既有缺口的影響面從「月檔快照」擴大到「幾乎每個設定檔的每一次存檔」。
我當時還跟使用者說「.bak-* 已被 .gitignore 排除,不會有 git 雜訊」,那句話少了
「前提是 .gitignore 真的寫成功了」。

修法歷經外審四輪才收斂,每一輪都是同一個洞的下一層:
  1. 不再 `add -A`,只 stage 白名單;`.gitignore` 寫不成功即 fail-closed。
  2. `commit` 也要限定 pathspec —— 白名單只約束 add 的話,index 裡既有的東西
     照樣會被 commit 帶出去。
  3. 只收正典檔名 `YYYY-MM.json`,擋掉 `2026-08-rescue.json` 這種救援副本。
  4. fail-closed 不能只擋 commit:狀態燈不可被成功的 pull 漆綠,push 也要擋,
     而且升級前遺留的「未推舊 commit」同樣不可被發佈出去。
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.gitsync_storage import GitSyncStorage  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, encoding="utf-8",
                          errors="replace", check=False)


def _has_git():
    try:
        return subprocess.run(["git", "--version"], capture_output=True,
                              check=False).returncode == 0
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _has_git(), reason="無 git")


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "roster"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "config", "commit.gpgsign", "false")
    return d


def _with_remote(tmp_path):
    """建 bare remote + clone,回 (remote, work)。"""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    work = tmp_path / "w"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "commit.gpgsign", "false")
    return remote, work


def _published(remote) -> set:
    """遠端 bare repo 的 HEAD 樹 —— 也就是「真的被發佈出去的東西」。

    ★查遠端要用 `-C <remote>`★ 我第一版是把 remote 路徑當【pathspec】丟給另一個
    repo,結果永遠是空集合 —— 於是「不該被發佈」的斷言變成拿空集合比對、恆為真。
    又一次假綠燈。
    """
    r = _git(remote, "-c", "core.quotepath=false",
             "ls-tree", "-r", "--name-only", "HEAD")
    return {ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()}


def _tracked(d) -> set:
    # core.quotepath=false：否則 git 會把非 ASCII 檔名跳脫成 "\346\210\221…"，
    # 字串比對永遠對不上 —— 同樣是我第一版拿到假綠燈的原因。
    r = _git(d, "-c", "core.quotepath=false", "ls-files")
    return {ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()}


def _block_gitignore(monkeypatch):
    real_open = open

    def _no_write(path, *a, **k):
        if str(path).endswith(".gitignore") and a and "w" in str(a[0]):
            raise OSError("模擬：.gitignore 無法寫入")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", _no_write)


# ─── 只 stage 白名單 ───────────────────────────────────────────────────────
def test_snapshots_and_backups_never_enter_the_repo(repo):
    """★核心★ 快照/壞檔備份/暫存/定案 PDF/手工救援副本都不可被 commit 進去。"""
    st = GitSyncStorage(str(repo), pull_interval_sec=0)
    st.save_config({"r_members": [{"id": "A"}]})
    st.save_config({"r_members": [{"id": "B"}]})       # 第二次 → 產生 .bak-
    st.save_month("2026-08", {"r_duty": {}})
    (repo / "config.json.corrupt-20260101").write_text("x", encoding="utf-8")
    (repo / "scratch.tmp").write_text("x", encoding="utf-8")
    (repo / "finalized").mkdir(exist_ok=True)
    (repo / "finalized" / "115年08月定案.pdf").write_bytes(b"%PDF-1.4")
    (repo / "我自己手動存的備份.json").write_text("{}", encoding="utf-8")
    st.flush()

    tracked = _tracked(repo)
    assert "config.json" in tracked and "months/2026-08.json" in tracked
    leaked = [p for p in tracked
              if ".bak-" in p or ".corrupt-" in p or p.endswith(".tmp")
              or p.startswith("finalized/") or "手動存的備份" in p]
    assert not leaked, f"★這些不該進 private repo★：{leaked}"


def test_the_canonical_data_files_are_all_still_synced(repo):
    """★不可矯枉過正★ 白名單不可以漏掉任何一個真正要跨機同步的檔,
    否則另一台電腦看到的設定是殘缺的(比多推幾個備份更糟)。"""
    from datetime import date
    st = GitSyncStorage(str(repo), pull_interval_sec=0)
    st.save_config({"r_members": [{"id": "A"}]})
    st.save_ledger({"r": {"A": 1.0}})
    st.save_biopsy({"counts": {"A": 1}})
    st.save_week_colors(2026, {"2026-W31": "pink"})
    st.save_holiday_duty({"r": {date(2026, 1, 1): "A"}, "vs": {}})
    st.save_clinic_template({"template": {}})
    st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03"}])
    st.save_biopsy_grid({"b1": {}})
    st.save_month("2026-08", {"r_duty": {}})
    st.mark_pending_settle("r", "2026-08")   # 未完成的結算意圖也要同步
    st.flush()

    tracked = _tracked(repo)
    for name in ("config.json", "ledger.json", "biopsy.json",
                 "week_colors.json", "holiday_duty.json",
                 "clinic_template.json", "clerk_batches.json",
                 "biopsy_grid.json", "pending_settle.json",
                 "months/2026-08.json", ".gitignore"):
        assert name in tracked, f"★{name} 沒有被同步★（他機會看到殘缺設定）"


def test_a_deleted_month_is_propagated(repo):
    """白名單 staging 仍要能傳遞【刪除】—— 否則他機永遠留著已被刪掉的月份。"""
    st = GitSyncStorage(str(repo), pull_interval_sec=0)
    st.save_month("2026-08", {"r_duty": {}})
    st.flush()
    assert "months/2026-08.json" in _tracked(repo)

    os.remove(st._month_path("2026-08"))
    st.save_config({"r_members": []})       # 觸發下一次 commit
    st.flush()

    assert "months/2026-08.json" not in _tracked(repo)


def test_rescue_copies_under_months_are_not_synced(repo):
    """★[外審第3輪] 只收正典檔名 YYYY-MM.json★

    人工解衝突時很容易在 months/ 底下留下「2026-08-rescue.json」
    「2026-08 - 複製.json」之類的副本 —— 只看副檔名就會把它們一起推出去。
    """
    st = GitSyncStorage(str(repo), pull_interval_sec=0)
    st.save_month("2026-08", {"r_duty": {}})
    m = repo / "months"
    (m / "2026-08-rescue.json").write_text("{}", encoding="utf-8")
    (m / "2026-08 - 複製.json").write_text("{}", encoding="utf-8")
    (m / "notes.json").write_text("{}", encoding="utf-8")
    st.flush()

    tracked = _tracked(repo)
    assert "months/2026-08.json" in tracked
    assert not [p for p in tracked if p.startswith("months/")
                and p != "months/2026-08.json"], tracked


def test_an_already_tracked_rescue_file_is_not_kept_alive(repo):
    """舊版 `add -A` 時期可能已經把救援副本收進 repo。ls-files 會把它吐回來,
    不濾掉就等於每次 commit 都自動幫它續命。"""
    st = GitSyncStorage(str(repo), pull_interval_sec=0)
    st.save_month("2026-08", {"r_duty": {}})
    st.flush()
    (repo / "months" / "2026-08-rescue.json").write_text("{}", encoding="utf-8")
    _git(repo, "add", "-f", "--", "months/2026-08-rescue.json")
    _git(repo, "commit", "-q", "-m", "舊版誤收")
    assert "months/2026-08-rescue.json" in _tracked(repo)   # 前提

    st.save_month("2026-09", {"r_duty": {}})
    st.flush()

    r = _git(repo, "-c", "core.quotepath=false", "show", "--name-only",
             "--format=", "HEAD")
    touched = {ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()}
    assert "months/2026-08-rescue.json" not in touched, \
        f"最後一次 commit 不該再碰那個救援副本：{touched}"


# ─── .gitignore 寫不成功 → fail-closed ────────────────────────────────────
def test_a_failed_gitignore_stops_syncing_instead_of_leaking(repo, monkeypatch):
    """★寫不成 .gitignore 就不要同步★

    原本只記一行 warning 然後照樣 `add -A` —— 那正是把備份推出去的那條路。
    現在:同步狀態轉 error(底部狀態列看得到),而且不 commit。
    """
    _block_gitignore(monkeypatch)          # ★鎖【持續】存在 —— 否則重試會成功
    st = GitSyncStorage(str(repo), pull_interval_sec=0)

    st.save_config({"r_members": [{"id": "A"}]})
    st.flush()
    monkeypatch.undo()

    assert st.sync_state == "error", f"實際 sync_state={st.sync_state}"
    assert "config.json" not in _tracked(repo), "★沒有防護就不該 commit★"


def test_git_info_exclude_is_the_second_layer(repo):
    """.gitignore 是會被同步的檔(他機可能改壞);.git/info/exclude 純本機、
    改不到也同步不掉,拿來當第二層。"""
    GitSyncStorage(str(repo), pull_interval_sec=0)
    p = repo / ".git" / "info" / "exclude"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    for pat in ("*.bak-*", "*.corrupt-*", "*.tmp", "finalized/"):
        assert pat in text, f"{pat} 不在 .git/info/exclude"


def test_a_successful_pull_cannot_paint_the_status_green(repo, monkeypatch):
    """★[外審第4輪] fail-closed 只擋 commit 是不夠的★

    一次成功的週期性 pull 會把 sync_state 設回 "ok" → 底部狀態列顯示「已同步」,
    而本機每一次存檔其實都被拒絕 commit。使用者看到綠燈、資料卻停在本機。
    """
    _block_gitignore(monkeypatch)
    st = GitSyncStorage(str(repo), pull_interval_sec=0)
    monkeypatch.undo()
    assert st.sync_state == "error"          # 前提

    st._set_state("ok")                       # 週期 pull 成功走的就是這條

    assert st.sync_state == "error", "★綠燈謊報:commit 其實全被拒絕★"


def test_the_status_goes_green_again_once_gitignore_is_fixed(repo):
    """★不可矯枉過正★ .gitignore 一切正常時,狀態當然要能回到 ok。"""
    st = GitSyncStorage(str(repo), pull_interval_sec=0)
    st._set_state("offline", "測試")
    st._set_state("ok")
    assert st.sync_state == "ok"


# ─── push 路徑:遺留的未推 commit 同樣不可發佈 ─────────────────────────────
def test_a_pending_local_commit_is_not_pushed_without_gitignore(tmp_path,
                                                                monkeypatch):
    """★[外審第6輪] fail-closed 不能只擋 commit★

    本機可能還躺著【舊版 `git add -A` 時期】收進來的未推 commit —— 裡面就有救援
    副本、快照、定案 PDF。擋住新的 commit 卻照樣 push,那些照樣會被發佈出去。
    """
    remote, work = _with_remote(tmp_path)
    (work / "months").mkdir(exist_ok=True)
    (work / "months" / "2026-08-rescue.json").write_text("{}", encoding="utf-8")
    _git(work, "add", "-f", "--", "months/2026-08-rescue.json")
    _git(work, "commit", "-q", "-m", "舊版誤收(未推)")

    _block_gitignore(monkeypatch)          # ★鎖【持續】存在
    st = GitSyncStorage(str(work), pull_interval_sec=0)

    st.save_config({"r_members": []})
    st.flush()
    monkeypatch.undo()

    published = _published(remote)
    assert "months/2026-08-rescue.json" not in published, \
        f"★未推的舊 commit 被發佈出去了★：{published}"
    assert published == set(), "前提:這次根本不該推出任何東西"


def test_a_legacy_commit_is_not_published_even_with_gitignore_ok(tmp_path):
    """★[外審第7輪] 白名單只管【新的】commit★

    升級前本機若躺著舊版 `add -A` 造出、還沒推出去的 commit,push 會把整條 HEAD
    祖先一起發佈。這次 .gitignore 一切正常,所以前一輪的 fail-closed 擋不到。
    """
    remote, work = _with_remote(tmp_path)
    (work / "months").mkdir(exist_ok=True)
    (work / "months" / "2026-08-rescue.json").write_text("{}", encoding="utf-8")
    _git(work, "add", "-f", "--", "months/2026-08-rescue.json")
    _git(work, "commit", "-q", "-m", "舊版 add -A 誤收(離線未推)")

    st = GitSyncStorage(str(work), pull_interval_sec=0)
    st.save_config({"r_members": []})
    st.flush()

    published = _published(remote)
    assert "months/2026-08-rescue.json" not in published, \
        f"★舊 commit 被發佈出去了★：{published}"
    assert st.sync_state == "error", "而且要讓使用者知道卡在哪、怎麼處理"


def test_a_clean_repo_still_pushes(tmp_path):
    """★不可矯枉過正★ 沒有髒東西時,推送必須照常成功。"""
    remote, work = _with_remote(tmp_path)
    st = GitSyncStorage(str(work), pull_interval_sec=0)
    st.save_month("2026-08", {"r_duty": {}})
    st.flush()

    published = _published(remote)
    assert "months/2026-08.json" in published, f"正常內容要推得出去：{published}"
    assert st.sync_state == "ok"


def test_the_documented_cleanup_can_actually_be_pushed(tmp_path):
    """★[外審第8輪] 守衛不可以擋住我們自己給的修復指示★

    `git diff --name-only` 連【被刪掉】的路徑也會列出來。於是使用者照訊息去做
    `git rm --cached months/2026-08-rescue.json`,那筆刪除又被判成一條非正典路徑
    → 永遠推不出去,已經發佈到遠端的髒檔就再也清不掉。
    """
    remote, work = _with_remote(tmp_path)
    # 先讓遠端已經有一個髒檔(模擬舊版早就推上去了)
    (work / "months").mkdir(exist_ok=True)
    (work / "months" / "2026-08-rescue.json").write_text("{}", encoding="utf-8")
    _git(work, "add", "-f", "--", "months/2026-08-rescue.json")
    _git(work, "commit", "-q", "-m", "舊版誤收")
    _git(work, "push", "-q", "origin", "HEAD")
    assert "months/2026-08-rescue.json" in _published(remote)   # 前提

    # 使用者照訊息做清理
    _git(work, "rm", "-q", "--cached", "--", "months/2026-08-rescue.json")
    _git(work, "commit", "-q", "-m", "清掉誤收的救援副本")

    st = GitSyncStorage(str(work), pull_interval_sec=0)
    st.save_month("2026-08", {"r_duty": {}})
    st.flush()

    assert st.sync_state == "ok", f"清理本身不該被擋:{st.sync_state}"
    published = _published(remote)
    assert "months/2026-08-rescue.json" not in published, \
        f"★清理沒有被推出去,遠端的髒檔還在★:{published}"
    assert "months/2026-08.json" in published


def test_a_transient_gitignore_failure_recovers_without_a_restart(repo,
                                                                  monkeypatch):
    """★[外審第11輪] 暫時性失敗不可讓整個行程永久停用同步★

    `_gitignore_ok` 原本只在 __init__ 判定一次。開程式當下若剛好被防毒/備份軟體
    鎖住幾秒,commit 與 push 就永久停擺,使用者得重開程式才會恢復 ——
    而畫面只說「.gitignore 未就緒」,看不出是暫時的。
    """
    _block_gitignore(monkeypatch)
    st = GitSyncStorage(str(repo), pull_interval_sec=0)
    monkeypatch.undo()                      # 鎖檔的那幾秒過去了
    assert st.sync_state == "error"         # 前提

    st.save_config({"r_members": [{"id": "A"}]})
    st.flush()

    assert "config.json" in _tracked(repo), "★恢復之後仍然不同步★"
    assert st.sync_state == "ok", f"狀態要跟著回綠:{st.sync_state}"


def test_a_file_added_then_deleted_in_unpushed_commits_still_blocks(tmp_path):
    """★[外審第12輪] 淨差比對會漏掉「先加後刪」★

    `diff base..HEAD` 什麼都看不到,但 push 會把【兩個 commit 都】送出去 ——
    那個檔就永遠留在 git 歷史裡,任何拿得到 repo 的人都翻得出來。
    """
    remote, work = _with_remote(tmp_path)
    (work / "months").mkdir(exist_ok=True)
    secret = work / "months" / "2026-08-rescue.json"
    secret.write_text('{"病人":"不該外流"}', encoding="utf-8")
    _git(work, "add", "-f", "--", "months/2026-08-rescue.json")
    _git(work, "commit", "-q", "-m", "舊版誤收")
    _git(work, "rm", "-q", "--", "months/2026-08-rescue.json")
    _git(work, "commit", "-q", "-m", "後來又刪掉了")

    st = GitSyncStorage(str(work), pull_interval_sec=0)
    st.save_month("2026-08", {"r_duty": {}})
    st.flush()

    r = _git(remote, "log", "--format=", "--name-only", "--all")
    in_history = {ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()}
    assert "months/2026-08-rescue.json" not in in_history, \
        f"★檔案雖已刪除,仍隨著歷史被推出去★：{in_history}"
    assert st.sync_state == "error"
