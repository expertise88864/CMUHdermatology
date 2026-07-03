# -*- coding: utf-8 -*-
"""PGY/Clerk 開診格網 + 五步驟填充器（純函式，無 ortools）。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.clinic_grid import is_session_open, month_grid  # noqa: E402
from cmuh_common.roster.model import ClerkBatch  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, REST, TREATMENT, DaySolveInput, FairCounters, month_solve_day,
    solve_session,
)

# 2026-08：週一 3/10/17/24/31；週三 5/12/19/26
_TEMPLATE = {
    "0": {"上午": [{"room": "101"}, {"room": "103"}],
          "下午": [{"room": "101"}]},
    "2": {"上午": [{"room": "102"}],
          "下午": [{"room": "102"}]},              # 週三下午應被強制關閉
}


# ─── clinic_grid ────────────────────────────────────────────────────────────
def test_month_grid_template_expansion():
    g = month_grid("2026-08", _TEMPLATE, holidays=set())
    assert g[date(2026, 8, 3)]["上午"] == ["101", "103"]     # 週一
    assert g[date(2026, 8, 3)]["下午"] == ["101"]
    assert is_session_open(g, date(2026, 8, 3), "上午")


def test_month_grid_wed_pm_closed_and_holiday_excluded():
    g = month_grid("2026-08", _TEMPLATE, holidays={date(2026, 8, 3)})
    assert date(2026, 8, 3) not in g                         # 假日休診
    assert g[date(2026, 8, 5)]["下午"] == []                 # 週三下午關閉
    assert g[date(2026, 8, 5)]["上午"] == ["102"]
    # 週末不在格網
    assert date(2026, 8, 1) not in g


def test_month_grid_self_paid_excluded_and_overrides():
    tmpl = {"0": {"上午": [{"room": "101"}, {"room": "105", "is_self_paid": True}]}}
    ov = {"2026-08-10": {"上午": {"closed_rooms": ["101"], "added_rooms": ["108"]}}}
    g = month_grid("2026-08", tmpl, set(), overrides=ov)
    assert g[date(2026, 8, 3)]["上午"] == ["101"]            # 自費 105 排除
    assert g[date(2026, 8, 10)]["上午"] == ["108"]           # 101 關、108 加


# ─── solve_session 五步驟 ───────────────────────────────────────────────────
def test_no_clerk_month_columns_fill():
    """無 Clerk：治療室 1 PGY，其餘 PGY 逐欄填診（101 兩人、102 一人）。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101", "102"],
        pgy_avail=["A", "B", "C", "D"], clerk_avail=[],
        biopsy_open=False, fc=fc)
    assert slots[TREATMENT] == ["A"]
    assert slots["101"] == ["B", "D"] and slots["102"] == ["C"]
    assert BIOPSY not in slots and REST not in slots


def test_mixed_one_clerk_one_pgy():
    """1 診間：Clerk 先坐、PGY 補第 2 位 → 1C+1P 混搭（治療室先吃掉 A）。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A", "B"], clerk_avail=["1"], biopsy_open=False, fc=fc)
    assert slots[TREATMENT] == ["A"]                         # 治療室先取 1 PGY
    assert slots["101"] == ["1", "B"]                        # Clerk 先、PGY 後


def test_fewer_clerks_than_rooms_pairs_first():
    """Clerk 少於診間：PGY 先與已坐 Clerk 的診間配對，而非先佔空房。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101", "102"],
        pgy_avail=["A", "B"], clerk_avail=["1"], biopsy_open=False, fc=fc)
    assert slots[TREATMENT] == ["A"]
    assert slots["101"] == ["1", "B"]                        # 配成 1C+1P
    assert "102" not in slots                                # 沒人 → 不輸出空房


def test_biopsy_assign_and_prefer_undone():
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A"], clerk_avail=["1", "2"],
        biopsy_open=True, fc=fc)
    assert slots[TREATMENT] == ["A"]
    assert slots[BIOPSY] == ["1"]                            # 未輪過者優先
    assert slots["101"] == ["2"]


def test_biopsy_open_but_no_clerk_warns():
    fc = FairCounters()
    slots, log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A"], clerk_avail=[], biopsy_open=True, fc=fc)
    assert BIOPSY not in slots
    assert any("切片室開放但無 Clerk" in ln for ln in log)


def test_treatment_no_pgy_warns_not_forced():
    fc = FairCounters()
    slots, log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=[], clerk_avail=["1"], biopsy_open=False, fc=fc)
    assert TREATMENT not in slots
    assert any("治療室無 PGY" in ln for ln in log)
    assert slots["101"] == ["1"]                             # Clerk 仍照排


def test_wed_pm_treatment_only():
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 5), "下午", [],                        # 週三下午跟診關閉
        pgy_avail=["A", "B"], clerk_avail=[], biopsy_open=False, fc=fc)
    assert slots[TREATMENT] == ["A"]
    assert fc.tx_wed_pm.get("A") == 1                        # 週三下午計數
    assert slots[REST] == ["B"]                              # 沒位子 → 放假


