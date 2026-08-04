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

──────────────────────────────────────────────────────────────────────────────
★[2026-08-04 外審 P1-07] 只比對「是不是 python 行程」擋不住 PID 重用★

前一版的 docstring 三處都宣稱「PID 會被作業系統重用，不驗就可能誤殺別人」，而實際
驗證只有 `proc.name() in ("python.exe", "pythonw.exe", ...)`。那只能濾掉**非 Python**
的行程 —— 可是這台機器上六支 CMUH 程式**全都是 pythonw.exe**。所以：

    程式崩潰 → PID 檔沒清掉 → 那個 PID 被作業系統配給【自家另一支程式】
    → name 檢查通過 → watchdog 對它下 `taskkill /F /T`

宣稱擋住了，實際沒有。**這個結果不是「找不到而不動作」，是強殺無關的自家程式。**

真正能建立身分的是**行程建立時間**：被重用的 PID 一定屬於「後來才建立」的行程，
建立時間不可能與當初記下的相同。實測本機 397 個行程（非提權 Python）：

    create_time()  成功 397 / 失敗 0     ← 最可靠，正是我們需要的那個
    name()         成功 397 / 失敗 0
    exe()          成功 396 / 失敗 1
    cmdline()      成功 393 / 失敗 4     ← 印證上面破口 2

所以 PID 檔改存 JSON，驗證改成：

    必要（任一不符就不採用）：schema、app_id、pid 仍活著、
                             ★create_time 相符★、name 是 python 系
    加強（讀得到才比，讀不到不否決）：executable 路徑

`exe()` 只做加強是刻意的：實測它會失敗（本機 1 次），若當成必要條件，就會在讀不到
的機器上退回那條壞掉的 cmdline 路徑 —— 把這個功能修回它原本要解決的問題。
身分已經由 pid + create_time 決定，exe 是縱深防禦。

★沒有加 nonce★：外審建議存 run_nonce。但 nonce 寫在檔案裡**無從與活著的行程對照**
（讀不到別人的行程記憶體），它不會增加任何鑑別力。與其多一個看起來嚴謹、實際上是
裝飾的欄位，不如三個真的驗得到的檢查 —— 宣稱要對得上實作。

