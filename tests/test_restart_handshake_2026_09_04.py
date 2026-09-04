# -*- coding: utf-8 -*-
"""[第九輪 §4] restart 兩階段 READY 交握:活著 ≠ 就緒。

舊版 `restart_self` 以「0.6 秒內 proc.poll() 為 None」當接手成功,然後拆光本行程。
子行程 0.8 秒後死在 config/UI 初始化 → 零個可用 instance。子行程要完整就緒必須先拿到
mutex、mutex 在父行程手上,所以不能只等一個 READY(死鎖)—— 改兩階段:
  PRE-READY(即將搶 mutex)→ 父行程放 mutex;READY(拿到 mutex + 核心初始化完成)→ 父行程退出;
  兩者之間子行程死掉 → 父行程★復原★。
★降版也要能重啟★:舊版子行程永遠不寫交握檔 → 照今天的時序運作。
"""
import ast
import errno
import io
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cmuh_common.paths as paths  # noqa: E402
from cmuh_common.paths import (  # noqa: E402
    HANDOVER_CONFIRMED, HANDSHAKE_READY, HANDSHAKE_WAITING_MUTEX,
    SPAWN_CHILD_CRASHED, SPAWN_CHILD_DIED_AFTER_HANDOVER, SPAWN_CHILD_EXITED_ORDERLY,
    mutex_retry_sec, read_handshake, restart_handshake_signal, wait_for_handover,
)

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")


# ─── 假子行程 + 假時鐘:用「時間表」驅動 ────────────────────────────────────
@pytest.fixture(autouse=True)
def _fresh_latch(monkeypatch):
    """交握路徑是行程內 latch 一次的;每個測試都要從「還沒 latch」開始。"""
    monkeypatch.setattr(paths, "_HANDSHAKE_PATH", [None])
    yield


class _Sim:
    """t 是模擬秒數。`exits_at`:子行程在此時之後 poll() 回 rc。
    `writes`:{t: state} 在該時刻把交握檔寫成 `state <pid>`(pid 預設是本子行程的;
    傳 (state, pid) 可模擬★別的行程★寫進來)。"""

    pid = 4242

    def __init__(self, path, *, exits_at=None, rc=0, writes=None):
        self.path, self.t = path, 0.0
        self.exits_at, self.rc = exits_at, rc
        self.writes = dict(writes or {})
        self.calls = []

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += sec
        for at in sorted(self.writes):
            if at <= self.t:
                item = self.writes.pop(at)
                state, pid = item if isinstance(item, tuple) else (item, self.pid)
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(f"{state} {pid}")

    def poll(self):
        return self.rc if (self.exits_at is not None and self.t >= self.exits_at) else None

    # 父行程終止子行程:記錄下來,並讓之後的 poll() 回傳結束碼(真的死了)。
    def terminate(self):
        self.calls.append(("terminate", round(self.t, 1)))
        self.exits_at, self.rc = self.t, -15

    def kill(self):
        self.calls.append(("kill", round(self.t, 1)))
        self.exits_at, self.rc = self.t, -9

    def wait(self, timeout=None):
        if self.exits_at is None or self.t < self.exits_at:
            raise TimeoutError("still alive")
        return self.rc

    def cb(self, name, ret=True):
        """回呼預設回 True(復原成立);on_recover 用 ret=... 模擬所有權答案。
        `ret` 可以是 list:依序回答(模擬終止子行程之後單例才回到自己手上)。"""
        answers = list(ret) if isinstance(ret, list) else None

        def _f():
            self.calls.append((name, round(self.t, 1)))
            if answers:
                return answers.pop(0) if len(answers) > 1 else answers[0]
            return ret
        return _f


def _run(sim, owner=None, **kw):
    """`owner`:所有權探針的回答(OWNER_SELF / OWNER_OTHER / OWNER_UNKNOWN)。
    預設 OWNER_SELF —— 子行程沒接手、單例回到本行程。"""
    kw.setdefault("on_preready", sim.cb("preready"))
    kw.setdefault("on_confirmed", sim.cb("confirmed"))
    kw.setdefault("on_recover", sim.cb("recover", ret=owner or paths.OWNER_SELF))
    return wait_for_handover(sim, sim.path, now=sim.now, sleep=sim.sleep,
                             stderr_tail=lambda: "", **kw)


