"""Real AutomationApp refresh flow against controllable anonymous sources."""

from concurrent.futures import Future
from queue import Queue
import threading

import main
from cmuh_common.bounded_executor import RejectedExecutionError
from cmuh_common.outpatient_refresh_lifecycle import OutpatientRefreshLifecycle
from cmuh_common.ui_messages import (
    UiClinicDataMessage, UiRefreshTickMessage, UiStatusMessage, put_ui_message,
)


def _doctor(name):
    return {"name": name, "doc_no": name}


class _Var:
    def __init__(self):
        self.value = None

    def set(self, value):
        assert threading.current_thread() is threading.main_thread()
        self.value = value


class _Button:
    def __init__(self):
        self.state = "normal"

    def config(self, *, state):
        assert threading.current_thread() is threading.main_thread()
        self.state = state


class _Root:
    def __init__(self):
        self.calls = 0
        self.scheduled = []

    def after(self, _delay, callback):
        assert threading.current_thread() is threading.main_thread(), "worker touched Tk"
        self.calls += 1
        self.scheduled.append(callback)
        return self.calls

    def after_cancel(self, _id):
        assert threading.current_thread() is threading.main_thread()


class _Executor:
    def __init__(self, *, reject=False):
        self.submitted = []
        self.reject = reject

    def submit(self, callback):
        future = Future()
        if self.reject:
            future.set_exception(RejectedExecutionError("synthetic full queue"))
        else:
            self.submitted.append((callback, future))
        return future

    def shutdown(self, **_kwargs):
        self.closed = True

    def run(self, index):
        callback, future = self.submitted[index]

        def worker():
            try:
                callback()
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(None)

        thread = threading.Thread(target=worker)
        thread.start()
        return thread


def _app(monkeypatch, *, clock=None, reject=False):
    monkeypatch.setattr(main, "_kick_off_alert_reconcile", lambda after=None: None)
    monkeypatch.setattr(main, "check_appointment_count", lambda *_a: (
        _ for _ in ()).throw(AssertionError("real appointment source invoked")))
    app = main.AutomationApp.__new__(main.AutomationApp)
    app._shutting_down = False
    now = [100.0] if clock is None else clock
    app._refresh_lifecycle = OutpatientRefreshLifecycle(
        max_age_seconds=900, clock=lambda: now[0])
    app._startup_defer_full_until_priority_done = False
    app._startup_priority_phase_b_pending = False
    app._heavy_modules_ready = True
    app._doctor_data_lock = threading.Lock()
    app._alert_state_lock = threading.Lock()
    app._live_clinic_data_keys = set()
    app.all_doctors_data = {}
    app.ui_queue = Queue(maxsize=100)
    app.log_queue = Queue()
    app._log_backlog = []
    app.root = _Root()
    app.status_text = _Var()
    app.startup_phase_text = _Var()
    app.last_refresh_text = _Var()
    app.refresh_button = _Button()
    app.bg_executor = _Executor(reject=reject)
    app._refresh_progress_total = 0
    app._refresh_progress_done = 0
    app._pending_refresh_tick_ui = None
    app._refresh_tick_after_id = None
    app._schedule_refresh = lambda: None
    app._schedule_save_cache = lambda *_a: None
    app._cancel_pending_refresh_tick_ui = lambda: None
    app._sweep_alert_pending = lambda: None
    return app, now


def _emit(queue, config, value):
    generation = config["_refresh_gen"]
    put_ui_message(queue, UiRefreshTickMessage(config["name"], refresh_gen=generation))
    put_ui_message(queue, UiClinicDataMessage(
        config["doc_no"], {"value": value}, refresh_gen=generation))


