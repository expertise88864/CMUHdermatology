# -*- coding: utf-8 -*-
"""★實機事故 2026-08-05 15:00★ 會診查詢全停，重開機也一樣。

【事故經過】診間 log：
```
14:52:28,451 [consult-extract] 擷取完成:清單 0 位
14:52:28,762 收掉 systemftp(現任):會診單已收掉,但主畫面沒回到可操作狀態:
             主畫面被 modal 對話框擋住(disabled)
14:52:31,765 ★主畫面關不掉★(hwnd=207298)
14:52:31,765 ★關不掉,先掛帳待重試★ —— 這個 HIS session 可能仍登入中(帳上共 1 個)
...           之後每一輪:仍有 1 個無法確認關閉的住院系統登入 → 本輪不建立新登入
```

【三個當天的改動疊在一起才炸】
1. `_main_ready_for_next_cycle`（P2-02）**只取樣一次**就判定主畫面被 modal 擋住。
   Delphi 的 modal form 是先 Hide、後把 owner 重新 enable，而
   `_consult_form_dismissed` 把「看不見」就算退場 —— 剛好卡在那 311 毫秒的縫裡。
2. 判定不可操作 → **收掉 session**。但 ★disabled 的視窗不會處理 WM_CLOSE★，
   所以在這個狀態下收尾【必然失敗】。
3. 收尾失敗 → 掛帳 → **無限期 fail-closed 閘門**（P1-02）擋住所有新登入。

三個都是我當天寫的，每一個單獨看都有道理，疊起來把一個會自己好的暫態變成
「臨床查詢完全停擺、而且重開機也一樣」。

【本檔守的三件事】
* 主畫面 enabled 要用等的，不是取樣一次
* 關主畫面之前要先把 modal 按掉（否則 WM_CLOSE 一定沒用）
* 閘門必須有上限（fail-closed 的前提是「遲早會好」，這裡證明了前提會不成立）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


class _Sess:
    def __init__(self):
        self.pid = 18072
        self.our_pids = {18072, 21472}
        self.main_hwnd = 207298
        self.main_pid = 18072
        self.main_class = cq.MAIN_CLASS
        self.hproc = object()


class TestTheModalIsClearedBeforeClosing:
    """★disabled 的視窗不會處理 WM_CLOSE★

    「收尾關不掉主畫面」與「主畫面被 modal 擋住」是同一件事的兩個症狀 ——
    當天三次失敗，前一行 log 都寫著被 modal 擋住。
    """

    def test_close_dismisses_modals_first(self, monkeypatch):
        order = []
        monkeypatch.setattr(cq, "_window_identity",
                            lambda _h: (18072, cq.MAIN_CLASS))
        monkeypatch.setattr(cq, "_dismiss_blocking_modals",
                            lambda _s: order.append("dismiss") or 1)

        cq._close_session_windows(
            _Sess(), close=lambda _h: order.append("close"),
            gone=lambda _h: "close" in order, sleep=lambda _s: None)

        assert order[:2] == ["dismiss", "close"], (
            f"★沒先按掉 modal 就送 WM_CLOSE★ 那個視窗是 disabled 的:{order}")

    def test_it_only_touches_our_own_process_notices(self, monkeypatch):
        """只按【我們登入的那個行程】的通知，不動醫師的。"""
        asked = {}

        def _find(cls=None, pids=None, **k):
            asked["cls"], asked["pids"] = cls, pids
            return []
        monkeypatch.setattr(cq, "find_windows", _find)

        cq._dismiss_blocking_modals(_Sess())

        assert asked["cls"] == cq.NOTICE_CLASS
        assert asked["pids"] == {18072}, (
            f"★用了太寬的 PID 集合★ 會按到醫師自己的視窗:{asked['pids']}")

    def test_no_main_pid_means_no_clicking(self):
        """身分不明 → 什麼都不按（不知道是誰的視窗就不要動它）。"""
        class _NoIdent:
            main_pid = None
        assert cq._dismiss_blocking_modals(_NoIdent()) == 0


class TestReturnToMainDoesNotTearDownOnATransientModal:
    """會自己好的暫態不可以被升級成「收掉 session」。"""

    def _prep(self, monkeypatch, ready_after_dismiss):
        monkeypatch.setattr(cq, "find_child", lambda *a: None)
        monkeypatch.setattr(cq.win32gui, "PostMessage", lambda *a: None)
        monkeypatch.setattr(cq.time, "sleep", lambda _s: None)
        monkeypatch.setattr(cq, "_consult_form_dismissed", lambda _h: True)
        state = {"dismissed": False}

        def _dismiss(_s):
            state["dismissed"] = True
            return 1
        monkeypatch.setattr(cq, "_dismiss_blocking_modals", _dismiss)
        monkeypatch.setattr(
            cq, "_main_ready_for_next_cycle",
            lambda _s: "" if (state["dismissed"] and ready_after_dismiss)
            else "主畫面仍被 modal 對話框擋住(disabled)")
        closed = []
        monkeypatch.setattr(cq, "_session_close_if_current",
                            lambda s, r: closed.append(r))
        return closed, state

    def test_a_modal_is_dismissed_and_the_session_survives(self, monkeypatch):
        """★核心★ 按掉 modal 之後主畫面恢復 → session 要留著。"""
        closed, state = self._prep(monkeypatch, ready_after_dismiss=True)
        cq._return_to_main(_Sess(), 7002)
        assert state["dismissed"], "根本沒試著按掉 modal"
        assert closed == [], (
            "★一個會自己好的 modal 把 session 收掉了★ 而且收尾必然失敗 → 整個停擺")

    def test_a_stuck_modal_still_ends_the_session(self, monkeypatch):
        """★反方向:按了還是不行,那就真的不能再用了★"""
        closed, _ = self._prep(monkeypatch, ready_after_dismiss=False)
        cq._return_to_main(_Sess(), 7002)
        assert closed and "沒回到可操作狀態" in closed[0]


class TestTheGateHasACeiling:
    """★fail-closed 的前提是「這個狀況遲早會解除」★

    當天證明前提會不成立：主畫面 disabled → 不處理 WM_CLOSE → 永遠關不掉
    → 閘門把會診查詢從下午 2:52 起全停，使用者重開機也一樣。

    臨床上「查詢完全停擺」比「隱藏桌面多一個登入中的 session」嚴重得多，
    而且後者本來就有 systemftp「最多兩個」的自然上限會讓它自己浮現。
    """

    def _clock(self, seconds):
        return lambda: seconds

    def test_it_blocks_at_first(self, monkeypatch):
        monkeypatch.setattr(cq, "_retry_unclosed_sessions", lambda: 1)
        monkeypatch.setattr(cq, "_unmanaged_since", 0.0)
        try:
            cq._ensure_no_unmanaged_sessions(now=self._clock(1000.0))
        except cq.UnmanagedSessionError:
            return
        raise AssertionError("一開始就不擋 → 會同時有兩個登入")

    def test_it_gives_up_blocking_after_the_ceiling(self, monkeypatch):
        """★這一支就是事故的解藥★ 擋太久要放行，否則臨床查詢無聲停擺。"""
        monkeypatch.setattr(cq, "_retry_unclosed_sessions", lambda: 1)
        monkeypatch.setattr(cq, "_unmanaged_since", 0.0)
        try:
            cq._ensure_no_unmanaged_sessions(now=self._clock(1000.0))
        except cq.UnmanagedSessionError:
            pass
        # 超過上限之後必須放行
        later = 1000.0 + cq._UNMANAGED_BLOCK_MAX_SEC + 1
        cq._ensure_no_unmanaged_sessions(now=self._clock(later))   # 不拋 = 放行

    def test_the_clock_resets_once_it_is_clean(self, monkeypatch):
        """關掉之後計時要歸零，下次出問題才會重新給滿一個窗口。"""
        monkeypatch.setattr(cq, "_retry_unclosed_sessions", lambda: 1)
        monkeypatch.setattr(cq, "_unmanaged_since", 0.0)
        try:
            cq._ensure_no_unmanaged_sessions(now=self._clock(1000.0))
        except cq.UnmanagedSessionError:
            pass
        monkeypatch.setattr(cq, "_retry_unclosed_sessions", lambda: 0)
        cq._ensure_no_unmanaged_sessions(now=self._clock(1100.0))
        assert cq._unmanaged_since == 0.0, "恢復之後計時沒有歸零"

    def test_the_ceiling_is_not_absurdly_long(self):
        """上限要是「臨床上還能接受的停擺時間」，不是形式上的無限大。"""
        assert 0 < cq._UNMANAGED_BLOCK_MAX_SEC <= 30 * 60
