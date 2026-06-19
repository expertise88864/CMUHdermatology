# -*- coding: utf-8 -*-
"""浮動門診動態小視窗 — 半透明、永遠置頂(不搶焦點)、無邊框、點擊穿透的浮窗。

每個診間一張小卡:診間號 · 時段 · 醫師 · 【燈號(放大、最顯眼)】· 待診人數。
資料由主程式餵入(沿用既有 reg64 60–90 秒輪詢的快取,本視窗不自行查詢、不增加醫院負載)。

[2026-06-19] 兩個無邊框 Toplevel 達成「真正懸浮」:
  - 標題列視窗(可點):拖曳移動 + ✕ 關閉。
  - 內容視窗(點擊穿透 WS_EX_TRANSPARENT):點卡片會穿到後方視窗,不擋住作業。
  - 沒有醫師姓名的診間自動隱藏、視窗高度自動縮放;字體放大。

設計:純邏輯(RoomStatus / should_show_room / clamp_opacity / room_card_view /
parse_geometry_size)抽出來可單元測試;ClinicFloatingWindow 為 Windows/GUI 專屬。
"""
from __future__ import annotations

import logging
import re
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
    fetched: bool = False          # 是否已從 reg64 查到過資料(False=還沒輪到)


def should_show_room(s: RoomStatus) -> bool:
    """這個診間要不要顯示在浮動視窗。純函式。

    使用者規則(2026-06-19):
      - 還沒查到資料(fetched=False)→ 先顯示(中性「—」),不要急著隱藏。
      - 查到了但【完全沒有醫師姓名、也沒有燈號】→ 代表今天沒有這個診 → 隱藏(UI 自動縮減)。
      - 有醫師姓名(即使未開診/關診)或有燈號 → 顯示(未開診就顯示「未開診」)。
    """
    if not s.fetched:
        return True
    if (s.doctor or "").strip():
        return True
    if str(s.light or "").strip():
        return True
    return False


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


def parse_geometry_pos(geometry: str) -> Optional[tuple]:
    """從 'WxH+X+Y' 取 (x, y);取不到回 None。純函式。"""
    m = re.search(r"\+(-?\d+)\+(-?\d+)", str(geometry))
    if m:
        try:
            return int(m.group(1)), int(m.group(2))
        except ValueError:
            pass
    return None


# ── GUI(Windows / tkinter 專屬) ───────────────────────────────────────
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_NOACTIVATE = 0x08000000
_GA_ROOT = 2


def _toplevel_hwnd(tk_window) -> int:
    """取得 tk window 的真正 Win32 top-level HWND。用 GetAncestor(GA_ROOT) 由 winfo_id
    往上走到根視窗 —— 比 GetParent 可靠(overrideredirect 無外框時 GetParent 會抓到桌面,
    把延伸樣式設到桌面就災難了)。設好 argtypes/restype 避免 64 位元 handle 截斷。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    u.GetAncestor.restype = wintypes.HWND
    wid = tk_window.winfo_id()
    return u.GetAncestor(wid, _GA_ROOT) or wid


def _set_ex_styles(tk_window, *, transparent: bool) -> None:
    """設定視窗延伸樣式。一律加 WS_EX_NOACTIVATE(點它不搶 HIS 焦點);transparent=True
    再加 WS_EX_TRANSPARENT(點擊穿透 —— 給內容窗,點卡片穿到後方)。標題列用
    transparent=False(仍可點拖曳/關閉,但不搶焦點)。

    用正確 ctypes 原型(argtypes/restype + LONG_PTR)避免 64 位元 handle/long 截斷。
    Windows 專屬,失敗忽略(非 Windows / 取不到 hwnd 時不影響其他功能)。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        long_ptr = ctypes.c_ssize_t  # LONG_PTR:64 位元為 8 bytes
        if hasattr(u, "GetWindowLongPtrW"):
            get_long, set_long = u.GetWindowLongPtrW, u.SetWindowLongPtrW
        else:  # 32-bit Python
            get_long, set_long = u.GetWindowLongW, u.SetWindowLongW
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = long_ptr
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]
        set_long.restype = long_ptr
        hwnd = _toplevel_hwnd(tk_window)
        ex = get_long(hwnd, _GWL_EXSTYLE)
        ex |= _WS_EX_LAYERED | _WS_EX_NOACTIVATE
        if transparent:
            ex |= _WS_EX_TRANSPARENT
        set_long(hwnd, _GWL_EXSTYLE, ex)
    except Exception:
        logging.debug("[浮動門診] 設定延伸樣式失敗", exc_info=True)