# ─── 1. 新版子行程:兩階段 ────────────────────────────────────────────────────
def test_new_child_preready_then_ready(tmp_path):
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX, 2.0: HANDSHAKE_READY})
    assert _run(sim) == HANDOVER_CONFIRMED
    names = [c[0] for c in sim.calls]
    assert names == ["preready", "confirmed"], sim.calls
    assert sim.calls[0][1] < 0.6, "看到 PRE-READY 就該放 mutex,不必等滿 0.6 秒"
    assert 2.0 <= sim.calls[1][1] < 2.5, "READY 一到就確認;之前不可以做慢的拆解"


def test_child_that_dies_after_preready_triggers_recovery(tmp_path):
    """★核心★:放了 mutex 之後子行程才死 → 復原,不可以退出(零 instance)。"""
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX}, exits_at=1.5, rc=1)
    assert _run(sim) == SPAWN_CHILD_DIED_AFTER_HANDOVER
    names = [c[0] for c in sim.calls]
    assert names == ["preready", "recover"], sim.calls
    assert "confirmed" not in names, "沒有 READY 就不可以做慢的拆解/退出"


def test_early_death_is_classified_and_touches_nothing(tmp_path):
    sim = _Sim(str(tmp_path / "hs"), exits_at=0.3, rc=1)
    assert _run(sim) == SPAWN_CHILD_CRASHED
    assert sim.calls == []
    sim2 = _Sim(str(tmp_path / "hs2"), exits_at=0.3, rc=0)
    assert _run(sim2) == SPAWN_CHILD_EXITED_ORDERLY
    assert sim2.calls == []


def test_alive_without_ready_is_never_enough_to_confirm(tmp_path):
    """★外審 r10 P3-high(本檔上一版把 bug 釘成正確答案)★:子行程回報過 WAITING_MUTEX、
    之後永遠不 READY 卻活著 —— 舊版等 30 秒就「視為接手成功」退出,那正是這批要消滅的
    「alive ⇒ ready」。判準改成★此刻誰持有單例★:探針說單例回到本行程 = 子行程根本沒接手
    → 不可以確認交棒,要恢復服務、本次不重啟。"""
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX})
    assert _run(sim, owner=paths.OWNER_SELF) == paths.SPAWN_CHILD_NEVER_READY
    names = [c[0] for c in sim.calls]
    assert names == ["preready", "recover", "terminate"], sim.calls
    assert "confirmed" not in names, "沒有 READY、單例又還在自己手上,不可以退出"
    assert sim.calls[1][1] >= 30.0
    assert paths.HANDSHAKE_READY_TIMEOUT_SEC == 30.0     # 上面的 30 是固定數,釘住常數


def test_a_capable_child_that_owns_the_mutex_is_a_real_handover(tmp_path):
    """★外審 r10-3(我上一版加的「殺掉卡死的接手者」已撤回)★:子行程送過交握、拿了單例、
    卻沒回報 READY —— ★缺席的訊號不是失敗的證據★:同樣的外觀也可能是 READY 寫不進去
    (ACL/防毒)而它其實健康、正在服務。要終止一個持有單例的行程必須有正面的失效證據,
    這裡沒有 → 一律當交棒成立。真的卡死時:打卡有 watchdog;主程式/排班是 GUI,
    使用者看得到(那正是 watchdog 對主程式刻意停用的理由)。"""
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX})
    assert _run(sim, owner=paths.OWNER_OTHER) == HANDOVER_CONFIRMED
    names = [c[0] for c in sim.calls]
    assert names == ["preready", "recover", "confirmed"], sim.calls
    assert "terminate" not in names, "沒有正面失效證據就不可以殺持有單例的接手者"


