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
        self._closed = threading.Event()

    def show(self) -> None:
        if self._parent is None:
            return
        try:
            top = tk.Toplevel(self._parent)
            top.title(self._title)
            top.overrideredirect(True)  # 無邊框
            top.attributes("-topmost", True)
            top.configure(bg="#FFFFFF")

            # 置中
            w, h = 400, 130
            sw = top.winfo_screenwidth()
            sh = top.winfo_screenheight()
            x = (sw - w) // 2
            y = (sh - h) // 2
            top.geometry(f"{w}x{h}+{x}+{y}")

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

            self._top = top
            top.update_idletasks()
        except Exception as e:
            logging.debug("splash show 失敗（忽略）: %s", e)

    def update_text(self, text: str) -> None:
        if self._label_var is None or self._top is None:
            return
        try:
            self._label_var.set(text)
            self._top.update_idletasks()
        except Exception:
            pass

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._top is None:
            return
        try:
            self._top.destroy()
        except Exception:
            logging.debug("splash close 失敗（忽略）", exc_info=True)
        self._top = None
