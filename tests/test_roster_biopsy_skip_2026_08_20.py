# -*- coding: utf-8 -*-
"""[批次RS-12 / 2026-08-20 使用者需求] 不是每個週六早上都要切片。

R/VS 月曆的切片右鍵新增「本週不切片」:`biopsy_override[iso] = ""`(哨兵)。
語意:該週★不排人、不累計任何人的次數、也不影響輪替★(run/last 不動,
之後的次數平衡就當這週不存在);與既有指定同樣沿用於所有重排,
「改回自動排」可還原。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.saturday_biopsy import (  # noqa: E402
    assign_saturday_biopsy,
)
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"
SATS = [date(2026, 8, d) for d in (1, 8, 15, 22, 29)]
SKIP = date(2026, 8, 15)


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "r1", "level": "R1"},
                                  {"id": "r2", "level": "R2"},
                                  {"id": "r3", "level": "R3"}]})
    st.save_month(YM, {"r_duty": {s.isoformat(): {"person": "r1"}
                                  for s in SATS}})
    s = RosterService(st)
    s.recompute_saturday_biopsy(YM)
    return s


def _sb(svc):
    return svc.storage.load_month(YM).get("saturday_biopsy") or {}


class TestSkippingASaturday:

    def test_the_skipped_week_has_no_assignment(self, svc):
        svc.set_biopsy_person(YM, SKIP, "")
        sb = _sb(svc)
        assert SKIP.isoformat() not in sb, "★指定不切片,月檔卻還排著人★"
        for s in SATS:
            if s != SKIP:
                assert s.isoformat() in sb, "★別的週六不該被波及★"

    def test_nobody_gets_a_count_for_that_week(self, svc):
        svc.set_biopsy_person(YM, SKIP, "")
        counts = svc.storage.load_biopsy()["counts"]
        assert sum(counts.values()) == len(SATS) - 1, \
            f"★不切片的那週還是被累計了★ {counts}"

    def test_the_rotation_stays_balanced_around_the_gap(self, svc):
        """跳過的那週對輪替是透明的:剩下四週仍要 2/2 平均。"""
        svc.set_biopsy_person(YM, SKIP, "")
        counts = svc.storage.load_biopsy()["counts"]
        assert sorted(counts.values()) == [2, 2], counts

    def test_the_skip_survives_a_recompute(self, svc):
        """與既有指定同語意:之後任何重排(手改值班/請假)都沿用。"""
        svc.set_biopsy_person(YM, SKIP, "")
        svc.set_cell("r", YM, date(2026, 8, 22), "r2")   # 觸發連動重排
        assert SKIP.isoformat() not in _sb(svc), \
            "★重排把「不切片」的指定洗掉了★"

    def test_restore_brings_the_auto_pick_back(self, svc):
        svc.set_biopsy_person(YM, SKIP, "")
        svc.set_biopsy_person(YM, SKIP, None)
        assert SKIP.isoformat() in _sb(svc)
        assert not svc.storage.load_month(YM).get("biopsy_override"), \
            "清除後不留空殼"

    def test_the_note_says_it_was_skipped_on_purpose(self, svc):
        """報告的附註要講「手動指定不切片」——與『排不出人』分得開。"""
        svc.set_biopsy_person(YM, SKIP, "")
        _a, notes, *_ = svc.recompute_saturday_biopsy(
            YM, svc.storage.load_month(YM))
        assert any("不切片" in n for n in notes), notes


class TestThePureFunctionSentinel:

    def _members(self):
        import types
        mk = lambda i, lv: types.SimpleNamespace(id=i, level=lv, name=i)  # noqa: E731
        return [mk("r2", "R2"), mk("r3", "R3")]

    def test_empty_string_skips_without_touching_run_or_last(self):
        assign, notes = assign_saturday_biopsy(
            year=2026, month=8, members=self._members(),
            duty={}, leaves={}, counts={},
            overrides={SKIP: ""})
        assert SKIP not in assign
        assert any("不切片" in n for n in notes)
        others = [s for s in SATS if s != SKIP]
        picks = [assign[s]["person"] for s in others]
        assert sorted((picks.count("r2"), picks.count("r3"))) == [2, 2]

    def test_an_unknown_id_is_still_ignored_with_a_note(self):
        """"" 是哨兵,名單外代號仍是另一種情況(忽略+附註),不可混為一談。"""
        assign, notes = assign_saturday_biopsy(
            year=2026, month=8, members=self._members(),
            duty={}, leaves={}, counts={},
            overrides={SKIP: "r9"})
        assert SKIP in assign, "名單外代號應改自動排,不是跳過"
        assert any("忽略" in n for n in notes)

    def test_the_override_parser_keeps_the_sentinel(self, tmp_path):
        st = RosterStorage(str(tmp_path))
        svc = RosterService(st)
        got = svc._biopsy_overrides(
            {"biopsy_override": {SKIP.isoformat(): "", "bad": "r2"}})
        assert got == {SKIP: ""}, \
            f"★用 truthiness 過濾會把哨兵跟 None 一起丟掉★ {got}"
