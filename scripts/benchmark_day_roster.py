"""Reproducible, entirely synthetic day-roster benchmark.

Usage: python scripts/benchmark_day_roster.py --samples 5 --output benchmark.json
Use --source-root with another checkout to compare the same fixtures against a
prior revision. No roster storage, HIS, mail or clock service is opened.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import Counter
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


def _source_root() -> tuple[argparse.Namespace, Path]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path,
                        default=Path(__file__).resolve().parents[1] / "src")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 1 or args.warmups < 0:
        parser.error("samples must be positive and warmups nonnegative")
    source = args.source_root.resolve()
    if not (source / "cmuh_common" / "roster" / "solve_day.py").is_file():
        parser.error(f"not a roster source tree: {source}")
    sys.path.insert(0, str(source))
    return args, source


def make_case(name: str):
    from cmuh_common.roster.model import ClerkBatch
    from cmuh_common.roster.solve_day import DaySolveInput, PHOTO, TREATMENT

    profiles = {
        "pgy2": (2, 0, 0, 0, False),
        "pgy4_clerk4_cross": (4, 4, 0, 0, True),
        "pgy4_clerk5_mix1": (4, 5, 1, 1, True),
        "pgy4_mix2": (4, 0, 2, 2, True),
    }
    pgy_n, clerk_n, external_n, family_n, constrained = profiles[name]
    month = "2026-10"
    days = [date(2026, 10, n) for n in range(1, 32)
            if date(2026, 10, n).weekday() < 5]
    grid = {}
    doctors = {}
    for d in days:
        grid[d] = {}
        doctors[d] = {}
        for s in ("上午", "下午"):
            rooms = [] if d.weekday() == 2 and s == "下午" else ["101", "102"]
            if constrained and d == date(2026, 10, 15) and s == "下午":
                rooms = []  # whole-session clinic closure; work time remains
            grid[d][s] = rooms
            doctors[d][s] = {r: f"D{(d.day + int(r)) % 5 + 1}" for r in rooms}
    batches = []
    if clerk_n:
        starts = ((date(2026, 9, 28), date(2026, 10, 12))
                  if "cross" in name
                  else (date(2026, 10, 5), date(2026, 10, 19)))
        batches = [ClerkBatch(f"B{i + 1}", start,
                               [f"C{i + 1}_{j + 1}" for j in range(clerk_n)])
                   for i, start in enumerate(starts)]
    external = [f"E{i + 1}" for i in range(external_n)]
    family = [f"F{i + 1}" for i in range(family_n)]
    family_follow = {p: {(d, s) for d in days for s in ("上午", "下午")
                         if (d.weekday() == 2 and s == "上午")
                         or (family_n == 1 and
                             (d.weekday() in (0, 4) or s == "下午"))}
                     for p in family}
    biopsy_open = {b.id: {
        (b.start_monday + timedelta(days=i)).isoformat(): {"下午": True}
        for i in range(14)
        if (b.start_monday + timedelta(days=i)).weekday() in (1, 3)}
        for b in batches}
    course_days = {d.isoformat() for d in days}
    for b in batches:
        course_days |= {(b.start_monday + timedelta(days=i)).isoformat()
                        for i in range(14)
                        if (b.start_monday + timedelta(days=i)).weekday() < 5}
    leave = {"external": {p: {date(2026, 10, 8 + i)}
                          for i, p in enumerate(external)}} if constrained else {}
    session_leave = {"family": {p: {(date(2026, 10, 20), "上午")}
                                for p in family}} if constrained else {}
    locked = ({"2026-10-06": {"上午": {
        PHOTO: ["P1"], TREATMENT: ["P2"], "101": [batches[0].members[0]]}}}
              if constrained and batches else {})
    return DaySolveInput(
        month, grid, [f"P{i + 1}" for i in range(pgy_n)],
        clerk_batches=batches, batch_order=batches, biopsy_open=biopsy_open,
        external_roster=external, family_roster=family,
        family_follow=family_follow, clinic_doctors=doctors,
        leaves=leave, session_leaves=session_leave, locked=locked,
        capacity=2, course_days=course_days,
        course_clinic_days={d.isoformat() for d in days
                            if any(grid[d].values())})


def _active_pgy_days(inp, slots):
    from cmuh_common.roster.solve_day import REST

    return {
        (iso, person)
        for iso, sessions in slots.items()
        for person in inp.pgy_roster
        if any(person in people for cells in sessions.values()
               for room, people in cells.items() if room != REST)
    }


def _activity_loss_issues(stage, active_before, active_after):
    return [
        f"{iso}/{person}:pgy-full-day-rest-created-by-{stage}"
        for iso, person in sorted(set(active_before) - set(active_after))
    ]


def _hard_checks(inp, slots, activity_issues=()):
    from cmuh_common.roster.solve_day import (
        PHOTO, TREATMENT, REST, TWO_PGY_PHOTO_ONLY, is_follow_slot)
    from cmuh_common.roster.session_leave import on_leave

    issues = []
    for d in inp.grid:
        iso = d.isoformat()
        if d.weekday() >= 5 or d in inp.holidays:
            continue
        for session in ("上午", "下午"):
            cells = slots.get(iso, {}).get(session, {})
            if not cells.get(PHOTO):
                issues.append(f"{iso}/{session}:photo-missing")
            photo_only = (d.weekday() == 2 and session == "下午") or (
                len(set(inp.pgy_roster)) == 2
                and (d.weekday(), session) in TWO_PGY_PHOTO_ONLY)
            if not photo_only and not cells.get(TREATMENT):
                issues.append(f"{iso}/{session}:treatment-missing")
            assigned = [p for people in cells.values() for p in people]
            if len(assigned) != len(set(assigned)):
                issues.append(f"{iso}/{session}:double-booked")
            for room, people in cells.items():
                if (is_follow_slot(room) and people
                        and room not in inp.grid[d].get(session, [])):
                    issues.append(f"{iso}/{session}/{room}:closed-room")
                if is_follow_slot(room) and len(people) > inp.capacity:
                    issues.append(f"{iso}/{session}/{room}:capacity")
                for p in people:
                    scope = next((scope for scope in ("pgy", "clerk", "external", "family")
                                  if p in (inp.pgy_roster if scope == "pgy" else
                                           inp.external_roster if scope == "external" else
                                           inp.family_roster if scope == "family" else
                                           [c for b in inp.clerk_batches for c in b.members])), None)
                    if (room != REST and scope == "family"
                            and (d, session) not in inp.family_follow.get(p, set())):
                        issues.append(f"{iso}/{session}/{p}:family-unavailable")
                    if room != REST and scope and on_leave(inp, scope, p, d, session):
                        issues.append(f"{iso}/{session}/{p}:on-leave")
            if session in inp.locked.get(iso, {}) and cells != inp.locked[iso][session]:
                issues.append(f"{iso}/{session}:lock-changed")
            if d.weekday() == 2 and session == "下午" and cells.get(TREATMENT):
                issues.append(f"{iso}/{session}:unexpected-treatment")
    issues.extend(activity_issues)
    return issues


def _quality(inp, slots, activity_issues=()):
    from cmuh_common.roster.solve_day import person_course_stats
    from cmuh_common.roster.training_bands import training_warnings

    pgy = person_course_stats(slots, include=set(inp.pgy_roster))
    totals = {p: sum(pgy.get(p, {}).get(k, 0) for k in ("photo", "tx", "follow"))
              for p in inp.pgy_roster}
    participants = set(inp.pgy_roster) | set(inp.external_roster) | set(inp.family_roster)
    participants.update(c for b in inp.clerk_batches for c in b.members)
    all_counts = person_course_stats(slots, include=participants)
    per_person = {p: {k: all_counts.get(p, {}).get(k, 0)
                      for k in ("photo", "tx", "follow", "biopsy")}
                  for p in sorted(participants)}
    return {"hard_issues": _hard_checks(inp, slots, activity_issues),
            "pgy_workload": totals,
            "pgy_workload_range": max(totals.values()) - min(totals.values()) if totals else 0,
             "sample_month_person_counts": per_person,
            "training_warnings": training_warnings(inp, slots)}


def run_once(inp):
    from ortools.sat.python import cp_model
    from cmuh_common.roster import course_balance, follow_priority, pgy_balance, solve_day

    phase_times = Counter()
    statuses = []
    stage_stack = []
    original_solve = cp_model.CpSolver.solve

    def tracked_solve(solver, model, *args, **kwargs):
        started = time.perf_counter()
        result = original_solve(solver, model, *args, **kwargs)
        proved_or_feasible = result in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        statuses.append({"stage": stage_stack[-1] if stage_stack else "other",
                         "status": solver.status_name(result),
                         "seconds": round(time.perf_counter() - started, 4),
                         "objective": (round(solver.objective_value, 4)
                                       if proved_or_feasible else None),
                         "best_bound": (round(solver.best_objective_bound, 4)
                                        if proved_or_feasible else None)})
        return result

    activity_issues = []

    def timed(module, name, label, stack):
        original = getattr(module, name)

        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            stage_stack.append(label)
            active_before = (
                _active_pgy_days(args[0], args[1])
                if label in {"clerk_spread", "pgy_balance"} and len(args) >= 2
                else set()
            )
            try:
                return original(*args, **kwargs)
            finally:
                if active_before:
                    active_after = _active_pgy_days(args[0], args[1])
                    activity_issues.extend(_activity_loss_issues(
                        label, active_before, active_after))
                stage_stack.pop()
                phase_times[label] += time.perf_counter() - started

        stack.enter_context(patch.object(module, name, wrapped))

    started = time.perf_counter()
    slots = None
    warnings = []
    error = None
    with ExitStack() as stack:
        stack.enter_context(patch.object(cp_model.CpSolver, "solve", tracked_solve))
        timed(solve_day, "_month_solve_attendance", "attendance", stack)
        timed(course_balance, "add_external", "training", stack)
        timed(follow_priority, "spread_clerk_days", "clerk_spread", stack)
        timed(pgy_balance, "balance_pgy", "pgy_balance", stack)
        timed(course_balance, "balance_rooms", "doctor_diversity", stack)
        try:
            slots, _log, warnings = solve_day.month_solve_day(inp)
        except Exception as exc:  # representative stress cases may not prove optimality
            error = {"type": type(exc).__name__, "message": str(exc)}
    elapsed = time.perf_counter() - started
    return {"seconds": round(elapsed, 4),
            "stages": {k: round(v, 4) for k, v in phase_times.items()},
            "solver_statuses": statuses,
            "quality": (_quality(inp, slots, activity_issues)
                        if slots is not None else None),
            "warning_count": len(warnings) if slots is not None else None,
            "error": error}


def main():
    # Dict/set iteration influences CP-SAT model construction. The seed must be
    # fixed before this interpreter starts; restart only this benchmark process.
    if os.environ.get("PYTHONHASHSEED") != "0":
        env = dict(os.environ, PYTHONHASHSEED="0")
        raise SystemExit(subprocess.run([sys.executable, *sys.argv], env=env,
                                        check=False).returncode)
    args, source = _source_root()
    import ortools
    from cmuh_common.roster.solve_day import day_input_fingerprint

    names = args.cases or ("pgy2", "pgy4_clerk4_cross", "pgy4_clerk5_mix1",
                           "pgy4_mix2")
    git_status = subprocess.run(
        ["git", "-C", str(source.parent), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=False)
    result = {"environment": {
        "python": sys.version.split()[0], "platform": platform.platform(),
        "processor": platform.processor(), "cpu_count": os.cpu_count(),
        "ortools": ortools.__version__, "source_root": str(source),
        "python_hash_seed": os.environ["PYTHONHASHSEED"],
        "revision": subprocess.run(["git", "-C", str(source.parent), "rev-parse", "HEAD"],
                                   capture_output=True, text=True, check=False).stdout.strip(),
        "source_dirty": bool(git_status.stdout.strip()) if git_status.returncode == 0 else None,
        "samples": args.samples, "warmups": args.warmups,
    }, "cases": {}}
    for name in names:
        inp = make_case(name)
        warmups = [run_once(inp) for _ in range(args.warmups)]
        samples = [run_once(inp) for _ in range(args.samples)]
        values = [sample["seconds"] for sample in samples]
        stages = {stage: {"median": statistics.median(
                    [sample["stages"].get(stage, 0) for sample in samples]),
                          "range": [min(sample["stages"].get(stage, 0) for sample in samples),
                                    max(sample["stages"].get(stage, 0) for sample in samples)]}
                  for stage in ("attendance", "training", "pgy_balance", "doctor_diversity")}
        result["cases"][name] = {
            "input_fingerprint": day_input_fingerprint(inp),
            "warmup_seconds": [s["seconds"] for s in warmups],
            "median_seconds": statistics.median(values),
            "range_seconds": [min(values), max(values)],
            "stages": stages, "samples": samples,
        }
        print(f"{name}: median {statistics.median(values):.2f}s; "
              f"range {min(values):.2f}–{max(values):.2f}s", flush=True)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
