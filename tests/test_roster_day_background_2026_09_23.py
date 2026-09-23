"""Exercise the actual Tk callback with a controlled, side-effect-free solver."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from cmuh_common.roster.service import RosterService
from cmuh_common.roster.storage import RosterStorage
from cmuh_common.roster.ui import day_tab


def test_day_dependency_preflight_rejects_wrong_version(monkeypatch):
    monkeypatch.setattr(day_tab.importlib, "import_module",
                        lambda _name: SimpleNamespace(__version__="0.0.0"))
    with pytest.raises(RuntimeError, match="版本不符"):
        day_tab._verify_day_solver_dependency()


def test_scheduler_startup_keeps_day_solver_optional():
    import scheduler

    assert not any(name == "ortools" for _spec, name in scheduler.REQUIRED_LIBS)


@pytest.fixture
def tab(tk_root, tmp_path, monkeypatch):
    storage = RosterStorage(str(tmp_path))
    storage.save_config({"pgy_members": [{"id": "A"}, {"id": "B"}]})
    storage.save_clinic_template({"template": {"0": {"上午": [{"room": "101"}]}}})
    service = RosterService(storage)
    app = SimpleNamespace(ym="2026-10")
    panel = day_tab.DayScheduleTab(tk_root, service, app)
    panel.pack(fill="both", expand=True)
    monkeypatch.setattr(day_tab, "_verify_day_solver_dependency", lambda: None)
    monkeypatch.setattr(day_tab.messagebox, "showerror", lambda *_a, **_k: None)
    monkeypatch.setattr(day_tab.messagebox, "showinfo", lambda *_a, **_k: None)
    yield panel
    if panel.winfo_exists():
        panel.destroy()


def _pump(root, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_optional_solver_install_failure_keeps_manual_tab_open(tab, monkeypatch):
    attempts = []
    monkeypatch.setattr(day_tab, "_verify_day_solver_dependency",
                        lambda: (_ for _ in ()).throw(RuntimeError("未安裝")))
    monkeypatch.setattr(day_tab.messagebox, "askyesno", lambda *_a, **_k: True)
    monkeypatch.setattr(day_tab, "ensure_dependencies",
                        lambda _deps: (_ for _ in ()).throw(SystemExit(1)))
    monkeypatch.setattr(tab, "_solve_day_async", lambda: attempts.append("solve"))

    tab._on_auto()
    assert not attempts
    assert tab.winfo_exists() and not tab._day_solving


def test_optional_solver_install_runs_on_tk_thread(tab, monkeypatch):
    ready = [False]
    installed_by = []

    def verify():
        if not ready[0]:
            raise RuntimeError("未安裝")

    def install(_deps):
        installed_by.append(threading.get_ident())
        ready[0] = True

    monkeypatch.setattr(day_tab, "_verify_day_solver_dependency", verify)
    monkeypatch.setattr(day_tab.messagebox, "askyesno", lambda *_a, **_k: True)
    monkeypatch.setattr(day_tab, "ensure_dependencies", install)
    monkeypatch.setattr(tab, "_solve_day_async", lambda: installed_by.append("solve"))

    tab._on_auto()
    assert installed_by == [threading.get_ident(), "solve"]


def test_pgy_only_solve_keeps_tk_responsive_and_cancel_discards_result(tab, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, previews = [], []

    def fake_solve(ym, *, today=None, control=None):
        calls.append(ym)
        entered.set()
        release.wait(3)
        return object()

    monkeypatch.setattr(tab.service, "run_day_solve", fake_solve)
    monkeypatch.setattr(tab.service, "day_solution_is_current", lambda *a, **k: True)
    monkeypatch.setattr(tab, "_preview_and_accept", previews.append)
    start = time.monotonic()
    try:
        tab._on_auto()
        assert time.monotonic() - start < 0.5
        assert entered.wait(2)
        tab._on_auto()
        assert calls == ["2026-10"]
        assert time.monotonic() - start < 2
        tab._cancel_day_solve()
        assert tab._day_control.cancelled
        release.set()
        assert _pump(tab, lambda: not tab._day_solving)
        assert previews == []
        assert str(tab._auto_btn["state"]) == "normal"
    finally:
        release.set()


def test_month_change_discards_completed_old_solve(tab, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    previews = []

    def fake_solve(ym, *, today=None, control=None):
        entered.set()
        release.wait(3)
        return object()

    monkeypatch.setattr(tab.service, "run_day_solve", fake_solve)
    monkeypatch.setattr(tab.service, "day_solution_is_current", lambda *a, **k: True)
    monkeypatch.setattr(tab, "_preview_and_accept", previews.append)
    try:
        tab._on_auto()
        assert entered.wait(2)
        tab._on_month_change("2026-11")
        release.set()
        assert _pump(tab, lambda: not tab._day_solving)
        assert tab.app.ym == "2026-11"
        assert previews == []
    finally:
        release.set()


def test_changed_input_prevents_preview(tab, monkeypatch):
    previews = []
    monkeypatch.setattr(tab.service, "run_day_solve", lambda *_a, **_k: object())
    monkeypatch.setattr(tab.service, "day_solution_is_current", lambda *a, **k: False)
    monkeypatch.setattr(tab, "_preview_and_accept", previews.append)
    tab._on_auto()
    assert _pump(tab, lambda: not tab._day_solving)
    assert not previews


def test_solver_error_reports_and_reenables_button(tab, monkeypatch):
    errors = []

    def broken(*_a, **_k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(tab.service, "run_day_solve", broken)
    monkeypatch.setattr(day_tab.messagebox, "showerror", lambda *args, **_k: errors.append(args))
    tab._on_auto()
    assert _pump(tab, lambda: not tab._day_solving)
    assert errors and "synthetic failure" in str(errors[0])
    assert str(tab._auto_btn["state"]) == "normal"


def test_missing_dependency_reports_and_reenables_button(tab, monkeypatch):
    errors = []
    monkeypatch.setattr(day_tab, "_verify_day_solver_dependency",
                        lambda: (_ for _ in ()).throw(RuntimeError("請重新啟動排班程式")))
    monkeypatch.setattr(day_tab.messagebox, "showerror", lambda *a, **_k: errors.append(a))
    tab._on_auto()
    assert _pump(tab, lambda: not tab._day_solving)
    assert errors and "請重新啟動排班程式" in str(errors[0])
    assert str(tab._auto_btn["state"]) == "normal"


def test_closing_tab_while_solving_stops_worker_callback(tab, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def fake_solve(*_a, **_k):
        entered.set()
        release.wait(2)
        return object()

    monkeypatch.setattr(tab.service, "run_day_solve", fake_solve)
    tab._on_auto()
    assert entered.wait(1)
    control = tab._day_control
    tab.destroy()
    release.set()
    assert control.cancelled
    assert not tab.winfo_exists()
