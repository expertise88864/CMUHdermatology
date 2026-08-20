# -*- coding: utf-8 -*-
"""[批次RS-7 / 排班審R2 P1-04] R/VS 的過期判準是【整份輸入】,不是白名單。

`_result_stale_reason` 逐項列舉了六七件事(涵蓋日期、名單、請假、指定、
連休段、點數),但★`fixed_weekday`(固定星期)與 `week_colors`(色塊連週)
都是 CP-SAT 的硬約束★,它從來沒看過;`duty_min/max`、帳本(決定每人的目標
點數)、上月尾端也一樣。預覽視窗開著的期間他機改了其中任一項,舊解照樣
落地成一份【違反目前硬限制】的班表,而且畫面上完全看不出來。

修法與日排班那一側同一套:對整個 `SolveContext` 取指紋,套用時重建再比。
白名單降級成第二層 —— 它驗的是「結果與輸入自不自洽」(指定有沒有被採用、
點數對不對),那是另一件事,不可以一起省掉。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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
    st.save_config({"r_members": [{"id": "A", "name": "甲", "level": "R1"},
                                  {"id": "B", "name": "乙", "level": "R2"}],
                    "vs_members": []})
    st.save_month(YM, {})
    return RosterService(st)


def _result(svc, person="A"):
    """照當前 ctx 產出一份【自洽】的結果(白名單那六七項全部通過)。"""
    ctx = svc.build_context("r", YM)
    assignments = {d: person for d in ctx.days}
    pts = {m.id: 0 for m in ctx.members}
    for d, mid in assignments.items():
        pts[mid] += day_point(d, ctx.holidays, ctx.params)
    return SolveResult(status="ok", scope="r", level_used=0, level_name="L0",
                       assignments=assignments, points_by_person=pts,
                       input_fingerprint=rvs_input_fingerprint(ctx),
                       month_revision=svc.storage.load_month_snapshot(YM)[1])


class TestTheWhitelistCannotSeeTheseHardConstraints:
    """★這兩項都是硬約束,而且舊判準完全看不見★"""

    def test_a_changed_fixed_weekday_makes_the_result_stale(self, svc):
        res = _result(svc)
        svc.update_config(lambda cfg: cfg["r_members"][0].update(
            {"fixed_weekday": 2}))          # 他機把 A 改成固定星期三
        with pytest.raises(ValueError, match="已過期"):
            svc.accept_solution("r", YM, res)
        assert not svc.storage.load_month(YM).get("r_duty"), \
            "★被擋下就不可以留下任何一格★"

    def test_a_changed_week_color_makes_the_result_stale(self, svc):
        res = _result(svc)
        svc.set_week_color(2026, "2026-W33", "pink")   # 他機改了某週顏色
        with pytest.raises(ValueError, match="已過期"):
            svc.accept_solution("r", YM, res)

    def test_a_changed_duty_range_makes_the_result_stale(self, svc):
        """值班數軟範圍(config 的 duty_range_soft)也是 solver 的輸入。"""
        res = _result(svc)
        svc.update_config(
            lambda cfg: cfg.update({"duty_range_soft": [1, 30]}))
        with pytest.raises(ValueError, match="已過期"):
            svc.accept_solution("r", YM, res)

    def test_a_changed_ledger_makes_the_result_stale(self, svc):
        """帳本決定每個人的目標點數 → 他機結算了別的月份,目標就變了。"""
        res = _result(svc)
        svc.update_ledger(lambda led: led["r"].update({"B": -7.0}))
        with pytest.raises(ValueError, match="已過期"):
            svc.accept_solution("r", YM, res)


class TestTheGateItself:

    def test_an_unchanged_input_still_applies(self, svc):
        svc.accept_solution("r", YM, _result(svc))
        month = svc.storage.load_month(YM)
        assert month["r_duty"]["2026-08-01"]["person"] == "A"

    def test_a_result_without_a_fingerprint_is_refused(self, svc):
        """★「無從確認」不可以當成「沒問題」★:沒有指紋就無法判斷它是照哪
        一份輸入算的 —— 放行等於這道守衛只保護「記得帶指紋」的呼叫端。"""
        res = _result(svc)
        res.input_fingerprint = ""
        with pytest.raises(ValueError, match="沒有輸入指紋"):
            svc.accept_solution("r", YM, res)

    def test_the_solver_always_stamps_the_fingerprint(self):
        """守衛要 fail-closed,就必須確定【生產的產生端】一定會蓋章;
        否則正常流程會被自己擋住。"""
        import inspect

        from cmuh_common.roster import solve_rvs as mod
        src = inspect.getsource(mod.solve_duty)
        i_fp = src.index("res.input_fingerprint = ")
        i_ret = src.index("return res")
        assert i_fp < i_ret, "★指紋要在任何一條 return 之前就蓋上★"
        assert "apply_boundary_from_prev(ctx)" in src[:i_fp], \
            "★取指紋的階段要與 build_context 對齊★(prepare + 跨月銜接之後)"

    def test_the_two_solvers_share_one_normalizer(self):
        """★正規化只有一份★:兩邊各寫一套,遲早只有一邊被修好。"""
        import inspect

        from cmuh_common.roster import fingerprint as fp
        from cmuh_common.roster import solve_day, solve_rvs
        for fn in (solve_day.day_input_fingerprint,
                   solve_rvs.rvs_input_fingerprint):
            src = inspect.getsource(fn)
            assert "from cmuh_common.roster.fingerprint import" in src
            assert "hashlib" not in src, "★不可以自己再算一次★"
        assert "sha256" in inspect.getsource(fp.input_fingerprint)

    def test_an_unnormalizable_field_is_not_silently_skipped(self):
        from cmuh_common.roster.fingerprint import input_fingerprint
        with pytest.raises(TypeError):
            input_fingerprint({"x": object()})


class TestTheWhitelistIsStillTheSecondLayer:
    """指紋相同 ⇒ 輸入沒變;但「結果與輸入自不自洽」是另一件事。"""

    def test_a_self_inconsistent_result_is_still_refused(self, svc):
        res = _result(svc)
        res.assignments.pop(date(2026, 8, 1))      # 涵蓋日期與當月不符
        with pytest.raises(ValueError, match="涵蓋日期"):
            svc.accept_solution("r", YM, res)

    def test_the_reason_is_named_when_it_can_be(self, svc):
        """★講得出是哪一項就要講★:只回一句「輸入設定已變動」的話,
        使用者不知道是誰改了什麼(白名單是降級成診斷,不是拿掉)。"""
        res = _result(svc)
        svc.update_config(lambda cfg: cfg["r_members"].append(
            {"id": "C"}))
        with pytest.raises(ValueError, match="成員名單已變動"):
            svc.accept_solution("r", YM, res)
