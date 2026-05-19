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

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ─── 路徑 ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
# repo root：src/cmuh_common/.. = src，..再上一層 = root
_ROOT = _HERE.parent.parent
SETTINGS_DIR = _ROOT / "settings"
CONFIG_PATH = SETTINGS_DIR / "watchdog_config.json"
LOCK_DIR = SETTINGS_DIR / ".watchdog_locks"


# ─── 預設設定 ────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "check_interval_sec": 30,
    "heartbeat_log_sec": 300,
    "outer_threshold_multiplier": 1.5,  # outer C 的 max_stale_sec 乘這個倍率
    "action_lock_seconds": 90,          # 任一程式被 kill+restart 後 90s 內不允許再動
    "programs": [
        {
            "name": "會診查詢",
            "log_path": "settings/consult_query.log",
            "pyw": "中國醫皮膚科會診查詢程式.pyw",
            "process_match": "consult_query",
            "max_stale_sec": 180,  # 新版每 60s 一定有 heartbeat
            "enabled": True,
            "outer_only": False,
        },
        {
            "name": "打卡",
            "log_path": "settings/autoclock.log",
            "pyw": "中國醫皮膚科打卡程式.pyw",
            "process_match": "autoclock",
            "max_stale_sec": 0,    # 打卡 idle 沒 log，只看 process
            "enabled": True,
            "outer_only": False,
        },
        {
            "name": "主程式",
            "log_path": "automation_ui.log",
            "pyw": "中國醫皮膚科主程式.pyw",
            "process_match": "src\\main.py",  # 用路徑 keyword 避免誤抓
            "max_stale_sec": 0,    # 主程式不一定每分鐘寫 log，只看 process
            "enabled": True,
            "outer_only": True,    # 只有 C 檢查（B 在主程式內，不能監看自己）
        },
    ],
}


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
            CONFIG_PATH.write_text(
                json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            logging.exception("[watchdog] 寫預設 config 失敗")
        return json.loads(json.dumps(DEFAULT_CONFIG))

    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("[watchdog] 讀 config 失敗，用記憶體 default")
        return json.loads(json.dumps(DEFAULT_CONFIG))

    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


# ─── pythonw 路徑 ────────────────────────────────────────────────────────
def find_pythonw() -> str:
    """1) python_embed/pythonw.exe  2) PATH 上的 pythonw  3) 找不到 → ''"""
    embed = _ROOT / "python_embed" / "pythonw.exe"
    if embed.exists():
        return str(embed)
    import shutil
    return shutil.which("pythonw.exe") or shutil.which("pythonw") or ""


# ─── Process 列舉 ────────────────────────────────────────────────────────
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


# ─── Kill + start ───────────────────────────────────────────────────────
def kill_pid(pid: int) -> bool:
    """taskkill /F /PID — 需 admin 才砍得了 admin process。"""
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        logging.exception("[watchdog] kill PID %s 例外", pid)
        return False


def start_program(pyw_path: Path, pythonw: str) -> int:
    """以 admin 子行程啟動 .pyw（繼承父 process 的 admin token，無 UAC）。"""
    try:
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        p = subprocess.Popen(
            [pythonw, str(pyw_path)],
            cwd=str(_ROOT),
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            close_fds=True,
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
        if lock.exists():
            age = time.time() - lock.stat().st_mtime
            if age < max_age_sec:
                return False
        lock.write_text(f"{os.getpid()} {time.time():.0f}", encoding="utf-8")
        return True
    except Exception:
        logging.exception("[watchdog] lock 操作失敗 (%s)", prog_name)
        return True  # 失敗時保險選「允許動手」


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

    keyword = prog.get("process_match", "")
    pyw_rel = prog.get("pyw", "")
    log_rel = prog.get("log_path", "")
    max_stale = int(prog.get("max_stale_sec", 0))

    # outer 對 non-主程式 拉長 staleness threshold，避免跟 inner 搶
    if mode == "outer" and not prog.get("outer_only", False) and max_stale > 0:
        mult = float(cfg.get("outer_threshold_multiplier", 1.5))
        max_stale = int(max_stale * mult)

    if not keyword or not pyw_rel:
        return f"⚠ {name}: 缺 process_match 或 pyw 設定"

    pyw_path = _ROOT / pyw_rel
    log_path = _ROOT / log_rel if log_rel else None

    if not pyw_path.exists():
        return f"⚠ {name}: 找不到 {pyw_path}"

    pids = find_matching_pids(procs, keyword, exclude_pid=my_pid)
    action_lock_sec = int(cfg.get("action_lock_seconds", 90))

    # Case 1: 沒在跑 → 啟動
    if not pids:
        if not claim_action_lock(name, action_lock_sec):
            return f"⏭ {name}: 沒在跑，但 lock 還新（別人剛動過手），這輪先跳過"
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
            time.sleep(2)
            new_pid = start_program(pyw_path, pythonw)
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
        if msg.startswith(("▶", "⟳", "✗", "⚠")):
            (log_fn or logging.info)(msg)
    return actions
