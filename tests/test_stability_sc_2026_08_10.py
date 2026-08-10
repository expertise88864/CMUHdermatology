# -*- coding: utf-8 -*-
"""[穩定性總體檢 批次SC] 兩條 Critical：卡死的持有者不可以拖垮整個子系統。

#1 autoclock：一個打卡任務整段持有全域 `clock_lock`。其中的 Selenium
   transport command 在 chromedriver wedge 時**永久不返回**（page-load /
   script timeout 管不到）。持鎖者不放 → 之後每個 schedule key、每次 gate
   逾時接管開出的 worker 全部堵在無界的 `with clock_lock` 上；而 scheduler
   照常 tick、log 照常更新 → 兩層 watchdog 都認為一切正常 →
   **打卡從此永久失效且沒有人知道**。

#2 consult：240 秒 `join()` 到期只是放棄等待，worker 沒有被取消
   （`CloseDesktop` 只有 worker 自己走到 finally 才執行）。沒有
   single-flight 的話，每次重試都再開一條 thread + 新 HDESK + 新的
   systemftp 自動化，互相衝突。
"""
import ast
import importlib
import io
import os
import sys
import threading

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

ac = importlib.import_module("autoclock")
cq = importlib.import_module("consult_query")


def _fn_src(rel, name):
    text = io.open(os.path.join(REPO_ROOT, "src", rel), encoding="utf-8").read()
    tree = ast.parse(text)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(text, n) or ""
    raise AssertionError(f"{rel} 找不到 {name}")


# ══ #1 clock_lock 有界 ════════════════════════════════════════════════════
class TestClockLockIsBounded:
    def setup_method(self):
        ac._clock_lock_timeout_streak[0] = 0

    teardown_method = setup_method

    @staticmethod
    def _hold_from_another_thread():
        """★clock_lock 是 RLock:同一條緒再取一定成功★
        要模擬「別的任務卡住不放」，持鎖者必須是【另一條】緒。
        （第一版在同一條緒 acquire → 永遠拿得到 → 測不到任何東西。）
        """
        held = threading.Event()
        release = threading.Event()

        def _holder():
            ac.clock_lock.acquire()
            held.set()
            release.wait(30)
            ac.clock_lock.release()

        t = threading.Thread(target=_holder, daemon=True)
        t.start()
        assert held.wait(5), "前置:持鎖緒沒起來"
        return release, t

    def test_a_wedged_holder_does_not_block_forever(self):
        """★核心★ 拿不到就放棄本輪，不留下永久阻塞的緒。"""
        release, t = self._hold_from_another_thread()
        try:
            with pytest.raises(ac.ClockLockBusy):
                with ac.bounded_clock_lock("am_in", timeout=0.05):
                    raise AssertionError("不該進得來")
        finally:
            release.set()
            t.join(5)

    def test_a_free_lock_is_acquired_and_released(self):
        with ac.bounded_clock_lock("am_in", timeout=1.0):
            pass
        # 釋放了才能再拿
        with ac.bounded_clock_lock("am_in", timeout=1.0):
            pass

    def test_a_success_resets_the_streak(self):
        ac._clock_lock_timeout_streak[0] = 2
        with ac.bounded_clock_lock("am_in", timeout=1.0):
            pass
        assert ac._clock_lock_timeout_streak[0] == 0, (
            "★成功一次沒有重置連續計數 → 偶發逾時累積成誤判重啟★")

    def test_repeated_timeouts_escalate_to_hard_exit(self, monkeypatch):
        """★連續逾時 = 持鎖者真的死了 → 升級重啟★
        （重啟是唯一能終結 native-wedged thread 的手段；打卡是冪等的。）"""
        exits = []
        monkeypatch.setattr(ac, "_autoclock_hard_exit",
                            lambda reason, code=1: exits.append(reason))
        release, t = self._hold_from_another_thread()
        try:
            for i in range(ac._CLOCK_LOCK_TIMEOUT_STREAK_MAX):
                with pytest.raises(ac.ClockLockBusy):
                    with ac.bounded_clock_lock("am_in", timeout=0.02):
                        pass
                if i < ac._CLOCK_LOCK_TIMEOUT_STREAK_MAX - 1:
                    assert not exits, f"第 {i + 1} 次就重啟了(太早)"
        finally:
            release.set()
            t.join(5)
        assert exits, "★連續逾時沒有升級 → 打卡永久失效且沒人知道★"

    def test_the_task_uses_the_bounded_helper(self):
        seg = _fn_src("autoclock.py", "process_clock_task")
        code = NL.join(ln.split("#")[0] for ln in seg.splitlines())
        assert "bounded_clock_lock(" in code, "★仍用無界的 with clock_lock★"
        assert "with clock_lock," not in code

    def test_the_dispatcher_swallows_the_busy_signal(self):
        """等不到不是錯誤，是「本輪略過」—— 不可以讓例外冒到緒頂端
        （pythonw 下那等於完全無聲）。"""
        seg = _fn_src("autoclock.py", "_scheduler_tick")
        assert "ClockLockBusy" in seg, "派工端沒有處理 busy 訊號"

    def test_the_wait_bound_is_generous(self):
        """要大於正常任務的 worst case（5 次重試 × 各自逾時）。"""
        assert ac._CLOCK_LOCK_WAIT_SEC >= 300


