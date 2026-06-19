# -*- coding: utf-8 -*-
"""浮動門診動態小視窗 — 半透明、永遠置頂(不搶焦點)、無邊框、可調大小的 Toplevel。

每個診間一張小卡:診間號 · 時段 · 醫師 · 【燈號(放大、最顯眼)】· 待診人數。
資料由主程式餵入(沿用既有 reg64 60–90 秒輪詢的快取,本視窗不自行查詢、不增加醫院負載)。

[2026-06-19] 改為無系統標題列(overrideredirect)+ 自製細標題列(可拖曳 + 關閉)
+ 右下角縮放把手 + 深色 sleek 卡片,讓整體更美觀、旁邊近乎無邊框。

設計:純邏輯(RoomStatus / clamp_opacity / room_card_view / parse_geometry_size)抽出來
可單元測試;ClinicFloatingWindow(tkinter Toplevel)為 Windows/GUI 專屬,延後建立 widget。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

# ── 視覺常數(深色 sleek 風;半透明浮窗在深色上最好看) ──────────────────
_OPACITY_MIN = 0.25
_OPACITY_MAX = 0.95
_OPACITY_DEFAULT = 0.85

_FONT = "Microsoft JhengHei UI"

# 時段強調色(早上綠 / 下午天藍 / 晚上靛),亮色在深底上很顯眼
_SLOT_COLOR = {"早上": "#34d399", "上午": "#34d399",
               "下午": "#38bdf8", "晚上": "#818cf8"}
_WIN_BG = "#0d1320"        # 視窗底色(深)
_BORDER = "#2b3447"        # 細邊框
_HEADER_BG = "#0a0f1a"     # 標題列(更深)
_HEADER_FG = "#9aa6b8"
_CARD_BG = "#171f2e"       # 卡片底
_INK = "#e9eef6"           # 主要文字(亮)
_SUB = "#7c889b"           # 次要文字(灰)
_LIGHT_OPEN = "#34d399"    # 看診中燈號(綠,最顯眼)
_LIGHT_DIM = "#5b6678"     # 關診/未開診(灰)
_ERR_FG = "#f87171"        # 錯誤/離線(紅)


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
    error: bool = False            # 查詢失敗 / 連線錯誤


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
        light, waiting, state = "離線", "—", "error"
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

    - 無系統標題列/邊框(overrideredirect)→ 自製細標題列(拖曳 + 關閉)+ 右下角縮放把手
    - 永遠置頂(-topmost)但不主動搶焦點(不 focus_force)
    - 半透明(-alpha),可動態調整
    - 關閉(✕)時呼叫 on_close(讓主程式把設定關掉並存檔)
    """

    _MIN_W = 168
    _MIN_H = 120

    def __init__(self, root, *, opacity: float = _OPACITY_DEFAULT,
                 geometry: str = "", on_close: Optional[Callable] = None,
                 on_geometry_change: Optional[Callable] = None) -> None:
        import tkinter as tk

        self._tk = tk
        self.on_close = on_close
        self.on_geometry_change = on_geometry_change
        self._cards_frame = None
        self._grip = None
        self._drag = {}

        self.win = tk.Toplevel(root)
        self.win.configure(bg=_WIN_BG)
        # 無系統標題列/邊框
        try:
            self.win.overrideredirect(True)
        except Exception:
            logging.debug("[浮動門診] overrideredirect 設定失敗", exc_info=True)
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
            self.win.geometry("232x300")

        # 外框(細邊框感):highlightthickness 當 1px 邊
        self._outer = tk.Frame(self.win, bg=_WIN_BG,
                               highlightbackground=_BORDER,
                               highlightcolor=_BORDER, highlightthickness=1, bd=0)
        self._outer.pack(fill="both", expand=True)
        self._build_chrome()
        self._body = tk.Frame(self._outer, bg=_WIN_BG)
        self._body.pack(fill="both", expand=True, padx=6, pady=(2, 7))
        self._build_grip()

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
        frame = tk.Frame(self._body, bg=_WIN_BG)
        frame.pack(fill="both", expand=True)
        self._cards_frame = frame
        for s in rooms:
            self._build_card(frame, s)
        # 縮放把手保持在最上層
        try:
            if self._grip is not None:
                self._grip.lift()
        except Exception:
            pass

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

    # ── 內部:自製標題列(拖曳 + 關閉) ─────────────────────────
    def _build_chrome(self) -> None:
        tk = self._tk
        hdr = tk.Frame(self._outer, bg=_HEADER_BG)
        hdr.pack(fill="x", side="top")
        title = tk.Label(hdr, text="⠿  門診動態", bg=_HEADER_BG, fg=_HEADER_FG,
                         font=(_FONT, 9), cursor="fleur")
        title.pack(side="left", padx=(8, 0), pady=3)
        close = tk.Label(hdr, text="✕", bg=_HEADER_BG, fg=_HEADER_FG,
                         font=(_FONT, 10, "bold"), cursor="hand2")
        close.pack(side="right", padx=(0, 8))
        close.bind("<Button-1>", lambda e: self._handle_close())
        close.bind("<Enter>", lambda e: close.configure(fg=_ERR_FG))
        close.bind("<Leave>", lambda e: close.configure(fg=_HEADER_FG))
        # 拖曳:標題列任意處
        for w in (hdr, title):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

    def _build_grip(self) -> None:
        tk = self._tk
        grip = tk.Label(self._outer, text="◢", bg=_WIN_BG, fg=_SUB,
                        font=(_FONT, 8), cursor="size_nw_se")
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<Button-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)
        self._grip = grip

    def _drag_start(self, e) -> None:
        self._drag["ox"] = e.x_root - self.win.winfo_x()
        self._drag["oy"] = e.y_root - self.win.winfo_y()

    def _drag_move(self, e) -> None:
        try:
            x = e.x_root - self._drag.get("ox", 0)
            y = e.y_root - self._drag.get("oy", 0)
            self.win.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _resize_start(self, e) -> None:
        self._drag["w0"] = self.win.winfo_width()
        self._drag["h0"] = self.win.winfo_height()
        self._drag["rx"] = e.x_root
        self._drag["ry"] = e.y_root

    def _resize_move(self, e) -> None:
        try:
            w = max(self._MIN_W, self._drag.get("w0", self._MIN_W)
                    + (e.x_root - self._drag.get("rx", e.x_root)))
            h = max(self._MIN_H, self._drag.get("h0", self._MIN_H)
                    + (e.y_root - self._drag.get("ry", e.y_root)))
            self.win.geometry(f"{w}x{h}")
        except Exception:
            pass

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

    # ── 內部:卡片 ────────────────────────────────────────────
    def _build_card(self, parent, s: RoomStatus) -> None:
        tk = self._tk
        v = room_card_view(s)
        accent = slot_color(s.slot)
        card = tk.Frame(parent, bg=_CARD_BG, highlightbackground=_BORDER,
                        highlightcolor=_BORDER, highlightthickness=1, bd=0)
        card.pack(fill="x", pady=(0, 6))
        # 左側時段色條(modern 卡片重點)
        tk.Frame(card, bg=accent, width=4).pack(side="left", fill="y")
        inner = tk.Frame(card, bg=_CARD_BG)
        inner.pack(side="left", fill="both", expand=True, padx=(9, 10), pady=6)
        # 標題列:診間 · 時段(左) + 醫師(右)
        head = tk.Frame(inner, bg=_CARD_BG)
        head.pack(fill="x")
        tk.Label(head, text=v["title"], bg=_CARD_BG, fg=_INK,
                 font=(_FONT, 11, "bold")).pack(side="left")
        tk.Label(head, text=v["doctor"], bg=_CARD_BG, fg=_SUB,
                 font=(_FONT, 9)).pack(side="right")
        # 燈號(放大、最顯眼) + 待診
        body = tk.Frame(inner, bg=_CARD_BG)
        body.pack(fill="x", pady=(2, 0))
        state = v["state"]
        light_fg = {"open": _LIGHT_OPEN, "error": _ERR_FG}.get(state, _LIGHT_DIM)
        big = state == "open"
        tk.Label(body, text=v["light"], bg=_CARD_BG, fg=light_fg,
                 font=(_FONT, 30 if big else 15, "bold")).pack(side="left")
        if big:
            wf = tk.Frame(body, bg=_CARD_BG)
            wf.pack(side="right", anchor="s", pady=(0, 5))
            tk.Label(wf, text=v["waiting"], bg=_CARD_BG, fg=_INK,
                     font=(_FONT, 14, "bold")).pack(side="top")
            tk.Label(wf, text="待診", bg=_CARD_BG, fg=_SUB,
                     font=(_FONT, 8)).pack(side="top")
