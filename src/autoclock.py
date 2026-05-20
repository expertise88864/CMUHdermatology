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
import json  # noqa: E402
import logging  # noqa: E402
import queue  # noqa: E402
import random  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time as time_module  # noqa: E402
import tkinter as tk  # noqa: E402
import traceback  # noqa: E402
from datetime import date, datetime, time as dt_time  # noqa: E402
from pathlib import Path  # noqa: E402
from tkinter import messagebox, scrolledtext, simpledialog, ttk  # noqa: E402

import schedule  # noqa: E402
from selenium import webdriver  # noqa: E402
from selenium.common.exceptions import (  # noqa: E402
    StaleElementReferenceException, TimeoutException, WebDriverException,
)
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.support import expected_conditions as EC  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

from clock.webdriver_setup import initialize_driver  # noqa: E402
from cmuh_common.logging_setup import QueueHandler, setup_logging  # noqa: E402
from cmuh_common.paths import get_app_dir, get_settings_dir, restart_self  # noqa: E402
from cmuh_common.single_instance import ensure_single_instance, release_single_instance  # noqa: E402
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

accounts_data: list = []
_config_lock = threading.Lock()
running = threading.Event()
running.set()
background_thread: threading.Thread | None = None
tray_icon_object = None
log_queue: queue.Queue = queue.Queue(maxsize=5000)
clock_lock = threading.Lock()

# =============================================================================
# [autoclock 常駐 Chrome 池]
# 原本每個排程任務都新開 Chrome（~3 秒啟動）；改成跨任務重用同一 driver。
# 60 分鐘 idle 自動 quit，避免常駐記憶體無止境吃。
# 排程結束（程式關閉）時 atexit 確保 quit。
# =============================================================================
_persistent_driver_pool = {
    "driver": None,
    "last_used": 0.0,
    "lock": threading.Lock(),
}
_PERSISTENT_DRIVER_IDLE_TIMEOUT = 15 * 60  # 15 分鐘無使用 → 主動 quit
# 註：原本 60 分鐘但 idle 期間沒有人 wake 起來檢查，driver 等於永遠不釋放。
# 改 15 分鐘 + scheduler_loop 每分鐘主動檢查 → 兩批打卡之間 (08:00/12:00/12:30/
# 17:30/18:00) 中間 4 小時都會被釋放，省 ~150-250MB Chrome 記憶體。下次任務
# 重新 spin up 3-5 秒。


