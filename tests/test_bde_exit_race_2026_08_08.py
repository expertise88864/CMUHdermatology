# -*- coding: utf-8 -*-
"""[外審 P2-01] 「退出時取消重開」與「倒數收尾」會交錯，結果是機器照樣重開。

`_finish_bde_reboot("elapsed")` 的流程是：
    ① `shutdown /a` 取消【排定】的那一次
    ② 取最後一次 idle 樣本
    ③ 仍然沒人 → `shutdown /r /t 0` 立即重開

①→③ 之間，作業系統其實**沒有**排定中的重開機。使用者若剛好在這個窗口按退出：
`_abort_bde_shutdown_on_exit` 看到 `_bde_shutdown_pending` 還是 True、跑一個取消
不到任何東西的 `shutdown /a`（rc!=0，log 寫「機器仍將重開」），然後 ③ 照樣執行
—— **使用者剛剛把程式關掉，機器立刻重開。**

修法：③ 之前拿 `_bde_watch_lock` 並【再看一次】`_bde_reboot_cancel`。
退出那側整段（含它的 `shutdown /a`）也在同一把鎖裡，兩者不能交錯。

★兩個方向都要守★
  * 該擋沒擋 = 使用者關掉程式後診間電腦仍然重開。
  * 不該擋卻擋 = BDE 壞了永遠自動修不好（把 fail-open 修成一樣有害的 fail-closed）。
"""
import importlib
import threading

import pytest

cq = importlib.import_module("consult_query")


class _R:
    def __init__(self, rc=0):
        self.returncode = rc


@pytest.fixture(autouse=True)
def _clean_bde_state():
    """★每個測試都要從乾淨的旗標開始★

    `_bde_reboot_cancel` 是模組層的 Event。別的測試設過而沒清，這裡的
    「反方向」測試（仍然要重開）就會因為別人的殘留而假綠。
    """
    cq._bde_reboot_cancel.clear()
    with cq._bde_watch_lock:
        cq._bde_shutdown_pending = False
    yield
    cq._bde_reboot_cancel.clear()
    with cq._bde_watch_lock:
        cq._bde_shutdown_pending = False


def _finish(monkeypatch, idle=99999.0, cancel=False):
    cmds = []
    rolled = []
    monkeypatch.setattr(cq, "_user_idle_seconds_or_none", lambda: idle)
    if cancel:
        cq._bde_reboot_cancel.set()
    cq._finish_bde_reboot("elapsed", rollback=rolled.append,
                          run=lambda cmd: cmds.append(cmd) or _R(0))
    return cmds, rolled


def test_a_cancel_set_before_the_last_step_stops_the_reboot(monkeypatch):
    """★核心★ 取消令已經設起來 → 就算閒置條件成立也不可以下立即重開。"""
    cmds, rolled = _finish(monkeypatch, cancel=True)
    assert cmds == [["shutdown", "/a"]], (
        f"★退出已經設了取消令，機器仍然被重開★：{cmds}")
    assert rolled, "沒重開就要回滾時間戳，否則自動修復被壓住 24 小時"


def test_without_a_cancel_it_still_reboots(monkeypatch):
    """★反方向★ 沒有取消令 → 照樣重開（BDE 壞了就是要靠重開修）。"""
    cmds, rolled = _finish(monkeypatch, cancel=False)
    assert cmds[0] == ["shutdown", "/a"]
    assert cmds[1][:4] == ["shutdown", "/r", "/t", "0"], (
        f"★把 fail-open 修成 fail-closed：自動修復永遠不會發生★：{cmds}")
    assert rolled == [], "真的重開了就不該回滾"


def test_the_lock_is_held_while_the_reboot_is_issued(monkeypatch):
    """★真正的競態在這裡★ 「檢查取消令」與「下重開令」必須是一個不可分割的動作。

    只靠旗標還差最後一小段：檢查完、還沒 `/r` 之前使用者按退出，旗標設得再早
    也來不及。所以下 `/r` 的當下必須持著 `_bde_watch_lock` —— 而退出那側整段
    （含它的 `shutdown /a`）也在同一把鎖裡，兩者就不可能交錯。

    `threading.Lock` 不可重入：同一條執行緒在持鎖時再 `acquire(timeout=0)` 會拿
    不到。拿得到 = 下 `/r` 的時候鎖是放開的 = 沒有互斥。
    """
    monkeypatch.setattr(cq, "_user_idle_seconds_or_none", lambda: 99999.0)
    held = {}

    def _run(cmd):
        if cmd[:2] == ["shutdown", "/r"]:
            got = cq._bde_watch_lock.acquire(timeout=0)
            held["reboot"] = not got
            if got:
                cq._bde_watch_lock.release()
        return _R(0)

    cq._finish_bde_reboot("elapsed", rollback=lambda why: None, run=_run)
    assert held.get("reboot") is True, (
        "★下立即重開時沒有持鎖★ 退出那側可以在檢查與下令之間插進來，"
        "跑一個取消不到任何東西的 shutdown /a，然後機器照樣重開")


