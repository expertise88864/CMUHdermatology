# -*- coding: utf-8 -*-
"""Watchdog 共用核心 — 給 src/main.py (B 內層) 與 src/watchdog_runner.py (C 外層) 共用。

兩個呼叫者：
  - **內層 B**：main.py 啟動時開 daemon thread，每 30s 巡邏，模式 = 'inner'
    跳過 outer_only=true 的程式（例如主程式自己 — 不能自我監看）
  - **外層 C**：schtasks 每 2 分鐘觸發一次 `python watchdog_runner.py --once`
    模式 = 'outer'。檢查所有程式（含主程式）。non-main 程式的 max_stale_sec
    自動乘 outer_threshold_multiplier (預設 1.5)，給 B 優先處理的時間，
    避免 B+C 同時 kill 同一個程式。

雙重保險：B 死了 → C 還在 (2 分鐘內接手)；C 排程被誤刪 → B 還在 (主程式跑就在跑)。
"""
from __future__ import annotations

import contextlib
import csv
import errno as _errno
import json
import locale
import logging
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from cmuh_common.atomic_io import (atomic_write_json, safe_load_json,
                                   safe_load_json_ex)
from cmuh_common.paths import get_settings_dir, pinned_app_dir
from cmuh_common.process_launch import launch_python_script
from cmuh_common.update_policy import suspend_auto_updates

# ─── 路徑 ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
# repo root：src/cmuh_common/.. = src，..再上一層 = root
# ★[批次L L1 外審第 2 輪 P1] 版本化之後 `__file__` 推不出根目錄★
#   版本化時這支檔在 `<app>/versions/<V>/src/cmuh_common/`,推出來的是
#   `<app>/versions/<V>` —— 於是 watchdog 讀到【另一份】設定與鎖，
#   而且到版本目錄底下找六支 `.pyw`(那裡沒有)。
#   ★後果是 watchdog 再也救不回任何一支臨床程式★,而它正是最後一道防線。
#   啟動器釘住的值優先;沒釘住(過渡期、直接跑 src)就照舊。
_ROOT = Path(pinned_app_dir() or _HERE.parent.parent)
SETTINGS_DIR = _ROOT / "settings"
CONFIG_PATH = SETTINGS_DIR / "watchdog_config.json"
LOCK_DIR = SETTINGS_DIR / ".watchdog_locks"


# ─── 預設設定 ────────────────────────────────────────────────────────────
# 【重要】process_match 必須能在 pythonw.exe 的 cmdline 找到。.pyw shim 用
# runpy.run_path("src/foo.py") 動態載入 src/*.py，cmdline 上只有 .pyw 路徑，
# 沒有 src/foo.py 字串。所以 process_match 必須是 .pyw 中文檔名 (cmdline 一定含)。
# psutil 在 Windows 用 UTF-16 取 cmdline，Chinese keyword 安全可比對。
#
# 【required_config_file】v4 新增：本機沒這檔 → 不啟動 (per-machine opt-in)
# 打卡 / 會診查詢 全皮膚科只需「一台」電腦執行，靠對應 config 檔存在與否
# 自動判斷本機是否該跑。沒設定過該功能的電腦不會被打擾。
#
# 【mutex_name】v6 新增 (2026-05-22)：當 psutil 抓不到 admin process 的 cmdline
# (Windows 偶發 access denied) 時，用 named mutex 偵測該程式是否還活著。
# 沒這個的話 watchdog 會每 30s 啟新 instance → 撞 mutex 跳「已在執行中」對話框。
CONFIG_SCHEMA_VERSION = 7

DEFAULT_CONFIG = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    # 【總開關 v5】預設關閉 — 新裝機/沒設定過任何背景程式的電腦完全不會
    # 跑 watchdog。主程式設定頁有勾選 UI 可開啟。
    "master_enabled": False,
    # [v8 2026-05-25 CPU 優化] 30s → 60s — 每次 tick 跑 psutil.process_iter()
    # + WMIC fallback 蠻吃 (200-500ms 跨 process)。consult_query/打卡 max_stale
    # 都 300s，60s tick 仍有 5 次機會偵測卡死，足夠及時 kill+restart。
    "check_interval_sec": 60,
    "heartbeat_log_sec": 300,
    "outer_threshold_multiplier": 1.5,  # outer C 的 max_stale_sec 乘這個倍率
    "action_lock_seconds": 90,          # 任一程式被 kill+restart 後 90s 內不允許再動
    "programs": [
        {
            "name": "會診查詢",
            "log_path": "settings/consult_query.log",
            # [2026-08-04] 半死救援優先讀 settings/<pid_name>.pid（見 pidfile）
            "pid_name": "consult_query",
            "pyw": "中國醫皮膚科會診查詢程式.pyw",
            "process_match": "中國醫皮膚科會診查詢程式",
            "mutex_name": "Local\\CMUH_Skin_ConsultQuery_SingleInstance_v1",
            "max_stale_sec": 180,  # 新版每 60s 一定有 heartbeat
            "enabled": True,
            "outer_only": False,
            "required_config_file": "settings/consult_query_config.json",
        },
        {
            "name": "打卡",
            "log_path": "settings/autoclock.log",
            # [2026-08-04] 半死救援優先讀 settings/<pid_name>.pid（見 pidfile）
            "pid_name": "autoclock",
            "pyw": "中國醫皮膚科打卡程式.pyw",
            "process_match": "中國醫皮膚科打卡程式",
            "mutex_name": "Local\\CMUH_Skin_AutoClock_SingleInstance_v1",
            # [v7 2026-05-22 P1-4] 0→300s — autoclock v45 起每 5s 一定有
            # scheduler_loop heartbeat (last_tick) + scheduler_tick 每分鐘
            # 印 log，180s 內沒 log 就視為半死。原本 0 等於不檢查 log，
            # mutex 仍持有就「視為健在」，跟今天 consult_query 卡死同樣 pattern。
            "max_stale_sec": 300,
            "enabled": True,
            "outer_only": False,
            "required_config_file": "settings/autoclock_config.json",
        },
        {
            "name": "主程式",
            "log_path": "automation_ui.log",
            "pyw": "中國醫皮膚科主程式.pyw",
            "process_match": "中國醫皮膚科主程式",
            "mutex_name": "Local\\CMUH_Skin_Main_SingleInstance_v1",
            "max_stale_sec": 0,
            # 【v3 預設關閉】主程式有 GUI，崩潰使用者立刻看到 (熱鍵失效)，
            # 不需要自動重啟。且外層 C 若誤判沒在跑就 Popen，子程式 single_instance
            # 會拒絕並跳「已在啟動中」對話框，徒增困擾。要重開請手動雙擊 .pyw。
            "enabled": False,
            "outer_only": True,
            "required_config_file": "",  # 主程式不需 config gate
        },
    ],
}


def _default_config_copy() -> dict:
    return json.loads(json.dumps(DEFAULT_CONFIG))


# ─── Schema migration ───────────────────────────────────────────────────
# v1 → v2 (2026-05-19)：process_match 從 "consult_query"/"autoclock"/
# "src\\main.py" 改成 .pyw 中文名稱（cmdline 沒前者，watchdog 永遠找不到 →
# 一直想重啟 → 子程式 single_instance 跳「已在啟動中」對話框）。
_V1_TO_V2_PROCESS_MATCH = {
    "consult_query": "中國醫皮膚科會診查詢程式",
    "autoclock": "中國醫皮膚科打卡程式",
    "src\\main.py": "中國醫皮膚科主程式",
    "src/main.py": "中國醫皮膚科主程式",
}


_V3_TO_V4_REQUIRED_CONFIG = {
    "會診查詢": "settings/consult_query_config.json",
    "打卡": "settings/autoclock_config.json",
    "主程式": "",
}

# v5 → v6 (2026-05-22)：補 mutex_name 欄位 — admin process 長 uptime 後 psutil
# 偶發拿不到 cmdline，watchdog 改用 mutex 偵測 fallback 才不會誤判要重啟。
_V5_TO_V6_MUTEX_NAME = {
    "會診查詢": "Local\\CMUH_Skin_ConsultQuery_SingleInstance_v1",
    "打卡": "Local\\CMUH_Skin_AutoClock_SingleInstance_v1",
    "主程式": "Local\\CMUH_Skin_Main_SingleInstance_v1",
}

# v6 → v7 (2026-05-22)：打卡 max_stale_sec 從 0 改 300 — autoclock v45 起每 5s
# 有 heartbeat，180-300s 沒 log 就是半死狀態。今天 autoclock RLock bug + mutex
# 還在 → 外層 watchdog 永遠回「視為健在」沒救起來。
_V6_TO_V7_MAX_STALE = {
    "打卡": 300,
}

# [D] Crash loop 偵測：per-program 啟動歷史 (timestamp list)
# 若 10 分鐘內超過 5 次啟動 → 暫停該 program 30 分鐘
_RESTART_HISTORY: dict = {}    # name → [timestamps]
_SUSPENDED_UNTIL: dict = {}    # name → suspend_until_timestamp
_CRASH_LOOP_LOCK = threading.Lock()
CRASH_LOOP_WINDOW_SEC = 600       # 10 分鐘
CRASH_LOOP_MAX_RESTARTS = 5       # 內 5 次以上 → 視為 crash loop
CRASH_LOOP_SUSPEND_SEC = 1800     # 暫停 30 分鐘
# [AC-07] crash-loop 啟動歷史落盤路徑。--once 模式每次都是全新行程,記憶體 dict 會歸零 →
# crash-loop 計數永遠累積不起來、防護等於失效。落盤後 daemon 與 --once 共用同一份歷史。
RESTART_HISTORY_PATH = SETTINGS_DIR / ".watchdog_restart_history.json"


def _restart_history_path() -> str:
    return str(RESTART_HISTORY_PATH)


#: [外審 r5 P2] 歷史檔的★世代序號★:每次成功落盤 +1,寫在檔案內容裡(不是 mtime)。
#: 在歷史鎖內 load → +1 → save,所以跨行程單調;與牆上時鐘無關,時鐘回撥不會讓它「在未來」。
_HISTORY_GENERATION = [0]


