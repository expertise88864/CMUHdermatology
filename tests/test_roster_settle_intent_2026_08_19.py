# -*- coding: utf-8 -*-
"""[批次RS-4 / 排班審 P2-01] 月檔與帳本是兩個檔,跨檔寫入不可能真的原子。

★所以要選對「順序」與「可收斂性」★:
* 舊寫法「帳本先、月檔後」中斷 → 帳上多了一筆、月檔還是舊班表,而★沒有人
  說得出那筆該不該退★(帳本不是從月檔推導的,它是累計的);
* 改成「月檔先、帳本後」中斷 → 帳本【落後】,而帳本可以用月檔重算
  (`resettle_from_duty`)—— 那個方向永遠救得回來。
再配一筆意圖紀錄,下次開程式自動收斂,不必靠人記得。
"""
import pytest

from cmuh_common.roster.service import RosterService
from cmuh_common.roster.storage import RosterStorage

YM = "2026-09"


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path / "roster"))
    st.save_config({
        "r_members": [{"id": "K", "name": "K醫師"}, {"id": "C", "name": "C醫師"}],
        "vs_members": [], "pgy_members": [], "clerk_members": [],
    })
    st.save_month(YM, {"r_duty": {}})
    return RosterService(st)


def _result(svc):
    from cmuh_common.roster.solve_rvs import solve_duty
    out = solve_duty(svc.build_context("r", YM))
    assert out.status == "ok", out.status
    return out


class TestTheOrderIsChosenForRecoverability:

    def test_the_month_is_written_before_the_ledger(self, svc):
        """★順序本身就是設計★:量的是「月檔先落地」——中斷時留下的是可收斂的
        那一半。"""
        order: list = []
        real_month = svc.storage.save_month
        real_ledger = svc.storage.save_ledger

        def _m(*a, **kw):
            order.append("month")
            return real_month(*a, **kw)

        def _l(*a, **kw):
            order.append("ledger")
            return real_ledger(*a, **kw)

        svc.storage.save_month = _m                      # type: ignore
        svc.storage.save_ledger = _l                     # type: ignore
        try:
            svc.accept_solution("r", YM, _result(svc))
        finally:
            svc.storage.save_month = real_month          # type: ignore
            svc.storage.save_ledger = real_ledger        # type: ignore
        assert order[:2] == ["month", "ledger"], order

    def test_a_crash_between_the_two_writes_leaves_a_recoverable_state(
            self, svc):
        """帳本寫失敗 → 月檔是新的、帳本落後,而且★留著意圖紀錄★。"""
        real_ledger = svc.storage.save_ledger

        def _boom(_book):
            raise OSError("磁碟這一刻不給寫")

        svc.storage.save_ledger = _boom                  # type: ignore
        try:
            with pytest.raises(OSError):
                svc.accept_solution("r", YM, _result(svc))
        finally:
            svc.storage.save_ledger = real_ledger        # type: ignore

        assert svc.storage.load_month(YM)["r_duty"], "月檔應該已經落地"
        assert not (svc.storage.load_ledger().get("r") or {}), \
            "帳本應該還沒記上(這正是可收斂的那個方向)"
        pend = svc.storage.load_pending_settles()
        assert [(x["scope"], x["ym"]) for x in pend] == [("r", YM)], pend

    def test_the_next_start_reconciles_it_from_the_month(self, svc):
        real_ledger = svc.storage.save_ledger
        svc.storage.save_ledger = lambda _b: (_ for _ in ()).throw(
            OSError("x"))                                # type: ignore
        try:
            with pytest.raises(OSError):
                svc.accept_solution("r", YM, _result(svc))
        finally:
            svc.storage.save_ledger = real_ledger        # type: ignore

        done = svc.reconcile_pending_settles()           # ← 下次開程式
        assert done == [("r", YM)], done
        assert svc.storage.load_ledger().get("r"), \
            "★沒有用月檔把帳本重建回來★"
        assert not svc.storage.load_pending_settles(), "收斂後意圖要清掉"

    def test_a_successful_accept_leaves_no_intent_behind(self, svc):
        svc.accept_solution("r", YM, _result(svc))
        assert not svc.storage.load_pending_settles(), \
            "★成功了還留著意圖 → 下次開程式會做一次沒必要的重算★"

    def test_an_unrecoverable_entry_is_kept_not_silently_cleared(self, svc):
        """★收斂不了就不可以清掉★:清掉等於宣稱「已經一致了」。
        (定案月是唯讀,重算會被拒 —— 這是真的會發生的情況。)"""
        svc.storage.mark_pending_settle("r", YM)
        svc.finalize(YM, True)                           # 之後就唯讀
        done = svc.reconcile_pending_settles()
        assert done == [], done
        assert svc.storage.load_pending_settles(), \
            "★收斂失敗卻把意圖清掉了(從此沒有人知道帳本可能不一致)★"


