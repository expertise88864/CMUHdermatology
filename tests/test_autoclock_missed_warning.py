# -*- coding: utf-8 -*-
"""[新功能 2026-06-13] 補卡提醒(打卡窗結束仍未確認成功)的判定邏輯測試。

_windows_needing_missed_warning 為純函式:時間/帳號/完成狀態/已提醒狀態全部
由參數注入。判定窗 = check_end+90s < now <= check_end+15min。
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import autoclock as ac  # noqa: E402


# 2026-06-15 是星期一;am_in 驗證窗 = 07:30-08:00
_MON = "2026-06-15"


def _accounts(*specs):
    """specs: (username, {schedule_key: True, ...})"""
    return [{"username": u, "schedule": s} for u, s in specs]


def _call(now_str, accounts, done=(), warned=()):
    done_set = set(done)
    warned_set = set(warned)
    return ac._windows_needing_missed_warning(
        datetime.fromisoformat(now_str), accounts,
        is_done=lambda k, u: (k, u) in done_set,
        already_warned=lambda k: k in warned_set,
    )


def test_warns_for_unfinished_account_after_window_end():
    """窗結束 2 分鐘(90s<2min<=15min)、有排程且未完成 → 提醒。"""
    hits = _call(f"{_MON}T08:02:00",
                 _accounts(("D15728", {"mon_am_in": True})))
    assert hits == [("mon_am_in", ["D15728"])]


def test_no_warning_before_grace_start():
    """窗剛結束 30 秒(<90s) → 還不提醒(避開窗尾確認競態)。"""
    assert _call(f"{_MON}T08:00:30",
                 _accounts(("D15728", {"mon_am_in": True}))) == []


def test_no_warning_after_grace_end():
    """窗結束超過 15 分鐘 → 不再提醒(已無行動價值)。"""
    assert _call(f"{_MON}T08:20:00",
                 _accounts(("D15728", {"mon_am_in": True}))) == []


def test_done_account_not_warned():
    """本窗已確認完成(打卡成功/已有紀錄) → 不提醒。"""
    assert _call(f"{_MON}T08:02:00",
                 _accounts(("D15728", {"mon_am_in": True})),
                 done={("mon_am_in", "D15728")}) == []


def test_already_warned_today_not_repeated():
    """同窗當天已提醒過 → 不重複轟炸。"""
    assert _call(f"{_MON}T08:05:00",
                 _accounts(("D15728", {"mon_am_in": True})),
                 warned={"mon_am_in"}) == []


def test_account_not_scheduled_for_window_ignored():
    """沒排該窗的帳號不提醒(例:只排下午班)。"""
    assert _call(f"{_MON}T08:02:00",
                 _accounts(("D15728", {"mon_pm_out": True}))) == []


def test_sunday_never_warns():
    """週日不打卡(與 get_sched_key 一致) → 不提醒。2026-06-14 是星期日。"""
    assert _call("2026-06-14T08:02:00",
                 _accounts(("D15728", {"sun_am_in": True}))) == []


def test_multiple_accounts_and_mixed_done():
    """同窗多帳號:完成的排除、未完成的列出。"""
    hits = _call(f"{_MON}T08:02:00",
                 _accounts(("A1", {"mon_am_in": True}),
                           ("A2", {"mon_am_in": True}),
                           ("A3", {"mon_am_in": False})),
                 done={("mon_am_in", "A1")})
    assert hits == [("mon_am_in", ["A2"])]


def test_midday_windows_independent():
    """12:32 落在 midday_out(12:00-12:30)的提醒窗,midday_in(12:30-13:00)
    還沒結束 → 只提醒 midday_out。"""
    hits = _call(f"{_MON}T12:32:30",
                 _accounts(("A1", {"mon_midday_out": True,
                                   "mon_midday_in": True})))
    assert hits == [("mon_midday_out", ["A1"])]
