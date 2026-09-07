from collections import Counter
from copy import deepcopy
from datetime import date

import pytest

from cmuh_common.roster.course_balance import balance_rooms, external_quota_warnings
from cmuh_common.roster.model import ClerkBatch
from cmuh_common.roster.solve_day import (
    BIOPSY, PHOTO, TREATMENT, REST, DaySolveInput, month_solve_day,
    _month_solve_attendance, day_input_fingerprint, is_follow_slot,
)
from cmuh_common.roster.service import RosterService
from cmuh_common.roster.storage import RosterStorage, validate_authoritative_month


def make_input(people=("外訓1", "外訓2")):
    grid = {date(2026, 9, n): {"上午": ["101", "102", "103"],
                             "下午": [] if date(2026, 9, n).weekday() == 2
                             else ["101", "102", "103"]}
            for n in range(1, 31) if date(2026, 9, n).weekday() < 5}
    return DaySolveInput("2026-09", grid, ["P1", "P2"], external_roster=list(people))


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_monthly_external_quota_and_station_restrictions(n):
    pytest.importorskip("ortools")
    inp = make_input(tuple(f"外訓{i}" for i in range(n)))
    slots, _, warnings = month_solve_day(inp)
    assert not external_quota_warnings(inp, slots)
    assert not [w for w in warnings if "外訓" in w]
    for iso, sessions in slots.items():
        assert iso[:7] == inp.ym
        for cells in sessions.values():
            assigned = [p for ps in cells.values() for p in ps]
            assert len(assigned) == len(set(assigned))
            for special in (PHOTO, TREATMENT):
                assert not set(cells.get(special, [])) & set(inp.external_roster)
            assert len(cells.get(BIOPSY, [])) <= 1
            assert all(len(ps) <= inp.capacity for r, ps in cells.items() if is_follow_slot(r))
    for p in inp.external_roster:
        counts = Counter(r for ss in slots.values() for cells in ss.values()
                         for r, ps in cells.items() if is_follow_slot(r) and p in ps)
        assert set(counts) == {"101", "102", "103"}
        assert max(counts.values()) - min(counts.values()) <= 1
    assert slots == month_solve_day(inp)[0]


def test_leave_locks_future_reservation_and_fingerprint():
    pytest.importorskip("ortools")
    inp = make_input(("外訓1",))
    before = day_input_fingerprint(inp)
    inp.leaves = {"external": {"外訓1": {date(2026, 9, 8)}}}
    inp.locked = {"2026-09-11": {"上午": {BIOPSY: ["外訓1"]}},
                  "2026-09-09": {"上午": {"101": ["外訓1"]}},
                  "2026-09-01": {"上午": {}, "下午": {}}}
    original = deepcopy(inp)
    slots, _, warnings = month_solve_day(inp)
    assert inp == original
    assert before != day_input_fingerprint(inp)
    for iso, sessions in inp.locked.items():
        for s, cells in sessions.items():
            assert slots[iso][s] == cells
    assert all("外訓1" not in ps for cells in slots["2026-09-08"].values() for ps in cells.values())
    assert not [w for w in warnings if "不在本月" in w]
    assert not external_quota_warnings(inp, slots)


def test_impossible_quotas_warn_and_do_not_overfill():
    pytest.importorskip("ortools")
    inp = make_input(("外訓1",))
    inp.grid = {d: {s: [] for s in ss} for d, ss in inp.grid.items()}
    slots, _, warnings = month_solve_day(inp)
    assert any("外訓" in w and "跟診 0" in w for w in warnings)
    assert not any(is_follow_slot(r) for ss in slots.values() for cells in ss.values() for r in cells)


def test_room_balance_keeps_attendance_duties_and_clerk_course_boundaries():
    inp = make_input(())
    inp.clerk_batches = [ClerkBatch("B1", date(2026, 8, 31), ["C1", "C2"]),
                         ClerkBatch("B2", date(2026, 9, 14), ["C1", "C2"])]
    inp.prior_sessions = {"2026-08-31": {"上午": {"101": ["C1"]},
                                         "下午": {"101": ["C1"]}}}
    before = _month_solve_attendance(inp)[0]
    after = month_solve_day(inp)[0]
    for iso, ss in before.items():
        for s, cells in ss.items():
            new = after[iso][s]
            for r in (PHOTO, TREATMENT, BIOPSY, REST):
                assert cells.get(r) == new.get(r)
            assert sorted(p for r, ps in cells.items() if is_follow_slot(r) for p in ps) == sorted(
                p for r, ps in new.items() if is_follow_slot(r) for p in ps)
    for b in inp.clerk_batches:
        all_slots = {**inp.prior_sessions, **after}
        for p in b.members:
            counts = Counter(r for iso, ss in all_slots.items() if b.covers(date.fromisoformat(iso))
                             for cells in ss.values() for r, ps in cells.items()
                             if is_follow_slot(r) and p in ps)
            assert max(counts.values()) - min(counts.values()) <= 1


def test_balancing_counts_future_fixed_without_mutating_it():
    inp = make_input(())
    inp.clerk_batches = [ClerkBatch("B", date(2026, 9, 28), ["C"])]
    inp.course_fixed = {"2026-10-01": {"上午": {"101": ["C"]}, "下午": {"101": ["C"]}}}
    slots = {"2026-09-28": {"上午": {"101": ["C"]}},
             "2026-09-29": {"上午": {"101": ["C"]}}}
    fixed = deepcopy(inp.course_fixed)
    balance_rooms(inp, slots)
    assert inp.course_fixed == fixed
    assert sorted(r for ss in slots.values() for cells in ss.values() for r in cells) == ["102", "103"]