def test_the_exit_path_raises_the_cancel_flag_before_anything_else():
    """退出那側必須【先】把取消令設起來，收尾的第二次檢查才看得到。

    設在 `shutdown /a` 之後就沒有意義了：收尾可能早就檢查完並重開。
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._abort_bde_shutdown_on_exit)))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    set_line = shutdown_line = None
    for n in ast.walk(fn):
        if (isinstance(n, ast.Attribute) and n.attr == "set"
                and isinstance(n.value, ast.Name)
                and n.value.id == "_bde_reboot_cancel"):
            set_line = n.lineno
        if (isinstance(n, ast.List)
                and [e.value for e in n.elts
                     if isinstance(e, ast.Constant)] == ["shutdown", "/a"]):
            shutdown_line = n.lineno
    assert set_line and shutdown_line, f"{set_line} {shutdown_line}"
    assert set_line < shutdown_line, (
        "★取消令設得太晚★ 收尾那側可能已經檢查完並下了立即重開")


def test_the_two_sides_never_run_concurrently(monkeypatch):
    """★端對端★ 真的跑 `_abort_bde_shutdown_on_exit`，看它會不會擠進來。

    這一條測的是【生產的退出函式】，不是「另一條執行緒去 `with` 那把鎖」——
    後者只證明鎖存在，不證明退出那側真的用了它。
    """
    monkeypatch.setattr(cq, "_user_idle_seconds_or_none", lambda: 99999.0)
    exit_cmds = []
    monkeypatch.setattr(cq.subprocess, "run",
                        lambda cmd, **kw: exit_cmds.append(list(cmd)) or _R(0))
    with cq._bde_watch_lock:
        cq._bde_shutdown_pending = True       # 讓退出那側真的會走到 /a
    inside = threading.Event()
    overlap = []

    def _run(cmd):
        if cmd[:2] == ["shutdown", "/r"]:
            inside.set()
            other.join(timeout=1.0)           # 給它一個真正擠進來的機會
            overlap.append(other.is_alive())
        return _R(0)

    def _exit_side():
        inside.wait(5)
        cq._abort_bde_shutdown_on_exit()

    other = threading.Thread(target=_exit_side, daemon=True)
    other.start()
    cq._finish_bde_reboot("elapsed", rollback=lambda why: None, run=_run)
    other.join(timeout=25)
    assert overlap == [True], (
        f"★退出流程在收尾持鎖期間就跑完了 → 沒有互斥★：{overlap}")
    assert exit_cmds == [["shutdown", "/a"]], (
        f"退出那側最後仍要跑它的取消（只是排在後面）：{exit_cmds}")


def test_an_unobtainable_lock_does_not_reboot(monkeypatch):
    """拿不到鎖 = 退出流程正在跑 → 不重開（保守方向），而且要回滾。"""
    monkeypatch.setattr(cq, "_user_idle_seconds_or_none", lambda: 99999.0)

    class _Busy:
        def acquire(self, timeout=None):
            return False

        def release(self):                       # pragma: no cover - 不該被呼叫
            raise AssertionError("沒拿到鎖卻放鎖")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cq, "_bde_watch_lock", _Busy())
    cmds = []
    rolled = []
    cq._finish_bde_reboot("elapsed", rollback=rolled.append,
                          run=lambda cmd: cmds.append(cmd) or _R(0))
    assert cmds == [["shutdown", "/a"]], f"拿不到鎖卻還是重開了：{cmds}"
    assert rolled, "不重開就要回滾，否則 24 小時內不會再試"


def test_the_rollback_is_not_called_while_holding_the_lock(monkeypatch):
    """★threading.Lock 不可重入★

    `_bde_rollback_after_abort` 自己會拿 `_bde_watch_lock`。在鎖裡呼叫它
    會死鎖 —— 而死鎖的樣子是「倒數執行緒安靜地卡住」，不會有任何 log。
    """
    monkeypatch.setattr(cq, "_user_idle_seconds_or_none", lambda: 99999.0)
    seen = []

    def _rollback(why):
        # 回滾時鎖必須是放開的：這裡再拿一次，卡住就是死鎖
        got = cq._bde_watch_lock.acquire(timeout=1)
        seen.append(got)
        if got:
            cq._bde_watch_lock.release()

    cq._bde_reboot_cancel.set()
    cq._finish_bde_reboot("elapsed", rollback=_rollback,
                          run=lambda cmd: _R(0))
    assert seen == [True], f"★回滾是在持鎖狀態下呼叫的（會死鎖）★：{seen}"


def test_a_cancelled_outcome_still_takes_the_old_path(monkeypatch):
    """非 elapsed 的收尾不走這段新邏輯（它本來就是要取消）。"""
    cmds = []
    cq._finish_bde_reboot("user_back", rollback=lambda why: None,
                          run=lambda cmd: cmds.append(cmd) or _R(0))
    assert cmds == [["shutdown", "/a"]], cmds