# ══ #2 隱藏桌面 worker single-flight ══════════════════════════════════════
class TestHiddenWorkerSingleFlight:
    def setup_method(self):
        cq._last_hidden_worker = None

    teardown_method = setup_method

    @staticmethod
    def _stuck_worker():
        release = threading.Event()
        t = threading.Thread(target=lambda: release.wait(30), daemon=True)
        t.start()
        cq._last_hidden_worker = t
        return release, t

    def test_a_live_previous_worker_blocks_a_new_run(self, monkeypatch):
        """★核心★ 上一條還卡著就不再疊加（否則累積 thread + HDESK +
        互相衝突的 HIS session）。"""
        release, stuck = self._stuck_worker()
        try:
            monkeypatch.setattr(cq, "load_config", lambda: {})
            monkeypatch.setattr(cq, "_ensure_hidden_desktop", lambda: 12345)
            started = []
            monkeypatch.setattr(
                cq, "_automation_on_hidden",
                lambda *a, **k: started.append(1))
            with pytest.raises(RuntimeError, match="本輪略過"):
                cq.run_consult_flow("test")
            assert not started, "★上一條還卡著卻又開了一輪自動化★"
        finally:
            release.set()
            stuck.join(5)

    def test_the_guard_runs_before_opening_a_desktop(self, monkeypatch):
        """★外審第 1 輪★ 守衛擺在 `_ensure_hidden_desktop()` 之後的話,
        每一輪 poll 照樣先開一個新的 HDESK 才拋 —— 洩漏一模一樣。"""
        release, stuck = self._stuck_worker()
        try:
            monkeypatch.setattr(cq, "load_config", lambda: {})
            opened = []
            monkeypatch.setattr(cq, "_ensure_hidden_desktop",
                                lambda: opened.append(1) or 12345)
            with pytest.raises(RuntimeError, match="本輪略過"):
                cq.run_consult_flow("test")
            assert not opened, (
                "★守衛在開 HDESK 之後 → 每輪仍洩漏一個 desktop handle★")
        finally:
            release.set()
            stuck.join(5)

    def test_the_sw_hide_fallback_is_also_guarded(self, monkeypatch):
        """★外審第 1 輪★ `_ensure_hidden_desktop()` 回 None 的後備路徑
        原本【完全繞過守衛】—— 直接與卡住的 worker 併行操作同一套
        systemftp → 互相衝突的 HIS session。"""
        release, stuck = self._stuck_worker()
        try:
            monkeypatch.setattr(cq, "load_config", lambda: {})
            monkeypatch.setattr(cq, "_ensure_hidden_desktop", lambda: None)
            ran = []
            monkeypatch.setattr(cq, "_run_with_sw_hide",
                                lambda *a, **k: ran.append(1))
            monkeypatch.setattr(cq, "_demote_schedule_to_legacy",
                                lambda: None)
            with pytest.raises(RuntimeError, match="本輪略過"):
                cq.run_consult_flow("test")
            assert not ran, (
                "★SW_HIDE 後備路徑繞過守衛 → 與卡住的 worker 併行操作 HIS★")
        finally:
            release.set()
            stuck.join(5)

    def test_a_finished_previous_worker_does_not_block(self, monkeypatch):
        """★反方向★ 上一條結束了就不可以擋住下一輪（否則永久停擺）。"""
        done = threading.Thread(target=lambda: None, daemon=True)
        done.start()
        done.join(5)
        cq._last_hidden_worker = done
        monkeypatch.setattr(cq, "load_config", lambda: {})
        monkeypatch.setattr(cq, "_ensure_hidden_desktop", lambda: 12345)
        monkeypatch.setattr(cq, "_set_thread_desktop", lambda h: True)
        monkeypatch.setattr(cq, "_automation_on_hidden",
                            lambda *a, **k: "shot.png")
        monkeypatch.setattr(cq._user32, "CloseDesktop", lambda h: 1)
        assert cq.run_consult_flow("test") == "shot.png"
        assert cq._last_hidden_worker is None, "正常結束後沒有清掉引用"

    def test_a_timed_out_worker_keeps_the_reference(self, monkeypatch):
        """逾時後【要留著引用】—— 下一輪才看得到它還活著而跳過。"""
        release = threading.Event()
        monkeypatch.setattr(cq, "load_config", lambda: {})
        monkeypatch.setattr(cq, "_ensure_hidden_desktop", lambda: 12345)
        monkeypatch.setattr(cq, "_set_thread_desktop", lambda h: True)
        monkeypatch.setattr(cq, "_automation_on_hidden",
                            lambda *a, **k: release.wait(30))
        monkeypatch.setattr(cq._user32, "CloseDesktop", lambda h: 1)
        monkeypatch.setattr(cq, "_HIDDEN_WORKER_TIMEOUT_SEC", 0.1)
        try:
            with pytest.raises(RuntimeError, match="超過 4 分鐘"):
                cq.run_consult_flow("test")
            assert cq._last_hidden_worker is not None, (
                "★逾時把引用清掉 → 下一輪照樣疊加★")
            assert cq._last_hidden_worker.is_alive()
        finally:
            release.set()

    def test_the_timeout_is_a_named_constant(self):
        """硬編碼在 join() 裡的話，測試改不動它（只能真的等 4 分鐘）。"""
        seg = _fn_src("consult_query.py", "run_consult_flow")
        assert "_HIDDEN_WORKER_TIMEOUT_SEC" in seg
        assert "t.join(timeout=240)" not in seg
