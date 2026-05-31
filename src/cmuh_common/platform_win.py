# -*- coding: utf-8 -*-
"""Windows 平台工具。搬自原主程式 line 672-705、8748-8763。"""
import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes

# GetSystemMetrics 索引（multi-monitor 用）
_SM_CXSCREEN = 0          # 主螢幕寬（實體像素）
_SM_CYSCREEN = 1          # 主螢幕高
_SM_CMONITORS = 80        # 螢幕數量
_SM_XVIRTUALSCREEN = 76   # 虛擬桌面左上角 X（副螢幕在左/上方時為負）
_SM_YVIRTUALSCREEN = 77   # 虛擬桌面左上角 Y
_SM_CXVIRTUALSCREEN = 78  # 虛擬桌面總寬（含所有螢幕）
_SM_CYVIRTUALSCREEN = 79  # 虛擬桌面總高


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_as_admin() -> None:
    """若非管理員則以 UAC 提權重啟（會 sys.exit(0) 結束本進程）。"""
    if is_admin():
        return
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, _admin_relaunch_params(sys.argv), None, 1
        )
    except Exception as e:
        logging.error("run_as_admin 失敗: %s", e)
        return
    sys.exit(0)


def _admin_relaunch_params(argv=None) -> str:
    """Build ShellExecuteW params with Windows-safe quoting."""
    return subprocess.list2cmdline(list(argv if argv is not None else sys.argv))


def set_dpi_awareness() -> None:
    """讓視窗在高 DPI 螢幕上不模糊。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def get_monitor_count() -> int:
    """螢幕數量（偵測不到時回 1）。"""
    if os.name != 'nt':
        return 1
    try:
        return max(1, int(ctypes.windll.user32.GetSystemMetrics(_SM_CMONITORS)))
    except Exception:
        return 1


def get_primary_monitor_size() -> tuple:
    """主螢幕實體像素 (width, height)。偵測失敗時回 (0, 0)。

    注意：在多螢幕環境，這回的是「主螢幕」而非虛擬桌面 —
    座標式自動化(寫死 1920×1080 座標、像素判斷)都以主螢幕為基準。
    """
    if os.name != 'nt':
        return (0, 0)
    try:
        u = ctypes.windll.user32
        return (int(u.GetSystemMetrics(_SM_CXSCREEN)),
                int(u.GetSystemMetrics(_SM_CYSCREEN)))
    except Exception:
        return (0, 0)


def get_virtual_screen_rect() -> tuple:
    """整個虛擬桌面(涵蓋所有螢幕)的 (left, top, width, height)。

    left/top 在副螢幕位於主螢幕左方/上方時會是「負值」。
    用於需要橫跨所有螢幕的 overlay(例：重開機倒數警示)。
    偵測失敗時退回主螢幕大小。
    """
    if os.name == 'nt':
        try:
            u = ctypes.windll.user32
            x = int(u.GetSystemMetrics(_SM_XVIRTUALSCREEN))
            y = int(u.GetSystemMetrics(_SM_YVIRTUALSCREEN))
            w = int(u.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
            h = int(u.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
            if w > 0 and h > 0:
                return (x, y, w, h)
        except Exception:
            pass
    pw, ph = get_primary_monitor_size()
    return (0, 0, pw or 1920, ph or 1080)


def foreground_window_on_primary() -> bool:
    """前景視窗的中心點是否落在「主螢幕」範圍內。

    座標式自動化(scheduler 的 F3/F4/F10/F11)寫死了主螢幕座標，
    若使用者把醫院系統視窗拖到副螢幕，點擊會打到主螢幕的錯誤位置。
    本函式作為執行前的安全守衛。

    設計為 fail-open：單螢幕、偵測不到、或任何例外，一律回 True(不阻擋)，
    只有在「明確判定前景視窗中心不在主螢幕」時才回 False。
    """
    if os.name != 'nt':
        return True
    try:
        u = ctypes.windll.user32
        if get_monitor_count() <= 1:
            return True
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return True
        rect = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        prim_w, prim_h = get_primary_monitor_size()
        if prim_w <= 0 or prim_h <= 0:
            return True
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        return (0 <= cx < prim_w) and (0 <= cy < prim_h)
    except Exception:
        return True


def set_app_user_model_id(app_id: str = "CMUH.Dermatology.OutpatientTools.1") -> None:
    """Windows 工作列 / Alt+Tab 使用獨立 AppID。"""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_duration() -> float:
    """系統閒置秒數。搬自原主程式 line 1036-1048，供自動重開機判斷。"""
    if os.name != 'nt':
        return 0.0
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return millis / 1000.0
