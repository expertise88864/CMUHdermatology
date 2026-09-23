"""Benchmark validity: a documented two-PGY exception is not a hard failure."""

from copy import deepcopy

from scripts.benchmark_day_roster import (
    _activity_loss_issues, _hard_checks, make_case, run_once,
)

from cmuh_common.roster.solve_day import REST, month_solve_day


def test_two_pgy_photo_only_sessions_pass_hard_checks():
    inp = make_case("pgy2")
    slots, _log, _warnings = month_solve_day(inp)
    assert _hard_checks(inp, slots) == []
    sample = run_once(inp)
    assert sample["error"] is None
    assert sample["quality"]["hard_issues"] == []
    assert all(s["stage"] and s["status"] for s in sample["solver_statuses"])


def test_fixture_profiles_cover_requested_stress_counts():
    cross = make_case("pgy4_clerk4_cross")
    mixed = make_case("pgy4_clerk5_mix1")
    two_each = make_case("pgy4_mix2")
    assert [len(b.members) for b in cross.clerk_batches] == [4, 4]
    assert [len(b.members) for b in mixed.clerk_batches] == [5, 5]
    assert (len(mixed.external_roster), len(mixed.family_roster)) == (1, 1)
    assert (len(two_each.external_roster), len(two_each.family_roster)) == (2, 2)


def test_hard_checks_reject_follow_in_a_closed_room():
    inp = make_case("pgy2")
    slots, _log, _warnings = month_solve_day(inp)
    bad = deepcopy(slots)
    first = next(iter(inp.grid)).isoformat()
    bad.setdefault(first, {}).setdefault("上午", {})["999"] = ["CLOSED"]
    assert any(issue.endswith("/999:closed-room")
               for issue in _hard_checks(inp, bad))


def test_hard_checks_reject_family_outside_selected_half_day():
    inp = make_case("pgy4_mix2")
    slots, _log, _warnings = month_solve_day(inp)
    bad = deepcopy(slots)
    bad.setdefault("2026-10-01", {}).setdefault("上午", {}).setdefault("101", []).append("F1")
    assert "2026-10-01/上午/F1:family-unavailable" in _hard_checks(inp, bad)

    resting = deepcopy(slots)
    resting.setdefault("2026-10-01", {}).setdefault("上午", {}).setdefault(REST, []).append("F1")
    assert "2026-10-01/上午/F1:family-unavailable" not in _hard_checks(inp, resting)


def test_activity_detector_reports_a_created_full_day_rest():
    issues = _activity_loss_issues(
        "pgy_balance",
        {("2026-10-05", "P1"), ("2026-10-05", "P2")},
        {("2026-10-05", "P2")},
    )

    assert issues == [
        "2026-10-05/P1:pgy-full-day-rest-created-by-pgy_balance"
    ]
