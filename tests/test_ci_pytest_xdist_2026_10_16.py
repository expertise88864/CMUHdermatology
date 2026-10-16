"""CI pytest evidence under pytest-xdist stays as strict as the serial evidence.

Anonymous real pytest children (``-p xdist.plugin -n 2``). Every selected test must
finish exactly once across all workers; each process's stream must be contiguous
and complete; a crashed, missing, extra or overlapping worker stream is never green.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import psutil
import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_ci_pytest.py"
spec = importlib.util.spec_from_file_location("ci_runner_xdist_under_test", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

SOURCES = {
    "test_alpha.py": "import pytest\n@pytest.mark.parametrize('v', range(4))\n"
                     "def test_value(v):\n    assert v >= 0\ndef test_plain():\n    pass\n",
    "test_beta.py": "import pytest\n@pytest.fixture\ndef skipped():\n    pytest.skip('anonymous')\n"
                    "def test_skip(skipped):\n    pass\ndef test_other():\n    pass\n",
    "test_gamma.py": "def test_one():\n    pass\ndef test_two():\n    pass\n",
}


def _env():
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def _command(output, workers="2"):
    assert importlib.util.find_spec("xdist") is not None, "CI installs pytest-xdist"
    return [sys.executable, "-X", "utf8", str(RUNNER), "--output", str(output), "--",
            "-q", "-p", "no:cacheprovider", "-p", "xdist.plugin", "-n", workers,
            "--dist", "loadfile", "--max-worker-restart", "0", "--junitxml=junit.xml"]


class Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _run_owned(command: list, directory: Path, timeout: float = 90) -> Completed:
    """Files instead of pipes: xdist workers inherit handles, and a hung run must
    fail this test (killing only our own process tree) instead of hanging it."""
    out, err = directory / "run.stdout", directory / "run.stderr"
    with out.open("wb") as stdout, err.open("wb") as stderr:
        proc = subprocess.Popen(command, cwd=directory, env=_env(), stdout=stdout, stderr=stderr)
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            for child in psutil.Process(proc.pid).children(recursive=True):
                child.kill()
            proc.kill()
            proc.wait(timeout=30)
            pytest.fail("anonymous xdist run did not finish (hung after a worker crash?)")
    return Completed(returncode, out.read_text(encoding="utf-8", errors="replace"),
                     err.read_text(encoding="utf-8", errors="replace"))


def _run(directory: Path, sources: dict, workers="2"):
    for name, source in sources.items():
        (directory / name).write_text(source, encoding="utf-8")
    output = directory / "evidence"
    return _run_owned(_command(output, workers), directory), output


def _complete(directory: Path, output: Path):
    """Stamp synthetic provenance and the second report, as the serial tests do."""
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    runner._write(output / "metadata.json", {**metadata, "sha": "a" * 40, "dirty": False})
    (directory / "cov.json").write_text('{"synthetic": true}', encoding="utf-8")
    old_cwd = Path.cwd()
    try:
        os.chdir(directory)
        runner.finish(output, 0, time.perf_counter())
    finally:
        os.chdir(old_cwd)


def _events(path: Path) -> list:
    events, parsed = runner.read_events(path)
    assert parsed
    return events


def _rewrite(path: Path, events: list):
    for seq, event in enumerate(events, 1):
        event["seq"] = seq
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


@pytest.fixture(scope="module")
def valid_evidence(tmp_path_factory):
    directory = tmp_path_factory.mktemp("xdist-valid")
    cp, output = _run(directory, SOURCES)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    _complete(directory, output)
    assert runner.check(output, "a" * 40) == 0
    return directory


@pytest.fixture
def evidence(valid_evidence, tmp_path):
    """A private copy of the valid xdist evidence for one negative case."""
    output = tmp_path / "evidence"
    shutil.copytree(valid_evidence / "evidence", output)
    assert runner.check(output, "a" * 40) == 0
    return output


def test_every_selected_test_finishes_exactly_once_across_workers(evidence):
    controller = _events(evidence / "events.jsonl")
    last = controller[-1]
    assert last["mode"] == "controller" and last["exit_code"] == 0
    assert last["selected"] == last["finished"] == 9
    workers = sorted(p.name for p in evidence.glob("events.gw*.jsonl"))
    assert workers == ["events.gw0.jsonl", "events.gw1.jsonl"]
    assert [w["worker"] for w in last["workers"]] == ["gw0", "gw1"]
    controller_cases = [e["case"] for e in controller if e["event"] == "test_finished"]
    worker_cases = []
    for name in workers:
        stream = _events(evidence / name)
        assert stream[-1]["mode"] == "worker" and stream[-1]["selected"] == 9
        assert stream[-1]["selected_digest"] == last["selected_digest"]
        # Phase starts are recorded by the process that really ran the phase.
        assert {e["phase"] for e in stream if e["event"] == "phase_started"} <= {
            "setup", "call", "teardown"}
        worker_cases += [e["case"] for e in stream if e["event"] == "test_finished"]
    assert sorted(worker_cases) == sorted(controller_cases)
    assert len(set(controller_cases)) == 9
    assert "anonymous" not in (evidence / "events.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize("damage", [
    "missing_worker_stream", "extra_worker_stream", "worker_truncated",
    "worker_exit_code", "worker_errors", "worker_lost_test", "worker_overlap",
    "controller_duplicate_finish", "controller_selection_digest", "controller_mode",
    "controller_worker_summary",
])
def test_each_xdist_rejection_starts_from_valid_evidence(evidence, damage):
    gw0, gw1 = evidence / "events.gw0.jsonl", evidence / "events.gw1.jsonl"
    controller_path = evidence / "events.jsonl"
    controller = _events(controller_path)
    if damage == "missing_worker_stream":
        gw1.unlink()
    elif damage == "extra_worker_stream":
        shutil.copyfile(gw1, evidence / "events.gw7.jsonl")
    elif damage == "worker_truncated":
        gw0.write_text("".join(gw0.read_text(encoding="utf-8").splitlines(True)[:-1]),
                       encoding="utf-8")
    elif damage in ("worker_exit_code", "worker_errors"):
        stream = _events(gw0)
        if damage == "worker_exit_code":
            stream[-1]["exit_code"] = 1
        else:
            stream[-1]["diagnostic_errors"] = ["anonymous diagnostic failure"]
        _rewrite(gw0, stream)
    elif damage == "worker_lost_test":
        # Consistent-looking worker stream that no longer accounts for one test.
        stream = _events(gw0)
        index = next(i for i, e in enumerate(stream) if e["event"] == "test_finished")
        del stream[index]
        stream[-1]["finished"] -= 1
        _rewrite(gw0, stream)
    elif damage == "worker_overlap":
        source = next(e for e in _events(gw0) if e["event"] == "test_finished")
        stream = _events(gw1)
        stream.insert(-1, dict(source))
        stream[-1]["finished"] += 1
        _rewrite(gw1, stream)
    elif damage == "controller_duplicate_finish":
        finished = [e for e in controller if e["event"] == "test_finished"]
        controller.insert(-1, dict(finished[0]))
        controller[-1]["finished"] += 1
        controller[-1]["selected"] += 1
    elif damage == "controller_selection_digest":
        controller[-1]["selected_digest"] = "0" * 64
    elif damage == "controller_mode":
        controller[-1]["mode"] = "serial"
    elif damage == "controller_worker_summary":
        controller[-1]["workers"] = controller[-1]["workers"][:1]
    if damage.startswith("controller_"):
        _rewrite(controller_path, controller)
        result = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
        runner._write(evidence / "result.json", {**result, "last_event": controller[-1]})
    assert runner.check(evidence, "a" * 40) == 1


def test_crashed_worker_is_not_a_completed_run(tmp_path):
    sources = dict(SOURCES)
    sources["test_crash.py"] = "import os\ndef test_crash():\n    os._exit(3)\n"
    cp, output = _run(tmp_path, sources)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    last = _events(output / "events.jsonl")[-1]
    assert last["event"] == "session_finished" and last["exit_code"] == 1
    assert "worker_crashed" in last["diagnostic_errors"]
    assert last["finished"] < last["selected"]
    _complete(tmp_path, output)
    assert runner.check(output, "a" * 40) == 1


def test_failure_keeps_exit_code_and_controller_prints_phases(tmp_path):
    sources = {"test_fail.py": "def test_fails():\n    assert False\n", **SOURCES}
    cp, output = _run(tmp_path, sources)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    # Workers' stdout is not the job log; the controller prints every phase.
    for phase in ("setup", "call", "teardown"):
        assert f"[cmuh-pytest] {phase} test_fail.py::test_fails case=" in cp.stdout
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "finished" and result["exit_code"] == 1
    _complete(tmp_path, output)
    assert runner.check(output, "a" * 40) == 1


def test_serial_evidence_rejects_worker_streams_and_duplicates(tmp_path):
    (tmp_path / "test_serial.py").write_text("def test_a():\n    pass\ndef test_b():\n    pass\n",
                                             encoding="utf-8")
    output = tmp_path / "evidence"
    command = [sys.executable, "-X", "utf8", str(RUNNER), "--output", str(output), "--",
               "-q", "-p", "no:cacheprovider", "--junitxml=junit.xml"]
    cp = _run_owned(command, tmp_path)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    _complete(tmp_path, output)
    assert runner.check(output, "a" * 40) == 0
    events = _events(output / "events.jsonl")
    assert events[-1]["mode"] == "serial" and "workers" not in events[-1]
    (output / "events.gw0.jsonl").write_text("", encoding="utf-8")
    assert runner.check(output, "a" * 40) == 1
    (output / "events.gw0.jsonl").unlink()
    assert runner.check(output, "a" * 40) == 0
    # Same count, but one test completed twice and another never did.
    finished = [i for i, e in enumerate(events) if e["event"] == "test_finished"]
    events[finished[1]]["case"] = events[finished[0]]["case"]
    _rewrite(output / "events.jsonl", events)
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    runner._write(output / "result.json", {**result, "last_event": events[-1]})
    assert runner.check(output, "a" * 40) == 1


def test_ci_runs_pytest_on_four_xdist_workers_with_the_pinned_plugin():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r"- name: Pytest[^\n]*\n((?:[ \t]+(?!run:)[^\n]*\n)*)[ \t]+run: ([^\n]+)",
                      workflow)
    preamble, step = match.group(1), match.group(2)
    # Temp files on the runner's temp volume, not the OS disk (see ci.yml).
    assert "TEMP: ${{ runner.temp }}" in preamble and "TMP: ${{ runner.temp }}" in preamble
    assert "scripts/run_ci_pytest.py" in step
    assert " -n 4 " in step and " --dist loadfile " in step and "--cov=src" in step
    # A crashed worker already fails the run; a replacement worker can leave
    # xdist 3.8's loadfile scheduler waiting until the job timeout instead.
    assert " --max-worker-restart 0 " in step
    pinned = re.search(r'"pytest-xdist==([0-9.]+)"', workflow).group(1)
    setup = (ROOT / "tools" / "dev-env-setup.py").read_text(encoding="utf-8")
    assert f'"pytest-xdist=={pinned}"' in setup
    # The parallel run keeps the per-job ceiling that was measured for it.
    assert re.search(r"timeout-minutes: 45\n", workflow)
