# -*- coding: utf-8 -*-
"""浮動門診動態小視窗 純邏輯單元測試(不建立 tk 視窗)。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.floating_clinic import (  # noqa: E402
    RoomStatus,
    clamp_opacity,
    parse_geometry_pos,
    parse_geometry_size,
    room_card_view,
    room_status_for_current_slot,
    should_show_room,
    slot_color,
)


def test_should_show_room_autohide_rules():
    """[2026-06-19] 自動偵測診間:已查到但沒醫師沒燈號 → 隱藏(該診不存在);
    有醫師(即使未開診)或有燈號 → 顯示;還沒查到資料 → 先顯示。"""
    # 已查到、沒醫師、沒燈號 → 隱藏(例:103 今天沒這個診)
    assert should_show_room(
        RoomStatus(room="103", fetched=True)) is False
    # 實機 103:沒醫師 + 未開診 + 燈號是佔位字 '--' → 仍要隱藏(舊版誤判沒隱藏的 bug)
    assert should_show_room(
        RoomStatus(room="103", stopped=True, light="--", fetched=True)) is False
    assert should_show_room(
        RoomStatus(room="103", closed=True, light="休", fetched=True)) is False
    # 已查到、有醫師但未開診 → 顯示(會顯示未開診)
    assert should_show_room(
        RoomStatus(room="102", doctor="王醫師", stopped=True, fetched=True)) is True
    # 已查到、有燈號(看診中)→ 顯示
    assert should_show_room(
        RoomStatus(room="101", light="32", fetched=True)) is True
    # 還沒查到資料 → 先顯示(不要急著隱藏)
    assert should_show_room(RoomStatus(room="103", fetched=False)) is True


def test_parse_geometry_pos():
    assert parse_geometry_pos("232x300+100+50") == (100, 50)
    assert parse_geometry_pos("232x300+-20+-5") == (-20, -5)
    assert parse_geometry_pos("232x300") is None
    assert parse_geometry_pos("bad") is None


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


def test_room_status_for_current_slot_uses_cache_only_when_session_matches():
    """[2026-06-19 user] 浮動視窗時段永遠依電腦目前時間;只有快取資料正是目前時段才採用。"""
    # 快取是「下午(tc=2)」且目前正是下午 → 直接用該即時資料
    cached_pm = RoomStatus(room="101", slot="下午", doctor="吳醫師", light="15",
                           waiting=3, fetched=True, slot_tc="2")
    got = room_status_for_current_slot(cached_pm, "101", "2", "下午")
    assert got is cached_pm
    assert got.doctor == "吳醫師" and got.light == "15"


def test_room_status_for_current_slot_drops_stale_other_session():
    """快取是別的時段(早上 tc=1)但目前已是下午(tc=2)→ 不可沿用早上舊資料,
    改顯示『下午、待更新』中性狀態(不魚目混珠)。卡片被手動固定成別時段亦同此理。"""
    cached_am = RoomStatus(room="101", slot="早上", doctor="王醫師", light="32",
                           waiting=8, fetched=True, slot_tc="1")
    got = room_status_for_current_slot(cached_am, "101", "2", "下午")
    assert got is not cached_am
    assert got.room == "101"
    assert got.slot == "下午"        # 顯示目前時段
    assert got.slot_tc == "2"
    assert got.doctor == ""          # 不帶入早上的醫師
    assert got.light == ""           # 待更新 → room_card_view 顯示 —
    assert got.fetched is False      # pending → should_show_room 先顯示、不急著隱藏


def test_room_status_for_current_slot_no_cache_yet():
    """還沒輪詢到任何資料 → 顯示目前時段、待更新。"""
    got = room_status_for_current_slot(None, "103", "3", "晚上")
    assert got.room == "103"
    assert got.slot == "晚上"
    assert got.slot_tc == "3"
    assert got.fetched is False
    # 仍應顯示(pending),且卡片標題含目前時段
    assert should_show_room(got) is True
    assert room_card_view(got)["title"] == "103 · 晚上"
