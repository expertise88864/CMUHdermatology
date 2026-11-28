# -*- coding: utf-8 -*-
"""[2026-11-28 使用者] VS 三線:避免連值 4、5 天 → 年度週末 → 本月週末 → 點數
(為了週末最多讓點數多差 2 點,差額進帳本)→ 班數平均 → 平日偏好;
畫面另顯示到今天為止實際值過的週末次數。匿名代號、暫存資料夾。"""
import calendar
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
pytest.importorskip("ortools", reason="ortools 未安裝（自動排班引擎）")

from cmuh_common.roster import vs_preferences as vp  # noqa: E402
from cmuh_common.roster.calendar_colors import week_colors_for_year  # noqa: E402
from cmuh_common.roster.model import Member, SolveContext  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_rvs import solve_duty  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402
from cmuh_common.roster.weekend_share import counted_blocks, vs_targets  # noqa: E402

OLD = list("DJRSL")
NOW = OLD + ["T", "F"]


def _ctx(prior, ledger=None, leaves=None):
    def D(n):
        return date(2026, 12, n)
    return SolveContext(
        "vs", 2026, 12, members=[Member(p, p) for p in NOW], holidays={D(25)},
        week_colors=week_colors_for_year(2026), weekend_prior=prior,
        weekend_expected=dict.fromkeys(OLD, 3.4) | {"T": 0.0, "F": 0.0},
        ledger=ledger or {},
        leaves={p: {D(n) for n in days} for p, days in (leaves or {}).items()}).prepare()


def _measure(ctx, res):
    blocks = counted_blocks(ctx.blocks, ctx.weekend_exempt, ctx.holidays)
    targets = vs_targets(ctx, len(blocks))
    done = {p: ctx.weekend_prior.get(p, 0)
            + sum(res.assignments[b.saturday] == p for b in blocks) for p in NOW}
    year = sum((done[p] - targets[p]) ** 2 for p in NOW)
    points = sum(abs(res.points_by_person[p] - res.targets[p]) for p in NOW)
    return year, points


CASE = dict(prior={"D": 5, "J": 2, "R": 5, "S": 3, "L": 5},
            ledger={"D": -0.34, "J": -1.31, "R": 0.2, "S": 0.81, "L": 0.7,
                    "T": -0.5, "F": -0.24},
            leaves={"D": [4, 6, 17, 25], "J": [1, 12, 13, 16, 24], "S": [23, 28],
                    "L": [13, 19, 21], "T": [6], "F": [1, 7, 8, 25]})


def test_weekends_may_cost_up_to_two_points(monkeypatch):
    monkeypatch.setattr(vp, "POINT_SLACK", 0)
    strict_ctx = _ctx(**CASE)
    strict = solve_duty(strict_ctx)
    monkeypatch.setattr(vp, "POINT_SLACK", 200)
    loose_ctx = _ctx(**CASE)
    loose = solve_duty(loose_ctx)
    assert strict.status == loose.status == "ok"
    s_year, s_points = _measure(strict_ctx, strict)
    l_year, l_points = _measure(loose_ctx, loose)
    assert l_year < s_year                                   # 週末更接近目標
    assert s_points < l_points <= s_points + 2 + 1e-9        # 點數最多多差 2 點


def _long_runs(ctx, res):
    from cmuh_common.roster.solve_rvs import _ModelCtx
    from ortools.sat.python import cp_model
    model = cp_model.CpModel()
    x = {(d, m.id): model.NewBoolVar("") for d in ctx.days for m in ctx.members}
    score = vp._long_run_terms(_ModelCtx(model, x), ctx)[1]
    return score({(d, m.id): int(res.assignments.get(d) == m.id)
                  for d in ctx.days for m in ctx.members})


def test_long_runs_are_not_traded_for_weekends(monkeypatch):
    ctx = _ctx(**CASE)
    res = solve_duty(ctx)
    monkeypatch.setattr(vp, "refine_vs", lambda *a, **k: None)   # 只有主目標
    plain_ctx = _ctx(**CASE)
    plain = solve_duty(plain_ctx)
    assert _long_runs(ctx, res) <= _long_runs(plain_ctx, plain)


