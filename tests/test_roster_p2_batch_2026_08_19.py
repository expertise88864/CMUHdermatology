# -*- coding: utf-8 -*-
"""[批次RS-3] 排班審 P2 群:匯出一致快照 / 更新重啟的 Git 交棒 / 單例三態。

三條的共同形狀都是「把【不知道】或【拼裝的】當成安全」:
* 匯出:依序讀好幾個檔,中間可以被背景同步插入 → 一份正式班表是拼裝品;
* 交棒:先放 mutex 再做網路 push → 新舊兩代同時操作同一個 `.git`;
* 單例:mutex API 壞掉時回「拿到了」→ 把查不出來說成安全。
"""
import inspect
import subprocess
import threading
import time

import pytest

from cmuh_common import single_instance as si
from cmuh_common.roster.service import RosterService
from cmuh_common.roster.storage import RosterStorage


@pytest.fixture()
def st(tmp_path):
    s = RosterStorage(str(tmp_path / "roster"))
    s.save_config({"r_members": [{"id": "K"}], "vs_members": [],
                   "pgy_members": [], "clerk_members": []})
    return s


class TestTheExportIsOneConsistentSnapshot:
    """[P2-04] 匯出會依序讀 config → 月檔 → 年度假日 → 帳本,之後 build_day_input
    又再讀模板/梯次/切片格網/上月檔。背景同步插在任何兩次讀之間,那份 Excel/PDF
    就是「R/VS 舊版 + PGY 新版 + 帳本舊版」的拼裝品 —— 而它是要發出去的正式班表。"""

    def test_no_one_can_write_while_the_export_is_reading(self, st):
        svc = RosterService(st)
        st.save_month("2026-09", {"r_duty": {}})
        done: list = []
        started = threading.Event()
        seen_inside: list = []

        real_ledger = st.load_ledger

        def _slow_ledger():
            # 匯出讀到一半 —— 這一刻他機想寫進來
            seen_inside.append(True)
            t = threading.Thread(target=_writer)
            t.start()
            started.wait(timeout=5)
            t.join(timeout=0.6)
            return real_ledger()

        def _writer():
            started.set()
            st.save_config({"r_members": [{"id": "他機改的"}]})
            done.append(True)

        st.load_ledger = _slow_ledger                            # type: ignore
        try:
            svc.build_export("2026-09")
        finally:
            st.load_ledger = real_ledger                         # type: ignore
        assert seen_inside, "測試沒有真的插進匯出中間 —— 什麼都沒量到"
        assert not done, "★匯出讀到一半,他機的寫入插了進來(拼裝快照)★"

    def test_the_export_body_is_guarded(self):
        src = inspect.getsource(RosterService.build_export)
        assert "self.storage.write_barrier()" in src


