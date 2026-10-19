# -*- coding: utf-8 -*-
"""R/VS 值班求解器：CP-SAT + 放寬階梯（設計文件 §6）。

流程：
    1. run_prechecks — 任何 error → 不求解，回 precheck_failed（人話清單）。
    2. 全部規則求解。無解時【不自動放寬】（2026-10-19 使用者定案）：
       使用者已同意的放寬(allow_relax)先試；仍無解 → 找出「最少要放寬哪幾條
       可放寬規則（9–11 班 / 每月 1–2 個週末 / 色塊連週）才排得出來」，回
       need_confirm_relax（只有色塊時沿用 need_confirm_color）由 UI 跳窗確認，
       使用者按確認後帶著 allow_relax 重呼叫。全部放寬仍無解 → infeasible + 診斷。
    3. 成功 → R 再做「年度週末比例」第二段（固定第一段的目標值，只在一樣好的
       排法之間挑）→ 回 assignments / 點數結算 / 每格理由 / last_weekend。

決定性：random_seed 固定 + num_search_workers=1 + ortools 釘版
（cmuh_common.roster.ORTOOLS_PINNED_VERSION）→ 同輸入同輸出。

ortools 為重依賴：lazy import，未安裝時丟 RuntimeError 由 UI 引導安裝。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import combinations
from typing import Optional

from datetime import timedelta

from cmuh_common.roster.model import (
    SolveContext, day_point, is_weekend,
)
from cmuh_common.roster.rules import (
    RELAX_NAMES, RELAX_ORDER, collect_directives, rules_for, run_prechecks,
)
from cmuh_common.roster.weekend_share import counted_blocks, cumulative_targets


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

def relax_label(relaxed) -> str:
    """放寬組合的人話名稱（診斷/報告用）。"""
    if not relaxed:
        return "全部規則"
    names = [RELAX_NAMES.get(r, r) for r in RELAX_ORDER if r in relaxed]
    return "放寬「" + "、".join(names) + "」"

SOLVE_TIMEOUT_SEC = 20.0   # 問題極小(≤31天×≤10人)，正常 <1s；此為防呆上限
_RANDOM_SEED = 20260702


class _ModelCtx:
    """包住 cp_model 與變數，供規則 apply 使用。"""

    def __init__(self, model, x):
        self.model = model
        self.x = x  # {(date, member_id): BoolVar}


@dataclass
class SolveResult:
    status: str                       # ok / precheck_failed / need_confirm_relax / need_confirm_color / infeasible / timeout / error
    scope: str = ""
    #: 0＝全部規則；1＝有(經使用者確認的)放寬。舊版的 L1–L3 數字已不再使用。
    level_used: Optional[int] = None
    level_name: str = ""
    #: 實際被放寬的規則（ok 時）；需要確認時是【提議】要放寬的規則。
    relaxed: list = field(default_factory=list)
    relax_proposal: list = field(default_factory=list)
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
    #: ★這一批結果是照【哪一份輸入】算出來的★(外審排班第 2 輪 P1-04)。
    #: 套用時重建 ctx 再比一次:不同就拒絕。空字串＝來源不明,一律拒絕
    #: (沒有指紋就無從確認,而「無從確認」不可以當成「沒問題」)。
    input_fingerprint: str = ""
    #: ★這份解是從【哪一版月檔】算出來的★(RS-13,全審次輪 P1-01;與
    #: `DaySolveResult.month_revision` 對稱)。指紋只涵蓋 SolveContext 的
    #: 輸入 —— 「未鎖定格現在排誰」不是 solver 輸入,他機在預覽期間的手動
    #: 修改對指紋隱形,套用時整份重建 `{scope}_duty` 會把它靜默退回。
    #: 判準保守:月檔【任何】變動都拒(不猜哪些欄位無害,寧可重排)。
    #: ★哨兵是 None 不是空字串★:`""` 是「月檔還不存在」的【合法】身分
    #: (首次排一個全新月份,`load_month_snapshot` 缺檔回 `({}, "")`,CAS 也
    #: 認它)—— 拿它當「來源不明」用,新月份就永遠套用不了。None=物件沒被
    #: 求解器蓋過章,一律拒絕(與 input_fingerprint 的空字串同規)。
    month_revision: "str | None" = None


def _lazy_cp_model():
    try:
        from ortools.sat.python import cp_model  # noqa: PLC0415
        return cp_model
    except ImportError as e:
        raise RuntimeError(
            "未安裝 ortools（自動排班引擎）。請按 UI 提示安裝後重試。") from e


def _build_and_solve(ctx: SolveContext, scope: str, relaxed=frozenset()):
    """在指定放寬組合下建模求解 → (cp_status_name, assignments|None)。

    relaxed: 使用者同意放寬的 rule_id 集合（空＝全部規則）。"""
    cp_model = _lazy_cp_model()
    model = cp_model.CpModel()
    x = {(d, m.id): model.NewBoolVar(f"x_{d.isoformat()}_{m.id}")
         for d in ctx.days for m in ctx.members}
    mc = _ModelCtx(model, x)

    _cut = ctx.past_cutoff
    for d in ctx.days:
        # ★[RS-32 2026-08-30 使用者] 今天(含)以前是【事實】不是排班對象★
        #   釘成 past_duty 的內容:有人就是那個人、空的就是空的(不套
        #   「每日恰一人」—— 過去沒排到就是沒排到,不能回頭補)。
        #   與「每日恰一人」同層(核心,不屬任何可放寬層級):就算放寬到 L3,
        #   求解器也不可以改寫歷史。人選已不在名單的過去日 → 全 0
        #   (歷史由 solve_duty 的輸出合併原樣保留,見該處註解)。
        if _cut is not None and d <= _cut:
            _mid = ctx.past_duty.get(d)
            for m in ctx.members:
                model.Add(  # pyright: ignore[reportAttributeAccessIssue]
                    x[(d, m.id)] == (1 if m.id == _mid else 0))
            continue
        # 每日恰一人（核心，不屬任何可放寬規則）
        model.AddExactlyOne(x[(d, m.id)] for m in ctx.members)

    objective = []
    for rule in rules_for(scope):
        if not rule.active(relaxed):
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
        from .vs_preferences import refine_vs
        refined = (refine_vs(model, solver, cp_model, status, objective, x, ctx)
                   if scope == "vs" else
                   _refine_weekend_share(model, solver, cp_model, objective, x, ctx))
        out = {}
        for d in ctx.days:
            for m in ctx.members:
                if (refined[(d, m.id)] if refined is not None else solver.Value(x[(d, m.id)])):
                    out[d] = m.id
                    break
        # ★[RS-32] 過去的實況原樣併回輸出★:accept 是拿 assignments 整份重建
        #   `{scope}_duty` 的 —— 不在 assignments 裡的日子會被【刪掉】。
        #   人選已不在名單的過去日(離職/改代號)沒有變數可釘,在這裡補回,
        #   歷史才不會因為按了一次自動排班而消失。
        if _cut is not None:
            for d, mid in ctx.past_duty.items():
                if d in set(ctx.days) and d <= _cut and mid:
                    out[d] = mid
        return name, out
    return name, None


def _share_weights(ctx: SolveContext) -> dict:
    return {m.id: float(ctx.weekend_weights[m.id]) for m in ctx.members
            if float(ctx.weekend_weights.get(m.id) or 0) > 0}


def weekend_share_score(ctx: SolveContext, counts: dict) -> int:
    """年度週末偏差 Σ|100×(本週期之前+本月) − 100×目標|（越小越接近目標）。"""
    blocks = counted_blocks(ctx.blocks, ctx.weekend_exempt, ctx.holidays)
    weights = _share_weights(ctx)
    targets = cumulative_targets(ctx.weekend_prior, weights, len(blocks))
    return sum(abs(100 * (int(ctx.weekend_prior.get(mid, 0)) + counts.get(mid, 0))
                   - round(100 * targets[mid])) for mid in weights)


def _refine_weekend_share(model, solver, cp_model, objective, x, ctx):
    """[2026-10-19 使用者] R 年度週末比例 —— 嚴格版第二段。

    第一段(全部規則＋點數＋其餘軟規則)的目標值固定成上限,只在【一樣好或更好】
    的排法之間挑「本週期累計週末次數最接近權重目標」的那一個。不犧牲任何既有
    規則或點數公平;沒有一樣好的替代排法時,結果與第一段完全相同。
    第二段沒有算出更好的結果(逾時等) → 沿用第一段的解。"""
    blocks = counted_blocks(ctx.blocks, ctx.weekend_exempt, ctx.holidays)
    weights = _share_weights(ctx)
    if len(weights) < 2 or not blocks:
        return None
    saved = {key: solver.Value(var) for key, var in x.items()}

    def counts_of(assign) -> dict:
        return {mid: sum(assign[(b.saturday, mid)] for b in blocks)
                for mid in weights}

    if objective:
        primary = sum(var * w for var, w in objective)
        model.Add(primary <= solver.Value(primary))
    targets = cumulative_targets(ctx.weekend_prior, weights, len(blocks))
    devs = []
    for mid in weights:
        prior = int(ctx.weekend_prior.get(mid, 0))
        tgt = round(100 * targets[mid])
        cnt = sum(x[(b.saturday, mid)] for b in blocks)
        bound = 100 * (prior + len(blocks)) + abs(tgt) + 1
        dev = model.NewIntVar(0, bound, f"wkshare_{mid}")
        model.AddAbsEquality(dev, 100 * prior + 100 * cnt - tgt)
        devs.append(dev)
    model.Minimize(sum(devs))
    for key, var in x.items():
        model.AddHint(var, saved[key])
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return saved
    candidate = {key: solver.Value(var) for key, var in x.items()}
    if (weekend_share_score(ctx, counts_of(candidate))
            <= weekend_share_score(ctx, counts_of(saved))):
        return candidate
    return saved


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
        if ctx.past_cutoff is not None and d <= ctx.past_cutoff:
            out[d] = "今天以前(保留)"
            continue
        if d in directives:
            out[d] = directives[d][1]
        elif scope == "r" and fixed_days.get(d) == mid:
            out[d] = "固定週幾"
        elif d in in_block:
            out[d] = "假日成對"
        else:
            out[d] = "點數平衡"
    return out


def rvs_input_fingerprint(ctx: SolveContext) -> str:
    """R/VS 求解【吃到的全部輸入】的識別(見 `roster.fingerprint`)。

    ★取指紋的時機必須與套用時重建的那一刻對齊★:`prepare()` 與
    `apply_boundary_from_prev()` 之後 —— `build_context` 就是做完這兩步才
    回傳的,兩邊的階段不一樣就永遠不會相等(於是守衛變成「永遠說過期」)。
    """
    from cmuh_common.roster.fingerprint import input_fingerprint  # noqa: PLC0415
    return input_fingerprint(ctx)


#: 確認視窗裡「放寬之後會怎樣」的說明。
_RELAX_EFFECT = {
    "weekend_color": "將出現同色連週值班",
    "duty_range": "可能有人班數少於 9 或多於 11 班；點數差額照常進帳本、之後再還",
    "weekend_range": "可能有人本月 0 個或 3 個以上週末",
}


def solve_duty(ctx: SolveContext, allow_disable_color: bool = False, *,
               allow_relax=()) -> SolveResult:
    """主入口。ctx 需已 prepare()；scope 取 ctx.scope（"r"/"vs"）。

    allow_relax: 使用者【確認過】可以放寬的 rule_id（見 rules.RELAX_ORDER）。
    allow_disable_color=True 等同 allow_relax 含 "weekend_color"（舊介面）。"""
    scope = ctx.scope
    res = SolveResult(status="error", scope=scope)
    try:
        if not ctx.days:
            ctx.prepare()
        # [codex P2] 跨月銜接在此自動套用：呼叫端只需設 prev_last_weekend,
        # 不必記得另呼叫 helper（重複呼叫等冪,已設同值無害）。
        apply_boundary_from_prev(ctx)
        # ★指紋在【求解之前】就記下來★:這一批結果的來源是此刻這份輸入。
        #   放在 return 之前的話,求解過程若動到 ctx 就會記成別的東西。
        res.input_fingerprint = rvs_input_fingerprint(ctx)
        res.prechecks = run_prechecks(ctx, scope)
        if any(c.severity == "error" for c in res.prechecks):
            res.status = "precheck_failed"
            return res

        rules = rules_for(scope)
        relaxable = [rid for rid in RELAX_ORDER
                     if any(r.rule_id == rid and r.relaxable for r in rules)]
        wanted: set = set(allow_relax or ())
        if allow_disable_color:
            wanted.add("weekend_color")
        allowed = frozenset(r for r in wanted if r in relaxable)

        # ★[2026-08-02 補審] 求解器「沒算完」不等於「無解」★
        #   CP-SAT 逾時回 UNKNOWN、模型異常回 MODEL_INVALID,兩者都不是 INFEASIBLE。
        #   (措辭鐵律:只陳述程式確知的事;solve_day 已經為同一件事被外審抓過兩輪。)
        #   逾時叫使用者「稍後重試」是對的,模型異常重試一百次也一樣 —— 那是程式的
        #   臭蟲,要叫他回報、看 log。
        timed_out: list = []      # UNKNOWN：沒算完
        broken: list = []         # MODEL_INVALID 等：模型/求解器本身有問題
        tried: set = set()

        def attempt(relaxed: frozenset):
            tried.add(relaxed)
            name, got = _build_and_solve(ctx, scope, relaxed)
            logging.info("[roster.solve] %s %04d-%02d %s → %s",
                         scope, ctx.year, ctx.month, relax_label(relaxed), name)
            if got is None:
                if name == "UNKNOWN":
                    timed_out.append(relax_label(relaxed))
                elif name != "INFEASIBLE":
                    broken.append(f"{relax_label(relaxed)}（{name}）")
            return got

        chosen = None
        strict = attempt(frozenset())
        if strict is not None:
            chosen = (frozenset(), strict)
        elif allowed:
            # 使用者已確認過的放寬(上一輪跳窗按了確認)。
            got = attempt(allowed)
            if got is not None:
                chosen = (allowed, got)

        if chosen is None:
            # ★不自動放寬★:找出「最少要放寬哪幾條」才排得出來,交給使用者確認。
            proposal = None
            for size in range(1, len(relaxable) + 1):
                for combo in combinations(relaxable, size):
                    cs = frozenset(combo)
                    if cs in tried:
                        continue
                    if attempt(cs) is not None:
                        proposal = cs
                        break
                if proposal is not None:
                    break
            if proposal is not None:
                res.relax_proposal = [r for r in RELAX_ORDER if r in proposal]
                res.status = ("need_confirm_color" if proposal == {"weekend_color"}
                              else "need_confirm_relax")
                # ★不可宣稱「不放寬就無解」★:較嚴格的組合若只是沒算完,
                #   那句話就是程式沒驗證過的推斷(外審第 1 輪抓到)。
                unresolved = timed_out + broken
                what = "、".join(RELAX_NAMES[r] for r in res.relax_proposal)
                head = (f"在不放寬規則的前提下無解；放寬「{what}」後可解。"
                        if not unresolved else
                        "較嚴格的組合中有未得出結論者（"
                        + "、".join(unresolved)
                        + f"），並【未】證明不放寬就無解；放寬「{what}」後可解。")
                res.diagnosis = [head] + [
                    f"  放寬「{RELAX_NAMES[r]}」：{_RELAX_EFFECT[r]}"
                    for r in res.relax_proposal] + ["請確認是否放寬。"]
                return res
            if broken:
                # 模型異常不是逾時,重試沒有用；這是程式的問題,要能被回報。
                res.status = "error"
                res.diagnosis = [
                    "求解器回報模型異常——這【不是】「無解」，也不是逾時。",
                    "  異常組合：" + "、".join(broken),
                    "  重試不會有幫助；請回報此訊息並附上 automation_ui.log。",
                    "★請勿據此去調整請假或指定★——程式並沒有判定它們有衝突。"]
                return res
            if timed_out:
                # 只要有任何一個組合是「沒算完」,就不可以說無解。
                res.status = "timeout"
                res.diagnosis = [
                    "求解器在時限內沒有得出結論——這【不是】「無解」，是還沒算完。",
                    "  未得出結論的組合：" + "、".join(timed_out),
                    f"  時限 {SOLVE_TIMEOUT_SEC:.0f} 秒；本規模（≤31 天 × ≤10 人）"
                    f"正常應在 1 秒內完成，多半是機器當下負載過重。",
                    "  請稍後再按一次自動排班。",
                    "★請勿據此去調整請假或指定★——程式並沒有判定它們有衝突。"]
                return res
            res.status = "infeasible"
            res.diagnosis = _diagnose(ctx, scope, relaxable)
            return res

        relaxed, assignments = chosen
        if timed_out or broken:
            # ★較嚴格的組合只是沒算完,不代表它無解★
            res.diagnosis = ["以下組合未得出結論（非「無解」）："
                             + "、".join(timed_out + broken)]
            if timed_out:
                res.diagnosis.append(
                    "  求解器沒算完；重按一次自動排班有機會拿到更嚴格的結果。")
            if broken:
                res.diagnosis.append(
                    "  其中有模型異常，重試無用；請回報並附上 automation_ui.log。")
        level = 1 if relaxed else 0
        res.relaxed = [r for r in RELAX_ORDER if r in relaxed]
        res.status = "ok"
        res.level_used = level
        res.level_name = ((relax_label(relaxed) + "（經你確認）") if relaxed
                          else "全部規則")
        res.assignments = assignments
        res.reasons = _reasons_for(ctx, scope, assignments)

        total = ctx.total_points()
        n = max(1, len(ctx.members))
        for m in ctx.members:
            days_m = [d for d, mid in assignments.items() if mid == m.id]
            res.duty_counts[m.id] = len(days_m)
            # [RS-02] 平日的國定假日也算「假日班」(月曆標假日、點數也以假日計),
            # 須與 export_common.member_tally 的 we 欄一致(否則報告/UI 三處矛盾)。
            res.weekend_counts[m.id] = sum(
                1 for d in days_m if is_weekend(d) or d in ctx.holidays)
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
                # 取【週六】當天的人：連休段可能往前鏈入週五假日（days[0]=週五），
                # 且 [2026-07-27] 起使用者指定可拆段 → days[0] 未必是值週末的人。
                "person": assignments.get(last.saturday, ""),
            }
        return res
    except RuntimeError:
        raise   # ortools 未安裝 → 由 UI 處理
    except Exception:
        logging.exception("[roster.solve] 未預期例外")
        res.status = "error"
        res.diagnosis = ["求解器內部例外，詳見 log。"]
        return res


def _diagnose(ctx: SolveContext, scope: str, relaxable=()) -> list:
    """最終無解時的人話診斷。呼叫端已實際測過「放寬全部可放寬規則」仍無解。

    另列出「僅剩 1 人可值」的緊繃日，幫使用者定位是哪些請假密集的日子卡住。
    """
    out = ["放寬到底仍無解。診斷："]
    if relaxable:
        names = "、".join(RELAX_NAMES[r] for r in relaxable)
        out.append(f"  放寬全部可放寬規則（{names}）→ 仍無解（與這些規則無關）")

    tight = [f"{d.month}/{d.day}→僅 {elig[0]}"
             for d in ctx.days
             for elig in [[m.id for m in ctx.members if not ctx.on_leave(m.id, d)]]
             if len(elig) == 1]
    if tight:
        out.append("  僅 1 人可值（請假密集）: " + "、".join(tight[:10])
                   + ("…" if len(tight) > 10 else ""))
    out.append("若仍無解：多半是 請假/指定 彼此衝突，請檢查預檢警告與"
               "當月請假密度。")
    return out
