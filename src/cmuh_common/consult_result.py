"""Typed outcome at the consultation HIS-query boundary.

An empty roster is a successful observation.  An unreadable roster and a
failed HIS query are distinct, so neither can silently become an empty list.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class ConsultQueryStatus(Enum):
    READY = "ready"
    ROSTER_UNKNOWN = "roster_unknown"
    READ_FAILED = "read_failed"


@dataclass(frozen=True)
class ConsultQueryResult:
    status: ConsultQueryStatus
    shot: Any = None
    extracted_text: str = ""
    extracted_html: str = ""
    roster_texts: list[str] | None = None
    privacy_identities: list[dict] | None = None
    privacy_roster_complete: bool = False
    login_token: Any = None
    error: Exception | None = None


def capture_consult_query(
    query: Callable[[str], tuple], trigger_label: str
) -> ConsultQueryResult:
    """Adapt the existing seven-field query API without changing its retries."""
    try:
        raw = query(trigger_label)
        if not isinstance(raw, tuple) or len(raw) != 7:
            raise ValueError("HIS query returned an invalid result shape")
        (shot, text, html, roster, identities, roster_complete, token) = raw
        if roster is not None and not isinstance(roster, list):
            raise ValueError("HIS query returned an invalid roster")
        return ConsultQueryResult(
            status=(ConsultQueryStatus.ROSTER_UNKNOWN if roster is None
                    else ConsultQueryStatus.READY),
            shot=shot,
            extracted_text=text,
            extracted_html=html,
            roster_texts=roster,
            privacy_identities=identities,
            privacy_roster_complete=bool(roster_complete),
            login_token=token,
        )
    except Exception as exc:
        return ConsultQueryResult(status=ConsultQueryStatus.READ_FAILED, error=exc)