def test_a_healthy_child_whose_ready_never_arrived_is_not_killed(tmp_path):
    """★外審 r10-2 第二/三回指出的具體情境★:子行程健康、持有單例、只是 READY 連重試都
    寫不進去。它★不可以★被終止(否則每次自動更新都會殺掉一個正在服務的新版本)。"""
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX})
    assert _run(sim, owner=paths.OWNER_OTHER) == HANDOVER_CONFIRMED
    assert "terminate" not in [c[0] for c in sim.calls]


def test_an_unknown_owner_never_pretends_the_handover_worked(tmp_path):
    """查不出誰持有單例 → 不可以假裝接手成功,也不可以繼續當沒守衛的 instance。"""
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX})
    assert _run(sim, owner=paths.OWNER_UNKNOWN) == paths.SPAWN_RECOVERY_FAILED
    assert "confirmed" not in [c[0] for c in sim.calls]


# ─── 2. 舊版子行程(降版):與今天完全相同的時序 ───────────────────────────────
def test_a_legacy_child_that_took_over_is_confirmed_on_the_short_grace(tmp_path):
    """降版到不懂交握的舊版本:永遠不寫交握檔。短寬限(0.6+3s)到期就問所有權 —— 它拿到了
    單例 → 確認交棒。時序與今天相同,降版仍能重啟。"""
    sim = _Sim(str(tmp_path / "hs"))                       # 永遠不寫檔、活著
    assert _run(sim, owner=paths.OWNER_OTHER) == HANDOVER_CONFIRMED
    names = [c[0] for c in sim.calls]
    assert names == ["preready", "recover", "confirmed"], sim.calls
    assert abs(sim.calls[0][1] - 0.6) < 0.15, "舊版子行程:0.6 秒還活著就放 mutex(今天的時序)"
    assert sim.calls[2][1] >= 3.5 and sim.calls[2][1] < 4.0   # 0.6 + 3s 寬限
    assert paths.HANDSHAKE_LEGACY_GRACE_SEC == 3.0


def test_a_silent_child_that_did_not_take_over_is_not_guessed_to_be_legacy(tmp_path):
    """★外審 r10★:「沒看到交握檔」不等於「舊版子行程」—— 也可能是新版本卡在取得單例
    之前、或訊號寫失敗。舊版直接猜成舊版並退出;現在要問所有權:單例還在自己手上 →
    它沒接手,恢復服務、本次不重啟。"""
    sim = _Sim(str(tmp_path / "hs"))
    assert _run(sim, owner=paths.OWNER_SELF) == paths.SPAWN_CHILD_NEVER_READY
    names = [c[0] for c in sim.calls]
    assert "confirmed" not in names
    assert "terminate" in names, "已失去接手資格的子行程要被終止,不可以留成孤兒"


def test_legacy_caller_with_only_on_confirmed_is_called_exactly_once(tmp_path):
    """只傳 on_confirmed 的舊呼叫端:在 PRE-READY 時刻呼叫一次,之後不再呼叫。"""
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX, 1.0: HANDSHAKE_READY})
    calls = []
    out = wait_for_handover(sim, sim.path, on_confirmed=lambda: calls.append(sim.t),
                            now=sim.now, sleep=sim.sleep)
    assert out == HANDOVER_CONFIRMED and len(calls) == 1 and calls[0] < 0.6


def test_a_failing_preready_callback_does_not_abort_the_handover(tmp_path):
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX, 1.0: HANDSHAKE_READY})

    def boom():
        raise RuntimeError("unhook failed")
    out = _run(sim, on_preready=boom)
    assert out == HANDOVER_CONFIRMED and [c[0] for c in sim.calls] == ["confirmed"]


# ─── 3. 傳輸 ─────────────────────────────────────────────────────────────────
def test_signal_is_a_noop_without_the_env(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.RESTART_HANDSHAKE_ENV, raising=False)
    assert restart_handshake_signal(HANDSHAKE_READY) is False
    # conftest 會在 tmp_path 底下建 _cmuh_app;這裡只看有沒有交握檔/tmp 檔被寫出來
    assert not [p for p in tmp_path.rglob("*") if p.is_file()], "冷啟動不可以寫任何交握檔"
    assert mutex_retry_sec() == 1.5, "冷啟動維持 1.5s 的重試窗"


