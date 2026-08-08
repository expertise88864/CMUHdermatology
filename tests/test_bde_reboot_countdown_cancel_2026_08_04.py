# -*- coding: utf-8 -*-
"""BDE 自動重開機:倒數期間使用者回來就必須取消（2026-08-04 外審 P1-06）。

【問題】
下達 `shutdown /r /t 60` 之後，原本只有 `_bde_reboot_cancel.wait(55.0)`，而那個
事件只由「HIS 恢復」或「程式退出」設置 —— 倒數期間完全不再看使用者。醫師在這
60 秒內回座打字，機器照樣重開。

整個自動重開機的前提就是「沒有人在用這台電腦」。那個前提在倒數期間隨時可能不
成立，所以要持續驗證，不是進入倒數前驗一次就算數。

【判準：絕對，不是相對】（2026-08-04 外審第 2 輪 P1-01 修正）
第一版用「閒置秒數比執行中的峰值倒退超過容差」——那是【相對】判準，需要一個
乾淨的 baseline，而 baseline 是進入倒數之後才取的第一個樣本。使用者若在「決定
重開」與「第一個樣本」之間回來，baseline 本身就是低的，之後單調上升永遠看不到
倒退 → ★初始取樣競態★。

改用【絕對】判準：自動重開機的前提就是「已閒置滿 30 分鐘」，那個前提在倒數期間
必須持續成立。任何一次量到閒置低於門檻就是有人動過 —— 不需要 baseline、不需要
容差，也就沒有初始取樣競態。

【已知且刻意的盲區】下的是 `/t 60` 但只監測 55 秒 —— 監測滿 60 秒就沒有時間執行
`shutdown /a`。留餘裕是取捨，不是疏漏。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


class _Clock:
    """可控的 monotonic：每次讀取前進 1 秒。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


def _install(monkeypatch, idle_seq, clock):
    """把閒置讀數換成一串腳本；每次呼叫吐一個，並讓時鐘前進 1 秒。"""
    seq = list(idle_seq)
    calls = {"n": 0}

    def _idle():
        calls["n"] += 1
        return seq.pop(0) if seq else (seq[-1] if seq else 0.0)

    monkeypatch.setattr(cq, "_user_idle_seconds_or_none", _idle)
    monkeypatch.setattr(cq.time, "monotonic", clock)
    return calls


def _no_cancel_event(monkeypatch, clock):
    """取消令永遠不會被設起來，但每次 wait 讓時鐘前進（模擬真的等了 1 秒）。"""
    class _Ev:
        def wait(self, timeout=None):
            clock.advance(timeout or 1.0)
            return False

        def is_set(self):
            return False
    monkeypatch.setattr(cq, "_bde_reboot_cancel", _Ev())


def test_user_moving_the_mouse_at_second_10_cancels_the_reboot(monkeypatch):
    """★外審點名的情境★ 倒數剩 10 秒時醫師回來 → 必須偵測到。

    閒置從 1800 一路長到 1845（沒人），第 46 次讀到 0.5（有人動了）。
    """
    clock = _Clock()
    idle = [1800.0 + i for i in range(45)] + [0.5]   # 第 46 次:醫師回來了
    _install(monkeypatch, idle, clock)
    _no_cancel_event(monkeypatch, clock)

    assert cq._await_reboot_countdown(55.0) == "user_back"


def test_nobody_comes_back_so_the_reboot_proceeds(monkeypatch):
    """★反方向:不可以變成永遠不重開★ 沒有人回來就要讓它重開（那是修復動作）。"""
    clock = _Clock()
    idle = [1800.0 + i for i in range(200)]
    _install(monkeypatch, idle, clock)
    _no_cancel_event(monkeypatch, clock)

    assert cq._await_reboot_countdown(55.0) == "elapsed"


