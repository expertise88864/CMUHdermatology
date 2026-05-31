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

from cmuh_common.atomic_io import atomic_write_json, atomic_write_text, safe_load_json
from cmuh_common.process_launch import launch_python_script

# ─── 路徑 ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
# repo root：src/cmuh_common/.. = src，..再上一層 = root
_ROOT = _HERE.parent.parent
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
AUTO_UPDATE_SUSPEND_FLAG = SETTINGS_DIR / ".auto_update_suspended_until"


def _record_restart_and_check_crash_loop(name: str) -> bool:
    """紀錄一次啟動。回傳 True = 沒進入 crash loop, 可以繼續啟動。
    回傳 False = 已經 crash loop 中，呼叫端應跳過啟動。"""
    now = time.time()
    with _CRASH_LOOP_LOCK:
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
                atomic_write_text(
                    str(AUTO_UPDATE_SUSPEND_FLAG),
                    f"{int(now) + 3600}\n"
                    f"reason: {name} crash loop at {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                    encoding="utf-8")
                logging.critical(
                    "[watchdog] 已寫 %s 暫停 auto-update 1 小時",
                    AUTO_UPDATE_SUSPEND_FLAG)
            except Exception:
                logging.exception("[watchdog] 寫 auto-update suspend flag 失敗")
            # 清歷史避免後續又連續觸發
            hist.clear()
            return False
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
def load_config() -> dict:
    """讀 config；不存在自動寫 default；缺漏鍵 fallback。"""
    if not CONFIG_PATH.exists():
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(str(CONFIG_PATH), DEFAULT_CONFIG, indent=2)
        except Exception:
            logging.exception("[watchdog] 寫預設 config 失敗")
        return _default_config_copy()

    cfg = safe_load_json(str(CONFIG_PATH), default=None)
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


def _read_wmic_python_process_csv() -> str:
    global _wmic_cache_until, _wmic_cache_stdout, _wmic_cache_run
    now = time.monotonic()
    run_fn = subprocess.run
    if now < _wmic_cache_until and _wmic_cache_run is run_fn:
        return _wmic_cache_stdout

    # [v16 2026-05-25] CREATE_NO_WINDOW — admin watchdog tick 每 60s 走 WMIC fallback
    # (因為 admin process 用 psutil 看不到 cmdline)，原本沒設 creationflags 會閃
    # 黑色 console 視窗。Windows-only flag，os.name=='nt' 才有意義。
    _CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            ["wmic", "process", "where",
             "(name='pythonw.exe' or name='python.exe')",
             "get", "ProcessId,CommandLine", "/FORMAT:CSV"],
            capture_output=True, text=True, timeout=10,
            encoding=locale.getpreferredencoding(False), errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        logging.debug("[watchdog] wmic fallback 例外", exc_info=True)
        return _remember_wmic_process_csv("", run_fn, now)

    if r.returncode == 0:
        return _remember_wmic_process_csv(r.stdout or "", run_fn, now)
    return _remember_wmic_process_csv("", run_fn, now)


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


def _find_pids_holding_mutex(process_keyword: str, mutex_name: str = "") -> list:
    """[2026-05-22 v36] 當 psutil 抓不到 cmdline 但已知 mutex 被持有時，
    用 WMIC 突破 psutil 的 admin cmdline 限制。

    保留 backward-compat 簽章 (mutex_name 參數雖未使用，外部 caller 跟 test
    都已綁定)。實際工作委派給 _wmic_find_pids，log_on_empty=True 因為這條
    路徑是 kill 前的 PID 查詢，找不到要警告。
    """
    return _wmic_find_pids(process_keyword, log_on_empty=True)


# ─── Kill + start ───────────────────────────────────────────────────────
def kill_pid(pid: int) -> bool:
    """taskkill /F /PID — 需 admin 才砍得了 admin process。
    [v16 2026-05-25] 加 CREATE_NO_WINDOW 避免閃 console。"""
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except Exception:
        logging.exception("[watchdog] kill PID %s 例外", pid)
        return False


def start_program(pyw_path: Path, pythonw: str) -> int:
    """以 admin 子行程啟動 .pyw（繼承父 process 的 admin token，無 UAC）。"""
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
                    half_dead_pids = _find_pids_holding_mutex(keyword, mutex_name)
                    if half_dead_pids:
                        if not claim_action_lock(name, action_lock_sec):
                            return (f"⏭ {name}: 半死狀態但 lock 還新，"
                                    f"這輪先跳過 [{mode}]")
                        killed = [pid for pid in half_dead_pids if kill_pid(pid)]
                        if not killed:
                            return (f"⚠ {name}: 半死狀態 PID {half_dead_pids} "
                                    f"kill 失敗，未啟動新 instance 以避免重複 [{mode}]")
                        time.sleep(2)
                        if not _record_restart_and_check_crash_loop(name):
                            until = _SUSPENDED_UNTIL.get(name, 0.0)
                            remain = max(0, int(until - time.time()))
                            return (f"⛔ {name}: 半死且 crash loop，"
                                    f"暫停 {remain // 60} 分鐘 [{mode}]")
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
            killed = [pid for pid in pids if kill_pid(pid)]
            if not killed:
                return (f"⚠ {name}: log {age:.0f}s 沒更新但 PID {pids} "
                        f"kill 失敗，未啟動新 instance 以避免重複 [{mode}]")
            time.sleep(2)
            if not _record_restart_and_check_crash_loop(name):
                until = _SUSPENDED_UNTIL.get(name, 0.0)
                remain = max(0, int(until - time.time()))
                return f"⛔ {name}: stale 且 crash loop 中，暫停 {remain // 60} 分鐘 [{mode}]"
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
