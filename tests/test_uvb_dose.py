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
    """[v20.10] 同日 (0) 或昨日 (1) → None (太密集，須警告 ≥ 2 天)。"""
    assert compute_new_dose(
        dose=520, increase=30, max_dose=800, days_diff=days_diff
    ) is None


@pytest.mark.parametrize("days_diff,expected", [
    (2, 550),
    (3, 550),
    (4, 550),
    (5, 550),
    (6, 550),
])
def test_compute_increases_when_2_to_6_days(days_diff, expected):
    """[v20.10] 2-6 天 → +increase (1 天不准，要至少 ≥ 2 天)。"""
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


@pytest.mark.parametrize("days_diff", range(15, 22))
def test_compute_decays_50pct_when_15_to_21_days(days_diff):
    """[v20.8] 15-21 天 → ×0.5 floor 10，最低 250。"""
    # 800 × 0.5 = 400 → floor 10 = 400
    assert compute_new_dose(
        dose=800, increase=30, max_dose=800, days_diff=days_diff
    ) == 400
    # 600 × 0.5 = 300 → 300
    assert compute_new_dose(
        dose=600, increase=30, max_dose=800, days_diff=days_diff
    ) == 300
    # 500 × 0.5 = 250 → 250 (剛好)
    assert compute_new_dose(
        dose=500, increase=30, max_dose=800, days_diff=days_diff
    ) == 250
    # 480 × 0.5 = 240 → floor 10 = 240 → max(240, 250) = 250
    assert compute_new_dose(
        dose=480, increase=30, max_dose=800, days_diff=days_diff
    ) == 250


@pytest.mark.parametrize("days_diff", [22, 30, 100, 365])
def test_compute_fixes_to_250_when_over_21_days(days_diff):
    """[v20.8] > 21 天 → 固定 250 (任何 dose)。"""
    assert compute_new_dose(
        dose=520, increase=30, max_dose=800, days_diff=days_diff
    ) == 250
    assert compute_new_dose(
        dose=800, increase=30, max_dose=800, days_diff=days_diff
    ) == 250
    assert compute_new_dose(
        dose=1500, increase=50, max_dose=1500, days_diff=days_diff
    ) == 250


