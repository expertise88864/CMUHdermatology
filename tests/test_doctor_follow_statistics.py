from datetime import date
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cmuh_common.roster.doctor_follow import doctor_follow_rows, natural_key
from cmuh_common.roster.service import RosterService
from cmuh_common.roster.solve_day import BIOPSY, PHOTO, REST, TREATMENT
from cmuh_common.roster.storage import RosterStorage
from cmuh_common.roster.ui.day_tab import DayScheduleTab
from cmuh_common.roster.ui.doctor_follow import DoctorFollowStats


@pytest.fixture
def service(tmp_path):
    storage = RosterStorage(str(tmp_path / "roster"))
    storage.save_config({"pgy_members": [{"id": "P1"}]})
    storage.save_clinic_template({"template": {
        str(day): {session: [{"room": "101", "doctor": "A"},
                             {"room": "102", "doctor": "B"},
                             {"room": "103"}, {"room": "104", "doctor": "A"}]
                   for session in ("上午", "下午")}
        for day in range(5)}})
    storage.save_clerk_batches([
        {"id": "B1", "start_monday": "2026-09-28", "members": ["10", "2", "1"]},
        {"id": "B2", "start_monday": "2026-10-12", "members": ["1"]},
    ])
    storage.save_month("2026-09", {"day_slots": {
        "2026-09-28": {"上午": {"101": ["1"]}},
        "2026-10-01": {"上午": {"101": ["1"]}},  # Wrong-month entry must not win.
    }})
    storage.save_month("2026-10", {
        "external_month_roster": ["E1"], "family_month_roster": ["F1"],
        "day_slots": {
            "2026-10-01": {"上午": {"102": ["1", "P1", "E1", "F1"]},
                           "下午": {"103": ["1"], BIOPSY: ["P1"]}},
            "2026-10-02": {"上午": {"104": ["1"], PHOTO: ["P1"]},
                           "下午": {"102": ["1"], TREATMENT: ["P1"], REST: ["E1"]}},
            "2026-10-12": {"上午": {"101": ["1"]}},
        }})
    return RosterService(storage)


def rows(service, ym="2026-10"):
    return doctor_follow_rows(service.day_current_course_stats(ym), ym)


def test_natural_sort():
    assert sorted(["10", "2", "1", "P10", "P2"], key=natural_key) == [
        "1", "2", "10", "P2", "P10"]
    assert natural_key("¹")


def test_all_scopes_cross_month_ties_and_special_counts(service):
    data = rows(service)
    assert {r.scope for r in data} == {"Clerk", "PGY", "外訓", "家醫科"}
    clerks = [r for r in data if r.scope == "Clerk"]
    assert [(r.course, r.code) for r in clerks] == [
        ("B1", "1"), ("B1", "2"), ("B1", "10"), ("B2", "1")]
    first = clerks[0]
    assert first.follow == 5
    assert first.doctors == (("A", 2), ("B", 2))
    assert first.rank_groups() == ("A／B 各 2 次",)
    assert first.unknown == 1
    assert len(first.visits) == 5
    assert first.visits[0].day == date(2026, 9, 28)
    assert first.visits[1].doctor == "B"
    assert first.period == "2026-09-28 ~ 2026-10-11"
    assert clerks[-1].follow == 1
    assert clerks[-1].identity != first.identity
    assert clerks[1].rank_groups() == ()
    pgy = next(r for r in data if r.scope == "PGY")
    assert (pgy.follow, pgy.photo, pgy.treatment, pgy.biopsy) == (1, 1, 1, 1)
    assert len(pgy.visits) == 1
    for scope in ("外訓", "家醫科"):
        row = next(r for r in data if r.scope == scope)
        assert row.doctors == (("B", 1),)
    for row in data:
        assert row.follow == len(row.visits) == sum(n for _, n in row.doctors) + row.unknown


def test_read_only_and_refresh_after_manual_edit(service):
    directory = Path(service.storage._month_path("2026-10")).parent
    before = {p: p.read_bytes() for p in directory.glob("*.json")}
    original = rows(service)
    assert before == {p: p.read_bytes() for p in directory.glob("*.json")}
    service.set_day_slot("2026-10", date(2026, 10, 2), "上午", "104", [])
    refreshed = rows(service)
    assert original[0].follow == 5
    assert refreshed[0].follow == 4
    assert refreshed[0].doctors == (("B", 2), ("A", 1))
    assert refreshed[0].rank_groups() == ("B 2 次", "A 1 次")


def test_future_course_and_conflicting_sessions_remain_explicit(service):
    first = rows(service, "2026-09")[0]
    assert first.ongoing
    assert first.follow == 5
    service.set_day_slot("2026-10", date(2026, 10, 1), "上午", "101", ["1"])
    first = rows(service)[0]
    assert first.conflicts == 1
    assert len(first.visits) == first.follow == 6


