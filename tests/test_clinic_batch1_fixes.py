# -*- coding: utf-8 -*-
"""診間批次1 回歸（§4C：CL-01 / FC-01 / FC-02，2026-07-11）。

  CL-01 clinic_int_count 對 float nan/inf 直接 int() → ValueError/OverflowError(fail-open 破口)。
  FC-01 浮窗燈號解析失敗預設 "0",room_card_view 原樣顯示成 32pt 大字「0」誤導醫護。
  FC-02 _persisted_session_overrun_state 不驗日期 → 跨午夜把昨日早診當「還在拖班」。
"""
import os
import sys
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.reg64_utils import clinic_int_count  # noqa: E402
from cmuh_common.floating_clinic import RoomStatus, room_card_view  # noqa: E402
import main  # noqa: E402


# ══ CL-01：float nan/inf 回保守 default,不可拋例外 ═══════════════════════════════
def test_cl01_clinic_int_count_nonfinite_returns_default():
    assert clinic_int_count(float("nan")) == 0
    assert clinic_int_count(float("inf")) == 0
    assert clinic_int_count(float("-inf")) == 0
    assert clinic_int_count(float("nan"), default=7) == 7


def test_cl01_clinic_int_count_finite_floats_unchanged():
    assert clinic_int_count(3.0) == 3           # 整數值 float 仍照常轉
    assert clinic_int_count(3.5) == 0           # 帶小數 → default(維持原行為)
    assert clinic_int_count(3.5, default=-1) == -1
    assert clinic_int_count(5) == 5             # int 原樣


# ══ FC-01：燈號 "0"/"--"/空 收斂成佔位「—」;真燈號/「休」不動 ═══════════════════════
def test_fc01_room_card_view_normalizes_zero_and_dashes():
    assert room_card_view(RoomStatus(room="A", light="0"))["light"] == "—"
    assert room_card_view(RoomStatus(room="A", light="--"))["light"] == "—"
    assert room_card_view(RoomStatus(room="A", light=""))["light"] == "—"


def test_fc01_room_card_view_keeps_real_light_and_rest():
    assert room_card_view(RoomStatus(room="A", light="5"))["light"] == "5"
    assert room_card_view(RoomStatus(room="A", light="12"))["light"] == "12"
    # "休" 不是本次要正規化的對象(與主 UI 一致,僅收斂 "0"/"--")→ 維持原樣
    assert room_card_view(RoomStatus(room="A", light="休"))["light"] == "休"


# ══ FC-02：只信「今日」的持久化早診狀態,跨午夜昨日殘留回 (False, False)═══════════════
def _fake_app(cache, today):
    return types.SimpleNamespace(
        _clinic_dynamic_state_cache=cache,
        _clinic_dynamic_state_lock=threading.Lock(),
        _clinic_dynamic_state_key=lambda room, s: f"{room}/{s}",
        _clinic_dynamic_today_str=lambda: today,
    )


def test_fc02_today_state_reports_activity():
    cache = {"R1/1": {"date": "2026/07/11", "had_any_activity": True, "is_ended": False}}
    app = _fake_app(cache, "2026/07/11")
    had, closed = main.AutomationApp._persisted_session_overrun_state(app, "R1", 1)
    assert (had, closed) == (True, False)


def test_fc02_stale_yesterday_state_ignored():
    # 跨午夜:昨日早診 had_activity 但沒落 is_ended,今日不可誤判為仍在拖班
    cache = {"R1/1": {"date": "2026/07/10", "had_any_activity": True, "is_ended": False}}
    app = _fake_app(cache, "2026/07/11")
    assert main.AutomationApp._persisted_session_overrun_state(app, "R1", 1) == (False, False)


def test_fc02_missing_or_nondict_state_returns_false():
    app = _fake_app({}, "2026/07/11")
    assert main.AutomationApp._persisted_session_overrun_state(app, "R1", 1) == (False, False)
    app2 = _fake_app({"R1/1": "not-a-dict"}, "2026/07/11")
    assert main.AutomationApp._persisted_session_overrun_state(app2, "R1", 1) == (False, False)