def test_duplicate_and_queued_refresh_use_one_executor_worker_at_a_time(monkeypatch):
    app, _ = _app(monkeypatch)
    seen = []
    app._appointment_fetcher = lambda queue, config: (
        seen.append((config["name"], config["_refresh_gen"])),
        _emit(queue, config, config["name"]))
    app._trigger_refresh(False, [_doctor("A")])
    app._trigger_refresh(False, [_doctor("A")])
    app._trigger_refresh(True, [_doctor("B")])
    assert len(app.bg_executor.submitted) == 1
    assert app._refresh_lifecycle.queue_size == 1
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(app.bg_executor.submitted) == 1  # worker did not dispatch next/Tk
    app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 2
    assert app.all_doctors_data["A"] == {"value": "A"}
    worker = app.bg_executor.run(1)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert app.all_doctors_data["B"] == {"value": "B"}
    assert seen == [("A", 1), ("B", 2)]
    assert app.refresh_button.state == "normal"


def test_old_worker_finishing_after_takeover_cannot_overwrite_or_clear_new_run(monkeypatch):
    app, clock = _app(monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def source(queue, config):
        if config["name"] == "OLD":
            entered.set()
            assert release.wait(5)
        _emit(queue, config, config["name"])

    app._appointment_fetcher = source
    app._trigger_refresh(False, [_doctor("OLD")])
    old_worker = app.bg_executor.run(0)
    assert entered.wait(5)
    clock[0] += 901
    app._trigger_refresh(True, [_doctor("NEW")])
    assert len(app.bg_executor.submitted) == 2
    new_worker = app.bg_executor.run(1)
    new_worker.join(timeout=5)
    app.process_ui_queue()
    assert app.all_doctors_data == {"NEW": {"value": "NEW"}}
    release.set()
    old_worker.join(timeout=5)
    assert not old_worker.is_alive()
    app.process_ui_queue()
    assert app.all_doctors_data == {"NEW": {"value": "NEW"}}
    assert app._refresh_lifecycle.generation == 2
    assert app._refresh_progress_done == 1
    assert app.refresh_button.state == "normal"


def test_takeover_after_message_check_cannot_write_old_clinic_data(monkeypatch):
    app, clock = _app(monkeypatch)
    app._trigger_refresh(False, [_doctor("OLD")])
    old_generation = app._refresh_lifecycle.generation
    put_ui_message(app.ui_queue, UiClinicDataMessage(
        "OLD", {"value": "old"}, is_live_final=True,
        refresh_gen=old_generation))

    original_accepts = app._refresh_lifecycle.accepts_message
    took_over = []

    def check_then_take_over(generation):
        accepted = original_accepts(generation)
        if generation == old_generation and accepted and not took_over:
            clock[0] += 901
            worker = threading.Thread(
                target=lambda: app._trigger_refresh(True, [_doctor("NEW")]))
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive()
            took_over.append(True)
        return accepted

    monkeypatch.setattr(app._refresh_lifecycle, "accepts_message", check_then_take_over)
    app.process_ui_queue()

    assert took_over
    assert app._refresh_lifecycle.generation == old_generation + 1
    assert "OLD" not in app.all_doctors_data
    assert "OLD" not in app._live_clinic_data_keys
    assert app.refresh_button.state == "disabled"


def test_old_progress_callback_cannot_update_new_generation_before_ui_poll(monkeypatch):
    app, clock = _app(monkeypatch)
    app._trigger_refresh(False, [_doctor("A")])
    old_generation = app._refresh_lifecycle.generation
    put_ui_message(app.ui_queue, UiRefreshTickMessage("A", refresh_gen=old_generation))
    app.process_ui_queue()
    before = app.status_text.value
    assert app._pending_refresh_tick_ui is not None

    clock[0] += 901
    caller = threading.Thread(target=lambda: app._trigger_refresh(False, [_doctor("B")]))
    caller.start()
    caller.join(timeout=5)
    assert not caller.is_alive()
    assert app._refresh_lifecycle.generation == old_generation + 1
    app._flush_refresh_tick_ui()
    assert app.status_text.value == before
    assert app._pending_refresh_tick_ui is None


def test_background_request_never_calls_tk_and_ui_poll_starts_it(monkeypatch):
    app, _ = _app(monkeypatch)
    app._appointment_fetcher = lambda queue, config: _emit(queue, config, "OK")
    thread = threading.Thread(target=lambda: app._trigger_refresh(True, [_doctor("A")]))
    thread.start()
    thread.join(timeout=5)
    assert app.root.calls == 0
    assert len(app.bg_executor.submitted) == 0
    app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 1
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert app.all_doctors_data["A"] == {"value": "OK"}


def test_background_claim_starts_despite_unrelated_ui_backlog(monkeypatch):
    app, _ = _app(monkeypatch)
    app.ui_queue = Queue(maxsize=1000)
    for index in range(300):
        app.ui_queue.put(UiStatusMessage(f"synthetic {index}"))
    thread = threading.Thread(target=lambda: app._trigger_refresh(True, [_doctor("A")]))
    thread.start()
    thread.join(timeout=5)
    assert len(app.bg_executor.submitted) == 0
    app.process_ui_queue()  # first 250 messages; backlog remains
    assert len(app.bg_executor.submitted) == 1


def test_completion_waits_until_all_batched_ui_messages_are_applied(monkeypatch):
    app, _ = _app(monkeypatch)
    app.ui_queue = Queue(maxsize=1000)

    def source(queue, config):
        for index in range(300):
            put_ui_message(queue, UiClinicDataMessage(
                f"N{index}", {"value": index}, refresh_gen=config["_refresh_gen"]))

    app._appointment_fetcher = source
    app._trigger_refresh(False, [_doctor("A")])
    app._trigger_refresh(True, [_doctor("B")])
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 1
    assert len(app.all_doctors_data) == 250
    app.process_ui_queue()
    assert len(app.all_doctors_data) == 300
    assert len(app.bg_executor.submitted) == 2


def test_completion_after_observation_does_not_drop_final_clinic_data(monkeypatch):
    app, _ = _app(monkeypatch)
    app.ui_queue = Queue(maxsize=1000)
    app._trigger_refresh(False, [_doctor("A")])
    app._trigger_refresh(True, [_doctor("B")])
    generation = app._refresh_lifecycle.generation
    for index in range(250):
        app.ui_queue.put(UiStatusMessage(f"unrelated {index}"))
    put_ui_message(app.ui_queue, UiClinicDataMessage(
        "A", {"value": "FINAL"}, is_live_final=True, refresh_gen=generation))

    lifecycle = app._refresh_lifecycle
    original = OutpatientRefreshLifecycle.pending_completion_generation.fget
    observed = [False]

    def finish_after_unfinished_read(current):
        pending = original(current)
        if current is lifecycle and not observed[0]:
            observed[0] = True
            assert pending is None
            assert current.mark_finished(generation)
            return None
        return pending

    monkeypatch.setattr(OutpatientRefreshLifecycle, "pending_completion_generation",
                        property(finish_after_unfinished_read))
    app.process_ui_queue()
    assert app._refresh_lifecycle.generation == generation
    assert "A" not in app.all_doctors_data

    app.process_ui_queue()
    assert app.all_doctors_data["A"] == {"value": "FINAL"}
    assert "A" in app._live_clinic_data_keys
    assert app._refresh_lifecycle.generation == generation + 1


def test_unrelated_ui_backlog_does_not_starve_queued_refresh(monkeypatch):
    app, _ = _app(monkeypatch)
    app.ui_queue = Queue(maxsize=1000)
    app._appointment_fetcher = lambda queue, config: _emit(queue, config, "OK")
    app._trigger_refresh(False, [_doctor("A")])
    app._trigger_refresh(True, [_doctor("B")])
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    for index in range(500):
        app.ui_queue.put(UiStatusMessage(f"unrelated {index}"))
    app.process_ui_queue()
    assert not app.ui_queue.empty()
    assert app.all_doctors_data["A"] == {"value": "OK"}
    assert len(app.bg_executor.submitted) == 2


def test_network_failure_and_executor_rejection_release_ui(monkeypatch):
    app, _ = _app(monkeypatch)
    app._appointment_fetcher = lambda *_a: (_ for _ in ()).throw(OSError("fake network"))
    app._trigger_refresh(True, [_doctor("A")])
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert app.refresh_button.state == "normal"
    assert app._refresh_lifecycle.pending_callback_count == 0

    rejected, _ = _app(monkeypatch, reject=True)
    rejected._trigger_refresh(True, [_doctor("B")])
    assert rejected.refresh_button.state == "disabled"
    rejected.process_ui_queue()
    assert rejected.refresh_button.state == "normal"
    assert "未啟動" in rejected.status_text.value
    assert rejected._refresh_lifecycle.pending_callback_count == 0


def test_stop_blocks_later_dispatch_even_when_old_worker_finishes(monkeypatch):
    app, _ = _app(monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def source(queue, config):
        entered.set()
        assert release.wait(5)
        _emit(queue, config, "OLD")

    app._appointment_fetcher = source
    app._trigger_refresh(False, [_doctor("OLD")])
    old_worker = app.bg_executor.run(0)
    assert entered.wait(5)
    app._trigger_refresh(True, [_doctor("QUEUED")])
    app._shutting_down = True
    app._refresh_lifecycle.stop()
    release.set()
    old_worker.join(timeout=5)
    app._trigger_refresh(False, [_doctor("LATE")])
    app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 1
    assert app._refresh_lifecycle.queue_size == 0
    assert app._refresh_lifecycle.pending_callback_count == 0
    assert app.all_doctors_data == {}


def test_takeover_escalates_through_existing_idle_restart_gate_only_once(monkeypatch):
    app, clock = _app(monkeypatch)
    monkeypatch.setitem(main._reg52_slot_state, "exhausted", True)
    restart_gate = lambda: None
    app._restart_when_hotkey_idle = restart_gate
    app._trigger_refresh(False, [_doctor("A")])
    clock[0] += 901
    app._trigger_refresh(True, [_doctor("B")])
    assert app.root.scheduled.count(restart_gate) == 1
    assert app._reg52_restart_requested
    clock[0] += 901
    app._trigger_refresh(False, [_doctor("C")])
    assert app.root.scheduled.count(restart_gate) == 1
    assert len(app.bg_executor.submitted) == 3


def test_priority_startup_refresh_chains_a_full_run_on_ui_without_extra_hop(monkeypatch):
    app, _ = _app(monkeypatch)
    monkeypatch.setattr(main, "DOCTORS", [_doctor("A")])
    app._startup_defer_full_until_priority_done = True
    app._appointment_fetcher = lambda queue, config: _emit(
        queue, config, config["_refresh_gen"])
    app._trigger_refresh(False, [_doctor("A")])
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 2
    assert app._startup_defer_full_until_priority_done is False
    worker = app.bg_executor.run(1)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert app.all_doctors_data["A"] == {"value": 2}
    assert app.refresh_button.state == "normal"


def test_queued_full_run_consumes_startup_defer_without_second_full(monkeypatch):
    app, _ = _app(monkeypatch)
    monkeypatch.setattr(main, "DOCTORS", [_doctor("A"), _doctor("B")])
    app._startup_defer_full_until_priority_done = True
    app._appointment_fetcher = lambda queue, config: _emit(
        queue, config, config["_refresh_gen"])
    app._trigger_refresh(False, [_doctor("A")])
    app._trigger_refresh(False, [_doctor("B")])
    app._trigger_refresh(False)
    assert len(app.bg_executor.submitted) == 1

    for index in range(3):
        worker = app.bg_executor.run(index)
        worker.join(timeout=5)
        app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 3
    assert app._startup_defer_full_until_priority_done is False

    app._trigger_refresh(False, [_doctor("A")])
    worker = app.bg_executor.run(3)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 4
    assert app._refresh_lifecycle.queue_size == 0


def test_startup_waits_for_delayed_phase_b_before_full(monkeypatch):
    app, _ = _app(monkeypatch)
    monkeypatch.setattr(main, "DOCTORS", [_doctor("A"), _doctor("B")])
    app._startup_defer_full_until_priority_done = True
    app._startup_priority_phase_b_pending = True
    app._appointment_fetcher = lambda queue, config: _emit(
        queue, config, config["_refresh_gen"])
    app._trigger_refresh(False, [_doctor("A")])
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 1
    assert app._startup_defer_full_until_priority_done

    app._startup_priority_phase_b_pending = False
    app._trigger_refresh(False, [_doctor("B")])
    worker = app.bg_executor.run(1)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 3
    assert app._startup_defer_full_until_priority_done is False


def test_rejected_delayed_phase_b_still_attempts_full_refresh(monkeypatch):
    app, _ = _app(monkeypatch)
    monkeypatch.setattr(main, "DOCTORS", [_doctor("A"), _doctor("B")])
    app._startup_defer_full_until_priority_done = True
    app._startup_priority_phase_b_pending = True
    app._appointment_fetcher = lambda queue, config: _emit(
        queue, config, config["_refresh_gen"])
    app._trigger_refresh(False, [_doctor("A")])
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 1

    app._startup_priority_phase_b_pending = False
    app.bg_executor.reject = True
    app._trigger_refresh(False, [_doctor("B")])
    app.bg_executor.reject = False
    app.process_ui_queue()
    assert len(app.bg_executor.submitted) == 2
    assert app._startup_defer_full_until_priority_done is False
    worker = app.bg_executor.run(1)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert app.refresh_button.state == "normal"


def test_stop_between_claim_and_outer_submit_prevents_dispatch(monkeypatch):
    app, _ = _app(monkeypatch)
    original_set = app.status_text.set

    def stop_while_preparing(value):
        app._refresh_lifecycle.stop()
        original_set(value)

    app.status_text.set = stop_while_preparing
    app._trigger_refresh(False, [_doctor("A")])
    assert app.bg_executor.submitted == []
    assert app._refresh_lifecycle.stopped


def test_close_between_worker_check_and_alert_kickoff_starts_no_alert(monkeypatch):
    app, _ = _app(monkeypatch)
    started = []
    monkeypatch.setattr(main, "_kick_off_alert_reconcile", lambda after=None: started.append(after))
    original_check = app._refresh_lifecycle.can_dispatch_batch

    def close_after_first_check(generation):
        permitted = original_check(generation)
        if permitted:
            app._refresh_lifecycle.stop()
        return permitted

    app._refresh_lifecycle.can_dispatch_batch = close_after_first_check
    app._appointment_fetcher = lambda *_a: (
        _ for _ in ()).throw(AssertionError("doctor fetch after close"))
    app._trigger_refresh(False, [_doctor("A")])
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert started == []


def test_alert_reconcile_finishing_after_close_skips_app_callback(monkeypatch):
    app, _ = _app(monkeypatch)
    after_callbacks = []
    swept = []
    monkeypatch.setattr(main, "_kick_off_alert_reconcile", lambda after=None: after_callbacks.append(after))
    app._sweep_alert_pending = lambda: swept.append(True)
    app._appointment_fetcher = lambda queue, config: _emit(queue, config, "OK")
    app._trigger_refresh(False, [_doctor("A")])
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    assert len(after_callbacks) == 1
    app._refresh_lifecycle.stop()
    after_callbacks[0]()
    assert swept == []


def test_alert_cleanup_runs_after_owning_refresh_has_finished(monkeypatch):
    app, _ = _app(monkeypatch)
    after_callbacks = []
    swept = []
    monkeypatch.setattr(main, "_kick_off_alert_reconcile", lambda after=None: after_callbacks.append(after))
    app._sweep_alert_pending = lambda: swept.append(True)
    app._appointment_fetcher = lambda queue, config: _emit(queue, config, "OK")
    app._trigger_refresh(False, [_doctor("A")])
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert len(after_callbacks) == 1
    assert not app._refresh_lifecycle.owns(1)
    after_callbacks[0]()
    assert swept == [True]


def test_alert_cleanup_from_taken_over_run_still_sweeps_global_pending_state(monkeypatch):
    app, clock = _app(monkeypatch)
    after_callbacks = []
    swept = []
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(main, "_kick_off_alert_reconcile", lambda after=None: after_callbacks.append(after))
    app._sweep_alert_pending = lambda: swept.append(True)

    def source(_queue, config):
        if config["name"] == "OLD":
            entered.set()
            assert release.wait(5)

    app._appointment_fetcher = source
    app._trigger_refresh(False, [_doctor("OLD")])
    old_worker = app.bg_executor.run(0)
    assert entered.wait(5)
    clock[0] += 901
    app._trigger_refresh(False, [_doctor("NEW")])
    assert app._refresh_lifecycle.generation == 2
    after_callbacks[0]()
    assert swept == [True]
    release.set()
    old_worker.join(timeout=5)
    assert not old_worker.is_alive()


def test_claimed_setup_failure_releases_ownership(monkeypatch):
    app, _ = _app(monkeypatch)
    original_set = app.status_text.set
    app.status_text.set = lambda _value: (
        _ for _ in ()).throw(RuntimeError("synthetic Tk setup failure"))
    app._trigger_refresh(False, [_doctor("A")])
    assert app.bg_executor.submitted == []
    assert app._refresh_lifecycle.pending_callback_count == 1
    app.status_text.set = original_set
    app.process_ui_queue()
    assert app._refresh_lifecycle.pending_callback_count == 0
    assert app.refresh_button.state == "normal"


def test_drain_exception_does_not_stop_ui_polling(monkeypatch):
    app, _ = _app(monkeypatch)
    app._drain_refresh_lifecycle_ui = lambda: (
        _ for _ in ()).throw(RuntimeError("synthetic drain failure"))
    app.process_ui_queue()
    assert app.root.scheduled[-1] == app.process_ui_queue


def test_uncopyable_full_snapshot_does_not_leave_refresh_button_disabled(monkeypatch):
    app, _ = _app(monkeypatch)
    monkeypatch.setattr(main, "DOCTORS", [_doctor("A")])
    app.all_doctors_data = {"uncopyable": threading.Lock()}
    app._appointment_fetcher = lambda _queue, _config: None
    app._trigger_refresh(False)
    worker = app.bg_executor.run(0)
    worker.join(timeout=5)
    app.process_ui_queue()
    assert app.refresh_button.state == "normal"
    assert app._refresh_lifecycle.pending_callback_count == 0


def test_window_close_cleanup_stops_refresh_before_executor_shutdown(monkeypatch):
    app, _ = _app(monkeypatch)
    app._exit_cleanup_done = False
    app._save_floating_clinic_settings = lambda: None
    app._close_floating_clinic = lambda: None
    app._ui_queue_poll_id = None
    app.clinic_loop_id = None
    monkeypatch.setattr(main, "stop_event_main", threading.Event())
    monkeypatch.setattr(main, "stop_event_automation", threading.Event())
    monkeypatch.setattr(main, "safe_unhook_all_hotkeys", lambda *_a: None)
    monkeypatch.setattr(main, "_pl_kill_orphan_chromedriver", lambda: None)
    monkeypatch.setattr(main, "_status_driver_pool", {
        "lock": threading.Lock(), "driver": None, "last_used": 0.0, "epoch": 0})
    app._trigger_refresh(False, [_doctor("A")])
    app._trigger_refresh(True, [_doctor("B")])
    app._cleanup_for_exit()
    assert app._shutting_down and app._refresh_lifecycle.stopped
    assert app._refresh_lifecycle.queue_size == 0
    assert app.bg_executor.closed
    app._trigger_refresh(False, [_doctor("C")])
    assert len(app.bg_executor.submitted) == 1