class TestTheRestartHandsOverTheRepoCleanly:
    """[P2-02] 舊寫法是「先放 mutex 再 flush」,而 flush 會 fetch/merge/push
    (離線可等 30 秒)—— 新一代只等 mutex 1.5 秒就開始 startup pull,兩代同時
    操作同一個 `.git`(working tree/index/HEAD)。"""

    @staticmethod
    def _repo_with_remote(tmp_path):
        """★反例要有 remote★:沒有 remote 時 `_push` 一進去就 early-return,
        「quiesce 有沒有碰網路」根本分不出勝負(第一版就是這樣量不到東西)。"""
        remote = tmp_path / "remote.git"
        work = tmp_path / "work"
        subprocess.run(["git", "init", "--bare", str(remote)],
                       capture_output=True, check=True)
        subprocess.run(["git", "clone", str(remote), str(work)],
                       capture_output=True, check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "tester")):
            subprocess.run(["git", "-C", str(work), "config", k, v],
                           capture_output=True, check=True)
        (work / "README").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(work), "add", "-A"],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(work), "commit", "-m", "init"],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "HEAD"],
                       capture_output=True, check=True)
        return remote, work

    def test_quiesce_does_not_touch_the_network(self, tmp_path, monkeypatch):
        from cmuh_common.roster.gitsync_storage import GitSyncStorage
        _remote, work = self._repo_with_remote(tmp_path)
        gst = GitSyncStorage(str(work), pull_interval_sec=0)
        gst.save_config({"r_members": [{"id": "A"}]})
        net: list = []
        real_git = gst._git

        def _watch(*args, **kw):
            if args and args[0] in ("fetch", "push", "pull", "ls-remote"):
                net.append(args[0])
            return real_git(*args, **kw)

        monkeypatch.setattr(gst, "_git", _watch)
        gst.quiesce_local()
        assert not net,             f"★quiesce 碰了網路 {net} —— 交棒會被離線 timeout 卡住 30 秒★"

    def test_quiesce_commits_what_the_save_could_not(self, tmp_path):
        """★反例要是「寫了盤但還沒 commit」★:存檔時 git 忙碌會略過 commit
        (既有設計:檔案先寫盤、下次補收)。交棒前那一筆必須被收進來,否則
        新一代 push 不到它,變更留在這台機器上。"""
        from cmuh_common.roster.gitsync_storage import GitSyncStorage
        _remote, work = self._repo_with_remote(tmp_path)
        # ★去抖拉長★:預設 3 秒的 push timer 會在 `_push_locked_body` 開頭做
        #   「背景補收」commit —— 它一旦在測試中途插進來,這條反例就不是只由
        #   `quiesce_local` 的 commit 決定勝負了(會偶爾自己變綠)。
        gst = GitSyncStorage(str(work), pull_interval_sec=0,
                             push_debounce_sec=600)

        holder_done = threading.Event()
        release = threading.Event()

        def _hold():
            with gst._git_lock:                  # 讓存檔拿不到 git 鎖
                holder_done.set()
                release.wait(timeout=10)

        t = threading.Thread(target=_hold)
        t.start()
        assert holder_done.wait(timeout=5)
        gst.save_config({"r_members": [{"id": "只寫了盤"}]})   # commit 被略過
        release.set()
        t.join(timeout=10)
        log_before = subprocess.run(
            ["git", "-C", str(work), "log", "--name-only", "--pretty=format:"],
            capture_output=True, text=True).stdout or ""
        assert "config.json" not in log_before,             "前提不成立:這一筆應該還沒被 commit(反例沒建立起來)"

        gst.quiesce_local()
        log_after = subprocess.run(
            ["git", "-C", str(work), "log", "--name-only", "--pretty=format:"],
            capture_output=True, text=True).stdout or ""
        assert "config.json" in log_after,             "★交棒前沒有把已寫盤但未 commit 的變更收進來★"

    def test_the_mutex_is_released_before_the_slow_work(self):
        """★時間預算★(RS-3 第 1 輪 P1):新行程搶 mutex 只重試 1.5 秒,而
        `restart_self` 為確認它沒早夭已先等掉約 0.6 秒。把 quiesce(可能等 git
        鎖、join 執行緒)放進那不到 1 秒的窗口,新行程會判定「已在執行中」而
        退出、舊行程隨後也退出 —— ★整個排班程式消失★。
        所以順序必須是:spawn 之前先 quiesce;`on_confirmed` 裡第一件事是放
        mutex。"""
        import scheduler
        order: list = []

        class _St:
            def quiesce_local(self):
                order.append("quiesce")

            def resume_sync(self):
                order.append("resume")

        class _App:
            storage = _St()

        def _fake_restart(on_confirmed=None, **kw):
            order.append("spawn")
            if on_confirmed:
                on_confirmed()
            return None

        real_restart = scheduler.restart_self
        real_release = scheduler.release_single_instance
        scheduler.restart_self = _fake_restart              # type: ignore
        scheduler.release_single_instance = (                # type: ignore
            lambda: order.append("release"))
        scheduler._HANDING_OVER = False
        try:
            scheduler._handover_and_restart(_App())
        finally:
            scheduler.restart_self = real_restart           # type: ignore
            scheduler.release_single_instance = real_release  # type: ignore
            handed = scheduler._HANDING_OVER
            scheduler._HANDING_OVER = False
        assert order[0] == "quiesce", f"quiesce 要在 spawn 之前 {order}"
        assert order.index("release") == order.index("spawn") + 1,             f"★釋放 mutex 必須是 on_confirmed 的第一件事★ {order}"
        assert "resume" not in order, "接班成立就不該把同步收回來"
        assert handed, "接班成立要記下交棒(收尾才不會再去碰 repo)"

    def test_a_failed_restart_restores_sync_and_stays_a_normal_close(self):
        """★接班沒成立時舊行程會繼續活著★:同步要收回來,而且交棒旗標不可以
        留著 —— 否則之後的一般關閉會被誤判成交棒而不 push(變更留在本機)。"""
        import scheduler
        order: list = []

        class _St:
            def quiesce_local(self):
                order.append("quiesce")

            def resume_sync(self):
                order.append("resume")

        class _App:
            storage = _St()

        real_restart = scheduler.restart_self
        scheduler.restart_self = (                           # type: ignore
            lambda on_confirmed=None, **kw: order.append("spawn-failed"))
        scheduler._HANDING_OVER = False
        try:
            scheduler._handover_and_restart(_App())
        finally:
            scheduler.restart_self = real_restart            # type: ignore
            handed = scheduler._HANDING_OVER
            scheduler._HANDING_OVER = False
        assert order == ["quiesce", "spawn-failed", "resume"], order
        assert not handed,             "★重啟沒成立卻留著交棒旗標 → 之後的一般關閉不會 push★"

    def test_a_normal_close_still_pushes(self):
        """★沒有接班的人時仍然要 push★:一般關閉若只 commit 到本機,他機要等到
        這台下次被打開才看得到 —— 那是把 P2-02 修成另一個更常見的缺陷。"""
        import scheduler
        src = inspect.getsource(scheduler._run_app)
        assert "quiesce_local()" in src and "storage.flush()" in src, \
            "★兩種關閉要有兩種收尾★"
        i = src.index("handing_over")
        assert "if handing_over" in src[i:], "要依「有沒有接班」分流"

    def test_the_next_generation_pushes_what_the_last_one_left(self, tmp_path):
        """★量行為★:上一代只 commit 到本機就交棒 —— 新一代啟動時必須把它推
        出去,否則那些變更要等到有人再存一次檔才會出去(他機看不到)。"""
        from cmuh_common.roster.gitsync_storage import GitSyncStorage
        remote, work = self._repo_with_remote(tmp_path)
        # 模擬上一代:commit 了但沒 push
        (work / "config.json").write_text('{"r_members": [{"id": "上一代"}]}',
                                          encoding="utf-8")
        subprocess.run(["git", "-C", str(work), "add", "config.json"],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(work), "commit", "-m", "上一代的變更"],
                       capture_output=True, check=True)

        GitSyncStorage(str(work), pull_interval_sec=0, push_debounce_sec=0.05)
        deadline = time.monotonic() + 15
        pushed = False
        while time.monotonic() < deadline:
            out = subprocess.run(
                ["git", "-C", str(remote), "log", "--name-only",
                 "--pretty=format:"], capture_output=True, text=True).stdout
            if "config.json" in (out or ""):
                pushed = True
                break
            time.sleep(0.2)
        assert pushed, "★新一代啟動沒有補推 → 上一代的變更留在本機★"


