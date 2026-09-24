"""Reproducible, entirely synthetic day-roster benchmark.

Usage: python scripts/benchmark_day_roster.py --samples 5 --output benchmark.json
Use --source-root with another checkout to compare the same fixtures against a
prior revision. No roster storage, HIS, mail or clock service is opened.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
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
        "pgy4_offset": (4, 0, 0, 0, True),
    }
    pgy_n, clerk_n, external_n, family_n, constrained = profiles[name]
    month = "2026-10"
    days = [date(2026, 10, n) for n in range(1, 32)
            if date(2026, 10, n).weekday() < 5
            and not (name == "pgy4_offset" and n == 9)]
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
    prior_sessions = {}
    if name == "pgy4_clerk4_cross":
        prior_sessions = {
            "2026-09-28": {"上午": {"101": ["C1_1"], "102": ["C1_2"]},
                           "下午": {"101": ["C1_3"], "102": ["C1_4"]}},
            "2026-09-29": {"上午": {"101": ["C1_1"], "102": ["C1_2"]},
                           "下午": {"切片室": ["C1_3"]}},
            "2026-09-30": {"上午": {"101": ["C1_3"], "102": ["C1_4"]}},
        }
        for day_number in (28, 29, 30):
            prior_day = date(2026, 9, day_number)
            doctors[prior_day] = {
                session: {room: f"D{(day_number + int(room)) % 5 + 1}"
                          for room in ("101", "102")}
                for session in ("上午", "下午")}
    return DaySolveInput(
        month, grid, [f"P{i + 1}" for i in range(pgy_n)],
        clerk_batches=batches, batch_order=batches, biopsy_open=biopsy_open,
        external_roster=external, family_roster=family,
        family_follow=family_follow, clinic_doctors=doctors,
        leaves=leave, session_leaves=session_leave, locked=locked,
        prior_sessions=prior_sessions,
        holidays=({date(2026, 10, 9)} if name == "pgy4_offset" else set()),
        pgy_photo_offsets=({"P1": -1} if name == "pgy4_offset" else {}),
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
    grid_dates = {d.isoformat() for d in inp.grid}
    for iso, sessions in slots.items():
        if (iso not in grid_dates and
                any(people for cells in sessions.values()
                    for room, people in cells.items() if room != REST)):
            issues.append(f"{iso}:out-of-grid-assignment")
    for d in inp.grid:
        iso = d.isoformat()
        if d.weekday() >= 5 or d in inp.holidays:
            if any(people for cells in slots.get(iso, {}).values()
                   for room, people in cells.items() if room != REST):
                issues.append(f"{iso}:closed-day-assignment")
            continue
        for session in ("上午", "下午"):
            cells = slots.get(iso, {}).get(session, {})
            if len(cells.get(PHOTO, [])) != 1:
                issues.append(f"{iso}/{session}:photo-staffing")
            if any(p not in inp.pgy_roster for p in cells.get(PHOTO, [])):
                issues.append(f"{iso}/{session}:photo-non-pgy")
            photo_only = (d.weekday() == 2 and session == "下午") or (
                len(set(inp.pgy_roster)) == 2
                and (d.weekday(), session) in TWO_PGY_PHOTO_ONLY)
            if not photo_only and len(cells.get(TREATMENT, [])) != 1:
                issues.append(f"{iso}/{session}:treatment-staffing")
            if any(p not in inp.pgy_roster for p in cells.get(TREATMENT, [])):
                issues.append(f"{iso}/{session}:treatment-non-pgy")
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
            if photo_only and cells.get(TREATMENT):
                issues.append(f"{iso}/{session}:unexpected-treatment")
    issues.extend(activity_issues)
    return issues


def _quality(inp, slots, activity_issues=()):
    from cmuh_common.roster.solve_day import (
        BIOPSY, STUDENT_SESSIONS, is_follow_slot, person_course_stats,
    )
    from cmuh_common.roster.training_bands import (
        available_slots, band, training_warnings,
    )

    pgy = person_course_stats(slots, include=set(inp.pgy_roster))
    totals = {p: sum(pgy.get(p, {}).get(k, 0) for k in ("photo", "tx", "follow"))
              for p in inp.pgy_roster}
    participants = set(inp.pgy_roster) | set(inp.external_roster) | set(inp.family_roster)
    participants.update(c for b in inp.clerk_batches for c in b.members)
    all_counts = person_course_stats(slots, include=participants)
    per_person = {p: {k: all_counts.get(p, {}).get(k, 0)
                      for k in ("photo", "photo_wed_pm", "tx", "follow", "biopsy")}
                  for p in sorted(participants)}
    for p in inp.pgy_roster:
        row = per_person[p]
        row["necessary"] = row["photo"] + row["tx"]
        row["total"] = row["necessary"] + row["follow"]
        row["photo_offset"] = inp.pgy_photo_offsets.get(p, 0)
        row["offset_adjusted_total"] = row["total"] - row["photo_offset"]

    # Include the actual adjacent-month course segments. A month-only count can
    # make a cross-month Clerk course appear to miss its quota or biopsy target.
    sources = {iso: sessions for iso, sessions in inp.prior_sessions.items()
               if iso[:7] < inp.ym}
    sources.update({iso: sessions for iso, sessions in inp.course_fixed.items()
                    if iso[:7] > inp.ym})
    sources.update({iso: sessions for iso, sessions in slots.items()
                    if iso[:7] == inp.ym})
    weeks = defaultdict(Counter)
    clerk_course_weeks = defaultdict(Counter)
    daily = defaultdict(Counter)
    biopsies = defaultdict(Counter)
    clerk_biopsy_weeks = defaultdict(Counter)
    doctors = defaultdict(Counter)
    unknown_doctors = Counter()
    for iso, sessions in sources.items():
        d = date.fromisoformat(iso)
        for session, cells in sessions.items():
            if session not in STUDENT_SESSIONS:
                continue
            for room, people in cells.items():
                for person in people:
                    if person not in participants:
                        continue
                    if room == BIOPSY:
                        if iso[:7] == inp.ym:
                            biopsies[person]["first" if d.day <= 14 else "second"] += 1
                        for batch in inp.clerk_batches:
                            if person in batch.members and batch.covers(d):
                                course_week = 1 + (d - batch.start_monday).days // 7
                                clerk_biopsy_weeks[batch.id, person][str(course_week)] += 1
                    if not is_follow_slot(room):
                        continue
                    if iso[:7] != inp.ym and person not in {
                        p for b in inp.clerk_batches if b.covers(d) for p in b.members
                    }:
                        continue
                    week_year, week, _ = d.isocalendar()
                    weeks[person][f"{week_year}-W{week:02d}"] += 1
                    for batch in inp.clerk_batches:
                        if person in batch.members and batch.covers(d):
                            clerk_course_weeks[batch.id, person][
                                f"{week_year}-W{week:02d}"] += 1
                    daily[person][iso] += 1
                    doctor = inp.clinic_doctors.get(d, {}).get(session, {}).get(room)
                    if doctor:
                        doctors[person][doctor] += 1
                    else:
                        unknown_doctors[person] += 1

    clerk_courses = {}
    for batch in inp.clerk_batches:
        end = batch.start_monday + timedelta(days=13)
        # A Clerk course has Monday-Friday duties for two weeks. Its trailing
        # weekend may be in the next month even when every duty is complete.
        last_workday = end - timedelta(days=2)
        stats = person_course_stats(sources, include=set(batch.members),
                                    start=batch.start_monday, end=end)
        clerk_courses[batch.id] = {
            "start": batch.start_monday.isoformat(), "end": end.isoformat(),
            "complete": last_workday.strftime("%Y-%m") <= inp.ym,
            "members": {p: {"follow": stats.get(p, {}).get("follow", 0),
                            "biopsy": stats.get(p, {}).get("biopsy", 0),
                            "biopsy_by_course_week": dict(sorted(
                                clerk_biopsy_weeks[batch.id, p].items())),
                            "weekly_follows": dict(sorted(
                                clerk_course_weeks[batch.id, p].items()))}
                        for p in batch.members},
        }
    training = {}
    for scope, people in (("family", inp.family_roster),
                          ("external", inp.external_roster)):
        for person in people:
            available = available_slots(inp, scope, person)
            low, target, high = band(len(available))
            training[person] = {
                "scope": scope, "available_half_days": len(available),
                "minimum": low, "target": target, "maximum": high,
                "follow": per_person[person]["follow"],
                "follow_ratio": (round(per_person[person]["follow"] / len(available), 4)
                                 if available else None),
                "biopsy_by_half": dict(sorted(biopsies[person].items())),
            }
    diversity = {
        p: {"known_doctors": dict(sorted(doctors[p].items())),
            "distinct_doctors": len(doctors[p]),
            "unknown_doctor_follows": unknown_doctors[p]}
        for p in sorted(participants)
    }
    return {"hard_issues": _hard_checks(inp, slots, activity_issues),
            "pgy_workload": totals,
            "pgy_workload_range": max(totals.values()) - min(totals.values()) if totals else 0,
            "sample_month_person_counts": per_person,
            "weekly_follows": {p: dict(sorted(weeks[p].items()))
                               for p in sorted(participants)},
            "daily_follows": {p: dict(sorted(daily[p].items()))
                              for p in sorted(participants)},
            "clerk_courses": clerk_courses,
            "training": training,
            "doctor_diversity": diversity,
            "training_warnings": training_warnings(inp, slots)}


def run_once(inp):
    from ortools.sat.python import cp_model
    from cmuh_common.roster import course_balance, follow_priority, pgy_balance, solve_day

    phase_times = Counter()
    statuses = []
    stage_stack = []
    original_solve = cp_model.CpSolver.solve
    original_model_init = cp_model.CpModel.__init__
    model_born = {}
    last_solve_end = {}
    model_phase_count = Counter()

    def tracked_model_init(model, *args, **kwargs):
        original_model_init(model, *args, **kwargs)
        model_born[model] = time.perf_counter()

    def tracked_solve(solver, model, *args, **kwargs):
        started = time.perf_counter()
        model_phase_count[model] += 1
        phase_index = model_phase_count[model]
        setup_start = (model_born.get(model) if phase_index == 1
                       else last_solve_end.get(model))
        setup_seconds = (round(started - setup_start, 4)
                         if setup_start is not None else None)
        model_variables = len(model.proto.variables)
        model_constraints = len(model.proto.constraints)
        result = original_solve(solver, model, *args, **kwargs)
        finished = time.perf_counter()
        last_solve_end[model] = finished
        proved_or_feasible = result in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        has_bound = result in (cp_model.OPTIMAL, cp_model.FEASIBLE,
                               cp_model.UNKNOWN)
        raw_bound = solver.best_objective_bound if has_bound else None
        statuses.append({"stage": stage_stack[-1] if stage_stack else "other",
                         "phase_index": phase_index,
                         "status": solver.status_name(result),
                         "seconds": round(finished - started, 4),
                         "setup_seconds": setup_seconds,
                         "model_variables": model_variables,
                         "model_constraints": model_constraints,
                         "objective": (round(solver.objective_value, 4)
                                       if proved_or_feasible else None),
                         "best_bound": (round(raw_bound, 4)
                                        if raw_bound is not None
                                        and math.isfinite(raw_bound) else None)})
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
        stack.enter_context(patch.object(cp_model.CpModel, "__init__", tracked_model_init))
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
            "warnings": list(warnings) if slots is not None else None,
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
                           "pgy4_mix2", "pgy4_offset")
    git_status = subprocess.run(
        ["git", "-C", str(source.parent), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=False)
    relevant_status = subprocess.run(
        ["git", "-C", str(source.parent), "status", "--porcelain",
         "--untracked-files=all", "--", "src/cmuh_common/roster",
         "scripts/benchmark_day_roster.py", "scripts/compare_day_roster.py"],
        capture_output=True, text=True, check=False)
    result = {"environment": {
        "python": sys.version.split()[0], "platform": platform.platform(),
        "processor": platform.processor(), "cpu_count": os.cpu_count(),
        "ortools": ortools.__version__, "source_root": str(source),
        "python_hash_seed": os.environ["PYTHONHASHSEED"],
        "revision": subprocess.run(["git", "-C", str(source.parent), "rev-parse", "HEAD"],
                                   capture_output=True, text=True, check=False).stdout.strip(),
        "source_dirty": bool(git_status.stdout.strip()) if git_status.returncode == 0 else None,
        "relevant_source_dirty": (bool(relevant_status.stdout.strip())
                                  if relevant_status.returncode == 0 else None),
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
                  for stage in ("attendance", "training", "clerk_spread",
                                "pgy_balance", "doctor_diversity")}
        layer_keys = sorted({(status["stage"], status["phase_index"])
                             for sample in samples
                             for status in sample["solver_statuses"]})
        layer_summaries = []
        for stage, phase_index in layer_keys:
            observed = [status for sample in samples
                        for status in sample["solver_statuses"]
                        if status["stage"] == stage
                        and status["phase_index"] == phase_index]
            seconds = [status["seconds"] for status in observed]
            setups = [status["setup_seconds"] for status in observed
                      if status["setup_seconds"] is not None]
            layer_summaries.append({
                "stage": stage, "phase_index": phase_index,
                "observations": len(observed),
                "statuses": dict(Counter(s["status"] for s in observed)),
                "median_seconds": statistics.median(seconds),
                "range_seconds": [min(seconds), max(seconds)],
                "median_setup_seconds": statistics.median(setups) if setups else None,
                "model_variables": [min(s["model_variables"] for s in observed),
                                    max(s["model_variables"] for s in observed)],
                "model_constraints": [min(s["model_constraints"] for s in observed),
                                      max(s["model_constraints"] for s in observed)],
            })
        result["cases"][name] = {
            "input_fingerprint": day_input_fingerprint(inp),
            "warmup_seconds": [s["seconds"] for s in warmups],
            "median_seconds": statistics.median(values),
            "range_seconds": [min(values), max(values)],
            "stages": stages, "solver_layers": layer_summaries,
            "samples": samples,
        }
        print(f"{name}: median {statistics.median(values):.2f}s; "
              f"range {min(values):.2f}–{max(values):.2f}s", flush=True)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
