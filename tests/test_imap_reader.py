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


@pytest.mark.parametrize("bad_port", [True, -1, 0, 65536, "bad"])
def test_load_imap_settings_replaces_invalid_port(monkeypatch, bad_port):
    monkeypatch.setattr(
        imap_reader, "load_credentials",
        lambda: {"imap_port": bad_port, "username": "", "password": ""},
    )

    assert imap_reader._load_imap_settings()["port"] == \
        imap_reader.DEFAULT_IMAP_PORT
