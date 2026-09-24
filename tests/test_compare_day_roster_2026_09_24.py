"""The benchmark gate must report actual quality loss, not just elapsed time."""

from copy import deepcopy
from datetime import date
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.benchmark_day_roster import _hard_checks, _quality, make_case, run_once
from scripts.compare_day_roster import (
    PairedRunError, _run_once, _safe_manual_reduction, compare_reports, main,
    run_paired,
)
from cmuh_common.roster.course_balance import _use_priority_hints
from cmuh_common.roster.model import ClerkBatch
from cmuh_common.roster import pgy_balance
from cmuh_common.roster.solve_day import month_solve_day


def _paired_reports():
    sample = run_once(make_case("pgy2"))
    environment = {
        "python": "test", "platform": "test", "cpu_count": 1,
        "ortools": "test", "python_hash_seed": "0", "source_dirty": False,
        "relevant_source_dirty": False,
    }
    report = {"environment": environment, "cases": {
        "pgy2": {"input_fingerprint": "same", "samples": [sample]},
    }}
    return report, deepcopy(report)


def test_identical_results_pass_quality_gate():
    before, after = _paired_reports()
    result = compare_reports(before, after, min_samples=1)
    assert result["quality_gate_passed"]
    assert not result["performance_target_met"]  # stress cases were not measured
    assert not result["measurement_gate_passed"]
    assert not result["repeatable_improvement"]


def test_cli_rejects_incomplete_adoption_evidence(tmp_path, monkeypatch):
    before, after = _paired_reports()
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(json.dumps(before), encoding="utf-8")
    candidate_path.write_text(json.dumps(after), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "compare_day_roster.py", "--baseline-json", str(baseline_path),
        "--candidate-json", str(candidate_path), "--samples", "1",
        "--output", str(tmp_path / "comparison.json"),
    ])
    assert main() == 1


def test_paired_measurement_rejects_revision_change(tmp_path, monkeypatch):
    from scripts import compare_day_roster

    calls = 0

    def fake_run(_script, _root, name, _warmup, _output):
        nonlocal calls
        calls += 1
        return {"environment": {"revision": "changed" if calls == 3 else "base",
                                "warmups": 1 if calls <= 2 else 0},
                "cases": {name: {"input_fingerprint": "same", "samples": [],
                                 "warmup_seconds": []}}}

    monkeypatch.setattr(compare_day_roster, "_run_once", fake_run)
    with pytest.raises(PairedRunError, match="revision changed"):
        run_paired(tmp_path, tmp_path, samples=2, cases=("pgy2",))


def test_capacity_and_leave_regressions_are_detected():
    inp = make_case("pgy2")
    slots, _log, _warnings = month_solve_day(inp)
    first = next(iter(inp.grid))
    bad = deepcopy(slots)
    bad[first.isoformat()]["上午"]["101"] = ["P1", "P2", "P3"]
    assert any(issue.endswith("/101:capacity") for issue in _hard_checks(inp, bad))
    inp.session_leaves = {"pgy": {"P1": {(first, "上午")}}}
    assert any("on-leave" in issue for issue in _hard_checks(inp, bad))
    before, after = _paired_reports()
    after["cases"]["pgy2"]["samples"][0]["quality"]["hard_issues"] = [
        "2026-10-01/上午/101:capacity", "2026-10-01/上午/P1:on-leave",
    ]
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert any("hard constraint failure" in issue for issue in result["issues"])


def test_closed_days_and_two_pgy_photo_only_period_are_hard_checked():
    from cmuh_common.roster.solve_day import PHOTO, TREATMENT

    holiday_input = make_case("pgy4_offset")
    holiday = next(iter(holiday_input.holidays))
    bad_holiday = {holiday.isoformat(): {"上午": {PHOTO: ["P1"]}}}
    assert f"{holiday.isoformat()}:out-of-grid-assignment" in _hard_checks(
        holiday_input, bad_holiday)
    holiday_input.grid[holiday] = {"上午": [], "下午": []}
    assert f"{holiday.isoformat()}:closed-day-assignment" in _hard_checks(
        holiday_input, bad_holiday)
    assert "2026-11-01:out-of-grid-assignment" in _hard_checks(
        holiday_input, {"2026-11-01": {"上午": {PHOTO: ["P1"]}}})

    two_pgy_input = make_case("pgy2")
    day = next(d for d in two_pgy_input.grid if d.weekday() == 1)
    slots, _log, _warnings = month_solve_day(two_pgy_input)
    slots[day.isoformat()]["上午"][TREATMENT] = ["P2"]
    assert f"{day.isoformat()}/上午:unexpected-treatment" in _hard_checks(
        two_pgy_input, slots)


