# -*- coding: utf-8 -*-
"""Tk 視窗圖示套用。搬自原主程式 line 509-585。

Tk 的 iconbitmap 在 Windows 常只套用 16x16，工作列/Alt+Tab 依 WM_SETICON 取大圖。
本模組同時套用兩者，並延遲重送 WM_SETICON 處理 Tk 後續重繪覆寫的情況。
"""
import ctypes
import logging
import os
import tkinter as tk

from cmuh_common.icons import ensure_cmuh_app_icon_path


def _apply_windows_wm_seticon_from_ico(root: tk.Misc, ico_path: str) -> None:
    if os.name != "nt":
        return
    path = os.path.abspath(ico_path)
    if not os.path.isfile(path):
        return
    try:
        root.update_idletasks()
        wid = int(root.winfo_id())
    except (tk.TclError, ValueError, TypeError):
        return
    if not wid:
        return

    user32 = ctypes.windll.user32
    GA_ROOT = 2
    hwnd = user32.GetAncestor(wid, GA_ROOT) or wid
    if not hwnd:
        hwnd = wid

    LoadImageW = user32.LoadImageW
    SendMessageW = user32.SendMessageW
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x10
    LR_DEFAULTSIZE = 0x40
    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG = 1

    def _load(w, h, extra=0):
        hicon = LoadImageW(None, path, IMAGE_ICON, w, h, LR_LOADFROMFILE | extra)
        return hicon if hicon else None

    hicon_small = None
    for w, h in ((16, 16), (20, 20), (24, 24), (32, 32)):
        hicon_small = _load(w, h)
        if hicon_small:
            break
    if not hicon_small:
        hicon_small = _load(0, 0, LR_DEFAULTSIZE)

    hicon_big = None
    for w, h in ((64, 64), (48, 48), (40, 40), (32, 32), (256, 256), (128, 128)):
        hicon_big = _load(w, h)
        if hicon_big:
            break
    if not hicon_big:
        hicon_big = _load(0, 0, LR_DEFAULTSIZE)

    try:
        if hicon_small:
            SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
        if hicon_big:
            SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
    except Exception as e:
        logging.debug("WM_SETICON 設定圖示失敗: %s", e)


def apply_tk_window_icon(root: tk.Misc) -> None:
    """套用主視窗圖示。"""
    path = ensure_cmuh_app_icon_path()
    if not path:
        return
    try:
        root.iconbitmap(path)  # type: ignore[attr-defined]
    except Exception as e:
        logging.debug("設定視窗圖示失敗: %s", e)
    _apply_windows_wm_seticon_from_ico(root, path)

    def _redo():
        try:
            _apply_windows_wm_seticon_from_ico(root, path)
        except Exception:
            logging.debug("延遲 WM_SETICON 重試失敗", exc_info=True)

    try:
        root.after(80, _redo)
        root.after(400, _redo)
    except Exception:
        pass
