# -*- coding: utf-8 -*-
"""[批次RS-9 / 新一輪 review P1-02] 求解要看的是【本月結算之前】的餘額。

`ledger.py` 的契約寫得很清楚:正值＝之前多值、目標調低;而且「同月重排 =
先 rollback 該月舊分錄再重記」。但 `build_context` 直接讀當下的帳本 ——
同一個月按第二次「自動排班」時,solver 看到的是★本月第一次班表造成的
暫時差額★,於是刻意排出一份反向傾斜的新班表去補償;接受時 `settle_month`
又把第一次那筆 rollback 掉,最後反而留下一筆方向相反的欠帳。

★這條不需要跨機 race、crash 或壞檔★:自動排班 → 套用 → 再按一次自動排班,
正常操作就會踩到。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.ledger import settle_month  # noqa: E402
from cmuh_common.roster.model import day_point  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_rvs import (  # noqa: E402
    SolveResult, rvs_input_fingerprint,
)
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "A"}, {"id": "B"}], "vs_members": []})
    st.save_month(YM, {})
    return RosterService(st)


def _settle(svc, deltas, month=YM, scope="r"):
    """直接在帳本上記一筆該月分錄(模擬「本月已經排過一次」)。"""
    led = svc.storage.load_ledger()
    total = sum(deltas.values())
    n = len(deltas)
    pts = {k: v + total / n for k, v in deltas.items()}   # settle 會扣掉均分
    settle_month(led, scope, month, pts)
    svc.storage.save_ledger(led)


class TestTheSolverSeesThePreMonthBalance:

    def test_a_same_month_resolve_does_not_see_its_own_settlement(self, svc):
        """★核心反例★ 本月第一次排完 A=+5 / B=-5;重排時基準必須仍是 0/0。"""
        _settle(svc, {"A": 5.0, "B": -5.0})
        assert svc.storage.load_ledger()["r"] == {"A": 5.0, "B": -5.0}
        ctx = svc.build_context("r", YM, for_solve=True)
        assert ctx.ledger.get("A", 0.0) == 0.0, \
            f"★把本月自己的暫時差額當成 carry-in★ {ctx.ledger}"
        assert ctx.ledger.get("B", 0.0) == 0.0

    def test_the_carry_in_from_earlier_months_is_kept(self, svc):
        """跨月的欠點必須留著 —— 不可以連上個月的一起回滾掉。"""
        _settle(svc, {"A": 3.0, "B": -3.0}, month="2026-07")
        _settle(svc, {"A": 5.0, "B": -5.0})
        assert svc.storage.load_ledger()["r"] == {"A": 8.0, "B": -8.0}
        ctx = svc.build_context("r", YM, for_solve=True)
        assert ctx.ledger["A"] == 3.0, f"★應是 +3(不是 +8)★ {ctx.ledger}"
        assert ctx.ledger["B"] == -3.0

    def test_the_other_scope_is_untouched(self, svc):
        """回滾只針對這個 scope 的這個月。"""
        led = svc.storage.load_ledger()
        led["vs"] = {"A": 4.0}
        svc.storage.save_ledger(led)
        _settle(svc, {"A": 5.0, "B": -5.0})
        assert svc.build_context("vs", YM, for_solve=True).ledger["A"] == 4.0

    def test_the_disk_is_not_modified(self, svc):
        """★這裡是求解的輸入,不是結算★:磁碟上的帳本一個位元都不能動。"""
        _settle(svc, {"A": 5.0, "B": -5.0})
        before = open(svc.storage._path("ledger.json"), encoding="utf-8").read()
        svc.build_context("r", YM, for_solve=True)
        after = open(svc.storage._path("ledger.json"), encoding="utf-8").read()
        assert after == before

    def test_it_does_not_mutate_the_object_it_was_handed(self, svc):
        """★回滾只能發生在自己的副本上★

        `load_ledger()` 現在每次都重新解析,所以「磁碟沒被改」這件事其實不是
        由 deepcopy 保證的 —— 那條測試量不到這一條(把 deepcopy 拿掉照樣綠,
        突變沒轉紅才發現)。真正要釘住的是:拿到的那份物件不可以被就地改掉,
        否則哪天 `load_ledger` 加上快取,求解就會把別人手上的帳本改成
        「本月還沒結算」的樣子。
        """
        _settle(svc, {"A": 5.0, "B": -5.0})
        # ★探針要打在生產真的用的那次讀取上★:求解基準改走嚴格快照
        # (外審次輪 P2-01) —— 還盯著 `load_ledger` 的話,這條就量不到了。
        shared = svc.storage.load_ledger()
        svc.storage.canonical_snapshot = (                # type: ignore
            lambda name, *a, **k: (shared, "rev")
            if name == "ledger.json" else
            RosterStorage.canonical_snapshot(svc.storage, name, *a, **k))
        svc.build_context("r", YM, for_solve=True)
        assert shared["r"] == {"A": 5.0, "B": -5.0},             f"★求解把呼叫端手上的那份帳本就地回滾了★ {shared['r']}"
        assert [h["month"] for h in shared["history"]] == [YM]

    def test_a_trimmed_month_fails_closed(self, svc, monkeypatch):
        """分錄可能已被修剪 → 無從確認本月之前是多少,寧可擋下並說清楚
        (硬猜一個基準會讓之後每個月的公平目標都跟著錯)。"""
        import cmuh_common.roster.service as mod
        monkeypatch.setattr(mod, "can_rollback", lambda *a, **k: False)
        with pytest.raises(ValueError, match="最舊月份"):
            svc.build_context("r", YM, for_solve=True)


class TestTheFingerprintIsStableAcrossAnAccept:
    """RS-7 的指紋含帳本;基準改成 pre-month 之後,套用一次不再讓輸入『變了』。

    ★餘額 0 與「還沒有分錄」對求解是同一件事★:回滾會留下一堆值為 0 的鍵,
    不正規化的話指紋會因為表示法而不同 —— 使用者看到的是「輸入設定已變動」,
    而其實什麼都沒變(誤報也是缺陷)。
    """

    def _result(self, svc):
        ctx = svc.build_context("r", YM, for_solve=True)
        assignments = {d: "A" for d in ctx.days}
        pts = {m.id: 0 for m in ctx.members}
        for d, mid in assignments.items():
            pts[mid] += day_point(d, ctx.holidays, ctx.params)
        return ctx, SolveResult(
            status="ok", scope="r", level_used=0, level_name="L0",
            assignments=assignments, points_by_person=pts,
            input_fingerprint=rvs_input_fingerprint(ctx),
            month_revision=svc.storage.load_month_snapshot(YM)[1])

    def test_zero_balances_are_canonicalized_away(self, svc):
        _settle(svc, {"A": 5.0, "B": -5.0})
        assert svc.build_context("r", YM, for_solve=True).ledger == {}

    def test_accepting_does_not_make_the_same_result_look_stale(self, svc):
        _ctx, res = self._result(svc)
        svc.accept_solution("r", YM, res)
        after = rvs_input_fingerprint(svc.build_context("r", YM, for_solve=True))
        assert after == res.input_fingerprint, \
            "★套用之後輸入其實沒變,指紋不該不同(否則重排會被誤判過期)★"

    def test_a_real_carry_in_change_still_shows_up(self, svc):
        """★不可以矯枉過正★:別的月份的結算改變了 carry-in,那是真的變了。"""
        _ctx, res = self._result(svc)
        _settle(svc, {"A": 2.0, "B": -2.0}, month="2026-07")
        assert rvs_input_fingerprint(svc.build_context("r", YM, for_solve=True)) \
            != res.input_fingerprint


class TestTheWholeRoundTrip:

    def test_solve_accept_solve_keeps_the_same_fair_target(self, svc):
        """外審點名的那條路徑:排班 → 套用 → 再排一次,基準必須一樣。"""
        first = svc.build_context("r", YM, for_solve=True).ledger
        _settle(svc, {"A": 5.0, "B": -5.0})          # 第一次套用的結果
        second = svc.build_context("r", YM, for_solve=True).ledger
        assert first == second == {}, (first, second)
        # 而磁碟上的帳本確實記著第一次的結算(它不是被清掉,是求解不看它)
        assert svc.storage.load_ledger()["r"] == {"A": 5.0, "B": -5.0}


# ══ 第 2 輪外審 ═════════════════════════════════════════════════════════
class TestOnlyTheSolverPathRollsBack:
    """★同一個 context 也餵給顯示/報告/驗證★(外審 RS-9 第 1 輪)

    一律回滾的話:
      * 使用者套用排班之後打開分頁,結算面板只會看到 0/0,而設定頁直接讀
        帳本又顯示 +5/-5 —— 兩邊自相矛盾,而且「重算帳本」的契約(重算後
        面板要跟著更新)當場失效;
      * 「分錄可能已被修剪」的 fail-closed 會讓【單純想看一個舊月份】的人
        連分頁都打不開(refresh 不接例外)。
    """

    def test_the_display_context_keeps_the_persisted_balance(self, svc):
        _settle(svc, {"A": 5.0, "B": -5.0})
        assert svc.build_context("r", YM).ledger == {"A": 5.0, "B": -5.0}, \
            "★顯示端要的是帳本現在的實際餘額★"

    def test_the_display_context_never_fails_closed_on_a_trimmed_month(
            self, svc, monkeypatch):
        import cmuh_common.roster.service as mod
        monkeypatch.setattr(mod, "can_rollback", lambda *a, **k: False)
        ctx = svc.build_context("r", YM)          # 只是要看,不是要排班
        assert ctx.days, "★看一個舊月份不該丟例外★"
        with pytest.raises(ValueError):
            svc.build_context("r", YM, for_solve=True)

    def test_quick_validate_and_report_use_the_display_context(self):
        """驗證/報告不可以偷偷改用求解基準(那會讓它們對著另一份帳本說話),
        也不可以因為帳本被修剪就整個壞掉。"""
        import ast

        src = inspect.getsource(RosterService)
        tree = ast.parse(src)
        # ★報告也算「求解那一側」★:它描述的是這一份 result,而 result 的
        #   目標是照本月結算之前的餘額算的(外審 RS-9 第 2 輪 P1 —— 第一版
        #   的這個守衛反而把錯誤的分類釘成了通過條件)。
        solve_only = {"run_solve", "_accept_solution_locked", "render_report"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "build_context"):
                    uses = any(k.arg == "for_solve" for k in sub.keywords)
                    if node.name in solve_only:
                        assert uses, f"★{node.name} 必須用求解基準★"
                    else:
                        assert not uses, (
                            f"★{node.name} 是顯示/驗證路徑,不該用求解基準★")

    def test_the_solve_and_the_accept_use_the_same_kind(self, svc):
        """★指紋比對只在兩邊看到同一份輸入時才有意義★"""
        _settle(svc, {"A": 5.0, "B": -5.0})
        solve_side = svc.build_context("r", YM, for_solve=True).ledger
        accept_side = svc.build_context("r", YM, for_solve=True).ledger
        assert solve_side == accept_side == {}

    def test_the_preview_report_uses_the_solver_baseline(self, svc):
        """★報告描述的是【這一份 result】,所以要用它的基準★

        (外審 RS-9 第 2 輪 P1)報告的「新帳本」＝ ctx.ledger + (點數 - 份額)。
        同月第二次求解時,求解看的是本月結算之前的餘額;報告若用顯示端的帳本
        (已含第一次結算),印出來的結轉/新帳本就與接受後的實際結果對不上 ——
        而接受時 `settle_month` 會先把第一次那筆回滾掉。
        """
        ctx = svc.build_context("r", YM, for_solve=True)
        assignments = {d: "A" for d in ctx.days}
        pts = {m.id: 0 for m in ctx.members}
        for d, mid in assignments.items():
            pts[mid] += day_point(d, ctx.holidays, ctx.params)
        share = sum(pts.values()) / len(pts)
        res = SolveResult(status="ok", scope="r", level_used=0, level_name="L0",
                          assignments=assignments, points_by_person=pts,
                          duty_counts={"A": len(ctx.days), "B": 0},
                          weekday_counts={"A": 0, "B": 0},
                          weekend_counts={"A": 0, "B": 0},
                          targets={"A": share, "B": share},
                          input_fingerprint=rvs_input_fingerprint(ctx),
                          month_revision=svc.storage.load_month_snapshot(YM)[1])
        _settle(svc, {"A": 5.0, "B": -5.0})       # 本月已經排過一次
        txt = svc.render_report("r", YM, res)
        line = next(ln for ln in txt.splitlines() if ln.strip().startswith("B"))
        expected = round(0.0 + (pts["B"] - share), 2)
        assert f"{expected:+.2f}" in line, (
            f"★報告把本月自己的暫時差額當成結轉了★ {line!r}"
            f"(應為 {expected:+.2f},不是 {round(-5.0 + (pts['B'] - share), 2):+.2f})")
