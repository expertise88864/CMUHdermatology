# -*- coding: utf-8 -*-
"""[v17 2026-05-25] consult_query retry backoff + pending re-trigger 測試。

防今天 (2026-05-25) 觀察到的兩個設計缺陷再 regression：
  1. 16:54 IMAP 觸發 → 醫院 systemftp transient 慢 → 3 次 retry 全失敗
     原本 retry 間 sleep 3s 太密，最後一次也撞在同個 server 卡死期
  2. 17:00 排程觸發被 task_gate 擋掉 (前一個 IMAP retry 還在跑)
     原本被擋就「掉地上」，user 永遠收不到 17:00 排程信
"""
import os
import sys
import threading
import time
import json
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import consult_query  # noqa: E402


def test_load_config_normalizes_non_string_list_values(tmp_path, monkeypatch):
    cfg_path = tmp_path / "consult_query_config.json"
    cfg_path.write_text(json.dumps({
        "recipients": [" a@example.com ", 123, None, ""],
        "test_recipients": "bad",
        "email_trigger_recipients": [" b@example.com "],
        "allowed_trigger_senders": [" USER@EXAMPLE.COM ", 456],
        "retry_count": 999,
    }), encoding="utf-8")
    monkeypatch.setattr(consult_query, "CONFIG_FILE", cfg_path)

    cfg = consult_query.load_config()

    assert cfg["recipients"] == ["a@example.com", "123"]
    assert cfg["test_recipients"] == consult_query.DEFAULT_CONFIG["test_recipients"]
    assert cfg["email_trigger_recipients"] == ["b@example.com"]
    assert cfg["allowed_trigger_senders"] == ["user@example.com", "456"]
    assert cfg["retry_count"] == consult_query.MAX_RETRY_COUNT


def test_retry_count_normalization_bounds_bad_and_large_values():
    assert consult_query._normalize_retry_count("bad") == \
        consult_query.DEFAULT_CONFIG["retry_count"]
    assert consult_query._normalize_retry_count(0) == 3
    assert consult_query._normalize_retry_count(-5) == 1
    assert consult_query._normalize_retry_count(999) == \
        consult_query.MAX_RETRY_COUNT


def test_pending_retrigger_enqueue_and_drain(monkeypatch):
    """[v17 regression] 排程被擋時 enqueue，當前 job release 後 drain 補跑。"""
    # 清空既有 queue
    with consult_query._pending_retriggers_lock:
        consult_query._pending_retriggers.clear()

    # 直接 enqueue 一筆
    consult_query._enqueue_pending_retrigger("17:00", None)
    with consult_query._pending_retriggers_lock:
        assert "17:00" in consult_query._pending_retriggers
        assert consult_query._pending_retriggers["17:00"] is None

    # drain 應該 dispatch 並清空 queue (但補跑是在 thread 內 delayed，
    # 我們只驗證 queue 被清空 + thread 已啟動，不等補跑實際執行)
    triggered = []
    monkeypatch.setattr(consult_query, "trigger_job_async",
                        lambda label, override_recipients=None:
                            triggered.append((label, override_recipients)))
    # 把 delay 改 0 讓 test 不要等
    monkeypatch.setattr(consult_query, "_RETRIGGER_DELAY_SEC", 0.01)

    consult_query._drain_pending_retriggers()

    # 等補跑 thread 跑完 (短 delay + 一次 trigger)
    time.sleep(0.2)

    with consult_query._pending_retriggers_lock:
        assert consult_query._pending_retriggers == {}, \
            "drain 後 queue 必須清空"
    assert triggered == [("17:00", None)], \
        f"應該補跑 17:00 排程一次，實際: {triggered}"


def test_pending_retrigger_same_label_overrides_not_stacks(monkeypatch):
    """同個 trigger_label 進 queue 兩次只保留一筆 (避免堆積)。"""
    with consult_query._pending_retriggers_lock:
        consult_query._pending_retriggers.clear()

    consult_query._enqueue_pending_retrigger("17:00", ["a@example.com"])
    consult_query._enqueue_pending_retrigger("17:00", ["b@example.com"])

    with consult_query._pending_retriggers_lock:
        assert len(consult_query._pending_retriggers) == 1
        assert consult_query._pending_retriggers["17:00"] == [
            "a@example.com", "b@example.com"]


def test_pending_retrigger_merges_email_recipients_without_duplicates():
    """多封 email 觸發被 gate 擋住時，不可讓後一封覆蓋前一封寄件人。"""
    with consult_query._pending_retriggers_lock:
        consult_query._pending_retriggers.clear()

    consult_query._enqueue_pending_retrigger(
        "email", ["a@example.com", "B@example.com"])
    consult_query._enqueue_pending_retrigger(
        "email", ["b@example.com", "c@example.com"])

    with consult_query._pending_retriggers_lock:
        assert consult_query._pending_retriggers["email"] == [
            "a@example.com", "B@example.com", "c@example.com"]


def test_pending_retrigger_does_not_overwrite_recipients_with_none():
    """A later fallback email trigger must not erase already parsed senders."""
    with consult_query._pending_retriggers_lock:
        consult_query._pending_retriggers.clear()

    consult_query._enqueue_pending_retrigger("email", ["a@example.com"])
    consult_query._enqueue_pending_retrigger("email", None)

    with consult_query._pending_retriggers_lock:
        assert consult_query._pending_retriggers["email"] == ["a@example.com"]


