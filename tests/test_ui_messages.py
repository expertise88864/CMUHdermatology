# -*- coding: utf-8 -*-
import os
import sys
from queue import Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.ui_messages import UiStatusMessage, put_ui_message  # noqa: E402


def test_put_ui_message_drops_oldest_when_queue_is_full():
    q = Queue(maxsize=1)

    put_ui_message(q, UiStatusMessage("old"))
    put_ui_message(q, UiStatusMessage("new"))

    assert q.qsize() == 1
    assert q.get_nowait() == UiStatusMessage("new")


def test_put_ui_message_does_not_block_unbounded_queue():
    q = Queue()

    put_ui_message(q, UiStatusMessage("one"))
    put_ui_message(q, UiStatusMessage("two"))

    assert q.get_nowait() == UiStatusMessage("one")
    assert q.get_nowait() == UiStatusMessage("two")


def test_put_ui_message_does_not_treat_unexpected_put_error_as_full():
    class BrokenQueue:
        get_called = False

        def put_nowait(self, _msg):
            raise RuntimeError("broken")

        def get_nowait(self):
            self.get_called = True
            raise AssertionError("should not drop when put failed unexpectedly")

    q = BrokenQueue()

    put_ui_message(q, UiStatusMessage("msg"))

    assert not q.get_called
