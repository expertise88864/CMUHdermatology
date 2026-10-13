from collections import Counter
from dataclasses import replace
from datetime import date

import pytest

from cmuh_common.roster.model import ClerkBatch
from cmuh_common.roster.service import RosterService
from cmuh_common.roster.solve_day import (
    BIOPSY, DaySolveInput, day_input_fingerprint, is_follow_slot, month_solve_day,
)
from cmuh_common.roster.storage import RosterStorage, StaleRosterDataError
from cmuh_common.roster.training_bands import band, training_warnings
from cmuh_common.roster.training_params import TrainingParams
from cmuh_common.roster.ui.training_settings import TrainingSettings


def service(tmp_path):
    storage = RosterStorage(str(tmp_path))
    storage.save_config({"pgy_members": [{"id": "P1"}]})
    return RosterService(storage)


def course(params):
    grid = {date(2026, 10, n): {"上午": ["101", "102"],
            "下午": [] if date(2026, 10, n).weekday() == 2 else ["101", "102"]}
            for n in range(5, 17) if date(2026, 10, n).weekday() < 5}
    return DaySolveInput("2026-10", grid, ["P1", "P2", "P3", "P4"],
                         clerk_batches=[ClerkBatch("B", date(2026, 10, 5), ["C1", "C2"])],
                         biopsy_open={"B": {d.isoformat(): {s: bool(rooms) for s, rooms in ss.items()}
                                            for d, ss in grid.items()}},
                         external_roster=["E"], family_roster=["F"],
                         family_follow={"F": {(d, s) for d, ss in grid.items() for s in ss}},
                         training=params)


def counts(slots):
    follows, biopsies, weekly = Counter(), Counter(), Counter()
    for iso, sessions in slots.items():
        for cells in sessions.values():
            assigned = [p for ps in cells.values() for p in ps]
            assert len(assigned) == len(set(assigned))
            for room, people in cells.items():
                if is_follow_slot(room):
                    follows.update(people)
                    weekly.update((date.fromisoformat(iso).isocalendar()[:2], p) for p in people)
                elif room == BIOPSY:
                    biopsies.update(people)
    return follows, biopsies, weekly


def test_legacy_config_defaults_and_integer_percentage_rounding():
    assert TrainingParams.from_config({}) == TrainingParams()
    assert TrainingParams.from_config({"training_params": {"pgy_high": 4}}).pgy_target == 2
    assert band(1) == (0, 0, 0)
    assert band(20, (20, 40, 60)) == (4, 8, 12)
    assert band(3, (0, 50, 100)) == (0, 2, 3)


@pytest.mark.parametrize("raw", [None, [], {"pgy_high": True}, {"pgy_high": 2.0},
    {"pgy_high": "2"}, {"pgy_low": -1}, {"pgy_high": 11}, {"external_high": 101},
    {"clerk_low": 12}, {"clerk_target": 8}, {"clerk_high": 21}, {"clerk_biopsy_low": 3}])
def test_reject_invalid_rules_before_saving(tmp_path, raw):
    svc = service(tmp_path)
    before = svc.storage.canonical_revision("config.json")
    with pytest.raises(ValueError):
        svc.storage.save_config({"training_params": raw})
    assert svc.storage.canonical_revision("config.json") == before


def test_service_merges_disjoint_fields_and_preserves_other_config_and_months(tmp_path):
    svc = service(tmp_path)
    baseline = TrainingParams().to_dict()
    month = svc.storage.load_month("2026-10")
    month["finalized"] = True
    month["day_slots"] = {"2026-10-05": {"上午": {"101": ["P1"]}}}
    svc.storage.save_month("2026-10", month)
    month = svc.storage.load_month("2026-10")
    month_rev = svc.storage.month_revision("2026-10")
    cfg = svc.storage.load_config()
    cfg.update(training_params={"family_low": 20, "future_rule": 7}, room_capacity=3)
    svc.storage.save_config(cfg)
    saved = svc.set_training_params({**baseline, "pgy_high": 4}, baseline=baseline)
    assert saved["pgy_high"] == 4 and saved["family_low"] == 20
    cfg = svc.storage.load_config()
    assert cfg["room_capacity"] == 3
    assert cfg["pgy_members"] == [{"id": "P1"}]
    assert cfg["training_params"]["future_rule"] == 7
    assert svc.storage.month_revision("2026-10") == month_rev
    assert svc.storage.load_month("2026-10") == month


def test_service_conflicting_field_or_combined_invalid_range_is_not_written(tmp_path):
    svc = service(tmp_path)
    baseline = TrainingParams().to_dict()
    svc.set_training_params({**baseline, "clerk_low": 10}, baseline=baseline)
    revision = svc.storage.canonical_revision("config.json")
    with pytest.raises(StaleRosterDataError):
        svc.set_training_params({**baseline, "clerk_low": 8}, baseline=baseline)
    with pytest.raises(ValueError):
        svc.set_training_params({**baseline, "clerk_target": 9}, baseline=baseline)
    assert svc.storage.canonical_revision("config.json") == revision


