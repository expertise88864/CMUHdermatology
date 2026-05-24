# -*- coding: utf-8 -*-
"""Windows 平台工具。搬自原主程式 line 672-705、8748-8763。"""
import ctypes
import logging
import os
import subprocess
import sys


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
