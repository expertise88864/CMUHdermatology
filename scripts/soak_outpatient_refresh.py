"""Offline refresh stress: real bounded executor, existing anonymous app harness.

Developer checkout only; dependencies and pytest must already be installed.
python scripts/soak_outpatient_refresh.py --cycles 100 --output soak.json
No real Tk or service is used. GUI resources are measured by benchmark_runtime_offline.py.
"""
from pathlib import Path
import argparse
import gc
import hashlib
import importlib.util
import json
import logging
import socket
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from unittest.mock import patch
import pytest
import psutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--cycles', type=int, default=100)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
if args.cycles < 4 or args.cycles % 4:
    parser.error('--cycles must be a positive multiple of 4')
ROOT = args.root.resolve()
sys.path.insert(0, str(ROOT / 'src'))
rows = []
blocked = []

class ForbiddenExternalAction(BaseException):
    pass

def forbidden(*args, **kwargs):
    blocked.append('external action')
    raise ForbiddenExternalAction('No external I/O permitted')

def resources():
    process = psutil.Process()
    return dict(rss_bytes=process.memory_info().rss, handles=process.num_handles(),
                python_threads=len(threading.enumerate()), os_threads=process.num_threads(),
                children=len(process.children(recursive=True)))

def close_probe_handlers():
    for handler in list(logging.getLogger().handlers):
        handler.close()
        logging.getLogger().removeHandler(handler)

args.output.write_text(json.dumps({'status': 'running', 'cycles': args.cycles,
                                  'source_root': str(ROOT)}), encoding='utf-8')
