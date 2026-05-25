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
