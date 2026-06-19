# -*- coding: utf-8 -*-
"""門診動態邊緣常駐條(AppBar)純邏輯單元測試(不建立 tk 視窗、不碰 Win32)。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.clinic_appbar import appbar_segment_view  # noqa: E402
from cmuh_common.floating_clinic import RoomStatus  # noqa: E402


def test_appbar_segment_view_open():
    """看診中:open=True、燈號=看診號、醫師/候診帶出。"""
    v = appbar_segment_view(RoomStatus(room="101", slot="早上", doctor="吳醫師",
                                       light="32", waiting=5, fetched=True))
    assert v["label"] == "101 · 早上"
    assert v["doctor"] == "吳醫師"
    assert v["light"] == "32"
    assert v["waiting"] == "5"
    assert v["open"] is True
    assert v["error"] is False


def test_appbar_segment_view_stopped_and_closed():
    """未開診/關診:open=False、燈號轉成對應字、不應誤判為看診中。"""
    stop = appbar_segment_view(RoomStatus(room="103", stopped=True, fetched=True))
    assert stop["open"] is False
    assert stop["light"] == "未開診"
    closed = appbar_segment_view(RoomStatus(room="102", closed=True, fetched=True))
    assert closed["open"] is False
    assert closed["light"] == "關診"


def test_appbar_segment_view_error_offline():
    """連線錯誤:error=True、燈號顯示「離線」。"""
    v = appbar_segment_view(RoomStatus(room="104", error=True, fetched=True))
    assert v["error"] is True
    assert v["light"] == "離線"
    assert v["open"] is False


def test_appbar_segment_view_pending_blank():
    """還沒輪到(pending):有時段標題、燈號/醫師/候診為中性「—」。"""
    v = appbar_segment_view(RoomStatus(room="101", slot="下午", slot_tc="2"))
    assert v["label"] == "101 · 下午"
    assert v["light"] == "—"
    assert v["doctor"] == "—"
    assert v["waiting"] == "—"
    assert v["open"] is False
