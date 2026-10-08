"""Offline Windows UI/resource benchmark; dependencies must already be installed.

Run from a development checkout, never a hospital production directory:
python scripts/benchmark_runtime_offline.py --cycles 100 --output result.json
No bootstrap, real service, mail, hotkey, browser or clock action is permitted.
The deferred operational startup is stubbed; this is not launcher end-to-end.
"""
import time
PROCESS_STARTED = time.perf_counter()
import os

# Dependency checks can signal a parent's restart before application startup.
# A developer probe must never inherit a live handshake file or READY event.
for key in ('CMUH_RESTART_HANDSHAKE', 'CMUH_RESTART_READY_EVENT', 'CMUH_RESTART_PARENT_CAPS'):
    os.environ.pop(key, None)

from pathlib import Path
import argparse
import gc
import hashlib
import importlib.util
import json
import logging
import platform
import shutil
import socket
import subprocess
import sys
import statistics
import tempfile
import threading
from unittest.mock import patch
from contextlib import ExitStack

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--cycles', type=int, default=30)
parser.add_argument('--missing-icon', action='store_true')
parser.add_argument('--operations', action='store_true', help='Also time synthetic settings, UI message application and exports')
args = parser.parse_args()
if args.cycles < 1:
    parser.error('--cycles must be positive')
root_path = args.root.resolve()
sys.path.insert(0, str(root_path / 'src'))
import psutil


class ForbiddenExternalAction(BaseException):
    pass


blocked = []
thread_errors = []
callback_errors = []


def forbidden(*_args, **_kwargs):
    blocked.append('external action attempted')
    raise ForbiddenExternalAction('Offline probe forbids external actions')


process = psutil.Process()


def resources():
    from cmuh_common.resource_meter import _sample_handle
    from cmuh_common.window_icon import _owned_icons
    gui = _sample_handle(-1, want_gui=True)
    return {'rss_bytes': process.memory_info().rss,
            'handles': process.num_handles(), 'os_threads': process.num_threads(),
            'python_threads': len(threading.enumerate()),
            'child_processes': len(process.children(recursive=True)),
            'gdi': gui['gdi'] if gui else None,
            'user_objects': gui['user_objs'] if gui else None,
            'owned_icon_windows': len(_owned_icons)}


