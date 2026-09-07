"""Bootstrap regressions: no real pip, application launch, or production data."""
import os
import ast
import logging
from pathlib import Path
import subprocess
import sys
import pytest
from cmuh_common import deps_lock
from cmuh_common import deps_runtime as dr, deps_installer as di, paths
from tests.test_restart_handshake_2026_09_04 import _Sim, _run


def test_live_repair_lock_cannot_be_stolen_by_elapsed_time(tmp_path, monkeypatch):
    path = str(tmp_path / 'repair.lock')
    first = dr._acquire_repair_lock(path)
    assert first is not None
    second = None
    try:
        monkeypatch.setattr(dr.time, 'time', lambda: 10**12)
        second = dr._acquire_repair_lock(path)
        assert second is None, 'elapsed time is not evidence that the repair exited'
    finally:
        if second is not None:
            dr._release_repair_lock(second, path)
        dr._release_repair_lock(first, path)


def test_ready_after_bootstrap_is_not_ignored(tmp_path):
    sim = _Sim(str(tmp_path / 'hs'), writes={0.1: 'bootstrapping', 0.2: 'ready'})
    assert _run(sim) == paths.HANDOVER_CONFIRMED
    assert not any(call[0] == 'terminate' for call in sim.calls)


def test_installer_finishes_all_declared_upgrades_before_any_import(tmp_path, monkeypatch):
    installer = object.__new__(di.DependencyInstaller)
    installer.libs = [('good', 'good'), ('bad_a', 'bad_a'), ('bad_b', 'bad_b')]
    installer.total_libs = 3
    installer._repair_libs = {('bad_a', 'bad_a'), ('bad_b', 'bad_b')}
    installer._bootstrap = False
    installer.failed_libs = []
    installer._closing = False
    installer.is_finished = False
    installer.update_ui = lambda *_args: None
    installer._run_on_ui_thread = lambda _callback: True
    installed, early_imports = set(), []
    def fake_import(name):
        if installed != {'bad_a', 'bad_b'}:
            early_imports.append(name)
        return object()
    monkeypatch.setattr(di.importlib, 'import_module', fake_import)
    monkeypatch.setattr(di, '_resolve_pip_spec', lambda pkg: pkg)
    monkeypatch.setattr(di, '_dependency_install_log_path', lambda: str(tmp_path / 'pip.log'))
    monkeypatch.setattr(di, '_rotate_dependency_install_log', lambda *_args: False)
    monkeypatch.setattr(di, 'parent_understands_bootstrapping', lambda: False)
    monkeypatch.setattr(dr, '_repair_lock_path', lambda: str(tmp_path / 'unused.lock'))
    monkeypatch.setattr(di.subprocess, 'run', lambda cmd, **_kwargs: installed.add(cmd[4]))
    installer._repair_lock_fd = deps_lock.acquire(str(tmp_path / 'repair.lock'))
    try:
        installer.run_installation()
    finally:
        os.close(installer._repair_lock_fd)
    assert installed == {'bad_a', 'bad_b'}
    assert early_imports == [], 'a good module can transitively import a not-yet-upgraded dependency'


def test_lazy_dependency_install_does_not_recreate_restart_handshake(tmp_path, monkeypatch):
    installer = object.__new__(di.DependencyInstaller)
    installer.libs = [('good', 'json')]
    installer.total_libs = 1
    installer._repair_libs = set()
    installer._bootstrap = False
    installer.failed_libs = []
    installer._closing = False
    installer.is_finished = False
    installer.update_ui = lambda *_args: None
    installer._run_on_ui_thread = lambda _callback: True
    installer._repair_lock_fd = deps_lock.acquire(str(tmp_path / 'repair.lock'))
    signals = []
    monkeypatch.setattr(di, 'parent_understands_bootstrapping', lambda: True)
    monkeypatch.setattr(di, 'parent_supports_repair_only', lambda: False)
    monkeypatch.setattr(di, 'restart_handshake_signal', signals.append)
    monkeypatch.setattr(di, '_dependency_install_log_path', lambda: str(tmp_path / 'pip.log'))
    monkeypatch.setattr(di, '_rotate_dependency_install_log', lambda *_args: False)
    try:
        installer.run_installation()
    finally:
        os.close(installer._repair_lock_fd)
    assert signals == []