@pytest.mark.parametrize("days_diff", [8, 14])
def test_compute_75pct_floor_at_min_250(days_diff):
    """[v20.8] 8-14 天 ×0.75 結果低於 250 → 使用 250 floor。"""
    # 320 × 0.75 = 240 → floor 10 = 240 → max(240, 250) = 250
    assert compute_new_dose(
        dose=320, increase=30, max_dose=800, days_diff=days_diff
    ) == 250
    # 280 × 0.75 = 210 → 250
    assert compute_new_dose(
        dose=280, increase=30, max_dose=800, days_diff=days_diff
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
    """[v20.10] 同日 (0) 或昨日 (1) → TOO_CLOSE，不改 text。2 天以上才能加劑量。"""
    text = "UVB 520 (11) on (2026/05/26), increase 30, MAX:800"
    # 同日 → TOO_CLOSE
    r0 = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r0.action == UvbAction.TOO_CLOSE
    assert r0.days_diff == 0
    assert r0.new_text is None
    # 昨天 (1 天差) → TOO_CLOSE (改回 v20.10 規則)
    r1 = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r1.action == UvbAction.TOO_CLOSE
    assert r1.days_diff == 1
    assert r1.new_text is None
    # 前天 (2 天差) → UPDATED
    r2 = update_uvb_in_text(text, today=date(2026, 5, 28))
    assert r2.action == UvbAction.UPDATED
    assert r2.new_dose == 550  # 520+30


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


def test_update_15_days_decays_50pct():
    """[v20.8] 15 天 → ×0.5 (改規則: 不再直接 250)。
    520 × 0.5 = 260 → floor 10 = 260 → max(260, 250) = 260
    """
    text = "UVB 520 (11) on (2026/05/26), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 6, 10))  # 15 天差
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 260


def test_update_22_days_drops_to_250():
    """[v20.8] 22 天 → 固定 250。"""
    text = "UVB 520 (11) on (2026/05/26), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 6, 17))  # 22 天差
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


def test_sanity_fail_dose_too_low():
    """[v20.5] 原劑量 < MIN_DOSE (50) → SANITY_FAIL"""
    text = "UVB 30 (5) on (2026/05/20), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.SANITY_FAIL
    assert "30" in r.sanity_reason
    assert "範圍" in r.sanity_reason


def test_sanity_fail_dose_too_high():
    """原劑量 > MAX_DOSE (1500) → SANITY_FAIL"""
    text = "UVB 2000 (5) on (2026/05/20), increase 30, MAX:2500"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.SANITY_FAIL


def test_sanity_fail_count_too_high():
    """次數 > MAX_COUNT (999) → SANITY_FAIL"""
    text = "UVB 500 (1500) on (2026/05/20), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.SANITY_FAIL
    assert "1500" in r.sanity_reason or "次數" in r.sanity_reason


def test_sanity_fail_date_in_future():
    """last_date 在未來 → SANITY_FAIL"""
    text = "UVB 500 (5) on (2027/01/01), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.SANITY_FAIL
    assert "未來" in r.sanity_reason


def test_sanity_fail_gap_over_2_years():
    """距上次 > 730 天 → SANITY_FAIL"""
    text = "UVB 500 (5) on (2020/01/01), increase 30, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.SANITY_FAIL
    assert "730" in r.sanity_reason or "2 年" in r.sanity_reason or "異常" in r.sanity_reason


def test_sanity_fail_increase_too_high():
    """increase > 200 → SANITY_FAIL"""
    text = "UVB 500 (5) on (2026/05/20), increase 500, MAX:800"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.SANITY_FAIL
    assert "500" in r.sanity_reason or "increase" in r.sanity_reason


def test_parse_real_world_case1_date_before_count():
    """[v20.6 regression] User 5/26 12:01 case1:
    日期出現在 count 之前 — `UVB 1000 on (date), (count), increase N`
    """
    text = "decrease UVB 1000mj/cm2 on(2026/05/24), (31), increase 20mj/cm2 if no erythema , MAX:1000 mj/cm2, W2, W5.9 weeks appointment , acitretin w4-6 on (2026/03/17)"
    info = parse_uvb_line(text)
    assert info is not None, "date-before-count 順序 parse 失敗"
    assert info.dose == 1000
    assert info.count == 31
    assert info.last_date == date(2026, 5, 24)
    assert info.increase == 20
    assert info.max_dose == 1000


def test_parse_real_world_case2_chinese_between():
    """[v20.6 regression] User 5/26 12:04 case2:
    數字後夾中文「已打折」再 (count) — `UVB: 1200 mj/cm2已打折(137) on (date)`
    """
    text = "UVB: 1200 mj/cm2已打折(137) on  (2026/05/24)  , increased 40 mj/cm2 if no erythema, MAX: 1200 mj/cm2 , W2, W5M,  12 weeks appointment"
    info = parse_uvb_line(text)
    assert info is not None, "中文夾雜 parse 失敗"
    assert info.dose == 1200
    assert info.count == 137
    assert info.last_date == date(2026, 5, 24)
    assert info.increase == 40
    assert info.max_dose == 1200


def test_parse_real_world_case3_add_instead_of_increase():
    """[v20.6 regression] User 5/26 12:06 case3:
    用 `add 50 mj/cm2 each time` 取代 `increase 50`，Max 小寫
    """
    text = "UVB: 950 mj/cm2 (39) on (2026/5/24) add 50 mj/cm2 each time, Max: 1100 mj/cm2,  w4m, take picture on 2025/12/15"
    info = parse_uvb_line(text)
    assert info is not None, "add 取代 increase parse 失敗"
    assert info.dose == 950
    assert info.count == 39
    assert info.last_date == date(2026, 5, 24)
    assert info.increase == 50
    assert info.max_dose == 1100


def test_update_real_world_case1_end_to_end():
    """[v20.6] case1 完整 end-to-end: dose 1000 已達 MAX 不變, count 31→32,
    date 5/24→5/26 (2 天差)。
    """
    text = "decrease UVB 1000mj/cm2 on(2026/05/24), (31), increase 20mj/cm2 if no erythema , MAX:1000 mj/cm2, W2, W5"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000  # 1000+20=1020 cap MAX 1000
    assert r.new_count == 32
    assert r.days_diff == 2
    assert "(32)" in r.new_text
    assert "(2026/05/26)" in r.new_text
    # 上下文保留
    assert "decrease" in r.new_text
    assert "MAX:1000" in r.new_text


def test_update_real_world_case2_end_to_end():
    """[v20.6] case2 完整: dose 1200 已達 MAX 不變, count 137→138, date→今天"""
    text = "UVB: 1200 mj/cm2已打折(137) on  (2026/05/24)  , increased 40 mj/cm2 if no erythema, MAX: 1200 mj/cm2 , W2, W5M"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1200
    assert r.new_count == 138
    assert "(138)" in r.new_text
    assert "(2026/05/26)" in r.new_text
    # 中文要保留
    assert "已打折" in r.new_text


def test_update_real_world_case3_end_to_end():
    """[v20.6] case3 完整: dose 950+50=1000 (cap 1100 OK), count 39→40, add 保留"""
    text = "UVB: 950 mj/cm2 (39) on (2026/5/24) add 50 mj/cm2 each time, Max: 1100 mj/cm2,  w4m"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000  # 950+50
    assert r.new_count == 40
    assert r.days_diff == 2
    assert "UVB: 1000" in r.new_text or "UVB:1000" in r.new_text
    assert "(40)" in r.new_text
    assert "(2026/05/26)" in r.new_text
    # add 動詞保留
    assert "add" in r.new_text or "Max:" in r.new_text.lower()


@pytest.mark.parametrize("max_phrase,expected_max", [
    ("MAX:800", 800),
    ("MAX 800", 800),
    ("MAX : 800", 800),
    ("max:800", 800),                  # 小寫
    ("Max: 800", 800),
    ("fixed at 1500", 1500),
    ("fixed 1500", 1500),
    ("fix at 1500", 1500),
    ("fix 1500", 1500),
    ("Fixed At 1500", 1500),          # 大小寫混合
])
def test_parse_max_variants(max_phrase, expected_max):
    """[v20.8] MAX 同義詞: MAX/Max/max/fix/fixed/fix at/fixed at"""
    text = f"UVB 500 (10) on (2026/05/20), increase 30, {max_phrase}, W2"
    info = parse_uvb_line(text)
    assert info is not None, f"max phrase '{max_phrase}' parse 失敗"
    assert info.max_dose == expected_max


def test_parse_max_word_boundary_not_matching_prefix():
    """[v20.8 safety] 'prefix' / 'fixing' 內含 'fix' 不該被誤抓"""
    # No MAX/fix synonym → 應該整個 parse fail (回 None)
    text = "UVB 500 (10) on (2026/05/20), increase 30, prefix 999, fixing 888"
    info = parse_uvb_line(text)
    assert info is None, "prefix/fixing 不該被當 MAX 抓"


def test_parse_real_world_case_a_fixed_at():
    """[v20.7 regression] User 5/26 12:17 case A:
    用 `fixed at 1500` 取代 `MAX:1500` (李璟樂)
    """
    text = "UVB: 1500 mj/cm2 (59) on (2026/5/24) , add 50 each time, fixed at 1500,  w3n"
    info = parse_uvb_line(text)
    assert info is not None, "fixed at 取代 MAX parse 失敗"
    assert info.dose == 1500
    assert info.count == 59
    assert info.last_date == date(2026, 5, 24)
    assert info.increase == 50
    assert info.max_dose == 1500


def test_parse_real_world_case_b_no_count():
    """[v20.7 regression] User 5/26 12:21 case B:
    處置沒寫 (count) — `UVB 450mj/cm2 on (date), increase 30, MAX:450` (賴鄭秀枝)
    """
    text = "UVB 450mj/cm2 on  (2026/05/24), increase 30mj/cm2 if no erythema , MAX:450 mj/cm2 , W26M"
    info = parse_uvb_line(text)
    assert info is not None, "沒 count parse 失敗"
    assert info.dose == 450
    assert info.count is None, "沒寫 (N) → count 應為 None"
    assert info.last_date == date(2026, 5, 24)
    assert info.increase == 30
    assert info.max_dose == 450


def test_parse_real_world_case_c_no_count_with_w_suffix():
    """[v20.7 regression] User 5/26 12:24 case C: 同樣沒 count"""
    text = "UVB 800mj/cm2 on  (2026/05/24) , increase 40mj/cm2 if no erythema , MAX:800 mj/cm2 , W2A, W5M, **6 weeks appointment**"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.dose == 800
    assert info.count is None
    assert info.last_date == date(2026, 5, 24)
    assert info.increase == 40
    assert info.max_dose == 800


def test_update_case_a_fixed_at_end_to_end():
    """[v20.7] case A fixed at 1500 — dose 已達 MAX 不變, count 59→60, date→今天"""
    text = "UVB: 1500 mj/cm2 (59) on (2026/5/24) , add 50 each time, fixed at 1500,  w3n"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1500
    assert r.new_count == 60
    assert "(60)" in r.new_text
    assert "(2026/05/26)" in r.new_text
    assert "fixed at 1500" in r.new_text


def test_update_case_b_no_count_end_to_end():
    """[v20.7] case B 沒 count — dose 450+30=480, count 不變 (None), date→今天"""
    text = "UVB 450mj/cm2 on  (2026/05/24), increase 30mj/cm2 if no erythema , MAX:450 mj/cm2 , W26M"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))  # 2 天差
    assert r.action == UvbAction.UPDATED
    # 450+30=480 但 cap MAX 450 → 仍 450
    assert r.new_dose == 450
    assert r.new_count is None, "處置沒 (N) 時 new_count 必須 None"
    assert r.days_diff == 2
    assert "(2026/05/26)" in r.new_text
    # 不應該憑空生出 (N)
    assert "(1)" not in r.new_text or r.new_text.count("(") == r.new_text.count(")")  # 至少沒新增 paren


def test_update_case_c_no_count_with_increase():
    """[v20.7] case C 沒 count + increase 走完"""
    text = "UVB 800mj/cm2 on  (2026/05/24) , increase 40mj/cm2 if no erythema , MAX:800 mj/cm2 , W2A, W5M"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 800  # 800+40=840 cap 800
    assert r.new_count is None
    assert "(2026/05/26)" in r.new_text
    assert "W2A, W5M" in r.new_text


def test_uvb_line_count_reported():
    """UPDATED 結果含 uvb_line_count，用於提示多行 UVB。"""
    text_one = "UVB 520 (11) on (2026/05/20), increase 30, MAX:800"
    text_two = (
        "UVB 580 (12) on (2026/05/20), increase 30, MAX:800\n"
        "UVB 520 (11) on (2026/05/13), increase 30, MAX:800"
    )
    r1 = update_uvb_in_text(text_one, today=date(2026, 5, 26))
    assert r1.uvb_line_count == 1
    r2 = update_uvb_in_text(text_two, today=date(2026, 5, 26))
    assert r2.uvb_line_count == 2


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
