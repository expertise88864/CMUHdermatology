"""Explicit observation states for one account in one clock window."""

from enum import Enum


class ClockActionState(Enum):
    NOT_CHECKED = "not_checked"
    READ_UNKNOWN = "read_unknown"
    NO_RECORD = "no_record"
    CLICK_PENDING = "click_pending"
    OFFICIAL_CONFIRMED = "official_confirmed"
    AUTH_FAILED = "auth_failed"


def classify_clock_observation(
    *, read_ok: bool, has_record: bool, click_pending: bool,
    auth_failed: bool = False,
) -> ClockActionState:
    """Official records outrank a prior click; unreadable data never means empty."""
    if auth_failed:
        return ClockActionState.AUTH_FAILED
    if not read_ok:
        return ClockActionState.READ_UNKNOWN
    if has_record:
        return ClockActionState.OFFICIAL_CONFIRMED
    if click_pending:
        return ClockActionState.CLICK_PENDING
    return ClockActionState.NO_RECORD
