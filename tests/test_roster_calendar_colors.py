# -*- coding: utf-8 -*-
"""決定性週色規則：以 115 行事曆 PDF 實測的 2026 全年 53 週為 oracle 驗證。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.calendar_colors import (  # noqa: E402
    week_color, week_colors_for_year,
)
from cmuh_common.roster.model import week_key  # noqa: E402

# 實測自 1_115年行事曆.pdf（西元2026）：4 週一段交替
_PINK_WEEKS_2026 = {3, 4, 5, 6, 11, 12, 13, 14, 19, 20, 21, 22, 27, 28, 29, 30,
                    35, 36, 37, 38, 43, 44, 45, 46, 51, 52, 53}
_GREEN_WEEKS_2026 = {1, 2, 7, 8, 9, 10, 15, 16, 17, 18, 23, 24, 25, 26,
                     31, 32, 33, 34, 39, 40, 41, 42, 47, 48, 49, 50}


def test_matches_pdf_oracle_2026_all_weeks():
    colors = week_colors_for_year(2026)
    for w in _PINK_WEEKS_2026:
        assert colors[f"2026-W{w:02d}"] == "pink", f"W{w} 應為粉"
    for w in _GREEN_WEEKS_2026:
        assert colors[f"2026-W{w:02d}"] == "green", f"W{w} 應為綠"
    # 沒有多餘/缺漏的週
    assert len(colors) == len(_PINK_WEEKS_2026) + len(_GREEN_WEEKS_2026)


def test_known_anchor_and_boundaries():
    assert week_color(date(2026, 1, 12)) == "pink"     # 錨(2026-W03)
    assert week_color(date(2026, 1, 1)) == "green"     # W01
    assert week_color(date(2026, 8, 3)) == "green"     # W32(8月上半)
    assert week_color(date(2026, 8, 24)) == "pink"     # W35


def test_four_week_block_alternation():
    # 同一段內 4 週同色；跨段換色
    for w in (3, 4, 5, 6):
        assert week_color(date.fromisocalendar(2026, w, 1)) == "pink"
    for w in (7, 8, 9, 10):
        assert week_color(date.fromisocalendar(2026, w, 1)) == "green"


def test_continuous_into_next_year():
    # 相位對「絕對週」連續 → 2026 末與 2027 初不斷點
    dec28 = date(2026, 12, 28)          # 2026-W53 週一(粉)
    jan4_2027 = date(2027, 1, 4)        # 下一週週一
    assert week_color(dec28) == "pink"
    # 2026-W51/52/53 粉(3週) → 2027 初應延續成第 4 週粉，再翻綠
    assert week_color(jan4_2027) == "pink"          # 該粉段第 4 週
    assert week_color(date(2027, 1, 11)) == "green"  # 翻綠


def test_year_dict_covers_boundary_weeks():
    c = week_colors_for_year(2026)
    assert "2026-W01" in c and "2026-W53" in c
    # W01 的週一在 2025-12-29，week_key 仍歸 2026-W01
    assert week_key(date(2025, 12, 29)) == "2026-W01"
