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
        expected = {0: "T", 1: "R", 2: "S", 3: "L"}.get(d.weekday())
        if expected:
            assert result[d, expected] == 1


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


def _solve(month, holidays=(), leaves=None, ledger=None, ids="DJRSLTFU"):
    from cmuh_common.roster.calendar_colors import week_colors_for_year
    from cmuh_common.roster.solve_rvs import solve_duty
    ctx = SolveContext("vs", 2026, month, members=[Member(p, p) for p in ids],
                       holidays=set(holidays), leaves=leaves or {}, ledger=ledger or {},
                       week_colors=week_colors_for_year(2026)).prepare()
    return ctx, solve_duty(ctx)


def test_weekday_order_2026_11_24(monkeypatch):
    """[2026-11-24 使用者] 一 T>F、二 R、三 S、四 L>U;點數平衡不變、請假照避。"""
    from cmuh_common.roster import vs_preferences as vp
    leaves = {"T": {date(2026, 12, 7)}, "L": {date(2026, 12, 10)}}
    ctx, res = _solve(12, {date(2026, 12, 25)}, leaves)
    assert res.status == "ok"
    for wd, order in vp.PREFERRED.items():
        days = [d for d in ctx.days if d.weekday() == wd]
        hit = sum(res.assignments[d] in order for d in days)
        assert hit == len(days), (wd, [res.assignments[d] for d in days])
    assert res.assignments[date(2026, 12, 7)] == "F"         # T 請假 → F
    assert res.assignments[date(2026, 12, 10)] == "U"        # L 請假 → U
    monkeypatch.setattr(vp, "PREFERRED", {})
    _ctx, plain = _solve(12, {date(2026, 12, 25)}, leaves)
    def spread(r):
        return sum(abs(r.points_by_person[p] - r.targets[p]) for p in "DJRSLTFU")
    assert abs(spread(res) - spread(plain)) < 1e-6


def test_first_choice_beats_second_choice():
    from cmuh_common.roster.vs_preferences import _weekday_points as pts
    mon, fri = date(2026, 12, 7), date(2026, 12, 11)
    assert pts(mon, "T", set()) > pts(mon, "F", set()) > pts(mon, "R", set()) == 0
    assert pts(fri, "T", set()) == 0 and pts(mon, "T", {mon}) == 0


def test_eight_vs_months_reach_balanced_points():
    """[2026-11-24] 8 位 VS:以前 11、12 月會逾時,有人 9 點、其他人 4 點。"""
    for month, leaves in ((11, {}), (12, {"T": {date(2026, 12, 7)}})):
        _ctx, res = _solve(month, leaves=leaves)
        pts = res.points_by_person.values()
        assert res.status == "ok" and max(pts) - min(pts) <= 1, (month, dict(res.points_by_person))
