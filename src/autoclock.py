# -*- coding: utf-8 -*-
"""中國醫皮膚科自動打卡程式（重構自 中國醫皮膚科打卡程式.pyw）。

【保留】所有時段常數、LOCATORS、exponential_backoff_sleep、prune_debug_dumps、
       Mutex 單例、原子寫設定、winotify 通知、托盤等行為。
【新增】線上更新（原本沒有）、ChromeDriver 路徑磁碟快取（原只快取在記憶體）。
【修正】os._exit(0) → sys.exit(0) 讓 atexit handler 跑完。
"""
from __future__ import annotations

import os
import sys

# === 必須在最前面：把 src/ 加到 sys.path ===
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# === 自動依賴安裝 ===
from cmuh_common.deps_runtime import ensure_dependencies  # noqa: E402

REQUIRED_LIBS = [
    ("requests", "requests"),
    ("schedule", "schedule"),
    ("pystray", "pystray"),
    ("Pillow", "PIL"),
    ("selenium", "selenium"),
    ("webdriver-manager", "webdriver_manager"),
    ("winotify", "winotify"),
    ("pywin32", "win32gui"),
]
ensure_dependencies(REQUIRED_LIBS)

# === 主要 import ===
import ctypes  # noqa: E402
import logging  # noqa: E402
import queue  # noqa: E402
import random  # noqa: E402
import threading  # noqa: E402
import time as time_module  # noqa: E402
import tkinter as tk  # noqa: E402
import traceback  # noqa: E402
from datetime import date, datetime, time as dt_time  # noqa: E402
from pathlib import Path  # noqa: E402
from tkinter import messagebox, scrolledtext, simpledialog, ttk  # noqa: E402

import schedule  # noqa: E402
from selenium.common.exceptions import (  # noqa: E402
    StaleElementReferenceException, TimeoutException, WebDriverException,
)
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.support import expected_conditions as EC  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

from clock.webdriver_setup import initialize_driver  # noqa: E402
from cmuh_common.atomic_io import atomic_write_json, safe_load_json  # noqa: E402
from cmuh_common.logging_setup import (  # noqa: E402
    attach_queue_handler,
    attach_stream_handler,
    setup_logging,
)
from cmuh_common.paths import get_app_dir, get_settings_dir, restart_self  # noqa: E402
from cmuh_common.platform_win import set_dpi_awareness  # noqa: E402
from cmuh_common.single_instance import ensure_single_instance, release_single_instance  # noqa: E402
from cmuh_common.task_gate import ActiveTaskGate  # noqa: E402
from cmuh_common.version import CURRENT_VERSION  # noqa: E402

try:
    from winotify import Notification as WinotifyNotification  # type: ignore[import-not-found]
    WINOTIFY_AVAILABLE = True
except ImportError:
    WinotifyNotification = None
    WINOTIFY_AVAILABLE = False

try:
    import win32con  # type: ignore[import-not-found]  # noqa: E402
    import win32console  # type: ignore[import-not-found]  # noqa: E402
    import win32gui  # type: ignore[import-not-found]  # noqa: E402
    WINDOWS_API_AVAILABLE = True
except ImportError:
    # 綁成 None（而非完全不綁）：所有使用處都有 WINDOWS_API_AVAILABLE 守衛，不會真的
    # 用到；但讓名稱無條件 bound，未來若有人漏加守衛也是清楚的 AttributeError 而非
    # NameError，且消除靜態分析的 possibly-unbound 雜訊。
    win32con = win32console = win32gui = None  # type: ignore[assignment]
    WINDOWS_API_AVAILABLE = False

# =============================================================================
# 路徑與設定
# =============================================================================
BASE_DIR = Path(get_app_dir())
SETTINGS_DIR = Path(get_settings_dir())
DEBUG_DUMPS_DIR = SETTINGS_DIR / "debug_dumps"
MAX_DEBUG_DUMP_FILES = 40
try:
    DEBUG_DUMPS_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

CONFIG_FILE = SETTINGS_DIR / "autoclock_config.json"
LOG_FILE = SETTINGS_DIR / "autoclock.log"
ICON_FILE = BASE_DIR / "assets" / "AutoClockIcon.png"
if not ICON_FILE.exists():  # 兼容舊路徑
    legacy = BASE_DIR / "AutoClockIcon.png"
    if legacy.exists():
        ICON_FILE = legacy

TOAST_APP_ID = "CMUH.SkinDept.AutoClock"
ADD_NEW_ACCOUNT_TEXT = "+ 新增帳號"
SCRIPT_NAME = os.path.basename(__file__)
AUTOCLOCK_MUTEX_NAME = "Local\\CMUH_Skin_AutoClock_SingleInstance_v1"

accounts_data: list = []
_config_lock = threading.Lock()
running = threading.Event()
running.set()
background_thread: threading.Thread | None = None
tray_icon_object = None
_exit_lock = threading.Lock()
_exit_started = False
log_queue: queue.Queue = queue.Queue(maxsize=5000)
LOG_POLL_MAX_RECORDS = 200
clock_lock = threading.RLock()  # 【穩定性 2026-05-21】RLock 避免 janitor 與 process_clock_task 重入時 deadlock
_clock_task_gate = ActiveTaskGate(stale_after_sec=90 * 60)
_test_login_gate = ActiveTaskGate(stale_after_sec=10 * 60)

# [2026-05-22 v45 P0-1] scheduler liveness — 給 self-watchdog 用，跟 consult_query
# 同一套 pattern。每次 scheduler_loop iteration 更新 last_tick；watchdog 偵測
# > 180s 沒 tick 視為 thread 卡死，> 20s 沒解套就 os._exit(1) 讓 process 重啟。
_AUTOCLOCK_LIVENESS = {"last_tick": 0.0}
_scheduler_thread_ref: threading.Thread | None = None
_self_watchdog_thread_ref: threading.Thread | None = None
_self_watchdog_lock = threading.Lock()

# [W12 2026-07-03] 目前正在跑的打卡任務(供 self-watchdog 偵測「任務卡太久」——
# scheduler 仍每分鐘 tick,但單一 Selenium 打卡任務可能 hang;原本 watchdog 只看
# scheduler tick、看不到卡住的任務)。started 為 monotonic 秒;0=無任務在跑。
import contextlib as _contextlib  # noqa: E402
_active_clock_task = {"started": 0.0, "label": ""}
_active_clock_task_lock = threading.Lock()
# 單一打卡任務跑超過這個秒數即視為異常(driver 逾時 30s×步驟 + 5 次重試,正常遠低於此)。
_ACTIVE_TASK_WARN_SEC = 300


@_contextlib.contextmanager
def _active_clock_task_scope(label):
    with _active_clock_task_lock:
        _active_clock_task["started"] = time_module.monotonic()
        _active_clock_task["label"] = label or ""
    try:
        yield
    finally:
        with _active_clock_task_lock:
            _active_clock_task["started"] = 0.0
            _active_clock_task["label"] = ""


def _active_clock_task_age():
    """回 (label, age_sec);無任務在跑回 (None, 0.0)。純讀,供 watchdog 判斷卡住。"""
    with _active_clock_task_lock:
        started = _active_clock_task["started"]
        label = _active_clock_task["label"]
    if started <= 0:
        return None, 0.0
    return label, time_module.monotonic() - started


def _sleep_while_running(seconds: float, step: float = 0.5) -> bool:
    """Sleep up to seconds, but return quickly after running.clear()."""
    deadline = time_module.monotonic() + max(0.0, float(seconds))
    step = max(0.05, float(step))
    while running.is_set():
        remaining = deadline - time_module.monotonic()
        if remaining <= 0:
            return True
        time_module.sleep(min(step, remaining))
    return False


# =============================================================================
# [autoclock 常駐 Chrome 池]
# 原本每個排程任務都新開 Chrome（~3 秒啟動）；改成跨任務重用同一 driver。
# 60 分鐘 idle 自動 quit，避免常駐記憶體無止境吃。
# 排程結束（程式關閉）時 atexit 確保 quit。
# =============================================================================
_persistent_driver_pool = {
    "driver": None,
    "last_used": 0.0,
    "in_use": False,  # [stability r4] 任務使用中旗標：True 時 idle 回收器不得 quit driver
    "lock": threading.Lock(),
    "init_lock": threading.Lock(),
}
_PERSISTENT_DRIVER_IDLE_TIMEOUT = 15 * 60  # 15 分鐘無使用 → 主動 quit
# 註：原本 60 分鐘但 idle 期間沒有人 wake 起來檢查，driver 等於永遠不釋放。
# 改 15 分鐘 + scheduler_loop 每分鐘主動檢查 → 兩批打卡之間 (08:00/12:00/12:30/
# 17:30/18:00) 中間 4 小時都會被釋放，省 ~150-250MB Chrome 記憶體。下次任務
# 重新 spin up 3-5 秒。
_CLOCK_DRIVER_PAGE_LOAD_TIMEOUT = 30
_CLOCK_DRIVER_SCRIPT_TIMEOUT = 30


def _configure_clock_driver_timeouts(driver) -> None:
    for method_name, timeout_sec in (
        ("set_page_load_timeout", _CLOCK_DRIVER_PAGE_LOAD_TIMEOUT),
        ("set_script_timeout", _CLOCK_DRIVER_SCRIPT_TIMEOUT),
    ):
        try:
            getattr(driver, method_name)(timeout_sec)
        except Exception:
            logging.debug("[autoclock] 設定 WebDriver timeout 失敗: %s",
                          method_name, exc_info=True)


