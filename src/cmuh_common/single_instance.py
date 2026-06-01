# -*- coding: utf-8 -*-
"""Windows named mutex helpers for single-instance apps."""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes

_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183
_instance_mutex_handles: dict[str, int] = {}
_instance_mutex_lock = threading.RLock()


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _set_last_error(value: int) -> None:
    ctypes.set_last_error(value)


def _last_error() -> int:
    return ctypes.get_last_error()


def _configure_create_mutex(kernel32) -> None:
    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE


def ensure_single_instance(mutex_name: str, retry_sec: float = 1.5) -> bool:
    """Return True only for the process that successfully creates the mutex.

    retry_sec：看到 ERROR_ALREADY_EXISTS 時，短暫重試的總秒數（每 0.25s 一次）。
    用於「重啟」競態：新 instance 可能比舊 instance 釋放 mutex 早一步啟動，若不
    重試會直接判定『已在執行中』而退出 → 重啟靜默失敗、程式整個消失。重試給舊
    instance 一點時間釋放。正常雙開情境最多多等 retry_sec 才顯示提示，可接受。
    """
    if not mutex_name:
        return True
    with _instance_mutex_lock:
        if mutex_name in _instance_mutex_handles:
            return True

        deadline = time.monotonic() + max(0.0, retry_sec)
        attempt = 0
        while True:
            try:
                kernel32 = _kernel32()
                _configure_create_mutex(kernel32)
                _set_last_error(0)
                handle = kernel32.CreateMutexW(None, False, mutex_name)
                last_err = _last_error()

                if last_err in (_ERROR_ALREADY_EXISTS, _ERROR_ACCESS_DENIED):
                    if handle:
                        try:
                            kernel32.CloseHandle(handle)
                        except Exception:
                            pass
                    # 只對 ALREADY_EXISTS 重試（重啟競態：舊 instance 正在釋放
                    # mutex）。ACCESS_DENIED 是別的 session/權限持有，重試無意義
                    # → 直接判定為已在執行。
                    if (last_err == _ERROR_ALREADY_EXISTS
                            and time.monotonic() < deadline):
                        attempt += 1
                        time.sleep(0.25)
                        continue
                    return False

                if not handle:
                    logging.warning("CreateMutexW failed for %s (err=%s)", mutex_name, last_err)
                    return True

                _instance_mutex_handles[mutex_name] = handle
                if attempt:
                    logging.info(
                        "ensure_single_instance: 取得 mutex %s（重試 %d 次後）",
                        mutex_name, attempt)
                return True
            except Exception as exc:
                logging.warning("ensure_single_instance failed for %s: %s", mutex_name, exc)
                return True


def release_single_instance() -> None:
    """Release all mutex handles held by this process."""
    with _instance_mutex_lock:
        try:
            kernel32 = _kernel32()
        except Exception:
            kernel32 = None

        for mutex_name, handle in list(_instance_mutex_handles.items()):
            if handle and kernel32 is not None:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
            _instance_mutex_handles.pop(mutex_name, None)


def is_instance_running(mutex_name: str) -> bool:
    """Return True when another process already owns the named mutex."""
    if not mutex_name:
        return False
    try:
        kernel32 = _kernel32()
        _configure_create_mutex(kernel32)
        _set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        last_err = _last_error()
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
        return last_err in (_ERROR_ALREADY_EXISTS, _ERROR_ACCESS_DENIED)
    except Exception:
        logging.debug("is_instance_running failed for %s", mutex_name, exc_info=True)
        return False
