# -*- coding: utf-8 -*-
"""浮動門診動態小視窗 純邏輯單元測試(不建立 tk 視窗)。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.floating_clinic import (  # noqa: E402
    RoomStatus,
    clamp_opacity,
    parse_geometry_size,
    room_card_view,
    slot_color,
)


def test_clamp_opacity_bounds_and_default():
    assert clamp_opacity(0.85) == 0.85
    assert clamp_opacity(0.1) == 0.25     # 夾到下限
    assert clamp_opacity(2.0) == 0.95     # 夾到上限
    assert clamp_opacity("abc") == 0.85   # 壞值 → 預設
    assert clamp_opacity(None) == 0.85


def test_room_card_view_open():
    v = room_card_view(RoomStatus(room="101", slot="早上", doctor="吳醫師",
                                   light="32", waiting=5))
    assert v["title"] == "101 · 早上"
    assert v["doctor"] == "吳醫師"
    assert v["light"] == "32"
    assert v["waiting"] == "5"
    assert v["state"] == "open"


def test_room_card_view_waiting_zero_vs_none():
    assert room_card_view(RoomStatus(room="101", waiting=0))["waiting"] == "0"
    assert room_card_view(RoomStatus(room="101", waiting=None))["waiting"] == "—"


def test_room_card_view_closed_stopped_error():
    assert room_card_view(RoomStatus(room="102", closed=True))["state"] == "closed"
    assert room_card_view(RoomStatus(room="102", closed=True))["light"] == "關診"
    assert room_card_view(RoomStatus(room="103", stopped=True))["state"] == "stopped"
    assert room_card_view(RoomStatus(room="103", stopped=True))["light"] == "未開診"
    assert room_card_view(RoomStatus(room="104", error=True))["state"] == "error"
    # [2026-06-19] 錯誤改顯示「離線」(比 "?" 清楚);無資料則由主程式餵 light="" → 顯示 —
    assert room_card_view(RoomStatus(room="104", error=True))["light"] == "離線"


def test_room_card_view_blanks():
    v = room_card_view(RoomStatus(room="101"))
    assert v["doctor"] == "—"
    assert v["light"] == "—"      # light 空字串 → —
    assert v["title"] == "101"    # 無時段 → 只有診間號


def test_slot_color():
    # [2026-06-19] 深色主題:時段色改亮色(在深底上才顯眼)
    assert slot_color("早上") == "#34d399"
    assert slot_color("上午") == "#34d399"
    assert slot_color("下午") == "#38bdf8"
    assert slot_color("晚上") == "#818cf8"
    assert slot_color("") != ""   # 未知時段 → 有預設色,不空


def test_parse_geometry_size():
    assert parse_geometry_size("250x320+100+50") == (250, 320)
    assert parse_geometry_size("180x140") == (180, 140)
    assert parse_geometry_size("bad") is None
    assert parse_geometry_size("") is None
    assert parse_geometry_size("0x0+1+1") is None   # 非正 → None
