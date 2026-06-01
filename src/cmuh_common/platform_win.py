# -*- coding: utf-8 -*-
"""Windows 平台工具。搬自原主程式 line 672-705、8748-8763。"""
import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass

# GetSystemMetrics 索引（multi-monitor 用）
_SM_CXSCREEN = 0          # 主螢幕寬（實體像素）
_SM_CYSCREEN = 1          # 主螢幕高
_SM_CMONITORS = 80        # 螢幕數量
_SM_XVIRTUALSCREEN = 76   # 虛擬桌面左上角 X（副螢幕在左/上方時為負）
_SM_YVIRTUALSCREEN = 77   # 虛擬桌面左上角 Y
_SM_CXVIRTUALSCREEN = 78  # 虛擬桌面總寬（含所有螢幕）
_SM_CYVIRTUALSCREEN = 79  # 虛擬桌面總高
_SHELL_EXECUTE_SUCCESS_MIN = 32
_MONITORINFOF_PRIMARY = 0x00000001
_DISPLAY_DEVICE_MIRRORING_DRIVER = 0x00000008
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010


@dataclass(frozen=True, slots=True)
class MonitorRect:
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False

    @property
    def area(self) -> int:
        return self.width * self.height


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _shell_execute_succeeded(result) -> bool:
    try:
        return int(result) > _SHELL_EXECUTE_SUCCESS_MIN
    except (TypeError, ValueError):
        return False


def _show_admin_relaunch_error(result) -> None:
    message = (
        "無法以系統管理員身分重新啟動程式。\n\n"
        "請在 UAC 視窗按「是」，或右鍵程式後選擇「以系統管理員身分執行」。\n"
        f"Windows 錯誤碼：{result}"
    )
    try:
        ctypes.windll.user32.MessageBoxW(
            0, message, "程式啟動失敗", 0x10 | 0x1000,
        )
    except Exception:
        logging.error(message)


def run_as_admin() -> None:
    """若非管理員則以 UAC 提權重啟（會 sys.exit(0) 結束本進程）。"""
    if is_admin():
        return
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, _admin_relaunch_params(sys.argv), None, 1
        )
    except Exception as e:
        logging.error("run_as_admin 失敗: %s", e)
        _show_admin_relaunch_error(type(e).__name__)
        return
    if not _shell_execute_succeeded(result):
        logging.error("run_as_admin ShellExecuteW 失敗: %s", result)
        _show_admin_relaunch_error(result)
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


def _display_device_is_mirror_driver(user32, device_name: str) -> bool:
    """Return true only when Windows explicitly marks a display as mirrored."""
    enum_devices = getattr(user32, "EnumDisplayDevicesW", None)
    if not callable(enum_devices):
        return False
    try:
        device = _DISPLAY_DEVICEW()
        device.cb = ctypes.sizeof(device)
        if not enum_devices(device_name, 0, ctypes.byref(device), 0):
            return False
        return bool(int(device.StateFlags) & _DISPLAY_DEVICE_MIRRORING_DRIVER)
    except Exception:
        return False


def _display_device_has_physical_monitor_id(user32, device_name: str) -> bool | None:
    """Return physical PnP status when the driver exposes a child monitor ID."""
    enum_devices = getattr(user32, "EnumDisplayDevicesW", None)
    if not callable(enum_devices):
        return None
    try:
        device = _DISPLAY_DEVICEW()
        device.cb = ctypes.sizeof(device)
        if not enum_devices(device_name, 0, ctypes.byref(device), 0):
            return None
        device_id = str(device.DeviceID or "").upper()
        if not device_id:
            return None
        return device_id.startswith("MONITOR\\")
    except Exception:
        return None


