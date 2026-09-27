"""An older HIS refresh cannot overwrite data delivered by the current run."""

from queue import Queue
from threading import Lock
from types import SimpleNamespace

import main
from cmuh_common.outpatient_refresh_lifecycle import OutpatientRefreshLifecycle
from cmuh_common.ui_messages import UiClinicDataMessage


def test_old_clinic_result_is_ignored_after_a_newer_refresh():
    app = main.AutomationApp.__new__(main.AutomationApp)
    app._shutting_down = False
    clock = [0.0]
    app._refresh_lifecycle = OutpatientRefreshLifecycle(
        max_age_seconds=900, clock=lambda: clock[0])
    app._refresh_lifecycle.request(False)
    app._refresh_lifecycle.take_start()
    clock[0] = 901.0
    app._refresh_lifecycle.request(True)
    app._refresh_lifecycle.take_start()
    app._doctor_data_lock = Lock()
    app._alert_state_lock = Lock()
    app._live_clinic_data_keys = set()
    app.all_doctors_data = {"SYNTHETIC": {"current": 2}}
    app.ui_queue = Queue()
    app.log_queue = Queue()
    app._log_backlog = []
    app.root = SimpleNamespace(after=lambda *_args: None)
    app._schedule_refresh = lambda: None
    app._schedule_save_cache = lambda *_args: None
    app.ui_queue.put(UiClinicDataMessage(
        doctor_name="SYNTHETIC", data={"old": 1}, refresh_gen=1))

    app.process_ui_queue()

    assert app.all_doctors_data == {"SYNTHETIC": {"current": 2}}
