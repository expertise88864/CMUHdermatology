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


#: `acquire_single_instance` 的三態(外審排班 P2-03)。
#: ★「拿到了」與「不知道有沒有人拿著」不是同一件事★:mutex API 壞掉時
#: 舊介面一律回 True(= 拿到了),呼叫端因此把「查不出來」當成「安全」。
#: 排班程式尤其在意:它是整批 whole-file writer + 一個 git working tree,
#: 兩個 instance 同時跑比一般 UI app 危險。
#: ★一定要用模組 logger,不可以用 module-level `logging.warning(...)`★
#: (外審 R3-P2-04 R1 P1-2):後者在 root 還沒有 handler 時會★隱式呼叫
#: `basicConfig()`★裝一個 stderr handler —— 而各程式的 `setup_logging()`
#: 又是靠 `basicConfig` 裝檔案 handler 的,「已經有 handler 就整個不做事」。
#: 結果:單例判定發生在 logging 設定之前 → 檔案 handler 永遠裝不上 →
#: log 檔一行都不會寫 → watchdog 把健康的行程判成 log stale 反覆重啟。
#: `logging.getLogger(__name__)` 不會碰 basicConfig。
_log = logging.getLogger(__name__)

INSTANCE_ACQUIRED = "acquired"
INSTANCE_ALREADY_RUNNING = "already_running"
INSTANCE_UNKNOWN = "unknown"


def acquire_single_instance(mutex_name: str, retry_sec: float = 1.5) -> str:
    """取得單例 mutex,回三態之一(見上面的常數)。

    `ensure_single_instance` 是它的相容包裝:UNKNOWN 仍回 True(維持既有
    fail-open 行為,不改動既有呼叫端);★在意「不知道」的呼叫端改用這個函式★。
    """
    state = {"value": INSTANCE_UNKNOWN}
    ok = ensure_single_instance(mutex_name, retry_sec, _state_out=state)
    if ok and state["value"] == INSTANCE_UNKNOWN:
        return INSTANCE_UNKNOWN
    return INSTANCE_ACQUIRED if ok else INSTANCE_ALREADY_RUNNING


def startup_instance_state(mutex_name: str, app_id: str = "") -> str:
    """開機單例判定:三態,而且 UNKNOWN 時★再走一條不依賴 mutex API 的路★。

    (外審 R3-P2-04 R1 P1-1)`CreateMutexW` 壞掉時,原本只能回「不知道」而各程式
    一律照常執行。但打卡/會診都會★自報 PID★(`pidfile.write_pid_file`),而那份
    紀錄的驗證(行程還在、是 python 系、建立時間相符 → 沒有 PID 重用)
    ★完全不經過 mutex API★ —— 查得到活著的第二份,就是確定「已在執行中」。

    ★這不是「完全擋得住」★:pidfile 可能是舊格式、psutil 不可用、或對方還沒寫
    到那一步 —— 那時仍然回 UNKNOWN,由呼叫端依自己的處置決定。誠實標註,
    因為打卡那條路的重複代價很高(repo 內有「清理重複打卡程式.ps1」這種
    現場工具就是證據),不可以宣稱一個做不到的保證。
    """
    state = acquire_single_instance(mutex_name)
    if state != INSTANCE_UNKNOWN or not app_id:
        return state
    try:
        from cmuh_common.pidfile import read_verified_pid  # noqa: PLC0415
        other = read_verified_pid(app_id)
    except Exception:
        _log.warning("[單例] mutex 查不出來,pidfile 這條路也失敗", exc_info=True)
        return INSTANCE_UNKNOWN
    if other is not None:
        _log.error("[單例] mutex 機制異常,但 pidfile 查到另一個 %s 還活著"
                   "（PID=%s）→ 視為已在執行中", app_id, other)
        return INSTANCE_ALREADY_RUNNING
    return INSTANCE_UNKNOWN


def ensure_single_instance(mutex_name: str, retry_sec: float = 1.5,
                           _state_out: "dict | None" = None) -> bool:
    """Return True only for the process that successfully creates the mutex.

    retry_sec：看到 ERROR_ALREADY_EXISTS 時，短暫重試的總秒數（每 0.25s 一次）。
    用於「重啟」競態：新 instance 可能比舊 instance 釋放 mutex 早一步啟動，若不
    重試會直接判定『已在執行中』而退出 → 重啟靜默失敗、程式整個消失。重試給舊
    instance 一點時間釋放。正常雙開情境最多多等 retry_sec 才顯示提示，可接受。
    """
    def _mark(value: str) -> None:
        if _state_out is not None:
            _state_out["value"] = value

    if not mutex_name:
        _mark(INSTANCE_ACQUIRED)
        return True
    with _instance_mutex_lock:
        if mutex_name in _instance_mutex_handles:
            _mark(INSTANCE_ACQUIRED)
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
                    _mark(INSTANCE_ALREADY_RUNNING)
                    return False

                if not handle:
                    # ★這裡【不知道】有沒有別人拿著★:回 True 是舊介面的
                    #   fail-open 相容行為,狀態要如實標成 UNKNOWN。
                    _log.warning("CreateMutexW failed for %s (err=%s)",
                                 mutex_name, last_err)
                    _mark(INSTANCE_UNKNOWN)
                    return True

                _instance_mutex_handles[mutex_name] = handle
                _mark(INSTANCE_ACQUIRED)
                if attempt:
                    _log.info(
                        "ensure_single_instance: 取得 mutex %s（重試 %d 次後）",
                        mutex_name, attempt)
                return True
            except Exception as exc:
                # 同上:mutex 機制本身壞了 → 不知道,不是「安全」。
                _log.warning("ensure_single_instance failed for %s: %s",
                             mutex_name, exc)
                _mark(INSTANCE_UNKNOWN)
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
        _log.debug("is_instance_running failed for %s", mutex_name,
                   exc_info=True)
        return False
