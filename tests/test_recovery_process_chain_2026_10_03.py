"""Owned-process interruption through real recovery, resolver and launchers.

Clinical entry bodies are markers: no HIS, SMTP, clock click or scheduler runs.
Only the tested commit checkpoint is instrumented; recovery is not a stub.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading

import pytest


REPO = Path(__file__).resolve().parents[1]
OLD = "2026.10.01.5"
NEW = "2026.10.03.1"
LAUNCHER = "中國醫皮膚科會診查詢程式.pyw"


def _entry(tag):
    return (
        "import json, os\nfrom pathlib import Path\n"
        "from cmuh_common.paths import get_app_dir\n"
        "from cmuh_common.version import CURRENT_VERSION\n"
        "root = Path(os.environ['CMUH_APP_DIR'])\n"
        f"payload = {{'tag': {tag!r}, 'source': __file__, 'root': get_app_dir(), 'version': CURRENT_VERSION}}\n"
        "(root / 'entry.json').write_text(json.dumps(payload), encoding='utf-8')\n"
    )


@pytest.fixture
def installation(tmp_path):
    app = tmp_path / "isolated-install"
    app.mkdir()
    shutil.copytree(REPO / "src", app / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    version_module = app / "src" / "cmuh_common" / "version.py"
    version_module.write_text(re.sub(r"(CURRENT_VERSION\s*=\s*['\"])[\d.]+",
                                     lambda match: match[1] + OLD,
                                     version_module.read_text(encoding="utf-8")), encoding="utf-8")
    safe_entries = ("consult_query.py", "scheduler.py", "coord_detector.py", "watchdog_runner.py")
    for entry in safe_entries:
        (app / "src" / entry).write_text(_entry("legacy"), encoding="utf-8")
    for launcher in REPO.glob("*.pyw"):
        shutil.copyfile(launcher, app / launcher.name)
    shutil.copyfile(REPO / "version_pointer.py", app / "version_pointer.py")
    shutil.copyfile(REPO / "manifest.json", app / "manifest.json")
    manifest = json.loads((app / "manifest.json").read_text(encoding="utf-8"))
    manifest["app_version"] = OLD
    for item in manifest["files"]:
        destination = app / item["local_filename"]
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / item["local_filename"], destination)
        item["version"] = OLD
        item["sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
    (app / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    version = app / "versions" / OLD
    shutil.copytree(app / "src", version / "src")
    for entry in safe_entries:
        (version / "src" / entry).write_text(_entry(OLD), encoding="utf-8")
    (version / ".complete").write_text(OLD, encoding="utf-8")
    (app / "current.txt").write_text(OLD, encoding="utf-8")
    (app / "settings").mkdir()
    return app


def _env(app):
    # Inherited launcher/restart state must not point a child at a real install.
    env = {key: value for key, value in os.environ.items() if not key.startswith("CMUH_")}
    env["CMUH_APP_DIR"] = str(app)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(app / "versions" / OLD / "src")
    return env


def _run(app, code=None, *, check=True):
    if code is None:
        command = [sys.executable, str(app / LAUNCHER)]
    else:
        script = app / "owned-probe.py"
        script.write_text(code, encoding="utf-8")
        command = [sys.executable, str(script), str(app)]
    result = subprocess.run(command, cwd=app, env=_env(app), capture_output=True,
                            text=True, encoding="utf-8", timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if check:
        assert result.returncode == 0, (result.stdout, result.stderr)
    return result


@contextmanager
def _paused_writer(app, code):
    script = app / "owned-interrupted-write.py"
    script.write_text(code, encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script), str(app)], cwd=app, env=_env(app),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert lines.get(timeout=20).strip() == "CHECKPOINT", "owned writer did not reach checkpoint"
        assert process.poll() is None
        yield process
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        reader.join(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    assert not reader.is_alive()


def _interrupt(app, code):
    with _paused_writer(app, code) as process:
        process.kill()  # Only this test-owned process, while its write lock is held.
        process.wait(timeout=10)
        assert process.returncode != 0


def _update_writer(*, omit_journal=False):
    return (
        "import sys\nfrom pathlib import Path\nfrom cmuh_common import updater as u\n"
        "app = Path(sys.argv[1])\n"
        "real_replace = u._replace_file_with_retry\n"
        "def checkpoint(src, dst):\n"
        "    real_replace(src, dst)\n"
        "    if Path(dst) == app / 'version_pointer.py':\n"
        "        print('CHECKPOINT', flush=True)\n"
        "        sys.stdin.read(1)\n"
        "u._replace_file_with_retry = checkpoint\n"
        + ("u._write_commit_journal = lambda *args: True\n" if omit_journal else "")
        + f"result = u.UpdateResult(manifest_app_version={NEW!r})\n"
        "version_code = (app / 'versions' / " + repr(OLD) + " / 'src/cmuh_common/version.py').read_text(encoding='utf-8')\n"
        "writes = [\n"
        f" ('entry', 'src/consult_query.py', {NEW!r}, {_entry(NEW)!r}, str(app / 'src/consult_query.py')),\n"
        f" ('version', 'src/cmuh_common/version.py', {NEW!r}, version_code.replace({OLD!r}, {NEW!r}), str(app / 'src/cmuh_common/version.py')),\n"
        f" ('resolver', 'version_pointer.py', {NEW!r}, 'raise RuntimeError(\"interrupted resolver\")', str(app / 'version_pointer.py')),\n"
        f" ('manifest', 'manifest.json', {NEW!r}, '{{\"app_version\": \"{NEW}\"}}', str(app / 'manifest.json'))]\n"
        "with u._updater_write_lock():\n"
        "    u._commit_pending_writes(writes, result)\n"
    )


def _assert_old_launch(app):
    result = _run(app)
    payload = json.loads((app / "entry.json").read_text(encoding="utf-8"))
    assert payload["tag"] == OLD
    assert payload["version"] == OLD
    assert Path(payload["root"]) == app
    assert Path(payload["source"]) == app / "versions" / OLD / "src" / "consult_query.py"
    assert (app / "current.txt").read_text(encoding="utf-8") == OLD
    manifest = json.loads((app / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["app_version"] == OLD
    assert all(item["version"] == OLD and
               hashlib.sha256((app / item["local_filename"]).read_bytes()).hexdigest() == item["sha256"]
               for item in manifest["files"])
    assert "無法" not in result.stderr


def test_killed_versioned_update_recovers_in_a_fresh_real_launcher(installation):
    app = installation
    before = {name: (app / name).read_bytes() for name in ("version_pointer.py", "manifest.json", LAUNCHER)}
    _interrupt(app, _update_writer())
    assert (app / ".updater_commit.journal").exists()
    assert (app / "version_pointer.py").read_bytes() != before["version_pointer.py"]
    assert (app / "versions" / NEW / ".complete").exists()
    _assert_old_launch(app)
    assert not (app / ".updater_commit.journal").exists()
    assert {name: (app / name).read_bytes() for name in before} == before
    _assert_old_launch(app)  # A second recovery/launch is idempotent.


def test_omitting_the_update_journal_is_detected_by_the_launch_invariant(installation):
    app = installation
    _interrupt(app, _update_writer(omit_journal=True))
    assert not (app / ".updater_commit.journal").exists()
    with pytest.raises(AssertionError):
        _assert_old_launch(app)
    assert json.loads((app / "entry.json").read_text(encoding="utf-8"))["tag"] == "legacy"


def test_real_file_lock_prevents_clinical_launch_then_a_fresh_retry_recovers(installation):
    from cmuh_common.deps_lock import acquire

    app = installation
    _interrupt(app, _update_writer())
    descriptor = acquire(str(app / "version_pointer.py"))
    assert descriptor is not None
    try:
        blocked = _run(app, check=False)
        assert blocked.returncode == 3
        assert not (app / "entry.json").exists()
        assert (app / ".updater_commit.journal").exists()
        assert (app / "version_pointer.py.bak").exists()
        assert "retryable_failure" in (app / "update_recovery.log").read_text(encoding="utf-8")
        # Preserve the explicit non-writing tool exception, with honest failure logs.
        prior_log = (app / "update_recovery.log").read_bytes()
        tool = subprocess.run([sys.executable, str(app / "中國醫皮膚科排班程式.pyw")],
                              env=_env(app), cwd=app, capture_output=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert tool.returncode == 0
        assert json.loads((app / "entry.json").read_text(encoding="utf-8"))["tag"] == "legacy"
        assert (app / ".updater_commit.journal").exists()
        current_log = (app / "update_recovery.log").read_bytes()
        assert current_log.startswith(prior_log)
        new_log = current_log[len(prior_log):].decode("utf-8")
        assert "retryable_failure" in new_log and "排班程式" in new_log
        (app / "entry.json").unlink()
    finally:
        os.close(descriptor)
    _assert_old_launch(app)
    assert not (app / ".updater_commit.journal").exists()


@pytest.mark.parametrize("launcher, program", [
    ("中國醫皮膚科排班程式.pyw", "排班程式"),
    ("中國醫皮膚科點座標偵測程式.pyw", "點座標偵測程式"),
    ("中國醫皮膚科守護程式.pyw", "守護程式"),
])
def test_missing_recovery_module_is_logged_without_blocking_nonwriting_tools(installation, launcher, program):
    app = installation
    (app / "src" / "bootstrap_recovery.py").unlink()
    prior_log = b"earlier recovery evidence\n"
    (app / "update_recovery.log").write_bytes(prior_log)
    journal = b"unresolved synthetic journal\n"
    (app / ".updater_commit.journal").write_bytes(journal)
    result = subprocess.run([sys.executable, str(app / launcher)],
                            env=_env(app), cwd=app, capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    payload = json.loads((app / "entry.json").read_text(encoding="utf-8"))
    assert payload["tag"] == OLD and payload["version"] == OLD
    assert (app / ".updater_commit.journal").read_bytes() == journal
    current_log = (app / "update_recovery.log").read_bytes()
    assert current_log.startswith(prior_log)
    new_log = current_log[len(prior_log):].decode("utf-8")
    assert "unknown" in new_log and "復原未完成" in new_log
    assert program in new_log and "FileNotFoundError" in new_log


def _settings_writer():
    return (
        "import sys\nfrom pathlib import Path\nfrom cmuh_common import atomic_io as io\n"
        "settings = Path(sys.argv[1]) / 'settings'\n"
        "target = settings / 'threshold_settings.json'\n"
        "real_replace = io._replace_with_retry\n"
        "def checkpoint(src, dst):\n"
        "    real_replace(src, dst)\n"
        "    if Path(dst) == target:\n"
        "        print('CHECKPOINT', flush=True)\n"
        "        sys.stdin.read(1)\n"
        "io._replace_with_retry = checkpoint\n"
        "io.atomic_write_json_multi([(str(target), {'alert_chen_enabled': False}),\n"
        " (str(settings / 'doctors.json'), [{'name': 'new synthetic'}])])\n"
    )


def _read_settings(app, *, omit_recovery=False):
    code = (
        "import json\nfrom cmuh_common import app_settings, atomic_io\n"
        + ("atomic_io.recover_interrupted_multiwrite = lambda directory: atomic_io.RecoveryResult(True)\n"
           if omit_recovery else "")
        + "a = app_settings.load_threshold_settings()\n"
        "b = app_settings.load_doctors_settings()\n"
        "print(json.dumps([a['alert_chen_enabled'], b]))\n"
    )
    result = _run(app, code)
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_killed_settings_transaction_recovers_through_real_loaders(installation):
    app = installation
    settings = app / "settings"
    (settings / "threshold_settings.json").write_text('{"alert_chen_enabled": true}', encoding="utf-8")
    (settings / "doctors.json").write_text('[{"name": "old synthetic"}]', encoding="utf-8")
    _interrupt(app, _settings_writer())
    assert json.loads((settings / "threshold_settings.json").read_text()) == {"alert_chen_enabled": False}
    assert (settings / ".multiwrite.manifest.json").exists()
    expected = [True, [{"name": "old synthetic", "doc_no": ""}]]
    assert _read_settings(app, omit_recovery=True) != expected
    assert _read_settings(app) == expected
    assert not (settings / ".multiwrite.manifest.json").exists()
    assert _read_settings(app) == expected


def _state_hashes(settings):
    return {p.relative_to(settings).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in settings.rglob("*") if p.is_file()}


def _program_hashes(app, files):
    paths = [app / name for name in files]
    for directory in ("src", "versions"):
        paths.extend(p for p in (app / directory).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    return {p.relative_to(app).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_program_only_rollback_preserves_post_snapshot_business_state(installation):
    from cmuh_common.action_ledger import ActionLedger, verify_generations
    from cmuh_common.delivery_ledger import DeliveryLedger

    app = installation
    snapshot = app.parent / "program-snapshot"
    manifest = json.loads((app / "manifest.json").read_text(encoding="utf-8"))
    program_files = [item["local_filename"] for item in manifest["files"]
                     if not item["local_filename"].startswith("src/")]
    program_files += ["manifest.json", "current.txt"]
    snapshot.mkdir()
    for name in program_files:
        (snapshot / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(app / name, snapshot / name)
    shutil.copytree(app / "src", snapshot / "src")
    shutil.copytree(app / "versions", snapshot / "versions")
    program_before = _program_hashes(app, program_files)

    # Newer state is created after the program snapshot. No external action occurs.
    settings = app / "settings"
    delivery = DeliveryLedger(str(settings / "delivery_ledger.sqlite3"))
    sent = delivery.claim_initial_delivery(business_key="synthetic-sent", category="consult",
                                   recipients=["demo@example.invalid"], occurrence_keys=["synthetic-slot"])
    delivery.settle(sent)
    pending = delivery.begin(business_key="synthetic-pending", category="consult", recipients=["demo@example.invalid"])
    delivery.settle(pending, unknown=True)
    states = [delivery.state_of(sent), delivery.state_of(pending)]
    delivery._conn.close()  # Controlled maintenance: every writer is stopped before copying.
    audit = settings / "action_ledger.jsonl"
    assert ActionLedger(str(audit), max_bytes=1).record("his_menu", "F9").fully_verifiable
    assert ActionLedger(str(audit), max_bytes=1).record("his_menu", "F10").fully_verifiable
    (settings / "clock_state.json").write_text('{"synthetic": {"pending": true}}', encoding="utf-8")
    (settings / "roster_finalized.json").write_text('{"synthetic": {"locked": true}}', encoding="utf-8")
    before = _state_hashes(settings)
    assert any(name.endswith(".anchor.json") for name in before)
    assert len(before) >= 6

    # Simulate a complete newer program, then rehearse the documented program-only restore.
    (app / "version_pointer.py").write_text('raise RuntimeError("newer program")', encoding="utf-8")
    (app / "versions" / OLD / "src" / "consult_query.py").write_text(_entry(NEW), encoding="utf-8")
    (app / "src" / "newer-only-module.py").write_text("newer = True", encoding="utf-8")
    for name in program_files:
        shutil.copyfile(snapshot / name, app / name)
    for directory in ("src", "versions"):
        retired = app.parent / ("retired-" + directory)
        assert (app / directory).resolve().is_relative_to(app.parent.resolve())
        assert retired.resolve().is_relative_to(app.parent.resolve())
        (app / directory).rename(retired)
        shutil.copytree(snapshot / directory, app / directory)
    assert _program_hashes(app, program_files) == program_before
    _assert_old_launch(app)
    assert _state_hashes(settings) == before
    assert verify_generations(str(audit))[0] is True
    reopened = DeliveryLedger(str(settings / "delivery_ledger.sqlite3"))
    try:
        assert [reopened.state_of(sent), reopened.state_of(pending)] == states
        assert reopened.claim_initial_delivery(business_key="synthetic-sent", category="consult",
                                        recipients=["demo@example.invalid"], occurrence_keys=["synthetic-slot"]) == ""
        assert reopened.has_live_delivery("synthetic-pending")
    finally:
        reopened._conn.close()


def test_maintenance_window_writers_use_a_real_cross_process_lease(installation, monkeypatch):
    from cmuh_common import update_policy

    app = installation
    monkeypatch.setattr(update_policy, "get_settings_dir", lambda: str(app / "settings"))
    code = (
        "import sys\nfrom cmuh_common import update_policy as p\n"
        "real_write = p.atomic_write_text\n"
        "def checkpoint(*args, **kwargs):\n"
        "    result = real_write(*args, **kwargs)\n"
        "    print('CHECKPOINT', flush=True)\n"
        "    sys.stdin.read(1)\n"
        "    return result\n"
        "p.atomic_write_text = checkpoint\n"
        "p.suspend_auto_updates('maintenance', duration_sec=86400, now=1000)\n"
    )
    with _paused_writer(app, code) as process:
        with pytest.raises(TimeoutError):
            update_policy.suspend_auto_updates("watchdog", duration_sec=3600, now=1001)
        assert update_policy.get_auto_update_suspend_until(now=5000) == 87400
        process.stdin.write("x")
        process.stdin.flush()
        process.wait(timeout=10)
        assert process.returncode == 0
    update_policy.suspend_auto_updates("watchdog", duration_sec=3600, now=1001)
    assert update_policy.get_auto_update_suspend_until(now=5000) == 87400
