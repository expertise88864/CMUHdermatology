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
# 【重要】process_match 必須能在 pythonw.exe 的 cmdline 找到。.pyw shim 用
# runpy.run_path("src/foo.py") 動態載入 src/*.py，cmdline 上只有 .pyw 路徑，
# 沒有 src/foo.py 字串。所以 process_match 必須是 .pyw 中文檔名 (cmdline 一定含)。
# psutil 在 Windows 用 UTF-16 取 cmdline，Chinese keyword 安全可比對。
#
# 【required_config_file】v4 新增：本機沒這檔 → 不啟動 (per-machine opt-in)
# 打卡 / 會診查詢 全皮膚科只需「一台」電腦執行，靠對應 config 檔存在與否
# 自動判斷本機是否該跑。沒設定過該功能的電腦不會被打擾。
CONFIG_SCHEMA_VERSION = 5

DEFAULT_CONFIG = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    # 【總開關 v5】預設關閉 — 新裝機/沒設定過任何背景程式的電腦完全不會
    # 跑 watchdog。主程式設定頁有勾選 UI 可開啟。
    "master_enabled": False,
    "check_interval_sec": 30,
    "heartbeat_log_sec": 300,
    "outer_threshold_multiplier": 1.5,  # outer C 的 max_stale_sec 乘這個倍率
    "action_lock_seconds": 90,          # 任一程式被 kill+restart 後 90s 內不允許再動
    "programs": [
        {
            "name": "會診查詢",
            "log_path": "settings/consult_query.log",
            "pyw": "中國醫皮膚科會診查詢程式.pyw",
            "process_match": "中國醫皮膚科會診查詢程式",
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
            "max_stale_sec": 0,    # 打卡 idle 沒 log，只看 process
            "enabled": True,
            "outer_only": False,
            "required_config_file": "settings/autoclock_config.json",
        },
        {
            "name": "主程式",
            "log_path": "automation_ui.log",
            "pyw": "中國醫皮膚科主程式.pyw",
            "process_match": "中國醫皮膚科主程式",
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
                AUTO_UPDATE_SUSPEND_FLAG.parent.mkdir(parents=True, exist_ok=True)
                AUTO_UPDATE_SUSPEND_FLAG.write_text(
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

    # Schema migration (v1 → v2 修 process_match)
    cfg, migrated = _migrate_config(cfg)
    if migrated:
        try:
            CONFIG_PATH.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            logging.info("[watchdog] config 升級至 schema v%d",
                          CONFIG_SCHEMA_VERSION)
        except Exception:
            logging.exception("[watchdog] 寫回升級後 config 失敗 (本次仍用新版記憶體)")
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

    # Case 1: 沒找到 PID → 可能真的沒在跑 OR psutil 看不到 cmdline (Windows 偶發)
    if not pids:
        # [穩定性] Fallback：log 還新鮮 → 程式幾乎肯定健在，psutil 找不到只是
        # cmdline access 失敗。
        if log_path is not None and max_stale > 0 and log_path.exists():
            try:
                age = time.time() - log_path.stat().st_mtime
                if age < max_stale:
                    return (f"~ {name}: psutil 找不到 PID 但 log {age:.0f}s "
                            f"前剛更新 (<{max_stale}s)，視為健在 [{mode}]")
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
        if msg.startswith(("▶", "⟳", "✗", "⚠")):
            (log_fn or logging.info)(msg)
    return actions