def _get_or_create_clock_driver(_depth: int = 0):
    """取得常駐 driver；若 idle 過久或健康檢查失敗則重建。

    [2026-05-22 v45 P0-2 修補] driver.quit() 移到 lock 外 — 原本持鎖 quit
    若 quit hang (chromedriver 沒回應，最多 30s) → 所有等 lock 的 caller 全卡。
    task #68 標 completed 但只改了部分 path，此處 idle/health-check 仍持鎖 quit。
    """
    pool = _persistent_driver_pool
    old_driver_to_quit = None  # 鎖外才能 quit 的 driver

    with pool["lock"]:
        d = pool["driver"]
        now = time_module.time()

        # idle 過久 → 標記重建 (quit 鎖外做)
        if d is not None and (now - pool["last_used"]) > _PERSISTENT_DRIVER_IDLE_TIMEOUT:
            logging.info("[autoclock] driver idle 超過 %d 分鐘，重建",
                         _PERSISTENT_DRIVER_IDLE_TIMEOUT // 60)
            old_driver_to_quit = d
            d = None
            pool["driver"] = None

    # [AC-04] 健康檢查（window_handles）改到【鎖外】做：chromedriver wedge 時 window_handles
    # 會阻塞，若在 pool lock 內就會連鎖卡住 heartbeat/scheduler 取鎖 → 整個打卡子系統癱瘓。
    # 鎖內只取引用，鎖外探測；探測失敗回鎖內 CAS（仍是同一 driver 才清，免清掉別緒新建的）。
    if d is not None:
        try:
            _ = d.window_handles
        except Exception:
            logging.info("[autoclock] driver 已死，重建")
            with pool["lock"]:
                if pool["driver"] is d:
                    pool["driver"] = None
            old_driver_to_quit = d
            d = None

    # 鎖外 quit 舊 driver — 即使 hang 也不影響其他 thread 取得 pool lock
    if old_driver_to_quit is not None:
        try:
            old_driver_to_quit.quit()
        except Exception:
            logging.debug("[autoclock] 舊 driver quit 例外", exc_info=True)

    # 不存在或剛被清掉 → 重建 (initialize 走網路，務必鎖外)
    if d is None:
        # initialize 不持 pool lock，但仍要確保同時間只有一個 thread 建 driver。
        # 否則兩個 caller 同時看到 None 會各開一個 Chrome，後者覆蓋 pool、前者殘留。
        with pool["init_lock"]:
            with pool["lock"]:
                d = pool["driver"]
                if d is not None:
                    pool["last_used"] = time_module.time()
                    return d

            for attempt in range(4):
                d = initialize_driver()
                if d:
                    break
                logging.warning("[autoclock] WebDriver 初始化失敗 (%s/4)，退避重試", attempt + 1)
                if attempt < 3:
                    exponential_backoff_sleep(attempt, base_seconds=2.0, max_seconds=60.0)
            if d:
                _configure_clock_driver_timeouts(d)
                # 鎖內最後 set 回 pool
                with pool["lock"]:
                    pool["driver"] = d
                    pool["last_used"] = time_module.time()
        return d

    # 走原路徑（既有 driver 仍健康）— 鎖內更新 last_used
    # [codex P2] 健康檢查已移到鎖外做(AC-04),探測「成功」到此的空窗內,別緒可能已把這個
    # driver quit 掉並換成新的(或清成 None)。若直接回傳舊 d,呼叫端會立刻 WebDriver 失敗、
    # 可能漏打卡。鎖內確認 pool 仍是同一 driver 才回傳;被換掉→回傳當前新 driver;被清成
    # None→有限深度重試(走重建路徑)。
    with pool["lock"]:
        current = pool["driver"]
        if current is not None:      # 仍是 d、或別緒剛換上的新 driver → 皆可回傳
            pool["last_used"] = time_module.time()
            return current
    # pool 被別緒清成 None(mid-rebuild)→ 有限深度重試(此時 d 已非權威,重跑會走重建)
    if _depth < 3:
        return _get_or_create_clock_driver(_depth + 1)
    return d


def _release_persistent_clock_driver():
    """關閉常駐 driver（程式退出時呼叫）。

    [2026-05-22 v45 P1-5 修補] 改 taskkill 不走 driver.quit()。
    atexit 路徑可能被卡死的 thread 持有 pool lock；driver.quit() 走 HTTP 到
    chromedriver 也可能 hang 30s。process 退出時應立刻砍掉 chromedriver
    子進程，不等 graceful shutdown。
    """
    pool = _persistent_driver_pool
    # 鎖內只 nullify，鎖外 kill
    with pool["lock"]:
        pool["driver"] = None
    # 直接 taskkill chromedriver / chrome (本 process 啟動的)
    try:
        import psutil as _psutil
        my_pid = os.getpid()
        for p in _psutil.process_iter(["pid", "name", "ppid"]):
            try:
                n = (p.info.get("name") or "").lower()
                if "chromedriver" not in n:
                    continue
                if p.info.get("ppid") != my_pid:
                    continue
                # 連帶 kill 子 chrome
                for ch in p.children(recursive=True):
                    try:
                        ch.kill()
                    except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                        pass
                p.kill()
            except (_psutil.NoSuchProcess, _psutil.AccessDenied, Exception):
                continue
    except Exception:
        logging.debug("[autoclock] release 時 taskkill chromedriver 例外",
                       exc_info=True)


import atexit as _atexit_clock
_atexit_clock.register(_release_persistent_clock_driver)

# =============================================================================
# 業務常數
# =============================================================================
LOGIN_URL = "http://10.20.8.47/peoplesystem/electron_card/login.aspx"

CLOCK_IN_START_TIME = dt_time(7, 31, 0)
CLOCK_IN_END_TIME = dt_time(7, 59, 59)
CLOCK_MIDDAY_IN_START_TIME = dt_time(12, 31, 0)  # [2026-06-01] 比窗起(12:30)晚 1 分觸發，確保打卡落在 1230-1300 內
CLOCK_MIDDAY_IN_END_TIME = dt_time(12, 59, 59)
CLOCK_MIDDAY_OUT_START_TIME = dt_time(12, 0, 0)
CLOCK_MIDDAY_OUT_END_TIME = dt_time(12, 30, 59)
CLOCK_PM_OUT_START_TIME = dt_time(17, 0, 0)
CLOCK_PM_OUT_END_TIME = dt_time(17, 30, 59)
TRIGGER_PM_OUT_START_TIME = dt_time(17, 1, 0)
CLOCK_EVE_OUT_START_TIME = dt_time(21, 1, 0)  # [2026-06-01] 比窗起(21:00)晚 1 分觸發，確保打卡落在 2100-2130 內
CLOCK_EVE_OUT_END_TIME = dt_time(21, 30, 59)

VALIDATION_WINDOWS = {
    "am_in": (dt_time(7, 30, 0), dt_time(8, 0, 0)),
    "midday_out": (dt_time(12, 0, 0), dt_time(12, 30, 0)),
    "midday_in": (dt_time(12, 30, 0), dt_time(13, 0, 0)),
    "pm_out": (dt_time(17, 0, 0), dt_time(17, 30, 0)),
    "eve_out": (dt_time(21, 0, 0), dt_time(21, 30, 0)),
}


class ClockAuthError(Exception):
    """[AC-09] 帳號/密碼錯誤——不可重試(重試也一樣,只會反覆登入失敗、有帳號鎖定風險)。"""


def _clock_window_passed(check_end: dt_time, grace_sec: int = 0) -> bool:
    """[AC-01] 當下是否已超過打卡窗尾(可加緩衝秒)。打卡窗皆日間、不跨午夜。"""
    from datetime import timedelta
    now = datetime.now()
    deadline = datetime.combine(now.date(), check_end) + timedelta(seconds=grace_sec)
    return now > deadline

LOCATORS = {
    "username": ("id", "TB_logid"),
    "password": ("id", "TB_pwd"),
    "login_button": ("id", "bt_login"),
    "work_on_radio": ("id", "Rb_flage_0"),
    "work_off_radio": ("id", "Rb_flage_1"),
    "execute_button": ("id", "bt_electron"),
    "health_button": ("id", "Bt_health"),
    "health_submit": ("id", "btnsave"),
    "system_time": ("id", "lb_systime"),
    "swipe_table": ("id", "Gv_attppre"),
    "login_error_message": ("id", "lblErrorMessage"),
}

# =============================================================================
# Logging
# =============================================================================
def _setup_clock_logging() -> None:
    """打卡程式的特化 logging：RotatingFile + Stream + Queue."""
    setup_logging(str(LOG_FILE), max_bytes=5 * 1024 * 1024, backup_count=2)
    # 加上 stream（保留原行為）
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    attach_stream_handler(formatter, replace_existing=True)
    # 加上 queue handler 給 UI 顯示
    qh = attach_queue_handler(log_queue, replace_existing=True)
    qh.setFormatter(formatter)


# =============================================================================
# 工具函式
# =============================================================================
def _safe_filename_part(text: str, max_len: int = 64) -> str:
    bad = '\\/:*?"<>|\n\r\t'
    s = "".join((c if c not in bad else "_") for c in str(text))[:max_len]
    return s or "unknown"


def exponential_backoff_sleep(attempt_zero_based: int, *,
                              base_seconds: float = 1.5,
                              max_seconds: float = 45.0) -> None:
    """指數退避 + 抖動。"""
    raw = min(max_seconds, base_seconds * (2 ** attempt_zero_based))
    jitter = random.uniform(0, min(2.0, raw * 0.15))
    time_module.sleep(raw + jitter)


# 【重構 2026-05-21】抽到 cmuh_common.date_utils（與 main/scheduler 共用）
from cmuh_common.date_utils import roc_to_gregorian_year, parse_roc_date_str  # noqa: E402


def save_debug_artifacts(driver, filename_prefix: str, error_hint: str = "") -> list:
    saved: list = []
    if driver is None:
        return saved
    try:
        DEBUG_DUMPS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = DEBUG_DUMPS_DIR / f"{_safe_filename_part(filename_prefix, 80)}_{ts}"
    try:
        png_path = base.with_suffix(".png")
        driver.save_screenshot(str(png_path))
        saved.append(png_path)
        logging.info("已儲存除錯截圖: %s", png_path)
    except Exception as e:
        logging.warning("截圖失敗: %s", e)
    try:
        html_path = base.with_suffix(".html")
        src = driver.page_source or ""
        html_path.write_text(src, encoding="utf-8", errors="replace")
        saved.append(html_path)
        logging.info("已儲存頁面 HTML: %s", html_path)
    except Exception as e:
        logging.warning("儲存 HTML 失敗: %s", e)
    if error_hint and saved:
        try:
            meta_path = base.with_suffix(".txt")
            meta_path.write_text(
                f"time={datetime.now().isoformat()}\n{error_hint}\n",
                encoding="utf-8", errors="replace",
            )
            saved.append(meta_path)
        except OSError:
            pass
    return saved


def prune_debug_dumps() -> None:
    try:
        if not DEBUG_DUMPS_DIR.is_dir():
            return
        files = [p for p in DEBUG_DUMPS_DIR.iterdir() if p.is_file()]
        if len(files) <= MAX_DEBUG_DUMP_FILES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[: len(files) - MAX_DEBUG_DUMP_FILES]:
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def notify_clock_failure(title_suffix: str, body_lines, saved_paths=None) -> None:
    if not WINOTIFY_AVAILABLE or WinotifyNotification is None:
        return
    try:
        title = f"自動打卡 — {title_suffix}"
        msg_parts = list(body_lines) if body_lines else []
        if saved_paths:
            msg_parts.append("除錯檔已寫入 settings\\debug_dumps")
            msg_parts.append(saved_paths[0].name[:120])
        msg = "\n".join(msg_parts)[:350]
        icon_arg = str(ICON_FILE.resolve()) if ICON_FILE.is_file() else None
        kw = dict(app_id=TOAST_APP_ID, title=title[:128], msg=msg, duration="long")
        if icon_arg:
            kw["icon"] = icon_arg
        toast = WinotifyNotification(**kw)
        toast.show()
    except Exception as e:
        logging.warning("Windows 通知顯示失敗: %s", e)


def _handle_clock_failure(driver, username: str, task_label: str, exc, dry_run: bool) -> None:
    hint = str(exc) if exc else ""
    if exc and not isinstance(exc, str):
        hint = f"{type(exc).__name__}: {exc}"
    prefix = f"{task_label}_{username}" if task_label else f"fail_{username}"
    paths = save_debug_artifacts(driver, prefix, error_hint=hint)
    prune_debug_dumps()
    logging.error("打卡失敗已存除錯檔 (%s 個): %s", len(paths), paths)
    if not dry_run:
        notify_clock_failure(
            "失敗",
            [f"帳號: {username}", f"排程: {task_label or '-'}",
             hint[:200] if hint else "未知錯誤"],
            paths,
        )


# =============================================================================
# 設定檔讀寫
# =============================================================================
_last_config_warn_set: set = set()


def _validate_accounts(accounts) -> list:
    """[W13 2026-07-03] 純函式:回傳帳號設定的警告清單(不擋啟動、不丟棄帳號 —— 只醒目
    提示使用者修正)。檢查:非 dict 項、缺 username/password、重複 username。"""
    if not isinstance(accounts, list):
        # [codex review] 對非 list 純量(如整數)保持 total,不拋 TypeError
        return [] if not accounts else [f"帳號設定不是清單:{accounts!r}"]
    warnings = []
    seen: dict = {}
    for i, acc in enumerate(accounts):
        if not isinstance(acc, dict):
            warnings.append(f"第 {i + 1} 筆帳號設定不是物件:{acc!r}")
            continue
        u = str(acc.get("username", "") or "").strip()
        if u:
            seen[u] = seen.get(u, 0) + 1
        else:
            warnings.append(f"第 {i + 1} 筆帳號缺 username(該筆無法登入打卡)")
        if not acc.get("password"):
            warnings.append(f"帳號 {u or ('#' + str(i + 1))} 缺 password(該筆無法登入打卡)")
    for u, n in seen.items():
        if n > 1:
            warnings.append(f"username {u} 重複出現 {n} 次(會重複登入/打卡,請刪除多餘筆)")
    return warnings


def _warn_config_issues(accounts) -> None:
    """驗證並【只在警告集合有變化時】記 log,避免每分鐘 load_config 洗版。"""
    global _last_config_warn_set
    cur = set(_validate_accounts(accounts))
    if cur and cur != _last_config_warn_set:
        for w in sorted(cur):
            logging.warning("[autoclock][config] %s", w)
    _last_config_warn_set = cur


def _sanitize_accounts(accounts) -> list:
    """[AC-02] 消毒成 tick 路徑安全可迭代的形狀:剔除非 dict 項、schedule 非 dict
    以 {} 取代(手改 JSON 塞 schedule:null / 非物件項時,避免每分鐘 tick 崩潰整批漏
    打卡)。原始問題仍由 _warn_config_issues 對未消毒資料醒目提示。"""
    out = []
    for acc in (accounts if isinstance(accounts, list) else []):
        if not isinstance(acc, dict):
            continue
        if not isinstance(acc.get("schedule"), dict):
            acc = {**acc, "schedule": {}}
        out.append(acc)
    return out


def load_config() -> list:
    global accounts_data
    with _config_lock:
        data = safe_load_json(str(CONFIG_FILE), default=[])
        accounts_data = data if isinstance(data, list) else []
    _warn_config_issues(accounts_data)  # [W13] 對原始資料醒目提示設定問題(不擋啟動)
    return _sanitize_accounts(accounts_data)  # [AC-02] tick/迴圈路徑一律拿到安全資料


def save_config() -> bool:
    global accounts_data
    with _config_lock:
        try:
            accounts_data.sort(key=lambda x: x.get("username", ""))
            atomic_write_json(str(CONFIG_FILE), accounts_data)
            return True
        except Exception as e:
            logging.error("儲存失敗: %s", e)
            return False


# =============================================================================
# 打卡核心流程
# =============================================================================
def login(driver, wait, username: str, password: str) -> None:
    def get_loc(key):
        return (getattr(By, LOCATORS[key][0].upper()), LOCATORS[key][1])

    max_retries = 5
    for attempt in range(max_retries):
        try:
            driver.get(LOGIN_URL)

            user_elem = wait.until(EC.visibility_of_element_located(get_loc("username")))
            user_elem.clear()
            user_elem.send_keys(username)

            try:
                pwd_elem = wait.until(EC.element_to_be_clickable(get_loc("password")))
                pwd_elem.clear()
                pwd_elem.send_keys(password)
            except StaleElementReferenceException:
                pwd_elem = driver.find_element(*get_loc("password"))
                pwd_elem.clear()
                pwd_elem.send_keys(password)

            login_btn = driver.find_element(*get_loc("login_button"))
            driver.execute_script("arguments[0].click();", login_btn)

            # [AC-09] 錯帳密多半跳 JS alert(非 lblErrorMessage 元素)。先處理掉並分類為
            # 不可重試,否則 alert 卡住頁面 → 後續 wait 逾時(WebDriverException)被當一般
            # 網路錯誤重試 5x×外層 5x,反覆登入失敗且有 portal 帳號鎖定風險。
            try:
                _alert = WebDriverWait(driver, 2).until(EC.alert_is_present())
                _txt = (_alert.text or "").strip()
                try:
                    _alert.accept()
                except Exception:
                    pass
                raise ClockAuthError(f"登入被拒(可能帳號/密碼錯誤): {_txt[:40]}")
            except TimeoutException:
                pass

            try:
                err_elem = WebDriverWait(driver, 2).until(
                    EC.visibility_of_element_located(get_loc("login_error_message")))
                if err_elem.text.strip():
                    raise ClockAuthError(f"登入失敗: {err_elem.text.strip()[:40]}")
            except TimeoutException:
                pass

            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(get_loc("execute_button")))
            return

        except (StaleElementReferenceException, WebDriverException) as e:
            logging.warning(
                "登入嘗試 %s/%s 遇到問題: %s。重新整理頁面後重試...",
                attempt + 1, max_retries, type(e).__name__,
            )
            if attempt < max_retries - 1:
                exponential_backoff_sleep(attempt, base_seconds=1.25, max_seconds=30.0)
            try:
                driver.refresh()
            except WebDriverException:
                pass

    raise RuntimeError("多次登入失敗，請檢查網路或帳號密碼")


