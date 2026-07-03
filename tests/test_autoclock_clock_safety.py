# -*- coding: utf-8 -*-
"""打卡安全回歸測試(W3/W4 2026-07-03)。

W4:讀刷卡表失敗須與「當日無紀錄」區分(read_ok),失敗時不可被當成無紀錄而重複打卡。
W3:點擊執行後須重讀刷卡表確認紀錄寫入才標記完成(_verify_clock_recorded),
    確認不到不標記(交 re-fire 重讀),讀取失敗一律當未確認、不誤判成功。
"""
import os
import sys
from datetime import time as dt_time
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import autoclock as ac  # noqa: E402
from selenium.common.exceptions import WebDriverException  # noqa: E402


class _FakeWait:
    def until(self, cond):
        return None


class _FakeDriver:
    def __init__(self, script_result, sys_time_text="115年7月3日"):
        self._script = script_result
        self._sys_time = sys_time_text

    def find_element(self, by, val):
        m = Mock()
        m.text = self._sys_time
        return m

    def execute_script(self, js, *args):
        if callable(self._script):
            return self._script()
        return self._script


def _loc(key):
    return ("id", key)


# ─── W4:get_current_swipe_info 的 read_ok ────────────────────────────────

def test_swipe_read_ok_true_when_table_empty():
    """execute_script 回空 list → read_ok=True(確定當日尚無紀錄,可放心打卡)。"""
    _sd, swipes, _last, read_ok = ac.get_current_swipe_info(
        _FakeDriver([]), _FakeWait(), _loc)
    assert read_ok is True
    assert swipes == []


def test_swipe_read_ok_false_on_exception():
    """execute_script 拋例外 → read_ok=False(讀取失敗,絕不可當無紀錄)。"""
    def boom():
        raise WebDriverException("js failed")
    _sd, _swipes, _last, read_ok = ac.get_current_swipe_info(
        _FakeDriver(boom), _FakeWait(), _loc)
    assert read_ok is False


def test_swipe_read_ok_false_on_non_list():
    """execute_script 回 None(非 list)→ read_ok=False(視為讀取異常)。"""
    _sd, _swipes, _last, read_ok = ac.get_current_swipe_info(
        _FakeDriver(None), _FakeWait(), _loc)
    assert read_ok is False


def test_swipe_read_ok_true_with_rows():
    """execute_script 成功回非空 list → read_ok=True(有讀到表;逐列解析為既有邏輯)。"""
    rows = [["115/07/03", "0731", "上班"]]
    _sd, swipes, _last, read_ok = ac.get_current_swipe_info(
        _FakeDriver(rows), _FakeWait(), _loc)
    assert read_ok is True
    assert isinstance(swipes, list)


# ─── W3:_verify_clock_recorded ───────────────────────────────────────────

def test_verify_true_when_record_appears(monkeypatch):
    """重讀到 read_ok + 區間內符合紀錄 → True(可標記完成)。"""
    monkeypatch.setattr(ac, "get_current_swipe_info",
                        lambda d, w, g: (None, [("0731", "上班")], None, True))
    assert ac._verify_clock_recorded(
        object(), _loc, "上班", dt_time(7, 31), dt_time(8, 0), "u1",
        timeout_sec=0.0, poll_sec=0.0) is True


def test_verify_false_on_timeout_when_no_record(monkeypatch):
    """重讀成功但一直沒有符合紀錄 → 逾時 False(不標記,交 re-fire)。"""
    monkeypatch.setattr(ac, "get_current_swipe_info",
                        lambda d, w, g: (None, [], None, True))
    monkeypatch.setattr(ac.time_module, "sleep", lambda s: None)
    assert ac._verify_clock_recorded(
        object(), _loc, "上班", dt_time(7, 31), dt_time(8, 0), "u1",
        timeout_sec=0.0, poll_sec=0.0) is False


def test_verify_read_failure_never_confirms(monkeypatch):
    """即使重讀回來『有資料』,只要 read_ok=False 就一律當未確認(不誤判成功)。"""
    monkeypatch.setattr(ac, "get_current_swipe_info",
                        lambda d, w, g: (None, [("0731", "上班")], None, False))
    monkeypatch.setattr(ac.time_module, "sleep", lambda s: None)
    assert ac._verify_clock_recorded(
        object(), _loc, "上班", dt_time(7, 31), dt_time(8, 0), "u1",
        timeout_sec=0.0, poll_sec=0.0) is False


# ─── W13:帳號設定驗證(不擋啟動,只警告) ─────────────────────────────────

def test_validate_accounts_ok():
    assert ac._validate_accounts(
        [{"username": "a01", "password": "x"}]) == []


def test_validate_accounts_flags_missing_fields():
    w = ac._validate_accounts([{"username": "", "password": ""}])
    assert any("username" in s for s in w)
    assert any("password" in s for s in w)


def test_validate_accounts_flags_duplicate_username():
    w = ac._validate_accounts([
        {"username": "a01", "password": "x"},
        {"username": "a01", "password": "y"}])
    assert any("重複" in s and "a01" in s for s in w)


def test_validate_accounts_flags_non_dict():
    w = ac._validate_accounts(["not-a-dict", {"username": "a", "password": "p"}])
    assert any("不是物件" in s for s in w)


def test_validate_accounts_total_on_non_list():
    """[codex review] 非 list 純量不可拋例外。"""
    assert ac._validate_accounts(None) == []          # None → 空(無設定)
    assert ac._validate_accounts({}) == []            # 空 dict falsy → 空
    assert any("不是清單" in s for s in ac._validate_accounts(123))  # truthy 純量 → 提示


# ─── W12:打卡任務進行中追蹤(供 self-watchdog 偵測卡住任務) ─────────────

def test_active_clock_task_scope_tracks_and_clears():
    assert ac._active_clock_task_age() == (None, 0.0)
    with ac._active_clock_task_scope("mon_am_in"):
        label, age = ac._active_clock_task_age()
        assert label == "mon_am_in" and age >= 0.0
    assert ac._active_clock_task_age() == (None, 0.0)   # 離開 scope 清空


def test_active_clock_task_scope_clears_on_exception():
    """任務中途拋例外也要清掉標記(否則 watchdog 誤判永遠卡住)。"""
    with pytest.raises(RuntimeError):
        with ac._active_clock_task_scope("x"):
            raise RuntimeError("boom")
    assert ac._active_clock_task_age()[0] is None
