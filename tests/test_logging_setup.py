# -*- coding: utf-8 -*-
import logging
from queue import Queue

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.logging_setup import QueueHandler  # noqa: E402


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        "test", logging.INFO, __file__, 1, message, (), None,
    )


def test_queue_handler_drops_oldest_when_queue_is_full():
    q = Queue(maxsize=1)
    handler = QueueHandler(q)

    first = _record("first")
    second = _record("second")

    handler.emit(first)
    handler.emit(second)

    assert q.qsize() == 1
    assert q.get_nowait().msg == "second"


def test_queue_handler_does_not_block_unbounded_queue():
    q = Queue()
    handler = QueueHandler(q)

    handler.emit(_record("one"))
    handler.emit(_record("two"))

    assert [q.get_nowait().msg, q.get_nowait().msg] == ["one", "two"]