def handle_health_declaration(driver, wait, short_wait, get_loc) -> None:
    orig = driver.current_window_handle
    try:
        btns = short_wait.until(EC.presence_of_all_elements_located(get_loc("health_button")))
    except (TimeoutException, WebDriverException):
        # [opt B4] 找不到健康宣告按鈕 = 今天不需宣告(正常情況)，靜默跳過。
        return
    try:
        btn = next((b for b in btns if b.is_displayed() and b.is_enabled()), None)
        if btn:
            wins_before = driver.window_handles
            try:
                driver.execute_script("arguments[0].click();", btn)
            except WebDriverException:
                btn.click()

            wait.until(EC.number_of_windows_to_be(len(wins_before) + 1))
            new_win = next((w for w in driver.window_handles if w not in wins_before), None)

            if new_win:
                driver.switch_to.window(new_win)
                WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable(get_loc("health_submit"))).click()
                try:
                    WebDriverWait(driver, 5).until(EC.alert_is_present()).accept()
                except TimeoutException:
                    pass
                time_module.sleep(1)
                driver.close()
                driver.switch_to.window(orig)
                wait.until(EC.element_to_be_clickable(get_loc("execute_button")))
    except (TimeoutException, WebDriverException):
        # [opt B4] 偵測到健康宣告按鈕、但後續流程(開窗/送出/切回)失敗 → 不再靜默吞掉
        # (原為 except: pass，連 log 都沒有)。留 warning 供 log / Live Log 排障。
        # 控制流維持原樣(仍 return，呼叫端照常往下點 execute)，不升級為打卡失敗——因為
        # 「健康宣告是否為打卡硬性前置」未確認，誤升級會把『今天不需宣告』誤判成失敗。
        logging.warning(
            "[autoclock] 健康宣告流程失敗(已偵測到按鈕但未完成)，請留意是否需手動宣告",
            exc_info=True)


def get_current_swipe_info(driver, wait, get_loc):
    """回 (sys_date, swipes, last_swipe, read_ok)。
    [W4 2026-07-03] read_ok:是否『成功讀到刷卡表』(即使當日空)。逾時/例外/JS 未回
    list 時為 False —— 呼叫端據此區分「確定無紀錄」vs「讀取失敗」,讀取失敗時絕不打卡
    (重複打卡比晚打卡嚴重),交由重試/下一分鐘 re-fire 重讀。"""
    sys_date = None
    swipes: list = []
    last_swipe = None
    read_ok = False
    try:
        # [2026-07-06] 錨點改用 system_time(lb_systime),不再等 swipe_table(Gv_attppre)。
        # 空的 ASP.NET GridView(當日尚無刷卡紀錄,例如早上第一次上班打卡前)不渲染任何
        # <table> 元素 → 等 Gv_attppre 會逾時 → read_ok=False → 誤判「讀取失敗」而跳過打卡
        # (死結:無紀錄→空表→不渲染→等不到→不打卡→仍無紀錄)。lb_systime 在登入後的打卡頁
        # 一定存在(空表也在),用它確認「已在打卡頁」;下方 JS querySelectorAll 對不存在的表
        # 安全回 []=當日無紀錄,read_ok=True → 可正常打卡。真的沒登入成功時 lb_systime 等不到
        # → 仍逾時 → read_ok=False(安全:不誤打卡)。
        wait.until(EC.presence_of_element_located(get_loc("system_time")))
        try:
            txt = driver.find_element(*get_loc("system_time")).text
            if "年" in txt:
                y = txt.split("年")[0]
                rest = txt.split("年")[1]
                m = rest.split("月")[0]
                d = rest.split("月")[1].split("日")[0]
                gy = roc_to_gregorian_year(y)
                if gy is not None:
                    sys_date = date(gy, int(m), int(d))
        except (ValueError, IndexError, TypeError):
            sys_date = date.today()

        if sys_date is None:
            sys_date = date.today()

        rows_data = driver.execute_script(
            """
            var rows = document.querySelectorAll("#Gv_attppre tbody tr");
            var data = [];
            for (var i = 1; i < rows.length; i++) {
                var cols = rows[i].querySelectorAll("td");
                if (cols.length >= 3) {
                    data.push([cols[0].innerText, cols[1].innerText, cols[2].innerText]);
                }
            }
            return data;
            """
        )
        # [W4] execute_script 成功回 list(即使空)= 確實讀到刷卡表;非 list(None/JS 異常)
        # 視為讀取失敗,不可被下游當成「當日無紀錄」而重複打卡。
        read_ok = isinstance(rows_data, list)

        all_dts = []
        for r in rows_data or []:
            sd = parse_roc_date_str(r[0].strip())
            if sd:
                t_str = r[1].strip()
                if len(t_str) == 4:
                    try:
                        dt = datetime.combine(sd, dt_time(int(t_str[:2]), int(t_str[2:])))
                        all_dts.append(dt)
                        if sd == sys_date:
                            swipes.append((t_str, r[2].strip()))
                    except ValueError:
                        continue
        if all_dts:
            last_swipe = max(all_dts)
    except (TimeoutException, WebDriverException, TypeError):
        read_ok = False
    return sys_date, swipes, last_swipe, read_ok


# [fix 2026-06-08] 本窗已完成打卡的帳號標記。
# 排程設計是「打卡窗內每分鐘 re-fire 同一任務、靠讀刷卡表冪等跳過」。但 re-fire 必須先
# 成功登入才能讀刷卡表；若某次 re-fire 的登入剛好失敗(WebDriverException/多次登入失敗)，
# 就會在『其實已打卡成功』的情況下跳出假的「打卡失敗」通知(user 實測：每到打卡時間右下角
# 一直跳失敗，但實際已打卡成功)。改為：帳號一旦在本窗確認完成(打卡成功 或 已偵測到既有
# 紀錄)，就記下來；後續 re-fire 直接略過該帳號，不再重開 driver/重新登入 → 不再產生假失敗。
# key=(schedule_key, username)，value=date_str；以日期判定自動跨日重置、且大小有界(覆寫同 key)。
_clock_done_lock = threading.Lock()
_clock_done: dict = {}


