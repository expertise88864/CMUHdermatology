"""Refresh workers can be exercised without contacting the hospital HIS."""

from collections import deque
from concurrent.futures import Future
from queue import Queue
from threading import Lock
from types import SimpleNamespace

import main


class _Executor:
    def __init__(self):
        self.submitted = []

    def submit(self, callback):
        self.submitted.append(callback)
        return Future()


def test_refresh_uses_injected_fetcher_and_stamps_its_generation(monkeypatch):
    monkeypatch.setattr(
        main, "check_appointment_count",
        lambda *_args: (_ for _ in ()).throw(AssertionError("real HIS fetch invoked")),
    )
    monkeypatch.setattr(main, "_kick_off_alert_reconcile", lambda **_kwargs: None)
    fetched = []
    app = main.AutomationApp.__new__(main.AutomationApp)
    app._shutting_down = False
    app._refresh_worker_running = False
    app._refresh_worker_started_at = 0.0
    app._refresh_generation = 0
    app._refresh_queue_lock = Lock()
    app._queued_refresh_requests = deque()
    app._queued_refresh_signatures = set()
    app._active_refresh_signature = None
    app._startup_defer_full_until_priority_done = False
    app._heavy_modules_ready = True
    app._doctor_data_lock = Lock()
    app.all_doctors_data = {}
    app.ui_queue = Queue()
    app.bg_executor = _Executor()
    app.root = SimpleNamespace(after=lambda *_args: None)
    app.status_text = SimpleNamespace(set=lambda _value: None)
    app.startup_phase_text = SimpleNamespace(set=lambda _value: None)
    app.refresh_button = SimpleNamespace(config=lambda **_kwargs: None)
    app._sweep_alert_pending = lambda: None
    app._appointment_fetcher = lambda queue, config: fetched.append(
        (queue, dict(config)))

    app._trigger_refresh(
        is_manual=True,
        specific_doctors=[{"name": "SYNTHETIC", "doc_no": "D1"}],
    )
    assert len(app.bg_executor.submitted) == 1
    app.bg_executor.submitted[0]()

    assert len(fetched) == 1
    queue, config = fetched[0]
    assert queue is app.ui_queue
    assert config["doc_no"] == "D1"
    assert config["_refresh_gen"] == app._refresh_generation == 1
    assert config["_is_manual_refresh"] is True
