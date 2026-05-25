# -*- coding: utf-8 -*-
"""watchdog_core 安全行為測試。"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import watchdog_core as wc  # noqa: E402


def test_find_pids_holding_mutex_does_not_broad_kill_when_wmic_fails(monkeypatch):
    """WMIC 失敗時不可退化成回傳所有 pythonw.exe PID。"""

    def fake_run(*args, **kwargs):
        raise OSError("wmic unavailable")

    monkeypatch.setattr(wc.subprocess, "run", fake_run)
    monkeypatch.setattr(
        wc,
        "_get_psutil",
        lambda: pytest.fail("_get_psutil should not be used for broad fallback"),
    )

    assert wc._find_pids_holding_mutex("中國醫皮膚科打卡程式",
                                       "Local\\CMUH_Skin_AutoClock") == []


def test_find_pids_holding_mutex_parses_wmic_csv_with_commas(monkeypatch):
    """WMIC CSV 的 CommandLine 可能含逗號，仍要抓到正確 PID。"""

    class Result:
        returncode = 0
        stdout = (
            "Node,CommandLine,ProcessId\n"
            'PC,"pythonw.exe C:\\with,comma\\中國醫皮膚科打卡程式.pyw",1234\n'
        )

    monkeypatch.setattr(wc.subprocess, "run", lambda *args, **kwargs: Result())

    assert wc._find_pids_holding_mutex("中國醫皮膚科打卡程式",
                                       "Local\\CMUH_Skin_AutoClock") == [1234]


def test_ensure_program_stale_kill_failure_does_not_start_duplicate(tmp_path, monkeypatch):
    """既有 PID kill 失敗時，不應再啟動第二個 instance。"""
    pyw = tmp_path / "target.pyw"
    log = tmp_path / "target.log"
    pyw.write_text("# shim\n", encoding="utf-8")
    log.write_text("old heartbeat\n", encoding="utf-8")
    stale_ts = time.time() - 10
    os.utime(log, (stale_ts, stale_ts))

    started = []
    monkeypatch.setattr(wc, "claim_action_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr(wc, "kill_pid", lambda pid: False)
    monkeypatch.setattr(
        wc,
        "start_program",
        lambda *args, **kwargs: started.append(args) or 9999,
    )

    msg = wc.ensure_program(
        {
            "name": "打卡",
            "enabled": True,
            "pyw": str(pyw),
            "process_match": "中國醫皮膚科打卡程式",
            "log_path": str(log),
            "max_stale_sec": 1,
        },
        pythonw="pythonw.exe",
        procs=[{"pid": 1234, "cmdline": "pythonw.exe 中國醫皮膚科打卡程式.pyw"}],
        my_pid=9999,
        mode="inner",
        cfg={"action_lock_seconds": 90},
    )

    assert "kill 失敗" in msg
    assert started == []


def test_ensure_program_tolerates_bad_numeric_config(tmp_path, monkeypatch):
    """Bad numeric watchdog config should fall back instead of failing a tick."""
    pyw = tmp_path / "target.pyw"
    pyw.write_text("# shim\n", encoding="utf-8")
    started = []

    monkeypatch.setattr(wc, "claim_action_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        wc,
        "start_program",
        lambda *args, **kwargs: started.append(args) or 123,
    )

    msg = wc.ensure_program(
        {
            "name": "clock",
            "enabled": True,
            "pyw": str(pyw),
            "process_match": "autoclock",
            "log_path": "",
            "max_stale_sec": "bad",
        },
        pythonw="pythonw.exe",
        procs=[],
        my_pid=9999,
        mode="outer",
        cfg={"action_lock_seconds": "bad", "outer_threshold_multiplier": "bad"},
    )

    assert "123" in msg
    assert len(started) == 1


def test_get_loop_timing_tolerates_bad_numeric_config():
    heartbeat, interval = wc.get_loop_timing({
        "heartbeat_log_sec": "bad",
        "check_interval_sec": "bad",
    })

    assert heartbeat == 300
    assert interval == 30


def test_get_loop_timing_enforces_minimums():
    heartbeat, interval = wc.get_loop_timing({
        "heartbeat_log_sec": -10,
        "check_interval_sec": 1,
    })

    assert heartbeat == 1
    assert interval == 5


def test_run_one_tick_logs_crash_loop_suspend_message(monkeypatch):
    messages = []

    monkeypatch.setattr(
        wc,
        "load_config",
        lambda: {"master_enabled": True, "programs": [{"name": "打卡"}]},
    )
    monkeypatch.setattr(wc, "find_pythonw", lambda: "pythonw.exe")
    monkeypatch.setattr(wc, "list_python_processes", lambda: [])
    monkeypatch.setattr(
        wc,
        "ensure_program",
        lambda *args, **kwargs: "⛔ 打卡: crash loop 中，暫停 30 分鐘 [inner]",
    )

    actions = wc.run_one_tick("inner", log_fn=messages.append)

    assert actions == ["⛔ 打卡: crash loop 中，暫停 30 分鐘 [inner]"]
    assert messages == actions


# ─── log-freshness regression tests (2026-05-25) ─────────────────────────
# 防今天踩的坑：v45 把 autoclock max_stale_sec 0→300 但 autoclock idle 時段
# 不印 log → 被 InnerWatchdog 當死的 kill+restart → 整夜 crash loop 沒打到卡。
# 補三組 test 涵蓋 log freshness 三種狀態，下次有人改 max_stale 行為時 CI 攔下來。


def test_ensure_program_does_not_kill_when_log_fresh(tmp_path, monkeypatch):
    """PID 存活 + log 還新鮮 → 不可 kill (autoclock idle 時段必須要被當活的)。"""
    pyw = tmp_path / "target.pyw"
    log = tmp_path / "target.log"
    pyw.write_text("# shim\n", encoding="utf-8")
    log.write_text("recent heartbeat\n", encoding="utf-8")
    # log 剛剛才更新 (now)，max_stale=300 → 應認定健康
    fresh_ts = time.time()
    os.utime(log, (fresh_ts, fresh_ts))

    killed: list = []
    started: list = []
    monkeypatch.setattr(wc, "kill_pid",
                         lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(wc, "start_program",
                         lambda *args, **kwargs: started.append(args) or 8888)

    msg = wc.ensure_program(
        {
            "name": "打卡",
            "enabled": True,
            "pyw": str(pyw),
            "process_match": "中國醫皮膚科打卡程式",
            "log_path": str(log),
            "max_stale_sec": 300,
        },
        pythonw="pythonw.exe",
        procs=[{"pid": 1234, "cmdline": "pythonw.exe 中國醫皮膚科打卡程式.pyw"}],
        my_pid=9999,
        mode="inner",
        cfg={"action_lock_seconds": 90},
    )

    assert killed == [], "log 還新鮮 (<max_stale) 不該 kill PID"
    assert started == [], "log 還新鮮不該 start 新 instance"
    assert "✓" in msg or "log" in msg, f"應該回報健在，實際: {msg!r}"


def test_ensure_program_kills_when_log_stale_beyond_threshold(tmp_path, monkeypatch):
    """PID 存活 + log 超過 max_stale_sec → 必須 kill+restart (真的卡死的情境)。"""
    pyw = tmp_path / "target.pyw"
    log = tmp_path / "target.log"
    pyw.write_text("# shim\n", encoding="utf-8")
    log.write_text("ancient heartbeat\n", encoding="utf-8")
    # log 600s 前更新，max_stale=300 → 已 stale
    stale_ts = time.time() - 600
    os.utime(log, (stale_ts, stale_ts))

    killed: list = []
    started: list = []
    monkeypatch.setattr(wc, "claim_action_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr(wc, "kill_pid",
                         lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(wc, "start_program",
                         lambda *args, **kwargs: started.append(args) or 7777)

    msg = wc.ensure_program(
        {
            "name": "打卡",
            "enabled": True,
            "pyw": str(pyw),
            "process_match": "中國醫皮膚科打卡程式",
            "log_path": str(log),
            "max_stale_sec": 300,
        },
        pythonw="pythonw.exe",
        procs=[{"pid": 1234, "cmdline": "pythonw.exe 中國醫皮膚科打卡程式.pyw"}],
        my_pid=9999,
        mode="inner",
        cfg={"action_lock_seconds": 90},
    )

    assert killed == [1234], f"log stale 應該 kill 1234, 實際 killed={killed}"
    assert len(started) == 1, f"log stale 應該 start 新 instance, 實際 started={started}"
    assert "⟳" in msg or "killed" in msg


def test_ensure_program_skips_log_check_when_max_stale_zero(tmp_path, monkeypatch):
    """max_stale_sec=0 → 完全不檢查 log 新鮮度，純看 PID 存活。

    為什麼重要：某些程式 (e.g. 沒 heartbeat 的小工具) 設 max_stale=0 表示
    「不要管 log，只看 process」。若改了行為，0 變成 fallback 到別的值就會誤殺。
    """
    pyw = tmp_path / "target.pyw"
    log = tmp_path / "target.log"
    pyw.write_text("# shim\n", encoding="utf-8")
    log.write_text("very old log\n", encoding="utf-8")
    # log 1 小時前更新 (不論 max_stale_sec 是什麼大值，此 log 都該算 stale)
    ancient_ts = time.time() - 3600
    os.utime(log, (ancient_ts, ancient_ts))

    killed: list = []
    started: list = []
    monkeypatch.setattr(wc, "kill_pid",
                         lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(wc, "start_program",
                         lambda *args, **kwargs: started.append(args) or 6666)

    msg = wc.ensure_program(
        {
            "name": "打卡",
            "enabled": True,
            "pyw": str(pyw),
            "process_match": "中國醫皮膚科打卡程式",
            "log_path": str(log),
            "max_stale_sec": 0,  # ← 關鍵：跳過 log check
        },
        pythonw="pythonw.exe",
        procs=[{"pid": 1234, "cmdline": "pythonw.exe 中國醫皮膚科打卡程式.pyw"}],
        my_pid=9999,
        mode="inner",
        cfg={"action_lock_seconds": 90},
    )

    assert killed == [], "max_stale_sec=0 不該因為 log 老就 kill"
    assert started == [], "max_stale_sec=0 不該因為 log 老就 restart"
    assert "✓" in msg or "PID" in msg


# ─── WMIC PID fallback regression test (2026-05-25 v14) ──────────────────
# 防將來有人拆掉 ensure_program 裡的 WMIC fallback。背景：consult_query 用
# admin 跑，psutil NtQueryInformationProcess access denied → cmdline 拿不到，
# 害 watchdog 每次心跳都印「psutil 找不到 PID 但 mutex 仍 hold」雜訊。
# WMIC 用不同 API 拿 admin process cmdline，應該被當作 found-PID 路徑走。


def test_ensure_program_uses_wmic_fallback_when_psutil_blind(tmp_path, monkeypatch):
    """psutil 沒看到 admin process PID 時，WMIC fallback 應該補回 PID
    並走正常 Case 2 (PID found + log fresh)，回 ✓ 而不是「找不到 PID 但
    mutex 仍 hold」半死訊息。
    """
    pyw = tmp_path / "consult.pyw"
    log = tmp_path / "consult.log"
    pyw.write_text("# shim\n", encoding="utf-8")
    log.write_text("fresh\n", encoding="utf-8")
    fresh_ts = time.time()
    os.utime(log, (fresh_ts, fresh_ts))

    killed: list = []
    started: list = []
    monkeypatch.setattr(wc, "kill_pid",
                        lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(wc, "start_program",
                        lambda *args, **kwargs: started.append(args) or 5555)

    # 模擬 psutil blind: list_python_processes 回空，WMIC 回 [4321]
    monkeypatch.setattr(wc, "_wmic_find_pids",
                        lambda kw, log_on_empty=True: [4321])

    msg = wc.ensure_program(
        {
            "name": "會診查詢",
            "enabled": True,
            "pyw": str(pyw),
            "process_match": "中國醫皮膚科會診查詢程式",
            "log_path": str(log),
            "max_stale_sec": 180,
        },
        pythonw="pythonw.exe",
        procs=[],  # psutil 看不到 → empty
        my_pid=9999,
        mode="inner",
        cfg={"action_lock_seconds": 90},
    )

    assert killed == [], f"WMIC fallback 找到 PID 不該 kill, killed={killed}"
    assert started == [], f"WMIC fallback 找到 PID 不該 start, started={started}"
    assert "4321" in msg, f"訊息應該含 WMIC 找到的 PID 4321, 實際: {msg!r}"
    assert "✓" in msg, f"應該走 Case 2 健在分支, 實際: {msg!r}"
    # 確認不是走「mutex 仍 hold」半死分支
    assert "mutex 仍 hold" not in msg, \
        f"WMIC 補到 PID 後不該再走半死分支, 實際: {msg!r}"


def test_ensure_program_falls_through_when_wmic_also_blind(tmp_path, monkeypatch):
    """WMIC 也找不到 PID → 維持原本「沒找到 PID」路徑 (mutex+log 救回 / 啟動新 instance)。"""
    pyw = tmp_path / "consult.pyw"
    log = tmp_path / "consult.log"
    pyw.write_text("# shim\n", encoding="utf-8")
    log.write_text("fresh\n", encoding="utf-8")
    fresh_ts = time.time()
    os.utime(log, (fresh_ts, fresh_ts))

    monkeypatch.setattr(wc, "_wmic_find_pids",
                        lambda kw, log_on_empty=True: [])  # WMIC 也 blind
    # 沒設 mutex_name → 不會走 mutex held 分支，會走 log freshness fallback
    msg = wc.ensure_program(
        {
            "name": "會診查詢",
            "enabled": True,
            "pyw": str(pyw),
            "process_match": "中國醫皮膚科會診查詢程式",
            "log_path": str(log),
            "max_stale_sec": 180,
        },
        pythonw="pythonw.exe",
        procs=[],
        my_pid=9999,
        mode="inner",
        cfg={"action_lock_seconds": 90},
    )

    # 沒 PID + log 還新鮮 → 應該回「視為健在」(Fallback 2)
    assert "視為健在" in msg or "找不到 PID 但 log" in msg, \
        f"WMIC 也 blind 時應走 log 新鮮度 fallback, 實際: {msg!r}"
