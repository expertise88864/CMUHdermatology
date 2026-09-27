"""Thread-safe ownership and handoff for outpatient background refreshes.

The coordinator knows nothing about Tk, HTTP, doctors' data, or the executor.
One active generation owns its result messages until the UI has drained them;
only then does the UI acknowledge completion and start the next queued request.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import islice
import threading
import time
from typing import Any, Callable


MAX_PARTIAL_DOCTORS = 256


def _name(doctor: Any) -> str:
    return str(doctor.get("name", "")) if isinstance(doctor, dict) else str(doctor)


@dataclass(frozen=True)
class RefreshRequest:
    manual: bool
    doctors: tuple[Any, ...] | None

    @classmethod
    def make(cls, manual: bool, doctors: Any) -> RefreshRequest:
        rows = None if doctors is None else tuple(
            dict(row) if isinstance(row, dict) else row
            for row in islice(doctors, MAX_PARTIAL_DOCTORS + 1))
        return cls(bool(manual), rows)

    @property
    def signature(self) -> tuple[str, bool, tuple[str, ...] | None]:
        if self.doctors is None:
            return ("all", self.manual, None)
        return ("partial", self.manual, tuple(sorted(
            name for row in self.doctors if (name := _name(row)))))


@dataclass(frozen=True)
class RefreshRun:
    generation: int
    request: RefreshRequest
    took_over: bool = False


@dataclass(frozen=True)
class RefreshDecision:
    kind: str  # started / queued / duplicate / merged / too_many_doctors / stopped
    queue_size: int
    run: RefreshRun | None = None


@dataclass(frozen=True)
class RefreshCompletion:
    run: RefreshRun
    rejected: bool
    next_run: RefreshRun | None


class OutpatientRefreshLifecycle:
    """Single authority for de-duplication, queueing, generations and shutdown.

    At most four queued signatures exist: full/partial times manual/automatic.
    Partial requests in the same class merge by doctor name. Completion is a
    single bounded slot, not an accumulating callback queue.
    """

    MAX_QUEUED = 4
    MAX_PARTIAL_DOCTORS = MAX_PARTIAL_DOCTORS

    def __init__(self, *, max_age_seconds: float, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._max_age = max_age_seconds
        self._generation = 0
        self._active: RefreshRun | None = None
        self._started_at = 0.0
        self._queued: deque[RefreshRequest] = deque()
        self._pending_start: RefreshRun | None = None
        self._completion: tuple[RefreshRun, bool] | None = None
        self._stopped = False

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def queue_size(self) -> int:
        with self._lock:
            return len(self._queued)

    @property
    def pending_callback_count(self) -> int:
        with self._lock:
            return int(self._completion is not None)

    @property
    def pending_completion_generation(self) -> int | None:
        with self._lock:
            return self._completion[0].generation if self._completion is not None else None

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def _claim(self, request: RefreshRequest, *, took_over: bool = False) -> RefreshRun:
        self._generation += 1
        run = RefreshRun(self._generation, request, took_over)
        self._active = run
        self._started_at = self._clock()
        self._pending_start = run
        self._completion = None
        return run

    def request(self, manual: bool = False, doctors: Any = None) -> RefreshDecision:
        incoming = RefreshRequest.make(manual, doctors)
        signature = incoming.signature
        with self._lock:
            if self._stopped:
                return RefreshDecision("stopped", 0)
            if incoming.doctors is not None and len(incoming.doctors) > self.MAX_PARTIAL_DOCTORS:
                return RefreshDecision("too_many_doctors", len(self._queued))
            stale = (self._active is not None and self._completion is None
                     and self._clock() - self._started_at > self._max_age)
            if self._active is None or stale:
                if stale:
                    remaining = deque()
                    taken_names = (None if incoming.doctors is None else {
                        _name(row) for row in incoming.doctors if _name(row)})
                    for item in self._queued:
                        if item.signature == signature:
                            continue
                        if (taken_names is not None and item.doctors is not None
                                and item.manual == incoming.manual):
                            doctors_left = tuple(
                                row for row in item.doctors if _name(row) not in taken_names)
                            if not doctors_left:
                                continue
                            item = RefreshRequest(item.manual, doctors_left)
                        remaining.append(item)
                    self._queued = remaining
                run = self._claim(incoming, took_over=stale)
                return RefreshDecision("started", len(self._queued), run)
            if (signature == self._active.request.signature and self._completion is None):
                return RefreshDecision("duplicate", len(self._queued))
            if any(item.signature == signature for item in self._queued):
                return RefreshDecision("duplicate", len(self._queued))
            if incoming.doctors is not None:
                for index, item in enumerate(self._queued):
                    if item.doctors is None or item.manual != incoming.manual:
                        continue
                    by_name = {_name(row): row for row in item.doctors if _name(row)}
                    if all(_name(row) in by_name for row in incoming.doctors):
                        return RefreshDecision("duplicate", len(self._queued))
                    for row in incoming.doctors:
                        name = _name(row)
                        if name and name not in by_name:
                            by_name[name] = row
                    if len(by_name) > self.MAX_PARTIAL_DOCTORS:
                        return RefreshDecision("too_many_doctors", len(self._queued))
                    self._queued[index] = RefreshRequest(item.manual, tuple(by_name.values()))
                    return RefreshDecision("merged", len(self._queued))
            # Four request classes bound the queue; no class can be appended twice.
            if len(self._queued) >= self.MAX_QUEUED:
                raise AssertionError("refresh request classes exceeded their bound")
            self._queued.append(incoming)
            return RefreshDecision("queued", len(self._queued))

    def take_start(self) -> RefreshRun | None:
        """UI thread takes the one claimed run and submits it to the executor."""
        with self._lock:
            run = self._pending_start
            self._pending_start = None
            if self._stopped or run != self._active:
                return None
            return run

    def owns(self, generation: int) -> bool:
        with self._lock:
            return not self._stopped and self._active is not None and generation == self._generation

    def can_dispatch_batch(self, generation: int) -> bool:
        with self._lock:
            return (not self._stopped and self._active is not None
                    and self._completion is None and generation == self._generation)

    def submit_if_current(
            self, generation: int, submit: Callable[..., Any],
            *args: Any, **kwargs: Any) -> Any | None:
        """Serialize a short local executor submission with stop/takeover."""
        with self._lock:
            if (self._stopped or self._active is None or self._completion is not None
                    or generation != self._generation):
                return None
            return submit(*args, **kwargs)

    def accepts_message(self, generation: int | None) -> bool:
        with self._lock:
            return not self._stopped and (generation is None or generation == self._generation)

    def mark_finished(self, generation: int, *, rejected: bool = False) -> bool:
        """Worker thread records completion without touching Tk or dispatching work."""
        with self._lock:
            if (self._stopped or self._active is None or generation != self._generation
                    or self._completion is not None):
                return False
            self._completion = (self._active, rejected)
            return True

    def finish_on_ui(self) -> RefreshCompletion | None:
        """UI acknowledges completion after consuming that generation's messages."""
        with self._lock:
            if self._stopped or self._completion is None:
                return None
            run, rejected = self._completion
            self._completion = None
            if self._active != run:
                return None
            self._active = None
            next_run = self._claim(self._queued.popleft()) if self._queued else None
            return RefreshCompletion(run, rejected, next_run)

    def stop(self) -> None:
        """Invalidate in-flight results and prevent any later dispatch."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._generation += 1
            self._active = None
            self._queued.clear()
            self._pending_start = None
            self._completion = None
