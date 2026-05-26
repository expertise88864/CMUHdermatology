# -*- coding: utf-8 -*-
"""UVB 自動調整劑量 — 純邏輯測試。

涵蓋所有 day-bucket 規則 + edge cases + format 容錯。
劑量算錯 = 醫療事故，test cover 要充足。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.uvb_dose import (  # noqa: E402
    UvbAction,
    UvbLineInfo,
    compute_new_dose,
    format_uvb_line,
    parse_uvb_line,
    update_uvb_in_text,
)


# ─── compute_new_dose: 各 day-bucket ────────────────────────────────────

@pytest.mark.parametrize("days_diff", [0, 1])
def test_compute_returns_none_when_too_close(days_diff):
    """0-1 天 → None (太密集，須警告)。"""
    assert compute_new_dose(
        dose=520, increase=30, max_dose=800, days_diff=days_diff
    ) is None


@pytest.mark.parametrize("days_diff,expected", [
    (2, 550),   # 520 + 30
    (3, 550),
    (4, 550),
    (5, 550),
    (6, 550),
])
def test_compute_increases_when_2_to_6_days(days_diff, expected):
    """2-6 天 → +increase。"""
    assert compute_new_dose(
        dose=520, increase=30, max_dose=800, days_diff=days_diff
    ) == expected


def test_compute_caps_at_max_when_increase_overflows():
    """+increase 會超過 MAX → cap MAX。"""
    assert compute_new_dose(
        dose=780, increase=30, max_dose=800, days_diff=3
    ) == 800
    assert compute_new_dose(
        dose=800, increase=30, max_dose=800, days_diff=3
    ) == 800


def test_compute_keeps_dose_at_7_days_exact():
    """剛好 7 天 → 保持。"""
    assert compute_new_dose(
        dose=520, increase=30, max_dose=800, days_diff=7
    ) == 520


@pytest.mark.parametrize("days_diff", range(8, 15))
def test_compute_decays_75pct_floor_10_when_8_to_14_days(days_diff):
    """8-14 天 → ×0.75 floor 10。"""
    # 520 × 0.75 = 390 (已 0 結尾)
    assert compute_new_dose(
        dose=520, increase=30, max_dose=800, days_diff=days_diff
    ) == 390
    # 580 × 0.75 = 435 → floor 10 → 430
    assert compute_new_dose(
        dose=580, increase=30, max_dose=800, days_diff=days_diff
    ) == 430
    # 600 × 0.75 = 450 (已 0 結尾)
    assert compute_new_dose(
        dose=600, increase=30, max_dose=800, days_diff=days_diff
    ) == 450
    # 432 (假設) × 0.75 = 324 → floor 10 → 320
    assert compute_new_dose(
        dose=432, increase=30, max_dose=800, days_diff=days_diff
    ) == 320


@pytest.mark.parametrize("days_diff", [15, 16, 30, 100, 365])
def test_compute_fixes_to_250_when_over_14_days(days_diff):
    """> 14 天 → 固定 250。"""
    assert compute_new_dose(
        dose=520, increase=30, max_dose=800, days_diff=days_diff
    ) == 250
    # 不論原 dose 多少都 250
    assert compute_new_dose(
        dose=800, increase=30, max_dose=800, days_diff=days_diff
    ) == 250


# ─── parse_uvb_line: 各種 format 容錯 ────────────────────────────────────

def test_parse_normal_format():
    text = "UVB 520mj/cm2  (11) on  (2026/05/26)  , increase 30mj/cm2 if no erythema , MAX:800 mj/cm2 , W2, W5M"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.dose == 520
    assert info.count == 11
    assert info.last_date == date(2026, 5, 26)
    assert info.increase == 30
    assert info.max_dose == 800


def test_parse_without_zero_padded_date():
    """日期 (2026/5/6) 不零填充也要過。"""
    text = "UVB 520 (11) on (2026/5/6), increase 30 if no erythema, MAX:800"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.last_date == date(2026, 5, 6)


def test_parse_without_mj_cm2_unit():
    """劑量不帶 mj/cm2 也要過。"""
    text = "UVB 520 (11) on (2026/05/26), increase 30 if no erythema, MAX:800"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.dose == 520
    assert info.increase == 30


def test_parse_max_without_colon():
    """MAX 800 沒冒號也要過。"""
    text = "UVB 520 (11) on (2026/05/26), increase 30, MAX 800"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.max_dose == 800


def test_parse_uvb_with_colon():
    """[v20.2 regression] UVB: (含冒號) 也要 parse 成功。
    User 5/26 實機 data: 'UVB: 970mj/cm2 (197) on (2026/05/24), increase 50, MAX: 1000'
    """
    text = "已打8折medication and follow up, UVB: 970mj/cm2 (197) on   (2026/05/24)         , increase 50mj/cm2 if no erythema  , MAX: 1000, W2, , 8 weeks (2025/3/4) tar shampoo"
    info = parse_uvb_line(text)
    assert info is not None, f"UVB: 冒號 parse 失敗，real-world text 無法處理"
    assert info.dose == 970
    assert info.count == 197
    assert info.last_date == date(2026, 5, 24)
    assert info.increase == 50
    assert info.max_dose == 1000


def test_parse_increased_past_tense():
    """[v20.4 regression] 'increased 40' (有 d) 也要 parse 成功。
    User 5/26 11:38 real-world: 'UVB: 1100mj/cm2(87) on (2026/5/24), 已打8折,
    increased 40 mj/cm2 if no erythema, MAX: 1100 mj/cm2, W2,W5'
    """
    text = "UVB: 1100mj/cm2(87) on  (2026/5/24), 已打8折, increased 40 mj/cm2 if no erythema, MAX: 1100 mj/cm2, W2,W5 ( 2weeks,) than W5 , , 12weeks appointment"
    info = parse_uvb_line(text)
    assert info is not None, "increased (past tense) parse 失敗"
    assert info.dose == 1100
    assert info.count == 87
    assert info.last_date == date(2026, 5, 24)
    assert info.increase == 40
    assert info.max_dose == 1100


def test_update_real_world_increased_at_max():
    """[v20.4 regression] dose 已達 MAX 仍要更新 count + date。
    User 5/26 病人: dose 1100 == MAX 1100, 預期 dose 不變但 count 87→88,
    date 5/24→5/26。
    """
    text = "UVB: 1100mj/cm2(87) on  (2026/5/24), 已打8折, increased 40 mj/cm2 if no erythema, MAX: 1100 mj/cm2, W2,W5"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))  # 2 天差
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1100  # 1100+40=1140 cap MAX 1100 → 維持 1100
    assert r.new_count == 88
    assert r.days_diff == 2
    assert "(88)" in r.new_text
    assert "(2026/05/26)" in r.new_text
    # 1100 不變不該被誤改 — 確認 UVB: 1100 仍在
    assert "UVB: 1100" in r.new_text or "UVB:1100" in r.new_text


def test_update_real_world_with_colon():
    """[v20.2 regression] 完整 end-to-end 帶冒號 + 上下文亂七八糟字元。"""
    text = "已打8折medication and follow up, UVB: 970mj/cm2 (197) on   (2026/05/24)         , increase 50mj/cm2 if no erythema  , MAX: 1000, W2, , 8 weeks (2025/3/4) tar shampoo"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))  # 2 天差
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000  # 970+50=1020 cap MAX 1000
    assert r.new_count == 198
    assert r.days_diff == 2
    assert "UVB: 1000" in r.new_text or "UVB:1000" in r.new_text
    assert "(198)" in r.new_text
    assert "(2026/05/26)" in r.new_text
    # 保留其餘
    assert "已打8折medication" in r.new_text
    assert "MAX: 1000" in r.new_text
    assert "8 weeks (2025/3/4) tar shampoo" in r.new_text
    # 舊值不應殘留
    assert "(197)" not in r.new_text
    assert "(2026/05/24)" not in r.new_text


def test_parse_with_extra_whitespace():
    """多餘空白都要忽略。"""
    text = "UVB    520    mj/cm2   (  11  )  on   (  2026/05/26  )  , increase    30    mj/cm2 if no erythema , MAX  :  800"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.dose == 520
    assert info.count == 11


def test_parse_returns_none_when_no_uvb():
    """沒含 UVB → None。"""
    text = "局部 keep phototherapy on both lower limbs to 680 mj/cm2 on (2026/5/26)"
    assert parse_uvb_line(text) is None


def test_parse_returns_none_when_format_broken():
    """有 UVB 但缺欄位 → None。"""
    assert parse_uvb_line("UVB 520") is None
    assert parse_uvb_line("UVB 520 (11)") is None  # 缺 date
    assert parse_uvb_line("UVB 520 (11) on (2026/05/26)") is None  # 缺 increase


def test_parse_picks_first_uvb_line_when_multiple():
    """處置含多行 UVB 歷史，只抓第一個。"""
    text = (
        "UVB 580mj/cm2 (12) on (2026/05/26), increase 30mj/cm2 if no erythema, MAX:800\n"
        "UVB 520mj/cm2 (11) on (2026/05/19), increase 30mj/cm2 if no erythema, MAX:800"
    )
    info = parse_uvb_line(text)
    assert info is not None
    assert info.dose == 580
    assert info.count == 12
    assert info.last_date == date(2026, 5, 26)


# ─── format_uvb_line: 替換 dose/count/date 保留其餘 ──────────────────────

def test_format_replaces_dose_count_date_keeps_max_increase():
    """替換 dose/count/date，MAX/increase/W2/W5M 等保留。"""
    info = UvbLineInfo(
        full_match="UVB 520mj/cm2  (11) on  (2026/05/26)  , increase 30mj/cm2 if no erythema , MAX:800 mj/cm2 , W2, W5M",
        dose=520, count=11, last_date=date(2026, 5, 26),
        increase=30, max_dose=800,
        span=(0, 0),
    )
    new_line = format_uvb_line(info, new_dose=550, new_count=12,
                               today=date(2026, 5, 28))
    assert "UVB 550" in new_line
    assert "(12)" in new_line
    assert "(2026/05/28)" in new_line
    # 保留
    assert "MAX:800" in new_line
    assert "increase 30" in new_line
    assert "W2, W5M" in new_line
    # 不該還有 520, 11, 2026/05/26
    assert "520" not in new_line
    assert "(11)" not in new_line
    assert "2026/05/26" not in new_line


def test_format_normalizes_date_to_zero_padded():
    """原日期 (2026/5/6) → 寫回零填充 (2026/05/06)。"""
    info = UvbLineInfo(
        full_match="UVB 520 (11) on (2026/5/6), increase 30, MAX:800",
        dose=520, count=11, last_date=date(2026, 5, 6),
        increase=30, max_dose=800,
        span=(0, 0),
    )
    new_line = format_uvb_line(info, new_dose=550, new_count=12,
                               today=date(2026, 7, 4))
    assert "(2026/07/04)" in new_line


# ─── update_uvb_in_text: 端對端 ──────────────────────────────────────────

def test_update_normal_case_2_to_6_days_increases():
    """2-6 天 + increase + count+1 + date→today。"""
    text = (
        "局部 something else\n"
        "UVB 520mj/cm2 (11) on (2026/05/26), increase 30mj/cm2 if no erythema, MAX:800 mj/cm2, W2\n"
        "其他歷史紀錄"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 28))  # 2 天差
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 550
    assert r.new_count == 12
    assert r.days_diff == 2
    assert "UVB 550" in r.new_text
    assert "(12)" in r.new_text
    assert "(2026/05/28)" in r.new_text
    # 上下文保留
    assert "局部 something else" in r.new_text
    assert "其他歷史紀錄" in r.new_text


def test_update_too_close_returns_warning_no_text_change():
    """0-1 天 → TOO_CLOSE，不改 text。"""
    text = "UVB 520 (11) on (2026/05/26), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 5, 27))  # 1 天差
    assert r.action == UvbAction.TOO_CLOSE
    assert r.days_diff == 1
    assert r.new_text is None
    assert r.last_date == date(2026, 5, 26)


def test_update_same_day_is_too_close():
    text = "UVB 520 (11) on (2026/05/26), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))  # 0 天
    assert r.action == UvbAction.TOO_CLOSE
    assert r.days_diff == 0


def test_update_no_uvb_returns_fallback():
    """處置沒 UVB → NO_UVB_LINE 給 caller 走 fallback。"""
    text = "keep phototherapy on both lower limbs to 680 mj/cm2 on (2026/5/26)"
    r = update_uvb_in_text(text, today=date(2026, 5, 28))
    assert r.action == UvbAction.NO_UVB_LINE
    assert r.new_text is None


def test_update_8_days_decay():
    """8 天 → ×0.75 floor 10。"""
    text = "UVB 520 (11) on (2026/05/26), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 6, 3))  # 8 天差
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 390  # 520 × 0.75 = 390


def test_update_15_days_drops_to_250():
    """15 天 → 固定 250。"""
    text = "UVB 520 (11) on (2026/05/26), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 6, 10))  # 15 天差
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 250


def test_update_cap_max_when_increase_overflows():
    """劑量 +increase 會超過 MAX → cap MAX。"""
    text = "UVB 790 (20) on (2026/05/26), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 5, 28))  # 2 天
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 800  # cap at MAX


def test_update_count_increments_correctly():
    """count: 11 → 12, 23 → 24, 99 → 100。"""
    for old_count, new_count in [(11, 12), (23, 24), (99, 100)]:
        text = f"UVB 520 ({old_count}) on (2026/05/26), increase 30, MAX:800"
        r = update_uvb_in_text(text, today=date(2026, 5, 28))
        assert r.new_count == new_count


def test_update_preserves_extra_text_after_uvb_line():
    """UVB 行後面還有文字（W2 / W5M / 其他歷史）要保留。"""
    text = (
        "UVB 520mj/cm2 (11) on (2026/05/26), increase 30mj/cm2 if no erythema, "
        "MAX:800 mj/cm2 , W2, W5M\n"
        "(2025/01/15) certificate"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 28))
    assert r.action == UvbAction.UPDATED
    assert "W2, W5M" in r.new_text
    assert "(2025/01/15) certificate" in r.new_text