def get_active_physical_monitors() -> list[MonitorRect]:
    """Enumerate active desktop monitors, excluding mirror display drivers."""
    if os.name != "nt":
        return []
    try:
        user32 = ctypes.windll.user32
        callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
            wintypes.BOOL,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )
        monitors = []

        def _collect(hmonitor, _hdc, _rect, _data):
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(info)
            if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                return True
            if _display_device_is_mirror_driver(user32, info.szDevice):
                return True
            if _display_device_has_physical_monitor_id(user32, info.szDevice) is False:
                return True
            rect = info.rcMonitor
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width > 0 and height > 0:
                monitors.append(MonitorRect(
                    left=int(rect.left),
                    top=int(rect.top),
                    width=width,
                    height=height,
                    is_primary=bool(int(info.dwFlags) & _MONITORINFOF_PRIMARY),
                ))
            return True

        callback = callback_type(_collect)
        if not user32.EnumDisplayMonitors(None, None, callback, 0):
            return []
        return sorted(
            monitors,
            key=lambda item: (not item.is_primary, item.left, item.top),
        )
    except Exception:
        logging.debug("Unable to enumerate active physical monitors", exc_info=True)
        return []


def choose_preferred_monitor(monitors) -> MonitorRect | None:
    """Prefer the largest real secondary monitor; otherwise use primary."""
    rows = [m for m in monitors if isinstance(m, MonitorRect) and m.area > 0]
    if not rows:
        return None
    secondary = [m for m in rows if not m.is_primary]
    candidates = secondary or [m for m in rows if m.is_primary] or rows
    return sorted(candidates, key=lambda m: (-m.area, m.left, m.top))[0]


def get_preferred_monitor_rect() -> MonitorRect | None:
    """Return the preferred monitor for opening the main desktop window."""
    return choose_preferred_monitor(get_active_physical_monitors())


def _tk_toplevel_hwnd(root) -> int:
    """Return Tk's outermost HWND so SetWindowPos moves the framed window."""
    hwnd = int(root.winfo_id())
    if os.name != "nt":
        return hwnd
    try:
        user32 = ctypes.windll.user32
        for _ in range(8):
            parent = int(user32.GetParent(hwnd) or 0)
            if not parent:
                break
            hwnd = parent
    except Exception:
        logging.debug("Unable to resolve Tk top-level HWND", exc_info=True)
    return hwnd


def move_tk_window_to_monitor(root, monitor: MonitorRect) -> bool:
    """Move a Tk window to exact virtual-desktop coordinates."""
    try:
        root.update_idletasks()
        if os.name == "nt":
            hwnd = _tk_toplevel_hwnd(root)
            if hwnd and ctypes.windll.user32.SetWindowPos(
                hwnd,
                0,
                monitor.left,
                monitor.top,
                monitor.width,
                monitor.height,
                _SWP_NOZORDER | _SWP_NOACTIVATE,
            ):
                return True
    except Exception:
        logging.debug("Unable to position Tk window with SetWindowPos", exc_info=True)
    try:
        root.geometry(
            f"{monitor.width}x{monitor.height}"
            f"{monitor.left:+d}{monitor.top:+d}"
        )
        return True
    except Exception:
        logging.debug("Unable to position Tk window with geometry", exc_info=True)
        return False


def place_tk_window_on_preferred_monitor(
    root,
    *,
    fallback_geometry: str = "1280x720",
) -> MonitorRect | None:
    """Move a Tk window to a real secondary display when available and maximize."""
    monitor = get_preferred_monitor_rect()
    moved = monitor is not None and move_tk_window_to_monitor(root, monitor)
    if not moved:
        try:
            root.geometry(fallback_geometry)
        except Exception:
            logging.debug("Unable to apply fallback Tk geometry", exc_info=True)
    try:
        if root.state() != "withdrawn":
            root.state("zoomed")
    except Exception:
        logging.debug("Unable to maximize Tk window", exc_info=True)
    return monitor


def get_monitor_count() -> int:
    """螢幕數量（偵測不到時回 1）。"""
    if os.name != 'nt':
        return 1
    monitors = get_active_physical_monitors()
    if monitors:
        return len(monitors)
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
