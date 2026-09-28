"""Bounded, code-only observations for the consult and clock settings windows.

These records are *not* delivery or clock state. The delivery ledger and the
persisted clock state remain authoritative for send/click decisions. No caller
may pass free-form text (including exception messages) into this store.
"""

from __future__ import annotations

import math
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path


RETENTION_SECONDS = 7 * 86400
MAX_EVENTS = 256
MAX_RUNS = 8192
MAX_DURATION_MS = 30 * 60 * 1000

_STAGES = {
    "consult": frozenset({
        "query", "parse", "roster", "redact", "prepare", "send", "reconcile", "done",
    }),
    "clock": frozenset({
        "browser", "website", "login", "read", "submit", "confirm", "done",
    }),
}
_OUTCOMES = frozenset({
    "started", "ok", "read_failed", "roster_unknown", "empty_roster",
    "no_new", "pending", "refused", "accepted", "failed", "no_record",
    "read_unknown", "click_pending", "official_confirmed", "auth_failed",
    "skipped", "dry_run",
})
_ERRORS = frozenset({
    "none", "timeout", "auth", "read", "parse", "privacy", "transport",
    "refusal", "ledger", "portal", "storage", "unknown",
})
_REASONS = frozenset({
    "none", "verify_his", "verify_delivery", "fix_recipient", "check_portal",
    "verify_clock", "fix_credentials", "check_storage", "check_job",
    "flow_busy", "order_uncertain",
})