def test_rules_are_part_of_solver_snapshot_and_fingerprint(tmp_path):
    svc = service(tmp_path)
    before = svc.build_day_input("2026-10")
    base = TrainingParams().to_dict()
    svc.set_training_params({**base, "pgy_high": 3}, baseline=base)
    after = svc.build_day_input("2026-10")
    assert before.training.pgy_high == 2
    assert after.training.pgy_high == 3
    assert day_input_fingerprint(before) != day_input_fingerprint(after)


def test_saving_rules_invalidates_open_preview_without_touching_month(tmp_path):
    svc = service(tmp_path)
    today = date(2026, 10, 1)
    result = svc.run_day_solve("2026-10", today=today)
    revision = svc.storage.month_revision("2026-10")
    assert svc.day_solution_is_current("2026-10", result, today=today)
    baseline = TrainingParams().to_dict()
    svc.set_training_params({**baseline, "pgy_high": 3}, baseline=baseline)
    assert not svc.day_solution_is_current("2026-10", result, today=today)
    with pytest.raises(ValueError):
        svc.accept_day_solution("2026-10", result.day_slots, expect=result, today=today)
    assert svc.storage.month_revision("2026-10") == revision


def test_save_retries_against_latest_snapshot_without_losing_other_edits(tmp_path, monkeypatch):
    svc = service(tmp_path)
    original = svc.storage.save_config
    calls = []
    def racing_save(cfg, **kwargs):
        if not calls:
            calls.append(True)
            original({**svc.storage.load_config(), "room_capacity": 4})
        original(cfg, **kwargs)
    monkeypatch.setattr(svc.storage, "save_config", racing_save)
    base = TrainingParams().to_dict()
    svc.set_training_params({**base, "pgy_high": 4}, baseline=base)
    cfg = svc.storage.load_config()
    assert cfg["room_capacity"] == 4 and cfg["training_params"]["pgy_high"] == 4


def test_custom_rules_reach_solver_statistics_and_warnings():
    params = replace(TrainingParams(), clerk_low=4, clerk_target=5, clerk_high=6,
                     pgy_low=1, pgy_target=3, pgy_high=3,
                     external_low=20, external_target=30, external_high=40,
                     family_low=10, family_target=20, family_high=30,
                     clerk_biopsy_low=0, clerk_biopsy_high=0,
                     external_biopsy=0, family_biopsy=0)
    inp = course(params)
    slots, _, _ = month_solve_day(inp)
    follows, biopsies, weekly = counts(slots)
    assert not biopsies
    assert all(4 <= follows[p] <= 6 for p in ("C1", "C2"))
    assert follows["E"] == 6
    assert follows["F"] == 4
    assert all(1 <= weekly[w, p] <= 3 for w in {(2026, 41), (2026, 42)} for p in inp.pgy_roster)
    warnings = training_warnings(inp, {})
    assert any("最低 1、目標 3、上限 3" in w for w in warnings)
    assert any("外訓 E" in w and "最低 4、目標 6、上限 8" in w for w in warnings)
    assert not any("切片室" in w for w in warnings)


def test_zero_pgy_limit_survives_refill_and_workload_balance():
    inp = course(replace(TrainingParams(), pgy_low=0, pgy_target=0, pgy_high=0))
    slots, _, _ = month_solve_day(inp)
    follows, _, _ = counts(slots)
    assert all(follows[p] == 0 for p in inp.pgy_roster)


def test_lower_limits_preserve_locked_excess_without_adding_more():
    inp = course(replace(TrainingParams(), pgy_low=0, pgy_target=0, pgy_high=0,
                         clerk_low=0, clerk_target=0, clerk_high=0))
    inp.locked = {"2026-10-05": {"上午": {"101": ["P1", "C1"]}}}
    slots, _, _ = month_solve_day(inp)
    assert slots["2026-10-05"]["上午"] == inp.locked["2026-10-05"]["上午"]
    follows, _, _ = counts(slots)
    assert follows["P1"] == follows["C1"] == 1
    assert follows["C2"] == 0


def test_cross_month_clerk_budget_uses_configured_limit():
    from cmuh_common.roster.follow_priority import clerk_month_caps
    batch = ClerkBatch("B", date(2026, 9, 28), ["C"])
    inp = DaySolveInput("2026-09", {}, [], clerk_batches=[batch],
                        training=replace(TrainingParams(), clerk_low=3, clerk_target=4, clerk_high=5))
    assert clerk_month_caps(inp, batch) == {"C": 2}
    inp.course_fixed = {"2026-10-01": {"上午": {"101": ["C"]}}}
    assert clerk_month_caps(inp, batch) == {"C": 3}


def test_custom_biopsy_minimum_and_half_month_targets():
    inp = course(replace(TrainingParams(), clerk_biopsy_low=3, clerk_biopsy_high=3,
                         external_biopsy=2, family_biopsy=0))
    slots, _, _ = month_solve_day(inp)
    _, biopsies, _ = counts(slots)
    assert biopsies["C1"] == biopsies["C2"] == 3
    assert biopsies["F"] == 0
    # Both half-month periods are present in this course's grid.
    assert biopsies["E"] == 4