def _history_timestamp(value) -> float | None:
    """Only finite JSON numbers represent restart/suspension timestamps."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        stamp = float(value)
    except (OverflowError, ValueError):
        return None
    return stamp if math.isfinite(stamp) else None


def _load_restart_history() -> None:
    """[AC-07] 從檔載入啟動歷史/暫停狀態（讓 --once 也能累積 crash-loop 計數）。"""
    try:
        data = safe_load_json(_restart_history_path(), {})
    except Exception:
        return
    if not isinstance(data, dict):
        # 有效 JSON 不一定是歷史物件；保留已知的暫停/計數/世代，不讓 .get
        # 例外中斷整個 tick。後續仍走原本的持鎖記錄與落盤授權流程。
        logging.warning("[watchdog] 啟動歷史格式不是物件 → 保留已知歷史與暫停狀態")
        return
    gen = data.get("generation")
    _HISTORY_GENERATION[0] = int(gen) if isinstance(gen, int) and gen >= 0 else 0
    hist = data.get("history")
    if isinstance(hist, dict):
        _RESTART_HISTORY.clear()
        for name, ts in hist.items():
            if isinstance(ts, list):
                _RESTART_HISTORY[str(name)] = [
                    stamp for t in ts if (stamp := _history_timestamp(t)) is not None]
    susp = data.get("suspended_until")
    if isinstance(susp, dict):
        _SUSPENDED_UNTIL.clear()
        for name, until in susp.items():
            stamp = _history_timestamp(until)
            if stamp is not None:
                _SUSPENDED_UNTIL[str(name)] = stamp


def _save_restart_history() -> Exception | None:
    """[AC-07] 落盤啟動歷史/暫停狀態。回 None=寫成功;回例外物件=寫失敗(已記 log)。

    [R9-§6 外審 r1 P2-2] 舊版回 None 且把例外吞成 debug:寫失敗 = 這次啟動★沒被記下★,
    `--once` 結束記憶體就沒了、daemon 下一輪也可能從舊檔重載蓋掉 → 反覆重啟永遠累積不到
    crash-loop 門檻。呼叫端要看得到失敗,★而且要看得到是哪一種失敗★(權限/磁碟滿是持續
    狀況,sharing violation 是暫時的 —— 處置不同)。
    """
    gen = _HISTORY_GENERATION[0] + 1
    try:
        atomic_write_json(_restart_history_path(),
                          {"history": _RESTART_HISTORY,
                           "suspended_until": _SUSPENDED_UNTIL,
                           "generation": gen})
        _HISTORY_GENERATION[0] = gen         # 成功落盤才算新世代
        return None
    except Exception as e:
        logging.warning("[watchdog] 寫入啟動歷史失敗(%s): %s", _restart_history_path(), e)
        return e


class _RestartHistoryLockBusy(Exception):
    """啟動歷史鎖在 timeout 內拿不到(另一個 watchdog 正在寫)。"""


class _RestartHistoryUnsaved(Exception):
    """這次啟動記錄寫不進磁碟(磁碟滿/ACL/超過重試期的檔案鎖)。"""


#: 啟動歷史鎖的等待上限(s)。測試會把它調小。
RESTART_HISTORY_LOCK_TIMEOUT_SEC = 3.0
_LOCK_DEGRADED_WARNED = [False]   # 鎖檔根本建不出來 → 只警告一次(每行程)


@contextlib.contextmanager
def _restart_history_lock(timeout_sec: float | None = None):
    """[codex P2] 跨行程互斥檔案鎖，序列化 crash-loop 歷史的 read-modify-write，避免
    daemon 與 --once 同時 load-modify-save 造成 lost update（掉某程式的啟動記錄、破壞
    crash-loop 偵測）。O_CREAT|O_EXCL 建鎖檔；拿不到就短暫等；離開刪鎖檔。與歷史檔同目錄。

    [第九輪 §6] ★逾時不再 fail-open★:拿不到鎖就 `raise _RestartHistoryLockBusy`,由
    `_authorize_restart()` 轉成「本 tick 不授權重啟」。理由:少重啟一輪的代價很低
    (下一 tick 再試),而不持鎖的 read-modify-write 會讓 daemon 與 --once 互相蓋掉啟動
    記錄 → crash-loop 計數被低估 → 保護失效。
    ★但「鎖檔根本建不出來」(目錄不可寫等★持續★狀況)仍 fail-open★:那不會自己解除,
    fail-closed 會讓 watchdog 永遠不重啟任何程式(2026-08-05 教訓:fail-closed 前先證明
    狀況會解除);改成據實警告一次,退回舊的不持鎖行為。
    """
    if timeout_sec is None:
        timeout_sec = RESTART_HISTORY_LOCK_TIMEOUT_SEC
    lock_path = _restart_history_path() + ".lock"
    deadline = time.monotonic() + timeout_sec
    fd = -1
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    except Exception:
        pass
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 10:
                    os.remove(lock_path)      # stale（持鎖行程崩潰）→ 搶過來
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                # 逾時 → 本 tick 不授權(FileExistsError 是觸發條件,不是處理錯誤 → from None)
                raise _RestartHistoryLockBusy(lock_path) from None
            time.sleep(0.02)
        except OSError as e:
            # [外審 r1 P2-3] 其他 OSError 可能是★暫時性★的(防毒短暫攔截、Windows sharing
            # violation —— atomic_io 對同類錯誤就是重試)。先重試到 deadline。
            if time.monotonic() < deadline:
                time.sleep(0.02)
                continue
            # [外審 r2 P2-2] 到了 deadline ★依錯誤類型分流★:
            #   * 可確認的持續狀況(權限/路徑/唯讀/磁碟滿)→ fail-open 但不靜音(不會自己解除,
            #     fail-closed 會讓 watchdog 永遠不重啟任何程式);
            #   * sharing/lock violation 與★不認得的★錯誤 → 當「忙」:本 tick 不授權,下輪再試
            #     (不持鎖讀改寫會重現 lost update;少動手一輪的代價低於猜錯持續狀況)。
            if not _persistent_os_error(e):
                raise _RestartHistoryLockBusy(lock_path) from e
            if not _LOCK_DEGRADED_WARNED[0]:
                _LOCK_DEGRADED_WARNED[0] = True
                logging.warning(
                    "[watchdog] 啟動歷史鎖檔在 %.1fs 內都建不出來(%s: %s)→ 視為持續狀況,"
                    "退回不持鎖的讀改寫;daemon 與 --once 同時寫時 crash-loop 計數可能被低估",
                    timeout_sec, lock_path, e)
            break
    try:
        yield
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                os.remove(lock_path)
            except Exception:
                pass


#: `_authorize_restart()` 的三個結論。★「鎖忙」與「crash loop」處置不同★:前者是
#: 「這一輪不知道能不能重啟,下一輪再問」,後者是「知道不能,暫停一段時間」。
RESTART_AUTH_OK = "ok"
RESTART_AUTH_CRASH_LOOP = "crash_loop"
RESTART_AUTH_LOCK_BUSY = "lock_busy"
RESTART_AUTH_HISTORY_UNSAVED = "history_unsaved"   # 這次啟動記不下來 → 本輪不動手

#: 啟動歷史★持續★寫不進去多久之後降級(授權但 WARNING):寫不進去是持續狀況時,
#: fail-closed 會讓 watchdog 永遠不重啟任何程式(2026-08-05 教訓);抑制要有出口。
#: [外審 r2 P2-1] 以「第一次寫失敗的時間」為準而不是行程內的連敗計數 —— `--once` 每兩分鐘
#: 都是全新行程,行程內計數永遠到不了門檻。時間戳落盤到 sidecar(歷史檔本身寫不進去,
#: 不能記在同一個檔);sidecar 也寫不進去就退到 %TEMP%;兩邊都寫不進去 = 機器狀態已壞
#: → 視為持續狀況立即降級。
HISTORY_UNSAVED_DEGRADE_AFTER_SEC = 180.0
#: [外審 r3 P2] ★每個程式一個檔★(檔名帶 name 的 hex):共用一個 JSON 的 read-modify-write
#: 在歷史鎖之外進行,daemon 與 --once 為不同程式寫時會互相蓋掉時間戳。獨立檔 → 寫入是
#: 單次原子寫、清除是刪檔,沒有共用狀態,不需要鎖。
HISTORY_UNSAVED_SIDECAR_PREFIX = ".watchdog_history_unsaved."

#: 可確認為★持續★狀況的 OSError(權限 / 路徑不存在 / 唯讀 / 磁碟滿):不必等,直接當持續。
_PERSISTENT_ERRNOS = frozenset({
    _errno.EACCES, _errno.EPERM, _errno.ENOENT, _errno.ENOTDIR,
    _errno.EROFS, _errno.ENOSPC,
})
#: 已知的★暫時性★ Windows 錯誤碼(sharing violation / lock violation):重試,逾時當「忙」。
_TRANSIENT_WINERRORS = frozenset({32, 33})


def _persistent_os_error(exc: BaseException | None) -> bool:
    """這個錯誤是不是「不會自己解除」的那一種。只認得出來的才回 True;不認得回 False
    (不認得 → 當暫時性處理:少動手一輪的代價低於把持續狀況猜錯)。"""
    if not isinstance(exc, OSError):
        return False
    # ★先看 winerror★:Windows 的 sharing violation(32)會被 Python 對應成 errno EACCES,
    # 也就是自動建成 PermissionError 子類 —— 先看子類會把暫時性錯誤誤判成持續狀況。
    if getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS:
        return False
    if isinstance(exc, PermissionError | FileNotFoundError | NotADirectoryError):
        return True
    return exc.errno in _PERSISTENT_ERRNOS


def _unsaved_sidecar_paths(name: str) -> list:
    """`name` 專屬的時間戳檔候選位置:settings/ 優先;它寫不進去就退到 %TEMP%
    (不同的 ACL/磁碟輪廓)。★每個程式一個檔★,沒有共用的讀改寫。"""
    import tempfile
    fname = HISTORY_UNSAVED_SIDECAR_PREFIX + name.encode("utf-8").hex() + ".json"
    return [os.path.join(get_settings_dir(), fname),
            os.path.join(tempfile.gettempdir(), "cmuh_" + fname)]


def _unsaved_since_get(name: str) -> float | None:
    """回 `name` 第一次寫失敗的牆上時間;任何一個候選位置讀得到就算。
    檔內的 name 要對得上 —— 這是「不同程式不可以共用一個檔」的自我檢查。"""
    for p in _unsaved_sidecar_paths(name):
        try:
            data = safe_load_json(p, {}) or {}
            if isinstance(data, dict) and data.get("name") == name:
                v = data.get("since")
                if isinstance(v, int | float):
                    return float(v)
        except Exception:
            continue
    return None


def _unsaved_generation_get(name: str) -> int | None:
    """回 sidecar 記的「失敗當時的歷史檔世代」;沒有/壞掉 → None。"""
    for p in _unsaved_sidecar_paths(name):
        try:
            data = safe_load_json(p, {}) or {}
            if isinstance(data, dict) and data.get("name") == name:
                g = data.get("generation")
                if isinstance(g, int):
                    return g
        except Exception:
            continue
    return None


def _unsaved_since_set(name: str, ts: float, generation: int) -> bool:
    """記下第一次寫失敗的時間與★當時的歷史檔世代★(單次原子寫,無讀改寫)。
    回 True=至少一個位置寫成功;False=★兩邊都寫不進去★。"""
    for p in _unsaved_sidecar_paths(name):
        try:
            atomic_write_json(p, {"name": name, "since": ts, "generation": int(generation)})
            return True
        except Exception:
            continue
    return False


def _unsaved_since_clear(name: str) -> None:
    """清掉 `name` 的時間戳 = 刪檔(冪等,兩個位置都刪)。

    [外審 r4 P2] 刪檔可能被防毒/sharing violation 短暫擋住:留下的舊時間戳會讓日後第一次
    暫時性寫失敗直接讀到「早已超過窗口」→ 立即降級成未記錄的授權。所以:★重試★;仍失敗
    就★寫入「已清除」標記★(since=None,寫入通常比刪除更容易成功);連標記都寫不進去才
    WARNING —— 而讀取端另有歷史檔 mtime 的世代檢查兜底(見 `_authorize_restart`)。
    """
    for p in _unsaved_sidecar_paths(name):
        removed = False
        for _attempt in range(3):
            try:
                os.remove(p)
                removed = True
                break
            except FileNotFoundError:
                removed = True
                break
            except OSError:
                time.sleep(0.02)
        if removed:
            continue
        try:
            atomic_write_json(p, {"name": name, "since": None, "cleared": True})
        except Exception:
            logging.warning("[watchdog] 清不掉 %s 的寫失敗時間戳(%s)—— 殘留的舊窗口由歷史檔"
                            " mtime 世代檢查擋住", name, p)


def _authorize_restart(name: str) -> str:
    """要不要授權這一輪對 `name` 動手(kill / 啟動)。回 RESTART_AUTH_*。

    [第九輪 §6] 三個動手的地方(半死 kill、沒在跑→啟動、log 陳舊 kill)都改問這一支。
    * 鎖忙(另一個 watchdog 正在寫啟動歷史)→ LOCK_BUSY:★本 tick 不動手★,不做不持鎖的
      讀改寫;下一 tick 再問。
    * [外審 r1 P2-2 / r2 P2-1] 歷史寫不下去 → HISTORY_UNSAVED:本 tick 不動手(沒記下的重啟
      永遠累積不到 crash-loop 門檻)。★出口★(三條,任一成立就降級成 OK + WARNING,明講
      crash-loop 保護已降級):(a) 錯誤本身是可確認的持續狀況(權限/路徑/唯讀/磁碟滿);
      (b) 第一次失敗至今 ≥ HISTORY_UNSAVED_DEGRADE_AFTER_SEC —— 時間戳落盤,跨 `--once`
      行程有效;(c) 連時間戳都沒地方寫(settings/ 與 %TEMP% 都失敗)。寫成功就清掉時間戳。
    """
    try:
        ok = _record_restart_and_check_crash_loop(name)
    except _RestartHistoryLockBusy:
        logging.info("[watchdog] %s: 啟動歷史鎖忙(另一個 watchdog 正在寫)→ 本輪不授權重啟",
                     name)
        return RESTART_AUTH_LOCK_BUSY
    except _RestartHistoryUnsaved as e:
        cause = e.__cause__
        now = _wall_now()
        # 失敗當下觀察到的世代 = 鎖內 _load_restart_history 讀到的(這次沒寫成功,檔沒變)。
        gen_now = _HISTORY_GENERATION[0]
        since = _unsaved_since_get(name)
        # [外審 r4/r5 P2] ★世代檢查★:sidecar 記的是失敗當時的世代;現在的世代不同 = 那次失敗
        # 之後已經成功落盤過、只是 sidecar 沒清乾淨 → 殘留,不可沿用(否則第一次暫時性失敗
        # 就拿舊窗口立即降級)。用檔案內容裡的序號,★不用 mtime★:mtime 是牆上時鐘,時鐘回撥
        # 後會「在未來」,每一輪都把有效的失敗起點當殘骸重設,180 秒出口永遠到不了。
        if since is not None and _unsaved_generation_get(name) != gen_now:
            logging.info("[watchdog] %s: 殘留的寫失敗時間戳(世代 %s ≠ 現在 %s)→ 不沿用",
                         name, _unsaved_generation_get(name), gen_now)
            since = None
        if since is None:
            since = now
            recorded = _unsaved_since_set(name, since, gen_now)
        else:
            recorded = True
        reason = None
        if _persistent_os_error(cause):
            reason = f"錯誤為持續狀況({type(cause).__name__})"
        elif not recorded:
            reason = "連失敗時間戳都沒地方寫(settings/ 與 %TEMP% 都失敗)"
        elif now - since >= HISTORY_UNSAVED_DEGRADE_AFTER_SEC:
            reason = f"已持續 {now - since:.0f}s"
        if reason is None:
            logging.warning("[watchdog] %s: 啟動歷史寫入失敗(自 %.0fs 前起)→ 本輪不授權重啟",
                            name, now - since)
            return RESTART_AUTH_HISTORY_UNSAVED
        logging.warning(
            "[watchdog] %s: 啟動歷史寫不進去,%s → 視為持續狀況,★降級★授權重啟;"
            "crash-loop 保護在寫入恢復前無法累積", name, reason)
        return RESTART_AUTH_OK
    _unsaved_since_clear(name)
    return RESTART_AUTH_OK if ok else RESTART_AUTH_CRASH_LOOP


def _record_restart_and_check_crash_loop(name: str) -> bool:
    """紀錄一次啟動。回傳 True = 沒進入 crash loop, 可以繼續啟動。
    回傳 False = 已經 crash loop 中，呼叫端應跳過啟動。
    ★鎖在 timeout 內拿不到 → raise `_RestartHistoryLockBusy`★(第九輪 §6;
    生產呼叫端一律經由 `_authorize_restart()`,它會把例外轉成「本 tick 不授權」)。"""
    now = time.time()
    # [codex P2] _CRASH_LOOP_LOCK 只擋同行程;_restart_history_lock 擋跨行程,一起把整段
    # load-modify-save 序列化,避免 daemon 與 --once 併發 lost update。
    with _CRASH_LOOP_LOCK, _restart_history_lock():
        _load_restart_history()          # [AC-07] 先讀檔(--once 也能累積)
        # 檢查是否仍在 suspend 期間
        until = _SUSPENDED_UNTIL.get(name, 0.0)
        if now < until:
            return False
        # 取出歷史，砍掉視窗外的
        hist = _RESTART_HISTORY.setdefault(name, [])
        cutoff = now - CRASH_LOOP_WINDOW_SEC
        hist[:] = [t for t in hist if t >= cutoff]
        hist.append(now)
        if len(hist) > CRASH_LOOP_MAX_RESTARTS:
            # 觸發 crash loop！
            _SUSPENDED_UNTIL[name] = now + CRASH_LOOP_SUSPEND_SEC
            logging.critical(
                "[watchdog] %s crash loop! %d 次啟動在 %d 秒內 → 暫停 %d 分鐘 "
                "(直到 %s)。如為新版 bug 請降版或修復後手動清除 settings/"
                ".auto_update_suspended_until",
                name, len(hist), CRASH_LOOP_WINDOW_SEC,
                CRASH_LOOP_SUSPEND_SEC // 60,
                time.strftime("%H:%M:%S",
                                time.localtime(now + CRASH_LOOP_SUSPEND_SEC)))
            # [H] 同時暫停 auto-update 1 小時 (避免又拉到同個爛版本)
            try:
                suspend_path = suspend_auto_updates(
                    f"{name} crash loop at "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}",
                    duration_sec=3600,
                    now=now,
                )
                logging.critical(
                    "[watchdog] 已寫 %s 暫停 auto-update 1 小時",
                    suspend_path)
            except Exception:
                logging.exception("[watchdog] 寫 auto-update suspend flag 失敗")
            # 清歷史避免後續又連續觸發
            hist.clear()
            _save_restart_history()      # [AC-07] 落盤(suspend + 清空後的歷史)
            return False
        err = _save_restart_history()    # [AC-07] 落盤本次啟動記錄
        if err is not None:
            # [外審 r1 P2-2] 沒記下就不能說「授權成功」:那會讓反覆重啟永遠累積不到門檻。
            hist.pop()                   # 記憶體也撤回這一筆,與磁碟一致
            raise _RestartHistoryUnsaved(_restart_history_path()) from err
        return True


def _migrate_config(cfg: dict) -> tuple:
    """回傳 (migrated_cfg, changed)。
    v1→v2：把舊 process_match 改成新版 keyword。
    v2→v3：把主程式 enabled 設成 False (使用者反映外層 C 一直誤判沒在跑就重啟)。
    v3→v4：加 required_config_file 欄位，打卡/會診查詢 per-machine opt-in
           (本機沒對應 config → watchdog 跳過、不啟動)。
    """
    cur_v = int(cfg.get("schema_version", 1))
    if cur_v >= CONFIG_SCHEMA_VERSION:
        return cfg, False
    # v1 → v2
    if cur_v < 2:
        for prog in cfg.get("programs", []):
            old = prog.get("process_match", "")
            new = _V1_TO_V2_PROCESS_MATCH.get(old)
            if new and old != new:
                prog["process_match"] = new
    # v2 → v3: 主程式 enabled=false
    if cur_v < 3:
        for prog in cfg.get("programs", []):
            if prog.get("name") == "主程式":
                prog["enabled"] = False
    # v3 → v4: 加 required_config_file 欄位
    if cur_v < 4:
        for prog in cfg.get("programs", []):
            name = prog.get("name", "")
            req = _V3_TO_V4_REQUIRED_CONFIG.get(name, "")
            prog.setdefault("required_config_file", req)
    # v4 → v5: 加 master_enabled 總開關
    # 智慧 default：本機若有 autoclock_config.json 或 consult_query_config.json
    # → 表示本機是「設定過的主機」→ master_enabled=True (保留現行行為)
    # → 沒有任何相關 config → master_enabled=False (新裝機/不該跑 watchdog)
    if cur_v < 5:
        auto_default = False
        for chk in ("settings/autoclock_config.json",
                     "settings/consult_query_config.json"):
            if (_ROOT / chk).exists():
                auto_default = True
                break
        cfg.setdefault("master_enabled", auto_default)
    # v5 → v6: 加 mutex_name 欄位 (psutil cmdline 不可靠時的可靠 fallback)
    if cur_v < 6:
        for prog in cfg.get("programs", []):
            name = prog.get("name", "")
            mutex = _V5_TO_V6_MUTEX_NAME.get(name, "")
            if mutex:
                prog.setdefault("mutex_name", mutex)
    # v6 → v7: 打卡 max_stale_sec 0→300 — autoclock v45 起有 heartbeat，
    # 外層 watchdog 終於能偵測「process 在但 thread 凍」的半死狀態
    if cur_v < 7:
        for prog in cfg.get("programs", []):
            name = prog.get("name", "")
            new_stale = _V6_TO_V7_MAX_STALE.get(name)
            if new_stale is not None:
                # 強制覆寫 (而非 setdefault) — 舊值 0 是個 bug
                prog["max_stale_sec"] = new_stale
    cfg["schema_version"] = CONFIG_SCHEMA_VERSION
    return cfg, True


def get_root() -> Path:
    return _ROOT


# ─── psutil ─────────────────────────────────────────────────────────────
def _get_psutil():
    """Lazy import psutil — 失敗時呼叫者要 fallback。"""
    try:
        import psutil  # noqa: F401
        return psutil
    except Exception:
        return None


# ─── Config ──────────────────────────────────────────────────────────────
_config_load_failed = False    # [2026-07-26] 上一次讀 config 是「暫時性失敗」


def config_load_failed() -> bool:
    """上一次 load_config 是否為暫時性讀取失敗(檔案仍在、只是讀不到)。

    [2026-07-26 審查] 與打卡/排班同一個病灶:防毒/備份軟體鎖檔時 safe_load_json 回 default,
    watchdog 會拿【預設設定】跑一輪 —— 使用者關掉的程式被當成「該啟動」、
    per-machine 的啟用選項全被忽略;而且 _migrate_config 一旦判定要遷移,還會把
    那份預設值【寫回檔案】,永久蓋掉使用者的設定。呼叫端應據此跳過本輪。"""
    return _config_load_failed


def load_config() -> dict:
    """讀 config；不存在自動寫 default；缺漏鍵 fallback。

    暫時性讀取失敗時設定 config_load_failed(),並【絕不】把預設值寫回檔案。
    """
    global _config_load_failed
    _config_load_failed = False
    if not CONFIG_PATH.exists():
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(str(CONFIG_PATH), DEFAULT_CONFIG, indent=2)
        except Exception:
            logging.exception("[watchdog] 寫預設 config 失敗")
        return _default_config_copy()

    cfg, status = safe_load_json_ex(str(CONFIG_PATH), default=None)
    if status == "error":
        # 原檔仍完好、只是暫時讀不到 → 不可用預設值蓋回去,也不該拿預設設定跑一輪。
        _config_load_failed = True
        logging.warning("[watchdog] config 暫時讀取失敗(檔案仍在)→ 本輪跳過,不覆寫")
        return _default_config_copy()
    if not isinstance(cfg, dict):
        logging.warning("[watchdog] config 不可用或格式錯誤，用記憶體 default")
        return _default_config_copy()

    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, json.loads(json.dumps(v)))

    # Schema migration (v1 → v2 修 process_match)
    cfg, migrated = _migrate_config(cfg)
    if migrated:
        try:
            atomic_write_json(str(CONFIG_PATH), cfg, indent=2)
            logging.info("[watchdog] config 升級至 schema v%d",
                          CONFIG_SCHEMA_VERSION)
        except Exception:
            logging.exception("[watchdog] 寫回升級後 config 失敗 (本次仍用新版記憶體)")
    return cfg


# ─── pythonw 路徑 ────────────────────────────────────────────────────────
def find_pythonw() -> str:
    """Find a Python launcher suitable for detached watchdog restarts."""
    embed = _ROOT / "python_embed" / "pythonw.exe"
    if embed.exists():
        return str(embed)
    current_exe = Path(sys.executable).resolve()
    sibling = current_exe.with_name("pythonw.exe")
    if sibling.exists():
        return str(sibling)
    import shutil
    from_path = shutil.which("pythonw.exe") or shutil.which("pythonw")
    if from_path:
        return from_path
    if current_exe.exists():
        return str(current_exe)
    return ""


# ─── Process 列舉 ────────────────────────────────────────────────────────
_WMIC_CACHE_TTL_SEC = 2.0
_wmic_cache_until = 0.0
_wmic_cache_stdout = ""
_wmic_cache_run = None


def _remember_wmic_process_csv(stdout: str, run_fn, now: float) -> str:
    global _wmic_cache_until, _wmic_cache_stdout, _wmic_cache_run
    _wmic_cache_stdout = stdout or ""
    _wmic_cache_until = now + _WMIC_CACHE_TTL_SEC
    _wmic_cache_run = run_fn
    return _wmic_cache_stdout


def list_python_processes() -> list:
    """[{pid, cmdline}, ...] — 抓 pythonw.exe / python.exe（admin 才看得到 admin 的 cmdline）。"""
    psutil = _get_psutil()
    if psutil is None:
        logging.warning("[watchdog] psutil 不可用，process 列舉退化為空")
        return []
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name not in ("pythonw.exe", "python.exe"):
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            out.append({"pid": p.info["pid"], "cmdline": cmd})
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            continue
    return out


def find_matching_pids(procs: list, keyword: str, exclude_pid: int = 0) -> list:
    """cmdline 含 keyword（不分大小寫）的 PID 清單，排除 exclude_pid。"""
    if not keyword:
        return []
    kw = keyword.lower()
    return [p["pid"] for p in procs
            if kw in p.get("cmdline", "").lower() and p["pid"] != exclude_pid]


def _read_python_process_csv_via_powershell() -> str:
    """[AC-06] WMIC 在 Win11 24H2+ 已移除 → 改用 PowerShell CIM（Get-CimInstance
    Win32_Process，不受 WMIC 移除影響）列舉 python launchers。輸出成與 wmic /FORMAT:CSV
    同結構的 CSV（欄：Node,CommandLine,ProcessId）供既有 parser 沿用；CREATE_NO_WINDOW
    免閃黑窗；utf-8 解碼。"""
    _CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # [codex P1] Windows PowerShell 5.1 的原生 stdout 預設用 OEM/主控台碼頁(繁中機為
    # cp950),不是 UTF-8。若不強制輸出 UTF-8,下面 encoding='utf-8' 解碼會把中文 CommandLine
    # 打亂 → _wmic_find_pids 比對不到中文關鍵字 → 這條 fallback 形同虛設。先設 OutputEncoding。
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process "
        "-Filter \"Name='pythonw.exe' or Name='python.exe'\" | "
        "Select-Object @{Name='Node';Expression={$env:COMPUTERNAME}},"
        "CommandLine,ProcessId | ConvertTo-Csv -NoTypeInformation"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        logging.debug("[watchdog] PowerShell CIM fallback 例外", exc_info=True)
        return ""
    return (r.stdout or "") if r.returncode == 0 else ""


def _read_wmic_python_process_csv(*, force: bool = False) -> str:
    """列舉 python 系行程的 (CommandLine, ProcessId)。

    `force=True`:★略過快取★。動手前的身分重驗必須看【此刻】的事實 ——
    吃到「查詢當下那一份」的話,重驗就只是把同一個觀測再讀一次,
    競態原封不動(外審第五輪 R5-P3-01)。
    """
    global _wmic_cache_until, _wmic_cache_stdout, _wmic_cache_run
    now = time.monotonic()
    run_fn = subprocess.run
    if not force and now < _wmic_cache_until and _wmic_cache_run is run_fn:
        return _wmic_cache_stdout

    # [v16 2026-05-25] CREATE_NO_WINDOW — admin watchdog tick 每 60s 走 WMIC fallback
    # (因為 admin process 用 psutil 看不到 cmdline)，原本沒設 creationflags 會閃
    # 黑色 console 視窗。Windows-only flag，os.name=='nt' 才有意義。
    _CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    stdout = ""
    try:
        r = subprocess.run(
            ["wmic", "process", "where",
             "(name='pythonw.exe' or name='python.exe')",
             "get", "ProcessId,CommandLine", "/FORMAT:CSV"],
            capture_output=True, text=True, timeout=10,
            encoding=locale.getpreferredencoding(False), errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            stdout = r.stdout or ""
    except Exception:
        # [AC-06] wmic 不存在(Win11 24H2+ 已移除,FileNotFoundError)或執行失敗 →
        # 落 PowerShell CIM fallback,否則整條半死救援失效。
        logging.debug("[watchdog] wmic 不可用,改用 PowerShell CIM", exc_info=True)

    if not stdout.strip():
        stdout = _read_python_process_csv_via_powershell()
    return _remember_wmic_process_csv(stdout, run_fn, now)


def _cmdline_is_target(cmdline: str, process_keyword: str) -> bool:
    """這條命令列★確實是在跑那支程式★嗎(而不是剛好提到它的名字)。

    ★[外審第五輪 R5-P3-01] 發現用寬鬆的、動手前用嚴格的★
    `_wmic_find_pids()` 的比對是 `keyword in cmdline`(沒有邊界的子字串)。
    那只證明「命令列的某個位置出現這串字」,沒有證明「實際執行的就是那支
    程式」—— 例如 `python 修檔工具.py 中國醫皮膚科主程式的備份.txt` 也會命中,
    而下游是 `taskkill /F /T`(連子行程一起殺)。
    這一支要求 keyword 是★實際 script 引數的檔名本體★，而非後續資料引數。
    ★刻意寬容的地方★:引號、大小寫、有沒有副檔名 —— 那些是 Windows 命令列
    的表面差異,不是身分差異。★刻意不寬容的地方★:它必須是【那個引數本身】,
    不能只是某個更長字串的一部分。
    """
    return _tokens_are_target(_split_cmdline_tokens(cmdline or ""),
                              process_keyword)


def _python_script_argument(tokens) -> str | None:
    """Find the script operand without treating script data or option values as code.

    Preserve argv boundaries. Recognize our Python launchers and known CPython
    flags; unknown modes are not sufficient evidence for a destructive action.
    Script-only observations remain supported for legacy callers.
    """
    if not tokens or isinstance(tokens, (str, bytes)):
        return None
    argv = [str(token).strip().strip(chr(34)) for token in tokens]
    exe = os.path.basename(argv[0].replace(chr(92), "/")).lower()
    if exe not in ("python.exe", "pythonw.exe", "python", "pythonw"):
        return argv[0] if not argv[0].startswith("-") else None
    i = 1
    while i < len(argv):
        token = argv[i]
        if token == "--":
            return argv[i + 1] if i + 1 < len(argv) else None
        if token == "-":
            return None                  # stdin, not a script path
        if not token.startswith("-"):
            return token
        if token == "--check-hash-based-pycs":
            if i + 1 >= len(argv) or argv[i + 1] not in ("default", "always", "never"):
                return None
            i += 2
            continue
        if token.startswith("--"):
            return None
        # Short options may be grouped (-IB); W/X consume the remainder or
        # the next argument. c/m switch execution mode and must never match.
        for offset, option in enumerate(token[1:], 1):
            if option in "WX":
                if offset == len(token) - 1:
                    i += 1
                    if i >= len(argv):
                        return None
                break
            if option not in "bBdEhiIOPqRsSuvx" or option == "h":
                return None
        i += 1
    return None


def _tokens_are_target(tokens, process_keyword: str) -> bool:
    """Verify the actual script operand; never scan subsequent data arguments."""
    kw = (process_keyword or "").strip().strip(chr(34)).lower()
    if not kw:
        return False
    script = _python_script_argument(tokens)
    if not script:
        return False
    base = os.path.basename(script.replace(chr(92), "/")).lower()
    stem, extension = os.path.splitext(base)
    return extension in ("", ".py", ".pyw") and (base == kw or stem == kw)


def _split_cmdline_tokens(cmdline: str) -> list:
    """把 Windows 命令列切成引數。引號內的空白不切(路徑常有空白)。

    ★不用 shlex★:它是 POSIX 語意,會把反斜線當跳脫字元 —— 而 Windows 路徑
    的分隔符號正是反斜線,那樣會把我們要比對的東西吃掉。
    """
    out, buf, in_q = [], [], False
    for ch in str(cmdline or ""):
        if ch == chr(34):
            in_q = not in_q
            continue
        if ch.isspace() and not in_q:
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _cmdline_tokens_of_pid_now(pid: int):
    """這個 PID ★此刻★ 的命令列引數(list)。查不出來回 None(≠ 空清單)。

    ★回傳的是【引數清單】不是字串★(外審第 1 輪 P1):psutil 給的本來就是
    切好的 argv,把它 join 成字串再重新切,會把「一個含空白的引數」拆成兩段
    —— 而我們正是拿每一段的 basename 去比對身分。有邊界資訊就不可以丟掉。
    只有 WMIC 那條回的是原始字串,才需要自己切。

    psutil 便宜但對 admin 行程常拿不到 cmdline —— 那正是 WMIC 後備存在的
    理由,所以拿不到就走 WMIC(★強制略過快取★:重驗要看此刻的事實)。
    ★「查不出來」要能與「查到了但不是目標」分開★:呼叫端一律當
    「不能證明」處理(不殺)。
    """
    try:
        import psutil  # noqa: PLC0415
        cl = psutil.Process(int(pid)).cmdline()
        if cl:
            return [str(x) for x in cl]      # ★原樣帶走,不 join★
    except Exception:
        logging.debug("[watchdog] psutil 取不到 PID %s 的命令列", pid,
                      exc_info=True)
    try:
        stdout = _read_wmic_python_process_csv(force=True)
        for parts in csv.reader((stdout or "").splitlines()):
            if len(parts) < 3 or parts[0].strip().lower() == "node":
                continue
            pid_str = parts[-1].strip()
            if pid_str.isdigit() and int(pid_str) == int(pid):
                # WMIC 只給得出原始字串 —— 這裡才需要自己切。
                return _split_cmdline_tokens(",".join(parts[1:-1]).strip())
    except Exception:
        logging.debug("[watchdog] WMIC 取不到 PID %s 的命令列", pid,
                      exc_info=True)
    return None


def _pid_is_still_the_target(pid: int, process_keyword: str) -> bool:
    """釘住之後再問一次:這個 PID ★現在★ 還是那支程式嗎。

    三個條件都要成立才算數:還活著且是 python 系、命令列查得出來、
    而且那條命令列★確實在跑那支程式★(精確判準,不是子字串)。
    查不出來一律回 False —— ★不能證明就不動手★。
    """
    try:
        from cmuh_common.pidfile import pid_looks_like_python  # noqa: PLC0415
        if not pid_looks_like_python(int(pid)):
            logging.warning("[watchdog] PID %s 已不是 python 系行程 → 不殺", pid)
            return False
    except Exception:
        logging.warning("[watchdog] 無法確認 PID %s 是否為 python 行程 → 不殺",
                        pid, exc_info=True)
        return False
    tokens = _cmdline_tokens_of_pid_now(pid)
    if tokens is None:
        logging.warning("[watchdog] 查不出 PID %s 此刻的命令列 → 不殺"
                        "(無法證明身分)", pid)
        return False
    if not _tokens_are_target(tokens, process_keyword):
        logging.warning("[watchdog] PID %s 此刻的命令列已不是 %s → 不殺"
                        "(可能已結束、PID 被回收給別的程式)",
                        pid, process_keyword)
        return False
    return True


def _wmic_find_pids(process_keyword: str, *, log_on_empty: bool = True) -> list:
    """WMIC fallback：列舉 Python launchers + cmdline，回 cmdline 含 keyword 的 PID。

    psutil 在 admin process 上偶發 NtQueryInformationProcess access denied →
    cmdline 抓不到 → 找不到 PID。WMIC 的權限模型不同，admin 執行
    wmic process 通常能拿到 admin process 的 cmdline。

    log_on_empty=False：cmdline 真的找不到時不印 WARNING (給日常心跳呼叫用，
    避免每 30s 印一行誤導訊息)。kill 路徑用 True (預期一定要找到 PID 才能 kill)。
    """
    pids = []
    my_pid = os.getpid()
    try:
        stdout = _read_wmic_python_process_csv()
        if stdout:
            kw_lower = (process_keyword or "").lower()
            for parts in csv.reader(stdout.splitlines()):
                # CSV: Node,CommandLine,ProcessId
                if len(parts) < 3:
                    continue
                if parts[0].strip().lower() == "node":
                    continue
                cmdline = ",".join(parts[1:-1]).strip()
                pid_str = parts[-1].strip()
                if not pid_str.isdigit():
                    continue
                pid = int(pid_str)
                if pid == my_pid:
                    continue
                if kw_lower and kw_lower in cmdline.lower():
                    pids.append(pid)
            if pids:
                return pids
    except Exception:
        logging.debug("[watchdog] wmic fallback 例外", exc_info=True)

    if log_on_empty:
        # 不做「所有 pythonw/python.exe」fallback。這裡若抓不到 cmdline，就無法確認
        # PID 是否真屬於目標程式；直接 kill 全部 pythonw 風險太高，寧可讓
        # caller 回報找不到 PID，交給下一輪或人工處理。
        logging.warning(
            "[watchdog] 無法用 WMIC 找到 %s 的 PID；為避免誤殺其他 Python 程序，"
            "本輪不執行 broad fallback kill",
            process_keyword)
    return []


def _find_pids_holding_mutex(process_keyword: str, mutex_name: str = "",
                             pid_name: str = "") -> list:
    r"""半死救援：找出該程式的 PID。★先問 PID 檔,再退回 cmdline 比對★

    [2026-08-04 實機] 舊版只走 cmdline 比對,在 Windows 11 上連續兩小時找不到
    PID、救援完全失效(每 60 秒印同一組警告什麼都沒做)。三個破口同時成立:
    WMIC 已被移除、CIM 對提權行程回傳空 CommandLine、而且實機 cmdline 是
    `...\srcutoclock.py` 根本不含啟動器檔名關鍵字(見 cmuh_common/pidfile)。
    PID 檔是行程【自報】的直接事實,不受這三者影響;讀回來仍會驗活著且是
    python 行程(PID 會被重用),驗不過就退回原本的 cmdline 路徑。

    保留 backward-compat 簽章(mutex_name 未使用,外部 caller 與測試都已綁定)。
    """
    if pid_name:
        try:
            from cmuh_common.pidfile import read_verified_pid  # noqa: PLC0415
            pid = read_verified_pid(pid_name)
            if pid:
                logging.info("[watchdog] 由 PID 檔取得 %s 的 PID=%s(不需 cmdline 比對)",
                             process_keyword, pid)
                _PID_FROM_PIDFILE[pid_name] = pid      # ★記下來源★
                return [pid]
            _PID_FROM_PIDFILE.pop(pid_name, None)      # 這次是 cmdline 後備
        except Exception:
            _PID_FROM_PIDFILE.pop(pid_name, None)
            logging.debug("[watchdog] 讀 PID 檔失敗,退回 cmdline 比對", exc_info=True)
    return _wmic_find_pids(process_keyword, log_on_empty=True)


# ─── Kill + start ───────────────────────────────────────────────────────
# 查詢當下記下「這個 PID 是從 PID 檔驗來的」——供 kill 階段判斷來源。
# (外審第 4 回:事後重讀 PID 檔推不出來源,因為 PID 可能已經被回收。)
_PID_FROM_PIDFILE: dict = {}


def kill_pids_verified(pids: list, pid_name: str = "",
                       process_keyword: str = "") -> list:
    """殺掉這幾個 PID;若它們是從 PID 檔驗來的,期間把身分【釘住】。

    ★[2026-08-08 外審]★ `read_verified_pid()` 回的是裸 PID。從驗證到這裡
    真正執行 `taskkill /F /T` 之間,那個行程可能已結束、PID 被配給另一支
    自家程式 —— 我們就會連同它的子行程一起強殺。
    `pinned_verified_pid()` 在整個 kill 期間握著該行程的 handle,
    Windows 不會在那段時間把 PID 配給別人。

    ★[外審第 3 回] 但不可以因此把「cmdline 後備」那條路整個關掉★
    `_find_pids_holding_mutex()` 回的 PID 可能來自【PID 檔】,也可能來自
    【cmdline 比對後備】—— 後者正是在「PID 檔不存在/舊格式/驗不過」時啟用的。
    我第一版不管來源一律要求釘住,於是那些情況下 pin 必然失敗、PID 永遠殺不掉:
    半死救援在那條路上整個停擺。修一個競態卻關掉一整條救援路徑,比原本的問題嚴重。
    現在先問「這個 PID 是不是驗得出來的那一個」:
      * 是 → 走釘住的路(釘不住就【不殺】,fail-closed);
      * 不是(PID 檔不可用 → 這是 cmdline 後備來的)→ 見下。

    ★[外審第五輪 R5-P3-01] 後備那條路的「維持既有行為直接殺」已經改掉★
    上一版的理由是「那條路本來就沒有可驗證的身分」。那句話只對【PID 檔的
    身分】成立 —— 它其實還有另一種可驗證的身分:★命令列★。
    於是把兩件事拆開(審查的原話:fallback discovery ≠ authorization to kill):
      * ★發現★仍然用寬鬆的 cmdline 子字串(能力不變,不廢掉半死救援);
      * ★動手★先開 handle 釘住 PID(釘住期間 Windows 不會把它配給別人),
        再用★精確判準★重問一次「它此刻還是那支程式嗎」,驗不出來就不殺。
    沒有 keyword 可驗時(呼叫端沒傳)才退回既有行為,並記一行 warning ——
    那是唯一還沒有身分可驗的情況,不可以靜悄悄。
    """
    if not pid_name:
        return [pid for pid in pids
                if _kill_unverified_source(pid, process_keyword)]
    try:
        from cmuh_common.pidfile import (  # noqa: PLC0415
            pinned_verified_pid,
        )
    except Exception:
        # ★[外審第 3 回] 安全機制載入不了 → 不要退回不安全的路★
        #   那等於「保護消失的時候剛好把保護關掉」。少殺一次的代價是
        #   這一輪不重啟(下一輪會再來);誤殺的代價是砍掉一支無關的程式。
        logging.error("[watchdog] 載入 pinned_verified_pid 失敗 → 本輪不 kill"
                      "(避免在無法驗證身分的情況下 /F /T)", exc_info=True)
        return []
    # ★[外審第 4 回] 來源要【帶著走】,不可以事後再讀一次去推★
    #   我上一版在這裡重讀 PID 檔:若那個行程剛好在查詢之後結束、PID 被回收,
    #   重讀會回 None —— 而我把 None 解讀成「這是 cmdline 後備來的」,
    #   於是直接 kill 了那個【已經被換掉的】PID。判斷來源的證據必須來自
    #   查詢當下,不是來自一次新的觀測(那正是這一輪要修的競態本身)。
    verified = _PID_FROM_PIDFILE.get(pid_name)
    killed = []
    for pid in pids:
        if verified != pid:
            # 這個 PID 不是從 PID 檔驗來的(cmdline 後備)→ 釘住並重驗身分。
            if _kill_unverified_source(pid, process_keyword):
                killed.append(pid)
            continue
        with pinned_verified_pid(pid_name) as pinned:
            if pinned != pid:
                logging.warning("[watchdog] PID %s 已無法確認身分(可能已結束或"
                                "被回收)→ 不殺", pid)
                continue
            if kill_pid(pid):
                killed.append(pid)
    return killed


def _kill_unverified_source(pid: int, process_keyword: str) -> bool:
    """cmdline 後備找到的 PID:★釘住 → 重驗 → 才殺★(外審第五輪 R5-P3-01)。

    競態長這樣:
        T0 列舉,PID 1234 的 cmdline 命中 keyword
        T1 目標自己結束
        T2 Windows 把 1234 配給另一支程式
        T3 我們 `taskkill /F /T 1234` —— 殺到 T2 那一支,連它的子行程一起。
    釘住(OpenProcess)之後 Windows 不會回收該 PID,重驗因此問得到的是
    「我們釘住的那個行程」,不是另一次觀測。
    """
    if not process_keyword:
        # ★唯一還沒有身分可驗的情況★:呼叫端沒給 keyword。維持既有行為,
        #   但要說出來 —— 這是一次「在無法驗證身分下的 /F /T」。
        logging.warning("[watchdog] PID %s 沒有可驗證的身分(未提供 keyword)"
                        "→ 維持既有行為直接 kill", pid)
        return kill_pid(pid)
    try:
        from cmuh_common.pidfile import pinned_matching_pid  # noqa: PLC0415
    except Exception:
        # ★安全機制載入不了 → 不要退回不安全的路★(與 pinned_verified_pid 同)
        logging.error("[watchdog] 載入 pinned_matching_pid 失敗 → 本輪不 kill"
                      "(避免在無法驗證身分的情況下 /F /T)", exc_info=True)
        return False
    with pinned_matching_pid(
            pid, lambda p: _pid_is_still_the_target(p, process_keyword)) as ok:
        if ok is None:
            return False
        return kill_pid(ok)


def kill_pid(pid: int) -> bool:
    """taskkill /F /T /PID — 需 admin 才砍得了 admin process。
    [v16 2026-05-25] 加 CREATE_NO_WINDOW 避免閃 console。
    [AC-03] 加 /T 連同子行程樹一起殺 → 硬殺卡住的打卡程式時，其子 chromedriver/Chrome
    也一併清掉，不遺留孤兒瀏覽器佔資源。"""
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except Exception:
        logging.exception("[watchdog] kill PID %s 例外", pid)
        return False


def current_session_id():
    """本行程所在的 Windows terminal session id;取不到回 None(不猜)。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        sid = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(
            ctypes.c_ulong(os.getpid()), ctypes.byref(sid))
        return int(sid.value) if ok else None
    except Exception:
        logging.debug("[watchdog] ProcessIdToSessionId 失敗", exc_info=True)
        return None


