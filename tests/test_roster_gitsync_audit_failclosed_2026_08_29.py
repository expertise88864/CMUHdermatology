# -*- coding: utf-8 -*-
"""[外審第二輪 R2-P2-04] 隱私守衛判定失敗時反而放行 push。

`_outgoing_non_canonical()` 原本的政策是「git 出錯一律回空」,而呼叫端寫的是
`if stray:` —— ★空清單(乾淨)與檢查不出來(未知)被壓成同一格★,於是三種情況
只剩兩種行為:

    檢查說乾淨   → push
    檢查說有髒東西 → 擋
    ★檢查自己壞掉 → push★   ← 方向反了

這道守衛保護的是【一旦 push 就永遠留在 git 歷史裡】的資料(病歷號、救援副本、
定案 PDF…)。兩邊的成本完全不對稱:
  * 暫時不能同步 = 晚一點再推(資料仍在本機,不會遺失);
  * 誤推 = 外洩,之後刪工作樹也沒用,必須改寫歷史。
所以這裡沒有 availability-first 的理由,必須 fail-closed。

★而且不可以用「回 None 代表未知」★:呼叫端本來就是用真假值判斷的,
None 一樣是 falsy —— 同一個陷阱換一種寫法再犯一次。改回 (status, paths)。
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


def _with_remote(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    work = tmp_path / "w"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "commit.gpgsign", "false")
    return remote, work


def _published(remote) -> set:
    r = _git(remote, "-c", "core.quotepath=false",
             "ls-tree", "-r", "--name-only", "HEAD")
    return {ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()}


def _break_the_audit(monkeypatch, st):
    """讓★只有稽核用的那一次 git log★失敗,其餘 git 操作照常。

    ★反例只靠「檢查不出來」分勝負★:repo 是乾淨的、commit/push 都正常,
    差別只在那一次檢查跑不起來。
    """
    real = st._git

    def _fake(*args, **kwargs):
        if args and args[0] == "log":
            raise OSError("模擬:稽核用的 git log 跑不起來")
        return real(*args, **kwargs)
    monkeypatch.setattr(st, "_git", _fake)


class TestAnInconclusiveAuditBlocksThePush:
    def test_it_does_not_publish_when_the_check_cannot_run(self, tmp_path,
                                                           monkeypatch):
        """★核心★:檢查跑不起來 → 這一次不推(而不是當成乾淨)。"""
        remote, work = _with_remote(tmp_path)
        st = GitSyncStorage(str(work), pull_interval_sec=0)
        _break_the_audit(monkeypatch, st)
        st.save_month("2026-08", {"r_duty": {}})
        st.flush()
        assert _published(remote) == set(), (
            f"★檢查不出來卻照推★:{_published(remote)}")
        assert st.sync_state == "error", st.sync_state

    def test_the_message_says_it_will_retry_and_data_is_safe(self, tmp_path,
                                                             monkeypatch):
        """★擋下來要有出口★:訊息必須說得出「資料還在、下次會自動重試」——
        否則使用者只看到同步變紅,不知道自己該不該重做一次。"""
        _remote, work = _with_remote(tmp_path)
        seen = []
        # ★用生產的形狀拿明細★:明細只透過 `on_sync_state(state, detail)` 回報,
        #   沒有存成欄位(UI 是這樣接的)。
        st = GitSyncStorage(str(work), pull_interval_sec=0,
                            on_sync_state=lambda s2, d: seen.append((s2, d)))
        _break_the_audit(monkeypatch, st)
        st.save_month("2026-08", {"r_duty": {}})
        st.flush()
        msg = next((d for s2, d in reversed(seen) if s2 == "error"), "")
        assert "下次存檔" in msg and "本機" in msg, (msg, seen)

    def test_it_recovers_by_itself_once_git_works_again(self, tmp_path,
                                                        monkeypatch):
        """★出口要真的存在★:git 恢復正常之後,不必重開程式就會推出去。"""
        remote, work = _with_remote(tmp_path)
        st = GitSyncStorage(str(work), pull_interval_sec=0)
        _break_the_audit(monkeypatch, st)
        st.save_month("2026-08", {"r_duty": {}})
        st.flush()
        assert _published(remote) == set()
        monkeypatch.undo()                      # git 恢復
        st.save_month("2026-09", {"r_duty": {}})
        st.flush()
        assert "months/2026-08.json" in _published(remote), _published(remote)
        assert st.sync_state == "ok", st.sync_state


class TestTheThreeStatesStayDistinct:
    def test_a_clean_repo_reports_clean_and_pushes(self, tmp_path):
        """★對照組★:乾淨 → clean,而且照推(不可矯枉過正)。"""
        remote, work = _with_remote(tmp_path)
        st = GitSyncStorage(str(work), pull_interval_sec=0)
        st.save_month("2026-08", {"r_duty": {}})
        st.flush()
        assert "months/2026-08.json" in _published(remote)
        assert st.sync_state == "ok"
        branch = (_git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout
                  or "").strip()
        assert st._outgoing_non_canonical("origin", branch) == ("clean", [])

    def test_a_dirty_repo_reports_dirty_with_the_paths(self, tmp_path):
        """★對照組★:有髒東西 → dirty 且列得出路徑(訊息要點名)。"""
        _remote, work = _with_remote(tmp_path)
        (work / "months").mkdir(exist_ok=True)
        (work / "months" / "2026-08-rescue.json").write_text("{}",
                                                             encoding="utf-8")
        _git(work, "add", "-f", "--", "months/2026-08-rescue.json")
        _git(work, "commit", "-q", "-m", "舊版誤收")
        st = GitSyncStorage(str(work), pull_interval_sec=0)
        branch = (_git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout
                  or "").strip()
        status, paths = st._outgoing_non_canonical("origin", branch)
        assert status == "dirty", (status, paths)
        assert "months/2026-08-rescue.json" in paths, paths

    def test_an_unknown_result_is_not_falsy_shaped(self, tmp_path,
                                                   monkeypatch):
        """★未知不可以長成「假的空集合」★:呼叫端是用真假值判斷的,
        回 None/[] 都會讓「檢查壞掉」被當成「乾淨」——同一個陷阱換個寫法。
        狀態要是一個★明確的字串★,而且與 clean 不同。"""
        _remote, work = _with_remote(tmp_path)
        st = GitSyncStorage(str(work), pull_interval_sec=0)
        st.save_month("2026-08", {"r_duty": {}})     # ★要先有 commit★
        _break_the_audit(monkeypatch, st)
        status, paths = st._outgoing_non_canonical("origin", "master")
        assert status == "unknown", (status, paths)
        assert status != "clean"

    def test_an_empty_repo_is_clean_not_unknown(self, tmp_path):
        """★沒有任何 commit ≠ 檢查不出來★:那時本來就不會發佈任何東西,
        判成 unknown 會讓全新的 repo 一開始就卡在紅燈。"""
        d = tmp_path / "roster"
        d.mkdir()
        _git(d, "init", "-q")
        _git(d, "config", "user.email", "t@t")
        _git(d, "config", "user.name", "t")
        st = GitSyncStorage(str(d), pull_interval_sec=0)
        assert st._outgoing_non_canonical("origin", "master") == ("clean", [])

    def test_a_nonzero_git_log_is_unknown_too(self, tmp_path, monkeypatch):
        """★兩種失敗形狀都要蓋到★:git 可能不是拋例外,而是★回非 0★
        (壞掉的 repo、權限問題、objects 損毀)。第一版我只做了拋例外的樁,
        於是「returncode 非 0 也要回 unknown」那條突變不會紅 —— 假綠燈。

        (`{remote}/{branch}..HEAD` 回非 0 是首推的正常情況,所以它會退到
         `log HEAD`;要量的是【連那個也失敗】時的判定。)
        """
        _remote, work = _with_remote(tmp_path)
        st = GitSyncStorage(str(work), pull_interval_sec=0)
        st.save_month("2026-08", {"r_duty": {}})
        real = st._git

        class _R:
            returncode, stdout, stderr = 128, "", "fatal: bad object"

        def _fake(*args, **kwargs):
            return _R() if args and args[0] == "log" else real(*args, **kwargs)
        monkeypatch.setattr(st, "_git", _fake)
        branch = (_git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout
                  or "").strip()
        assert st._outgoing_non_canonical("origin", branch) == ("unknown", [])

    def test_a_failing_commit_probe_is_unknown_not_clean(self, tmp_path,
                                                         monkeypatch):
        """★外審 R1-1:我這一批自己新開的洞★

        第一版用 `_rev_parse("HEAD") is None` 當「這個 repo 還沒有 commit」——
        但 `_rev_parse()` 把【git 執行失敗/回非 0】也映射成 None。於是在剛建立的
        fail-closed 契約上開了一個後門:探測失敗 → 判 clean → 夾帶 PHI 的未推
        commit 照樣被發佈。★便利的判斷式不等於那個狀態★(又一次)。

        反例:repo ★有★ commit(而且是髒的),只有那次探測失敗。
        """
        remote, work = _with_remote(tmp_path)
        (work / "months").mkdir(exist_ok=True)
        (work / "months" / "2026-08-rescue.json").write_text("{}",
                                                             encoding="utf-8")
        _git(work, "add", "-f", "--", "months/2026-08-rescue.json")
        _git(work, "commit", "-q", "-m", "舊版誤收(未推)")
        st = GitSyncStorage(str(work), pull_interval_sec=0)
        real = st._git

        def _fake(*args, **kwargs):
            if args and args[0] == "rev-list":
                raise OSError("模擬:探測 commit 的那次 git 跑不起來")
            return real(*args, **kwargs)
        monkeypatch.setattr(st, "_git", _fake)
        branch = (_git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout
                  or "").strip()
        assert st._outgoing_non_canonical("origin", branch) == ("unknown", [])
        st.save_month("2026-08", {"r_duty": {}})
        st.flush()
        assert "months/2026-08-rescue.json" not in _published(remote), (
            f"★探測失敗被當成乾淨,髒 commit 被發佈了★:{_published(remote)}")

    def test_a_nonzero_commit_probe_is_unknown_too(self, tmp_path,
                                                   monkeypatch):
        """同一條的另一種失敗形狀:探測★回非 0★(壞掉的 repo)。"""
        _remote, work = _with_remote(tmp_path)
        st = GitSyncStorage(str(work), pull_interval_sec=0)
        st.save_month("2026-08", {"r_duty": {}})
        real = st._git

        class _R:
            returncode, stdout, stderr = 128, "", "fatal: bad object"

        def _fake(*args, **kwargs):
            return (_R() if args and args[0] == "rev-list"
                    else real(*args, **kwargs))
        monkeypatch.setattr(st, "_git", _fake)
        assert st._outgoing_non_canonical("origin", "master") == ("unknown", [])
