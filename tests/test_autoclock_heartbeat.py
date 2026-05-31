# -*- coding: utf-8 -*-
"""autoclock idle 時段 heartbeat 行為測試。

防止今天 2026-05-25 早上的 regression：
v45 把 watchdog max_stale_sec 從 0 改 300，但忽略 autoclock 在 idle 時段
(非打卡時間) _scheduler_tick 沒 sched_key 就 return 不印 log → 整夜 log
mtime 不更新 → InnerWatchdog 看 log >300s 沒動 → kill+restart → crash loop。

加 _maybe_emit_heartbeat helper + 這組 test 確保未來改 heartbeat 邏輯時
這些假設不會被打破。
"""
import logging
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# autoclock import 期間會跑 ensure_dependencies — 設定環境讓它跑過
import autoclock  # noqa: E402


def test_heartbeat_emits_when_interval_elapsed(caplog):
    """now - last_log_ts >= interval → 必須 emit heartbeat log + 回傳新 ts。"""
    caplog.set_level(logging.INFO)
    new_ts = autoclock._maybe_emit_heartbeat(
        now=1000.0, last_log_ts=900.0, interval=60.0,
    )
    assert new_ts == 1000.0, "過 interval 後 last_log_ts 必須更新到 now"
    assert any(
        autoclock.HEARTBEAT_MSG in record.message for record in caplog.records
    ), "過 interval 必須印 heartbeat log line"


def test_heartbeat_skips_when_interval_not_elapsed(caplog):
    """now - last_log_ts < interval → 不能 emit + 回傳原 ts。"""
    caplog.set_level(logging.INFO)
    new_ts = autoclock._maybe_emit_heartbeat(
        now=950.0, last_log_ts=900.0, interval=60.0,
    )
    assert new_ts == 900.0, "沒過 interval 時 last_log_ts 必須維持原值"
    assert not any(
        autoclock.HEARTBEAT_MSG in record.message for record in caplog.records
    ), "沒過 interval 不可印 heartbeat log line"


def test_heartbeat_emits_on_first_tick_when_last_log_zero(caplog):
    """第一次 tick (last_log_ts=0) 一定要 emit — 避免 log 一開始就空白等 60s。"""
    caplog.set_level(logging.INFO)
    new_ts = autoclock._maybe_emit_heartbeat(
        now=1000.0, last_log_ts=0.0, interval=60.0,
    )
    assert new_ts == 1000.0
    assert any(
        autoclock.HEARTBEAT_MSG in record.message for record in caplog.records
    )


def test_process_clock_task_uses_time_module_not_time(caplog):
    """[v17 P0 regression] autoclock.py 用 `import time as time_module` 別名，
    所以 process_clock_task 內必須用 time_module.time()，不能用 time.time()。

    背景：2026-05-25 中午 user 打卡失敗 — 12:38-12:50 持續 NameError:
    'time is not defined'。原因是 #102 修 RLock bug 時加 timing log，
    寫成 `time.time()` 但 autoclock 沒 import 純 time，每次中午打卡觸發
    process_clock_task 立刻 crash → 中午沒打到下班卡。

    這 test 解析 process_clock_task source code，確認沒任何 `time.time()`
    或 `time.sleep()` 純名稱呼叫 (必須是 time_module.xxx)。
    """
    import inspect
    import re

    src = inspect.getsource(autoclock.process_clock_task)
    # 找「不是字母或底線」的 word boundary 後接 "time." 的呼叫
    # 排除 time_module / time.something_else / datetime / strftime
    matches = re.findall(r"(?<![\w_])time\.(time|sleep|monotonic|localtime|strftime)\(",
                         src)
    assert not matches, (
        f"process_clock_task 內有純 `time.xxx(` 呼叫 {matches}，autoclock 用 "
        f"`import time as time_module` 別名，會 NameError 害打卡 crash。"
        f"請改 time_module.xxx()。"
    )


def test_heartbeat_interval_constant_is_safe_for_max_stale_300(caplog):
    """[regression] HEARTBEAT_INTERVAL_SEC 必須 << watchdog max_stale_sec。

    今天的 crash loop 就是因為 heartbeat 設 0 (從不印)、max_stale=300
    (允許 5min 沒 log)，結果 log 永遠不更新 → 觸發 stale kill。
    這個 invariant：heartbeat 至少要在 max_stale 的 1/2 內必發一次，
    才能讓 InnerWatchdog 看到「正常活著」。
    autoclock max_stale 預設 300s，heartbeat 必須 ≤ 150s。
    """
    AUTOCLOCK_DEFAULT_MAX_STALE_SEC = 300
    assert autoclock.HEARTBEAT_INTERVAL_SEC <= AUTOCLOCK_DEFAULT_MAX_STALE_SEC / 2, (
        f"HEARTBEAT_INTERVAL_SEC={autoclock.HEARTBEAT_INTERVAL_SEC}s 太久，"
        f"watchdog max_stale={AUTOCLOCK_DEFAULT_MAX_STALE_SEC}s 會誤殺 autoclock。"
        f"請把 heartbeat 設 ≤ {AUTOCLOCK_DEFAULT_MAX_STALE_SEC // 2}s。"
    )


