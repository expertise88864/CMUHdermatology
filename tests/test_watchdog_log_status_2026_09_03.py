# -*- coding: utf-8 -*-
"""[R3-P3-01] 「log 不在」「讀不到」與「還很新」是三件事。

舊版 `is_log_stale()` 把前兩者都壓成 `stale=False` —— 也就是「這支程式很
健康」。而★行程在跑、log 檔卻根本不存在★正是 logging 壞掉的樣子
(見 `logging_setup`:設定之前有人 module-level `logging.warning`,檔案
handler 就永遠裝不上)。壓成一格之後 watchdog 永遠不會察覺。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.watchdog_core import (  # noqa: E402
    LOG_ABSENT, LOG_OK, LOG_UNREADABLE, is_log_stale, log_status,
)


def test_a_fresh_log_is_ok(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("x", encoding="utf-8")
    stale, _age, status = log_status(p, 180)
    assert (stale, status) == (False, LOG_OK)


def test_an_old_log_is_stale(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("x", encoding="utf-8")
    os.utime(p, (time.time() - 10_000, time.time() - 10_000))
    stale, age, status = log_status(p, 180)
    assert stale is True and status == LOG_OK and age > 180


def test_a_missing_log_is_absent_not_healthy(tmp_path):
    """★核心★:檔案不在 ≠「很新」。要分得出來,否則 logging 壞掉時
    watchdog 會一直以為那支程式很健康。"""
    stale, _age, status = log_status(tmp_path / "nope.log", 180)
    assert status == LOG_ABSENT, "★不在被當成 ok★"
    assert stale is False, "這一批刻意不改重啟行為(見 log_status 的說明)"


def test_an_unreadable_log_is_its_own_state(tmp_path, monkeypatch):
    p = tmp_path / "a.log"
    p.write_text("x", encoding="utf-8")
    import cmuh_common.watchdog_core as wc
    # [第九輪 §5 修正] 「讀不到」= ★stat 失敗★。第一版用 `time.time()` 拋例外來冒充,
    # 那不是生產會發生的失敗形狀;年齡改用進展觀察後它也不再落在 stat 的 try 裡。
    real_stat = Path.stat

    def _stat_denied(self, *a, **k):
        if self == p:
            raise PermissionError("no")
        return real_stat(self, *a, **k)
    monkeypatch.setattr(Path, "stat", _stat_denied)
    # `Path.exists()` 會把 stat 的 OSError 吞成 False → 那會變成「不存在」。生產上
    # 「在、但 stat 被拒」的形狀是 exists 為真而 stat 拋;這裡把 exists 釘成真。
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists",
                        lambda self, *a, **k: True if self == p else real_exists(self, *a, **k))
    assert wc is not None
    stale, _age, status = log_status(p, 180)
    assert status == LOG_UNREADABLE and stale is False


def test_the_three_states_are_distinct():
    assert len({LOG_OK, LOG_ABSENT, LOG_UNREADABLE}) == 3


def test_disabled_is_not_a_failure(tmp_path):
    """`max_stale_sec <= 0` = 這支程式不看 log 新鮮度 —— 那不是「讀不到」。"""
    stale, _age, status = log_status(tmp_path / "nope.log", 0)
    assert (stale, status) == (False, LOG_OK)


def test_the_old_interface_keeps_its_shape(tmp_path):
    """★相容包裝不變★:舊呼叫端拿到的仍是兩個值、行為一致
    (在意「不知道」的才改用 `log_status`)。"""
    p = tmp_path / "a.log"
    p.write_text("x", encoding="utf-8")
    out = is_log_stale(p, 180)
    assert isinstance(out, tuple) and len(out) == 2
    assert is_log_stale(tmp_path / "nope.log", 180) == (False, 0.0)


def _prog(tmp_path, log_name="a.log", max_stale=180):
    """★生產的呼叫形狀★:`log_path` 是【相對於程式根目錄】的,不是絕對路徑;
    鍵名也是 `log_path` 不是 `log`(第一版兩個都寫錯,於是整條 log 判定根本
    沒被走到 —— 測試卻「綠」在別的地方)。"""
    (tmp_path / "x.pyw").write_text("", encoding="utf-8")   # 要真的存在
    return {"name": "打卡", "pyw": str(tmp_path / "x.pyw"),
            "log_path": log_name, "max_stale_sec": max_stale,
            "process_match": "x", "pid_name": "autoclock",
            "mutex_name": ""}


def _run(monkeypatch, tmp_path, *, pids, mutex_held=False, prog=None):
    """走完整的 `ensure_program()` —— ★不是只呼叫 log_status★。

    (外審 R3 剩餘批:第一版的接線測試只比對原始碼字串再直接呼叫
     `log_status`,所以「另一條分支根本沒改」完全量不到。)
    """
    import cmuh_common.single_instance as si
    import cmuh_common.watchdog_core as wc
    monkeypatch.setattr(wc, "find_matching_pids",
                        lambda *a, **k: list(pids))
    monkeypatch.setattr(wc, "_wmic_find_pids", lambda *a, **k: [])
    monkeypatch.setattr(wc, "start_program", lambda *a, **k: 4321)
    monkeypatch.setattr(wc, "kill_pids_verified", lambda *a, **k: [1])
    monkeypatch.setattr(wc, "claim_action_lock", lambda *a, **k: True)
    monkeypatch.setattr(wc, "_record_restart_and_check_crash_loop",
                        lambda *a, **k: True)
    monkeypatch.setattr(si, "is_instance_running", lambda _n: mutex_held)
    monkeypatch.setattr(wc, "_ROOT", tmp_path)   # log_path 相對於它
    return wc.ensure_program(prog or _prog(tmp_path), "pythonw.exe", [],
                             my_pid=1, mode="outer", cfg={})


def test_the_running_branch_says_so_out_loud(tmp_path, monkeypatch, caplog):
    """★行程在跑、log 檔卻不在 → 要留下一筆看得出來的紀錄★
    (而且不可以因此重啟 —— 這一批只記錄)。"""
    import logging
    with caplog.at_level(logging.WARNING):
        out = _run(monkeypatch, tmp_path, pids=[111])
    assert any("無從判斷新鮮度" in r.message for r in caplog.records),         f"★靜音了★:{[r.message for r in caplog.records]}"
    assert "重啟" not in out, out


def test_the_mutex_branch_says_so_too(tmp_path, monkeypatch, caplog):
    """★另一條分支也要★(外審 P1):PID 找不到、靠 mutex 判定健在的那一條
    原本用 `exists()/stat()` 自己判 —— log 不在就直接回「mutex 確認健在」,
    正是這一批要修的靜音失敗。"""
    import logging
    prog = _prog(tmp_path)
    prog["mutex_name"] = "Local" + chr(92) + "X"
    with caplog.at_level(logging.WARNING):
        out = _run(monkeypatch, tmp_path, pids=[], mutex_held=True, prog=prog)
    assert any("無從判斷新鮮度" in r.message for r in caplog.records),         f"★mutex 分支還是靜音的★:{[r.message for r in caplog.records]}"
    assert "健在" in out, out


def test_an_unreadable_log_does_not_blow_up_the_tick(tmp_path, monkeypatch):
    """★尾端不可以再 stat 一次★(外審 P3):`LOG_UNREADABLE` 正是「stat 會拋」
    的那個狀態,再呼叫一次會讓整個 `ensure_program` 拋出去,
    `run_one_tick` 只看得到「tick 例外」,三態就傳不出來了。"""
    import cmuh_common.watchdog_core as wc
    p = tmp_path / "a.log"
    p.write_text("x", encoding="utf-8")
    real = wc.time.time
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        if calls["n"] > 1:               # 第一次給 log_status 用
            raise PermissionError("stat 壞了")
        return real()
    monkeypatch.setattr(wc, "log_status",
                        lambda *a, **k: (False, 0.0, LOG_UNREADABLE))
    out = _run(monkeypatch, tmp_path, pids=[111])
    assert "新鮮度未知" in out, out       # 不可以拋,而且要講清楚


def test_a_fresh_log_still_reports_the_age(tmp_path, monkeypatch):
    """★對照組★:正常情況仍要回報 log 幾秒前更新(不可以退化成沒資訊)。"""
    (tmp_path / "a.log").write_text("x", encoding="utf-8")
    out = _run(monkeypatch, tmp_path, pids=[111])
    assert "前更新" in out, out
