"""Pair synthetic roster benchmarks and reject quality regressions.

Example:
  python scripts/compare_day_roster.py --baseline-root OLD_CHECKOUT \
      --candidate-root NEW_CHECKOUT --samples 10 --output OUTSIDE_REPO.json

Two source trees run in separate Python processes. Each measured pair reverses
execution order on alternating rounds to reduce warm-machine order bias.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path


CASES = ("pgy2", "pgy4_clerk4_cross", "pgy4_clerk5_mix1", "pgy4_mix2",
         "pgy4_offset")
STRESS_CASES = ("pgy4_clerk4_cross", "pgy4_clerk5_mix1")
STATUS_RANK = {"UNKNOWN": 0, "FEASIBLE": 1, "OPTIMAL": 2}


class PairedRunError(RuntimeError):
    def __init__(self, message: str, partial: dict):
        super().__init__(message)
        self.partial = partial


def _differences(before, after, path="quality"):
    """Return exact field-level changes; source and UI use the same slots."""
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            where = f"{path}.{key}"
            if key not in before or key not in after:
                yield where
            else:
                yield from _differences(before[key], after[key], where)
    elif before != after:
        yield path


def _protected_outcome_regressions(before: dict, after: dict):
    """Protect hard minima and person-level coverage across changed schedules."""
    old_clerk_gap = new_clerk_gap = 0
    for course, old in before["clerk_courses"].items():
        new = after["clerk_courses"].get(course)
        if new is None:
            yield f"clerk_courses.{course}: missing"
            continue
        for person, old_row in old["members"].items():
            new_row = new["members"].get(person)
            if new_row is None:
                yield f"clerk_courses.{course}.{person}: missing"
                continue
            if old.get("complete", True):
                for field, minimum in (("follow", 9), ("biopsy", 1)):
                    if max(0, minimum - new_row[field]) > max(
                            0, minimum - old_row[field]):
                        yield f"clerk_courses.{course}.{person}.{field}: minimum worsened"
            if new_row["follow"] > 11 or new_row["biopsy"] > 2:
                yield f"clerk_courses.{course}.{person}: course cap exceeded"
            old_clerk_gap += abs(old_row["follow"] - 10)
            new_clerk_gap += abs(new_row["follow"] - 10)
    if new_clerk_gap > old_clerk_gap:
        yield "clerk_courses: combined target gap worsened"
    old_training_gap = new_training_gap = 0
    for person, old in before["training"].items():
        new = after["training"].get(person)
        if new is None:
            yield f"training.{person}: missing"
            continue
        if max(0, new["minimum"] - new["follow"]) > max(
                0, old["minimum"] - old["follow"]):
            yield f"training.{person}: minimum worsened"
        if new["follow"] > new["maximum"]:
            yield f"training.{person}: maximum exceeded"
        old_training_gap += abs(old["follow"] - old["target"])
        new_training_gap += abs(new["follow"] - new["target"])
        for half, count in old["biopsy_by_half"].items():
            if count >= 1 and new["biopsy_by_half"].get(half, 0) < 1:
                yield f"training.{person}.{half}: biopsy target lost"
    if new_training_gap > old_training_gap:
        yield "training: combined target gap worsened"
    old_pgy_target_gap = new_pgy_target_gap = 0
    for person, old in before["weekly_follows"].items():
        if person not in before["pgy_workload"]:
            continue
        new = after["weekly_follows"].get(person, {})
        for week in old.keys() | new.keys():
            count = old.get(week, 0)
            if count >= 1 and new.get(week, 0) < 1:
                yield f"pgy.{person}.{week}: weekly minimum lost"
            old_pgy_target_gap += max(0, 2 - count)
            new_pgy_target_gap += max(0, 2 - new.get(week, 0))
    if new_pgy_target_gap > old_pgy_target_gap:
        yield "pgy: combined weekly target gap worsened"
    for field in ("necessary", "offset_adjusted_total"):
        def spread(quality, metric=field):
            values = [quality["sample_month_person_counts"][p][metric]
                      for p in quality["pgy_workload"]]
            return max(values) - min(values) if values else 0
        if spread(after) > spread(before):
            yield f"pgy.{field}: fairness spread worsened"
    for person, old in before["doctor_diversity"].items():
        new = after["doctor_diversity"].get(person)
        if new is None or new["distinct_doctors"] < old["distinct_doctors"]:
            yield f"doctor_diversity.{person}: distinct doctors lost"


def _safe_manual_reduction(before: dict, after: dict,
                           old_warnings: list, new_warnings: list) -> bool:
    """Recognize an actual manual PGY reduction without hiding other changes."""
    old_rows = before["sample_month_person_counts"]
    new_rows = after["sample_month_person_counts"]
    if old_rows.keys() != new_rows.keys():
        return False
    reduced = {}
    for person, old in old_rows.items():
        new = new_rows[person]
        if new == old:
            continue
        offset = old.get("photo_offset", 0)
        drop = old["total"] - new["total"]
        if (offset >= 0 or not 1 <= drop <= -offset
                or any(old[field] != new[field] for field in
                       ("photo", "photo_wed_pm", "tx", "biopsy",
                        "necessary", "photo_offset"))
                or any(old[field] - new[field] != drop for field in
                       ("follow", "total", "offset_adjusted_total"))):
            return False
        old_weeks = before["weekly_follows"].get(person, {})
        new_weeks = after["weekly_follows"].get(person, {})
        if (sum(old_weeks.values()) - sum(new_weeks.values()) != drop
                or any(new_weeks.get(week, 0) > count or
                       new_weeks.get(week, 0) < min(2, count)
                       for week, count in old_weeks.items())
                or set(new_weeks) - set(old_weeks)):
            return False
        old_daily = before["daily_follows"].get(person, {})
        new_daily = after["daily_follows"].get(person, {})
        if (sum(old_daily.values()) - sum(new_daily.values()) != drop
                or any(new_daily.get(day, 0) > count
                       for day, count in old_daily.items())
                or set(new_daily) - set(old_daily)):
            return False
        old_doctors = before["doctor_diversity"][person]
        new_doctors = after["doctor_diversity"][person]
        if (new_doctors["distinct_doctors"] < old_doctors["distinct_doctors"]
                or new_doctors["unknown_doctor_follows"] >
                old_doctors["unknown_doctor_follows"]
                or max(new_doctors["known_doctors"].values(), default=0) >
                max(old_doctors["known_doctors"].values(), default=0)
                or sum(new_doctors["known_doctors"].values()) +
                new_doctors["unknown_doctor_follows"] !=
                sum(old_doctors["known_doctors"].values()) +
                old_doctors["unknown_doctor_follows"] - drop):
            return False
        if (before["pgy_workload"].get(person) != old["total"]
                or after["pgy_workload"].get(person) != new["total"]):
            return False
        reduced[person] = drop
    if not reduced:
        return False
    removed = set(old_warnings) - set(new_warnings)
    if (set(new_warnings) - set(old_warnings)
            or not removed
            or any(not any(f"PGY {person} 總工作量減量目標未達" in warning
                           for person in reduced) for warning in removed)):
        return False
    for quality in (before, after):
        workloads = quality["pgy_workload"].values()
        if quality["pgy_workload_range"] != max(workloads) - min(workloads):
            return False
    old_rest, new_rest = deepcopy(before), deepcopy(after)
    for field in ("sample_month_person_counts", "weekly_follows",
                  "daily_follows", "doctor_diversity", "pgy_workload"):
        for person in reduced:
            old_rest[field].pop(person, None)
            new_rest[field].pop(person, None)
    old_rest.pop("pgy_workload_range")
    new_rest.pop("pgy_workload_range")
    return old_rest == new_rest and not list(
        _protected_outcome_regressions(before, after))


def compare_reports(baseline: dict, candidate: dict, *,
                    min_samples: int = 10, target_fraction: float = 0.15) -> dict:
    issues = []
    evidence_issues = []
    summaries = {}
    layer_comparisons = []
    schedule_changes = []
    proof_changes = []
    base_env, cand_env = baseline.get("environment", {}), candidate.get("environment", {})
    for field in ("python", "platform", "cpu_count", "ortools", "python_hash_seed"):
        if base_env.get(field) != cand_env.get(field):
            evidence_issues.append(f"environment.{field}: different")
    if base_env.get("relevant_source_dirty") is not False or cand_env.get(
            "relevant_source_dirty") is not False:
        evidence_issues.append("roster or benchmark source is dirty or unverifiable")
    base_cases, cand_cases = baseline.get("cases", {}), candidate.get("cases", {})
    if set(base_cases) != set(cand_cases):
        evidence_issues.append("case sets differ")
    for name in sorted(base_cases.keys() | cand_cases.keys()):
        if name not in base_cases or name not in cand_cases:
            continue
        base, cand = base_cases[name], cand_cases[name]
        if base.get("input_fingerprint") != cand.get("input_fingerprint"):
            issues.append(f"{name}: different input fingerprints")
        bs, cs = base.get("samples", []), cand.get("samples", [])
        required_samples = max(10, min_samples)
        if len(bs) != len(cs) or min(len(bs), len(cs)) < required_samples:
            evidence_issues.append(
                f"{name}: requires {required_samples} paired samples on each side")
        for index, (left, right) in enumerate(zip(bs, cs, strict=False), 1):
            prefix = f"{name}/pair{index}"
            if left.get("error") or right.get("error"):
                issues.append(f"{prefix}: solver error: {left.get('error')} / {right.get('error')}")
                continue
            for side, sample in (("baseline", left), ("candidate", right)):
                quality = sample.get("quality")
                if not isinstance(quality, dict):
                    issues.append(f"{prefix}/{side}: hard constraint failure")
                elif not isinstance(quality.get("hard_issues"), list):
                    issues.append(f"{prefix}/{side}: missing hard constraint evidence")
                elif quality["hard_issues"]:
                    issues.append(f"{prefix}/{side}: hard constraint failure")
                elif not all(field in quality for field in (
                    "sample_month_person_counts", "weekly_follows", "daily_follows",
                    "clerk_courses", "pgy_workload",
                    "training", "doctor_diversity", "training_warnings",
                )):
                    issues.append(f"{prefix}/{side}: incomplete quality schema")
                if not isinstance(sample.get("warnings"), list):
                    issues.append(f"{prefix}/{side}: missing warning contents")
            old_sequence = left.get("solver_statuses", [])
            new_sequence = right.get("solver_statuses", [])
            required_layer_fields = {"stage", "phase_index", "status",
                                     "objective", "best_bound"}
            if any(not isinstance(seq, list) or not seq or any(
                    not isinstance(layer, dict)
                    or not required_layer_fields <= layer.keys()
                    for layer in seq) for seq in (old_sequence, new_sequence)):
                issues.append(f"{prefix}: missing or malformed solver layer evidence")
                continue
            old_layers = {(s["stage"], s["phase_index"]): s
                          for s in old_sequence}
            new_layers = {(s["stage"], s["phase_index"]): s
                          for s in new_sequence}
            if len(old_layers) != len(old_sequence) or len(new_layers) != len(new_sequence):
                issues.append(f"{prefix}: duplicate solver layer key")
                continue
            if old_layers.keys() - new_layers.keys():
                issues.append(f"{prefix}: candidate skipped a baseline solver layer")
            for key in sorted(new_layers.keys() - old_layers.keys()):
                extra = new_layers[key]
                layer_comparisons.append({
                    "case": name, "pair": index, "stage": key[0],
                    "phase_index": key[1],
                    "baseline_status": None, "candidate_status": extra.get("status"),
                    "baseline_objective": None,
                    "candidate_objective": extra.get("objective"),
                    "baseline_bound": None,
                    "candidate_bound": extra.get("best_bound"),
                })
                if (extra.get("status") not in ("OPTIMAL", "FEASIBLE")
                        or extra.get("objective") is None):
                    issues.append(f"{prefix}/{key}: added layer has no feasible objective")
            priority_improved = False
            training_prefix_equal = True
            for old_layer in old_sequence:
                key = old_layer["stage"], old_layer["phase_index"]
                if key not in new_layers:
                    continue
                old, new = old_layers[key], new_layers[key]
                old_status, new_status = old["status"], new["status"]
                layer_comparisons.append({
                    "case": name, "pair": index, "stage": key[0],
                    "phase_index": key[1],
                    "baseline_status": old_status, "candidate_status": new_status,
                    "baseline_objective": old.get("objective"),
                    "candidate_objective": new.get("objective"),
                    "baseline_bound": old.get("best_bound"),
                    "candidate_bound": new.get("best_bound"),
                })
                if (old_status not in STATUS_RANK or new_status not in STATUS_RANK
                        or STATUS_RANK[new_status] < STATUS_RANK[old_status]):
                    issues.append(f"{prefix}/{key}: status {old_status} -> {new_status}")
                old_obj, new_obj = old.get("objective"), new.get("objective")
                if old_obj is not None and new_obj is None:
                    issues.append(f"{prefix}/{key}: candidate objective missing")
                if old_obj is not None and new_obj is not None and new_obj > old_obj + 1e-6:
                    issues.append(f"{prefix}/{key}: objective {old_obj} -> {new_obj}")
                # Only the first changed training objective can justify a
                # different schedule. Later balance stages use different input
                # slots and may even skip tiers, so their solve indices are not
                # comparable evidence of a higher-priority gain.
                if key[0] == "training" and training_prefix_equal:
                    improved = (old_obj is not None and new_obj is not None
                                and new_obj < old_obj - 1e-6)
                    improved |= (old_status == "UNKNOWN"
                                 and new_status in ("FEASIBLE", "OPTIMAL")
                                 and new_obj is not None)
                    priority_improved = improved
                    training_prefix_equal = (
                        old_status == new_status and old_obj == new_obj)
                old_bound, new_bound = old.get("best_bound"), new.get("best_bound")
                if old_bound != new_bound:
                    proof_changes.append({"case": name, "pair": index,
                                          "stage": key[0], "phase_index": key[1],
                                          "baseline_bound": old_bound,
                                          "candidate_bound": new_bound})
            if (left.get("quality") and right.get("quality")
                    and left.get("warnings") is not None
                    and right.get("warnings") is not None):
                changed = list(_differences(left["quality"], right["quality"]))
                manual_reduction = (not priority_improved and
                                    _safe_manual_reduction(
                                        left["quality"], right["quality"],
                                        left["warnings"], right["warnings"]))
                schedule_changes.append({"case": name, "pair": index,
                                         "priority_improved": priority_improved,
                                         "manual_reduction_improved": manual_reduction,
                                         "quality_fields_changed": changed,
                                         "warnings_removed": sorted(set(left["warnings"])
                                                                    - set(right["warnings"])),
                                         "warnings_added": sorted(set(right["warnings"])
                                                                  - set(left["warnings"]))})
                if priority_improved:
                    issues.extend(f"{prefix}/{path}" for path in
                                  _protected_outcome_regressions(
                                      left["quality"], right["quality"]))
                elif not manual_reduction:
                    issues.extend(f"{prefix}/{path}: changed without higher-priority gain"
                                  for path in changed)
                    if left.get("warnings") != right.get("warnings"):
                        issues.append(f"{prefix}: warning contents changed")
        before = [s["seconds"] for s in bs if s.get("error") is None]
        after = [s["seconds"] for s in cs if s.get("error") is None]
        if before and after:
            old_median, new_median = statistics.median(before), statistics.median(after)
            improvement = (old_median - new_median) / old_median if old_median else 0.0
            summaries[name] = {
                "baseline_median": old_median, "candidate_median": new_median,
                "baseline_range": [min(before), max(before)],
                "candidate_range": [min(after), max(after)],
                "improvement_fraction": round(improvement, 4),
                "paired_wins": sum(new["seconds"] < old["seconds"]
                                   for old, new in zip(bs, cs, strict=False)),
                "baseline_samples": len(bs), "candidate_samples": len(cs),
            }
    quality_passed = not issues
    complete = all(name in summaries for name in CASES)
    stress_no_regression = all(
        summaries.get(name, {}).get("improvement_fraction", -1) >= -0.03
        for name in STRESS_CASES)
    stress_repeatable = any(
        summaries.get(name, {}).get("improvement_fraction", -1) >= 0.08
        and summaries[name]["baseline_samples"] >= 10
        and summaries[name]["paired_wins"] >=
        math.ceil(0.8 * summaries[name]["baseline_samples"])
        for name in STRESS_CASES if name in summaries)
    stress_met = (stress_no_regression and any(
        summaries.get(name, {}).get("improvement_fraction", -1) >= target_fraction
        for name in STRESS_CASES))
    small_safe = all(after["candidate_median"] <=
                     max(after["baseline_median"] * 1.10,
                         after["baseline_median"] + 0.1)
                     for name, after in summaries.items() if name not in STRESS_CASES)
    return {"quality_gate_passed": quality_passed,
            "measurement_gate_passed": complete and not evidence_issues,
            "repeatable_improvement": stress_no_regression and stress_repeatable and small_safe,
            "performance_target_met": stress_met and small_safe,
            "adoptable": (quality_passed and complete and not evidence_issues
                          and stress_no_regression and stress_repeatable and small_safe),
            "target_fraction": target_fraction,
            "cases": summaries, "layers": layer_comparisons,
            "schedule_changes": schedule_changes, "proof_changes": proof_changes,
            "issues": issues + evidence_issues}


def _run_once(script: Path, root: Path, name: str, warmup: bool, out: Path) -> dict:
    try:
        subprocess.run(
            [sys.executable, str(script), "--source-root", str(root / "src"),
             "--case", name, "--samples", "1", "--warmups", str(int(warmup)),
             "--output", str(out)],
            check=True, capture_output=True, text=True, timeout=360,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        def decoded(value):
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value or ""

        details = [str(exc)]
        for stream in ("stdout", "stderr"):
            content = decoded(getattr(exc, stream, None)).strip()
            if content:
                details.append(f"{stream}: {content[-4000:]}")
        raise RuntimeError("; ".join(details)) from exc
    return json.loads(out.read_text(encoding="utf-8"))


def run_paired(baseline_root: Path, candidate_root: Path, *,
               samples: int, cases: tuple[str, ...] = CASES) -> tuple[dict, dict]:
    script = Path(__file__).with_name("benchmark_day_roster.py")
    reports = {"baseline": {"environment": {}, "cases": {}},
               "candidate": {"environment": {}, "cases": {}}}
    roots = {"baseline": baseline_root.resolve(),
             "candidate": candidate_root.resolve()}
    with tempfile.TemporaryDirectory(prefix="roster-bench-") as tmp:
        out = Path(tmp) / "sample.json"
        for name in cases:
            for round_index in range(samples):
                order = (("baseline", "candidate") if round_index % 2 == 0
                         else ("candidate", "baseline"))
                for side in order:
                    try:
                        report = _run_once(script, roots[side], name,
                                           round_index == 0, out)
                        result = reports[side]
                        observed_env = {key: value for key, value in
                                        report["environment"].items() if key != "warmups"}
                        if result["environment"]:
                            prior_env = {key: value for key, value in
                                         result["environment"].items() if key != "warmups"}
                            if observed_env != prior_env:
                                raise ValueError(
                                    f"{name}/{side}: environment or revision changed during measurement")
                        else:
                            result["environment"] = report["environment"]
                        entry = result["cases"].setdefault(name, {
                            "input_fingerprint": report["cases"][name]["input_fingerprint"],
                            "samples": [], "warmup_seconds": [],
                        })
                        sample = report["cases"][name]
                        if entry["input_fingerprint"] != sample["input_fingerprint"]:
                            raise ValueError(f"{name}/{side}: input changed during measurement")
                        entry["samples"].extend(sample["samples"])
                        entry["warmup_seconds"].extend(sample["warmup_seconds"])
                    except Exception as exc:
                        raise PairedRunError(
                            f"{name}/{side}/pair {round_index + 1}: {exc}",
                            reports) from exc
                print(f"{name}: paired {round_index + 1}/{samples}", flush=True)
    return reports["baseline"], reports["candidate"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--baseline-json", type=Path)
    parser.add_argument("--candidate-json", type=Path)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--target-fraction", type=float, default=0.15)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    live = args.baseline_root and args.candidate_root
    offline = args.baseline_json and args.candidate_json
    if bool(live) == bool(offline) or args.samples < 1:
        parser.error("provide either both source roots or both report JSON files")
    if live:
        try:
            baseline, candidate = run_paired(
                args.baseline_root, args.candidate_root,
                samples=args.samples, cases=tuple(args.case or CASES))
        except PairedRunError as exc:
            result = {"status": "incomplete", "error": str(exc),
                      **exc.partial}
            encoded = json.dumps(result, ensure_ascii=False, indent=2,
                                 allow_nan=False)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(encoded + "\n", encoding="utf-8")
            print(str(exc), file=sys.stderr, flush=True)
            return 1
    else:
        baseline = json.loads(args.baseline_json.read_text(encoding="utf-8"))
        candidate = json.loads(args.candidate_json.read_text(encoding="utf-8"))
    comparison = compare_reports(baseline, candidate, min_samples=args.samples,
                                 target_fraction=args.target_fraction)
    result = {"comparison": comparison, "baseline": baseline,
              "candidate": candidate}
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    print(f"quality={comparison['quality_gate_passed']} "
          f"performance={comparison['performance_target_met']} "
          f"adoptable={comparison['adoptable']} "
          f"issues={len(comparison['issues'])}", flush=True)
    return 0 if comparison["adoptable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