def _mark_clock_done(schedule_key, username) -> None:
    if not schedule_key or not username:
        return
    with _clock_done_lock:
        _clock_done[(schedule_key, username)] = date.today().isoformat()
    _save_clock_state()


def _is_clock_done(schedule_key, username) -> bool:
    if not schedule_key or not username:
        return False
    with _clock_done_lock:
        return _clock_done.get((schedule_key, username)) == date.today().isoformat()


# =============================================================================
# [新功能 2026-06-13] 補卡提醒：打卡窗結束後仍未確認成功 → 跳通知提醒使用者
# 去電子刷卡系統確認/補卡。原本失敗只記 log + 單次失敗通知，若整窗的 re-fire
# 全部失敗(或程式中途才啟動)，使用者可能完全不知道漏打卡。
# =============================================================================
# 同一 (schedule_key) 當天只提醒一次；value=date_iso，跨日自動失效、大小有界。
_missed_warned_lock = threading.Lock()
_missed_warned: dict = {}

# 窗結束後 90 秒才開始判定：避免「窗內最後一刻刷卡成功、但確認流程還在跑」的
# 競態造成假提醒。15 分鐘後不再提醒(太久前的窗,提醒已無行動價值)。
_MISSED_GRACE_START_SEC = 90
_MISSED_GRACE_END_MIN = 15


def _was_missed_warned_today(schedule_key: str) -> bool:
    with _missed_warned_lock:
        return _missed_warned.get(schedule_key) == date.today().isoformat()


def _mark_missed_warned(schedule_key: str) -> None:
    with _missed_warned_lock:
        _missed_warned[schedule_key] = date.today().isoformat()
    _save_clock_state()


# =============================================================================
# [新功能 2026-06-15] 打卡狀態跨重啟持久化
# _clock_done / _missed_warned 原為純記憶體 → watchdog/自動更新重啟即清空：
#   - 重啟後 _clock_done 沒了 → 已打卡帳號又重開 driver+登入(re-fire 登入失敗會跳假失敗)
#   - _missed_warned 沒了 → 同窗補卡提醒可能重跳
# 落盤到 clock_state.json(全檔一個 date 戳,跨日整批失效),scheduler 啟動時載回。
# 沿用 consult_query dedup 已驗證的持久化 pattern；全程 fail-open 降級純記憶體。
# 只有長駐 scheduler 實例才啟用寫盤(_clock_state_persistence_enabled),避免 GUI/
# 短命實例污染檔案。
# =============================================================================
CLOCK_STATE_FILE = SETTINGS_DIR / "clock_state.json"
_clock_state_persistence_enabled = False
# [codex review 2026-06-15] 序列化寫盤:worker(打卡)與 scheduler(補卡提醒)兩個
# thread 都會觸發 _save_clock_state;原本只各自快照無序列化,並發時較舊的整檔快照
# 可能後寫覆蓋較新的 → 漏存某些 clock_done/missed_warned。用一把存檔鎖把「快照+寫盤」
# 串起來。注意:_mark_* 是先放掉 dict 鎖才呼叫 _save_clock_state,故不會與此鎖巢狀死結。
_clock_state_save_lock = threading.Lock()


def _save_clock_state() -> None:
    """把今日的 _clock_done / _missed_warned 落盤(原子寫)。未啟用或失敗 → 靜默降級。
    存檔鎖序列化整個「快照→寫檔」,避免並發後寫覆蓋。"""
    if not _clock_state_persistence_enabled:
        return
    today = date.today().isoformat()
    with _clock_state_save_lock:
        with _clock_done_lock:
            done = [[k, u] for (k, u), v in _clock_done.items() if v == today]
        with _missed_warned_lock:
            warned = [k for k, v in _missed_warned.items() if v == today]
        try:
            atomic_write_json(str(CLOCK_STATE_FILE),
                              {"date": today, "clock_done": done,
                               "missed_warned": warned})
        except Exception:
            logging.debug("[clock-state] 寫盤失敗(降級純記憶體)", exc_info=True)


def _load_clock_state() -> None:
    """啟動時載回今日打卡狀態(跨重啟防假失敗/防補卡提醒重跳)。
    全檔 date != 今日 → 視為跨日舊狀態整批忽略;壞檔/缺檔/欄位型別異常一律靜默降級。
    [codex review 2026-06-15] 整個函式包 try/except:合法 JSON 但欄位畸形
    (如 clock_done=null)不可拋例外殺掉 scheduler 啟動(載入在註冊排程之前)。"""
    try:
        raw = safe_load_json(str(CLOCK_STATE_FILE), default={})
        today = date.today().isoformat()
        if not isinstance(raw, dict) or raw.get("date") != today:
            return
        done_n = 0
        done_items = raw.get("clock_done") or []
        warned_items = raw.get("missed_warned") or []
        if not isinstance(done_items, list):
            done_items = []
        if not isinstance(warned_items, list):
            warned_items = []
        with _clock_done_lock:
            for item in done_items:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    _clock_done[(str(item[0]), str(item[1]))] = today
                    done_n += 1
        warned_n = 0
        with _missed_warned_lock:
            for k in warned_items:
                if isinstance(k, str) and k:
                    _missed_warned[k] = today
                    warned_n += 1
        if done_n or warned_n:
            logging.info("[clock-state] 已載回今日狀態:%d 筆已完成打卡、%d 筆已提醒"
                         "(跨重啟)", done_n, warned_n)
    except Exception:
        logging.warning("[clock-state] 載入失敗(降級純記憶體,不影響打卡)",
                        exc_info=True)


def _windows_needing_missed_warning(now_dt, accounts, *, is_done,
                                    already_warned) -> list:
    """回傳 [(schedule_key, [username, ...]), ...]:剛結束的打卡窗中,
    「有排該窗」但「本窗未確認完成」且「今天還沒提醒過」的帳號。

    純函式(時間/狀態全由參數注入)以便單元測試。判定窗:
    check_end+90s < now <= check_end+15min(避開窗尾確認競態、過久不再提醒)。
    """
    from datetime import timedelta
    w = now_dt.weekday()
    if w > 5:  # 週日不打卡(與 get_sched_key 一致)
        return []
    day = ["mon", "tue", "wed", "thu", "fri", "sat"][w]
    out = []
    for task_type, (_start, end) in VALIDATION_WINDOWS.items():
        end_dt = datetime.combine(now_dt.date(), end)
        lo = end_dt + timedelta(seconds=_MISSED_GRACE_START_SEC)
        hi = end_dt + timedelta(minutes=_MISSED_GRACE_END_MIN)
        if not (lo < now_dt <= hi):
            continue
        skey = f"{day}_{task_type}"
        if already_warned(skey):
            continue
        missing = [
            a.get("username") for a in accounts
            if a.get("schedule", {}).get(skey, False)
            and a.get("username")
            and not is_done(skey, a.get("username"))
        ]
        if missing:
            out.append((skey, missing))
    return out


def _missed_clock_check() -> None:
    """每分鐘由排程器呼叫:打卡窗剛結束仍有帳號未確認成功 → 通知 + log。"""
    try:
        hits = _windows_needing_missed_warning(
            datetime.now(), load_config(),
            is_done=_is_clock_done,
            already_warned=_was_missed_warned_today,
        )
        for skey, users in hits:
            _mark_missed_warned(skey)
            names = ", ".join(str(u) for u in users)
            logging.warning(
                "[補卡提醒] 打卡時段 %s 已結束,以下帳號本窗未確認成功打卡: %s "
                "(可能登入連續失敗或程式窗內未執行,請至電子刷卡系統確認/補卡)",
                skey, names)
            notify_clock_failure(
                "補卡提醒",
                [f"打卡時段 {skey} 已結束",
                 f"未確認成功: {names}",
                 "請至電子刷卡系統確認,必要時補打卡"],
                None,
            )
    except Exception:
        logging.exception("[補卡提醒] 檢查例外(吞掉,不影響排程)")


def _check_swipes(type_str: str, start: dt_time, end: dt_time, swipes) -> bool:
    """檢查是否有「特定類型」紀錄『嚴格』落在 [start, end] 官方打卡區間內。

    [2026-06-01] 不放寬區間(不吸收時鐘偏差)。改以「比窗起晚 1 分鐘才觸發打卡」
    (am_in 7:31、midday_in 12:31、pm_out 17:01、eve_out 21:01)確保打卡時間穩穩
    落在官方區間(0730-0800 / 1230-1300 / 1700-1730 / 2100-2130)內。
    """
    for t, typ in swipes:
        if typ == type_str:
            try:
                swipe_time = dt_time(int(t[:2]), int(t[2:]))
                if start <= swipe_time <= end:
                    return True
            except (ValueError, IndexError):
                continue
    return False


def _verify_clock_recorded(driver, get_loc, act_name: str,
                           check_start: dt_time, check_end: dt_time,
                           username: str, timeout_sec: float = 20.0,
                           poll_sec: float = 3.0) -> bool:
    """[W3 2026-07-03] 點擊執行後重讀刷卡表,確認 act_name 的新紀錄已落在打卡區間內。
    輪詢至確認或逾時。每次用短逾時 wait(5s)避免頁面跳走時卡滿整個 timeout。
    回 True=已確認寫入(可標記完成);False=逾時仍未確認(caller 不標記,交 re-fire 重讀)。
    讀取失敗(read_ok=False)一律當作『尚未確認』,絕不當成已成功。"""
    short_wait = WebDriverWait(driver, 5)
    deadline = time_module.monotonic() + timeout_sec
    while True:
        try:
            _sd, swipes, _last, read_ok = get_current_swipe_info(
                driver, short_wait, get_loc)
            if read_ok and _check_swipes(act_name, check_start, check_end, swipes):
                return True
        except Exception:
            logging.debug("[autoclock] 打卡後重讀刷卡表例外(續輪詢) user=%s",
                          username, exc_info=True)
        if time_module.monotonic() >= deadline:
            return False
        time_module.sleep(poll_sec)


