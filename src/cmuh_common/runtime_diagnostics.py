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
    "flow_busy", "order_uncertain", "routine_skip", "check_mail_client",
})


def _safe_code(value: object, allowed: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def read_only_sqlite_uri(path: Path) -> str:
    """Keep mapped drives local and omit URI authority for UNC shares."""
    uri = path.absolute().as_uri()
    if uri.startswith("file://") and not uri.startswith("file:///"):
        uri = "file:////" + uri[len("file://"):]
    return uri + "?mode=ro"


def _is_executed_observation(stage: str, outcome: str, reason: str) -> bool:
    return (stage not in {"parse", "reconcile"} and outcome != "dry_run"
            and not (stage == "done" and outcome == "skipped" and
                     reason in {"flow_busy", "check_job", "routine_skip"}))


class DiagnosticEvents(list[dict]):
    """List-compatible read result that distinguishes absence from read failure."""

    def __init__(self, events=(), *, unavailable: bool = False,
                 clock_anomaly: bool = False, retry_later: bool = False,
                 ordering_uncertain: bool = False,
                 current_run_floor: int = 0):
        super().__init__(events)
        self.unavailable = unavailable
        self.clock_anomaly = clock_anomaly
        self.retry_later = retry_later
        self.ordering_uncertain = ordering_uncertain
        self.current_run_floor = current_run_floor


def sqlite_temporarily_busy(error: sqlite3.Error) -> bool:
    """A transient SQLite lock is different from corrupt or unreadable data."""
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in {5, 6}  # SQLITE_BUSY / SQLITE_LOCKED
    # Python 3.10 lacks sqlite_errorcode. Inspect only SQLite's known lock
    # messages locally; exception text never enters the diagnostic store.
    return isinstance(error, sqlite3.OperationalError) and str(error).lower().split(
        ":", 1)[0] in {
            "database is locked", "database table is locked",
            "database schema is locked",
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
               reason: str = "none", observed_at: float | None = None
               ) -> bool | None:
        """Best-effort append; None means transient SQLite contention."""
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
                    # The settings window reads a multi-statement snapshot.
                    # WAL lets that reader coexist with this short append;
                    # rollback journals can block COMMIT beyond 20 ms and
                    # falsely mark a healthy clinical run as write-lost.
                    conn.execute("PRAGMA journal_mode=WAL")
                    # This is a best-effort observation store, never the send
                    # or clock ledger. Avoid FULL-sync cost on each event.
                    conn.execute("PRAGMA synchronous=NORMAL")
                    # SQLite DDL may autocommit without an explicit BEGIN.
                    # Creation, legacy backfill and the new event must be one
                    # transaction so a crash cannot leave an empty runs table.
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS events ("
                        "seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL, "
                        "run_started_at REAL NOT NULL, observed_at REAL NOT NULL, "
                        "retention_at REAL NOT NULL, "
                        "stage TEXT NOT NULL, outcome TEXT NOT NULL, "
                        "duration_ms INTEGER NOT NULL, error TEXT NOT NULL, "
                        "reason TEXT NOT NULL)")
                    event_columns = {row[1] for row in conn.execute(
                        "PRAGMA table_info(events)")}
                    if "retention_at" not in event_columns:
                        conn.execute(
                            "ALTER TABLE events ADD COLUMN retention_at REAL")
                        conn.execute(
                            "WITH held AS (SELECT seq, MAX(CASE WHEN "
                            "observed_at<=? THEN observed_at END) OVER "
                            "(ORDER BY seq ROWS UNBOUNDED PRECEDING) AS "
                            "retention_at FROM events) UPDATE events SET "
                            "retention_at=(SELECT held.retention_at FROM held "
                            "WHERE held.seq=events.seq)",
                            (now + RETENTION_SECONDS,))
                    # A still-running older process can append to the migrated
                    # table without this nullable column. Keep those rows
                    # readable and eligible for ordinary age pruning, and
                    # repair the run-order marker before the row is evicted.
                    legacy_executed = conn.execute(
                        "SELECT run_id,run_started_at,MAX(observed_at) "
                        "FROM events WHERE retention_at IS NULL AND "
                        "stage NOT IN ('parse','reconcile') AND "
                        "outcome!='dry_run' AND NOT (stage='done' AND "
                        "outcome='skipped' AND reason IN "
                        "('flow_busy','check_job','routine_skip')) "
                        "GROUP BY run_id,run_started_at").fetchall()
                    conn.execute(
                        "UPDATE events SET retention_at=observed_at "
                        "WHERE retention_at IS NULL")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS runs ("
                        "run_order INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "run_id TEXT NOT NULL, run_started_at REAL NOT NULL, "
                        "max_retention_at REAL, executed INTEGER NOT NULL "
                        "DEFAULT 0, expired_barrier INTEGER NOT NULL DEFAULT 0, "
                        "UNIQUE(run_id,run_started_at))")
                    run_columns = {row[1] for row in conn.execute(
                        "PRAGMA table_info(runs)")}
                    needs_retention_backfill = "max_retention_at" not in run_columns
                    needs_executed_backfill = "executed" not in run_columns
                    needs_barrier_backfill = "expired_barrier" not in run_columns
                    if needs_retention_backfill:
                        conn.execute(
                            "ALTER TABLE runs ADD COLUMN max_retention_at REAL")
                    if needs_executed_backfill:
                        conn.execute(
                            "ALTER TABLE runs ADD COLUMN executed INTEGER "
                            "NOT NULL DEFAULT 0")
                    if needs_barrier_backfill:
                        conn.execute(
                            "ALTER TABLE runs ADD COLUMN expired_barrier "
                            "INTEGER NOT NULL DEFAULT 0")
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
                    for legacy_id, legacy_started, legacy_retention in legacy_executed:
                        conn.execute(
                            "UPDATE runs SET executed=1,max_retention_at="
                            "MAX(COALESCE(max_retention_at,?),?) "
                            "WHERE run_id=? AND run_started_at=?",
                            (legacy_retention, legacy_retention,
                             legacy_id, legacy_started))
                    if needs_retention_backfill or orphan:
                        conn.execute(
                            "UPDATE runs SET max_retention_at=("
                            "SELECT MAX(e.retention_at) FROM events e "
                            "WHERE e.run_id=runs.run_id AND "
                            "e.run_started_at=runs.run_started_at "
                            "AND e.stage NOT IN ('parse','reconcile') "
                            "AND e.outcome!='dry_run' AND NOT ("
                            "e.stage='done' AND e.outcome='skipped' AND "
                            "e.reason IN ('flow_busy','check_job','routine_skip'))) "
                            "WHERE max_retention_at IS NULL")
                    if needs_executed_backfill or orphan:
                        conn.execute(
                            "UPDATE runs SET executed=CASE WHEN EXISTS ("
                            "SELECT 1 FROM events e WHERE e.run_id=runs.run_id "
                            "AND e.run_started_at=runs.run_started_at "
                            "AND e.stage NOT IN ('parse','reconcile') "
                            "AND e.outcome!='dry_run' AND NOT ("
                            "e.stage='done' AND e.outcome='skipped' AND "
                            "e.reason IN ('flow_busy','check_job','routine_skip'))) "
                            "THEN 1 ELSE 0 END")
                    if (needs_retention_backfill or needs_executed_backfill
                            or needs_barrier_backfill):
                        conn.execute(
                            "UPDATE runs SET expired_barrier=1 WHERE "
                            "executed=1 AND max_retention_at < ?",
                            (now - RETENTION_SECONDS,))
                    existing_run = conn.execute(
                        "SELECT run_order FROM runs WHERE run_id=? "
                        "AND run_started_at=?", (run_id, started)).fetchone()
                    if not existing_run:
                        conn.execute("INSERT INTO runs(run_id,run_started_at) "
                                     "VALUES(?,?)", (run_id, started))
                    # A clock corrected after a far-future setting must not
                    # make the next observation inherit that future expiry.
                    # Keep prior rows, including ledgerless pending sends:
                    # they may be the only remaining manual-action evidence.
                    conn.execute(
                        "UPDATE runs SET expired_barrier=1 WHERE executed=1 "
                        "AND max_retention_at > ?",
                        (now + RETENTION_SECONDS,))
                    previous_retention = conn.execute(
                        "SELECT MAX(retention_at) FROM events WHERE "
                        "retention_at <= ?",
                        (now + RETENTION_SECONDS,)).fetchone()[0]
                    retention_at = max(now, previous_retention or now)
                    inserted = conn.execute(
                        "INSERT INTO events(run_id,run_started_at,observed_at,"
                        "retention_at,stage,outcome,duration_ms,error,reason) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (run_id, started, now, retention_at, stage, outcome,
                         elapsed, error, reason))
                    inserted_seq = inserted.lastrowid
                    if inserted_seq is None:
                        raise sqlite3.DatabaseError("diagnostic insert had no sequence")
                    if _is_executed_observation(stage, outcome, reason):
                        conn.execute(
                            "UPDATE runs SET max_retention_at="
                            "MAX(COALESCE(max_retention_at,?),?), "
                            "executed=1 WHERE run_id=? AND run_started_at=?",
                            (retention_at, retention_at, run_id, started))
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
                        conn.execute(
                            "UPDATE runs SET executed=0 WHERE run_id=?",
                            (run_id,))
                    if (self.domain == "consult" and stage == "done" and
                            outcome == "skipped" and
                            reason in {"check_job", "routine_skip"}):
                        # An already-served retrigger or a routine preflight
                        # skip never queried HIS. Its initial started marker
                        # must not displace the last executed result.
                        other_execution = conn.execute(
                            "SELECT 1 FROM events WHERE run_id=? AND seq!=? "
                            "AND stage NOT IN ('parse','reconcile') AND "
                            "outcome!='dry_run' AND NOT (stage='query' "
                            "AND outcome='started') AND NOT (stage='done' "
                            "AND outcome='skipped' AND reason IN "
                            "('flow_busy','check_job','routine_skip')) LIMIT 1",
                            (run_id, inserted_seq)).fetchone()
                        if not other_execution:
                            conn.execute(
                                "DELETE FROM events WHERE run_id=? AND "
                                "stage='query' AND outcome='started' AND "
                                "seq<?", (run_id, inserted_seq))
                            conn.execute(
                                "UPDATE runs SET executed=0 WHERE run_id=? "
                                "AND run_started_at=?",
                                (run_id, started))
                    # Store the insertion-time high-water mark on each row:
                    # cap eviction and late older runs cannot change expiry.
                    conn.execute(
                        "UPDATE runs SET expired_barrier=1 WHERE executed=1 "
                        "AND max_retention_at < ?",
                        (now - RETENTION_SECONDS,))
                    conn.execute("DELETE FROM events WHERE "
                                 "COALESCE(retention_at,observed_at) < ?",
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
                            "AND reason IN ('flow_busy','check_job','routine_skip'))")
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
                        # Keep one highest expired executed-run tombstone even
                        # after its events disappear. It blocks an older,
                        # still-retained success from becoming current.
                        conn.execute(
                            "DELETE FROM runs WHERE run_order NOT IN "
                            "(SELECT run_order FROM runs ORDER BY run_order DESC "
                            "LIMIT ?) AND run_order NOT IN (SELECT run_order "
                            "FROM runs WHERE executed=1 AND ("
                            "expired_barrier=1 OR max_retention_at < ?) "
                            "ORDER BY run_order DESC "
                            "LIMIT 1) AND (run_id,run_started_at) NOT IN "
                            "(SELECT DISTINCT run_id,run_started_at FROM events)",
                            (MAX_RUNS, now - RETENTION_SECONDS))
            return True
        except sqlite3.Error as exc:
            return None if sqlite_temporarily_busy(exc) else False
        except (OSError, TypeError, ValueError, OverflowError):
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
            uri = read_only_sqlite_uri(self.path)
            with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as conn:
                conn.execute("PRAGMA query_only=ON")
                # Schema, run-order markers and observations must come from
                # one snapshot when a background writer appends concurrently.
                conn.execute("BEGIN")
                has_runs = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='runs'").fetchone()
                run_column = ("r.run_order" if has_runs else
                              "NULL AS run_order")
                run_join = (" LEFT JOIN runs r ON r.run_id=e.run_id "
                            "AND r.run_started_at=e.run_started_at"
                            if has_runs else "")
                run_columns = ({row[1] for row in conn.execute(
                    "PRAGMA table_info(runs)")} if has_runs else set())
                max_retention_column = (
                    "max_retention_at" if "max_retention_at" in run_columns
                    else "NULL AS max_retention_at")
                executed_column = ("executed" if "executed" in run_columns
                                   else "NULL AS executed")
                barrier_column = (
                    "expired_barrier" if "expired_barrier" in run_columns
                    else "0 AS expired_barrier")
                run_rows = (conn.execute(
                    "SELECT run_order,run_id,run_started_at," +
                    max_retention_column + "," + executed_column + "," +
                    barrier_column + " FROM runs "
                    "ORDER BY run_order").fetchall() if has_runs else [])
                event_columns = {row[1] for row in conn.execute(
                    "PRAGMA table_info(events)")}
                retention_column = (
                    "COALESCE(e.retention_at,e.observed_at)" if
                    "retention_at" in event_columns else
                    "MAX(e.observed_at) OVER (ORDER BY e.seq "
                    "ROWS UNBOUNDED PRECEDING)")
                rows = conn.execute(
                    "SELECT e.seq," + run_column +
                    ",e.run_id,e.run_started_at,e.observed_at,e.stage,e.outcome,"
                    "e.duration_ms,e.error,e.reason," + retention_column +
                    " AS retention_at "
                    "FROM events e" + run_join +
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
        fallback_retention = {}
        fallback_executed = {}
        for (seq, _order, run_id, started, _observed, stage, outcome,
             _duration, _error, reason, retention_at) in rows:
            if isinstance(seq, int) and isinstance(run_id, str):
                key = (run_id, started)
                fallback_orders[key] = min(seq, fallback_orders.get(key, seq))
                executed = _is_executed_observation(stage, outcome, reason)
                if (executed and isinstance(retention_at, (int, float)) and
                        math.isfinite(retention_at)):
                    fallback_retention[key] = max(
                        retention_at,
                        fallback_retention.get(key, retention_at))
                if executed:
                    fallback_executed[key] = True
        for row in rows:
            (seq, run_order, run_id, started, observed, stage, outcome,
             duration, error, reason, retention_at) = row
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
                retention_at = float(retention_at)
                duration = int(duration)
            except (TypeError, ValueError, OverflowError):
                rejected = True
                continue
            if (not math.isfinite(started) or not math.isfinite(observed)
                    or not math.isfinite(retention_at)
                    or started <= 0 or observed <= 0
                    or retention_at < observed
                    or duration < 0 or duration > MAX_DURATION_MS):
                rejected = True
                continue
            try:
                datetime.fromtimestamp(started)
                datetime.fromtimestamp(observed)
            except (OverflowError, OSError, ValueError):
                rejected = True
                continue
            if retention_at < cutoff:
                continue
            if (observed > instant + RETENTION_SECONDS or
                    retention_at > instant + RETENTION_SECONDS):
                clock_anomaly = True
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
        visible_orders = {event["run_order"] for event in result
                          if _is_executed_observation(
                              event["stage"], event["outcome"],
                              event["reason"])}
        known_runs = (run_rows if has_runs else [
            (order, run_id, started,
             fallback_retention.get((run_id, started)),
             fallback_executed.get((run_id, started), False), 0)
            for (run_id, started), order
            in fallback_orders.items()])
        expired_missing = []
        for (order, run_id, started, max_retention, executed,
             expired_barrier) in known_runs:
            if order in visible_orders or executed == 0:
                continue
            if executed is None and not fallback_executed.get(
                    (run_id, started), False):
                continue
            if expired_barrier == 1 or max_retention is None:
                expired_missing.append(order)
            elif isinstance(max_retention, (int, float)) and math.isfinite(
                    max_retention):
                if (max_retention < cutoff or
                        max_retention > instant + RETENTION_SECONDS):
                    expired_missing.append(order)
            else:
                rejected = True
                expired_missing.append(order)
        missing_floor = max(expired_missing, default=0)
        ordering_uncertain = bool(missing_floor and any(
            event["run_order"] < missing_floor for event in result))
        # Keep retained, unresolved warnings and historical successes in the
        # projection; only current-run selection must obey the expired floor.
        # A run's first append may have been lost to contention; its later
        # successful append does not imply that the system clock rolled back.
        # Compare event observations in insertion order, not run start times.
        ordered_observations = sorted(
            (event["seq"], event["observed_at"]) for event in result)
        if any(previous > current + 60 for (_old_order, previous),
               (_new_order, current) in zip(
                   ordered_observations, ordered_observations[1:], strict=False)):
            clock_anomaly = True
        return DiagnosticEvents(result, unavailable=rejected,
                                clock_anomaly=clock_anomaly,
                                ordering_uncertain=ordering_uncertain,
                                current_run_floor=(missing_floor if
                                                   ordering_uncertain else 0))


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
        self._write_lost_storage = False

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
            if self._write_lost_storage and error == "none":
                error = "storage"
        written = self.store.record(
            run_id=self.run_id,
            run_started_at=(self.action_started_at if self.action_started_at is not None
                            else self.started_at),
            stage=stage, outcome=outcome, duration_ms=duration,
            error=error, reason=reason, observed_at=self._wall_clock())
        if written is not True:
            self._write_lost = True
            if written is False:
                self._write_lost_storage = True


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
               show_contended_after_success: bool = False,
               min_run_order: int = 0) -> dict | None:
    """Choose by persisted run acquisition order, then event insertion order."""
    dry_run_ids = {e["run_id"] for e in events if e["outcome"] == "dry_run"}
    floor = max(min_run_order, getattr(events, "current_run_floor", 0))
    eligible = [e for e in events if _run_order(e) >= floor
                and e["run_started_at"] > 0
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
                and e["reason"] in {"flow_busy", "check_job", "routine_skip"}}
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
        lines.append(f"{stage} {outcome} {duration}ms "
                     f"{error} {reason}")
    return "\n".join(lines) + "\n"