class TestTheReconcileIsWiredUp:

    def test_the_app_reconciles_on_startup(self):
        """沒有呼叫端 = 這個機制不存在。"""
        import inspect

        import scheduler
        src = inspect.getsource(scheduler.ScheduleApp.__init__)
        assert "reconcile_pending_settles()" in src

    def test_the_reconcile_holds_the_barrier(self):
        """★收斂是「讀帳本 → 依月檔重算 → 寫回」★:開程式的當下 GitSync 正在
        做啟動 pull/補推,中間被 merge 換掉帳本的話,寫回去的是手上那份舊的,
        而我們接著還把意圖清掉 —— 等於宣稱已經一致。"""
        import inspect
        src = inspect.getsource(RosterService.reconcile_pending_settles)
        i = src.index("self.storage.write_barrier()")
        j = src.index("self.resettle_from_duty(")
        k = src.index("self.storage.clear_pending_settle(", j)
        assert i < j < k, "★重算與清除都要在同一個臨界區內★"

    def test_finalize_marks_and_clears(self):
        import inspect
        src = inspect.getsource(RosterService.finalize)
        assert "mark_pending_settle" in src and "clear_pending_settle" in src
        # ★錨在【它保護的那個寫入】上★:finalize 這裡真正會動帳本的是
        #   `resettle_from_duty`,不是最後那個 `save_month`。拿 save_month
        #   當錨的話,「意圖記在重算【之後】」照樣排在它前面 —— 量不到東西
        #   (而且 docstring 裡也有 save_month,第一版連錨都打在註解上)。
        assert (src.index("self.storage.mark_pending_settle(")
                < src.index("self.resettle_from_duty(")), \
            "★意圖要記在【它保護的那次寫入】之前★ 記在後面等於沒記"


class TestTheIntentRecordItself:

    def test_marking_is_idempotent(self, svc):
        svc.storage.mark_pending_settle("r", YM)
        svc.storage.mark_pending_settle("r", YM)
        assert len(svc.storage.load_pending_settles()) == 1

    def test_clearing_only_removes_that_one(self, svc):
        svc.storage.mark_pending_settle("r", YM)
        svc.storage.mark_pending_settle("vs", YM)
        svc.storage.clear_pending_settle("r", YM)
        left = [(x["scope"], x["ym"])
                for x in svc.storage.load_pending_settles()]
        assert left == [("vs", YM)], left


class TestTheIntentTravelsToTheOtherMachine:
    """★意圖只留在寫壞的那一台 = 沒有人會去收斂★:B 機拉到新月檔配舊帳本卻
    毫不知情;而 A 機若剛好壞掉/沒再開,帳本就永遠停在舊值,之後每個月的公平
    結算都以錯的餘額為基礎。"""

    def test_a_second_clone_sees_the_intent_and_reconciles(self, tmp_path):
        import subprocess

        from cmuh_common.roster.gitsync_storage import GitSyncStorage
        remote = tmp_path / "remote.git"
        a = tmp_path / "A"
        b = tmp_path / "B"
        subprocess.run(["git", "init", "--bare", str(remote)],
                       capture_output=True, check=True)
        for work in (a, b):
            subprocess.run(["git", "clone", str(remote), str(work)],
                           capture_output=True, check=True)
            for k, v in (("user.email", "t@t"), ("user.name", "t")):
                subprocess.run(["git", "-C", str(work), "config", k, v],
                               capture_output=True, check=True)

        st_a = GitSyncStorage(str(a), pull_interval_sec=0)
        st_a.save_config({
            "r_members": [{"id": "K", "name": "K"}, {"id": "C", "name": "C"}],
            "vs_members": [], "pgy_members": [], "clerk_members": []})
        st_a.save_month(YM, {"r_duty": {}})
        svc_a = RosterService(st_a)
        real = st_a.save_ledger
        st_a.save_ledger = lambda _b: (_ for _ in ()).throw(  # type: ignore
            OSError("A 機這一刻寫不進帳本"))
        try:
            with pytest.raises(OSError):
                svc_a.accept_solution("r", YM, _result(svc_a))
        finally:
            st_a.save_ledger = real                          # type: ignore
        st_a.flush()

        st_b = GitSyncStorage(str(b), pull_interval_sec=0)
        assert st_b.load_month(YM)["r_duty"], "B 應該拉到新月檔"
        pend = st_b.load_pending_settles()
        assert [(x["scope"], x["ym"]) for x in pend] == [("r", YM)], \
            "★意圖沒有同步出去 → B 機不知道帳本落後★"
        RosterService(st_b).reconcile_pending_settles()
        assert st_b.load_ledger().get("r"), "B 應該能自己把帳本收斂回來"