def perform_clock_action(driver, wait, acc, is_in: bool,
                        check_start: dt_time, check_end: dt_time,
                        dry_run: bool = False, task_label: str = "") -> None:
    def get_loc(key):
        return (getattr(By, LOCATORS[key][0].upper()), LOCATORS[key][1])

    retries = 5
    last_exc = None
    for attempt in range(retries):
        try:
            login(driver, wait, acc["username"], acc["password"])

            _sys_date, swipes, _last, swipes_read_ok = get_current_swipe_info(
                driver, wait, get_loc)
            act_name = "上班" if is_in else "下班"

            # [W4 2026-07-03] 讀刷卡表失敗 → 無法判斷是否已打卡 → 絕不打卡(避免重複打卡),
            # 拋出交給重試/下一分鐘 re-fire 重讀。dry_run 例外(僅驗流程)。
            if not dry_run and not swipes_read_ok:
                raise WebDriverException(
                    "讀取刷卡表失敗,略過本次打卡以免重複打卡(將重試/下次 re-fire 重讀)")

            has_record_in_window = _check_swipes(act_name, check_start, check_end, swipes)

            if not dry_run and has_record_in_window:
                logging.info(
                    "%s 在區間 %s-%s 內已有 %s 紀錄，跳過。",
                    acc["username"], check_start, check_end, act_name)
                # [fix] 已有紀錄=本窗確認完成 → 標記，後續 re-fire 直接略過不再登入
                _mark_clock_done(task_label, acc["username"])
                return
            if not dry_run:
                logging.info(
                    "%s 區間 %s-%s 無有效紀錄，準備執行打卡 (目前紀錄: %s)",
                    acc["username"], check_start, check_end, swipes)

            delay = random.randint(1, 5)
            logging.info("%s 準備打卡，隨機延遲 %s 秒...", acc["username"], delay)
            time_module.sleep(delay)

            rid_locator = get_loc("work_on_radio") if is_in else get_loc("work_off_radio")
            radio_btn = wait.until(EC.presence_of_element_located(rid_locator))
            driver.execute_script("arguments[0].click();", radio_btn)

            if is_in:
                handle_health_declaration(driver, wait, WebDriverWait(driver, 5), get_loc)

            exec_btn = wait.until(EC.presence_of_element_located(get_loc("execute_button")))

            if dry_run:
                driver.execute_script("arguments[0].style.border='5px solid red'", exec_btn)
                logging.info("[測試模式] %s %s 流程驗證成功！(未點擊執行)", acc["username"], act_name)
                messagebox.showinfo(
                    "測試成功",
                    f"帳號: {acc['username']}\n"
                    f"動作: {act_name}\n"
                    f"區間: {check_start}-{check_end}\n\n"
                    f"流程驗證成功，未實際執行打卡。",
                )
                return

            # [AC-01] 窗尾防線:portal 緩慢/登入重試堆疊時,確認點擊當下仍未超過打卡窗尾
            # (加 60s 緩衝吸收「點擊→刷卡表登錄」延遲)。超窗放棄點擊,避免打出遲到紀錄;
            # 未標記完成 → 由 _missed_clock_check 於窗結束後發補卡提醒接手。
            if _clock_window_passed(check_end, grace_sec=60):
                logging.warning(
                    "[窗尾防線] %s %s 準備點擊時已超過打卡窗尾 %s(+60s 緩衝),放棄點擊"
                    "避免遲到紀錄,交補卡提醒。", acc.get("username", "?"), act_name, check_end)
                return

            driver.execute_script("arguments[0].click();", exec_btn)

            try:
                WebDriverWait(driver, 5).until(EC.alert_is_present()).accept()
            except TimeoutException:
                pass

            # [W3 2026-07-03] 不再「點擊即標記完成」——那會在點擊後 portal/網路失敗時
            # 造成假成功(本窗 re-fire 全跳過→漏打卡)。改為重讀刷卡表確認新紀錄真的
            # 寫入才標記。確認不到就不標記(記警告),交下一分鐘 re-fire 重讀:紀錄真在
            # 會走 has_record 路徑補標記;真沒進去則重打。任何情況都不會重複打卡。
            if _verify_clock_recorded(driver, get_loc, act_name,
                                      check_start, check_end, acc["username"]):
                logging.info("%s %s 打卡成功(已重讀刷卡表確認紀錄)！",
                             acc["username"], act_name)
                _mark_clock_done(task_label, acc["username"])
            else:
                logging.warning(
                    "%s %s 打卡已送出,但重讀刷卡表未能確認到紀錄 — 不標記完成,"
                    "下次 re-fire 會重讀確認(避免假成功漏打卡)。",
                    acc["username"], act_name)
            return

        except ClockAuthError as e:
            # [AC-09] 帳密錯誤:當窗不再重試,單次醒目通知(避免反覆登入 + 帳號鎖定風險)。
            logging.error("%s 帳號/密碼錯誤,當窗不再重試: %s", acc.get("username", "?"), e)
            if dry_run:
                messagebox.showerror("測試失敗(帳號/密碼錯誤)", str(e))
            else:
                _handle_clock_failure(driver, acc.get("username", "?"),
                                      task_label, e, dry_run)
            return
        except (StaleElementReferenceException, WebDriverException) as e:
            last_exc = e
            logging.warning(
                "%s 操作遇到 %s，重試中 (%s/%s)...",
                acc.get("username", "?"), type(e).__name__, attempt + 1, retries)
            if attempt < retries - 1:
                exponential_backoff_sleep(attempt, base_seconds=2.0, max_seconds=60.0)
            else:
                logging.error("%s WebDriver 錯誤，重試用盡: %s", acc.get("username", "?"), e)
        except Exception as e:
            last_exc = e
            logging.error("%s 操作失敗: %s", acc.get("username", "?"), e)
            if dry_run:
                messagebox.showerror("測試失敗", str(e))
            break

    _handle_clock_failure(driver, acc.get("username", "?"), task_label, last_exc, dry_run)


# =============================================================================
# 排程
# =============================================================================
def _driver_session_alive(driver) -> bool:
    """[opt A2] 輕量探測 WebDriver session 是否還活著(Chrome 未被防毒/系統殺、未 crash)。
    跑一個極輕量的跨行程 command(driver.title)；session 已死會丟 InvalidSessionIdException
    /WebDriverException。回 False 代表該重建 driver。"""
    if driver is None:
        return False
    try:
        _ = driver.title
        return True
    except Exception:
        return False


def process_clock_task(schedule_key: str | None) -> None:
    if schedule_key is None:
        return
    # [2026-05-22 v43] 修致命 bug — clock_lock 是 RLock (task #68 從 Lock 改的，
    # 因 janitor + process_clock_task 重入會 deadlock)，但 RLock **沒有**
    # .locked() method (只有 threading.Lock 有)。原本這行每次排程觸發都
    # AttributeError → process_clock_task crash → 打卡完全失效。
    # 純 informational warning，移除即可 (actual locking 仍由 with clock_lock 處理)。
    # 若真的有重入會在下面 with clock_lock 直接阻塞。
    # [v17 2026-05-25 P0 HOTFIX] 改用 time_module — autoclock.py 用 `import time
    # as time_module` 別名 (line 40)，「time」名稱根本不存在。原本 v43 修
    # RLock.locked() bug 時加這段 timing log，沒注意別名 → 每次中午/早上打卡
    # 觸發 process_clock_task 立刻 NameError crash → user 中午沒打到卡。
    t_wait_start = time_module.time()
    # [W12] 先取 clock_lock 再進 scope:單一全域標記只在「真正執行中(持鎖)」的任務上設,
    # 避免第二個 process_clock_task(如 UI 測試 dry-run 與排程並行)在等鎖時覆蓋標記、
    # 或第一個任務 finally 清掉還在跑的第二個任務的標記(codex review)。clock_lock 已
    # 序列化執行,故同時最多一個 scope 生效。
    with clock_lock, _active_clock_task_scope(schedule_key):
        wait_ms = (time_module.time() - t_wait_start) * 1000
        if wait_ms > 100:
            logging.warning(
                "任務 %s 取得 clock_lock 等了 %.0fms (上一個任務還沒結束)",
                schedule_key, wait_ms)
        is_in = "_in" in schedule_key
        try:
            task_type = schedule_key.split("_", 1)[1]
            check_start, check_end = VALIDATION_WINDOWS.get(
                task_type, (dt_time(0, 0), dt_time(23, 59)))
        except (IndexError, ValueError):
            check_start, check_end = dt_time(0, 0), dt_time(23, 59)

        accs = [a for a in load_config()
                if a.get("schedule", {}).get(schedule_key, False)]
        if not accs:
            return

        # [fix 2026-06-08] 本窗已確認完成打卡(成功/已有紀錄)的帳號，直接從本次 re-fire 排除。
        # 打卡窗內每分鐘都會 re-fire 同一任務；第一次成功後，後續 re-fire 若還重開 driver+登入，
        # 一旦登入剛好失敗就會跳假的「打卡失敗」通知。先過濾掉已完成帳號 → 全部完成就連 driver
        # 都不開、直接返回，根除假失敗通知與每分鐘的重複登入開銷。
        accs = [a for a in accs if not _is_clock_done(schedule_key, a.get("username"))]
        if not accs:
            logging.info("排程觸發: %s — 本窗所有帳號已完成打卡，略過 re-fire", schedule_key)
            return

        logging.info(
            "排程觸發: %s，驗證區間: %s-%s，共有 %s 個帳號需執行。",
            schedule_key, check_start, check_end, len(accs))

        # [autoclock 常駐 Chrome] 不再每次任務開新 driver；用常駐池
        driver = _get_or_create_clock_driver()
        if not driver:
            notify_clock_failure(
                "瀏覽器啟動失敗",
                ["無法建立 Chrome / WebDriver",
                 f"排程: {schedule_key}",
                 "請查看 settings\\autoclock.log"],
                None,
            )
            return

        # [stability r4] 標記 driver「使用中」，整個任務期間 idle 回收器都不得 quit。
        # 這比單靠每帳號刷新 last_used 更穩健：即使單一帳號內部(login 多次重試+backoff)
        # 連續耗時 >15 分鐘，使用中 driver 也不會被回收器砍掉造成 InvalidSessionId。
        with _persistent_driver_pool["lock"]:
            _persistent_driver_pool["in_use"] = True
        try:
            wait = WebDriverWait(driver, 20)
            # [opt A2] 任務中途若 Chrome session 死掉(被防毒/系統殺、crash、OOM)，原本
            # perform_clock_action 會對「同一顆死 driver」每個帳號各跑 5 次重試+指數 backoff
            # (單帳號光 backoff 就 ~62s 全失敗)，多帳號連坐 → 可能整個 30 分鐘打卡窗錯過。
            # 改為每帳號前輕量探測 session；死掉就重建一顆健康 driver 再繼續(上限 2 次，避免
            # Chrome 反覆死掉時無限重建耗光窗口)。
            _rebuilds = 0
            _MAX_REBUILDS = 2
            for acc in accs:
                if not running.is_set():
                    break
                # [AC-01] 窗尾防線:任務在窗尾起跑 + portal 慢時,已排隊到窗外的帳號不再
                # 打卡(避免遲到紀錄),交補卡提醒接手。perform_clock_action 內另有一道點擊前檢查。
                if _clock_window_passed(check_end):
                    logging.warning(
                        "[窗尾防線] 任務 %s 執行到 %s 已超過打卡窗尾 %s,剩餘帳號不再打卡"
                        "(避免遲到紀錄),交補卡提醒。", schedule_key,
                        datetime.now().strftime("%H:%M:%S"), check_end)
                    break
                if _rebuilds < _MAX_REBUILDS and not _driver_session_alive(driver):
                    logging.warning(
                        "[autoclock] 偵測到 Chrome session 已死 → 重建 driver 後繼續"
                        "(第 %d/%d 次)", _rebuilds + 1, _MAX_REBUILDS)
                    driver = _get_or_create_clock_driver()
                    _rebuilds += 1
                    if not driver:
                        logging.error("[autoclock] 重建 driver 失敗，中止本任務剩餘帳號")
                        break
                    wait = WebDriverWait(driver, 20)
                perform_clock_action(
                    driver, wait, acc, is_in, check_start, check_end,
                    dry_run=False, task_label=schedule_key,
                )
                # 每處理完一個帳號就刷新 last_used，讓任務結束後 idle 倒數從「最後一個
                # 帳號完成」起算（in_use 旗標負責任務進行中的保護，此處負責任務後計時）。
                with _persistent_driver_pool["lock"]:
                    _persistent_driver_pool["last_used"] = time_module.time()
        except Exception as e:
            logging.error("任務 %s 執行期間發生錯誤: %s", schedule_key, e)
            try:
                _handle_clock_failure(driver, "system", schedule_key, e, dry_run=False)
            except Exception:
                pass
            # 任務級錯誤不關 driver；下次任務若 driver 不健康會自動重建
        finally:
            # 注意：不再 driver.quit()！常駐池管理（idle 才釋放）。務必清 in_use，
            # 否則回收器永遠不敢 quit → driver 永不釋放(RAM 漏)。
            with _persistent_driver_pool["lock"]:
                _persistent_driver_pool["last_used"] = time_module.time()
                _persistent_driver_pool["in_use"] = False
            logging.info("任務 %s 執行週期結束（driver 保留以待下次任務）。", schedule_key)


