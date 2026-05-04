# -*- coding: utf-8 -*-
"""Windows 通知工具。搬自原主程式 show_windows_notification (line 683-691)。"""
import ctypes
import logging


def show_windows_notification(title: str, message: str) -> None:
    """跳出最上層 MessageBox + 警示音。"""
    try:
        logging.info("*** Attempting to show notification: %s ***", title)
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        # 0x40 ICONINFORMATION | 0x40000 MB_TOPMOST
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x40000)
    except Exception as e:
        logging.error("通知顯示失敗: %s", e)


def show_winotify_toast(title: str, message: str, app_id: str = "CMUH AutoClock") -> bool:
    """winotify 系統匣彈窗（用於打卡程式背景通知）；失敗回 False 由呼叫端 fallback。"""
    try:
        from winotify import Notification, audio
        toast = Notification(app_id=app_id, title=title, msg=message)
        toast.set_audio(audio.Default, loop=False)
        toast.show()
        return True
    except Exception as e:
        logging.debug("winotify 通知失敗: %s", e)
        return False
