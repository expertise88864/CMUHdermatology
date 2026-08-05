# -*- coding: utf-8 -*-
"""真正下命令的那一行也必須用確切身分（外審第 5 輪 P1-01）。

【上一批漏掉的地方】
批次S 把【判活】(`_session_death_reason`) 與【收尾】(`_close_session_windows`)
都改成用 `sess.main_hwnd`，卻把**真正操作**那一段留在原地：

    mains = find_windows(MAIN_CLASS, pids=sess.our_pids)
    main_hwnd = mains[0]                      # ← 列舉順序決定的「第一個」
    PostMessage(main_hwnd, WM_COMMAND, cmd_id)

而實機 log 已證實 `our_pids` **每一次登入**都混著醫師自己的 systemftp
（`登入視窗 hwnd=... pid=[8036, 16276]`，三次登入三次都要在收尾時排除一個）。

於是可能發生：

    用確切身分確認自己的 session 健康
    → 下一行從污染的集合挑出醫師正在用的 HIS
    → 對它送出「我的會診清單」命令
    → 醫師畫面自己跳出會診單，我們還把他螢幕上的病人清單擷取下來寄出去

會診視窗同理：`hits[0]` 會撿到醫師自己已經開著的會診單。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402

OURS_MAIN = 5001          # 我們登入的主畫面
DOCS_MAIN = 4001          # 醫師自己開的 HIS 主畫面
OUR_PID = 16276
DOC_PID = 8036


class _Sess:
    """our_pids 含醫師的 pid —— 實機三次登入三次如此。"""

    def __init__(self):
        self.pid = OUR_PID
        self.our_pids = {OUR_PID, DOC_PID}
        self.main_hwnd = OURS_MAIN
        self.main_pid = OUR_PID
        self.main_class = cq.MAIN_CLASS
        self.hproc = object()


def _healthy(monkeypatch):
    monkeypatch.setattr(cq, "_session_death_reason", lambda _s: "")


def _fast_clock(monkeypatch, step=0.5):
    """讓 `time.time()` 快轉，等待迴圈才不會真的空轉 60 秒。"""
    t = {"v": 1000.0}

    def _now():
        t["v"] += step
        return t["v"]
    monkeypatch.setattr(cq.time, "time", _now)
    monkeypatch.setattr(cq.time, "sleep", lambda _s: None)


class TestTheMenuCommandGoesToOurOwnWindow:

    def test_the_doctors_main_window_is_never_commanded(self, monkeypatch):
        """★核心★ 醫師的主畫面排在列舉結果第一個時，命令仍只能送到我們那一個。

        這正是舊寫法會出錯的排列：`mains[0]` 就是醫師的視窗。
        """
        _healthy(monkeypatch)
        # 列舉時醫師的視窗排第一 —— 舊寫法會挑中它
        monkeypatch.setattr(
            cq, "find_windows",
            lambda cls=None, **k: ([DOCS_MAIN, OURS_MAIN]
                                   if cls == cq.MAIN_CLASS else []))
        monkeypatch.setattr(cq, "resolve_menu_command_id", lambda _h: 42)
        posted = []
        monkeypatch.setattr(cq.win32gui, "PostMessage",
                            lambda h, m, w, l: posted.append(h))
        _fast_clock(monkeypatch)
        # 等不到會診視窗 → 會拋例外，但命令早就送出去了，那才是重點
        try:
            cq._query_cycle(_Sess(), {}, "今日會診病人")
        except RuntimeError:
            pass

        assert posted, "根本沒送出命令"
        assert posted[0] == OURS_MAIN, (
            f"★命令送到 hwnd={posted[0]}★ 那是醫師正在用的住院系統")
        assert DOCS_MAIN not in posted

    def test_it_refuses_to_run_when_the_session_is_not_ours(self, monkeypatch):
        """身分對不上／已死 → 當場停手，不可以退回用 PID 集合找一個來用。"""
        monkeypatch.setattr(cq, "_session_death_reason",
                            lambda _s: "主畫面 hwnd 已被回收給別的視窗")
        posted = []
        monkeypatch.setattr(cq.win32gui, "PostMessage",
                            lambda h, m, w, l: posted.append(h))

        try:
            cq._query_cycle(_Sess(), {}, "今日會診病人")
        except RuntimeError as e:
            assert "session 已不可用" in str(e)
            assert posted == [], "身分不明卻還是送了命令"
            return
        raise AssertionError("身分不明卻照常執行查詢")


class TestTheConsultWindowIsBoundToThisCommand:

    def _run(self, monkeypatch, consult_windows_over_time):
        """consult_windows_over_time：每次 find_windows(CONSULT_CLASS) 回什麼。"""
        _healthy(monkeypatch)
        seq = list(consult_windows_over_time)
        seen = {"n": 0}

        def _find(cls=None, pids=None, **k):
            if cls == cq.MAIN_CLASS:
                return [DOCS_MAIN, OURS_MAIN]
            if cls == cq.CONSULT_CLASS:
                cur = seq[min(seen["n"], len(seq) - 1)]
                seen["n"] += 1
                # 生產行為：pids 過濾會把不屬於該 pid 的視窗濾掉
                if pids is not None:
                    cur = [h for h in cur if _owner_of(h) in pids]
                return list(cur)
            return []
        monkeypatch.setattr(cq, "find_windows", _find)
        monkeypatch.setattr(cq, "resolve_menu_command_id", lambda _h: 42)
        monkeypatch.setattr(cq.win32gui, "PostMessage", lambda *a: None)
        _fast_clock(monkeypatch)
        got = {}

        def _capture(h, **_k):
            got["consult"] = h
            return "IMG", cq._RosterSnapshot([], True, [], [])
        monkeypatch.setattr(cq, "_capture_with_settled_roster", _capture)
        monkeypatch.setattr(cq, "_extract_consult_text",
                            lambda *a, **k: ("", "", []))
        monkeypatch.setattr(cq, "_return_to_main", lambda *a: None)
        cq._query_cycle(_Sess(), {}, "今日會診病人")
        return got.get("consult")

    def test_a_preexisting_consult_window_is_not_taken(self, monkeypatch):
        """★醫師自己已經開著一張會診單 → 不可以把它當成本輪結果★

        舊寫法 `hits[0]` 會直接撿到它 —— 我們會擷取【他螢幕上】的病人清單、
        截圖、寄給整組人。
        """
        stale, ours = 7001, 7002
        globals()["_OWNERS"] = {stale: OUR_PID, ours: OUR_PID}
        picked = self._run(monkeypatch, [[stale], [stale], [stale, ours]])
        assert picked == ours, (
            f"★撿到命令送出前就存在的那張會診單★(hwnd={picked})")

    def test_a_window_from_another_process_is_ignored(self, monkeypatch):
        """屬於別的行程（醫師那個 HIS）的會診視窗不可以被採用。"""
        doc_consult, ours = 7101, 7102
        globals()["_OWNERS"] = {doc_consult: DOC_PID, ours: OUR_PID}
        picked = self._run(monkeypatch, [[], [doc_consult], [doc_consult, ours]])
        assert picked == ours, f"採用了別的行程的會診視窗(hwnd={picked})"


_OWNERS: dict = {}


def _owner_of(hwnd):
    return _OWNERS.get(hwnd, OUR_PID)


class TestReturnToMainWatchesOurOwnForm:
    """★[外審第 5 輪 P1-01/P2-02]★ 退場判準只看我們這一張會診單。

    舊判準是「污染的 PID 集合裡看不到任何會診視窗」，兩個方向都會錯：
      * 醫師自己開著一張 → 我們這張明明關掉了，卻永遠等不到「都沒有」
        → 誤判退場失敗 → 收掉一個健康的 session、下一輪重新送帳密
      * 反之亦然（我們這張還在、別人的先消失）不會被發現
    """

    def _prep(self, monkeypatch, *, ours_dismissed, others_present=True):
        monkeypatch.setattr(cq, "find_child", lambda *a: None)
        monkeypatch.setattr(cq.win32gui, "PostMessage", lambda *a: None)
        _fast_clock(monkeypatch)
        monkeypatch.setattr(cq, "_consult_form_dismissed",
                            lambda _h: ours_dismissed)
        # 別人的會診視窗一直都在 —— 舊判準會被它卡死
        monkeypatch.setattr(
            cq, "find_windows",
            lambda *a, **k: [9999] if others_present else [])
        monkeypatch.setattr(cq, "_main_ready_for_next_cycle", lambda _s: "")
        closed = []
        monkeypatch.setattr(cq, "_session_close_if_current",
                            lambda s, r: closed.append(r))
        return closed

    def test_the_doctors_consult_window_does_not_block_us(self, monkeypatch):
        closed = self._prep(monkeypatch, ours_dismissed=True)
        cq._return_to_main(_Sess(), 7002)
        assert closed == [], (
            "★別人的會診視窗害我們誤判退場失敗★ 健康的 session 被收掉、下一輪重送帳密")

    def test_our_form_still_up_is_a_failure(self, monkeypatch):
        """★反方向:我們這張真的關不掉時仍要收掉 session★"""
        closed = self._prep(monkeypatch, ours_dismissed=False,
                            others_present=False)
        cq._return_to_main(_Sess(), 7002)
        assert closed and "關不掉" in closed[0]

    def test_a_blocked_main_is_not_reported_as_ready(self, monkeypatch):
        """★[P2-02]★ 會診單收掉了，但主畫面被 modal 擋住 → 不可以宣稱 session 續留。

        否則要等到下一輪送命令沒反應，才會在更晚、更難查的地方失敗。
        """
        closed = self._prep(monkeypatch, ours_dismissed=True)
        monkeypatch.setattr(cq, "_main_ready_for_next_cycle",
                            lambda _s: "主畫面被 modal 對話框擋住(disabled)")
        cq._return_to_main(_Sess(), 7002)
        assert closed and "主畫面沒回到可操作狀態" in closed[0]


class TestTheDismissPredicateIsTheRightQuestion:
    """會診單這一張表單:「看不見了」＝退回主畫面了（與 session 收尾相反）。"""

    def test_hidden_counts_as_dismissed(self, monkeypatch):
        monkeypatch.setattr(cq.win32gui, "IsWindow", lambda _h: True)
        monkeypatch.setattr(cq.win32gui, "IsWindowVisible", lambda _h: False)
        assert cq._consult_form_dismissed(1) is True

    def test_visible_is_not_dismissed(self, monkeypatch):
        monkeypatch.setattr(cq.win32gui, "IsWindow", lambda _h: True)
        monkeypatch.setattr(cq.win32gui, "IsWindowVisible", lambda _h: True)
        assert cq._consult_form_dismissed(1) is False

    def test_unknown_is_not_dismissed(self, monkeypatch):
        def _boom(_h):
            raise OSError("handle 無效")
        monkeypatch.setattr(cq.win32gui, "IsWindow", _boom)
        assert cq._consult_form_dismissed(1) is False, (
            "查不到卻宣稱已經退回主畫面")


def _ticks(step=0.25):
    t = {"v": 0.0}

    def _now():
        t["v"] += step
        return t["v"]
    return _now


class TestMainReadiness:
    """★[2026-08-05 實機事故] 不可以只取樣一次★

    我第一版在會診單「看不見了」之後【立刻】問一次 `IsWindowEnabled`，
    當天下午三次全部答 disabled → 三次都收掉 session → 三次都關不掉 →
    掛帳閘門把整個會診查詢停掉，使用者重開機也一樣。

        14:52:28,451 擷取完成
        14:52:28,762 會診單已收掉,但主畫面沒回到可操作狀態:被 modal 擋住
        14:52:31,765 ★主畫面關不掉★

    相隔 311 毫秒。Delphi 的 modal form 是【先 Hide、後把 owner 重新 enable】，
    而 `_consult_form_dismissed` 把「看不見」就算退場 —— 剛好卡在那個縫裡。
    """

    def test_a_main_that_reenables_shortly_after_is_ready(self, monkeypatch):
        """★這就是事故的形狀★ 前幾次還是 disabled，稍後就恢復了。"""
        monkeypatch.setattr(cq, "_session_death_reason", lambda _s: "")
        calls = {"n": 0}

        def _enabled(_h):
            calls["n"] += 1
            return calls["n"] > 3          # 前三次 disabled（＝那 311 毫秒）
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled", _enabled)

        assert cq._main_ready_for_next_cycle(
            _Sess(), sleep=lambda _s: None, now=_ticks()) == "", (
            "★只取樣一次就判定★ 健康的 session 會被收掉，然後關不掉、整個停擺")

    def test_a_permanently_disabled_main_is_still_reported(self, monkeypatch):
        """★反方向:真的一直被擋住仍要說出來★（不可以修成永遠回 OK）。"""
        monkeypatch.setattr(cq, "_session_death_reason", lambda _s: "")
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled", lambda _h: False)
        assert "modal" in cq._main_ready_for_next_cycle(
            _Sess(), sleep=lambda _s: None, now=_ticks())

    def test_a_healthy_main_is_ready(self, monkeypatch):
        monkeypatch.setattr(cq, "_session_death_reason", lambda _s: "")
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled", lambda _h: True)
        assert cq._main_ready_for_next_cycle(
            _Sess(), sleep=lambda _s: None, now=_ticks()) == ""

    def test_a_dead_session_is_not_ready(self, monkeypatch):
        monkeypatch.setattr(cq, "_session_death_reason", lambda _s: "主畫面不在")
        assert cq._main_ready_for_next_cycle(_Sess()) == "主畫面不在"

    def test_unknown_enabled_state_is_not_ready(self, monkeypatch):
        monkeypatch.setattr(cq, "_session_death_reason", lambda _s: "")

        def _boom(_h):
            raise OSError("查不到")
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled", _boom)
        assert cq._main_ready_for_next_cycle(
            _Sess(), sleep=lambda _s: None, now=_ticks()) != ""


def test_query_cycle_never_picks_a_main_window_by_enumeration():
    """★接線★ `_query_cycle` 不可以再從列舉結果挑主畫面。

    行為測試證明「命令送到我們那一個」，但若有人把
    `main_hwnd = sess.main_hwnd` 改回 `mains[0]`，只要列舉順序剛好正確，
    行為測試仍可能通過。這裡直接釘住來源。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._query_cycle)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and node.targets
                and getattr(node.targets[0], "id", "") == "main_hwnd"):
            v = node.value
            assert (isinstance(v, ast.Attribute) and v.attr == "main_hwnd"), (
                "★主畫面又是從列舉結果挑的★ 可能送命令給醫師的 HIS")
            return
    raise AssertionError("找不到 main_hwnd 的來源")
