# -*- coding: utf-8 -*-
"""[2026-11-24 使用者] 三線(VS)年度週末統計:由當時在輪的人均分,加入前不算。"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster import weekend_share as ws  # noqa: E402
from cmuh_common.roster.calendar_colors import week_colors_for_year  # noqa: E402
from cmuh_common.roster.model import Member, SolveContext  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_rvs import solve_duty  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

OLD = list("DJRSL")
NOW = OLD + ["T", "F"]


def test_joiners_start_from_their_first_month():
    history = [(OLD, 5), (OLD, 4), (OLD, 5), (OLD, 4)]        # 8–11 月 5 人
    exp = ws.vs_expected(history, NOW)
    assert all(exp[p] == pytest.approx(18 / 5) for p in OLD)
    assert exp["T"] == exp["F"] == 0                          # 不用補之前的
    exp = ws.vs_expected(history + [(NOW, 7)], NOW + ["U"])   # 之後再加入一位
    assert exp["T"] == pytest.approx(1) and exp["U"] == 0
    assert exp["D"] == pytest.approx(18 / 5 + 1)


def test_leaver_and_gap_month():
    # X 8–9 月在輪後離開;A 9 月沒值班但仍在輪
    exp = ws.vs_expected([(["A", "B", "X"], 3), (["B", "X"], 3), (["A", "B"], 4)], ["A", "B"])
    assert exp == {"A": pytest.approx(4), "B": pytest.approx(4)}
    assert ws.vs_expected([], ["A"]) == {"A": 0}


def _ctx(prior, expected, ids=NOW):
    return SolveContext("vs", 2026, 12, members=[Member(p, p) for p in ids],
                        week_colors=week_colors_for_year(2026),
                        weekend_prior=prior, weekend_expected=expected).prepare()


def _weekends(ctx, res, p):
    return sum(res.assignments[b.saturday] == p for b in ctx.blocks if b.saturday)


def test_solver_gives_weekends_to_those_behind_without_costing_points():
    expected = dict.fromkeys(OLD, 3.6) | {"T": 0.0, "F": 0.0}
    prior = {"D": 6, "J": 4, "R": 4, "S": 2, "L": 2}
    ctx = _ctx(prior, expected)
    res = solve_duty(ctx)
    plain_ctx = _ctx({}, None)
    plain = solve_duty(plain_ctx)
    assert res.status == plain.status == "ok"
    assert _weekends(ctx, res, "D") == 0
    assert _weekends(ctx, res, "S") >= 1 and _weekends(ctx, res, "L") >= 1

    def points(r):
        return sum(abs(r.points_by_person[p] - r.targets[p]) for p in NOW)
    assert points(res) == pytest.approx(points(plain))

    def spread(c, r):
        n = [_weekends(c, r, p) for p in NOW]
        return max(n) - min(n)
    assert spread(ctx, res) <= spread(plain_ctx, plain)


def _service(tmp_path):
    st = RosterStorage(str(tmp_path))
    cfg = st.load_config()
    cfg["vs_members"] = [{"id": p, "name": p} for p in NOW]
    st.save_config(cfg)
    for mm, people in ((8, OLD), (9, OLD)):
        doc = st.load_month(f"2026-{mm:02d}")
        sats = [d for d in (date(2026, mm, n) for n in range(1, 31)) if d.weekday() == 5]
        doc["vs_duty"] = {d.isoformat(): {"person": people[i % len(people)], "auto": True}
                          for i, d in enumerate(sats)}
        st.save_month(f"2026-{mm:02d}", doc)
    return RosterService(st)


def test_service_counts_vs_month_files(tmp_path):
    svc = _service(tmp_path)
    ctx = svc.build_context("vs", "2026-10")
    assert sum(ctx.weekend_prior.values()) == 9               # 8 月 5 個、9 月 4 個
    assert ctx.weekend_expected["T"] == 0
    assert sum(ctx.weekend_expected.values()) == pytest.approx(9)
    rows = {r["id"]: r for r in svc.weekend_year_summary(ctx, {})}
    assert set(rows) == set(NOW) and rows["T"]["done"] == 0
    assert rows["T"]["to_date"] < rows["D"]["to_date"] < rows["D"]["year"]


def test_r_context_is_unchanged(tmp_path):
    ctx = _service(tmp_path).build_context("r", "2026-10")
    assert ctx.weekend_expected is None and ws.vs_targets(ctx, 4) == {}


@pytest.mark.parametrize("blank", [None, date(2026, 8, 5), date(2026, 8, 8)])
def test_reopening_a_past_month_ignores_later_joiners(blank):
    """[PR #31 審查] 回頭看 8 月(有一格空白也一樣):之後才加入的人那個月目標是 0。"""
    ctx = SolveContext("vs", 2026, 8, members=[Member(p, p) for p in NOW],
                       weekend_expected=dict.fromkeys(NOW, 0.0)).prepare()
    duty = {d: OLD[i % 5] for i, d in enumerate(ctx.days) if d != blank}
    rows = {r["id"]: r for r in ws.year_summary(ctx, duty, today=date(2026, 11, 24))}
    assert rows["D"]["to_date"] == 1.0
    assert rows["T"]["to_date"] == rows["T"]["year"] == 0


def test_planning_a_future_month_includes_new_members():
    ctx = SolveContext("vs", 2026, 12, members=[Member(p, p) for p in NOW],
                       weekend_expected=dict.fromkeys(NOW, 0.0)).prepare()
    sats = {b.saturday: OLD[i % 5] for i, b in enumerate(ctx.blocks) if b.saturday}
    for duty in ({}, {date(2026, 12, 1): "D"}, sats):       # 週末全排給舊成員也一樣
        rows = {r["id"]: r for r in ws.year_summary(ctx, duty, today=date(2026, 11, 24))}
        assert rows["T"]["to_date"] == 0.6


def test_small_target_differences_survive_the_integer_costs():
    """[PR #31 審查] 目標差 0.029 次也要分得出先後(取到小數一位會變同分)。"""
    from cmuh_common.roster.vs_preferences import _cumulative_deviation
    from ortools.sat.python import cp_model
    ids = list("ABCDEFGH")
    base = 18 / 5 + 17 / 7
    ctx = _ctx({"A": 6}, dict.fromkeys(ids, 0.0) | {"A": base}, ids)
    sats = [b.saturday for b in ctx.blocks if b.saturday]
    model = cp_model.CpModel()
    x = {(d, p): model.NewBoolVar(f"{d}{p}") for d in ctx.days for p in ids}
    deviation = _cumulative_deviation(model, x, ctx)
    assert deviation

    def give(people):
        return deviation[1]({(d, p): int(people[i] == p)
                             for i, d in enumerate(sats) for p in ids})
    assert len(sats) == 4
    assert give("ABCD") < give("HBCD")      # 只差一個週末給舊成員 A 還是新成員 H
