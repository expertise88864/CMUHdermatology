"""The benchmark gate must report actual quality loss, not just elapsed time."""

from copy import deepcopy
import json
import sys

import pytest

from scripts.benchmark_day_roster import _hard_checks, _quality, make_case, run_once
from scripts.compare_day_roster import compare_reports, main, run_paired
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
    with pytest.raises(ValueError, match="revision changed"):
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
