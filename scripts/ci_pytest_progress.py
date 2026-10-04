"""Opt-in pytest phase evidence. No test output, parameter values or exceptions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

import pytest


class Progress:
    def __init__(self, path: Path):
        self.stream = path.open("x", encoding="utf-8", buffering=1)
        self.started = time.perf_counter()
        self.seq = 0
        self.selected = 0
        self.finished = 0
        self.totals = {phase: 0.0 for phase in ("setup", "call", "teardown")}
        self.errors = []

    def emit(self, event: str, **data):
        self.seq += 1
        row = {"seq": self.seq, "elapsed_s": time.perf_counter() - self.started,
               "event": event, **data}
        try:
            self.stream.write(json.dumps(row, ensure_ascii=True) + "\n")
        except OSError:
            if "progress_write" not in self.errors:
                self.errors.append("progress_write")

    @staticmethod
    def identity(nodeid: str) -> dict:
        # Parametrized IDs may contain values; use an opaque ID instead.
        label = nodeid.split("[", 1)[0].replace("\\", "/")
        if (not re.fullmatch(r"[A-Za-z0-9_./:\-]{1,300}", label) or
                label.startswith("/") or re.match(r"[A-Za-z]:", label) or
                ".." in label.split("/")):
            label = "test"
        return {"test": label, "case": hashlib.sha256(nodeid.encode()).hexdigest()[:16]}

    def phase_start(self, item, phase: str):
        identity = self.identity(item.nodeid)
        self.emit("phase_started", phase=phase, **identity)
        # Bypass pytest capture. The runner log retains the last phase even
        # when forced termination prevents artifact upload or sessionfinish.
        try:
            sys.__stdout__.write(
                f"[cmuh-pytest] {phase} {identity['test']} case={identity['case']}\n")
            sys.__stdout__.flush()
        except (OSError, ValueError, AttributeError):
            if "console_write" not in self.errors:
                self.errors.append("console_write")

    def pytest_sessionstart(self, session):
        self.emit("session_started")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_collection(self, session):
        self.emit("collection_started")
        started = time.perf_counter()
        yield
        self.emit("collection_finished", duration_s=time.perf_counter() - started)

    def pytest_collection_finish(self, session):
        self.selected = len(session.items)
        self.emit("selected", tests=self.selected)

    def pytest_collectstart(self, collector):
        identity = self.identity(collector.nodeid)
        self.emit("collector_started", **identity)
        try:
            sys.__stdout__.write(f"[cmuh-pytest] collect {identity['test']}\n")
            sys.__stdout__.flush()
        except (OSError, ValueError, AttributeError):
            if "console_write" not in self.errors:
                self.errors.append("console_write")

    def pytest_collectreport(self, report):
        self.emit("collector_finished", outcome=report.outcome, **self.identity(report.nodeid))

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        self.phase_start(item, "setup")
        yield

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        self.phase_start(item, "call")
        yield

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item):
        self.phase_start(item, "teardown")
        yield

    def pytest_runtest_logreport(self, report):
        self.totals[report.when] += report.duration
        self.emit("phase_finished", phase=report.when, outcome=report.outcome,
                  duration_s=report.duration, **self.identity(report.nodeid))

    def pytest_runtest_logfinish(self, nodeid, location):
        self.finished += 1
        self.emit("test_finished", **self.identity(nodeid))

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        self.emit("session_finished", exit_code=int(exitstatus),
                  selected=self.selected, finished=self.finished,
                  phase_seconds=self.totals, diagnostic_errors=self.errors)
        try:
            self.stream.close()
        except OSError:
            pass  # A diagnostic close must not replace pytest's own verdict.


def pytest_configure(config):
    path = os.environ.get("CMUH_CI_PROGRESS_FILE")
    if path:
        try:
            progress = Progress(Path(path))
        except OSError:
            print("pytest phase evidence unavailable", file=sys.__stderr__, flush=True)
            return
        config.pluginmanager.register(progress, "cmuh-phase-evidence")
