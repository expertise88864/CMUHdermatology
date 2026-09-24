"""An uncertain portal response never authorizes a duplicate click or success."""

from datetime import date, datetime, time

import pytest

import autoclock as clock
from clock.action_state import ClockActionState, classify_clock_observation


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


def test_fake_portal_persists_uncertain_click_then_confirms_without_resubmit(
        monkeypatch, tmp_path):
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


def test_unreadable_fake_portal_never_submits_or_marks_done(monkeypatch):
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