def start_program(pyw_path: Path, pythonw: str) -> int:
    """以 admin 子行程啟動 .pyw（繼承父 process 的 admin token，無 UAC）。

    [2026-07-26 審查 ★重複打卡★] session 0 一律拒絕啟動。背景:安裝腳本用
    `schtasks /SC MINUTE` 建立 watchdog 的週期性 task 時【沒有 /IT】,那種 task 跑在
    session 0(非互動);子行程會跟著落在 session 0 —— 使用者看不到、Chrome 自動化也
    沒有互動桌面,而且各程式的單例 mutex 都是 `Local\\`(per-session),擋不住跨 session
    的第二份 → 打卡程式同時跑兩份、重複打卡(repo 內的「清理重複打卡程式.ps1」
    「診斷打卡重複執行.ps1」就是這個現象留下的現場工具)。
    偵測(WMIC/psutil)與 kill 在 session 0 仍然有效,故只擋【啟動】這一個動作。
    安裝腳本已補 /IT;此處是給既有安裝的兜底,使用者不必重跑安裝也不會再被重複打卡。
    """
    session_id = current_session_id()
    if session_id == 0:
        logging.error(
            "[watchdog] 目前跑在 session 0(非互動),拒絕啟動 %s —— 從這裡啟動的程式"
            "使用者看不到,而且單例 mutex 是 Local\\(per-session)擋不住跨 session 的"
            "第二份,會造成重複執行/重複打卡。請重跑「安裝開機自動啟動」讓排程加上 /IT。",
            pyw_path.name)
        return 0
    try:
        p = launch_python_script(
            str(pyw_path),
            executable=pythonw,
            cwd=str(_ROOT),
            detached=True,
        )
        return p.pid
    except Exception:
        logging.exception("[watchdog] 啟動 %s 失敗", pyw_path)
        return 0


