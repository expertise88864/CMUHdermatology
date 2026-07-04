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
