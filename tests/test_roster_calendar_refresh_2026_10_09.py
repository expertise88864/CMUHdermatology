"""Real Tk calendar reuse must preserve fresh data, controls and recovery."""
from datetime import date
from types import SimpleNamespace

import pytest

from cmuh_common.roster.service import RosterService
from cmuh_common.roster.storage import RosterStorage
from cmuh_common.roster.ui import day_tab


@pytest.fixture
def panel(tk_root, tmp_path):
    storage = RosterStorage(str(tmp_path))
    storage.save_config({"pgy_members": [{"id": "A"}, {"id": "B"}]})
    storage.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
    storage.save_month("2026-10", {"day_slots": {
        "2026-10-05": {"上午": {"照光": ["A"], "治療室": ["B"]}}}})
    tab = day_tab.DayScheduleTab(
        tk_root, RosterService(storage), SimpleNamespace(ym="2026-10"))
    tab.pack()
    tk_root.update_idletasks()
    yield tab
    tab.destroy()


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _texts(panel):
    return [str(w.cget("text")) for w in _walk(panel._cal_body) if "text" in w.keys()]


def test_lock_refresh_reuses_calendar_but_refreshes_validation_and_list(panel, monkeypatch):
    children = panel._cal_body.winfo_children()
    calls = []
    original = panel.service.quick_validate_day

    def validate(ym):
        calls.append(ym)
        return [*original(ym), f"anonymous current warning {len(calls)}"]

    monkeypatch.setattr(panel.service, "quick_validate_day", validate)
    panel._set_lock_session(date(2026, 10, 5), "上午", True)
    assert panel.service.is_day_locked("2026-10", date(2026, 10, 5), "上午")
    assert panel._tree.item("2026-10-05|上午", "values")[2] == "🔒"
    assert calls == ["2026-10"]
    assert "⚠ anonymous current warning 1" in panel._warns.get(0, "end")
    assert panel._cal_body.winfo_children() == children
    panel.refresh()
    assert calls == ["2026-10", "2026-10"]
    assert "⚠ anonymous current warning 2" in panel._warns.get(0, "end")
    assert "⚠ anonymous current warning 1" not in panel._warns.get(0, "end")
    assert panel._cal_body.winfo_children() == children


def test_same_dictionary_mutation_updates_calendar(panel):
    slots = {"2026-10-05": {"上午": {"照光": ["Before"]}}}
    panel._render_calendar(slots)
    children = panel._cal_body.winfo_children()
    slots["2026-10-05"]["上午"]["照光"][0] = "After"
    panel._render_calendar(slots)
    assert panel._cal_body.winfo_children() != children
    assert "After" in _texts(panel) and "Before" not in _texts(panel)


def test_changed_saved_month_updates_list_and_calendar(panel):
    panel.service.set_day_slot("2026-10", date(2026, 10, 5), "上午", "照光", ["B"])
    panel.refresh()
    assert panel._tree.item("2026-10-05|上午", "values")[3] == "B"
    assert "B" in _texts(panel)
    assert "A" not in _texts(panel)


def test_holiday_source_is_reread_for_unchanged_slots(panel):
    panel.service.storage.save_holiday_duty({"r": {date(2026, 10, 5): "R"}, "vs": {}})
    panel.refresh()
    assert "5（一）假" in _texts(panel)
    panel.service.storage.save_holiday_duty({"r": {}, "vs": {}})
    panel.refresh()
    assert "5（一）假" not in _texts(panel)


def test_today_rollover_repaints_without_touching_system_clock(panel, monkeypatch):
    class LocalDate(date):
        current = date(2026, 10, 5)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr(day_tab, "date", LocalDate)
    panel.refresh()
    assert "5（一）  ⬅今天" in _texts(panel)
    LocalDate.current = date(2026, 10, 6)
    panel.refresh()
    assert "6（二）  ⬅今天" in _texts(panel)
    assert "5（一）  ⬅今天" not in _texts(panel)


def test_finalized_and_unfinalized_calendar_binding_states(panel):
    storage = panel.service.storage
    month = storage.load_month("2026-10")
    for finalized in (True, False):
        if finalized:
            month["finalized"] = True
            storage.save_month("2026-10", month)
        else:
            panel.service.finalize("2026-10", False)
        panel.refresh()
        bindings = [w.bind("<Button-1>") for w in _walk(panel._cal_body)]
        assert bool(any(bindings)) is not finalized
        assert str(panel._auto_btn.cget("state")) == ("disabled" if finalized else "normal")


def test_month_switch_does_not_reuse_previous_calendar(panel):
    panel.app.ym = "2026-11"
    panel.refresh()
    assert "2026 年 11 月" in _texts(panel)
    assert "2026 年 10 月" not in _texts(panel)
    assert not any(i.startswith("2026-10") for i in panel._tree.get_children())


def test_tk_scaling_change_rebuilds_calendar(panel):
    original_scale = float(panel.tk.call("tk", "scaling"))
    children = panel._cal_body.winfo_children()
    try:
        panel.tk.call("tk", "scaling", original_scale * 1.25)
        panel.refresh()
        assert panel._cal_body.winfo_children() != children
    finally:
        panel.tk.call("tk", "scaling", original_scale)


def test_failed_rebuild_then_return_to_previous_data_recovers(panel, monkeypatch):
    original_slots = panel.service.storage.load_month("2026-10")["day_slots"]
    expected_texts = _texts(panel)
    original = panel._build_overview_cell

    def fail(*args, **kwargs):
        raise RuntimeError("anonymous rendering failure")

    monkeypatch.setattr(panel, "_build_overview_cell", fail)
    with pytest.raises(RuntimeError, match="anonymous"):
        panel._render_calendar({})
    monkeypatch.setattr(panel, "_build_overview_cell", original)
    panel._render_calendar(original_slots)
    assert _texts(panel) == expected_texts
