"""Read-only operational summaries contain codes, never clinical content."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import zipfile
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from cmuh_common.runtime_diagnostics import (
    DiagnosticEvents, DiagnosticRun, DiagnosticStore, MAX_EVENTS, RETENTION_SECONDS,
    last_success, latest_run, render_safe_events,
)
from cmuh_common.runtime_summary import (
    clock_summary, consult_summary, read_clock_state, read_consult_delivery,
)


def _event(run_id, started, observed, stage, outcome, *, error="none",
           reason="none"):
    return {"run_id": run_id, "run_started_at": started,
            "observed_at": observed, "stage": stage, "outcome": outcome,
            "duration_ms": 10, "error": error, "reason": reason}


def test_store_is_bounded_allowlisted_and_read_does_not_create(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = DiagnosticStore(path, "consult")
    assert store.read(now=1_000_000) == []
    assert not path.exists()
    private = "Patient Name 9876543210 / secret@example.test"
    run = DiagnosticRun(store, wall_clock=lambda: 1_000_000,
                        monotonic=lambda: 12.0)
    run.emit(private, private, error=private, reason=private,
             since=-100_000)
    for index in range(MAX_EVENTS + 10):
        store.record(run_id=run.run_id, run_started_at=1_000_000,
                     stage="query", outcome="ok", duration_ms=index,
                     observed_at=1_000_000 + index)
    events = store.read(now=1_000_000 + MAX_EVENTS + 10)
    assert len(events) == MAX_EVENTS
    assert private not in render_safe_events(events)
    assert private.encode() not in path.read_bytes()
    assert {e["stage"] for e in events} == {"query"}
    with sqlite3.connect(path) as conn:
        run_sequence = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='runs'").fetchone()[0]
    assert run_sequence == 1
    assert store.read(now=1_000_000 + MAX_EVENTS + RETENTION_SECONDS + 20) == []


@pytest.mark.parametrize("domain,success_stage,success_outcome", [
    ("consult", "send", "accepted"),
    ("clock", "done", "official_confirmed"),
])
def test_recent_success_survives_noisy_event_cap(
        tmp_path, domain, success_stage, success_outcome):
    store = DiagnosticStore(tmp_path / "events.sqlite3", domain)
    run_id = "d" * 32
    store.record(run_id=run_id, run_started_at=1_000_000,
                 observed_at=1_000_001, stage=success_stage,
                 outcome=success_outcome)
    for index in range(MAX_EVENTS + 20):
        store.record(run_id=run_id, run_started_at=1_000_000,
                     observed_at=1_000_002 + index,
                     stage="query" if domain == "consult" else "read",
                     outcome="started")
    events = store.read(now=1_000_050 + MAX_EVENTS)
    assert len(events) <= MAX_EVENTS
    assert last_success(events, domain=domain) == 1_000_001


def test_last_clock_success_follows_completion_even_if_older_claim_finishes_last(
        tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    first, second = "a" * 32, "b" * 32
    store.record(run_id=first, run_started_at=100,
                 observed_at=101, stage="confirm", outcome="official_confirmed")
    store.record(run_id=second, run_started_at=200,
                 observed_at=202, stage="done", outcome="official_confirmed")
    store.record(run_id=first, run_started_at=100,
                 observed_at=203, stage="done", outcome="official_confirmed")
    for index in range(MAX_EVENTS + 10):
        store.record(run_id=f"{index + 2:032x}",
                     run_started_at=204 + index,
                     observed_at=204 + index, stage="done",
                     outcome="skipped", reason="flow_busy")
    events = store.read(now=215 + MAX_EVENTS)
    assert last_success(events, domain="clock") == 203


def test_clock_failure_survives_noisy_following_account(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    run_id = "e" * 32
    store.record(run_id=run_id, run_started_at=1_000_000,
                 observed_at=1_000_001, stage="done", outcome="read_unknown",
                 error="read", reason="check_portal")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id=run_id, run_started_at=1_000_000,
                     observed_at=1_000_002 + index,
                     stage="read", outcome="started")
    store.record(run_id=run_id, run_started_at=1_000_000,
                 observed_at=1_000_023 + MAX_EVENTS,
                 stage="done", outcome="official_confirmed")
    events = store.read(now=1_000_024 + MAX_EVENTS)
    assert len(events) <= MAX_EVENTS
    assert any(event["outcome"] == "read_unknown" for event in events)


def test_distinct_clock_failures_survive_noisy_following_accounts(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    run_id = "e" * 32
    store.record(run_id=run_id, run_started_at=1_000_000,
                 observed_at=1_000_001, stage="done", outcome="failed",
                 error="storage", reason="check_storage")
    store.record(run_id=run_id, run_started_at=1_000_000,
                 observed_at=1_000_002, stage="done", outcome="read_unknown",
                 error="read", reason="check_portal")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id=run_id, run_started_at=1_000_000,
                     observed_at=1_000_003 + index,
                     stage="read", outcome="started")
    store.record(run_id=run_id, run_started_at=1_000_000,
                 observed_at=1_000_024 + MAX_EVENTS,
                 stage="done", outcome="official_confirmed")
    events = store.read(now=1_000_025 + MAX_EVENTS)
    assert len(events) <= MAX_EVENTS
    assert {(item["error"], item["reason"]) for item in events
            if item["stage"] == "done"} >= {
                ("storage", "check_storage"), ("read", "check_portal")}
    text = clock_summary(events, None,
                         today=datetime.fromtimestamp(1_000_025 + MAX_EVENTS).date(),
                         now=1_000_025 + MAX_EVENTS)
    assert "請檢查本機儲存空間或權限" in text
    assert "請檢查打卡網站狀態" in text


def test_clock_account_start_times_do_not_consume_failure_reservations(
        tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    instant = [1_000_000.0]

    def wall_clock():
        instant[0] += 1
        return instant[0]

    run = DiagnosticRun(store, wall_clock=wall_clock)
    run.mark_action_started()
    run.emit("done", "failed", error="storage", reason="check_storage")
    for _ in range(MAX_EVENTS // 2 + 4):
        run.mark_action_started()
        run.emit("done", "auth_failed", error="auth",
                 reason="fix_credentials")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id="f" * 32, run_started_at=1_100_000,
                     observed_at=1_100_001 + index,
                     stage="read", outcome="started")
    events = store.read(now=1_100_002 + MAX_EVENTS + 20)
    assert any(item["run_id"] == run.run_id and
               item["error"] == "storage" for item in events)


def test_newest_clock_batch_keeps_failure_when_older_run_repeats_category(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    current = "a" * 32
    older = "b" * 32
    store.record(run_id=current, run_started_at=2000,
                 observed_at=2001, stage="done", outcome="failed",
                 error="storage", reason="check_storage")
    store.record(run_id=older, run_started_at=1000,
                 observed_at=2002, stage="done", outcome="failed",
                 error="storage", reason="check_storage")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id=current, run_started_at=2000,
                     observed_at=2003 + index, stage="read", outcome="started")
    store.record(run_id=current, run_started_at=2000,
                 observed_at=2024 + MAX_EVENTS,
                 stage="done", outcome="official_confirmed")
    events = store.read(now=2025 + MAX_EVENTS)
    assert len(events) <= MAX_EVENTS
    assert any(item["run_id"] == current and item["error"] == "storage"
               for item in events)
    text = clock_summary(events, None,
                         today=datetime.fromtimestamp(2025 + MAX_EVENTS).date(),
                         now=2025 + MAX_EVENTS)
    assert "請檢查本機儲存空間或權限" in text


def test_interleaved_clock_batches_keep_each_runs_failure(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    resumed = "a" * 32
    newer = "b" * 32
    store.record(run_id=resumed, run_started_at=1000,
                 observed_at=1001, stage="done", outcome="failed",
                 error="storage", reason="check_storage")
    store.record(run_id=newer, run_started_at=2000,
                 observed_at=2001, stage="done", outcome="failed",
                 error="storage", reason="check_storage")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id=newer, run_started_at=2000,
                     observed_at=2002 + index, stage="read", outcome="started")
    store.record(run_id=resumed, run_started_at=2300,
                 observed_at=2301, stage="done", outcome="official_confirmed")
    events = store.read(now=2302)
    assert len(events) <= MAX_EVENTS
    assert any(item["run_id"] == resumed and item["error"] == "storage"
               for item in events)
    text = clock_summary(events, None,
                         today=datetime.fromtimestamp(2302).date(), now=2302)
    assert "部分帳號未確認" in text
    assert "請檢查本機儲存空間或權限" in text


def test_noisy_fully_contended_clock_runs_never_revive_as_executed(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    holder = "a" * 32
    store.record(run_id=holder, run_started_at=1000,
                 observed_at=1001, stage="done", outcome="read_unknown",
                 error="read", reason="check_portal")
    for index in range(MAX_EVENTS + 20):
        run_id = f"{index:032x}"
        started = 1002 + 3 * index
        store.record(run_id=run_id, run_started_at=started,
                     observed_at=started, stage="browser", outcome="ok")
        store.record(run_id=run_id, run_started_at=started,
                     observed_at=started + 1, stage="done", outcome="skipped",
                     reason="verify_clock")
        store.record(run_id=run_id, run_started_at=started,
                     observed_at=started + 2, stage="done", outcome="skipped",
                     reason="flow_busy")
    events = store.read(now=2000)
    assert len(events) <= MAX_EVENTS
    assert latest_run(events, now=2000)["run_id"] == holder
    assert "請檢查打卡網站狀態" in clock_summary(
        events, None, today=datetime.fromtimestamp(2000).date(), now=2000)


def test_fully_contended_clock_window_without_holder_result_is_visible(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    store.record(run_id="a" * 32, run_started_at=100,
                 observed_at=101, stage="done", outcome="official_confirmed")
    store.record(run_id="b" * 32, run_started_at=200,
                 observed_at=201, stage="done", outcome="skipped",
                 reason="flow_busy")
    text = clock_summary(store.read(now=202), None,
                         today=datetime.fromtimestamp(202).date(), now=202)
    assert "最近觀察：略過" in text
    assert "請至官方系統確認，勿重複點擊" in text


def test_stale_lock_warning_survives_full_failure_retention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=100,
                 observed_at=101, stage="roster", outcome="empty_roster")
    for index in range(MAX_EVENTS + 5):
        store.record(run_id=f"{index + 1:032x}",
                     run_started_at=200 + index, observed_at=201 + index,
                     stage="query", outcome="read_failed",
                     error="read", reason="verify_his")
    store.record(run_id="f" * 32, run_started_at=500,
                 observed_at=501, stage="done", outcome="skipped",
                 reason="check_job")
    for index in range(3):
        store.record(run_id=f"{index + 500:032x}",
                     run_started_at=502 + index, observed_at=502 + index,
                     stage="done", outcome="skipped", reason="flow_busy")
    events = store.read(now=510)
    assert any(item["reason"] == "check_job" for item in events)


def test_stale_lock_skip_shown_after_nonfinal_roster_result():
    holder = "a" * 32
    skipped = "b" * 32
    events = [
        _event(holder, 100, 101, "roster", "empty_roster"),
        _event(skipped, 200, 201, "done", "skipped", reason="check_job"),
    ]
    assert latest_run(events, now=202)["run_id"] == skipped
    assert "請檢查執行紀錄並人工核對結果" in consult_summary(
        events, None, now=202)


def test_stale_lock_skip_shown_while_send_has_only_started():
    holder, skipped = "a" * 32, "b" * 32
    events = [
        _event(holder, 100, 101, "send", "started"),
        _event(skipped, 200, 201, "done", "skipped", reason="check_job"),
    ]
    assert latest_run(events, now=202)["run_id"] == skipped
    assert "請檢查執行紀錄並人工核對結果" in consult_summary(
        events, None, now=202)


def test_late_parse_cannot_hide_stale_lock_for_unfinished_query():
    holder, skipped = "a" * 32, "b" * 32
    events = [
        _event(holder, 100, 101, "query", "started"),
        _event(skipped, 200, 102, "done", "skipped", reason="check_job"),
        _event(holder, 100, 103, "parse", "ok"),
    ]
    assert latest_run(events, now=104)["run_id"] == skipped
    assert "請檢查執行紀錄並人工核對結果" in consult_summary(
        events, None, now=104)


def test_parse_only_run_after_lost_caller_write_cannot_hide_new_his_failure(
        tmp_path):
    store = DiagnosticStore(tmp_path / "consult_diag.sqlite3", "consult")
    older = DiagnosticRun(store, wall_clock=lambda: 100.0)
    with sqlite3.connect(store.path) as blocker:
        blocker.execute("CREATE TABLE IF NOT EXISTS locked (id INTEGER)")
        blocker.execute("BEGIN EXCLUSIVE")
        older.emit("query", "started")
    newer = DiagnosticRun(store, wall_clock=lambda: 200.0)
    newer.emit("query", "read_failed", error="read", reason="verify_his")
    older.emit("parse", "ok")
    events = store.read(now=210)
    assert latest_run(events, now=210)["run_id"] == newer.run_id
    text = consult_summary(events, None, now=210)
    assert "查詢結果：查詢失敗" in text
    assert "解析紀錄無對應流程結果" in text
    assert "請人工核對 HIS 會診清單" in text


@pytest.mark.parametrize("error,reason", [
    ("none", "none"), ("ledger", "check_storage"),
])
def test_late_uncertain_consult_acceptance_keeps_newer_his_failure(
        tmp_path, error, reason):
    store = DiagnosticStore(tmp_path / "consult_diag.sqlite3", "consult")
    older = DiagnosticRun(store, wall_clock=lambda: 100.0)
    with sqlite3.connect(store.path) as blocker:
        blocker.execute("CREATE TABLE IF NOT EXISTS locked (id INTEGER)")
        blocker.execute("BEGIN EXCLUSIVE")
        older.emit("query", "started")
    newer = DiagnosticRun(store, wall_clock=lambda: 200.0)
    newer.emit("query", "read_failed", error="read", reason="verify_his")
    older.emit("send", "accepted", error=error, reason=reason)
    text = consult_summary(store.read(now=210), None, now=210)
    assert "診斷順序不明" in text
    assert "另有查詢失敗" in text
    assert "請人工核對 HIS 會診清單" in text
    assert "請至官方系統確認，勿重複點擊" not in text
    if error == "ledger":
        assert "請檢查本機儲存空間或權限" in text


def test_outlook_pending_without_ledger_survives_later_success():
    earlier, later = "a" * 32, "b" * 32
    events = [
        _event(earlier, 100, 101, "send", "pending",
               error="transport", reason="verify_delivery"),
        _event(later, 200, 201, "send", "accepted"),
    ]
    text = consult_summary(events, None, now=202)
    assert "另有無帳本寄送待確認" in text
    assert "請核對寄件備份，勿直接重寄" in text


def test_outlook_pending_survives_other_pending_and_retention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    outlook_run = "a" * 32
    store.record(run_id=outlook_run, run_started_at=1000,
                 observed_at=1001, stage="send", outcome="pending",
                 error="transport", reason="verify_delivery")
    store.record(run_id="b" * 32, run_started_at=1002,
                 observed_at=1003, stage="send", outcome="pending",
                 error="timeout", reason="verify_delivery")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id=f"{index + 500:032x}",
                     run_started_at=1004 + index,
                     observed_at=1005 + index, stage="query",
                     outcome="read_failed", error="read",
                     reason="verify_his")
    store.record(run_id="c" * 32, run_started_at=2000,
                 observed_at=2001, stage="send", outcome="accepted")
    events = store.read(now=2002)
    assert any(item["run_id"] == outlook_run for item in events)
    text = consult_summary(events, None, now=2002)
    assert "另有無帳本寄送待確認" in text
    assert "請核對寄件備份，勿直接重寄" in text


def test_current_outlook_pending_retains_earlier_ledger_storage_failure():
    earlier, later = "a" * 32, "b" * 32
    events = [
        _event(earlier, 100, 101, "send", "pending",
               error="ledger", reason="check_storage"),
        _event(later, 200, 201, "send", "pending",
               error="transport", reason="verify_delivery"),
    ]
    text = consult_summary(events, None, now=202)
    assert "請檢查本機儲存空間或權限" in text
    assert "請核對寄件備份，勿直接重寄" in text


def test_read_only_store_uri_escapes_fragment_characters(tmp_path):
    path = tmp_path / "clinic#1" / "events.sqlite3"
    store = DiagnosticStore(path, "consult")
    store.record(run_id="a" * 32, run_started_at=100,
                 observed_at=100, stage="query", outcome="ok")
    assert store.read(now=101)[0]["outcome"] == "ok"


def test_interrupted_run_order_migration_cannot_hide_new_his_failure(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    with sqlite3.connect(store.path) as conn:
        conn.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, "
                     "run_id TEXT NOT NULL, run_started_at REAL NOT NULL, "
                     "observed_at REAL NOT NULL, stage TEXT NOT NULL, "
                     "outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL, "
                     "error TEXT NOT NULL, reason TEXT NOT NULL)")
        conn.execute("CREATE TABLE runs (run_order INTEGER PRIMARY KEY "
                     "AUTOINCREMENT, run_id TEXT NOT NULL, "
                     "run_started_at REAL NOT NULL, UNIQUE(run_id,run_started_at))")
        conn.execute("INSERT INTO events VALUES (100,?,?,?,?,?,?,?,?)", (
            "a" * 32, 100.0, 101.0, "send", "accepted", 1, "none", "none"))
    assert store.read(now=201).unavailable
    assert store.record(run_id="b" * 32, run_started_at=200,
                        observed_at=201, stage="query", outcome="read_failed",
                        error="read", reason="verify_his")
    events = store.read(now=202)
    assert not events.unavailable
    assert latest_run(events, now=202)["run_id"] == "b" * 32
    assert "查詢結果：查詢失敗" in consult_summary(events, None, now=202)


def test_corrupt_or_untrusted_event_content_never_enters_export(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = DiagnosticStore(path, "clock")
    store.record(run_id="a" * 32, run_started_at=100,
                 observed_at=100, stage="read", outcome="no_record")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE events SET duration_ms=?", ("private patient name",))
    events = store.read(now=110)
    assert events == []
    assert events.unavailable
    assert "診斷紀錄無法讀取" in clock_summary(
        events, None, today=datetime.fromtimestamp(110).date(), now=110)
    assert "diagnostics_read_unavailable" in render_safe_events(events)
    text = render_safe_events([{
        "observed_at": 100, "stage": "private patient name",
        "outcome": "private patient name", "duration_ms": 1,
        "error": "private patient name", "reason": "private patient name",
    }])
    assert "private patient name" not in text


def test_safe_export_omits_absolute_event_times():
    event = _event("a" * 32, 1_790_550_000, 1_790_550_001,
                   "query", "ok")
    text = render_safe_events([event])
    assert "1790550001" not in text
    assert "query ok 10ms none none" in text


def test_locked_diagnostic_database_requests_retry_without_storage_repair(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    store.record(run_id="a" * 32, run_started_at=100,
                 observed_at=101, stage="read", outcome="no_record")
    with sqlite3.connect(store.path) as blocker:
        blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
        blocker.execute("BEGIN EXCLUSIVE")
        events = store.read(now=110)
    assert events == []
    assert events.retry_later and not events.unavailable
    text = clock_summary(events, None,
                         today=datetime.fromtimestamp(110).date(), now=110)
    assert "稍後重試" in text
    assert "請檢查本機儲存空間或權限" not in text
    assert "diagnostics_read_busy" in render_safe_events(events)


@pytest.mark.parametrize("column,value", [
    ("observed_at", None),
    ("observed_at", "invalid time"),
    ("run_started_at", 0),
])
def test_malformed_diagnostic_timestamps_are_not_empty_history(
        tmp_path, column, value):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=100,
                 observed_at=101, stage="query", outcome="started")
    with sqlite3.connect(store.path) as conn:
        if value is None:
            conn.execute("DROP TABLE events")
            conn.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, "
                         "run_id TEXT, run_started_at REAL, observed_at REAL, "
                         "stage TEXT, outcome TEXT, duration_ms INTEGER, "
                         "error TEXT, reason TEXT)")
            conn.execute("INSERT INTO events VALUES (1,?,?,?,?,?,?,?,?)", (
                "a" * 32, 100, None, "query", "started", 0, "none", "none"))
        else:
            conn.execute(f"UPDATE events SET {column}=?", (value,))
    events = store.read(now=110)
    assert events.unavailable
    assert "診斷紀錄無法讀取" in consult_summary(events, None, now=110)


def test_backward_wall_clock_does_not_hide_terminal_query_failure(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    run_id = "a" * 32
    store.record(run_id=run_id, run_started_at=100,
                 observed_at=110, stage="query", outcome="started")
    store.record(run_id=run_id, run_started_at=100,
                 observed_at=109, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    events = store.read(now=120)
    assert latest_run(events, now=120)["outcome"] == "read_failed"
    assert "查詢結果：查詢失敗" in consult_summary(events, None, now=120)


def test_backward_clock_recovered_query_survives_retention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    run_id = "a" * 32
    for observed, stage, outcome in (
            (110, "query", "read_failed"),
            (109, "query", "ok"),
            (108, "roster", "ok"),
            (107, "send", "accepted")):
        store.record(run_id=run_id, run_started_at=100,
                     observed_at=observed, stage=stage, outcome=outcome,
                     error="read" if outcome == "read_failed" else "none",
                     reason="verify_his" if outcome == "read_failed" else "none")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id="b" * 32, run_started_at=111,
                     observed_at=112 + index, stage="done",
                     outcome="skipped", reason="flow_busy")
    events = store.read(now=113 + MAX_EVENTS + 20)
    text = consult_summary(events, None, now=113 + MAX_EVENTS + 20)
    assert "階段：寄送｜狀態：寄送端接受" in text
    assert "查詢結果：名單已讀取" in text


def test_large_backward_clock_adjustment_keeps_terminal_failure(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    run_id = "a" * 32
    store.record(run_id=run_id, run_started_at=1000,
                 observed_at=1001, stage="query", outcome="started")
    store.record(run_id=run_id, run_started_at=1000,
                 observed_at=900, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    events = store.read(now=1002)
    assert events.clock_anomaly
    assert latest_run(events, now=1002)["outcome"] == "read_failed"
    text = consult_summary(events, None, now=1002)
    assert "查詢結果：查詢失敗" in text
    assert "請核對系統時鐘" in text


def test_backward_clock_between_runs_keeps_new_failure_over_old_success(
        tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    old_run, new_run = "a" * 32, "b" * 32
    store.record(run_id=old_run, run_started_at=1000,
                 observed_at=1001, stage="send", outcome="accepted")
    store.record(run_id=new_run, run_started_at=990,
                 observed_at=991, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    store.record(run_id=old_run, run_started_at=1000,
                 observed_at=1002, stage="parse", outcome="ok")
    events = store.read(now=1003)
    assert latest_run(events, now=1003)["run_id"] == new_run
    assert "查詢結果：查詢失敗" in consult_summary(events, None, now=1003)


def test_backward_clock_new_failure_does_not_expire_before_old_success(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    old_run, new_run = "a" * 32, "b" * 32
    store.record(run_id=old_run, run_started_at=1000,
                 observed_at=1001, stage="send", outcome="accepted")
    store.record(run_id=new_run, run_started_at=990,
                 observed_at=991, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    now = RETENTION_SECONDS + 992
    events = store.read(now=now)
    assert latest_run(events, now=now)["run_id"] == new_run
    assert latest_run(events, now=now)["outcome"] == "read_failed"
    # A subsequent append also prunes old database rows; the failed run must
    # remain until the preceding accepted result expires.
    store.record(run_id="c" * 32, run_started_at=now,
                 observed_at=now, stage="done", outcome="skipped",
                 reason="flow_busy")
    events = store.read(now=now)
    assert latest_run(events, now=now)["run_id"] == new_run
    assert latest_run(events, now=now)["outcome"] == "read_failed"


def test_late_older_run_success_does_not_revive_after_new_failure(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    old_run, new_run = "a" * 32, "b" * 32
    store.record(run_id=old_run, run_started_at=800,
                 observed_at=801, stage="query", outcome="started")
    store.record(run_id=new_run, run_started_at=890,
                 observed_at=891, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    # The older run finishes after the newer run was acquired.
    store.record(run_id=old_run, run_started_at=800,
                 observed_at=1001, stage="send", outcome="accepted")
    now = RETENTION_SECONDS + 892
    events = store.read(now=now)
    assert latest_run(events, now=now) is None
    assert events.ordering_uncertain
    assert "執行順序不明" in consult_summary(events, None, now=now)
    # Read-only access to a pre-migration database uses first-event order.
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TABLE runs")
    events = store.read(now=now)
    assert latest_run(events, now=now) is None
    store.record(run_id="c" * 32, run_started_at=now,
                 observed_at=now, stage="done", outcome="skipped",
                 reason="flow_busy")
    events = store.read(now=now)
    assert latest_run(events, now=now)["run_id"] == "c" * 32
    assert any(e["run_id"] == old_run for e in events)


def test_newer_failed_run_expiry_uses_observation_not_start(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=801, stage="query", outcome="started")
    store.record(run_id="b" * 32, run_started_at=1000,
                 observed_at=991, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=1001, stage="send", outcome="accepted")
    now = RETENTION_SECONDS + 992
    events = store.read(now=now)
    assert latest_run(events, now=now) is None
    assert events.ordering_uncertain


def test_existing_run_order_table_backfills_expiry_marker(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=801, stage="query", outcome="started")
    store.record(run_id="b" * 32, run_started_at=1000,
                 observed_at=991, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    with sqlite3.connect(store.path) as conn:
        conn.execute("ALTER TABLE runs DROP COLUMN max_retention_at")
        conn.execute("ALTER TABLE runs DROP COLUMN executed")
        conn.execute("ALTER TABLE runs DROP COLUMN expired_barrier")
        conn.execute("ALTER TABLE events DROP COLUMN retention_at")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=1001, stage="send", outcome="accepted")
    now = RETENTION_SECONDS + 992
    events = store.read(now=now)
    assert latest_run(events, now=now) is None
    assert events.ordering_uncertain
    with sqlite3.connect(store.path) as conn:
        marker = conn.execute(
            "SELECT max_retention_at,executed FROM runs WHERE run_id=?",
            ("b" * 32,)).fetchone()
    assert marker == (991, 1)


def test_expired_newer_run_keeps_older_unresolved_delivery_warning(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    old_run = "a" * 32
    store.record(run_id=old_run, run_started_at=800,
                 observed_at=801, stage="query", outcome="started")
    store.record(run_id="b" * 32, run_started_at=890,
                 observed_at=891, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    store.record(run_id=old_run, run_started_at=800,
                 observed_at=1001, stage="send", outcome="pending",
                 error="ledger", reason="verify_delivery")
    now = RETENTION_SECONDS + 892
    events = store.read(now=now)
    assert latest_run(events, now=now) is None
    assert any(e["run_id"] == old_run and e["outcome"] == "pending"
               for e in events)
    text = consult_summary(events, None, now=now)
    assert "勿直接重寄" in text
    assert "請檢查本機儲存空間或權限" in text


def test_expired_new_run_does_not_reappear_after_old_parse_arrives(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    old_run, new_run = "a" * 32, "b" * 32
    store.record(run_id=old_run, run_started_at=1000,
                 observed_at=1000, stage="query", outcome="started")
    store.record(run_id=new_run, run_started_at=1001,
                 observed_at=1001, stage="send", outcome="pending",
                 reason="verify_delivery")
    now = RETENTION_SECONDS + 1002
    assert store.read(now=now) == []
    store.record(run_id=old_run, run_started_at=1000,
                 observed_at=now, stage="parse", outcome="ok")
    events = store.read(now=now)
    assert not any(e["run_id"] == new_run for e in events)
    assert latest_run(events, now=now) is None
    assert events.ordering_uncertain


def test_newer_failed_run_parser_cannot_revive_older_success(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=801, stage="query", outcome="started")
    store.record(run_id="b" * 32, run_started_at=890,
                 observed_at=891, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=1001, stage="send", outcome="accepted")
    store.record(run_id="b" * 32, run_started_at=890,
                 observed_at=1002, stage="parse", outcome="ok")
    now = RETENTION_SECONDS + 892
    events = store.read(now=now)
    assert any(e["run_id"] == "b" * 32 and e["stage"] == "parse"
               for e in events)
    assert latest_run(events, now=now) is None
    assert events.ordering_uncertain


def test_run_cap_keeps_expired_execution_order_barrier(tmp_path, monkeypatch):
    import cmuh_common.runtime_diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "MAX_RUNS", 8)
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=801, stage="query", outcome="started")
    store.record(run_id="b" * 32, run_started_at=890,
                 observed_at=891, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=1001, stage="send", outcome="accepted")
    now = RETENTION_SECONDS + 892
    for index in range(20):
        store.record(run_id=f"{index + 2:032x}",
                     run_started_at=now + index,
                     observed_at=now + index, stage="done",
                     outcome="skipped", reason="flow_busy")
    events = store.read(now=now + 20)
    assert latest_run(events, now=now + 20)["run_id"] != "a" * 32
    assert events.ordering_uncertain
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT executed,max_retention_at FROM runs WHERE run_id=?",
            ("b" * 32,)).fetchone() == (1, 891)


def test_rollback_after_age_pruning_keeps_order_barrier(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=801, stage="query", outcome="started")
    store.record(run_id="b" * 32, run_started_at=890,
                 observed_at=891, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=1001, stage="send", outcome="accepted")
    now = RETENTION_SECONDS + 892
    store.record(run_id="c" * 32, run_started_at=now,
                 observed_at=now, stage="done", outcome="skipped",
                 reason="flow_busy")
    events = store.read(now=now - 2)
    assert latest_run(events, now=now - 2)["run_id"] != "a" * 32
    assert events.ordering_uncertain


def test_backward_clock_across_runs_marks_anomaly(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=1000,
                 observed_at=1001, stage="send", outcome="accepted")
    store.record(run_id="b" * 32, run_started_at=900,
                 observed_at=901, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    events = store.read(now=1002)
    assert events.clock_anomaly
    assert "系統時間異常" in consult_summary(events, None, now=1002)


def test_backward_clock_terminal_result_does_not_expire_before_start(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    run_id = "a" * 32
    store.record(run_id=run_id, run_started_at=1000,
                 observed_at=1001, stage="query", outcome="started")
    store.record(run_id=run_id, run_started_at=1000,
                 observed_at=991, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    now = RETENTION_SECONDS + 992
    events = store.read(now=now)
    assert latest_run(events, now=now)["outcome"] == "read_failed"
    assert store.read(now=RETENTION_SECONDS + 1002) == []


def test_backward_clock_official_confirmation_does_not_expire_early(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    run_id = "a" * 32
    store.record(run_id=run_id, run_started_at=1000,
                 observed_at=1001, stage="read", outcome="no_record")
    store.record(run_id=run_id, run_started_at=1000,
                 observed_at=991, stage="done", outcome="official_confirmed")
    now = RETENTION_SECONDS + 992
    events = store.read(now=now)
    assert latest_run(events, now=now)["outcome"] == "official_confirmed"
    store.record(run_id="b" * 32, run_started_at=now,
                 observed_at=now, stage="done", outcome="skipped",
                 reason="flow_busy")
    events = store.read(now=now)
    assert latest_run(events, now=now)["outcome"] == "official_confirmed"


def test_event_cap_cannot_remove_rollback_retention_anchor(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    old_run, new_run = "a" * 32, "b" * 32
    store.record(run_id=old_run, run_started_at=800,
                 observed_at=801, stage="query", outcome="started")
    store.record(run_id=new_run, run_started_at=1100,
                 observed_at=1100, stage="query", outcome="started")
    store.record(run_id=new_run, run_started_at=1100,
                 observed_at=990, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    store.record(run_id=old_run, run_started_at=800,
                 observed_at=1050, stage="send", outcome="accepted")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id=f"{index + 2:032x}",
                     run_started_at=1101 + index,
                     observed_at=1101 + index, stage="done",
                     outcome="skipped", reason="flow_busy")
    now = RETENTION_SECONDS + 1000
    events = store.read(now=now)
    assert latest_run(events, now=now)["run_id"] == new_run
    assert latest_run(events, now=now)["outcome"] == "read_failed"


def test_corrected_far_future_clock_does_not_poison_retention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    now = 1_000_000
    far_future = now + 365 * 86400
    store.record(run_id="a" * 32, run_started_at=far_future - 1,
                 observed_at=far_future, stage="send", outcome="accepted")
    before = store.read(now=now)
    assert before.clock_anomaly
    assert latest_run(before, now=now) is None
    store.record(run_id="b" * 32, run_started_at=now - 1,
                 observed_at=now, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    events = store.read(now=now)
    assert latest_run(events, now=now)["outcome"] == "read_failed"
    assert not any(e["run_id"] == "a" * 32 for e in events)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT retention_at FROM events WHERE run_id=?",
            ("b" * 32,)).fetchone()[0] <= (
                now + RETENTION_SECONDS)
        assert conn.execute("SELECT COUNT(*) FROM events WHERE run_id=?",
                            ("a" * 32,)).fetchone()[0] == 1


def test_backward_clock_correction_preserves_ledgerless_send_warning(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    correct_time = 10_000_000.0
    backward_time = correct_time - 8 * 86400
    store.record(run_id="a" * 32, run_started_at=correct_time - 1,
                 observed_at=correct_time, stage="send", outcome="pending",
                 error="ledger", reason="verify_delivery")
    store.record(run_id="b" * 32, run_started_at=backward_time - 1,
                 observed_at=backward_time, stage="query",
                 outcome="read_failed", error="read", reason="verify_his")
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE run_id=?",
            ("a" * 32,)).fetchone()[0] == 1
    restored = store.read(now=correct_time + 1)
    assert any(item["run_id"] == "a" * 32 and
               item["outcome"] == "pending" for item in restored)
    text = consult_summary(restored, None, now=correct_time + 1)
    assert "另有無帳本寄送待確認" in text
    assert "請核對寄件備份，勿直接重寄" in text


def test_hidden_future_executed_run_blocks_older_current_success(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    now = 10_000_000.0
    store.record(run_id="a" * 32, run_started_at=now - 2,
                 observed_at=now - 1, stage="send", outcome="accepted")
    store.record(run_id="b" * 32,
                 run_started_at=now + 365 * 86400 - 1,
                 observed_at=now + 365 * 86400,
                 stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    # The current writer age-prunes the old row; a retained row from an older
    # writer or interrupted cleanup must still not become the current result.
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "INSERT INTO events(run_id,run_started_at,observed_at,retention_at,"
            "stage,outcome,duration_ms,error,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("a" * 32, now - 2, now - 1, now - 1, "send", "accepted", 0,
             "none", "none"))
    events = store.read(now=now)
    assert events.clock_anomaly and events.ordering_uncertain
    assert latest_run(events, now=now) is None


def test_older_writer_nullable_retention_remains_readable_and_expires(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    assert store.record(run_id="a" * 32, run_started_at=1000,
                        observed_at=1001, stage="query", outcome="ok")
    with sqlite3.connect(store.path) as conn:
        conn.execute("ALTER TABLE events DROP COLUMN retention_at")
    assert store.record(run_id="a" * 32, run_started_at=1000,
                        observed_at=1002, stage="query", outcome="ok")
    with sqlite3.connect(store.path) as conn:
        conn.execute("INSERT INTO runs(run_id,run_started_at) VALUES (?,?)",
                     ("b" * 32, 1100))
        conn.execute(
            "INSERT INTO events(run_id,run_started_at,observed_at,stage,"
            "outcome,duration_ms,error,reason) VALUES (?,?,?,?,?,?,?,?)",
            ("b" * 32, 1100, 1101, "send", "pending", 0, "ledger",
             "verify_delivery"))
    read = store.read(now=1102)
    assert not read.unavailable
    assert any(item["run_id"] == "b" * 32 for item in read)
    assert store.record(run_id="c" * 32,
                        run_started_at=1101 + RETENTION_SECONDS + 2,
                        observed_at=1101 + RETENTION_SECONDS + 3,
                        stage="query", outcome="ok")
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE run_id=?",
            ("b" * 32,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT executed,max_retention_at,expired_barrier FROM runs "
            "WHERE run_id=?", ("b" * 32,)).fetchone() == (1, 1101, 1)


def test_diagnostic_append_survives_concurrent_summary_snapshot(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    assert store.record(run_id="a" * 32, run_started_at=1000,
                        observed_at=1001, stage="query", outcome="ok")
    with sqlite3.connect(store.path) as reader:
        assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        # Hold a read snapshot longer than the writer's 20 ms busy timeout.
        assert store.record(run_id="b" * 32, run_started_at=1002,
                            observed_at=1003, stage="send", outcome="accepted")
        assert reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert latest_run(store.read(now=1004), now=1004)["outcome"] == "accepted"


def test_busy_diagnostic_writer_marks_order_without_storage_repair(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    assert store.record(run_id="a" * 32, run_started_at=900,
                        observed_at=901, stage="send", outcome="accepted")
    run = DiagnosticRun(store, wall_clock=lambda: 1000.0)
    with sqlite3.connect(store.path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        run.emit("query", "started")
    run.emit("send", "accepted")
    events = store.read(now=1001)
    latest = latest_run(events, now=1001)
    assert latest["reason"] == "order_uncertain"
    assert latest["error"] == "none"
    text = consult_summary(events, None, now=1001)
    assert "診斷順序不明" in text
    assert "請檢查本機儲存空間或權限" not in text


def test_legacy_migration_does_not_spread_future_time_to_pending_send(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    now = 10_000_000.0
    future = now + 365 * 86400
    assert store.record(run_id="a" * 32, run_started_at=future - 1,
                        observed_at=future, stage="send", outcome="accepted")
    with sqlite3.connect(store.path) as conn:
        conn.execute("ALTER TABLE events DROP COLUMN retention_at")
        conn.execute("INSERT INTO runs(run_id,run_started_at) VALUES (?,?)",
                     ("b" * 32, now - 1))
        conn.execute(
            "INSERT INTO events(run_id,run_started_at,observed_at,stage,"
            "outcome,duration_ms,error,reason) VALUES (?,?,?,?,?,?,?,?)",
            ("b" * 32, now - 1, now, "send", "pending", 0, "ledger",
             "verify_delivery"))
    assert store.record(run_id="c" * 32, run_started_at=now,
                        observed_at=now + 1,
                        stage="query", outcome="read_failed",
                        error="read", reason="verify_his")
    events = store.read(now=now + 2)
    assert any(item["run_id"] == "b" * 32 and
               item["outcome"] == "pending" for item in events)
    assert "另有無帳本寄送待確認" in consult_summary(
        events, None, now=now + 2)


@pytest.mark.parametrize("stage,outcome,reason", [
    ("done", "skipped", "flow_busy"),
    ("reconcile", "ok", "none"),
])
def test_expired_nonexecution_does_not_hide_holder_failure(
        tmp_path, stage, outcome, reason):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    holder = "a" * 32
    store.record(run_id=holder, run_started_at=800,
                 observed_at=801, stage="query", outcome="started")
    store.record(run_id="b" * 32, run_started_at=890,
                 observed_at=891, stage=stage, outcome=outcome,
                 reason=reason)
    store.record(run_id=holder, run_started_at=800,
                 observed_at=1001, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    now = RETENTION_SECONDS + 892
    events = store.read(now=now)
    assert latest_run(events, now=now)["run_id"] == holder
    assert latest_run(events, now=now)["outcome"] == "read_failed"


def test_clock_expired_newer_run_cannot_make_old_confirmation_current(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=801, stage="read", outcome="no_record")
    store.record(run_id="b" * 32, run_started_at=890,
                 observed_at=891, stage="done", outcome="read_unknown",
                 error="read", reason="check_portal")
    store.record(run_id="a" * 32, run_started_at=800,
                 observed_at=1001, stage="done", outcome="official_confirmed")
    now = RETENTION_SECONDS + 892
    events = store.read(now=now)
    assert latest_run(events, now=now) is None
    assert events.ordering_uncertain
    assert "執行順序不明" in clock_summary(
        events, None, today=datetime.fromtimestamp(now).date(), now=now)


def test_rollback_new_query_survives_contended_event_retention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=1000,
                 observed_at=1001, stage="send", outcome="accepted")
    store.record(run_id="b" * 32, run_started_at=990,
                 observed_at=991, stage="query", outcome="started")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id=f"{index + 2:032x}",
                     run_started_at=980 - index, observed_at=1002 + index,
                     stage="done", outcome="skipped", reason="flow_busy")
    events = store.read(now=1023 + MAX_EVENTS)
    assert latest_run(events, now=1023 + MAX_EVENTS)["run_id"] == "b" * 32
    assert "查詢結果：讀取中" in consult_summary(
        events, None, now=1023 + MAX_EVENTS)


def test_unrenderable_diagnostic_timestamp_is_reported_as_unavailable(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "clock")
    store.record(run_id="a" * 32, run_started_at=100,
                 observed_at=101, stage="read", outcome="no_record")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE events SET observed_at=?", (1e100,))
    events = store.read(now=110)
    assert events == [] and events.unavailable
    assert "診斷紀錄無法讀取" in clock_summary(
        events, None, today=datetime.fromtimestamp(110).date(), now=110)


def test_stage_duration_uses_monotonic_clock_not_wall_time(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    monotonic_ticks = iter([100.0, 101.25])
    run = DiagnosticRun(store, wall_clock=lambda: 1_000_000.0,
                        monotonic=lambda: next(monotonic_ticks))
    started = run.start()
    run.emit("query", "ok", since=started)
    assert store.read(now=1_000_000.0)[0]["duration_ms"] == 1250


def test_late_old_run_cannot_replace_newer_result():
    old = "a" * 32
    new = "b" * 32
    events = [
        _event(old, 100, 210, "query", "read_failed"),
        _event(new, 200, 205, "send", "accepted"),
    ]
    assert latest_run(events, now=220)["outcome"] == "accepted"
    text = consult_summary(events, None, now=220)
    assert "寄送端接受" in text
    assert "查詢失敗" not in text


def test_intermediate_empty_parse_does_not_claim_a_completed_consult():
    run = "f" * 32
    parsed = _event(run, 100, 101, "parse", "empty_roster")
    failed = _event(run, 100, 102, "query", "read_failed")
    assert last_success([parsed, failed], domain="consult") is None
    assert "最後成功：無紀錄" in consult_summary(
        [parsed, failed], None, now=103)
    done = _event(run, 100, 104, "done", "empty_roster")
    assert last_success([parsed, done], domain="consult") == 104


def test_late_hidden_parse_cannot_hide_query_failure_or_settled_send():
    run = "1" * 32
    failed = _event(run, 100, 102, "query", "read_failed",
                    error="read", reason="verify_his")
    late_parse = _event(run, 100, 103, "parse", "ok")
    text = consult_summary([failed, late_parse], None, now=104)
    assert "階段：HIS 查詢｜狀態：查詢失敗" in text
    assert "錯誤分類：讀取" in text
    assert "人工處理：請人工核對 HIS" in text

    accepted = _event(run, 100, 105, "send", "accepted")
    still_later_parse = _event(run, 100, 106, "parse", "ok")
    text = consult_summary([accepted, still_later_parse], None, now=107)
    assert "狀態：寄送端接受" in text

    recovered = _event(run, 100, 104, "query", "ok")
    retry_parse = _event(run, 100, 105, "parse", "ok")
    text = consult_summary([failed, recovered, retry_parse], None, now=106)
    assert "狀態：查詢失敗" not in text


def test_previous_attempt_parse_cannot_override_new_query_start_or_result():
    run = "f" * 32
    first_failed = _event(run, 100, 101, "query", "read_failed",
                          error="read", reason="verify_his")
    retry_started = _event(run, 100, 102, "query", "started")
    late_parse = _event(run, 100, 103, "parse", "roster_unknown",
                        error="parse", reason="verify_his")
    text = consult_summary([first_failed, retry_started, late_parse],
                           None, now=104)
    assert "狀態：進行中" in text
    assert "查詢結果：讀取中" in text
    retry_ok = _event(run, 100, 105, "query", "ok")
    still_late = _event(run, 100, 106, "parse", "roster_unknown",
                        error="parse", reason="verify_his")
    text = consult_summary([first_failed, retry_started, late_parse,
                            retry_ok, still_late], None, now=107)
    assert "狀態：完成" in text
    assert "查詢結果：名單已讀取" in text
    verified_unknown = _event(run, 100, 108, "roster", "roster_unknown",
                              error="parse", reason="verify_his")
    last_old_parse = _event(run, 100, 109, "parse", "ok")
    text = consult_summary([first_failed, retry_started, late_parse,
                            retry_ok, still_late, verified_unknown,
                            last_old_parse], None, now=110)
    assert "查詢結果：名單未知" in text


def test_overlapping_skipped_poll_cannot_hide_active_his_failure():
    active = "a" * 32
    overlap = "b" * 32
    started = _event(active, 100, 101, "query", "started")
    busy = _event(overlap, 102, 103, "done", "skipped",
                  reason="flow_busy")
    failed = _event(active, 100, 104, "query", "read_failed",
                    error="read", reason="verify_his")
    text = consult_summary([started, busy, failed], None, now=105)
    assert "狀態：查詢失敗" in text
    assert "請人工核對 HIS" in text


def test_expired_lock_skip_warns_until_holder_reports_his_failure():
    holder = "a" * 32
    overlap = "b" * 32
    started = _event(holder, 100, 101, "query", "started")
    stale_skip = _event(overlap, 102, 103, "done", "skipped",
                        error="unknown", reason="check_job")
    waiting = consult_summary([started, stale_skip], None, now=104)
    assert "請檢查執行紀錄" in waiting
    failed = _event(holder, 100, 105, "query", "read_failed",
                    error="read", reason="verify_his")
    settled = consult_summary([started, stale_skip, failed], None, now=106)
    assert "狀態：查詢失敗" in settled
    assert "請人工核對 HIS" in settled


def test_expired_lock_skip_after_his_failure_does_not_hide_failure():
    holder = "a" * 32
    overlap = "b" * 32
    failed = _event(holder, 100, 105, "query", "read_failed",
                    error="read", reason="verify_his")
    stale_skip = _event(overlap, 106, 107, "done", "skipped",
                        error="unknown", reason="check_job")
    text = consult_summary([failed, stale_skip], None, now=108)
    assert "階段：HIS 查詢｜狀態：查詢失敗" in text
    assert "請人工核對 HIS" in text


def test_late_parse_and_expired_lock_skip_do_not_hide_holder_failure():
    holder = "a" * 32
    overlap = "b" * 32
    failed = _event(holder, 100, 105, "query", "read_failed",
                    error="read", reason="verify_his")
    late_parse = _event(holder, 100, 106, "parse", "ok")
    stale_skip = _event(overlap, 107, 108, "done", "skipped",
                        error="unknown", reason="check_job")
    text = consult_summary([failed, late_parse, stale_skip], None, now=109)
    assert "階段：HIS 查詢｜狀態：查詢失敗" in text
    assert "請人工核對 HIS" in text


def test_redaction_failure_survives_noisy_lock_contention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=1000,
                 observed_at=1001, stage="send", outcome="accepted")
    store.record(run_id="b" * 32, run_started_at=1002,
                 observed_at=1003, stage="redact", outcome="failed",
                 error="privacy", reason="verify_his")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id="c" * 32, run_started_at=1004,
                     observed_at=1005 + index, stage="done",
                     outcome="skipped", reason="flow_busy")
    events = store.read(now=1006 + MAX_EVENTS + 20)
    assert len(events) <= MAX_EVENTS
    assert any(item["stage"] == "redact" and item["outcome"] == "failed"
               for item in events)
    assert "階段：去識別｜狀態：失敗" in consult_summary(
        events, None, now=1006 + MAX_EVENTS + 20)


def test_unfinished_his_holder_survives_noisy_lock_contention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    store.record(run_id="a" * 32, run_started_at=1000,
                 observed_at=1001, stage="send", outcome="accepted")
    store.record(run_id="b" * 32, run_started_at=1002,
                 observed_at=1003, stage="query", outcome="started")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id="c" * 32, run_started_at=1004,
                     observed_at=1005 + index, stage="done",
                     outcome="skipped", reason="flow_busy")
    events = store.read(now=1006 + MAX_EVENTS + 20)
    assert len(events) <= MAX_EVENTS
    assert any(item["run_id"] == "b" * 32 and item["stage"] == "query"
               for item in events)
    assert "查詢結果：讀取中" in consult_summary(
        events, None, now=1006 + MAX_EVENTS + 20)


def test_unfinished_his_query_with_worker_parse_survives_retention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    holder = "b" * 32
    store.record(run_id=holder, run_started_at=1002,
                 observed_at=1003, stage="query", outcome="started")
    store.record(run_id=holder, run_started_at=1002,
                 observed_at=1004, stage="parse", outcome="empty_roster")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id="c" * 32, run_started_at=1005,
                     observed_at=1006 + index, stage="done",
                     outcome="skipped", reason="flow_busy")
    events = store.read(now=1007 + MAX_EVENTS + 20)
    assert len(events) <= MAX_EVENTS
    text = consult_summary(events, None, now=1007 + MAX_EVENTS + 20)
    assert "查詢結果：讀取中" in text
    assert "確定空名單" not in text


def test_recovered_his_query_keeps_success_after_retention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    holder = "b" * 32
    store.record(run_id=holder, run_started_at=1002,
                 observed_at=1003, stage="query", outcome="read_failed",
                 error="read", reason="verify_his")
    store.record(run_id=holder, run_started_at=1002,
                 observed_at=1004, stage="query", outcome="ok")
    store.record(run_id=holder, run_started_at=1002,
                 observed_at=1005, stage="roster", outcome="ok")
    store.record(run_id=holder, run_started_at=1002,
                 observed_at=1006, stage="send", outcome="accepted")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id="c" * 32, run_started_at=1007,
                     observed_at=1008 + index, stage="done",
                     outcome="skipped", reason="flow_busy")
    text = consult_summary(store.read(now=1009 + MAX_EVENTS + 20), None,
                           now=1009 + MAX_EVENTS + 20)
    assert "寄送端接受" in text
    assert "查詢結果：名單已讀取" in text
    assert "查詢結果：查詢失敗" not in text


def test_parse_only_run_cannot_evict_recovered_his_query(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    holder = "b" * 32
    for observed, stage, outcome in (
            (1003, "query", "read_failed"),
            (1004, "query", "ok"),
            (1005, "roster", "ok"),
            (1006, "send", "accepted")):
        store.record(run_id=holder, run_started_at=1002,
                     observed_at=observed, stage=stage, outcome=outcome,
                     error="read" if outcome == "read_failed" else "none",
                     reason="verify_his" if outcome == "read_failed" else "none")
    store.record(run_id="d" * 32, run_started_at=1007,
                 observed_at=1008, stage="parse", outcome="ok")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id="c" * 32, run_started_at=1009,
                     observed_at=1010 + index, stage="done",
                     outcome="skipped", reason="flow_busy")
    text = consult_summary(store.read(now=1011 + MAX_EVENTS + 20), None,
                           now=1011 + MAX_EVENTS + 20)
    assert "查詢結果：名單已讀取" in text
    assert "查詢結果：查詢失敗" not in text


def test_older_recovered_query_does_not_reappear_after_retention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    older = "a" * 32
    for observed, stage, outcome in (
            (1001, "query", "read_failed"),
            (1002, "query", "ok"),
            (1003, "roster", "ok"),
            (1004, "send", "accepted")):
        store.record(run_id=older, run_started_at=1000,
                     observed_at=observed, stage=stage, outcome=outcome,
                     error="read" if outcome == "read_failed" else "none",
                     reason="verify_his" if outcome == "read_failed" else "none")
    newer = DiagnosticRun(store, wall_clock=lambda: 1100.0)
    with sqlite3.connect(store.path) as blocker:
        blocker.execute("BEGIN EXCLUSIVE")
        newer.emit("query", "started")
    newer.emit("send", "accepted")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id=f"{index + 2000:032x}",
                     run_started_at=1200 + index,
                     observed_at=1201 + index, stage="done",
                     outcome="skipped", reason="flow_busy")
    events = store.read(now=1202 + MAX_EVENTS + 20)
    assert any(item["run_id"] == newer.run_id for item in events)
    text = consult_summary(events, None, now=1202 + MAX_EVENTS + 20)
    assert "診斷順序不明" in text
    assert "另有查詢失敗" not in text


def test_resolved_ledgerless_refusal_stays_resolved_after_retention(tmp_path):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    older = "a" * 32
    store.record(run_id=older, run_started_at=1000,
                 observed_at=1001, stage="send", outcome="refused",
                 error="ledger", reason="check_storage")
    store.record(run_id=older, run_started_at=1000,
                 observed_at=1002, stage="send", outcome="accepted",
                 error="ledger", reason="check_storage")
    store.record(run_id="b" * 32, run_started_at=1100,
                 observed_at=1101, stage="send", outcome="accepted")
    for index in range(MAX_EVENTS + 20):
        store.record(run_id=f"{index + 3000:032x}",
                     run_started_at=1200 + index,
                     observed_at=1201 + index, stage="query",
                     outcome="read_failed", error="read",
                     reason="verify_his")
    text = consult_summary(store.read(now=1202 + MAX_EVENTS + 20), None,
                           now=1202 + MAX_EVENTS + 20)
    assert "另有無帳本確認拒收" not in text
    assert "請檢查收件設定與拒收原因" not in text


@pytest.mark.parametrize("failure_stage,failure_outcome,recovery_stage", [
    ("query", "read_failed", "query"),
    ("send", "refused", "send"),
])
def test_backward_clock_expiry_does_not_revive_resolved_failure(
        tmp_path, failure_stage, failure_outcome, recovery_stage):
    store = DiagnosticStore(tmp_path / "events.sqlite3", "consult")
    run_id = "a" * 32
    store.record(run_id=run_id, run_started_at=900,
                 observed_at=1003, stage=failure_stage,
                 outcome=failure_outcome,
                 error="read" if failure_stage == "query" else "ledger",
                 reason="verify_his" if failure_stage == "query" else
                 "check_storage")
    store.record(run_id=run_id, run_started_at=900,
                 observed_at=1000, stage=recovery_stage,
                 outcome="ok" if recovery_stage == "query" else "accepted")
    now = RETENTION_SECONDS + 1002
    events = store.read(now=now)
    assert not any(item["outcome"] == failure_outcome for item in events)
    store.record(run_id="b" * 32, run_started_at=now,
                 observed_at=now, stage="query", outcome="started")
    assert not any(item["outcome"] == failure_outcome
                   for item in store.read(now=now))


def test_late_parse_cannot_hide_redaction_failure():
    run = "c" * 32
    query = _event(run, 100, 101, "query", "ok")
    roster = _event(run, 100, 102, "roster", "ok")
    privacy_failure = _event(run, 100, 103, "redact", "failed",
                             error="privacy", reason="verify_his")
    late_parse = _event(run, 100, 104, "parse", "ok")
    text = consult_summary([query, roster, privacy_failure, late_parse],
                           None, now=105)
    assert "階段：去識別｜狀態：失敗" in text
    assert "錯誤分類：去識別" in text


def test_late_ledger_reconciliation_does_not_replace_new_query_failure():
    current = _event("d" * 32, 200, 210, "query", "read_failed",
                     error="read", reason="verify_his")
    old_delivery = {"state": "confirmed", "created_at": 99.0,
                    "observed_at": 220.0,
                    "counts": {"confirmed": 1, "transient_refused": 0,
                               "permanent_refused": 0, "unknown": 0}}
    text = consult_summary([current], old_delivery, now=225)
    assert "階段：HIS 查詢｜狀態：查詢失敗" in text
    assert "最新寄送：寄送端接受" in text
    assert "人工處理：請人工核對 HIS" in text


def test_unrelated_concurrent_delivery_cannot_clear_current_send_warning():
    current = _event("d" * 32, 100.0, 103.0, "send", "accepted",
                     error="ledger", reason="check_storage")
    unrelated = {"state": "confirmed", "created_at": 101.0,
                 "observed_at": 104.0,
                 "counts": {"confirmed": 1, "transient_refused": 0,
                            "permanent_refused": 0, "unknown": 0}}
    text = consult_summary([current], unrelated, now=105)
    assert "階段：寄送｜狀態：寄送端接受" in text
    assert "人工處理：請檢查本機儲存空間或權限" in text
    assert "最新寄送：寄送端接受" in text


def test_stale_started_consult_keeps_outstanding_ledger_warning():
    started = _event("d" * 32, 100, 101, "send", "started")
    delivery = {"state": "unknown", "created_at": 90.0,
                "observed_at": 102.0,
                "counts": {"confirmed": 0, "transient_refused": 0,
                           "permanent_refused": 0, "unknown": 1}}
    text = consult_summary([started], delivery, now=2000)
    assert "執行結果不明" in text
    assert "另有寄送待確認" in text
    assert "狀態：進行中" not in text


def test_stale_started_clock_keeps_diagnostic_read_warning():
    started = _event("d" * 32, 100, 101, "website", "started")
    events = DiagnosticEvents([started], unavailable=True)
    text = clock_summary(events, None, today=datetime.fromtimestamp(101).date(),
                         now=500)
    assert "執行結果不明" in text
    assert "診斷紀錄無法讀取" in text
    assert "最近觀察：進行中" not in text


def test_unlinked_delivery_does_not_claim_stale_started_event_settled():
    started = _event("e" * 32, 100, 101, "send", "started")
    delivery = {"state": "confirmed", "created_at": 100.0,
                "observed_at": 102.0,
                "counts": {"confirmed": 1, "transient_refused": 0,
                           "permanent_refused": 0, "unknown": 0}}
    text = consult_summary([started], delivery, now=2000)
    assert "執行結果不明" in text
    assert "最新寄送：寄送端接受" in text


@pytest.mark.parametrize("outcome,error,reason", [
    ("pending", "timeout", "verify_delivery"),
    ("refused", "refusal", "fix_recipient"),
])
def test_timestamp_overlap_alone_does_not_clear_send_warning(
        outcome, error, reason):
    current = _event("d" * 32, 100.0, 102.0, "send", outcome,
                     error=error, reason=reason)
    settled = {"state": "confirmed", "created_at": 101.0,
               "observed_at": 103.0,
               "counts": {"confirmed": 1, "transient_refused": 0,
                          "permanent_refused": 0, "unknown": 0}}
    text = consult_summary([current], settled, now=104)
    expected = {"pending": "寄送待確認", "refused": "確認拒收"}[outcome]
    assert f"階段：寄送｜狀態：{expected}" in text
    assert "最新寄送：寄送端接受" in text
    action = {"verify_delivery": "請核對寄件備份，勿直接重寄",
              "fix_recipient": "請檢查收件設定與拒收原因"}[reason]
    assert action in text


@pytest.mark.parametrize("stage,outcome,error,reason,expected", [
    ("prepare", "failed", "storage", "check_storage", "失敗"),
    ("send", "pending", "timeout", "verify_delivery", "寄送待確認"),
])
def test_previous_confirmed_delivery_within_one_second_cannot_settle_new_run(
        stage, outcome, error, reason, expected):
    current = _event("d" * 32, 100.5, 101.0, stage, outcome,
                     error=error, reason=reason)
    prior = {"state": "confirmed", "created_at": 100.0,
             "observed_at": 101.5,
             "counts": {"confirmed": 1, "transient_refused": 0,
                        "permanent_refused": 0, "unknown": 0}}
    text = consult_summary([current], prior, now=102)
    assert f"狀態：{expected}" in text
    assert "最新寄送：寄送端接受" in text
    action = ("請檢查本機儲存空間或權限" if reason == "check_storage"
              else "請核對寄件備份，勿直接重寄")
    assert action in text


def test_ledger_projection_distinguishes_pending_partial_and_accepted(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT)")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (
            "unknown", 100.0,
            json.dumps({"private@example.test": "unknown"}), "consult", 99.0,
            ""))
    before = path.stat().st_mtime_ns
    snapshot = read_consult_delivery(path)
    assert path.stat().st_mtime_ns == before
    assert snapshot == {"state": "unknown", "observed_at": 100.0,
                        "created_at": 99.0,
                        "counts": {"unknown": 1, "confirmed": 0,
                                   "transient_refused": 0,
                                   "permanent_refused": 0}}
    assert "寄送待確認" in consult_summary([], snapshot, now=110)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE deliveries SET state=?, updated_at=?, recipients=?", (
            "partial", 120.0, json.dumps({
                "private@example.test": "confirmed",
                "other@example.test": "permanent_refused"})))
    partial = read_consult_delivery(path)
    text = consult_summary([], partial, now=125)
    assert "部分收件人確認拒收" in text
    assert "private@example.test" not in text
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE deliveries SET state=?, updated_at=?", ("confirmed", 130.0))
    assert "寄送端接受" in consult_summary(
        [], read_consult_delivery(path), now=135)
    assert read_consult_delivery(path) is not None
    assert path.stat().st_mtime_ns != before  # only the fake writer changed it


def test_newer_skipped_poll_keeps_older_unknown_delivery_actionable():
    delivery = {"state": "unknown", "created_at": 100.0,
                "observed_at": 105.0,
                "counts": {"confirmed": 0, "transient_refused": 0,
                           "permanent_refused": 0, "unknown": 1}}
    skipped = _event("a" * 32, 200, 201, "prepare", "skipped")
    text = consult_summary([skipped], delivery, now=205)
    assert "另有寄送待確認" in text
    assert "人工處理：請核對寄件備份，勿直接重寄" in text


def test_newer_confirmed_delivery_keeps_other_active_parents_actionable(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", [
            ("unknown", 120.0, "{}", "consult", 100.0, "", ""),
            ("partial", 130.0, "{}", "consult", 105.0, "", "replacement"),
            ("confirmed", 140.0, "{}", "consult", 110.0, "", ""),
        ])
    snapshot = read_consult_delivery(path)
    assert snapshot["state"] == "confirmed"
    assert snapshot["other_attention"] == {"pending": True,
                                            "partial": False,
                                            "failed": False}
    text = consult_summary([], snapshot, now=145)
    assert "寄送端接受" in text
    assert "另有寄送待確認" in text
    assert "請核對寄件備份" in text
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", (
            "partial", 135.0, "{}", "consult", 108.0, "", ""))
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", (
            "confirmed", 141.0, "{}", "consult", 111.0, "", ""))
    snapshot = read_consult_delivery(path)
    assert snapshot["other_attention"] == {"pending": True,
                                            "partial": True,
                                            "failed": False}
    text = consult_summary([], snapshot, now=145)
    assert "另有部分送達／其餘未送達" in text
    assert "請檢查執行紀錄" in text
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE deliveries SET superseded_by='replacement' "
                     "WHERE state='partial' AND superseded_by=''")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", (
            "failed", 136.0, "{}", "consult", 109.0, "", ""))
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", (
            "confirmed", 142.0, "{}", "consult", 112.0, "", ""))
    snapshot = read_consult_delivery(path)
    assert snapshot["other_attention"] == {"pending": True,
                                            "partial": False,
                                            "failed": True}
    text = consult_summary([], snapshot, now=145)
    assert "另有未送出／寄送失敗" in text
    assert "另有確認拒收／部分拒收" not in text


def test_newest_confirmed_delivery_reports_older_unknown_parent_state(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", [
            ("unexpected_state", 120.0, "{}", "consult", 100.0, "", ""),
            ("confirmed", 140.0, "{}", "consult", 110.0, "", ""),
        ])
    snapshot = read_consult_delivery(path)
    assert snapshot["state"] == "confirmed"
    assert snapshot["other_attention"]["unknown_state"]
    text = consult_summary([], snapshot, now=145)
    assert "寄送端接受" in text
    assert "另有帳本狀態無法判讀" in text
    assert "請檢查本機儲存空間或權限" in text


@pytest.mark.parametrize("old_updated,old_recipients", [
    (120.0, "not JSON"),
    (0.0, "{}"),
])
def test_newest_confirmed_delivery_reports_older_malformed_parent(
        tmp_path, old_updated, old_recipients):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", [
            ("unknown", old_updated, old_recipients,
             "consult", 100.0, "", ""),
            ("confirmed", 140.0, "{}", "consult", 110.0, "", ""),
        ])
    snapshot = read_consult_delivery(path)
    assert snapshot["state"] == "confirmed"
    assert snapshot["other_attention"]["unknown_state"]
    text = consult_summary([], snapshot, now=145)
    assert "另有帳本狀態無法判讀" in text
    assert "請檢查本機儲存空間或權限" in text


def test_many_older_confirmed_parents_do_not_create_storage_warning(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", [
            ("confirmed", float(index + 100), "{}", "consult",
             float(index + 90), "", "") for index in range(301)])
    snapshot = read_consult_delivery(path)
    assert snapshot["state"] == "confirmed"
    assert not snapshot.get("other_attention", {}).get("unknown_state")
    text = consult_summary([], snapshot, now=450)
    assert "帳本狀態無法判讀" not in text
    assert "請檢查本機儲存空間或權限" not in text


def test_many_older_confirmed_parents_do_not_create_permanent_manual_prompt(
        tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", [
            ("confirmed", float(index + 100),
             "malformed" if index == 0 else "{}", "consult",
             float(index + 90), "", "") for index in range(258)])
    snapshot = read_consult_delivery(path)
    assert snapshot["state"] == "confirmed"
    assert not snapshot.get("other_attention", {}).get("scan_limited")
    assert not snapshot.get("confirmed_order_uncertain")
    text = consult_summary([], snapshot, now=450)
    assert "摘要僅檢查最近 256 筆" not in text
    assert "帳本狀態無法判讀" not in text
    assert "請檢查執行紀錄並人工核對結果" not in text
    expected = datetime.fromtimestamp(357).strftime("%m/%d %H:%M:%S")
    assert f"最後成功：{expected}" in text


def test_deep_confirmed_history_with_late_completion_keeps_order_warning(
        tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT)")
        conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", [
            ("confirmed", 10_000.0 if index == 0 else float(index + 100),
             "{}", "consult", float(index + 90), "")
            for index in range(301)])
    snapshot = read_consult_delivery(path)
    assert snapshot["confirmed_order_uncertain"]
    assert not snapshot.get("other_attention", {}).get("scan_limited")


def test_closed_partial_parent_does_not_claim_outstanding_followup(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT, body_text TEXT)")
        conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?)", [
            ("partial", 120.0,
             json.dumps({"synthetic@example.test": "permanent_refused"}),
             "consult", 100.0, "", "", ""),
            ("confirmed", 140.0, "{}", "consult", 110.0, "", "", ""),
        ])
    snapshot = read_consult_delivery(path)
    assert snapshot["state"] == "confirmed"
    assert not snapshot.get("other_attention", {}).get("partial")
    assert "另有部分送達" not in consult_summary([], snapshot, now=145)


def test_ledger_clock_rollback_is_not_called_storage_corruption(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    current = time.time()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", (
            "confirmed", current + 120, "{}", "consult",
            current + 100, "", ""))
    snapshot = read_consult_delivery(path)
    assert snapshot["state"] == "confirmed"
    assert snapshot["clock_anomaly"]
    text = consult_summary([], snapshot)
    assert "系統時間異常" in text
    assert "請檢查本機儲存空間或權限" not in text


def test_ledger_created_before_backward_clock_change_is_clock_warning(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", (
            "unknown", 100.0, "{}", "consult", 200.0, "", ""))
    snapshot = read_consult_delivery(path)
    assert snapshot["state"] == "unknown" and snapshot["clock_anomaly"]
    text = consult_summary([], snapshot, now=210)
    assert "系統時間異常" in text
    assert "請檢查本機儲存空間或權限" not in text


def test_latest_ledger_parent_uses_insert_order_after_clock_rollback(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", [
            ("confirmed", 205.0, "{}", "consult", 200.0, "", ""),
            ("unknown", 195.0, "{}", "consult", 190.0, "", ""),
        ])
    snapshot = read_consult_delivery(path)
    assert snapshot["state"] == "unknown"
    text = consult_summary([], snapshot, now=210)
    assert "最新寄送：寄送待確認" in text
    assert "請核對寄件備份" in text


def test_clock_state_file_sharing_violation_requests_retry(tmp_path, monkeypatch):
    path = tmp_path / "clock_state.json"
    path.write_text('{"date":"2026-09-28"}', encoding="utf-8")
    original_read = type(path).read_text
    error = PermissionError("synthetic sharing violation")
    error.winerror = 32

    def busy_read(self, *args, **kwargs):
        if self == path:
            raise error
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", busy_read)
    state = read_clock_state(path)
    assert state == {"retry_later": True}
    text = clock_summary([], state, today=date(2026, 9, 28))
    assert "稍後重試" in text
    assert "請檢查本機儲存空間或權限" not in text


def test_unrenderable_clock_state_mtime_preserves_pending_guidance(
        tmp_path, monkeypatch):
    path = tmp_path / "clock_state.json"
    path.write_text(json.dumps({
        "date": "2026-09-28", "clock_done": [],
        "click_pending": [["account", "morning"]],
        "auth_failed": [],
    }), encoding="utf-8")
    original_stat = type(path).stat

    def invalid_time_stat(self, *args, **kwargs):
        metadata = original_stat(self, *args, **kwargs)
        if self != path:
            return metadata
        return SimpleNamespace(st_mode=metadata.st_mode,
                               st_size=metadata.st_size, st_mtime=1e100)

    monkeypatch.setattr(type(path), "stat", invalid_time_stat)
    state = read_clock_state(path)
    assert state["counts"]["click_pending"] == 1
    assert state["clock_anomaly"]
    text = clock_summary([], state, today=date(2026, 9, 28), now=100)
    assert "點擊待確認" in text
    assert "勿重複點擊" in text
    assert "系統時間異常" in text


def test_unrenderable_ledger_timestamp_preserves_unknown_send_warning(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", (
            "unknown", 1e100, "{}", "consult", 100.0, "", ""))
    snapshot = read_consult_delivery(path)
    assert snapshot == {"unreadable": True}
    text = consult_summary([], snapshot, now=145)
    assert "寄送狀態不明" in text
    assert "請核對寄件備份" in text


def test_deeply_nested_ledger_json_is_reported_unreadable(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    nested = "[" * 4000 + "0" + "]" * 4000
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", (
            "unknown", 105.0, nested, "consult", 100.0, "", ""))
    snapshot = read_consult_delivery(path)
    assert snapshot == {"unreadable": True}
    text = consult_summary([], snapshot, now=110)
    assert "請核對寄件備份" in text
    assert "請檢查本機儲存空間或權限" in text


def test_failed_delivery_does_not_claim_recipient_refusal():
    delivery = {"state": "failed", "created_at": 100.0,
                "observed_at": 105.0,
                "counts": {"confirmed": 0, "transient_refused": 1,
                           "permanent_refused": 0, "unknown": 0}}
    text = consult_summary([], delivery, now=110)
    assert "未送出／寄送失敗" in text
    assert "確認拒收" not in text
    assert "請檢查執行紀錄" in text


def test_partial_delivery_without_permanent_refusal_uses_neutral_status():
    delivery = {"state": "partial", "created_at": 100.0,
                "observed_at": 105.0,
                "counts": {"confirmed": 1, "transient_refused": 1,
                           "permanent_refused": 0, "unknown": 0}}
    text = consult_summary([], delivery, now=110)
    assert "部分送達／其餘未送達" in text
    assert "確認拒收" not in text


def test_unreadable_consult_ledger_is_not_reported_as_absent(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    assert read_consult_delivery(path) is None
    path.write_bytes(b"not a SQLite database")
    snapshot = read_consult_delivery(path)
    assert snapshot == {"unreadable": True}
    text = consult_summary([], snapshot, now=200)
    assert "寄送狀態不明" in text
    assert "請核對寄件備份" in text
    assert "請檢查本機儲存空間或權限" in text


def test_locked_consult_ledger_requests_retry_without_storage_repair(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT, superseded_by TEXT)")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)", (
            "confirmed", 140.0, "{}", "consult", 110.0, "", ""))
    with sqlite3.connect(path) as blocker:
        blocker.execute("BEGIN EXCLUSIVE")
        snapshot = read_consult_delivery(path)
    assert snapshot == {"retry_later": True}
    text = consult_summary([], snapshot, now=145)
    assert "稍後重試" in text
    assert "寄送狀態不明" in text
    assert "請檢查本機儲存空間或權限" not in text
    assert "尚無寄送帳本紀錄" not in text
    malformed = tmp_path / "bad_recipients.sqlite3"
    with sqlite3.connect(malformed) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT)")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (
            "partial", 150.0, "{corrupt", "consult", 100.0, ""))
    assert read_consult_delivery(malformed) == {"unreadable": True}


def test_late_old_delivery_update_cannot_replace_newer_send(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT)")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (
            "confirmed", 300.0, "{}", "consult", 100.0, ""))
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (
            "unknown", 250.0, "{}", "consult", 200.0, ""))
    latest = read_consult_delivery(path)
    assert latest is not None
    assert latest["created_at"] == 200.0
    assert latest["state"] == "unknown"
    assert latest["last_confirmed_at"] == 300.0
    text = consult_summary([], latest, now=310)
    expected = datetime.fromtimestamp(300).strftime("%m/%d %H:%M:%S")
    assert f"最後成功：{expected}" in text


def test_multiple_confirmed_ledger_parents_do_not_guess_success_order(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT)")
        for state, updated, created in (
                ("confirmed", 1000.0, 100.0),
                ("confirmed", 900.0, 200.0),
                ("unknown", 800.0, 300.0)):
            conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (
                state, updated, "{}", "consult", created, ""))
    snapshot = read_consult_delivery(path)
    assert snapshot["confirmed_order_uncertain"]
    text = consult_summary([], snapshot, now=1100)
    assert "最後成功：已確認寄送，但先後不明" in text


def test_normal_confirmed_delivery_sequence_keeps_last_success_time(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT)")
        conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", [
            ("confirmed", 1000.0, "{}", "consult", 100.0, ""),
            ("confirmed", 1100.0, "{}", "consult", 200.0, ""),
        ])
    snapshot = read_consult_delivery(path)
    assert not snapshot.get("confirmed_order_uncertain")
    text = consult_summary([], snapshot, now=1200)
    expected = datetime.fromtimestamp(1100).strftime("%m/%d %H:%M:%S")
    assert f"最後成功：{expected}" in text


def test_new_parent_inserted_during_ledger_read_is_not_called_older(
        monkeypatch, tmp_path):
    import cmuh_common.runtime_summary as summaries

    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT)")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (
            "confirmed", 1000.0, "{}", "consult", 900.0, ""))
    real_connect = sqlite3.connect
    inserted = False

    class FirstRow:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            nonlocal inserted
            row = self.cursor.fetchone()
            if not inserted:
                inserted = True
                writer = real_connect(path)
                try:
                    with writer:
                        writer.execute(
                            "INSERT INTO deliveries VALUES (?,?,?,?,?,?)",
                            ("unknown", 1001.0, "{}", "consult", 901.0, ""))
                finally:
                    writer.close()
            return row

    class Connection:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *args):
            cursor = self.inner.execute(sql, *args)
            return FirstRow(cursor) if sql.startswith(
                "SELECT rowid,state,updated_at") and sql.endswith(
                "LIMIT 1") else cursor

        def close(self):
            self.inner.close()

    def connect(*args, **kwargs):
        inner = real_connect(*args, **kwargs)
        return Connection(inner) if kwargs.get("uri") else inner

    monkeypatch.setattr(summaries.sqlite3, "connect", connect)
    snapshot = read_consult_delivery(path)
    assert inserted
    assert snapshot["state"] == "confirmed"
    assert not snapshot.get("other_attention", {}).get("pending")
    assert not snapshot.get("confirmed_order_uncertain")


def test_confirmed_order_anomaly_keeps_independent_diagnostic_success(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT)")
        conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", [
            ("confirmed", 1100.0, "{}", "consult", 100.0, ""),
            ("confirmed", 1000.0, "{}", "consult", 200.0, ""),
        ])
    snapshot = read_consult_delivery(path)
    assert snapshot["confirmed_order_uncertain"]
    event = _event("a" * 32, 1200, 1201, "send", "accepted")
    text = consult_summary([event], snapshot, now=1210)
    expected = datetime.fromtimestamp(1201).strftime("%m/%d %H:%M:%S")
    assert f"最後成功：{expected}" in text


def test_latest_consult_delivery_uses_parent_chain_not_successful_retry(tmp_path):
    path = tmp_path / "clinic#1" / "ledger.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                     "recipients TEXT, category TEXT, created_at REAL, "
                     "parent_id TEXT)")
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (
            "partial", 300.0, json.dumps({
                "first@example.test": "permanent_refused",
                "second@example.test": "confirmed"}),
            "consult", 100.0, ""))
        conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (
            "confirmed", 301.0, json.dumps({
                "second@example.test": "confirmed"}),
            "consult", 200.0, "parent-id"))
    snapshot = read_consult_delivery(path)
    assert snapshot is not None
    assert snapshot["state"] == "partial"
    text = consult_summary([], snapshot, now=302)
    assert "部分收件人確認拒收" in text
    assert "永久拒收 1" in text
    assert "請檢查收件設定" in text


def test_consult_ledger_read_only_projection_handles_live_wal(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE deliveries (state TEXT, updated_at REAL, "
                       "recipients TEXT, category TEXT, created_at REAL, "
                       "parent_id TEXT)")
        writer.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", (
            "unknown", 150.0,
            json.dumps({"recipient@example.test": "unknown"}),
            "consult", 100.0, ""))
        writer.commit()
        assert (tmp_path / "ledger.sqlite3-wal").exists()
        snapshot = read_consult_delivery(path)
        assert snapshot is not None
        assert snapshot["state"] == "unknown"
        assert snapshot["counts"]["unknown"] == 1
    assert read_consult_delivery(path)["state"] == "unknown"


def test_clock_state_cross_day_and_pending_restart_projection(tmp_path):
    path = tmp_path / "clock_state.json"
    path.write_text(json.dumps({
        "date": "2026-10-05", "clock_done": [],
        "click_pending": [["mon_am_in", "private_employee"]],
        "auth_failed": [],
    }), encoding="utf-8")
    state = read_clock_state(path)
    assert state["counts"]["click_pending"] == 1
    morning = datetime(2026, 10, 5, 7, 40).timestamp()
    text = clock_summary([], state, today=date(2026, 10, 5), now=morning)
    assert "點擊待確認" in text
    assert "private_employee" not in text
    next_day = datetime(2026, 10, 6, 7, 40).timestamp()
    text = clock_summary([], state, today=date(2026, 10, 6), now=next_day)
    assert "今日持久化：尚無今日紀錄" in text
    assert "最近觀察：無紀錄" in text


def test_unreadable_clock_state_never_claims_zero_persisted_actions(tmp_path):
    path = tmp_path / "clock_state.json"
    path.write_text("{corrupt", encoding="utf-8")
    state = read_clock_state(path)
    assert state == {"unreadable": True}
    instant = datetime(2026, 10, 5, 7, 40).timestamp()
    text = clock_summary([], state, today=date(2026, 10, 5), now=instant)
    assert "持久化狀態不明" in text
    assert "今日持久化：無法讀取，數量不明" in text
    assert "請檢查本機儲存空間或權限" in text
    assert "已確認 0" not in text
    assert "點擊待確認 0" not in text
    assert "今日持久化：尚無狀態檔" in clock_summary(
        [], read_clock_state(tmp_path / "missing.json"),
        today=date(2026, 10, 5), now=instant)
    path.write_text(json.dumps({"date": "2026-10-05",
                                "click_pending": None}), encoding="utf-8")
    assert read_clock_state(path) == {"unreadable": True}
    confirmed = _event("c" * 32, instant - 2, instant - 1,
                       "done", "official_confirmed")
    text = clock_summary([confirmed], read_clock_state(path),
                         today=date(2026, 10, 5), now=instant)
    assert "官方已確認；持久化狀態不明" in text
    assert "最後官方確認：無紀錄" not in text


def test_deeply_nested_clock_state_json_is_reported_unreadable(tmp_path):
    path = tmp_path / "clock_state.json"
    nested = "[" * 4000 + "0" + "]" * 4000
    path.write_text('{"date":"2026-10-05","clock_done":' + nested + "}",
                    encoding="utf-8")
    state = read_clock_state(path)
    assert state == {"unreadable": True}
    text = clock_summary([], state, today=date(2026, 10, 5),
                         now=datetime(2026, 10, 5, 8, 0).timestamp())
    assert "持久化狀態不明" in text
    assert "請檢查本機儲存空間或權限" in text


def test_pending_clock_state_outranks_other_confirmed_action():
    instant = datetime(2026, 10, 5, 12, 45).timestamp()
    state = {"date": "2026-10-05", "observed_at": instant,
             "counts": {"clock_done": 1, "click_pending": 1,
                        "auth_failed": 0}}
    text = clock_summary([], state, today=date(2026, 10, 5), now=instant)
    assert "最近觀察：點擊待確認" in text
    assert "人工處理：請至官方系統確認" in text


def test_future_dated_clock_state_requests_clock_and_official_verification():
    now = datetime(2026, 10, 5, 12, 45).timestamp()
    state = {"date": "2026-10-06", "observed_at": now + 120,
             "counts": {"clock_done": 1, "click_pending": 1,
                        "auth_failed": 0}}
    text = clock_summary([], state, today=date(2026, 10, 5), now=now)
    assert "系統時間異常" in text
    assert "請核對系統時鐘" in text
    assert "請至官方系統確認，勿重複點擊" in text
    assert "今日持久化：未來日期紀錄" in text
    assert "今日持久化：尚無今日紀錄" not in text
    same_day_future_time = dict(state, date="2026-10-05")
    text = clock_summary([], same_day_future_time,
                         today=date(2026, 10, 5), now=now)
    assert "系統時間異常" in text
    assert "請至官方系統確認，勿重複點擊" in text


def test_pending_clock_action_keeps_other_storage_failure_guidance():
    now = datetime(2026, 10, 5, 12, 45).timestamp()
    state = {"date": "2026-10-05", "observed_at": now,
             "counts": {"clock_done": 0, "click_pending": 1,
                        "auth_failed": 0}}
    failed = _event("a" * 32, now - 2, now - 1, "done", "failed",
                    error="storage", reason="check_storage")
    text = clock_summary([failed], state, today=date(2026, 10, 5), now=now)
    assert "請檢查本機儲存空間或權限" in text
    assert "請至官方系統確認" in text


def test_other_clock_account_success_cannot_hide_persisted_auth_failure():
    now = datetime(2026, 10, 5, 12, 45).timestamp()
    state = {"date": "2026-10-05", "observed_at": now,
             "counts": {"clock_done": 1, "click_pending": 0,
                        "auth_failed": 1}}
    confirmed = _event("2" * 32, now - 1, now, "confirm",
                       "official_confirmed")
    text = clock_summary([confirmed], state, today=date(2026, 10, 5), now=now)
    assert "今日另有帳密錯誤" in text
    assert "人工處理：請檢查帳號密碼" in text


def test_clock_event_states_do_not_turn_pending_click_into_success():
    now = datetime(2026, 10, 5, 7, 40).timestamp()
    run = "c" * 32
    state = {"date": "2026-10-05", "observed_at": now,
             "counts": {"clock_done": 0, "click_pending": 1,
                        "auth_failed": 0}}
    unknown = _event(run, now - 2, now - 1, "read", "read_unknown",
                     error="read", reason="check_portal")
    text = clock_summary([unknown], state, today=date(2026, 10, 5), now=now)
    assert "讀取不明" in text and "點擊待確認 1" in text
    assert "最後官方確認：無紀錄" in text
    confirmed = _event(run, now - 2, now, "confirm", "official_confirmed")
    text = clock_summary([confirmed, unknown], state,
                         today=date(2026, 10, 5), now=now)
    assert "官方已確認" in text
    assert "最後官方確認：無紀錄" not in text


def test_latest_clock_account_success_does_not_hide_earlier_final_failure():
    now = datetime(2026, 10, 5, 12, 45).timestamp()
    run = "b" * 32
    failed = _event(run, now - 10, now - 5, "done", "read_unknown",
                    error="read", reason="check_portal")
    success = _event(run, now - 10, now - 1, "done", "official_confirmed")
    text = clock_summary([success, failed], None,
                         today=date(2026, 10, 5), now=now)
    assert "最近觀察：部分帳號未確認" in text
    assert "人工處理：請檢查打卡網站狀態" in text
    assert "最後官方確認：無紀錄" not in text


def test_previous_day_failure_cannot_hide_todays_contended_clock_window():
    yesterday = datetime(2026, 10, 4, 16, 0).timestamp()
    today = datetime(2026, 10, 5, 8, 0).timestamp()
    events = [
        _event("a" * 32, yesterday - 1, yesterday,
               "done", "read_unknown", error="read", reason="check_portal"),
        _event("b" * 32, today - 1, today,
               "done", "skipped", reason="flow_busy"),
    ]
    text = clock_summary(events, None,
                         today=date(2026, 10, 5), now=today + 1)
    assert "最近觀察：略過" in text
    assert "請至官方系統確認，勿重複點擊" in text


def test_clock_multi_account_summary_preserves_all_repair_guidance():
    now = datetime(2026, 10, 5, 12, 45).timestamp()
    run = "b" * 32
    storage = _event(run, now - 10, now - 5, "done", "failed",
                     error="storage", reason="check_storage")
    unknown = _event(run, now - 10, now - 4, "done", "read_unknown",
                     error="read", reason="check_portal")
    confirmed = _event(run, now - 10, now - 1, "done",
                       "official_confirmed")
    text = clock_summary([storage, unknown, confirmed], None,
                         today=date(2026, 10, 5), now=now)
    assert "部分帳號未確認" in text
    assert "儲存" in text and "讀取" in text
    assert "請檢查本機儲存空間或權限" in text
    assert "請檢查打卡網站狀態" in text


def test_clock_failure_ending_run_preserves_earlier_storage_guidance():
    now = datetime(2026, 10, 5, 12, 45).timestamp()
    run = "b" * 32
    storage = _event(run, now - 10, now - 5, "done", "failed",
                     error="storage", reason="check_storage")
    unknown = _event(run, now - 10, now - 4, "done", "read_unknown",
                     error="read", reason="check_portal")
    text = clock_summary([storage, unknown], None,
                         today=date(2026, 10, 5), now=now)
    assert "請檢查本機儲存空間或權限" in text
    assert "請檢查打卡網站狀態" in text


def test_current_clock_retry_failure_keeps_its_guidance_with_earlier_failure():
    now = datetime(2026, 10, 5, 12, 45).timestamp()
    run = "b" * 32
    storage = _event(run, now - 10, now - 5, "done", "failed",
                     error="storage", reason="check_storage")
    website = _event(run, now - 10, now - 1, "website", "failed",
                     error="portal", reason="check_portal")
    text = clock_summary([storage, website], None,
                         today=date(2026, 10, 5), now=now)
    assert "階段：網站載入｜最近觀察：失敗" in text
    assert "錯誤分類：儲存；網站" in text
    assert "請檢查本機儲存空間或權限" in text
    assert "請檢查打卡網站狀態" in text


def test_claim_contended_clock_run_cannot_hide_holders_read_failure(
        monkeypatch, tmp_path):
    from contextlib import contextmanager
    import autoclock as clock

    schedule_key = "mon_am_in"
    store = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    monkeypatch.setattr(clock, "DIAGNOSTICS", store)
    instant = datetime(2026, 10, 5, 12, 45).timestamp()
    monkeypatch.setattr(
        clock, "DiagnosticRun",
        lambda target, **kwargs: DiagnosticRun(
            target, wall_clock=lambda: instant, **kwargs))
    monkeypatch.setattr(clock, "load_config", lambda: [
        {"username": "synthetic", "schedule": {schedule_key: True}}])
    monkeypatch.setattr(clock, "_is_clock_done", lambda *_args: False)
    monkeypatch.setattr(clock, "_is_auth_failed", lambda *_args: False)
    monkeypatch.setattr(clock, "_clock_window_passed", lambda *_args: False)
    monkeypatch.setattr(clock, "_get_or_create_clock_driver", lambda: object())
    monkeypatch.setattr(clock, "_driver_session_alive", lambda _driver: True)
    monkeypatch.setattr(clock, "WebDriverWait", lambda _driver, _timeout: object())

    @contextmanager
    def held_by_other_process(_key):
        yield False

    monkeypatch.setattr(clock, "exclusive_claim", held_by_other_process)
    clock.running.set()
    store.record(run_id="a" * 32, run_started_at=instant - 5,
                 observed_at=instant - 2, stage="done",
                 outcome="read_unknown", error="read", reason="check_portal")
    clock.process_clock_task(schedule_key)
    events = store.read()
    assert any(item["stage"] == "done" and item["outcome"] == "skipped"
               and item["reason"] == "flow_busy" for item in events)
    text = clock_summary(events, None, today=date(2026, 10, 5), now=instant)
    assert "讀取不明" in text
    assert "請檢查打卡網站狀態" in text

    monkeypatch.setattr(clock, "load_config", lambda: [
        {"username": "synthetic", "schedule": {schedule_key: True}},
        {"username": "owner", "schedule": {schedule_key: True}}])

    @contextmanager
    def mixed_claim(key):
        yield "owner" in key

    monkeypatch.setattr(clock, "exclusive_claim", mixed_claim)
    monkeypatch.setattr(
        clock, "_perform_clock_action_locked",
        lambda *_args, **kwargs: kwargs["diagnostic_run"].emit(
            "done", "official_confirmed"))
    clock.process_clock_task(schedule_key)
    events = store.read()
    latest = latest_run(events)
    assert latest["outcome"] == "official_confirmed"
    assert not any(item["run_id"] == latest["run_id"] and
                   item["stage"] == "done" and item["outcome"] == "skipped"
                   and item["reason"] == "flow_busy" for item in events)
    assert any(item["run_id"] == latest["run_id"] and
               item["stage"] == "done" and item["outcome"] == "skipped"
               and item["reason"] == "verify_clock" for item in events)
    text = clock_summary(events, None, today=date(2026, 10, 5), now=instant)
    assert "部分帳號未確認" in text
    assert "請至官方系統確認" in text


def test_browser_startup_failure_survives_noisy_clock_contention(
        monkeypatch, tmp_path):
    import autoclock as clock

    schedule_key = "mon_am_in"
    store = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    monkeypatch.setattr(clock, "DIAGNOSTICS", store)
    instant = datetime(2026, 10, 5, 12, 45).timestamp()
    monkeypatch.setattr(
        clock, "DiagnosticRun",
        lambda target, **kwargs: DiagnosticRun(
            target, wall_clock=lambda: instant, **kwargs))
    monkeypatch.setattr(clock, "load_config", lambda: [
        {"username": "synthetic", "schedule": {schedule_key: True}}])
    monkeypatch.setattr(clock, "_is_clock_done", lambda *_args: False)
    monkeypatch.setattr(clock, "_is_auth_failed", lambda *_args: False)

    def fail_browser():
        raise RuntimeError("synthetic browser startup failure")

    monkeypatch.setattr(clock, "_get_or_create_clock_driver", fail_browser)
    store.record(run_id="a" * 32, run_started_at=instant - 10,
                 observed_at=instant - 9, stage="done",
                 outcome="official_confirmed")
    with pytest.raises(RuntimeError, match="synthetic browser"):
        clock.process_clock_task(schedule_key)
    for index in range(MAX_EVENTS + 20):
        run_id = f"{index:032x}"
        started = instant + 0.01 * index
        store.record(run_id=run_id, run_started_at=started,
                     observed_at=started, stage="browser", outcome="ok")
        store.record(run_id=run_id, run_started_at=started,
                     observed_at=started + 0.001, stage="done", outcome="skipped",
                     reason="flow_busy")
    events = store.read(now=instant + 10)
    text = clock_summary(events, None, today=date(2026, 10, 5),
                         now=instant + 10)
    assert "階段：完成｜最近觀察：失敗" in text
    assert "請檢查打卡網站狀態" in text


def test_clock_batch_stopped_after_first_account_reports_incomplete(
        monkeypatch, tmp_path):
    from contextlib import contextmanager
    from threading import Event
    import autoclock as clock

    schedule_key = "mon_am_in"
    store = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    monkeypatch.setattr(clock, "DIAGNOSTICS", store)
    monkeypatch.setattr(clock, "load_config", lambda: [
        {"username": "first", "schedule": {schedule_key: True}},
        {"username": "second", "schedule": {schedule_key: True}}])
    monkeypatch.setattr(clock, "_is_clock_done", lambda *_args: False)
    monkeypatch.setattr(clock, "_is_auth_failed", lambda *_args: False)
    monkeypatch.setattr(clock, "_clock_window_passed", lambda *_args: False)
    monkeypatch.setattr(clock, "_get_or_create_clock_driver", lambda: object())
    monkeypatch.setattr(clock, "_driver_session_alive", lambda _driver: True)
    monkeypatch.setattr(clock, "WebDriverWait", lambda _driver, _timeout: object())
    running = Event()
    running.set()
    monkeypatch.setattr(clock, "running", running)

    @contextmanager
    def owned(_key):
        yield True

    monkeypatch.setattr(clock, "exclusive_claim", owned)

    def finish_first(*_args, **kwargs):
        kwargs["diagnostic_run"].emit("done", "official_confirmed")
        running.clear()

    monkeypatch.setattr(clock, "_perform_clock_action_locked", finish_first)
    clock.process_clock_task(schedule_key)
    text = clock_summary(store.read(), None, today=datetime.now().date())
    assert "最近觀察：官方已確認" not in text
    assert "請至官方系統確認" in text


def test_unreadable_diagnostics_are_not_empty_history(tmp_path):
    consult = DiagnosticStore(tmp_path / "consult.sqlite3", "consult")
    clock = DiagnosticStore(tmp_path / "clock.sqlite3", "clock")
    consult.path.write_bytes(b"not a SQLite database")
    clock.path.write_bytes(b"not a SQLite database")
    consult_events = consult.read()
    clock_events = clock.read()
    assert consult_events.unavailable and clock_events.unavailable
    assert "診斷紀錄無法讀取" in consult_summary(consult_events, None)
    assert "診斷紀錄無法讀取" in clock_summary(
        clock_events, None, today=datetime.now().date())
    assert "diagnostics_read_unavailable" in render_safe_events(consult_events)


def test_consult_export_contains_only_allowlisted_runtime_diagnostics(
        tmp_path, monkeypatch):
    import tkinter.filedialog as filedialog
    import consult_query as cq
    import cmuh_common.debug_privacy as privacy

    dest = tmp_path / "consult.zip"
    private = "Patient Example 123456789"
    events = DiagnosticEvents([{
        "observed_at": 100, "stage": private, "outcome": private,
        "duration_ms": 12, "error": private, "reason": private,
    }])
    monkeypatch.setattr(cq, "DIAGNOSTICS",
                        SimpleNamespace(read=lambda: events))
    monkeypatch.setattr(filedialog, "asksaveasfilename",
                        lambda **_kwargs: str(dest))
    monkeypatch.setattr(cq.messagebox, "showinfo", lambda *_args: None)
    monkeypatch.setattr(cq.messagebox, "showwarning", lambda *_args: None)
    monkeypatch.setattr(privacy, "sanitized_logging_since", lambda: None)
    cq.ConfigApp._export_runtime_summary(SimpleNamespace())
    with zipfile.ZipFile(dest) as archive:
        assert archive.namelist() == ["debug_meta/runtime_diagnostics.txt"]
        content = archive.read("debug_meta/runtime_diagnostics.txt")
    assert private.encode() not in content
    assert b"done failed 12ms unknown none" in content


def test_clock_export_excludes_stale_runtime_file_if_rewrite_fails(
        tmp_path, monkeypatch):
    import tkinter.filedialog as filedialog
    import autoclock as clock
    import cmuh_common.debug_privacy as privacy

    meta = tmp_path / "debug_dumps"
    meta.mkdir()
    (meta / "runtime_diagnostics.txt").write_text(
        "stale patient content", encoding="utf-8")
    (meta / "other_debug.txt").write_text(
        "safe existing diagnostic", encoding="utf-8")
    dest = tmp_path / "clock.zip"
    monkeypatch.setattr(clock, "DEBUG_DUMPS_DIR", meta)
    monkeypatch.setattr(clock, "LOG_FILE", tmp_path / "missing.log")
    monkeypatch.setattr(filedialog, "asksaveasfilename",
                        lambda **_kwargs: str(dest))
    monkeypatch.setattr(clock.messagebox, "showinfo", lambda *_args: None)
    monkeypatch.setattr(clock.messagebox, "showwarning", lambda *_args: None)
    monkeypatch.setattr(privacy, "sanitized_logging_since", lambda: None)
    original_write = type(meta).write_text

    def fail_runtime_write(self, *args, **kwargs):
        if self == meta / "runtime_diagnostics.txt":
            raise OSError("synthetic write failure")
        return original_write(self, *args, **kwargs)

    monkeypatch.setattr(type(meta), "write_text", fail_runtime_write)
    clock.ClockApp._make_diag_bundle(SimpleNamespace(accounts=[]))
    with zipfile.ZipFile(dest) as archive:
        assert archive.namelist() == ["debug_meta/other_debug.txt"]
        assert b"stale patient content" not in archive.read(
            "debug_meta/other_debug.txt")


def test_clock_action_order_follows_claim_acquisition(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from datetime import time as dt_time
    import autoclock as clock

    store = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    monkeypatch.setattr(clock, "DIAGNOSTICS", store)

    @contextmanager
    def owned(_key):
        yield True

    monkeypatch.setattr(clock, "exclusive_claim", owned)
    earlier = DiagnosticRun(store)
    earlier.emit("browser", "started")
    time.sleep(0.01)
    later = DiagnosticRun(store)
    later.emit("browser", "ok")

    def action(*_args, **kwargs):
        diag = kwargs["diagnostic_run"]
        if diag is later:
            diag.emit("done", "read_unknown", error="read",
                      reason="check_portal")
        else:
            diag.emit("done", "official_confirmed")

    monkeypatch.setattr(clock, "_perform_clock_action_locked", action)
    for diag in (later, earlier):
        clock.perform_clock_action(None, None, {"username": "synthetic"},
                                   True, dt_time(8), dt_time(9),
                                   diagnostic_run=diag)
    text = clock_summary(store.read(), None, today=datetime.now().date())
    assert "最近觀察：官方已確認" in text


def test_dropped_first_observation_does_not_hide_other_clock_failure(tmp_path):
    store = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    now = datetime(2026, 10, 5, 12, 45).timestamp()
    older = DiagnosticRun(store, wall_clock=lambda: now)
    with sqlite3.connect(store.path) as blocker:
        blocker.execute("CREATE TABLE IF NOT EXISTS locked (id INTEGER)")
        blocker.execute("BEGIN EXCLUSIVE")
        older.emit("browser", "started")
    newer = DiagnosticRun(store, wall_clock=lambda: now + 1)
    newer.emit("done", "read_unknown", error="read", reason="check_portal")
    older.emit("done", "official_confirmed")
    events = store.read(now=now + 2)
    text = clock_summary(events, None, today=date(2026, 10, 5), now=now + 2)
    assert "診斷順序不明" in text
    assert "請檢查打卡網站狀態" in text


def test_partial_contention_then_window_expiry_keeps_batch_failure(
        monkeypatch, tmp_path):
    from contextlib import contextmanager
    import autoclock as clock

    schedule_key = "mon_am_in"
    store = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    monkeypatch.setattr(clock, "DIAGNOSTICS", store)
    monkeypatch.setattr(clock, "load_config", lambda: [
        {"username": "first", "schedule": {schedule_key: True}},
        {"username": "second", "schedule": {schedule_key: True}}])
    monkeypatch.setattr(clock, "_is_clock_done", lambda *_args: False)
    monkeypatch.setattr(clock, "_is_auth_failed", lambda *_args: False)
    checks = iter([False, True])
    monkeypatch.setattr(clock, "_clock_window_passed", lambda *_args: next(checks))
    monkeypatch.setattr(clock, "_get_or_create_clock_driver", lambda: object())
    monkeypatch.setattr(clock, "_driver_session_alive", lambda _driver: True)
    monkeypatch.setattr(clock, "WebDriverWait", lambda _driver, _timeout: object())

    @contextmanager
    def held_by_other_process(_key):
        yield False

    monkeypatch.setattr(clock, "exclusive_claim", held_by_other_process)
    clock.running.set()
    clock.process_clock_task(schedule_key)
    events = store.read()
    latest = latest_run(events)
    assert latest["stage"] == "done" and latest["outcome"] == "failed"
    assert not any(item["run_id"] == latest["run_id"] and
                   item["reason"] == "flow_busy" for item in events)
    assert "請至官方系統確認" in clock_summary(
        events, None, today=datetime.now().date())


def test_legacy_clock_dry_run_does_not_hide_real_failure_or_claim_success():
    scheduled = _event("1" * 32, 100, 101, "done", "read_unknown",
                       error="read", reason="check_portal")
    tested_read = _event("2" * 32, 200, 201, "read", "official_confirmed")
    tested_done = _event("2" * 32, 200, 202, "done", "dry_run")
    events = [scheduled, tested_read, tested_done]
    assert latest_run(events, now=300) == scheduled
    assert last_success(events, domain="clock") is None


@pytest.mark.parametrize("program", ["clock", "consult"])
def test_both_settings_windows_read_summary_off_tk_thread(monkeypatch, program):
    import autoclock as clock
    import consult_query as consult

    module, app_type = ((clock, clock.ClockApp) if program == "clock"
                        else (consult, consult.ConfigApp))
    started = threading.Event()
    release = threading.Event()
    labels = []
    after_calls = []

    class SlowStore:
        def read(self):
            started.set()
            assert release.wait(2)
            return []

    monkeypatch.setattr(module, "DIAGNOSTICS", SlowStore())
    if module is clock:
        monkeypatch.setattr(clock, "read_clock_state", lambda _path: None)
    else:
        monkeypatch.setattr(consult, "read_consult_delivery",
                            lambda _path: None)
    fake = SimpleNamespace(
        runtime_summary_var=SimpleNamespace(set=labels.append),
        _summary_queue=queue.Queue(maxsize=1), _summary_busy=False,
        after=lambda delay, fn: after_calls.append((delay, fn)),
    )
    fake._refresh_runtime_summary = lambda: app_type._refresh_runtime_summary(fake)
    before = time.monotonic()
    app_type._refresh_runtime_summary(fake)
    assert time.monotonic() - before < 0.2
    assert started.wait(1)
    assert not labels  # worker is still blocked; no Tk call from it
    release.set()
    deadline = time.monotonic() + 2
    while fake._summary_busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not fake._summary_busy
    app_type._refresh_runtime_summary(fake)
    assert labels and "階段：" in labels[-1]
    assert after_calls and after_calls[0][0] == 3000
