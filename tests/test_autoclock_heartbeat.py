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

import pytest

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
    monkeypatch.setattr(
        autoclock,
        "restart_self",
        lambda extra, hard_exit_code=None:
            calls.append(("restart", extra, hard_exit_code)),
    )
    monkeypatch.setattr(sys, "argv", ["autoclock.py", "--configure"])
    autoclock.running.set()

    autoclock.restart_program()

    assert calls == ["release", ("restart", [], None)]
    assert not autoclock.running.is_set()


def test_restart_program_passes_hard_exit_code_for_background_restart(monkeypatch):
    calls = []

    monkeypatch.setattr(autoclock, "tray_icon_object", None)
    monkeypatch.setattr(autoclock, "release_single_instance",
                        lambda: calls.append("release"))
    monkeypatch.setattr(
        autoclock,
        "restart_self",
        lambda extra, hard_exit_code=None:
            calls.append(("restart", extra, hard_exit_code)),
    )
    monkeypatch.setattr(sys, "argv", ["autoclock.py"])
    autoclock.running.set()

    autoclock.restart_program(hard_exit_code=1)

    assert calls == ["release", ("restart", [], 1)]
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


# ─── [2026-06-01] _check_swipes 嚴格區間：不放寬，打卡須落在官方窗內 ──────────
# 設計：不吸收時鐘偏差(不放寬窗)，改以「比窗起晚 1 分觸發打卡」確保落在官方窗內。
# 對應打卡觸發時間：am_in 7:31 / midday_in 12:31 / pm_out 17:01 / eve_out 21:01。

from datetime import time as _dt_time  # noqa: E402


def test_check_swipes_hits_inside_official_window():
    """打卡落在官方窗內(12:31 於 1230-1300) → 判定已打卡。"""
    assert autoclock._check_swipes(
        "上班", _dt_time(12, 30, 0), _dt_time(13, 0, 0),
        [("1231", "上班")]) is True


def test_check_swipes_strict_excludes_before_window():
    """嚴格：窗起前(12:29 於 1230-1300 之外)不算(不放寬)。"""
    assert autoclock._check_swipes(
        "上班", _dt_time(12, 30, 0), _dt_time(13, 0, 0),
        [("1229", "上班")]) is False


def test_check_swipes_strict_excludes_after_window():
    assert autoclock._check_swipes(
        "下班", _dt_time(17, 0, 0), _dt_time(17, 30, 0),
        [("1731", "下班")]) is False


def test_check_swipes_ignores_wrong_type():
    # 下班紀錄不該被當成上班已打卡
    assert autoclock._check_swipes(
        "上班", _dt_time(12, 30, 0), _dt_time(13, 0, 0),
        [("1245", "下班")]) is False


def test_clock_trigger_times_are_one_minute_into_window():
    """打卡觸發時間應比官方窗起晚 1 分(確保落在窗內)。"""
    assert autoclock.CLOCK_IN_START_TIME == _dt_time(7, 31, 0)
    assert autoclock.CLOCK_MIDDAY_IN_START_TIME == _dt_time(12, 31, 0)
    assert autoclock.TRIGGER_PM_OUT_START_TIME == _dt_time(17, 1, 0)
    assert autoclock.CLOCK_EVE_OUT_START_TIME == _dt_time(21, 1, 0)


# === [stability r4] idle 回收器不得 quit 使用中的 driver ===

def test_idle_janitor_skips_driver_in_use(monkeypatch):
    """driver 標記 in_use(任務進行中)時，idle 回收器絕不 quit；避免單帳號耗時 >15 分
    時砍掉使用中的 driver 造成後續帳號 InvalidSessionId。清 in_use 後才可回收。"""
    pool = autoclock._persistent_driver_pool
    quit_called = []

    class _FakeDriver:
        def quit(self):
            quit_called.append(True)

    # last_used=0 → idle_for 遠超過 timeout；唯一不該 quit 的理由就是 in_use=True
    monkeypatch.setitem(pool, "driver", _FakeDriver())
    monkeypatch.setitem(pool, "last_used", 0.0)
    monkeypatch.setitem(pool, "in_use", True)

    autoclock._idle_driver_janitor()
    assert quit_called == [], "in_use 時不得 quit driver"
    assert pool["driver"] is not None

    # 任務結束清 in_use 後，閒置(last_used 很舊)的 driver 才可被回收
    monkeypatch.setitem(pool, "in_use", False)
    autoclock._idle_driver_janitor()
    assert quit_called == [True]
    assert pool["driver"] is None


# === [opt A2] session 死亡偵測 + 重建 ===

def test_driver_session_alive_probe():
    """session 探測：title 正常→True；丟例外(InvalidSessionId 等)→False；None→False。"""
    class _Alive:
        @property
        def title(self):
            return "ok"

    class _Dead:
        @property
        def title(self):
            raise Exception("invalid session id: session deleted")

    assert autoclock._driver_session_alive(_Alive()) is True
    assert autoclock._driver_session_alive(_Dead()) is False
    assert autoclock._driver_session_alive(None) is False


