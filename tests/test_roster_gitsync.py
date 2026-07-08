# -*- coding: utf-8 -*-
"""GitSyncStorage：以本地 git（bare remote + clone）驗證 pull/commit/push 與退化。"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.gitsync_storage import GitSyncStorage  # noqa: E402


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _has_git(), reason="git 未安裝")


def _git(d, *args):
    return subprocess.run(["git", "-C", str(d), *args],
                          capture_output=True, text=True, check=True)


def _init_repo_with_remote(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)],
                   capture_output=True, check=True)
    subprocess.run(["git", "clone", str(remote), str(work)],
                   capture_output=True, check=True)
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "tester")
    (work / "README").write_text("roster", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "-u", "origin", "HEAD")
    return remote, work


def test_non_git_dir_degrades_to_plain(tmp_path):
    st = GitSyncStorage(str(tmp_path / "roster"))
    st.save_config({"r_members": [{"id": "A"}]})
    assert st.load_config()["r_members"] == [{"id": "A"}]
    assert st._git_ok is False                      # 沒有 .git → 純 storage


def test_save_commits_and_flush_pushes(tmp_path):
    remote, work = _init_repo_with_remote(tmp_path)
    st = GitSyncStorage(str(work))
    st.save_config({"r_members": [{"id": "A"}]})
    log = _git(work, "log", "--oneline").stdout
    assert "roster sync" in log                     # 存檔即本地 commit
    st.flush()                                       # 立即 push
    check = tmp_path / "check"
    subprocess.run(["git", "clone", str(remote), str(check)],
                   capture_output=True, check=True)
    assert (check / "config.json").exists()          # 遠端已有推上去的 config


def test_pull_on_init_gets_remote_changes(tmp_path):
    remote, work = _init_repo_with_remote(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)],
                   capture_output=True, check=True)
    _git(other, "config", "user.email", "o@o")
    _git(other, "config", "user.name", "other")
    (other / "config.json").write_text(
        '{"r_members":[{"id":"Z"}],"schema_version":1}', encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "from other")
    _git(other, "push")
    # 開檔即 pull → 應拿到另一台推的 config
    st = GitSyncStorage(str(work))
    assert st.load_config()["r_members"] == [{"id": "Z"}]


def test_push_without_upstream(tmp_path):
    """git init + add remote 但未 set-upstream → 仍能 push（origin HEAD）。"""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)],
                   capture_output=True, check=True)
    subprocess.run(["git", "init", str(work)], capture_output=True, check=True)
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "tester")
    _git(work, "remote", "add", "origin", str(remote))    # 未 push -u
    st = GitSyncStorage(str(work))
    st.save_config({"r_members": [{"id": "A"}]})
    st.flush()
    check = tmp_path / "check"
    subprocess.run(["git", "clone", str(remote), str(check)],
                   capture_output=True, check=True)
    assert (check / "config.json").exists()


def test_commit_failure_prevents_push(tmp_path, monkeypatch):
    """commit 真失敗（如未設 identity）→ 不排 push、且被記錄，不靜默略過。"""
    _remote, work = _init_repo_with_remote(tmp_path)
    st = GitSyncStorage(str(work))
    real_git = st._git

    def fake_git(*args, **kw):
        if args and args[0] == "commit":
            return subprocess.CompletedProcess(
                args, 128, "", "Please tell me who you are")
        if args and args[0] == "push":
            pytest.fail("commit 失敗後不應 push")
        return real_git(*args, **kw)
    monkeypatch.setattr(st, "_git", fake_git)
    st.save_config({"r_members": [{"id": "X"}]})           # commit 假裝失敗
    assert st.load_config()["r_members"] == [{"id": "X"}]   # 本機存檔仍成功


def test_offline_push_failure_is_non_fatal(tmp_path):
    """遠端不存在（離線）→ push 失敗只警告，存檔/讀取仍正常。"""
    work = tmp_path / "work"
    subprocess.run(["git", "init", str(work)], capture_output=True, check=True)
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "tester")
    st = GitSyncStorage(str(work))                   # 無 remote
    st.save_config({"r_members": [{"id": "A"}]})     # commit ok
    st.flush()                                        # push 失敗但不拋
    assert st.load_config()["r_members"] == [{"id": "A"}]


# ── RF-01/06/07/13 回歸測試 ───────────────────────────────────────────────
def _two_clones(tmp_path):
    """建 bare remote + 兩個設好 identity 的工作 clone（含一個初始 commit）。"""
    remote = tmp_path / "remote.git"
    a = tmp_path / "work_a"
    b = tmp_path / "work_b"
    subprocess.run(["git", "init", "--bare", str(remote)],
                   capture_output=True, check=True)
    subprocess.run(["git", "clone", str(remote), str(a)],
                   capture_output=True, check=True)
    _git(a, "config", "user.email", "a@a")
    _git(a, "config", "user.name", "aaa")
    (a / "README").write_text("roster", encoding="utf-8")
    # 已初始化的共享 repo：.gitignore 早已被 commit（後續 clone 皆取得追蹤版本，
    # _ensure_gitignore 補缺行後不變，不會各機留未追蹤檔撞 ff-only merge）。
    (a / ".gitignore").write_text(
        "*.bak-*\n*.corrupt-*\n*.tmp\nfinalized/\n", encoding="utf-8")
    _git(a, "add", "-A")
    _git(a, "commit", "-m", "init")
    _git(a, "push", "-u", "origin", "HEAD")
    subprocess.run(["git", "clone", str(remote), str(b)],
                   capture_output=True, check=True)
    _git(b, "config", "user.email", "b@b")
    _git(b, "config", "user.name", "bbb")
    return remote, a, b


def test_rf01_periodic_pull_gets_other_machine_change(tmp_path):
    """長駐 B：A 中途改班並 flush → B 週期 pull 一輪即拿到 A 的資料並通知。"""
    _remote, a, b = _two_clones(tmp_path)
    st_a = GitSyncStorage(str(a), pull_interval_sec=0)      # 關背景執行緒，手動觸發
    notified = []
    st_b = GitSyncStorage(str(b), pull_interval_sec=0,
                          on_remote_change=lambda: notified.append(1))
    st_a.save_month("2026-08", {"r_duty": {"2026-08-01": {"person": "A"}}})
    st_a.flush()
    assert st_b.load_month("2026-08").get("r_duty") == {}   # 尚未 pull
    st_b._periodic_pull()                                    # 模擬一輪週期 pull
    assert st_b.load_month("2026-08")["r_duty"] == {"2026-08-01": {"person": "A"}}
    assert notified == [1]                                   # on_remote_change 有被呼叫
    assert st_b.sync_state == "ok"


def test_rf01_divergent_different_files_auto_rebase(tmp_path):
    """A 改 config、B 改 ledger（不同檔）→ rebase 自動復原，兩邊收斂、狀態 ok。"""
    _remote, a, b = _two_clones(tmp_path)
    st_a = GitSyncStorage(str(a), pull_interval_sec=0)
    st_b = GitSyncStorage(str(b), pull_interval_sec=0)
    st_a.save_config({"r_members": [{"id": "A"}]})
    st_a.flush()
    st_b.save_ledger({"r": {"bal": {"X": 1.5}}})           # 不同檔
    st_b.flush()                                            # 分歧 → rebase 自動復原
    assert st_b.sync_state == "ok"
    st_a._periodic_pull()                                   # A 補拉 B 的 ledger
    assert st_a.load_ledger()["r"] == {"bal": {"X": 1.5}}
    assert st_b.load_config()["r_members"] == [{"id": "A"}]


def test_rf01_divergent_same_file_flags_diverged(tmp_path):
    """A、B 改同一檔 → rebase 衝突 → abort、狀態 diverged、工作樹乾淨。"""
    _remote, a, b = _two_clones(tmp_path)
    st_a = GitSyncStorage(str(a), pull_interval_sec=0)
    states = []
    st_b = GitSyncStorage(str(b), pull_interval_sec=0,
                          on_sync_state=lambda s, d: states.append(s))
    st_a.save_config({"r_members": [{"id": "AAA"}]})
    st_a.flush()
    st_b.save_config({"r_members": [{"id": "BBB"}]})       # 同一檔、不同內容
    st_b.flush()
    assert st_b.sync_state == "diverged"
    assert "diverged" in states
    porcelain = _git(b, "status", "--porcelain").stdout
    assert "rebase" not in porcelain.lower()              # rebase --abort 後無殘留
    assert not (b / ".git" / "rebase-merge").exists()
    assert not (b / ".git" / "rebase-apply").exists()


def test_rf06_push_is_serialized_by_git_lock(tmp_path):
    """RF-06：flush() 與 _push() 並發時，任一時刻最多一個 git 操作在跑。"""
    _remote, work = _init_repo_with_remote(tmp_path)
    st = GitSyncStorage(str(work), pull_interval_sec=0)
    st.save_config({"r_members": [{"id": "A"}]})
    import threading as _t
    active = [0]
    peak = [0]
    mlock = _t.Lock()
    real = st._git

    def traced(*a, **k):
        with mlock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            if a and a[0] == "push":
                import time as _time
                _time.sleep(0.3)
            return real(*a, **k)
        finally:
            with mlock:
                active[0] -= 1
    st._git = traced
    t1 = _t.Thread(target=st.flush)
    t2 = _t.Thread(target=st._push)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert peak[0] == 1                                    # 全程序列化，無併發 git


def test_rf07_snapshots_excluded_by_gitignore(tmp_path):
    """RF-07：.gitignore 排除 *.bak-* → 快照不進 repo、不無界膨脹。"""
    remote, work = _init_repo_with_remote(tmp_path)
    st = GitSyncStorage(str(work))
    assert (work / ".gitignore").exists()
    for i in range(3):                                     # 3 次 → 產生 2 個 .bak-*
        st.save_month("2026-08", {"r_duty": {"2026-08-01": {"person": str(i)}}})
    assert list((work / "months").glob("*.bak-*"))         # 本地確實有快照
    st.flush()
    tracked = _git(work, "ls-files").stdout
    assert "bak-" not in tracked                           # 但沒被 git 追蹤
    assert ".gitignore" in tracked
    check = tmp_path / "check"
    subprocess.run(["git", "clone", str(remote), str(check)],
                   capture_output=True, check=True)
    assert not list((check / "months").glob("*.bak-*"))    # 遠端也沒有


def test_gitignore_includes_finalized(tmp_path):
    """codex(794124e)：定案 PDF 目錄 finalized/ 應被 gitignore（純本機、不進 git）。"""
    _remote, work = _init_repo_with_remote(tmp_path)
    st = GitSyncStorage(str(work))
    assert st._git_ok
    lines = (work / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "finalized/" in lines and "*.bak-*" in lines


def test_push_catches_up_uncommitted_change(tmp_path):
    """codex(7657f7a)：_save 因鎖逾時只寫盤未 commit 時，背景 _push 會先補 commit 再推。"""
    import json
    remote, work = _init_repo_with_remote(tmp_path)
    st = GitSyncStorage(str(work), pull_interval_sec=0)
    # 模擬「已寫盤但未 commit」（跳過 _commit 的狀態）
    (work / "config.json").write_text(
        '{"r_members":[{"id":"Z"}],"schema_version":1}', encoding="utf-8")
    st._push()                                     # 背景推送應先補 commit 再推
    check = tmp_path / "check"
    subprocess.run(["git", "clone", str(remote), str(check)],
                   capture_output=True, check=True)
    got = json.loads((check / "config.json").read_text(encoding="utf-8"))
    assert got["r_members"] == [{"id": "Z"}]        # 遠端已收到補收的變更


def test_rf13_git_uses_create_no_window(tmp_path, monkeypatch):
    """RF-13：_git 帶 creationflags=CREATE_NO_WINDOW（Windows 下不閃黑窗）。"""
    if os.name != "nt":
        pytest.skip("僅 Windows 有 CREATE_NO_WINDOW")
    work = tmp_path / "work"
    subprocess.run(["git", "init", str(work)], capture_output=True, check=True)
    st = GitSyncStorage(str(work))
    seen = {}
    real_run = subprocess.run

    def spy_run(*a, **k):
        seen["creationflags"] = k.get("creationflags")
        return real_run(*a, **k)
    monkeypatch.setattr(subprocess, "run", spy_run)
    st._git("status")
    assert seen["creationflags"] & 0x08000000              # CREATE_NO_WINDOW


# ─── RP3-02 / RP3-13 ────────────────────────────────────────────────────────
def test_rp3_02_is_git_repo_accepts_gitfile(tmp_path):
    """[RP3-02] worktree/submodule 的 .git 是「檔案」(gitdir 指標) → 仍應認得是
    repo，不因用 isdir 而誤判成非 repo、靜默停用同步。"""
    base = tmp_path / "wt"
    base.mkdir()
    (base / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n",
                               encoding="utf-8")
    st = GitSyncStorage(str(base), remote_sync=False)
    assert st._git_ok is True


def test_rp3_13_pull_timeout_degrades_offline(tmp_path, monkeypatch):
    """[RP3-13] pull 逾時 → 以本機資料開啟（offline），不卡 UI、不炸背景緒。"""
    st = GitSyncStorage(str(tmp_path / "roster"), remote_sync=False)

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="git pull", timeout=8.0)
    monkeypatch.setattr(st, "_git", boom)
    st._pull()
    assert st.sync_state == "offline"
