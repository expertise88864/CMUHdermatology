"""Offline benchmark must never execute an unowned deployment or reuse success."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

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