def test_signal_writes_atomically_and_reader_ignores_garbage(monkeypatch, tmp_path):
    p = tmp_path / "hs"
    monkeypatch.setenv(paths.RESTART_HANDSHAKE_ENV, str(p))
    assert restart_handshake_signal(HANDSHAKE_WAITING_MUTEX) is True
    assert read_handshake(str(p)) == HANDSHAKE_WAITING_MUTEX
    assert not [f for f in tmp_path.iterdir() if f.name.endswith(".tmp")], "不留 tmp 檔"
    p.write_text("half-writ", encoding="utf-8")
    assert read_handshake(str(p)) is None
    assert read_handshake(str(tmp_path / "nope")) is None
    assert mutex_retry_sec() == paths.HANDSHAKE_MUTEX_RETRY_SEC > 1.5, "交握存在 → 重試窗放大"


def test_a_transient_write_failure_is_retried(monkeypatch, tmp_path):
    """★外審 r10-2 第二回★:READY 漏掉的代價很高(父行程分不出「卡死」與「訊號寫不進去」,
    會終止一個其實健康的接手者)。暫時性失敗要重試 —— 同一條路徑幾秒前才剛寫成功過。"""
    p = tmp_path / "hs"
    monkeypatch.setenv(paths.RESTART_HANDSHAKE_ENV, str(p))
    monkeypatch.setattr(paths, "HANDSHAKE_SIGNAL_RETRY_SEC", 0.0)
    real_open = open
    calls = {"n": 0}

    def _flaky(path, *a, **k):
        if str(path).endswith(".tmp"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EACCES, "sharing violation", None, 32)
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", _flaky)

    assert restart_handshake_signal(HANDSHAKE_READY) is True
    assert calls["n"] >= 2, "第一次失敗要重試"
    monkeypatch.setattr("builtins.open", real_open)
    assert read_handshake(str(p), os.getpid()) == HANDSHAKE_READY


