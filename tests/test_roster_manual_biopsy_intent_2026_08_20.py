# -*- coding: utf-8 -*-
"""[批次RS-10 / 新一輪 review P2-02] 手動切片路徑也要留得下線索。

RS-5 把「月檔 + biopsy.json」收進同一個 `write_barrier`,那擋得住背景 Git
merge 與其他執行緒 —— ★擋不住行程被砍、停電、或第二次寫入的 I/O 失敗★。
留下來的是「月檔已經換成新的 saturday_biopsy、biopsy.json 還是舊的」,
而且沒有任何紀錄;更糟的是 `set_leaves(r)` 這條路還會把重排的例外吞掉。

意圖只記 (scope, 月份):帳本與切片計數都是【可以從月檔重算出來的衍生物】,
下次開程式的 `reconcile_pending_settles` 用月檔把它們重建到一致。
"""
import inspect
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"
SAT = date(2026, 8, 8)


def _cell(p):
    return {"person": p, "locked": False, "source": "test"}


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "r1", "level": "R1"},
                                  {"id": "r2", "level": "R2"},
                                  {"id": "r3", "level": "R3"}]})
    st.save_month(YM, {"r_duty": {SAT.isoformat(): _cell("r1")}})
    return RosterService(st)


def _pending(svc):
    return [(x["scope"], x["ym"]) for x in svc.storage.load_pending_settles()]


class TestTheIntentSurvivesACrashBetweenTheTwoFiles:

    def test_a_crash_before_the_biopsy_write_leaves_the_intent(self, svc):
        """★第二個檔沒寫成功 → 意圖留著★(下次開程式會用月檔重建)。"""
        real = svc.storage.save_biopsy

        def _boom(*a, **kw):
            raise OSError("模擬:寫到一半電腦被關掉")

        svc.storage.save_biopsy = _boom                      # type: ignore
        try:
            with pytest.raises(OSError):
                svc.set_cell("r", YM, SAT, "r2")
        finally:
            svc.storage.save_biopsy = real                   # type: ignore
        assert ("r", YM) in _pending(svc), \
            "★兩個檔之間斷掉,卻沒有留下任何線索★"

    def test_a_successful_edit_clears_the_intent(self, svc):
        svc.set_cell("r", YM, SAT, "r2")
        assert _pending(svc) == [], f"★成功了就不該留著★ {_pending(svc)}"

    def test_the_reconcile_rebuilds_from_the_month(self, svc):
        """留著的那一筆,下次開程式要真的把切片帳本重建到與月檔一致。"""
        svc.set_cell("r", YM, SAT, "r2")
        book = svc.storage.load_biopsy()
        book["counts"] = {}                       # 模擬:那一次沒寫進去
        book["history"] = []
        svc.storage.save_biopsy(book)
        svc.storage.mark_pending_settle("r", YM)
        assert svc.reconcile_pending_settles() == [("r", YM)]
        after = svc.storage.load_biopsy()
        assert after["counts"], "★沒有用月檔把切片計數重建回來★"
        assert _pending(svc) == []

    def test_a_swallowed_recompute_failure_still_leaves_a_record(self, svc):
        """★`set_leaves(r)` 把重排的例外吞掉★:寫壞了至少要留得下線索,
        否則那個不一致就永遠留在磁碟上,而 log 以外沒有人會知道。"""
        real = svc.storage.save_biopsy

        def _boom(*a, **kw):
            raise OSError("模擬:切片帳本寫入失敗")

        svc.storage.save_biopsy = _boom                      # type: ignore
        try:
            svc.set_leaves("r", YM, "r1", {SAT}, baseline=set())  # 不拋
        finally:
            svc.storage.save_biopsy = real                   # type: ignore
        assert ("r", YM) in _pending(svc), \
            "★例外被吞掉,而且連意圖都沒留★"


