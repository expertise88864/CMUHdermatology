# -*- coding: utf-8 -*-
"""R/VS 值班求解器：CP-SAT + 放寬階梯（設計文件 §6）。

流程：
    1. run_prechecks — 任何 error → 不求解，回 precheck_failed（人話清單）。
    2. 放寬階梯 L0 → L1 → L2 逐級求解；仍無解且未獲授權停用色塊 →
       快速測試「停用色塊是否可解」：可 → need_confirm_color（UI 跳窗確認後
       以 allow_disable_color=True 重呼叫走 L3）；否 → infeasible + 診斷。
    3. 成功 → 回 assignments / 點數結算 / 每格理由 / last_weekend（存檔供下月）。

決定性：random_seed 固定 + num_search_workers=1 + ortools 釘版
（cmuh_common.roster.ORTOOLS_PINNED_VERSION）→ 同輸入同輸出。

ortools 為重依賴：lazy import，未安裝時丟 RuntimeError 由 UI 引導安裝。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from datetime import timedelta

from cmuh_common.roster.model import (
    SolveContext, day_point, is_weekend,
)
from cmuh_common.roster.rules import (
    L0_FULL, L1_NO_RANGE, L2_RESERVED, L3_NO_COLOR,
    collect_directives, rules_for, run_prechecks,
)


def apply_boundary_from_prev(ctx: SolveContext) -> None:
    """跨月銜接：上月最後週末的「連休鏈」若延伸進本月，鏈上的本月日期全部
    固定給上月人選（同一連休段同一人）。

    從上月週六翌日(週日)開始逐日走鏈：週日、或「平日的國定假日」都算鏈
    （週六=下一個獨立週末,斷鏈）。涵蓋三種跨月：
      - 月初=週日（上月末=週六）
      - 月初=週一國定假日（上月末=六日 → 三連休跨月,codex 指出的 case）
      - 月初=週日+後續連假（春節型,鏈到第一個非假日或週六為止）
    呼叫前 ctx 需已 prepare() 且設好 prev_last_weekend。等冪,可重複呼叫。"""
    if not ctx.prev_last_weekend or not ctx.days:
        return
    prev_sat, prev_person = ctx.prev_last_weekend
    if prev_person not in ctx.member_ids():
        return
    in_month = set(ctx.days)
    cur = prev_sat + timedelta(days=1)          # 上月週日起走
    for _ in range(10):                         # 防呆上限(連休不可能 >10 天)
        if cur > ctx.days[-1]:
            break
        chained = (cur.weekday() == 6
                   or (cur.weekday() < 5 and cur in ctx.holidays))
        if not chained:
            break
        if cur in in_month:
            ctx.boundary_fix[cur] = prev_person
        cur += timedelta(days=1)

_LEVEL_NAMES = {
    L0_FULL: "L0 全部規則",
    L1_NO_RANGE: "L1 放寬班數範圍",
    L2_RESERVED: "L2 放寬次要公平",
    L3_NO_COLOR: "L3 停用色塊連週(經確認)",
}

SOLVE_TIMEOUT_SEC = 20.0   # 問題極小(≤31天×≤10人)，正常 <1s；此為防呆上限
_RANDOM_SEED = 20260702


class _ModelCtx:
    """包住 cp_model 與變數，供規則 apply 使用。"""

    def __init__(self, model, x):
        self.model = model
        self.x = x  # {(date, member_id): BoolVar}


@dataclass
class SolveResult:
    status: str                       # ok / precheck_failed / need_confirm_color / infeasible / error
    scope: str = ""
    level_used: Optional[int] = None
    level_name: str = ""
    assignments: dict = field(default_factory=dict)   # {date: member_id}
    reasons: dict = field(default_factory=dict)       # {date: 標籤}
    points_by_person: dict = field(default_factory=dict)
    duty_counts: dict = field(default_factory=dict)
    weekday_counts: dict = field(default_factory=dict)
    weekend_counts: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)       # {mid: 目標點數(float)}
    prechecks: list = field(default_factory=list)
    diagnosis: list = field(default_factory=list)     # infeasible 時的人話診斷
    last_weekend: Optional[dict] = None               # {"saturday": iso, "person": id}


def _lazy_cp_model():
    try:
        from ortools.sat.python import cp_model  # noqa: PLC0415
        return cp_model
    except ImportError as e:
        raise RuntimeError(
            "未安裝 ortools（自動排班引擎）。請按 UI 提示安裝後重試。") from e


def _build_and_solve(ctx: SolveContext, scope: str, level: int):
    """在指定放寬層級建模求解 → (cp_status_name, assignments|None)。"""
    cp_model = _lazy_cp_model()
    model = cp_model.CpModel()
    x = {(d, m.id): model.NewBoolVar(f"x_{d.isoformat()}_{m.id}")
         for d in ctx.days for m in ctx.members}
    mc = _ModelCtx(model, x)

    for d in ctx.days:  # 每日恰一人（核心，不屬任何可放寬規則）
        model.AddExactlyOne(x[(d, m.id)] for m in ctx.members)

    objective = []
    for rule in rules_for(scope):
        if not rule.active_at(level):
            continue
        rule.apply(mc, ctx)
        objective.extend(rule.objective_terms(mc, ctx))
    if objective:
        model.Minimize(sum(var * w for var, w in objective))

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = _RANDOM_SEED
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = SOLVE_TIMEOUT_SEC
    status = solver.Solve(model)
    name = solver.StatusName(status)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        out = {}
        for d in ctx.days:
            for m in ctx.members:
                if solver.Value(x[(d, m.id)]):
                    out[d] = m.id
                    break
        return name, out
    return name, None


def _reasons_for(ctx: SolveContext, scope: str, assignments: dict) -> dict:
    """每格「為什麼是這個人」標籤（報告用；優先序同規則）。"""
    directives, _ = collect_directives(ctx)
    fixed_days = {}
    for m in ctx.members:
        if m.fixed_weekday is None:
            continue
        for d in ctx.days:
            if (d.weekday() == m.fixed_weekday and d.weekday() < 5
                    and d not in ctx.holidays and d not in directives
                    and not ctx.on_leave(m.id, d)):
                fixed_days[d] = m.id
    in_block = {d: b for b in ctx.blocks for d in b.days}
    out = {}
    for d, mid in assignments.items():
        if d in directives:
            out[d] = directives[d][1]
        elif scope == "r" and fixed_days.get(d) == mid:
            out[d] = "固定週幾"
        elif d in in_block:
            out[d] = "假日成對"
        else:
            out[d] = "點數平衡"
    return out


def solve_duty(ctx: SolveContext, allow_disable_color: bool = False) -> SolveResult:
    """主入口。ctx 需已 prepare()；scope 取 ctx.scope（"r"/"vs"）。"""
    scope = ctx.scope
    res = SolveResult(status="error", scope=scope)
    try:
        if not ctx.days:
            ctx.prepare()
        # [codex P2] 跨月銜接在此自動套用：呼叫端只需設 prev_last_weekend,
        # 不必記得另呼叫 helper（重複呼叫等冪,已設同值無害）。
        apply_boundary_from_prev(ctx)
        res.prechecks = run_prechecks(ctx, scope)
        if any(c.severity == "error" for c in res.prechecks):
            res.status = "precheck_failed"
            return res

        max_auto = L2_RESERVED
        levels = [L0_FULL, L1_NO_RANGE, L2_RESERVED]
        if allow_disable_color:
            levels.append(L3_NO_COLOR)

        chosen = None
        for level in levels:
            name, assignments = _build_and_solve(ctx, scope, level)
            logging.info("[roster.solve] %s %04d-%02d %s → %s",
                         scope, ctx.year, ctx.month, _LEVEL_NAMES[level], name)
            if assignments is not None:
                chosen = (level, assignments)
                break

        if chosen is None:
            # 自動層級全數無解：測「停用色塊」可否解 → 請使用者確認
            if not allow_disable_color:
                _n, test = _build_and_solve(ctx, scope, L3_NO_COLOR)
                if test is not None:
                    res.status = "need_confirm_color"
                    res.diagnosis = [
                        "在不動色塊連週規則的前提下無解；停用色塊規則後可解。",
                        "請確認是否放寬（將出現同色連週值班）。"]
                    return res
            res.status = "infeasible"
            res.diagnosis = _diagnose(ctx, scope)
            return res

        level, assignments = chosen
        if level > max_auto and not allow_disable_color:  # 理論上不會到
            res.status = "need_confirm_color"
            return res

        res.status = "ok"
        res.level_used = level
        res.level_name = _LEVEL_NAMES[level]
        res.assignments = assignments
        res.reasons = _reasons_for(ctx, scope, assignments)

        total = ctx.total_points()
        n = max(1, len(ctx.members))
        for m in ctx.members:
            days_m = [d for d, mid in assignments.items() if mid == m.id]
            res.duty_counts[m.id] = len(days_m)
            res.weekend_counts[m.id] = sum(1 for d in days_m if is_weekend(d))
            res.weekday_counts[m.id] = res.duty_counts[m.id] - res.weekend_counts[m.id]
            res.points_by_person[m.id] = sum(
                day_point(d, ctx.holidays, ctx.params) for d in days_m)
            res.targets[m.id] = round(
                total / n - float(ctx.ledger.get(m.id, 0.0)), 2)

        # 供下月跨月銜接/色塊使用
        weekend_blocks = [b for b in ctx.blocks if b.saturday is not None]
        if weekend_blocks:
            last = weekend_blocks[-1]
            res.last_weekend = {
                "saturday": last.saturday.isoformat(),
                "person": assignments.get(last.days[0], ""),
            }
        return res
    except RuntimeError:
        raise   # ortools 未安裝 → 由 UI 處理
    except Exception:
        logging.exception("[roster.solve] 未預期例外")
        res.status = "error"
        res.diagnosis = ["求解器內部例外，詳見 log。"]
        return res


def _diagnose(ctx: SolveContext, scope: str) -> list:
    """最終無解時的人話診斷：逐一單獨停用可疑規則試解，指出元凶。"""
    out = ["自動放寬到底仍無解。逐一停用規則測試："]
    cp_model = _lazy_cp_model()  # noqa: F841 （確保已安裝，統一錯誤訊息）
    suspects = [(L3_NO_COLOR, "色塊連週")]
    for level, label in suspects:
        try:
            _n, test = _build_and_solve(ctx, scope, level)
            out.append(f"  停用「{label}」→ {'可解' if test else '仍無解'}")
        except Exception:
            out.append(f"  停用「{label}」測試失敗")
    out.append("若仍無解：多半是 請假/指定 彼此衝突，請檢查預檢警告與"
               "當月請假密度。")
    return out