class ClinicFloatingWindow:
    """浮動門診動態視窗 — 兩個無邊框 Toplevel 達成「真正懸浮」:

    - 標題列視窗(self.win):可點 → 拖曳移動 + ✕ 關閉。
    - 內容視窗(self._content):點擊穿透 → 點卡片會穿到後方視窗,不擋住打 HIS。
    兩窗皆 -topmost + -alpha,移動/關閉/透明度同步。沒醫師的診自動隱藏、高度自動縮放。
    對外 API(update_rooms/set_opacity/get_geometry/exists/lift_to_top/destroy)與舊版相容。
    """

    _BAR_H = 26
    _DEFAULT_W = 232
    _MIN_W = 150

    def __init__(self, root, *, opacity: float = _OPACITY_DEFAULT,
                 geometry: str = "", on_close: Optional[Callable] = None,
                 on_geometry_change: Optional[Callable] = None) -> None:
        import tkinter as tk

        self._tk = tk
        self.on_close = on_close
        self.on_geometry_change = on_geometry_change
        self._opacity = clamp_opacity(opacity)
        self._cards_frame = None
        self._drag: dict = {}

        # 位置/寬度(高度自動縮放)
        self._x, self._y, self._w = self._parse_geo(geometry)

        # ── 標題列視窗(可點:拖曳 + 關閉) ──
        self.win = tk.Toplevel(root)
        self._setup_toplevel(self.win, _HEADER_BG)
        self._build_bar()

        # ── 內容視窗(點擊穿透) ──
        self._content = tk.Toplevel(root)
        self._setup_toplevel(self._content, _WIN_BG)
        self._outer = tk.Frame(self._content, bg=_WIN_BG,
                               highlightbackground=_BORDER,
                               highlightcolor=_BORDER, highlightthickness=1, bd=0)
        self._outer.pack(fill="both", expand=True)
        self._body = tk.Frame(self._outer, bg=_WIN_BG)
        self._body.pack(fill="both", expand=True, padx=6, pady=6)

        self._reposition(content_h=120)
        # 延伸樣式要在視窗 map 之後設(取得 hwnd):內容窗點擊穿透;標題列只不搶焦點。
        try:
            self.win.update_idletasks()
            self._content.update_idletasks()
        except Exception:
            pass
        _set_ex_styles(self._content, transparent=True)
        _set_ex_styles(self.win, transparent=False)

    # ── 建構輔助 ─────────────────────────────────────────────
    def _setup_toplevel(self, win, bg) -> None:
        try:
            win.overrideredirect(True)
        except Exception:
            logging.debug("[浮動門診] overrideredirect 失敗", exc_info=True)
        try:
            win.attributes("-topmost", True)
        except Exception:
            logging.debug("[浮動門診] -topmost 失敗", exc_info=True)
        try:
            win.attributes("-alpha", self._opacity)
        except Exception:
            logging.debug("[浮動門診] -alpha 失敗", exc_info=True)
        win.configure(bg=bg)

    def _parse_geo(self, geometry: str):
        x, y, w = 80, 80, self._DEFAULT_W
        wh = parse_geometry_size(geometry)
        if wh:
            w = max(self._MIN_W, wh[0])
        pos = parse_geometry_pos(geometry)
        if pos:
            x, y = pos
        return x, y, w

    def _build_bar(self) -> None:
        tk = self._tk
        bar = tk.Frame(self.win, bg=_HEADER_BG, highlightbackground=_BORDER,
                       highlightcolor=_BORDER, highlightthickness=1, bd=0)
        bar.pack(fill="both", expand=True)
        title = tk.Label(bar, text="⠿  門診動態", bg=_HEADER_BG, fg=_HEADER_FG,
                         font=(_FONT, 10), cursor="fleur")
        title.pack(side="left", padx=(8, 0))
        close = tk.Label(bar, text="✕", bg=_HEADER_BG, fg=_HEADER_FG,
                         font=(_FONT, 11, "bold"), cursor="hand2")
        close.pack(side="right", padx=(0, 8))
        close.bind("<Button-1>", lambda e: self._handle_close())
        close.bind("<Enter>", lambda e: close.configure(fg=_ERR_FG))
        close.bind("<Leave>", lambda e: close.configure(fg=_HEADER_FG))
        for w in (bar, title):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

    def _reposition(self, content_h: Optional[int] = None) -> None:
        try:
            self.win.geometry(f"{self._w}x{self._BAR_H}+{self._x}+{self._y}")
            if content_h is None:
                content_h = max(60, self._content.winfo_height())
            cy = self._y + self._BAR_H
            self._content.geometry(f"{self._w}x{content_h}+{self._x}+{cy}")
        except Exception:
            logging.debug("[浮動門診] reposition 失敗", exc_info=True)

    def _drag_start(self, e) -> None:
        self._drag["ox"] = e.x_root - self._x
        self._drag["oy"] = e.y_root - self._y

    def _drag_move(self, e) -> None:
        self._x = e.x_root - self._drag.get("ox", 0)
        self._y = e.y_root - self._drag.get("oy", 0)
        try:
            self._reposition(content_h=self._content.winfo_height())
        except Exception:
            self._reposition()

    # ── 對外 API ─────────────────────────────────────────────
    def set_opacity(self, value) -> None:
        self._opacity = clamp_opacity(value)
        for w in (self.win, getattr(self, "_content", None)):
            try:
                if w is not None:
                    w.attributes("-alpha", self._opacity)
            except Exception:
                logging.debug("[浮動門診] -alpha 設定失敗", exc_info=True)

    def update_rooms(self, rooms: list) -> None:
        """rooms: list[RoomStatus]。沒醫師的診自動隱藏;重建卡片 + 自動縮放高度。"""
        tk = self._tk
        visible = [s for s in rooms if should_show_room(s)]
        if self._cards_frame is not None:
            try:
                self._cards_frame.destroy()
            except Exception:
                pass
        frame = tk.Frame(self._body, bg=_WIN_BG)
        frame.pack(fill="both", expand=True)
        self._cards_frame = frame
        if not visible:
            tk.Label(frame, text="目前無開診", bg=_WIN_BG, fg=_SUB,
                     font=(_FONT, 12)).pack(pady=16)
        for s in visible:
            self._build_card(frame, s)
        # 依內容自動縮放高度
        try:
            self._content.update_idletasks()
            h = max(60, self._outer.winfo_reqheight())
            self._reposition(content_h=h)
            _set_ex_styles(self._content, transparent=True)  # rebuild 後重申(保險)
        except Exception:
            logging.debug("[浮動門診] 自動縮放失敗", exc_info=True)
        self.lift_to_top()

    def get_geometry(self) -> str:
        try:
            ch = self._content.winfo_height()
        except Exception:
            ch = 200
        return f"{self._w}x{self._BAR_H + ch}+{self._x}+{self._y}"

    def lift_to_top(self) -> None:
        """重申置頂(不搶焦點)。"""
        for w in (getattr(self, "_content", None), self.win):
            try:
                if w is not None:
                    w.attributes("-topmost", True)
            except Exception:
                pass

    def exists(self) -> bool:
        """兩個視窗都在才算存在。其中一個沒了 → 清掉殘存的另一個、回 False,
        讓主程式重建乾淨的一對(避免孤兒內容窗/標題列殘留)。"""
        def _alive(w):
            try:
                return bool(w is not None and w.winfo_exists())
            except Exception:
                return False
        bar_ok = _alive(self.win)
        content_ok = _alive(getattr(self, "_content", None))
        if bar_ok and content_ok:
            return True
        if bar_ok or content_ok:  # 只剩一個 → 清掉(不走 on_geometry_change,狀態已壞)
            for w in (getattr(self, "_content", None), self.win):
                try:
                    if w is not None:
                        w.destroy()
                except Exception:
                    pass
        return False

    def destroy(self) -> None:
        try:
            if self.on_geometry_change:
                g = self.get_geometry()
                if g:
                    self.on_geometry_change(g)
        except Exception:
            pass
        for w in (getattr(self, "_content", None), self.win):
            try:
                if w is not None:
                    w.destroy()
            except Exception:
                pass

    # ── 內部 ─────────────────────────────────────────────────
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
        accent = slot_color(s.slot)
        card = tk.Frame(parent, bg=_CARD_BG, highlightbackground=_BORDER,
                        highlightcolor=_BORDER, highlightthickness=1, bd=0)
        card.pack(fill="x", pady=(0, 6))
        tk.Frame(card, bg=accent, width=4).pack(side="left", fill="y")
        inner = tk.Frame(card, bg=_CARD_BG)
        inner.pack(side="left", fill="both", expand=True, padx=(9, 10), pady=7)
        # 標題列:診間 · 時段(左) + 醫師(右) —— 醫師字體放大到與診間/時段一致
        head = tk.Frame(inner, bg=_CARD_BG)
        head.pack(fill="x")
        tk.Label(head, text=v["title"], bg=_CARD_BG, fg=_INK,
                 font=(_FONT, 13, "bold")).pack(side="left")
        tk.Label(head, text=v["doctor"], bg=_CARD_BG, fg=_SUB,
                 font=(_FONT, 13)).pack(side="right")
        # 燈號(放大、最顯眼) + 待診人數(也放大)
        body = tk.Frame(inner, bg=_CARD_BG)
        body.pack(fill="x", pady=(3, 0))
        state = v["state"]
        light_fg = {"open": _LIGHT_OPEN, "error": _ERR_FG}.get(state, _LIGHT_DIM)
        big = state == "open"
        tk.Label(body, text=v["light"], bg=_CARD_BG, fg=light_fg,
                 font=(_FONT, 32 if big else 17, "bold")).pack(side="left")
        if big:
            wf = tk.Frame(body, bg=_CARD_BG)
            wf.pack(side="right", anchor="s", pady=(0, 6))
            tk.Label(wf, text=v["waiting"], bg=_CARD_BG, fg=_INK,
                     font=(_FONT, 18, "bold")).pack(side="top")
            tk.Label(wf, text="待診", bg=_CARD_BG, fg=_SUB,
                     font=(_FONT, 11)).pack(side="top")
