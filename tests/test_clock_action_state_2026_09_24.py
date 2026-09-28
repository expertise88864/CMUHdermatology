"""An uncertain portal response never authorizes a duplicate click or success."""

from datetime import date, datetime, time

import pytest

import autoclock as clock
from clock.action_state import ClockActionState, classify_clock_observation
from cmuh_common.runtime_diagnostics import DiagnosticRun, DiagnosticStore
from cmuh_common.runtime_summary import clock_summary


@pytest.mark.parametrize(
    "read_ok,has_record,pending,auth_failed,expected",
    [
        (False, False, False, False, ClockActionState.READ_UNKNOWN),
        (False, True, True, False, ClockActionState.READ_UNKNOWN),
        (True, False, False, False, ClockActionState.NO_RECORD),
        (True, False, True, False, ClockActionState.CLICK_PENDING),
        (True, True, True, False, ClockActionState.OFFICIAL_CONFIRMED),
        (True, False, False, True, ClockActionState.AUTH_FAILED),
    ],
)
def test_observation_state_preserves_uncertainty_and_official_evidence(
        read_ok, has_record, pending, auth_failed, expected):
    assert classify_clock_observation(
        read_ok=read_ok,
        has_record=has_record,
        click_pending=pending,
        auth_failed=auth_failed,
    ) is expected


class _FakePortal:
    def __init__(self):
        self.reads = iter([[], [], [("0735", "上班")]])
        self.submits = 0

    def login(self, *_args):
        pass

    def read_swipes(self, *_args):
        return None, next(self.reads), None, True

    def select_action(self, *_args):
        pass

    def handle_health(self, *_args):
        pass

    def execute_button(self, *_args):
        return object()

    def highlight(self, *_args):
        pass

    def submit(self, *_args):
        self.submits += 1

    def accept_alert(self, *_args):
        pass

    def verify(self, *_args):
        return False


def test_slow_diagnostics_never_delay_click_after_final_window_check(monkeypatch):
    elapsed = [0.0]
    checked_at = []
    clicked_at = []

    class SlowStore:
        def record(self, **_kwargs):
            elapsed[0] += 0.25  # deterministic slow disk, no real wait
            return True

    diagnostics = DiagnosticRun(SlowStore(), monotonic=lambda: elapsed[0])
    portal = _FakePortal()
    portal.submit = lambda *_a: clicked_at.append(elapsed[0])
    monkeypatch.setattr(clock, "_clock_window_passed",
                        lambda *_a, **_k: checked_at.append(elapsed[0]) or False)
    monkeypatch.setattr(clock, "_is_clock_click_pending", lambda *_a: False)
    monkeypatch.setattr(clock, "_mark_clock_click_pending", lambda *_a: True)
    monkeypatch.setattr(clock.time_module, "sleep", lambda _s: None)
    clock._perform_clock_action_locked(
        None, None, {"username": "synthetic", "password": "synthetic"},
        True, time(7, 30), time(8, 0), task_label="am_in", portal=portal,
        diagnostic_run=diagnostics)
    assert len(clicked_at) == 1
    assert clicked_at == checked_at


