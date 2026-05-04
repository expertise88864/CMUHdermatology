# -*- coding: utf-8 -*-
"""Windows Mutex 單例機制。搬自原打卡程式 line 345-365。"""
import ctypes
import logging
from ctypes import wintypes


_ERROR_ALREADY_EXISTS = 183
_instance_mutex_handle: int | None = None


def ensure_single_instance(mutex_name: str) -> bool:
    """確保同名 Mutex 只有一個進程持有。回傳 True 表示成功取得（首次啟動）。

    若回傳 False，呼叫端應顯示「程式已在執行中」並結束。
    """
    global _instance_mutex_handle
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        last_err = ctypes.GetLastError()
        if not handle:
            logging.warning("建立 Mutex 失敗 (err=%s)", last_err)
            return True  # 安全側：失敗就放行
        if last_err == _ERROR_ALREADY_EXISTS:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
            return False
        _instance_mutex_handle = handle
        return True
    except Exception as e:
        logging.warning("ensure_single_instance 例外: %s", e)
        return True


def release_single_instance() -> None:
    """進程結束前釋放 Mutex（atexit 用）。"""
    global _instance_mutex_handle
    if _instance_mutex_handle:
        try:
            ctypes.windll.kernel32.CloseHandle(_instance_mutex_handle)
        except Exception:
            pass
        _instance_mutex_handle = None
