"""Run unchanged pytest arguments once, preserving its exit code and fresh evidence.

Only for isolated development checkouts. Removes stale junit.xml and cov.json.
The evidence checker is an additional delivery requirement, not a pytest verdict.

Every selected test must finish exactly once: counts, unique opaque identities and a
digest of the selected identities all have to agree. Under pytest-xdist each process
numbers its own stream contiguously; the controller stream is the completion record,
and every worker stream must be complete and account for a disjoint share of it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time


REPORTS = ("junit.xml", "cov.json")
TEST_TOOLS = ("pytest", "pytest-cov", "coverage", "pytest-xdist", "execnet")
CASE = re.compile(r"[0-9a-f]{16}")
WORKER = re.compile(r"gw[0-9]{1,4}")
WORKER_STREAM = re.compile(r"events\.(.+)\.jsonl")


def _git(*args) -> str | None:
    try:
        result = subprocess.run(["git", *args], capture_output=True, check=False,
                                encoding="utf-8", errors="replace", timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def metadata() -> dict:
    sha = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--untracked-files=normal")
    return {"schema": 1, "sha": sha if sha and re.fullmatch(r"[0-9a-f]{40}", sha) else None,
            "dirty": bool(status) if status is not None else None,
            "run_id": _digits(os.environ.get("GITHUB_RUN_ID", "")),
            "attempt": _digits(os.environ.get("GITHUB_RUN_ATTEMPT", "")),
            "python": platform.python_version(), "system": platform.system(),
            "platform_version": platform.version(), "test_tools": _tool_versions()}


def _tool_versions() -> dict:
    """Versions only (no paths): which pytest/xdist/coverage produced the evidence."""
    versions = {}
    for name in TEST_TOOLS:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _digits(value: str) -> str | None:
    return value if re.fullmatch(r"[0-9]{1,30}", value) else None


def _write(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def read_events(path: Path) -> tuple[list[dict], bool]:
    events = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    return events, False
                if not isinstance(event, dict) or event.get("seq") != len(events) + 1:
                    return events, False
                events.append(event)
    except OSError:
        return events, False
    return events, True


def run(output: Path, args: list[str]) -> int:
    # Never remove/reuse evidence from another invocation.
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    info = metadata()
    _write(output / "metadata.json", info)
    _write(output / "result.json", {"status": "running", "exit_code": None})
    for name in REPORTS:
        Path(name).unlink(missing_ok=True)
    env = os.environ.copy()
    scripts = str(Path(__file__).resolve().parent)
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = scripts + (os.pathsep + existing_path if existing_path else "")
    env["CMUH_CI_PROGRESS_FILE"] = str((output / "events.jsonl").resolve())
    cp = subprocess.run([sys.executable, "-m", "pytest",
                         "-p", "ci_pytest_progress", *args], env=env, check=False)
    try:
        finish(output, cp.returncode, started)
    except (OSError, ValueError, TypeError):
        print("pytest evidence: finalization failed; original exit code retained", flush=True)
    return cp.returncode


def finish(output: Path, exit_code: int, started: float):
    events, parsed = read_events(output / "events.jsonl")
    last = events[-1] if events else {}
    complete = (parsed and last.get("event") == "session_finished" and
                last.get("exit_code") == exit_code and
                not last.get("diagnostic_errors"))
    reports = {}
    errors = []
    for name in REPORTS:
        source = Path(name)
        if source.is_file():
            try:
                shutil.copyfile(source, output / name)
                raw = (output / name).read_bytes()
                reports[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            except OSError:
                errors.append("report_copy")
    _write(output / "result.json", {
        "status": "finished" if complete else "incomplete", "exit_code": exit_code,
        "elapsed_s": time.perf_counter() - started, "last_event": last,
        "reports": reports, "diagnostic_errors": errors,
    })


def check(output: Path, sha: str, run_id=None, attempt=None) -> int:
    try:
        info = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
        result = json.loads((output / "result.json").read_text(encoding="utf-8"))
        events, parsed = read_events(output / "events.jsonl")
        last = events[-1] if events else {}
        if (not re.fullmatch(r"[0-9a-f]{40}", sha) or info.get("sha") != sha or
                (run_id is not None and info.get("run_id") != run_id) or
                (attempt is not None and info.get("attempt") != attempt) or
                info.get("dirty") is not False or result.get("status") != "finished" or
                result.get("exit_code") != 0 or result.get("diagnostic_errors") or
                not parsed or last.get("event") != "session_finished" or
                last.get("exit_code") != 0 or last.get("diagnostic_errors") or
                type(last.get("selected")) is not int or last.get("selected", 0) <= 0 or
                last.get("selected") != last.get("finished") or
                last.get("finished") != sum(e.get("event") == "test_finished" for e in events) or
                result.get("last_event") != last):
            raise ValueError("incomplete")
        finished = _finished_cases(events)
        if _digest(finished) != last.get("selected_digest"):
            raise ValueError("selection_mismatch")
        _check_workers(output, last, finished)
        for name in REPORTS:
            raw = (output / name).read_bytes()
            if result["reports"][name] != {
                    "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}:
                raise ValueError("report_changed")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("pytest evidence: incomplete, mismatched or unreadable", flush=True)
        return 1
    print("pytest evidence: complete and matched to SHA", flush=True)
    return 0


def _digest(cases) -> str:
    # Same order-independent digest as ci_pytest_progress.cases_digest.
    return hashlib.sha256("\n".join(sorted(cases)).encode("ascii")).hexdigest()


def _finished_cases(events: list[dict]) -> set:
    """Opaque identities of completed tests; each may complete only once."""
    cases = [e.get("case") for e in events if e.get("event") == "test_finished"]
    if not all(isinstance(c, str) and CASE.fullmatch(c) for c in cases):
        raise ValueError("case_identity")
    if len(set(cases)) != len(cases):
        raise ValueError("duplicate_finish")
    return set(cases)


def _check_workers(output: Path, last: dict, finished: set):
    """Serial evidence has no worker streams; xdist evidence needs every one of them."""
    present = set()
    for path in output.iterdir():
        match = WORKER_STREAM.fullmatch(path.name)
        if match:
            present.add(match.group(1))
    mode = last.get("mode")
    if mode == "serial":
        if present or "workers" in last:
            raise ValueError("unexpected_worker_streams")
        return
    workers = last.get("workers")
    if mode != "controller" or not isinstance(workers, list) or not workers:
        raise ValueError("mode")
    names = [w.get("worker") if isinstance(w, dict) else None for w in workers]
    if (not all(isinstance(n, str) and WORKER.fullmatch(n) for n in names) or
            len(set(names)) != len(names) or set(names) != present):
        raise ValueError("worker_set")
    accounted = set()
    for summary, name in zip(workers, names, strict=True):
        events, parsed = read_events(output / f"events.{name}.jsonl")
        tail = events[-1] if events else {}
        cases = _finished_cases(events)
        if (not parsed or tail.get("event") != "session_finished" or
                tail.get("mode") != "worker" or tail.get("worker") != name or
                tail.get("exit_code") != 0 or tail.get("diagnostic_errors") or
                tail.get("selected") != last["selected"] or
                tail.get("selected_digest") != last["selected_digest"] or
                tail.get("finished") != len(cases) or
                tail.get("finished_digest") != _digest(cases) or
                summary.get("events") != len(events) or
                summary.get("finished") != len(cases) or accounted & cases):
            raise ValueError("worker_stream")
        accounted |= cases
    if accounted != finished:
        raise ValueError("worker_accounting")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--sha")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.check:
        return check(args.output, args.sha or "",
                     _digits(os.environ.get("GITHUB_RUN_ID", "")),
                     _digits(os.environ.get("GITHUB_RUN_ATTEMPT", "")))
    pytest_args = args.pytest_args
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    try:
        return run(args.output, pytest_args)
    except (OSError, ValueError, subprocess.SubprocessError):
        # No completed pytest exit code exists. Never convert this into success.
        print("pytest evidence runner: preparation or launch failed", flush=True)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