def test_inherited_lease_survives_parent_handle_close(tmp_path):
    """Real dummy child, no pip: mimic GUI exit while its subprocess is alive."""
    path = str(tmp_path / 'repair.lock')
    fd = deps_lock.acquire(path)
    assert fd is not None
    child = None
    try:
        with deps_lock.child_lease(fd) as kwargs:
            child = subprocess.Popen(
                [sys.executable, '-c', 'import sys; print("ready", flush=True); sys.stdin.read()'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **kwargs)
        assert child.stdout.readline().strip() == b'ready'
        os.close(fd)
        fd = None
        contender = deps_lock.acquire(path)
        if contender is not None:
            os.close(contender)
            pytest.fail('pip must still hold the lease after its parent releases it')
    finally:
        if fd is not None:
            os.close(fd)
        if child is not None:
            child.communicate(timeout=10)
    reacquired = deps_lock.acquire(path)
    assert reacquired is not None
    os.close(reacquired)


@pytest.mark.parametrize('arrival', [0.1, 0.9])
def test_repair_only_returns_promptly_and_never_kills_repairer(tmp_path, arrival):
    sim = _Sim(str(tmp_path / 'hs'), writes={arrival: paths.HANDSHAKE_REPAIR_ONLY})
    assert _run(sim) == paths.SPAWN_CHILD_REPAIRING
    assert sim.t < 1.1
    assert not any(call[0] in ('terminate', 'confirmed') for call in sim.calls)


def test_repair_only_child_exits_before_application_can_start(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, 'is_frozen', lambda: False)
    monkeypatch.setattr(dr, 'get_app_dir', lambda: str(tmp_path))
    monkeypatch.setattr(dr, '_repair_lock_path', lambda: str(tmp_path / 'repair.lock'))
    monkeypatch.setattr(dr, 'parent_supports_repair_only', lambda: True)
    monkeypatch.setattr(dr, 'restart_handshake_active', lambda: True)
    signals, repaired = [], []
    monkeypatch.setattr(dr, 'restart_handshake_signal', signals.append)
    monkeypatch.setattr(
        dr, '_ensure_dependencies_locked',
        lambda *args, **kwargs: repaired.append((args, kwargs)))
    with pytest.raises(SystemExit) as exc:
        dr.ensure_dependencies([], bootstrap=True)
    assert exc.value.code == 0
    assert signals == [paths.HANDSHAKE_REPAIR_ONLY]
    assert len(repaired) == 1


def test_autoclock_repairing_does_not_report_failure():
    from types import SimpleNamespace
    # Execute only the real restart function, never import/run the punch app.
    source = Path(__file__).resolve().parents[1] / 'src' / 'autoclock.py'
    fn = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
              if isinstance(n, ast.FunctionDef) and n.name == 'restart_program')
    notified = []
    ns = {
        'sys': sys, 'logging': logging,
        '_scheduler_thread_ref': SimpleNamespace(is_alive=lambda: True),
        'CONFIGURE_IF_EMPTY_FLAG': '--configure-if-empty',
        '_machine_has_clock_accounts': lambda: False,
        'restart_self': lambda *a, **kw: paths.SPAWN_CHILD_REPAIRING,
        '_SPAWN_CHILD_REPAIRING': paths.SPAWN_CHILD_REPAIRING,
        '_SPAWN_CHILD_EXITED_ORDERLY': paths.SPAWN_CHILD_EXITED_ORDERLY,
        '_SPAWN_RECOVERY_FAILED': paths.SPAWN_RECOVERY_FAILED,
        '_SPAWN_CHILD_NEVER_READY': paths.SPAWN_CHILD_NEVER_READY,
        '_notify_restart_failed': lambda: notified.append('failure'),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), 'exec'), ns)
    ns['restart_program']()
    assert notified == []