def test_workload_and_clerk_course_regressions_are_detected():
    before, after = _paired_reports()
    baseline_quality = before["cases"]["pgy2"]["samples"][0]["quality"]
    baseline_quality["clerk_courses"] = {
        "B1": {"members": {"C1": {"follow": 10, "biopsy": 1}}},
    }
    after_quality = after["cases"]["pgy2"]["samples"][0]["quality"]
    after_quality["clerk_courses"] = deepcopy(baseline_quality["clerk_courses"])
    after_quality["clerk_courses"]["B1"]["members"]["C1"]["follow"] = 8
    after_quality["sample_month_person_counts"]["P1"]["total"] -= 1
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert any("clerk_courses.B1.members.C1.follow" in issue
               for issue in result["issues"])
    assert any("sample_month_person_counts.P1.total" in issue
               for issue in result["issues"])


def test_layer_status_and_warning_contents_are_checked():
    before, after = _paired_reports()
    candidate = after["cases"]["pgy2"]["samples"][0]
    candidate["solver_statuses"][0]["status"] = "UNKNOWN"
    candidate["warnings"].append("invented warning")
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert any("status" in issue for issue in result["issues"])
    assert any("warning contents" in issue for issue in result["issues"])


def test_layer_objective_and_proof_bound_are_compared():
    before, after = _paired_reports()
    candidate = after["cases"]["pgy2"]["samples"][0]
    layer = candidate["solver_statuses"][0]
    layer["objective"] += 1
    layer["best_bound"] -= 1
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert any("objective" in issue for issue in result["issues"])
    assert result["proof_changes"][0]["candidate_bound"] == layer["best_bound"]
    assert result["layers"][0]["baseline_bound"] is not None


def test_new_failed_layer_and_missing_objective_are_rejected():
    before, after = _paired_reports()
    after["cases"]["pgy2"]["samples"][0]["solver_statuses"].append({
        "stage": "training", "phase_index": 99, "status": "UNKNOWN",
        "objective": None, "best_bound": None,
    })
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert any("added layer" in issue for issue in result["issues"])

    before, after = _paired_reports()
    after["cases"]["pgy2"]["samples"][0]["solver_statuses"][0]["objective"] = None
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert any("objective missing" in issue for issue in result["issues"])


def test_gate_requires_hard_and_solver_layer_evidence():
    before, after = _paired_reports()
    for report in (before, after):
        report["cases"]["pgy2"]["samples"][0]["quality"].pop("hard_issues")
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert any("missing hard constraint evidence" in issue for issue in result["issues"])

    before, after = _paired_reports()
    for report in (before, after):
        report["cases"]["pgy2"]["samples"][0]["solver_statuses"] = []
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert any("missing or malformed solver layer evidence" in issue
               for issue in result["issues"])


def test_better_priority_can_change_schedule_but_not_lose_minimum():
    before, after = _paired_reports()
    baseline = before["cases"]["pgy2"]["samples"][0]
    candidate = after["cases"]["pgy2"]["samples"][0]
    baseline["solver_statuses"][0]["objective"] = 1.0
    candidate["solver_statuses"][0]["objective"] = 0.0
    candidate["quality"]["daily_follows"]["P1"] = {"2026-10-01": 1}
    result = compare_reports(before, after, min_samples=1)
    assert result["quality_gate_passed"]
    assert result["schedule_changes"][0]["priority_improved"]

    baseline["quality"]["clerk_courses"] = {
        "B1": {"members": {"C1": {"follow": 9, "biopsy": 1}}},
    }
    candidate["quality"]["clerk_courses"] = {
        "B1": {"members": {"C1": {"follow": 8, "biopsy": 1}}},
    }
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert any("minimum worsened" in issue for issue in result["issues"])


