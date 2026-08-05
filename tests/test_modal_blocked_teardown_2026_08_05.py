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

    def test_it_only_touches_our_own_process_dialogs(self, monkeypatch):
        """只按【我們自己的行程】的對話框，不動醫師的。"""
        asked = {}

        def _find(cls=None, pids=None, **k):
            asked["pids"] = pids
            return []
        monkeypatch.setattr(cq, "find_windows", _find)

        cq._dismiss_blocking_modals(_Sess())

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


class TestTheBlockerIsFoundByEnabledStateNotByClassName:
    """★[2026-08-05 實機 log 的關鍵發現]★ 診斷傾印把答案直接寫出來了：

        TFrmLogin(vis=1,en=0)      ← 我們的登入視窗，被擋住
        TFMTimeOut_1(vis=1,en=1)   ← ★唯一 enabled 的，它才是擋路的那個★
        TFMShowMessage(vis=1,en=0) ← 通知視窗，它自己也被擋住
        TFMNewMain(vis=1,en=0)     ← 主畫面，被擋住

    而程式對著【disabled 的】TFMShowMessage 按了 6 次「確認」——disabled 的視窗
    根本不會處理點擊 —— 整整 120 秒的登入預算就這樣燒光，最後回報「登入沒有完成」。
    `TFMTimeOut_1` 這個 class 程式從來不認得。

    所以判準不可以是「class 是不是 NOTICE_CLASS」（那是在猜對話框長什麼樣）。
    Win32 已經有正規訊號：modal 會把其他視窗 disable，而它自己是 enabled 的。
    """

    def _machine(self, monkeypatch, windows):
        """windows: [(hwnd, class, enabled)] —— 模擬那台機器當下的畫面。"""
        by_hwnd = {h: (c, en) for h, c, en in windows}
        monkeypatch.setattr(cq, "find_windows",
                            lambda cls=None, pids=None, **k: list(by_hwnd))
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled",
                            lambda h: by_hwnd[h][1])
        monkeypatch.setattr(cq.win32gui, "GetClassName",
                            lambda h: by_hwnd[h][0])
        return by_hwnd

    # 實機那一刻的畫面
    _REAL = [(1, cq.LOGIN_CLASS, False),
             (2, "TFMTimeOut_1", True),
             (3, cq.NOTICE_CLASS, False),
             (4, cq.MAIN_CLASS, False)]

    def test_it_finds_the_unknown_dialog(self, monkeypatch):
        self._machine(monkeypatch, self._REAL)
        found = cq._blocking_dialogs({18072})
        assert found == [(2, "TFMTimeOut_1")], (
            f"★沒找到真正擋路的那個★ 找到的是:{found}")

    def test_it_ignores_the_disabled_notice(self, monkeypatch):
        """★這正是燒掉 120 秒的那個誤判★ 通知視窗自己被擋住時按它沒有用。"""
        self._machine(monkeypatch, self._REAL)
        assert 3 not in [h for h, _c in cq._blocking_dialogs({18072})]

    def test_content_windows_are_never_treated_as_dialogs(self, monkeypatch):
        """主畫面/登入視窗/會診單就算是 enabled 也不是「擋路的對話框」。"""
        self._machine(monkeypatch, [(1, cq.LOGIN_CLASS, True),
                                    (4, cq.MAIN_CLASS, True),
                                    (5, cq.CONSULT_CLASS, True)])
        assert cq._blocking_dialogs({18072}) == []

    def test_it_clicks_an_affirmative_button(self, monkeypatch):
        self._machine(monkeypatch, self._REAL)
        monkeypatch.setattr(cq, "enum_children",
                            lambda _h: [(90, "TLabel", "連線逾時", (0,) * 4),
                                        (91, "TButton", "確認", (0,) * 4)])
        clicked = []
        monkeypatch.setattr(cq, "click_button", clicked.append)

        assert cq._dismiss_blocking_modals(pids={18072}) == 1
        assert clicked == [91]

    def test_it_never_blind_clicks_an_unknown_dialog(self, monkeypatch, caplog):
        """★不認得的按鈕一律不按★

        在醫院系統上亂點按鈕的代價，比多停一輪查詢大得多。
        不認得就把 class 與按鈕字樣記進 log，讓下一次知道它長什麼樣。
        """
        import logging as _lg
        self._machine(monkeypatch, self._REAL)
        monkeypatch.setattr(cq, "enum_children",
                            lambda _h: [(92, "TButton", "刪除病歷", (0,) * 4)])
        clicked = []
        monkeypatch.setattr(cq, "click_button", clicked.append)

        with caplog.at_level(_lg.WARNING):
            assert cq._dismiss_blocking_modals(pids={18072}) == 0
        assert clicked == [], "★盲按了不認得的按鈕★"
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "TFMTimeOut_1" in msgs and "刪除病歷" in msgs, (
            "沒有把不認得的對話框記下來 → 下一次還是不知道它長什麼樣")

    def test_no_pids_means_no_action(self):
        assert cq._blocking_dialogs(set()) == []
        assert cq._dismiss_blocking_modals(pids=set()) == 0