@pytest.mark.parametrize('returncode', [0, 1])
def test_repair_reaper_is_async_and_retains_diagnostics(tmp_path, monkeypatch, returncode):
    import threading
    from types import SimpleNamespace
    err, hs = tmp_path / 'child.err', tmp_path / 'child.hs'
    err.write_text('repair failure details', encoding='utf-8')
    hs.write_text('repair_only 123', encoding='utf-8')
    pending, calls = [], []
    monkeypatch.setattr(threading, 'Thread', lambda **kw: SimpleNamespace(
        start=lambda: pending.append(kw['target'])))
    proc = SimpleNamespace(wait=lambda: calls.append('wait') or returncode)
    paths._retain_repair_child(proc, str(err), str(hs),
                               lambda: calls.append('retry'))
    assert calls == []  # parent/Tk returns without joining the child
    assert len(pending) == 1
    pending[0]()
    assert calls == (['wait', 'retry'] if returncode == 0 else ['wait'])
    assert err.read_text(encoding='utf-8') == 'repair failure details'
    assert not hs.exists()


def _functions_from_source(filename, names, namespace):
    source = Path(__file__).resolve().parents[1] / 'src' / filename
    functions = [n for n in ast.parse(source.read_text(encoding='utf-8')).body
                 if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)


def test_scheduler_repair_completion_retries_without_checking_updates():
    from types import SimpleNamespace
    queued, callbacks, calls = [], [], []
    def restart(**kwargs):
        calls.append('restart')
        if len(callbacks) == 0:
            callbacks.append(kwargs['on_repair_complete'])
            return paths.SPAWN_CHILD_REPAIRING
        kwargs['on_confirmed']()
        raise SystemExit(0)
    ns = {'logging': logging, 'restart_self': restart, '_HANDING_OVER': False,
          'SPAWN_RECOVERY_FAILED': paths.SPAWN_RECOVERY_FAILED}
    _functions_from_source('scheduler.py', ['_handover_and_restart'], ns)
    app = SimpleNamespace(
        storage=SimpleNamespace(quiesce_local=lambda: calls.append('quiesce'),
                                resume_sync=lambda: calls.append('resume')),
        root=SimpleNamespace(after=lambda delay, fn: queued.append((delay, fn))))
    ns['_handover_and_restart'](app)
    assert calls == ['quiesce', 'restart', 'resume']
    callbacks[0]()
    assert queued[0][0] == 5000
    with pytest.raises(SystemExit):
        queued[0][1]()
    assert ns['_HANDING_OVER'] is True
    assert calls == ['quiesce', 'restart', 'resume', 'quiesce', 'restart']


@pytest.mark.parametrize('original_exit_code', [None, 1])
def test_clock_repair_completion_waits_for_punch_lock_then_retries(original_exit_code):
    from types import SimpleNamespace
    lock_results, calls = iter([False, True]), []
    ns = {
        'running': SimpleNamespace(is_set=lambda: True),
        '_sleep_while_running': lambda seconds: calls.append(('sleep', seconds)) or True,
        'clock_lock': SimpleNamespace(acquire=lambda **kw: next(lock_results),
                                     release=lambda: calls.append('release')),
        'restart_program': lambda args, **kw: calls.append(('restart', args, kw)),
    }
    _functions_from_source('autoclock.py', ['_retry_after_dependency_repair'], ns)
    ns['_retry_after_dependency_repair']('--background', original_exit_code)
    assert calls == [('sleep', 5), ('sleep', 5),
                     ('restart', '--background', {
                         'hard_exit_code': 0 if original_exit_code is None
                         else original_exit_code}), 'release']


@pytest.mark.parametrize('scheduler_alive', [False, True])
def test_clock_defers_only_when_background_scheduler_keeps_parent_alive(scheduler_alive):
    from types import SimpleNamespace
    captured = []
    ns = {
        'sys': SimpleNamespace(argv=['autoclock.py', '--configure-if-empty']),
        'logging': logging, 'CONFIGURE_IF_EMPTY_FLAG': '--configure-if-empty',
        '_machine_has_clock_accounts': lambda: False,
        '_scheduler_thread_ref': SimpleNamespace(is_alive=lambda: scheduler_alive),
        'restart_self': lambda *a, **kw: captured.append(kw) or paths.SPAWN_CHILD_EXITED_ORDERLY,
        '_SPAWN_CHILD_REPAIRING': paths.SPAWN_CHILD_REPAIRING,
        '_SPAWN_CHILD_EXITED_ORDERLY': paths.SPAWN_CHILD_EXITED_ORDERLY,
    }
    _functions_from_source('autoclock.py', ['restart_program'], ns)
    ns['restart_program']()
    assert (captured[0]['on_repair_complete'] is not None) is scheduler_alive
