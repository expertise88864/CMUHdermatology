# -*- coding: utf-8 -*-
"""[R3-P2-03] 只試第一個 A 記錄 = 沒有備援。

`IPV4_ONLY_HOSTS` 的 DNS 結果原本被砍到★只剩 1 個 IP★ —— 當初是為了避免
「N 個 IP × 逾時」累積成 21-42 秒(CDN 常回 5-10 個)。但代價是:★第一個 A
不通、其它還活著時,整台被判成不通★(院方主站與掛號系統都是多 A 的)。

兩者要一起顧 → 取★有上限的多個★:最壞 2×逾時(約 4 秒),仍遠低於原本的
21-42 秒,而備援回來了。
"""
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main as m  # noqa: E402

HOST = "appointment.cmuh.org.tw"        # 在 IPV4_ONLY_HOSTS 裡


def _entry(ip):
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))


class TestTheResolverKeepsAFallback:
    def test_more_than_one_address_survives(self, monkeypatch):
        """★核心★:不可以只回一個 —— 那等於沒有備援。"""
        monkeypatch.setattr(
            m, "_orig_getaddrinfo",
            lambda *a, **k: [_entry("1.1.1.1"), _entry("2.2.2.2"),
                             _entry("3.3.3.3")])
        got = m._ipv4_first_only_getaddrinfo(HOST, 443)
        assert len(got) >= 2, f"★只剩一個 IP,沒有備援★:{got}"

    def test_it_is_still_bounded(self, monkeypatch):
        """★對照組★:也不可以全部回 —— CDN 回 5-10 個正是當初的病灶
        (N×逾時累積成 21-42 秒)。"""
        monkeypatch.setattr(
            m, "_orig_getaddrinfo",
            lambda *a, **k: [_entry(f"9.9.9.{i}") for i in range(10)])
        got = m._ipv4_first_only_getaddrinfo(HOST, 443)
        assert len(got) == m.IPV4_MAX_ADDRS, got

    def test_a_single_address_is_passed_through(self, monkeypatch):
        monkeypatch.setattr(m, "_orig_getaddrinfo",
                            lambda *a, **k: [_entry("1.1.1.1")])
        assert len(m._ipv4_first_only_getaddrinfo(HOST, 443)) == 1

    def test_an_empty_result_stays_empty(self, monkeypatch):
        """★空的不可以變成別的東西★(呼叫端靠空清單判「解析不到」)。"""
        monkeypatch.setattr(m, "_orig_getaddrinfo", lambda *a, **k: [])
        assert m._ipv4_first_only_getaddrinfo(HOST, 443) == []

    def test_other_hosts_are_untouched(self, monkeypatch):
        """不在清單裡的 host 完全不動(那是別人的網路,不該被我們限制)。"""
        all_addrs = [_entry(f"8.8.8.{i}") for i in range(6)]
        monkeypatch.setattr(m, "_orig_getaddrinfo", lambda *a, **k: all_addrs)
        assert m._ipv4_first_only_getaddrinfo("example.com", 443) == all_addrs

    def test_the_bound_is_small(self):
        """上限要小 —— 它同時是「最壞等多久」的乘數。"""
        assert 2 <= m.IPV4_MAX_ADDRS <= 3


class TestTheConnectorFallsOver:
    """連線那一側也要真的試第二個 —— 不然 DNS 回兩個也沒用。"""

    def _fake_socket(self, monkeypatch, fail_ips):
        tried = []

        class _S:
            def __init__(self, *_a):
                pass

            def settimeout(self, _t):
                pass

            def bind(self, _a):
                pass

            def connect(self, sa):
                tried.append(sa[0])
                if sa[0] in fail_ips:
                    raise OSError(f"refused {sa[0]}")

            def close(self):
                pass
        monkeypatch.setattr(m._socket, "socket", lambda *a, **k: _S())
        return tried

    def test_it_tries_the_second_when_the_first_fails(self, monkeypatch):
        monkeypatch.setattr(
            m._socket, "getaddrinfo",
            lambda *a, **k: [_entry("1.1.1.1"), _entry("2.2.2.2")])
        tried = self._fake_socket(monkeypatch, {"1.1.1.1"})
        m._create_ipv4_connection((HOST, 443))
        assert tried == ["1.1.1.1", "2.2.2.2"], f"★沒有備援★:{tried}"

    def test_it_stops_at_the_bound(self, monkeypatch):
        """全部不通時也不可以一直試下去(那正是 21-42 秒的來源)。"""
        monkeypatch.setattr(
            m._socket, "getaddrinfo",
            lambda *a, **k: [_entry(f"9.9.9.{i}") for i in range(10)])
        tried = self._fake_socket(monkeypatch, {f"9.9.9.{i}"
                                                for i in range(10)})
        with pytest.raises(OSError):
            m._create_ipv4_connection((HOST, 443))
        assert len(tried) == m.IPV4_MAX_ADDRS, tried

    def test_the_first_one_still_wins_when_it_works(self, monkeypatch):
        """★對照組★:第一個通就不要再試第二個(不可以每次都多連一次)。"""
        monkeypatch.setattr(
            m._socket, "getaddrinfo",
            lambda *a, **k: [_entry("1.1.1.1"), _entry("2.2.2.2")])
        tried = self._fake_socket(monkeypatch, set())
        m._create_ipv4_connection((HOST, 443))
        assert tried == ["1.1.1.1"]

    def test_it_raises_the_last_error_not_a_generic_one(self, monkeypatch):
        """失敗要帶著★真正的原因★出去,不然使用者看到的是「空清單」這種
        對不上現場的訊息。"""
        monkeypatch.setattr(m._socket, "getaddrinfo",
                            lambda *a, **k: [_entry("1.1.1.1")])
        self._fake_socket(monkeypatch, {"1.1.1.1"})
        with pytest.raises(OSError) as e:
            m._create_ipv4_connection((HOST, 443))
        assert "refused" in str(e.value)

    def test_an_empty_resolution_still_raises(self, monkeypatch):
        monkeypatch.setattr(m._socket, "getaddrinfo", lambda *a, **k: [])
        with pytest.raises(OSError):
            m._create_ipv4_connection((HOST, 443))
