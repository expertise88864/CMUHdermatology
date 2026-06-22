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
    # [2026-06-19 user] 已關診 → 一律隱藏,即使有醫師(早診拖班看完就消失,不佔位)
    assert should_show_room(
        RoomStatus(room="101", doctor="王醫師", light="58", closed=True, fetched=True)) is False
    # 還沒查到資料 → 先顯示(不要急著隱藏)
    assert should_show_room(RoomStatus(room="103", fetched=False)) is True


def test_should_show_room_hides_errored_no_clinic_room():
    """[2026-06-22 user] 今天沒這個診、reg64 連線逾時/錯誤(error=True 但沒醫師)→ 隱藏。
    主程式 update_single_clinic_ui_error 會把這種診以 error 旗標餵進浮動視窗,
    不再卡成 fetched=False 的 pending 空白卡(原 102 沒診卻一直顯示的 bug)。"""
    # 102 今天沒診、連線逾時:error=True、無醫師、燈號佔位 → 隱藏
    assert should_show_room(
        RoomStatus(room="102", error=True, light="--", fetched=True)) is False
    # 但若(暫時)連線異常卻仍記得醫師姓名 → 仍顯示(視為該診存在,只是這輪連線異常)
    assert should_show_room(
        RoomStatus(room="102", error=True, doctor="王醫師", fetched=True)) is True


def test_floating_window_autosizes_before_first_show():
    """[2026-06-22 user] 浮動視窗開啟時先算好高度再現身,避免一開始字被壓縮:
      - __init__ 先 withdraw 兩窗,第一次 _render 後才 deiconify(_ensure_shown);
        另有 fallback 計時器保險,避免任何意外讓視窗卡在 withdraw 變隱形窗。
      - _render 先算高度 + _reposition,再重建卡片(卡片 pack 進已正確高度的窗,不被擠壓)。
    GUI 行為以原始碼守門(避免被改回先建卡片才量高度的舊流程)。"""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "cmuh_common" / "floating_clinic.py"
    code = src.read_text(encoding="utf-8")
    assert "def _ensure_shown(" in code
    assert ".withdraw()" in code and ".deiconify()" in code
    assert "_first_shown" in code
    # fallback:withdraw 後排一個 after 計時器叫 _ensure_shown(防呆,不會變隱形窗)
    assert "after(400, self._ensure_shown)" in code
    # _render 內:先 _reposition(算好高度)才 _build_card(先尺寸後卡片),結尾呼叫 _ensure_shown
    render_body = code.split("def _render(", 1)[1].split("\n    def ", 1)[0]
    assert render_body.index("self._reposition(content_h=ch)") < render_body.index("self._build_card(")
    assert "self._ensure_shown()" in render_body


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


def test_floating_window_has_current_time_row():
    """[2026-06-19 user] 浮動視窗在標題列下、卡片上要有「目前時間」列:日期(含星期)+
    時:分:秒,自走更新,且高度計算要納入該列(不會被裁)。GUI 元件以原始碼守門避免被改掉。"""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "cmuh_common" / "floating_clinic.py"
    code = src.read_text(encoding="utf-8")
    assert "_TIME_ROW_H" in code
    assert "def _update_time(" in code
    assert 'strftime("%H:%M:%S")' in code   # 時:分:秒(含秒)
    assert 'strftime("%Y/%m/%d")' in code   # 日期
    assert "週" in code                       # 星期
    # 時間列固定在 body 最上方(side="top"),會排在 _render 重建的卡片之上
    assert "self._time_frame" in code
    assert "self._time_lbl" in code and "self._date_lbl" in code
    # 時間列高度以容器 winfo_reqheight 量測(含 DPI/字型縮放),避免固定常數在高 DPI 下裁切卡片
    assert "winfo_reqheight" in code.split("def _time_row_height", 1)[1].split("\n    def ", 1)[0]
    # 高度計算有納入時間列(用量測的 _time_row_height)
    assert "_time_row_height()" in code.split("def _content_height", 1)[1].split("\n    def ", 1)[0]
    # 銷毀時取消自走計時器,避免孤兒 after 回呼
    assert "after_cancel(self._time_after_id)" in code
