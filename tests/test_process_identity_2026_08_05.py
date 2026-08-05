# -*- coding: utf-8 -*-
"""PID 會被回收（外審第 5 輪 P1-05 / P2-06）＋ 基準遺失告警要節流（自查 P1-B）。

【P1-05 / P2-06 是同一件事的兩面】
Windows 會把結束行程的 PID 發給新的行程。只比對 PID —— 甚至「PID + 執行檔名稱」
—— 都擋不住「同一個 PID 換了一個行程」，而在這台機器上那個新行程很可能就是
**醫師自己開的同一支住院系統**。

  * P1-05：session 的主畫面身分（hwnd + pid + class）全對得上，底下的行程仍可能換人
  * P2-06：`_validated_systemftp_pids` 驗完之後，WM_CLOSE 與 terminate 都是
    【之後】才發生的；中間 PID 被回收就會關到別人的程式
    （舊版只在 terminate 前補驗**名稱**，而回收給同一支程式時名稱一樣）

【★這個訊號必須是選用的★】
讀不到建立時間（權限／psutil 不可用）時，一律**不採用這個訊號**，不是判定
「不一樣」。把「讀不到」當成「不是同一個」會讓每一輪都重登 —— 那正是
2026-08-04 才修掉的實機故障的形狀，也是 2026-08-05 事故的共同病灶：
**一個無法成功的檢查被接上破壞性動作**。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


class _Sess:
    def __init__(self, started=1000.0):
        self.pid = 111
        self.our_pids = {111}
        self.main_hwnd = 555
        self.main_pid = 222
        self.main_class = cq.MAIN_CLASS
        self.main_proc_started = started


class TestARecycledPidIsNotTheSameSession:

    def _identity_ok(self, monkeypatch):
        monkeypatch.setattr(cq, "_window_identity",
                            lambda _h: (222, cq.MAIN_CLASS))

    def test_the_same_process_is_the_same_session(self, monkeypatch):
        self._identity_ok(monkeypatch)
        monkeypatch.setattr(cq, "_process_started_at", lambda _p: 1000.0)
        assert cq._is_same_window(_Sess()) is True

    def test_a_recycled_pid_is_a_different_session(self, monkeypatch):
        """★核心★ hwnd/pid/class 全對得上，但底下的行程換人了。"""
        self._identity_ok(monkeypatch)
        monkeypatch.setattr(cq, "_process_started_at", lambda _p: 9999.0)
        assert cq._is_same_window(_Sess()) is False, (
            "★同一個 PID 換了行程仍被當成我們的 session★ 可能對醫師的 HIS 動手")

    def test_an_unreadable_start_time_does_not_mean_different(self, monkeypatch):
        """★安全方向★ 讀不到 ≠ 不一樣。

        把「讀不到」當成「不是同一個」→ 每一輪都判死 → 每 3 分鐘重送帳密，
        那正是 2026-08-04 才修掉的實機故障。
        """
        self._identity_ok(monkeypatch)
        monkeypatch.setattr(cq, "_process_started_at", lambda _p: None)
        assert cq._is_same_window(_Sess()) is True

    def test_a_session_without_a_recorded_start_time_still_works(self,
                                                                monkeypatch):
        """當初就沒記到（舊 session／權限不足）→ 不採用這個訊號。"""
        self._identity_ok(monkeypatch)
        monkeypatch.setattr(cq, "_process_started_at", lambda _p: 4242.0)
        assert cq._is_same_window(_Sess(started=None)) is True

    def test_small_clock_jitter_is_tolerated(self, monkeypatch):
        """建立時間的精度在不同來源會有毫秒級差異，不可因此判成換人。"""
        self._identity_ok(monkeypatch)
        monkeypatch.setattr(cq, "_process_started_at", lambda _p: 1000.4)
        assert cq._is_same_window(_Sess()) is True

    def test_the_session_records_it_at_login(self, monkeypatch):
        monkeypatch.setattr(cq, "_window_identity",
                            lambda _h: (777, cq.MAIN_CLASS))
        monkeypatch.setattr(cq, "_process_started_at",
                            lambda p: 2222.0 if p == 777 else None)
        s = cq._PersistentSession(None, None, 123, {123}, main_hwnd=555)
        assert s.main_proc_started == 2222.0


class TestProcessStartedAt:

    def test_no_pid_returns_none(self):
        assert cq._process_started_at(None) is None
        assert cq._process_started_at(0) is None

    def test_a_lookup_failure_returns_none_not_a_made_up_value(self,
                                                              monkeypatch):
        """★查不到就是查不到★ 不可以編一個值出來。

        突變驗證抓到的洞：`except: return 0.0`。上面那支只走到「pid 是空的」
        那條 early return，根本沒有進到例外處理 —— 編出來的 0.0 會讓
        「兩邊都讀不到」變成假的「時間一樣」＝假的「同一個行程」。
        """
        class _Boom:
            @staticmethod
            def Process(_pid):
                raise OSError("拿不到")
        monkeypatch.setattr(cq, "psutil", _Boom)
        assert cq._process_started_at(12345) is None

    def test_it_reads_a_real_process(self):
        """自己這個行程一定讀得到 —— 否則整個機制在這台機器上等於沒有。"""
        assert isinstance(cq._process_started_at(os.getpid()), float)


class TestCloseDoesNotActOnARecycledPid:
    """★P2-06★ 驗證與動手之間 PID 被回收 → 不可以動它。"""

    def _prep(self, monkeypatch, started_seq):
        monkeypatch.setattr(cq, "_validated_systemftp_pids", lambda p: {321})
        seq = list(started_seq)
        monkeypatch.setattr(cq, "_process_started_at",
                            lambda _p: seq.pop(0) if len(seq) > 1 else seq[0])
        monkeypatch.setattr(cq, "_systemftp_pids", lambda: {321})
        monkeypatch.setattr(cq.time, "sleep", lambda _s: None)
        clock = {"t": 0.0}

        def _now():
            clock["t"] += 1.0
            return clock["t"]
        monkeypatch.setattr(cq.time, "time", _now)
        posted = []
        asked = {}

        def _find(pids=None, **k):
            # ★記下【實際被拿去找視窗的那個集合】★
            #   突變驗證抓到的洞:`live = set(pids)`(不過濾)之後,
            #   原本只看「有沒有送出 WM_CLOSE」的斷言照樣全綠。
            asked["pids"] = set(pids or ())
            return [9] if pids else []
        monkeypatch.setattr(cq, "find_windows", _find)
        monkeypatch.setattr(cq.win32gui, "PostMessage",
                            lambda h, *a: posted.append(h))
        self.asked = asked
        killed = []

        class _P:
            def __init__(self, pid):
                self.pid = pid

            def name(self):
                return cq.SYSTEMFTP_EXE_NAME

            def terminate(self):
                killed.append(self.pid)
        monkeypatch.setattr(cq.psutil, "Process", _P)
        return posted, killed

    def test_a_recycled_pid_is_not_terminated(self, monkeypatch):
        """驗證時是 A 行程，等待期間換成 B 行程 → 不可以殺 B。"""
        _posted, killed = self._prep(monkeypatch, [1000.0, 9999.0])
        cq.close_pids({321})
        assert killed == [], "★殺掉了在等待期間才被回收的那個行程★"

    def test_a_recycled_pid_gets_no_wm_close_either(self, monkeypatch):
        """★WM_CLOSE 也是關閉★ 不可以只在 terminate 前把關。

        `close_pids` 的既有註解就寫著這件事(「驗證必須在【送 WM_CLOSE 之前】」)。
        這裡確認被回收的 PID 根本沒有進到「要找視窗的那個集合」。
        """
        posted, _killed = self._prep(monkeypatch, [1000.0, 9999.0])
        cq.close_pids({321})
        assert self.asked["pids"] == set(), (
            f"★對已被回收的 PID 送了 WM_CLOSE★ 集合:{self.asked['pids']}")
        assert posted == []

    def test_the_same_process_is_still_terminated(self, monkeypatch):
        """★反方向:真的是同一個就要關掉★ 否則孤兒永遠清不掉。"""
        posted, killed = self._prep(monkeypatch, [1000.0])
        cq.close_pids({321})
        assert killed == [321]
        assert self.asked["pids"] == {321} and posted == [9], (
            "同一個行程卻連 WM_CLOSE 都沒送 → 孤兒清不掉")

    def test_an_unreadable_start_time_does_not_block_cleanup(self, monkeypatch):
        """讀不到建立時間 → 不採用這個訊號（維持既有行為，仍會清理）。"""
        _posted, killed = self._prep(monkeypatch, [None])
        cq.close_pids({321})
        assert killed == [321]


class TestTheBaselineAlertIsThrottled:
    """★[自查 P1-B]★ 這支函式被呼叫的位置在【重試迴圈裡面】。

      * 同一輪：寄信失敗會重試 3 次 → 最多 3 封
      * 跨輪：基準要等到「寄信成功」才會被重建，所以只要寄不出去，
        每 3 分鐘的下一輪又會再發現一次「基準不見了」

    一次磁碟事故可以變成整天每 3 分鐘一封。
    """

    def _count_sends(self, monkeypatch):
        started = []
        monkeypatch.setattr(cq.threading, "Thread",
                            lambda target=None, **k: type(
                                "T", (), {"start": lambda s: started.append(1)})())
        return started

    def test_a_second_alert_within_the_window_is_suppressed(self, monkeypatch):
        started = self._count_sends(monkeypatch)
        monkeypatch.setattr(cq, "_baseline_alert_at", 0.0)
        monkeypatch.setattr(cq.time, "time", lambda: 100000.0)

        cq._alert_baseline_lost("corrupt", 3)
        cq._alert_baseline_lost("corrupt", 3)
        cq._alert_baseline_lost("corrupt", 3)

        assert len(started) == 1, f"一次事故寄了 {len(started)} 封"

    def test_it_alerts_again_after_the_cooldown(self, monkeypatch):
        """★反方向:冷卻過了要再說一次★ 這是不會自己好的事情。"""
        started = self._count_sends(monkeypatch)
        monkeypatch.setattr(cq, "_baseline_alert_at", 0.0)
        clock = {"t": 100000.0}
        monkeypatch.setattr(cq.time, "time", lambda: clock["t"])

        cq._alert_baseline_lost("corrupt", 3)
        clock["t"] += cq._BASELINE_ALERT_COOLDOWN_SEC + 1
        cq._alert_baseline_lost("corrupt", 3)

        assert len(started) == 2

    def test_a_backwards_clock_does_not_lock_it_out(self, monkeypatch):
        """時鐘往前跳(NTP/使用者改時間)不可以把告警鎖死到那個未來時間。"""
        started = self._count_sends(monkeypatch)
        monkeypatch.setattr(cq, "_baseline_alert_at", 9_000_000_000.0)
        monkeypatch.setattr(cq.time, "time", lambda: 100000.0)

        cq._alert_baseline_lost("corrupt", 3)

        assert len(started) == 1, "上次時間落在未來 → 告警被自己鎖死"
