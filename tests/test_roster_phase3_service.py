# -*- coding: utf-8 -*-
"""Phase 3 玻合層：ClerkBatch/batches_covering + storage(模板/梯次/切片格網) +
service(build_day_input / run_day_solve / accept / 手動改格)。純函式，無 ortools。"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.model import ClerkBatch, batches_covering  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_day import REST, TREATMENT  # noqa: E402
from cmuh_common.roster.storage import (  # noqa: E402
    FinalizedMonthError, RosterStorage,
)

YM = "2026-08"
TEMPLATE = {"template": {"0": {"上午": [{"room": "101"}, {"room": "103"}],
                               "下午": [{"room": "101"}]}}}   # 週一


def _svc(tmp_path, with_clerk=False):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "pgy_members": [{"id": "A"}, {"id": "B"}, {"id": "C"}],
        "room_capacity": 2,
    })
    st.save_clinic_template(TEMPLATE)
    if with_clerk:
        st.save_clerk_batches([
            {"id": "b1", "start_monday": "2026-08-03", "members": ["1", "2"]}])
        st.save_biopsy_grid({"b1": {"2026-08-03": {"上午": True}}})
    return RosterService(st)


# ─── ClerkBatch ─────────────────────────────────────────────────────────────
def test_clerk_batch_covers_two_weeks():
    b = ClerkBatch("b1", date(2026, 8, 3), ["1", "2"])
    assert b.covers(date(2026, 8, 3)) and b.covers(date(2026, 8, 16))
    assert not b.covers(date(2026, 8, 17))              # 第 15 天已不含
    assert not b.covers(date(2026, 8, 2))


def test_batches_covering_and_roundtrip():
    b = ClerkBatch.from_dict(
        {"id": "b1", "start_monday": "2026-07-27", "members": ["1"]})
    assert batches_covering([b], 2026, 8)              # 7/27 梯次延伸進 8 月
    assert not batches_covering([b], 2026, 10)
    assert ClerkBatch.from_dict(b.to_dict()).members == ["1"]


# ─── storage roundtrip ──────────────────────────────────────────────────────
def test_storage_phase3_roundtrip(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_clinic_template(TEMPLATE)
    assert st.load_clinic_template()["template"]["0"]["上午"][0]["room"] == "101"
    st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03",
                            "members": ["1"]}])
    assert st.load_clerk_batches()[0]["id"] == "b1"
    st.save_biopsy_grid({"b1": {"2026-08-03": {"上午": True}}})
    assert st.load_biopsy_grid()["b1"]["2026-08-03"]["上午"] is True


# ─── service：日排班 ────────────────────────────────────────────────────────
def test_build_day_input_defaults_pgy_from_config(tmp_path):
    inp = _svc(tmp_path).build_day_input(YM)
    assert inp.pgy_roster == ["A", "B", "C"]           # 月檔未指定→config 預設
    assert inp.clerk_batches == []                      # 無梯次
    assert inp.grid[date(2026, 8, 3)]["上午"] == ["101", "103"]


def test_build_day_input_with_clerk_and_biopsy(tmp_path):
    inp = _svc(tmp_path, with_clerk=True).build_day_input(YM)
    assert [b.members for b in inp.clerk_batches] == [["1", "2"]]
    assert inp.biopsy_open["2026-08-03"]["上午"] is True


def test_run_and_accept_day_solution(tmp_path):
    svc = _svc(tmp_path)
    day_slots, log, warnings = svc.run_day_solve(YM)
    mon = day_slots["2026-08-03"]["上午"]
    assert mon[TREATMENT]                               # 週一早有治療室
    assert log
    svc.accept_day_solution(YM, day_slots)
    assert svc.storage.load_month(YM)["day_slots"] == day_slots


def test_pgy_roster_override_and_leaves(tmp_path):
    svc = _svc(tmp_path)
    svc.set_pgy_month_roster(YM, ["A", "B"])           # 當月只有 A,B
    inp = svc.build_day_input(YM)
    assert inp.pgy_roster == ["A", "B"]
    # PGY 請假反映到 avail（透過 month leaves）
    month = svc.storage.load_month(YM)
    month.setdefault("leaves", {})["pgy"] = {"A": ["2026-08-03"]}
    svc.storage.save_month(YM, month)
    inp2 = svc.build_day_input(YM)
    assert inp2.leaves["pgy"]["A"] == {date(2026, 8, 3)}


def test_set_day_slot_manual_and_finalize_guard(tmp_path):
    svc = _svc(tmp_path)
    svc.set_day_slot(YM, date(2026, 8, 3), "上午", TREATMENT, ["C"])
    ds = svc.storage.load_month(YM)["day_slots"]["2026-08-03"]["上午"]
    assert ds[TREATMENT] == ["C"]
    assert svc.storage.load_month(YM)["audit"][-1]["via"] == "manual"
    # 清空移除
    svc.set_day_slot(YM, date(2026, 8, 3), "上午", TREATMENT, [])
    assert TREATMENT not in svc.storage.load_month(YM)["day_slots"]["2026-08-03"]["上午"]
    # 定案後 accept 擋下
    m = svc.storage.load_month(YM)
    m["finalized"] = True
    svc.storage.save_month(YM, m)
    with pytest.raises(FinalizedMonthError):
        svc.accept_day_solution(YM, {})


def test_run_day_solve_deterministic(tmp_path):
    svc = _svc(tmp_path)
    assert svc.run_day_solve(YM)[0] == svc.run_day_solve(YM)[0]
    _ds, _log, warnings = svc.run_day_solve(YM)
    assert REST is not None                             # 匯入正常


def test_day_lock_toggle_and_preserved_on_resolve(tmp_path):
    svc = _svc(tmp_path)
    svc.accept_day_solution(YM, svc.run_day_solve(YM)[0])   # 先套用一次
    d = date(2026, 8, 3)
    assert svc.toggle_day_lock(YM, d, "上午") is True
    assert svc.is_day_locked(YM, d, "上午")
    locked_slots = svc.storage.load_month(YM)["day_slots"]["2026-08-03"]["上午"]
    assert svc.build_day_input(YM).locked["2026-08-03"]["上午"] == locked_slots
    # 重排 → 鎖定時段不變
    ds2, _l, _w = svc.run_day_solve(YM)
    assert ds2["2026-08-03"]["上午"] == locked_slots
    assert svc.toggle_day_lock(YM, d, "上午") is False      # 解鎖
    assert not svc.is_day_locked(YM, d, "上午")


def test_clear_unlocked_day_keeps_locked(tmp_path):
    svc = _svc(tmp_path)
    svc.accept_day_solution(YM, svc.run_day_solve(YM)[0])
    svc.toggle_day_lock(YM, date(2026, 8, 3), "上午")
    svc.clear_unlocked_day(YM)
    remaining = svc.storage.load_month(YM)["day_slots"]
    assert "上午" in remaining.get("2026-08-03", {})          # 鎖定保留
    assert "下午" not in remaining.get("2026-08-03", {})       # 未鎖定清掉


def test_clear_unlocked_clears_stale_report(tmp_path):
    svc = _svc(tmp_path)
    svc.accept_day_solution(YM, svc.run_day_solve(YM)[0], report="OLD")
    svc.clear_unlocked_day(YM)
    assert svc.storage.load_month(YM)["day_report"] == ""    # 舊報告一併清除


def test_wed_pm_treatment_present(tmp_path):
    """週三下午跟診關閉但治療室仍排（day_slots 有治療室、無房）。"""
    svc = _svc(tmp_path)
    day_slots, _log, _w = svc.run_day_solve(YM)
    wed_pm = day_slots["2026-08-05"]["下午"]            # 8/5 週三
    assert TREATMENT in wed_pm
    assert not any(k.isdigit() for k in wed_pm)         # 無房號格
