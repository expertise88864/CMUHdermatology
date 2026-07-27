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


def test_two_must_same_weekend_splits_block_per_directives():
    """[2026-07-27 使用者] 同一連休段被指定給不同人 → 【不再中止求解】，
    改依指定拆段照排（使用者的手動指定就是要蓋過「連休段同一人」原則），
    僅留警告。舊行為 precheck_failed 會讓人「怎麼指定都排不出來」。"""
    ctx = make_ctx(must={R1: [date(2026, 8, 8)], R2: [date(2026, 8, 9)]})
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert r.assignments[date(2026, 8, 8)] == R1
    assert r.assignments[date(2026, 8, 9)] == R2
    assert any(c.severity == "warn" and "被指定給多人" in c.msg
               for c in r.prechecks)
    assert not any(c.severity == "error" for c in r.prechecks)


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


# ─── 2026-07-25 使用者規則：每人每月最多 2 個週末 ─────────────────────────────
def _weekend_counts(ctx, r):
    """每人「含週六的週末段」數（週六+週日同段算一個）。"""
    out = {m.id: 0 for m in ctx.members}
    for b in ctx.blocks:
        if b.saturday is None:
            continue
        who = r.assignments.get(b.days[0])
        if who:
            out[who] = out.get(who, 0) + 1
    return out


def test_weekend_cap_two_per_person():
    """[使用者] 3 人值班 → 一個月不該有人值到 3 個週末（8 月有 5 個週末 = 2+2+1）。"""
    ctx = make_ctx()
    r = solve_duty(ctx)
    assert r.status == "ok"
    wk = _weekend_counts(ctx, r)
    assert sum(wk.values()) == 5, f"8 月應有 5 個週末段：{wk}"
    assert max(wk.values()) <= 2, f"不得有人值超過 2 個週末：{wk}"


def test_weekend_cap_holds_even_when_ledger_pushes_points():
    """帳本讓某人需要補很多點數（週末點數最高）時,仍不得靠塞第 3 個週末達成。"""
    ctx = make_ctx(ledger={R1: -8.0})          # r1 欠 8 點 → 目標點數拉高
    r = solve_duty(ctx)
    assert r.status == "ok"
    wk = _weekend_counts(ctx, r)
    assert wk[R1] <= 2, f"r1 縱使需補點數也不得值 3 個週末：{wk}"


def test_weekend_cap_precheck_warns_when_arithmetic_impossible():
    """人數不足以讓每人 ≤2 個週末（5 週末 ÷ 2 人）→ 預檢提示,但仍求解不擋。"""
    two = [Member(R1, "甲", "R1"), Member(R2, "乙", "R2")]
    ctx = make_ctx(members=two)
    r = solve_duty(ctx)
    assert r.status == "ok"
    assert any(c.rule_id == "weekend_cap" and c.severity == "warn"
               for c in r.prechecks), \
        f"應提示週末數與人數在算術上不相容：{[c.msg for c in r.prechecks]}"


def test_weekend_cap_ignores_orphan_sunday_block():
    """月初孤兒週日（其週六在上月）屬上月那個週末 → 不計入本月週末上限。
    2026/11/1 為週日 → 產生孤兒塊。"""
    ctx = make_ctx(year=2026, month=11, colors={})
    blocks = [b for b in ctx.blocks if b.saturday is None]
    assert blocks and blocks[0].kind == "weekend_orphan", "11 月應有孤兒週日塊"
    from cmuh_common.roster.rules import WeekendCapRule
    counted = WeekendCapRule._weekend_blocks(ctx)
    assert all(b.saturday is not None for b in counted)
    assert len(counted) == len(ctx.blocks) - 1


def test_weekend_cap_weight_dominates_point_transfer():
    """[codex R1] 權重必須嚴格大於「把一個週末段換人」造成的點數目標變動。
    搬動 P 點的週末段會【同時】改變兩人的絕對偏差（最壞各 P 點）→ 上界 2×P 點;
    第一版寫死 4,000,000 只算了單邊 4 點,長連休段(含國定假日)更會超出。"""
    from cmuh_common.roster.rules import POINT_WEIGHT, WeekendCapRule
    ctx = make_ctx()
    blocks = WeekendCapRule._weekend_blocks(ctx)
    w = WeekendCapRule.over_weight(ctx, blocks)
    max_pts = max(b.points(ctx.holidays, ctx.params) for b in blocks)
    assert w > 2 * max_pts * 100 * POINT_WEIGHT, "須嚴格支配雙人點數偏差上界"
    assert max_pts >= 4, "8 月週末段至少 週六2+週日2 = 4 點"


def test_weekend_cap_holds_without_fixed_weekday_confounders():
    """隔離情境（無固定週幾、色塊交替不設限）+ 極端帳本壓力：點數平衡單獨會想把
    第 3 個週末塞給欠點數最多的人,週末上限仍須守住。"""
    plain = [Member(R1, "甲", "R1"), Member(R2, "乙", "R2"),
             Member(R3, "丙", "R3")]
    ctx = make_ctx(members=plain, ledger={R1: -12.0, R2: 2.0, R3: 2.0})
    r = solve_duty(ctx)
    assert r.status == "ok"
    wk = _weekend_counts(ctx, r)
    assert max(wk.values()) <= 2, f"帳本壓力下仍不得有人值 3 個週末：{wk}"
