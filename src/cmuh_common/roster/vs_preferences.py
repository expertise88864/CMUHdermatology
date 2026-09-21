"""Tie breakers after the existing VS objective has reached its optimum."""


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
    preferred = {1: {"R"}, 2: {"S", "T"}, 3: {"L"}}
    weekday = [-x[d, m.id] for d in ctx.days if d.weekday() < 5 and d not in ctx.holidays
               for m in ctx.members if m.id in preferred.get(d.weekday(), set())]
    # Preserve a usable preceding solution if a lower-priority solve times out.
    saved = {key: solver.Value(var) for key, var in x.items()}
    def scores(assignment):
        counts = {m.id: sum(assignment[d, m.id] for d in anchors) for m in ctx.members}
        return (max(counts.values()) - min(counts.values()),
                sum(n * ctx.prev_weekend_counts.get(p, 0) for p, n in counts.items()),
                -sum(assignment[d, m.id] for d in ctx.days if d.weekday() < 5 and d not in ctx.holidays
                     for m in ctx.members if m.id in preferred.get(d.weekday(), set())))
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
