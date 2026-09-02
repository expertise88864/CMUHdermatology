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
import json
import locale
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from cmuh_common.atomic_io import (atomic_write_json, safe_load_json,
                                   safe_load_json_ex)
from cmuh_common.paths import pinned_app_dir
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


def _load_restart_history() -> None:
    """[AC-07] 從檔載入啟動歷史/暫停狀態（讓 --once 也能累積 crash-loop 計數）。"""
    try:
        data = safe_load_json(_restart_history_path(), {}) or {}
    except Exception:
        return
    hist = data.get("history")
    if isinstance(hist, dict):
        _RESTART_HISTORY.clear()
        for name, ts in hist.items():
            if isinstance(ts, list):
                _RESTART_HISTORY[str(name)] = [
                    float(t) for t in ts if isinstance(t, (int, float))]
    susp = data.get("suspended_until")
    if isinstance(susp, dict):
        _SUSPENDED_UNTIL.clear()
        for name, until in susp.items():
            if isinstance(until, (int, float)):
                _SUSPENDED_UNTIL[str(name)] = float(until)


def _save_restart_history() -> None:
    """[AC-07] 落盤啟動歷史/暫停狀態。"""
    try:
        atomic_write_json(_restart_history_path(),
                          {"history": _RESTART_HISTORY,
                           "suspended_until": _SUSPENDED_UNTIL})
    except Exception:
        logging.debug("[watchdog] 寫入啟動歷史失敗", exc_info=True)


@contextlib.contextmanager
def _restart_history_lock(timeout_sec: float = 3.0):
    """[codex P2] 跨行程互斥檔案鎖，序列化 crash-loop 歷史的 read-modify-write，避免
    daemon 與 --once 同時 load-modify-save 造成 lost update（掉某程式的啟動記錄、破壞
    crash-loop 偵測）。O_CREAT|O_EXCL 建鎖檔；拿不到就短暫等；逾時 fail-open（寧可極
    罕見的 lost update 也不要卡住整個 watchdog tick）；離開刪鎖檔。與歷史檔同目錄。"""
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
                break                         # 逾時 → fail-open，不持鎖也繼續
            time.sleep(0.02)
        except Exception:
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


def _record_restart_and_check_crash_loop(name: str) -> bool:
    """紀錄一次啟動。回傳 True = 沒進入 crash loop, 可以繼續啟動。
    回傳 False = 已經 crash loop 中，呼叫端應跳過啟動。"""
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
        _save_restart_history()          # [AC-07] 落盤本次啟動記錄
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
    這一支要求 keyword 是某個★引數的檔名本體★:引數的 basename 去掉副檔名
    之後要等於 keyword。
    ★刻意寬容的地方★:引號、大小寫、有沒有副檔名 —— 那些是 Windows 命令列
    的表面差異,不是身分差異。★刻意不寬容的地方★:它必須是【那個引數本身】,
    不能只是某個更長字串的一部分。
    """
    return _tokens_are_target(_split_cmdline_tokens(cmdline or ""),
                              process_keyword)


def _tokens_are_target(tokens, process_keyword: str) -> bool:
    """★判準的本體:吃【已經切好的引數】★

    [外審第五輪 R5-P3-01 第 1 輪 P1] 上一版把這裡寫成只吃字串,於是
    `_cmdline_of_pid_now()` 拿到 psutil 的★引數清單★之後用空白 join 起來、
    再由這裡重新切一次 —— ★引數邊界就這樣被毀掉★:
    一個【單一引數】`C:(路徑)中國醫皮膚科主程式 backup.txt`
    會被切成兩段,第一段的 basename 剛好等於 keyword → 驗證通過 →
    一支毫不相干的程式連同它的子行程被 `taskkill /F /T`。
    那正是這一批要消滅的東西(子字串誤判),被我自己的 join 又放回來一次。
    ★有邊界資訊就不可以丟掉它★:psutil 已經切好了,直接比對它的每一個引數。
    """
    kw = (process_keyword or "").strip().strip(chr(34)).lower()
    if not kw:
        return False
    for token in (tokens or ()):
        t = str(token).strip().strip(chr(34))
        base = os.path.basename(t.replace(chr(92), "/")).lower()
        if base == kw or os.path.splitext(base)[0] == kw:
            return True
    return False


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


def is_log_stale(log_path: Path, max_stale_sec: int) -> tuple:
    """(stale?, age_sec) — max_stale_sec <= 0 表示跳過。"""
    if max_stale_sec <= 0:
        return False, 0.0
    if not log_path.exists():
        return False, 0.0
    try:
        age = time.time() - log_path.stat().st_mtime
        return age > max_stale_sec, age
    except Exception:
        return False, 0.0


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
            if log_path is not None and max_stale > 0 and log_path.exists():
                try:
                    age = time.time() - log_path.stat().st_mtime
                    if age < max_stale:
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
                        if not _record_restart_and_check_crash_loop(name):
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
            try:
                age = time.time() - log_path.stat().st_mtime
                if age < max_stale:
                    # [v16] 文案改友善
                    return (f"✓ {name}: log {age:.0f}s 前更新，視為健在 [{mode}]")
            except Exception:
                pass
        if not claim_action_lock(name, action_lock_sec):
            return f"⏭ {name}: 沒在跑，但 lock 還新（別人剛動過手），這輪先跳過"
        # [D] Crash loop 偵測 — 短時間內反覆啟動 → 暫停
        if not _record_restart_and_check_crash_loop(name):
            until = _SUSPENDED_UNTIL.get(name, 0.0)
            remain = max(0, int(until - time.time()))
            return f"⛔ {name}: crash loop 中，暫停 {remain // 60} 分鐘 [{mode}]"
        new_pid = start_program(pyw_path, pythonw)
        if new_pid:
            return f"▶ {name}: 沒在跑，已啟動 (PID {new_pid}) [{mode}]"
        return f"✗ {name}: 沒在跑且啟動失敗 [{mode}]"

    # Case 2: 在跑 → 看 log 新鮮度
    if log_path is not None and max_stale > 0:
        stale, age = is_log_stale(log_path, max_stale)
        if stale:
            if not claim_action_lock(name, action_lock_sec):
                return (f"⏭ {name}: log {age:.0f}s 沒更新但 lock 還新，"
                        f"這輪先跳過 [{mode}]")
            # [stability] crash-loop 檢查移到 kill 之前（理由同半死路徑）：暫停期
            # 不 kill，避免殺了又不重啟、留下空窗。
            if not _record_restart_and_check_crash_loop(name):
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

    if max_stale > 0 and log_path is not None and log_path.exists():
        age = time.time() - log_path.stat().st_mtime
        return f"✓ {name}: PID {pids}, log {age:.0f}s 前更新 [{mode}]"
    return f"✓ {name}: PID {pids} [{mode}]"


# ─── 跑一輪 ──────────────────────────────────────────────────────────────
def run_one_tick(mode: str, log_fn=None) -> list:
    """跑一輪所有 enabled 程式檢查。mode='inner' 或 'outer'。

    log_fn: 用來決定哪些訊息要寫入 log 的回呼。預設只寫 action/warning，不寫 ✓。
    回傳：[msg, msg, ...]
    """
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
