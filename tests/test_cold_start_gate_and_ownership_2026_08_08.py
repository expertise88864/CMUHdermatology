# -*- coding: utf-8 -*-
"""外審第 9 輪（HEAD 現況 · session 生命週期）三個 CONFIRMED finding。

【P1-01】UNMANAGED 閘門要涵蓋【每一條】冷啟動路徑。
舊寫法把閘門接在 `_acquire_session()` 的冷啟動分支,而【掉線恢復】那條路是
直接呼叫 `_cold_start_session()` 的 —— 偏偏那正是「剛剛才關不掉一個 session」
的時刻。而且閘門【只能查一次】:超過 15 分鐘上限的分支會「放行一次並重新
起算」,查兩次的話第二次 `blocked_for≈0` 會把剛放行的那次擋回去。

【P1-02】SW_HIDE 後備模式用「比我們晚出現的 pid」冒充所有權。
醫師在那 120 秒內自己開住院系統也完全符合這個條件,於是我們會:把自動化帳密
打進他的登入視窗、把他的視窗移到螢幕外、開會診單、擷取全院病人資料寄出去。
這條路跑在【醫師自己的桌面】上,所以差集裡的外來 pid 會真的貢獻出視窗
(隱藏桌面那條路不會,因為 `find_windows` 只列舉我們自己的桌面 —— 這是兩條
路徑的關鍵不對稱,不能因為「隱藏桌面那邊沒事」就以為這邊也沒事)。

【P2-01】送查詢命令前沒有確認主畫面 enabled。
`_session_death_reason` 只看 hwnd 還在、身分相符;主畫面被閒置提示擋住時是
disabled 的,判活會過,但 disabled 的視窗不會處理 WM_COMMAND。命令像是送出去
了 → 等滿 60 秒 → 走恢復路徑拆掉一個【按一下就好】的 session。與 2026-08-05
實機事故同一個形狀,只是從退場換到進場。
"""
import ast
import inspect
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402

OUR_PID = 4321
ALIEN_PID = 9999          # 醫師自己開的那個


class _Sess:
    pid = OUR_PID
    our_pids = {OUR_PID}
    main_hwnd = 5001
    main_pid = OUR_PID
    main_class = cq.MAIN_CLASS
    main_proc_started = None
    hproc = object()
    in_use = False
    started_at = 0.0


# ===========================================================================
# P1-01 冷啟動閘門
# ===========================================================================
class TestEveryColdStartPassesTheGate:

    def _stub_cold_start_impl(self, monkeypatch, calls):
        monkeypatch.setattr(cq, "_cold_start_session_impl",
                            lambda *a, **k: calls.append("cold") or _Sess())

    def test_the_recovery_path_is_also_blocked(self, monkeypatch):
        """★核心★ 查詢失敗 → 收不掉 → 掛帳 → 【不可以】立刻再登一個。"""
        calls = []
        self._stub_cold_start_impl(monkeypatch, calls)
        monkeypatch.setattr(cq, "_acquire_session", lambda _c: _Sess())
        monkeypatch.setattr(
            cq, "_query_cycle",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("查詢失敗")))
        monkeypatch.setattr(cq, "_session_close_if_current",
                            lambda *a, **k: True)      # 仍是現任 → 會想重登
        monkeypatch.setattr(
            cq, "_ensure_no_unmanaged_sessions",
            lambda **k: (_ for _ in ()).throw(
                cq.UnmanagedSessionError("還有 1 個關不掉")))
        cq.running.set()
        with pytest.raises(cq.UnmanagedSessionError):
            cq._automation_on_hidden({}, "今日會診病人")
        assert not calls, (
            "★掉線恢復繞過了閘門★ 剛剛才關不掉一個 session,卻立刻又登入一個")

    def test_the_gate_is_consulted_exactly_once_per_cold_start(self,
                                                               monkeypatch):
        """★不可以查兩次★ 15 分鐘上限那條分支是「放行一次、重新起算」——
        查第二次時 `blocked_for≈0`,剛放行的那一次會被自己擋回去。"""
        calls = []
        self._stub_cold_start_impl(monkeypatch, [])
        monkeypatch.setattr(cq, "_ensure_no_unmanaged_sessions",
                            lambda **k: calls.append(k.get("block", True)))
        monkeypatch.setattr(cq, "_psession", None, raising=False)
        cq._acquire_session({})
        blocking = [c for c in calls if c is not False]
        assert len(blocking) == 1, (
            f"★阻擋型閘門被查了 {len(blocking)} 次★(calls={calls}) "
            "每多查一次,就多一次把『放行一次』吃掉的機會")

    def test_cold_start_itself_is_the_gate(self):
        """★接線★ 閘門必須在 `_cold_start_session` 裡,不能只在呼叫端 ——
        呼叫端有兩個(`_acquire_session` 與掉線恢復),漏一個就等於沒有。"""
        src = textwrap.dedent(inspect.getsource(cq._cold_start_session))
        names = {n.func.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_ensure_no_unmanaged_sessions" in names, (
            "★冷啟動的唯一入口沒有閘門★ 掛在呼叫端的閘門一定會被繞過")