def test_ui_filters_selection_dates_and_unknown(tk_root, service):
    app = SimpleNamespace(ym="2026-10")
    page = DoctorFollowStats(tk_root, service, app, on_back=lambda: None)
    page.pack(fill="both", expand=True)
    try:
        page.refresh()
        tk_root.update()
        assert len(page._summary.get_children()) == 7
        assert page._person.identity == ("Clerk", "B1", "1")
        assert len(page._visits.get_children()) == 5
        assert [page._ranks.item(i, "values")[0] for i in ("0", "1")] == ["1", "1"]
        page._ranks.selection_set("unknown")
        tk_root.update()
        assert len(page._visits.get_children()) == 1
        assert page._visits.item(page._visits.get_children()[0], "values")[-1] == "醫師未標示"
        page._scope.set("PGY")
        page._filter_changed()
        tk_root.update()
        assert len(page._summary.get_children()) == 1
        assert page._person.scope == "PGY"
        page.refresh()
        assert page._person.scope == "PGY"
        page._scope.set("Clerk")
        page._filter_changed()
        label = next(k for k, v in page._courses.items() if v == ("Clerk", "B2"))
        page._course.set(label)
        page._render_summary()
        assert page._person.identity == ("Clerk", "B2", "1")
        assert len(page._visits.get_children()) == 1
    finally:
        page.destroy()


def test_ui_failure_clears_stale_data_and_month_change_resets_filter(tk_root, service, monkeypatch):
    app = SimpleNamespace(ym="2026-10")
    page = DoctorFollowStats(tk_root, service, app, on_back=lambda: None)
    page.pack(fill="both", expand=True)
    try:
        page.refresh()
        page._scope.set("PGY")
        page._filter_changed()
        label = next(k for k, v in page._courses.items() if v == ("PGY", "2026-10"))
        page._course.set(label)
        app.ym = "2026-11"
        page.refresh()
        assert page._course.get() == "全部梯次／月份"
        assert page._person.follow == 0
        original = service.day_current_course_stats
        def fail(_ym):
            raise ValueError("unreadable source")
        monkeypatch.setattr(service, "day_current_course_stats", fail)
        page.refresh()
        tk_root.update()
        assert not page._summary.get_children()
        assert not page._ranks.get_children()
        assert not page._visits.get_children()
        assert "讀取失敗" in page._status.cget("text")
        monkeypatch.setattr(service, "day_current_course_stats", original)
        page.refresh()
        assert page._summary.get_children()
    finally:
        page.destroy()


def test_day_toolbar_fourth_row_navigation_and_refresh(tk_root, service):
    app = SimpleNamespace(ym="2026-10")
    tab = DayScheduleTab(tk_root, service, app)
    tab.pack(fill="both", expand=True)
    try:
        toolbar = tab._doctor_tab.master
        assert tab.winfo_children().index(toolbar) == 3
        assert toolbar.winfo_children() == [tab._doctor_tab]
        assert tab._doctor_tab.cget("text") == "醫師跟診統計"
        assert tab._doctor_stats is None
        tab._doctor_tab.invoke()
        tk_root.update()
        assert tab._doctor_stats.winfo_ismapped()
        assert not tab._schedule_body.winfo_ismapped()
        assert tab._lock_btn.instate(("disabled",))
        service.set_day_slot("2026-10", date(2026, 10, 2), "上午", "104", [])
        tab.refresh()
        assert tab._doctor_stats._person.follow == 4
        tab._show_schedule()
        tk_root.update()
        assert tab._schedule_body.winfo_ismapped()
        assert not tab._doctor_stats.winfo_ismapped()
        assert not tab._lock_btn.instate(("disabled",))
        tab._show_doctor_stats()
        tab._on_month_change("2026-11")
        assert tab._doctor_stats._ym == "2026-11"
        tab._on_toggle_view()
        assert tab._page_var.get() == "schedule"
        assert tab._view_mode == "list"
    finally:
        tab.destroy()


@pytest.mark.parametrize("width,height", [(1280, 850), (1000, 700)])
def test_statistics_viewport(tk_root, service, width, height):
    tk_root.geometry(f"{width}x{height}+0+0")
    tab = DayScheduleTab(tk_root, service, SimpleNamespace(ym="2026-10"))
    tab.pack(fill="both", expand=True)
    try:
        tab._show_doctor_stats()
        tk_root.update()
        page = tab._doctor_stats
        assert page._summary.winfo_width() > width - 70
        assert page._ranks.winfo_width() >= 200
        assert page._visits.winfo_width() >= 300
        assert page._summary.winfo_height() >= 100
        assert page._visits.winfo_height() >= 80
        assert page._visits.winfo_rooty() > page._summary.winfo_rooty()
        assert page._course_box.winfo_rootx() + page._course_box.winfo_width() < width
        if directory := os.environ.get("CMUH_TEST_SCREENSHOTS"):
            from PIL import ImageGrab
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            x, y = tk_root.winfo_rootx(), tk_root.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + width, y + height)).save(
                target / f"doctor-follow-{width}.png")
    finally:
        tab.destroy()
