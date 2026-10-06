"""Run unchanged pytest arguments once, preserving its exit code and fresh evidence.

Only for isolated development checkouts. Removes stale junit.xml and cov.json.
The evidence checker is an additional delivery requirement, not a pytest verdict.
"""
from __future__ import annotations

import argparse
import hashlib
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
            "platform_version": platform.version()}


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
