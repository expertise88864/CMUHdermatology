# -*- coding: utf-8 -*-
"""Windows named mutex helpers for single-instance apps."""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183
_instance_mutex_handles: dict[str, int] = {}


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


def ensure_single_instance(mutex_name: str) -> bool:
    """Return True only for the process that successfully creates the mutex."""
    if not mutex_name:
        return True
    if mutex_name in _instance_mutex_handles:
        return True

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
            return False

        if not handle:
            logging.warning("CreateMutexW failed for %s (err=%s)", mutex_name, last_err)
            return True

        _instance_mutex_handles[mutex_name] = handle
        return True
    except Exception as exc:
        logging.warning("ensure_single_instance failed for %s: %s", mutex_name, exc)
        return True


def release_single_instance() -> None:
    """Release all mutex handles held by this process."""
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