#: `log_status()` 的狀態(R3-P3-01 三態 + 第九輪 §5 第四態)。
LOG_OK = "ok"                 # 讀得到 mtime
LOG_ABSENT = "absent"         # 檔案不在
LOG_UNREADABLE = "unreadable"  # 在,但 stat 失敗(權限/被鎖/磁碟)
LOG_CLOCK_JUMP = "clock_jump"  # 本 tick 牆上時鐘剛跳動 / 系統剛睡過 → 新鮮度無從判斷


# ─── 時間基準與喚醒守衛(第九輪 §5)────────────────────────────────────────
# 舊的陳舊判定是 `time.time() - st_mtime > max_stale`。牆上時鐘會做兩件 log 本身
# 沒做的事:★系統睡眠★(診間電腦午休/隔夜)與★被調整★(NTP/人工)。
#   * 睡 N 分鐘醒來:每支程式的 mtime 都停在睡前,age 一律 ≥ N ≥ max_stale;而被監看
#     程式的 heartbeat 是 sleep/wait 驅動、醒來才補寫,watchdog 醒來卻立刻 tick ——
#     誰先醒是擲硬幣,watchdog 先醒就 ★kill 一支完全健康的程式★。
#   * 時鐘往回:age 變小/負,卡死的程式看起來很新(第八輪指出的方向)。
# 修法分兩層,缺一不可:
#   1. 喚醒/跳動守衛:每 tick 比較「牆上時鐘走了多少」與「系統醒著的時間走了多少」
#      (Windows `QueryUnbiasedInterruptTime`,★文件明定不含睡眠★;取不到退回
#      monotonic)。差值超過容忍 → 本 tick 隔離:所有 log 新鮮度回 LOG_CLOCK_JUMP、
#      不以它動手,並把進展基準重設為現在。這一條同時涵蓋往回跳(差值為負)。
#   2. 進展觀察:年齡改成「最後一次觀察到 mtime/size 變化,距今★醒著★多久」,而不是
#      「mtime 與現在牆上時鐘差幾秒」。只有守衛沒有這一層的話,隔離只保護第一個 tick,
#      第二個 tick 又拿睡前的 mtime 算年齡 → 照樣 kill。
# `--once`(schtasks 每 2 分鐘起一個新行程)沒有行程內的上一 tick,所以 (wall, awake)
# 與各 log 的基準會落盤到 settings/CLOCK_STATE_FILENAME;讀不到就當第一次(退回牆上時鐘年齡、
# 不隔離)。多寫者(inner 執行緒 + outer)的 lost update 無害:最壞是基準重設晚一輪。
# ★誠實的邊界★:保護力靠「awake 時鐘不含睡眠」這個性質。Windows 的
# `QueryUnbiasedInterruptTime` 文件明定如此;退回 `time.monotonic()` 時(非 Windows /
# API 失敗)它是否含睡眠沒有實測(fetch_resilience 的 docstring 說了同一件事)——若含,
# Δwall−Δawake 為 0 → 不隔離、年齡也含睡眠 → ★退回今天的行為(醒來可能誤殺)★,
# 但永遠不會因此誤隔離或誤放。所以這個守衛的失效模式是「沒幫上忙」,不是「幫倒忙」。
# 在診間機器實測一次 monotonic 跨睡眠的行為,是使用者要做的量測(不進 CI)。
CLOCK_JUMP_TOLERANCE_SEC = 60.0
CLOCK_STATE_FILENAME = ".watchdog_clock.json"

