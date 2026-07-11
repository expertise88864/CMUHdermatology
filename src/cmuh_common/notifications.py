# -*- coding: utf-8 -*-
"""Windows 通知工具。搬自原主程式 show_windows_notification (line 683-691)。"""
import ctypes
import logging
import threading


def show_windows_notification(title: str, message: str) -> None:
    """跳出最上層 MessageBox + 警示音。

    [IF-03 注意] MessageBoxW 會【阻塞】到使用者按掉。切勿在 health monitor 等「必須持續 tick」的
    背景監看緒 inline 呼叫——無人在場按掉 → 監看緒卡死不再 tick → RAM 自動重啟保險絲失效。
    那種場景改用 show_windows_notification_async(丟到 daemon thread、呼叫端立即返回)。
    """
    try:
        logging.info("*** Attempting to show notification: %s ***", title)
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        # 0x40 ICONINFORMATION | 0x40000 MB_TOPMOST
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x40000)
    except Exception as e:
        logging.error("通知顯示失敗: %s", e)


def show_windows_notification_async(title: str, message: str) -> None:
    """[IF-03] show_windows_notification 的非阻塞版:把阻塞式 MessageBox 丟到 daemon thread,呼叫端
    立即返回。給 health monitor 等『不能因為等使用者按掉而停下來 tick』的背景緒用(否則保險絲失效)。"""
    try:
        threading.Thread(
            target=show_windows_notification, args=(title, message),
            name="win-notify", daemon=True).start()
    except Exception as e:
        logging.error("非阻塞通知啟動失敗: %s", e)


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
