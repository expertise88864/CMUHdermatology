# -*- coding: utf-8 -*-
"""ThreadPoolExecutor variant with a bounded pending-task budget."""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable


class RejectedExecutionError(RuntimeError):
    """Raised on returned futures when the executor backlog is saturated."""


class BoundedThreadPoolExecutor(ThreadPoolExecutor):
    """Drop new submissions once running + queued work reaches ``max_pending``.

    Standard ThreadPoolExecutor uses an unbounded internal queue. In a GUI app
    with periodic background jobs, a hung network or WebDriver call can otherwise
    accumulate work indefinitely and make the process sluggish over time.
    """

    def __init__(
        self,
        *args: Any,
        max_pending: int | None = None,
        reject_message: str = "background executor backlog is full",
        **kwargs: Any,
    ) -> None:
        max_workers = kwargs.get("max_workers")
        if args:
            max_workers = args[0]
        if max_workers is None:
            max_workers = min(32, (os.cpu_count() or 1) + 4)
        if max_pending is None:
            max_pending = int(max_workers) * 4
        max_pending = max(1, int(max_pending), int(max_workers))

        super().__init__(*args, **kwargs)
        self._pending_slots = threading.BoundedSemaphore(max_pending)
        self._max_pending = max_pending
        self._reject_message = reject_message

    @property
    def max_pending(self) -> int:
        return self._max_pending

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        if not self._pending_slots.acquire(blocking=False):
            future: Future = Future()
            future.set_exception(RejectedExecutionError(self._reject_message))
            logging.warning("%s; dropping task %s", self._reject_message, _callable_name(fn))
            return future

        task_name = _callable_name(fn)
        try:
            future = super().submit(_run_with_exception_logging, task_name, fn, args, kwargs)
        except Exception:
            self._pending_slots.release()
            raise

        future.add_done_callback(lambda _future: self._pending_slots.release())
        return future


def _callable_name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))


def _run_with_exception_logging(
    task_name: str,
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception:
        logging.exception("background task failed: %s", task_name)
        raise