def test_downstream_balance_gain_cannot_hide_schedule_changes():
    before, after = _paired_reports()
    baseline = before["cases"]["pgy2"]["samples"][0]
    candidate = after["cases"]["pgy2"]["samples"][0]
    old_balance = next(s for s in baseline["solver_statuses"]
                       if s["stage"] == "pgy_balance" and s["objective"] > 0)
    new_balance = next(s for s in candidate["solver_statuses"]
                       if s["stage"] == "pgy_balance"
                       and s["phase_index"] == old_balance["phase_index"])
    new_balance["objective"] = old_balance["objective"] - 1
    candidate["quality"]["daily_follows"]["P1"] = {"2026-10-01": 1}
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert not result["schedule_changes"][0]["priority_improved"]
    assert any("daily_follows.P1" in issue for issue in result["issues"])

def test_cross_month_clerk_credit_does_not_inflate_monthly_external_biopsy():
    inp = make_case("pgy4_clerk4_cross")
    inp.external_roster = ["E1"]
    inp.prior_sessions = {"2026-09-29": {"上午": {
        "101": ["C1_1"], "切片室": ["C1_2", "E1"],
    }}}
    quality = _quality(inp, {})
    course = quality["clerk_courses"]["B1"]["members"]
    assert course["C1_1"]["follow"] == 1
    assert course["C1_2"]["biopsy"] == 1
    assert course["C1_2"]["biopsy_by_course_week"] == {"1": 1}
    assert quality["training"]["E1"]["biopsy_by_half"] == {}


def test_cross_month_benchmark_includes_prior_course_work():
    inp = make_case("pgy4_clerk4_cross")
    assert "2026-09-28" in inp.prior_sessions
    quality = _quality(inp, {})
    assert quality["clerk_courses"]["B1"]["members"]["C1_1"]["follow"] == 2
    assert quality["clerk_courses"]["B1"]["members"]["C1_3"]["biopsy"] == 1


def test_course_completion_uses_last_workday_not_trailing_sunday():
    inp = make_case("pgy4_clerk5_mix1")
    course = _quality(inp, {})["clerk_courses"]["B2"]
    assert course["end"] == "2026-11-01"
    assert course["complete"]  # Its last working day is 2026-10-30.

    inp.clerk_batches = [ClerkBatch("B3", date(2026, 10, 26), ["C3"])]
    assert not _quality(inp, {})["clerk_courses"]["B3"]["complete"]


def test_completed_course_minimum_cannot_hide_in_unchanged_combined_gap():
    before, after = _paired_reports()
    baseline = before["cases"]["pgy2"]["samples"][0]
    candidate = after["cases"]["pgy2"]["samples"][0]
    baseline["solver_statuses"][0]["objective"] = 1.0
    candidate["solver_statuses"][0]["objective"] = 0.0
    baseline["quality"]["clerk_courses"] = {
        "B2": {"complete": True, "members": {
            "C2_1": {"follow": 9, "biopsy": 1},
            "C2_2": {"follow": 9, "biopsy": 1},
        }},
    }
    candidate["quality"]["clerk_courses"] = deepcopy(
        baseline["quality"]["clerk_courses"])
    candidate["quality"]["clerk_courses"]["B2"]["members"]["C2_1"]["follow"] = 8
    candidate["quality"]["clerk_courses"]["B2"]["members"]["C2_2"]["follow"] = 10
    result = compare_reports(before, after, min_samples=1)
    assert not result["quality_gate_passed"]
    assert any("B2.C2_1.follow: minimum worsened" in issue
               for issue in result["issues"])
    assert not any("combined target gap worsened" in issue
                   for issue in result["issues"])


def test_priority_hints_only_for_large_clerk_course_without_adjacent_work():
    inp = make_case("pgy4_clerk5_mix1")
    assert _use_priority_hints(inp)
    inp.prior_sessions = {"2026-09-30": {"上午": {"101": ["unrelated"]}}}
    assert _use_priority_hints(inp)
    inp.prior_sessions["2026-09-30"]["上午"]["101"] = ["C2_1"]
    assert not _use_priority_hints(inp)
    inp.prior_sessions = {}
    inp.course_fixed = {"2026-11-30": {"上午": {"101": ["C2_1"]}}}
    assert not _use_priority_hints(inp)  # Conservatively exclude code reuse.
    inp.course_fixed = {}
    for batch in inp.clerk_batches:
        batch.members = batch.members[:3]
    assert not _use_priority_hints(inp)
    assert not _use_priority_hints(make_case("pgy4_mix2"))