def test_drain_with_empty_queue_does_nothing(monkeypatch):
    """queue 是空的 → drain 不啟動 thread / 不呼叫 trigger_job_async。"""
    with consult_query._pending_retriggers_lock:
        consult_query._pending_retriggers.clear()

    triggered = []
    monkeypatch.setattr(consult_query, "trigger_job_async",
                        lambda label, override_recipients=None:
                            triggered.append(label))

    consult_query._drain_pending_retriggers()
    time.sleep(0.1)
    assert triggered == []


def test_pending_retrigger_drain_skips_when_app_is_stopping(monkeypatch):
    with consult_query._pending_retriggers_lock:
        consult_query._pending_retriggers.clear()

    triggered = []

    class ImmediateThread:
        def __init__(self, *, target, name=None, daemon=None):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            self.target()

    monkeypatch.setattr(consult_query.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(consult_query, "trigger_job_async",
                        lambda label, override_recipients=None:
                            triggered.append((label, override_recipients)))

    consult_query._enqueue_pending_retrigger("email", ["a@example.com"])
    consult_query.running.clear()
    try:
        consult_query._drain_pending_retriggers()
    finally:
        consult_query.running.set()

    assert triggered == []
    with consult_query._pending_retriggers_lock:
        assert consult_query._pending_retriggers == {}


def test_pending_retrigger_delay_is_cancelable():
    src = inspect.getsource(consult_query._drain_pending_retriggers)

    assert "_sleep_while_running(_RETRIGGER_DELAY_SEC)" in src
    assert "time.sleep(_RETRIGGER_DELAY_SEC)" not in src


def test_config_app_log_polling_is_bounded():
    src = inspect.getsource(consult_query.ConfigApp._poll_log)

    assert "LOG_POLL_MAX_RECORDS" in src
    assert "while not log_queue.empty()" not in src
    assert consult_query.LOG_POLL_MAX_RECORDS <= 500


def test_consult_scheduler_uses_single_self_watchdog_guard():
    scheduler_src = inspect.getsource(consult_query.scheduler_loop)
    guard_src = inspect.getsource(consult_query._ensure_scheduler_self_watchdog)

    assert "_ensure_scheduler_self_watchdog()" in scheduler_src
    assert "threading.Thread(target=_scheduler_self_watchdog" not in scheduler_src
    assert "_self_watchdog_thread_ref.is_alive()" in guard_src


def test_consult_sleep_uses_monotonic_clock():
    src = inspect.getsource(consult_query._sleep_while_running)

    assert "time.monotonic()" in src
    assert "time.time()" not in src


def test_tray_test_email_skips_duplicate_until_worker_finishes(monkeypatch):
    targets = []
    sent = []
    notices = []

    class FakeThread:
        def __init__(self, *, target, name=None, daemon=None):
            targets.append(target)
            self.name = name
            self.daemon = daemon

        def start(self):
            pass

    monkeypatch.setattr(consult_query, "_test_email_gate",
                        consult_query.ActiveTaskGate(stale_after_sec=600))
    monkeypatch.setattr(consult_query, "_send_test_email",
                        lambda: sent.append("sent"))
    monkeypatch.setattr(consult_query, "_notify",
                        lambda title, message: notices.append((title, message)))
    monkeypatch.setattr(consult_query.threading, "Thread", FakeThread)

    consult_query._tray_test_email()
    consult_query._tray_test_email()

    assert len(targets) == 1
    assert notices == [("測試寄信執行中", "請等待目前測試完成")]
    assert sent == []

    targets[0]()
    consult_query._tray_test_email()

    assert sent == ["sent"]
    assert len(targets) == 2


def test_configure_mode_has_dedicated_single_instance_guard():
    import inspect

    src = inspect.getsource(consult_query.main)

    assert "CONFIG_MUTEX_NAME" in src
    assert "ensure_single_instance(CONFIG_MUTEX_NAME)" in src
    assert "release_single_instance()" in src
    assert "ConfigApp().mainloop()" in src


def test_tray_configure_uses_shared_python_launcher():
    src = inspect.getsource(consult_query._tray_configure)

    assert "launch_python_script(" in src
    assert 'args=["--configure"]' in src
    assert "cwd=get_app_dir()" in src
    assert "subprocess.Popen(" not in src


def test_backoff_schedule_is_exponential():
    """[v17 regression] retry sleep 必須是 exponential backoff (3s, 30s, 90s)，
    不能改回固定 3s — 那樣會撞在同個 server 卡死期。"""
    import inspect
    src = inspect.getsource(consult_query._do_full_job)
    # 驗證 BACKOFF_SCHEDULE 是有遞增的數字 list
    assert "BACKOFF_SCHEDULE" in src, "_do_full_job 必須定義 BACKOFF_SCHEDULE"
    # 簡單檢查含三個 backoff 值的字串
    # (硬編 [3, 30, 90] 是 design choice — 改值要同步改 test 提醒未來)
    assert "[3, 30, 90]" in src, (
        "BACKOFF_SCHEDULE 應該是 [3, 30, 90] (exponential)。"
        "若有意改值請同步改此 test 的 assert"
    )
