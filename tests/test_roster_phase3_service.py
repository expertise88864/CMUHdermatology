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
from cmuh_common.roster.solve_day import BIOPSY, PHOTO, REST, TREATMENT  # noqa: E402
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


def test_rf09_build_day_input_pulls_prior_month_sessions(tmp_path):
    """RF-09：build_day_input 對跨月梯次讀上月 day_slots 填 prior_sessions。"""
    svc = _svc(tmp_path)
    svc.storage.save_clerk_batches([
        {"id": "b", "start_monday": "2026-07-27", "members": ["1", "2"]}])
    prev = svc.storage.load_month("2026-07")
    prev["day_slots"] = {"2026-07-30": {"上午": {BIOPSY: ["1"]}}}
    svc.storage.save_month("2026-07", prev)
    inp = svc.build_day_input(YM)
    assert inp.prior_sessions.get("2026-07-30", {}).get("上午", {}).get(BIOPSY) == ["1"]
    assert "A" in inp.prior_pgy                             # 上月 PGY 供 replay 剔除


def test_clinic_closure_removes_room_for_range(tmp_path):
    """本月停診：某診間在選定日期範圍不進格網、不排人；範圍外照常；恢復後清乾淨。"""
    svc = _svc(tmp_path)                                    # 週一 上午 [101,103]
    assert "101" in svc.clinic_rooms_for_month(YM)
    svc.set_clinic_closed(YM, "101", date(2026, 8, 3), date(2026, 8, 10), ["上午"])
    grid = svc.build_day_input(YM).grid
    assert "101" not in grid[date(2026, 8, 3)]["上午"]      # 停診日不含 101
    assert "103" in grid[date(2026, 8, 3)]["上午"]          # 其他診照常
    assert "101" in grid[date(2026, 8, 17)]["上午"]         # 範圍外照常開
    assert svc.clinic_closures(YM)["2026-08-03"]["上午"] == ["101"]
    ds, _l, _w = svc.run_day_solve(YM)                      # 自動排班不排人進停診診間
    assert "101" not in ds.get("2026-08-03", {}).get("上午", {})
    # 恢復開診 → 清乾淨
    svc.set_clinic_closed(YM, "101", date(2026, 8, 3), date(2026, 8, 10),
                          ["上午"], closed=False)
    assert "101" in svc.build_day_input(YM).grid[date(2026, 8, 3)]["上午"]
    assert svc.clinic_closures(YM) == {}


def test_clinic_closure_clears_existing_assignments(tmp_path):
    """停診時清掉既有班表中該診間的人（未鎖定時段）；鎖定時段保留（鎖定契約）。"""
    svc = _svc(tmp_path)
    svc.set_day_slot(YM, date(2026, 8, 3), "上午", "101", ["A"])
    svc.set_day_slot(YM, date(2026, 8, 10), "上午", "101", ["B"])
    svc.toggle_day_lock(YM, date(2026, 8, 10), "上午")     # 鎖定 8/10 上午
    svc.set_clinic_closed(YM, "101", date(2026, 8, 3), date(2026, 8, 10), ["上午"])
    m = svc.storage.load_month(YM)
    assert "101" not in m["day_slots"]["2026-08-03"]["上午"]      # 未鎖 → 一併清掉
    assert m["day_slots"]["2026-08-10"]["上午"]["101"] == ["B"]   # 鎖定 → 保留


def test_clinic_closure_finalized_guard(tmp_path):
    svc = _svc(tmp_path)
    m = svc.storage.load_month(YM)
    m["finalized"] = True
    svc.storage.save_month(YM, m)
    with pytest.raises(FinalizedMonthError):
        svc.set_clinic_closed(YM, "101", date(2026, 8, 3), date(2026, 8, 3), ["上午"])


