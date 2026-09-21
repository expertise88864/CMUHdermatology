from collections import Counter
from copy import deepcopy
from datetime import date

import pytest

from cmuh_common.roster.pgy_balance import balance_pgy
from cmuh_common.roster.service import RosterService
from cmuh_common.roster.solve_day import DaySolveInput, PHOTO, TREATMENT, day_input_fingerprint
from cmuh_common.roster.storage import RosterStorage


def example():
    days = [date(2026, 9, n) for n in range(1, 16) if date(2026, 9, n).weekday() < 5]
    inp = DaySolveInput("2026-09", {d: {"上午": ["101"]} for d in days}, ["A", "B", "C"])
    slots = {d.isoformat(): {"上午": {PHOTO: ["ABC"[i % 3]],
                                     TREATMENT: ["ABC"[(i + 1) % 3]],
                                     "101": ["ABC"[(i + 2) % 3]]}}
             for i, d in enumerate(days)}
    return inp, slots


def test_negative_photo_offset_and_treatment_balance():
    inp, slots = example()
    inp.pgy_photo_offsets = {"A": -1}
    before = deepcopy(slots)
    balance_pgy(inp, slots, [], [])
    photo, tx = Counter(), Counter()
    for iso, ss in slots.items():
        for session, cells in ss.items():
            assert {r: len(ps) for r, ps in cells.items()} == {
                r: len(ps) for r, ps in before[iso][session].items()}
            assert sorted(p for ps in cells.values() for p in ps) == ["A", "B", "C"]
            photo.update(cells[PHOTO])
            tx.update(cells[TREATMENT])
    assert photo == {"A": 3, "B": 4, "C": 4}
    assert max(tx.values()) - min(tx.values()) <= 1


def test_locks_and_achieved_weekly_clinic_minimum_are_preserved():
    inp, slots = example()
    inp.pgy_photo_offsets = {"A": -3}
    inp.locked = {"2026-09-01": deepcopy(slots["2026-09-01"])}
    before = deepcopy(slots)
    def clinics(schedule):
        return Counter((date.fromisoformat(iso).isocalendar()[:2], p)
                       for iso, ss in schedule.items() for cells in ss.values() for p in cells["101"])
    balance_pgy(inp, slots, [], [])
    assert slots["2026-09-01"] == before["2026-09-01"]
    actual = clinics(slots)
    assert all(actual[key] >= min(2, value) for key, value in clinics(before).items())


def test_monthly_offsets_merge_and_fingerprint(tmp_path):
    storage = RosterStorage(str(tmp_path))
    storage.save_config({"pgy_members": [{"id": p} for p in ("A", "B")],
                         "r_members": [], "vs_members": []})
    service = RosterService(storage)
    original = day_input_fingerprint(service.build_day_input("2026-09"))
    service.set_pgy_photo_offsets("2026-09", {"A": -1, "B": 0}, baseline={})
    service.set_pgy_photo_offsets("2026-09", {"A": 0, "B": 1}, baseline={})
    inp = service.build_day_input("2026-09")
    assert inp.pgy_photo_offsets == {"A": -1, "B": 1}
    assert day_input_fingerprint(inp) != original
    assert service.build_day_input("2026-10").pgy_photo_offsets == {}
    with pytest.raises(ValueError, match="其他電腦"):
        service.set_pgy_photo_offsets("2026-09", {"A": -2}, baseline={})
    with pytest.raises(ValueError):
        service.set_pgy_photo_offsets("2026-09", {"A": 0.5}, baseline={})


def test_zero_offsets_are_the_default():
    inp, slots = example()
    implicit = deepcopy(slots)
    balance_pgy(inp, implicit, [], [])
    inp.pgy_photo_offsets = {p: 0 for p in inp.pgy_roster}
    balance_pgy(inp, slots, [], [])
    assert slots == implicit


def test_unknown_solver_result_preserves_known_schedule(monkeypatch):
    from ortools.sat.python import cp_model
    inp, slots = example()
    inp.pgy_photo_offsets = {"A": -1}
    before, warnings = deepcopy(slots), []
    monkeypatch.setattr(cp_model.CpSolver, "solve", lambda *_a, **_k: cp_model.UNKNOWN)
    balance_pgy(inp, slots, [], warnings)
    assert slots == before
    assert any("已知最佳" in text for text in warnings)


def four_people(offsets):
    days = [date(2026, 9, n) for n in (1, 3, 4, 7, 8, 10)]
    people = ["A", "B", "C", "D"]
    inp = DaySolveInput("2026-09", {d: {s: ["101", "102"] for s in ("上午", "下午")} for d in days},
                        people, pgy_photo_offsets=offsets)
    slots = {}
    for i, (d, s) in enumerate((d, s) for d in days for s in ("上午", "下午")):
        slots.setdefault(d.isoformat(), {})[s] = {
            PHOTO: [people[i % 4]], TREATMENT: [people[(i + 1) % 4]],
            "101": [people[(i + 2) % 4]], "102": [people[(i + 3) % 4]]}
    return inp, slots


@pytest.mark.parametrize("offset,expected", [(-1, 2), (1, 4)])
def test_adjustment_is_effective_when_total_is_divisible(offset, expected):
    inp, slots = four_people({"A": offset})
    assert sum("A" in cells[PHOTO] for ss in slots.values() for cells in ss.values()) == 3
    balance_pgy(inp, slots, [], [])
    assert sum("A" in cells[PHOTO] for ss in slots.values() for cells in ss.values()) == expected


def test_adjusting_multiple_people_preserves_relative_direction():
    inp, slots = four_people({p: 1 for p in ("B", "C", "D")})
    balance_pgy(inp, slots, [], [])
    counts = Counter(p for ss in slots.values() for cells in ss.values() for p in cells[PHOTO])
    assert all(counts[p] > counts["A"] for p in ("B", "C", "D"))


def test_uniform_offsets_have_no_relative_effect():
    inp, slots = example()
    baseline = deepcopy(slots)
    balance_pgy(inp, baseline, [], [])
    inp.pgy_photo_offsets = {p: -1 for p in inp.pgy_roster}
    balance_pgy(inp, slots, [], [])
    assert slots == baseline
