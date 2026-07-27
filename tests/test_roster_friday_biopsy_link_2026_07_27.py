# -*- coding: utf-8 -*-
"""[2026-07-27 使用者需求] 週六早上切片的人,盡量也值週五。

使用者原話:「若R排班 盡量禮拜六早上切片的人 也同樣安排禮拜五值班,但是要在排班
允許的情況下(最後條件,可以選擇的話盡量禮拜六早上切片的人選也值班禮拜五)」。

兩側各一半,合起來才完整(切片人選有兩種來路):
  值班側 FridayBiopsyLinkRule —— 週六排到 R2/R3 時(→ 他就是切片者,值班連動),
      獎勵同一人也值週五。
  切片側 assign_saturday_biopsy —— 週六值班是 R1 時切片改走次數平衡,
      此時同分優先挑【已經值週五】的人。

同一則需求裡使用者也重申「R2/R3 週六切片次數要盡量一整年一樣」,
所以週五連動【不可】排在次數平衡之前 —— 本檔多項測試就是釘這件事。
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.model import Member  # noqa: E402
from cmuh_common.roster.rules import (  # noqa: E402
    POINT_WEIGHT, ConsecutiveDutyRule, DutyCountBalanceRule,
    FridayBiopsyLinkRule,
)
from cmuh_common.roster.saturday_biopsy import (  # noqa: E402
    assign_saturday_biopsy, settle_biopsy,
)

SATS = [date(2026, 8, d) for d in (1, 8, 15, 22, 29)]
R1, R2, R3 = "r1", "r2", "r3"


def members():
    return [Member(R1, "甲", "R1", fixed_weekday=2),
            Member(R2, "乙", "R2", fixed_weekday=3),
            Member(R3, "丙", "R3", fixed_weekday=1)]


def _assign(duty=None, leaves=None, counts=None, last=None, overrides=None):
    return assign_saturday_biopsy(
        year=2026, month=8, members=members(), duty=duty or {},
        leaves=leaves or {}, counts=counts or {}, last_person=last,
        overrides=overrides)


# ─── 權重推導(最脆弱的部分,直接釘住)─────────────────────────────────────
MIN_POINT_STEP = 1 * 100 * POINT_WEIGHT   # 0.01 點 × scale × 權重 = 10,000


def test_link_weight_cannot_outrank_point_fairness():
    """★「在排班允許的情況下」★ 每月最多 5 個週六,獎勵總和必須小於
    最小點數步進(0.01 點),否則會為了連動而犧牲點數公平。"""
    assert 5 * FridayBiopsyLinkRule.LINK_WEIGHT < MIN_POINT_STEP


def test_count_balance_outranks_the_friday_link():
    """★使用者把週五連動定為「最後條件」、把班數一致列為獨立要求★
    單一單位班數全距必須壓過單一個週五連動。"""
    assert (DutyCountBalanceRule.RANGE_WEIGHT
            > FridayBiopsyLinkRule.LINK_WEIGHT)


def test_count_balance_still_below_point_fairness():
    """班數項在實務全距(≤5,再大點數項就先擋下了)內不可突破點數步進。"""
    assert 5 * DutyCountBalanceRule.RANGE_WEIGHT < MIN_POINT_STEP


def test_count_balance_outranks_three_in_a_row():
    """★原本權重 1 的問題★ 1 單位班數全距連 3 連值罰則(500)都輸,
    等於「幾乎不會為了班數平均動任何一格」。使用者明確要求班數一致 → 要贏。"""
    assert (DutyCountBalanceRule.RANGE_WEIGHT
            > ConsecutiveDutyRule.RUN3_WEIGHT)


def test_link_weight_outranks_three_in_a_row():
    """週五+週六+週日=3 連值,會吃到 3 連罰則。本獎勵必須壓過它,
    否則這條規則在最常見的情境下等於沒作用。
    (使用者 2026-07-13 已定案「3 天勉強可接受」,2026-07-27 又明確要這條連動。)"""
    assert (FridayBiopsyLinkRule.LINK_WEIGHT
            > ConsecutiveDutyRule.RUN3_WEIGHT)


def test_link_weight_never_induces_four_in_a_row():
    """4 連/5 連仍要遠遠壓過本獎勵 —— 連動不可誘發使用者明確不要的長連值。"""
    assert ConsecutiveDutyRule.RUN4_WEIGHT > 5 * FridayBiopsyLinkRule.LINK_WEIGHT
    assert ConsecutiveDutyRule.RUN5_WEIGHT > 5 * FridayBiopsyLinkRule.LINK_WEIGHT


def test_terms_are_rewards_not_penalties():
    """★形式很重要★ 若寫成「懲罰 R2 值週六卻沒值週五」,求解器可以改把 R1
    排進週六來躲掉罰則(成本 0)—— 反而破壞使用者更想要的「週末由 R2/R3 值」。
    獎勵式:連上得負權重、沒連上 0、週六排 R1 也是 0 → 只會往連上推。"""
    src = open(os.path.join(os.path.dirname(__file__), '..', 'src',
                            'cmuh_common', 'roster', 'rules.py'),
               encoding='utf-8').read()
    i = src.index("class FridayBiopsyLinkRule")
    j = src.index("class DutyCountBalanceRule", i)
    body = src[i:j]
    assert "terms.append((linked, -self.LINK_WEIGHT))" in body, "必須是負權重(獎勵)"
    assert "linked <= mc.x[(fri, m.id)]" in body
    assert "linked <= mc.x[(sat, m.id)]" in body


# ─── 值班側:端到端(需要 ortools)─────────────────────────────────────────
def _solved(link_weight=None):
    # 沿用 test_roster_solve 的 2026/8 情境(3 人、固定週幾、交替週色)。
    # 不在此動 sys.path —— pytest 已把 tests/ 放進來,執行期再插路徑會影響
    # 其他測試的 import 解析(踩過:整份測試的 skip 行為都變了)。
    from test_roster_solve import make_ctx
    from cmuh_common.roster import rules as R
    from cmuh_common.roster.solve_rvs import solve_duty
    old = R.FridayBiopsyLinkRule.LINK_WEIGHT
    try:
        if link_weight is not None:
            R.FridayBiopsyLinkRule.LINK_WEIGHT = link_weight
        return solve_duty(make_ctx())
    finally:
        R.FridayBiopsyLinkRule.LINK_WEIGHT = old


def _linked_saturdays(res):
    """週六排到 R2/R3、且同一人也值週五的週六。"""
    return sorted(s.day for s in SATS
                  if res.assignments.get(s) in (R2, R3)
                  and res.assignments.get(s - timedelta(days=1))
                  == res.assignments.get(s))


def test_solver_links_friday_when_it_is_free():
    """★端到端★ 2026/8 基準情境:關閉本規則時 0 個週六連上,啟用後至少 2 個,
    而且【點數與班數分布完全不變】—— 這正是「排班允許的情況下」。

    刻意不釘死是哪幾個週六:同分最佳解不只一組,釘日期會變成脆弱測試
    (只是在測 CP-SAT 的搜尋順序,不是在測需求)。要測的性質是
    「連上的數量變多、而且沒有付出任何代價」。"""
    pytest.importorskip("ortools", reason="ortools 未安裝")
    off = _solved(link_weight=0)      # 只關掉本規則,其餘權重不動
    on = _solved()
    assert off.status == "ok" and on.status == "ok"
    assert _linked_saturdays(off) == [], "關閉時不該有連上(否則測不出效果)"
    assert len(_linked_saturdays(on)) >= 2
    assert (sorted(on.points_by_person.values())
            == sorted(off.points_by_person.values())), "不可犧牲點數公平"
    assert (sorted(on.duty_counts.values())
            == sorted(off.duty_counts.values())), "不可犧牲班數平衡"
    assert (sorted(on.weekend_counts.values())
            == sorted(off.weekend_counts.values())), "不可犧牲週末平均"


def test_solver_still_balances_counts():
    """★使用者第 3 點★ 值班班數在點數允許下要接近一致。
    (DutyCountBalanceRule 早已實作,此處釘住不被本次改動打破:
     31 天 ÷ 3 人 → 只能是 11/10/10,全距 1。)"""
    pytest.importorskip("ortools", reason="ortools 未安裝")
    res = _solved()
    counts = sorted(res.duty_counts.values())
    assert counts == [10, 10, 11]
    assert max(counts) - min(counts) <= 1


# ─── 切片側:週五連動只在次數平手時決勝 ───────────────────────────────────
def test_friday_duty_breaks_the_tie():
    """次數平手 + 8/1 週五(7/31)值班是 r3 → 挑 r3,理由要標出週五連動。"""
    assign, _ = _assign(duty={SATS[0]: R1, date(2026, 7, 31): R3},
                        counts={R2: 5, R3: 5})
    assert assign[SATS[0]]["person"] == R3
    assert assign[SATS[0]]["reason"] == "次數平衡·週五連動"


def test_counts_still_win_over_friday_duty():
    """★核心防線★ 使用者同一則需求裡要「切片次數一整年一樣」,
    週五連動是最後條件 —— 次數落後者仍優先,不可被週五連動蓋過。"""
    assign, _ = _assign(duty={SATS[0]: R1, date(2026, 7, 31): R3},
                        counts={R2: 3, R3: 9})
    assert assign[SATS[0]]["person"] == R2, "次數少的 r2 仍要優先"
    assert assign[SATS[0]]["reason"] == "次數平衡"


def test_duty_link_still_highest():
    """週六值班本人是 R2/R3 → 值班連動不受週五影響(即使週五是另一位)。"""
    assign, _ = _assign(duty={SATS[1]: R3, date(2026, 8, 7): R2},
                        counts={R2: 0, R3: 99})
    assert assign[SATS[1]] == {"person": R3, "reason": "值班連動"}


def test_manual_override_still_highest():
    """手動右鍵指定仍是最高優先,週五連動不得插隊。"""
    assign, _ = _assign(duty={SATS[0]: R1, date(2026, 7, 31): R3},
                        counts={R2: 5, R3: 5},
                        overrides={SATS[0]: R2})
    assert assign[SATS[0]] == {"person": R2, "reason": "手動指定"}


def test_reason_label_only_when_friday_actually_decided():
    """★訊息只能陳述程式確知的事★ 次數本來就分出勝負時,不可宣稱是週五連動決定的。"""
    assign, _ = _assign(duty={SATS[0]: R1, date(2026, 7, 31): R2},
                        counts={R2: 1, R3: 9})
    assert assign[SATS[0]]["person"] == R2
    assert assign[SATS[0]]["reason"] == "次數平衡", "是次數決定的,不是週五"


def test_leave_still_beats_friday_duty():
    """請假最高優先(全系統 R4)——週五有值班也不能把請假的人排進切片。"""
    assign, _ = _assign(duty={SATS[0]: R1, date(2026, 7, 31): R3},
                        leaves={R3: {SATS[0]}}, counts={R2: 5, R3: 5})
    assert assign[SATS[0]]["person"] == R2


# ─── 全年次數平均(使用者第 2 點)不可被本次改動破壞 ───────────────────────
def test_annual_balance_survives_friday_preference():
    """★迴歸★ 連續 12 個月、每個週六的週五都固定值 r3(最偏心的情境),
    R2/R3 的全年累計切片次數仍要維持全距 ≤1。"""
    import calendar
    book: dict = {"counts": {}, "history": []}
    for month in range(1, 13):
        _, last = calendar.monthrange(2026, month)
        sats = [date(2026, month, d) for d in range(1, last + 1)
                if date(2026, month, d).weekday() == 5]
        duty = {}
        for s in sats:
            duty[s] = R1                       # 週六都給 R1 → 一律走次數平衡
            duty[s - timedelta(days=1)] = R3    # 週五永遠是 r3(最偏心)
        assign, _ = assign_saturday_biopsy(
            year=2026, month=month, members=members(), duty=duty,
            leaves={}, counts=dict(book["counts"]),
            last_person=None)
        settle_biopsy(book, f"2026-{month:02d}", assign)
    c2, c3 = book["counts"][R2], book["counts"][R3]
    assert abs(c2 - c3) <= 1, f"全年累計 r2={c2} r3={c3},全距必須 ≤1"
    assert c2 + c3 == 52 or c2 + c3 == 53, f"2026 年共 {c2 + c3} 個週六"
