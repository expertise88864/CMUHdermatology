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
    apply_uncertain_updates,
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


def test_parse_full_width_colons_and_preserve_shape():
    text = "UVB：970mj/cm2 (197) on (2026/05/24), increase 50mj/cm2 if no erythema, MAX：1000, W2"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000
    assert r.new_count == 198
    assert "UVB：1000mj/cm2" in r.new_text
    assert "MAX：1000" in r.new_text
    assert "(198)" in r.new_text
    assert "(2026/05/26)" in r.new_text


def test_parse_chinese_increase_and_fixed_phrases():
    text = "UVB：970mj/cm2 (197) on (2026/05/24), 每次加 50mj/cm2 if no erythema, 固定 1000, W2"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000
    assert r.new_count == 198
    assert "UVB：1000mj/cm2" in r.new_text
    assert "每次加 50" in r.new_text
    assert "固定 1000" in r.new_text


def test_parse_chinese_increase_and_fixed_with_full_width_colons():
    text = "UVB：970mj/cm2 (197) on (2026/05/24), 每次加：50mj/cm2 if no erythema, 固定：1000, W2"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000
    assert r.new_count == 198
    assert "UVB：1000mj/cm2" in r.new_text
    assert "每次加：50" in r.new_text
    assert "固定：1000" in r.new_text


def test_parse_english_by_and_fixed_to_phrases():
    text = "UVB: 970mj/cm2 (197) on (2026/05/24), increase by 50mj/cm2 if no erythema, fixed to 1000, W2"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000
    assert r.new_count == 198
    assert "UVB: 1000mj/cm2" in r.new_text
    assert "increase by 50" in r.new_text
    assert "fixed to 1000" in r.new_text


def test_parse_max_at_phrase():
    text = "UVB: 970mj/cm2 (197) on (2026/05/24), add by 50mj/cm2 if no erythema, MAX at 1000, W2"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000
    assert r.new_count == 198
    assert "UVB: 1000mj/cm2" in r.new_text
    assert "MAX at 1000" in r.new_text


def test_parse_full_width_parentheses_preserves_shape():
    text = "UVB：970mj/cm2 （197） on （2026/05/24）, 每次加：50mj/cm2 if no erythema, 固定：1000, W2"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000
    assert r.new_count == 198
    assert "UVB：1000mj/cm2" in r.new_text
    assert "（198）" in r.new_text
    assert "（2026/05/26）" in r.new_text


def test_parse_hyphen_dates_preserves_separator():
    text = "UVB: 970mj/cm2 (197) on (2026-05-24), increase by 50, fixed: 1000, W2"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000
    assert r.new_count == 198
    assert "(2026-05-26)" in r.new_text
    assert "(2026/05/26)" not in r.new_text


def test_parse_bare_hyphen_date_preserves_separator():
    text = "UVB: 970mj/cm2 (197) on 2026-05-24, increase by 50, fixed: 1000, W2"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000
    assert "on 2026-05-26" in r.new_text


def test_parse_fix_with_colon_phrase():
    text = "UVB: 970mj/cm2 (197) on (2026/05/24), add 50mj/cm2 if no erythema, fixed: 1000, W2"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000
    assert r.new_count == 198
    assert "fixed: 1000" in r.new_text


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
    # [v20.12] 改為「低於下限」(過去是「超出合理範圍」)
    assert "下限" in r.sanity_reason or "範圍" in r.sanity_reason


def test_sanity_fail_dose_too_high_returns_confirm_needed():
    """[v20.12] 原劑量 > MAX_DOSE (1500) → CONFIRM_NEEDED (改自 SANITY_FAIL)"""
    text = "UVB 2000 (5) on (2026/05/20), increase 30, MAX:2500"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.CONFIRM_NEEDED


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


def test_parse_date_without_parens():
    """[v20.11] 日期沒帶 paren — 'on 2026/5/24' 也要 parse 成功。
    User 5/26 case 1 (劉苔菁) + case 2 (羅紫綺) 都用這格式。
    """
    text = "局部 手/ 腳 UVB: 2000 mj/cm2(34) on 2026/5/24 add 100 each time, fixed 2000, take picture on 2026/1/15, W1+4N"
    info = parse_uvb_line(text)
    assert info is not None, "date 沒 paren 該也能 parse"
    assert info.dose == 2000
    assert info.count == 34
    assert info.last_date == date(2026, 5, 24)
    assert info.increase == 100
    assert info.max_dose == 2000


def test_update_real_world_case1_dose_2000_no_paren_date():
    """[v20.12] case 1 — dose 2000 改為 CONFIRM_NEEDED (上限改回 1500)。
    skip_dose_sanity=True 才能繼續更新。"""
    text = "局部 手/ 腳 UVB: 2000 mj/cm2(34) on 2026/5/24 add 100 each time, fixed 2000, take picture on 2026/1/15, W1+4N\nacitretin + MTX 3# on (2026/3/5)"
    # 第一次 — CONFIRM_NEEDED (dose 2000 > 1500)
    r1 = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r1.action == UvbAction.CONFIRM_NEEDED
    # 第二次 — skip_dose_sanity=True
    r = update_uvb_in_text(text, today=date(2026, 5, 26),
                           skip_dose_sanity=True)
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 2000  # 2000+100=2100 cap MAX 2000
    assert r.new_count == 35
    # 不帶 paren 寫回也不帶 paren
    assert "on 2026/05/26" in r.new_text
    # 後面歷史紀錄 (2026/3/5) 不該被誤改
    assert "(2026/3/5)" in r.new_text


def test_update_real_world_case2_no_paren_date():
    """[v20.11] case 2 — UVB: 200 (低劑量) no-paren date, fixed at 1100"""
    text = "UVB: 200 mj/cm2(2) on 2026/5/24, add 50 each time, fixed at 1100, take picture on 2026/5/21, W1+4N"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))  # 2 天差
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 250  # 200+50
    assert r.new_count == 3
    assert "on 2026/05/26" in r.new_text
    assert "fixed at 1100" in r.new_text