def test_input_before_the_first_sample_is_still_caught(monkeypatch):
    """★初始取樣競態★（2026-08-04 外審第 2 輪 P1-01 盲區 A）

    使用者若在「決定重開」與「倒數的第一個取樣」之間回來，第一個樣本就已經是
    0.5 秒。第一版用【相對】判準（比執行中的峰值倒退超過容差），此時 baseline
    本身就是低的，之後閒置從 0.5 單調上升、永遠看不到倒退 —— 機器照樣重開。

    改用【絕對】判準（閒置是否仍達 30 分鐘門檻）就沒有這個競態：第一個樣本
    0.5 秒就已經低於門檻。
    """
    clock = _Clock()
    idle = [0.5 + i for i in range(200)]      # 使用者已經回來了，之後才開始取樣
    _install(monkeypatch, idle, clock)
    _no_cancel_event(monkeypatch, clock)

    assert cq._await_reboot_countdown(55.0) == "user_back", (
        "★初始取樣就是低值時偵測不到 → 機器在有人用的時候重開★")


def test_a_user_who_returns_and_stops_touching_is_still_caught(monkeypatch):
    """使用者回來點一下就不動了 —— 閒置從 0 重新單調上升。

    相對判準在這種情況下同樣看不到倒退（審查點名的第三個情境）。
    絕對判準只要閒置還沒重新累積到 30 分鐘，就一直算「有人在」。
    """
    clock = _Clock()
    idle = [1800.0, 1801.0, 0.2] + [1.2 + i for i in range(200)]
    _install(monkeypatch, idle, clock)
    _no_cancel_event(monkeypatch, clock)

    assert cq._await_reboot_countdown(55.0) == "user_back"


def test_an_unreadable_idle_time_is_not_reported_as_a_user_return(monkeypatch):
    """★措辭鐵律★ 查不出來 ≠ 量到使用者回來。

    處置相同（都取消重開），但說法必須不同 —— 否則事後看 log 無從分辨
    機器為什麼沒被修好。
    """
    clock = _Clock()
    idle = [1800.0, 1801.0, None]
    _install(monkeypatch, idle, clock)
    _no_cancel_event(monkeypatch, clock)

    assert cq._await_reboot_countdown(55.0) == "idle_unknown"


def test_an_unreadable_idle_time_at_the_very_start_also_stops_it(monkeypatch):
    """一開始就查不出來 → 保守當作可能有人在，不重開。"""
    clock = _Clock()
    _install(monkeypatch, [None], clock)
    _no_cancel_event(monkeypatch, clock)

    assert cq._await_reboot_countdown(55.0) == "idle_unknown"


def test_the_his_recovered_path_still_works(monkeypatch):
    """既有行為不可以被弄壞:HIS 恢復仍要回報 cancelled。"""
    clock = _Clock()
    _install(monkeypatch, [1800.0 + i for i in range(200)], clock)

    class _Ev:
        def wait(self, timeout=None):
            clock.advance(timeout or 1.0)
            return True

        def is_set(self):
            return True
    monkeypatch.setattr(cq, "_bde_reboot_cancel", _Ev())

    assert cq._await_reboot_countdown(55.0) == "cancelled"


def test_the_idle_helper_reports_failure_as_none_not_zero(monkeypatch):
    """`_user_idle_seconds_or_none` 失敗要回 None；相容包裝才回 0.0。

    0.0 的意思是「剛剛才有輸入」——拿它當「查不到」用，就是在說程式不知道的事。

    ★用行為驗，不要掃原始碼★（突變驗證抓到）：原本只斷言原始碼裡有
    `return None`，而那個函式有兩個 return None（查詢失敗、例外）——把其中一個
    改成 0.0，另一個仍然餵飽斷言，測試照樣全綠。
    """
    class _FakeUser32:
        def GetLastInputInfo(self, _p):
            return 0                       # Win32:0 = 查詢失敗

    monkeypatch.setattr(cq, "_user32", _FakeUser32())
    assert cq._user_idle_seconds_or_none() is None, "查詢失敗必須回 None"
    assert cq._user_idle_seconds() == 0.0, "相容包裝要維持舊語意(當作剛有輸入)"