**舊格式（純數字）一律不採用**：那種檔案沒有 create_time，無從驗證身分。此時退回
原本的 cmdline 路徑（＝這個模組出現以前的行為：可能找不到，但不會殺錯）。程式下次
啟動就會自報成新格式，所以這只是升級後到重啟前的短暫狀態。
"""
from __future__ import annotations

import json
import logging
import os
import sys

from cmuh_common.paths import get_settings_dir

_PY_NAMES = ("python.exe", "pythonw.exe", "python", "pythonw")
_SCHEMA = 1
# 建立時間的比對容差。psutil 對同一行程回傳的值是穩定的，JSON round-trip 也不失真；
# 容差只是不想讓浮點表示的最後一位決定「要不要強殺一支程式」。
# 真的被重用時，兩個行程的建立時間相差【好幾秒以上】（前者死了後者才生得出來）。
_CREATE_TIME_TOLERANCE_SEC = 0.05


def pid_file_path(name: str) -> str:
    """name 用英數（如 "autoclock"/"consult_query"）→ settings/<name>.pid。"""
    return os.path.join(get_settings_dir(), f"{name}.pid")


def _own_create_time():
    """本行程的建立時間。psutil 不可用 → None（此時無法自報可驗證的身分）。"""
    try:
        import psutil                                     # noqa: PLC0415
        return float(psutil.Process(os.getpid()).create_time())
    except Exception:
        return None


def write_pid_file(name: str) -> bool:
    """行程啟動時自報身分。失敗只記 log（自報失敗不該擋住程式啟動）。"""
    try:
        create_time = _own_create_time()
        if create_time is None:
            # 沒有 create_time 的檔案讀回來也不會被採用（無從驗身分）——
            # 照樣寫下去沒有意義，而且會讓人以為自報成功了。
            logging.warning("[pidfile] 取不到本行程建立時間（psutil 不可用）"
                            "→ 不自報 PID；watchdog 會退回 cmdline 比對")
            return False
        payload = {
            "schema": _SCHEMA,
            "app_id": name,
            "pid": os.getpid(),
            "create_time": create_time,
            "executable": os.path.abspath(sys.executable or ""),
        }
        path = pid_file_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())       # "w" 是可寫入 handle → fsync 真的有效
        os.replace(tmp, path)          # 原子取代，讀者不會看到半截內容
        logging.info("[pidfile] 已自報 PID=%s（建立時間 %.3f）→ %s",
                     payload["pid"], create_time, path)
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


def _load_record(name: str):
    """讀出 PID 檔的內容 → dict、或 None（不存在／壞掉／舊格式）。"""
    try:
        with open(pid_file_path(name), encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None            # 舊格式（純數字）或壞檔 → 這裡不認得
    return data if isinstance(data, dict) else None


def read_raw_pid(name: str):
    """只讀 PID 數字，不做任何驗證 → int 或 None（壞檔/不存在）。

    新舊格式都讀得出來（`clear_pid_file` 靠它認出「這個檔是不是我的」）。
    ★這個函式的結果不可以直接拿去 kill★——驗證請走 `read_verified_pid`。
    """
    rec = _load_record(name)
    if rec is not None:
        pid = rec.get("pid")
        return pid if isinstance(pid, int) and pid > 0 else None
    try:                       # 舊格式：整個檔就是一個數字
        with open(pid_file_path(name), encoding="utf-8") as f:
            pid = int(f.read().strip())
        return pid if pid > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def pid_looks_like_python(pid: int) -> bool:
    """PID 仍活著、且是 python 系行程。

    ★這一項【不足以】建立身分★——本機六支 CMUH 程式全是 pythonw.exe，PID 被其中
    另一支重用時這裡照樣通過。真正的判準是 `_identity_matches` 的建立時間比對。
    保留它是因為它便宜、而且能擋掉「PID 被完全無關的程式拿走」這種最明顯的情況。
    """
    try:
        import psutil                                     # noqa: PLC0415
        proc = psutil.Process(pid)
        return (proc.name() or "").lower() in _PY_NAMES
    except Exception:
        return False


def _identity_matches(pid: int, rec: dict) -> bool:
    """★防 PID 重用的核心★ 建立時間相符才是同一個行程。

    被重用的 PID 必然屬於「後來才建立」的行程，建立時間不可能與當初記下的相同。
    """
    try:
        import psutil                                     # noqa: PLC0415
        proc = psutil.Process(pid)
    except Exception:
        return False
    want = rec.get("create_time")
    if not isinstance(want, (int, float)):
        return False           # 沒記建立時間 → 無從驗身分 → 不採用
    try:
        got = float(proc.create_time())
    except Exception:
        # create_time 實測從不失敗；真的失敗就是驗不了 → 保守不採用
        logging.info("[pidfile] 讀不到 PID %s 的建立時間 → 不採用", pid)
        return False
    if abs(got - float(want)) > _CREATE_TIME_TOLERANCE_SEC:
        logging.warning("[pidfile] PID %s 的建立時間不符（記錄 %.3f、實際 %.3f）"
                        "→ ★PID 已被重用★，不採用", pid, want, got)
        return False
    # executable 只做加強：讀不到不否決（實測會失敗，當成必要條件會讓這個功能
    # 退回它本來要解決的問題）。身分已由 pid + create_time 決定。
    want_exe = str(rec.get("executable") or "")
    if want_exe:
        try:
            got_exe = os.path.abspath(proc.exe() or "")
        except Exception:
            got_exe = ""
        if got_exe and os.path.normcase(got_exe) != os.path.normcase(want_exe):
            logging.warning("[pidfile] PID %s 的執行檔不符（記錄 %s、實際 %s）"
                            "→ 不採用", pid, want_exe, got_exe)
            return False
    return True


def read_verified_pid(name: str):
    """→ 可以安全 kill 的 PID，或 None。

    None 的情形：沒有 PID 檔、內容壞掉、★舊格式（無從驗身分）★、schema 不認得、
    app_id 不符、行程已不在、不是 python 系行程、或★建立時間不符（PID 被重用）★。
    呼叫端據此決定退回原本的 cmdline 查詢路徑。
    """
    rec = _load_record(name)
    if rec is None:
        if read_raw_pid(name) is not None:
            logging.warning("[pidfile] %s 的 PID 檔是舊格式（只有數字，沒有建立"
                            "時間）→ 無從驗證身分，不採用；程式下次啟動會自動"
                            "改寫成新格式", name)
        return None
    if rec.get("schema") != _SCHEMA:
        logging.warning("[pidfile] %s 的 PID 檔 schema 不認得（%r）→ 不採用",
                        name, rec.get("schema"))
        return None
    if rec.get("app_id") != name:
        logging.warning("[pidfile] %s 的 PID 檔記的是別支程式（app_id=%r）→ 不採用",
                        name, rec.get("app_id"))
        return None
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    if pid == os.getpid():
        return None                    # 自己（watchdog 內嵌在同一支程式時）
    if not pid_looks_like_python(pid):
        logging.info("[pidfile] %s 的 PID %s 已不是 python 行程（結束或 PID 重用）"
                     "→ 不採用", name, pid)
        return None
    if not _identity_matches(pid, rec):
        return None
    return pid