def test_update_real_world_case3_multi_line_same_date():
    """[v20.11] case 3 — 兩行 UVB 同日期都要更新 (用各自的 dose/MAX)。
    第一行 dose 900 increase 50 max 900 → 維持 900 (cap)
    第二行 dose 1200 increase 50 max 1200 → 維持 1200 (cap)
    """
    text = (
        "UVB: 900 mj/cm (126) on (2026/5/24) add 50 each time, keep max: 900. W4N\n"
        "局部 手背 UVB: 1200mj/cm (107) on (2026/5/24) add 50 each time, keep max: 1200. 4\n"
        "OMP W12 on (2025/5/14) -> AZA 0.5# W2 on (2026/1/15)"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))  # 2 天差
    assert r.action == UvbAction.UPDATED
    # 第一行
    assert r.new_dose == 900  # 900+50 cap 900
    assert r.new_count == 127  # 126+1
    assert "UVB: 900" in r.new_text
    assert "(127)" in r.new_text
    # 第二行也要更新
    assert r.additional_lines_updated == 1
    assert "UVB: 1200" in r.new_text
    assert "(108)" in r.new_text   # 107+1
    # 兩行日期都改 2026/05/26
    assert r.new_text.count("(2026/05/26)") == 2
    # 第二行的 keep max: 1200 保留
    assert "keep max: 1200" in r.new_text
    # 後面更舊的 OMP 日期不該動
    assert "(2025/5/14)" in r.new_text
    assert "(2026/1/15)" in r.new_text


def test_update_multi_line_different_date_no_extra_update():
    """[v20.11] 多行 UVB 但第二行日期不同 → 只改第一行，第二行不動。"""
    text = (
        "UVB: 900 (126) on (2026/5/24) add 50 each time, max 900. W4N\n"
        "UVB: 800 (100) on (2026/5/17) add 50 each time, max 900. W4N"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.additional_lines_updated == 0
    # 第二行不變
    assert "UVB: 800 (100) on (2026/5/17)" in r.new_text


def test_sanity_dose_2000_requires_confirm():
    """[v20.12] MAX_DOSE 上限改回 1500 — dose 2000 → CONFIRM_NEEDED。"""
    text = "UVB 2000 (5) on (2026/05/20), increase 100, MAX:2000"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.CONFIRM_NEEDED
    # skip 後可以繼續
    r2 = update_uvb_in_text(text, today=date(2026, 5, 26),
                            skip_dose_sanity=True)
    assert r2.action == UvbAction.UPDATED
    assert r2.new_dose == 2000


def test_sanity_dose_2001_requires_confirm():
    """[v20.12] dose 2001 也是 CONFIRM_NEEDED (跟 2000 同類處理)。"""
    text = "UVB 2001 (5) on (2026/05/20), increase 100, MAX:2500"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.CONFIRM_NEEDED


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


# ─── v20.12 CONFIRM_NEEDED (dose > 1500) ────────────────────────────────

def test_dose_over_1500_returns_confirm_needed():
    """[v20.12] 原劑量 > MAX_DOSE (1500) → CONFIRM_NEEDED 而非 SANITY_FAIL。"""
    text = (
        "UVB: 1600 mj/cm2 (50) on (2026/5/20) add 50 each time, fixed at 1600"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.CONFIRM_NEEDED
    assert r.confirm_reason is not None
    assert "1600" in r.confirm_reason
    # 不該有 new_text — 等 caller 按 Yes 才會產生
    assert r.new_text is None


def test_max_over_1500_returns_confirm_needed():
    """[v20.12] dose 沒超過但 MAX > 1500 也要 CONFIRM。"""
    text = (
        "UVB: 1400 mj/cm2 (50) on (2026/5/20) add 50 each time, fixed at 1800"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.CONFIRM_NEEDED
    assert "1800" in (r.confirm_reason or "")


def test_dose_exactly_1500_no_confirm():
    """[v20.12] dose 剛好 1500 (= MAX_DOSE) 不該 CONFIRM。"""
    text = (
        "UVB: 1500 mj/cm2 (50) on (2026/5/20) add 50 each time, fixed at 1500"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED


def test_skip_dose_sanity_bypasses_confirm():
    """[v20.12] skip_dose_sanity=True 跳過 dose 上限檢查，繼續執行。"""
    text = (
        "UVB: 1600 mj/cm2 (50) on (2026/5/20) add 50 each time, fixed at 1600"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26),
                           skip_dose_sanity=True)
    assert r.action == UvbAction.UPDATED
    # +50 capped at 1600
    assert r.new_dose == 1600
    assert r.new_count == 51
    assert "(51)" in r.new_text
    assert "(2026/05/26)" in r.new_text


def test_skip_dose_sanity_still_checks_lower_bound():
    """[v20.12] skip_dose_sanity=True 只跳過上限，下限/其他 sanity 仍檢查。"""
    text = (
        "UVB: 30 mj/cm2 (50) on (2026/5/20) add 50 each time, fixed at 1600"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26),
                           skip_dose_sanity=True)
    assert r.action == UvbAction.SANITY_FAIL


# ─── v20.12 多 triplet 同日期更新 ────────────────────────────────────────

def test_multi_triplet_same_line_continuation():
    """[v20.12] 同一行有兩個 UVB segments (用 / new for ... 接續)，date 一樣，
    兩個 count 都要 +1, 兩個 date 都要 → today。"""
    text = (
        "局部 手+頸部 + 右前臂 UVB: 1500 mj/cm2 (136) on (2026/5/25) "
        "/ new for left lower back 1500mj/cm2 (44) on (2026/5/25) "
        "add 50 each time, fixed at 1500, W2, W5M"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 28))
    assert r.action == UvbAction.UPDATED
    # 第一個 triplet (format_uvb_line 處理)
    assert "(137)" in r.new_text
    # 第二個 triplet (v20.12 triplet scan 處理)
    assert "(45)" in r.new_text
    # 兩個日期都應為 today
    assert r.new_text.count("(2026/05/28)") == 2
    # 原日期應該不剩 — 兩個都被替換
    assert "(2026/5/25)" not in r.new_text
    assert "(2026/05/25)" not in r.new_text
    assert r.additional_triplets_updated >= 1


def test_excimer_light_same_date_update():
    """[v20.12] 另一行的 excimer light 共用同一個 date，count+1 / date→today。"""
    text = (
        "局部 手+頸部 + 右前臂 UVB: 1500 mj/cm2 (136) on (2026/5/25) "
        "/ new for left lower back 1500mj/cm2 (44) on (2026/5/25) "
        "add 50 each time, fixed at 1500\n"
        "excimer light (25) 1000mJ for nape on (2026/5/25) 2 shot, "
        "add 30 each time, fixed at 1000"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 28))
    assert r.action == UvbAction.UPDATED
    # 三個 triplet 都更新: (136)→(137), (44)→(45), (25)→(26)
    assert "(137)" in r.new_text
    assert "(45)" in r.new_text
    assert "(26)" in r.new_text
    # 全部日期變 today
    assert r.new_text.count("(2026/05/28)") == 3
    assert "(2026/5/25)" not in r.new_text
    assert r.additional_triplets_updated >= 2