def test_wed_pm_biopsy_forced_closed():
    """週三下午即使 biopsy_open=True，切片室仍硬性關閉（C3）。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 5), "下午", [],
        pgy_avail=["A"], clerk_avail=["1"], biopsy_open=True, fc=fc)
    assert BIOPSY not in slots
    assert slots[REST] == ["1"]                              # Clerk 沒位子→放假


def test_capacity3_clerk_overflow_before_third_pgy():
    """容量 3：第 3 位留給 Clerk overflow，多餘 PGY 放假（非塞第 3 個 PGY）。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A", "P1", "P2"], clerk_avail=["1", "2"],
        biopsy_open=False, fc=fc, capacity=3)
    assert slots[TREATMENT] == ["A"]
    assert slots["101"] == ["1", "P1", "2"]                  # C, P(2nd), C(3rd)
    assert slots[REST] == ["P2"]                             # 多餘 PGY 放假


def test_treatment_fairness_rotates():
    fc = FairCounters()
    picks = []
    for _ in range(3):                                       # 連續 3 個時段
        slots, _l = solve_session(date(2026, 8, 3), "上午", [],
                                  ["A", "B", "C"], [], False, fc)
        picks.append(slots[TREATMENT][0])
    assert picks == ["A", "B", "C"]                          # 輪平均


def test_determinism_same_input():
    def run():
        fc = FairCounters()
        return solve_session(date(2026, 8, 3), "上午", ["101", "102"],
                             ["A", "B", "C"], ["1", "2"], True, fc)[0]
    assert run() == run()


# ─── month_solve_day ────────────────────────────────────────────────────────
def test_month_solve_day_no_clerk():
    grid = month_grid("2026-08", _TEMPLATE, set())
    inp = DaySolveInput(ym="2026-08", grid=grid,
                        pgy_roster=["A", "B", "C"], clerk_batches=[])
    day_slots, log, warnings = month_solve_day(inp)
    mon = day_slots["2026-08-03"]["上午"]
    assert mon[TREATMENT]                                    # 週一早有治療室
    assert log and not warnings                              # 無 Clerk → 無切片警告


def test_month_solve_day_biopsy_missed_warning():
    grid = month_grid("2026-08", _TEMPLATE, set())
    batch = ClerkBatch("b1", date(2026, 8, 3), ["1", "2", "3"])
    inp = DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=["A"],
        clerk_batches=[batch], biopsy_open={})               # 切片室全程不開
    _ds, _log, warnings = month_solve_day(inp)
    assert any("切片室輪不到" in w for w in warnings)         # 3 人都沒輪到


def test_month_solve_day_clerk_only_within_batch():
    """跨梯次：batch1 成員不得排進 batch2 涵蓋的日期（P1 修正）。"""
    grid = month_grid("2026-08", _TEMPLATE, set())
    b1 = ClerkBatch("b1", date(2026, 8, 3), ["1"])       # 8/3–8/16
    b2 = ClerkBatch("b2", date(2026, 8, 17), ["9"])      # 8/17–8/30
    inp = DaySolveInput(ym="2026-08", grid=grid, pgy_roster=["A", "B"],
                        clerk_batches=[b1, b2])
    day_slots, _log, _w = month_solve_day(inp)

    def _people(iso):
        out = set()
        for sess in day_slots.get(iso, {}).values():
            for who in sess.values():
                out.update(who)
        return out
    assert "1" in _people("2026-08-03") and "9" not in _people("2026-08-03")
    assert "9" in _people("2026-08-17") and "1" not in _people("2026-08-17")


def test_clerk_fairness_resets_per_batch():
    """代號跨梯重用：新梯的 '1' 不應繼承舊梯 '1' 的座位數而被冷落。"""
    one_room = {str(wd): {"上午": [{"room": "101"}]} for wd in range(5)}
    grid = month_grid("2026-08", one_room, set())
    b1 = ClerkBatch("b1", date(2026, 8, 3), ["1"])       # 前兩週只有 "1"，天天就座
    b2 = ClerkBatch("b2", date(2026, 8, 17), ["1", "2"])  # 後兩週 "1","2"（"1" 是新人）
    inp = DaySolveInput(ym="2026-08", grid=grid, pgy_roster=["A"],
                        clerk_batches=[b1, b2])
    day_slots, _log, _w = month_solve_day(inp)
    # 收集 b2 期間 101 診間坐過的人
    seated_b2 = set()
    for iso, sess in day_slots.items():
        if iso >= "2026-08-17":
            seated_b2.update((sess.get("上午") or {}).get("101", []))
    assert "1" in seated_b2 and "2" in seated_b2          # 新梯 "1" 有被公平排到
