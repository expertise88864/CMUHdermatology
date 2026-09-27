"""Read-only operational summaries contain codes, never clinical content."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from cmuh_common.runtime_diagnostics import (
    DiagnosticRun, DiagnosticStore, MAX_EVENTS, RETENTION_SECONDS,
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


def test_read_only_store_uri_escapes_fragment_characters(tmp_path):
    path = tmp_path / "clinic#1" / "events.sqlite3"
    store = DiagnosticStore(path, "consult")
    store.record(run_id="a" * 32, run_started_at=100,
                 observed_at=100, stage="query", outcome="ok")
    assert store.read(now=101)[0]["outcome"] == "ok"


def test_corrupt_or_untrusted_event_content_never_enters_export(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = DiagnosticStore(path, "clock")
    store.record(run_id="a" * 32, run_started_at=100,
                 observed_at=100, stage="read", outcome="no_record")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE events SET duration_ms=?", ("private patient name",))
    assert store.read(now=110) == []
    text = render_safe_events([{
        "observed_at": 100, "stage": "private patient name",
        "outcome": "private patient name", "duration_ms": 1,
        "error": "private patient name", "reason": "private patient name",
    }])
    assert "private patient name" not in text


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


def test_persisted_delivery_resolution_beats_stale_started_event():
    started = _event("e" * 32, 100, 101, "send", "started")
    delivery = {"state": "confirmed", "created_at": 100.0,
                "observed_at": 102.0,
                "counts": {"confirmed": 1, "transient_refused": 0,
                           "permanent_refused": 0, "unknown": 0}}
    text = consult_summary([started], delivery, now=2000)
    assert "狀態：寄送端接受" in text
    assert "執行結果不明" not in text


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
    assert "點擊待確認 0" in text
    assert "最近觀察：無紀錄" in text


def test_pending_clock_state_outranks_other_confirmed_action():
    instant = datetime(2026, 10, 5, 12, 45).timestamp()
    state = {"date": "2026-10-05", "observed_at": instant,
             "counts": {"clock_done": 1, "click_pending": 1,
                        "auth_failed": 0}}
    text = clock_summary([], state, today=date(2026, 10, 5), now=instant)
    assert "最近觀察：點擊待確認" in text
    assert "人工處理：請至官方系統確認" in text


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
    now = time.time()
    run = "b" * 32
    failed = _event(run, now - 10, now - 5, "done", "read_unknown",
                    error="read", reason="check_portal")
    success = _event(run, now - 10, now - 1, "done", "official_confirmed")
    text = clock_summary([success, failed], None,
                         today=datetime.fromtimestamp(now).date(), now=now)
    assert "最近觀察：部分帳號未確認" in text
    assert "人工處理：請檢查打卡網站狀態" in text
    assert "最後官方確認：無紀錄" not in text


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
