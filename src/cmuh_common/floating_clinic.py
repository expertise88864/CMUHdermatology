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
_DOCTOR_FG = "#93c5fd"     # 醫師姓名(柔藍,有顏色更好認)
_PILL_BG = "#243049"       # 待診 pill 底(比卡片稍亮)
_WAIT_FG = "#fbbf24"       # 待診人數(琥珀,最醒目)
_TAG_FG = "#0b1018"        # 時段 tag 上的深色字(在亮色 accent 上)


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
    slot_tc: str = ""              # 這筆資料屬於哪個 reg64 TimeCode("1"/"2"/"3");用來判斷是否為「目前時段」


_PLACEHOLDER_LIGHT = {"--", "—", "休", "0", ""}


def room_status_for_current_slot(cached: Optional[RoomStatus], room: str,
                                 cur_tc: str, cur_slot: str) -> RoomStatus:
    """浮動視窗一律顯示「目前電腦時間」對應的時段(早上/下午/晚上)。純函式。

    使用者需求(2026-06-19):浮動視窗的時段永遠依電腦時間自動切換,**不受**主程式
    「目前門診動態」卡片被手動固定成某時段影響。
      - 若快取資料正好是目前時段(slot_tc == cur_tc)→ 直接用該即時資料。
      - 否則(還沒輪詢到 / 卡片被固定成別的時段 / 剛跨過時段交界尚未補上)→ 回傳該診間
        「目前時段、資料待更新」的中性狀態(fetched=False → 顯示「—」),
        絕不拿「別的時段」的舊資料魚目混珠。
    """
    if cached is not None and str(cur_tc) != "" and str(cached.slot_tc) == str(cur_tc):
        return cached
    return RoomStatus(room=room, slot=cur_slot, slot_tc=str(cur_tc))


def should_show_room(s: RoomStatus) -> bool:
    """這個診間要不要顯示在浮動視窗。純函式。

    使用者規則(2026-06-19):
      - 還沒查到資料(fetched=False)→ 先顯示(中性「—」),不要急著隱藏。
      - 有醫師姓名 → 顯示(即使未開診/關診,顯示「未開診」/「關診」)。
      - 沒醫師姓名:未開診/關診/離線 → 代表今天沒有這個診 → 隱藏(UI 自動縮減);
        其餘只有「真的有有效看診號」才顯示。
    [2026-06-19 修] 未開診的診間 reg64 燈號常是 '--' 佔位字,舊版誤判成「有燈號」而沒隱藏。
    """
    if not s.fetched:
        return True
    if (s.doctor or "").strip():
        return True
    if s.error or s.stopped or s.closed:
        return False
    return str(s.light or "").strip() not in _PLACEHOLDER_LIGHT


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


_CORNER_R = 12  # 圓角半徑


def _apply_round_region(tk_window, w: int, h: int, radius: int, mode: str) -> None:
    """用 GDI 把視窗剪成圓角(SetWindowRgn)→ 視窗本體就有圓角(不只卡片)。
    mode:'top'=只圓上面兩角(標題列)、'bottom'=只圓下面兩角(內容窗)、'all'=四角。
    只圓一邊的作法:把另一邊的圓角推到視窗外(被高度裁掉)→ 那邊看起來是直角。
    系統接管 region(設新的會自動刪舊的),SetWindowRgn 失敗才自己 DeleteObject。
    Windows 專屬,失敗忽略。"""
    try:
        import ctypes
        from ctypes import wintypes
        gdi = ctypes.windll.gdi32
        u = ctypes.windll.user32
        gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        gdi.CreateRoundRectRgn.restype = ctypes.c_void_p
        gdi.DeleteObject.argtypes = [ctypes.c_void_p]
        u.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
        u.SetWindowRgn.restype = ctypes.c_int
        d = radius * 2
        if mode == "top":          # 下面兩角推到 h+radius(視窗只有 h 高 → 下緣是直的)
            rgn = gdi.CreateRoundRectRgn(0, 0, w + 1, h + radius + 1, d, d)
        elif mode == "bottom":     # 上面兩角推到 -radius(視窗外 → 上緣是直的)
            rgn = gdi.CreateRoundRectRgn(0, -radius, w + 1, h + 1, d, d)
        else:
            rgn = gdi.CreateRoundRectRgn(0, 0, w + 1, h + 1, d, d)
        hwnd = _toplevel_hwnd(tk_window)
        if not u.SetWindowRgn(hwnd, rgn, True):
            gdi.DeleteObject(rgn)
    except Exception:
        logging.debug("[浮動門診] 圓角剪裁失敗", exc_info=True)


