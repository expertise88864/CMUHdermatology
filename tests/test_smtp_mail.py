# -*- coding: utf-8 -*-
"""SMTP rate-limit recovery tests."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import smtp_mail  # noqa: E402


def _credentials() -> dict:
    return {
        "host": "smtp.example.com",
        "port": 587,
        "username": "sender@example.com",
        "password": "secret",
        "use_tls": True,
        "from_address": "sender@example.com",
        "from_name": "Sender",
    }


def test_failed_send_releases_rate_limit_slot(monkeypatch):
    smtp_mail._recent_send_reservations.clear()

    def fail_send(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(smtp_mail, "_send_once", fail_send)

    with pytest.raises(RuntimeError, match="offline"):
        smtp_mail.send_mail(
            ["recipient@example.com"], "subject", "body",
            override_credentials=_credentials(), max_retries=0,
        )

    assert list(smtp_mail._recent_send_reservations) == []


def test_successful_send_keeps_rate_limit_slot(monkeypatch):
    smtp_mail._recent_send_reservations.clear()
    monkeypatch.setattr(smtp_mail, "_send_once", lambda *_args, **_kwargs: None)

    smtp_mail.send_mail(
        ["recipient@example.com"], "subject", "body",
        override_credentials=_credentials(), max_retries=0,
    )

    assert len(smtp_mail._recent_send_reservations) == 1
    smtp_mail._recent_send_reservations.clear()


def test_negative_retry_count_still_sends_once(monkeypatch):
    smtp_mail._recent_send_reservations.clear()
    sent = []
    monkeypatch.setattr(
        smtp_mail,
        "_send_once",
        lambda *_args, **_kwargs: sent.append("sent"),
    )

    smtp_mail.send_mail(
        ["recipient@example.com"], "subject", "body",
        override_credentials=_credentials(), max_retries=-1,
    )

    assert sent == ["sent"]
    smtp_mail._recent_send_reservations.clear()


@pytest.mark.parametrize("value", [True, None, "bad"])
def test_bad_retry_count_uses_default(value):
    assert smtp_mail._normalize_max_retries(value) == \
        smtp_mail.DEFAULT_MAX_RETRIES


def test_retry_count_is_clamped():
    assert smtp_mail._normalize_max_retries(-5) == 0
    assert smtp_mail._normalize_max_retries(999) == smtp_mail.MAX_RETRIES


@pytest.mark.parametrize("bad_port", [True, -1, 0, 65536, "bad"])
def test_load_credentials_replaces_invalid_smtp_port(tmp_path, monkeypatch,
                                                     bad_port):
    path = tmp_path / "smtp_credentials.json"
    path.write_text(json.dumps({"port": bad_port}), encoding="utf-8")
    monkeypatch.setattr(smtp_mail, "CREDENTIALS_FILE", path)

    assert smtp_mail.load_credentials()["port"] == \
        smtp_mail.DEFAULT_CREDENTIALS["port"]