def load_fixture(name):
    spec = importlib.util.spec_from_file_location(name, root_path / 'tests' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def operation_timings(temp_dir):
    """Reuse existing anonymous fixtures; no solver run or external action."""
    from cmuh_common.config_io import load_json_dict_ex
    from cmuh_common.roster import export_xlsx, export_docx
    from cmuh_common.ui_messages import UiClinicDataMessage, put_ui_message
    import pytest
    exports = load_fixture('test_roster_export')
    refresh = load_fixture('test_outpatient_refresh_app_flow_2026_09_27')
    directory = Path(temp_dir) / 'benchmark_operations'
    directory.mkdir()
    svc = exports._svc(directory)
    settings_path = directory / 'synthetic_settings.json'
    payload = {f'field_{i}': {'enabled': True, 'threshold': i} for i in range(100)}
    settings_path.write_text(json.dumps(payload), encoding='utf-8')
    samples = {name: [] for name in ('settings_json', 'roster_config', 'roster_month',
                                   'build_export', 'xlsx', 'docx', 'pdf', 'apply_300_messages')}
    for _index in range(6):  # first sample retained as cold-in-process, then 5 warm
        for name, callback in (
                ('settings_json', lambda: load_json_dict_ex(str(settings_path))),
                ('roster_config', svc.storage.load_config),
                ('roster_month', lambda: svc.storage.load_month(exports.YM)),
                ('build_export', lambda: svc.build_export(exports.YM))):
            start = time.perf_counter()
            value = callback()
            samples[name].append(time.perf_counter() - start)
            if name == 'settings_json':
                assert value == (payload, 'ok')
        data = svc.build_export(exports.YM)
        for name, callback in (
                ('xlsx', lambda data=data: export_xlsx.export(str(directory / 'out.xlsx'), data)),
                ('docx', lambda data=data: export_docx.export(str(directory / 'out.docx'), data)),
                ('pdf', lambda: svc.archive_finalize_pdf(exports.YM))):
            start = time.perf_counter()
            callback()
            samples[name].append(time.perf_counter() - start)
        with pytest.MonkeyPatch.context() as mp:
            app, _clock = refresh._app(mp)
            app._trigger_refresh(False, [refresh._doctor('A')])
            from queue import Queue
            app.ui_queue = Queue(maxsize=1000)
            generation = app._refresh_lifecycle.generation
            for item in range(300):
                put_ui_message(app.ui_queue, UiClinicDataMessage(
                    f'N{item}', {'value': item}, refresh_gen=generation))
            start = time.perf_counter()
            app.process_ui_queue()
            app.process_ui_queue()
            samples['apply_300_messages'].append(time.perf_counter() - start)
            assert len(app.all_doctors_data) == 300 and app.ui_queue.empty()
            app._refresh_lifecycle.stop()
            app.root.scheduled.clear()
    return {'scope': '100-field synthetic JSON; existing two-resident/one-VS export fixture; 300 generation-tagged UI messages on existing fake-widget app harness. Includes real temporary file reads/writes; no p95 or hospital claim.',
            'samples_seconds': samples,
            'warm_median_seconds': {name: statistics.median(values[1:]) for name, values in samples.items()}}


result = {'scope': 'Offline application UI construction with temporary empty settings; deferred operational startup, window placement, hotkey registration and process cleanup are excluded. Not launcher end-to-end, OS cold-cache, hospital network, or clinical validation.',
          'status': 'running',
          'source_root': str(root_path), 'samples': [], 'blocked_actions': blocked,
          'thread_errors': thread_errors, 'callback_errors': callback_errors,
          'missing_icon': args.missing_icon,
          'environment': {'python': sys.version, 'platform': platform.platform(),
                          'psutil': psutil.__version__},
          'source_sha256': {p.relative_to(root_path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted((root_path / 'src').rglob('*.py'))},
          'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
with tempfile.TemporaryDirectory(prefix='cmuh_offline_perf_') as temp_dir, ExitStack() as stack:
    # Normal installed packages already ship this unchanged version-7 asset.
    if not args.missing_icon:
        assets = Path(temp_dir) / 'assets'
        assets.mkdir()
        for name in ('cmuh_app.ico', 'cmuh_icon_version.txt'):
            shutil.copyfile(root_path / 'assets' / name, assets / name)
    from cmuh_common import paths
    stack.enter_context(patch.object(paths, 'get_app_dir', lambda: temp_dir))
    stack.enter_context(patch.object(socket.socket, 'connect', forbidden))
    stack.enter_context(patch.object(socket.socket, 'connect_ex', forbidden))
    stack.enter_context(patch.object(socket, 'create_connection', forbidden))
    stack.enter_context(patch.object(socket, 'getaddrinfo', forbidden))
    import requests
    stack.enter_context(patch.object(requests.Session, 'request', forbidden))
    stack.enter_context(patch.object(subprocess, 'Popen', forbidden))
    stack.enter_context(patch.object(threading, 'excepthook', lambda e: thread_errors.append(type(e.exc_value).__name__)))
    import keyboard
    for name in ('add_hotkey', 'hook', 'press', 'send', 'write', 'unhook_all', 'unhook_all_hotkeys'):
        if hasattr(keyboard, name):
            stack.enter_context(patch.object(keyboard, name, forbidden))
    import win32gui
    for name in ('PostMessage', 'SendMessage', 'SendMessageTimeout'):
        if hasattr(win32gui, name):
            stack.enter_context(patch.object(win32gui, name, forbidden))
    start = time.perf_counter()
    import main
    result['main_import_s'] = time.perf_counter()-start
    import tkinter as tk
    for name in ('place_tk_window_on_preferred_monitor', '_pl_kill_orphan_chromedriver', 'safe_unhook_all_hotkeys'):
        stack.enter_context(patch.object(main, name, lambda *_a, **_k: None))
    stack.enter_context(patch.object(main.AutomationApp, 'deferred_initialization', lambda self: None))
    start = time.perf_counter()
    ui = tk.Tk()
    ui.withdraw()
    ui.report_callback_exception = lambda kind, _value, _tb: callback_errors.append(kind.__name__)
    result['tk_root_s'] = time.perf_counter()-start
    callbacks_before = set(ui.tk.call('after', 'info'))
    try:
        for index in range(args.cycles):
            main.stop_event_main.clear()
            main.stop_event_automation.clear()
            window = tk.Toplevel(ui)
            window.withdraw()
            start = time.perf_counter()
            app = main.AutomationApp(window, {})
            constructed = time.perf_counter()
            ui.update_idletasks()
            ui.update()
            idle = time.perf_counter()
            if index == 0:
                result['probe_process_to_first_idle_s'] = idle - PROCESS_STARTED
            assert app.notebook.winfo_exists()
            callbacks_open = len(set(ui.tk.call('after', 'info')) - callbacks_before)
            before_close = resources()
            app._cleanup_for_exit()
            app.bg_executor.shutdown(wait=True, cancel_futures=True)
            app.session.close()
            app.duty_session.close()
            callbacks_after_app_cleanup = len(set(ui.tk.call('after', 'info')) - callbacks_before)
            # Probe owns every timer in this fresh interpreter. Production
            # process exit normally destroys all of them; cancel before reopen.
            for callback in set(ui.tk.call('after', 'info')) - callbacks_before:
                # Cancel only scheduling here; the owning widget must delete
                # its Tcl command, otherwise its later destroy double-deletes.
                ui.tk.call('after', 'cancel', callback)
            window.destroy()
            del app, window
            gc.collect()
            ui.update_idletasks()
            after_close = resources()
            row = {'cycle': index, 'warmup': index < 5, 'construct_s': constructed-start,
                   'first_idle_s': idle-start, 'callbacks_open': callbacks_open,
                   'callbacks_after_app_cleanup_before_probe_cleanup': callbacks_after_app_cleanup,
                   'callbacks_after_close': len(set(ui.tk.call('after', 'info')) - callbacks_before),
                   'before_close': before_close, 'after_close': after_close}
            result['samples'].append(row)
            args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
            if index in (0, 4, args.cycles-1):
                print(json.dumps(row), flush=True)
            assert not blocked and not thread_errors and not callback_errors
        if args.operations:
            result['operations'] = operation_timings(temp_dir)
    finally:
        ui.destroy()
        for thread in threading.enumerate():
            if thread.name == 'VacuumOldCounts':
                thread.join(timeout=5)
        from cmuh_common.sqlite_cache import _close_cached_conn
        _close_cached_conn()
        # Module-level handlers only target this probe's temporary app path.
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)
            handler.close()
    assert not blocked and not thread_errors and not callback_errors
result['status'] = 'completed'
args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
