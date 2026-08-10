# -*- coding: utf-8 -*-
"""Tk 視窗圖示套用。搬自原主程式 line 509-585。

Tk 的 iconbitmap 在 Windows 常只套用 16x16，工作列/Alt+Tab 依 WM_SETICON 取大圖。
本模組同時套用兩者，並延遲重送 WM_SETICON 處理 Tk 後續重繪覆寫的情況。
"""
import ctypes
import logging
import os
import threading
import tkinter as tk

from cmuh_common.icons import ensure_cmuh_app_icon_path

# ★[2026-08-10 批次SD #8] 我們自己載的 HICON 要自己釋放★
# `LoadImageW(..., LR_LOADFROMFILE)`(無 LR_SHARED)回傳的是 owned handle。
# 舊版每次套用載 2 個、加上兩次延遲 redo = 每開一個視窗洩 6 個 USER 物件,
# 高頻開縮寫編輯器等 Toplevel 會讓 handle 計數單向上升,最後建不出圖示/
# 視窗/對話框。
# ★只釋放【自己上一次載的】★ WM_SETICON 回傳的「前一個」可能是 Tk 自己
# 管理的 handle(iconbitmap 也走 WM_SETICON)—— 摧毀別人的 handle 比洩漏
# 更糟。所以用 per-hwnd 登記表:設好新的之後,釋放我們上一次為同一個
# hwnd 載的那一對。
_owned_icons: dict = {}          # hwnd -> (hicon_small, hicon_big)
_owned_icons_lock = threading.Lock()
_OWNED_ICONS_MAX = 64


def _destroy_icons(*handles) -> None:
    for h in handles:
        if not h:
            continue
        try:
            ctypes.windll.user32.DestroyIcon(h)
        except Exception:
            logging.debug("DestroyIcon 失敗(忽略)", exc_info=True)


def _remember_owned(hwnd, small, big) -> None:
    """登記這一輪載的 handle,並釋放上一輪為同一個 hwnd 載的。"""
    with _owned_icons_lock:
        prev = _owned_icons.pop(hwnd, None)
        _owned_icons[hwnd] = (small, big)
        if len(_owned_icons) > _OWNED_ICONS_MAX:
            # 最舊的視窗多半早就 destroy 了 —— 釋放其 handle 後除名。
            oldest = next(iter(_owned_icons))
            old_pair = _owned_icons.pop(oldest)
            _destroy_icons(*old_pair)
    if prev:
        _destroy_icons(*prev)


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
        # [批次SD #8] 設好新的 → 釋放我們上一輪為這個 hwnd 載的那一對。
        _remember_owned(int(hwnd), hicon_small, hicon_big)
    except Exception as e:
        logging.debug("WM_SETICON 設定圖示失敗: %s", e)
        _destroy_icons(hicon_small, hicon_big)


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