_CLOCK_LOCK = threading.Lock()
_CLOCK: dict = {
    "prev_wall": None,      # 本行程上一 tick 的牆上時鐘
    "prev_awake": None,     # 本行程上一 tick 的醒著時間
    "quarantined": False,   # 本 tick 是否隔離
    "jump": 0.0,            # 本 tick 觀測到的跳動秒數(Δwall − Δawake)
    "logs": {},             # normcase(path) → {"mtime","size","seen_awake"}
}


def _wall_now() -> float:
    return time.time()


def _awake_now() -> float:
    """系統「醒著」的秒數(不含睡眠/休眠)。

    Windows:`QueryUnbiasedInterruptTime`(kernel32,Win7+),100ns 單位,文件明定
    ★不包含★系統睡眠/休眠期間。取不到(非 Windows / API 失敗)退回 `time.monotonic()`。
    """
    if os.name == "nt":
        try:
            import ctypes
            t = ctypes.c_ulonglong(0)
            if ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(t)):
                return t.value / 1e7
        except Exception:
            pass
    return time.monotonic()


def _clock_state_path() -> str:
    """狀態檔位置:呼叫時透過 `get_settings_dir()` 取,不是 import 期算死的常數。

    生產上 `get_settings_dir()` 與 `SETTINGS_DIR` 是同一個目錄(兩者都以啟動器釘住的
    app 根為準);差別在★測試★:conftest 只會把 `get_app_dir()` 系的路徑導向每個
    測試的 tmp,`_ROOT` 系的常數不會 —— 第一版用常數,結果每個跑到 `run_one_tick`
    的測試都把狀態檔寫進真的 settings/,而且跨測試互相看到對方的假時鐘。
    """
    return os.path.join(get_settings_dir(), CLOCK_STATE_FILENAME)