def test_year_comes_before_the_month():
    """J 落後年度目標很多:寧可本月給 J 兩個週末(本月較不平均)也要補。
    (帳本讓 J 本月該多拿 1 點;舊版先求本月平均,只給 J 一個。)"""
    ctx = _ctx({"D": 4, "J": 1, "R": 4, "S": 4, "L": 4},
               ledger={"J": -1.0} | {p: 1 / 6 for p in NOW if p != "J"})
    res = solve_duty(ctx)
    assert res.status == "ok"
    blocks = counted_blocks(ctx.blocks, ctx.weekend_exempt, ctx.holidays)
    assert sum(res.assignments[b.saturday] == "J" for b in blocks) == 2


def test_month_weekends_only_count_saturday_blocks():
    """本月平均與年度同一個算法:平日國定假日(12/25 五)併入週末段,不另算一次。"""
    ctx = _ctx({p: 3 for p in OLD})
    blocks = counted_blocks(ctx.blocks, ctx.weekend_exempt, ctx.holidays)
    assert [b.saturday.day for b in blocks] == [5, 12, 19, 26]


def test_so_far_counts_weekends_up_to_today(tmp_path):
    st = RosterStorage(str(tmp_path))
    cfg = st.load_config()
    cfg["vs_members"] = [{"id": p, "name": p} for p in NOW]
    st.save_config(cfg)
    for mm in (8, 9, 10, 11):
        doc = st.load_month(f"2026-{mm:02d}")
        days = [date(2026, mm, n) for n in range(1, calendar.monthrange(2026, mm)[1] + 1)]
        sats = [d for d in days if d.weekday() == 5]
        doc["vs_duty"] = {d.isoformat(): {"person": OLD[i % 5], "auto": True}
                          for i, d in enumerate(sats)}
        st.save_month(f"2026-{mm:02d}", doc)
    svc = RosterService(st)
    ctx = svc.build_context("vs", "2026-12")
    dec = {b.saturday: "T" for b in ctx.blocks if b.saturday}
    # 今天 11/28(六):11 月最後一個週末已值;12 月還沒開始
    rows = {r["id"]: r for r in svc.weekend_year_summary(ctx, dec, today=date(2026, 11, 28))}
    total = sum(r["so_far"] for r in rows.values())
    assert total == 5 + 4 + 5 + 4 and rows["T"]["so_far"] == 0
    assert rows["T"]["done"] == 4                            # 含 12 月班表
    rows = {r["id"]: r for r in svc.weekend_year_summary(ctx, dec, today=date(2026, 12, 13))}
    assert rows["T"]["so_far"] == 2                          # 12/5、12/12
    rows = {r["id"]: r for r in svc.weekend_year_summary(ctx, dec, today=date(2026, 10, 1))}
    assert sum(r["so_far"] for r in rows.values()) == 5 + 4  # 8、9 月


def test_spring_festival_weekend_is_not_counted(tmp_path):
    st = RosterStorage(str(tmp_path))
    cfg = st.load_config()
    cfg["vs_members"] = [{"id": p, "name": p} for p in NOW]
    st.save_config(cfg)
    svc = RosterService(st)
    ctx = svc.build_context("vs", "2027-02")
    ctx.weekend_exempt = {date(2027, 2, 6)}
    duty = {b.saturday: "D" for b in ctx.blocks if b.saturday}
    rows = {r["id"]: r for r in svc.weekend_year_summary(ctx, duty, today=date(2027, 2, 28))}
    assert rows["D"]["so_far"] == len(duty) - 1


# ── 在輪期間的平均份(畫面與匯出) ──────────────────────────────────────────
def _month_duty(y, mm, people):
    days = [date(y, mm, n) for n in range(1, calendar.monthrange(y, mm)[1] + 1)]
    return {d: people[i % len(people)] for i, d in enumerate(x for x in days if x.weekday() == 5)}