def get_sched_key() -> str | None:
    n = datetime.now()
    w = n.weekday()
    t = n.time()
    if w > 5:
        return None
    day = ["mon", "tue", "wed", "thu", "fri", "sat"][w]
    if CLOCK_IN_START_TIME <= t <= CLOCK_IN_END_TIME:
        return f"{day}_am_in"
    # 先判斷午休上班 (12:30~)，再判斷午休下班 (12:00~12:30)，避免邊界誤判
    if CLOCK_MIDDAY_IN_START_TIME <= t <= CLOCK_MIDDAY_IN_END_TIME:
        return f"{day}_midday_in"
    if CLOCK_MIDDAY_OUT_START_TIME <= t <= CLOCK_MIDDAY_OUT_END_TIME:
        return f"{day}_midday_out"
    if TRIGGER_PM_OUT_START_TIME <= t <= CLOCK_PM_OUT_END_TIME:
        return f"{day}_pm_out"
    if CLOCK_EVE_OUT_START_TIME <= t <= CLOCK_EVE_OUT_END_TIME:
        return f"{day}_eve_out"
    return None


def _scheduler_tick() -> None:
    """每分鐘觸發一次：只呼叫一次 get_sched_key，避免邊界競態。"""
    key = get_sched_key()
    if not key:
        return
    lease = _clock_task_gate.acquire_lease(key)
    if lease is None:
        age = _clock_task_gate.active_age_sec(key)
        logging.info(
            "[autoclock] %s task is still running (age=%ss), skip this tick",
            key,
            "?" if age is None else f"{age:.0f}",
        )
        return

    def _worker():
        try:
            process_clock_task(key)
        finally:
            _clock_task_gate.release(key, lease)

    threading.Thread(target=_worker, name=f"AutoClockTask-{key}", daemon=True).start()


def _idle_driver_janitor() -> None:
    """檢查 idle driver 是否該主動 quit (省記憶體)。

    讓使用者在沒打卡的空檔不會多佔 ~150-250MB Chrome 進程。
    跟 _get_or_create_clock_driver 的 idle-check 邏輯一致，但這邊主動觸發。

    [2026-05-22 v45 P0-2/P1-7 修補]
    (1) quit() 移到鎖外 — 原本持鎖 quit 若 hang 30s+ 會卡所有 driver 取得者
    (2) 整個 body 包 try/except logging.exception — schedule lib 不會 catch
        user job exception，若這裡丟例外整個 scheduler thread 會死
    """
    try:
        pool = _persistent_driver_pool
        old_driver_to_quit = None

        with pool["lock"]:
            d = pool["driver"]
            if d is None:
                return
            if pool.get("in_use"):
                # [stability r4] 任務正在使用此 driver，絕不回收(避免砍使用中 driver
                # 造成後續帳號 InvalidSessionId)。任務 finally 會清 in_use 後才可回收。
                return
            now = time_module.time()
            idle_for = now - pool["last_used"]
            if idle_for > _PERSISTENT_DRIVER_IDLE_TIMEOUT:
                logging.info("[autoclock] driver idle %.0f 分鐘 (>%.0f 分)，主動 quit 省 RAM",
                             idle_for / 60, _PERSISTENT_DRIVER_IDLE_TIMEOUT / 60)
                old_driver_to_quit = d
                pool["driver"] = None

        # 鎖外 quit
        if old_driver_to_quit is not None:
            try:
                old_driver_to_quit.quit()
            except Exception:
                logging.debug("idle driver quit 例外（忽略）", exc_info=True)
    except Exception:
        # schedule lib 不會 catch user job exception，若這裡冒泡會殺整個 scheduler thread
        logging.exception("[autoclock] _idle_driver_janitor 未預期例外（已吞掉避免殺 thread）")


def _autoclock_hard_exit(reason: str, code: int = 1) -> None:
    """[2026-05-22 v45 P0-1] 強制終止 process，不走 logging.shutdown (會 deadlock)。

    照 consult_query._hard_exit 那套 pattern。
    """
    import os as _os
    try:
        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            lock = getattr(h, "lock", None)
            acquired = False
            try:
                if lock is not None:
                    acquired = lock.acquire(blocking=False)
                    if not acquired:
                        continue
                stream = getattr(h, "stream", None)
                if stream is not None and hasattr(stream, "flush"):
                    stream.flush()
                else:
                    h.flush()
            except Exception:
                pass
            finally:
                if lock is not None and acquired:
                    try:
                        lock.release()
                    except Exception:
                        pass
    except Exception:
        pass
    # [AC-03] os._exit 會跳過 atexit → 常駐 chromedriver/Chrome 變孤兒。硬退前 best-effort
    # 釋放常駐 driver,避免每次硬退堆積一組孤兒瀏覽器。
    # [codex P1] 但「一定要退」優先於「清乾淨」:_release_persistent_clock_driver 會取 pool
    # lock,若正是被卡死的執行緒持著(hard_exit 要救的情境)就會永遠卡在取鎖、到不了 os._exit
    # → self-watchdog 失效。故放獨立 daemon 緒 + 短逾時;逾時就放棄清理直接硬退。
    try:
        _rel = threading.Thread(target=_release_persistent_clock_driver,
                                name="AutoclockHardExitRelease", daemon=True)
        _rel.start()
        _rel.join(timeout=2.0)
    except Exception:
        pass
    _os._exit(code)


def _autoclock_self_watchdog() -> None:
    """[2026-05-22 v45 P0-1] autoclock scheduler self-watchdog daemon。

    照 consult_query._scheduler_self_watchdog 那套 pattern：
      1. scheduler_thread.is_alive()==False → 立刻 _hard_exit (thread 真死)
      2. last_tick > 180s 沒更新 → log CRITICAL (沒 IMAP socket 可砍，autoclock
         是 Chrome WebDriver，砍它要走 process kill — 直接走 stage 2)
      3. > 200s 沒 tick → _hard_exit(1) 強制重啟整個 process

    為什麼 autoclock 需要這個：consult_query 死過、加了 self-watchdog；今天
    autoclock 死了一整下午 (RLock.locked() AttributeError + 沒 watchdog 救)
    才被發現。autoclock max_stale_sec=0 配上 mutex 仍持有 → 外層 watchdog 也
    救不回。沒這個 in-process watchdog 等於沒人看。
    """
    DEAD_THRESHOLD = 180
    KILL_THRESHOLD = 20
    CHECK_INTERVAL = 30
    dead_detected_at = 0.0
    stuck_task_warned = False   # [W12] 卡住任務只警告一次(不洗版)
    while running.is_set():
        try:
            if not _sleep_while_running(CHECK_INTERVAL):
                break
            # [W12] 偵測「單一打卡任務卡太久」——scheduler 仍 tick,但 Selenium 任務可能
            # hang。只警告(卡住的 Selenium 執行緒無法安全 kill;driver 逾時/task gate
            # 會兜底)。持續卡住只警告一次,任務結束後重置。
            task_label, task_age = _active_clock_task_age()
            if task_label and task_age > _ACTIVE_TASK_WARN_SEC:
                if not stuck_task_warned:
                    stuck_task_warned = True
                    logging.critical(
                        "[autoclock/self-watchdog] 打卡任務 %s 已執行 %.0fs(>%ds,疑似"
                        " Selenium 卡住)——請留意是否有帳號漏打卡;driver 逾時與 task "
                        "gate 會兜底,scheduler 本身仍存活。",
                        task_label, task_age, _ACTIVE_TASK_WARN_SEC)
            else:
                stuck_task_warned = False
            # Stage 0: thread 真死了 → 立刻退場
            global _scheduler_thread_ref
            if _scheduler_thread_ref is not None and not _scheduler_thread_ref.is_alive():
                logging.critical(
                    "[autoclock/self-watchdog] scheduler thread is_alive()=False "
                    "→ _hard_exit(1) 強制重啟 (外層 watchdog 會接手)")
                _autoclock_hard_exit("scheduler thread dead", code=1)
            last = _AUTOCLOCK_LIVENESS.get("last_tick", 0.0)
            if last == 0.0:
                continue
            age = time_module.time() - last
            if age > DEAD_THRESHOLD and dead_detected_at == 0.0:
                logging.critical(
                    "[autoclock/self-watchdog] scheduler 已 %.0f 秒沒 tick "
                    "(>%.0fs 視為死亡)，準備 hard_exit",
                    age, DEAD_THRESHOLD)
                dead_detected_at = time_module.time()
                continue
            if dead_detected_at > 0:
                if last > dead_detected_at:
                    logging.info("[autoclock/self-watchdog] scheduler 已恢復 tick，取消重啟")
                    dead_detected_at = 0.0
                elif time_module.time() - dead_detected_at > KILL_THRESHOLD:
                    logging.critical(
                        "[autoclock/self-watchdog] dead 偵測後 %.0fs 仍沒 tick "
                        "→ _hard_exit(1) 強制重啟 (外層 watchdog 會接手)",
                        time_module.time() - dead_detected_at)
                    _autoclock_hard_exit("scheduler stuck", code=1)
        except Exception:
            logging.exception("[autoclock/self-watchdog] tick 例外")


def _ensure_autoclock_self_watchdog() -> None:
    global _self_watchdog_thread_ref
    with _self_watchdog_lock:
        if (_self_watchdog_thread_ref is not None
                and _self_watchdog_thread_ref.is_alive()):
            return
        _self_watchdog_thread_ref = threading.Thread(
            target=_autoclock_self_watchdog,
            name="AutoclockSelfWatchdog",
            daemon=True,
        )
        _self_watchdog_thread_ref.start()