def test_process_clock_task_rebuilds_dead_session():
    """[opt A2] 任務中途 session 死掉 → 重建 driver 後繼續(原始碼守門，防回歸/被覆蓋)。"""
    import inspect
    src = inspect.getsource(autoclock.process_clock_task)
    assert "_driver_session_alive(driver)" in src
    assert "_get_or_create_clock_driver()" in src
    assert "_MAX_REBUILDS" in src  # 有重建上限，避免無限重建耗光打卡窗


def test_health_declaration_logs_on_failure():
    """[opt B4] 健康宣告偵測到按鈕但流程失敗 → 留 warning(原為 except: pass 完全靜默)。"""
    import inspect
    src = inspect.getsource(autoclock.handle_health_declaration)
    assert "健康宣告流程失敗" in src
    # 找不到按鈕(今天不需宣告)仍靜默 return，不誤報
    assert "今天不需宣告" in src
    assert "except (TimeoutException, WebDriverException):\n        pass" not in src


# === [fix 2026-06-08] 打卡成功後 re-fire 不再跳假「打卡失敗」===

def test_clock_done_marking():
    """本窗標記完成後 _is_clock_done 同日為 True；不同帳號/窗不受影響；跨日自動失效。"""
    autoclock._clock_done.clear()
    try:
        assert autoclock._is_clock_done("mon_midday_in", "N24367") is False
        autoclock._mark_clock_done("mon_midday_in", "N24367")
        assert autoclock._is_clock_done("mon_midday_in", "N24367") is True
        # 不同帳號 / 不同打卡窗互不影響
        assert autoclock._is_clock_done("mon_midday_in", "OTHER") is False
        assert autoclock._is_clock_done("mon_am_in", "N24367") is False
        # 模擬跨日(value 改成舊日期) → 失效，隔天會重新打卡
        autoclock._clock_done[("mon_midday_in", "N24367")] = "2000-01-01"
        assert autoclock._is_clock_done("mon_midday_in", "N24367") is False
        # 防呆：空值不誤判
        autoclock._mark_clock_done("", "N24367")
        assert autoclock._is_clock_done("", "N24367") is False
    finally:
        autoclock._clock_done.clear()


def test_process_clock_task_filters_done_before_driver():
    """原始碼守門：process_clock_task 在開 driver 前就排除本窗已完成帳號(全完成→不開 driver)。"""
    import inspect
    src = inspect.getsource(autoclock.process_clock_task)
    assert "_is_clock_done(schedule_key" in src
    # 過濾必須發生在 _get_or_create_clock_driver 之前
    assert src.index("_is_clock_done(schedule_key") < src.index("_get_or_create_clock_driver()")


def test_process_clock_task_done_accounts_skip_driver(monkeypatch):
    """行為測試：同一打卡窗帳號已完成時，不建立 WebDriver、不重複登入。"""
    schedule_key = "mon_am_in"
    autoclock._clock_done.clear()
    autoclock._mark_clock_done(schedule_key, "N24367")
    monkeypatch.setattr(
        autoclock,
        "load_config",
        lambda: [{"username": "N24367", "schedule": {schedule_key: True}}],
    )
    monkeypatch.setattr(
        autoclock,
        "_get_or_create_clock_driver",
        lambda: pytest.fail("done account should skip driver creation"),
    )

    try:
        autoclock.process_clock_task(schedule_key)
    finally:
        autoclock._clock_done.clear()


def test_process_clock_task_rebuilds_dead_session_behavior(monkeypatch):
    """行為測試：初始 driver session 死掉時，重建後用新 driver 執行帳號。"""
    schedule_key = "mon_am_in"
    first_driver = object()
    rebuilt_driver = object()
    created = [first_driver, rebuilt_driver]
    performed = []

    monkeypatch.setattr(
        autoclock,
        "load_config",
        lambda: [{"username": "N24367", "schedule": {schedule_key: True}}],
    )
    monkeypatch.setattr(autoclock, "_get_or_create_clock_driver",
                        lambda: created.pop(0))
    # [AC-01] 本測試與窗尾防線無關;測試在任意實際時間跑,停用窗尾檢查以免超窗 break。
    monkeypatch.setattr(autoclock, "_clock_window_passed", lambda *a, **k: False)
    monkeypatch.setattr(autoclock, "_driver_session_alive",
                        lambda driver: driver is rebuilt_driver)
    monkeypatch.setattr(autoclock, "WebDriverWait",
                        lambda driver, timeout: ("wait", driver, timeout))
    monkeypatch.setattr(
        autoclock,
        "perform_clock_action",
        lambda driver, wait, *args, **kwargs: performed.append((driver, wait)),
    )
    autoclock.running.set()

    autoclock.process_clock_task(schedule_key)

    assert performed == [(rebuilt_driver, ("wait", rebuilt_driver, 20))]


def test_perform_clock_action_marks_done_on_success_and_record():
    """原始碼守門：打卡成功 與 已有紀錄 兩條路徑都標記本窗完成，避免 re-fire 重登入。"""
    import inspect
    src = inspect.getsource(autoclock.perform_clock_action)
    assert src.count("_mark_clock_done(task_label") >= 2