def test_fake_portal_persists_uncertain_click_then_confirms_without_resubmit(
        monkeypatch, tmp_path):
    diagnostics = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    monkeypatch.setattr(clock, "DIAGNOSTICS", diagnostics)
    def forbidden(*_args):
        raise AssertionError("real portal called")

    for name in ("login", "get_current_swipe_info", "_verify_clock_recorded"):
        monkeypatch.setattr(clock, name, forbidden)
    monkeypatch.setattr(clock, "_clock_now",
                        lambda: datetime(2026, 10, 5, 7, 40))
    monkeypatch.setattr(clock, "_clock_today", lambda: date(2026, 10, 5))
    monkeypatch.setattr(clock.time_module, "sleep", lambda _seconds: None)
    monkeypatch.setattr(clock.random, "randint", lambda *_args: 1)
    monkeypatch.setattr(clock, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    monkeypatch.setattr(clock, "_clock_state_persistence_enabled", True)
    monkeypatch.setattr(clock, "_clock_done", {})
    monkeypatch.setattr(clock, "_clock_click_pending", {})
    monkeypatch.setattr(clock, "_auth_failed", {})
    monkeypatch.setattr(clock, "_missed_warned", {})
    portal = _FakePortal()
    args = (None, None, {"username": "synthetic", "password": "synthetic"},
            True, time(7, 30), time(8, 0))

    clock._perform_clock_action_locked(*args, task_label="am_in", portal=portal)
    assert portal.submits == 1
    assert not clock._is_clock_done("am_in", "synthetic")
    clock._clock_click_pending.clear()
    clock._load_clock_state()  # 模擬重啟後由磁碟恢復「已點擊待確認」
    assert clock._is_clock_click_pending("am_in", "synthetic")

    clock._perform_clock_action_locked(*args, task_label="am_in", portal=portal)
    assert portal.submits == 1
    assert not clock._is_clock_done("am_in", "synthetic")

    clock._perform_clock_action_locked(*args, task_label="am_in", portal=portal)
    assert portal.submits == 1
    assert clock._is_clock_done("am_in", "synthetic")
    assert not clock._is_clock_click_pending("am_in", "synthetic")
    outcomes = [e["outcome"] for e in diagnostics.read()]
    assert "no_record" in outcomes
    assert "click_pending" in outcomes
    assert "official_confirmed" in outcomes
    assert b"synthetic" not in diagnostics.path.read_bytes()


def test_unreadable_fake_portal_never_submits_or_marks_done(monkeypatch, tmp_path):
    diagnostics = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    monkeypatch.setattr(clock, "DIAGNOSTICS", diagnostics)
    portal = _FakePortal()
    portal.read_swipes = lambda *_args: (None, [], None, False)
    failures = []
    monkeypatch.setattr(clock, "_clock_today", lambda: date(2026, 10, 5))
    monkeypatch.setattr(clock, "_clock_done", {})
    monkeypatch.setattr(clock, "_clock_click_pending", {})
    monkeypatch.setattr(clock, "exponential_backoff_sleep",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(clock, "_handle_clock_failure",
                        lambda *_args: failures.append(True))

    clock._perform_clock_action_locked(
        None, None, {"username": "synthetic", "password": "synthetic"},
        True, time(7, 30), time(8, 0), task_label="am_in", portal=portal)

    assert portal.submits == 0
    assert not clock._is_clock_done("am_in", "synthetic")
    assert not clock._is_clock_click_pending("am_in", "synthetic")
    assert failures == [True]
    assert diagnostics.read()[0]["outcome"] == "read_unknown"


def test_two_fake_accounts_report_partial_confirmation(monkeypatch, tmp_path):
    diagnostics = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    run = DiagnosticRun(diagnostics)
    monkeypatch.setattr(clock, "DIAGNOSTICS", diagnostics)
    monkeypatch.setattr(clock, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    monkeypatch.setattr(clock, "_clock_state_persistence_enabled", True)
    monkeypatch.setattr(clock, "_clock_done", {})
    monkeypatch.setattr(clock, "_clock_click_pending", {})
    monkeypatch.setattr(clock, "_auth_failed", {})
    monkeypatch.setattr(clock, "_clock_today", lambda: date(2026, 10, 5))
    monkeypatch.setattr(clock, "exponential_backoff_sleep",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(clock, "_handle_clock_failure", lambda *_args: None)

    unreadable = _FakePortal()
    unreadable.read_swipes = lambda *_args: (None, [], None, False)
    confirmed = _FakePortal()
    confirmed.read_swipes = lambda *_args: (
        None, [("0735", "上班")], None, True)

    for username, portal in (("synthetic-a", unreadable),
                             ("synthetic-b", confirmed)):
        clock._perform_clock_action_locked(
            None, None, {"username": username, "password": "synthetic"},
            True, time(7, 30), time(8, 0), task_label="am_in",
            portal=portal, diagnostic_run=run)

    events = diagnostics.read()
    observed = max(event["observed_at"] for event in events)
    text = clock_summary(events, None, today=datetime.fromtimestamp(observed).date(),
                         now=observed)
    assert "最近觀察：部分帳號未確認" in text
    assert "人工處理：請檢查打卡網站狀態" in text
    assert unreadable.submits == confirmed.submits == 0
    assert b"synthetic-a" not in diagnostics.path.read_bytes()
    assert b"synthetic-b" not in diagnostics.path.read_bytes()


def test_transient_prepare_timeout_then_official_record_is_not_partial(
        monkeypatch, tmp_path):
    diagnostics = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    monkeypatch.setattr(clock, "DIAGNOSTICS", diagnostics)
    monkeypatch.setattr(clock, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    monkeypatch.setattr(clock, "_clock_state_persistence_enabled", True)
    monkeypatch.setattr(clock, "_clock_done", {})
    monkeypatch.setattr(clock, "_clock_click_pending", {})
    monkeypatch.setattr(clock, "_auth_failed", {})
    monkeypatch.setattr(clock, "_clock_today", lambda: date(2026, 10, 5))
    monkeypatch.setattr(clock.time_module, "sleep", lambda _seconds: None)
    monkeypatch.setattr(clock, "exponential_backoff_sleep",
                        lambda *_args, **_kwargs: None)

    class FlakyPortal(_FakePortal):
        def __init__(self):
            super().__init__()
            self.reads = iter([[], [("0735", "上班")]])
            self.attempts = 0

        def select_action(self, *_args):
            self.attempts += 1
            if self.attempts == 1:
                raise clock.TimeoutException("synthetic timeout")

    portal = FlakyPortal()
    clock._perform_clock_action_locked(
        None, None, {"username": "synthetic", "password": "synthetic"},
        True, time(7, 30), time(8, 0), task_label="am_in", portal=portal)
    events = diagnostics.read()
    observed = max(event["observed_at"] for event in events)
    text = clock_summary(events, None, today=datetime.fromtimestamp(observed).date(),
                         now=observed)
    assert portal.attempts == 1
    assert portal.submits == 0
    assert "最近觀察：官方已確認" in text
    assert "部分帳號未確認" not in text
    assert not any(event["stage"] == "done" and event["outcome"] == "failed"
                   for event in diagnostics.read())


def test_dry_run_does_not_replace_scheduled_diagnostics(monkeypatch, tmp_path):
    diagnostics = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    monkeypatch.setattr(clock, "DIAGNOSTICS", diagnostics)
    monkeypatch.setattr(clock.time_module, "sleep", lambda _seconds: None)
    monkeypatch.setattr(clock.messagebox, "showinfo", lambda *_args: None)
    portal = _FakePortal()
    portal.reads = iter([[]])
    clock._perform_clock_action_locked(
        None, None, {"username": "synthetic", "password": "synthetic"},
        True, time(0, 0), time(23, 59), dry_run=True,
        task_label="test", portal=portal)
    assert portal.submits == 0
    assert diagnostics.read() == []
    assert not diagnostics.path.exists()


def test_fake_website_load_failure_has_bounded_private_free_timing(
        monkeypatch, tmp_path):
    from cmuh_common.runtime_diagnostics import DiagnosticRun
    diagnostics = DiagnosticStore(tmp_path / "clock_diag.sqlite3", "clock")
    run = DiagnosticRun(diagnostics)
    token = clock._CURRENT_CLOCK_DIAGNOSTIC_RUN.set(run)

    class FakeDriver:
        def get(self, _url):
            raise clock.WebDriverException("private password 9876543210")

        def refresh(self):
            pass

    monkeypatch.setattr(clock, "exponential_backoff_sleep",
                        lambda *_a, **_k: None)
    try:
        with pytest.raises(RuntimeError):
            clock.login(FakeDriver(), None, "synthetic", "private password")
    finally:
        clock._CURRENT_CLOCK_DIAGNOSTIC_RUN.reset(token)
    events = diagnostics.read()
    assert len([e for e in events if e["stage"] == "website"
                and e["outcome"] == "failed"]) == 5
    assert b"private password" not in diagnostics.path.read_bytes()