def _reset_clock_state() -> None:
    """測試用:清掉行程內的時間基準狀態(生產路徑不呼叫)。"""
    with _CLOCK_LOCK:
        _CLOCK.update(prev_wall=None, prev_awake=None, quarantined=False,
                      jump=0.0, logs={})


def _load_clock_state() -> tuple:
    """回 (prev_wall, prev_awake, logs) —— 讀不到/壞掉一律 (None, None, {})。"""
    try:
        data = safe_load_json(_clock_state_path(), {}) or {}
        pw, pa = data.get("wall"), data.get("awake")
        if not isinstance(pw, (int, float)) or not isinstance(pa, (int, float)):
            return None, None, {}
        logs = {}
        raw = data.get("logs")
        if isinstance(raw, dict):
            for k, v in raw.items():
                if (isinstance(v, dict)
                        and all(isinstance(v.get(f), (int, float))
                                for f in ("mtime", "size", "seen_awake"))):
                    logs[str(k)] = {"mtime": float(v["mtime"]),
                                    "size": int(v["size"]),
                                    "seen_awake": float(v["seen_awake"])}
        return float(pw), float(pa), logs
    except Exception:
        return None, None, {}


def _save_clock_state(wall: float, awake: float) -> None:
    try:
        atomic_write_json(_clock_state_path(),
                          {"wall": wall, "awake": awake, "logs": _CLOCK["logs"]})
    except Exception:
        logging.debug("[watchdog] 寫入時間基準狀態失敗", exc_info=True)


def _flush_clock_state() -> None:
    """tick ★結束★時落盤:`_note_tick` 在 tick 開頭存的是「上一輪」的基準,本輪
    `log_status` 新建/更新的基準只在記憶體裡。長命行程下一輪開頭會補存,但
    `--once` 跑完就結束 —— 不在這裡 flush,新行程永遠拿不到任何基準,每次都是
    「第一次觀測」→ 牆上時鐘年齡 → 喚醒守衛在 --once 上等於沒裝。"""
    with _CLOCK_LOCK:
        if _CLOCK["prev_wall"] is None:
            return                              # 這個行程還沒 tick 過,沒東西可存
        _save_clock_state(_CLOCK["prev_wall"], _CLOCK["prev_awake"])


def _note_tick() -> float:
    """每個 tick 開頭呼叫一次。回本 tick 觀測到的時鐘跳動秒數(Δwall − Δawake)。

    |跳動| > CLOCK_JUMP_TOLERANCE_SEC → 本 tick 隔離(`log_status` 一律回
    LOG_CLOCK_JUMP、呼叫端不以新鮮度動手),並把所有 log 的進展基準重設為「現在」——
    下一個 tick 起年齡從 0 用醒著的時間重新累積。★隔離只持續這一個 tick★:下一 tick
    差值回到 0 就恢復正常判定;程式真的卡死,仍會在 max_stale 醒著的秒數後被抓到
    (抑制要有出口)。
    """
    wall, awake = _wall_now(), _awake_now()
    with _CLOCK_LOCK:
        pw, pa = _CLOCK["prev_wall"], _CLOCK["prev_awake"]
        if pw is None:
            # 本行程第一個 tick(含 --once):拿落盤的上一 tick 與各 log 基準來比。
            pw, pa, logs = _load_clock_state()
            if not _CLOCK["logs"] and logs:
                _CLOCK["logs"] = logs
        jump = 0.0 if pw is None else (wall - pw) - (awake - pa)
        quarantined = abs(jump) > CLOCK_JUMP_TOLERANCE_SEC
        _CLOCK.update(prev_wall=wall, prev_awake=awake,
                      quarantined=quarantined, jump=jump)
        if quarantined:
            for rec in _CLOCK["logs"].values():
                rec["seen_awake"] = awake          # 進展基準重設:從現在開始觀察
            logging.warning(
                "[watchdog] 牆上時鐘與醒著時間差了 %+.0fs → 系統剛睡過或時鐘被調;"
                "本輪不以 log 新鮮度動手(不 kill、不啟動),進展基準重設", jump)
        _save_clock_state(wall, awake)
    return jump


def _observe_progress(log_path: Path, mtime: float, size: int) -> float:
    """回這個 log「最後一次觀察到變化」距今★醒著★的秒數。

    第一次觀測沒有進展歷史 → 只能用牆上時鐘的 mtime 年齡(誠實的退路);但★本 tick
    若已隔離,連這個退路都不能用★(牆上時鐘剛跳過,mtime 年齡正是不可信的那個數),
    改從現在開始觀察 —— 否則隔離結束後的第一個 tick 會拿灌水的年齡去 kill。
    """
    key = os.path.normcase(os.path.abspath(str(log_path)))
    now_awake = _awake_now()
    with _CLOCK_LOCK:
        rec = _CLOCK["logs"].get(key)
        if rec is None:
            age = 0.0 if _CLOCK["quarantined"] else max(0.0, _wall_now() - mtime)
            _CLOCK["logs"][key] = {"mtime": float(mtime), "size": int(size),
                                   "seen_awake": now_awake - age}
            return age
        if (float(mtime), int(size)) != (rec["mtime"], rec["size"]):
            rec.update(mtime=float(mtime), size=int(size), seen_awake=now_awake)
            return 0.0
        return max(0.0, now_awake - rec["seen_awake"])


def log_status(log_path: Path, max_stale_sec: int) -> tuple:
    """(stale?, age_sec, status) — max_stale_sec <= 0 表示跳過。

    ★「不在」「讀不到」與「還很新」是三件事★(外審 R3-P3-01):舊版把前兩者
    都壓成 `stale=False` —— 也就是「這支程式很健康」。而「行程在跑、log 檔卻
    根本不存在」正是 logging 壞掉的樣子(見 `logging_setup` 那條:設定之前
    有人 module-level `logging.warning`,檔案 handler 就永遠裝不上)。
    壓成一格之後,watchdog ★永遠不會察覺★。

    ★這一批只把三態分出來、據實記一筆,【不改重啟行為】★:
    因為「log 不在就重啟」會在 log 路徑設錯/剛啟動還沒建檔時變成重啟迴圈,
    而那個狀況★不會自己解除★(2026-08-05 事故的教訓:fail-closed 前要先
    證明狀況會解除)。要不要升級成重啟,由使用者看過實機的紀錄再決定。
    """
    if max_stale_sec <= 0:
        return False, 0.0, LOG_OK
    if not log_path.exists():
        return False, 0.0, LOG_ABSENT
    try:
        st = log_path.stat()
    except Exception:
        return False, 0.0, LOG_UNREADABLE
    # [第九輪 §5] 年齡 = 最後一次觀察到變化距今★醒著★多久,不是「mtime 與牆上時鐘差幾秒」。
    age = _observe_progress(log_path, st.st_mtime, st.st_size)
    if _CLOCK["quarantined"]:
        # 本 tick 牆上時鐘剛跳動/系統剛睡過:新鮮度無從判斷 → 第四態,呼叫端不動手。
        return False, age, LOG_CLOCK_JUMP
    return age > max_stale_sec, age, LOG_OK


def is_log_stale(log_path: Path, max_stale_sec: int) -> tuple:
    """(stale?, age_sec) —— `log_status()` 的相容包裝。

    ★舊介面刻意保留原本的回傳形狀★:在意「不知道」的呼叫端改用
    `log_status()`(與 `ensure_single_instance` / `acquire_single_instance`
    同一個作風)。
    """
    stale, age, _status = log_status(log_path, max_stale_sec)
    return stale, age