def _safe_code(value: object, allowed: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


class DiagnosticEvents(list[dict]):
    """List-compatible read result that distinguishes absence from read failure."""

    def __init__(self, events=(), *, unavailable: bool = False,
                 clock_anomaly: bool = False, retry_later: bool = False):
        super().__init__(events)
        self.unavailable = unavailable
        self.clock_anomaly = clock_anomaly
        self.retry_later = retry_later


def sqlite_temporarily_busy(error: sqlite3.Error) -> bool:
    """A transient SQLite lock is different from corrupt or unreadable data."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in {
        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
    }


class DiagnosticStore:
    """One small SQLite file per program; writes never decide clinical actions."""

    def __init__(self, path: str | Path, domain: str):
        if domain not in _STAGES:
            raise ValueError("unknown diagnostic domain")
        self.path = Path(path)
        self.domain = domain

    def record(self, *, run_id: str, run_started_at: float, stage: str,
               outcome: str, duration_ms: float = 0, error: str = "none",
               reason: str = "none", observed_at: float | None = None) -> bool:
        """Best-effort append; never changes the caller's send/click outcome."""
        if not isinstance(run_id, str) or len(run_id) != 32 or not all(
                c in "0123456789abcdef" for c in run_id):
            return False
        stage = _safe_code(stage, _STAGES[self.domain], "done")
        outcome = _safe_code(outcome, _OUTCOMES, "failed")
        error = _safe_code(error, _ERRORS, "unknown")
        reason = _safe_code(reason, _REASONS, "none")
        now = time.time() if observed_at is None else observed_at
        try:
            now = float(now)
            started = float(run_started_at)
            elapsed = max(0, min(MAX_DURATION_MS, round(float(duration_ms))))
            if (not math.isfinite(started) or not math.isfinite(now) or
                    started <= 0 or now <= 0):
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Diagnostics must never hold a clinical action behind a busy DB.
            with closing(sqlite3.connect(str(self.path), timeout=0.02)) as conn:
                with conn:
                    conn.execute("PRAGMA busy_timeout=20")
                    # SQLite DDL may autocommit without an explicit BEGIN.
                    # Creation, legacy backfill and the new event must be one
                    # transaction so a crash cannot leave an empty runs table.
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS events ("
                        "seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL, "
                        "run_started_at REAL NOT NULL, observed_at REAL NOT NULL, "
                        "stage TEXT NOT NULL, outcome TEXT NOT NULL, "
                        "duration_ms INTEGER NOT NULL, error TEXT NOT NULL, "
                        "reason TEXT NOT NULL)")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS runs ("
                        "run_order INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "run_id TEXT NOT NULL, run_started_at REAL NOT NULL, "
                        "UNIQUE(run_id,run_started_at))")
                    # Also repair a pre-fix interrupted migration that left
                    # the runs table present but empty. Mixed mapped/orphan
                    # rows have ambiguous order and must not be guessed.
                    orphan = conn.execute(
                        "SELECT 1 FROM events e WHERE NOT EXISTS ("
                        "SELECT 1 FROM runs r WHERE r.run_id=e.run_id "
                        "AND r.run_started_at=e.run_started_at) LIMIT 1"
                    ).fetchone()
                    if orphan and conn.execute(
                            "SELECT 1 FROM runs LIMIT 1").fetchone():
                        raise sqlite3.DatabaseError("incomplete run migration")
                    if orphan:
                        conn.execute(
                            "INSERT INTO runs(run_id,run_started_at) "
                            "SELECT run_id,run_started_at FROM events "
                            "GROUP BY run_id,run_started_at "
                            "ORDER BY MIN(seq)")
                    existing_run = conn.execute(
                        "SELECT run_order FROM runs WHERE run_id=? "
                        "AND run_started_at=?", (run_id, started)).fetchone()
                    if not existing_run:
                        conn.execute("INSERT INTO runs(run_id,run_started_at) "
                                     "VALUES(?,?)", (run_id, started))
                    inserted = conn.execute(
                        "INSERT INTO events(run_id,run_started_at,observed_at,"
                        "stage,outcome,duration_ms,error,reason) VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, started, now, stage, outcome, elapsed, error, reason))
                    inserted_seq = inserted.lastrowid
                    if inserted_seq is None:
                        raise sqlite3.DatabaseError("diagnostic insert had no sequence")
                    if self.domain == "consult" and (
                            (stage, outcome) in {
                                ("query", "ok"), ("send", "accepted")}):
                        # A backward wall-clock jump can make the recovery's
                        # timestamp expire before the failure's timestamp.
                        # Retire only reversed-time failures already resolved
                        # by this later event, before either age filter runs.
                        failed_outcome = ("read_failed" if stage == "query"
                                          else "refused")
                        conn.execute(
                            "DELETE FROM events WHERE run_id=? AND "
                            "run_started_at=? AND stage=? AND outcome=? "
                            "AND seq<? AND observed_at>?",
                            (run_id, started, stage, failed_outcome,
                             inserted_seq, now))
                    if (self.domain == "clock" and stage == "done" and
                            outcome == "skipped" and reason == "flow_busy"):
                        # An entirely contended invocation is one observation.
                        # Keep it atomic under retention, or older per-account
                        # markers can later appear to be a real clock attempt.
                        conn.execute(
                            "DELETE FROM events WHERE run_id=? AND seq!=?",
                            (run_id, inserted_seq))
                    conn.execute("DELETE FROM events WHERE observed_at < ?",
                                 (now - RETENTION_SECONDS,))
                    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                    if count > MAX_EVENTS:
                        run_order_expr = (
                            "(SELECT run_order FROM runs WHERE "
                            "runs.run_id=events.run_id AND "
                            "runs.run_started_at=events.run_started_at)")
                        success_filter = (
                            "(stage='send' AND outcome='accepted') OR "
                            "(stage='done' AND outcome IN ('empty_roster','no_new'))"
                            if self.domain == "consult" else
                            "outcome='official_confirmed' AND "
                            "stage IN ('confirm','done')")
                        unresolved_refusal = (
                            "NOT EXISTS (SELECT 1 FROM events AS recovery "
                            "WHERE recovery.run_id=events.run_id AND "
                            "recovery.run_started_at=events.run_started_at "
                            "AND recovery.stage='send' AND "
                            "recovery.outcome='accepted' AND "
                            "recovery.seq>events.seq)")
                        failure_filter = (
                            "(stage='query' AND outcome='read_failed' "
                            "AND NOT EXISTS (SELECT 1 FROM events AS recovery "
                            "WHERE recovery.run_id=events.run_id AND "
                            "recovery.run_started_at=events.run_started_at "
                            "AND recovery.stage='query' AND recovery.outcome='ok' "
                            "AND recovery.seq>events.seq)) OR "
                            "(stage='roster' AND outcome='roster_unknown') OR "
                            "(stage='redact' AND outcome='failed') OR "
                            "(stage='reconcile' AND outcome='failed') OR "
                            "(stage='send' AND (outcome IN ('pending','failed') "
                            f"OR (outcome='refused' AND {unresolved_refusal}))) OR "
                            "(stage='done' AND outcome='failed' AND reason!='none')"
                            if self.domain == "consult" else
                            "stage='done' AND outcome IN "
                            "('read_unknown','failed','click_pending','auth_failed') "
                            "AND reason!='none' OR "
                            "(stage='done' AND outcome='skipped' "
                            "AND reason='verify_clock')")
                        # The current observation must survive even when a
                        # week of distinct historical failures fills the cap.
                        priority_seqs = {inserted_seq}
                        latest_success = conn.execute(
                            f"SELECT seq FROM events WHERE {success_filter} "
                            "ORDER BY seq DESC LIMIT 1"
                        ).fetchone()
                        if latest_success:
                            priority_seqs.add(latest_success[0])
                        nonbusy = (
                            "run_id NOT IN (SELECT run_id FROM events "
                            "WHERE stage='done' AND outcome='skipped' "
                            "AND reason IN ('flow_busy','check_job'))")
                        if self.domain == "consult":
                            for condition in (
                                    "stage='send' AND outcome='pending'",
                                    "stage='send' AND outcome='pending' "
                                    "AND error='ledger'",
                                    "stage='send' AND outcome='pending' "
                                    "AND error='transport'",
                                    "stage='send' AND outcome='refused' "
                                    f"AND {unresolved_refusal}",
                                    "stage='send' AND outcome='refused' "
                                    f"AND error='ledger' AND {unresolved_refusal}",
                                    "stage='send' AND outcome='failed'",
                                    "stage='done' AND outcome='failed' "
                                    "AND error='ledger'"):
                                row = conn.execute(
                                    "SELECT seq FROM events WHERE " + condition +
                                    " ORDER BY seq DESC LIMIT 1").fetchone()
                                if row:
                                    priority_seqs.add(row[0])
                            latest_reconcile = conn.execute(
                                "SELECT seq FROM events WHERE stage='reconcile' "
                                "AND outcome IN ('ok','failed') "
                                "ORDER BY seq DESC LIMIT 1").fetchone()
                            if latest_reconcile:
                                priority_seqs.add(latest_reconcile[0])
                            latest_stale_skip = conn.execute(
                                "SELECT seq FROM events WHERE stage='done' "
                                "AND outcome='skipped' AND reason='check_job' "
                                f"ORDER BY {run_order_expr} DESC,seq DESC "
                                "LIMIT 1").fetchone()
                            if latest_stale_skip:
                                priority_seqs.add(latest_stale_skip[0])
                            current = conn.execute(
                                "SELECT run_id FROM events WHERE " + nonbusy +
                                " AND stage NOT IN ('reconcile','browser','parse') "
                                f"ORDER BY {run_order_expr} DESC,seq DESC "
                                "LIMIT 1").fetchone()
                            if current:
                                for condition in (
                                        "stage='query' AND outcome!='started'",
                                        "stage='roster' AND outcome IN "
                                        "('ok','roster_unknown','empty_roster')"):
                                    row = conn.execute(
                                        "SELECT seq FROM events WHERE run_id=? AND " +
                                        condition + " ORDER BY seq DESC "
                                        "LIMIT 1", current).fetchone()
                                    if row:
                                        priority_seqs.add(row[0])
                        # Keep distinct failures per batch. A later event
                        # from an older concurrent batch must not replace the
                        # current batch's failure of the same category.
                        failure_group = (
                            "run_id,run_started_at,stage,outcome,reason,error"
                            if self.domain == "consult" else
                            "run_id,stage,outcome,reason,error")
                        failure_rows = conn.execute(
                            f"SELECT MAX(seq),MAX({run_order_expr}) FROM events "
                            f"WHERE {failure_filter} "
                            f"GROUP BY {failure_group} "
                            f"ORDER BY MAX({run_order_expr}) DESC,MAX(seq) DESC")
                        for failure_seq, _started in failure_rows:
                            if len(priority_seqs) >= MAX_EVENTS // 2:
                                break
                            priority_seqs.add(failure_seq)
                        # Preserve a newer in-progress holder as well as
                        # distinct terminal failures. Browser-only contenders
                        # are not yet account actions; reserve the newest real
                        # action separately until claim ownership is known.
                        for stage_filter in (
                                ("stage NOT IN ('reconcile','browser','parse')"
                                 if self.domain == "consult" else
                                 "stage NOT IN ('reconcile','browser')"),
                                "stage!='reconcile'"):
                            row = conn.execute(
                                "SELECT seq FROM events WHERE " + nonbusy +
                                " AND " + stage_filter +
                                 f" ORDER BY {run_order_expr} DESC,seq DESC "
                                 "LIMIT 1").fetchone()
                            if row:
                                priority_seqs.add(row[0])
                        keep_recent = MAX_EVENTS - len(priority_seqs)
                        priority = tuple(sorted(priority_seqs))
                        placeholders = ",".join("?" for _ in priority)
                        exclude_priority = (f"WHERE seq NOT IN ({placeholders})"
                                            if priority else "")
                        preserve = (f" AND seq NOT IN ({placeholders})"
                                    if priority else "")
                        conn.execute(
                            "DELETE FROM events WHERE seq NOT IN "
                            "(SELECT seq FROM events " + exclude_priority +
                            " ORDER BY seq DESC LIMIT ?)"
                            + preserve,
                            (*priority, keep_recent, *priority))
                    run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                    if run_count > MAX_RUNS:
                        conn.execute(
                            "DELETE FROM runs WHERE run_order NOT IN "
                            "(SELECT run_order FROM runs ORDER BY run_order DESC "
                            "LIMIT ?) AND (run_id,run_started_at) NOT IN "
                            "(SELECT DISTINCT run_id,run_started_at FROM events)",
                            (MAX_RUNS,))
            return True
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
            return False

    def read(self, *, now: float | None = None) -> DiagnosticEvents:
        """Read-only projection; no file creation or schema migration."""
        instant = time.time() if now is None else now
        cutoff = instant - RETENTION_SECONDS
        try:
            metadata = self.path.stat()
        except FileNotFoundError:
            return DiagnosticEvents()
        except OSError:
            return DiagnosticEvents(unavailable=True)
        if not stat.S_ISREG(metadata.st_mode):
            return DiagnosticEvents(unavailable=True)
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as conn:
                conn.execute("PRAGMA query_only=ON")
                has_runs = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='runs'").fetchone()
                run_column = ("r.run_order" if has_runs else
                              "NULL AS run_order")
                run_join = (" LEFT JOIN runs r ON r.run_id=e.run_id "
                            "AND r.run_started_at=e.run_started_at"
                            if has_runs else "")
                rows = conn.execute(
                    "SELECT e.seq," + run_column +
                    ",e.run_id,e.run_started_at,e.observed_at,e.stage,e.outcome,"
                    "e.duration_ms,e.error,e.reason FROM events e" + run_join +
                    " ORDER BY e.seq DESC LIMIT ?", (MAX_EVENTS,)).fetchall()
        except sqlite3.Error as error:
            if sqlite_temporarily_busy(error):
                return DiagnosticEvents(retry_later=True)
            return DiagnosticEvents(unavailable=True)
        except (OSError, TypeError, ValueError):
            return DiagnosticEvents(unavailable=True)
        result = []
        rejected = False
        clock_anomaly = False
        fallback_orders = {}
        for seq, _order, run_id, started, *_rest in rows:
            if isinstance(seq, int) and isinstance(run_id, str):
                key = (run_id, started)
                fallback_orders[key] = min(seq, fallback_orders.get(key, seq))
        for row in rows:
            (seq, run_order, run_id, started, observed, stage, outcome,
             duration, error, reason) = row
            if (not isinstance(run_id, str) or len(run_id) != 32 or
                    any(c not in "0123456789abcdef" for c in run_id) or
                    stage not in _STAGES[self.domain] or outcome not in _OUTCOMES or
                    not isinstance(seq, int) or seq <= 0):
                rejected = True
                continue
            if run_order is None:
                if has_runs:
                    rejected = True
                    continue
                run_order = fallback_orders.get((run_id, started))
            if not isinstance(run_order, int) or run_order <= 0:
                rejected = True
                continue
            try:
                started = float(started)
                observed = float(observed)
                duration = int(duration)
            except (TypeError, ValueError, OverflowError):
                rejected = True
                continue
            if (not math.isfinite(started) or not math.isfinite(observed)
                    or started <= 0 or observed <= 0
                    or duration < 0 or duration > MAX_DURATION_MS):
                rejected = True
                continue
            try:
                datetime.fromtimestamp(started)
                datetime.fromtimestamp(observed)
            except (OverflowError, OSError, ValueError):
                rejected = True
                continue
            if observed < cutoff:
                continue
            if started > observed + 60 or observed > instant + 60:
                clock_anomaly = True
            if error not in _ERRORS or reason not in _REASONS:
                rejected = True
            result.append({
                "seq": seq, "run_order": run_order,
                "run_id": run_id, "run_started_at": started,
                "observed_at": observed, "stage": stage,
                "outcome": outcome, "duration_ms": duration,
                "error": _safe_code(error, _ERRORS, "unknown"),
                "reason": _safe_code(reason, _REASONS, "none"),
            })
        return DiagnosticEvents(result, unavailable=rejected,
                                clock_anomaly=clock_anomaly)


class DiagnosticRun:
    """Carries one process-local observation identity across retries."""

    def __init__(self, store: DiagnosticStore, *, wall_clock=time.time,
                 monotonic=time.monotonic, enabled: bool = True):
        self.store = store
        self.enabled = enabled
        self.run_id = uuid.uuid4().hex
        self.started_at = wall_clock()
        self.action_started_at: float | None = None
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._write_lost = False

    def start(self) -> float:
        return self._monotonic()

    def mark_action_started(self) -> None:
        """Order account actions by claim ownership, preserving batch identity."""
        self.action_started_at = self._wall_clock()

    def emit(self, stage: str, outcome: str, *, since: float | None = None,
             error: str = "none", reason: str = "none") -> None:
        if not self.enabled:
            return
        duration = (self._monotonic() - since) * 1000 if since is not None else 0
        if (self._write_lost and outcome in {
                "official_confirmed", "accepted", "empty_roster", "no_new"}):
            reason = "order_uncertain"
            if error == "none":
                error = "storage"
        written = self.store.record(
            run_id=self.run_id,
            run_started_at=(self.action_started_at if self.action_started_at is not None
                            else self.started_at),
            stage=stage, outcome=outcome, duration_ms=duration,
            error=error, reason=reason, observed_at=self._wall_clock())
        if not written:
            self._write_lost = True


def event_order(event: dict) -> float | int:
    """Insertion order survives wall-clock adjustments within a diagnostic run."""
    return event.get("seq", event["observed_at"])


def event_not_after(item: dict, anchor: dict) -> bool:
    if "seq" in item and "seq" in anchor:
        return item["seq"] <= anchor["seq"]
    return item["observed_at"] <= anchor["observed_at"]


def _run_order(event: dict) -> float | int:
    return event.get("run_order", event["run_started_at"])


def latest_run(events: list[dict], *, now: float | None = None,
               show_contended_after_success: bool = False) -> dict | None:
    """Choose by persisted run acquisition order, then event insertion order."""
    dry_run_ids = {e["run_id"] for e in events if e["outcome"] == "dry_run"}
    eligible = [e for e in events if e["run_started_at"] > 0
                and e["observed_at"] > 0
                and e["stage"] != "parse"
                and e["run_id"] not in dry_run_ids]
    if not eligible:
        return None
    # A concurrent invocation may reconcile mail, then skip the HIS flow
    # because another run holds its lock. Keep the lock holder's later result
    # visible instead of letting the newer skipped run take over the panel.
    busy_ids = {e["run_id"] for e in eligible
                if e["stage"] == "done" and e["outcome"] == "skipped"
                and e["reason"] in {"flow_busy", "check_job"}}
    executed_ids = {e["run_id"] for e in eligible
                    if e["stage"] != "reconcile" and e["run_id"] not in busy_ids}
    executed = [e for e in eligible if e["run_id"] in executed_ids]
    if executed:
        newest_executed = max(
            executed, key=lambda e: (_run_order(e), event_order(e)))
        run_events = [e for e in executed
                      if e["run_id"] == newest_executed["run_id"]]
        latest_executed = max(run_events, key=event_order)
        holder_status = latest_executed
        if latest_executed["stage"] == "parse":
            caller_events = [e for e in run_events if e["stage"] != "parse"
                             and event_not_after(e, latest_executed)]
            if caller_events:
                holder_status = max(caller_events, key=event_order)
        # An expired holder is still an actionable warning while no newer
        # result exists. Once the holder reports its result, that result wins.
        stale_skips = [e for e in eligible
                       if e["stage"] == "done" and e["outcome"] == "skipped"
                       and e["reason"] == "check_job"
                        and _run_order(e) > _run_order(latest_executed)
                        and not event_not_after(e, holder_status)]
        holder_final = (holder_status["stage"] == "done" or
                        (holder_status["stage"] == "send" and
                         holder_status["outcome"] != "started") or
                        (holder_status["stage"] == "query" and
                         holder_status["outcome"] == "read_failed") or
                        (holder_status["stage"] == "redact" and
                         holder_status["outcome"] == "failed"))
        if stale_skips and not holder_final:
            return max(stale_skips, key=event_order)
        # A clock window that was entirely blocked still needs a visible
        # verification prompt if no lock holder subsequently reported a result.
        later_busy = [e for e in eligible
                      if e["stage"] == "done" and e["outcome"] == "skipped"
                      and e["reason"] == "flow_busy"
                      and _run_order(e) > _run_order(latest_executed)
                      and event_not_after(latest_executed, e)]
        if (show_contended_after_success and later_busy and
                holder_status["outcome"] in {
                "official_confirmed", "accepted", "empty_roster", "no_new"}):
            return max(later_busy, key=event_order)
        return latest_executed
    newest = max(eligible, key=lambda e: (_run_order(e), event_order(e)))
    run_events = [e for e in eligible if e["run_id"] == newest["run_id"]]
    return max(run_events, key=event_order)


def last_success(events: list[dict], *, domain: str) -> float | None:
    # A parsed empty roster is only an intermediate observation: returning to
    # the HIS main screen or the rest of the job can still fail afterward.
    completed = {"consult": {("send", "accepted"),
                              ("done", "empty_roster"),
                              ("done", "no_new")},
                 "clock": {("confirm", "official_confirmed"),
                           ("done", "official_confirmed")}}[domain]
    dry_run_ids = {e["run_id"] for e in events if e["outcome"] == "dry_run"}
    found = [e for e in events
             if e["run_id"] not in dry_run_ids and
             (e["stage"], e["outcome"]) in completed]
    latest = max(found, key=event_order, default=None)
    return latest["observed_at"] if latest else None


def render_safe_events(events: list[dict]) -> str:
    """Plain-text input for the existing safe diagnostic bundle exporter."""
    lines = ["CMUH runtime diagnostics (allowlisted codes only)"]
    if getattr(events, "unavailable", False):
        lines.append("diagnostics_read_unavailable")
    if getattr(events, "retry_later", False):
        lines.append("diagnostics_read_busy")
    if getattr(events, "clock_anomaly", False):
        lines.append("diagnostics_clock_anomaly")
    for event in events[:MAX_EVENTS]:
        try:
            observed = float(event.get("observed_at", 0))
            duration = int(event.get("duration_ms", 0))
        except (TypeError, ValueError, OverflowError, AttributeError):
            continue
        if (not math.isfinite(observed) or observed <= 0 or
                not 0 <= duration <= MAX_DURATION_MS):
            continue
        stage = _safe_code(event.get("stage"),
                           _STAGES["consult"] | _STAGES["clock"], "done")
        outcome = _safe_code(event.get("outcome"), _OUTCOMES, "failed")
        error = _safe_code(event.get("error"), _ERRORS, "unknown")
        reason = _safe_code(event.get("reason"), _REASONS, "none")
        lines.append(f"{observed:.0f} {stage} {outcome} {duration}ms "
                     f"{error} {reason}")
    return "\n".join(lines) + "\n"
