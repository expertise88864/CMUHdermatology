# -*- coding: utf-8 -*-
"""W2(2026-07-03):Win32 安全逾時呼叫層。callback 阻塞(HIS 凍結)時 fail-open 回
default,不阻塞呼叫緒。"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import win32_safe  # noqa: E402


def test_returns_result_normally():
    assert win32_safe.call_with_timeout(lambda: 42, 1.0, default=0) == 42


def test_exception_returns_default():
    def boom():
        raise RuntimeError("win32 boom")
    assert win32_safe.call_with_timeout(boom, 1.0, default=-1) == -1


def test_timeout_returns_default_fast():
    """fn 卡住 → 在 timeout 內回 default,不等 fn 跑完(不阻塞呼叫緒)。"""
    def slow():
        time.sleep(5)
        return "SLOW_DONE"
    t0 = time.monotonic()
    r = win32_safe.call_with_timeout(slow, 0.1, default="TIMEOUT")
    elapsed = time.monotonic() - t0
    assert r == "TIMEOUT"
    assert elapsed < 2.0   # 沒有等滿 5 秒


# ══ [2026-08-10 批次SB #4] 放生 thread 要有上限 ═══════════════════════════
class TestStrandedThreadCap:
    """★「偶發洩一條」無上限 = 慢性自殺★

    HIS 凍結持續期間，使用者每按一次熱鍵就再洩一條（3 秒 timeout 很快
    回來，使用者以為沒按到就一直按）。堆到最後 thread 建不出來、
    熱鍵全面失效 —— 為了「不卡死」而放生的東西，累積起來又把程式弄死。
    """

    def setup_method(self):
        import cmuh_common.win32_safe as ws
        with ws._stranded_lock:
            ws._stranded.clear()

    teardown_method = setup_method

    @staticmethod
    def _wedge(release):
        def _fn():
            release.wait(30)
        return _fn

    def test_the_cap_stops_new_threads(self):
        import threading

        import cmuh_common.win32_safe as ws
        release = threading.Event()
        try:
            for _ in range(ws.MAX_STRANDED_PER_NAME):
                assert ws.call_with_timeout(
                    self._wedge(release), timeout_sec=0.05,
                    default="D", name="cap-test") == "D"
            before = threading.active_count()
            # ★到頂之後不可以再開新 thread★
            assert ws.call_with_timeout(
                self._wedge(release), timeout_sec=0.05,
                default="D", name="cap-test") == "D"
            assert threading.active_count() <= before, (
                "★到頂了還在開新 thread —— 堆積沒有被擋住★")
        finally:
            release.set()

    def test_recovered_threads_free_the_cap(self):
        """★反方向★ HIS 恢復、卡著的 thread 收斂之後，額度要放出來。"""
        import threading
        import time as _t

        import cmuh_common.win32_safe as ws
        release = threading.Event()
        try:
            for _ in range(ws.MAX_STRANDED_PER_NAME):
                ws.call_with_timeout(self._wedge(release), timeout_sec=0.05,
                                     default=None, name="cap-free")
            release.set()                    # HIS 恢復
            for _ in range(100):
                if ws._stranded_count("cap-free") == 0:
                    break
                _t.sleep(0.02)
            got = ws.call_with_timeout(lambda: "OK", timeout_sec=2.0,
                                       default=None, name="cap-free")
            assert got == "OK", "★額度沒有隨收斂釋放 → 熱鍵永久退化★"
        finally:
            release.set()

    def test_different_names_have_separate_caps(self):
        """A 呼叫點卡滿不可以連累 B 呼叫點。"""
        import threading

        import cmuh_common.win32_safe as ws
        release = threading.Event()
        try:
            for _ in range(ws.MAX_STRANDED_PER_NAME):
                ws.call_with_timeout(self._wedge(release), timeout_sec=0.05,
                                     default=None, name="cap-a")
            got = ws.call_with_timeout(lambda: "B-OK", timeout_sec=2.0,
                                       default=None, name="cap-b")
            assert got == "B-OK"
        finally:
            release.set()

    def test_a_successful_call_is_not_counted(self):
        import cmuh_common.win32_safe as ws
        for _ in range(ws.MAX_STRANDED_PER_NAME + 2):
            assert ws.call_with_timeout(lambda: 1, timeout_sec=2.0,
                                        default=None, name="cap-ok") == 1
        assert ws._stranded_count("cap-ok") == 0


class TestCapIsRaceFree:
    """★外審 SB 第 1 輪★ 檢查與佔位要在同一個臨界區。

    第一版「鎖內數、鎖外開」：HIS 凍結時多個熱鍵回呼同時進來，每條都
    數到 <上限 → 全部開 → 上限形同虛設。
    """

    def setup_method(self):
        import cmuh_common.win32_safe as ws
        with ws._stranded_lock:
            ws._stranded.clear()

    teardown_method = setup_method

    def test_a_concurrent_burst_cannot_exceed_the_cap(self):
        import threading

        import cmuh_common.win32_safe as ws
        release = threading.Event()
        entered = []
        entered_lock = threading.Lock()

        def _wedge():
            with entered_lock:
                entered.append(1)
            release.wait(30)

        start_gate = threading.Event()
        results = []

        def _caller():
            start_gate.wait(10)
            results.append(ws.call_with_timeout(
                _wedge, timeout_sec=0.4, default="D", name="burst"))

        callers = [threading.Thread(target=_caller, daemon=True)
                   for _ in range(ws.MAX_STRANDED_PER_NAME * 3)]
        try:
            for c in callers:
                c.start()
            start_gate.set()               # ★同時衝進去★
            for c in callers:
                c.join(10)
            assert len(entered) <= ws.MAX_STRANDED_PER_NAME, (
                f"★併發爆量開出了 {len(entered)} 條 native thread"
                f"(上限 {ws.MAX_STRANDED_PER_NAME})—— 檢查與佔位沒有原子化★")
            assert all(r == "D" for r in results)
        finally:
            release.set()

    def test_an_unstarted_reservation_still_occupies_a_slot(self):
        """★佔位的 thread 還沒 start 時 is_alive()=False★
        剔除條件必須是「started 且 not alive」，否則併發窗內佔位被
        別的呼叫剔掉 → 上限又被繞過。"""
        import threading

        import cmuh_common.win32_safe as ws
        t = threading.Thread(target=lambda: None)
        assert ws._occupies_slot(t) is True, "還沒 start 的佔位被當成死的"
        t.start()
        t.join()
        assert ws._occupies_slot(t) is False, "真的結束了卻不釋放"


class TestStartFailureReleasesTheSlot:
    """★外審 SB 第 2 輪 #4★ `t.start()` 失敗(can't start new thread)是
    暫時性的;佔位的 thread 永遠不會有 ident → 被當成永久占用 ——
    四次失敗之後這個呼叫點就【永久停用】,資源恢復了也救不回來。"""

    def setup_method(self):
        import cmuh_common.win32_safe as ws
        with ws._stranded_lock:
            ws._stranded.clear()

    teardown_method = setup_method

    def test_a_failed_start_returns_default_and_frees_the_slot(self,
                                                               monkeypatch):
        import threading

        import cmuh_common.win32_safe as ws

        def _boom(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", _boom)
        for _ in range(ws.MAX_STRANDED_PER_NAME + 2):
            assert ws.call_with_timeout(lambda: 1, timeout_sec=0.2,
                                        default="D", name="sf") == "D"
        monkeypatch.undo()
        got = ws.call_with_timeout(lambda: "OK", timeout_sec=2.0,
                                   default="D", name="sf")
        assert got == "OK", (
            "★start 失敗的佔位沒釋放 → 呼叫點被永久停用★")
