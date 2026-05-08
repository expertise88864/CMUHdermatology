# -*- coding: utf-8 -*-
"""[O18] 啟動 splash 視窗。

在依賴檢查後、主視窗建立前顯示「載入中」，給使用者即時反饋。
也可由呼叫者更新文字（例：載入到哪一階段）。
"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import ttk


class StartupSplash:
    """簡單的啟動 splash 視窗。

    用法:
        splash = StartupSplash("載入中…")
        splash.show()
        # ... 做事 ...
        splash.update_text("初始化視窗")
        # ... 主視窗 .deiconify() 後 ...
        splash.close()
    """

    def __init__(self, text: str = "載入中…", title: str = "中國醫皮膚科常用程式"):
        self._text = text
        self._title = title
        self._root: tk.Tk | None = None
        self._label_var: tk.StringVar | None = None
        self._closed = threading.Event()

    def show(self) -> None:
        try:
            r = tk.Tk()
            r.title(self._title)
            r.overrideredirect(True)  # 無邊框
            r.attributes("-topmost", True)
            r.configure(bg="#FFFFFF")

            # 置中
            w, h = 380, 110
            sw = r.winfo_screenwidth()
            sh = r.winfo_screenheight()
            x = (sw - w) // 2
            y = (sh - h) // 2
            r.geometry(f"{w}x{h}+{x}+{y}")

            frame = tk.Frame(r, bg="#FFFFFF", highlightbackground="#005A9C",
                             highlightthickness=2)
            frame.pack(fill=tk.BOTH, expand=True)

            tk.Label(frame, text=self._title,
                     font=("Microsoft JhengHei UI", 14, "bold"),
                     fg="#005A9C", bg="#FFFFFF").pack(pady=(15, 5))

            self._label_var = tk.StringVar(value=self._text)
            tk.Label(frame, textvariable=self._label_var,
                     font=("Microsoft JhengHei UI", 10),
                     fg="#333333", bg="#FFFFFF").pack(pady=(0, 5))

            pb = ttk.Progressbar(frame, mode="indeterminate", length=300)
            pb.pack(pady=(0, 10))
            pb.start(20)

            self._root = r
            r.update_idletasks()
            r.update()
        except Exception as e:
            logging.debug("splash show 失敗（忽略）: %s", e)

    def update_text(self, text: str) -> None:
        if self._label_var is None or self._root is None:
            return
        try:
            self._label_var.set(text)
            self._root.update_idletasks()
        except Exception:
            pass

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._root is None:
            return
        try:
            self._root.destroy()
        except Exception:
            logging.debug("splash close 失敗（忽略）", exc_info=True)
        self._root = None
