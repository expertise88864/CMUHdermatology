# -*- coding: utf-8 -*-
"""[R3-P2-05] UI 佇列滿了要丟哪一筆,看的是臨床重要性,不是誰最舊。

原本一律丟最舊的。於是佇列被一串定時 tick 塞滿時,★被犧牲的可能正是那一則
錯誤通知★ —— 而 tick 下一輪就會再來,錯誤通知丟了就永遠不見
(那是使用者唯一會知道「出事了」的管道)。
"""
import os
import sys
from queue import Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.ui_messages import (  # noqa: E402
    UiAlertErrorMessage, UiAlertInfoMessage, UiClockStatusMessage,
    UiRefreshTickMessage, UiStatusMessage, is_expendable_ui_message,
    put_ui_message,
)


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _err(msg="boom"):
    return UiAlertErrorMessage(title="t", msg=msg)


class TestWhatCountsAsExpendable:
    def test_periodic_ones_are(self):
        assert is_expendable_ui_message(UiRefreshTickMessage(doctor_name="D"))
        assert is_expendable_ui_message(UiStatusMessage(text="x"))

    def test_one_shot_ones_are_not(self):
        """★丟了就永遠不見的不可以是可丟的★。"""
        assert not is_expendable_ui_message(_err())
        assert not is_expendable_ui_message(
            UiAlertInfoMessage(title="t", msg="m", need_restart=False))
        assert not is_expendable_ui_message(
            UiClockStatusMessage(status_data="querying", generation=1))


class TestWhoGetsDropped:
    def test_an_error_evicts_a_tick(self):
        """★核心★:佇列被 tick 塞滿時,錯誤通知要擠得進來。"""
        q = Queue(maxsize=3)
        for _ in range(3):
            q.put_nowait(UiRefreshTickMessage(doctor_name="D"))
        put_ui_message(q, _err())
        got = _drain(q)
        assert any(isinstance(m, UiAlertErrorMessage) for m in got), \
            f"★錯誤通知被擋在門外★:{[type(m).__name__ for m in got]}"

    def test_it_drops_the_oldest_expendable_not_the_newest(self):
        """可丟的裡面要丟最舊的(新的比較接近現況)。"""
        q = Queue(maxsize=3)
        q.put_nowait(UiStatusMessage(text="舊"))
        q.put_nowait(UiStatusMessage(text="新"))
        q.put_nowait(_err("先來的錯誤"))
        put_ui_message(q, _err("後來的錯誤"))
        texts = [getattr(m, "text", None) for m in _drain(q)]
        assert "舊" not in texts and "新" in texts, texts

    def test_a_periodic_message_yields_when_everything_else_matters(self):
        """★整個佇列都是一次性訊息時,新來的週期性訊息讓路★:它下一輪還會來,
        不可以拿它換掉一則使用者只會看到一次的錯誤通知。
        (★注意不是「一律讓路」★:週期性訊息越新越對 —— 狀態列就是這樣,
         所以佇列裡有可丟的時,還是要擠掉最舊那一筆讓新的進來。)"""
        q = Queue(maxsize=2)
        q.put_nowait(_err("a"))
        q.put_nowait(_err("b"))
        put_ui_message(q, UiRefreshTickMessage(doctor_name="D"))
        got = _drain(q)
        assert [m.msg for m in got] == ["a", "b"], \
            "★週期性訊息把一次性訊息擠掉了★"

    def test_all_important_falls_back_to_dropping_the_oldest(self):
        """★保底★:全部都重要時仍然要放得進去 —— 不可以讓背景執行緒卡死。"""
        q = Queue(maxsize=2)
        q.put_nowait(_err("a"))
        q.put_nowait(_err("b"))
        put_ui_message(q, _err("c"))
        assert [m.msg for m in _drain(q)] == ["b", "c"]

    def test_a_newer_periodic_message_replaces_an_older_one(self):
        """★週期性訊息越新越對★:狀態列滿了的時候,要留下最新那一筆
        (既有測試 `test_put_ui_message_drops_oldest_when_queue_is_full`
         釘的就是這個 —— 我第一版把新的丟掉,把它弄紅了)。"""
        q = Queue(maxsize=1)
        put_ui_message(q, UiStatusMessage(text="舊"))
        put_ui_message(q, UiStatusMessage(text="新"))
        assert [m.text for m in _drain(q)] == ["新"]

    def test_the_fallback_is_atomic_too(self, monkeypatch):
        """★保底那條路也要在同一個臨界區裡★(外審 R2 P2):
        拆成鎖外的 `get_nowait()` + `put_nowait()` 的話,中間會被別的
        producer 補位 —— 騰出來的空位被搶走,自己的 put 又拿到 Full 而被
        靜默放棄,重要訊息照樣不見。

        判準:把 `get_nowait` 換成會炸的替身 —— 標準 Queue 的這條路
        ★根本不該再呼叫它★。
        (★驗收也不可以用 `_drain`★:它自己就是靠 `get_nowait` 撈的,
         會反過來踩到這個替身 —— 第一版就是這樣紅的。直接看佇列內容。)
        """
        q = Queue(maxsize=2)
        q.put_nowait(_err("a"))
        q.put_nowait(_err("b"))
        called = []
        monkeypatch.setattr(
            q, "get_nowait",
            lambda: called.append(1) or _err("x"))
        put_ui_message(q, _err("c"))
        assert not called, "★保底走了鎖外的 get_nowait★"
        assert [m.msg for m in list(q.queue)] == ["b", "c"]

    def test_a_non_full_queue_is_untouched(self):
        """★對照組★:沒滿的時候什麼都不要動。"""
        q = Queue(maxsize=5)
        q.put_nowait(UiRefreshTickMessage(doctor_name="D"))
        put_ui_message(q, _err("x"))
        got = _drain(q)
        assert len(got) == 2 and isinstance(got[0], UiRefreshTickMessage)

    def test_order_is_preserved_after_an_eviction(self):
        """拿掉中間一筆之後,其餘的順序不可以被打亂。"""
        q = Queue(maxsize=4)
        q.put_nowait(_err("1"))
        q.put_nowait(UiStatusMessage(text="tick"))
        q.put_nowait(_err("2"))
        q.put_nowait(_err("3"))
        put_ui_message(q, _err("4"))
        assert [getattr(m, "msg", getattr(m, "text", None))
                for m in _drain(q)] == ["1", "2", "3", "4"]


def test_a_queue_without_a_mutex_still_works():
    """★不是標準 Queue 也不可以炸★(退回原本的「丟最舊」)。"""
    class _Q:
        def __init__(self):
            self.items = []

        def put_nowait(self, m):
            from queue import Full
            if len(self.items) >= 2:
                raise Full
            self.items.append(m)

        def get_nowait(self):
            from queue import Empty
            if not self.items:
                raise Empty
            return self.items.pop(0)
    q = _Q()
    q.put_nowait(_err("a"))
    q.put_nowait(_err("b"))
    put_ui_message(q, _err("c"))
    assert [m.msg for m in q.items] == ["b", "c"]
