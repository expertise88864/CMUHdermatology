# -*- coding: utf-8 -*-
"""規則註冊表 + R/VS 值班規則（設計文件 §3.2/§3.3/§8）。

擴充 SOP（使用者要求「隨時可能更改排班邏輯」）：
    新增規則 = 寫一個 Rule 子類 + @register_rule + 一個測試檔，不動 solver 主體。
    停用/放寬 = 調 relax_level；參數 = 進 config 由 ctx.params 帶入。

每條規則三件事：
    precheck(ctx)          排班前人話檢查 → [(severity, msg)]，severity ∈ {"error","warn","info"}
    apply(mc, ctx)         對 CP-SAT 模型下硬約束
    objective_terms(mc, ctx) 軟規則回傳 [(IntVar/LinearExpr, weight)] 供目標函數

優先序（使用者定案 R10）：請假 > 指定值班(含年度表/鎖定/跨月銜接) > 固定週幾 > 點數平衡。
directive（指定類）之間互相衝突一律 error，不靜默蓋掉。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from cmuh_common.roster.model import (
    SolveContext, day_point, week_key,
)

# 放寬階梯層級（設計文件 §6）
L0_FULL = 0          # 全部規則
L1_NO_RANGE = 1      # 放寬 9-11 班數範圍（自動）
L2_RESERVED = 2      # 保留級（目前 R/VS 無次要軟公平可放，同 L1）
L3_NO_COLOR = 3      # 停用色塊連週（必須使用者確認）

RULE_REGISTRY: list = []


def register_rule(cls):
    RULE_REGISTRY.append(cls)
    return cls


def rules_for(scope: str) -> list:
    """依 scope 取用規則實例（宣告順序）。scope: "r" / "vs"。"""
    return [cls() for cls in RULE_REGISTRY
            if cls.scope in ("both", scope)]


@dataclass
class Precheck:
    severity: str   # "error" | "warn" | "info"
    rule_id: str
    msg: str


class Rule:
    scope = "both"          # "r" / "vs" / "both"
    kind = "hard"           # "hard" / "soft"
    rule_id = ""
    描述 = ""
    relax_level: Optional[int] = None   # 於該階梯層級(含)以上被停用；None=永不
    needs_confirm = False               # 放寬需使用者確認（色塊）

    def active_at(self, level: int) -> bool:
        return self.relax_level is None or level < self.relax_level

    def precheck(self, ctx: SolveContext) -> list:
        return []

    def apply(self, mc, ctx: SolveContext) -> None:
        pass

    def objective_terms(self, mc, ctx: SolveContext) -> list:
        return []


# ─── directive 彙整（供多條規則共用；不是規則本身）──────────────────────────
def collect_directives(ctx: SolveContext) -> tuple:
    """彙整「指定類」來源 → ({date: (member_id, source)}, [Precheck])。

    來源與標籤：鎖定格="鎖定"、一定要值班="指定"、年度假日表="年度指定"、
    跨月銜接="跨月銜接"。同日不同來源指定不同人 → error。
    「指定週六自動帶週日」由區塊等式自然達成，不在此展開。
    """
    out: dict = {}
    checks: list = []
    mids = set(ctx.member_ids())

    def put(d: date, mid: str, source: str):
        if mid not in mids:
            checks.append(Precheck(
                "warn", "directives",
                f"{d.month}/{d.day} {source}的人選 '{mid}' 不在名單，忽略"))
            return
        if d in out and out[d][0] != mid:
            checks.append(Precheck(
                "error", "directives",
                f"{d.month}/{d.day} 指定衝突：{out[d][1]}={out[d][0]} vs "
                f"{source}={mid}，請先解決"))
            return
        out[d] = (mid, source)

    day_set = set(ctx.days)
    for d, mid in sorted(ctx.locks.items()):
        if d in day_set:
            put(d, mid, "鎖定")
    for mid, dates in sorted(ctx.must_duty.items()):
        for d in sorted(dates):
            if d in day_set:
                put(d, mid, "指定")
    for d, mid in sorted(ctx.annual_holiday.items()):
        if d in day_set:
            put(d, mid, "年度指定")
    for d, mid in sorted(ctx.boundary_fix.items()):
        if d in day_set:
            put(d, mid, "跨月銜接")

    # 指定人當日請假 → error（使用者定案：報衝突不靜默）
    for d, (mid, source) in sorted(out.items()):
        if ctx.on_leave(mid, d):
            checks.append(Precheck(
                "error", "directives",
                f"{d.month}/{d.day} {source}={mid} 但該員當日請假，請先解決"))
    return out, checks


# ─── 硬規則 ──────────────────────────────────────────────────────────────
@register_rule
class LeaveRule(Rule):
    rule_id = "leave"
    描述 = "請假日絕不排班（最高優先）"

    def apply(self, mc, ctx):
        for m in ctx.members:
            for d in ctx.days:
                if ctx.on_leave(m.id, d):
                    mc.model.Add(mc.x[(d, m.id)] == 0)


@register_rule
class DirectiveRule(Rule):
    rule_id = "directives"
    描述 = "鎖定/一定要值班/年度假日指定/跨月銜接 → 固定人選"

    def precheck(self, ctx):
        _, checks = collect_directives(ctx)
        return checks

    def apply(self, mc, ctx):
        directives, _ = collect_directives(ctx)
        for d, (mid, _src) in directives.items():
            mc.model.Add(mc.x[(d, mid)] == 1)


@register_rule
class WeekendBlockRule(Rule):
    rule_id = "weekend_pair"
    描述 = "週六+週日(含相鄰國定假日連休段)須同一人"

    def precheck(self, ctx):
        checks = []
        directives, _ = collect_directives(ctx)
        for b in ctx.blocks:
            # 區塊內兩個不同指定人 → error
            assigned = {directives[d][0] for d in b.days if d in directives}
            if len(assigned) > 1:
                span = f"{b.days[0].month}/{b.days[0].day}-{b.days[-1].day}"
                checks.append(Precheck(
                    "error", self.rule_id,
                    f"週末連休段 {span} 被指定給多人 {sorted(assigned)}，"
                    f"同段必須同一人"))
            # 區塊完全無人可值 → error
            ok = [m.id for m in ctx.members
                  if all(not ctx.on_leave(m.id, d) for d in b.days)]
            if not ok:
                span = f"{b.days[0].month}/{b.days[0].day}-{b.days[-1].day}"
                checks.append(Precheck(
                    "error", self.rule_id,
                    f"週末連休段 {span} 所有人皆請假，無人可值"))
            if b.kind == "weekend_orphan" and not ctx.boundary_fix:
                checks.append(Precheck(
                    "warn", self.rule_id,
                    f"{b.days[0].month}/{b.days[0].day}(週日) 的週六在上月且"
                    f"無上月資料 → 該日獨立指派（無法成對）"))
        return checks

    def apply(self, mc, ctx):
        for b in ctx.blocks:
            first = b.days[0]
            for d in b.days[1:]:
                for m in ctx.members:
                    mc.model.Add(mc.x[(d, m.id)] == mc.x[(first, m.id)])


@register_rule
class FixedWeekdayRule(Rule):
    scope = "r"
    rule_id = "fixed_weekday"
    描述 = "R 固定值班週幾（預設 R1=三 R2=四 R3=二；可設定）"

    def _applicable(self, ctx, m, d, directives) -> bool:
        return (m.fixed_weekday is not None
                and d.weekday() == m.fixed_weekday
                and d.weekday() < 5                # 固定週幾僅適用平日
                and d not in ctx.holidays          # 假日歸年度指定表管
                and d not in directives            # 指定類優先
                and not ctx.on_leave(m.id, d))     # 請假最優先

    def precheck(self, ctx):
        checks = []
        directives, _ = collect_directives(ctx)
        for m in ctx.members:
            if m.fixed_weekday is None:
                continue
            for d in ctx.days:
                if d.weekday() != m.fixed_weekday or d.weekday() >= 5:
                    continue
                if ctx.on_leave(m.id, d):
                    checks.append(Precheck(
                        "info", self.rule_id,
                        f"{d.month}/{d.day} 為 {m.name} 固定值班日但已請假 → "
                        f"由其他人代（點數自然流動）"))
                elif d in directives and directives[d][0] != m.id:
                    checks.append(Precheck(
                        "warn", self.rule_id,
                        f"{d.month}/{d.day} 為 {m.name} 固定值班日，但被"
                        f"{directives[d][1]}給 {directives[d][0]}（指定優先）"))
        return checks

    def apply(self, mc, ctx):
        directives, _ = collect_directives(ctx)
        for m in ctx.members:
            for d in ctx.days:
                if self._applicable(ctx, m, d, directives):
                    mc.model.Add(mc.x[(d, m.id)] == 1)


@register_rule
class ColorRule(Rule):
    rule_id = "weekend_color"
    描述 = "連續兩週末同一人僅當兩週色塊不同；同色須休一週（R/VS 皆適用）"
    relax_level = L3_NO_COLOR
    needs_confirm = True

    def _pairs(self, ctx):
        """相鄰週末區塊對（含上月最後週末 → 本月第一個「不同週」的區塊）。

        跨月陷阱：月初孤兒週日與上月週六是**同一個週末**（同 ISO 週），
        不是「連續兩週末」——孤兒日由跨月銜接固定給上月人選，若誤配對會
        產生 x==1 與 x==0 矛盾。故孤兒塊與 prev 同週時：連週配對改為
        (上月人選, 下一塊)，塊間配對也從下一塊開始。
        回 [(prev_person_or_None, block_a_or_None, block_b, same_color, unknown)]"""
        out = []
        blocks = list(ctx.blocks)
        idx0 = 0
        if ctx.prev_last_weekend and blocks:
            prev_sat, prev_person = ctx.prev_last_weekend
            first = blocks[0]
            target = first
            if (first.kind == "weekend_orphan"
                    and week_key(first.color_anchor()) == week_key(prev_sat)):
                idx0 = 1                      # 孤兒塊=上月週末的延伸,跳過
                target = blocks[1] if len(blocks) > 1 else None
            if target is not None:
                ca = ctx.week_colors.get(week_key(prev_sat))
                cb = ctx.color_of_block(target)
                unknown = ca is None or cb is None
                out.append((prev_person, None, target,
                            unknown or ca == cb, unknown))
        for a, b in zip(blocks[idx0:], blocks[idx0 + 1:]):
            ca, cb = ctx.color_of_block(a), ctx.color_of_block(b)
            unknown = ca is None or cb is None
            out.append((None, a, b, unknown or ca == cb, unknown))
        return out

    def precheck(self, ctx):
        checks = []
        if len(ctx.members) <= 1:
            checks.append(Precheck(
                "warn", self.rule_id,
                "只有 1 位成員 → 色塊連週規則自動停用（無從輪替）"))
            return checks
        for prev_p, a, b, same, unknown in self._pairs(ctx):
            if unknown and same:
                anchor = b.color_anchor()
                checks.append(Precheck(
                    "warn", self.rule_id,
                    f"{anchor.month}/{anchor.day} 該週或前一週的色塊未設定 → "
                    f"保守視為同色（禁止連值），請至設定頁匯入/校正行事曆週色"))
        if ctx.prev_last_weekend is None and ctx.blocks:
            checks.append(Precheck(
                "warn", self.rule_id,
                "無上月「最後週末」資料 → 本月第一個週末不受跨月連週限制"))
        return checks

    def apply(self, mc, ctx):
        if len(ctx.members) <= 1:
            return
        for prev_p, a, b, same, _unknown in self._pairs(ctx):
            if not same:
                continue
            if a is None:  # 跨月：上月人選不得值本月第一週末
                if prev_p in ctx.member_ids():
                    mc.model.Add(mc.x[(b.days[0], prev_p)] == 0)
            else:
                for m in ctx.members:
                    mc.model.Add(
                        mc.x[(a.days[0], m.id)] + mc.x[(b.days[0], m.id)] <= 1)


@register_rule
class DutyRangeRule(Rule):
    scope = "r"
    rule_id = "duty_range"
    描述 = "每人每月班數範圍（預設 9-11；無解自動放寬 → 只求點數平衡）"
    relax_level = L1_NO_RANGE

    def precheck(self, ctx):
        n, days = len(ctx.members), len(ctx.days)
        if n <= 1:
            return [Precheck("warn", self.rule_id,
                             "只有 1 位成員 → 班數範圍規則自動停用")]
        lo, hi = ctx.params.duty_min * n, ctx.params.duty_max * n
        if not (lo <= days <= hi):
            return [Precheck(
                "warn", self.rule_id,
                f"本月 {days} 天 ÷ {n} 人與範圍 {ctx.params.duty_min}-"
                f"{ctx.params.duty_max} 班在算術上不相容 → 將自動放寬(L1)")]
        return []

    def apply(self, mc, ctx):
        if len(ctx.members) <= 1:
            return
        for m in ctx.members:
            n = sum(mc.x[(d, m.id)] for d in ctx.days)
            mc.model.Add(n >= ctx.params.duty_min)
            mc.model.Add(n <= ctx.params.duty_max)


# ─── 軟規則（目標函數）───────────────────────────────────────────────────
# 點數項權重：遠大於次要「班數全距」項的最大可能值（≤ 當月天數，恆 <1000）。
# 點數 dev 為整數，任何非零改善 ≥ POINT_WEIGHT×1 ＝ 10000 >> 班數項 → 保證
# 「點數平衡優先、班數平衡僅為同分決勝」，即使帳本為任意分數（round 後 dev 可
# 差 <100 也無妨，因為權重差距把它壓死）。
POINT_WEIGHT = 10000


@register_rule
class PointBalanceRule(Rule):
    kind = "soft"
    rule_id = "point_balance"
    描述 = "點數平衡：|每人點數 −(公平份額−帳本結轉)| 總和最小化（最高優先軟目標）"

    def objective_terms(self, mc, ctx):
        if len(ctx.members) <= 1:
            return []
        total = ctx.total_points()
        n = len(ctx.members)
        terms = []
        for m in ctx.members:
            pts_scaled = sum(
                day_point(d, ctx.holidays, ctx.params) * 100 * mc.x[(d, m.id)]
                for d in ctx.days)
            target = round(100 * (total / n - float(ctx.ledger.get(m.id, 0.0))))
            dev = mc.model.NewIntVar(0, 100 * total + abs(target),
                                     f"dev_{m.id}")
            mc.model.AddAbsEquality(dev, pts_scaled - target)
            terms.append((dev, POINT_WEIGHT))
        return terms


@register_rule
class ConsecutiveDutyRule(Rule):
    kind = "soft"
    rule_id = "consecutive_duty"
    描述 = ("連續值班軟限制（2026-07-13 使用者需求，取代 G4「無連續限制」舊定案）："
          "盡量不排連 4 天、更不排連 5 天；3 天勉強可接受。純軟性——硬約束（假日"
          "成對/三連休/指定/固定週幾）逼出的連值照常成立，本規則只在可行解之間挑"
          "連值較少的。跨月連續性由 ctx.prev_tail（上月最後 4 天值班）當常數納入。")

    # 權重階梯（objective 單位；1.0 點的點數 dev = 100(scale)×POINT_WEIGHT = 1,000,000；
    # 最小點數步進 0.01 點 = 10,000；count_balance 全距 ≤31）：
    #   RUN5 ≈ 10 點 dev —— 幾乎只剩硬約束逼迫才會出現 5 連。
    #   RUN4 ≈ 3 點 dev —— 寧可挪一班（≈2 點 dev，帳本下月自動找補）也要拆 4 連。
    #   RUN3 = 500 —— 低於最小點數步進（不犧牲點數公平）、高於 count_balance（≤31）
    #     → 純同分決勝：白給的情況下偏好 2 連以下（「3 天勉強可接受」）。
    # 一個 5 連同時含 3 個 3 連窗＋2 個 4 連窗 → 懲罰自然疊加遞增。
    RUN3_WEIGHT = 500
    RUN4_WEIGHT = 3_000_000
    RUN5_WEIGHT = 10_000_000

    def _windows(self, ctx):
        """產生 (win_dates, length) —— 對「上月尾端＋本月」連續時間軸取 3/4/5 日窗，
        至少含一個本月日期。ctx.days 為整月升冪（必然日曆連續）。"""
        from datetime import timedelta
        tail = sorted(d for d in ctx.prev_tail if d < ctx.days[0])
        # 只納「與本月首日連續銜接」的尾端（缺天=斷鏈,之前的日子與本月不連續）
        timeline: list = []
        cur = ctx.days[0]
        for d in reversed(tail):
            if d == cur - timedelta(days=1):
                timeline.insert(0, d)
                cur = d
            else:
                break
        timeline += ctx.days
        first_in_month = len(timeline) - len(ctx.days)
        for length in (3, 4, 5):
            for i in range(len(timeline) - length + 1):
                if i + length - 1 < first_in_month:
                    continue                      # 全在上月 → 與本次求解無關
                yield timeline[i:i + length], length

    def objective_terms(self, mc, ctx):
        if not ctx.days:
            return []
        weight = {3: self.RUN3_WEIGHT, 4: self.RUN4_WEIGHT, 5: self.RUN5_WEIGHT}
        in_month = set(ctx.days)
        terms = []
        for m in ctx.members:
            for win, length in self._windows(ctx):
                var_days = [d for d in win if d in in_month]
                prev_days = [d for d in win if d not in in_month]
                # 上月尾端日不是本人 → 這扇窗不可能成為本人的連值 → 免建變數
                if any(ctx.prev_tail.get(d) != m.id for d in prev_days):
                    continue
                if not var_days:
                    continue
                b = mc.model.NewBoolVar(
                    f"run{length}_{m.id}_{win[0].isoformat()}")
                # 窗內本月日全排本人 ⇒ sum==len ⇒ b 被逼成 1;否則 b 可為 0(最小化)
                mc.model.Add(
                    sum(mc.x[(d, m.id)] for d in var_days)
                    - (len(var_days) - 1) <= b)
                terms.append((b, weight[length]))
        return terms


@register_rule
class DutyCountBalanceRule(Rule):
    kind = "soft"
    rule_id = "count_balance"
    描述 = ("班數平衡（次要）：讓每人『總班數』盡量接近，但**僅在不損及點數平衡"
          "時**。本項最大貢獻＝班數全距(≤天數<1000)，遠小於點數項每步 "
          "POINT_WEIGHT=10000 → 點數平均優先、班數平均當同分決勝。")

    def objective_terms(self, mc, ctx):
        if len(ctx.members) <= 1:
            return []
        days = len(ctx.days)
        cmax = mc.model.NewIntVar(0, days, "cnt_max")
        cmin = mc.model.NewIntVar(0, days, "cnt_min")
        for m in ctx.members:
            cnt = sum(mc.x[(d, m.id)] for d in ctx.days)
            mc.model.Add(cmax >= cnt)
            mc.model.Add(cmin <= cnt)
        # 最小化 (cmax - cmin) = 班數全距；權重 1（點數項每步 ≥100 主導）
        return [(cmax, 1), (cmin, -1)]


# ─── 整體可行性預檢（非約束）──────────────────────────────────────────────
def core_feasibility_precheck(ctx: SolveContext) -> list:
    """逐日檢查：扣掉請假後至少一人可值。（區塊級檢查在 WeekendBlockRule）"""
    checks = []
    for d in ctx.days:
        ok = [m.id for m in ctx.members if not ctx.on_leave(m.id, d)]
        if not ok:
            checks.append(Precheck(
                "error", "core",
                f"{d.month}/{d.day} 所有人皆請假，無人可值"))
    if not ctx.members:
        checks.append(Precheck("error", "core", "成員名單為空"))
    return checks


def run_prechecks(ctx: SolveContext, scope: str) -> list:
    checks = list(core_feasibility_precheck(ctx))
    for rule in rules_for(scope):
        try:
            checks.extend(rule.precheck(ctx))
        except Exception:
            logging.exception("[roster.rules] precheck 例外 rule=%s", rule.rule_id)
            checks.append(Precheck("warn", rule.rule_id,
                                   f"規則 {rule.rule_id} 預檢執行例外（已略過）"))
    return checks
