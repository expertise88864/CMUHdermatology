# -*- coding: utf-8 -*-
import io
import logging
from queue import Queue

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.logging_setup import (  # noqa: E402
    QueueHandler,
    attach_queue_handler,
    attach_stream_handler,
)


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


def test_attach_queue_handler_reuses_handler_for_same_queue():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    for handler in original_handlers:
        root.removeHandler(handler)

    try:
        q = Queue()
        first = attach_queue_handler(q, level=logging.WARNING)
        second = attach_queue_handler(q, level=logging.ERROR)

        queue_handlers = [h for h in root.handlers if isinstance(h, QueueHandler)]
        assert second is first
        assert first.level == logging.ERROR
        assert queue_handlers == [first]
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)


def test_attach_queue_handler_keeps_different_queues_separate():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    for handler in original_handlers:
        root.removeHandler(handler)

    try:
        first = attach_queue_handler(Queue())
        second = attach_queue_handler(Queue())

        queue_handlers = [h for h in root.handlers if isinstance(h, QueueHandler)]
        assert first is not second
        assert queue_handlers == [first, second]
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)


def test_attach_queue_handler_can_replace_stale_queue_handlers():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    for handler in original_handlers:
        root.removeHandler(handler)

    try:
        stale = attach_queue_handler(Queue())
        current = attach_queue_handler(Queue(), replace_existing=True)

        queue_handlers = [h for h in root.handlers if isinstance(h, QueueHandler)]
        assert stale is not current
        assert queue_handlers == [current]
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)


def test_attach_stream_handler_reuses_handler_for_same_stream():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    for handler in original_handlers:
        root.removeHandler(handler)

    try:
        stream = io.StringIO()
        first = attach_stream_handler(stream=stream, level=logging.WARNING)
        second = attach_stream_handler(stream=stream, level=logging.ERROR)

        stream_handlers = [
            h for h in root.handlers
            if type(h) is logging.StreamHandler
        ]
        assert second is first
        assert first.level == logging.ERROR
        assert stream_handlers == [first]
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)


def test_attach_stream_handler_can_replace_stale_stream_handlers():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    for handler in original_handlers:
        root.removeHandler(handler)

    try:
        stale = attach_stream_handler(stream=io.StringIO())
        current = attach_stream_handler(
            stream=io.StringIO(),
            replace_existing=True,
        )

        stream_handlers = [
            h for h in root.handlers
            if type(h) is logging.StreamHandler
        ]
        assert stale is not current
        assert stream_handlers == [current]
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)
