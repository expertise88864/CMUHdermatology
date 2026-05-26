# -*- coding: utf-8 -*-
"""Tk callback exception handler — 共用模組。

Tk 預設 `report_callback_exception` 把例外 print 到 stderr，pythonw.exe 模式
下完全看不到，等於黑箱。安裝這個 handler 後，所有 `.after()` / 事件 binding
callback 拋的例外都會進 logging 系統，後續可以從 log 看完整 traceback。

主程式 main.py 已用過此 pattern，本模組把它抽出來給 scheduler.py /
consult_query.py / autoclock.py 共用 — 那三支也都有 Tk UI (設定視窗 / tray
互動 / config dialog)，原本各自的 callback 例外都漏進 stderr 黑洞。
"""
from __future__ import annotations

import logging
from typing import Optional


def _report_callback_exception(exc, val, tb) -> None:
    """Tk override hook — 把 callback 例外寫進 logging instead of stderr。"""
    logging.error("Uncaught Tk callback exception", exc_info=(exc, val, tb))


def install_tk_exception_handler(root: Optional[object] = None) -> bool:
    """安裝 Tk callback exception handler。

    root: 已建立的 Tk root instance (主程式 main_root / 設定視窗 self)。
          傳入後既覆蓋該 instance 的 hook，也 patch tk.Tk class itself
          (讓後續 Toplevel 自動繼承)。
          None → 只 patch class，給「import 時就先設好」場景用。

    回傳：True 安裝成功，False 例外吞掉 (不阻擋呼叫端流程)。
    """
    try:
        import tkinter as tk  # noqa: F401 — late import 避免無 Tk 環境炸
        if root is not None:
            try:
                root.report_callback_exception = _report_callback_exception
            except Exception:
                logging.debug("Tk root report_callback_exception 設定失敗",
                              exc_info=True)
        # 同時 patch class，讓後續 Toplevel 自動繼承
        try:
            tk.Tk.report_callback_exception = _report_callback_exception
        except Exception:
            logging.debug("Tk.Tk class hook patch 失敗", exc_info=True)
        return True
    except Exception:
        logging.debug("install_tk_exception_handler 例外", exc_info=True)
        return False