def test_service_roundtrip_merge_identity_stats_and_manual_validation(tmp_path):
    service = RosterService(RosterStorage(str(tmp_path)))
    ym = "2026-09"
    service.set_pgy_month_roster(ym, ["P"], baseline=[])
    service.set_external_month_roster(ym, ["外訓1"], baseline=[])
    service.set_external_month_roster(ym, ["外訓2"], baseline=[])
    assert service.build_day_input(ym).external_roster == ["外訓1", "外訓2"]
    with pytest.raises(ValueError):
        service.set_external_month_roster(ym, ["P"], baseline=[])
    with pytest.raises(ValueError):
        service.set_external_month_roster(ym, ["X", "X"], baseline=[])
    inp = service.build_day_input(ym)
    good = {"2026-09-01": {"上午": {BIOPSY: ["外訓1"], "101": ["外訓2"]}}}
    assert service.validate_day_structure(ym, inp=inp, day_slots=good) == []
    bad = {"2026-09-01": {"上午": {PHOTO: ["外訓1"]}}}
    assert service.validate_day_structure(ym, inp=inp, day_slots=bad)
    stats = service.day_course_stats(ym, day_slots_override=good)["external"]
    assert stats["stats"]["外訓1"]["biopsy"] == 1
    assert stats["stats"]["外訓2"]["follow"] == 1


def test_reverse_identity_edits_and_nonoverlapping_months(tmp_path):
    service = RosterService(RosterStorage(str(tmp_path)))
    service.set_external_month_roster("2026-09", ["X"], baseline=[])
    with pytest.raises(ValueError):
        service.set_pgy_month_roster("2026-09", ["X"], baseline=[])
    with pytest.raises(ValueError):
        service.set_pgy_default_members(["X"], baseline=[])
    batch = {"id": "B", "start_monday": "2026-08-31", "members": ["X"]}
    with pytest.raises(ValueError):
        service.add_clerk_batch(batch)
    service.add_clerk_batch({**batch, "members": ["C"]})
    with pytest.raises(ValueError):
        service.update_clerk_batches(lambda bs: bs[0].update(members=["X"]))
    assert service.storage.load_clerk_batches()[0]["members"] == ["C"]
    service.set_pgy_month_roster("2026-10", ["X"], baseline=[])
    service.add_clerk_batch({"id": "OLD", "start_monday": "2026-08-03", "members": ["X"]})


@pytest.mark.parametrize("bad", ["外訓1", {}, 0, [None], [1], [""], ["X", "X"]])
def test_invalid_external_roster_is_rejected_at_authoritative_boundary(tmp_path, bad):
    raw = {"external_month_roster": bad}
    with pytest.raises(ValueError, match="external_month_roster"):
        validate_authoritative_month("2026-09", raw)
    service = RosterService(RosterStorage(str(tmp_path)))
    month = service.storage.load_month("2026-09")
    month.update(raw)
    service.storage.save_month("2026-09", month)
    with pytest.raises(ValueError, match="external_month_roster"):
        service.build_day_input("2026-09")


def test_clean_remote_merge_conflict_is_visible_before_first_schedule(tmp_path):
    service = RosterService(RosterStorage(str(tmp_path)))
    service.set_external_month_roster("2026-09", ["X"], baseline=[])
    month = service.storage.load_month("2026-09")
    month["pgy_month_roster"] = ["X"]
    service.storage.save_month("2026-09", month)  # Simulate independently merged files.
    assert not month.get("day_slots")
    assert any("外訓" in w and "X" in w for w in service.quick_validate_day("2026-09"))
    assert any("外訓" in w and "X" in w for w in service.validate_roster_identity_invariants())


def test_malformed_day_key_does_not_hide_other_warnings(tmp_path):
    service = RosterService(RosterStorage(str(tmp_path)))
    service.set_external_month_roster("2026-09", ["X"], baseline=[])
    month = service.storage.load_month("2026-09")
    month["day_slots"] = {
        "2026-09": {"上午": {"101": ["X"]}},
        "2026-09-01": {"上午": {"101": ["UNKNOWN"]}},
    }
    service.storage.save_month("2026-09", month)

    warnings = service.quick_validate_day("2026-09")

    assert any("外訓 X" in warning for warning in warnings)
    assert any("UNKNOWN" in warning for warning in warnings)


def test_malformed_locked_day_key_does_not_break_external_solver():
    pytest.importorskip("ortools")
    inp = make_input(("外訓1",))
    inp.locked["2026-09"] = {"上午": {BIOPSY: ["外訓1"]}}

    slots, _, warnings = month_solve_day(inp)

    assert slots
    assert isinstance(warnings, list)


def test_grid_external_locks_do_not_influence_room_fairness():
    inp = make_input(())
    baseline = {"2026-09-07": {"上午": {"101": ["P1"]}},
                "2026-09-08": {"上午": {"101": ["P1"]}}}
    extra = {"2026-09-05": {"上午": {"102": ["P1"]}, "下午": {"102": ["P1"]}}}
    locked = {**deepcopy(baseline), **deepcopy(extra)}
    balance_rooms(inp, baseline)
    balance_rooms(inp, locked)
    assert {i: ss for i, ss in locked.items() if i not in extra} == baseline
    assert locked["2026-09-05"] == extra["2026-09-05"]
