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


def _build_and_solve(ctx: SolveContext, scope: str, level: int):
    """在指定放寬層級建模求解 → (cp_status_name, assignments|None)。"""
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
        # ★指紋在【求解之前】就記下來★:這一批結果的來源是此刻這份輸入。
        #   放在 return 之前的話,求解過程若動到 ctx 就會記成別的東西。
        res.input_fingerprint = rvs_input_fingerprint(ctx)
        res.prechecks = run_prechecks(ctx, scope)
        if any(c.severity == "error" for c in res.prechecks):
            res.status = "precheck_failed"
            return res

        auto_levels = [L0_FULL, L1_NO_RANGE, L2_RESERVED]
        rules = rules_for(scope)

        chosen = None
        prev_active = None
        # ★[2026-08-02 補審] 求解器「沒算完」不等於「無解」★
        #   CP-SAT 逾時回 UNKNOWN、模型異常回 MODEL_INVALID,兩者都不是 INFEASIBLE。
        #   原本一律當成無解,診斷還會斷言「停用色塊連週 → 仍無解(與色塊無關)」——
        #   那是程式沒有驗證過的推斷。使用者會照著去翻請假/指定找一個不存在的衝突,
        #   而真正該做的只是重試。(措辭鐵律:只陳述程式確知的事;solve_day 已經
        #   為同一件事被外審抓過兩輪。)
        #   ★而 UNKNOWN 與 MODEL_INVALID 也不可混為一談★(第 2 輪外審):
        #   逾時叫使用者「稍後重試」是對的,模型異常重試一百次也一樣 —— 那是程式的
        #   臭蟲,要叫他回報、看 log。把兩者說成同一件事,又是一次「宣稱不確知的事」。
        timed_out: list = []      # UNKNOWN：沒算完
        broken: list = []         # MODEL_INVALID 等：模型/求解器本身有問題
        for level in auto_levels:
            # [OPT-1] 該層 active 規則集與前一層相同（如 VS 無 duty_range、或 L2
            # 保留級）→ 必得同解，跳過避免重複求解。
            active = frozenset(r.rule_id for r in rules if r.active_at(level))
            if active == prev_active:
                logging.info("[roster.solve] %s %04d-%02d 跳過 %s（規則集同前層）",
                             scope, ctx.year, ctx.month, _LEVEL_NAMES[level])
                continue
            prev_active = active
            name, assignments = _build_and_solve(ctx, scope, level)
            logging.info("[roster.solve] %s %04d-%02d %s → %s",
                         scope, ctx.year, ctx.month, _LEVEL_NAMES[level], name)
            if assignments is not None:
                chosen = (level, assignments)
                break
            if name == "UNKNOWN":
                timed_out.append(_LEVEL_NAMES[level])
            elif name != "INFEASIBLE":
                broken.append(f"{_LEVEL_NAMES[level]}（{name}）")

        if chosen is None:
            # [OPT-3] 自動層級全無解 → 測「停用色塊連週」恰一次，結果同時決定
            # need_confirm/採用/診斷（不再於 _diagnose 重測一次）。
            l3_name, l3 = _build_and_solve(ctx, scope, L3_NO_COLOR)
            if l3 is None and l3_name == "UNKNOWN":
                timed_out.append(_LEVEL_NAMES[L3_NO_COLOR])
            elif l3 is None and l3_name != "INFEASIBLE":
                broken.append(f"{_LEVEL_NAMES[L3_NO_COLOR]}（{l3_name}）")
            if l3 is not None and allow_disable_color:
                chosen = (L3_NO_COLOR, l3)                # 已獲授權 → 直接採用
            elif l3 is not None:
                res.status = "need_confirm_color"
                # ★不可宣稱「不動色塊就無解」★ 自動層級若只是沒算完,那句話就是
                #   程式沒驗證過的推斷(外審第 1 輪抓到)。
                unresolved = timed_out + broken
                head = ("在不動色塊連週規則的前提下無解；停用色塊規則後可解。"
                        if not unresolved else
                        "自動層級中有層級未得出結論（"
                        + "、".join(unresolved)
                        + "），並【未】證明不動色塊就無解；停用色塊後可解。")
                res.diagnosis = [head, "請確認是否放寬（將出現同色連週值班）。"]
                return res
            elif broken:
                # 模型異常不是逾時,重試沒有用；這是程式的問題,要能被回報。
                res.status = "error"
                res.diagnosis = [
                    "求解器回報模型異常——這【不是】「無解」，也不是逾時。",
                    "  異常層級：" + "、".join(broken),
                    "  重試不會有幫助；請回報此訊息並附上 automation_ui.log。",
                    "★請勿據此去調整請假或指定★——程式並沒有判定它們有衝突。"]
                return res
            elif timed_out:
                # 只要有任何一層是「沒算完」,就不可以說無解——連色塊那條路都還沒
                # 排除,使用者甚至沒被問到要不要放寬色塊。
                res.status = "timeout"
                res.diagnosis = [
                    "求解器在時限內沒有得出結論——這【不是】「無解」，是還沒算完。",
                    "  未得出結論的層級：" + "、".join(timed_out),
                    f"  時限 {SOLVE_TIMEOUT_SEC:.0f} 秒；本規模（≤31 天 × ≤10 人）"
                    f"正常應在 1 秒內完成，多半是機器當下負載過重。",
                    "  請稍後再按一次自動排班。",
                    "★請勿據此去調整請假或指定★——程式並沒有判定它們有衝突。"]
                return res
            else:
                res.status = "infeasible"
                res.diagnosis = _diagnose(ctx, scope, l3_solvable=False)
                return res

        level, assignments = chosen
        if timed_out or broken:
            # ★較嚴格的層級只是沒算完,不代表它無解★ 報告會寫「有規則被放寬」,
            #   讀起來像是「嚴格規則滿足不了」——那件事程式並沒有證明。
            res.diagnosis = ["以下層級未得出結論（非「無解」）："
                             + "、".join(timed_out + broken)]
            if timed_out:
                res.diagnosis.append(
                    "  求解器沒算完；重按一次自動排班有機會拿到更嚴格層級的結果。")
            if broken:
                res.diagnosis.append(
                    "  其中有模型異常，重試無用；請回報並附上 automation_ui.log。")
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


def _diagnose(ctx: SolveContext, scope: str, l3_solvable=None) -> list:
    """最終無解時的人話診斷。

    l3_solvable: 呼叫端已測過「停用色塊連週」是否可解（True/False）→ 直接引用，
    不重測；None（向後相容）→ 自行測一次。另列出「僅剩 1 人可值」的緊繃日，
    幫使用者定位是哪些請假密集的日子卡住。
    """
    out = ["自動放寬到底仍無解。診斷："]
    if l3_solvable is None:
        try:
            _n, test = _build_and_solve(ctx, scope, L3_NO_COLOR)
            l3_solvable = test is not None
        except Exception:
            out.append("  停用「色塊連週」測試失敗")
    if l3_solvable is True:
        out.append("  停用「色塊連週」→ 可解（元凶多為色塊連週太緊）")
    elif l3_solvable is False:
        out.append("  停用「色塊連週」→ 仍無解（與色塊無關）")

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