# ===========================================================================
# P1-02 SW_HIDE 後備模式的所有權
# ===========================================================================
def _drive_fallback(monkeypatch, *, login_pids, root_alive=True):
    """跑到「挑登入視窗」為止。回傳 (被輸入帳密的 hwnd, 被藏起來的 pid 集合)。

    `login_pids`: {hwnd: pid} —— 畫面上看得到的登入視窗。
    """
    hidden_pids = set()
    typed = {}

    class _Popen:
        pid = OUR_PID

        def poll(self):
            return None if root_alive else 0
    monkeypatch.setattr(cq.subprocess, "Popen", lambda *a, **k: _Popen())
    # ★`before` 要是空的★(啟動前機器上沒有 systemftp)。這是本輪反例能不能
    #   量到東西的關鍵:醫師那個行程必須是【我們啟動之後才出現】的,舊判準
    #   (`pid not in before`)才會把它算成自己人。若讓醫師那個 pid 落在
    #   `before` 裡,舊判準自己就先排除掉了 —— 反例會被前置條件擋住,
    #   把突變放回去測試照樣綠,等於什麼都沒測到。
    seen = {"n": 0}

    def _pids():
        seen["n"] += 1
        return set() if seen["n"] == 1 else {OUR_PID, ALIEN_PID}
    monkeypatch.setattr(cq, "_systemftp_pids", _pids)
    monkeypatch.setattr(cq, "_window_pid", lambda h: login_pids.get(h, -1))
    monkeypatch.setattr(cq.win32gui, "GetForegroundWindow", lambda: 0)
    # 隱形執行緒:不要真的開 thread,直接把它的 body 跑一次就好
    started = {}

    class _Thread:
        def __init__(self, target=None, **k):
            started["target"] = target

        def start(self):
            pass
    monkeypatch.setattr(cq.threading, "Thread", _Thread)

    def _find(cls=None, title=None, pids=None, **k):
        if cls == cq.LOGIN_CLASS:
            return list(login_pids)
        if cls is None:                       # 隱形執行緒那一個呼叫
            return [h for h, p in login_pids.items() if p in (pids or set())]
        return []
    monkeypatch.setattr(cq, "find_windows", _find)
    monkeypatch.setattr(cq, "find_child", lambda *a, **k: None)
    monkeypatch.setattr(cq, "hide_window",
                        lambda h: hidden_pids.add(login_pids.get(h, -1)))
    monkeypatch.setattr(cq, "show_offscreen",
                        lambda h: typed.setdefault("hwnd", h))
    monkeypatch.setattr(cq, "force_foreground", lambda h: True)
    monkeypatch.setattr(cq, "enum_children", lambda h: [])
    monkeypatch.setattr(cq, "_cleanup_pids_excluding_borrowed",
                        lambda our, before, borrowed, root_pid=None: set())
    monkeypatch.setattr(cq, "close_pids", lambda p: None)
    monkeypatch.setattr(cq.time, "sleep", lambda _s: None)
    ticks = {"v": 0.0}
    monkeypatch.setattr(
        cq.time, "time",
        lambda: ticks.__setitem__("v", ticks["v"] + 20.0) or ticks["v"])
    cq.running.set()
    try:
        cq._run_with_sw_hide({"username": "u", "password": "p"}, "今日會診病人")
    except Exception:
        pass                                   # 一定會在後面某處失敗,不是重點
    # 隱形執行緒的 body 跑一輪(它自己是 while 迴圈,這裡只取它的 find_windows 參數)
    return typed.get("hwnd"), started.get("target"), hidden_pids


