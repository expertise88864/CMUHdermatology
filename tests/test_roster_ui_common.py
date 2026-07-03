# -*- coding: utf-8 -*-
"""roster UI 純函式（不建立 Tk 視窗，可在無顯示器環境跑）。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.ui.common import (  # noqa: E402
    MEMBER_PALETTE, calendar_matrix, member_color, next_in_cycle, ym_add, ym_of,
)


def test_ym_add_wraps_year():
    assert ym_add("2026-08", 1) == "2026-09"
    assert ym_add("2026-12", 1) == "2027-01"
    assert ym_add("2026-01", -1) == "2025-12"
    assert ym_add("2026-08", -14) == "2025-06"
    assert ym_add("2026-08", 0) == "2026-08"


def test_ym_of():
    assert ym_of(date(2026, 8, 3)) == "2026-08"


def test_calendar_matrix_shape_and_placement():
    # 2026/8/1 = 週六 → 週一起始下，第一列前 5 格是 None，8/1 落在第 6 欄
    weeks = calendar_matrix(2026, 8)
    assert all(len(w) == 7 for w in weeks)
    assert weeks[0][:5] == [None] * 5
    assert weeks[0][5] == date(2026, 8, 1)      # 週六欄
    assert weeks[0][6] == date(2026, 8, 2)      # 週日欄
    # 攤平後恰好含全部 31 天、其餘為 None
    flat = [d for w in weeks for d in w]
    real = [d for d in flat if d is not None]
    assert real == [date(2026, 8, d) for d in range(1, 32)]
    assert len(flat) % 7 == 0


def test_calendar_matrix_month_starting_monday():
    # 2026/6/1 = 週一 → 第一格就是 6/1，無前導 None
    weeks = calendar_matrix(2026, 6)
    assert weeks[0][0] == date(2026, 6, 1)


def test_next_in_cycle_full_loop():
    ids = ["A", "B", "C"]
    assert next_in_cycle(None, ids) == "A"
    assert next_in_cycle("A", ids) == "B"
    assert next_in_cycle("C", ids) is None       # 尾端回 None
    assert next_in_cycle("ZZ", ids) is None       # 非名單 → 清掉
    assert next_in_cycle(None, []) is None        # 空名單


def test_member_color_stable_and_wraps():
    assert member_color(0) == MEMBER_PALETTE[0]
    assert member_color(len(MEMBER_PALETTE)) == MEMBER_PALETTE[0]   # 循環
    assert member_color(1) != member_color(0)