def test_course_explanation_uses_configured_thresholds(tmp_path):
    svc = service(tmp_path)
    base = TrainingParams().to_dict()
    svc.set_training_params({**base, "pgy_low": 2, "pgy_target": 3, "pgy_high": 4,
                             "clerk_low": 4, "clerk_target": 5, "clerk_high": 6}, baseline=base)
    svc.storage.save_clerk_batches([{"id": "B", "start_monday": "2026-10-05", "members": ["C"]}])
    data = svc.day_current_course_stats("2026-10")
    explanation = data["explanation"]
    assert explanation.pgy[0].weekly_band == (2, 3, 4)
    clerk = next(row for row in explanation.training if row.scope == "Clerk")
    assert (clerk.minimum, clerk.target, clerk.maximum) == (4, 5, 6)
    diagnostics = data["diagnostics"]
    pgy = next(r for r in diagnostics.rows if r.scope == "PGY")
    assert (pgy.minimum, pgy.target) == (2, 3)
    clerk = next(r for r in diagnostics.rows if r.scope == "Clerk" and r.kind == "跟診")
    assert (clerk.minimum, clerk.target) == (4, 5)


def test_editor_layout_fits_narrow_window(editor, tk_root):
    widget, _, _, _ = editor
    tk_root.geometry("680x510")
    tk_root.update()
    for child in widget.winfo_children():
        assert child.winfo_x() >= 0
        assert child.winfo_x() + child.winfo_width() <= widget.winfo_width()
    import os
    if directory := os.environ.get("CMUH_TEST_SCREENSHOTS"):
        from pathlib import Path
        from PIL import ImageGrab
        tk_root.deiconify()
        tk_root.update()
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        x, y = widget.winfo_rootx(), widget.winfo_rooty()
        ImageGrab.grab(bbox=(x, y, x + widget.winfo_width(), y + widget.winfo_height())).save(
            path / "training-settings.png")


def test_unproven_fallback_cannot_bypass_pgy_limit(monkeypatch):
    from ortools.sat.python import cp_model
    from cmuh_common.roster.course_balance import finish_courses
    inp = course(replace(TrainingParams(), pgy_low=0, pgy_target=0, pgy_high=0))
    monkeypatch.setattr(cp_model.CpSolver, "solve", lambda *a, **k: cp_model.UNKNOWN)
    original = {"2026-10-05": {"上午": {"101": ["P1"]}}}
    with pytest.raises(ValueError, match="PGY"):
        finish_courses(inp, original, [], [])
    assert original == {"2026-10-05": {"上午": {"101": ["P1"]}}}


@pytest.fixture
def editor(tk_root, tmp_path, monkeypatch):
    from cmuh_common.roster.ui import training_settings as module
    errors = []
    monkeypatch.setattr(module.messagebox, "showerror", lambda *a, **k: errors.append(a))
    monkeypatch.setattr(module.messagebox, "showwarning", lambda *a, **k: errors.append(a))
    svc = service(tmp_path)
    changed = []
    widget = TrainingSettings(tk_root, svc, svc.storage.load_config(), lambda: changed.append(True))
    widget.pack(fill="x")
    tk_root.update()
    yield widget, svc, errors, changed
    widget.destroy()


def test_editor_requires_explicit_save_and_preserves_unfinished_input(editor):
    widget, svc, errors, changed = editor
    widget.variables["clerk_low"].set("")
    widget.reload({"training_params": {"family_low": 20}})
    assert widget.variables["clerk_low"].get() == ""
    widget.save_button.invoke()
    assert errors and not changed
    assert "training_params" not in svc.storage.load_config()
    widget.variables["clerk_low"].set("8")
    widget.save_button.invoke()
    assert svc.storage.load_config()["training_params"]["clerk_low"] == 8
    assert len(changed) == 1 and not widget.dirty()
    widget.defaults()
    assert widget.variables["clerk_low"].get() == "9" and widget.dirty()
    assert svc.storage.load_config()["training_params"]["clerk_low"] == 8


def test_editor_conflict_preserves_input_until_explicit_reload(editor, monkeypatch):
    from cmuh_common.roster.ui import training_settings as module
    widget, svc, errors, changed = editor
    baseline = TrainingParams().to_dict()
    widget.variables["pgy_high"].set("4")
    svc.set_training_params({**baseline, "pgy_high": 3}, baseline=baseline)
    widget.save()
    assert errors and not changed
    assert widget.variables["pgy_high"].get() == "4"
    assert widget.dirty()
    monkeypatch.setattr(module.messagebox, "askyesno", lambda *a, **k: False)
    widget.reload_from_disk()
    assert widget.variables["pgy_high"].get() == "4"
    monkeypatch.setattr(module.messagebox, "askyesno", lambda *a, **k: True)
    widget.reload_from_disk()
    assert widget.variables["pgy_high"].get() == "3" and not widget.dirty()
