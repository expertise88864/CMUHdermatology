"""Bounded, code-only observations for the consult and clock settings windows.

These records are *not* delivery or clock state. The delivery ledger and the
persisted clock state remain authoritative for send/click decisions. No caller
may pass free-form text (including exception messages) into this store.
"""

from __future__ import annotations

import math
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


RETENTION_SECONDS = 7 * 86400
MAX_EVENTS = 256
MAX_DURATION_MS = 30 * 60 * 1000

_STAGES = {
    "consult": frozenset({
        "query", "parse", "redact", "prepare", "send", "reconcile", "done",
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
    "verify_clock", "fix_credentials", "check_storage",
})


def _safe_code(value: object, allowed: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


class DiagnosticStore:
    """One small SQLite file per program; writes never decide clinical actions."""

    def __init__(self, path: str | Path, domain: str):
        if domain not in _STAGES:
            raise ValueError("unknown diagnostic domain")
        self.path = Path(path)
        self.domain = domain

    def record(self, *, run_id: str, run_started_at: float, stage: str,
               outcome: str, duration_ms: float = 0, error: str = "none",
               reason: str = "none", observed_at: float | None = None) -> None:
        """Best-effort append; never changes the caller's send/click outcome."""
        if not isinstance(run_id, str) or len(run_id) != 32 or not all(
                c in "0123456789abcdef" for c in run_id):
            return
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
                    not (0 < started <= now + 60) or now <= 0):
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Diagnostics must never hold a clinical action behind a busy DB.
            with closing(sqlite3.connect(str(self.path), timeout=0.02)) as conn:
                with conn:
                    conn.execute("PRAGMA busy_timeout=20")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS events ("
                        "seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL, "
                        "run_started_at REAL NOT NULL, observed_at REAL NOT NULL, "
                        "stage TEXT NOT NULL, outcome TEXT NOT NULL, "
                        "duration_ms INTEGER NOT NULL, error TEXT NOT NULL, "
                        "reason TEXT NOT NULL)")
                    conn.execute(
                        "INSERT INTO events(run_id,run_started_at,observed_at,"
                        "stage,outcome,duration_ms,error,reason) VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, started, now, stage, outcome, elapsed, error, reason))
                    conn.execute("DELETE FROM events WHERE observed_at < ?",
                                 (now - RETENTION_SECONDS,))
                    conn.execute("DELETE FROM events WHERE seq NOT IN "
                                 "(SELECT seq FROM events ORDER BY seq DESC LIMIT ?)",
                                 (MAX_EVENTS,))
        except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
            pass

    def read(self, *, now: float | None = None) -> list[dict]:
        """Read-only projection; no file creation or schema migration."""
        instant = time.time() if now is None else now
        cutoff = instant - RETENTION_SECONDS
        if not self.path.is_file():
            return []
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as conn:
                conn.execute("PRAGMA query_only=ON")
                rows = conn.execute(
                    "SELECT run_id,run_started_at,observed_at,stage,outcome,"
                    "duration_ms,error,reason FROM events WHERE observed_at>=? "
                    "AND observed_at<=? ORDER BY seq DESC LIMIT ?",
                    (cutoff, instant + 60, MAX_EVENTS)).fetchall()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return []
        result = []
        for row in rows:
            run_id, started, observed, stage, outcome, duration, error, reason = row
            if (not isinstance(run_id, str) or len(run_id) != 32 or
                    any(c not in "0123456789abcdef" for c in run_id) or
                    stage not in _STAGES[self.domain] or outcome not in _OUTCOMES):
                continue
            try:
                started = float(started)
                observed = float(observed)
                duration = int(duration)
            except (TypeError, ValueError, OverflowError):
                continue
            if (not math.isfinite(started) or not math.isfinite(observed)
                    or duration < 0 or duration > MAX_DURATION_MS):
                continue
            result.append({
                "run_id": run_id, "run_started_at": started,
                "observed_at": observed, "stage": stage,
                "outcome": outcome, "duration_ms": duration,
                "error": _safe_code(error, _ERRORS, "unknown"),
                "reason": _safe_code(reason, _REASONS, "none"),
            })
        return result


class DiagnosticRun:
    """Carries one process-local observation identity across retries."""

    def __init__(self, store: DiagnosticStore, *, wall_clock=time.time,
                 monotonic=time.monotonic):
        self.store = store
        self.run_id = uuid.uuid4().hex
        self.started_at = wall_clock()
        self._wall_clock = wall_clock
        self._monotonic = monotonic

    def start(self) -> float:
        return self._monotonic()

    def emit(self, stage: str, outcome: str, *, since: float | None = None,
             error: str = "none", reason: str = "none") -> None:
        duration = (self._monotonic() - since) * 1000 if since is not None else 0
        self.store.record(
            run_id=self.run_id, run_started_at=self.started_at,
            stage=stage, outcome=outcome, duration_ms=duration,
            error=error, reason=reason, observed_at=self._wall_clock())


def latest_run(events: list[dict], *, now: float | None = None) -> dict | None:
    """Choose by run start, so a late event from an older run cannot take over."""
    instant = time.time() if now is None else now
    eligible = [e for e in events if 0 < e["run_started_at"] <= instant + 60
                and e["observed_at"] <= instant + 60]
    if not eligible:
        return None
    newest = max(eligible, key=lambda e: (e["run_started_at"], e["observed_at"]))
    run_events = [e for e in eligible if e["run_id"] == newest["run_id"]]
    return max(run_events, key=lambda e: e["observed_at"])


def last_success(events: list[dict], *, domain: str) -> float | None:
    # A parsed empty roster is only an intermediate observation: returning to
    # the HIS main screen or the rest of the job can still fail afterward.
    completed = {"consult": {("send", "accepted"),
                              ("done", "empty_roster"),
                              ("done", "no_new")},
                 "clock": {("read", "official_confirmed"),
                           ("confirm", "official_confirmed"),
                           ("done", "official_confirmed")}}[domain]
    found = [e["observed_at"] for e in events
             if (e["stage"], e["outcome"]) in completed]
    return max(found) if found else None


def render_safe_events(events: list[dict]) -> str:
    """Plain-text input for the existing safe diagnostic bundle exporter."""
    lines = ["CMUH runtime diagnostics (allowlisted codes only)"]
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
