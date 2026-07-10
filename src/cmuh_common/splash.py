# -*- coding: utf-8 -*-
"""[O18] 啟動 splash 視窗 — 用 Toplevel 不要建第二個 tk.Tk()。

【BUG 修正】原版 show() 用 tk.Tk() 會造成主程式有 2 個 root Tk，導致 ttk 樣式表
錯亂，所有 ttk 元件出現「部分 widget 渲染失敗、字級變形」等怪現象。
"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import ttk

from cmuh_common.platform_win import (
    MonitorRect,
    get_preferred_monitor_rect,
    move_tk_window_to_monitor,
)


class StartupSplash:
    """啟動 splash 視窗。需要 parent root。

    用法:
        root = tk.Tk()
        root.withdraw()
        splash = StartupSplash(root, "載入中…")
        splash.show()
        # ... 做事 ...
        splash.close()
        root.deiconify()
    """

    def __init__(self, parent: tk.Misc, text: str = "載入中…",
                 title: str = "中國醫皮膚科常用程式"):
        self._parent = parent
        self._text = text
        self._title = title
        self._top: tk.Toplevel | None = None
        self._label_var: tk.StringVar | None = None
        self._geom: tuple[int, int, int, int] | None = None  # (x,y,w,h) 供關閉後強制重繪該區
        self._closed = threading.Event()

    def show(self) -> None:
        if self._parent is None:
            return
        # [2026-07-09 白框修正] 原版 `self._top = top` 在函式最後才賦值——中間任何一步
        # 失敗（破損 tk 的 ttk.Progressbar 建不出來等），白底置頂無邊框的 Toplevel 已經
        # 建立、卻沒掛到 self._top → close() 變 no-op → 「白色方框」孤兒永遠擋在桌面
        # （主視窗改為啟動即最小化後特別明顯）。改為：
        #   1. Toplevel 一建立就掛上 self._top（close 永遠找得到它）。
        #   2. 先 withdraw、全部內容蓋好才 deiconify（蓋到一半失敗＝從沒現身）。
        #   3. 失敗路徑主動銷毀 + 記 warning（可在 automation_ui.log 追蹤）。
        try:
            top = tk.Toplevel(self._parent)
        except Exception as e:
            logging.warning("splash 建立失敗（忽略）: %s", e)
            return
        self._top = top
        try:
            top.withdraw()              # 蓋好內容才現身
            top.title(self._title)
            top.overrideredirect(True)  # 無邊框
            top.configure(bg="#FFFFFF")

            # 置中
            w, h = 400, 130
            monitor = get_preferred_monitor_rect()
            if monitor is not None:
                x = monitor.left + (monitor.width - w) // 2
                y = monitor.top + (monitor.height - h) // 2
            else:
                sw = top.winfo_screenwidth()
                sh = top.winfo_screenheight()
                x = (sw - w) // 2
                y = (sh - h) // 2
            move_tk_window_to_monitor(top, MonitorRect(x, y, w, h))
            self._geom = (x, y, w, h)   # 記住位置,close() 後強制重繪此區消除白框殘影

            # 邊框 + 主題色 frame
            outer = tk.Frame(top, bg="#005A9C")
            outer.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
            inner = tk.Frame(outer, bg="#FFFFFF")
            inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

            tk.Label(inner, text=self._title,
                     font=("Microsoft JhengHei UI", 14, "bold"),
                     fg="#005A9C", bg="#FFFFFF").pack(pady=(18, 6))

            self._label_var = tk.StringVar(value=self._text)
            tk.Label(inner, textvariable=self._label_var,
                     font=("Microsoft JhengHei UI", 10),
                     fg="#555555", bg="#FFFFFF").pack(pady=(0, 8))

            pb = ttk.Progressbar(inner, mode="indeterminate", length=320)
            pb.pack(pady=(0, 12))
            try:
                pb.start(20)
            except Exception:
                pass

            top.deiconify()             # 全部蓋好，這時才現身
            top.attributes("-topmost", True)
            top.update_idletasks()
        except Exception as e:
            logging.warning("splash show 失敗，銷毀殘窗（忽略）: %s", e)
            try:
                top.withdraw()          # 先 SW_HIDE(讓 OS 重繪桌面),再 destroy,避免白框殘影
                top.update_idletasks()
            except Exception:
                pass
            try:
                top.destroy()
            except Exception:
                pass
            self._top = None
            self._label_var = None
            self._repaint_ghost_region()

    def update_text(self, text: str) -> None:
        if self._label_var is None or self._top is None:
            return
        try:
            self._label_var.set(text)
            self._top.update_idletasks()
        except Exception:
            pass

    def _repaint_ghost_region(self) -> None:
        """[2026-07-10 白框殘影修正] 強制重繪 splash 曾佔用的桌面矩形,消除殘影白框。

        overrideredirect(無邊框)的白窗 destroy 時,若主視窗仍 withdrawn(畫面上沒有其他視窗
        會自然重繪該區),Windows 常留下一塊「純白、無邊框、不能移動」的殘影(把所有視窗縮到
        桌面時最明顯)。withdraw(SW_HIDE)通常已足以觸發重繪,這裡再用 RedrawWindow 對桌面該
        矩形強制 invalidate+erase 當保險。純 Windows API,失敗忽略。"""
        geom = self._geom
        if not geom:
            return
        try:
            import ctypes
            from ctypes import wintypes
            x, y, w, h = geom
            rect = wintypes.RECT(int(x), int(y), int(x + w), int(y + h))
            RDW_INVALIDATE = 0x0001
            RDW_ERASE = 0x0004
            RDW_ALLCHILDREN = 0x0080
            # hWnd=NULL → 更新桌面;lprcUpdate=rect → 只重繪 splash 曾佔的那塊,避免全螢幕閃爍。
            ctypes.windll.user32.RedrawWindow(
                None, ctypes.byref(rect), None,
                RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN)
        except Exception:
            logging.debug("splash 殘影區重繪失敗（忽略）", exc_info=True)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        top = self._top
        self._top = None
        if top is None:
            return
        try:
            # 先解除 topmost，避免 Windows 把 topmost 旗標「傳染」給 parent root
            top.attributes("-topmost", False)
        except Exception:
            pass
        # [2026-07-10 白框殘影修正] 直接 destroy overrideredirect 白窗(且主視窗還 withdrawn)會在
        # 桌面留下純白殘影方框。改為:先 withdraw(SW_HIDE,OS 重繪底下桌面)+ flush,再 destroy。
        try:
            top.withdraw()
            top.update_idletasks()
        except Exception:
            pass
        try:
            top.destroy()
        except Exception:
            logging.warning("splash destroy 失敗（已先 withdraw 藏起）", exc_info=True)
        # 保險:對 splash 曾佔的桌面矩形強制重繪,徹底消除殘影白框。
        self._repaint_ghost_region()
        # 確保 parent root 沒被殘留 topmost
        try:
            if self._parent is not None:
                self._parent.attributes("-topmost", False)
        except Exception:
            pass
