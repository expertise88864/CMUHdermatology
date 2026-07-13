# -*- coding: utf-8 -*-
"""連續值班軟限制(2026-07-13 使用者需求):盡量不連 4/5 天、3 天勉強可接受。

軟性:硬約束(假日成對/三連休/指定)逼出的連值照常成立、不影響可行性;
跨月連續性由 ctx.prev_tail(上月最後 4 天)納入。
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

pytest.importorskip("ortools", reason="ortools 未安裝（自動排班引擎）")

from cmuh_common.roster.model import Member, SolveContext, week_key  # noqa: E402
from cmuh_common.roster.rules import (  # noqa: E402
    RULE_REGISTRY, POINT_WEIGHT, ConsecutiveDutyRule)
from cmuh_common.roster.solve_rvs import solve_duty  # noqa: E402

R1, R2, R3 = "r1", "r2", "r3"


def r_members():
    return [Member(R1, "甲", "R1", fixed_weekday=2),
            Member(R2, "乙", "R2", fixed_weekday=3),
            Member(R3, "丙", "R3", fixed_weekday=1)]


def aug_colors():
    sats = [date(2026, 8, d) for d in (1, 8, 15, 22, 29)]
    return {week_key(s): ("pink" if i % 2 == 0 else "green")
            for i, s in enumerate(sats)}


def make_ctx(**kw):
    ctx = SolveContext(
        scope=kw.get("scope", "r"), year=2026, month=8,
        members=kw.get("members", r_members()),
        holidays=set(kw.get("holidays") or set()),
        leaves={k: set(v) for k, v in (kw.get("leaves") or {}).items()},
        must_duty={k: set(v) for k, v in (kw.get("must") or {}).items()},
        ledger=dict(kw.get("ledger") or {}),
        week_colors=dict(kw.get("colors", aug_colors())),
        prev_last_weekend=kw.get("prev"),
        prev_tail=dict(kw.get("prev_tail") or {}),
    )
    return ctx.prepare()


def max_run(assignments: dict, mid: str) -> int:
    best = cur = 0
    prev_d = None
    for d in sorted(assignments):
        if assignments[d] == mid:
            cur = (cur + 1 if prev_d == d - timedelta(days=1)
                   and assignments.get(prev_d) == mid else 1)
            best = max(best, cur)
        prev_d = d
    return best


# ── 一般月:任何人不得連 4 天(有其他可行解時)────────────────────────────────
def test_default_month_no_run_of_four():
    r = solve_duty(make_ctx())
    assert r.status == "ok"
    for m in (R1, R2, R3):
        assert max_run(r.assignments, m) <= 3, \
            f"{m} 連值 {max_run(r.assignments, m)} 天(應 ≤3)"


# ── 三連休(硬規則):3 連照常成立、不被軟限制打破 ─────────────────────────────
def test_three_day_holiday_block_still_same_person():
    # 8/10(一)國定假日 → 8/8-8/10 三連休段同一人(硬),軟限制容忍 3 連
    hol = {date(2026, 8, 10)}
    r = solve_duty(make_ctx(holidays=hol))
    assert r.status == "ok"
    p = r.assignments[date(2026, 8, 8)]
    assert r.assignments[date(2026, 8, 9)] == p
    assert r.assignments[date(2026, 8, 10)] == p
    assert max(max_run(r.assignments, m) for m in (R1, R2, R3)) <= 3


# ── 指定逼出 4 連:軟限制讓路、照樣可解(「班表允許的情況下」語意)──────────────
def test_hard_must_duty_can_still_force_four_run():
    must = {R1: [date(2026, 8, d) for d in (17, 18, 19, 20)]}  # 週一~四
    r = solve_duty(make_ctx(must=must))
    assert r.status == "ok", "軟限制不得破壞可行性"
    assert max_run(r.assignments, R1) >= 4


# ── 跨月:上月尾端連值 → 月初不再延長成 4 連 ────────────────────────────────
def test_prev_tail_blocks_month_start_extension():
    # 上月 7/30(四)7/31(五)=r1;8/1-8/2 是週末區塊 → r1 接該區塊即成 4/5 連
    tail = {date(2026, 7, 30): R1, date(2026, 7, 31): R1}
    r = solve_duty(make_ctx(prev_tail=tail))
    assert r.status == "ok"
    assert r.assignments[date(2026, 8, 1)] != R1, \
        "上月尾端已連 2 天,月初週末區塊不應再給同一人(4+ 連)"


def test_prev_tail_other_person_not_restricted():
    # 上月尾端是別人 → 對本月無額外限制(視窗被常數擋掉)
    tail = {date(2026, 7, 30): "someone_gone", date(2026, 7, 31): "someone_gone"}
    r = solve_duty(make_ctx(prev_tail=tail))
    assert r.status == "ok"


# ── service:build_context 載入上月尾端 4 天 → prev_tail ─────────────────────
def test_service_build_context_loads_prev_tail(tmp_path):
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [m.to_dict() for m in r_members()]})
    prev = st.load_month("2026-07")
    prev["r_duty"] = {
        "2026-07-30": {"person": R1, "locked": False, "source": "auto"},
        "2026-07-31": {"person": R1, "locked": False, "source": "auto"},
        "2026-07-15": {"person": R2, "locked": False, "source": "auto"},  # 非尾端
    }
    st.save_month("2026-07", prev)
    ctx = RosterService(st).build_context("r", "2026-08")
    assert ctx.prev_tail == {date(2026, 7, 30): R1, date(2026, 7, 31): R1}


def test_service_build_context_prev_tail_empty_when_no_prev_month(tmp_path):
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [m.to_dict() for m in r_members()]})
    ctx = RosterService(st).build_context("r", "2026-08")
    assert ctx.prev_tail == {}


# ── 註冊/權重階梯釘位 ─────────────────────────────────────────────────────
def test_rule_registered_soft_with_weight_ladder():
    assert ConsecutiveDutyRule in RULE_REGISTRY
    rule = ConsecutiveDutyRule()
    assert rule.kind == "soft" and rule.scope == "both"
    # 階梯:RUN5 > RUN4 > 1 點 dev(100×POINT_WEIGHT) > 最小點數步進 > RUN3 > 31
    one_point = 100 * POINT_WEIGHT
    assert rule.RUN5_WEIGHT > rule.RUN4_WEIGHT > one_point
    assert rule.RUN3_WEIGHT < POINT_WEIGHT       # 低於最小點數步進(0.01 點)
    assert rule.RUN3_WEIGHT > 31                 # 高於 count_balance 全距上限
