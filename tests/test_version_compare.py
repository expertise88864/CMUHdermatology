# -*- coding: utf-8 -*-
"""parse_version 測試。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.version import parse_version  # noqa: E402


def test_basic_ordering():
    assert parse_version("2026.5.4.1") > parse_version("2026.5.4.0")
    assert parse_version("2026.5.4.10") > parse_version("2026.5.4.9")


def test_year_boundary():
    """字串排序的經典 bug：'2026.1.1' < '2025.12.16' (字串)，tuple 比較才正確。"""
    assert parse_version("2026.1.1") > parse_version("2025.12.16")


def test_invalid_returns_zero():
    assert parse_version("") == (0,)
    assert parse_version("not.a.version") == (0,)
    assert parse_version(None) == (0,)


def test_equality():
    assert parse_version("2026.5.4.1") == parse_version("2026.5.4.1")


if __name__ == "__main__":
    test_basic_ordering()
    test_year_boundary()
    test_invalid_returns_zero()
    test_equality()
    print("[OK] version_compare tests passed")