def test_rf04_locked_preserved_when_day_becomes_holiday(tmp_path):
    """RF-04：鎖定日事後變假日（掉出格網）→ 即使套用一份漏掉該日的 stale 預覽，
    service 層仍會把鎖定時段強制併回，不得刪除鎖定內容。"""
    svc = _svc(tmp_path)
    svc.accept_day_solution(YM, svc.run_day_solve(YM)[0])
    d = date(2026, 8, 3)
    svc.toggle_day_lock(YM, d, "上午")
    locked_slots = svc.storage.load_month(YM)["day_slots"]["2026-08-03"]["上午"]
    # 事後把 8/3 加進國定假日表 → month_grid 排除該日
    svc.storage.save_holiday_duty({"r": {d: "X"}, "vs": {}})
    ds2, _l, _w = svc.run_day_solve(YM)
    # 模擬掉出格網的 stale 預覽（漏掉 8/3）→ 直接餵給 accept 測 service 層防線
    stale = {iso: sess for iso, sess in ds2.items() if iso != "2026-08-03"}
    assert "2026-08-03" not in stale
    svc.accept_day_solution(YM, stale)                     # 整批覆蓋（漏了鎖定日）
    kept = svc.storage.load_month(YM)["day_slots"].get("2026-08-03", {}).get("上午")
    assert kept == locked_slots                            # 鎖定內容仍原樣併回保留


def test_wed_pm_photo_present_treatment_absent(tmp_path):
    """週三下午：照光照排、治療室休診（day_slots 有照光、無治療室、無房）。"""
    svc = _svc(tmp_path)
    day_slots, _log, _w = svc.run_day_solve(YM)
    wed_pm = day_slots["2026-08-05"]["下午"]            # 8/5 週三
    assert PHOTO in wed_pm and TREATMENT not in wed_pm
    assert not any(k.isdigit() for k in wed_pm)         # 無房號格


def test_photo_every_session_treatment_skips_wed_pm(tmp_path):
    """照光每個時段一律有人；治療室只在非週三下午的時段有人。"""
    svc = _svc(tmp_path)                                # pgy A,B,C
    day_slots, _log, _w = svc.run_day_solve(YM)
    for iso, sessions in day_slots.items():
        wd = date.fromisoformat(iso).weekday()
        for session, slots in sessions.items():
            assert slots.get(PHOTO), f"{iso} {session} 照光應必排"
            if wd == 2 and session == "下午":
                assert TREATMENT not in slots             # 週三下午治療室休診
            else:
                assert slots.get(TREATMENT), f"{iso} {session} 治療室應排"


# ─── RS-03 / RS-05：停診清報告 + audit + 撞鎖定回饋 ──────────────────────────
def test_rs03_closure_clears_stale_day_report(tmp_path):
    """[RS-03] 停診清掉既有指派後，舊 day_report 一併清空（被清者不在報告幽靈化），
    並回傳清除數量供對話框提示。"""
    svc = _svc(tmp_path)
    svc.set_day_slot(YM, date(2026, 8, 3), "上午", "101", ["A"])
    m = svc.storage.load_month(YM)
    m["day_report"] = "舊報告內容"
    svc.storage.save_month(YM, m)
    res = svc.set_clinic_closed(YM, "101", date(2026, 8, 3), date(2026, 8, 3), ["上午"])
    assert res["cleared"] == 1
    assert svc.storage.load_month(YM)["day_report"] == ""


def test_rs05_closure_skipped_locked_and_audit(tmp_path):
    """[RS-05] 停診撞到鎖定時段：不清該指派、回報 skipped_locked；且寫 closure audit。"""
    svc = _svc(tmp_path)
    svc.set_day_slot(YM, date(2026, 8, 10), "上午", "101", ["B"])
    svc.toggle_day_lock(YM, date(2026, 8, 10), "上午")
    res = svc.set_clinic_closed(YM, "101", date(2026, 8, 10), date(2026, 8, 10),
                                ["上午"])
    assert res["cleared"] == 0
    assert ("2026-08-10", "上午") in res["skipped_locked"]
    m = svc.storage.load_month(YM)
    assert m["day_slots"]["2026-08-10"]["上午"]["101"] == ["B"]   # 鎖定保留
    assert any(a.get("via") == "closure" for a in m.get("audit", []))
