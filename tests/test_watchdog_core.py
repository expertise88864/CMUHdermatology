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
