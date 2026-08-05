# -*- coding: utf-8 -*-
"""收掉 session 必須真的把主畫面關掉（2026-08-04 外審第 3 輪 P1-05）。

★這是我自己造成的迴歸★
批次 P 把 teardown 收緊成「只終止驗證過的自有行程」，而實機證實 systemftp 是
啟動器型行程 —— 我們 spawn 的 root 立刻結束。兩件事加在一起：

    _verified_owned_pids(已死的 root, ...)  → 只剩 {已死的 root}
    close_pids({已死的 pid})                → 什麼都關不到
    WaitForSingleObject(hproc)              → 早已 signaled → 不執行 Terminate

淨結果：teardown 只清掉 Python 這邊的參照，真正的 HIS UI 還留在隱藏桌面登入中。
「6 小時定期重啟」「休息時段收掉」「接管舊 worker」全都只是說說而已。

行程層面關不掉，就從【視窗層面】關：對確切的主畫面送 WM_CLOSE 並★回讀確認★。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


class _Sess:
    """our_pids 刻意含一個外來 pid（15056）—— 實機就是這樣。

    ★[2026-08-05] 身分指紋★ 登入當下記下的 (pid, class)。`_close_session_windows`
    在送 WM_CLOSE 之前會拿它跟現況比對，所以測試也必須帶著它 —— 用生產的形狀。
    """

    pid = 1860
    our_pids = {1860, 15056}

    def __init__(self, main_hwnd=111, main_pid=1860, main_class=cq.MAIN_CLASS):
        self.main_hwnd = main_hwnd
        self.main_pid = main_pid
        self.main_class = main_class


def _same_window(monkeypatch, sess=None, pid=1860, cls=None):
    """讓身分比對成立（多數測試關心的不是這一段）。"""
    monkeypatch.setattr(cq, "_window_identity",
                        lambda _h: (pid, cls or cq.MAIN_CLASS))


class TestOnlyOurOwnLoggedInWindowIsClosed:
    """★[2026-08-04 自查 P0] 不可以用 `our_pids` 決定「哪個視窗是我的」★

    第一版寫成 `find_windows(MAIN_CLASS, pids=sess.our_pids)`。`our_pids` 是全機
    PID 差集，實機已證實會混進外來的 systemftp（log：「pid 10928 已非 systemftp」、
    「收尾時排除 1 個不屬於本次啟動的」）。拿它當授權去送 WM_CLOSE，等於把批次 P
    擋掉的傷害從【視窗】這道門放回來 —— 醫師的住院系統會被關掉。

    改成只關 `sess.main_hwnd`：`_wait_main_window_after_login()` 回傳的、我們
    【確切登入進去】的那一個。身分由「我們自己登入它」保證，不靠 PID 猜。
    """

    def test_it_closes_the_exact_window_and_confirms(self, monkeypatch):
        _same_window(monkeypatch)
        closed = []
        state = {"gone": False}

        def _close(h):
            closed.append(h)
            state["gone"] = True

        ok = cq._close_session_windows(
            _Sess(main_hwnd=111), close=_close,
            gone=lambda _h: state["gone"], sleep=lambda _s: None)

        assert ok is True
        assert closed == [111], f"沒有關到我們登入的那個視窗：{closed}"

    def test_it_never_enumerates_by_pid(self):
        """★核心★ 就算 our_pids 裡有醫師的行程，也不可以碰到它的視窗。

        用「`find_windows` 一旦被呼叫就炸」來證明這條路根本沒被走。
        """
        def _boom(*_a, **_k):
            raise AssertionError("★又用 PID 集合去找視窗了★ 會關到醫師的 HIS")

        import unittest.mock as _m
        with _m.patch.object(cq, "find_windows", _boom):
            ok = cq._close_session_windows(
                _Sess(main_hwnd=111), close=lambda _h: None,
                gone=lambda _h: True, sleep=lambda _s: None)
        assert ok is True

    def test_a_session_that_never_logged_in_has_nothing_to_close(self):
        """`main_hwnd is None` = 從未登入成功 → 沒有我們的主畫面可關。

        `_wait_main_window_after_login()` 只有一條成功出口、失敗一律拋例外，
        所以成功建立的 session 必然帶著 hwnd（不變式由下面的接線測試守著）。
        ★重點是它【不會】退回用 PID 差集去找★——那個集合含醫師的行程。
        """
        closed = []
        ok = cq._close_session_windows(
            _Sess(main_hwnd=None), close=closed.append,
            gone=lambda _h: False, sleep=lambda _s: None)

        assert ok is True
        assert closed == [], f"沒有 hwnd 卻還是關了東西：{closed}"

    def test_a_window_that_refuses_to_close_is_reported(self, caplog,
                                                       monkeypatch):
        """★送了不回讀就是開迴路★ 關不掉要說出來，不可以假裝收乾淨了。"""
        _same_window(monkeypatch)
        import logging as _lg
        with caplog.at_level(_lg.ERROR):
            ok = cq._close_session_windows(
                _Sess(main_hwnd=111), close=lambda _h: None,
                gone=lambda _h: False, sleep=lambda _s: None)

        assert ok is False, "關不掉卻回報成功"
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "關不掉" in msgs and "仍然登入" in msgs, msgs

    def test_a_window_already_gone_is_not_a_failure(self):
        """本來就不在了 → 沒東西要關，不算失敗。"""
        assert cq._close_session_windows(
            _Sess(main_hwnd=111), close=lambda _h: None,
            gone=lambda _h: True, sleep=lambda _s: None) is True

    def test_a_failed_post_is_not_reported_as_closed(self, monkeypatch):
        """★措辭鐵律★ 送不出去 ≠ 已經關掉。"""
        _same_window(monkeypatch)

        def _boom(_h):
            raise OSError("PostMessage 失敗")
        assert cq._close_session_windows(
            _Sess(main_hwnd=111), close=_boom,
            gone=lambda _h: False, sleep=lambda _s: None) is False


class TestTwoQuestionsTwoPredicates:
    """★[2026-08-05 外審第 4 輪 P1-01/P1-02]★ 兩個相反的問題不可以共用一個述詞。

    這個檔案原本有一支 `_window_is_gone`，同時回答：
        「還能用嗎？」（判活）  和  「關掉了嗎？」（收尾確認）
    它把「隱藏」與「API 例外」都算成 True。對判活是對的（不能用就重登），
    對收尾卻是**災難**：HIS 明明還登入著，程式回報收尾成功、還把參照丟掉，
    從此沒有任何程式碼認得那個 session。

    ★而且這個檔案的舊測試把它釘成了通過條件★
    `test_a_hidden_but_alive_window_is_also_gone` 與
    `test_an_api_failure_counts_as_gone` —— 兩支名字就寫著 P1-01/P1-02 的內容，
    綠燈綠了一整天。測試的期望本身也是一種宣稱，會把缺陷釘死。

    現在分成兩支，對「不知道」的處置刻意相反。
    """

    # ── 判活：不知道 → 當作不能用（安全方向 = 重登） ──
    def test_a_destroyed_window_is_not_alive(self, monkeypatch):
        monkeypatch.setattr(cq.win32gui, "IsWindow", lambda _h: False)
        assert cq._window_alive(111) is False

    def test_a_hidden_window_is_still_alive(self, monkeypatch):
        """★不可以看可見性★ SW_HIDE 後備模式是【我們自己】把它藏起來的。

        把「被自己藏起來」讀成「死了」= 每輪重登 = 每 3 分鐘重送一次帳密，
        那正是 2026-08-04 才剛修掉的實機故障（21/21 輪判死）。
        """
        monkeypatch.setattr(cq.win32gui, "IsWindow", lambda _h: True)
        monkeypatch.setattr(cq.win32gui, "IsWindowVisible", lambda _h: False)
        assert cq._window_alive(111) is True

    def test_an_api_failure_means_not_usable(self, monkeypatch):
        def _boom(_h):
            raise OSError("handle 無效")
        monkeypatch.setattr(cq.win32gui, "IsWindow", _boom)
        assert cq._window_alive(111) is False, "判活查不到 → 保守當作不能用"

    # ── 收尾確認：不知道 → 當作【沒關掉】（安全方向 = 告警） ──
    def test_only_a_destroyed_window_counts_as_closed(self, monkeypatch):
        monkeypatch.setattr(cq.win32gui, "IsWindow", lambda _h: False)
        assert cq._window_destroyed(111) is True

    def test_a_hidden_window_is_not_confirmed_closed(self, monkeypatch):
        """★P1-01★ Delphi 表單關閉後常常只是 Hide（2026-07-27 事故）——
        所以「看不見」【更不能】拿來當「已經關掉」的證據，HIS 還登入著。"""
        monkeypatch.setattr(cq.win32gui, "IsWindow", lambda _h: True)
        monkeypatch.setattr(cq.win32gui, "IsWindowVisible", lambda _h: False)
        assert cq._window_destroyed(111) is False, (
            "★隱藏被當成關掉了★ HIS 仍登入中卻回報收尾成功")

    def test_an_api_failure_is_not_confirmed_closed(self, monkeypatch):
        """★P1-02★ 查不到 ≠ 關掉了。"""
        def _boom(_h):
            raise OSError("handle 無效")
        monkeypatch.setattr(cq.win32gui, "IsWindow", _boom)
        assert cq._window_destroyed(111) is False

    def test_the_two_predicates_disagree_on_unknown(self, monkeypatch):
        """★核心不變量★ 對「不知道」，兩者必須給出**不同**的答案。

        若哪天有人把其中一支改成另一支的別名（或用 `not` 互相定義），
        其中一邊就會落在錯的安全方向 —— 這一支會轉紅。
        """
        def _boom(_h):
            raise OSError("handle 無效")
        monkeypatch.setattr(cq.win32gui, "IsWindow", _boom)
        assert cq._window_alive(111) is False      # 不能用
        assert cq._window_destroyed(111) is False  # 也還沒關掉
        # 兩個都是 False → 它們不是互為反面

    def test_teardown_uses_the_destroyed_predicate(self):
        """★接線★ 收尾的預設判準必須是 `_window_destroyed`，不是判活那支。"""
        import inspect
        src = inspect.getsource(cq._close_session_windows)
        assert "gone = gone or _window_destroyed" in src, src[:400]


class TestHandleRecycling:
    """★P1-04★ hwnd 值會被 Windows 回收再發給別的視窗。"""

    def test_a_recycled_handle_is_not_closed(self, monkeypatch, caplog):
        """身分對不上 → 不送 WM_CLOSE（那可能是醫師自己的住院系統）。"""
        import logging as _lg
        # 現在這個 hwnd 屬於別的行程（醫師自己開的 systemftp，class 一模一樣）
        monkeypatch.setattr(cq, "_window_identity",
                            lambda _h: (18748, cq.MAIN_CLASS))
        closed = []
        with caplog.at_level(_lg.ERROR):
            ok = cq._close_session_windows(
                _Sess(main_hwnd=111, main_pid=1860), close=closed.append,
                gone=lambda _h: False, sleep=lambda _s: None)
        assert closed == [], "★關到別人的視窗了★"
        assert ok is False, "沒關成功就不可以回報成功"
        assert "不是我們登入的那個視窗" in " ".join(
            r.getMessage() for r in caplog.records)

    def test_identity_is_unknown_means_not_same(self):
        """舊 session／查不到身分 → 一律當成「不是」（不知道不可以當成是）。"""
        s = _Sess(main_hwnd=111, main_pid=None, main_class=None)
        assert cq._is_same_window(s) is False

    def test_no_hwnd_is_not_same(self):
        assert cq._is_same_window(_Sess(main_hwnd=None)) is False

    def test_the_session_records_its_identity_at_login(self, monkeypatch):
        """★接線★ 建 session 當下就要把 (pid, class) 記下來。

        事後再問等於沒問 —— handle 可能早就被回收了。
        """
        monkeypatch.setattr(cq, "_window_identity",
                            lambda h: (777, cq.MAIN_CLASS) if h else (None, None))
        s = cq._PersistentSession(None, None, 123, {123}, main_hwnd=555)
        assert (s.main_pid, s.main_class) == (777, cq.MAIN_CLASS)


def test_teardown_actually_calls_the_window_close():
    """★接線本身也要被測到★（本 session 這個形狀第八次）

    上面幾支直接呼叫 `_close_session_windows`。若 `_terminate_session_process`
    沒有接上它，真正的 HIS UI 照樣關不掉 —— 而那些測試會全綠。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._terminate_session_process)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_close_session_windows" in called, (
        "★teardown 沒有關主畫面★ 只清掉 Python 參照，HIS 仍登入中")