def test_a_persistent_write_failure_is_reported_not_swallowed(monkeypatch, tmp_path, caplog):
    """全部重試都失敗 → 回 False 並記 WARNING(呼叫端才有辦法留下 CRITICAL 說明自己其實
    已就緒);舊版是靜默吞掉 debug。"""
    import logging
    monkeypatch.setenv(paths.RESTART_HANDSHAKE_ENV, str(tmp_path / "hs"))
    monkeypatch.setattr(paths, "HANDSHAKE_SIGNAL_RETRY_SEC", 0.0)

    def _denied(path, *a, **k):
        raise OSError(errno.EACCES, "denied")
    monkeypatch.setattr("builtins.open", _denied)
    with caplog.at_level(logging.WARNING):
        assert restart_handshake_signal(HANDSHAKE_READY) is False
    assert any("連 3 次失敗" in r.getMessage() or "次失敗" in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("rel,fn", [
    ("main.py", "_finalize_hotkey_setup"),
    ("autoclock.py", "main"),
    ("scheduler.py", "main"),
])
def test_every_program_reports_a_failed_ready_signal(rel, fn):
    """★外審 r10-2★:三支程式原本都忽略 READY 的回傳值 —— 送不到就靜默,
    之後被父行程終止時完全查不出原因。現在要留 CRITICAL。"""
    f = _func(_tree(rel), fn)
    crit = [n for n in ast.walk(f) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "critical"
            and any(isinstance(a, ast.Constant) and "READY" in str(a.value) for a in n.args)]
    assert crit, f"{rel}:{fn} 沒有在 READY 送不到時留 CRITICAL"


def test_real_child_process_transport(tmp_path):
    """真的 subprocess:子行程照協定寫兩個階段;父行程用真時鐘等到 READY。"""
    hs = tmp_path / "hs"
    child = (
        "import os,sys,time; sys.path.insert(0, sys.argv[1]);"
        "from cmuh_common.paths import restart_handshake_signal as s;"
        "s('waiting_mutex'); time.sleep(0.3); s('ready'); time.sleep(3)"
    )
    env = dict(os.environ, **{paths.RESTART_HANDSHAKE_ENV: str(hs), "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.Popen([sys.executable, "-c", child, os.path.abspath(_SRC)], env=env)
    calls = []
    try:
        t0 = time.monotonic()
        out = wait_for_handover(proc, str(hs), on_preready=lambda: calls.append("pre"),
                                on_confirmed=lambda: calls.append("conf"),
                                on_recover=lambda: calls.append("rec"))
        assert out == HANDOVER_CONFIRMED and calls == ["pre", "conf"]
        assert time.monotonic() - t0 < 5.0
    finally:
        proc.kill()
        proc.wait(timeout=5)


# ─── 3b. 外審 r1 P1-1:訊號綁直接子行程的 PID;孫行程冒充不了 ─────────────────
def test_signals_from_another_pid_are_ignored(tmp_path):
    """孫行程(例如新主程式的 watchdog 拉起的打卡)若寫進同一個交握檔,父行程不可以認。

    [外審 r10 §11] 而且★不可以因此降級成「大概是舊版子行程」★:檔案存在就代表交握機制
    在場 → 用完整的 READY 窗口,並以所有權判定(這裡:接手者持有單例 → 確認)。"""
    sim = _Sim(str(tmp_path / "hs"),
               writes={0.2: (HANDSHAKE_WAITING_MUTEX, 9999), 1.0: (HANDSHAKE_READY, 9999)})
    assert _run(sim, owner=paths.OWNER_OTHER) == HANDOVER_CONFIRMED
    names = [c[0] for c in sim.calls]
    assert names == ["preready", "recover", "confirmed"], sim.calls
    # 冒充的訊號被忽略 → 0.6s 才放 mutex(不是 0.2s),而且要等到完整的 READY 窗口
    assert abs(sim.calls[0][1] - 0.6) < 0.15
    assert sim.calls[1][1] >= paths.HANDSHAKE_READY_TIMEOUT_SEC, \
        "無效訊號被當成『沒有交握』→ 走短寬限,等於默許冒充者縮短窗口"


def test_an_invalid_handshake_file_is_not_treated_as_a_legacy_child(tmp_path, caplog):
    """壞掉的交握內容同理:要 WARNING 並用完整窗口,不可以靜默當舊版。"""
    import logging
    p = tmp_path / "hs"
    p.write_text("garbage", encoding="utf-8")
    sim = _Sim(str(p))
    with caplog.at_level(logging.WARNING):
        assert _run(sim, owner=paths.OWNER_SELF) == paths.SPAWN_CHILD_NEVER_READY
    assert any("內容無效" in r.getMessage() for r in caplog.records)
    probe = [c[1] for c in sim.calls if c[0] == "recover"][0]
    assert probe >= paths.HANDSHAKE_READY_TIMEOUT_SEC


def test_read_handshake_binds_the_pid(tmp_path):
    p = tmp_path / "hs"
    p.write_text(f"{HANDSHAKE_READY} 4242", encoding="utf-8")
    assert read_handshake(str(p), 4242) == HANDSHAKE_READY
    assert read_handshake(str(p), 4243) is None
    assert read_handshake(str(p)) == HANDSHAKE_READY          # 不指定 pid 才不檢查
    p.write_text(HANDSHAKE_READY, encoding="utf-8")            # 舊格式(沒 pid)
    assert read_handshake(str(p), 4242) is None, "指定 pid 時,沒帶 pid 的訊號不可以認"


def test_the_handshake_path_is_latched_and_removed_from_the_environment(monkeypatch, tmp_path):
    """★latch 後立刻從 os.environ 拿掉★:子行程再起的孫行程不可以繼承交握路徑。"""
    p = tmp_path / "hs"
    monkeypatch.setenv(paths.RESTART_HANDSHAKE_ENV, str(p))
    assert paths.restart_handshake_active() is True
    assert paths.RESTART_HANDSHAKE_ENV not in os.environ, "latch 之後 env 要被拿掉"
    assert restart_handshake_signal(HANDSHAKE_WAITING_MUTEX) is True, "latch 住的路徑仍可用"
    assert read_handshake(str(p), os.getpid()) == HANDSHAKE_WAITING_MUTEX


def test_a_grandchild_cannot_impersonate_the_child(tmp_path):
    """真的三層行程:子行程先起一個孫行程(孫行程照協定寫 ready),自己 1.2 秒後才 READY。
    父行程必須等到★子行程自己★的 READY;而且孫行程的環境裡不可以有交握路徑。"""
    hs = tmp_path / "hs"
    marker = tmp_path / "grandchild_env.txt"
    grandchild = (
        "import os,sys; sys.path.insert(0, sys.argv[1]);"
        "from cmuh_common import paths as p;"
        "open(sys.argv[2],'w').write('inherited' if p.RESTART_HANDSHAKE_ENV in os.environ else 'clean');"
        "p.restart_handshake_signal('ready')"          # 若繼承到了就會冒充 READY
    )
    child = (
        "import os,sys,subprocess,time; sys.path.insert(0, sys.argv[1]);"
        "from cmuh_common.paths import restart_handshake_signal as s;"
        "s('waiting_mutex');"
        f"subprocess.run([sys.executable,'-c',{grandchild!r},sys.argv[1],sys.argv[2]]);"
        "time.sleep(1.2); s('ready'); time.sleep(3)"
    )
    env = dict(os.environ, **{paths.RESTART_HANDSHAKE_ENV: str(hs), "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.Popen([sys.executable, "-c", child, os.path.abspath(_SRC), str(marker)], env=env)
    seen = []
    try:
        t0 = time.monotonic()
        out = wait_for_handover(proc, str(hs), on_preready=lambda: seen.append(("pre", time.monotonic() - t0)),
                                on_confirmed=lambda: seen.append(("conf", time.monotonic() - t0)),
                                on_recover=lambda: seen.append(("rec", 0)) or True)
        assert out == HANDOVER_CONFIRMED and [s[0] for s in seen] == ["pre", "conf"]
        assert marker.read_text(encoding="utf-8") == "clean", "孫行程繼承到交握路徑"
        assert seen[1][1] >= 1.0, "父行程認了孫行程的 READY,提早確認"
    finally:
        proc.kill()
        proc.wait(timeout=5)


# ─── 3c. 外審 r1 P1-2:復原要明講成不成立 ─────────────────────────────────────
def test_recovery_that_cannot_reacquire_the_mutex_is_reported(tmp_path):
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX}, exits_at=1.5, rc=1)
    assert _run(sim, owner=paths.OWNER_UNKNOWN) == paths.SPAWN_RECOVERY_FAILED
    assert [c[0] for c in sim.calls] == ["preready", "recover"]


def test_a_dead_child_whose_mutex_someone_else_took_hands_over_quietly(tmp_path):
    """子行程交棒後死了,但單例已被★第三方★拿走:這裡不是「沒人服務」,不可以搶回來,
    安靜走完拆解退出。"""
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX}, exits_at=1.5, rc=1)
    assert _run(sim, owner=paths.OWNER_OTHER) == HANDOVER_CONFIRMED
    assert [c[0] for c in sim.calls] == ["preready", "recover", "confirmed"]


def test_the_old_bool_recover_contract_still_works(tmp_path):
    """相容:舊契約回 True/False 的 on_recover 仍要能用(True→自己持有;False→不知道)。"""
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX}, exits_at=1.5, rc=1)
    assert _run(sim, on_recover=lambda: True) == paths.SPAWN_CHILD_DIED_AFTER_HANDOVER
    sim2 = _Sim(str(tmp_path / "hs2"), writes={0.2: HANDSHAKE_WAITING_MUTEX}, exits_at=1.5, rc=1)
    assert _run(sim2, on_recover=lambda: False) == paths.SPAWN_RECOVERY_FAILED


