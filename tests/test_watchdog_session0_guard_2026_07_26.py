# -*- coding: utf-8 -*-
r"""[2026-07-26 審查] watchdog 在 session 0 不得啟動任何程式(重複打卡的根因)。

安裝腳本用 `schtasks /SC MINUTE` 建立 watchdog 週期性 task 時原本【沒有 /IT】,
那種 task 跑在 session 0(非互動)。watchdog 從那裡 Popen 出來的子程式也落在
session 0:使用者看不到、Chrome 自動化沒有互動桌面,而且各程式的單例 mutex 都是
`Local\`(per-session)—— 擋不住跨 session 的第二份 → 打卡程式同時跑兩份、重複打卡。
repo 內的「清理重複打卡程式.ps1」「診斷打卡重複執行.ps1」就是這現象留下的現場工具。
"""
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import watchdog_core as wc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_start_program_refuses_in_session_0(monkeypatch):
    called = []
    monkeypatch.setattr(wc, "current_session_id", lambda: 0)
    monkeypatch.setattr(wc, "launch_python_script",
                        lambda *a, **k: called.append(a) or object())
    pid = wc.start_program(Path("打卡程式.pyw"), "pythonw.exe")
    assert pid == 0, "session 0 必須拒絕啟動"
    assert not called, "拒絕時不可真的 spawn"


def test_start_program_still_works_in_interactive_session(monkeypatch):
    """兜底不可誤殺正常情境:互動 session 照常啟動。"""
    class _P:
        pid = 4321
    monkeypatch.setattr(wc, "current_session_id", lambda: 1)
    monkeypatch.setattr(wc, "launch_python_script", lambda *a, **k: _P())
    assert wc.start_program(Path("打卡程式.pyw"), "pythonw.exe") == 4321


def test_start_program_allows_when_session_unknown(monkeypatch):
    """取不到 session id 時不猜 —— 維持既有行為照常啟動(寧可保守不改變現狀,
    也不要因為查不到就讓守護整個失效)。"""
    class _P:
        pid = 99
    monkeypatch.setattr(wc, "current_session_id", lambda: None)
    monkeypatch.setattr(wc, "launch_python_script", lambda *a, **k: _P())
    assert wc.start_program(Path("打卡程式.pyw"), "pythonw.exe") == 99


def test_installer_creates_periodic_task_interactively():
    """安裝腳本的週期性 task 必須帶 /IT,否則新裝的機器又會落回 session 0。"""
    ps1 = (ROOT / "安裝開機自動啟動.ps1").read_text(encoding="utf-8")
    i = ps1.index("schtasks.exe /Create /F")
    block = ps1[i:i + 600]
    assert re.search(r"^\s*/IT\s*`", block, re.MULTILINE), \
        "schtasks /Create 必須帶 /IT(跑在使用者的互動 session)"
    assert "/RL HIGHEST" in block, "既有的提權設定不可被改掉"


# ── ★外審 R1★ 閘門必須在 kill / 重啟記帳【之前】 ─────────────────────────────
def _prog(tmp_path, *, stale: bool):
    pyw = tmp_path / "target.pyw"
    pyw.write_text("# shim\n", encoding="utf-8")
    log = tmp_path / "target.log"
    log.write_text("heartbeat\n", encoding="utf-8")
    if stale:
        old = time.time() - 600
        os.utime(log, (old, old))
    return {
        "name": "打卡",
        "enabled": True,
        "pyw": str(pyw),
        "process_match": "中國醫皮膚科打卡程式",
        "log_path": str(log),
        "max_stale_sec": 300,
    }


def _run_ensure(monkeypatch, prog, procs):
    """回 (msg, killed, started, recorded)。所有破壞性動作都被攔下來記錄。"""
    killed, started, recorded = [], [], []
    monkeypatch.setattr(wc, "current_session_id", lambda: 0)
    monkeypatch.setattr(wc, "claim_action_lock", lambda *a, **k: True)
    monkeypatch.setattr(wc, "kill_pid", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(wc, "start_program",
                        lambda *a, **k: started.append(a) or 4321)
    monkeypatch.setattr(wc, "_record_restart_and_check_crash_loop",
                        lambda name: recorded.append(name) or False)
    msg = wc.ensure_program(prog, pythonw="pythonw.exe", procs=procs,
                            my_pid=9999, mode="outer",
                            cfg={"action_lock_seconds": 90})
    return msg, killed, started, recorded


def test_session0_never_kills_a_stale_interactive_process(tmp_path, monkeypatch):
    """★最嚴重的情境★ kill+restart 是一筆交易。先砍了才發現不能起 → 互動 session 的
    打卡程式被砍掉又沒補回來,可能整段錯過打卡 —— 比放著不管更糟。"""
    msg, killed, started, recorded = _run_ensure(
        monkeypatch, _prog(tmp_path, stale=True),
        [{"pid": 1234, "cmdline": "pythonw.exe 中國醫皮膚科打卡程式.pyw"}])
    assert killed == [], "session 0 不可砍任何行程"
    assert started == [], "session 0 不可啟動任何行程"
    assert recorded == [], "被拒的補救不可被記成一次重啟嘗試"
    assert "session 0" in msg


def test_session0_does_not_record_restart_attempts_when_absent(tmp_path, monkeypatch):
    """程式根本不在時,每次被拒的啟動若都記成一次重啟嘗試,週期性 task 每 2 分鐘跑一次
    → 很快誤觸 crash-loop 判定,把自動更新停掉一小時。"""
    msg, killed, started, recorded = _run_ensure(
        monkeypatch, _prog(tmp_path, stale=False), [])
    assert (killed, started, recorded) == ([], [], [])
    assert "session 0" in msg
