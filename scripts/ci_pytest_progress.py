"""Opt-in pytest phase evidence. No test output, parameter values or exceptions.

Serial runs write one stream (``events.jsonl``). Under pytest-xdist the
controller writes ``events.jsonl`` from the reports the workers forward (the
authoritative completion order, used for counts and the test-identity digest),
and every worker additionally writes its own ``events.<workerid>.jsonl`` with
the real phase starts and its own collection. Each stream is numbered
contiguously by its own process; there is no cross-process ordering to check.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

import pytest


PHASES = ("setup", "call", "teardown")
WORKER_ID = re.compile(r"gw[0-9]{1,4}")


def case_id(nodeid: str) -> str:
    return hashlib.sha256(nodeid.encode()).hexdigest()[:16]


def cases_digest(cases) -> str:
    """Order-independent digest of a set of opaque test identities."""
    return hashlib.sha256("\n".join(sorted(cases)).encode("ascii")).hexdigest()


def worker_stream(path: Path, worker: str) -> Path:
    return path.with_name(f"{path.stem}.{worker}{path.suffix}")


class Progress:
    def __init__(self, path: Path, worker: str | None = None):
        self.stream = path.open("x", encoding="utf-8", buffering=1)
        self.started = time.perf_counter()
        self.worker = worker
        # "worker" is fixed at configure; serial/controller is known at sessionstart.
        self.mode = "worker" if worker else "serial"
        self.seq = 0
        self.selected = 0
        self.selected_digest = None
        self.finished = 0
        self.finished_cases = set()
        self.totals = {phase: 0.0 for phase in PHASES}
        self.errors = []
        self.workers = {}

    def error(self, name: str):
        if name not in self.errors:
            self.errors.append(name)

    def emit(self, event: str, **data):
        self.seq += 1
        row = {"seq": self.seq, "elapsed_s": time.perf_counter() - self.started,
               "event": event, **data}
        try:
            self.stream.write(json.dumps(row, ensure_ascii=True) + "\n")
        except OSError:
            self.error("progress_write")

    @staticmethod
    def identity(nodeid: str) -> dict:
        # Parametrized IDs may contain values; use an opaque ID instead.
        label = nodeid.split("[", 1)[0].replace("\\", "/")
        if (not re.fullmatch(r"[A-Za-z0-9_./:\-]{1,300}", label) or
                label.startswith("/") or re.match(r"[A-Za-z]:", label) or
                ".." in label.split("/")):
            label = "test"
        return {"test": label, "case": case_id(nodeid)}

    def console(self, line: str):
        # Workers' stdout is not the job log (execnet owns it); the controller
        # prints the phases for them from the forwarded reports.
        if self.mode == "worker":
            return
        try:
            sys.__stdout__.write(f"[cmuh-pytest] {line}\n")
            sys.__stdout__.flush()
        except (OSError, ValueError, AttributeError):
            self.error("console_write")

    def phase_start(self, item, phase: str):
        identity = self.identity(item.nodeid)
        self.emit("phase_started", phase=phase, **identity)
        # Bypass pytest capture. The runner log retains the last phase even
        # when forced termination prevents artifact upload or sessionfinish.
        self.console(f"{phase} {identity['test']} case={identity['case']}")

    def select(self, nodeids, worker: str | None = None):
        cases = [case_id(nodeid) for nodeid in nodeids]
        digest = cases_digest(cases)
        if len(set(cases)) != len(cases):
            self.error("duplicate_selection")
        if worker is not None:
            self.emit("worker_collected", worker=worker, tests=len(cases), digest=digest)
            if self.selected_digest is not None:
                if (len(cases), digest) != (self.selected, self.selected_digest):
                    self.error("worker_collection_mismatch")
                return
        self.selected = len(cases)
        self.selected_digest = digest
        self.emit("selected", tests=self.selected, digest=digest)

    def pytest_sessionstart(self, session):
        if self.mode != "worker" and session.config.pluginmanager.get_plugin("dsession"):
            self.mode = "controller"
        self.emit("session_started", mode=self.mode,
                  **({"worker": self.worker} if self.worker else {}))

    @pytest.hookimpl(hookwrapper=True)
    def pytest_collection(self, session):
        self.emit("collection_started")
        started = time.perf_counter()
        yield
        self.emit("collection_finished", duration_s=time.perf_counter() - started)

    def pytest_collection_finish(self, session):
        # Not called on an xdist controller: it never collects.
        self.select([item.nodeid for item in session.items])

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        worker = str(getattr(node.gateway, "id", ""))
        if not WORKER_ID.fullmatch(worker):
            self.error("worker_identity")
            worker = "unknown"
        self.select(ids, worker)

    def pytest_collectstart(self, collector):
        identity = self.identity(collector.nodeid)
        self.emit("collector_started", **identity)
        self.console(f"collect {identity['test']}")

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

    def pytest_runtest_logstart(self, nodeid, location):
        if self.mode == "controller":
            identity = self.identity(nodeid)
            self.emit("test_started", **identity)
            self.console(f"setup {identity['test']} case={identity['case']}")

    def pytest_runtest_logreport(self, report):
        identity = self.identity(report.nodeid)
        extra = {}
        if self.mode == "controller":
            worker = str(getattr(getattr(getattr(report, "node", None), "gateway", None), "id", ""))
            extra["worker"] = worker if WORKER_ID.fullmatch(worker) else "unknown"
        if report.when in self.totals:
            self.totals[report.when] += report.duration
        else:
            # xdist reports a crashed worker's test with an unknown phase.
            self.error("unknown_phase")
        self.emit("phase_finished", phase=str(report.when), outcome=report.outcome,
                  duration_s=report.duration, **identity, **extra)
        if self.mode == "controller":
            if report.when == "setup":
                following = "call" if report.outcome == "passed" else "teardown"
            elif report.when == "call":
                following = "teardown"
            else:
                return
            self.console(f"{following} {identity['test']} case={identity['case']}")

    def pytest_runtest_logfinish(self, nodeid, location):
        identity = self.identity(nodeid)
        if identity["case"] in self.finished_cases:
            self.error("duplicate_finish")
        self.finished_cases.add(identity["case"])
        self.finished += 1
        self.emit("test_finished", **identity)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        worker = str(getattr(getattr(node, "gateway", None), "id", ""))
        summary = getattr(node, "workeroutput", {}).get("cmuh_progress")
        if not WORKER_ID.fullmatch(worker):
            self.error("worker_identity")
            worker = "unknown"
        if error is not None:
            self.error("worker_crashed")
        if (not isinstance(summary, dict) or summary.get("worker") != worker or
                summary.get("errors") or type(summary.get("events")) is not int or
                type(summary.get("finished")) is not int):
            self.error("worker_evidence")
            summary = {}
        if worker in self.workers:
            self.error("worker_replaced")
        self.workers[worker] = {"worker": worker, "events": summary.get("events"),
                                "finished": summary.get("finished")}
        self.emit("worker_finished", worker=worker, crashed=error is not None,
                  events=summary.get("events"), finished=summary.get("finished"))

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        terminal = {"exit_code": int(exitstatus), "mode": self.mode,
                    "selected": self.selected, "finished": self.finished,
                    "selected_digest": self.selected_digest,
                    "phase_seconds": self.totals}
        if self.mode == "worker":
            terminal.update(worker=self.worker,
                            finished_digest=cases_digest(self.finished_cases))
        elif self.mode == "controller":
            terminal["workers"] = [self.workers[key] for key in sorted(self.workers)]
        self.emit("session_finished", **terminal, diagnostic_errors=list(self.errors))
        if self.mode == "worker":
            # Read by the controller in pytest_testnodedown.
            session.config.workeroutput["cmuh_progress"] = {
                "worker": self.worker, "events": self.seq, "finished": self.finished,
                "errors": list(self.errors)}
        try:
            self.stream.close()
        except OSError:
            pass  # A diagnostic close must not replace pytest's own verdict.


def pytest_configure(config):
    path = os.environ.get("CMUH_CI_PROGRESS_FILE")
    if not path:
        return
    workerinput = getattr(config, "workerinput", None)
    worker = None
    if workerinput is not None:
        worker = str(workerinput.get("workerid", ""))
        if not WORKER_ID.fullmatch(worker):
            config.workeroutput["cmuh_progress"] = {"worker": None,
                                                    "errors": ["worker_identity"]}
            return
    try:
        target = Path(path) if worker is None else worker_stream(Path(path), worker)
        progress = Progress(target, worker)
    except OSError:
        if worker is not None:
            # The controller turns this into a diagnostic error; no silent gap.
            config.workeroutput["cmuh_progress"] = {"worker": worker,
                                                    "errors": ["progress_unavailable"]}
        print("pytest phase evidence unavailable", file=sys.__stderr__, flush=True)
        return
    config.pluginmanager.register(progress, "cmuh-phase-evidence")
