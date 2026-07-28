# -*- coding: utf-8 -*-
"""[2026-08-02 補審] Codex GPT-5.6-sol 對 f3561d6..915f39e(排班拆段那批)的兩條 finding。

兩條都是 2026-07-27「連休段可依使用者指定拆段」引進的迴歸:那個 commit 已經為了
「連休段可能往前鏈入週五國定假日 → days[0] 未必是值週末的人」修過 WeekendCapRule
與 res.last_weekend,但**同一個理由**的另外兩處沒跟上。

  P1 ColorRule.apply 仍用 b.days[0] 當週末代表日。
  P2 WeekendBlockRule.precheck 的「無人可值」仍要求同一人覆蓋【整個原始區塊】。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.model import Member, SolveContext, week_key  # noqa: E402
from cmuh_common.roster.rules import WeekendBlockRule  # noqa: E402

# 2026/09:9/25=五、9/26=六、9/27=日、9/28=一。把 9/25 與 9/28 設為國定假日 →
# 25-28 鏈成一個四天連休段(使用者 2026-07-27 實際遇到的情境)。
SEP_HOLIDAYS = {date(2026, 9, 25), date(2026, 9, 28)}
Z, K, W = "z", "k", "w"


def _members():
    return [Member(Z, "甲", "R1"), Member(K, "乙", "R2"), Member(W, "丙", "R3")]


def _ctx(*, leaves=None, locks=None, colors=None, prev=None):
    ctx = SolveContext(
        scope="r", year=2026, month=9, members=_members(),
        holidays=set(SEP_HOLIDAYS),
        leaves={k: set(v) for k, v in (leaves or {}).items()},
        must_duty={}, annual_holiday={}, locks=dict(locks or {}),
        ledger={}, week_colors=dict(colors or {}), prev_last_weekend=prev)
    return ctx.prepare()


def _block_of(ctx, day):
    for b in ctx.blocks:
        if day in b.days:
            return b
    raise AssertionError(f"{day} 不在任何區塊")


def test_the_september_block_really_chains_the_friday_holiday():
    """前提確認:9/25(五·假)真的被鏈進 9/26-27 那個連休段,且 days[0] != saturday。
    (若這個前提不成立,下面兩條 finding 都無從發生 —— 先釘住情境本身。)"""
    ctx = _ctx()
    b = _block_of(ctx, date(2026, 9, 26))
    assert b.days[0] == date(2026, 9, 25), "週五假日要被往前鏈入"
    assert b.saturday == date(2026, 9, 26)
    assert b.color_anchor() == date(2026, 9, 26), "週色/週末代表日是週六"
    assert b.days[0] != b.saturday, "★這正是 days[0] 不可當代表日的原因★"


# ─── P1:ColorRule 的代表日 ────────────────────────────────────────────────
def test_color_rule_constrains_the_saturday_person_not_the_friday_one():
    """★P1★ 連週色塊限制必須套在【值週末的人】身上。

    拆段之後週五可以是別人:9/25 鎖給 K、9/26-27 由求解決定。若限制仍套在 days[0]
    (週五的 K),就會變成「限制到不相干的人」—— 既可能誤擋合規班表,也可能放行
    真正值週末的人連值兩個同色週末。
    """
    src = open(os.path.join(os.path.dirname(__file__), '..', 'src',
                            'cmuh_common', 'roster', 'rules.py'),
               encoding='utf-8').read()
    i = src.index("class ColorRule")
    j = src.index("class DutyRangeRule", i)
    body = src[i:j]
    i_apply = body.index("def apply(")
    apply_src = body[i_apply:]
    assert "b.days[0]" not in apply_src, "跨月配對不可用 days[0] 當週末代表日"
    assert "a.days[0]" not in apply_src, "相鄰配對不可用 days[0] 當週末代表日"
    assert "b.color_anchor()" in apply_src
    assert "a.color_anchor()" in apply_src


def test_color_rule_end_to_end_lets_the_weekend_person_repeat_when_broken():
    """★端到端(實測兩邊都跑過才寫下的)★

    要碰到這個 bug,連休段必須【真的拆段】—— 而拆段需要**兩個不同的指定**,
    只指定週五不會拆(我前兩版的情境都因此在舊碼下也通過,等於沒測到)。

    情境:9/25(五·假)指定 K、9/26(六)指定 Z → 拆成 [9/25] 與 [9/26-28]。
    week(9/19) 與 week(9/26) 同色;K、W 在 9/19-20 請假 → 9/19 只剩 Z 可值。

      舊碼:限制套在 b.days[0] = 9/25(K)→ 對 Z 完全沒有約束
            → status=ok,而且 **Z 同時值 9/19 與 9/26 兩個同色連續週末**(實測)。
      新碼:限制套在 b.color_anchor() = 9/26(Z)→ 不放寬色塊就無解
            → status=need_confirm_color(交由使用者確認是否放寬),規則真的生效了。
    """
    pytest.importorskip("ortools", reason="ortools 未安裝")
    from cmuh_common.roster.solve_rvs import solve_duty
    sat_prev, sat = date(2026, 9, 19), date(2026, 9, 26)
    colors = {week_key(sat_prev): "pink", week_key(sat): "pink",
              week_key(date(2026, 9, 5)): "green",
              week_key(date(2026, 9, 12)): "green"}
    ctx = _ctx(colors=colors,
               locks={date(2026, 9, 25): K, sat: Z},
               leaves={K: {sat_prev, date(2026, 9, 20)},
                       W: {sat_prev, date(2026, 9, 20)}})
    res = solve_duty(ctx)
    assert res.status == "need_confirm_color", (
        f"限制若沒套在週六的人身上,這裡會是 ok 且 Z 連值兩個同色週末;"
        f"實際 status={res.status} 9/19={res.assignments.get(sat_prev)}")


def test_color_rule_does_not_over_block_when_colors_differ():
    """★不可矯枉過正★ 兩個週末【不同色】時本來就允許同一人 —— 不得誤擋。"""
    pytest.importorskip("ortools", reason="ortools 未安裝")
    from cmuh_common.roster.solve_rvs import solve_duty
    sat_prev, sat = date(2026, 9, 19), date(2026, 9, 26)
    colors = {week_key(sat_prev): "green", week_key(sat): "pink",
              week_key(date(2026, 9, 5)): "pink",
              week_key(date(2026, 9, 12)): "green"}
    ctx = _ctx(colors=colors,
               locks={date(2026, 9, 25): K, sat: Z},
               leaves={K: {sat_prev, date(2026, 9, 20)},
                       W: {sat_prev, date(2026, 9, 20)}})
    res = solve_duty(ctx)
    assert res.status == "ok", res.diagnosis
    assert res.assignments[sat_prev] == Z, "不同色 → 同一人連值兩個週末是允許的"


# ─── P2:逐段檢查「無人可值」──────────────────────────────────────────────
def test_precheck_checks_each_run_not_the_whole_block():
    """★P2★ Codex 給的反例:9/25-27 指定 Z、9/28 指定 K,
    Z 只在 9/28 請假、K 只在 9/25-27 請假 —— 兩段各自完全可解、指定日也沒有請假衝突,
    但「有沒有人能值完整四天」的答案是「沒有」→ 舊寫法回 error 把整個求解擋掉。"""
    ctx = _ctx(
        locks={date(2026, 9, 25): Z, date(2026, 9, 26): Z, date(2026, 9, 27): Z,
               date(2026, 9, 28): K},
        leaves={Z: {date(2026, 9, 28)},
                K: {date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27)},
                W: {date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27),
                    date(2026, 9, 28)}})
    checks = WeekendBlockRule().precheck(ctx)
    errs = [c for c in checks if c.severity == "error"]
    assert not errs, f"逐段皆可值,不該報 error:{[c.msg for c in errs]}"


def test_precheck_still_reports_a_run_nobody_can_cover():
    """★不可矯枉過正★ 某一段真的全員請假 → 仍要報 error,且訊息點名的是【那一段】。"""
    all_days = [date(2026, 9, d) for d in (25, 26, 27, 28)]
    ctx = _ctx(
        locks={date(2026, 9, 25): Z, date(2026, 9, 28): K},
        leaves={m: {date(2026, 9, 28)} for m in (Z, K, W)})
    checks = WeekendBlockRule().precheck(ctx)
    errs = [c for c in checks if c.severity == "error"]
    assert errs, "9/28 全員請假,必須報 error"
    assert "9/28" in errs[0].msg, f"要點名出問題的那一段,實際:{errs[0].msg}"
    assert len(all_days) == 4      # 情境自我說明


def test_unsplit_block_behaviour_is_unchanged():
    """無指定時 runs 只有一段 → 與舊行為完全相同(全員請假仍報整段 error)。"""
    ctx = _ctx(leaves={m: set(_block_of(_ctx(), date(2026, 9, 26)).days)
                       for m in (Z, K, W)})
    checks = WeekendBlockRule().precheck(ctx)
    errs = [c for c in checks if c.severity == "error"]
    assert errs and "9/25-28" in errs[0].msg
