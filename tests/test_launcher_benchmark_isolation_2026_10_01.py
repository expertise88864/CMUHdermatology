"""Offline benchmark must never execute an unowned deployment or reuse success."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from cmuh_common.paths import PARENT_CAP_BOOTSTRAPPING, PARENT_CAP_REPAIR_ONLY

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_launcher_offline.py"


@pytest.mark.parametrize("use_repo", [True, False])
def test_worker_refuses_unowned_app_before_any_source_runs(tmp_path, use_repo):
    app = ROOT if use_repo else tmp_path
    output = tmp_path / "result.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--worker", "--app", str(app),
         "--output", str(output)], capture_output=True, text=True,
        encoding="utf-8", timeout=15,
    )
    assert result.returncode != 0
    assert "Worker refuses non-isolated deployment" in result.stderr
    assert not output.exists()


def test_failed_parent_cannot_leave_an_old_completed_result(tmp_path):
    app = tmp_path / "empty_checkout"
    app.mkdir()
    output = tmp_path / "result.json"
    output.write_text('{"status":"completed"}', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(app),
         "--output", str(output)], capture_output=True, text=True,
        encoding="utf-8", timeout=15,
    )
    assert result.returncode != 0
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "running"


@pytest.mark.parametrize("entry", ["parent", "worker"])
def test_inherited_restart_handshake_cannot_touch_external_sentinel(tmp_path, entry):
    sentinel = tmp_path / "outside-deployment-handshake"
    sentinel.write_text("unchanged", encoding="utf-8")
    checkout = tmp_path / "synthetic_checkout"
    (checkout / "src").mkdir(parents=True)
    (checkout / "assets").mkdir()
    (checkout / "launcher.pyw").write_text("# main.py _PROGRAM", encoding="utf-8")
    (checkout / "version_pointer.py").write_text("", encoding="utf-8")
    env = dict(os.environ, CMUH_RESTART_HANDSHAKE=str(sentinel),
               CMUH_RESTART_READY_EVENT="synthetic-event-not-to-open",
               CMUH_RESTART_PARENT_CAPS="bootstrapping")
    # Exercise actual script entry and the actual dependency-bootstrap handshake
    # function. Substitute only child launch / named-event access; no real event.
    driver = r'''
import os, runpy, subprocess, sys
from unittest.mock import patch
from pathlib import Path
script, source, checkout, output, entry = sys.argv[1:]
captured = []
def child_boundary(*args, **kwargs):
    captured.append(kwargs["env"])
    raise SystemExit("synthetic child boundary")
sys.argv = [script, "--root", checkout, "--output", output]
if entry == "worker":
    sys.argv += ["--worker", "--app", checkout]
with patch.object(subprocess, "run", child_boundary):
    try: runpy.run_path(script, run_name="__main__")
    except SystemExit: pass
if entry == "parent":
    assert len(captured) == 1
    os.environ.clear()
    os.environ.update(captured[0])
sys.path.insert(0, str(Path(source) / "src"))
from cmuh_common import paths
events = []
with patch.object(paths, "open_ready_event", lambda: events.append(True)):
    paths.restart_handshake_signal("bootstrapping")
assert not events, "Inherited READY event was accessed"
assert not any(key in os.environ for key in (
    "CMUH_RESTART_HANDSHAKE", "CMUH_RESTART_READY_EVENT", "CMUH_RESTART_PARENT_CAPS"))
'''
    result = subprocess.run(
        [sys.executable, "-c", driver, str(SCRIPT), str(ROOT), str(checkout),
         str(tmp_path / "result.json"), entry], env=env, capture_output=True,
        text=True, encoding="utf-8", timeout=15,
    )
    assert sentinel.read_text(encoding="utf-8") == "unchanged", result.stderr
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failure", ["none", "body", "cleanup"])
def test_runtime_probe_only_completes_after_cleanup(tmp_path, failure):
    output = tmp_path / "runtime-result.json"
    output.write_text('{"status":"completed"}', encoding="utf-8")
    # Execute the real report/TemporaryDirectory boundary, substituting the UI
    # work only. No application import, Tk window or actual HIS is needed.
    driver = r'''
import ast, sys, tempfile
from pathlib import Path
from unittest.mock import patch
script, source, output, failure = sys.argv[1:]
tree = ast.parse(Path(script).read_text(encoding="utf-8"))
outer = next(node for node in tree.body if isinstance(node, ast.With))
first_work = next(i for i, node in enumerate(outer.body)
                  if isinstance(node, ast.ImportFrom) and node.module == "cmuh_common")
last_check = max(i for i, node in enumerate(outer.body) if isinstance(node, ast.Assert))
# Replace only application/UI work. Keep the actual report writes, their
# indentation and both context managers, including real temporary cleanup.
outer.body[first_work:last_check] = ast.parse("substitute_work()").body
ast.fix_missing_locations(tree)
entered, cleaned = [], []
original = tempfile.TemporaryDirectory

class ControlledDirectory(original):
    def __exit__(self, *args):
        result = super().__exit__(*args)
        assert not Path(self.name).exists()
        cleaned.append(True)
        if failure == "cleanup":
            raise PermissionError("synthetic cleanup failure")
        return result

def substitute_work():
    entered.append(True)
    if failure == "body":
        raise RuntimeError("synthetic body failure")

sys.argv = [script, "--root", source, "--cycles", "1", "--missing-icon", "--output", output]
with patch.object(tempfile, "TemporaryDirectory", ControlledDirectory):
    try:
        exec(compile(tree, script, "exec"), {
            "__file__": script, "__name__": "__main__", "substitute_work": substitute_work})
    finally:
        assert entered == [True] and cleaned == [True]
'''
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", driver,
         str(ROOT / "scripts" / "benchmark_runtime_offline.py"),
         str(ROOT), str(output), failure],
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    if failure == "none":
        assert result.returncode == 0, result.stderr
        assert report["status"] == "completed"
    else:
        assert result.returncode != 0
        assert f"synthetic {failure} failure" in result.stderr
        assert report["status"] == "running"


@pytest.mark.parametrize("probe", ["benchmark_runtime_offline.py", "soak_outpatient_refresh.py"])
@pytest.mark.parametrize("caps", [PARENT_CAP_BOOTSTRAPPING,
                                 f"{PARENT_CAP_BOOTSTRAPPING} {PARENT_CAP_REPAIR_ONLY}"])
def test_other_probe_entries_do_not_inherit_restart_bootstrap(tmp_path, probe, caps):
    sentinel = tmp_path / "outside-probe-handshake"
    sentinel.write_text("unchanged", encoding="utf-8")
    isolated = tmp_path / "isolated-probe"
    isolated.mkdir()
    env = dict(os.environ, CMUH_RESTART_HANDSHAKE=str(sentinel),
               CMUH_RESTART_READY_EVENT="synthetic-event-never-open",
               CMUH_RESTART_PARENT_CAPS=caps)
    # Run each actual entry until its first cmuh import, without opening Tk or
    # launching workers. Then exercise the real bootstrap cache-miss handshake;
    # only lock/package work and named event access use isolated substitutes.
    driver = r'''
import ast, os, runpy, sys
from pathlib import Path
from unittest.mock import patch
script, source, isolated, output = sys.argv[1:]
tree = ast.parse(Path(script).read_text(encoding="utf-8"))
boundary = min(node.lineno for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) and node.module == "cmuh_common")
class BeforeApplicationImport(BaseException): pass
def stop_before_import(frame, event, arg):
    if event == "line" and frame.f_code.co_filename == script and frame.f_lineno == boundary:
        raise BeforeApplicationImport()
    return stop_before_import
sys.argv = [script, "--root", source, "--cycles", "4", "--output", output]
sys.settrace(stop_before_import)
try:
    try: runpy.run_path(script, run_name="__main__")
    except BeforeApplicationImport: pass
finally: sys.settrace(None)
sys.path.insert(0, str(Path(source) / "src"))
from cmuh_common import paths, deps_runtime
events, checked = [], []
with patch.object(paths, "open_ready_event", lambda: events.append(True)), \
     patch.object(deps_runtime, "get_app_dir", lambda: isolated), \
     patch.object(deps_runtime, "_repair_lock_path", lambda: str(Path(isolated) / "repair.lock")), \
     patch.object(deps_runtime, "_acquire_repair_lock", lambda _path: object()), \
     patch.object(deps_runtime, "_release_repair_lock", lambda *_args: None), \
     patch.object(deps_runtime, "_ensure_dependencies_locked", lambda *_args, **_kw: checked.append(True)):
    try: deps_runtime.ensure_dependencies([], bootstrap=True)
    except SystemExit as exc: assert exc.code == 0
assert checked == [True], "Actual cache-miss bootstrap was not exercised"
assert not events, "Inherited READY event was accessed"
assert not any(key in os.environ for key in (
    "CMUH_RESTART_HANDSHAKE", "CMUH_RESTART_READY_EVENT", "CMUH_RESTART_PARENT_CAPS"))
'''
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", driver,
         str(ROOT / "scripts" / probe), str(ROOT), str(isolated),
         str(tmp_path / "probe-result.json")],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert sentinel.read_text(encoding="utf-8") == "unchanged", result.stderr
    assert result.returncode == 0, result.stderr