def scheduler_loop() -> None:
    """背景排程主迴圈。

    [2026-05-22 v45 P0-1/P1-7 修補]
    (1) schedule.run_pending() 包 try/except — schedule lib 不會 catch user job
        例外，會冒泡到此 (今天的 RLock.locked() bug 就是這樣讓 process_clock_task
        crash 但 scheduler loop 本身倖存，因為它本來就 catch)；保險仍要包。
    (2) 每 iter 更新 _AUTOCLOCK_LIVENESS["last_tick"] 給 self-watchdog 用
    (3) 啟動 self-watchdog daemon thread 監看 scheduler thread is_alive + tick
    """
    logging.info("背景排程器已啟動...")
    # [新功能 2026-06-15] 長駐 scheduler 啟用打卡狀態持久化 + 載回今日狀態
    # (跨 watchdog/自動更新重啟,防已打卡帳號重登假失敗、補卡提醒重跳)。
    global _clock_state_persistence_enabled
    _clock_state_persistence_enabled = True
    _load_clock_state()
    schedule.clear()
    schedule.every(1).minute.at(":01").do(_scheduler_tick)
    # [優化] 每 2 分鐘主動檢查 idle driver，過期就 quit (省 ~150-250MB Chrome)
    schedule.every(2).minutes.do(_idle_driver_janitor)
    # [新功能 2026-06-13] 補卡提醒:打卡窗結束 90s~15min 內檢查未完成帳號並通知
    schedule.every(1).minute.at(":31").do(_missed_clock_check)

    # [P0-1] 啟動 self-watchdog 子 thread
    _ensure_autoclock_self_watchdog()

    # [2026-05-25 P0 emergency 修補] heartbeat log — 給外層 InnerWatchdog 看
    # log mtime 用。原本 v45 把 max_stale_sec 0→300 但忽略 autoclock idle 時段
    # _scheduler_tick 沒 sched_key 會直接 return 不印 log → log mtime 整夜不更新
    # → InnerWatchdog 看 log >300s 沒動 → kill+restart → 整夜 crash loop →
    # 早上 7:30 打卡時間 autoclock 剛重啟還沒就緒 → 沒打到卡。
    # 修法：每 60s 印一行 INFO 級 heartbeat 強制更新 log mtime。
    last_heartbeat_log = 0.0

    while running.is_set():
        # [P0-1] heartbeat — 給 self-watchdog 偵測
        now = time_module.time()
        _AUTOCLOCK_LIVENESS["last_tick"] = now

        # [P0 emergency] 每 60s 印一行 log 讓 InnerWatchdog 看到 process 活著
        last_heartbeat_log = _maybe_emit_heartbeat(now, last_heartbeat_log)

        try:
            schedule.run_pending()
        except Exception:
            logging.exception("[autoclock] scheduler.run_pending 例外 (已吞掉，scheduler 繼續跑)")
        # [優化] 改 5s sleep — schedule 套件本身有 :01 精度，5s 內仍會準時觸發
        # 每分鐘任務。早期 1s 太密；對打卡 job 觀感無差，CPU 用量降 5 倍。
        if not _sleep_while_running(5):
            break


HEARTBEAT_INTERVAL_SEC = 60.0
HEARTBEAT_MSG = "[autoclock][heartbeat] scheduler alive (idle 等待下個打卡時段)"


def _maybe_emit_heartbeat(now: float, last_log_ts: float,
                          interval: float = HEARTBEAT_INTERVAL_SEC) -> float:
    """[2026-05-25] 每 `interval` 秒印一行 heartbeat log — 確保 idle 時段
    log mtime 還是會被更新，外層 InnerWatchdog 不會誤判 autoclock 卡死。

    回傳「下次比較用的 last_log_ts」（若這次有印就是 now，沒印就維持原值）。
    抽 helper 讓 tests/test_autoclock_heartbeat.py 能不跑 scheduler 主迴圈
    直接驗證 (a) 過 interval 一定要 emit (b) 沒過不能 emit。
    """
    if now - last_log_ts >= interval:
        logging.info(HEARTBEAT_MSG)
        return now
    return last_log_ts


# =============================================================================
# 線上更新檢查（背景）
# =============================================================================
def _check_update_in_background() -> None:
    try:
        from cmuh_common.updater import check_and_update, need_restart_after_update
        result = check_and_update()
        if need_restart_after_update(result):
            logging.info("打卡程式偵測到新版，立即重新啟動")
            restart_program(hard_exit_code=1)
    except Exception:
        logging.debug("打卡程式背景更新檢查失敗", exc_info=True)


# =============================================================================
# UI（設定視窗）
# =============================================================================
class ClockApp(tk.Tk):
    def __init__(self, loaded_data):
        super().__init__()
        self.title(f"自動打卡設定 (v{CURRENT_VERSION})")
        self.geometry("1000x650")
        # [v18 2026-05-25] 攔截 Tk callback 例外進 log (原本進 stderr 黑洞)
        try:
            from cmuh_common.tk_exception import install_tk_exception_handler
            install_tk_exception_handler(self)
        except Exception:
            logging.debug("Tk callback exception hook 失敗", exc_info=True)
        self.accounts = loaded_data
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.setup_styles()
        self.setup_ui()
        self.after(100, self.poll_log_queue)

    def setup_styles(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        main_font = ("Microsoft JhengHei UI", 10)
        bold_font = ("Microsoft JhengHei UI", 10, "bold")
        style.configure(".", font=main_font)
        style.configure("Treeview", font=main_font, rowheight=25)
        style.configure("Treeview.Heading", font=bold_font)
        style.configure("TLabelframe.Label", font=bold_font, foreground="#333333")
        style.configure("Action.TButton", font=bold_font, foreground="#0055AA")

    def setup_ui(self):
        main_container = ttk.Frame(self, padding="10")
        main_container.pack(fill=tk.BOTH, expand=True)
        top_pane = ttk.Frame(main_container)
        top_pane.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        left_panel = ttk.LabelFrame(top_pane, text="帳號管理", padding="10")
        left_panel.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        self.listbox = tk.Listbox(
            left_panel, width=25, font=("Microsoft JhengHei UI", 11),
            selectmode=tk.SINGLE, bd=1, relief="solid",
        )
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(left_panel, orient="vertical", command=self.listbox.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.config(yscrollcommand=scroll.set)
        self.listbox.bind("<<ListboxSelect>>", self.on_select)
        right_panel = ttk.Frame(top_pane)
        right_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cred_frame = ttk.LabelFrame(right_panel, text="登入資訊", padding="10")
        cred_frame.pack(fill=tk.X, pady=(0, 10))
        grid_opts = {"padx": 5, "pady": 5, "sticky": "w"}
        ttk.Label(cred_frame, text="員工編號 (帳號):").grid(row=0, column=0, **grid_opts)
        self.user_var = tk.StringVar()
        ttk.Entry(cred_frame, textvariable=self.user_var, width=25,
                  font=("Consolas", 11)).grid(row=0, column=1, **grid_opts)
        ttk.Label(cred_frame, text="登入密碼:").grid(row=1, column=0, **grid_opts)
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(
            cred_frame, textvariable=self.pass_var, show="●", width=25, font=("Consolas", 11))
        self.pass_entry.grid(row=1, column=1, **grid_opts)
        self.show_pass_var = tk.BooleanVar()
        ttk.Checkbutton(
            cred_frame, text="顯示", variable=self.show_pass_var,
            command=lambda: self.pass_entry.config(
                show="" if self.show_pass_var.get() else "●"),
        ).grid(row=1, column=2, **grid_opts)
        sched_frame = ttk.LabelFrame(right_panel, text="自動打卡排程 (勾選即啟用)", padding="10")
        sched_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        headers = ["早班\n(07:31~)", "午上\n(12:30~)", "午下\n(12:00~)",
                   "午退\n(17:00~)", "晚退\n(21:00~)"]
        cols = ["am_in", "midday_in", "midday_out", "pm_out", "eve_out"]
        days_map = [("mon", "一"), ("tue", "二"), ("wed", "三"),
                    ("thu", "四"), ("fri", "五"), ("sat", "六")]
        ttk.Label(sched_frame, text="週",
                  font=("Microsoft JhengHei UI", 10, "bold")).grid(row=0, column=0, padx=5, pady=2)
        for i, h in enumerate(headers):
            ttk.Label(
                sched_frame, text=h, font=("Microsoft JhengHei UI", 8),
                foreground="#555", justify="center",
            ).grid(row=0, column=i + 1, padx=2, pady=2)
        self.schedule_vars = {}
        for r, (code, name) in enumerate(days_map):
            ttk.Label(sched_frame, text=name).grid(row=r + 1, column=0, padx=5, pady=2)
            for c, type_key in enumerate(cols):
                var = tk.BooleanVar()
                key = f"{code}_{type_key}"
                self.schedule_vars[key] = var
                cb = ttk.Checkbutton(sched_frame, variable=var)
                cb.grid(row=r + 1, column=c + 1, padx=5, pady=2)
        btn_frame = ttk.Frame(right_panel)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="保存帳號", command=self.save_account,
                   style="Action.TButton").pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="刪除", command=self.delete_account).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="儲存並重啟(背景)", command=self.save_and_bg,
                   style="Action.TButton").pack(side=tk.RIGHT)
        log_frame = ttk.LabelFrame(main_container, text="執行紀錄 (Live Log)", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8, state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.populate_listbox()

    def poll_log_queue(self):
        lines = []
        for _ in range(LOG_POLL_MAX_RECORDS):
            try:
                record = log_queue.get_nowait()
                msg = (
                    f"{datetime.fromtimestamp(record.created).strftime('%H:%M:%S')} "
                    f"[{record.levelname}]: {record.getMessage()}"
                )
                lines.append(msg + "\n")
            except queue.Empty:
                break
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, "".join(lines))
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        self.after(100, self.poll_log_queue)

    def populate_listbox(self):
        self.listbox.delete(0, tk.END)
        self.listbox.insert(0, ADD_NEW_ACCOUNT_TEXT)
        for acc in self.accounts:
            self.listbox.insert(tk.END, acc.get("username"))

    def on_select(self, event):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx == 0:
            self.user_var.set("")
            self.pass_var.set("")
            self.show_pass_var.set(False)
            for v in self.schedule_vars.values():
                v.set(False)
        else:
            acc = self.accounts[idx - 1]
            self.user_var.set(acc.get("username"))
            self.pass_var.set(acc.get("password"))
            for k, v in self.schedule_vars.items():
                v.set(acc.get("schedule", {}).get(k, False))

    def save_account(self):
        u = self.user_var.get().strip()
        p = self.pass_var.get()
        if not u or u == ADD_NEW_ACCOUNT_TEXT:
            return
        s = {k: v.get() for k, v in self.schedule_vars.items()}
        exist = next((a for a in self.accounts if a["username"] == u), None)
        if exist:
            exist.update({"password": p, "schedule": s})
        else:
            self.accounts.append({"username": u, "password": p, "schedule": s})

        global accounts_data
        accounts_data = self.accounts

        save_config()
        self.populate_listbox()
        messagebox.showinfo("成功", f"帳號 {u} 已儲存")

    def delete_account(self):
        sel = self.listbox.curselection()
        if not sel or sel[0] == 0:
            return
        u = self.listbox.get(sel[0])
        if messagebox.askyesno("確認", f"刪除 {u}?"):
            self.accounts = [a for a in self.accounts if a["username"] != u]
            global accounts_data
            accounts_data = self.accounts
            save_config()
            self.populate_listbox()
            self.user_var.set("")

    def save_and_bg(self):
        global _config_restart_requested
        # [AC-05/codex P2] 旗標只在 save_config 成功後才設：否則 save_config 拋例外時旗標
        # 已 True 但 restart_program 從未執行 → 使用者關窗時 main() 誤跳過回背景 → 打卡消失。
        save_config()
        _config_restart_requested = True    # 已主動重啟,mainloop 返回後別再重啟
        restart_program()

    def on_closing(self):
        if messagebox.askyesno("關閉", "離開前儲存設定?"):
            save_config()
        self.destroy()