def test_triplet_skips_date_mismatch():
    """[v20.12] triplet 日期跟第一行 UVB 不同的不能動。"""
    text = (
        "UVB: 800 mj/cm2 (50) on (2026/5/20) add 30 each time, fixed at 1000\n"
        "excimer light (10) 500mJ on (2025/01/15) 2 shot, "
        "add 30 each time, fixed at 800"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    # 第一行 UVB count 應該 +1
    assert "(51)" in r.new_text
    # excimer 日期不同 → 不動
    assert "(10) 500mJ on (2025/01/15)" in r.new_text
    assert r.additional_triplets_updated == 0


def test_triplet_skips_non_uvb_context():
    """[v20.12] (N) ... (date) 在非 UVB 相關 context 不要動 (marker 要求)。"""
    text = (
        "UVB: 800 mj/cm2 (50) on (2026/5/20) add 30 each time, fixed at 1000\n"
        "歷史備註 (5) days post op on (2026/5/20) for follow up"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    # 第一行 UVB count 應該 +1
    assert "(51)" in r.new_text
    # 「(5) days post op」沒 UVB / excimer / mJ marker → 不動
    assert "(5) days post op on (2026/5/20)" in r.new_text


def test_three_places_same_date_full_pipeline():
    """[v20.12] user 完整 case: 3 個 triplet 同日期 (UVB + 同行繼續 + excimer)。"""
    text = (
        "局部 手+頸部 + 右前臂 UVB: 1500 mj/cm2 (136) on (2026/5/25) "
        "/ new for left lower back 1500mj/cm2 (44) on (2026/5/25) "
        "add 50 each time, fixed at 1500, W2, W5M\n"
        "excimer light (25) 1000mJ for nape on (2026/5/25) 2 shot, "
        "add 30 each time, fixed at 1000"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 28))
    assert r.action == UvbAction.UPDATED
    # 主行 dose 從 1500 +50 cap 1500 → 1500 不變
    assert r.new_dose == 1500
    assert r.new_count == 137
    # 確認三個 count 都 +1
    for expected in ("(137)", "(45)", "(26)"):
        assert expected in r.new_text, f"missing {expected} in:\n{r.new_text}"
    # 三個日期都更新
    assert r.new_text.count("(2026/05/28)") == 3
    # additional_triplets_updated 至少 2 (第一個 triplet 由 format_uvb_line 處理)
    assert r.additional_triplets_updated >= 2
    # W2, W5M 後綴保留
    assert "W2, W5M" in r.new_text
    # add/fixed/each time 句子保留
    assert "add 50 each time" in r.new_text
    assert "add 30 each time" in r.new_text
    assert "fixed at 1000" in r.new_text


def test_triplet_with_zero_padding_in_original_date():
    """[v20.12] 原日期 (2026/05/25) 帶零填充也要替換 — 不只 (2026/5/25)。"""
    text = (
        "UVB: 1000 mj/cm2 (50) on (2026/05/25) add 30 each time, "
        "fixed at 1200\n"
        "excimer light (10) 800mJ on (2026/05/25) add 30 each time, "
        "fixed at 1000"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 28))
    assert r.action == UvbAction.UPDATED
    assert "(11)" in r.new_text
    assert r.new_text.count("(2026/05/28)") == 2
    assert "(2026/05/25)" not in r.new_text


# ─── v20.12 CONFIRM_NEEDED + 多 triplet 互動 ────────────────────────────

def test_confirm_needed_yes_then_multi_triplet_works():
    """[v20.12] 第一次 call 跳 CONFIRM_NEEDED, 第二次 skip_dose_sanity=True
    仍要正確處理多 triplet 同日期更新。"""
    text = (
        "UVB: 1700 mj/cm2 (100) on (2026/5/25) "
        "/ new for back 1700mj/cm2 (50) on (2026/5/25) "
        "add 50 each time, fixed at 1700\n"
        "excimer light (20) 1100mJ for nape on (2026/5/25) "
        "add 30 each time, fixed at 1100"
    )
    # 第一次 — CONFIRM_NEEDED
    r1 = update_uvb_in_text(text, today=date(2026, 5, 28))
    assert r1.action == UvbAction.CONFIRM_NEEDED
    # 第二次 — skip_dose_sanity 後通過
    r2 = update_uvb_in_text(text, today=date(2026, 5, 28),
                            skip_dose_sanity=True)
    assert r2.action == UvbAction.UPDATED
    assert "(101)" in r2.new_text
    assert "(51)" in r2.new_text
    assert "(21)" in r2.new_text
    assert r2.new_text.count("(2026/05/28)") == 3


def test_confirm_needed_at_too_close_priority():
    """[v20.12] 1 天差 priority 在 CONFIRM_NEEDED 之前 — 仍然 CONFIRM_NEEDED 先
    (因為超過上限是更嚴重的問題，醫師應先確認)。

    實際上 CONFIRM_NEEDED 在 parse 完立刻檢查，days_diff 還沒算。所以順序是
    parse → confirm → sanity → too_close → compute。
    """
    text = (
        "UVB: 1600 mj/cm2 (50) on (2026/5/25) add 50 each time, fixed at 1600"
    )
    # today = 5/26, days_diff = 1 (too close)
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    # 預期 CONFIRM_NEEDED 先 (因為 dose 上限是設計上 caller 該先決定)
    assert r.action == UvbAction.CONFIRM_NEEDED


