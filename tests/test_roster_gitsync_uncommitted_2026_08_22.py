# -*- coding: utf-8 -*-
"""[批次RS-19 / 全審 2026-08-22 P2] 「推成功」不等於「已同步」。

正典檔的存檔順序是:先寫工作樹 → 本機 commit → 排 push。commit 失敗時
(此機沒設 git user.name/email、hook 擋下、index.lock…),最新資料只在工作樹,
而背景 push 仍會把【舊的 HEAD】推成功 —— 舊寫法接著把狀態設成 ok,底部狀態列
顯示「已同步」,他機卻永遠收不到這次修改。
"""
import os
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.gitsync_storage import GitSyncStorage    # noqa: E402


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


def _repo(tmp_path):
    remote, work = tmp_path / "remote.git", tmp_path / "work"
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


def _break_commits(work) -> None:
    """讓 commit 一定失敗:git 對空的 ident 會拒絕(此機沒設 user.name/email
    正是實地最常見的那個原因)。"""
    _git(work, "config", "user.email", "")
    _git(work, "config", "user.name", "")


def test_a_failed_commit_is_not_reported_as_synced(tmp_path):
    """★反例本體★:資料只在工作樹,push 推的是舊 HEAD —— 不可以說已同步。"""
    _remote, work = _repo(tmp_path)
    st = GitSyncStorage(str(work), push_debounce_sec=0.01)
    _break_commits(work)
    st.save_config({"r_members": [{"id": "A"}]})
    assert "roster sync" not in _git(work, "log", "--oneline").stdout, \
        "前提不成立:commit 其實成功了,量不到任何東西"
    assert st._uncommitted, "★沒有察覺本機變更還沒進 commit★"
    st.flush()                                    # 推(舊 HEAD 會推成功)
    assert st.sync_state != "ok", "★畫面會顯示「已同步」,而他機收不到★"


def test_a_healthy_save_is_still_synced(tmp_path):
    """守衛不得讓正常路徑一直亮紅燈。"""
    _remote, work = _repo(tmp_path)
    st = GitSyncStorage(str(work), push_debounce_sec=0.01)
    st.save_config({"r_members": [{"id": "A"}]})
    assert st._uncommitted == ""
    st.flush()
    assert st.sync_state == "ok"


def test_the_state_recovers_once_the_commit_goes_through(tmp_path):
    """★出口★:設好 identity 之後,下一次存檔要能把狀態帶回 ok ——
    否則這個守衛就變成一個永遠關不掉的紅燈。"""
    _remote, work = _repo(tmp_path)
    st = GitSyncStorage(str(work), push_debounce_sec=0.01)
    _break_commits(work)
    st.save_config({"r_members": [{"id": "A"}]})
    assert st._uncommitted
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "tester")
    st.save_config({"r_members": [{"id": "A"}, {"id": "B"}]})
    assert st._uncommitted == ""
    st.flush()
    assert st.sync_state == "ok"


def test_the_state_comes_from_the_measurement_not_the_return_value(
        tmp_path, monkeypatch):
    """★接上去了才存在★:`_commit` 的回傳值只說「這次 commit 有沒有成功」,
    而「已同步」要的是【工作樹裡的正典資料是不是都進了 commit】—— 兩者在
    commit 失敗時才分岔,但要證明狀態真的來自量測,得讓量測說「髒」而 commit
    說「成功」。"""
    _remote, work = _repo(tmp_path)
    st = GitSyncStorage(str(work), push_debounce_sec=0.01)
    monkeypatch.setattr(st, "_canonical_dirt", lambda _paths: "量到的髒東西")
    st.save_config({"r_members": [{"id": "A"}]})       # commit 會成功
    assert st._uncommitted == "量到的髒東西",         "★狀態是從 commit 的回傳值推出來的,不是量出來的★"


def test_the_dirt_is_measured_not_inferred(tmp_path):
    """★量,不要推理★:靠「commit 回傳 True」推斷已同步,會漏掉那些
    根本沒進到這次 commit 的路徑。"""
    _remote, work = _repo(tmp_path)
    st = GitSyncStorage(str(work), push_debounce_sec=0.01)
    st.save_config({"r_members": [{"id": "A"}]})
    assert st._canonical_dirt(["config.json"]) == ""
    (work / "config.json").write_text('{"r_members": []}', encoding="utf-8")
    assert "config.json" in st._canonical_dirt(["config.json"])
    # 量不到 → 一律當成髒的(「查不出來」不可以顯示成「已同步」)
    assert st._canonical_dirt(None)


def test_a_failed_commit_is_published_without_waiting_for_a_push(tmp_path):
    """★量到髒的要立刻說★(外審 RS-19 R1-3):舊寫法只把它記在身上,要等到
    下一次 pull/push/存檔經過 `_set_state` 才反映出來 —— 週期 pull 關掉時
    就是「永遠」。這段期間畫面顯示的是上一次成功同步留下的 ok。
    ★這個測試刻意不呼叫 flush()★:生產的存檔本來就不會順便推一次,
    第一版的測試就是靠 flush() 才綠的(量到的是另一條路)。
    """
    _remote, work = _repo(tmp_path)
    st = GitSyncStorage(str(work), push_debounce_sec=30.0, pull_interval_sec=0)
    st.save_config({"r_members": [{"id": "A"}]})
    st.flush()
    assert st.sync_state == "ok", "前提不成立:一開始就不是 ok,量不到降級"
    _break_commits(work)
    st.save_config({"r_members": [{"id": "A"}, {"id": "B"}]})
    assert st.sync_state != "ok", "★畫面停在「已同步」,而資料還在工作樹★"


def test_a_barrier_write_that_cannot_commit_is_published_too(tmp_path):
    """★臨界區那條路也要立刻發出去★(外審 RS-19 R2-2):accept / finalize
    這些權威寫入正是走臨界區 —— 它們留下的未 commit 變更最不該被顯示成
    「已同步」。這裡讓別的執行緒佔住 git 鎖,重現「拿不到鎖 → 延後 commit」。
    """
    _remote, work = _repo(tmp_path)
    st = GitSyncStorage(str(work), push_debounce_sec=30.0, pull_interval_sec=0)
    st.save_config({"r_members": [{"id": "A"}]})
    st.flush()
    assert st.sync_state == "ok", "前提不成立:一開始就不是 ok,量不到降級"
    held, release = threading.Event(), threading.Event()

    def _hold():
        with st._git_lock:            # 背景 push/pull 正在進行的樣子
            held.set()
            release.wait(20)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert held.wait(5), "前提不成立:沒有真的佔住 git 鎖"
    try:
        with st.write_barrier():      # 權威寫入的形狀
            st.save_config({"r_members": [{"id": "A"}, {"id": "B"}]})
    finally:
        release.set()
        t.join(timeout=10)
    assert st.sync_state != "ok", "★畫面停在「已同步」,而變更還沒 commit★"
