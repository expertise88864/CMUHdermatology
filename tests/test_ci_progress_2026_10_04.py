"""Anonymous real pytest children; phase failures and termination are not success."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_ci_pytest.py"
spec = importlib.util.spec_from_file_location("ci_runner_under_test", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def _env():
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def _command(output):
    return [sys.executable, "-X", "utf8", str(RUNNER), "--output", str(output),
            "--", "-q", "-p", "no:cacheprovider", "--junitxml=junit.xml"]


def _run(tmp_path, source):
    (tmp_path / "test_anonymous.py").write_text(source, encoding="utf-8")
    output = tmp_path / "evidence"
    cp = subprocess.run(_command(output), cwd=tmp_path, env=_env(),
                        capture_output=True, encoding="utf-8", errors="replace", timeout=20)
    events, parsed = runner.read_events(output / "events.jsonl")
    assert parsed
    return cp, output, events


def test_normal_phase_counts_and_no_parameter_or_environment_values(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_TOKEN", "do-not-record-this")
    cp, output, events = _run(tmp_path, '''import pytest
@pytest.mark.parametrize("value", ["do-not-record-this"])
def test_example(value):
    assert value
''')
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert [e["phase"] for e in events if e["event"] == "phase_started"] == [
        "setup", "call", "teardown"]
    assert events[-1]["selected"] == events[-1]["finished"] == 1
    assert all(e["test"] == "test_anonymous.py::test_example" for e in events if "phase" in e)
    assert "do-not-record-this" not in (output / "events.jsonl").read_text()
    assert "do-not-record-this" not in (output / "metadata.json").read_text()
    assert "do-not-record-this" not in cp.stdout
    assert json.loads((output / "result.json").read_text())["exit_code"] == 0


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_failure_preserves_exit_code_and_identifies_phase(tmp_path, phase):
    source = f'''import pytest
@pytest.fixture
def prepared():
    if {phase!r} == "setup": assert False
    yield
    if {phase!r} == "teardown": assert False
def test_example(prepared):
    if {phase!r} == "call": assert False
'''
    cp, output, events = _run(tmp_path, source)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    assert any(e.get("phase") == phase and e.get("outcome") == "failed" for e in events)
    result = json.loads((output / "result.json").read_text())
    assert result["exit_code"] == 1 and result["status"] == "finished"
    assert runner.check(output, "a" * 40) == 1


def test_collection_error_is_not_a_completed_passing_run(tmp_path):
    cp, output, events = _run(tmp_path, "raise ValueError('anonymous collection error')\n")
    assert cp.returncode == 2
    assert any(e["event"] == "collection_started" for e in events)
    assert events[-1]["exit_code"] == 2
    assert runner.check(output, "a" * 40) == 1


@pytest.mark.parametrize("phase", ["collection", "setup", "call", "teardown"])
def test_owned_child_terminated_mid_phase_keeps_last_phase_and_partial_state(tmp_path, phase):
    source = f'''import os, threading, pathlib, pytest
def stop_here():
    pathlib.Path("owned.pid").write_text(str(os.getpid()))
    threading.Event().wait(60)
if {phase!r} == "collection": stop_here()
@pytest.fixture
def prepared():
    if {phase!r} == "setup": stop_here()
    yield
    if {phase!r} == "teardown": stop_here()
def test_example(prepared):
    if {phase!r} == "call": stop_here()
'''
    (tmp_path / "test_anonymous.py").write_text(source, encoding="utf-8")
    output = tmp_path / "evidence"
    proc = subprocess.Popen(_command(output), cwd=tmp_path, env=_env(), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    child_pid = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            marker = tmp_path / "owned.pid"
            if marker.exists():
                child_pid = int(marker.read_text())
                break
            assert proc.poll() is None, "wrapper exited before anonymous checkpoint"
            time.sleep(0.01)
        assert child_pid is not None, "anonymous child did not reach synchronized checkpoint"
        events, parsed = runner.read_events(output / "events.jsonl")
        assert parsed
        if phase == "collection":
            assert events[-1]["event"] == "collector_started"
            assert events[-1]["test"] == "test_anonymous.py"
        else:
            assert events[-1]["phase"] == phase
        os.kill(child_pid, signal.SIGTERM)
        child_pid = None
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode != 0, stdout + stderr
        result = json.loads((output / "result.json").read_text())
        assert result["status"] == "incomplete" and result["exit_code"] == proc.returncode
        assert result["last_event"] == events[-1]
        expected = "collect test_anonymous.py" if phase == "collection" else f"{phase} test_anonymous.py::test_example"
        assert f"[cmuh-pytest] {expected}" in stdout
        assert not (output / "junit.xml").exists()
        assert runner.check(output, "a" * 40) == 1
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)


def test_stale_reports_are_removed_and_old_evidence_is_never_reused(tmp_path):
    for name in runner.REPORTS:
        (tmp_path / name).write_text("stale evidence", encoding="utf-8")
    cp, output, _events = _run(tmp_path, "def test_example(): assert True\n")
    assert cp.returncode == 0
    assert "stale evidence" not in (output / "junit.xml").read_text(encoding="utf-8")
    assert not (tmp_path / "cov.json").exists()
    original = (output / "result.json").read_bytes()
    cp2 = subprocess.run(_command(output), cwd=tmp_path, env=_env(), capture_output=True, timeout=20)
    assert cp2.returncode == 125
    assert (output / "result.json").read_bytes() == original


def test_checker_requires_fresh_sha_complete_counts_and_matching_reports(tmp_path):
    cp, output, events = _run(tmp_path, "def test_example(): assert True\n")
    assert cp.returncode == 0
    # Complete the synthetic provenance and second report for checker negatives.
    metadata = json.loads((output / "metadata.json").read_text())
    metadata.update(sha="a" * 40, dirty=False)
    runner._write(output / "metadata.json", metadata)
    (tmp_path / "cov.json").write_text('{"synthetic": true}')
    monkey_start = time.perf_counter()
    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        runner.finish(output, 0, monkey_start)
    finally:
        os.chdir(old_cwd)
    assert runner.check(output, "a" * 40) == 0
    assert runner.check(output, "b" * 40) == 1
    assert runner.check(output, "a" * 40, run_id="98765") == 1
    assert runner.check(output, "a" * 40, attempt="99") == 1
    # A forged terminal count cannot replace the actual completed-test events.
    last = events[-1]
    last["selected"] = last["finished"] = 2
    runner._write(output / "result.json", {**json.loads((output / "result.json").read_text()),
                                         "last_event": last})
    (output / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    assert runner.check(output, "a" * 40) == 1
    last["selected"] = last["finished"] = 1
    (output / "cov.json").write_text('{"changed": true}')
    assert runner.check(output, "a" * 40) == 1
    assert events[-1]["selected"] == 1


def test_absolute_and_parent_paths_are_not_disclosed():
    spec = importlib.util.spec_from_file_location("progress_for_privacy", ROOT / "scripts/ci_pytest_progress.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for node in ["C:/Users/private/test_case.py::test_x", "../private/test_case.py::test_x",
                 "/home/private/test_case.py::test_x"]:
        assert module.Progress.identity(node)["test"] == "test"


def test_truncated_or_out_of_sequence_events_are_not_complete(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"seq":1,"event":"phase_started"}\n{"seq":2', encoding="utf-8")
    events, valid = runner.read_events(path)
    assert len(events) == 1 and not valid
    path.write_text('{"seq":2,"event":"session_finished"}\n')
    assert runner.read_events(path) == ([], False)


def test_finalization_io_error_cannot_change_original_pytest_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    class Result:
        returncode = 1
    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs: Result())
    monkeypatch.setattr(runner, "metadata", lambda: {})
    monkeypatch.setattr(runner, "finish", lambda *_args: (_ for _ in ()).throw(OSError()))
    assert runner.run(tmp_path / "evidence", []) == 1
