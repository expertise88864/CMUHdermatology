"""Consultation query states and sealed mail content use synthetic identities."""

from dataclasses import FrozenInstanceError

import pytest

import consult_query as cq
from cmuh_common.consult_result import ConsultQueryStatus, capture_consult_query


def _query_with(roster):
    return lambda _label: (
        None, "原因：皮疹", "<p>皮疹</p>", roster, [], True, None,
    )


def test_empty_roster_is_not_a_read_failure_or_unknown():
    empty = capture_consult_query(_query_with([]), "poll")
    unknown = capture_consult_query(_query_with(None), "poll")

    def failed(_label):
        raise OSError("synthetic HIS read failure")

    error = capture_consult_query(failed, "poll")
    assert empty.status is ConsultQueryStatus.READY
    assert empty.roster_texts == []
    assert unknown.status is ConsultQueryStatus.ROSTER_UNKNOWN
    assert unknown.roster_texts is None
    assert error.status is ConsultQueryStatus.READ_FAILED
    assert isinstance(error.error, OSError)


def test_invalid_query_shape_is_a_failure_not_an_empty_roster():
    result = capture_consult_query(lambda _label: (), "poll")
    assert result.status is ConsultQueryStatus.READ_FAILED
    assert isinstance(result.error, ValueError)


def test_sealed_content_hides_identity_in_subject_bodies_and_has_no_attachment():
    identity = {"name": "測試甲", "chart": "9876543210"}
    delivery = cq._seal_consult_delivery(
        recipients=["doctor@example.test"],
        subject="測試甲 9876543210 會診",
        text_body="床位 A1；主治 醫師乙；測試甲 9876543210；原因：皮疹",
        html_body="<p>測試甲 9876543210；原因：皮疹</p>",
        privacy_identities=[identity],
        message_id="<synthetic@example.test>",
        business_key="synthetic",
        occurrence_keys=["synthetic-event"],
    )
    assert "測試甲" not in delivery.subject + delivery.text_body + delivery.html_body
    assert "9876543210" not in delivery.subject + delivery.text_body + delivery.html_body
    assert "原因：皮疹" in delivery.text_body
    assert "原因：皮疹" in delivery.html_body
    assert delivery.attachment is None
    assert delivery.recipients == ("doctor@example.test",)
    with pytest.raises(FrozenInstanceError):
        delivery.text_body = "changed"
