# -*- coding: utf-8 -*-
"""[批次RS-18 / 外審 2026-08-22 剩餘 P2] 義務的涵蓋範圍與名單身分的事後驗證。

P2-01 「先改來源、再重建切片」的每一條路都要留得下義務 —— 不能靠呼叫端
      自己記得(`set_leaves` 與跨月週五原本只 log,請假成功落地、切片停在
      舊狀態,而且沒有人負責收斂)。
P2-02 定案＝所有正典/衍生資料已一致:帶著未完成的義務定案,月檔從此唯讀,
      那筆義務就永遠做不完(收斂要寫月檔)。
P2-03 寫入邊界只擋得住這一台;兩台各改一個檔時 git 乾淨合併卻違反不變量
      → 事後一定看得到(開程式記錄 + 日排班警告面板)。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.service import RosterService          # noqa: E402
from cmuh_common.roster.storage import RosterStorage          # noqa: E402

YM = "2026-08"


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": "A", "name": "甲", "level": "R2"},
                      {"id": "B", "name": "乙", "level": "R3"}],
        "vs_members": [], "pgy_members": [{"id": "P1"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
    })
    st.save_month(YM, {"r_duty": {}})
    st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03",
                            "members": ["C1"]}])
    return RosterService(st)


def _boom(svc, monkeypatch):
    monkeypatch.setattr(svc, "recompute_saturday_biopsy",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            RuntimeError("切片重排壞了")))


# ══ P2-01 每一條「改來源」的路都要留得下義務 ═════════════════════════════
class TestEverySourceEditCarriesTheObligation:
    def test_set_leaves_keeps_the_obligation(self, svc, monkeypatch):
        """★反例本體★:請假成功落地,但 saturday_biopsy/切片計數還停在舊的
        —— 使用者只看到「請假成功」,沒有人知道要收斂。"""
        _boom(svc, monkeypatch)
        svc.set_leaves("r", YM, "A", {date(2026, 8, 8)}, baseline=set())
        assert (svc.storage.load_month(YM)["leaves"]["r"]["A"]
                == ["2026-08-08"]), "請假本身不該被擋"
        pend = [(x["ym"], svc.storage.pending_kind(x))
                for x in svc.storage.load_pending_settles()]
        assert pend == [(YM, "biopsy")], f"★義務消失了★ {pend}"

    def test_a_successful_leave_edit_leaves_nothing(self, svc):
        svc.set_leaves("r", YM, "A", {date(2026, 8, 8)}, baseline=set())
        assert not svc.storage.load_pending_settles()

    def test_a_vs_leave_never_creates_a_biopsy_obligation(
            self, svc, monkeypatch):
        """VS 不碰切片 —— 連【重排會失敗】時都不該記一筆 r 的義務。

        ★反例要在失敗的前提下量★:重排成功的話義務本來就會被清掉,
        「有沒有記過」根本分不出勝負(第一版就是這樣,突變沒轉紅)。
        """
        svc.storage.save_config({**svc.storage.load_config(),
                                 "vs_members": [{"id": "V"}]})
        _boom(svc, monkeypatch)
        svc.set_leaves("vs", YM, "V", {date(2026, 8, 8)}, baseline=set())
        assert not svc.storage.load_pending_settles(),             "★VS 請假記了一筆 R 的切片義務(開程式會去重算沒壞的東西)★"
        assert svc.storage.load_month(YM)["leaves"]["vs"]["V"]             == ["2026-08-08"]

    def test_a_non_boundary_friday_does_not_touch_next_month(self, svc):
        """★範圍要對★:不是月底那個週五就不該碰下個月(記錯月份的義務,
        誰也收斂不到)。"""
        svc.storage.save_month("2026-09", {"r_duty": {}})
        svc.set_cell("r", YM, date(2026, 8, 28), "A")   # 週五,但翌日不是 9/1
        assert all(x["ym"] != "2026-09"
                   for x in svc.storage.load_pending_settles())

    def test_the_cross_month_friday_keeps_its_own_obligation(
            self, svc, monkeypatch):
        """月底週五(翌日是下月 1 號的週六)→ 下月月初那個週六要跟著重排;
        失敗時義務要記在【下個月】頭上。"""
        # ★2026 年只有 7/31 是「月底週五」且翌日 8/1 正好是週六★
        ym = "2026-07"
        svc.storage.save_month(ym, {"r_duty": {}})
        real = svc.recompute_saturday_biopsy

        def _only_next_fails(y, month=None):
            if y == YM:                                  # 下個月=2026-08
                raise RuntimeError("下個月的切片重排壞了")
            return real(y, month)
        monkeypatch.setattr(svc, "recompute_saturday_biopsy", _only_next_fails)
        svc.set_cell("r", ym, date(2026, 7, 31), "A")
        pend = [(x["ym"], svc.storage.pending_kind(x))
                for x in svc.storage.load_pending_settles()]
        assert (YM, "biopsy") in pend, pend

    def test_a_finalized_next_month_still_gets_an_obligation(self, svc):
        """★靜默略過是最糟的一種★(外審 RS-18 R1-1):下月已定案只表示那份
        月檔唯讀,不表示它已經與新的週五值班一致 —— 略過的話,下月的切片
        資料會繼續反映舊值班,而且完全沒有紀錄。義務要留著(出口=解除定案)。
        """
        ym = "2026-07"
        svc.storage.save_month(ym, {"r_duty": {}})
        svc.finalize(YM, True)                           # 下個月已定案
        svc.set_cell("r", ym, date(2026, 7, 31), "A")    # 月底週五
        pend = [(x["ym"], svc.storage.pending_kind(x))
                for x in svc.storage.load_pending_settles()]
        assert (YM, "biopsy") in pend, f"★靜默略過了★ {pend}"

    def test_the_wrapper_is_the_only_way(self):
        """★不要靠每個呼叫端記得補意圖★:凡是「接住 recompute 例外還繼續」
        的地方,都必須在 `biopsy_obligation` / `_biopsy_intent` 之內。"""
        import ast
        import inspect
        import textwrap
        # ★判準要看【最外層那個方法】整段★:巢狀 mutator(`_mut`)裡呼叫重排,
        #   而義務是外層方法開的 —— 只看 `_mut` 自己會誤報;而且義務未必走
        #   context manager,accept/重算是直接 mark/retype(同樣算數)。
        #   (第一版就是這兩點都沒想到,四個誤報。)
        OBLIGATION = ("biopsy_obligation", "_biopsy_intent", "settle_intent",
                      "mark_pending_settle", "retype_pending_settle")
        SELF = ("recompute_saturday_biopsy",          # 重建入口自己
                "_recompute_saturday_biopsy_locked",
                "_reconcile_biopsy_only")             # 收斂端
        src = textwrap.dedent(inspect.getsource(RosterService))
        cls = ast.parse(src).body[0]
        bad = []
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            if fn.name in SELF:
                continue
            names = [n.func.attr for n in ast.walk(fn)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)]
            if "recompute_saturday_biopsy" not in names:
                continue
            if not any(isinstance(n, ast.Try) for n in ast.walk(fn)):
                continue                    # 不接例外 → 失敗會上拋,無須義務
            if not any(k in names for k in OBLIGATION):
                bad.append(fn.name)
        assert not bad, f"★這些地方接住了重排失敗卻沒有留下義務★ {bad}"


# ══ P2-02 定案要求資料已一致 ═════════════════════════════════════════════
class TestFinalizeDemandsConsistency:
    def test_an_open_obligation_blocks_finalize(self, svc):
        svc.storage.mark_pending_settle("r", YM, kind="biopsy")
        with pytest.raises(Exception, match="尚未重建完成"):
            svc.finalize(YM, True)
        assert not svc.storage.load_month(YM).get("finalized")

    def test_another_months_obligation_does_not_block_this_one(self, svc):
        """★範圍要對★:別的月份的義務與這個月的定案無關,擋它是誤報。"""
        svc.storage.save_month("2026-07", {"r_duty": {}})
        svc.storage.mark_pending_settle("r", "2026-07", kind="biopsy")
        svc.finalize(YM, True)
        assert svc.storage.load_month(YM)["finalized"] is True

    def test_the_message_points_at_the_way_out(self, svc):
        svc.finalize(YM, True)
        svc.storage.mark_pending_settle("r", YM, kind="biopsy")  # 舊資料
        assert svc.reconcile_pending_settles() == []
        assert svc.storage.load_pending_settles(), "收斂不了就要留著"


# ══ P2-03 合併後才成立的衝突要看得見 ═════════════════════════════════════
class TestTheMergedConflictIsVisible:
    def _merge_conflict(self, svc):
        """模擬兩台各改一個檔之後 git 乾淨合併的結果(誰都沒違規)。"""
        svc.storage.save_month(YM, {"pgy_month_roster": ["P1", "C9"]})
        svc.storage.save_clerk_batches(
            [{"id": "b1", "start_monday": "2026-08-03", "members": ["C9"]}])

    def test_the_validator_names_it(self, svc):
        self._merge_conflict(svc)
        msgs = svc.validate_roster_identity_invariants()
        assert any("C9" in m and "同時是" in m for m in msgs), msgs

    def test_a_clean_repo_says_nothing(self, svc):
        assert svc.validate_roster_identity_invariants() == []

    def test_it_reaches_the_day_warning_panel(self, svc):
        """★只寫進 log 等於沒說★:使用者看的是警告面板。"""
        self._merge_conflict(svc)
        assert any("C9" in m for m in svc.quick_validate_day(YM))

    def test_clearing_unlocked_does_not_wipe_the_panel(self):
        """★清除未鎖定不得清空警告面板★(外審 RS-18 R1-2):`refresh()` 剛
        用 quick_validate_day 填進【現況】的問題,無條件清掉的話,使用者會
        在衝突還在時看到一片空白,接著就能定案/匯出。"""
        import inspect
        from cmuh_common.roster.ui import day_tab
        src = inspect.getsource(day_tab.DayScheduleTab._on_clear)
        assert "_refresh_warnings([])" not in src,             "★又把剛跑出來的檢查結果清掉了★"

    def test_it_is_wired_at_startup(self):
        import inspect

        import scheduler
        src = inspect.getsource(scheduler.ScheduleApp.__init__)
        assert "validate_roster_identity_invariants()" in src

    def test_duplicate_inside_one_file_is_also_named(self, svc):
        svc.storage.save_month(YM, {"pgy_month_roster": ["P1", "P1"]})
        assert any("重複代號" in m
                   for m in svc.validate_roster_identity_invariants())

    def test_the_local_check_runs_inside_the_barrier(self):
        """★TOCTOU★:在臨界區外查 Clerk 名單,背景 pull 可以在查完與寫入
        之間把他機的 Clerk 拉進來 —— 寫出一份當場違反不變量的月檔。"""
        import inspect
        src = inspect.getsource(RosterService.set_pgy_month_roster)
        i = src.index("write_barrier()")
        j = src.index("assert_no_cross_roster(")
        assert i < j, "★跨池檢查在臨界區之外★"