def test_the_watch_loop_actually_uses_the_countdown_helper():
    """★接線本身也要被測到★（突變驗證抓到）

    上面每一支都直接呼叫 `_await_reboot_countdown`，所以就算 watch loop 改回
    「只等取消令」，它們照樣全綠 —— 而那正是 bug 還在的樣子。倒數那段沒辦法
    在測試裡跑（會真的下 shutdown 指令），所以用 AST 檢查它確實被呼叫。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._bde_reboot_watch_loop)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_await_reboot_countdown" in called, (
        "watch loop 沒有用倒數守衛 → 倒數期間不會偵測使用者（bug 原樣）")


def test_countdown_outcomes_all_have_a_distinct_message():
    """三種取消原因在 log 裡要講得出差別（不可以共用同一句）。

    ★措辭鐵律★ 三種原因的【處置】相同（都取消重開），但事後看 log 必須分得出
    是「HIS 自己好了」「有人回來了」還是「根本量不到」—— 否則無從判斷這台機器
    為什麼沒被修好。
    """
    reasons = cq._COUNTDOWN_ABORT_REASONS
    assert set(reasons) == {"cancelled", "user_back", "idle_unknown"}, (
        f"取消原因的集合變了：{set(reasons)}")
    assert len(set(reasons.values())) == 3, (
        f"三種原因沒有各自的說法：{reasons}")
    assert all(v.strip() for v in reasons.values()), "有空白的說法"


def test_elapsed_is_never_an_abort_reason():
    """★這張表決定機器會不會被修好★

    「倒數走完」不在表裡＝不取消＝讓它重開，那正是自動修復本身。哪天有人把
    `elapsed` 加進來，自動重開機就從此不會發生，而且不會有任何錯誤訊息。
    """
    assert "elapsed" not in cq._COUNTDOWN_ABORT_REASONS, (
        "★倒數走完被列成取消原因★ 自動修復永遠不會發生")


class _Result:
    def __init__(self, rc):
        self.returncode = rc


class TestBothDirectionsOfTheAbortDecision:
    """★修法不可以比 bug 更糟★（突變驗證抓到的缺口）

    只守「該取消要取消」是不夠的：把 `elapsed` 的早退拿掉會變成【一律取消】，
    自動重開機修復從此永遠不會發生 —— 那是把一個 fail-open 修成同樣有害的
    fail-closed，而且沒有任何測試會發現。
    """

    def _spy(self):
        calls = []

        def _run(cmd):
            calls.append(list(cmd))
            return _Result(0)
        return calls, _run

    def test_nobody_came_back_so_it_lets_the_machine_reboot(self):
        calls, run = self._spy()
        rolled = []

        did = cq._abort_reboot_if_needed(
            "elapsed", rollback=rolled.append, run=run)

        assert did is False
        assert calls == [], f"★倒數走完卻去取消★ 自動修復永遠不會發生：{calls}"
        assert rolled == [], "沒有取消就不該回滾時間戳"

    def test_the_user_came_back_so_it_cancels_and_rolls_back(self):
        calls, run = self._spy()
        rolled = []

        did = cq._abort_reboot_if_needed(
            "user_back", rollback=rolled.append, run=run)

        assert did is True
        assert calls == [["shutdown", "/a"]], f"沒有真的取消：{calls}"
        assert len(rolled) == 1, "取消了就要回滾時間戳(沒真的重開,別吃掉防護)"
        assert "使用者回來" in rolled[0], f"回滾理由說不清楚：{rolled[0]}"

    def test_an_unknown_idle_time_also_cancels(self):
        calls, run = self._spy()
        did = cq._abort_reboot_if_needed(
            "idle_unknown", rollback=lambda _w: None, run=run)
        assert did is True and calls == [["shutdown", "/a"]]

    def test_his_recovery_still_cancels(self):
        calls, run = self._spy()
        did = cq._abort_reboot_if_needed(
            "cancelled", rollback=lambda _w: None, run=run)
        assert did is True and calls == [["shutdown", "/a"]]

    def test_a_failed_cancel_is_reported_as_not_cancelled(self, caplog):
        """★措辭鐵律★ shutdown /a 失敗 = 機器仍會重開，不可以回報成取消了。"""
        import logging as _lg
        rolled = []
        with caplog.at_level(_lg.CRITICAL):
            did = cq._abort_reboot_if_needed(
                "user_back", rollback=rolled.append,
                run=lambda _cmd: _Result(1))

        assert did is False, "取消失敗卻回報成功"
        assert rolled == [], "沒真的取消就不可以回滾(機器等一下真的會重開)"
        assert any("仍會重開" in r.message for r in caplog.records), \
            "取消失敗要留下 critical 痕跡"

    def test_an_exception_is_also_reported_as_not_cancelled(self):
        def _boom(_cmd):
            raise OSError("shutdown 不存在")
        rolled = []
        did = cq._abort_reboot_if_needed(
            "user_back", rollback=rolled.append, run=_boom)
        assert did is False and rolled == []


class TestThereIsNoUnwatchedWindow:
    """★[2026-08-08 外審]★ 舊版下 `/t 60` 卻只監測 55 秒 —— 最後 5 秒不再讀
    idle time。醫師若在那時回座打字，機器照重開。

    那是一個「用停止偵測換來的安全窗」，而它換掉的正是這個功能唯一的前提：
    沒有人在用這台電腦。餘裕要從【OS 的倒數】拿，不是從【監測時間】扣。
    """

    def test_the_os_countdown_is_longer_than_the_watch(self):
        assert (cq._REBOOT_COUNTDOWN_SEC
                == cq._REBOOT_WATCH_SEC + cq._REBOOT_CANCEL_MARGIN_SEC)
        assert cq._REBOOT_CANCEL_MARGIN_SEC > 0, "還是要留時間執行 shutdown /a"

    def test_the_whole_watch_window_is_monitored(self):
        """★核心★ 監測秒數不可以再從倒數裡扣 —— 那就是那個盲區。"""
        import ast
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(cq))
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_await_reboot_countdown"):
                arg = n.args[0] if n.args else None
                assert isinstance(arg, ast.Name) and arg.id == "_REBOOT_WATCH_SEC", (
                    "★監測時間又被扣掉餘裕了★ 最後那幾秒偵測不到使用者")
                return
        pytest.fail("找不到 _await_reboot_countdown 呼叫")

    def test_no_shutdown_command_hardcodes_the_countdown(self):
        """★排定的那一次必須用常數★(立即重開的 `/t 0` 例外)

        ★這個測試第一版是「找到第一個 shutdown 就 return」★ —— 而檔案裡有
        三條 shutdown 指令(排定重開／取消／立即重開)。第一版先撞到取消指令,
        後來又先撞到立即重開,兩次都在量錯的東西。改成檢查【全部】。
        """
        import ast
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(cq))
        found = 0
        for n in ast.walk(ast.parse(src)):
            if not isinstance(n, ast.List):
                continue
            consts = [e.value for e in n.elts if isinstance(e, ast.Constant)]
            if "shutdown" not in consts or "/t" not in consts:
                continue
            found += 1
            dump = ast.dump(n)
            assert ("_REBOOT_COUNTDOWN_SEC" in dump
                    or "/t', ctx=Load()), Constant(value='0')" in dump
                    or "value='0'" in dump), (
                f"★shutdown 的倒數秒數寫死了★:{consts}")
        assert found >= 2, (
            f"預期至少兩條帶 /t 的 shutdown(排定 + 立即),只找到 {found}")

    def test_the_final_decision_happens_after_cancelling(self):
        """★核心(第 2 回)★ 把 OS 倒數拉長只是把盲區搬家 —— 第 60~65 秒
        一樣沒有人在看。真正消滅它:監測期滿【先取消】排定的重開,
        再取最後一次 idle 樣本,確認仍然沒人才下立即重開。
        """
        import ast
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(cq._finish_bde_reboot))
        tree = ast.parse(src)
        cancel_line = idle_line = None
        for n in ast.walk(tree):
            if (isinstance(n, ast.List)
                    and [e.value for e in n.elts
                         if isinstance(e, ast.Constant)] == ["shutdown", "/a"]):
                cancel_line = n.lineno
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_user_idle_seconds_or_none"):
                idle_line = n.lineno
        assert cancel_line and idle_line, f"{cancel_line} {idle_line}"
        assert cancel_line < idle_line, (
            "★最後一次 idle 取樣排在取消之前★ 那代表取樣之後仍有一段"
            "沒有人看守的時間,使用者在那時回來照樣被重開")

    def test_an_unknown_idle_at_the_last_moment_does_not_reboot(self,
                                                               monkeypatch):
        """★查不出來就當作有人★(與倒數期間同一個保守方向)

        ★stub 要用生產的呼叫形狀★(外審第 3 回抓到)生產的回呼是
        `_rollback_ts(why, ...)` —— 單引數。我第一版用 `lambda: None`,
        於是「呼叫端漏傳引數」這個缺陷被測試自己蓋掉了:實際上會 TypeError、
        被吞掉、時間戳沒回滾,自動修復被壓住 24 小時。
        """
        cmds = []
        _rb = []
        monkeypatch.setattr(cq, "_user_idle_seconds_or_none", lambda: None)
        cq._finish_bde_reboot(
            "elapsed", rollback=_rb.append,
            run=lambda cmd: cmds.append(cmd) or type("R", (), {"returncode": 0})())
        assert cmds == [["shutdown", "/a"]], (
            f"★最後一刻查不出閒置時間卻仍然重開★:{cmds}")

    def test_a_user_returning_at_the_last_moment_does_not_reboot(self,
                                                                 monkeypatch):
        cmds = []
        _rb = []
        monkeypatch.setattr(cq, "_user_idle_seconds_or_none", lambda: 3.0)
        cq._finish_bde_reboot(
            "elapsed", rollback=_rb.append,
            run=lambda cmd: cmds.append(cmd) or type("R", (), {"returncode": 0})())
        assert cmds == [["shutdown", "/a"]], (
            f"★使用者在最後一刻回來卻仍然重開★:{cmds}")

    def test_a_still_idle_machine_reboots_immediately(self, monkeypatch):
        """★反方向★ 一路都沒人 → 仍然要重開(否則 BDE 壞了就一直壞著)。"""
        cmds = []
        _rb = []
        monkeypatch.setattr(cq, "_user_idle_seconds_or_none", lambda: 99999.0)
        cq._finish_bde_reboot(
            "elapsed", rollback=_rb.append,
            run=lambda cmd: cmds.append(cmd) or type("R", (), {"returncode": 0})())
        assert cmds[0] == ["shutdown", "/a"]
        assert cmds[1][:4] == ["shutdown", "/r", "/t", "0"], (
            f"★沒有下立即重開★:{cmds}")

    def test_the_timestamp_is_rolled_back_with_a_reason(self, monkeypatch):
        """★核心(第 3 回)★ 生產的回呼是 `_rollback_ts(why, ...)` —— 單引數。
        用 `rollback()` 呼叫會 TypeError,而它又被吞掉:時間戳沒回滾,
        自動修復被壓住 24 小時(BDE 壞了就一直壞著)。"""
        rolled = []
        monkeypatch.setattr(cq, "_user_idle_seconds_or_none", lambda: 3.0)
        cq._finish_bde_reboot(
            "elapsed", rollback=lambda why: rolled.append(why),
            run=lambda cmd: type("R", (), {"returncode": 0})())
        assert rolled and isinstance(rolled[0], str), (
            f"★沒有回滾時間戳(或沒帶原因)★:{rolled}")
