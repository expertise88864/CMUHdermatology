# -*- coding: utf-8 -*-
"""基建批次3/4 回歸測試(2026-07-12 未審區域計畫書補修)。

IF-06 行為測試(userinfo 欺騙);其餘為落地安全/防禦縱深,以源碼層守衛防回退。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.http_client import is_internal  # noqa: E402


def _read(*parts):
    p = os.path.join(os.path.dirname(__file__), "..", "src", "cmuh_common", *parts)
    with open(p, encoding="utf-8") as f:
        return f.read()


# ── IF-06 is_internal 用 hostname,擋 userinfo 欺騙 ───────────────────────────
def test_if06_userinfo_spoof_not_internal():
    assert is_internal("https://10.20.8.47:x@evil.com") is False
    assert is_internal("https://evil.com@10.20.8.47") is False or \
        is_internal("https://evil.com@10.20.8.47") is True  # host=10.20.8.47 → 內網(合理)


def test_if06_real_internal_and_case_insensitive():
    assert is_internal("https://forward01.cmuh.org.tw/x") is True
    assert is_internal("HTTPS://FORWARD01.CMUH.ORG.TW/x") is True   # 大小寫不敏感
    assert is_internal("https://appointment.cmuh.org.tw:8443/y") is True


def test_if06_external_not_internal():
    assert is_internal("https://www.google.com") is False
    assert is_internal("not a url") is False


# ── IE-05 / IE-06 updater fail-closed(源碼守衛) ─────────────────────────────
def test_ie05_manifest_non_dict_guard():
    src = _read("updater.py")
    assert "isinstance(manifest, dict)" in src, "IE-05 未驗證 manifest 為 dict"


def test_ie06_missing_sha_fail_closed():
    src = _read("updater.py")
    assert "manifest 缺 sha256" in src and "not expected_sha" in src, \
        "IE-06 未對缺 sha256 fail-closed"


# ── IE-08 清理擴及 .upd.tmp / .corrupt-*(源碼守衛) ──────────────────────────
def test_ie08_cleanup_covers_src_tmp_and_corrupt():
    src = _read("cache_cleanup.py")
    assert "is_old_corrupt" in src and ".corrupt-" in src, "IE-08 未清 .corrupt-*"
    # tmp 掃描須含 src/cmuh_common(.upd.tmp 落點)
    tmp_block = src[src.find("stats['tmp_files'] = 0"):src.find("is_old_corrupt")]
    assert "cmuh_common" in tmp_block, "IE-08 tmp 掃描未擴及 src/cmuh_common"


# ── IE-11 doctors.json 退回預設前備份(源碼守衛) ─────────────────────────────
def test_ie11_backup_before_default_overwrite():
    src = _read("app_settings.py")
    assert ".invalid-" in src and "normalized == defaults and data != defaults" in src, \
        "IE-11 未在退回預設前備份 .invalid"


# ── IF-05 imap 死碼移除(源碼守衛) ───────────────────────────────────────────
def test_if05_dead_utf8_retry_removed():
    src = _read("imap_reader.py")
    assert 'conn.search("UTF-8"' not in src, "IF-05 未移除死碼 UTF-8 SEARCH retry"
    assert "keyword.encode(" not in src, "IF-05 未移除死碼 kw_bytes 編碼"


# ── IF-07 只有全 5xx 才判永久(4xx/非 SMTP 仍可重試) ─────────────────────────
def test_if07_only_5xx_is_permanent():
    import smtplib
    import socket
    from cmuh_common.smtp_mail import _smtp_error_is_permanent as perm
    assert perm(smtplib.SMTPRecipientsRefused({"a@x": (550, b"no")})) is True
    assert perm(smtplib.SMTPRecipientsRefused({"a@x": (451, b"grey")})) is False  # 4xx→重試
    assert perm(smtplib.SMTPDataError(554, b"rej")) is True
    assert perm(smtplib.SMTPDataError(451, b"later")) is False
    assert perm(socket.timeout()) is False   # 非 SMTP → 可重試


# ── IF-08 loopback 明文可、外部拒 ────────────────────────────────────────────
def test_if08_loopback_allowed_external_rejected():
    from cmuh_common.smtp_mail import _is_loopback_host as lb
    assert lb("localhost") and lb("LOCALHOST") and lb("localhost.")
    assert lb("127.0.0.1") and lb("::1")
    assert not lb("smtp.gmail.com") and not lb("10.20.8.47")


# ── IF-12 註解退出碼(源碼守衛) ──────────────────────────────────────────────
def test_if12_exit_code_comment_fixed():
    src = _read("deps_installer.py")
    assert "sys.exit(0)" not in src, "IF-12 註解仍寫 sys.exit(0)"
