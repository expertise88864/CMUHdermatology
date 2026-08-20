# -*- coding: utf-8 -*-
"""[批次RS-13 / 全審次輪 P1-01] R/VS 預覽要記得「這份解是從哪一版月檔算的」。

`input_fingerprint`(RS-7)涵蓋的是 SolveContext 的輸入 —— 鎖定格、請假、
指定、名單、假日、週色、帳本。但「未鎖定格【現在】排誰」不是 solver 的
輸入:A 機開著預覽時,B 機手動把 8/15 改成 R3,指紋完全看不見;A 按下
套用時才讀 revision,讀到的是 B 改完之後那一版 —— CAS 對得上、指紋對得上,
整份 `r_duty` 重建就把 B 明確做的修改靜默退回舊解。

修法與 `DaySolveResult` 對稱:revision 在【求解當下】捕捉(與 context 同一個
write_barrier 讀),套用時對不上就拒絕。政策保守:月檔任何變動都拒
(他 scope/日排班也一樣 —— 不猜「哪些欄位無害」,寧可重排)。
"""
import os
import sys
from dataclasses import fields as dc_fields
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.rules import day_point                      # noqa: E402
from cmuh_common.roster.service import (                            # noqa: E402
    DaySolveResult, RosterService,
)
from cmuh_common.roster.solve_rvs import (                          # noqa: E402
    SolveResult, rvs_input_fingerprint,
)
from cmuh_common.roster.storage import RosterStorage                # noqa: E402

YM = "2026-08"
D15 = date(2026, 8, 15)


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": "A", "name": "甲"}, {"id": "B", "name": "乙"}],
        "vs_members": [{"id": "V", "name": "V醫師"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
        "duty_range_soft": [9, 11],
    })
    st.save_month(YM, {})
    return RosterService(st)


def _cover(svc, scope="r", person="A"):
    ctx = svc.build_context(scope, YM)
    a = {d: person for d in ctx.days}
    for b in ctx.blocks:
        for x in b.days:
            a[x] = a[b.days[0]]
    return a


def _preview(svc, scope="r", person="A"):
    """照生產形狀造一份預覽結果:指紋+求解當下的月檔 revision。"""
    ctx = svc.build_context(scope, YM, for_solve=True)
    assignments = _cover(svc, scope, person)
    pts = {m.id: 0 for m in ctx.members}
    for d, mid in assignments.items():
        pts[mid] += day_point(d, ctx.holidays, ctx.params)
    return SolveResult(status="ok", scope=scope, level_used=0, level_name="L0",
                       assignments=assignments, points_by_person=pts,
                       input_fingerprint=rvs_input_fingerprint(ctx),
                       month_revision=svc.storage.load_month_snapshot(YM)[1])


class TestTheRemoteEditSurvivesThePreview:
    def test_a_remote_unlocked_duty_edit_makes_the_preview_stale(self, svc):
        """★本輪的反例本體★:未鎖格的 person 不是 solver 輸入,指紋看不見。"""
        res = _preview(svc, person="A")
        svc.set_cell("r", YM, D15, "B")          # B 機:手動把 8/15 改成 B
        with pytest.raises(ValueError, match="月檔已被修改"):
            svc.accept_solution("r", YM, res)
        cell = svc.storage.load_month(YM)["r_duty"][D15.isoformat()]
        assert cell["person"] == "B", "★B 的手動修改被舊預覽蓋掉了★"

    def test_a_remote_locked_duty_is_still_caught_by_the_fingerprint(self, svc):
        """鎖定格【是】ctx 輸入 → 原本的指紋層先擋,訊息要說得出原因。"""
        res = _preview(svc, person="A")
        svc.set_cell("r", YM, D15, "B")
        svc.set_lock("r", YM, D15, True)
        with pytest.raises(ValueError, match="已過期"):
            svc.accept_solution("r", YM, res)

    def test_an_untouched_month_accepts(self, svc):
        svc.accept_solution("r", YM, _preview(svc, person="A"))
        assert svc.storage.load_month(YM)["r_duty"]

    def test_an_unrelated_scope_change_is_conservatively_refused(self, svc):
        """★政策明訂★:採整份月檔 revision —— 他 scope 的變動也拒。
        不猜「vs_duty 對 r 的套用無害」:月檔欄位彼此有連動(週六切片讀
        r_duty、報告讀兩邊),白名單會腐爛,保守拒絕的代價只是重排一次。"""
        res = _preview(svc, scope="r", person="A")
        svc.set_cell("vs", YM, D15, "V")         # 他機改的是另一個 scope
        with pytest.raises(ValueError, match="月檔已被修改"):
            svc.accept_solution("r", YM, res)


class TestTheStampItself:
    def test_a_result_without_the_stamp_is_refused(self, svc):
        res = _preview(svc)
        res.month_revision = None                # 舊版程式/手造物件
        with pytest.raises(ValueError, match="沒有月檔版本標記"):
            svc.accept_solution("r", YM, res)

    def test_a_fresh_month_still_previews_and_accepts(self, tmp_path):
        """★空字串是「月檔還不存在」的合法身分,不是「沒標記」★:
        第一次排一個全新月份,快照回 ({}, "") —— 拿 "" 當 fail-closed
        哨兵的話,新月份永遠套用不了(第一版就犯了這個)。"""
        st = RosterStorage(str(tmp_path))
        st.save_config({
            "r_members": [{"id": "A", "name": "甲"}, {"id": "B", "name": "乙"}],
            "vs_members": [],
            "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
            "duty_range_soft": [9, 11],
        })                                        # ★月檔刻意不建★
        svc = RosterService(st)
        res = _preview(svc, person="A")
        assert res.month_revision == ""           # 缺檔的合法 revision
        svc.accept_solution("r", YM, res)
        assert svc.storage.load_month(YM)["r_duty"]

    def test_run_solve_stamps_the_solve_time_revision(self, svc):
        """生產路徑:`run_solve` 蓋章的是【求解當下】那一版。"""
        pytest.importorskip("ortools")
        res = svc.run_solve("r", YM)
        assert res.status == "ok", res.diagnosis
        assert res.month_revision == svc.storage.load_month_snapshot(YM)[1]

    def test_both_result_types_carry_the_month_identity(self):
        """★結構守衛★:日排班與 R/VS 兩邊不得再次漂移(全審次輪的建議)。
        DaySolveResult 有 revision 而 SolveResult 沒有 —— 這次的 P1 就是
        這樣長出來的;之後任何一邊拿掉這個欄位都要在這裡先紅。"""
        assert "month_revision" in DaySolveResult._fields
        assert "fingerprint" in DaySolveResult._fields
        names = {f.name for f in dc_fields(SolveResult)}
        assert "month_revision" in names
        assert "input_fingerprint" in names