# ─── Action lock：避免 B+C 同時 kill+restart 同一個程式 ─────────────────
def _coerce_int(value, default: int, *, min_value: int | None = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    if min_value is not None:
        out = max(min_value, out)
    return out


def _coerce_float(value, default: float, *, min_value: float | None = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        out = default
    if min_value is not None:
        out = max(min_value, out)
    return out


def get_loop_timing(cfg: dict) -> tuple[int, int]:
    """Return (heartbeat_log_sec, check_interval_sec) with safe bounds."""
    heartbeat = min(
        3600,
        _coerce_int(cfg.get("heartbeat_log_sec", 300), 300, min_value=1),
    )
    # [v8 2026-05-25] default 30→60 (見 DEFAULT_CONFIG 註解)
    interval = min(
        300,
        _coerce_int(cfg.get("check_interval_sec", 60), 60, min_value=5),
    )
    return heartbeat, interval


def _should_log_action_message(msg: str) -> bool:
    """Return True for watchdog messages worth persisting to logs."""
    return msg.startswith(("▶", "⟳", "✗", "⚠", "⛔"))


def _lock_path_for(prog_name: str) -> Path:
    safe = "".join(c if c.isalnum() else "_" for c in prog_name)
    return LOCK_DIR / f"{safe}.lock"


def release_action_lock(prog_name: str) -> bool:
    """撤回★本行程自己剛建立★的動作鎖(只在「這一輪最後沒動手」時用)。

    [R9-§6 外審 r1 P2-1] 三個動手點都是先 `claim_action_lock`(留 90s 檔)再問授權;
    鎖忙/寫入失敗而不動手時,那個檔會讓下一 tick 又被「lock 還新」擋掉 —— 「下輪再判」
    就變成兩輪後。★只撤自己的★:鎖檔 payload 是 `<pid> <ts>`,pid 不是自己就不動
    (那是別的 watchdog 剛動過手,節流仍然要成立)。回 True=確實撤了。
    """
    try:
        lock = _lock_path_for(prog_name)
        try:
            owner = lock.read_bytes().split(b" ", 1)[0].decode("ascii", "replace")
        except FileNotFoundError:
            return False
        if owner != str(os.getpid()):
            logging.debug("[watchdog] 動作鎖不是本行程的(owner=%s)→ 不撤", owner)
            return False
        lock.unlink()
        return True
    except Exception:
        logging.debug("[watchdog] 撤回動作鎖失敗 (%s)", prog_name, exc_info=True)
        return False


def claim_action_lock(prog_name: str, max_age_sec: int) -> bool:
    """嘗試取得「我要對 prog_name 動手」的 lock。
    若 lock 檔存在且 < max_age_sec 內被改過 → 別人剛動過手，回 False。
    否則寫入新 lock 並回 True。
    """
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock = _lock_path_for(prog_name)
        payload = f"{os.getpid()} {time.time():.0f}".encode("utf-8")

        for _ in range(3):
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - lock.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age < max_age_sec:
                    return False
                try:
                    lock.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    logging.warning(
                        "[watchdog] stale lock 移除失敗，跳過本輪動作 (%s)",
                        prog_name,
                        exc_info=True,
                    )
                    return False
                continue
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(payload)
                return True
        return False
    except Exception:
        logging.exception("[watchdog] lock 操作失敗 (%s)", prog_name)
        return False  # Fail closed: wait for the next tick instead of risking duplicate restarts.


# ─── 單一程式 tick ──────────────────────────────────────────────────────
def ensure_program(prog: dict, pythonw: str, procs: list,
                    my_pid: int, mode: str, cfg: dict) -> str:
    """檢查單一程式，必要時 kill+restart。回傳行動描述（給 log 用）。

    mode:
      'inner' — main.py 裡的 thread；跳過 outer_only=true 的程式
      'outer' — schtasks 觸發；non-主程式的 max_stale_sec 自動乘 multiplier
    """
    name = prog.get("name", "?")
    if not prog.get("enabled", True):
        return f"○ {name}: disabled"
    if mode == "inner" and prog.get("outer_only", False):
        return f"○ {name}: outer_only (跳過)"

    # [2026-07-26 審查 ★重複打卡 / 外審 R1★] session 0 的 watchdog【什麼補救動作都不做】。
    # 閘門必須在這裡(kill 與 restart 記帳之前),不能只擋 start_program:
    # kill+restart 是一筆交易,先砍了才發現不能起 → 互動 session 的打卡程式被砍掉又沒被
    # 補回來,比放著不管更糟(可能整段錯過打卡);而且每次被拒的啟動都會被記成一次重啟嘗試,
    # 週期性 task 每 2 分鐘跑一次 → 很快誤觸 crash-loop 判定、把自動更新停掉一小時。
    # 檔案 851-853 附近的既有不變式也寫著「不能重啟就不可以砍」。
    # 只擋補救動作;偵測/log 照跑,互動 session 的 watchdog 仍會正常把它救起來。
    if current_session_id() == 0:
        return (f"○ {name}: 跳過 (watchdog 跑在 session 0,非互動 —— 從這裡啟動的程式"
                f"使用者看不到,且單例 mutex 是 Local\\ 擋不住跨 session 的第二份,"
                f"會造成重複執行/重複打卡。請重跑「安裝開機自動啟動」讓排程加上 /IT)")

    # [v4] per-machine opt-in：required_config_file 不存在 → 本機不該跑這支
    # 程式 (e.g. 沒設定過打卡的電腦，autoclock_config.json 不會存在；其他電腦
    # 跑主程式時不該被打卡 popup 騷擾)。
    req_cfg = prog.get("required_config_file", "")
    if req_cfg:
        req_path = _ROOT / req_cfg
        if not req_path.exists():
            return f"○ {name}: 跳過 (本機無 {req_cfg} → 此功能未在本機設定)"

    keyword = prog.get("process_match", "")
    pyw_rel = prog.get("pyw", "")
    log_rel = prog.get("log_path", "")
    max_stale = _coerce_int(prog.get("max_stale_sec", 0), 0, min_value=0)

    # outer 對 non-主程式 拉長 staleness threshold，避免跟 inner 搶
    if mode == "outer" and not prog.get("outer_only", False) and max_stale > 0:
        mult = _coerce_float(cfg.get("outer_threshold_multiplier", 1.5), 1.5,
                             min_value=1.0)
        max_stale = int(max_stale * mult)

    if not keyword or not pyw_rel:
        return f"⚠ {name}: 缺 process_match 或 pyw 設定"

    pyw_path = _ROOT / pyw_rel
    log_path = _ROOT / log_rel if log_rel else None

    if not pyw_path.exists():
        return f"⚠ {name}: 找不到 {pyw_path}"

    pids = find_matching_pids(procs, keyword, exclude_pid=my_pid)
    action_lock_sec = _coerce_int(cfg.get("action_lock_seconds", 90), 90,
                                  min_value=1)

    # [v8 2026-05-25] psutil 沒找到 → 先試 WMIC fallback。
    # admin process (consult_query / autoclock) 在主程式 admin watchdog thread
    # 用 psutil 經常 NtQueryInformationProcess access denied → cmdline 拿不到，
    # 害 watchdog 每次心跳都走「半死狀態」分支印雜訊，雖然 mutex+log 還能救起來
    # 但訊息誤導 (user 看以為真半死)。WMIC 用不同 API 可拿到 admin process
    # cmdline，把 PID 從這裡補回就走正常 found-PID 路徑。
    if not pids:
        wmic_pids = _wmic_find_pids(keyword, log_on_empty=False)
        if wmic_pids:
            pids = wmic_pids

    # Case 1: 沒找到 PID → 可能真的沒在跑 OR psutil 看不到 cmdline (Windows 偶發)
    if not pids:
        # [v6 Fallback 1 — 最可靠 2026-05-22] Mutex 偵測。
        # admin process 長 uptime 後 psutil 偶發抓不到 cmdline，但 named
        # mutex 偵測完全跳過 cmdline。對打卡 (max_stale_sec=0 沒 log 新鮮度
        # 可查) 而言這是唯一可靠的存活訊號 — 沒這個就會每 30s 啟新 instance
        # → 撞 mutex 跳「已在執行中」對話框。
        #
        # [2026-05-22 v36] 但 mutex held ≠ scheduler thread alive！
        # 進程還在 (mutex 持有) 但 thread 凍住 (log 不更新) → 半死狀態。
        # 必須同時檢查 log 新鮮度，凍住的 process 要 kill+restart。
        # 今天 (5-22 12:15-13:40) 就是這個 bug 害會診沒寄信 — watchdog
        # heartbeat 每 5 分鐘正常但每次都「mutex 仍 hold 視為健在」直接 return。
        mutex_name = prog.get("mutex_name", "")
        mutex_held = False
        if mutex_name:
            try:
                from cmuh_common.single_instance import is_instance_running
                mutex_held = is_instance_running(mutex_name)
            except Exception:
                logging.debug("[watchdog] mutex 偵測例外", exc_info=True)

        # mutex 持有 + log 新鮮 → 真的健在
        if mutex_held:
            # ★這條分支也要走三態★(外審 R3 剩餘批 P1):原本用
            #   `exists()/stat()` 自己判,於是「log 檔不在」直接掉到下面的
            #   「mutex 確認健在」—— 那正是這一批要修的靜音失敗。
            _m_stale, _m_age, _m_state = log_status(log_path, max_stale)                 if log_path is not None else (False, 0.0, LOG_OK)
            if _m_state == LOG_CLOCK_JUMP:
                # [第九輪 §5] mutex 持有 + 時鐘剛跳動:「log 沒更新」是睡眠/調時造成的
                # 假象,不可以走下面的半死 kill 路徑。本輪只觀察。
                return (f"⏭ {name}: mutex 持有，但時鐘剛跳動/系統剛喚醒，"
                        f"log 新鮮度本輪無從判斷，不動手 [{mode}]")
            if log_path is not None and max_stale > 0 and _m_state != LOG_OK:
                logging.warning(
                    "[watchdog] %s 的 log %s(%s)→ ★無從判斷新鮮度★,"
                    "本輪仍視為健在(mutex 持有);若持續出現代表該程式的"
                    " logging 壞了", name,
                    "不存在" if _m_state == LOG_ABSENT else "讀不到", log_path)
            if log_path is not None and max_stale > 0 and _m_state == LOG_OK:
                try:
                    age = _m_age
                    if not _m_stale:
                        # [v16 2026-05-25] 文案改友善 — Windows WMI 對含中文路徑的
                        # cmdline 有 codepage bug (測試確認 WMI BSTR→string 階段就
                        # 已亂碼，PowerShell 也救不回)。每次 fallback 不是錯，
                        # 不該用「psutil 找不到 PID」這種嚇人字眼。
                        return (f"✓ {name}: log {age:.0f}s 前更新，"
                                f"mutex+log 確認健在 [{mode}]")
                    # mutex 仍持有但 log stale → 半死狀態，需要 kill+restart
                    # 但 psutil 找不到 PID，怎麼 kill？用 mutex name 找對應 process
                    logging.warning(
                        "[watchdog] %s: mutex 持有但 log %.0fs 沒更新 (>%ds) — "
                        "process 半死，嘗試找 PID 強制 kill", name, age, max_stale)
                    half_dead_pids = _find_pids_holding_mutex(
                        keyword, mutex_name, pid_name=prog.get("pid_name", ""))
                    if half_dead_pids:
                        if not claim_action_lock(name, action_lock_sec):
                            return (f"⏭ {name}: 半死狀態但 lock 還新，"
                                    f"這輪先跳過 [{mode}]")
                        # [stability] crash-loop 檢查移到 kill 之前：若已在暫停期
                        # 就不要 kill。否則「殺了又拒絕重啟」會讓半死 process 被
                        # 殺死、整段暫停期都沒程式在跑；保留現有(半死)process 至少
                        # 還在，暫停結束後的下一輪才 kill+重啟。
                        _auth = _authorize_restart(name)
                        if _auth in (RESTART_AUTH_LOCK_BUSY, RESTART_AUTH_HISTORY_UNSAVED):
                            # [外審 r1 P2-1] 本輪沒動手 → 撤回剛建立的 90s 動作鎖,
                            # 否則下一 tick 會被「lock 還新」擋掉,「下輪再判」變成空話。
                            release_action_lock(name)
                            why = ("啟動歷史鎖忙（另一個 watchdog 正在寫）"
                                   if _auth == RESTART_AUTH_LOCK_BUSY
                                   else "啟動歷史寫入失敗")
                            return (f"⏭ {name}: 半死但{why}，本輪不授權 kill，"
                                    f"下輪再判 [{mode}]")
                        if _auth != RESTART_AUTH_OK:
                            until = _SUSPENDED_UNTIL.get(name, 0.0)
                            remain = max(0, int(until - time.time()))
                            return (f"⛔ {name}: 半死且 crash loop，暫停 {remain // 60} "
                                    f"分鐘（保留現有 process、不 kill）[{mode}]")
                        killed = kill_pids_verified(
                            half_dead_pids, prog.get("pid_name", ""),
                            prog.get("process_match", ""))
                        if not killed:
                            return (f"⚠ {name}: 半死狀態 PID {half_dead_pids} "
                                    f"kill 失敗，未啟動新 instance 以避免重複 [{mode}]")
                        time.sleep(2)
                        new_pid = start_program(pyw_path, pythonw)
                        if not new_pid:
                            return (f"✗ {name}: 半死狀態已 kill {killed}，"
                                    f"但重新啟動失敗 [{mode}]")
                        return (f"⟳ {name}: mutex 持有但 log {age:.0f}s 沒更新，"
                                f"killed {killed} → 重啟 PID {new_pid} [{mode}]")
                    return (f"⚠ {name}: mutex 持有但 log stale，"
                            f"找不到 PID 無法 kill (建議手動重啟) [{mode}]")
                except Exception:
                    logging.debug("[watchdog] mutex+log 檢查例外", exc_info=True)
            # max_stale=0：沒 log 新鮮度可查，仍視為健在 (原本邏輯)
            # [v16] 文案改友善
            return (f"✓ {name}: mutex 確認健在 "
                    f"({mutex_name.rsplit(chr(92), 1)[-1]}) [{mode}]")

        # [Fallback 2] log 還新鮮 → 程式幾乎肯定健在，psutil 找不到只是
        # cmdline access 失敗。(mutex 沒持有 → 不會誤判)
        if log_path is not None and max_stale > 0 and log_path.exists():
            # [第九輪 §5] 與其他兩處同一個時間基準(進展觀察 + 喚醒守衛)。
            _fb_stale, _fb_age, _fb_state = log_status(log_path, max_stale)
            if _fb_state == LOG_OK and not _fb_stale:
                # [v16] 文案改友善
                return (f"✓ {name}: log {_fb_age:.0f}s 前更新，視為健在 [{mode}]")
            if _fb_state == LOG_CLOCK_JUMP:
                # 找不到 PID 也沒 mutex,但時鐘剛跳過:「log 很舊」不可信 → 本輪不啟動
                # 新 instance(啟動了頂多撞單例退出,但沒必要賭),下一輪再判。
                return (f"⏭ {name}: 時鐘剛跳動/系統剛喚醒，log 新鮮度本輪無從判斷，"
                        f"不啟動新 instance [{mode}]")
            # LOG_UNREADABLE(stat 失敗)維持舊行為:往下走啟動流程。
        if not claim_action_lock(name, action_lock_sec):
            return f"⏭ {name}: 沒在跑，但 lock 還新（別人剛動過手），這輪先跳過"
        # [D] Crash loop 偵測 — 短時間內反覆啟動 → 暫停
        _auth = _authorize_restart(name)
        if _auth in (RESTART_AUTH_LOCK_BUSY, RESTART_AUTH_HISTORY_UNSAVED):
            release_action_lock(name)        # [外審 r1 P2-1] 沒動手就撤回動作鎖
            why = ("啟動歷史鎖忙（另一個 watchdog 正在寫）"
                   if _auth == RESTART_AUTH_LOCK_BUSY else "啟動歷史寫入失敗")
            return (f"⏭ {name}: 沒在跑，但{why}，本輪不授權啟動，下輪再判 [{mode}]")
        if _auth != RESTART_AUTH_OK:
            until = _SUSPENDED_UNTIL.get(name, 0.0)
            remain = max(0, int(until - time.time()))
            return f"⛔ {name}: crash loop 中，暫停 {remain // 60} 分鐘 [{mode}]"
        new_pid = start_program(pyw_path, pythonw)
        if new_pid:
            return f"▶ {name}: 沒在跑，已啟動 (PID {new_pid}) [{mode}]"
        return f"✗ {name}: 沒在跑且啟動失敗 [{mode}]"

    # Case 2: 在跑 → 看 log 新鮮度
    _c2_state, _c2_age = LOG_OK, 0.0
    if log_path is not None and max_stale > 0:
        stale, age, log_state = log_status(log_path, max_stale)
        _c2_state, _c2_age = log_state, age
        if log_state == LOG_CLOCK_JUMP:
            # [第九輪 §5] 在跑 + 時鐘剛跳動:這正是「喚醒後殺健康程式」的那一格。
            # 本輪不 kill、不重啟,只觀察;程式真的卡死,下一輪起用醒著的時間累積年齡,
            # max_stale 秒後照樣抓到。
            return (f"⏭ {name}: PID {pids} 在跑，但時鐘剛跳動/系統剛喚醒，"
                    f"log 新鮮度本輪無從判斷，不動手 [{mode}]")
        if log_state != LOG_OK:
            # ★據實記一筆★:行程在跑、log 檔卻不在/讀不到 —— 那是 logging
            #   壞掉的樣子,而不是「這支程式很健康」。這一批不改重啟行為
            #   (理由見 `log_status` 的說明),但不可以繼續是靜音的。
            logging.warning(
                "[watchdog] %s 的 log %s(%s)→ ★無從判斷新鮮度★,"
                "本輪不當成陳舊;若持續出現代表該程式的 logging 壞了",
                name, "不存在" if log_state == LOG_ABSENT else "讀不到",
                log_path)
        if stale:
            if not claim_action_lock(name, action_lock_sec):
                return (f"⏭ {name}: log {age:.0f}s 沒更新但 lock 還新，"
                        f"這輪先跳過 [{mode}]")
            # [stability] crash-loop 檢查移到 kill 之前（理由同半死路徑）：暫停期
            # 不 kill，避免殺了又不重啟、留下空窗。
            _auth = _authorize_restart(name)
            if _auth in (RESTART_AUTH_LOCK_BUSY, RESTART_AUTH_HISTORY_UNSAVED):
                release_action_lock(name)    # [外審 r1 P2-1] 沒動手就撤回動作鎖
                why = ("啟動歷史鎖忙（另一個 watchdog 正在寫）"
                       if _auth == RESTART_AUTH_LOCK_BUSY else "啟動歷史寫入失敗")
                return (f"⏭ {name}: log 陳舊但{why}，本輪不授權 kill，下輪再判 [{mode}]")
            if _auth != RESTART_AUTH_OK:
                until = _SUSPENDED_UNTIL.get(name, 0.0)
                remain = max(0, int(until - time.time()))
                return (f"⛔ {name}: stale 且 crash loop 中，暫停 {remain // 60} "
                        f"分鐘（保留現有 process、不 kill）[{mode}]")
            killed = kill_pids_verified(pids, prog.get("pid_name", ""),
                                        prog.get("process_match", ""))
            if not killed:
                return (f"⚠ {name}: log {age:.0f}s 沒更新但 PID {pids} "
                        f"kill 失敗，未啟動新 instance 以避免重複 [{mode}]")
            time.sleep(2)
            new_pid = start_program(pyw_path, pythonw)
            if not new_pid:
                return (f"✗ {name}: log {age:.0f}s 沒更新，已 kill PID {killed}，"
                        f"但重新啟動失敗 [{mode}]")
            return (f"⟳ {name}: log {age:.0f}s 沒更新 (>{max_stale}s)，"
                    f"killed PID {killed} → 重啟 PID {new_pid} [{mode}]")

    # ★不可以再 stat 一次★(外審 R3 剩餘批 P3):上面已經取過一次,
    #   而 `LOG_UNREADABLE` 正是「stat 會拋」的那個狀態 —— 再呼叫一次會讓
    #   整個 `ensure_program` 拋出去,`run_one_tick` 只看得到「tick 例外」,
    #   三態就傳不出來了。沿用上面那一次的結果。
    if max_stale > 0 and log_path is not None and _c2_state == LOG_OK:
        return f"✓ {name}: PID {pids}, log {_c2_age:.0f}s 前更新 [{mode}]"
    if max_stale > 0 and log_path is not None:
        return (f"✓ {name}: PID {pids}(log "
                f"{'不存在' if _c2_state == LOG_ABSENT else '讀不到'},"
                f"新鮮度未知)[{mode}]")
    return f"✓ {name}: PID {pids} [{mode}]"


# ─── 跑一輪 ──────────────────────────────────────────────────────────────
def run_one_tick(mode: str, log_fn=None) -> list:
    """跑一輪所有 enabled 程式檢查。mode='inner' 或 'outer'。

    log_fn: 用來決定哪些訊息要寫入 log 的回呼。預設只寫 action/warning，不寫 ✓。
    回傳：[msg, msg, ...]

    [第九輪 §5] 本函式是薄包裝:不論內層從哪一個 early-return 出去,tick 結束一定
    `_flush_clock_state()` —— `--once` 行程跑完就結束,基準不在這裡落盤就永遠丟了。
    """
    try:
        return _run_one_tick_unflushed(mode, log_fn)
    finally:
        _flush_clock_state()


def _run_one_tick_unflushed(mode: str, log_fn=None) -> list:
    # [第九輪 §5] 先記錄時鐘:牆上時鐘與醒著時間的差值決定本 tick 是否隔離。
    # 排在所有 early-return 之前,設定檔讀不到的那一輪也要記,否則下一輪拿到的
    # 「上一 tick」會是更早的,跳動量被放大/縮小。
    _note_tick()
    cfg = load_config()
    # [2026-07-26 審查] 設定檔只是【暫時】讀不到(防毒/備份鎖檔)時,拿到的是記憶體預設值:
    # master_enabled 預設 False 會讓 watchdog 這輪什麼都不做(還好),但若預設是 True,
    # 使用者在本機關掉的程式會被當成「該啟動」而重開,per-machine 的啟用選項也全被忽略。
    # 行動咽喉就在這裡,一律跳過本輪 —— 檔案解鎖後下一輪自然恢復。
    if config_load_failed():
        return ["○ watchdog: 設定檔暫時讀不到 → 本輪不做任何動作(不拿預設值當使用者設定)"]
    # [v5] 總開關：master_enabled=False → watchdog 整個不動 (預設情況)
    if not cfg.get("master_enabled", False):
        return ["○ watchdog: master_enabled=False (已停用，主程式設定頁可開啟)"]
    pythonw = find_pythonw()
    if not pythonw:
        msg = "[watchdog] 找不到 pythonw.exe，跳過這輪"
        logging.warning(msg)
        return [msg]

    procs = list_python_processes()
    my_pid = os.getpid()
    actions = []
    for prog in cfg.get("programs", []):
        try:
            msg = ensure_program(prog, pythonw, procs, my_pid, mode, cfg)
        except Exception:
            logging.exception("[watchdog/%s] tick 例外 (%s)",
                                mode, prog.get("name", "?"))
            msg = f"✗ {prog.get('name','?')}: tick 例外 [{mode}]"
        actions.append(msg)
        # 預設只寫「action / warning」進 log，✓ / ○ / ⏭ 不洗版
        if _should_log_action_message(msg):
            (log_fn or logging.info)(msg)
    return actions