def test_recovery_that_raises_is_reported_as_failed(tmp_path):
    sim = _Sim(str(tmp_path / "hs"), writes={0.2: HANDSHAKE_WAITING_MUTEX}, exits_at=1.5, rc=1)

    def boom():
        raise RuntimeError("mutex API broken")
    assert _run(sim, on_recover=boom) == paths.SPAWN_RECOVERY_FAILED


# ─── 4. 接線:三支程式 + restart_self 本身(來源 AST,不 import Tk 程式)──────────
def _tree(rel):
    return ast.parse(io.open(os.path.join(_SRC, rel), encoding="utf-8").read())


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"找不到 {name}")


def _calls(node):
    out = []
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            f = c.func
            out.append(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


def _signal_args(node):
    """回 restart_handshake_signal(...) 的第一個引數名稱列表。"""
    out = []
    for c in ast.walk(node):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == "restart_handshake_signal" and c.args):
            a = c.args[0]
            out.append(a.id if isinstance(a, ast.Name) else None)
    return out


@pytest.mark.parametrize("rel,gate,ready_fns,restart_fn", [
    ("main.py", "single_instance_gate",
     ["_finalize_hotkey_setup", "_handle_hotkey_setup_failure"], "_restart_app"),
    ("autoclock.py", "single_instance_gate", ["main"], "restart_program"),
    ("scheduler.py", "main", ["main"], "_handover_and_restart"),
])
def test_each_program_signals_both_stages_and_passes_three_callbacks(rel, gate, ready_fns, restart_fn):
    tree = _tree(rel)
    g = _func(tree, gate)
    assert "HANDSHAKE_WAITING_MUTEX" in _signal_args(g), f"{rel}:{gate} 沒回報 PRE-READY"
    assert "mutex_retry_sec" in _calls(g), f"{rel}:{gate} 搶 mutex 沒用交握的重試窗"
    assert any("HANDSHAKE_READY" in _signal_args(_func(tree, fn)) for fn in ready_fns), \
        f"{rel} 沒有任何就緒點回報 READY"
    r = _func(tree, restart_fn)
    kw = {k.arg for c in ast.walk(r) if isinstance(c, ast.Call)
          and isinstance(c.func, ast.Name) and c.func.id == "restart_self"
          for k in c.keywords}
    assert {"on_preready", "on_confirmed", "on_recover"} <= kw, f"{rel}:{restart_fn} 缺回呼 {kw}"


