"""Tie breakers after the existing VS objective has reached its optimum."""


# [2026-11-24 使用者] 三線平日順序(0=週一;前面的優先)。五六日不設,按點數平衡。
#   只在既有目標(請假等硬規則、點數、週末輪替)都已最佳的排法之間挑。
PREFERRED = {0: ("T", "F"), 1: ("R",), 2: ("S",), 3: ("L", "U")}
# 第一順位 3 分、第二順位 2 分:有偏好的人值 > 沒有;兩人都可以時取前者。
_RANK_POINTS = (3, 2)


def _weekday_points(d, member_id, holidays) -> int:
    order = PREFERRED.get(d.weekday(), ())
    if d in holidays or member_id not in order:
        return 0
    return _RANK_POINTS[order.index(member_id)]


def refine_vs(model, solver, cp_model, status, objective, x, ctx):
    if ctx.scope != "vs" or status != cp_model.OPTIMAL or not ctx.members:
        return None
    primary = sum(var * weight for var, weight in objective)
    model.Add(primary == solver.Value(primary))
    anchors = sorted({b.saturday or b.days[0] for b in ctx.blocks
                      if b.saturday is not None or any(d in ctx.holidays for d in b.days)})
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
    # Preserve a usable preceding solution if a lower-priority solve times out.
    saved = {key: solver.Value(var) for key, var in x.items()}
    def scores(assignment):
        counts = {m.id: sum(assignment[d, m.id] for d in anchors) for m in ctx.members}
        return (max(counts.values()) - min(counts.values()),
                sum(n * ctx.prev_weekend_counts.get(p, 0) for p, n in counts.items()),
                -sum(v * assignment[key] for key, v in points.items()))
    for index, expression in enumerate((maximum - minimum, sum(history), sum(weekday))):
        if isinstance(expression, int):
            continue
        model.Add(expression <= scores(saved)[index])
        model.Minimize(expression)
        next_status = solver.Solve(model)
        if next_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return saved
        candidate = {key: solver.Value(var) for key, var in x.items()}
        if scores(candidate)[:index + 1] <= scores(saved)[:index + 1]:
            saved = candidate
        if next_status != cp_model.OPTIMAL:
            return saved
        model.Add(expression == solver.Value(expression))
    return saved
