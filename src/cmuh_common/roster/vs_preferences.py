"""Tie breakers after the existing VS objective has reached its optimum."""
from fractions import Fraction
from math import lcm

from .weekend_share import counted_blocks, vs_targets


# [2026-11-24 使用者] 三線平日順序(0=週一;前面的優先)。五六日不設,按點數平衡。
#   最後一層:只在前面各層(見 refine_vs)都已最佳的排法之間挑。
PREFERRED = {0: ("T", "F"), 1: ("R",), 2: ("S",), 3: ("L", "U")}
# 第一順位 3 分、第二順位 2 分:有偏好的人值 > 沒有;兩人都可以時取前者。
_RANK_POINTS = (3, 2)


def _weekday_points(d, member_id, holidays) -> int:
    order = PREFERRED.get(d.weekday(), ())
    if d in holidays or member_id not in order:
        return 0
    return _RANK_POINTS[order.index(member_id)]


def _cumulative_deviation(model, x, ctx):
    """[2026-11-24 使用者] 年度週末:(已值＋本月 − 目標) 平方和 → (算式, 計分函式)。

    用平方:少兩次的人要比少一次的人先補(絕對值會把兩者看成一樣好)。"""
    blocks = counted_blocks(ctx.blocks, ctx.weekend_exempt, ctx.holidays)
    targets = vs_targets(ctx, len(blocks))
    if not targets or not blocks:
        return None
    # (已值＋n−目標)² 去掉與 n 無關的常數,再乘公分母 → 精確整數,不因捨入變成同分
    gaps = {p: int(ctx.weekend_prior.get(p, 0)) - Fraction(t).limit_denominator(100_000)
            for p, t in targets.items()}
    scale = lcm(*(g.denominator for g in gaps.values()))
    costs = {p: [int(scale * (n * n + 2 * n * g)) for n in range(len(blocks) + 1)]
             for p, g in gaps.items()}
    terms = []
    for p, table in costs.items():
        count = model.NewIntVar(0, len(blocks), f"vs_year_n_{p}")
        model.Add(count == sum(x[b.saturday, p] for b in blocks))
        cost = model.NewIntVar(min(table), max(table), f"vs_year_{p}")
        model.AddElement(count, table, cost)
        terms.append(cost)
    return sum(terms), lambda a: sum(
        table[sum(a[b.saturday, p] for b in blocks)] for p, table in costs.items())


#: [2026-11-28 使用者] 為了週末排得更平均,點數偏差總和最多可以比最佳多 2 點
#: (scale 100 → 200);差額照常進帳本,之後的月份再還。
POINT_SLACK = 200


def _point_terms(mc, ctx):
    """點數偏差(與 PointBalanceRule 同一套) → (算式, 計分函式);一人 → None。"""
    from .model import day_point  # noqa: PLC0415
    from .rules import PointBalanceRule  # noqa: PLC0415
    terms = PointBalanceRule().objective_terms(mc, ctx)
    if not terms:
        return None
    total = ctx.total_points()
    n = len(ctx.members)
    targets = {m.id: round(100 * (total / n - float(ctx.ledger.get(m.id, 0.0))))
               for m in ctx.members}
    pts = {d: day_point(d, ctx.holidays, ctx.params) * 100 for d in ctx.days}

    def score(a):
        return sum(abs(sum(pts[d] * a[d, m.id] for d in ctx.days) - targets[m.id])
                   for m in ctx.members)
    return sum(var for var, _w in terms), score


def _long_run_terms(mc, ctx):
    """連值 4、5 天的懲罰(與 ConsecutiveDutyRule 同權重) → (算式, 計分函式)。"""
    from .rules import ConsecutiveDutyRule  # noqa: PLC0415
    rule = ConsecutiveDutyRule()
    floor = min(rule.RUN4_WEIGHT, rule.RUN5_WEIGHT)
    terms = [(var, w) for var, w in rule.objective_terms(mc, ctx) if w >= floor]
    weight = {4: rule.RUN4_WEIGHT, 5: rule.RUN5_WEIGHT}
    in_month = set(ctx.days)
    windows = [(win, length) for win, length in rule._windows(ctx) if length >= 4]

    def score(a):
        total = 0
        for m in ctx.members:
            for win, length in windows:
                days = [d for d in win if d in in_month]
                if not days or any(ctx.prev_tail.get(d) != m.id
                                   for d in win if d not in in_month):
                    continue
                if all(a[d, m.id] for d in days):
                    total += weight[length]
        return total
    return sum(var * w for var, w in terms), score