def test_the_session_records_the_window_it_logged_into():
    """★接線★ 冷啟動必須接住 `_wait_main_window_after_login()` 的回傳值。

    那行以前寫成不接回傳值，於是 session 不知道自己登入了哪個視窗，收尾只好用
    PID 差集猜 —— 而那個集合含醫師的行程。這裡確認回傳值有被接住並傳進 session。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._cold_start_session_impl)))

    # `_wait_main_window_after_login(...)` 的結果必須被指派給某個名字
    assigned = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        v = node.value
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == "_wait_main_window_after_login"):
            assigned = node.targets[0].id
    assert assigned, (
        "★主畫面 hwnd 的回傳值沒被接住★ session 不知道自己登入了哪個視窗")

    # 而且要當成 main_hwnd 傳進 _PersistentSession
    # ★any 不是 last★：這個函式有兩處建構 —— 成功路徑（要帶 hwnd）與冷啟動失敗
    #   後為了收行程而臨時建的那個（本來就沒有主畫面，不該帶）。第一版寫成迴圈
    #   覆蓋，結果被【錯誤路徑那個】決定了結果而誤紅。
    passed = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_PersistentSession"
        and any(kw.arg == "main_hwnd" for kw in n.keywords)
        for n in ast.walk(tree))
    assert passed, "hwnd 接住了卻沒傳進 session"


def test_the_persistent_session_defaults_to_unknown():
    """沒指定就是 None —— 不可以預設成某個「看起來合理」的值。"""
    s = cq._PersistentSession(None, None, 123, {123})
    assert s.main_hwnd is None
