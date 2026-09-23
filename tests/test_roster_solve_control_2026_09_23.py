"""Cancellation must interrupt the native phase and never become infeasibility."""

import threading
import time
from datetime import date
from types import SimpleNamespace

import pytest

from cmuh_common.roster.clinic_diversity import balance_clinics
from cmuh_common.roster.solve_control import DaySolveControl, DaySolveStopped
from cmuh_common.roster.solve_day import DaySolveInput, month_solve_day


class BlockingSolver:
    def __init__(self):
        self.parameters = SimpleNamespace(max_time_in_seconds=0.0)
        self.started = threading.Event()
        self.stopped = threading.Event()

    def solve(self, _model):
        self.started.set()
        assert self.stopped.wait(2), "native stop_search was not invoked"
        return 0

    def stop_search(self):
        self.stopped.set()


class _DelayedParameters:
    def __init__(self):
        self._seconds = 0.0
        self.about_to_start = threading.Event()
        self.release = threading.Event()

    @property
    def max_time_in_seconds(self):
        return self._seconds

    @max_time_in_seconds.setter
    def max_time_in_seconds(self, value):
        self.about_to_start.set()
        assert self.release.wait(2)
        self._seconds = value


class _NativeStartupRaceSolver:
    def __init__(self):
        self.parameters = _DelayedParameters()
        self.started = threading.Event()
        self.early_stop_seen = threading.Event()
        self.stopped = threading.Event()

    def solve(self, _model):
        self.started.set()
        assert self.stopped.wait(2), "stop_search before Solve must not be the only call"
        return 0

    def stop_search(self):
        if self.started.is_set():
            self.stopped.set()
        else:
            self.early_stop_seen.set()  # native solver has not begun this search


def test_cancel_between_registration_and_native_solve_start():
    control = DaySolveControl(10)
    solver = _NativeStartupRaceSolver()
    outcome = []

    def run():
        try:
            control.solve(solver, object())
        except DaySolveStopped as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert solver.parameters.about_to_start.wait(1)
        control.cancel("起始邊界取消")
        assert solver.early_stop_seen.wait(1)
        solver.parameters.release.set()
        worker.join(2)
        assert not worker.is_alive()
        assert solver.started.is_set() and solver.stopped.is_set()
        assert len(outcome) == 1 and "起始邊界取消" in str(outcome[0])
    finally:
        solver.parameters.release.set()
        solver.stopped.set()
        worker.join(2)


def test_cancel_interrupts_active_solver():
    control = DaySolveControl(10)
    solver = BlockingSolver()
    outcome = []

    def run():
        try:
            outcome.append(control.solve(solver, object()))
        except DaySolveStopped as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert solver.started.wait(1)
        control.cancel("使用者取消")
        worker.join(2)
        assert not worker.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], DaySolveStopped)
        assert "使用者取消" in str(outcome[0])
    finally:
        solver.stopped.set()
        worker.join(2)


def test_deadline_is_distinct_from_infeasible():
    control = DaySolveControl(0.01)
    time.sleep(0.02)
    with pytest.raises(DaySolveStopped, match="時間上限"):
        control.checkpoint()
    assert control.cancelled


def test_cancel_reaches_attendance_day_loop_before_postpasses():
    class StopOnSecondDay(DaySolveControl):
        days = 0

        def checkpoint(self, stage=None):
            if stage == "安排每日必要工作":
                self.days += 1
                if self.days == 2:
                    self.cancel("逐日排班取消")
            super().checkpoint(stage)

    inp = DaySolveInput(
        "2026-10", {date(2026, 10, n): {"上午": ["101"], "下午": ["101"]}
                    for n in (1, 2, 5)}, ["P1", "P2"])
    control = StopOnSecondDay(10)
    with pytest.raises(DaySolveStopped, match="逐日排班取消"):
        month_solve_day(inp, control=control)
    assert control.days == 2


def test_cancel_reaches_doctor_diversity_pass():
    class StopAtDiversity(DaySolveControl):
        def checkpoint(self, stage=None):
            if stage == "平均跟診醫師":
                self.cancel("醫師分配取消")
            super().checkpoint(stage)

    inp = DaySolveInput("2026-10", {date(2026, 10, 1): {
        "上午": ["101"], "下午": ["101"]}}, ["P1", "P2"])
    with pytest.raises(DaySolveStopped, match="醫師分配取消"):
        balance_clinics(inp, {}, control=StopAtDiversity(10))
