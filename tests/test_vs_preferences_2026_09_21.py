from datetime import date

from ortools.sat.python import cp_model

from cmuh_common.roster.model import Member, SolveContext
from cmuh_common.roster.vs_preferences import refine_vs


def test_weekends_rotate_and_weekday_ties_prefer_requested_doctors():
    ctx = SolveContext("vs", 2026, 10, members=[Member(p, p) for p in "STLRX"]).prepare()
    model = cp_model.CpModel()
    x = {(d, m.id): model.NewBoolVar(f"{d}/{m.id}") for d in ctx.days for m in ctx.members}
    for d in ctx.days:
        model.AddExactlyOne(x[d, m.id] for m in ctx.members)
    for b in ctx.blocks:
        if b.saturday:
            for d in b.days:
                for m in ctx.members:
                    model.Add(x[d, m.id] == x[b.saturday, m.id])
    solver = cp_model.CpSolver()
    status = solver.Solve(model)
    result = refine_vs(model, solver, cp_model, status, [], x, ctx)
    assert result is not None
    for m in ctx.members:
        assert sum(result[d, m.id] for d in ctx.days if d.weekday() == 5) == 1
    for d in ctx.days:
        expected = {1: "R", 3: "L"}.get(d.weekday())
        if expected:
            assert result[d, expected] == 1
        if d.weekday() == 2:
            assert result[d, "S"] + result[d, "T"] == 1


def test_previous_unserved_vs_has_priority_and_primary_is_preserved():
    ctx = SolveContext("vs", 2026, 11, members=[Member(p, p) for p in "STLRX"],
                       prev_weekend_counts={p: 1 for p in "STLR"}).prepare()
    model = cp_model.CpModel()
    x = {(d, m.id): model.NewBoolVar(f"{d}/{m.id}") for d in ctx.days for m in ctx.members}
    for d in ctx.days:
        model.AddExactlyOne(x[d, m.id] for m in ctx.members)
    # Existing higher-priority condition takes precedence over Tuesday R.
    primary = x[date(2026, 11, 3), "X"]
    model.Minimize(-primary)
    solver = cp_model.CpSolver()
    status = solver.Solve(model)
    result = refine_vs(model, solver, cp_model, status, [(primary, -1)], x, ctx)
    assert result[date(2026, 11, 3), "X"] == 1
    assert sum(result[d, "X"] for d in ctx.days if d.weekday() == 5) == 1


def test_feasible_timeout_never_replaces_better_weekend_rotation():
    ctx = SolveContext("vs", 2026, 10, members=[Member(p, p) for p in "ST"]).prepare()
    model = cp_model.CpModel()
    x = {(d, m.id): model.NewBoolVar(f"{d}/{m.id}") for d in ctx.days for m in ctx.members}
    original = {(d, p): int(p == ("S" if d.isocalendar().week % 2 else "T"))
                for d, p in x}
    indices = {v.Index(): key for key, v in x.items()}
    class Timeout:
        expired = False
        def Value(self, expression):
            if isinstance(expression, int):
                return expression
            key = indices[expression.Index()]
            return int(key[1] == "S") if self.expired else original[key]
        def Solve(self, _model):
            self.expired = True
            return cp_model.FEASIBLE
    assert refine_vs(model, Timeout(), cp_model, cp_model.OPTIMAL, [], x, ctx) == original
