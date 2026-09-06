"""Authentication results must bind pass and identity to one actual result."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cmuh_common import imap_reader as ir  # noqa: E402


@pytest.mark.parametrize("results", [
    "dkim=pass header.d=other.example; dkim=fail header.i=@trusted.example",
    "dkim=pass; dkim=fail header.d=trusted.example",
    "spf=pass; spf=fail smtp.mailfrom=trusted.example",
    "dmarc=pass; dmarc=fail header.from=trusted.example",
    "dmarc=passive header.from=trusted.example",
    "x-dkim=pass header.d=trusted.example",
    "dkim=pass xheader.d=trusted.example",
    "dkim=fail (dkim=pass header.d=trusted.example)",
    'dkim=fail reason="dkim=pass header.d=trusted.example"',
    'dkim=fail reason="bad; dkim=pass header.d=trusted.example"',
    'dkim=pass reason="header.d=trusted.example" header.d=other.example',
    "dkim=pass (header.d=trusted.example) header.d=other.example",
    "dkim=pass (nested (header.i=@trusted.example)) header.d=other.example",
    "dkim=pass header.d=trusted.example header.d=other.example",
    'dkim=pass header.d=trusted.example reason="unclosed',
    "dkim=pass header.d=trusted.example (unclosed",
])
def test_unrelated_or_nonpassing_evidence_is_not_authenticated(results):
    assert not ir._from_is_authenticated("mx.google.com; " + results,
                                         "doctor@trusted.example")


@pytest.mark.parametrize("results", [
    "dkim=pass header.d=trusted.example",
    "dkim=fail header.d=other.example; dkim=pass header.d=trusted.example",
    "dkim=pass header.d=other.example; dkim=pass header.i=@trusted.example",
    "spf=pass smtp.mailfrom=sender@trusted.example",
    "dmarc=pass header.from=trusted.example",
    'dkim=pass header.d="trusted.example"',
    'dkim=pass header.i="test account"@trusted.example',
    "dkim (note) = pass (nested (note)) header.d=trusted.example",
    "dkim/1=pass header . d = trusted.example",
    'dkim=pass reason="ok; not another result" header.d=trusted.example',
    'dkim=pass reason="escaped \\" quote" header.d=trusted.example',
    "dkim=pass header.d=mail.trusted.example",
    "DKIM=PASS HEADER.D=TRUSTED.EXAMPLE",
])
def test_valid_result_keeps_its_own_identity(results):
    assert ir._from_is_authenticated("mx.google.com; " + results,
                                    "doctor@trusted.example")


@pytest.mark.parametrize("results, expected", [
    ("dkim=pass; dkim=fail header.d=trusted.example", False),
    ("dkim=pass header.d=trusted.example", True),
])
def test_trigger_uid_carries_the_correct_authentication_result(
        monkeypatch, results, expected):
    header = ("Subject: TRIG\r\nFrom: doctor@trusted.example\r\n"
              "Authentication-Results: mx.google.com; " + results + "\r\n").encode()
    class FakeIMAP:
        sock = None
        def login(self, *args):
            return "OK", []
        def select(self, *args):
            return "OK", []
        def response(self, code):
            return code, [b"123"]
        def uid(self, command, *args):
            if command.lower() == "search":
                return "OK", [b"7"]
            if command.lower() == "fetch":
                return "OK", [(b"x", header), b")"]
            pytest.fail("matched messages must not be marked read in this test")
    monkeypatch.setattr(ir.imaplib, "IMAP4_SSL", lambda *args, **kwargs: FakeIMAP())
    monkeypatch.setattr(ir, "_load_imap_settings", lambda: {
        "host": "imap.example", "port": 993, "username": "test", "password": "test",
    })
    result = ir.check_trigger("TRIG", defer_mark_matched=True, max_age_sec=0)
    assert result["error"] is None
    assert result["matched_uids"] == [("7", "doctor@trusted.example", expected)]
    assert result["authenticated_senders"] == (["doctor@trusted.example"] if expected else [])