# =============================================================================
# 托盤、重啟、測試
# =============================================================================
def toggle_console(icon=None, item=None):
    if not WINDOWS_API_AVAILABLE:
        return
    hwnd = win32console.GetConsoleWindow()
    if hwnd:
        if win32gui.IsWindowVisible(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)


# [AC-05] 設定視窗是否經「儲存並重啟(背景)」主動觸發重啟。用來區分：
#   True  = save_and_bg 已呼叫 restart_program（mainloop 返回後不要再重啟一次）
#   False = 使用者按 X 關閉設定窗 → mainloop 返回後應 restart_program 回背景模式
_config_restart_requested = False


def _cleanup_orphan_chromedrivers_at_startup() -> None:
    """[AC-03] 啟動時清掃「父行程已死」的孤兒 chromedriver + 其子 chrome。

    前一個 autoclock 實例崩潰/被硬殺（未走 /T）可能留下無主 chromedriver 佔資源；
    父行程已不存在的 chromedriver 已無 client 可操控、留著也沒用。**保守**：只清「父行程
    確定已死」者，父不明（ppid<=0）或父仍在（含 PID 被重用）一律不動，降低誤殺其他工具。
    """
    try:
        import psutil
    except ImportError:
        return
    try:
        alive = set(psutil.pids())
        for p in psutil.process_iter(['pid', 'name', 'ppid']):
            try:
                name = (p.info.get('name') or '').lower()
                if 'chromedriver' not in name:
                    continue
                ppid = p.info.get('ppid') or 0
                if not ppid or ppid in alive:
                    continue          # 父不明或仍在 → 保守不動
                for child in p.children(recursive=True):
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                p.kill()
                logging.info("[autoclock] 啟動清掃孤兒 chromedriver pid=%s（父行程已死）",
                             p.info.get('pid'))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        logging.debug("[autoclock] 清掃孤兒 chromedriver 例外", exc_info=True)


def restart_program(args_add=None, hard_exit_code=None) -> None:
    """[修正] 改用 cmuh_common.paths.restart_self 雙軌相容。"""
    global tray_icon_object
    running.clear()
    if tray_icon_object:
        tray_icon_object.stop()
    try:
        release_single_instance()
        logging.info("[autoclock restart] mutex released before respawn")
    except Exception:
        logging.debug("[autoclock restart] release_single_instance failed",
                      exc_info=True)
    # [stability] respawn 前先收掉本 process 的常駐 chromedriver/Chrome：否則重啟後
    # 新 instance 會再開一份，舊的若因 pystray 吞掉 callback 內的 SystemExit /
    # main thread 收尾延遲而沒退，chromedriver/Chrome 進程會累積。不依賴 atexit/race。
    try:
        _release_persistent_clock_driver()
    except Exception:
        logging.debug("[autoclock restart] release persistent driver failed",
                      exc_info=True)
    extra: list = []
    for a in sys.argv[1:]:
        if a not in ("--configure", "--test-login"):
            extra.append(a)
    if args_add:
        extra.append(args_add)
    restart_self(extra, hard_exit_code=hard_exit_code)


def exit_action(icon=None, item=None) -> None:
    """[v19 2026-05-26] 修 tray 退出關不掉 bug。

    原本流程：running.clear() → tray.stop() → cleanup → sys.exit(0)。
    問題：sys.exit(0) 在 pystray menu callback context raise SystemExit，
    pystray._dispatcher try/except 把 SystemExit 當一般例外吞掉，main thread
    Windows message pump 沒退 → process 永遠不結束。User 觀察到「常常關不掉」
    就是這個 — log 印「使用者要求退出程式」之後 callback ERROR `SystemExit: 0`
    然後就無聲。

    新流程：
      1. tray icon 先 visible=False 立刻從系統列消失 (給 user 視覺反饋)
      2. cleanup + os._exit() 移到 daemon thread (callback 乾淨返回，pystray
         dispatcher 不會吞 SystemExit)
      3. 0.5s 給 message pump 收尾，然後 os._exit(0) 強制退 — 跳過 atexit
         (本來 atexit 也只是 taskkill chromedriver 不需 graceful)
    """
    global _exit_started
    with _exit_lock:
        if _exit_started:
            return
        _exit_started = True
    logging.info("使用者要求退出程式...")
    running.clear()
    if tray_icon_object:
        try:
            tray_icon_object.visible = False  # 系統列圖示立刻消失
        except Exception:
            pass
        try:
            tray_icon_object.stop()
        except Exception:
            pass

    def _shutdown() -> None:
        try:
            _release_persistent_clock_driver()
        except Exception:
            pass
        try:
            release_single_instance()
        except Exception:
            pass
        try:
            time_module.sleep(0.5)  # 給 message pump 收尾
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_shutdown, daemon=True,
                     name="AutoclockShutdown").start()


def run_immediate_test(icon=None) -> None:
    lease = _test_login_gate.acquire_lease("test-login")
    if lease is None:
        logging.info("測試登入仍在執行中，本次點擊略過")
        notify_clock_failure("測試登入執行中", ["請等待目前測試完成"])
        return

    def _worker():
        try:
            _run_test_ui()
        finally:
            _test_login_gate.release("test-login", lease)

    threading.Thread(target=_worker, name="AutoClockTestLogin",
                     daemon=True).start()


def _run_test_ui() -> None:
    root = tk.Tk()
    root.withdraw()

    user = simpledialog.askstring("測試", "輸入要測試的帳號:", parent=root)
    if not user:
        root.destroy()
        return

    acc = next((a for a in load_config() if a["username"] == user), None)
    if not acc:
        messagebox.showerror("錯誤", "找不到此帳號設定")
        root.destroy()
        return

    is_in = messagebox.askyesno(
        "測試模式",
        "請選擇測試動作：\n\n是 (Yes) = 上班\n否 (No) = 下班",
        parent=root,
    )
    root.destroy()

    driver = initialize_driver(headless=False)
    if driver:
        try:
            wait = WebDriverWait(driver, 20)
            perform_clock_action(
                driver, wait, acc, is_in,
                dt_time(0, 0), dt_time(23, 59),
                dry_run=True, task_label="test_login",
            )
        except Exception as e:
            messagebox.showerror("錯誤", str(e))
        finally:
            try:
                driver.quit()
            except WebDriverException:
                pass


# =============================================================================
# 主入口
# =============================================================================
def main() -> None:
    if not ensure_single_instance(AUTOCLOCK_MUTEX_NAME):
        return
    # DPI 感知：設定視窗在高 DPI/縮放螢幕上才不會模糊，並與其他程式一致
    set_dpi_awareness()
    try:
        _setup_clock_logging()
        logging.info("=== autoclock v%s 啟動 ===", CURRENT_VERSION)

        # [穩定性] health monitor — RAM/時鐘/硬碟 + 記憶體 leak 自動重啟 (A/E/F)
        try:
            from cmuh_common.health import start_health_monitor
            # 打卡 Chrome 啟動後正常 RSS ~300MB；warn 500、crit 800
            # [AC-08] 傳 restart_callback：health RAM-crit 自殺(os._exit)假設「外層 watchdog
            # 必在跑」，但 watchdog 主程式監看預設 False → 沒人接手重啟 = 打卡直接消失。改由
            # autoclock 自身重啟(restart_program 會先釋放 driver 再 restart_self),不依賴外層。
            start_health_monitor("autoclock", ram_warn_mb=500, ram_crit_mb=800,
                                  interval_sec=300, network_check=False,
                                  auto_restart_on_crit=True,
                                  crit_persistence_ticks=6,
                                  restart_callback=lambda: restart_program(
                                      hard_exit_code=1))
        except Exception:
            logging.debug("health monitor 啟動失敗", exc_info=True)

        # [穩定性] 全域 thread/sys excepthook：未捕獲例外寫 log。
        # 沒這個的話 daemon thread 死了完全沒紀錄，事後 debug 困難。
        def _sys_excepthook(exc_type, exc_value, exc_tb):
            logging.critical("Uncaught main exception",
                              exc_info=(exc_type, exc_value, exc_tb))
        sys.excepthook = _sys_excepthook
        if hasattr(threading, "excepthook"):
            def _thread_excepthook(args):
                logging.critical(
                    "Uncaught thread exception in %s",
                    getattr(args.thread, "name", "?"),
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
                )
            threading.excepthook = _thread_excepthook

        # 背景檢查更新（不阻塞）
        threading.Thread(target=_check_update_in_background,
                         name="ClockUpdateChecker", daemon=True).start()

        load_config()

        if len(sys.argv) > 1:
            if sys.argv[1] == "--configure":
                ClockApp(accounts_data).mainloop()
                # [AC-05] 設定視窗關閉(按 X / on_closing)後，若不是經「儲存並重啟(背景)」
                # 離開，就回背景模式繼續自動打卡——否則關掉設定窗＝背景打卡程式一起消失，
                # 使用者以為還在打卡卻早已沒在跑(漏打卡)。
                if not _config_restart_requested:
                    logging.info("[autoclock] 設定視窗關閉，回背景模式繼續自動打卡")
                    restart_program()
                return
            if sys.argv[1] == "--test-login":
                _run_test_ui()
                return

        if not accounts_data:
            ClockApp(accounts_data).mainloop()
            return

        global background_thread, tray_icon_object

        if WINDOWS_API_AVAILABLE:
            try:
                hwnd = win32console.GetConsoleWindow()
                if hwnd:
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except Exception:
                pass

        # [AC-03] 背景模式起跑前先清掃前世崩潰遺留的孤兒 chromedriver（父行程已死者）。
        _cleanup_orphan_chromedrivers_at_startup()

        background_thread = threading.Thread(target=scheduler_loop, daemon=True,
                                              name="AutoclockScheduler")
        # [2026-05-22 v45 P0-1] 保存 thread 引用給 self-watchdog 的 is_alive() check
        global _scheduler_thread_ref
        _scheduler_thread_ref = background_thread
        background_thread.start()

        try:
            from PIL import Image
            import pystray  # type: ignore[import-not-found]

            img = Image.open(ICON_FILE) if ICON_FILE.exists() else Image.new("RGB", (64, 64), "grey")
            menu = (
                pystray.MenuItem("設定", lambda i, t: restart_program("--configure"), default=True),
                pystray.MenuItem("測試登入", run_immediate_test),
                pystray.MenuItem("顯示/隱藏控制台", toggle_console),
                pystray.MenuItem("退出", exit_action),
            )
            tray_icon_object = pystray.Icon(
                "AutoClock", img, f"自動打卡 v{CURRENT_VERSION}", menu)
            tray_icon_object.run()
        except ImportError:
            while running.is_set():
                time_module.sleep(1)

    except Exception:
        error_msg = f"程式發生嚴重錯誤導致崩潰：\n{traceback.format_exc()}"
        try:
            ctypes.windll.user32.MessageBoxW(0, error_msg, "自動打卡程式錯誤", 0x10)
        except Exception:
            print(error_msg, file=sys.stderr)
    finally:
        release_single_instance()


if __name__ == "__main__":
    main()