def test_clock_driver_timeouts_are_configured():
    calls = []

    class FakeDriver:
        def set_page_load_timeout(self, value):
            calls.append(("page", value))

        def set_script_timeout(self, value):
            calls.append(("script", value))

    autoclock._configure_clock_driver_timeouts(FakeDriver())

    assert calls == [
        ("page", autoclock._CLOCK_DRIVER_PAGE_LOAD_TIMEOUT),
        ("script", autoclock._CLOCK_DRIVER_SCRIPT_TIMEOUT),
    ]


def test_restart_program_releases_mutex_before_respawn(monkeypatch):
    calls = []

    monkeypatch.setattr(autoclock, "tray_icon_object", None)
    monkeypatch.setattr(autoclock, "release_single_instance",
                        lambda: calls.append("release"))
    monkeypatch.setattr(autoclock, "restart_self",
                        lambda extra: calls.append(("restart", extra)))
    monkeypatch.setattr(sys, "argv", ["autoclock.py", "--configure"])
    autoclock.running.set()

    autoclock.restart_program()

    assert calls == ["release", ("restart", [])]
    assert not autoclock.running.is_set()


def test_run_immediate_test_skips_duplicate_until_worker_finishes(monkeypatch):
    targets = []
    ran = []
    notices = []

    class FakeThread:
        def __init__(self, *, target, name=None, daemon=None):
            targets.append(target)
            self.name = name
            self.daemon = daemon

        def start(self):
            pass

    monkeypatch.setattr(autoclock, "_test_login_gate",
                        autoclock.ActiveTaskGate(stale_after_sec=600))
    monkeypatch.setattr(autoclock, "_run_test_ui",
                        lambda: ran.append("test"))
    monkeypatch.setattr(autoclock, "notify_clock_failure",
                        lambda title, lines: notices.append((title, lines)))
    monkeypatch.setattr(autoclock.threading, "Thread", FakeThread)

    autoclock.run_immediate_test()
    autoclock.run_immediate_test()

    assert len(targets) == 1
    assert notices == [("測試登入執行中", ["請等待目前測試完成"])]
    assert ran == []

    targets[0]()
    autoclock.run_immediate_test()

    assert ran == ["test"]
    assert len(targets) == 2


def test_exit_action_starts_only_one_shutdown_thread(monkeypatch):
    targets = []

    class FakeThread:
        def __init__(self, *, target, name=None, daemon=None):
            targets.append(target)

        def start(self):
            pass

    monkeypatch.setattr(autoclock, "_exit_started", False)
    monkeypatch.setattr(autoclock, "tray_icon_object", None)
    monkeypatch.setattr(autoclock.threading, "Thread", FakeThread)
    autoclock.running.set()

    autoclock.exit_action()
    autoclock.exit_action()

    assert len(targets) == 1
    assert not autoclock.running.is_set()
    autoclock.running.set()


def test_scheduler_tick_skips_duplicate_clock_task_until_worker_finishes(monkeypatch):
    targets = []
    ran = []

    class FakeThread:
        def __init__(self, *, target, name=None, daemon=None):
            targets.append(target)
            self.name = name
            self.daemon = daemon

        def start(self):
            pass

    monkeypatch.setattr(autoclock, "_clock_task_gate",
                        autoclock.ActiveTaskGate(stale_after_sec=600))
    monkeypatch.setattr(autoclock, "get_sched_key", lambda: "mon_am_in")
    monkeypatch.setattr(autoclock, "process_clock_task",
                        lambda key: ran.append(key))
    monkeypatch.setattr(autoclock.threading, "Thread", FakeThread)

    autoclock._scheduler_tick()
    autoclock._scheduler_tick()

    assert len(targets) == 1
    assert ran == []

    targets[0]()
    autoclock._scheduler_tick()

    assert ran == ["mon_am_in"]
    assert len(targets) == 2


def test_scheduler_tick_does_not_start_worker_outside_clock_window(monkeypatch):
    targets = []

    class FakeThread:
        def __init__(self, *, target, name=None, daemon=None):
            targets.append(target)

        def start(self):
            pass

    monkeypatch.setattr(autoclock, "get_sched_key", lambda: None)
    monkeypatch.setattr(autoclock.threading, "Thread", FakeThread)

    autoclock._scheduler_tick()

    assert targets == []


def test_config_app_log_polling_is_bounded():
    src = inspect.getsource(autoclock.ClockApp.poll_log_queue)

    assert "LOG_POLL_MAX_RECORDS" in src
    assert "while not log_queue.empty()" not in src
    assert autoclock.LOG_POLL_MAX_RECORDS <= 500


def test_autoclock_scheduler_clears_old_jobs_before_registering():
    src = inspect.getsource(autoclock.scheduler_loop)

    assert "schedule.clear()" in src
    assert src.index("schedule.clear()") < src.index("schedule.every(1).minute")


def test_autoclock_scheduler_uses_single_self_watchdog_guard():
    scheduler_src = inspect.getsource(autoclock.scheduler_loop)
    guard_src = inspect.getsource(autoclock._ensure_autoclock_self_watchdog)

    assert "_ensure_autoclock_self_watchdog()" in scheduler_src
    assert "threading.Thread(target=_autoclock_self_watchdog" not in scheduler_src
    assert "_self_watchdog_thread_ref.is_alive()" in guard_src


def test_autoclock_sleep_uses_monotonic_clock():
    src = inspect.getsource(autoclock._sleep_while_running)

    assert "time_module.monotonic()" in src
    assert "time_module.time()" not in src