def test_main_preready_releases_mutex_and_unhooks_and_recover_reacquires():
    """主程式:PRE-READY 做「拔熱鍵 + 放 mutex」,慢的拆解不可以提前;復原要重取 mutex 並重掛熱鍵。"""
    tree = _tree("main.py")
    r = _func(tree, "_restart_app")
    pre = _func(r, "_preready_for_handover")
    conf = _func(r, "_teardown_for_handover")
    rec = _func(r, "_recover_after_failed_handover")
    assert {"safe_unhook_all_hotkeys", "release_single_instance"} <= set(_calls(pre))
    assert "release_single_instance" not in _calls(conf), "mutex 只在 PRE-READY 放一次"
    assert not ({"_flush_ledger_before_exit", "_cleanup_for_exit", "destroy"} & set(_calls(pre))), \
        "慢的拆解不可以在 PRE-READY 做(子行程還沒 READY)"
    assert {"acquire_single_instance"} <= set(_calls(rec)) and "setup_hotkeys" in ast.dump(rec)


@pytest.mark.parametrize("rel,outer,inner,exit_marker", [
    ("main.py", "_restart_app", "_recover_after_failed_handover", "_teardown_for_handover"),
    ("autoclock.py", "restart_program", "_recover_after_failed_handover", "_teardown_for_handover"),
    ("scheduler.py", "_handover_and_restart", "_on_recover", "destroy"),
])
def test_recovery_only_resumes_service_after_the_mutex_is_reacquired(rel, outer, inner, exit_marker):
    """★外審 r1 P1-2★:復原只有在 INSTANCE_ACQUIRED 時才恢復服務;拿不到就安全退場,
    不可以繼續當一個沒守衛的 instance(兩份熱鍵 / 重複打卡 / 兩個 repo writer)。"""
    tree = _tree(rel)
    rec = _func(_func(tree, outer), inner)
    # ★用 AST 節點,不用 ast.dump 的子字串★:docstring 裡也寫了 INSTANCE_ACQUIRED,
    # 子字串比對會被說明文字騙過(突變把比較拿掉照樣綠)。
    compares = [n for n in ast.walk(rec) if isinstance(n, ast.Compare)
                and any(isinstance(c, ast.Name) and c.id == "INSTANCE_ACQUIRED"
                        for c in n.comparators)]
    assert compares, f"{rel}:{inner} 沒有拿重取結果與 INSTANCE_ACQUIRED 比較"
    # [外審 r10] 探針要分得出★三態★:自己拿回 / 別人持有 / 查不出來 —— 把後兩者壓成一格,
    # 「接手者拿到了」就會被當成「查不出來」而誤發停止警報(降版就會中招)。
    names = {n.id for n in ast.walk(rec) if isinstance(n, ast.Name)}
    assert {"OWNER_SELF", "OWNER_OTHER", "OWNER_UNKNOWN"} <= names, \
        f"{rel}:{inner} 沒有回報三態所有權"
    assert any(isinstance(c, ast.Name) and c.id == "INSTANCE_ALREADY_RUNNING"
               for n in ast.walk(rec) if isinstance(n, ast.Compare)
               for c in n.comparators), f"{rel}:{inner} 沒有分辨「別人持有」"
    # 探針★只回報、不做破壞性動作★:退場由呼叫端在拿到 outcome 之後做。
    assert exit_marker not in names, \
        f"{rel}:{inner} 是探針,不該自己做退場動作({exit_marker})"
    returns = [n for n in ast.walk(rec) if isinstance(n, ast.Return)]
    assert len(returns) >= 3


