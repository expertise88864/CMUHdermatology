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


def is_instance_running(mutex_name: str) -> bool:
    """[2026-05-22] 判斷指定 named mutex 是否已被別人持有 — 給 watchdog
    用來偵測目標程式是否還活著。

    為何不只用 psutil cmdline：admin 對 admin process 的 NtQueryInformationProcess
    在長 uptime 後偶發 access denied → cmdline 抓不到 → watchdog 誤判程式
    死掉 → 啟新 instance → 撞 mutex 跳「已在執行中」對話框（user 看到每 30
    秒一次的 popup）。Mutex 偵測完全跳過 cmdline，不受該限制。

    機制：CreateMutexW 同名 mutex：
      - 已存在: 取得既有 handle，GetLastError = ERROR_ALREADY_EXISTS (183)，ref count++
      - 不存在: 創建新 mutex，GetLastError = 0
    我們立刻 CloseHandle 釋放自己取得的 handle，ref count 回到原本，不影響
    其他 process。

    回傳 True = mutex 已被持有 (程式還在跑)
    回傳 False = 沒人持有 / 偵測失敗（保險側讓 caller 走 fallback）
    """
    if not mutex_name:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        last_err = ctypes.GetLastError()
        # 不論是否已存在，我們都立刻 CloseHandle (我們只是偵測，不想 own)
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
        return last_err == _ERROR_ALREADY_EXISTS
    except Exception:
        logging.debug("is_instance_running 偵測例外 (%s)", mutex_name, exc_info=True)
        return False