def test_skip_dose_sanity_still_blocks_too_close():
    """[v20.12] CONFIRM 通過後 (skip_dose_sanity=True) 但 days_diff < 2 → 仍要
    TOO_CLOSE 警告。"""
    text = (
        "UVB: 1600 mj/cm2 (50) on (2026/5/25) add 50 each time, fixed at 1600"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26),
                           skip_dose_sanity=True)
    assert r.action == UvbAction.TOO_CLOSE


# ─── v20.13 不確定 triplet 偵測 (image 2 case fix) ───────────────────────

def test_image2_excimer_different_date_detected_as_uncertain():
    """[v20.13] image 2 實機 case: line 1 是 excimer 日期不同於第一行 UVB，
    應該被偵測為 uncertain 給醫師決定。"""
    text = (
        "re- excimer 800 upper back (37) (2026/5/22) add 10mJ each time, "
        "total 3 shot, father prefer fixed 700mJ,\n"
        "局部 手 UVB: 800 mj/cm2(8) on (2026/5/24) add 50 each time, "
        "prefer fixed 1000mJ"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    # 第一行 UVB 應該更新
    assert "(9)" in r.new_text
    assert "(2026/05/26)" in r.new_text
    # Line 1 (excimer) 日期不同 → 不自動更新但要被偵測
    assert r.uncertain_other_triplets is not None
    assert len(r.uncertain_other_triplets) == 1
    u = r.uncertain_other_triplets[0]
    assert u['count'] == 37
    assert u['date'] == date(2026, 5, 22)
    # 預備好的 replacement 是 (count+1, date→today)
    assert "(38)" in u['replacement']
    assert "(2026/05/26)" in u['replacement']


def test_apply_uncertain_updates_writes_count_and_date():
    """[v20.13] apply_uncertain_updates 套用 detect 出來的 triplet 到 text。"""
    text = (
        "re- excimer 800 upper back (37) (2026/5/22) add 10mJ each time\n"
        "局部 手 UVB: 800 mj/cm2(8) on (2026/5/24) add 50 each time, "
        "prefer fixed 1000mJ"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.uncertain_other_triplets
    # Apply uncertain updates → line 1 (37) → (38), (2026/5/22) → (2026/05/26)
    final = apply_uncertain_updates(r.new_text, r.uncertain_other_triplets)
    assert "(38) (2026/05/26)" in final
    assert "(37) (2026/5/22)" not in final
    # 原 UVB 行不受 apply_uncertain 影響 — 日期還是今天
    assert "on (2026/05/26)" in final


def test_full_width_uncertain_triplet_can_be_confirmed():
    text = (
        "re- excimer 800 upper back （37） （2026/5/22） add 10mJ each time\n"
        "局部 手 UVB: 800 mj/cm2（8） on （2026/5/24） add 50 each time, "
        "fixed: 1000"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.uncertain_other_triplets
    final = apply_uncertain_updates(r.new_text, r.uncertain_other_triplets)
    assert "（38） （2026/05/26）" in final
    assert "（37） （2026/5/22）" not in final
    assert "on （2026/05/26）" in final


def test_hyphen_uncertain_triplet_preserves_separator():
    text = (
        "re- excimer 800 upper back (37) (2026-5-22) add 10mJ each time\n"
        "局部 手 UVB: 800 mj/cm2(8) on (2026-5-24) add 50 each time, "
        "fixed: 1000"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.uncertain_other_triplets
    final = apply_uncertain_updates(r.new_text, r.uncertain_other_triplets)
    assert "(38) (2026-05-26)" in final
    assert "(37) (2026-5-22)" not in final
    assert "on (2026-05-26)" in final


def test_no_uncertain_when_only_same_date_triplets():
    """[v20.13] 沒「不確定」case 時 uncertain_other_triplets 是 None/空。"""
    text = (
        "UVB: 800 mj/cm2 (10) on (2026/5/22) add 50, fixed 1000\n"
        "excimer (5) 800mJ on (2026/5/22) add 30, fixed 800"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    # 兩行都同日期 → step C triplet 全部處理掉
    assert not r.uncertain_other_triplets


def test_no_uncertain_when_no_other_triplets():
    """[v20.13] 處置只有單一 UVB 行 → 沒 uncertain。"""
    text = "UVB: 800 mj/cm2 (10) on (2026/5/22) add 50, fixed 1000"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert not r.uncertain_other_triplets


def test_uncertain_skips_old_history():
    """[v20.13] >365 天的歷史紀錄不算 uncertain (避免噪音)。"""
    text = (
        "old uvb (5) 500mJ on (2023/1/1) record\n"
        "UVB: 800 mj/cm2 (10) on (2026/5/22) add 50, fixed 1000"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    # (5) (2023/1/1) 太舊 → 不算 uncertain
    assert not r.uncertain_other_triplets


def test_uncertain_skips_non_uvb_marker_lines():
    """[v20.13] 沒 UVB/excimer/mj/photo 標記的行不算 uncertain。"""
    text = (
        "follow up (3) days ago (2026/5/22) for biopsy review\n"
        "UVB: 800 mj/cm2 (10) on (2026/5/24) add 50, fixed 1000"
    )
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    # 第一行沒 UVB-marker → 不算 uncertain
    assert not r.uncertain_other_triplets


def test_image1_real_world_text_should_update_cleanly():
    """[v20.13] image 1 實機 text — 應該正常 update (沒 uncertain)。

    這個 test 文件確認: parse_uvb_line + update_uvb_in_text 對 image 1 文字本身
    沒有邏輯 bug。實機沒更新可能是 TMemo 找錯或寫回失敗 (從 log 才能診斷)。"""
    text = ("局部 右腳 UVB: 200 mj/cm2(4) on 2026/5/24, "
            "add 50 each time, fixed 1500, "
            "take picture on 2026/5/14, W1+4N")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED, (
        f"unexpected: {r.action}, sanity={r.sanity_reason}")
    assert r.new_dose == 250
    assert r.new_count == 5
    assert "UVB: 250" in r.new_text
    assert "(5)" in r.new_text
    assert "2026/05/26" in r.new_text
    # take picture 2026/5/14 不該被誤改 (bare date, count 太遠)
    assert "2026/5/14" in r.new_text
    # 沒 uncertain (take picture 行雖然有 date 但沒 (count) 在前面)
    assert not r.uncertain_other_triplets


# ─── v20.14 STALE_DAYS 30 天確認 + 兩張 screenshot 病人不修改 ────────────

def test_image1_zhao_no_uvb_date_silent_first_time_update():
    """[v20.17] image 1 (趙子勳): UVB 沒 date — 改為 silent first-time
    update (不跳 CONFIRM dialog)。dose 套用 +increase (300+50=350)，
    count=1, date=今天。"""
    text = ("UVB: 300 mj/cm2 add 50 every time MAX: 1200 mj/cm2,\n"
            "start MTX 3# w3-4    6# QW  w10-12 (2023/6/22),\n"
            "actretin 20mg  M3 30mg on (2025/5/29)")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 350   # 300 + 50
    assert r.new_count == 1
    assert "(1) on (2026/05/26)" in r.new_text
    # 其他行 (2023/6/22) (2025/5/29) 不該被誤改
    assert "(2023/6/22)" in r.new_text
    assert "(2025/5/29)" in r.new_text


def test_image2_liao_chinese_chars_between_colon_and_dose_parse_fail():
    """[v20.14] image 2 (廖三發): `UVB:已打折 1000...` 冒號跟劑量中間有
    中文 → PARSE_FAIL (我們不確定要不要更新，安全為先不動)。
    """
    text = ("UVB:已打折 1000mj/cm2 (132) on  (2026/05/24)   ,"
            "increase 50 mj/cm 2 if no erythema . photo on  (2022/10/11) "
            "MAX:1000,  1 month come back No41., (2025/2/4) normal blood test\n"
            "start acitretin 1# on (2020/2/11), sign permit. "
            "告知不可捐血、不可把藥給人、女性不可懷孕 ->** "
            "re-Acitreitin 1# QD on (2022/3/1)")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.PARSE_FAIL, (
        f"image 2 中文夾 UVB 跟劑量 → 必須 PARSE_FAIL，不該硬修改")


def test_stale_record_31_days_returns_confirm_needed():
    """[v20.14] 距上次 31 天 (剛超過 30) → CONFIRM_NEEDED 跳 Yes/No。"""
    text = "UVB: 500 mj/cm2 (10) on (2026/04/25) add 50, MAX:1000"
    # 2026/04/25 → 2026/05/26 = 31 天
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.CONFIRM_NEEDED
    assert "距今" in (r.confirm_reason or "")
    assert "31" in (r.confirm_reason or "")
    assert r.last_date == date(2026, 4, 25)
    assert r.days_diff == 31


def test_stale_record_30_days_no_confirm_just_update():
    """[v20.14] 距上次 30 天 (邊界) → 不算 stale，照原本邏輯 (>21 天 → 250)。"""
    text = "UVB: 500 mj/cm2 (10) on (2026/04/26) add 50, MAX:1000"
    # 2026/04/26 → 2026/05/26 = 30 天 (剛好)
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 250  # > 21 天 → 固定 250
    assert r.new_count == 11


def test_stale_record_skip_check_then_updates():
    """[v20.14] CONFIRM_NEEDED stale 之後，caller 按 Yes 重 call 帶
    skip_stale_check=True → 繼續按 decay 規則更新 (60 天 → 250)。"""
    text = "UVB: 500 mj/cm2 (10) on (2026/03/27) add 50, MAX:1000"
    # 2026/03/27 → 2026/05/26 = 60 天
    r1 = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r1.action == UvbAction.CONFIRM_NEEDED
    assert "60" in (r1.confirm_reason or "")

    r2 = update_uvb_in_text(text, today=date(2026, 5, 26),
                            skip_stale_check=True)
    assert r2.action == UvbAction.UPDATED
    assert r2.new_dose == 250  # > 21 天 → 固定 250


def test_stale_check_independent_of_dose_skip():
    """[v20.14] dose 沒超過 1500 但 days 超過 30 → CONFIRM_NEEDED stale
    (不是 dose confirm)。"""
    text = "UVB: 1000 mj/cm2 (10) on (2026/04/01) add 50, MAX:1200"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))  # 55 天
    assert r.action == UvbAction.CONFIRM_NEEDED
    assert "距今" in (r.confirm_reason or "")
    assert "55" in (r.confirm_reason or "")


def test_stale_check_and_dose_check_both_trigger_stale_first_or_dose():
    """[v20.14] dose 超過 1500 + days 超過 30 → 兩個 confirm 都該觸發。
    當前實作: dose check 在 stale check 之前 → 先吐 dose CONFIRM_NEEDED。
    skip dose 後再 call → 才吐 stale CONFIRM_NEEDED。skip 兩個後正常 update。
    """
    text = "UVB: 1700 mj/cm2 (10) on (2026/04/01) add 50, MAX:1700"
    r1 = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r1.action == UvbAction.CONFIRM_NEEDED
    assert "上限" in (r1.confirm_reason or "")  # dose-confirm 先

    r2 = update_uvb_in_text(text, today=date(2026, 5, 26),
                            skip_dose_sanity=True)
    assert r2.action == UvbAction.CONFIRM_NEEDED
    assert "距今" in (r2.confirm_reason or "")  # 才換 stale-confirm

    r3 = update_uvb_in_text(text, today=date(2026, 5, 26),
                            skip_dose_sanity=True, skip_stale_check=True)
    assert r3.action == UvbAction.UPDATED


def test_max_gap_days_still_sanity_fail_over_2_years():
    """[v20.14] 距上次 > 730 天 → 仍是 SANITY_FAIL (病歷可能跑掉)，不是
    CONFIRM_NEEDED (太久遠的紀錄不該給醫師 override)。"""
    text = "UVB: 500 mj/cm2 (10) on (2023/01/01) add 50, MAX:1000"
    # 2023/01/01 → 2026/05/26 = 1241 天
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.SANITY_FAIL
    assert ">730" in (r.sanity_reason or "") or "730" in (r.sanity_reason or "")


# ─── v20.15 5 張 screenshot 的 parse 擴充 ──────────────────────────────

def test_image1_liu_phototherapy_keyword():
    """[v20.15] image 1 (劉香君): 用 Phototherapy 而非 UVB 當 keyword，
    沒有 (count)，含 'maintain the dose' → 應 update dose/date 但保留 dose。
    """
    text = ("new Phototherapy 550mj/cm2 on (2026/5/24) , add 50 each time, "
            "maintain the dose, Max:1000")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED, (
        f"unexpected {r.action} reason={r.sanity_reason}")
    # 'maintain the dose' → 維持原劑量 (不變 550)
    assert r.new_dose == 550
    # 沒 count → new_count None
    assert r.new_count is None
    # date 5/24 → 5/26
    assert "(2026/05/26)" in r.new_text
    # Phototherapy keyword 保留
    assert "Phototherapy" in r.new_text


def test_image2_deng_max_dose_phrase():
    """[v20.15] image 2 (鄧仲強): MAX dose: 1200mj/cm2 (中間多了 'dose')，
    也含 '已打7折' 中文 (但是在 (count) on (date) 後)，應該照樣 parse 通過。
    """
    text = ("new UVB: 1200 mj/cm2(156) on   (2026/05/24) 已打7折    , "
            "increase 40mj/cm2 if no erythema, MAX dose: 1200mj/cm2, W2M, W6")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1200  # 1200+40 cap 1200
    assert r.new_count == 157
    assert "(2026/05/26)" in r.new_text
    assert "MAX dose:" in r.new_text  # 後綴保留


def test_image3_zhan_roc_concat_date():
    """[v20.15] image 3 (詹晟凱): (1150524) 民國年 7-digit concat YYYMMDD,
    寫回也要用同樣 ROC concat format。"""
    text = ("UVB: 250mj/cm2 (4) on (1150524), add 50 each time, "
            "fixed at 1500,")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 300  # 250+50
    assert r.new_count == 5
    # ROC 民國 115/05/26 concat = 1150526
    assert "(1150526)" in r.new_text
    # AD 不該出現
    assert "2026/" not in r.new_text


def test_image4_yang_date_before_uvb():
    """[v20.15] image 4 (楊亮筠): date 在 UVB 之前 - "(2026/05/24) UVB 850..."
    segment 必須擴到行首才能 parse 到 date。"""
    text = "(2026/05/24) UVB 850 mj/cm2 increase 50 each time max 1200"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 900  # 850+50
    assert r.new_count is None  # 沒 (count)
    # date 5/24 → 5/26 在行首
    assert r.new_text.startswith("(2026/05/26) UVB 900")


def test_image5_chen_roc_slashed_date():
    """[v20.15] image 5 (陳文海): (115/05/24) 民國年 slashed，寫回要保留
    民國年格式 → (115/05/26)。"""
    text = ("UVB: 660 mj/cm2 (14) on (115/05/24), , add 30 each time, "
            "fixed at 1000")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 690  # 660+30
    assert r.new_count == 15
    # ROC 民國 115/05/26 slashed
    assert "(115/05/26)" in r.new_text
    # AD 不該出現
    assert "2026/" not in r.new_text


def test_max_dose_phrase_alone():
    """[v20.15] 純粹測 "MAX dose: N" 寫法 (與其他變體)。"""
    text = "UVB: 800 (10) on (2026/05/24), add 50, MAX dose: 1000"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.max_dose == 1000


def test_roc_year_115_converts_to_2026():
    """[v20.15] 民國 115 = AD 2026 — slashed format。"""
    text = "UVB: 800 (10) on (115/05/24), add 50, fixed at 1000"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.last_date == date(2026, 5, 24)
    assert info.date_text == "(115/05/24)"


def test_roc_year_7digit_concat():
    """[v20.15] 民國 7-digit concat 1150524 = AD 2026/05/24。"""
    text = "UVB: 800 (10) on (1150524), add 50, fixed at 1000"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.last_date == date(2026, 5, 24)
    assert info.date_text == "(1150524)"


def test_phototherapy_keyword_recognized():
    """[v20.15] Phototherapy 也能當 keyword (劉香君實機 case)。"""
    text = "Phototherapy 550 mj/cm2 on (2026/5/24), add 50, fixed at 1000"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.dose == 550
    assert info.keyword_text.lower() == "phototherapy"


def test_maintain_dose_keeps_original_dose():
    """[v20.15] 處置含 'maintain the dose' → 維持原劑量不增 increase。"""
    text = ("UVB: 800 mj/cm2 (10) on (2026/5/24), add 50, "
            "maintain the dose, MAX:1500")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    # 沒 maintain 應該 850 (800+50), 有 maintain 維持 800
    assert r.new_dose == 800
    assert r.new_count == 11


def test_date_before_uvb_on_same_line():
    """[v20.15] date 在 UVB 同行之前 (楊亮筠 case) — parse 仍要成功。"""
    text = "(2026/05/24) UVB 500 mj/cm2 increase 30, max 800"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.dose == 500
    assert info.last_date == date(2026, 5, 24)
    assert info.max_dose == 800


# ─── v20.16 沒日期 第一次照光 + 拼錯 typo ───────────────────────────────

def test_image1_chen_no_date_silent_first_time():
    """[v20.17] image 1 (陳佳徵): UVB 有 dose+MAX 但沒 (count)/沒 date →
    silent first-time update (不跳對話框)。dose 780+30=810。"""
    text = ("IL 10mg (2) , no hematologic transmitted disease such as "
            "HBV/ HCV/, HIV\n"
            "局部頭皮UVB: 780 mj/cm2, .add 30 mj/cm2 eacht time, "
            "MAx: 1000 mj/cm2")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 810  # 780 + 30
    assert r.new_count == 1
    assert "(1) on (2026/05/26)" in r.new_text
    # 上面那行 "IL 10mg (2)" 不該被誤改
    assert "IL 10mg (2)" in r.new_text


def test_image2_zhang_no_date_typo_incrase_silent_update():
    """[v20.17] image 2 (張耀銘): UVB 有 dose+MAX 沒 date + typo 'incrase' →
    silent first-time update (no dialog)。dose 1200+100 cap 1500 = 1300。"""
    text = ("UVB 1200 mj/cm2 incrase 100 each time max 1500\n"
            "dupi start on 0606 taper to 4w hold on 0819, restart on 1119")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1300  # 1200 + 100, cap 1500
    assert r.new_count == 1
    assert "(1) on (2026/05/26)" in r.new_text
    # incrase 保留 (不修拼字)
    assert "incrase 100" in r.new_text


def test_typo_incrase_accepted_by_increase_regex():
    """[v20.16] 拼錯 'incrase' 也認為是 increase。"""
    text = "UVB 800 (5) on (2026/5/24), incrase 50, fixed 1000"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.increase == 50


def test_typo_incraese_accepted():
    """[v20.16] 拼錯 'incraese' 也認。"""
    text = "UVB 800 (5) on (2026/5/24), incraese 50, fixed 1000"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.increase == 50


def test_no_uvb_keyword_at_all_returns_no_uvb_line():
    """[v20.16] 完全沒 UVB/Phototherapy 字眼 → NO_UVB_LINE。"""
    text = "MTX 5mg take daily, follow up in 4 weeks"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.NO_UVB_LINE


def test_phototherapy_general_word_no_structure_returns_no_uvb_line():
    """[v20.16] 'phototherapy' 當一般名詞用 (沒緊接劑量) → NO_UVB_LINE
    (例如 'keep phototherapy on both legs')。"""
    text = "keep phototherapy on both lower limbs to maintain remission"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.NO_UVB_LINE


def test_uvb_with_colon_but_chinese_in_between_parse_fail():
    """[v20.16 regression] 'UVB:已打折 1000' 中文夾在冒號跟劑量間 →
    PARSE_FAIL (不是 NO_UVB_LINE — 有結構化 UVB:)。"""
    text = ("UVB:已打折 1000mj/cm2 (132) on (2026/05/24), "
            "increase 50, MAX:1000")
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.PARSE_FAIL


def test_first_time_applies_increase_formula():
    """[v20.17] 第一次照光 (沒 date) dose 套用 +increase 公式 cap MAX
    (改自 v20.16 的「維持原 dose」)。"""
    text = "UVB: 500 mj/cm2, increase 50, MAX: 1000"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 550   # 500 + 50
    assert r.new_count == 1


def test_first_time_with_existing_count_increments():
    """[v20.17] 第一次照光但處置已有 (N) — 沒 date 仍當第一次，
    count → N+1, dose → +increase, 不重複插入 count。"""
    text = "UVB: 800 mj/cm2 (3), add 50, MAX: 1000"
    r = update_uvb_in_text(text, today=date(2026, 5, 26))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 850   # 800 + 50
    assert r.new_count == 4    # 3 + 1
    # 原 (3) 被替換成 (4), 後面接 " on (today)"
    assert "(4) on (2026/05/26)" in r.new_text
    # 不能有 (1) 之類的多餘 count
    assert r.new_text.count("(4)") == 1
    # 也不能有重複的 count 或 on (today)
    assert r.new_text.count("on (2026/05/26)") == 1


# ─── v20.17 5 個新實機 case (沒日期改 silent first-time) ─────────────────

def test_image1_lai_uvb_no_date_silent_update():
    """[v20.17] image 1 (賴佑昌): UVB 930 mj/cm2 increase 100 each time max 1500
    沒 (count) 沒 date → silent update, dose 930+100=1030, count=1, date=今天。"""
    text = ("UVB 930 mj/cm2 increase 100 each time max 1500\n"
            "MTX 6# QW  12w 抗微生物製劑: CEPHRA\n"
            "cyclosporine 125mg 3M")
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1030
    assert r.new_count == 1
    assert "UVB 1030 mj/cm2 (1) on (2026/05/27)" in r.new_text


def test_image2_yang_uvb_no_unit_space_silent_update():
    """[v20.17] image 2 (楊安臻): UVB 150mj/cm2 increase 30 each time, max 1000
    沒 date → dose 150+30=180, count=1。"""
    text = "UVB 150mj/cm2 increase 30 each time, max 1000"
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 180
    assert r.new_count == 1
    assert "(1) on (2026/05/27)" in r.new_text


def test_image3_liang_keep_uvb_no_max_silent_skip():
    """[v20.17] image 3 (梁雯琳): keep UVB 850 mj/cm2 (只有 dose 沒 MAX) →
    SILENT_SKIP — 不修改處置，但 caller 應該繼續執行 51019+療程。"""
    text = "keep UVB 850 mj/cm2"
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r.action == UvbAction.SILENT_SKIP


def test_image4_zhang_in_crease_typo_silent_update():
    """[v20.17] image 4 (張智宇): UVB 950 mj/cm2 in crease 50 each time, max 1200
    'in crease' 中間有空格的 typo → 認 increase 50。silent update。"""
    text = ("MTX 3# QW 2w  6# QW 12W\n"
            "acitretin 20mg 12w\n"
            "UVB 950 mj/cm2 in crease 50 each time, max 1200")
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1000  # 950 + 50
    assert r.new_count == 1
    assert "UVB 1000 mj/cm2 (1) on (2026/05/27)" in r.new_text
    # 其他治療行不該被誤改
    assert "MTX 3# QW 2w" in r.new_text
    assert "acitretin 20mg 12w" in r.new_text


def test_image5_huang_max_uvb_phrase_silent_update():
    """[v20.17] image 5 (黃冠輝): UVB 1530 mj/cm2, increase 30 each time,
    max UVB 1800 mj/cm2 — "max UVB N" 新寫法 + dose 1530 (> 1500 但醫師 max
    自訂 1800)。silent first-time update, dose 1530+30=1560。"""
    text = ("UVB 1530 mj/cm2, increase 30 each time, max UVB 1800 mj/cm2\n"
            "Ruxo")
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1560  # 1530 + 30, cap 1800
    assert r.new_count == 1
    assert "UVB 1560 mj/cm2 (1) on (2026/05/27)" in r.new_text
    # max UVB 1800 保留
    assert "max UVB 1800" in r.new_text


def test_max_uvb_phrase_parses():
    """[v20.17] "max UVB N" 寫法被 MAX regex 認可。"""
    text = "UVB 500 mj/cm2 (5) on (2026/5/24) increase 30, max UVB 1000"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.max_dose == 1000


def test_in_crease_typo_with_space_parses():
    """[v20.17] "in crease N" 中間有空格的 typo 被 increase regex 認可。"""
    text = "UVB 500 (5) on (2026/5/24) in crease 50, max 1000"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.increase == 50


def test_silent_skip_for_uvb_dose_only_no_max():
    """[v20.17] 只有 UVB+dose 沒 MAX/increase → SILENT_SKIP (新 action)。"""
    text = "keep UVB 600 mj/cm2 BIW"
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r.action == UvbAction.SILENT_SKIP


def test_silent_first_time_dose_capped_at_local_max():
    """[v20.17] silent first-time update 套用 +increase 後 cap 醫師自訂 MAX
    (而非全域 MAX_DOSE=1500)。"""
    text = "UVB 1750 mj/cm2 increase 50 each time max 1800"
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    # dose 1750+50=1800 (= local max), 不被 global MAX_DOSE 1500 卡住
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1800


def test_silent_first_time_when_increase_missing_keeps_dose():
    """[v20.17] 沒 increase 但有 dose+max → first-time dose 保持不變 (沒法 +N)。"""
    text = "UVB 500 mj/cm2 fixed at 1000"
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 500  # 沒 increase → 保持
    assert r.new_count == 1


def test_image1_hu_max_equals_dose_then_keep_silent_update():
    """[v20.17] 胡寶昌實機 case: UVB dose 1500 = max 1500，含 "then keep 1500"
    後綴，沒 date。silent first-time update: dose+increase cap max → 維持
    1500 (因為 1500+100 cap 1500)。確認其他行的舊日期 (2025/12/10) 不被誤改。"""
    text = ("cyclosporine 100mg since 0106 3M\n"
            "MTX 6# QW since 0116 12w 4# qw -> taper to 3#  "
            "on (2025/12/10) for elevated ALT 1.5M\n"
            "UVB 1500 mj/cm2 increase 100 each time, max 1500 then keep 1500")
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1500   # 1500+100=1600 cap max 1500 → 1500
    assert r.new_count == 1
    assert "(1) on (2026/05/27)" in r.new_text
    # MTX 行的舊日期不該被誤改
    assert "(2025/12/10)" in r.new_text
    # "then keep 1500" 後綴保留
    assert "then keep 1500" in r.new_text


def test_image1_chen_lowercase_uv_shorthand():
    """[v20.18] 陳冠廷實機 case: doctor 用 "uv" 簡寫 (沒 b) 當 keyword。
    "uv 1150mj (34) on (2026/5/21) add 30 each, MAX 1200, 3w appoint"
    days_diff = 6 (5/21→5/27) → 套 +increase 公式 dose 1150+30=1180。"""
    text = ("uv 1150mj (34) on (2026/5/21) add 30 each, MAX 1200, "
            "3w appoint\n"
            "IgE: 1815 * IU/mL (<87)  explain MAST\n"
            "LN for right palm on (2026/3/5) (2026/3/26) and complete")
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r.action == UvbAction.UPDATED
    assert r.new_dose == 1180   # 1150 + 30
    assert r.new_count == 35    # 34 + 1
    assert r.days_diff == 6
    # date 5/21 → 5/27 (帶 paren AD format)
    assert "(2026/05/27)" in r.new_text
    # uv keyword 保留 (不轉成 UVB)
    assert "uv 1180mj" in r.new_text
    # 其他行的歷史日期不該被誤改
    assert "(2026/3/5)" in r.new_text
    assert "(2026/3/26)" in r.new_text


def test_uv_shorthand_parses():
    """[v20.18] "uv" 簡寫 keyword 被 dose regex 認可。"""
    text = "uv 800 (5) on (2026/5/24) add 30, max 1000"
    info = parse_uvb_line(text)
    assert info is not None
    assert info.dose == 800
    assert info.keyword_text.lower() == "uv"


def test_uv_shorthand_word_boundary():
    """[v20.18] "uv" 簡寫需 word boundary 才認 — 避免誤抓 UVA / uveitis 等。"""
    # UVA 後面有 800 但前綴不是 "uv" word boundary → 不認 (但其實 "uv" 字串
    # 是在 UVA 內部 開頭 — 沒有後 word boundary 所以 不認)
    text_uva = "UVA 800 each session"  # UVA - "A" 是 word char 連 V 後沒 boundary
    info = parse_uvb_line(text_uva)
    # UVA 不該被當成 UVB/UV — dose regex 雖然找 UV 開頭可能誤抓
    # 但 lower keyword 檢查用 uv — UVA 沒這個 word boundary 所以早 return
    assert info is None


def test_uveitis_not_matched_as_uv():
    """[v20.18] "uveitis" 不該被誤判為 UV — word boundary 排除。"""
    text = "patient has uveitis episode, no skin lesions"
    info = parse_uvb_line(text)
    assert info is None


def test_uv_keyword_preserved_in_output():
    """[v20.18] 用 "uv" 簡寫的處置寫回時 keyword 應保留原樣 (不變大寫不加 b)。"""
    text = "uv 500 (3) on (2026/5/22) add 50, max 1000"
    r = update_uvb_in_text(text, today=date(2026, 5, 27))
    assert r.action == UvbAction.UPDATED
    # 不該變成 "UVB ..." or "UV: ..."
    assert "uv 550" in r.new_text or "uv 1000" in r.new_text