def test_joiners_are_compared_with_their_own_share():
    from cmuh_common.roster.weekend_share import vs_fair_share
    months = [(f"2026-{mm:02d}", _month_duty(2026, mm, OLD)) for mm in (8, 9, 10, 11)]
    months.append(("2026-12", _month_duty(2026, 12, ["T", "F", "D", "J"])))
    fair = vs_fair_share(months, NOW, set(), set(), date(2026, 12, 13))
    # 8–11 月 18 個週末 5 人分;12/5、12/12 兩個週末 7 人分
    assert fair["D"]["expected"] == pytest.approx(round(18 / 5 + 2 / 7, 1))
    assert fair["T"] == {"start": "2026-12", "so_far": 1,
                         "expected": round(2 / 7, 1), "diff": round(1 - round(2 / 7, 1), 1)}
    assert fair["F"]["so_far"] == 1 and fair["F"]["start"] == "2026-12"
    assert abs(fair["T"]["diff"]) < 1 and abs(fair["D"]["diff"]) < 1
    before = vs_fair_share(months, NOW, set(), set(), date(2026, 11, 30))
    assert before["T"] == {"start": None, "so_far": 0, "expected": 0.0, "diff": 0.0}


def test_stray_adjacent_month_cells_do_not_join_the_rotation(tmp_path):
    """[外審 PR #37 第 1 輪] 11 月檔殘留一格 12 月的 T:T 不算 11 月在輪。"""
    from cmuh_common.roster.weekend_share import vs_fair_share
    nov = _month_duty(2026, 11, ["D", "J"]) | {date(2026, 12, 5): "T"}
    fair = vs_fair_share([("2026-11", nov)], ["D", "J", "T"], set(), set(),
                         date(2026, 11, 30))
    assert fair["D"]["expected"] == 2.0 and fair["T"]["start"] is None
    st = RosterStorage(str(tmp_path))
    cfg = st.load_config()
    cfg["vs_members"] = [{"id": p, "name": p} for p in ("D", "J", "T")]
    st.save_config(cfg)
    for ym in ("2026-08", "2026-09", "2026-10"):
        st.save_month(ym, st.load_month(ym))
    doc = st.load_month("2026-11")
    doc["vs_duty"] = {d.isoformat(): {"person": p, "auto": True} for d, p in nov.items()}
    st.save_month("2026-11", doc)
    ctx = RosterService(st).build_context("vs", "2026-12")
    assert ctx.weekend_expected["T"] == 0 and ctx.weekend_expected["D"] == 2.0


def test_screen_rows_carry_the_fair_share(tmp_path):
    st = RosterStorage(str(tmp_path))
    cfg = st.load_config()
    cfg["vs_members"] = [{"id": p, "name": p} for p in NOW]
    st.save_config(cfg)
    for mm in (8, 9, 10, 11):
        doc = st.load_month(f"2026-{mm:02d}")
        doc["vs_duty"] = {d.isoformat(): {"person": p, "auto": True}
                          for d, p in _month_duty(2026, mm, OLD).items()}
        st.save_month(f"2026-{mm:02d}", doc)
    svc = RosterService(st)
    ctx = svc.build_context("vs", "2026-12")
    dec = _month_duty(2026, 12, ["T", "F", "D", "J"])     # 12/5 T、12/12 F、12/19 D、12/26 J
    rows = {r["id"]: r for r in svc.weekend_year_summary(ctx, dec, today=date(2026, 12, 13))}
    assert rows["T"]["start"] == "2026-12" and rows["T"]["so_far"] == 1
    # 應值/差額跟「含本月」同一段:算到 12 月底(4 個週末 7 人分)
    assert rows["D"]["expected"] == pytest.approx(round(18 / 5 + 4 / 7, 1))
    assert rows["D"]["diff"] == pytest.approx(rows["D"]["done"] - rows["D"]["expected"])