with tempfile.TemporaryDirectory(prefix='cmuh_refresh_soak_') as temp, ExitStack() as stack:
    stack.callback(close_probe_handlers)
    from cmuh_common import paths
    stack.enter_context(patch.object(paths, 'get_app_dir', lambda: temp))
    stack.enter_context(patch.object(socket, 'getaddrinfo', forbidden))
    stack.enter_context(patch.object(socket.socket, 'connect', forbidden))
    stack.enter_context(patch.object(subprocess, 'Popen', forbidden))
    import requests
    stack.enter_context(patch.object(requests.Session, 'request', forbidden))
    from cmuh_common.bounded_executor import BoundedThreadPoolExecutor, RejectedExecutionError
    spec = importlib.util.spec_from_file_location('existing_refresh_harness', ROOT / 'tests/test_outpatient_refresh_app_flow_2026_09_27.py')
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    for cycle in range(args.cycles):
        with pytest.MonkeyPatch.context() as mp:
            app, clock = harness._app(mp)
            executor = BoundedThreadPoolExecutor(max_workers=2, max_pending=2,
                                                 thread_name_prefix='SoakRefresh')
            app.bg_executor = executor
            futures = []
            submit = executor.submit
            def tracked_submit(fn, *args, _submit=submit, _futures=futures, **kwargs):
                future = _submit(fn, *args, **kwargs)
                _futures.append(future)
                return future
            mp.setattr(executor, 'submit', tracked_submit)
            release = threading.Event()
            entered = threading.Event()
            worker_errors, emitted = [], []
            observation_lock = threading.Lock()
            def fetch(queue, config, *, _entered=entered, _release=release,
                      _errors=worker_errors, _emitted=emitted, _lock=observation_lock):
                try:
                    if config['name'] == 'OLD':
                        _entered.set()
                        assert _release.wait(5)
                    harness._emit(queue, config, config['name'])
                    with _lock:
                        _emitted.append(config['name'])
                except BaseException as exc:
                    with _lock:
                        _errors.append(type(exc).__name__)
                    raise
            app._appointment_fetcher = fetch
            scenario = cycle % 4
            try:
                if scenario == 0:  # Real executor capacity exhaustion then recovery.
                    def blocker(_entered=entered, _release=release):
                        _entered.set()
                        assert _release.wait(5)
                    capacity_released = [threading.Event(), threading.Event()]
                    blocker_futures = []
                    for done in capacity_released:
                        future = submit(blocker)
                        blocker_futures.append(future)
                        # Registered after the executor's own slot release.
                        future.add_done_callback(lambda _f, done=done: done.set())
                    assert entered.wait(5)
                    app._trigger_refresh(False, [harness._doctor('REJECTED')])
                    assert isinstance(futures[-1].exception(), RejectedExecutionError)
                    app.process_ui_queue()
                    assert app.refresh_button.state == 'normal'
                    assert not app.all_doctors_data
                    release.set()
                    assert all(done.wait(5) for done in capacity_released)
                    for future in blocker_futures:
                        future.result(timeout=5)
                    futures.clear()
                    app._trigger_refresh(False, [harness._doctor('RECOVERED')])
                    futures[-1].result(timeout=5)
                    app.process_ui_queue()
                    assert app.all_doctors_data == {'RECOVERED': {'value': 'RECOVERED'}}
                else:
                    app._trigger_refresh(False, [harness._doctor('OLD')])
                    assert entered.wait(5)
                    if scenario == 1:  # Dedup and queue handoff.
                        app._trigger_refresh(False, [harness._doctor('OLD')])
                        app._trigger_refresh(True, [harness._doctor('NEW')])
                        assert len(futures) == 1 and app._refresh_lifecycle.queue_size == 1
                        release.set()
                        futures[0].result(timeout=5)
                        app.process_ui_queue()
                        assert len(futures) == 2
                        futures[1].result(timeout=5)
                        app.process_ui_queue()
                        assert app.all_doctors_data == {'OLD': {'value': 'OLD'}, 'NEW': {'value': 'NEW'}}
                    elif scenario == 2:  # Timed-out generation and real overlapping worker.
                        clock[0] += 901
                        app._trigger_refresh(True, [harness._doctor('NEW')])
                        futures[1].result(timeout=5)
                        app.process_ui_queue()
                        release.set()
                        futures[0].result(timeout=5)
                        app.process_ui_queue()
                        assert app.all_doctors_data == {'NEW': {'value': 'NEW'}}
                        assert 'OLD' in emitted, f'expected late OLD result, observed {emitted}'
                    else:  # Stop while worker is blocked; late result must be ignored.
                        app._trigger_refresh(True, [harness._doctor('NEW')])
                        app._refresh_lifecycle.stop()
                        app._shutting_down = True
                        executor.shutdown(wait=False, cancel_futures=True)
                        release.set()
                        futures[0].result(timeout=5)
                        app.process_ui_queue()
                        assert not app.all_doctors_data and len(futures) == 1
                        assert emitted == ['OLD'], f'expected late OLD result, observed {emitted}'
                assert app._refresh_lifecycle.queue_size == 0
                assert worker_errors == [], worker_errors
            finally:
                release.set()
                executor.shutdown(wait=True, cancel_futures=True)
            # The old fake root intentionally retains callbacks. It is not a
            # Tk resource measurement; the separate real-Tk probe owns that.
            callbacks_recorded = len(app.root.scheduled)
            app.root.scheduled.clear()
            del app, futures, executor
        gc.collect()
        row = {'cycle': cycle, 'scenario': scenario, 'resources': resources(),
               'fake_root_callbacks_recorded': callbacks_recorded,
               'successfully_emitted': emitted, 'worker_errors': worker_errors}
        rows.append(row)
        if cycle in (0, args.cycles-1):
            print(json.dumps(row), flush=True)
    assert not blocked
output = {'scope': 'Equal cycles of real bounded-executor saturation/recovery, duplicate+handoff, old/new overlapping generation, stop+late result. Existing fake UI only; no Tk performance claim.',
          'cycles': args.cycles, 'source_root': str(ROOT), 'python': sys.version,
          'source_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted((ROOT / 'src').rglob('*.py'))},
          'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'samples': rows, 'blocked_actions': blocked, 'status': 'completed'}
args.output.write_text(json.dumps(output, indent=2), encoding='utf-8')
