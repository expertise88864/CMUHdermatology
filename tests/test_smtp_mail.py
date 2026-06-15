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


# === [opt B1] load_credentials 純讀取、建範本分離 ===

def test_load_credentials_is_read_only_when_file_missing(tmp_path, monkeypatch):
    """檔案不存在時 load_credentials 不建檔(避免熱路徑 IMAP poll 每 20s 寫檔)，回 default。"""
    missing = tmp_path / "smtp_credentials.json"
    monkeypatch.setattr(smtp_mail, "CREDENTIALS_FILE", missing)

    cred = smtp_mail.load_credentials()

    assert not missing.exists()  # 純讀取，不建檔
    assert cred["password"] == ""  # 回 default(password 空 → is_configured False)
    assert cred["host"] == smtp_mail.DEFAULT_CREDENTIALS["host"]


def test_ensure_credentials_template_creates_then_preserves(tmp_path, monkeypatch):
    """ensure_credentials_template 缺檔時建範本；已存在則不覆寫使用者內容。"""
    path = tmp_path / "smtp_credentials.json"
    monkeypatch.setattr(smtp_mail, "CREDENTIALS_FILE", path)

    smtp_mail.ensure_credentials_template()
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["host"] == \
        smtp_mail.DEFAULT_CREDENTIALS["host"]

    # 已存在 → 不覆寫使用者已填的 password
    path.write_text(json.dumps({"password": "userset"}), encoding="utf-8")
    smtp_mail.ensure_credentials_template()
    assert json.loads(path.read_text(encoding="utf-8"))["password"] == "userset"


def test_build_message_html_is_multipart_alternative():
    """[美化] 有 html_body → 內文走 multipart/alternative,同時含 plain 與 html;
    截圖附件仍夾帶(外層 mixed)。"""
    msg = smtp_mail._build_message(
        "a@b.com", "Sender", ["r@x.tw"], "subj", "plain fallback",
        attachment_path=None, html_body="<b>hi</b>")
    types = [p.get_content_type() for p in msg.walk()]
    assert "multipart/alternative" in types
    assert "text/plain" in types and "text/html" in types
    # plain 在前(fallback),html 在後(客戶端優先顯示)
    leaves = [p.get_content_type() for p in msg.walk()
              if not p.is_multipart()]
    assert leaves.index("text/plain") < leaves.index("text/html")


def test_build_message_no_html_stays_plain():
    """無 html_body → 維持舊行為:純 text/plain,不產生 alternative。"""
    msg = smtp_mail._build_message(
        "a@b.com", "Sender", ["r@x.tw"], "subj", "plain only",
        attachment_path=None)
    types = [p.get_content_type() for p in msg.walk()]
    assert "multipart/alternative" not in types
    assert "text/plain" in types and "text/html" not in types