def _round_rect(cv, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上畫圓角矩形(smooth polygon)。kw 可帶 fill / outline / width。"""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class ClinicFloatingWindow:
    """浮動門診動態視窗 — 兩個無邊框 Toplevel 達成「真正懸浮」:

    - 標題列視窗(self.win):可點 → 拖曳移動 + ✕ 關閉。
    - 內容視窗(self._content):點擊穿透 → 點卡片會穿到後方視窗,不擋住打 HIS。
    兩窗皆 -topmost + -alpha,移動/關閉/透明度同步。沒醫師的診自動隱藏、高度自動縮放。
    對外 API(update_rooms/set_opacity/get_geometry/exists/lift_to_top/destroy)與舊版相容。
    """

    _BAR_H = 26
    _DEFAULT_W = 242
    _MIN_W = 206            # 要夠寬讓「燈號 + 待診 pill」不重疊(舊版太窄 → 格式跑掉)
    _CARD_H_OPEN = 90      # 看診中卡片高(燈號放大)
    _CARD_H_DIM = 68       # 未開診/關診/離線卡片高(要夠高,否則狀態字會疊到上排)
    _CARD_PADY = 7         # 卡片間距

    def __init__(self, root, *, opacity: float = _OPACITY_DEFAULT,
                 geometry: str = "", on_close: Optional[Callable] = None,
                 on_geometry_change: Optional[Callable] = None) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self._tk = tk
        self.on_close = on_close
        self.on_geometry_change = on_geometry_change
        self._opacity = clamp_opacity(opacity)
        self._cards_frame = None
        self._last_rooms: list = []
        self._manual_h = None        # 使用者手動拉的高度(None=自動依卡片數縮放)
        self._drag: dict = {}
        # 字型物件(可量測寬度 → 算 tag/pill 圓角矩形大小)
        self._fonts = {
            "tag": tkfont.Font(family=_FONT, size=10, weight="bold"),
            "room": tkfont.Font(family=_FONT, size=15, weight="bold"),
            "doctor": tkfont.Font(family=_FONT, size=13, weight="bold"),
            "light_big": tkfont.Font(family=_FONT, size=32, weight="bold"),
            "light_sm": tkfont.Font(family=_FONT, size=18, weight="bold"),
            "wait_lbl": tkfont.Font(family=_FONT, size=10),
            "wait_num": tkfont.Font(family=_FONT, size=18, weight="bold"),
            "empty": tkfont.Font(family=_FONT, size=12),
        }

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
        self._apply_round_regions(120)  # 初始圓角(資料進來後 _render 會再套真實高度)

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
        # 縮放把手(內容窗點擊穿透無法放可點 grip → 放標題列):拖它可同時改寬+高
        # (往右拉變寬、往下拉變高;高度變手動覆蓋自動縮放)。
        grip = tk.Label(bar, text="⤢", bg=_HEADER_BG, fg=_HEADER_FG,
                        font=(_FONT, 11, "bold"), cursor="bottom_right_corner")
        grip.pack(side="right", padx=(0, 4))
        grip.bind("<Button-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)
        for w in (bar, title):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

    def _resize_start(self, e) -> None:
        self._drag["rw"] = self._w
        self._drag["rh"] = (self._manual_h
                            if self._manual_h is not None
                            else self._content_height(self._visible_rooms()))
        self._drag["rx"] = e.x_root
        self._drag["ry"] = e.y_root

    def _resize_move(self, e) -> None:
        neww = max(self._MIN_W,
                   self._drag.get("rw", self._w) + (e.x_root - self._drag.get("rx", e.x_root)))
        newh = max(60,
                   self._drag.get("rh", 120) + (e.y_root - self._drag.get("ry", e.y_root)))
        self._w = neww
        self._manual_h = newh  # 手動高度(覆蓋自動縮放)
        self._render()  # 用新寬高重畫 + reposition

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

    def _apply_round_regions(self, content_h: int) -> None:
        """把標題列(上圓)與內容窗(下圓)剪成圓角 → 整個浮窗看起來是圓角的。"""
        _apply_round_region(self.win, self._w, self._BAR_H, _CORNER_R, "top")
        _apply_round_region(self._content, self._w,
                            max(2 * _CORNER_R, content_h), _CORNER_R, "bottom")

    # ── 對外 API ─────────────────────────────────────────────
    def _all_windows(self):
        return (self.win, getattr(self, "_content", None))

    def set_opacity(self, value) -> None:
        self._opacity = clamp_opacity(value)
        for w in self._all_windows():
            try:
                if w is not None:
                    w.attributes("-alpha", self._opacity)
            except Exception:
                logging.debug("[浮動門診] -alpha 設定失敗", exc_info=True)

    def update_rooms(self, rooms: list) -> None:
        """rooms: list[RoomStatus]。沒醫師的診自動隱藏;重建卡片 + 自動縮放高度。"""
        self._last_rooms = list(rooms or [])
        self._render()

    def _visible_rooms(self) -> list:
        return [s for s in self._last_rooms if should_show_room(s)]

    def _content_height(self, visible: list) -> int:
        """依顯示的卡片數【直接算】內容高(不靠 winfo_reqheight,更穩、不會虛高/被裁)。"""
        if not visible:
            return 64
        total = 2 + 12  # 外框(1+1) + body pady(6+6)
        for s in visible:
            is_open = room_card_view(s)["state"] == "open"
            total += (self._CARD_H_OPEN if is_open else self._CARD_H_DIM) + self._CARD_PADY
        return total

    def _render(self) -> None:
        tk = self._tk
        visible = [s for s in self._last_rooms if should_show_room(s)]
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
                     font=self._fonts["empty"]).pack(pady=16)
        for s in visible:
            self._build_card(frame, s)
        try:
            # 先讓卡片 pack 落定,_reposition 設的 geometry 才不會被延後的 pack 蓋掉。
            self._content.update_idletasks()
            # 高度:使用者手動拉過就用手動值,否則依卡片數自動縮放
            h = self._manual_h if self._manual_h is not None else self._content_height(visible)
            ch = max(60, int(h))
            self._reposition(content_h=ch)
            _set_ex_styles(self._content, transparent=True)  # rebuild 後重申(保險)
            self._apply_round_regions(ch)  # 圓角(視窗本體)
        except Exception:
            logging.debug("[浮動門診] 重繪/縮放失敗", exc_info=True)
        self.lift_to_top()

    def get_geometry(self) -> str:
        try:
            ch = self._content.winfo_height()
        except Exception:
            ch = 200
        return f"{self._w}x{self._BAR_H + ch}+{self._x}+{self._y}"

    def lift_to_top(self) -> None:
        """重申置頂(不搶焦點)。"""
        for w in self._all_windows():
            try:
                if w is not None:
                    w.attributes("-topmost", True)
            except Exception:
                pass

    def exists(self) -> bool:
        """標題列 + 內容窗都在才算存在。其中一個沒了 → 清掉殘存的(含縮放把手)、回
        False,讓主程式重建乾淨的一組(避免孤兒視窗殘留)。"""
        def _alive(w):
            try:
                return bool(w is not None and w.winfo_exists())
            except Exception:
                return False
        bar_ok = _alive(self.win)
        content_ok = _alive(getattr(self, "_content", None))
        if bar_ok and content_ok:
            return True
        if bar_ok or content_ok:  # 只剩一個 → 全部清掉(狀態已壞)
            for w in self._all_windows():
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
        for w in self._all_windows():
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

    def _card_width(self) -> int:
        # self._w 已在 _parse_geo 夾在 >= _MIN_W;扣掉 body padx(12)+ 外框(2)= -14。
        return self._w - 14

    def _build_card(self, parent, s: RoomStatus) -> None:
        """用 Canvas 畫【圓角】卡片(tkinter 原生 Frame 無圓角)。"""
        tk = self._tk
        f = self._fonts
        v = room_card_view(s)
        accent = slot_color(s.slot)
        state = v["state"]
        is_open = state == "open"
        slot = (s.slot or "").strip()
        cw = self._card_width()
        ch = self._CARD_H_OPEN if is_open else self._CARD_H_DIM
        pad = 13

        cv = tk.Canvas(parent, width=cw, height=ch, bg=_WIN_BG,
                       highlightthickness=0, bd=0)
        cv.pack(pady=(0, 7))  # 固定寬、置中(圓角卡片左右等距)
        # 卡片底(圓角 + 細邊)
        _round_rect(cv, 1, 1, cw - 1, ch - 1, 12,
                    fill=_CARD_BG, outline=_BORDER, width=1)
        # 左側時段色條(看診中=飽和色 / 否則暗,圓角)
        _round_rect(cv, 4, 11, 9, ch - 11, 2,
                    fill=(accent if is_open else _LIGHT_DIM), outline="")

        # ── 上排:時段 tag(圓角)+ 診間號(左) + 醫師(右,上色) ──
        x = pad
        if slot:
            tw = f["tag"].measure(slot) + 16
            _round_rect(cv, x, 11, x + tw, 31, 9, fill=accent, outline="")
            cv.create_text(x + tw / 2, 21, text=slot, fill=_TAG_FG,
                           font=f["tag"])
            x += tw + 8
        cv.create_text(x, 21, text=str(s.room).strip(), fill=_INK,
                       anchor="w", font=f["room"])
        cv.create_text(cw - pad, 21, text=v["doctor"], fill=_DOCTOR_FG,
                       anchor="e", font=f["doctor"])

        # ── 下排:燈號(hero) + 待診 pill(圓角琥珀,醒目) ──
        # 燈號用 anchor="w"(左、垂直置中)固定在上排【下方】,不會疊到上排(舊版用
        # 底部對齊在矮卡片上會與上排重疊 → 排版跑掉)。
        light_fg = accent if is_open else (_ERR_FG if state == "error" else _LIGHT_DIM)
        if is_open:
            ly = 58
            cv.create_text(pad, ly, text=v["light"], fill=light_fg,
                           anchor="w", font=f["light_big"])
            lbl_w = f["wait_lbl"].measure("待診")
            num_w = f["wait_num"].measure(v["waiting"])
            pill_w = lbl_w + num_w + 24
            px2 = cw - pad
            px1 = px2 - pill_w
            _round_rect(cv, px1, ly - 14, px2, ly + 14, 9, fill=_PILL_BG, outline="")
            cv.create_text(px1 + 9, ly, text="待診", fill=_SUB, anchor="w",
                           font=f["wait_lbl"])
            cv.create_text(px2 - 10, ly, text=v["waiting"], fill=_WAIT_FG,
                           anchor="e", font=f["wait_num"])
        else:
            cv.create_text(pad, 47, text=v["light"], fill=light_fg,
                           anchor="w", font=f["light_sm"])
