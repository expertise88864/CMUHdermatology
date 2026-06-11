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