def refine_vs(model, solver, cp_model, status, objective, x, ctx):
    """VS 主目標解完之後,依使用者定的順序逐層挑(2026-11-28 使用者定案):

    連值 4、5 天不變差 → 年度週末累計最接近目標 → 本月週末最平均 → 點數最平
    (整段點數偏差總和最多比原本多 POINT_SLACK)→ 其餘原目標(班數平均、連值 3 天)
    → 平日偏好。週末一律只算週六＋週日(春節等不計入的週末段不算)。"""
    if ctx.scope != "vs" or status != cp_model.OPTIMAL or not ctx.members:
        return None
    from .solve_rvs import _ModelCtx  # noqa: PLC0415
    mc = _ModelCtx(model, x)
    primary = sum(var * weight for var, weight in objective)
    anchors = [b.saturday for b in counted_blocks(ctx.blocks, ctx.weekend_exempt,
                                                  ctx.holidays)]
    maximum = model.NewIntVar(0, len(anchors), "vs_weekend_max")
    minimum = model.NewIntVar(0, len(anchors), "vs_weekend_min")
    history = []
    for m in ctx.members:
        count = sum(x[d, m.id] for d in anchors)
        model.Add(maximum >= count)
        model.Add(minimum <= count)
        history.append(count * ctx.prev_weekend_counts.get(m.id, 0))
    points = {(d, m.id): v for d in ctx.days for m in ctx.members
              if (v := _weekday_points(d, m.id, ctx.holidays))}
    weekday = [-v * x[key] for key, v in points.items()]
    cumulative = _cumulative_deviation(model, x, ctx)
    balance = _point_terms(mc, ctx)
    long_runs = _long_run_terms(mc, ctx)
    # Preserve a usable preceding solution if a lower-priority solve times out.
    saved = {key: solver.Value(var) for key, var in x.items()}

    def counts(a):
        return {m.id: sum(a[d, m.id] for d in anchors) for m in ctx.members}

    def year(a):
        if cumulative:
            return cumulative[1](a)
        return sum(n * ctx.prev_weekend_counts.get(p, 0) for p, n in counts(a).items())

    def spread(a):
        c = counts(a).values()
        return max(c) - min(c)

    weekend = [(year, cumulative[0] if cumulative else sum(history)),
               (spread, maximum - minimum)]
    if not cumulative:
        # 沒有年度資料(正式排班一律有)時沿用舊順序:先本月平均,再看上月誰值過。
        weekend.reverse()
    scorers = [long_runs[1], weekend[0][0], weekend[1][0],
               balance[1] if balance else (lambda _a: 0),
               None,                                      # 其餘原目標:只收最佳解
               lambda a: -sum(v * a[key] for key, v in points.items())]
    expressions = [long_runs[0], weekend[0][1], weekend[1][1],
                   balance[0] if balance else 0, primary, sum(weekday)]
    if balance:
        model.Add(balance[0] <= balance[1](saved) + POINT_SLACK)

    def better(candidate, upto):
        for score in scorers[:upto + 1]:
            if score is None:
                continue
            a, b = score(candidate), score(saved)
            if a != b:
                return a < b
        return True

    for index, expression in enumerate(expressions):
        if isinstance(expression, int):
            continue
        scorer = scorers[index]
        if scorer is not None:
            model.Add(expression <= scorer(saved))
        model.Minimize(expression)
        for key, var in x.items():
            model.AddHint(var, saved[key])
        next_status = solver.Solve(model)
        if next_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return saved
        candidate = {key: solver.Value(var) for key, var in x.items()}
        if scorer is None:
            if next_status != cp_model.OPTIMAL:
                return saved
            saved = candidate
        elif better(candidate, index):
            saved = candidate
        if next_status != cp_model.OPTIMAL:
            return saved
        model.Add(expression == solver.Value(expression))
        model.ClearHints()
    return saved