def test_december_joiners_show_up_before_december_starts(tmp_path):
    """[2026-11-28 使用者截圖] 11/28 打開 12 月:U、F 的開始/應值/差額要算進 12 月班表。"""
    st = RosterStorage(str(tmp_path))
    cfg = st.load_config()
    cfg["vs_members"] = [{"id": p, "name": p} for p in NOW]
    st.save_config(cfg)
    for mm in (8, 9, 10, 11):
        doc = st.load_month(f"2026-{mm:02d}")
        doc["vs_duty"] = {d.isoformat(): {"person": p, "auto": True}
                          for d, p in _month_duty(2026, mm, OLD).items()}
        st.save_month(f"2026-{mm:02d}", doc)
    svc = RosterService(st)
    ctx = svc.build_context("vs", "2026-12")
    dec = _month_duty(2026, 12, ["T", "F", "D", "J"])
    rows = {r["id"]: r for r in svc.weekend_year_summary(ctx, dec, today=date(2026, 11, 28))}
    assert rows["T"]["start"] == "2026-12" and rows["T"]["so_far"] == 0
    assert rows["T"]["done"] == 1 and rows["T"]["expected"] == round(4 / 7, 1)
    assert rows["T"]["diff"] == pytest.approx(1 - round(4 / 7, 1))


def _export_data(tmp_path):
    from test_roster_export import YM, _svc
    return _svc(tmp_path).build_export(YM)


def test_export_includes_the_vs_weekend_table(tmp_path):
    data = _export_data(tmp_path)
    assert data["vs_weekend"]["D"] == {"start": "2026-08", "so_far": 1,
                                       "expected": 1.0, "diff": 0.0}


def test_xlsx_has_the_vs_weekend_sheet(tmp_path):
    pytest.importorskip("openpyxl")
    from openpyxl import load_workbook

    from cmuh_common.roster import export_xlsx
    out = tmp_path / "out.xlsx"
    export_xlsx.export(str(out), _export_data(tmp_path))
    ws = load_workbook(str(out))["三線週末"]
    rows = [[c.value for c in row] for row in ws.iter_rows()]
    assert rows[1] == ["主治醫師", "開始輪值", "已值（含本月）", "應值", "差額"]
    assert rows[2] == ["D", "2026/8", 1, 1.0, "+0.0"]
    assert ws.cell(row=3, column=5).data_type == "s"


def test_docx_has_the_vs_weekend_table(tmp_path):
    pytest.importorskip("docx")
    from docx import Document

    from cmuh_common.roster import export_docx
    out = tmp_path / "out.docx"
    export_docx.export(str(out), _export_data(tmp_path))
    doc = Document(str(out))
    table = next(t for t in doc.tables if t.cell(0, 0).text == "主治醫師")
    assert [c.text for c in table.rows[1].cells] == ["D", "2026/8", "1", "1.0", "+0.0"]
    assert any("在輪的人平分" in p.text for p in doc.paragraphs)


def test_duty_tab_shows_the_fair_share_columns(tk_root, tmp_path):
    from test_roster_ui_smoke import YM, _app, _svc

    from cmuh_common.roster.ui.duty import CalendarDutyTab
    svc = _svc(tmp_path)
    svc.set_cell("vs", YM, date(2026, 8, 1), "D")
    tab = CalendarDutyTab(tk_root, svc, _app())
    tk_root.update()
    vs = tab._wk_vs
    assert [vs.heading(c)["text"] for c in vs["columns"]] == [
        "主治醫師", "開始", "至今", "含本月", "應值", "差額", "全年"]
    row = vs.item(vs.get_children()[0])["values"]
    assert str(row[0]).startswith("D") and row[1] == "8 月" and str(row[5]) == "+0.0"
    r = tab._wk
    assert [r.heading(c)["text"] for c in r["columns"]] == [
        "住院醫師", "至今", "含本月", "至本月", "全年"]
    tab.destroy()