def test_child_benchmark_error_exposes_stderr(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(7, "benchmark", stderr="synthetic failure")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        _run_once(tmp_path / "bench.py", tmp_path, "pgy2", False,
                  tmp_path / "out.json")


def test_failed_pair_writes_incomplete_partial_report(tmp_path, monkeypatch):
    from scripts import compare_day_roster

    report, _ = _paired_reports()
    report["cases"]["pgy2"]["warmup_seconds"] = []
    calls = 0

    def fake_run(_script, _root, _name, _warmup, _out):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic child failure")
        return report

    monkeypatch.setattr(compare_day_roster, "_run_once", fake_run)
    out = tmp_path / "partial.json"
    monkeypatch.setattr(sys, "argv", [
        "compare_day_roster.py", "--baseline-root", str(tmp_path),
        "--candidate-root", str(tmp_path), "--samples", "1",
        "--case", "pgy2", "--output", str(out),
    ])
    assert main() == 1
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["status"] == "incomplete"
    assert "synthetic child failure" in saved["error"]
    assert len(saved["baseline"]["cases"]["pgy2"]["samples"]) == 1
    assert saved["candidate"]["cases"] == {}


def test_changed_environment_writes_incomplete_partial_report(tmp_path, monkeypatch):
    from scripts import compare_day_roster

    sample = run_once(make_case("pgy2"))
    calls = 0

    def fake_run(_script, _root, _name, _warmup, _out):
        nonlocal calls
        calls += 1
        return {"environment": {"python": "changed" if calls == 3 else "same"},
                "cases": {"pgy2": {"input_fingerprint": "same",
                                    "samples": [sample], "warmup_seconds": []}}}

    monkeypatch.setattr(compare_day_roster, "_run_once", fake_run)
    out = tmp_path / "partial.json"
    monkeypatch.setattr(sys, "argv", [
        "compare_day_roster.py", "--baseline-root", str(tmp_path),
        "--candidate-root", str(tmp_path), "--samples", "2",
        "--case", "pgy2", "--output", str(out),
    ])
    assert main() == 1
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["status"] == "incomplete"
    assert "environment or revision changed" in saved["error"]
    assert len(saved["baseline"]["cases"]["pgy2"]["samples"]) == 1


def test_manual_photo_reduction_reduces_actual_monthly_pgy_work():
    results = {}
    for offset in (0, -1, -2):
        inp = make_case("pgy4_offset")
        inp.pgy_photo_offsets = {"P1": offset} if offset else {}
        slots, _log, warnings = month_solve_day(inp)
        assert not _hard_checks(inp, slots)
        quality = _quality(inp, slots)
        counts = quality["sample_month_person_counts"]["P1"]
        assert all(n >= 1 for n in quality["weekly_follows"]["P1"].values())
        assert not any("P1 總工作量減量目標未達" in w for w in warnings)
        results[offset] = counts

    assert [results[offset]["total"] for offset in (0, -1, -2)] == [38, 37, 36]
    assert [results[offset]["photo"] for offset in (0, -1, -2)] == [10, 9, 9]
    assert [results[offset]["tx"] for offset in (0, -1, -2)] == [10, 10, 9]


def test_manual_pgy_reduction_preserves_higher_priority_trainees():
    qualities = {}
    for offset in (0, -1):
        inp = make_case("pgy4_clerk5_mix1")
        inp.pgy_photo_offsets = {"P1": offset} if offset else {}
        slots, _log, _warnings = month_solve_day(inp)
        assert not _hard_checks(inp, slots)
        qualities[offset] = _quality(inp, slots)

    assert qualities[-1]["clerk_courses"] == qualities[0]["clerk_courses"]
    assert qualities[-1]["training"] == qualities[0]["training"]
    zero = qualities[0]["sample_month_person_counts"]["P1"]
    reduced = qualities[-1]["sample_month_person_counts"]["P1"]
    assert reduced["photo"] == zero["photo"] - 1
    assert reduced["total"] == zero["total"] - 1


def test_manual_reduction_keeps_apply_seat_and_distinct_doctors(monkeypatch):
    wednesday, tuesday = date(2026, 10, 7), date(2026, 10, 6)
    inp = SimpleNamespace(
        ym="2026-10", pgy_photo_offsets={"P1": -1},
        grid={wednesday, tuesday}, locked={}, leaves={}, session_leaves={},
        apply_pref={"P1"},
        clinic_doctors={
            wednesday: {"上午": {"102": "D2"}, "下午": {"103": "D2"}},
            tuesday: {"上午": {"104": "D1"}, "下午": {"101": "D1"}},
        },
    )
    slots = {
        wednesday.isoformat(): {
            "上午": {"102": ["P1"]}, "下午": {"103": ["P1"]}},
        tuesday.isoformat(): {
            "上午": {"104": ["P1"]}, "下午": {"101": ["P1"]}},
    }
    monkeypatch.setattr(
        pgy_balance, "_manual_target",
        lambda _counts, _people, _offsets, _person, kind:
        0 if kind == "photo" else 3,
    )
    log = []
    pgy_balance._reduce_manual_pgy_follow(inp, slots, ["P1", "P2", "P3"], log)
    assert len(log) == 1
    assert slots[tuesday.isoformat()]["下午"]["101"] == ["P1"]
    assert slots[tuesday.isoformat()]["上午"]["104"] == []
    assert sum("P1" in members for sessions in slots.values()
               for cells in sessions.values() for room, members in cells.items()
               if room != pgy_balance.REST) == 3

    # Without the preference, the ordinary tie-break removes the earlier
    # Tuesday afternoon 101 seat, so this fixture exercises the preference.
    inp.apply_pref = set()
    no_pref = {
        wednesday.isoformat(): {
            "上午": {"102": ["P1"]}, "下午": {"103": ["P1"]}},
        tuesday.isoformat(): {
            "上午": {"104": ["P1"]}, "下午": {"101": ["P1"]}},
    }
    pgy_balance._reduce_manual_pgy_follow(
        inp, no_pref, ["P1", "P2", "P3"], [])
    assert no_pref[tuesday.isoformat()]["下午"]["101"] == []
    inp.apply_pref = {"P1"}

    # If every clinic is with a different known doctor, no removal may erase
    # a doctor's only visit merely to improve the manual-workload target.
    inp.clinic_doctors[wednesday]["下午"]["103"] = "D3"
    inp.clinic_doctors[tuesday]["下午"]["101"] = "D4"
    fresh = {
        wednesday.isoformat(): {
            "上午": {"102": ["P1"]}, "下午": {"103": ["P1"]}},
        tuesday.isoformat(): {
            "上午": {"104": ["P1"]}, "下午": {"101": ["P1"]}},
    }
    log = []
    pgy_balance._reduce_manual_pgy_follow(inp, fresh, ["P1", "P2", "P3"], log)
    assert not log
    assert all(["P1"] == fresh[iso][session][room]
               for iso, sessions in fresh.items()
               for session, cells in sessions.items()
               for room in cells)


def test_manual_reduction_keeps_locked_leave_and_weekly_minimum(monkeypatch):
    monday, tuesday = date(2026, 10, 5), date(2026, 10, 6)
    inp = SimpleNamespace(
        ym="2026-10", pgy_photo_offsets={"P1": -1},
        grid={monday, tuesday},
        locked={monday.isoformat(): {"上午": True, "下午": True}},
        leaves={"pgy": {}}, session_leaves={}, apply_pref=set(),
        clinic_doctors={
            monday: {"上午": {"101": "D1"}, "下午": {"102": "D1"}},
            tuesday: {"上午": {"103": "D1"}},
        },
    )
    slots = {
        monday.isoformat(): {
            "上午": {"101": ["P1"]}, "下午": {"102": ["P1"]}},
        tuesday.isoformat(): {"上午": {"103": ["P1"]}},
    }
    monkeypatch.setattr(pgy_balance, "_manual_target",
                        lambda *_args: 0)
    log = []
    pgy_balance._reduce_manual_pgy_follow(inp, slots, ["P1", "P2"], log)
    assert not log  # Only the locked or sole daily seat could be removed.
    inp.locked = {}
    pgy_balance._reduce_manual_pgy_follow(inp, slots, ["P1", "P2"], log)
    assert len(log) == 1
    assert sum("P1" in members for sessions in slots.values()
               for cells in sessions.values()
               for room, members in cells.items()
               if room != pgy_balance.REST) == 2
    pgy_balance._reduce_manual_pgy_follow(inp, slots, ["P1", "P2"], log)
    assert len(log) == 1  # A further cut would breach the weekly two-clinic floor.


def test_manual_reduction_excludes_biopsy_from_total_target(monkeypatch):
    tuesday, wednesday, thursday = (date(2026, 10, day) for day in (6, 7, 8))
    inp = SimpleNamespace(
        ym="2026-10", pgy_photo_offsets={"P1": -1},
        grid={tuesday, wednesday, thursday}, locked={}, leaves={},
        session_leaves={}, apply_pref=set(), clinic_doctors={},
    )
    slots = {
        tuesday.isoformat(): {
            "上午": {"101": ["P1"]}, "下午": {"102": ["P1"]}},
        wednesday.isoformat(): {
            "上午": {"103": ["P1"]}, "下午": {"104": ["P1"]}},
        thursday.isoformat(): {"上午": {"切片室": ["P1"]}},
    }
    monkeypatch.setattr(
        pgy_balance, "_manual_target",
        lambda _counts, _people, _offsets, _person, kind:
        0 if kind == "photo" else 3,
    )
    log = []
    pgy_balance._reduce_manual_pgy_follow(inp, slots, ["P1", "P2"], log)
    assert len(log) == 1
    assert sum("P1" in members for sessions in slots.values()
               for cells in sessions.values() for room, members in cells.items()
               if room not in (pgy_balance.REST, "切片室")) == 3


def test_comparator_accepts_only_proven_manual_pgy_reduction():
    inp = make_case("pgy4_offset")
    after = _quality(inp, month_solve_day(inp)[0])
    before = deepcopy(after)
    person = "P1"
    for field in ("follow", "total", "offset_adjusted_total"):
        before["sample_month_person_counts"][person][field] += 1
    before["pgy_workload"][person] += 1
    values = before["pgy_workload"].values()
    before["pgy_workload_range"] = max(values) - min(values)
    before["weekly_follows"][person]["2026-W43"] += 1
    before["daily_follows"][person]["2026-10-20"] = (
        before["daily_follows"][person].get("2026-10-20", 0) + 1)
    before["doctor_diversity"][person]["known_doctors"]["D1"] += 1
    warning = "PGY P1 總工作量減量目標未達：實排 38 次，調整後目標 37 次"

    assert _safe_manual_reduction(before, after, [warning], [])
    poorer = deepcopy(after)
    poorer["doctor_diversity"][person]["distinct_doctors"] -= 1
    assert not _safe_manual_reduction(before, poorer, [warning], [])
    poorer = deepcopy(after)
    poorer["clerk_courses"]["invented"] = {"members": {}}
    assert not _safe_manual_reduction(before, poorer, [warning], [])
    assert not _safe_manual_reduction(before, after, [warning], ["new warning"])
    no_offset_before, no_offset_after = deepcopy(before), deepcopy(after)
    for report in (no_offset_before, no_offset_after):
        report["sample_month_person_counts"][person]["photo_offset"] = 0
    assert not _safe_manual_reduction(
        no_offset_before, no_offset_after, [warning], [])
    too_large = deepcopy(before)
    for field in ("follow", "total", "offset_adjusted_total"):
        too_large["sample_month_person_counts"][person][field] += 1
    assert not _safe_manual_reduction(too_large, after, [warning], [])
    lost_week = deepcopy(after)
    week = next(iter(lost_week["weekly_follows"][person]))
    lost_week["weekly_follows"][person][week] = 0
    assert not _safe_manual_reduction(before, lost_week, [warning], [])
    new_day = deepcopy(after)
    new_day["daily_follows"][person]["2026-11-01"] = 1
    assert not _safe_manual_reduction(before, new_day, [warning], [])
