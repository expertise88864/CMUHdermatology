# -*- coding: utf-8 -*-
"""IMAP active-connection cleanup tests."""
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import imap_reader  # noqa: E402


class _FakeSocket:
    def __init__(self):
        self.shutdown_calls = []
        self.close_calls = 0

    def shutdown(self, how):
        self.shutdown_calls.append(how)

    def close(self):
        self.close_calls += 1


class _FakeConn:
    def __init__(self):
        self.sock = _FakeSocket()


def test_force_close_active_closes_all_overlapping_connections():
    first = _FakeConn()
    second = _FakeConn()
    imap_reader._active_conns.clear()
    imap_reader._set_active(first)
    imap_reader._set_active(second)

    assert imap_reader.force_close_active() is True
    assert first.sock.shutdown_calls == [socket.SHUT_RDWR]
    assert second.sock.shutdown_calls == [socket.SHUT_RDWR]
    assert first.sock.close_calls == 1
    assert second.sock.close_calls == 1

    imap_reader._clear_active(first)
    imap_reader._clear_active(second)


def test_force_close_active_clear_discards_dead_conn():
    """[opt B2] clear=True：關閉後一併從 _active_conns 移除(供 worker 放生路徑使用，
    避免死連線物件被 set 永久強引用)。預設 clear=False 維持原契約(只關不移除)。"""
    conn = _FakeConn()
    imap_reader._active_conns.clear()
    imap_reader._set_active(conn)
    assert conn in imap_reader._active_conns

    # 預設(clear=False)：關閉但保留在 set(維持既有語意)
    assert imap_reader.force_close_active() is True
    assert conn in imap_reader._active_conns

    # clear=True：關閉後從 set 移除
    assert imap_reader.force_close_active(clear=True) is True
    assert conn not in imap_reader._active_conns
    assert imap_reader._active_conns == set()

    # 無 active 連線時回 False
    assert imap_reader.force_close_active(clear=True) is False


# === [會診2 2026-06-11] 觸發信時效過濾 ===

def _internaldate_raw(epoch: float) -> bytes:
    import imaplib as _imaplib
    s = _imaplib.Time2Internaldate(epoch)  # 例：'"17-Jul-1996 02:44:25 -0700"'
    return f"1 (INTERNALDATE {s})".encode()


def test_message_age_seconds_parses_internaldate():
    import time as _time

    class _Conn:
        def fetch(self, uid, parts):
            return ("OK", [_internaldate_raw(_time.time() - 7200)])

    age = imap_reader._message_age_seconds(_Conn(), b"1")
    assert age is not None
    assert 7000 < age < 7400  # 約 2 小時(留時鐘/時區換算餘裕)


def test_message_age_seconds_fails_open():
    class _Boom:
        def fetch(self, uid, parts):
            raise RuntimeError("network")

    class _BadResp:
        def fetch(self, uid, parts):
            return ("OK", [b"1 (FLAGS ())"])  # 無 INTERNALDATE

    assert imap_reader._message_age_seconds(_Boom(), b"1") is None
    assert imap_reader._message_age_seconds(_BadResp(), b"1") is None


def test_check_trigger_skips_stale_but_triggers_fresh(monkeypatch):
    """主旨命中但超過時效的舊信 → 標已讀、不觸發；新信照常觸發。
    INTERNALDATE 解析不出 → fail-open 照常觸發。"""
    import time as _time

    class _FakeIMAP:
        sock = None

        def __init__(self, *a, **k):
            self.stored = []

        def login(self, *a):
            return ("OK", [])

        def select(self, *a):
            return ("OK", [])

        def uid(self, command, *args):
            # [2026-08-06 外審 P2-02] imap_reader 改用 UID API(避免 EXPUNGE 位移
            # 導致讀錯/標錯信)。假 IMAP 依樣分派到既有的實作。
            return getattr(self, command.lower())(*args)

        def search(self, charset, *criteria):
            return ("OK", [b"1 2 3"])

        def fetch(self, uid, parts):
            if "INTERNALDATE" in str(parts):
                ages = {b"1": 12 * 3600, b"2": 600}  # 1=12小時前(舊), 2=10分鐘前(新)
                if uid == b"3":
                    return ("OK", [b"3 (FLAGS ())"])  # 解析不出 → fail-open
                return ("OK", [_internaldate_raw(_time.time() - ages[uid])])
            hdr = (f"Subject: TRIG test {uid.decode()}\r\n"
                   f"From: doc{uid.decode()}@x.tw\r\n").encode()
            return ("OK", [(b"x", hdr), b")"])

        def store(self, ids, op, flags):
            self.stored.append(ids)
            return ("OK", [])

    created = {}

    def _fake_imap(*a, **k):
        created["conn"] = _FakeIMAP()
        return created["conn"]

    monkeypatch.setattr(imap_reader.imaplib, "IMAP4_SSL", _fake_imap)
    monkeypatch.setattr(
        imap_reader, "_load_imap_settings",
        lambda: {"host": "h", "port": 993, "username": "u", "password": "p"})

    r = imap_reader.check_trigger("TRIG", max_age_sec=6 * 3600)

    assert r["error"] is None
    assert r["triggered"] is True
    # uid 1(12 小時前) 被時效過濾；uid 2(新)+uid 3(fail-open) 觸發
    assert r["matched"] == 2
    assert sorted(r["matched_senders"]) == ["doc2@x.tw", "doc3@x.tw"]
    # 標已讀涵蓋觸發的 2,3 + 陳舊清掉的 1
    assert created["conn"].stored == ["2,3,1"]


