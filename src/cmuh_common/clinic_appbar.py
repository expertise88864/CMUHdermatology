# -*- coding: utf-8 -*-
"""門診動態「邊緣常駐條」(AppBar)— 貼在螢幕最下緣、工作列上方的一條細長 bar。

與浮動視窗(floating_clinic.py)互為兩種顯示方式(設定可二選一 / 關閉):
  - 用 Windows SHAppBarMessage 把自己註冊成「桌面工具列」(跟工作列同一套機制),
    系統會在下緣【保留】一條空間 → 作業視窗最大化時自動讓出、永不互相遮擋。
  - 短高度、左右橫向延伸:三個診間一排,各顯示「診間·時段 醫師 燈號 候診」。
  - 資料沿用既有 reg64 輪詢快取 + room_status_for_current_slot(依電腦時間自動切時段),
    本模組不自行查詢、不增加醫院負載。

【重要】關閉/結束時務必 ABM_REMOVE 把保留空間還回去,否則桌面可用區會一直縮著。
所有 Win32 操作 fail-open(失敗只記 log,不影響主程式)。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from cmuh_common.floating_clinic import RoomStatus, room_card_view

# ── 視覺(與浮動視窗同一套深色,維持一致觀感) ──────────────────────
_FONT = "Microsoft JhengHei UI"
_BG = "#0d1320"
_BORDER = "#2b3447"
_INK = "#e9eef6"
_SUB = "#7c889b"
_DOCTOR = "#93c5fd"
_OPEN = "#34d399"      # 看診中燈號(綠,最顯眼)
_DIM = "#5b6678"       # 關診/未開診(灰)
_ERR = "#f87171"       # 離線(紅)
_WAIT_FG = "#fbbf24"   # 待診(琥珀)
_PILL_BG = "#243049"

_DEFAULT_HEIGHT = 30   # 條的高度(px);短，盡量少占用實際工作區

# 不算「看診中亮燈」的燈號值(佔位字/未開始);這些燈號顏色用灰,不用綠。
_DIM_LIGHTS = {"", "—", "--", "0", "休"}


def appbar_segment_view(rs: RoomStatus) -> dict:
    """把單一診間狀態整理成「常駐條一格」要顯示的純資料。純函式,可單元測試。

    沿用 room_card_view 的判讀(state/light/doctor/waiting),只是攤平成橫向一格用。
    注意:room_card_view 對「還沒輪到的 pending 診間」也會給 state='open'(燈號='—'),
    所以「是否亮綠燈(lit)」要再排除佔位燈號,否則 pending 會誤亮綠。
    """
    v = room_card_view(rs)
    state = v["state"]
    light = v["light"]
    return {
        "label": v["title"],       # 例:101 · 早上 / 還沒時段時只有診間號
        "doctor": v["doctor"],     # 醫師姓名;無則 "—"
        "light": light,            # 燈號;關診/未開診/離線/— 已由 room_card_view 轉好字
        "waiting": v["waiting"],   # 待診人數字串;無則 "—"
        "open": state == "open" and str(light).strip() not in _DIM_LIGHTS,  # 真的亮綠燈
        "error": state == "error",
        "state": state,
    }


def _toplevel_hwnd(tk_window) -> int:
    """取得 tk window 真正的 Win32 top-level HWND(GetAncestor GA_ROOT,比 GetParent 可靠)。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    u.GetAncestor.restype = wintypes.HWND
    wid = tk_window.winfo_id()
    return u.GetAncestor(wid, 2) or wid  # GA_ROOT = 2


