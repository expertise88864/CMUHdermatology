"""Course explanations use real slots and never mistake saved history for now."""

from datetime import date
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from cmuh_common.roster.day_explanation import (
    _nonnegative_targets, explain_day_courses, format_day_explanation,
)
from cmuh_common.roster.service import DaySolveResult, RosterService
from cmuh_common.roster.solve_day import (
    PHOTO, TREATMENT, DaySolveInput, person_course_stats,
)
from cmuh_common.roster.storage import RosterStorage
from cmuh_common.roster.ui import day_tab


def test_manual_reduction_never_makes_reported_work_target_negative():
    targets = _nonnegative_targets({"P1": 0, "P2": 1}, {"P1": -2})
    assert targets == {"P1": 0, "P2": 1}


def test_accounting_keeps_wednesday_inside_photo_and_closures_in_denominator():
    first, second, wednesday = (date(2026, 10, n) for n in (1, 2, 7))
    grid = {d: {"上午": ["101"], "下午": []}
            for d in (first, second, wednesday)}
    slots = {
        first.isoformat(): {"上午": {PHOTO: ["P1"], TREATMENT: ["P2"],
                                      "101": ["E", "F"]}},
        second.isoformat(): {"上午": {TREATMENT: ["P1"], "103": ["P2"]}},
        wednesday.isoformat(): {"下午": {PHOTO: ["P1"]}},
    }
    inp = DaySolveInput(
        "2026-10", grid, ["P1", "P2"], external_roster=["E"],
        family_roster=["F"],
        family_follow={"F": {(first, "上午"), (first, "下午"),
                             (second, "上午")}},
        session_leaves={"external": {"E": {(second, "下午")}},
                        "family": {"F": {(second, "上午")}}},
        clinic_doctors={first: {"上午": {"101": "Dr X"}},
                        second: {"上午": {"103": "Dr Y"}}},
        pgy_photo_offsets={"P1": -1})
    data = {scope: {"roster": roster,
                    "stats": person_course_stats(slots, include=set(roster))}
            for scope, roster in (("pgy", ["P1", "P2"]),
                                  ("external", ["E"]), ("family", ["F"]))}
    data["batches"] = []
    explanation = explain_day_courses(inp, data, slots)
    p1 = next(r for r in explanation.pgy if r.code == "P1")
    assert (p1.photo, p1.wednesday_photo, p1.treatment, p1.follow) == (2, 1, 1, 0)
    assert (p1.necessary, p1.total, p1.manual_reduction) == (3, 3, -1)
    assert p1.fair_target == Fraction(2)
    external = next(r for r in explanation.training if r.code == "E")
    family = next(r for r in explanation.training if r.code == "F")
    assert external.available_half_days == 5
    assert family.available_half_days == 2  # closed Thursday PM still counts
    assert family.known_doctors == (("Dr X", 1),)
    assert "尚未證明" in format_day_explanation(explanation)


def test_export_and_saved_report_recompute_after_manual_change(tmp_path):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": "P1"}]})
    month = storage.load_month("2026-10")
    month["day_slots"] = {"2026-10-01": {"上午": {PHOTO: ["P1"]}}}
    month["day_report"] = "OLD 999 visits"
    storage.save_month("2026-10", month)
    service = RosterService(storage)
    export = service.build_export("2026-10")
    assert "P1 1 0 0 0 1 1" in export["day_explanation"]
    assert "OLD 999 visits" not in export["day_explanation"]
    displayed = service.report_for_display("day", "2026-10")
    assert "P1 1 0 0 0 1 1" in displayed
    assert "歷史求解紀錄" in displayed

    month = storage.load_month("2026-10")
    month["day_report"] = "【逐日過程】\nold run\n\n" + format_day_explanation(
        service.day_course_stats("2026-10")["explanation"])
    storage.save_month("2026-10", month)
    displayed = service.report_for_display("day", "2026-10")
    assert displayed.count("【實際班表統計與公平性】") == 1
    assert "old run" in displayed

    month = storage.load_month("2026-10")
    month["day_report"] = "【逐日過程】\nold run\n\n【週期次數統計】\n舊跟診 999"
    storage.save_month("2026-10", month)
    displayed = service.report_for_display("day", "2026-10")
    assert "跟診 999" not in displayed
    assert "old run" in displayed

    openpyxl = pytest.importorskip("openpyxl")
    from cmuh_common.roster import export_xlsx

    target = tmp_path / "roster.xlsx"
    export_xlsx.export(str(target), export)
    workbook = openpyxl.load_workbook(target)
    assert "排班統計" in workbook.sheetnames
    sheet = workbook["排班統計"]
    assert any("P1 1 0 0 0 1 1" in str(row[0].value)
               for row in sheet.iter_rows() if row[0].value)


