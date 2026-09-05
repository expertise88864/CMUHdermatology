"""QUIT failure must not replace the result of SMTP DATA."""
import smtplib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cmuh_common import smtp_mail as sm  # noqa: E402


class Server:
    # Use the real stdlib context exit (QUIT followed by close), with no network.
    __enter__ = smtplib.SMTP.__enter__
    __exit__ = smtplib.SMTP.__exit__

    def __init__(self, final_error=None, login_error=None):
        self.final_error = final_error
        self.login_error = login_error
        self.sent = 0
        self.closed = False
        self.quit_code = None

    def ehlo(self):
        pass

    def ehlo_or_helo_if_needed(self):
        pass

    def starttls(self, **_kwargs):
        pass

    def login(self, *_args):
        if self.login_error:
            raise self.login_error

    def mail(self, *_args):
        return 250, b"ok"

    def rcpt(self, address, *_args):
        return (452, b"busy") if address == "retry@example.invalid" else (250, b"ok")

    def docmd(self, command, *_args):
        if command.upper() == "QUIT":
            if self.quit_code is not None:
                return self.quit_code, b"closing"
            raise TimeoutError("QUIT timed out")
        assert command.lower() == "data"
        return 354, b"send content"

    def send(self, _payload):
        self.sent += 1

    def getreply(self):
        if self.final_error:
            raise self.final_error
        return 250, b"queued"

    def close(self):
        self.closed = True


@pytest.fixture
def smtp(monkeypatch):
    server = Server()
    monkeypatch.setattr(sm.smtplib, "SMTP", lambda *_a, **_kw: server)
    monkeypatch.setattr(sm.smtplib, "SMTP_SSL", lambda *_a, **_kw: server)
    monkeypatch.setattr(sm, "_reserve_rate_limit_slot", lambda *_a: object())
    monkeypatch.setattr(sm, "_rollback_rate_limit_slot", lambda *_a: None)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    return server


def credentials(port=587):
    return {"host": "localhost", "port": port, "use_tls": True,
            "username": "test", "password": "test-only",
            "from_address": "sender@example.invalid", "from_name": "Test"}


@pytest.mark.parametrize("port", [587, 465])
@pytest.mark.parametrize("quit_code", [None, 421, 221])
def test_success_and_partial_refusals_survive_quit_failure(smtp, port, quit_code):
    smtp.quit_code = quit_code
    refused = sm.send_mail(
        ["accepted@example.invalid", "retry@example.invalid"], "test", "test",
        override_credentials=credentials(port), max_retries=2)
    assert refused == {"retry@example.invalid": (452, b"busy")}
    assert smtp.sent == 1, "a successful DATA must not be retried because QUIT failed"
    assert smtp.closed


def test_unknown_data_result_survives_quit_failure(smtp):
    original = ConnectionResetError("lost final DATA reply")
    smtp.final_error = original
    with pytest.raises(sm.DeliveryOutcomeUnknown) as caught:
        sm.send_mail(["accepted@example.invalid"], "test", "test",
                     override_credentials=credentials(), max_retries=2)
    assert caught.value.__cause__ is original
    assert smtp.sent == 1
    assert smtp.closed


def test_authentication_failure_survives_quit_failure(smtp):
    original = smtplib.SMTPAuthenticationError(535, b"bad login")
    smtp.login_error = original
    with pytest.raises(RuntimeError) as caught:
        sm.send_mail(["accepted@example.invalid"], "test", "test",
                     override_credentials=credentials(), max_retries=2)
    assert caught.value.__cause__ is original
    assert smtp.sent == 0
    assert smtp.closed
