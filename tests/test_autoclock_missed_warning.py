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


# ─── [新功能 2026-06-15] 打卡狀態跨重啟持久化 ───────────────────────────────

import json  # noqa: E402


def _reset_clock_state():
    ac._clock_done.clear()
    ac._missed_warned.clear()


def test_clock_state_round_trip_survives_restart(tmp_path, monkeypatch):
    """啟用持久化 → mark 落盤 → 清空記憶體(模擬重啟) → 載回仍恢復。"""
    monkeypatch.setattr(ac, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    monkeypatch.setattr(ac, "_clock_state_persistence_enabled", True)
    _reset_clock_state()
    try:
        ac._mark_clock_done("mon_am_in", "D15728")
        ac._mark_missed_warned("mon_pm_out")
        assert (tmp_path / "clock_state.json").exists()

        _reset_clock_state()              # 模擬程序重啟:記憶體消失
        ac._load_clock_state()
        assert ac._is_clock_done("mon_am_in", "D15728") is True
        assert ac._was_missed_warned_today("mon_pm_out") is True
        assert ac._is_clock_done("mon_am_in", "OTHER") is False  # 沒存的不誤判
    finally:
        _reset_clock_state()


def test_clock_state_cross_day_ignored(tmp_path, monkeypatch):
    """檔案 date 非今日 → 跨日舊狀態整批忽略(避免昨天打卡擋掉今天)。"""
    monkeypatch.setattr(ac, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    _reset_clock_state()
    try:
        (tmp_path / "clock_state.json").write_text(json.dumps({
            "date": "2020-01-01",
            "clock_done": [["mon_am_in", "D15728"]],
            "missed_warned": ["mon_am_in"],
        }), encoding="utf-8")
        ac._load_clock_state()
        assert ac._is_clock_done("mon_am_in", "D15728") is False
        assert ac._was_missed_warned_today("mon_am_in") is False
    finally:
        _reset_clock_state()


def test_clock_state_not_written_when_disabled(tmp_path, monkeypatch):
    """未啟用持久化(短命/GUI 實例) → 只記憶體、不寫檔。"""
    monkeypatch.setattr(ac, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    monkeypatch.setattr(ac, "_clock_state_persistence_enabled", False)
    _reset_clock_state()
    try:
        ac._mark_clock_done("mon_am_in", "D15728")
        assert not (tmp_path / "clock_state.json").exists()
        assert ac._is_clock_done("mon_am_in", "D15728") is True
    finally:
        _reset_clock_state()


def test_clock_state_load_survives_corrupt_file(tmp_path, monkeypatch):
    """壞檔不可 raise(fail-open 降級純記憶體)。"""
    monkeypatch.setattr(ac, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    _reset_clock_state()
    try:
        (tmp_path / "clock_state.json").write_text("{garbage", encoding="utf-8")
        ac._load_clock_state()  # 不可 raise
        assert ac._is_clock_done("mon_am_in", "D15728") is False
    finally:
        _reset_clock_state()


def test_clock_state_load_survives_malformed_fields(tmp_path, monkeypatch):
    """[codex review 2026-06-15] 合法 JSON 但欄位畸形(null/型別錯)不可拋例外
    殺掉 scheduler 啟動(載入在註冊排程之前)。"""
    monkeypatch.setattr(ac, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    today = ac.date.today().isoformat()
    _reset_clock_state()
    try:
        # clock_done=null、missed_warned 是 dict(非 list) → 都不可 raise
        (tmp_path / "clock_state.json").write_text(json.dumps({
            "date": today, "clock_done": None, "missed_warned": {"x": 1},
        }), encoding="utf-8")
        ac._load_clock_state()  # 不可 raise
        assert ac._is_clock_done("mon_am_in", "D15728") is False
        # 畸形項中的合法部分:list 內混入壞元素也只跳過壞的
        (tmp_path / "clock_state.json").write_text(json.dumps({
            "date": today,
            "clock_done": [["mon_am_in", "D1"], "bad", ["only_one"], 123],
            "missed_warned": ["mon_am_in", 999, None],
        }), encoding="utf-8")
        _reset_clock_state()
        ac._load_clock_state()
        assert ac._is_clock_done("mon_am_in", "D1") is True   # 合法項載入
        assert ac._was_missed_warned_today("mon_am_in") is True
    finally:
        _reset_clock_state()