def test_the_login_wait_clears_blockers_before_clicking_the_notice():
    """★接線★ 等主畫面的迴圈要先處理擋路的對話框。

    那個迴圈就是燒掉 120 秒的地方：它只認得 `NOTICE_CLASS`，而那個視窗自己
    正被別的 modal 擋住。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._wait_main_window_after_login)))
    dismiss_at = notice_at = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", "")
        if name == "_dismiss_blocking_modals" and dismiss_at is None:
            dismiss_at = node.lineno
        if (name == "find_windows" and notice_at is None
                and any(getattr(a, "id", "") == "NOTICE_CLASS" for a in node.args)):
            notice_at = node.lineno
    assert dismiss_at is not None, (
        "★登入等待迴圈沒有處理擋路的對話框★ 會對 disabled 的通知視窗一直按")
    assert notice_at is not None, "找不到通知視窗的處理（測試失效了）"
    assert dismiss_at < notice_at, "要先清掉擋路的對話框，再處理通知視窗"


class TestDelphiInfrastructureWindowsAreNotDialogs:
    """★[2026-08-05 實機] `TApplication` 不是對話框★

    每個 Delphi 程式都有一個 class 為 `TApplication` 的隱形擁有者視窗：
    有 WS_VISIBLE、永遠 enabled、**沒有任何按鈕**。第一版把它算成「擋路的
    對話框」，於是每一輪都印一行「有擋路的對話框但沒有可按的按鈕」。

    實害只有雜訊（它沒有按鈕，本來就不會被按），但 log 是我們唯一的實機診斷
    管道 —— 2026-07-29 那次 1,568 行幾乎全是同一句的教訓還在。
    """

    def _machine(self, monkeypatch, windows):
        by_hwnd = {h: (c, en) for h, c, en in windows}
        monkeypatch.setattr(cq, "find_windows",
                            lambda cls=None, pids=None, **k: list(by_hwnd))
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled",
                            lambda h: by_hwnd[h][1])
        monkeypatch.setattr(cq.win32gui, "GetClassName",
                            lambda h: by_hwnd[h][0])

    def test_tapplication_is_ignored(self, monkeypatch):
        self._machine(monkeypatch, [(1, "TApplication", True),
                                    (2, cq.MAIN_CLASS, True)])
        assert cq._blocking_dialogs({17288}) == [], (
            "★Delphi 的隱形擁有者視窗被當成擋路的對話框★ 每輪都會多一行雜訊")

    def test_a_real_dialog_next_to_it_is_still_found(self, monkeypatch):
        """★反方向:不可以因此漏掉真的對話框★"""
        self._machine(monkeypatch, [(1, "TApplication", True),
                                    (2, cq.NOTICE_CLASS, True),
                                    (3, cq.MAIN_CLASS, False)])
        assert cq._blocking_dialogs({17288}) == [(2, cq.NOTICE_CLASS)]

    def test_the_unknown_dialog_warning_is_throttled(self, monkeypatch,
                                                     caplog):
        """同一個 class 只講一次 —— 這個迴圈每 0.4 秒跑一次。"""
        import logging as _lg
        self._machine(monkeypatch, [(9, "TSomethingNew", True)])
        monkeypatch.setattr(cq, "enum_children", lambda _h: [])
        monkeypatch.setattr(cq, "_reported_unknown_dialogs", set())

        with caplog.at_level(_lg.WARNING):
            for _ in range(5):
                cq._dismiss_blocking_modals(pids={17288})

        hits = [r for r in caplog.records if "沒有可按的按鈕" in r.getMessage()]
        assert len(hits) == 1, f"同一個 class 講了 {len(hits)} 次 → log 會被洗掉"
        assert "TSomethingNew" in hits[0].getMessage()