class TestTheFallbackNeverTouchesTheDoctorsHIS:

    def test_a_newer_pid_is_not_proof_of_ownership(self, monkeypatch):
        """★核心★ 我們的行程還活著、登入視窗還沒出來;醫師這 120 秒內自己
        開了一個 —— 它的 pid「比我們晚出現」,完全符合舊判準。於是我們會把
        自動化帳密打進他的登入視窗。

        ★這個反例刻意讓我們的行程【活著】★ 用「行程已結束」當反例的話,
        會先被 `poll()` 那道前置守衛擋掉,量到的就不是這條判準了。"""
        hwnd, _t, _h = _drive_fallback(
            monkeypatch, login_pids={7100: ALIEN_PID}, root_alive=True)
        assert hwnd is None, (
            "★把帳密打進醫師自己的住院系統★ "
            f"挑上了 hwnd={hwnd}(pid={ALIEN_PID}),那不是我們開的行程")

    def test_a_recycled_pid_must_not_pass_as_ours(self, monkeypatch):
        """★只比對 pid 還不夠★ 我們那個行程一結束,核心就可以把它的 pid 配給
        別人 —— 醫師接著開的住院系統剛好拿到同一個號碼,pid 比對就會過。
        所以「我們的行程還活著」是比對 pid 的前提,不是額外的保險。
        (2026-07-27 事故的同一條教訓:pid 不是身分,除非你握著它。)"""
        hwnd, _t, _h = _drive_fallback(
            monkeypatch, login_pids={7100: OUR_PID}, root_alive=False)
        assert hwnd is None, (
            "★我們的行程已經結束,卻仍然認了一個 pid 相同的視窗★ "
            "那個號碼可能已經被配給醫師剛開的住院系統")

    def test_our_own_login_window_is_still_accepted(self, monkeypatch):
        """★不可以連正常情況一起擋掉★ 沒有委派時,登入視窗確實屬於我們。"""
        hwnd, _t, _h = _drive_fallback(
            monkeypatch, login_pids={7100: OUR_PID}, root_alive=True)
        assert hwnd == 7100, "我們自己開的登入視窗反而被擋掉了"

    def test_an_alien_window_is_skipped_even_when_ours_is_present(
            self, monkeypatch):
        """兩個都在畫面上時,要挑我們的那一個。"""
        hwnd, _t, _h = _drive_fallback(
            monkeypatch, login_pids={7100: ALIEN_PID, 7200: OUR_PID},
            root_alive=True)
        assert hwnd == 7200, f"挑錯了(hwnd={hwnd})"

    def test_the_stealth_thread_only_hides_our_own_process(self, monkeypatch):
        """★第二個傷害面★ 隱形執行緒每 80 毫秒把「比我們晚出現的」視窗藏起來
        —— 醫師開的那個會被我們一直藏,他根本沒辦法用。"""
        _hwnd, target, _h = _drive_fallback(
            monkeypatch, login_pids={7100: OUR_PID}, root_alive=True)
        assert target is not None, "找不到隱形執行緒的 body"
        src = textwrap.dedent(inspect.getsource(target))
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "find_windows"):
                kw = {k.arg: k.value for k in n.keywords}
                pids = kw.get("pids")
                assert pids is not None and isinstance(pids, ast.Set), (
                    "★隱形執行緒仍用全機差集挑視窗★ 會藏到醫師自己的住院系統")
                assert any(isinstance(e, ast.Name)
                           and e.id == "spawned_root_pid" for e in pids.elts), (
                    "隱形執行緒藏的不是【我們啟動的那個行程】的視窗")

    def test_the_cleanup_fallback_is_not_the_machine_wide_diff(self):
        """提早中止(還沒認出登入視窗)時,收尾不可以拿全機差集去關 ——
        會把醫師剛開的住院系統一起關掉。"""
        src = textwrap.dedent(inspect.getsource(cq._run_with_sw_hide))
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_cleanup_pids_excluding_borrowed"):
                first = n.args[0]
                assert isinstance(first, ast.BoolOp), first
                fallback = first.values[-1]
                assert isinstance(fallback, ast.Set), (
                    "★收尾的 fallback 仍是全機差集★ 會關掉醫師自己開的實例")
                return
        pytest.fail("找不到收尾的 _cleanup_pids_excluding_borrowed")


