"""A simulated portal click is not a confirmed clock record."""

from datetime import datetime, time
from types import SimpleNamespace

import autoclock as clock
from cmuh_common.runtime_diagnostics import DiagnosticStore


def test_click_unknown_then_official_record_confirms_without_second_click(
        monkeypatch, tmp_path):
    reads = iter([[], [], ["confirmed-record"]])
    clicked = []
    marked = []
    wait = SimpleNamespace(until=lambda _condition: SimpleNamespace(accept=lambda: None))
    driver = SimpleNamespace(execute_script=lambda script, _element: clicked.append(script))
    monkeypatch.setattr(clock, "login", lambda *_a: None)
    monkeypatch.setattr(clock, "get_current_swipe_info",
                        lambda *_a: (None, next(reads), None, True))
    monkeypatch.setattr(clock, "_check_swipes", lambda _act, _start, _end, rows: bool(rows))
    monkeypatch.setattr(clock, "_clock_window_passed", lambda *_a, **_k: False)
    monkeypatch.setattr(clock, "_verify_clock_recorded", lambda *_a: False)
    monkeypatch.setattr(clock, "_mark_clock_done", lambda *a: marked.append(a))
    monkeypatch.setattr(clock, "_clock_click_pending", {}, raising=False)
    monkeypatch.setattr(clock, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    monkeypatch.setattr(clock, "_clock_state_persistence_enabled", True)
    monkeypatch.setattr(clock, "handle_health_declaration", lambda *_a: None)
    monkeypatch.setattr(clock, "WebDriverWait", lambda *_a, **_k: wait)
    monkeypatch.setattr(clock.time_module, "sleep", lambda _seconds: None)
    monkeypatch.setattr(clock.random, "randint", lambda *_a: 1)
    args = (driver, wait, {"username": "synthetic", "password": "synthetic"},
            True, time(7, 30), time(8, 0))

    clock._perform_clock_action_locked(*args, task_label="am_in")
    assert not marked, "點擊後回讀不明，不能記成打卡成功"
    first_clicks = len(clicked)
    assert first_clicks > 0

    clock._perform_clock_action_locked(*args, task_label="am_in")
    assert len(clicked) == first_clicks, "官方仍查無紀錄時也不得重複點擊"
    assert not marked

    clock._perform_clock_action_locked(*args, task_label="am_in")
    assert len(clicked) == first_clicks, "之後讀到官方紀錄，不得再次點擊"
    assert marked == [("am_in", "synthetic")]


def test_failed_logins_after_uncertain_submit_keep_pending_diagnostic(
        monkeypatch, tmp_path):
    store = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    monkeypatch.setattr(clock, "DIAGNOSTICS", store)
    monkeypatch.setattr(clock, "_clock_window_passed", lambda *_a, **_k: False)
    monkeypatch.setattr(clock, "_check_swipes", lambda *_a: False)
    monkeypatch.setattr(clock, "_mark_clock_click_pending", lambda *_a: True)
    monkeypatch.setattr(clock.time_module, "sleep", lambda _seconds: None)
    monkeypatch.setattr(clock.random, "randint", lambda *_a: 1)
    monkeypatch.setattr(clock, "exponential_backoff_sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(clock, "_handle_clock_failure", lambda *_a, **_k: None)

    class FakePortal:
        logins = 0
        submits = 0

        def login(self, *_args):
            self.logins += 1
            if self.logins > 1:
                raise clock.WebDriverException("synthetic login failure")

        def read_swipes(self, *_args):
            return None, [], None, True

        def select_action(self, *_args):
            pass

        def handle_health(self, *_args):
            pass

        def execute_button(self, *_args):
            return object()

        def submit(self, *_args):
            self.submits += 1
            raise clock.WebDriverException("synthetic uncertain submit")

    portal = FakePortal()
    clock._perform_clock_action_locked(
        object(), object(), {"username": "synthetic", "password": "synthetic"},
        True, time(7, 30), time(8, 0), task_label="am_in", portal=portal)
    assert portal.submits == 1
    assert portal.logins == 5
    latest = store.read()[0]
    assert (latest["stage"], latest["outcome"], latest["reason"]) == (
        "done", "click_pending", "verify_clock")


def test_click_pending_survives_restart_and_clears_on_official_record(
        tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    monkeypatch.setattr(clock, "_clock_state_persistence_enabled", True)
    monkeypatch.setattr(clock, "_clock_click_pending", {})
    monkeypatch.setattr(clock, "_clock_done", {})

    assert clock._mark_clock_click_pending("am_in", "synthetic")
    clock._clock_click_pending.clear()  # 模擬重啟
    clock._load_clock_state()
    assert clock._is_clock_click_pending("am_in", "synthetic")

    clock._mark_clock_done("am_in", "synthetic")
    assert not clock._is_clock_click_pending("am_in", "synthetic")


def test_state_write_failure_prevents_submit_click(monkeypatch):
    clicked = []
    wait = SimpleNamespace(until=lambda _condition: object())
    driver = SimpleNamespace(execute_script=lambda script, _element: clicked.append(script))
    monkeypatch.setattr(clock, "login", lambda *_a: None)
    monkeypatch.setattr(clock, "get_current_swipe_info",
                        lambda *_a: (None, [], None, True))
    monkeypatch.setattr(clock, "_check_swipes", lambda *_a: False)
    monkeypatch.setattr(clock, "_clock_window_passed", lambda *_a, **_k: False)
    monkeypatch.setattr(clock, "_mark_clock_click_pending", lambda *_a: False)
    monkeypatch.setattr(clock, "handle_health_declaration", lambda *_a: None)
    monkeypatch.setattr(clock.time_module, "sleep", lambda _seconds: None)
    monkeypatch.setattr(clock.random, "randint", lambda *_a: 1)

    clock._perform_clock_action_locked(
        driver, wait, {"username": "synthetic", "password": "synthetic"},
        True, time(7, 30), time(8, 0), task_label="am_in")
    assert clicked == ["arguments[0].click();"]  # 只選單選鈕，未按執行


def test_failed_pending_write_does_not_block_next_safe_attempt(monkeypatch):
    monkeypatch.setattr(clock, "_clock_click_pending", {})
    monkeypatch.setattr(clock, "_clock_state_persistence_enabled", True)
    outcomes = iter([False, True])
    monkeypatch.setattr(clock, "_save_clock_state", lambda: next(outcomes))

    assert not clock._mark_clock_click_pending("am_in", "synthetic")
    assert not clock._is_clock_click_pending("am_in", "synthetic")
    assert clock._mark_clock_click_pending("am_in", "synthetic")
    assert clock._is_clock_click_pending("am_in", "synthetic")


def test_pending_mark_requires_durable_state_enabled(monkeypatch):
    monkeypatch.setattr(clock, "_clock_click_pending", {})
    monkeypatch.setattr(clock, "_clock_state_persistence_enabled", False)
    assert not clock._mark_clock_click_pending("am_in", "synthetic")
    assert not clock._is_clock_click_pending("am_in", "synthetic")


def test_pending_click_still_qualifies_for_missed_window_alert(monkeypatch):
    monkeypatch.setattr(clock, "_clock_done", {})
    monkeypatch.setattr(clock, "_clock_click_pending", {
        ("mon_am_in", "synthetic"): clock.date.today().isoformat()})
    hits = clock._windows_needing_missed_warning(
        datetime(2026, 10, 5, 8, 3),
        [{"username": "synthetic", "schedule": {"mon_am_in": True}}],
        is_done=clock._is_clock_done, already_warned=lambda _key: False)
    assert hits == [("mon_am_in", ["synthetic"])]
