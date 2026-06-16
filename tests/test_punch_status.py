# -*- coding: utf-8 -*-
"""cmuh_common.punch_status 純函式測試(時間窗 / 排班 / 三態分類 / 帳號評估)。

只測無 selenium 相依的純邏輯;實際讀打卡 portal(read_today_swipes /
query_accounts_today)依賴 Win32+內網+Chrome,無法在 CI 純邏輯測。
"""
import os
import sys
from datetime import datetime, time as dt_time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import punch_status as ps  # noqa: E402

AM = (dt_time(7, 30), dt_time(12, 30))
PM = (dt_time(17, 0), dt_time(17, 30))
MON = datetime(2026, 6, 15)   # 週一
SUN = datetime(2026, 6, 14)   # 週日


def test_weekday_prefix():
    assert ps.weekday_prefix(MON) == "mon"
    assert ps.weekday_prefix(SUN) == "sun"


def test_hhmm_to_time():
    assert ps._hhmm_to_time("0815") == dt_time(8, 15)
    assert ps._hhmm_to_time("815") == dt_time(8, 15)
    assert ps._hhmm_to_time("bad") is None


def test_swipe_in_window_returns_time_or_none():
    swipes = [("0815", "上班"), ("1705", "下班"), ("0700", "上班")]
    # 上班 0815 在 07:30-12:30 內
    assert ps.swipe_in_window(swipes, "上班", *AM) == "08:15"
    # 下班 1705 在 17:00-17:30 內
    assert ps.swipe_in_window(swipes, "下班", *PM) == "17:05"
    # 沒有「下班」落在 AM 窗
    assert ps.swipe_in_window(swipes, "下班", *AM) is None


def test_swipe_in_window_noon_clock_in_counts():
    """中午上班(11:00)也屬「上班」型別,落在 07:30-12:30 → 算成功。"""
    swipes = [("1100", "上班")]
    assert ps.swipe_in_window(swipes, "上班", *AM) == "11:00"


def test_midday_clock_in_1231_needs_window_to_1240():
    """打卡系統中午 12:31 才打卡;上班窗必須到 12:40 才抓得到(只到 12:30 會漏)。"""
    swipes = [("1231", "上班")]
    assert ps.swipe_in_window(swipes, "上班", dt_time(7, 30), dt_time(12, 30)) is None
    assert ps.swipe_in_window(swipes, "上班", dt_time(7, 30), dt_time(12, 40)) == "12:31"


def test_swipe_outside_window_is_none():
    swipes = [("0700", "上班"), ("1740", "下班")]  # 都在窗外
    assert ps.swipe_in_window(swipes, "上班", *AM) is None
    assert ps.swipe_in_window(swipes, "下班", *PM) is None


def test_scheduled_today():
    sched = {"mon_am_in": True, "mon_midday_in": False, "mon_pm_out": False}
    assert ps.scheduled_today(sched, ("am_in", "midday_in"), MON) is True
    assert ps.scheduled_today(sched, ("pm_out",), MON) is False
    # 週日(無 sun_* key)→ False
    assert ps.scheduled_today(sched, ("am_in",), SUN) is False
    assert ps.scheduled_today({}, ("am_in",), MON) is False
    assert ps.scheduled_today(None, ("am_in",), MON) is False


def test_classify_three_states():
    assert ps.classify(scheduled=True, detected=True) == ps.PUNCH_OK
    assert ps.classify(scheduled=True, detected=False) == ps.PUNCH_FAIL
    assert ps.classify(scheduled=False, detected=True) == ps.PUNCH_OFF
    assert ps.classify(scheduled=False, detected=False) == ps.PUNCH_OFF


def test_evaluate_account_scheduled_and_punched():
    sched = {"mon_am_in": True, "mon_pm_out": True}
    swipes = [("0815", "上班"), ("1705", "下班")]
    ev = ps.evaluate_account(sched, swipes, AM, PM, MON)
    assert ev == {"on": "ok", "on_time": "08:15", "off": "ok", "off_time": "17:05"}


def test_evaluate_account_scheduled_but_missing_is_fail():
    sched = {"mon_am_in": True, "mon_pm_out": True}
    ev = ps.evaluate_account(sched, [], AM, PM, MON)
    assert ev["on"] == "fail" and ev["off"] == "fail"
    assert ev["on_time"] is None and ev["off_time"] is None


def test_evaluate_account_off_duty_is_not_fail():
    """沒排班 → off(不算失敗),即使查無紀錄。"""
    sched = {"mon_am_in": False, "mon_midday_in": False, "mon_pm_out": False}
    ev = ps.evaluate_account(sched, [], AM, PM, MON)
    assert ev["on"] == "off" and ev["off"] == "off"


def test_evaluate_account_midday_clock_in_satisfies_morning():
    """只排中午上班(midday_in)、中午 11:50 打卡上班 → 上班 ok。"""
    sched = {"mon_am_in": False, "mon_midday_in": True, "mon_pm_out": False}
    ev = ps.evaluate_account(sched, [("1150", "上班")], AM, PM, MON)
    assert ev["on"] == "ok" and ev["on_time"] == "11:50"
    assert ev["off"] == "off"
