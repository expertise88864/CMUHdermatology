# -*- coding: utf-8 -*-
"""浮動門診動態小視窗 — 半透明、永遠置頂(不搶焦點)、可調大小的 Toplevel。

每個診間一張小卡:診間號 · 時段 · 醫師 · 【燈號(放大、最顯眼)】· 待診人數。
資料由主程式餵入(沿用既有 reg64 60–90 秒輪詢的快取,本視窗不自行查詢、不增加醫院負載)。

設計:純邏輯(RoomStatus / clamp_opacity / room_card_view / parse_geometry_size)抽出來
可單元測試;ClinicFloatingWindow(tkinter Toplevel)為 Windows/GUI 專屬,延後建立 widget。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

# ── 視覺常數(與主程式信件/總覽色系一致) ──────────────────────────────
_OPACITY_MIN = 0.25
_OPACITY_MAX = 0.95
_OPACITY_DEFAULT = 0.85

# 時段顏色(早上綠 / 下午藍 / 晚上深藍),與 reg64_slot_label_color 同系
_SLOT_COLOR = {"早上": "#2E7D32", "上午": "#2E7D32",
               "下午": "#1565C0", "晚上": "#0D47A1"}
_INK = "#1a2230"
_SUB = "#5b6470"
_LIGHT_FG = "#0f766e"      # 燈號數字(主強調色)
_CLOSED_FG = "#9aa0a8"
_ERR_FG = "#c0392b"
_CARD_BG = "#ffffff"
_WIN_BG = "#eef1f4"


@dataclass
class RoomStatus:
    """主程式餵入的單診間狀態(都是已算好的顯示值)。"""
    room: str                      # 診間號,如 "101"
    slot: str = ""                 # 時段:早上/下午/晚上
    doctor: str = ""               # 醫師姓名
    light: str = ""                # 目前燈號(看診號)
    waiting: Optional[int] = None  # 待診人數
    closed: bool = False           # 已關診
    stopped: bool = False          # 未開診
    error: bool = False            # 查詢失敗 / 無資料


def clamp_opacity(value) -> float:
    """把透明度夾在 [0.25, 0.95];壞值回預設 0.85。純函式。"""
    try:
        a = float(value)
    except (TypeError, ValueError):
        return _OPACITY_DEFAULT
    return min(_OPACITY_MAX, max(_OPACITY_MIN, a))


def slot_color(slot: str) -> str:
    return _SLOT_COLOR.get((slot or "").strip(), _SUB)


def room_card_view(s: RoomStatus) -> dict:
    """RoomStatus → 卡片顯示字串 dict(title/doctor/light/waiting/state)。純函式。

    state: open / closed / stopped / error — 決定燈號區塊的文字與顏色。
    """
    if s.error:
        light, waiting, state = "?", "—", "error"
    elif s.stopped:
        light, waiting, state = "未開診", "—", "stopped"
    elif s.closed:
        light, waiting, state = "關診", "—", "closed"
    else:
        light = str(s.light).strip() or "—"
        waiting = "—" if s.waiting is None else str(s.waiting)
        state = "open"
    title = " · ".join(p for p in (str(s.room).strip(), (s.slot or "").strip()) if p)
    return {
        "title": title or "—",
        "doctor": (s.doctor or "").strip() or "—",
        "light": light,
        "waiting": waiting,
        "state": state,
    }


def parse_geometry_size(geometry: str) -> Optional[tuple]:
    """從 'WxH+X+Y' 取 (w, h);取不到回 None。純函式(給尺寸合理性檢查用)。"""
    try:
        wh = str(geometry).split("+", 1)[0]
        w_str, h_str = wh.lower().split("x", 1)
        w, h = int(w_str), int(h_str)
        if w > 0 and h > 0:
            return w, h
    except (ValueError, AttributeError, IndexError):
        pass
    return None


# ── GUI(Windows / tkinter 專屬) ───────────────────────────────────────
class ClinicFloatingWindow:
    """浮動門診動態視窗。主程式建立一個、用 update_rooms() 餵資料。

    - 保留系統標題列(原生可拉大小/拖曳/關閉/最小化)
    - 永遠置頂(-topmost)但建立/更新時不主動搶焦點(不 focus_force/lift)
    - 半透明(-alpha),可動態調整
    - 關閉(X)時呼叫 on_close(讓主程式把設定關掉並存檔)
    """

    def __init__(self, root, *, opacity: float = _OPACITY_DEFAULT,
                 geometry: str = "", on_close: Optional[Callable] = None,
                 on_geometry_change: Optional[Callable] = None) -> None:
        import tkinter as tk

        self._tk = tk
        self.on_close = on_close
        self.on_geometry_change = on_geometry_change
        self._cards: dict = {}          # room -> dict(widgets)
        self._cards_frame = None

        self.win = tk.Toplevel(root)
        self.win.title("門診動態")
        self.win.configure(bg=_WIN_BG)
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            logging.debug("[浮動門診] -topmost 設定失敗", exc_info=True)
        self.set_opacity(opacity)
        if parse_geometry_size(geometry):
            try:
                self.win.geometry(geometry)
            except Exception:
                logging.debug("[浮動門診] 套用 geometry 失敗", exc_info=True)
        else:
            self.win.geometry("250x320")
        self.win.minsize(170, 130)
        # 關閉鈕 → 交給主程式(關掉設定、存檔),不直接 destroy 以免狀態不同步
        self.win.protocol("WM_DELETE_WINDOW", self._handle_close)

    # ── 對外 API ──────────────────────────────────────────────
    def set_opacity(self, value) -> None:
        try:
            self.win.attributes("-alpha", clamp_opacity(value))
        except Exception:
            logging.debug("[浮動門診] -alpha 設定失敗(改用不透明)", exc_info=True)

    def update_rooms(self, rooms: list) -> None:
        """rooms: list[RoomStatus]。重建卡片(數量少,直接重繪最簡單可靠)。"""
        tk = self._tk
        if self._cards_frame is not None:
            try:
                self._cards_frame.destroy()
            except Exception:
                pass
        frame = tk.Frame(self.win, bg=_WIN_BG)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        self._cards_frame = frame
        for s in rooms:
            self._build_card(frame, s)

    def get_geometry(self) -> str:
        try:
            return self.win.winfo_geometry()
        except Exception:
            return ""

    def lift_to_top(self) -> None:
        """重申置頂(不搶焦點):部分情況 topmost 會被其他視窗壓過。"""
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass

    def exists(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except Exception:
            return False

    def destroy(self) -> None:
        try:
            if self.on_geometry_change:
                g = self.get_geometry()
                if g:
                    self.on_geometry_change(g)
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass

    # ── 內部 ──────────────────────────────────────────────────
    def _handle_close(self) -> None:
        try:
            if self.on_geometry_change:
                g = self.get_geometry()
                if g:
                    self.on_geometry_change(g)
        except Exception:
            pass
        if self.on_close:
            try:
                self.on_close()
                return
            except Exception:
                logging.debug("[浮動門診] on_close 例外", exc_info=True)
        self.destroy()

    def _build_card(self, parent, s: RoomStatus) -> None:
        tk = self._tk
        v = room_card_view(s)
        card = tk.Frame(parent, bg=_CARD_BG, bd=1, relief="solid",
                        highlightthickness=0)
        card.pack(fill="x", pady=3)
        head = tk.Frame(card, bg=_CARD_BG)
        head.pack(fill="x", padx=8, pady=(5, 0))
        tk.Label(head, text=v["title"], bg=_CARD_BG, fg=slot_color(s.slot),
                 font=("Microsoft JhengHei UI", 11, "bold")).pack(side="left")
        tk.Label(head, text=v["doctor"], bg=_CARD_BG, fg=_SUB,
                 font=("Microsoft JhengHei UI", 10)).pack(side="right")
        body = tk.Frame(card, bg=_CARD_BG)
        body.pack(fill="x", padx=8, pady=(0, 6))
        # 燈號:最大、最顯眼
        light_fg = {"open": _LIGHT_FG, "closed": _CLOSED_FG,
                    "stopped": _CLOSED_FG, "error": _ERR_FG}.get(v["state"], _INK)
        big = v["state"] == "open"
        tk.Label(body, text=v["light"], bg=_CARD_BG, fg=light_fg,
                 font=("Microsoft JhengHei UI", 28 if big else 14, "bold")
                 ).pack(side="left")
        tk.Label(body, text=("　待診 " + v["waiting"]) if big else "",
                 bg=_CARD_BG, fg=_SUB,
                 font=("Microsoft JhengHei UI", 11)).pack(side="left", anchor="s",
                                                          pady=(0, 6))
