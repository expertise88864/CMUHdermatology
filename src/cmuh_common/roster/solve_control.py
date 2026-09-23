"""Cooperative cancellation and a wall-clock deadline for day roster solves."""

from __future__ import annotations

import threading
import time
import logging


class DaySolveStopped(RuntimeError):
    """The user cancelled a solve, or its overall deadline expired."""


class DaySolveControl:
    def __init__(self, timeout_seconds: float = 180.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.deadline = time.monotonic() + timeout_seconds
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._reason = ""
        self._stage = "等待開始"
        self._solver = None
        self._solver_done: threading.Event | None = None

    @property
    def cancelled(self) -> bool:
        return self._stopped.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def stage(self) -> str:
        with self._lock:
            return self._stage

    def cancel(self, reason: str = "已取消") -> None:
        with self._lock:
            if self._stopped.is_set():
                return
            self._reason = reason
            self._stopped.set()
            solver = self._solver
            done = self._solver_done
        if solver is not None and done is not None:
            # StopSearch issued before native Solve starts may be forgotten.
            # Keep requesting it until this phase has actually returned.
            threading.Thread(target=self._stop_until_done, args=(solver, done),
                             name="roster-day-stop", daemon=True).start()

    @staticmethod
    def _stop_until_done(solver, done: threading.Event) -> None:
        logged_failure = False
        while not done.is_set():
            try:
                solver.stop_search()
            except Exception:
                if not logged_failure:
                    logging.exception("[roster] 無法立即中止排班求解器")
                    logged_failure = True
            done.wait(0.02)

    def checkpoint(self, stage: str | None = None) -> None:
        if stage is not None:
            with self._lock:
                self._stage = stage
        if time.monotonic() >= self.deadline:
            self.cancel("排班超過時間上限")
        if self.cancelled:
            raise DaySolveStopped(self.reason)

    def solve(self, solver, model):
        """Run one CP-SAT phase, retaining the native solver for stop_search()."""
        self.checkpoint()
        done = threading.Event()
        with self._lock:
            self._solver = solver
            self._solver_done = done
            stopped = self._stopped.is_set()
        if stopped:
            with self._lock:
                self._solver = None
                self._solver_done = None
            done.set()
            raise DaySolveStopped(self.reason)
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.cancel("排班超過時間上限")
                self.checkpoint()
            current = solver.parameters.max_time_in_seconds
            solver.parameters.max_time_in_seconds = (
                min(current, remaining) if current > 0 else remaining)
            result = solver.solve(model)
        finally:
            with self._lock:
                self._solver = None
                self._solver_done = None
            done.set()
        self.checkpoint()
        return result