def test_check_trigger_no_age_filter_by_default(monkeypatch):
    """max_age_sec 未傳 → 不過濾(向後相容)，且不應多發 INTERNALDATE fetch。"""
    fetch_parts = []

    class _FakeIMAP:
        sock = None

        def __init__(self, *a, **k):
            pass

        def login(self, *a):
            return ("OK", [])

        def select(self, *a):
            return ("OK", [])

        def uid(self, command, *args):
            # [2026-08-06 外審 P2-02] imap_reader 改用 UID API(避免 EXPUNGE 位移
            # 導致讀錯/標錯信)。假 IMAP 依樣分派到既有的實作。
            return getattr(self, command.lower())(*args)

        def search(self, charset, *criteria):
            return ("OK", [b"1"])

        def fetch(self, uid, parts):
            fetch_parts.append(str(parts))
            hdr = b"Subject: TRIG x\r\nFrom: a@x.tw\r\n"
            return ("OK", [(b"x", hdr), b")"])

        def store(self, ids, op, flags):
            return ("OK", [])

    monkeypatch.setattr(imap_reader.imaplib, "IMAP4_SSL",
                        lambda *a, **k: _FakeIMAP())
    monkeypatch.setattr(
        imap_reader, "_load_imap_settings",
        lambda: {"host": "h", "port": 993, "username": "u", "password": "p"})

    r = imap_reader.check_trigger("TRIG")
    assert r["triggered"] is True and r["matched"] == 1
    assert not any("INTERNALDATE" in p for p in fetch_parts)


@pytest.mark.parametrize("bad_port", [True, -1, 0, 65536, "bad"])
def test_load_imap_settings_replaces_invalid_port(monkeypatch, bad_port):
    monkeypatch.setattr(
        imap_reader, "load_credentials",
        lambda: {"imap_port": bad_port, "username": "", "password": ""},
    )

    assert imap_reader._load_imap_settings()["port"] == \
        imap_reader.DEFAULT_IMAP_PORT


# ── [2026-08-06 外審] IMAP 觸發的正確性與可信授權 ────────────────────────────

def test_folded_headers_are_parsed(monkeypatch):
    """★P2-04★ RFC 5322 允許 header 折行(長 display name / 多段 encoded-word)。

    舊版逐行 `startswith(b"subject:")` 只拿得到第一行 → 主旨關鍵字漏判、
    From 變空字串 → 白名單寄件人被誤拒、觸發【靜默】失效。
    """
    # 主旨與 From 都折成兩行(第二行以空白起始 = RFC 5322 的 folding)
    raw = (b"Subject: CMUH derm consult\r\n"
           b" trigger keyword\r\n"
           b"From: A Very Long Doctor Display Name\r\n"
           b" <Doctor@Example.COM>\r\n")
    subj, frm, _auth = imap_reader._parse_trigger_headers(raw)
    # 折行後半段必須被接起來(舊版逐行比對只拿得到第一行 → 關鍵字漏判)
    assert "trigger keyword" in subj, f"折行主旨沒接起來:{subj!r}"
    assert "Doctor@Example.COM" in frm, f"折行 From 沒接起來:{frm!r}"

    # 中文(RFC 2047 encoded-word)折行也要還原成同一個字串
    import base64
    b1 = base64.b64encode("皮膚科".encode()).decode()
    b2 = base64.b64encode("會診觸發".encode()).decode()
    raw2 = (f"Subject: =?UTF-8?B?{b1}?=\r\n"
            f" =?UTF-8?B?{b2}?=\r\n"
            "From: <dr@example.com>\r\n").encode()
    subj2, _f2, _a2 = imap_reader._parse_trigger_headers(raw2)
    assert "皮膚科" in subj2 and "會診觸發" in subj2, \
        f"折行的中文 encoded-word 沒接起來:{subj2!r}"


def test_parse_headers_never_raises_on_garbage():
    for raw in (b"", b"\x00\xff not a header", b"Subject:\r\n"):
        assert isinstance(imap_reader._parse_trigger_headers(raw), tuple)


# ── P1-05:寄件人驗證(From 可偽造,要看 Authentication-Results)────────────────
def test_dmarc_pass_counts_as_authenticated():
    ar = "mx.google.com; dkim=pass header.d=example.com; spf=pass; dmarc=pass"
    assert imap_reader._from_is_authenticated(ar, "dr@example.com") is True


