# -*- coding: utf-8 -*-
"""roster 求解器：規則/CP-SAT/放寬階梯/報告（設計文件 §12 測試清單）。

需要 ortools（重依賴）；未安裝環境整檔 skip（CI 若未裝只跳過本檔，
核心邏輯測試 test_roster_core.py 不受影響）。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

pytest.importorskip("ortools", reason="ortools 未安裝（自動排班引擎）")

from cmuh_common.roster.model import (  # noqa: E402
    Member, SolveContext, week_key,
)
from cmuh_common.roster.report import build_report  # noqa: E402
from cmuh_common.roster.solve_rvs import (  # noqa: E402
    apply_boundary_from_prev, solve_duty,
)

# 2026/08: 8/1=週六,31 天,週六 1/8/15/22/29;週二 4/11/18/25;週三 5/12/19/26;週四 6/13/20/27
R1, R2, R3 = "r1", "r2", "r3"


def r_members():
    return [Member(R1, "甲", "R1", fixed_weekday=2),   # 週三
            Member(R2, "乙", "R2", fixed_weekday=3),   # 週四
            Member(R3, "丙", "R3", fixed_weekday=1)]   # 週二


def aug_colors(alternate=True):
    """8 月各週末週色。alternate=True 交替(允許連值);False 全同色(禁連值)。"""
    sats = [date(2026, 8, d) for d in (1, 8, 15, 22, 29)]
    if alternate:
        return {week_key(s): ("pink" if i % 2 == 0 else "green")
                for i, s in enumerate(sats)}
    return {week_key(s): "pink" for s in sats}


def make_ctx(scope="r", year=2026, month=8, members=None, holidays=None,
             leaves=None, must=None, annual=None, locks=None, ledger=None,
             colors=None, prev=None):
    ctx = SolveContext(
        scope=scope, year=year, month=month,
        members=members if members is not None else r_members(),
        holidays=holidays or set(),
        leaves={k: set(v) for k, v in (leaves or {}).items()},
        must_duty={k: set(v) for k, v in (must or {}).items()},
        annual_holiday=dict(annual or {}),
        locks=dict(locks or {}),
        ledger=dict(ledger or {}),
        week_colors=dict(colors if colors is not None else aug_colors()),
        prev_last_weekend=prev,
    )
    return ctx.prepare()


# ─── 基本求解 ─────────────────────────────────────────────────────────────
def test_basic_ok_all_rules():
    ctx = make_ctx()
    r = solve_duty(ctx)
    assert r.status == "ok" and r.level_used == 0
    assert len(r.assignments) == 31                      # 每天都有人
    # 固定週幾
    for d in (4, 11, 18, 25):
        assert r.assignments[date(2026, 8, d)] == R3     # 週二
    for d in (5, 12, 19, 26):
        assert r.assignments[date(2026, 8, d)] == R1     # 週三
    for d in (6, 13, 20, 27):
        assert r.assignments[date(2026, 8, d)] == R2     # 週四
    # 假日成對
    for sat in (1, 8, 15, 22, 29):
        assert (r.assignments[date(2026, 8, sat)]
                == r.assignments[date(2026, 8, sat + 1)])
    # L0 → 班數 9-11
    assert all(9 <= n <= 11 for n in r.duty_counts.values())
    # 點數守恆
    assert sum(r.points_by_person.values()) == ctx.total_points()


def test_determinism_same_input_same_output():
    a = solve_duty(make_ctx()).assignments
    b = solve_duty(make_ctx()).assignments
    assert a == b


# ─── 請假 / 固定週幾代班 ─────────────────────────────────────────────────
def test_leave_excluded_and_fixed_weekday_substitute():
    leaves = {R1: [date(2026, 8, 5), date(2026, 8, 8), date(2026, 8, 9)]}
    ctx = make_ctx(leaves=leaves)
    r = solve_duty(ctx)
    assert r.status == "ok"
    for d in leaves[R1]:
        assert r.assignments[d] != R1                    # 請假日絕不排
    assert r.assignments[date(2026, 8, 5)] in (R2, R3)   # 固定週三由他人代
    assert any(c.severity == "info" and "固定值班日但已請假" in c.msg
               for c in r.prechecks)


# ─── 指定值班 ─────────────────────────────────────────────────────────────
def test_must_saturday_auto_pairs_sunday():
    ctx = make_ctx(must={R3: [date(2026, 8, 8)]})
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert r.assignments[date(2026, 8, 8)] == R3
    assert r.assignments[date(2026, 8, 9)] == R3         # 週日自動同人
    assert r.reasons[date(2026, 8, 8)] == "指定"


def test_two_must_same_weekend_is_conflict():
    ctx = make_ctx(must={R1: [date(2026, 8, 8)], R2: [date(2026, 8, 9)]})
    r = solve_duty(ctx)
    assert r.status == "precheck_failed"
    assert any(c.severity == "error" and "被指定給多人" in c.msg
               for c in r.prechecks)


def test_directive_on_leave_day_is_conflict():
    ctx = make_ctx(leaves={R1: [date(2026, 8, 14)]},
                   must={R1: [date(2026, 8, 14)]})
    r = solve_duty(ctx)
    assert r.status == "precheck_failed"


# ─── 年度假日指定 + 三連休 ────────────────────────────────────────────────
def test_annual_holiday_three_day_block_same_person():
    # 2026/9/28(一)=假日,年度表指定 r2 → 9/26,27,28 三天都 r2,週一算 1 點
    sats = [date(2026, 9, d) for d in (5, 12, 19, 26)]
    colors = {week_key(s): ("pink" if i % 2 == 0 else "green")
              for i, s in enumerate(sats)}
    ctx = make_ctx(month=9, holidays={date(2026, 9, 28)},
                   annual={date(2026, 9, 28): R2}, colors=colors)
    r = solve_duty(ctx)
    assert r.status == "ok"
    for d in (26, 27, 28):
        assert r.assignments[date(2026, 9, d)] == R2
    # 總點數: 21 平日 + 8 週末日×2 + 假日1 = 38
    assert ctx.total_points() == 38
    assert sum(r.points_by_person.values()) == 38


# ─── 色塊連週 ─────────────────────────────────────────────────────────────
def test_same_color_forbids_consecutive_weekends():
    ctx = make_ctx(colors=aug_colors(alternate=False))   # 全同色
    r = solve_duty(ctx)
    assert r.status == "ok"
    sats = [date(2026, 8, d) for d in (1, 8, 15, 22, 29)]
    for a, b in zip(sats, sats[1:], strict=False):       # 相鄰配對,長度刻意差一
        assert r.assignments[a] != r.assignments[b]      # 禁止連週


def test_prev_month_same_color_blocks_first_weekend():
    prev_sat = date(2026, 7, 25)
    colors = aug_colors()                                 # 8月交替
    colors[week_key(prev_sat)] = colors[week_key(date(2026, 8, 1))]  # 與 8/1 同色
    ctx = make_ctx(colors=colors, prev=(prev_sat, R1))
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert r.assignments[date(2026, 8, 1)] != R1          # 上月人選被擋


def test_missing_colors_conservative_and_warn():
    ctx = make_ctx(colors={})                             # 全部未設定
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert any("色塊未設定" in c.msg for c in r.prechecks)
    sats = [date(2026, 8, d) for d in (1, 8, 15, 22, 29)]
    for a, b in zip(sats, sats[1:], strict=False):        # 保守=視為同色禁連(相鄰配對)
        assert r.assignments[a] != r.assignments[b]


# ─── 跨月孤兒週日銜接 ─────────────────────────────────────────────────────
def test_orphan_sunday_boundary_fix_and_color_pair():
    # 2026/11/1=週日;上月最後週六 10/31 由 r2 值 → 11/1 固定 r2
    sats = [date(2026, 11, d) for d in (7, 14, 21, 28)]
    colors = {week_key(s): ("green" if i % 2 == 0 else "pink")
              for i, s in enumerate(sats)}
    colors[week_key(date(2026, 10, 31))] = "green"        # 與 11/7 同色
    ctx = make_ctx(month=11, colors=colors, prev=(date(2026, 10, 31), R2))
    apply_boundary_from_prev(ctx)
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert r.assignments[date(2026, 11, 1)] == R2         # 跨月銜接
    assert r.reasons[date(2026, 11, 1)] == "跨月銜接"
    assert r.assignments[date(2026, 11, 7)] != R2         # 同色連週被擋


def test_month_start_monday_holiday_chains_to_prev_weekend():
    """[codex P2] 月初=週一國定假日,上月末=六日 → 跨月三連休,週一固定給上月人選。
    2026/6/1=週一(假日);上月週末 5/30(六)+5/31(日)。"""
    sats = [date(2026, 6, d) for d in (6, 13, 20, 27)]
    colors = {week_key(s): ("pink" if i % 2 == 0 else "green")
              for i, s in enumerate(sats)}
    colors[week_key(date(2026, 5, 30))] = "green"
    ctx = make_ctx(month=6, holidays={date(2026, 6, 1)}, colors=colors,
                   prev=(date(2026, 5, 30), R3))
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert r.assignments[date(2026, 6, 1)] == R3       # 三連休跨月同一人
    assert r.reasons[date(2026, 6, 1)] == "跨月銜接"


def test_orphan_boundary_applied_automatically_by_solver():
    """[codex P2] 呼叫端只設 prev_last_weekend、未呼叫 helper → solve_duty
    內部自動套用跨月銜接,孤兒週日仍固定給上月人選。"""
    sats = [date(2026, 11, d) for d in (7, 14, 21, 28)]
    colors = {week_key(s): ("green" if i % 2 == 0 else "pink")
              for i, s in enumerate(sats)}
    colors[week_key(date(2026, 10, 31))] = "pink"
    ctx = make_ctx(month=11, colors=colors, prev=(date(2026, 10, 31), R2))
    r = solve_duty(ctx)          # 不手動呼叫 apply_boundary_from_prev
    assert r.status == "ok"
    assert r.assignments[date(2026, 11, 1)] == R2


# ─── 鎖定格 ───────────────────────────────────────────────────────────────
def test_locked_cell_respected():
    ctx = make_ctx(locks={date(2026, 8, 14): R3})         # 週五鎖 r3
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert r.assignments[date(2026, 8, 14)] == R3
    assert r.reasons[date(2026, 8, 14)] == "鎖定"


# ─── 放寬階梯 ─────────────────────────────────────────────────────────────
def test_range_auto_relax_L1_when_heavy_leave():
    # r1 只有 4 個週三能值 → 9-11 硬範圍必無解 → 自動 L1
    all_days = [date(2026, 8, d) for d in range(1, 32)]
    avail = {date(2026, 8, d) for d in (5, 12, 19, 26)}
    ctx = make_ctx(leaves={R1: [d for d in all_days if d not in avail]})
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert r.level_used >= 1
    assert r.duty_counts[R1] == 4                          # 只值他能值的
    assert "放寬" in r.level_name


def test_need_confirm_color_then_L3():
    # VS 2 人;J 全部週末請假 → D 須連值全部週末;全同色 → 需確認停用色塊
    vs = [Member("D", "吳"), Member("J", "張廖")]
    weekend_days = [date(2026, 8, s + off) for s in (1, 8, 15, 22, 29)
                    for off in (0, 1)]
    ctx = make_ctx(scope="vs", members=vs,
                   colors=aug_colors(alternate=False),
                   leaves={"J": weekend_days})
    r = solve_duty(ctx)
    assert r.status == "need_confirm_color"
    # 使用者按「是」→ 停用色塊重解
    ctx2 = make_ctx(scope="vs", members=vs,
                    colors=aug_colors(alternate=False),
                    leaves={"J": weekend_days})
    r2 = solve_duty(ctx2, allow_disable_color=True)
    assert r2.status == "ok" and r2.level_used == 3
    for d in weekend_days:
        assert r2.assignments[d] == "D"


def test_all_on_leave_day_precheck_failed():
    ctx = make_ctx(leaves={R1: [date(2026, 8, 14)],
                           R2: [date(2026, 8, 14)],
                           R3: [date(2026, 8, 14)]})
    r = solve_duty(ctx)
    assert r.status == "precheck_failed"
    assert any("無人可值" in c.msg for c in r.prechecks)


# ─── 退化與 VS 特性 ───────────────────────────────────────────────────────
def test_single_member_degenerate():
    ctx = make_ctx(members=[Member(R1, "甲", "R1", fixed_weekday=2)],
                   colors={})
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert set(r.assignments.values()) == {R1}
    assert any("自動停用" in c.msg for c in r.prechecks)


def test_vs_no_fixed_weekday_no_range():
    vs = [Member(x, x) for x in ("D", "J", "R", "S", "L", "T")]
    ctx = make_ctx(scope="vs", members=vs)
    r = solve_duty(ctx)
    assert r.status == "ok" and r.level_used == 0
    # 6 人 31 天 → 每人 4-7 班,絕不受 9-11 限制
    assert sum(r.duty_counts.values()) == 31
    assert max(r.duty_counts.values()) <= 8


# ─── 帳本目標與報告 ───────────────────────────────────────────────────────
def test_count_balance_secondary_keeps_points_priority():
    """次要班數平衡：點數仍平衡（優先），班數全距壓到最小（同分決勝）。"""
    from cmuh_common.roster.rules import RULE_REGISTRY
    assert any(getattr(c, "rule_id", "") == "count_balance" for c in RULE_REGISTRY)
    r = solve_duty(make_ctx())                       # 3 R, 2026-08
    assert r.status == "ok"
    pts = list(r.points_by_person.values())
    assert max(pts) - min(pts) <= 1                  # 點數平衡（優先）不被犧牲
    counts = list(r.duty_counts.values())
    assert max(counts) - min(counts) <= 2            # 班數全距最小化


def test_ledger_carryover_shifts_target():
    # r1 上月多值 3 點 → 目標調低;點數應低於其他人
    r = solve_duty(make_ctx(ledger={R1: 3.0}))
    assert r.status == "ok"
    assert r.points_by_person[R1] <= min(
        r.points_by_person[R2], r.points_by_person[R3])


def test_report_sections_and_content():
    ctx = make_ctx()
    r = solve_duty(ctx)
    text = build_report(ctx, r, "R 排班")
    for section in ("[輸入]", "[預檢]", "[過程]", "[結算]", "[警告]",
                    "2026/08 R 排班決策報告", "最後週末"):
        assert section in text
    # last_weekend 供下月使用
    assert r.last_weekend and r.last_weekend["saturday"] == "2026-08-29"


# ─── RS-02：平日國定假日算「假日班」（weekend_counts 三處一致） ──────────────
def test_rs02_weekend_counts_includes_weekday_holiday():
    """[RS-02] 平日國定假日算假日班：weekend_counts 需與 is_weekend|holiday 重算一致
    （＝export_common.member_tally 的 we 欄語意）。修正前 9/28（週一）假日會被漏算進
    平日欄，與月曆/點數/匯出三處矛盾。"""
    from cmuh_common.roster.model import is_weekend
    sats = [date(2026, 9, d) for d in (5, 12, 19, 26)]
    colors = {week_key(s): ("pink" if i % 2 == 0 else "green")
              for i, s in enumerate(sats)}
    ctx = make_ctx(month=9, holidays={date(2026, 9, 28)},
                   annual={date(2026, 9, 28): R2}, colors=colors)
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert not is_weekend(date(2026, 9, 28))          # 確為平日假日
    assert date(2026, 9, 28) in r.assignments          # 有指派 → 測得到
    for mid in ctx.member_ids():
        days_m = [d for d, p in r.assignments.items() if p == mid]
        expected_we = sum(1 for d in days_m if is_weekend(d) or d in ctx.holidays)
        assert r.weekend_counts[mid] == expected_we
        assert r.weekday_counts[mid] == r.duty_counts[mid] - r.weekend_counts[mid]
    holder = r.assignments[date(2026, 9, 28)]           # 指派到平日假日者
    assert r.weekend_counts[holder] >= 1                # 該天計入假日欄（修正前會少）