def _set_noactivate(tk_window) -> None:
    """加 WS_EX_NOACTIVATE:點這條 bar(例如 ✕)不會把焦點從 HIS 搶走。失敗忽略。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        long_ptr = ctypes.c_ssize_t
        if hasattr(u, "GetWindowLongPtrW"):
            get_long, set_long = u.GetWindowLongPtrW, u.SetWindowLongPtrW
        else:  # 32-bit Python
            get_long, set_long = u.GetWindowLongW, u.SetWindowLongW
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = long_ptr
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]
        set_long.restype = long_ptr
        gwl_exstyle = -20
        ws_ex_noactivate = 0x08000000
        hwnd = _toplevel_hwnd(tk_window)
        cur = get_long(hwnd, gwl_exstyle)
        set_long(hwnd, gwl_exstyle, cur | ws_ex_noactivate)
    except Exception:
        logging.debug("[常駐條] 設定 WS_EX_NOACTIVATE 失敗", exc_info=True)


class ClinicAppBar:
    """門診動態邊緣常駐條(Windows 專屬)。

    對外 API 與浮動視窗對齊:update_rooms / set_opacity / exists / lift_to_top / destroy。
    """

    _ABM_NEW = 0x00000000
    _ABM_REMOVE = 0x00000001
    _ABM_QUERYPOS = 0x00000002
    _ABM_SETPOS = 0x00000003
    _ABE_BOTTOM = 3

    def __init__(self, root, *, opacity: float = 0.95, edge: str = "bottom",
                 height: int = _DEFAULT_HEIGHT,
                 on_close: Optional[Callable] = None) -> None:
        import ctypes
        import tkinter as tk
        from ctypes import wintypes

        self._tk = tk
        self._ctypes = ctypes
        self.on_close = on_close
        self._opacity = max(0.25, min(1.0, float(opacity) if opacity else 0.95))
        self._h = max(20, int(height or _DEFAULT_HEIGHT))
        self._registered = False
        self._hwnd = 0
        self._last_rooms: list = []

        # SHAppBarMessage 原型(避免 64 位元截斷)
        self._shell32 = ctypes.windll.shell32

        class _APPBARDATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uCallbackMessage", wintypes.UINT),
                ("uEdge", wintypes.UINT),
                ("rc", wintypes.RECT),
                ("lParam", wintypes.LPARAM),
            ]

        self._APPBARDATA = _APPBARDATA
        self._shell32.SHAppBarMessage.restype = ctypes.c_size_t  # UINT_PTR(ctypes 指標寬,不會截斷)
        self._shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(_APPBARDATA)]
        self._user32 = ctypes.windll.user32
        self._user32.GetSystemMetrics.restype = ctypes.c_int
        self._user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        try:
            self._cb_msg = self._user32.RegisterWindowMessageW("CMUHClinicAppBarCallback") or 0x0440
        except Exception:
            self._cb_msg = 0x0440

        # ── 無邊框、置頂、不搶焦點的細長視窗 ──
        self.win = tk.Toplevel(root)
        try:
            self.win.overrideredirect(True)
        except Exception:
            logging.debug("[常駐條] overrideredirect 失敗", exc_info=True)
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            logging.debug("[常駐條] -topmost 失敗", exc_info=True)
        try:
            self.win.attributes("-alpha", self._opacity)
        except Exception:
            pass
        self.win.configure(bg=_BG)

        self._outer = tk.Frame(self.win, bg=_BG, highlightbackground=_BORDER,
                               highlightcolor=_BORDER, highlightthickness=1, bd=0)
        self._outer.pack(fill="both", expand=True)
        self._body = tk.Frame(self._outer, bg=_BG)
        self._body.pack(fill="both", expand=True)

        try:
            self.win.update_idletasks()
        except Exception:
            pass
        _set_noactivate(self.win)
        # 【重要】_dock() 一旦 ABM_NEW 成功就「保留了空間」;若之後 _render()/Tk 建構丟例外,
        # __init__ 會中止 → 主程式拿不到物件 → 永遠不會 destroy() → 保留空間漏掉、桌面一直被縮。
        # 故包起來:任一步失敗就先 destroy()(內含 ABM_REMOVE 還空間)再 re-raise。
        try:
            self._dock()      # 註冊 + 定位(保留下緣空間)
            self._render()
        except Exception:
            try:
                self.destroy()
            except Exception:
                logging.debug("[常駐條] 建構失敗清理時再次例外", exc_info=True)
            raise

    # ── AppBar 註冊 / 定位 / 移除 ────────────────────────────
    def _appbardata(self, hwnd):
        ctypes = self._ctypes
        abd = self._APPBARDATA()
        abd.cbSize = ctypes.sizeof(self._APPBARDATA)
        abd.hWnd = hwnd
        return abd

    def _dock(self) -> None:
        """註冊 appbar(首次)並把自己定位到最下緣、工作列上方,保留該空間。fail-open。"""
        try:
            ctypes = self._ctypes
            hwnd = _toplevel_hwnd(self.win)
            if not hwnd:
                self._fallback_position()   # 取不到 hwnd → 至少手動貼底,別讓視窗停在預設位置
                return
            self._hwnd = hwnd
            abd = self._appbardata(hwnd)
            if not self._registered:
                abd.uCallbackMessage = self._cb_msg
                self._shell32.SHAppBarMessage(self._ABM_NEW, ctypes.byref(abd))
                self._registered = True
            sw = self._user32.GetSystemMetrics(0)  # SM_CXSCREEN
            sh = self._user32.GetSystemMetrics(1)  # SM_CYSCREEN
            abd.uEdge = self._ABE_BOTTOM
            abd.rc.left = 0
            abd.rc.right = sw
            abd.rc.top = sh - self._h
            abd.rc.bottom = sh
            # QUERYPOS:系統依工作列/其他 appbar 調整 rc(下緣 → rc.bottom 落在工作列上方)
            self._shell32.SHAppBarMessage(self._ABM_QUERYPOS, ctypes.byref(abd))
            abd.rc.top = abd.rc.bottom - self._h
            self._shell32.SHAppBarMessage(self._ABM_SETPOS, ctypes.byref(abd))
            x = int(abd.rc.left)
            y = int(abd.rc.top)
            w = int(abd.rc.right - abd.rc.left)
            h = int(abd.rc.bottom - abd.rc.top)
            if w <= 0 or h <= 0:   # 異常 → 退回整螢幕寬、預設高
                x, y, w, h = 0, sh - self._h, sw, self._h
            self.win.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            logging.debug("[常駐條] dock 失敗 → 退回手動貼底", exc_info=True)
            self._fallback_position()

    def _fallback_position(self) -> None:
        """SHAppBarMessage 失敗時的退路:手動貼到螢幕最下緣(不保留空間,可能被蓋,但仍顯示)。"""
        try:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            self.win.geometry(f"{sw}x{self._h}+0+{sh - self._h}")
        except Exception:
            logging.debug("[常駐條] fallback 定位失敗", exc_info=True)

    def reassert(self) -> None:
        """重申位置(因應工作列移動/解析度變更)+ 重申置頂。每次 tick 呼叫,成本極小。"""
        if not self.exists():
            return
        self._dock()
        self.lift_to_top()

    def _undock(self) -> None:
        """把保留的空間還回去(ABM_REMOVE)。務必在視窗銷毀【前】呼叫(需要有效 hwnd)。"""
        if not self._registered:
            return
        try:
            ctypes = self._ctypes
            hwnd = self._hwnd or _toplevel_hwnd(self.win)
            abd = self._appbardata(hwnd)
            self._shell32.SHAppBarMessage(self._ABM_REMOVE, ctypes.byref(abd))
        except Exception:
            logging.debug("[常駐條] ABM_REMOVE 失敗", exc_info=True)
        finally:
            self._registered = False

    # ── 顯示 ────────────────────────────────────────────────
    def update_rooms(self, rooms: list) -> None:
        self._last_rooms = list(rooms or [])
        self._render()

    def _render(self) -> None:
        tk = self._tk
        try:
            for child in self._body.winfo_children():
                child.destroy()
        except Exception:
            return
        rooms = self._last_rooms
        n = len(rooms)
        col = 0
        for i, rs in enumerate(rooms):
            view = appbar_segment_view(rs)
            self._body.columnconfigure(col, weight=1, uniform="seg")
            seg = tk.Frame(self._body, bg=_BG)
            seg.grid(row=0, column=col, sticky="nsew")
            self._build_segment(seg, view)
            col += 1
            if i < n - 1:
                div = tk.Frame(self._body, bg=_BORDER, width=1)
                div.grid(row=0, column=col, sticky="ns", pady=4)
                col += 1
        # 最右邊放一個小 ✕ 可關閉(WS_EX_NOACTIVATE → 不搶 HIS 焦點)
        close = tk.Label(self._body, text="✕", bg=_BG, fg=_SUB,
                         font=(_FONT, 10, "bold"), cursor="hand2")
        close.grid(row=0, column=col, sticky="ns", padx=(6, 8))
        close.bind("<Button-1>", lambda e: self._handle_close())
        close.bind("<Enter>", lambda e: close.configure(fg=_ERR))
        close.bind("<Leave>", lambda e: close.configure(fg=_SUB))

    def _build_segment(self, seg, view) -> None:
        tk = self._tk
        # 左:診間·時段 + 醫師
        tk.Label(seg, text=view["label"], bg=_BG, fg=_INK,
                 font=(_FONT, 12, "bold")).pack(side="left", padx=(10, 5))
        doc = view["doctor"]
        tk.Label(seg, text=doc, bg=_BG,
                 fg=(_DOCTOR if doc and doc != "—" else _SUB),
                 font=(_FONT, 12)).pack(side="left")
        # 右:候診 pill + 燈號(燈號最顯眼)
        wait = view["waiting"]
        if wait and wait != "—":
            tk.Label(seg, text=f"候 {wait}", bg=_PILL_BG, fg=_WAIT_FG,
                     font=(_FONT, 11), padx=6).pack(side="right", padx=(4, 10))
        light_fg = _OPEN if view["open"] else (_ERR if view["error"] else _DIM)
        tk.Label(seg, text=view["light"], bg=_BG, fg=light_fg,
                 font=(_FONT, 15, "bold")).pack(side="right", padx=(0, 8))

    # ── 共用 API ────────────────────────────────────────────
    def set_opacity(self, value) -> None:
        try:
            self._opacity = max(0.25, min(1.0, float(value)))
            self.win.attributes("-alpha", self._opacity)
        except Exception:
            logging.debug("[常駐條] -alpha 設定失敗", exc_info=True)

    def lift_to_top(self) -> None:
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass

    def exists(self) -> bool:
        try:
            return bool(self.win is not None and self.win.winfo_exists())
        except Exception:
            return False

    def destroy(self) -> None:
        """先 ABM_REMOVE 把空間還回去,再銷毀視窗(順序很重要:REMOVE 需要有效 hwnd)。"""
        self._undock()
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            logging.debug("[常駐條] 銷毀視窗失敗", exc_info=True)
        finally:
            self.win = None

    def _handle_close(self) -> None:
        if self.on_close:
            try:
                self.on_close()
                return
            except Exception:
                logging.debug("[常駐條] on_close 例外", exc_info=True)
        self.destroy()
