# -*- coding: utf-8 -*-
"""RF-12：自動更新重啟（SystemExit 穿出 mainloop）時 storage.flush() 必須執行。"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import scheduler  # noqa: E402
from cmuh_common.roster.gitsync_storage import GitSyncStorage  # noqa: E402


class _FakeRoot:
    def mainloop(self):
        raise SystemExit(0)          # 模擬 restart_self 的 sys.exit(0)


class _FakeStorage:
    def __init__(self):
        self.flushed = False

    def flush(self):
        self.flushed = True


class _FakeApp:
    def __init__(self, storage):
        self.storage = storage


def test_run_app_flushes_and_releases_on_systemexit(monkeypatch):
    released = []
    monkeypatch.setattr(scheduler, "release_single_instance",
                        lambda: released.append(1))
    st = _FakeStorage()
    app = _FakeApp(st)
    with pytest.raises(SystemExit):
        scheduler._run_app(_FakeRoot(), app)
    assert st.flushed is True                 # SystemExit 路徑仍 flush
    assert released == [1]                     # 且先釋放單例 mutex


def _has_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _has_git(), reason="git 未安裝")
def test_run_app_pushes_pending_commit_on_systemexit(tmp_path, monkeypatch):
    """整合：存檔產生 pending push → SystemExit 收尾 flush → 遠端已收到 commit。"""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)],
                   capture_output=True, check=True)
    subprocess.run(["git", "clone", str(remote), str(work)],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "tester"],
                   capture_output=True, check=True)
    (work / "README").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "HEAD"],
                   capture_output=True, check=True)

    st = GitSyncStorage(str(work), pull_interval_sec=0)
    st.save_config({"r_members": [{"id": "A"}]})   # 本地 commit + 3s 去抖 timer
    monkeypatch.setattr(scheduler, "release_single_instance", lambda: None)
    app = _FakeApp(st)
    with pytest.raises(SystemExit):
        scheduler._run_app(_FakeRoot(), app)       # 收尾 flush 應立即推送
    check = tmp_path / "check"
    subprocess.run(["git", "clone", str(remote), str(check)],
                   capture_output=True, check=True)
    assert (check / "config.json").exists()
