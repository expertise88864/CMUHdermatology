# -*- coding: utf-8 -*-
"""程式自報 PID（settings/<name>.pid），給 watchdog 的半死救援用。

★[2026-08-04 實機] watchdog 的「找 PID → 強制 kill」在 Windows 11 上完全失效★
實機 log 連續兩小時每 60 秒印同一組警告、什麼都沒做：

    [watchdog] 打卡: mutex 持有但 log 6758s 沒更新 (>300s) — process 半死，嘗試找 PID 強制 kill
    [watchdog] 無法用 WMIC 找到 中國醫皮膚科打卡程式 的 PID；為避免誤殺其他 Python 程序，本輪不執行 broad fallback kill

原本靠「列舉 python 行程 → 比對 cmdline 是否含 .pyw 檔名」找 PID，這條路有三重破口，
在這台機器上三個同時成立：

  1. **WMIC 已被移除**：Windows 11 24H2 起 `wmic.exe` 不再隨附（實測本機
     `Get-Command wmic` 找不到）。
  2. **CommandLine 讀不到**：改用的 PowerShell CIM fallback 對「權限比自己高的行程」
     回傳【空字串】CommandLine（實測本機非提權查詢，pythonw 的 CommandLine 全空）。
  3. **cmdline 根本不含關鍵字**：實機那個 autoclock 的 cmdline 是
     `pythonw.exe ...\\src\\autoclock.py`，而關鍵字是啟動器檔名
     `中國醫皮膚科打卡程式`——不論前兩項是否成立，比對都必然落空。

三者都是「間接推測身分」的必然脆弱點。PID 檔是直接事實：**行程自己說我是誰、PID 多少**，
不需要列舉、不需要 cmdline、不受提權與啟動方式影響。

安全性：讀回來的 PID 一律驗活著且是 python 系行程（PID 會被作業系統重用，
不驗就可能誤殺別人）；驗不過就當作沒有這個檔，退回原本的 cmdline 路徑。
"""
from __future__ import annotations

import logging
import os

from cmuh_common.paths import get_settings_dir

_PY_NAMES = ("python.exe", "pythonw.exe", "python", "pythonw")


def pid_file_path(name: str) -> str:
    """name 用英數（如 "autoclock"/"consult_query"）→ settings/<name>.pid。"""
    return os.path.join(get_settings_dir(), f"{name}.pid")


def write_pid_file(name: str) -> bool:
    """行程啟動時自報 PID。失敗只記 log（自報失敗不該擋住程式啟動）。"""
    try:
        path = pid_file_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # 原子取代，讀者不會看到半截內容
        logging.info("[pidfile] 已自報 PID=%s → %s", os.getpid(), path)
        return True
    except Exception:
        logging.warning("[pidfile] 寫入失敗（不影響本程式運作）", exc_info=True)
        return False


def clear_pid_file(name: str) -> None:
    """正常結束時清掉自己的 PID 檔（留著只是讓下次多做一次驗證，不致命）。"""
    try:
        path = pid_file_path(name)
        if os.path.exists(path) and read_raw_pid(name) == os.getpid():
            os.remove(path)
    except Exception:
        logging.debug("[pidfile] 清除失敗（略過）", exc_info=True)


def read_raw_pid(name: str):
    """只讀數字，不做任何驗證 → int 或 None（壞檔/不存在）。純函式化的 IO。"""
    try:
        with open(pid_file_path(name), encoding="utf-8") as f:
            text = f.read().strip()
        pid = int(text)
        return pid if pid > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def pid_looks_like_python(pid: int) -> bool:
    """PID 仍活著、且是 python 系行程 → 才可以當成「我們的程式」。

    ★PID 會被作業系統重用★ 不驗就可能把別人的行程當成半死的打卡程式殺掉。
    psutil 不可用/查不到 → 回 False（保守：寧可退回舊路徑，也不誤殺）。
    """
    try:
        import psutil                                     # noqa: PLC0415
        proc = psutil.Process(pid)
        return (proc.name() or "").lower() in _PY_NAMES
    except Exception:
        return False


def read_verified_pid(name: str):
    """→ 可以安全 kill 的 PID，或 None。

    None 的情形：沒有 PID 檔、內容壞掉、行程已不在、或不是 python 系行程
    （PID 被重用）。呼叫端據此決定退回原本的 cmdline 查詢路徑。
    """
    pid = read_raw_pid(name)
    if pid is None:
        return None
    if pid == os.getpid():
        return None                    # 自己（watchdog 內嵌在同一支程式時）
    if not pid_looks_like_python(pid):
        logging.info("[pidfile] %s 的 PID %s 已不是 python 行程（結束或 PID 重用）"
                     "→ 不採用", name, pid)
        return None
    return pid