class TestUnknownIsNotSuccess:
    """[P2-03] mutex API 壞掉時舊介面一律回 True —— 把【不知道】說成【拿到了】。"""

    def test_a_broken_mutex_api_reports_unknown(self, monkeypatch):
        monkeypatch.setattr(si, "_kernel32",
                            lambda: (_ for _ in ()).throw(OSError("壞了")))
        assert si.acquire_single_instance("Local\\test_unknown") == \
            si.INSTANCE_UNKNOWN
        # 相容包裝仍維持既有的 fail-open(既有呼叫端行為不變)
        assert si.ensure_single_instance("Local\\test_unknown") is True

    def test_a_real_acquisition_reports_acquired(self):
        name = "Local\\CMUH_test_acquired_rs3"
        try:
            assert si.acquire_single_instance(name) == si.INSTANCE_ACQUIRED
        finally:
            si.release_single_instance()

    def test_the_scheduler_handles_unknown_explicitly(self):
        """★不是 fail-closed,而是【明講】★:單例壞掉時把使用者完全擋在門外
        (排不了班)代價更大,而整份覆寫那條路已由 RS-1 的 CAS 擋住。
        所以要求的是:排班程式必須認得 UNKNOWN 並讓使用者知道。"""
        import scheduler
        src = inspect.getsource(scheduler.main)
        assert "INSTANCE_UNKNOWN" in src and "INSTANCE_ALREADY_RUNNING" in src, \
            "★排班程式沒有把三態分開處理★"
        assert "acquire_single_instance" in src
        i = src.index("INSTANCE_UNKNOWN")
        assert "MessageBoxW" in src[i:i + 900], \
            "UNKNOWN 要有給使用者看的說明與復原指引"
