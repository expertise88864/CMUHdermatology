# -*- coding: utf-8 -*-
"""中國醫皮膚科守護程式（watchdog）— 監看 會診查詢 / 打卡 等背景程式。

設計目的：
  - 會診查詢卡死（imaplib socket hang、IMAP connection 死亡）
  - 打卡程式 crash / 被誤關
  - 任何長時間執行的程式自己崩潰沒人發現

兩種偵測：
  1. **process-alive check**：靠 cmdline 關鍵字找對應 pythonw.exe 行程，
     沒找到 → 重新啟動
  2. **log-freshness check (max_stale_sec > 0 時)**：log 檔案 mtime 超過
     N 秒沒更新 → 視為卡死，taskkill + 重啟（這跟 consult_query 新版的
     [heartbeat] 機制配合：consult 每 60s 一定寫一筆，>180s 沒寫就一定卡）

設定檔：settings/watchdog_config.json（不存在會自動建立 default）

對守護程式自身：
  - 無 IMAP / 網路 / SQL — 不會 hang
  - 每 5 分鐘寫一筆 [heartbeat] 給人看
  - 重啟用 subprocess.Popen + DETACHED_PROCESS，新 pythonw 不會跟著 watchdog 死

啟動方式：
  雙擊「中國醫皮膚科守護程式.pyw」(會自動以 admin 起來，必要時跳 UAC)
  或加進「安裝開機自動啟動」勾選 → 登入時自動跑（無 UAC）
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path

# ─── 路徑 + sys.path（讓 import cmuh_common 起作用） ──────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# 依賴自動安裝（跟其他 pyw 程式統一）
try:
    from cmuh_common.deps_runtime import ensure_dependencies
    ensure_dependencies(["psutil"])
except Exception:
    pass

import psutil  # noqa: E402

try:
    from cmuh_common.version import CURRENT_VERSION  # noqa: E402
except Exception:
    CURRENT_VERSION = "?.?.?.?"

# ─── 檔案位置 ─────────────────────────────────────────────────────────────
SETTINGS_DIR = _ROOT / "settings"
LOG_PATH = SETTINGS_DIR / "watchdog.log"
CONFIG_PATH = SETTINGS_DIR / "watchdog_config.json"

# ─── 預設設定 ─────────────────────────────────────────────────────────────
# max_stale_sec=0 表示「跳過 log 新鮮度檢查，只看 process 在不在」
DEFAULT_CONFIG = {
    "check_interval_sec": 30,
    "heartbeat_log_sec": 300,
    "programs": [
        {
            "name": "會診查詢",
            "log_path": "settings/consult_query.log",
            "pyw": "中國醫皮膚科會診查詢程式.pyw",
            "process_match": "consult_query",
            "max_stale_sec": 180,   # 新版每 60s 一定有 heartbeat log
            "enabled": True,
        },
        {
            "name": "打卡",
            "log_path": "settings/autoclock.log",
            "pyw": "中國醫皮膚科打卡程式.pyw",
            "process_match": "autoclock",
            "max_stale_sec": 0,     # 打卡 idle 期間沒 log；只看 process
            "enabled": True,
        },
    ],
}


# ─── Logging setup ───────────────────────────────────────────────────────
def _setup_logging() -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 移除既有 handler 避免重複寫
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    # console 也寫一份（從工作管理員開時看得到；ONLOGON pythonw 模式無 console，無影響）
    try:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root.addHandler(ch)
    except Exception:
        pass


# ─── Config ──────────────────────────────────────────────────────────────
def _load_config() -> dict:
    """讀 config；不存在就寫 default。merge 缺漏鍵。"""
    if not CONFIG_PATH.exists():
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(
                json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            logging.info("建立預設 config: %s", CONFIG_PATH)
        except Exception:
            logging.exception("寫預設 config 失敗，用記憶體 default")
        return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy

    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("讀 config 失敗，用記憶體 default")
        return json.loads(json.dumps(DEFAULT_CONFIG))

    # 缺漏鍵 fallback to default
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


# ─── pythonw.exe 找路徑 ──────────────────────────────────────────────────
def _find_pythonw() -> str:
    """1. python_embed/pythonw.exe  2. PATH 上的 pythonw.exe"""
    embed = _ROOT / "python_embed" / "pythonw.exe"
    if embed.exists():
        return str(embed)
    import shutil
    return shutil.which("pythonw.exe") or shutil.which("pythonw") or ""


# ─── Process 列舉 ────────────────────────────────────────────────────────
def _list_python_processes() -> list:
    """[{pid, cmdline}, ...] for pythonw.exe / python.exe (admin 才看得到 admin 的)。"""
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


def _find_matching_pids(procs: list, keyword: str, exclude_pid: int = 0) -> list:
    """cmdline 含 keyword（大小寫不敏感）的 PID 清單。"""
    kw = keyword.lower()
    return [p["pid"] for p in procs
            if kw in p.get("cmdline", "").lower() and p["pid"] != exclude_pid]


# ─── Kill + restart ──────────────────────────────────────────────────────
def _kill_pid(pid: int) -> bool:
    """taskkill /F /PID — 跟 watchdog 同 admin level 才砍得了 admin process。"""
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        logging.exception("kill PID %s 例外", pid)
        return False


def _start_program(pyw_path: Path, pythonw: str) -> int:
    """以 admin 子行程啟動 .pyw（繼承 watchdog 的 admin token，無 UAC）。回傳 PID。"""
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
        logging.exception("啟動 %s 失敗", pyw_path)
        return 0


def _is_log_stale(log_path: Path, max_stale_sec: int) -> tuple:
    """(stale?, age_sec) — max_stale_sec=0 表示跳過檢查。"""
    if max_stale_sec <= 0:
        return False, 0.0
    if not log_path.exists():
        return False, 0.0
    try:
        mtime = log_path.stat().st_mtime
        age = time.time() - mtime
        return age > max_stale_sec, age
    except Exception:
        return False, 0.0


# ─── 單個程式 tick ───────────────────────────────────────────────────────
def _ensure_program(prog: dict, pythonw: str, procs: list, my_pid: int) -> str:
    """檢查單一程式，必要時 kill + 重啟。回傳行動描述（給 log 用）。"""
    name = prog.get("name", "?")
    keyword = prog.get("process_match", "")
    pyw_rel = prog.get("pyw", "")
    log_rel = prog.get("log_path", "")
    max_stale = int(prog.get("max_stale_sec", 0))

    if not keyword or not pyw_rel:
        return f"⚠ {name}: 缺 process_match 或 pyw 設定"

    pyw_path = _ROOT / pyw_rel
    log_path = _ROOT / log_rel if log_rel else None

    if not pyw_path.exists():
        return f"⚠ {name}: 找不到 {pyw_path}"

    pids = _find_matching_pids(procs, keyword, exclude_pid=my_pid)

    # Case 1: 沒在跑 → 啟動
    if not pids:
        new_pid = _start_program(pyw_path, pythonw)
        if new_pid:
            return f"▶ {name}: 沒在跑，已啟動 (PID {new_pid})"
        return f"✗ {name}: 沒在跑且啟動失敗"

    # Case 2: 在跑 → 看 log 新鮮度
    if log_path is not None:
        stale, age = _is_log_stale(log_path, max_stale)
        if stale:
            killed = []
            for pid in pids:
                if _kill_pid(pid):
                    killed.append(pid)
            time.sleep(2)
            new_pid = _start_program(pyw_path, pythonw)
            return (f"⟳ {name}: log {age:.0f}s 沒更新 (>{max_stale}s)，"
                    f"killed PID {killed} → 重啟 PID {new_pid}")

    if max_stale > 0 and log_path is not None and log_path.exists():
        age = time.time() - log_path.stat().st_mtime
        return f"✓ {name}: PID {pids}, log {age:.0f}s 前更新"
    return f"✓ {name}: PID {pids}"


# ─── Main loop ───────────────────────────────────────────────────────────
def main() -> int:
    _setup_logging()
    logging.info("=" * 60)
    logging.info("=== 守護程式啟動 v%s ===", CURRENT_VERSION)

    pythonw = _find_pythonw()
    if not pythonw:
        logging.error("找不到 pythonw.exe — 無法啟動任何目標程式")
        return 1
    logging.info("pythonw = %s", pythonw)

    cfg = _load_config()
    interval = int(cfg.get("check_interval_sec", 30))
    heartbeat_interval = int(cfg.get("heartbeat_log_sec", 300))
    logging.info("check_interval=%ds heartbeat=%ds", interval, heartbeat_interval)
    logging.info("監看程式：%s",
                  [{"name": p["name"], "enabled": p.get("enabled", True)}
                   for p in cfg.get("programs", [])])

    my_pid = os.getpid()
    last_heartbeat = 0.0
    last_status = []

    # 首輪立刻檢查（不等 interval）
    while True:
        try:
            cfg = _load_config()  # 允許 hot-reload (改 config 不用重啟)
            procs = _list_python_processes()
            actions = []
            for prog in cfg.get("programs", []):
                if not prog.get("enabled", True):
                    continue
                try:
                    msg = _ensure_program(prog, pythonw, procs, my_pid)
                except Exception:
                    logging.exception("[%s] tick 例外", prog.get("name", "?"))
                    msg = f"✗ {prog.get('name','?')}: tick exception"
                actions.append(msg)
                # ✓ 健康狀態的 msg 不洗版，只在 action 或狀態變動時寫 log
                if msg.startswith(("▶", "⟳", "✗", "⚠")):
                    logging.info(msg)

            # heartbeat 強制寫
            if time.time() - last_heartbeat >= heartbeat_interval:
                logging.info("[heartbeat] watchdog alive — %s",
                              " | ".join(actions) if actions else "no enabled programs")
                last_heartbeat = time.time()
            last_status = actions
        except Exception:
            logging.exception("watchdog tick 整輪例外")

        time.sleep(max(5, int(cfg.get("check_interval_sec", 30))))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logging.info("收到 Ctrl-C，離開")
        sys.exit(0)