# ===========================================================================
# P2-01 送命令前主畫面要可操作
# ===========================================================================
def _drive_cycle(monkeypatch, enabled_seq, dismissed):
    """回傳 (是否送出 WM_COMMAND, 是否拋錯)。"""
    posted = []
    seq = list(enabled_seq)
    monkeypatch.setattr(cq, "_session_death_reason", lambda _s: "")
    monkeypatch.setattr(cq.win32gui, "IsWindowEnabled",
                        lambda _h: seq.pop(0) if len(seq) > 1 else seq[0])
    monkeypatch.setattr(cq, "_dismiss_blocking_modals",
                        lambda *a, **k: dismissed)
    monkeypatch.setattr(cq, "resolve_menu_command_id", lambda _h: 42)
    monkeypatch.setattr(cq.win32gui, "PostMessage",
                        lambda *a: posted.append(a))
    monkeypatch.setattr(cq, "find_windows", lambda *a, **k: [])
    monkeypatch.setattr(cq.win32gui, "IsWindowVisible", lambda _h: False)
    monkeypatch.setattr(cq.time, "sleep", lambda _s: None)
    # ★monotonic 必須會前進★ `_main_ready_for_next_cycle` 是【輪詢】到 deadline
    #   為止的(2026-08-05 事故的修法)。釘死成常數 → 那個迴圈永遠不結束。
    m = {"v": 0.0}
    monkeypatch.setattr(cq.time, "monotonic",
                        lambda: m.__setitem__("v", m["v"] + 1.0) or m["v"])
    t = {"v": 1000.0}
    monkeypatch.setattr(cq.time, "time",
                        lambda: t.__setitem__("v", t["v"] + 30.0) or t["v"])
    raised = False
    try:
        cq._query_cycle(_Sess(), {}, "今日會診病人")
    except RuntimeError:
        raised = True
    return bool(posted), raised


class TestTheMainWindowMustBeOperableBeforeWeSendTheCommand:

    def test_no_command_is_posted_into_a_disabled_window(self, monkeypatch):
        """★核心★ disabled 的視窗不會處理 WM_COMMAND。送進去只會白等 60 秒,
        然後拆掉一個其實還好好的 session。"""
        posted, raised = _drive_cycle(monkeypatch, [False], dismissed=0)
        assert not posted, (
            "★對 disabled 的主畫面送出了查詢命令★ 它不會處理,60 秒後這個"
            "本來按一下就能恢復的 session 會被拆掉")
        assert raised, "既然不能操作,就要當場中止讓恢復路徑接手"

    def test_a_modal_is_dismissed_and_then_the_cycle_proceeds(self,
                                                              monkeypatch):
        """★不可以直接放棄★ 按掉 modal 就好的情況要能繼續 ——
        「一律中止」會把 2026-08-05 那次事故換一個位置重演。"""
        posted, _raised = _drive_cycle(monkeypatch, [False, True], dismissed=1)
        assert posted, "按掉 modal、主畫面已回復可操作,卻仍然沒送出命令"

    def test_an_operable_main_window_is_not_disturbed(self, monkeypatch):
        """一開始就可操作 → 不需要去按任何東西。"""
        called = []
        monkeypatch.setattr(cq, "_dismiss_blocking_modals",
                            lambda *a, **k: called.append(1) or 0)
        posted, _raised = _drive_cycle(monkeypatch, [True], dismissed=0)
        assert posted, "主畫面可操作卻沒送出命令"