@pytest.mark.parametrize("rel,fn,exit_marker", [
    ("main.py", "_restart_app", "_teardown_for_handover"),
    ("autoclock.py", "restart_program", "_teardown_for_handover"),
    ("scheduler.py", "_handover_and_restart", "destroy"),
])
def test_an_unknown_owner_makes_the_caller_leave_safely(rel, fn, exit_marker):
    """[外審 r10] 三支程式在 SPAWN_RECOVERY_FAILED 時都要安全退場(不繼續當沒守衛的
    instance);退場動作在★呼叫端★,探針只負責回報。"""
    outer = _func(_tree(rel), fn)
    compares = [n for n in ast.walk(outer) if isinstance(n, ast.Compare)
                and any(isinstance(c, ast.Name)
                        and c.id in ("SPAWN_RECOVERY_FAILED", "_SPAWN_RECOVERY_FAILED")
                        for c in n.comparators)]
    assert compares, f"{rel}:{fn} 沒有處理 SPAWN_RECOVERY_FAILED"
    names = {n.id for n in ast.walk(outer) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(outer) if isinstance(n, ast.Attribute)}
    assert exit_marker in names, f"{rel}:{fn} 查不出擁有者時沒有安全退場"


def test_restart_self_wires_the_handshake_env_and_waiter():
    tree = _tree("cmuh_common/paths.py")
    r = _func(tree, "restart_self")
    src = ast.dump(r)
    assert "wait_for_handover" in _calls(r)
    assert "RESTART_HANDSHAKE_ENV" in src and "env" in {k.arg for c in ast.walk(r)
                                                        if isinstance(c, ast.Call) for k in c.keywords}


def test_startup_instance_state_threads_the_retry_window():
    tree = _tree("cmuh_common/single_instance.py")
    f = _func(tree, "startup_instance_state")
    assert "retry_sec" in [a.arg for a in f.args.args]
    call = [c for c in ast.walk(f) if isinstance(c, ast.Call)
            and getattr(c.func, "id", "") == "acquire_single_instance"][0]
    assert any(isinstance(a, ast.Name) and a.id == "retry_sec" for a in call.args) or \
        any(k.arg == "retry_sec" for k in call.keywords), "retry_sec 沒傳下去"
