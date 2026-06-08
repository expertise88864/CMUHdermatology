# -*- coding: utf-8 -*-
"""Consult-query pending re-trigger worker coalescing tests."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query  # noqa: E402


def test_pending_retrigger_drain_uses_single_delayed_worker(monkeypatch):
    targets = []
    triggered = []

    class DeferredThread:
        def __init__(self, *, target, name=None, daemon=None):
            targets.append(target)

        def start(self):
            pass

    with consult_query._pending_retriggers_lock:
        consult_query._pending_retriggers.clear()
        consult_query._pending_retrigger_drain_running = False
    monkeypatch.setattr(consult_query.threading, "Thread", DeferredThread)
    monkeypatch.setattr(consult_query, "_sleep_while_running", lambda _sec: True)
    monkeypatch.setattr(
        consult_query, "trigger_job_async",
        lambda label, override_recipients=None:
            triggered.append((label, override_recipients)),
    )

    consult_query._enqueue_pending_retrigger("17:00", None)
    consult_query._drain_pending_retriggers()
    consult_query._enqueue_pending_retrigger("email", ["a@example.com"])
    consult_query._drain_pending_retriggers()

    assert len(targets) == 1
    targets[0]()
    assert triggered == [
        ("17:00", None),
        ("email", ["a@example.com"]),
    ]
    assert consult_query._pending_retrigger_drain_running is False


# === [stability r4] IMAP 逾時 thread 不疊加 ===

def test_imap_check_skips_when_previous_thread_still_alive(monkeypatch):
    """上一條被放生的 IMAPCheck thread 仍 alive 時(force_close 砍不到連線建立前的
    卡死)，本輪不再疊加新 thread，直接回 error；避免長期半死網路下累積 daemon thread。"""
    import threading

    release = threading.Event()
    prev = threading.Thread(target=lambda: release.wait(5), daemon=True)
    prev.start()
    try:
        monkeypatch.setattr(consult_query, "_last_imap_thread", prev)

        res = consult_query._run_imap_check_with_timeout("kw", timeout=0.5)

        # 被跳過 → 沒有新生 thread(引用仍是 prev)、回 error result 帶 skip 訊息
        assert consult_query._last_imap_thread is prev
        assert res.get("triggered") is False
        assert "skip" in res.get("error", "").lower()
    finally:
        release.set()
        prev.join(timeout=1)


def test_imap_check_runs_when_no_previous_thread(monkeypatch):
    """沒有殘留 thread 時正常執行 check_trigger，並在成功後清掉引用。"""
    import cmuh_common.imap_reader as imap_reader

    monkeypatch.setattr(consult_query, "_last_imap_thread", None)
    monkeypatch.setattr(
        imap_reader, "check_trigger",
        lambda kw: {"triggered": False, "scanned": 1, "matched": 0,
                    "matched_senders": [], "samples": [], "error": ""},
    )

    res = consult_query._run_imap_check_with_timeout("kw", timeout=5.0)

    assert res.get("scanned") == 1
    assert res.get("error") == ""
    # 正常結束後清掉引用，不擋下一輪 poll
    assert consult_query._last_imap_thread is None


# === [opt A1] 畸形 From fallback 去重 + [opt B3] cooldown log 節流 ===

def test_no_sender_sentinel_dedup():
    """[opt A1] fallback 哨兵 key 去重：首次放行(False)，去重窗內第二次被擋(True)，
    避免畸形 From + mark-read 失敗時每 20s 重複截圖+寄信。"""
    with consult_query._trigger_dedup_lock:
        consult_query._recent_trigger_senders.pop("__no_sender__", None)
    try:
        assert consult_query._trigger_is_duplicate("__no_sender__") is False
        assert consult_query._trigger_is_duplicate("__no_sender__") is True
    finally:
        with consult_query._trigger_dedup_lock:
            consult_query._recent_trigger_senders.pop("__no_sender__", None)


def test_fallback_dedup_and_cooldown_throttle_present():
    """原始碼守門：A1 fallback 用哨兵 key 去重；B3 cooldown log 改時間節流(非 %60 modulo)。"""
    import pathlib
    src = pathlib.Path(consult_query.__file__).read_text(encoding="utf-8")
    # A1: malformed-From fallback 分支套用哨兵 key 去重
    assert '_trigger_is_duplicate("__no_sender__")' in src
    # B3: cooldown 進度 log 改時間節流，移除永遠命中不到的 %60 modulo
    # (用 "if int(...)" 比對實際程式碼，避免誤抓說明此修正的註解)
    assert "last_cooldown_log >= 60" in src
    assert "if int(remaining) % 60" not in src