def _get_or_create_clock_driver():
    """取得常駐 driver；若 idle 過久或健康檢查失敗則重建。"""
    pool = _persistent_driver_pool
    with pool["lock"]:
        d = pool["driver"]
        now = time_module.time()

        # idle 過久 → quit 重建
        if d is not None and (now - pool["last_used"]) > _PERSISTENT_DRIVER_IDLE_TIMEOUT:
            logging.info("[autoclock] driver idle 超過 %d 分鐘，重建",
                         _PERSISTENT_DRIVER_IDLE_TIMEOUT // 60)
            try:
                d.quit()
            except Exception:
                pass
            d = None
            pool["driver"] = None

        # 健康檢查
        if d is not None:
            try:
                _ = d.window_handles
            except Exception:
                logging.info("[autoclock] driver 已死，重建")
                try:
                    d.quit()
                except Exception:
                    pass
                d = None
                pool["driver"] = None

        # 不存在或剛被清掉 → 重建
        if d is None:
            for attempt in range(4):
                d = initialize_driver()
                if d:
                    break
                logging.warning("[autoclock] WebDriver 初始化失敗 (%s/4)，退避重試", attempt + 1)
                if attempt < 3:
                    exponential_backoff_sleep(attempt, base_seconds=2.0, max_seconds=60.0)
            if d:
                d.set_script_timeout(30)
                pool["driver"] = d

        pool["last_used"] = now
        return d


def _release_persistent_clock_driver():
    """關閉常駐 driver（程式退出時呼叫）。"""
    pool = _persistent_driver_pool
    with pool["lock"]:
        d = pool["driver"]
        if d is not None:
            try:
                d.quit()
            except Exception:
                pass
            pool["driver"] = None


import atexit as _atexit_clock
_atexit_clock.register(_release_persistent_clock_driver)

# =============================================================================
# 業務常數
# =============================================================================
LOGIN_URL = "http://10.20.8.47/peoplesystem/electron_card/login.aspx"

CLOCK_IN_START_TIME = dt_time(7, 31, 0)
CLOCK_IN_END_TIME = dt_time(7, 59, 59)
CLOCK_MIDDAY_IN_START_TIME = dt_time(12, 30, 0)
CLOCK_MIDDAY_IN_END_TIME = dt_time(12, 59, 59)
CLOCK_MIDDAY_OUT_START_TIME = dt_time(12, 0, 0)
CLOCK_MIDDAY_OUT_END_TIME = dt_time(12, 30, 59)
CLOCK_PM_OUT_START_TIME = dt_time(17, 0, 0)
CLOCK_PM_OUT_END_TIME = dt_time(17, 30, 59)
TRIGGER_PM_OUT_START_TIME = dt_time(17, 1, 0)
CLOCK_EVE_OUT_START_TIME = dt_time(21, 0, 0)
CLOCK_EVE_OUT_END_TIME = dt_time(21, 30, 59)

VALIDATION_WINDOWS = {
    "am_in": (dt_time(7, 30, 0), dt_time(8, 0, 0)),
    "midday_out": (dt_time(12, 0, 0), dt_time(12, 30, 0)),
    "midday_in": (dt_time(12, 30, 0), dt_time(13, 0, 0)),
    "pm_out": (dt_time(17, 0, 0), dt_time(17, 30, 0)),
    "eve_out": (dt_time(21, 0, 0), dt_time(21, 30, 0)),
}

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
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(stream_handler)
    # 加上 queue handler 給 UI 顯示
    qh = QueueHandler(log_queue)
    qh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(qh)


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


def roc_to_gregorian_year(roc_year_str: str):
    try:
        y = int(roc_year_str)
        return y + 1911 if y > 0 else None
    except (ValueError, TypeError):
        return None


def parse_roc_date_str(roc_date_str: str):
    try:
        if len(roc_date_str) != 7:
            return None
        y, m, d = int(roc_date_str[:3]), int(roc_date_str[3:5]), int(roc_date_str[5:7])
        gy = roc_to_gregorian_year(str(y))
        if gy is None:
            return None
        return date(gy, m, d)
    except (ValueError, TypeError):
        return None


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
def load_config() -> list:
    global accounts_data
    with _config_lock:
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                accounts_data = data if isinstance(data, list) else []
            else:
                accounts_data = []
        except (json.JSONDecodeError, OSError) as e:
            logging.error("讀取設定失敗: %s", e)
            accounts_data = []
    return accounts_data


def save_config() -> bool:
    global accounts_data
    with _config_lock:
        try:
            accounts_data.sort(key=lambda x: x.get("username", ""))
            fd, temp_path = tempfile.mkstemp(
                suffix=".tmp", prefix="autoclock_", dir=str(SETTINGS_DIR))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(accounts_data, f, indent=4, ensure_ascii=False)
                os.replace(temp_path, CONFIG_FILE)
            except Exception:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise
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

            try:
                err_elem = WebDriverWait(driver, 2).until(
                    EC.visibility_of_element_located(get_loc("login_error_message")))
                if err_elem.text.strip():
                    raise RuntimeError(f"登入失敗: {err_elem.text}")
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
        pass


def get_current_swipe_info(driver, wait, get_loc):
    sys_date = None
    swipes: list = []
    last_swipe = None
    try:
        wait.until(EC.presence_of_element_located(get_loc("swipe_table")))
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
        pass
    return sys_date, swipes, last_swipe


def _check_swipes(type_str: str, start: dt_time, end: dt_time, swipes) -> bool:
    """檢查是否有「特定類型」紀錄落在 [start, end] 區間內。"""
    for t, typ in swipes:
        if typ == type_str:
            try:
                swipe_time = dt_time(int(t[:2]), int(t[2:]))
                if start <= swipe_time <= end:
                    return True
            except (ValueError, IndexError):
                continue
    return False


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

            _sys_date, swipes, _last = get_current_swipe_info(driver, wait, get_loc)
            act_name = "上班" if is_in else "下班"

            has_record_in_window = _check_swipes(act_name, check_start, check_end, swipes)

            if not dry_run and has_record_in_window:
                logging.info(
                    "%s 在區間 %s-%s 內已有 %s 紀錄，跳過。",
                    acc["username"], check_start, check_end, act_name)
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

            driver.execute_script("arguments[0].click();", exec_btn)

            try:
                WebDriverWait(driver, 5).until(EC.alert_is_present()).accept()
            except TimeoutException:
                pass

            logging.info("%s %s 打卡成功！", acc["username"], act_name)
            return

        except (StaleElementReferenceException, WebDriverException) as e:
            last_exc = e
            logging.warning(
                "%s 操作遇到 %s，重試中 (%s/%s)...",
                acc["username"], type(e).__name__, attempt + 1, retries)
            if attempt < retries - 1:
                exponential_backoff_sleep(attempt, base_seconds=2.0, max_seconds=60.0)
            else:
                logging.error("%s WebDriver 錯誤，重試用盡: %s", acc["username"], e)
        except Exception as e:
            last_exc = e
            logging.error("%s 操作失敗: %s", acc["username"], e)
            if dry_run:
                messagebox.showerror("測試失敗", str(e))
            break

    _handle_clock_failure(driver, acc["username"], task_label, last_exc, dry_run)


# =============================================================================
# 排程
# =============================================================================
def process_clock_task(schedule_key: str | None) -> None:
    if schedule_key is None:
        return
    if clock_lock.locked():
        logging.warning("任務 %s 觸發，但上一個任務尚未結束，正在等待...", schedule_key)

    with clock_lock:
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

        try:
            wait = WebDriverWait(driver, 20)
            for acc in accs:
                if not running.is_set():
                    break
                perform_clock_action(
                    driver, wait, acc, is_in, check_start, check_end,
                    dry_run=False, task_label=schedule_key,
                )
        except Exception as e:
            logging.error("任務 %s 執行期間發生錯誤: %s", schedule_key, e)
            try:
                _handle_clock_failure(driver, "system", schedule_key, e, dry_run=False)
            except Exception:
                pass
            # 任務級錯誤不關 driver；下次任務若 driver 不健康會自動重建
        finally:
            # 注意：不再 driver.quit()！常駐池管理（idle 60min 才釋放）
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
    if key:
        threading.Thread(target=process_clock_task, args=(key,), daemon=True).start()


def _idle_driver_janitor() -> None:
    """檢查 idle driver 是否該主動 quit (省記憶體)。

    讓使用者在沒打卡的空檔不會多佔 ~150-250MB Chrome 進程。
    跟 _get_or_create_clock_driver 的 idle-check 邏輯一致，但這邊主動觸發。
    """
    pool = _persistent_driver_pool
    with pool["lock"]:
        d = pool["driver"]
        if d is None:
            return
        now = time_module.time()
        idle_for = now - pool["last_used"]
        if idle_for > _PERSISTENT_DRIVER_IDLE_TIMEOUT:
            logging.info("[autoclock] driver idle %.0f 分鐘 (>%.0f 分)，主動 quit 省 RAM",
                         idle_for / 60, _PERSISTENT_DRIVER_IDLE_TIMEOUT / 60)
            try:
                d.quit()
            except Exception:
                logging.debug("idle driver quit 例外（忽略）", exc_info=True)
            pool["driver"] = None


def scheduler_loop() -> None:
    logging.info("背景排程器已啟動...")
    schedule.every(1).minute.at(":01").do(_scheduler_tick)
    # [優化] 每 2 分鐘主動檢查 idle driver，過期就 quit (省 ~150-250MB Chrome)
    schedule.every(2).minutes.do(_idle_driver_janitor)
    while running.is_set():
        schedule.run_pending()
        # [優化] 改 5s sleep — schedule 套件本身有 :01 精度，5s 內仍會準時觸發
        # 每分鐘任務。早期 1s 太密；對打卡 job 觀感無差，CPU 用量降 5 倍。
        time_module.sleep(5)


# =============================================================================
# 線上更新檢查（背景）
# =============================================================================
def _check_update_in_background() -> None:
    try:
        from cmuh_common.updater import check_and_update, need_restart_after_update
        result = check_and_update()
        if need_restart_after_update(result):
            logging.info("打卡程式偵測到新版，下次重啟生效")
            # 打卡背景常駐：不立即重啟（避免打斷正在進行的打卡），下次手動重啟生效
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
        while not log_queue.empty():
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
        save_config()
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


def restart_program(args_add=None) -> None:
    """[修正] 改用 cmuh_common.paths.restart_self 雙軌相容。"""
    global tray_icon_object
    running.clear()
    if tray_icon_object:
        tray_icon_object.stop()
    extra: list = []
    for a in sys.argv[1:]:
        if a not in ("--configure", "--test-login"):
            extra.append(a)
    if args_add:
        extra.append(args_add)
    restart_self(extra)


def exit_action(icon=None, item=None) -> None:
    logging.info("使用者要求退出程式...")
    running.clear()
    if tray_icon_object:
        tray_icon_object.stop()
    # 關閉常駐 driver（避免殘留 chromedriver.exe）
    try:
        _release_persistent_clock_driver()
    except Exception:
        pass
    release_single_instance()
    # [修正] 改用 sys.exit 讓 atexit 跑完
    sys.exit(0)


def run_immediate_test(icon=None) -> None:
    threading.Thread(target=_run_test_ui, daemon=True).start()


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
    try:
        _setup_clock_logging()
        logging.info("=== autoclock v%s 啟動 ===", CURRENT_VERSION)

        # [穩定性] health monitor — RAM/時鐘/硬碟 + 記憶體 leak 自動重啟 (A/E/F)
        try:
            from cmuh_common.health import start_health_monitor
            # 打卡 Chrome 啟動後正常 RSS ~300MB；warn 500、crit 800
            start_health_monitor("autoclock", ram_warn_mb=500, ram_crit_mb=800,
                                  interval_sec=300, network_check=False,
                                  auto_restart_on_crit=True,
                                  crit_persistence_ticks=6)
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

        # Mutex 單例
        if not ensure_single_instance("Local\\CMUH_Skin_AutoClock_SingleInstance_v1"):
            ctypes.windll.user32.MessageBoxW(
                0, "自動打卡程式已在執行中。", "自動打卡", 0x40 | 0x1000)
            sys.exit(0)

        # 背景檢查更新（不阻塞）
        threading.Thread(target=_check_update_in_background,
                         name="ClockUpdateChecker", daemon=True).start()

        load_config()

        if len(sys.argv) > 1:
            if sys.argv[1] == "--configure":
                ClockApp(accounts_data).mainloop()
                return
            if sys.argv[1] == "--test-login":
                run_immediate_test()
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

        background_thread = threading.Thread(target=scheduler_loop, daemon=True)
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


if __name__ == "__main__":
    main()