class TestEveryPairIsCovered:
    """★機械化★:漏一條路徑,那條路徑就等於沒有這個保護。"""

    #: ★唯一的例外:改名★。它寫的是 config+帳本+所有月份+切片帳本,而意圖的
    #: 契約是「帳本/切片是可以從【月檔】重算出來的衍生物」—— 改名中斷時月檔
    #: 本身也只改了一半,重算救不回來,留一筆意圖只會讓收斂器一直失敗。
    #: 它的原子性靠的是整段在 `write_barrier` 內 + 失敗時的回滾。
    EXEMPT = {"_rename_member_locked"}

    def test_every_month_plus_biopsy_pair_records_an_intent(self):
        import ast

        tree = ast.parse(inspect.getsource(RosterService))
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            writes = [c for c in ast.walk(node)
                      if isinstance(c, ast.Call)
                      and isinstance(c.func, ast.Attribute)
                      and c.func.attr == "save_biopsy"]
            if not writes or node.name in self.EXEMPT:
                continue
            marks = [c for c in ast.walk(node)
                     if isinstance(c, ast.Call)
                     and isinstance(getattr(c, "func", None), ast.Attribute)
                     and c.func.attr in ("settle_intent", "_biopsy_intent",
                                         "mark_pending_settle")]
            if not marks:
                bad.append(node.name)
        assert not bad, f"★這幾個函式寫了切片帳本卻沒有意圖保護★ {bad}"

    def test_the_intent_is_only_cleared_on_success(self):
        """★用 finally 清掉的話,失敗時等於宣稱已經一致了★

        (判斷要看【程式碼】,不是看字串:docstring 裡就寫著「刻意不寫
        try/finally」—— 用 `"finally" not in src` 判會被自己的說明騙過去。)
        """
        import ast

        import textwrap

        fn = ast.parse(textwrap.dedent(
            inspect.getsource(RosterService.settle_intent))).body[0]
        finals = [t for t in ast.walk(fn)
                  if isinstance(t, ast.Try) and t.finalbody]
        assert not finals, "★清除不可以放在 finally★"
        i_mark = inspect.getsource(RosterService.settle_intent)
        assert (i_mark.index("mark_pending_settle(")
                < i_mark.index("yield")
                < i_mark.index("clear_pending_settle("))


# ══ 第 2 輪外審 ═════════════════════════════════════════════════════════
class TestTheIntentBelongsToWhoeverRecordedIt:
    """★不可以清掉一個不是自己記下的義務★(外審 RS-10 第 1 輪 P1)

    `mark_pending_settle` 是冪等的:已經有一筆就不再記 —— 而那一筆屬於
    【另一個還沒完成的操作】(例如上一次 accept 寫完月檔、帳本卻寫失敗)。
    手動改格這條路★只重算切片,不會重算帳本★,把別人的意圖一併清掉等於替它
    宣稱「已經一致了」:開程式時的收斂從此不會再跑,帳本就一直錯下去,
    而它還是下個月公平目標的基準。
    """

    def test_a_pre_existing_intent_survives_a_manual_edit(self, svc):
        svc.storage.mark_pending_settle("r", YM)     # 上一次沒做完的結算
        svc.set_cell("r", YM, SAT, "r2")             # 這次成功了
        assert ("r", YM) in _pending(svc), \
            "★把別人還沒完成的結算意圖清掉了★"

    def test_it_still_clears_the_one_it_recorded(self, svc):
        assert _pending(svc) == []
        svc.set_cell("r", YM, SAT, "r2")
        assert _pending(svc) == [], "★自己記的那一筆要清掉(否則永遠重算)★"

    def test_mark_reports_whether_it_created_the_record(self, svc):
        assert svc.storage.mark_pending_settle("r", YM) is True
        assert svc.storage.mark_pending_settle("r", YM) is False

    def test_a_failed_vs_edit_does_not_leave_an_r_intent(self, svc):
        """VS 改格不碰 biopsy.json → 中途失敗也不該留下一筆 "r" 的義務。

        ★成功的那條路看不出差別★(記了又清掉),所以反例要落在失敗上:
        意圖的鍵是 (scope, 月份),張冠李戴會讓「誰的義務」說不清楚 ——
        開程式時去重算一個根本沒有壞掉的東西,而真正壞掉的那個沒人管。
        """
        svc.storage.save_config({
            "r_members": [{"id": "r1"}], "vs_members": [{"id": "v1"}]})
        real = svc.storage.save_month

        def _boom(*a, **kw):
            raise OSError("模擬:寫月檔時電腦被關掉")

        svc.storage.save_month = _boom                       # type: ignore
        try:
            with pytest.raises(OSError):
                svc.set_cell("vs", YM, date(2026, 8, 4), "v1")
        finally:
            svc.storage.save_month = real                    # type: ignore
        assert _pending(svc) == [], f"★VS 改格留了 R 的意圖★ {_pending(svc)}"