def test_missing_header_is_not_authenticated():
    """沒有證據 ≠ 通過(fail-closed)。"""
    assert imap_reader._from_is_authenticated("", "dr@example.com") is False
    assert imap_reader._from_is_authenticated(None, "dr@example.com") is False


def test_all_fail_is_not_authenticated():
    ar = "mx.google.com; dkim=fail header.d=evil.tw; spf=fail; dmarc=fail"
    assert imap_reader._from_is_authenticated(ar, "dr@example.com") is False


def test_spf_pass_for_a_different_domain_is_not_enough():
    """★關鍵★ spf=pass 只證明【信封】寄件者,不證明 From。網域對不上不算通過
    —— 否則攻擊者用自己網域通過 SPF、把 From 偽造成白名單醫師就過關了。"""
    ar = "mx.google.com; spf=pass smtp.mailfrom=attacker.tw; dkim=none"
    assert imap_reader._from_is_authenticated(ar, "dr@example.com") is False


def test_dkim_pass_with_matching_domain_is_authenticated():
    ar = "mx.google.com; dkim=pass header.d=example.com; spf=none"
    assert imap_reader._from_is_authenticated(ar, "dr@example.com") is True


def test_check_trigger_reports_authenticated_senders(monkeypatch):
    """行為:結果要分成「命中的寄件人」與「其中通過驗證的」兩份清單。"""
    hdr = (b"Subject: TRIG\r\n"
           b"From: Dr <dr@example.com>\r\n"
           b"Authentication-Results: mx.google.com; dmarc=pass\r\n")

    class _FakeIMAP:
        def login(self, u, p):
            return ("OK", [])

        def select(self, box):
            return ("OK", [])

        def uid(self, command, *args):
            return getattr(self, command.lower())(*args)

        def search(self, charset, *criteria):
            return ("OK", [b"7"])

        def fetch(self, uid, parts):
            return ("OK", [(b"x", hdr), b")"])

        def store(self, ids, op, flags):
            return ("OK", [])

    monkeypatch.setattr(imap_reader.imaplib, "IMAP4_SSL",
                        lambda *a, **k: _FakeIMAP())
    monkeypatch.setattr(
        imap_reader, "_load_imap_settings",
        lambda: {"host": "h", "port": 993, "username": "u", "password": "p"})

    r = imap_reader.check_trigger("TRIG")
    assert r["matched_senders"] == ["dr@example.com"]
    assert r["authenticated_senders"] == ["dr@example.com"]


def test_forged_from_is_matched_but_not_authenticated(monkeypatch):
    """★P1-05 核心★ 偽造 From(驗證失敗)仍會命中主旨,但不可進 authenticated 清單。

    呼叫端要做可信授權時看 authenticated_senders,而不是只比對可偽造的 From。
    """
    hdr = (b"Subject: TRIG\r\n"
           b"From: Dr <dr@example.com>\r\n"
           b"Authentication-Results: mx.google.com; spf=fail; dkim=none; "
           b"dmarc=fail\r\n")

    class _FakeIMAP:
        def login(self, u, p):
            return ("OK", [])

        def select(self, box):
            return ("OK", [])

        def uid(self, command, *args):
            return getattr(self, command.lower())(*args)

        def search(self, charset, *criteria):
            return ("OK", [b"7"])

        def fetch(self, uid, parts):
            return ("OK", [(b"x", hdr), b")"])

        def store(self, ids, op, flags):
            return ("OK", [])

    monkeypatch.setattr(imap_reader.imaplib, "IMAP4_SSL",
                        lambda *a, **k: _FakeIMAP())
    monkeypatch.setattr(
        imap_reader, "_load_imap_settings",
        lambda: {"host": "h", "port": 993, "username": "u", "password": "p"})

    r = imap_reader.check_trigger("TRIG")
    assert r["matched_senders"] == ["dr@example.com"]
    assert r["authenticated_senders"] == [], \
        "★驗證失敗的偽造寄件人被當成已驗證★"


# ── P2-02:必須用 UID API(序號會因其他 client EXPUNGE 而位移)────────────────
def test_uses_uid_api_not_sequence_numbers():
    import inspect
    src = inspect.getsource(imap_reader.check_trigger)
    for bad in ("conn.search(", "conn.fetch(", "conn.store("):
        assert bad not in src, (
            f"★仍在用序號 API {bad}★ 其他 client EXPUNGE 後序號會位移 → "
            "可能讀錯信、甚至把另一封標成已讀")
    # 呼叫可能跨行寫,故用 regex 容許中間的空白/換行
    import re
    for cmd in ("search", "fetch", "store"):
        assert re.search(r'conn\.uid\(\s*"' + cmd + '"', src), \
            f'缺少 conn.uid("{cmd}", ...)'