def test_current_october_report_counts_a_september_clerk_course(tmp_path):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": "P1"}]})
    storage.save_clinic_template({"template": {
        "0": {"上午": [{"room": "101"}]},
        "3": {"上午": [{"room": "101"}]}}})
    storage.save_clerk_batches([{
        "id": "B1", "start_monday": "2026-09-28", "members": ["C1"]}])
    storage.save_month("2026-09", {
        "day_slots": {"2026-09-28": {"上午": {"101": ["C1"]}}}})
    storage.save_month("2026-10", {
        "day_slots": {"2026-10-01": {"上午": {"101": ["C1"]}}}})

    text = RosterService(storage).report_for_display("day", "2026-10")

    assert "Clerk B1 C1" in text
    assert "可參與半日" in text
    assert "跟診 2" in text


def test_preview_rejects_unreadable_cross_month_clerk_source(tmp_path,
                                                              monkeypatch):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": "P1"}]})
    storage.save_clinic_template({"template": {
        "0": {"上午": [{"room": "101"}]},
        "3": {"上午": [{"room": "101"}]}}})
    storage.save_clerk_batches([{
        "id": "B1", "start_monday": "2026-09-28", "members": ["C1"]}])
    storage.save_month("2026-09", {
        "day_slots": {"2026-09-28": {"上午": {"101": ["C1"]}}}})
    preview = {"2026-10-01": {"上午": {"101": ["C1"]}}}
    storage.save_month("2026-10", {"day_slots": preview})
    service = RosterService(storage)

    explanation = service.day_preview_explanation("2026-10", preview)
    assert "跟診 2" in format_day_explanation(explanation)
    stale = DaySolveResult(preview, [], [], "old fingerprint", "old revision")
    with pytest.raises(ValueError, match="資料已變動"):
        service.day_preview_explanation("2026-10", preview, expect=stale)

    Path(storage._month_path("2026-09")).write_text("{invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="2026-09.json"):
        service.day_preview_explanation("2026-10", preview)
    with pytest.raises(ValueError, match="2026-09.json"):
        service.day_current_course_stats("2026-10")

    class FakeTable:
        def __init__(self):
            self.values = ["old month value"]

        def get_children(self):
            return self.values[:]

        def delete(self, *_items):
            self.values.clear()

        def insert(self, _parent, _index, *, values):
            self.values.append(values[0])

    pgy, clerk = FakeTable(), FakeTable()
    fake_tab = SimpleNamespace(
        service=service, app=SimpleNamespace(ym="2026-10"),
        _stats_pgy=pgy, _stats_clerk=clerk)
    errors = []
    monkeypatch.setattr(day_tab.messagebox, "showerror",
                        lambda _title, detail, **_kw: errors.append(detail))
    day_tab.DayScheduleTab._refresh_stats(fake_tab)
    day_tab.DayScheduleTab._on_explanation(fake_tab)
    assert pgy.values == clerk.values == ["統計讀取失敗"]
    assert len(errors) == 1 and "2026-09.json" in errors[0]


def test_unfinished_cross_month_clerk_is_progress_not_confirmed_gap(tmp_path):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": "P1"}]})
    storage.save_clinic_template({"template": {
        "0": {"上午": [{"room": "101"}]}}})
    storage.save_clerk_batches([{
        "id": "B1", "start_monday": "2026-09-28", "members": ["C1"]}])
    storage.save_month("2026-09", {
        "day_slots": {"2026-09-28": {"上午": {"101": ["C1"]}}}})
    service = RosterService(storage)
    explanation = service.day_current_course_stats("2026-09")["explanation"]
    clerk = next(r for r in explanation.training if r.scope == "Clerk")
    assert clerk.follow == 1 and not clerk.complete
    assert not any("Clerk C1 跟診" in gap or "Clerk C1 切片" in gap
                   for gap in explanation.confirmed_gaps)
    assert "課程尚未結束" in format_day_explanation(explanation)

    class FakeTable:
        def __init__(self):
            self.rows = []

        def get_children(self):
            return list(range(len(self.rows)))

        def delete(self, *_items):
            self.rows.clear()

        def insert(self, _parent, _index, *, values, tags=()):
            self.rows.append((values, tags))

    pgy, students = FakeTable(), FakeTable()
    fake_tab = SimpleNamespace(
        service=service, app=SimpleNamespace(ym="2026-09"),
        _stats_pgy=pgy, _stats_clerk=students)
    day_tab.DayScheduleTab._refresh_stats(fake_tab)
    clerk_row = next((values, tags) for values, tags in students.rows
                     if values[0] == "C1")
    assert "miss" not in clerk_row[1]
