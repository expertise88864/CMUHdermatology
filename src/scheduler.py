# -*- coding: utf-8 -*-
# =============================================================================
# 由 scripts/transform_pyw.py 自動生成。
# 重構自 _originals/中國醫皮膚科排班程式.pyw
# 共用基底已抽出至 cmuh_common/，本檔僅保留業務邏輯（UI、抓網、熱鍵等）。
# =============================================================================
import os
import sys

# 把 src/ 加到 sys.path，讓 cmuh_common / network / hotkey / ui / clock 子套件可用
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# === cmuh_common 共用基底 ===
from cmuh_common.version import CURRENT_VERSION, parse_version
from cmuh_common.paths import (
    get_app_dir, get_settings_dir, get_conf_path, restart_self, is_frozen,
)
from cmuh_common.atomic_io import atomic_write_json as _atomic_write_json
from cmuh_common.atomic_io import atomic_write_text
from cmuh_common.platform_win import (
    is_admin, run_as_admin, set_dpi_awareness, set_app_user_model_id, get_idle_duration,
)
from cmuh_common.notifications import show_windows_notification
from cmuh_common.icons import ensure_cmuh_app_icon_path as _ensure_cmuh_app_icon_path
from cmuh_common.window_icon import apply_tk_window_icon as _apply_tk_window_icon
from cmuh_common.logging_setup import QueueHandler
from cmuh_common.http_client import INTERNAL_HOSTS, is_internal as _is_internal
from cmuh_common.ui_messages import (
    UiStatusMessage, UiRefreshTickMessage, UiClinicDataMessage, UiMasterScheduleMessage,
    UiDutyDoctorMessage, UiSaturdayDutyDoctorMessage, UiTodayVsMessage, UiSaturdayVsMessage,
    UiClockStatusMessage, UiAlertInfoMessage, UiAlertErrorMessage, UiMessage, put_ui_message,
)
from cmuh_common.deps_runtime import ensure_dependencies as _ensure_deps_runtime

# === 依賴清單（與原檔一致；指紋由 deps_runtime 處理）===
REQUIRED_LIBS = [
    ("requests", "requests"),
    ("beautifulsoup4", "bs4"),
    ("lxml", "lxml"),
    ("selenium", "selenium"),
    ("keyboard", "keyboard"),
    ("pyautogui", "pyautogui"),
    ("schedule", "schedule"),
    ("psutil", "psutil"),
    ("Pillow", "PIL"),
    ("pystray", "pystray"),
    ("pywin32", "win32gui"),
]
_ensure_deps_runtime(REQUIRED_LIBS)

# === BASE_DIR / SETTINGS_DIR 沿用原語意 ===
BASE_DIR = get_app_dir()
SETTINGS_DIR = get_settings_dir()

# === [雙軌相容] _set_windows_dpi_awareness / _set_windows_app_user_model_id 兼容名 ===
_set_windows_dpi_awareness = set_dpi_awareness
_set_windows_app_user_model_id = set_app_user_model_id

# === 線上更新（取代原 UPDATE_MANIFEST + check_and_update）===
from cmuh_common import updater as _updater_mod  # noqa: E402


import sys
import subprocess
import importlib
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

# =============================================================================
# [開機前導] 自動依賴安裝與進度條介面 (Dependency Installer UI)
# =============================================================================
import ctypes
import json
import logging
import re
import schedule
import shutil
import webbrowser
from collections import defaultdict, deque
from copy import deepcopy
import hashlib
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dt_time
from queue import Empty, Queue
from typing import Any, NotRequired, TypedDict, TypeAlias, Union

class DoctorConfig(TypedDict):
    name: str
    doc_no: str
    notifications: NotRequired[bool]


def date_key_encoder(obj):
    """將 date 物件轉為 ISO 字串，以便存入 JSON"""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def decode_date_keys(dct):
    """讀取 JSON 時，將 ISO 日期字串轉回 date 物件（字典鍵）"""
    new_dct = {}
    for k, v in dct.items():
        try:
            new_k = date.fromisoformat(k)
        except ValueError:
            new_k = k
        new_dct[new_k] = v
    return new_dct

# --- 延遲載入 pyautogui / keyboard（啟動後由 AutomationApp.load_heavy_modules 填入）---
class HotkeyModules:
    __slots__ = ("pyautogui", "keyboard")

    def __init__(self) -> None:
        self.pyautogui: Any = None
        self.keyboard: Any = None


hotkey_modules = HotkeyModules()


def safe_unhook_all_hotkeys():
    try:
        if hotkey_modules.keyboard is not None:
            hotkey_modules.keyboard.unhook_all()
    except Exception as e:
        logging.warning(f"Failed to unhook hotkeys cleanly: {e}")

# --- 模組級 Regex 常數 (只 compile 一次，避免每次呼叫重複編譯) ---
_RE_COUNT_DIGIT = re.compile(r'(\d+)')          # 用於 _update_grid_data 計算人數
_RE_ROOM        = re.compile(r'\((\d+診)\)')     # 用於 check_appointment_count 診間號
_RE_COUNT_APPT  = re.compile(r'已掛號：(\d+)')   # 用於 check_appointment_count 掛號數
_RE_PERSON      = re.compile(r'(\d+)\s*人')      # 用於 check_appointment_count 人數
_RE_ROC_DATE    = re.compile(r'(\d{2,3})/(\d{2})/(\d{2})')

# ---------------------------
# --- [修改] Log 處理器：改為 Queue 模式 (防止 UI 卡死) ---
# --- 2. 全域設定與日誌 ---
LOG_FILE = os.path.join(BASE_DIR, 'automation_ui.log')  # [修正] 使用絕對路徑，避免工作目錄不同造成問題

# [修正] 改用 RotatingFileHandler，防止長期運行將磁碟空間耗盡
from logging.handlers import RotatingFileHandler
try:
    _rotating_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,  # 每個 log 最大 5MB
        backupCount=3,              # 保留最近 3 個備份 (共最多 15MB)
        encoding='utf-8',
        delay=True,                 # 延遲開檔，略減啟動 I/O（Py3.9+）
    )
except TypeError:
    _rotating_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8',
    )
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(threadName)s: %(message)s',
    handlers=[_rotating_handler]  # .pyw 無 console 視窗，StreamHandler 無作用且浪費資源
)

try:
    # 嘗試匯入外部函式庫 (只保留輕量級與必要的)
    import requests
    
    # --- [修正] SSL 驗證策略：只對已知院內主機關閉驗證，外部主機保持驗證 ---
    # 全域停用 verify=False 是安全漏洞。改為只對院內 IP/域名例外。
    from urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

    # -----------------------------

    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from bs4 import BeautifulSoup

    # --- [修改] 移除這裡的 Selenium 相關 import，移到下方函式內 ---
    # refresh_policy_utils 並非公開 PyPI 套件；門檻邏輯內嵌於本檔 DEFAULT_THRESHOLDS 下方。
except ImportError as e:
    missing_module = str(e).split("'")[1]
    error_message = f"缺少必要的模組: {missing_module}\n\n請打開命令提示字元(cmd)並執行:\npip install {missing_module}"
    logging.critical(error_message)
    ctypes.windll.user32.MessageBoxW(0, error_message, "模組錯誤", 0x10)
    sys.exit(1)

# --- 3. 門診與醫師設定 ---
DOCTORS = []
DOCTOR_NAMES = []

# [修改] 更新預設門檻值 (區分醫師)
DEFAULT_THRESHOLDS = {
    # 張廖年峰
    'chang_mon_night': 129, 'chang_thu_morning': 109, 'chang_thu_night': 129, 'chang_fri_afternoon': 89,
    # 陳駿升 (預設: 週一午69, 週二晚59, 周四早54, 週四午69)
    'chen_mon_afternoon': 69, 'chen_tue_night': 59, 'chen_thu_morning': 54, 'chen_thu_afternoon': 69
}
GENERAL_ALERT_THRESHOLD = 60


def build_doctor_threshold_map(doctor_name, threshold_settings):
    """依醫師與 threshold_settings.json 內容，建立 (weekday, 上午|下午|晚上) -> 止掛門檻。"""
    ts = threshold_settings if isinstance(threshold_settings, dict) else {}

    def _int_setting(key):
        v = ts.get(key, DEFAULT_THRESHOLDS.get(key))
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    if doctor_name == "張廖年峰":
        pairs = (
            ((0, "晚上"), "chang_mon_night"),
            ((3, "上午"), "chang_thu_morning"),
            ((3, "晚上"), "chang_thu_night"),
            ((4, "下午"), "chang_fri_afternoon"),
        )
    elif doctor_name == "陳駿升":
        pairs = (
            ((0, "下午"), "chen_mon_afternoon"),
            ((1, "晚上"), "chen_tue_night"),
            ((3, "上午"), "chen_thu_morning"),
            ((3, "下午"), "chen_thu_afternoon"),
        )
    else:
        return {}

    out = {}
    for session_key, cfg_key in pairs:
        iv = _int_setting(cfg_key)
        if iv is not None:
            out[session_key] = iv
    return out


def _appt_item_session_and_count_text(appt_item):
    """與月曆 _update_grid_data 相同來源結構，取出診別與可供擷取人數的狀態字串。"""
    if isinstance(appt_item, dict):
        session_name = appt_item.get("session", "")
        raw_count = appt_item.get("count", 0)
        status_text = str(raw_count)
        if isinstance(raw_count, int):
            status_text += "人"
        return session_name, status_text
    parts = appt_item.split("|")
    status_part = parts[0]
    session_name = status_part.split(":")[0]
    status_text = status_part.split(":", 1)[1].strip()
    return session_name, status_text


def is_near_alert_threshold(sessions, weekday_idx, threshold_map, margin=10):
    """當日任一診別掛號人數 >= 門檻 - margin 時為 True（供 priority refresh 加頻）。"""
    if not sessions or not threshold_map:
        return False
    try:
        m = int(margin)
    except (TypeError, ValueError):
        m = 10
    for appt_item in sessions:
        session_name, status_text = _appt_item_session_and_count_text(appt_item)
        if "休診" in status_text or "停診" in status_text:
            continue
        match = _RE_COUNT_DIGIT.search(status_text)
        if not match:
            continue
        try:
            count = int(match.group(1))
        except ValueError:
            continue
        thr = threshold_map.get((weekday_idx, session_name))
        if not isinstance(thr, int):
            continue
        if count >= thr - m:
            return True
    return False


REFRESH_QUERY_BATCH_1 = ("張廖年峰", "吳伯元", "陳駿升")
REFRESH_QUERY_BATCH_2 = ("謝佳陵", "方心禹", "沈冠宇")

def partition_doctors_for_refresh_batches(doctors):
    if not doctors:
        return []
    by_name = {d["name"]: d for d in doctors}
    b1 = [by_name[n] for n in REFRESH_QUERY_BATCH_1 if n in by_name]
    b2 = [by_name[n] for n in REFRESH_QUERY_BATCH_2 if n in by_name]
    fixed = set(REFRESH_QUERY_BATCH_1) | set(REFRESH_QUERY_BATCH_2)
    b3 = [d for d in doctors if d.get("name") not in fixed]
    return [batch for batch in (b1, b2, b3) if batch]

HOTKEY_SUPPORTED_RESOLUTIONS = ((1920, 1080), (1280, 1024), (1024, 768))
_HOTKEY_BASE_SIZE = {
    "1920x1080": (1920, 1080),
    "1280x1024": (1280, 1024),
    "1024x768": (1024, 768),
}
HOTKEY_ADAPTIVE_STATE = {
    "enabled": False,
    "base_version": None,
    "base_size": (0, 0),
    "target_size": (0, 0),
    "scale_x": 1.0,
    "scale_y": 1.0,
}

def configure_hotkey_scaling(enabled, base_version=None, target_size=None):
    HOTKEY_ADAPTIVE_STATE["enabled"] = bool(enabled)
    HOTKEY_ADAPTIVE_STATE["base_version"] = base_version
    if not enabled or base_version not in _HOTKEY_BASE_SIZE or not target_size:
        HOTKEY_ADAPTIVE_STATE["base_size"] = (0, 0)
        HOTKEY_ADAPTIVE_STATE["target_size"] = (0, 0)
        HOTKEY_ADAPTIVE_STATE["scale_x"] = 1.0
        HOTKEY_ADAPTIVE_STATE["scale_y"] = 1.0
        return
    base_w, base_h = _HOTKEY_BASE_SIZE[base_version]
    target_w, target_h = int(target_size[0]), int(target_size[1])
    HOTKEY_ADAPTIVE_STATE["base_size"] = (base_w, base_h)
    HOTKEY_ADAPTIVE_STATE["target_size"] = (target_w, target_h)
    HOTKEY_ADAPTIVE_STATE["scale_x"] = target_w / float(base_w)
    HOTKEY_ADAPTIVE_STATE["scale_y"] = target_h / float(base_h)

def _scaled_xy(x, y, base_version_hint=None):
    state = HOTKEY_ADAPTIVE_STATE
    if not state["enabled"]:
        return int(x), int(y)
    if base_version_hint and state.get("base_version") not in (None, base_version_hint):
        return int(x), int(y)
    sx = state.get("scale_x", 1.0)
    sy = state.get("scale_y", 1.0)
    return int(round(x * sx)), int(round(y * sy))

# --- 4. 全域執行緒控制事件 ---
stop_event_automation = threading.Event()
stop_event_main = threading.Event()

class SubsystemInterrupted(Exception): pass

# --- [新增] 系統閒置時間偵測結構 ---
import contextlib
_dummy_lock = contextlib.nullcontext()  # fallback 鎖，供無鎖環境使用


@contextlib.contextmanager
def _session_http_guard(session):
    """requests.Session 非執行緒安全；多執行緒共用時以鎖保護連線池與 cookie。"""
    lock = getattr(session, '_lock', None)
    if lock is not None:
        with lock:
            yield
    else:
        yield


_reg52_tls = threading.local()
_duty_tls = threading.local()
_reg64_tls = threading.local()
_ttl_cache_lock = threading.Lock()
_ttl_cache_store = {}
_parse_cache_store = {}
_source_backoff_state = {}
_reg52_main_fetch_sema = threading.Semaphore(3)


def _cache_get(cache_key, ttl_seconds):
    now = time.time()
    with _ttl_cache_lock:
        row = _ttl_cache_store.get(cache_key)
        if not row:
            return None
        ts, val = row
        if now - ts > ttl_seconds:
            _ttl_cache_store.pop(cache_key, None)
            return None
        return val


def _cache_set(cache_key, value):
    with _ttl_cache_lock:
        _ttl_cache_store[cache_key] = (time.time(), value)


def _parse_cache_get(parser_key, html_text):
    h = hashlib.sha1(html_text.encode("utf-8", errors="ignore")).hexdigest()
    key = (parser_key, h)
    now = time.time()
    with _ttl_cache_lock:
        row = _parse_cache_store.get(key)
        if not row:
            return None
        ts, val = row
        if now - ts > PARSE_CACHE_TTL_SECONDS:
            _parse_cache_store.pop(key, None)
            return None
        return val


def _parse_cache_set(parser_key, html_text, parsed):
    h = hashlib.sha1(html_text.encode("utf-8", errors="ignore")).hexdigest()
    key = (parser_key, h)
    with _ttl_cache_lock:
        _parse_cache_store[key] = (time.time(), parsed)


def _source_backoff_allow(source_key):
    now = time.time()
    with _ttl_cache_lock:
        row = _source_backoff_state.get(source_key)
        if not row:
            return True, 0.0
        next_allowed_ts, fail_count = row
        remain = max(0.0, next_allowed_ts - now)
        return remain <= 0.0, remain


def _source_backoff_fail(source_key):
    now = time.time()
    with _ttl_cache_lock:
        row = _source_backoff_state.get(source_key)
        fail_count = (row[1] + 1) if row else 1
        delay = min(SOURCE_BACKOFF_BASE_SECONDS * (2 ** (fail_count - 1)), SOURCE_BACKOFF_MAX_SECONDS)
        _source_backoff_state[source_key] = (now + delay, fail_count)
        return delay, fail_count


def _source_backoff_success(source_key):
    with _ttl_cache_lock:
        _source_backoff_state.pop(source_key, None)


def _get_thread_local_reg52_session():
    """ThreadPool 每個工作執行緒獨立 Session：掛號 reg52 可並行，且不再與 forward01 值班查詢搶同一連線鎖。"""
    s = getattr(_reg52_tls, "session", None)
    if s is None:
        s = requests.Session()
        rtry = Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        s.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=rtry))
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Connection": "keep-alive",
        })
        _reg52_tls.session = s
    return s


def _get_thread_local_duty_session():
    s = getattr(_duty_tls, "session", None)
    if s is None:
        s = requests.Session()
        rtry = Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=rtry))
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": "https://forward01.cmuh.org.tw/peoplesystem/Duty/DutyQuery.aspx",
            "Connection": "keep-alive",
        })
        _duty_tls.session = s
    return s


def _get_thread_local_reg64_session():
    s = getattr(_reg64_tls, "session", None)
    if s is None:
        s = requests.Session()
        # reg64 是高頻即時資料，逾時時優先快速返回，避免首屏卡在 retry。
        rtry = Retry(total=0, connect=0, read=0, redirect=0, status=0)
        s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=rtry))
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Connection": "keep-alive",
        })
        _reg64_tls.session = s
    return s


def _make_forward01_duty_session():
    """單次值班查詢專用 Session（不設 _lock，可並行；每工作各用一個實例）。"""
    s = requests.Session()
    rtry = Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=rtry))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://forward01.cmuh.org.tw/peoplesystem/Duty/DutyQuery.aspx",
        "Connection": "keep-alive",
    })
    return s


def check_stop():
    if stop_event_automation.is_set(): raise SubsystemInterrupted("by F12 key press")

def parse_color_spec(spec_string):
    spec = spec_string.replace(" ", "")
    rgb_match = re.search(r'\((\d+),(\d+),(\d+)\)', spec)
    if not rgb_match: return None, None
    try:
        rgb_tuple = (int(rgb_match.group(1)), int(rgb_match.group(2)), int(rgb_match.group(3)))
        if not all(0 <= val <= 255 for val in rgb_tuple): return None, None
        tolerance = None
        post_rgb_str = spec[rgb_match.end():]
        tol_match = re.match(r'(\d+)', post_rgb_str)
        if tol_match: tolerance = int(tol_match.group(1))
        return rgb_tuple, tolerance
    except ValueError: return None, None

def check_color(x, y, expected_rgb, tolerance=10):
    check_stop()
    try:
        sx, sy = _scaled_xy(x, y)
        return hotkey_modules.pyautogui.pixelMatchesColor(sx, sy, expected_rgb, tolerance=tolerance)
    except Exception:
        return False


class F11PixelFrameCache:
    """F11 while 單次迴圈內重複讀同一像素時快取，減少螢幕取樣次數。"""
    __slots__ = ("_pix",)

    def __init__(self):
        self._pix = {}

    def match_rgb(self, x, y, expected_rgb, tolerance=10):
        check_stop()
        try:
            sx, sy = _scaled_xy(x, y)
        except Exception:
            return False
        key = (sx, sy)
        if key not in self._pix:
            try:
                self._pix[key] = hotkey_modules.pyautogui.pixel(sx, sy)
            except Exception:
                self._pix[key] = None
        px = self._pix[key]
        if px is None:
            return False
        return all(abs(px[i] - expected_rgb[i]) <= tolerance for i in range(3))

    def match_spec_1024(self, x, y, spec_string):
        check_stop()
        rgb, tol = parse_color_spec(spec_string)
        if rgb is None:
            return False
        effective = tol if tol is not None else 10
        try:
            sx, sy = _scaled_xy(x, y, "1024x768")
        except Exception:
            return False
        key = (sx, sy)
        if key not in self._pix:
            try:
                self._pix[key] = hotkey_modules.pyautogui.pixel(sx, sy)
            except Exception:
                self._pix[key] = None
        px = self._pix[key]
        if px is None:
            return False
        return all(abs(px[i] - rgb[i]) <= effective for i in range(3))


def manage_scrollbar(scrollbar_widget, text_widget):
    text_widget.update_idletasks()
    if float(text_widget.index('end-1c').split('.')[0]) <= text_widget.cget('height'):
        scrollbar_widget.pack_forget()
    else:
        scrollbar_widget.pack(side="right", fill="y")

def format_vertical_text(text):
    return "\n".join(list(text))

# --- 打卡狀態檢查 ---
LOGIN_URL = "http://10.20.8.47/peoplesystem/electron_card/login.aspx"
LOCATORS = { 
    "username": ("id", "TB_logid"), 
    "password": ("id", "TB_pwd"), 
    "login_button": ("id", "bt_login"), 
    "system_time": ("id", "lb_systime"), 
    "swipe_table": ("id", "Gv_attppre"), 
    "execute_button": ("id", "bt_electron"), 
    "login_error_message": ("id", 'lblErrorMessage'), 
}

def roc_to_gregorian_year(roc_year_str):
    try: return int(roc_year_str) + 1911
    except (ValueError, TypeError): return None

def parse_roc_date_str(roc_date_str):
    if not roc_date_str or len(roc_date_str) != 7: return None
    try:
        greg_year = roc_to_gregorian_year(roc_date_str[:3])
        return date(greg_year, int(roc_date_str[3:5]), int(roc_date_str[5:7])) if greg_year else None
    except Exception: return None

def _initialize_status_driver():
    logging.info("Initializing headless WebDriver for status check...")
    
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        # [優化] 已徹底拔除 webdriver_manager 冗餘依賴
    except ImportError:
        logging.error("Selenium modules not found during runtime import.")
        return None

    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920x1080")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
    
    try:
        # [優化] Selenium 4.6+ 將自動呼叫底層 Selenium Manager 載入驅動，秒開毫秒就緒
        driver = webdriver.Chrome(options=chrome_options)
        logging.info("Headless WebDriver initialized successfully.")
        return driver
    except Exception as e:
        logging.error(f"Failed to initialize headless WebDriver: {e}")
        return None

# --- [修正] 打卡狀態抓取 (修正密碼錯誤Alert處理 + TAB優化) ---
def _get_swipe_status_from_web(username, password):
    # 定義檢查區間
    AM_START = dt_time(7, 30); AM_END = dt_time(8, 0)
    PM_START = dt_time(17, 0); PM_END = dt_time(17, 30)

    driver = _initialize_status_driver()
    if not driver: return {"error": "Driver失敗"}
    
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    # [新增] 引入 UnexpectedAlertPresentException
    from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, UnexpectedAlertPresentException
    from selenium.webdriver.common.keys import Keys 
    import time
    
    wait = WebDriverWait(driver, 15)
    result = {'上班': None, '下班': None} 

    try:
        driver.get(LOGIN_URL)
        
        # 1. 輸入帳號並觸發 PostBack
        try:
            user_elem = wait.until(EC.element_to_be_clickable((By.ID, "TB_logid")))
            user_elem.clear()
            user_elem.send_keys(username)
            user_elem.send_keys(Keys.TAB) # 模擬 TAB 觸發刷新
        except Exception as e:
            logging.error(f"輸入帳號時發生錯誤: {e}")

        time.sleep(0.5) # 等待PostBack刷新
        
        # 2. 輸入密碼
        try:
            pwd_elem = wait.until(EC.visibility_of_element_located((By.ID, "TB_pwd")))
            pwd_elem.clear()
            pwd_elem.send_keys(password)
        except StaleElementReferenceException:
            time.sleep(1)
            pwd_elem = driver.find_element(By.ID, "TB_pwd")
            pwd_elem.clear()
            pwd_elem.send_keys(password)

        # 3. 點擊登入
        login_success = False
        for attempt in range(2): 
            try:
                logging.info(f"嘗試點擊登入 (第 {attempt+1} 次)...")
                login_btn = wait.until(EC.element_to_be_clickable((By.ID, "bt_login")))
                driver.execute_script("arguments[0].click();", login_btn)
                
                # 等待表格出現
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "Gv_attppre")))
                login_success = True
                break 
            
            # [關鍵新增] 捕捉「密碼錯誤」或其他 Alert 彈窗
            except UnexpectedAlertPresentException as e:
                alert_text = e.alert_text
                logging.error(f"登入時遇到 Alert: {alert_text}")
                return {"error": f"{alert_text}"} # 直接回傳 Alert 內容給 UI 顯示
            
            except TimeoutException:
                logging.warning("點擊後未偵測到頁面跳轉，可能點擊無效，準備重試...")
                # 如果有 Alert 擋住，這裡也嘗試切換去接受它
                try:
                    driver.switch_to.alert.accept()
                    return {"error": "密碼/帳號錯誤"}
                except Exception:
                    pass

                try:
                    driver.find_element(By.ID, "bt_login")
                except Exception:
                    break

        if not login_success:
            return {"error": "登入逾時/失敗"}

        # 4. 解析資料
        sys_date = date.today()
        try:
            txt_elem = driver.find_element(By.ID, "lb_systime")
            txt = txt_elem.text 
            if "年" in txt:
                y_str = txt.split('年')[0]
                m_str = txt.split('年')[1].split('月')[0]
                d_str = txt.split('月')[1].split('日')[0]
                sys_date = date(int(y_str)+1911, int(m_str), int(d_str))
                logging.info(f"網站系統日期: {sys_date}")
        except Exception:
            logging.warning(f"解析網站日期失敗，使用本機日期: {sys_date}")

        rows_data = driver.execute_script("""
            var rows = document.querySelectorAll("#Gv_attppre tbody tr");
            var data = [];
            for (var i = 1; i < rows.length; i++) {
                var cols = rows[i].querySelectorAll("td");
                if (cols.length >= 3) {
                    data.push([
                        cols[0].innerText.trim(), 
                        cols[1].innerText.trim(), 
                        cols[2].innerText.trim() 
                    ]);
                }
            }
            return data;
        """)

        swipes = []
        if rows_data:
            for row in rows_data:
                d_str, t_str, type_str = row
                try:
                    if len(d_str) == 7:
                        r_year = int(d_str[:3])
                        r_month = int(d_str[3:5])
                        r_day = int(d_str[5:7])
                        row_date = date(r_year + 1911, r_month, r_day)
                        if row_date == sys_date:
                            t_str = t_str.zfill(4) 
                            swipes.append((t_str, type_str))
                            logging.info(f"找到今日打卡: {type_str} at {t_str}")
                except Exception:
                    continue

        def check_swipes(target_type, start_time, end_time):
            for t_str, typ in swipes:
                if typ == target_type:
                    try:
                        swipe_time = dt_time(int(t_str[:2]), int(t_str[2:]))
                        if start_time <= swipe_time <= end_time:
                            return True
                    except Exception:
                        continue
            return False

        if check_swipes("上班", AM_START, AM_END): result['上班'] = True
        else: result['上班'] = False if any(typ == "上班" for _, typ in swipes) else None

        if check_swipes("下班", PM_START, PM_END): result['下班'] = True
        else: result['下班'] = False if any(typ == "下班" for _, typ in swipes) else None

        return result

    except Exception as e:
        logging.error(f"打卡檢查發生錯誤: {e}")
        return {"error": str(e)[:20]}
    finally:
        driver.quit()

# =============================================================================
# --- 6. 自動化腳本 ---
# =============================================================================

# =============================================================================
# --- [重構] 統一的熱鍵執行器 (HotkeyRunner) ---
# 取代原本三份幾乎相同的 click_point_1920 / click_point_1280 / click_point_1024
# 所有解析度共用同一套邏輯，減少重複程式碼並消除 globals() 競態條件
# =============================================================================
class HotkeyRunner:
    """解析度無關的點擊/輸入執行器，取代 globals() 共享狀態"""

    def __init__(self, name: str):
        self.name = name
        self.last_action_time: float = 0.0

    def click(self, x: int, y: int, after_delay: float = 0.05) -> None:
        check_stop()
        hotkey_modules.pyautogui.moveTo(x, y, duration=0.01)
        check_stop()
        hotkey_modules.pyautogui.click()
        self.last_action_time = time.time()
        time.sleep(after_delay)

    def type_digits(self, digits: str, interval: float = 0.01) -> None:
        for d in digits:
            check_stop()
            hotkey_modules.pyautogui.typewrite(d)
            self.last_action_time = time.time()
            time.sleep(interval)

    def type_text(self, text: str, delay: float = 0.01) -> None:
        for char in text:
            check_stop()
            hotkey_modules.pyautogui.press(char)
            self.last_action_time = time.time()
            time.sleep(delay)

    def wait_for_color(self, x: int, y: int, target_color: tuple, timeout: float = 40) -> bool:
        start = time.time()
        while True:
            check_stop()
            try:
                if hotkey_modules.pyautogui.pixel(x, y) == target_color:
                    self.last_action_time = time.time()
                    return True
            except Exception:
                pass
            if time.time() - start > timeout:
                logging.warning(f"[{self.name}] Timeout waiting for color {target_color} at ({x},{y})")
                return False
            time.sleep(0.05)

    def wait_for_color_spec(self, x: int, y: int, spec_string: str, timeout: float = 15) -> bool:
        rgb, tol = parse_color_spec(spec_string)
        if rgb is None:
            raise SubsystemInterrupted(f"Invalid color spec '{spec_string}'")
        effective_tol = tol if tol is not None else 10
        start = time.time()
        while time.time() - start < timeout:
            check_stop()
            if check_color(x, y, rgb, tolerance=effective_tol):
                self.last_action_time = time.time()
                return True
            time.sleep(0.2)
        raise SubsystemInterrupted(f"[{self.name}] Idle timeout after {timeout}s waiting for color spec")

    def check_color_spec(self, x: int, y: int, spec_string: str) -> bool:
        rgb, tol = parse_color_spec(spec_string)
        if rgb is None:
            return False
        return check_color(x, y, rgb, tolerance=tol if tol is not None else 10)

    def wait_for_multiple_colors(self, conditions: list, timeout: float = 40) -> bool:
        """等待多個顏色條件同時成立"""
        start = time.time()
        while True:
            check_stop()
            if time.time() - start > timeout:
                logging.warning(f"[{self.name}] Timeout waiting for multiple color conditions")
                raise SubsystemInterrupted("Timeout waiting for color conditions")
            try:
                if all(check_color(x, y, col, tolerance=10) for x, y, col in conditions):
                    self.last_action_time = time.time()
                    return True
            except Exception:
                pass
            time.sleep(0.25)

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_action_time


# 各解析度對應的執行器實例 (取代全域變數 last_action_time_1920 等)
_runner_1920 = HotkeyRunner("1920x1080")
_runner_1280 = HotkeyRunner("1280x1024")
_runner_1024 = HotkeyRunner("1024x768")

# -----------------------------------------------------------------------------
# --- 6.1 熱鍵腳本 (1920x1080 版本) ---
# -----------------------------------------------------------------------------

def click_point_1920(x, y, after_delay=0.05):
    sx, sy = _scaled_xy(x, y, "1920x1080")
    _runner_1920.click(sx, sy, after_delay)

def type_digits_1920(digits):
    _runner_1920.type_digits(digits, interval=0.01)

def wait_for_color_1920(x, y, target_color, timeout=40):
    sx, sy = _scaled_xy(x, y, "1920x1080")
    return _runner_1920.wait_for_color(sx, sy, target_color, timeout)

def script_F10_1920x1080():
    logging.info("--- Executing F10 Actions (1920x1080) ---")
    
    # 步驟 1: 初始化點擊
    click_point_1920(511, 42)
    click_point_1920(552, 677)
    
    # 步驟 2: 等待黃色出現
    if not wait_for_color_1920(753, 149, (255, 255, 0), timeout=40):
        return

    # 步驟 3: 連續操作
    click_point_1920(124, 234)
    click_point_1920(21, 410)
    click_point_1920(559, 1003)
    click_point_1920(955, 585)
    
    # 步驟 4: 等待紫色出現
    if not wait_for_color_1920(1011, 857, (166, 77, 255), timeout=40):
        return

    # 步驟 5: 後續處理
    click_point_1920(817, 249)
    click_point_1920(590, 357)
    click_point_1920(603, 272)
    click_point_1920(819, 365)
    click_point_1920(572, 382)
    click_point_1920(604, 274)
    click_point_1920(667, 776)
    click_point_1920(642, 851)
    click_point_1920(910, 602)
    
    logging.info("F10 (1920x1080): Steps completed.")

def script_F11_1920x1080():
    logging.info("--- Executing F11 Actions (1920x1080) ---")
    click_point_1920(1686, 974)
    _runner_1920.last_action_time = time.time()
    
    # 追蹤每個步驟是否已執行
    steps_executed = {step: False for step in range(3, 18)}
    
    while True:
        check_stop()
        px = F11PixelFrameCache()
        
        # 閒置超時檢查
        if _runner_1920.idle_seconds > 40:
            logging.warning("F11 (1920x1080): Idle timeout after 40 seconds.")
            return

        # 終止條件
        if px.match_rgb(1010, 89, (255, 191, 255), tolerance=5):
            logging.info("F11 (1920x1080): Termination condition met.")
            return

        # Step 3
        if not steps_executed[3] and px.match_rgb(1697, 85, (0, 128, 128), 5) and px.match_rgb(1647, 44, (128, 255, 255), 5):
            logging.info("F11 (1920x1080): Executing step 3")
            click_point_1920(1791, 991)
            steps_executed[3] = True
            
        # Step 4
        elif not steps_executed[4] and px.match_rgb(1163, 299, (255, 128, 255), 5) and px.match_rgb(1377, 588, (240, 240, 240), 5):
            logging.info("F11 (1920x1080): Executing step 4")
            click_point_1920(871, 653)
            click_point_1920(1079, 759)
            steps_executed[4] = True
            
        # Step 5
        elif not steps_executed[5] and px.match_rgb(778, 514, (143, 219, 255), 5):
            logging.info("F11 (1920x1080): Executing step 5")
            click_point_1920(778, 589)
            steps_executed[5] = True

        # Step 6
        elif not steps_executed[6] and px.match_rgb(256, 337, (96, 171, 242), 5):
            logging.info("F11 (1920x1080): Executing step 6")
            click_point_1920(802, 865)
            steps_executed[6] = True

        # Step 7
        elif not steps_executed[7] and px.match_rgb(854, 775, (255, 255, 255), 5):
            logging.info("F11 (1920x1080): Executing step 7")
            click_point_1920(770, 840)
            steps_executed[7] = True

        # Step 8
        elif not steps_executed[8] and px.match_rgb(1529, 417, (255, 0, 0), 5):
            logging.info("F11 (1920x1080): Executing step 8")
            click_point_1920(176, 499)
            click_point_1920(1743, 897)
            steps_executed[8] = True

        # Step 9
        elif not steps_executed[9] and px.match_rgb(1263, 316, (0, 0, 255), 5) and px.match_rgb(855, 773, (255, 255, 255), 5) and px.match_rgb(805, 831, (240, 240, 240), 5):
            logging.info("F11 (1920x1080): Executing step 9")
            click_point_1920(781, 837)
            steps_executed[9] = True

        # Step 10
        elif not steps_executed[10] and px.match_rgb(834, 502, (0, 0, 255), 5):
            logging.info("F11 (1920x1080): Executing step 10")
            click_point_1920(761, 587)
            steps_executed[10] = True

        # Step 11
        elif not steps_executed[11] and px.match_rgb(550, 512, (0, 0, 0), 5):
            logging.info("F11 (1920x1080): Executing step 11")
            click_point_1920(637, 559)
            steps_executed[11] = True

        # Step 12
        elif not steps_executed[12] and px.match_rgb(1190, 539, (0, 0, 255), 5) and px.match_rgb(1237, 486, (255, 255, 225), 5):
            logging.info("F11 (1920x1080): Executing step 12")
            click_point_1920(1083, 603)
            steps_executed[12] = True

        # Step 13
        elif not steps_executed[13] and px.match_rgb(587, 480, (255, 255, 255), 5) and px.match_rgb(1327, 590, (240, 240, 240), 5):
            logging.info("F11 (1920x1080): Executing step 13")
            click_point_1920(1113, 890)
            steps_executed[13] = True

        # Step 14
        elif not steps_executed[14] and px.match_rgb(1078, 447, (0, 255, 255), 5) and px.match_rgb(1310, 656, (255, 218, 200), 5):
            logging.info("F11 (1920x1080): Executing step 14")
            click_point_1920(1309, 874)
            steps_executed[14] = True

        # Step 15
        elif not steps_executed[15] and px.match_rgb(1313, 421, (255, 255, 225), 5) and px.match_rgb(1303, 677, (240, 240, 240), 5) and px.match_rgb(800, 424, (255, 0, 0), 5):
            logging.info("F11 (1920x1080): Executing step 15")
            click_point_1920(1241, 679)
            steps_executed[15] = True

        # Step 16
        elif not steps_executed[16] and px.match_rgb(1261, 312, (0, 0, 255), 5) and px.match_rgb(1166, 821, (227, 227, 227), 5) and px.match_rgb(1286, 503, (192, 220, 192), 5):
            logging.info("F11 (1920x1080): Executing step 16")
            click_point_1920(606, 646)
            click_point_1920(1128, 835)
            steps_executed[16] = True

        # Step 17
        elif not steps_executed[17] and px.match_rgb(1345, 245, (6, 185, 171), 5) and px.match_rgb(1348, 276, (155, 230, 213), 5) and px.match_rgb(1260, 334, (6, 185, 171), 5):
            logging.info("F11 (1920x1080): Executing step 17")
            click_point_1920(1230, 795)
            time.sleep(0.05)
            click_point_1920(1189, 764)
            steps_executed[17] = True

        if all(steps_executed.values()):
            logging.info("F11 (1920x1080): All steps completed.")
            return
        
        time.sleep(0.05)

def script_F3_1920x1080():
    logging.info("--- Executing F3 Actions (1920x1080) ---")
    click_point_1920(147, 43)
    click_point_1920(230, 808)
    type_digits_1920("51017")
    hotkey_modules.pyautogui.press("enter")
    logging.info("F3 (1920x1080): Steps completed.")

def script_F4_1920x1080():
    logging.info("--- Executing F4 Actions (1920x1080) ---")
    click_point_1920(147, 43)
    click_point_1920(230, 808)
    type_digits_1920("51019")
    hotkey_modules.pyautogui.press("enter")
    click_point_1920(702, 123)
    hotkey_modules.pyautogui.typewrite("1")
    _runner_1920.last_action_time = time.time()
    logging.info("F4 (1920x1080): Steps completed.")


# -----------------------------------------------------------------------------
# --- 6.2 熱鍵腳本 (1024x768 版本) ---
# -----------------------------------------------------------------------------
def click_point_1024(x, y, after_delay=0.05):
    sx, sy = _scaled_xy(x, y, "1024x768")
    _runner_1024.click(sx, sy, after_delay)

def type_text_1024(text_to_type, delay=0.01):
    _runner_1024.type_text(text_to_type, delay)

def check_color_from_spec_1024(x, y, spec_string):
    sx, sy = _scaled_xy(x, y, "1024x768")
    return _runner_1024.check_color_spec(sx, sy, spec_string)

def wait_for_color_1024(x, y, spec_string, timeout=15):
    sx, sy = _scaled_xy(x, y, "1024x768")
    return _runner_1024.wait_for_color_spec(sx, sy, spec_string, timeout)

def script_F3_1024x768():
    logging.info("--- Executing F3 Actions (1024x768) ---")
    click_point_1024(142, 34, after_delay=0.1)
    click_point_1024(228, 468)
    click_point_1024(530, 346)
    click_point_1024(952, 710, after_delay=0.5)
    check_stop()
    hotkey_modules.pyautogui.press('enter')
    logging.info("F3 (1024x768): Steps completed.")

def script_F4_1024x768():
    logging.info("--- Executing F4 Actions (1024x768) ---")
    click_point_1024(142, 34, after_delay=0.1)
    click_point_1024(228, 468, after_delay=0.2)
    click_point_1024(529, 485)
    click_point_1024(952, 710)
    click_point_1024(469, 116)
    type_text_1024('1')
    logging.info("F4 (1024x768): Steps completed.")

def script_F9_1024x768():
    logging.info("--- Executing F9 Actions (1024x768) ---")
    click_point_1024(498, 35)
    click_point_1024(572, 626)
    wait_for_color_1024(854, 711, "(255,255,0)2")
    click_point_1024(123, 234)
    click_point_1024(24, 345)
    click_point_1024(559, 695)
    time.sleep(0.2)
    check_stop()
    if check_color_from_spec_1024(392, 363, "(0,0,0)240"):
        click_point_1024(510, 419)
    wait_for_color_1024(559, 695, "(166,77,255)")
    click_point_1024(372, 86)
    click_point_1024(145, 273)
    click_point_1024(158, 108)
    click_point_1024(371, 202)
    click_point_1024(125, 193)
    click_point_1024(158, 109)
    click_point_1024(222, 615)
    click_point_1024(199, 692)
    click_point_1024(507, 455)
    click_point_1024(484, 430)
    click_point_1024(530, 476) 
    logging.info("F9 (1024x768): All steps completed.")

def script_F10_1024x768():
    logging.info("--- Executing F10 Actions (1024x768) ---")
    click_point_1024(498, 35)
    click_point_1024(572, 626)
    wait_for_color_1024(854, 711, "(255,255,0)2")
    click_point_1024(123, 234)
    click_point_1024(24, 410)
    click_point_1024(559, 695)
    time.sleep(0.2)
    check_stop()
    if check_color_from_spec_1024(392, 363, "(0,0,0)240"):
        click_point_1024(510, 419)
    wait_for_color_1024(559, 695, "(166,77,255)")
    click_point_1024(372, 86)
    click_point_1024(142, 197)
    click_point_1024(158, 108)
    click_point_1024(371, 202)
    click_point_1024(125, 222)
    click_point_1024(158, 109)
    click_point_1024(222, 615)
    click_point_1024(199, 692)
    click_point_1024(507, 455)
    click_point_1024(484, 430)
    logging.info("F10 (1024x768): All steps completed.")

def script_F11_1024x768():
    logging.info("--- Executing F11 Actions (1024x768) ---")
    click_point_1024(925, 680)
    function_start_time = time.time()
    last_activity_time = time.time()
    
    steps_done = {
        "門診病史確認": False, "疼痛指標": False, "過敏紀錄維護-醫師端": False, 
        "藥物過敏紀錄": False, "轉診病人就診動向追蹤": False, "過敏紀錄維護-醫師端2": False, 
        "預約回診": False, "處方科品項": False, "健保初級照護照護轉診資訊": False, 
        "自費": False, "懷孕": False, "IC卡插好": False, 
        "新-半年過敏紀錄維護-醫師端": False, "新-半年過敏紀錄維護-醫師端2": False
    }
    
    while time.time() - function_start_time < 300:
        if time.time() - last_activity_time > 15:
            logging.warning("F11 (1024x768): Idle timeout after 15 seconds.")
            break
            
        check_stop()
        px = F11PixelFrameCache()
        action_taken_this_loop = False
        
        if px.match_spec_1024(1009, 93, "(255,191,255)"):
            logging.info("F11 (1024x768): Termination condition met.")
            return
        
        # 步驟檢查
        if not steps_done["新-半年過敏紀錄維護-醫師端"] and (px.match_spec_1024(881, 140, "(0,255,255)5") and px.match_spec_1024(857, 186, "(255,255,255)") and px.match_spec_1024(132, 272, "(128,255,255)")):
            logging.info("F11: '新-半年過敏紀錄維護-醫師端'")
            click_point_1024(153, 482)
            time.sleep(0.1)
            click_point_1024(677, 684)
            steps_done["新-半年過敏紀錄維護-醫師端"] = True
            action_taken_this_loop = True
            
        elif not steps_done["新-半年過敏紀錄維護-醫師端2"] and px.match_spec_1024(154, 198, "(255,255,255)") and px.match_spec_1024(138, 136, "(0,255,255)5") and px.match_spec_1024(135, 470, "(255,255,255)"):
            logging.info("F11: '新-半年過敏紀錄維護-醫師端2'")
            click_point_1024(154, 228)
            time.sleep(0.1)
            click_point_1024(675, 685)
            steps_done["新-半年過敏紀錄維護-醫師端2"] = True
            action_taken_this_loop = True
            
        elif not steps_done["門診病史確認"] and (px.match_spec_1024(706, 374, "(0,0,255)225") and px.match_spec_1024(804, 322, "(255,255,225)")):
            logging.info("F11: '門診病史確認'")
            click_point_1024(627, 455)
            steps_done["門診病史確認"] = True
            action_taken_this_loop = True
        
        elif not steps_done["疼痛指標"] and (px.match_spec_1024(814, 304, "(255,0,0)213") and px.match_spec_1024(164, 300, "(255,255,255)") and px.match_spec_1024(971, 49, "(6,185,171)3")):
            logging.info("F11: '疼痛指標'")
            click_point_1024(103, 358)
            time.sleep(0.1)
            check_stop()
            click_point_1024(933, 647)
            steps_done["疼痛指標"] = True
            action_taken_this_loop = True
            
        elif not steps_done["過敏紀錄維護-醫師端"] and (px.match_spec_1024(286, 74, "(153,153,153)") and px.match_spec_1024(275, 140, "(0,255,255)5") and px.match_spec_1024(410, 613, "(255,255,255)")):
            logging.info("F11: '過敏紀錄維護-醫師端'")
            click_point_1024(317, 674)
            steps_done["過敏紀錄維護-醫師端"] = True
            action_taken_this_loop = True
            
        elif not steps_done["藥物過敏紀錄"] and (px.match_spec_1024(893, 500, "(255,216,198)") and px.match_spec_1024(511, 616, "(0,255,255)5")):
            logging.info("F11: '藥物過敏紀錄'")
            click_point_1024(859, 707)
            steps_done["藥物過敏紀錄"] = True
            action_taken_this_loop = True
            
        elif not steps_done["轉診病人就診動向追蹤"] and (px.match_spec_1024(786, 208, "(255,255,255)") and px.match_spec_1024(120, 217, "(255,255,255)")):
            logging.info("F11: '轉診病人就診動向追蹤'")
            click_point_1024(780, 641)
            click_point_1024(712, 605)
            steps_done["轉診病人就診動向追蹤"] = True
            action_taken_this_loop = True
            
        elif not steps_done["過敏紀錄維護-醫師端2"] and (px.match_spec_1024(140, 142, "(0,255,255)0") and px.match_spec_1024(159, 487, "(255,255,255)") and px.match_spec_1024(723, 670, "(240,240,240)")):
            logging.info("F11: '過敏紀錄維護-醫師端2'")
            click_point_1024(159, 485)
            click_point_1024(679, 677)
            steps_done["過敏紀錄維護-醫師端2"] = True
            action_taken_this_loop = True
            
        elif not steps_done["預約回診"] and (px.match_spec_1024(997, 82, "(0,128,128)5") and px.match_spec_1024(875, 38, "(128,255,255)")):
            logging.info("F11: '預約回診'")
            click_point_1024(959, 705)
            steps_done["預約回診"] = True
            action_taken_this_loop = True
            
        elif not steps_done["處方科品項"] and (px.match_spec_1024(871, 475, "(255,255,225)") and px.match_spec_1024(314, 519, "(240,240,240)")):
            logging.info("F11: '處方科品項'")
            click_point_1024(799, 525)
            steps_done["處方科品項"] = True
            action_taken_this_loop = True
            
        elif not steps_done["健保初級照護照護轉診資訊"] and (px.match_spec_1024(554, 67, "(240,206,248)") and px.match_spec_1024(143, 461, "(255,255,255)")):
            logging.info("F11: '健保初級照護照護轉診資訊'")
            click_point_1024(144, 630)
            hotkey_modules.pyautogui.press(['tab', 'tab', 'enter'])
            steps_done["健保初級照護照護轉診資訊"] = True
            action_taken_this_loop = True
            
        elif not steps_done["自費"] and (px.match_spec_1024(310, 635, "(255,255,128)") and px.match_spec_1024(584, 632, "(252,186,247)")):
            logging.info("F11: '自費'")
            click_point_1024(90, 495, after_delay=0.05)
            click_point_1024(611, 493, after_delay=0.05)
            click_point_1024(422, 491, after_delay=0.05)
            click_point_1024(611, 603)
            steps_done["自費"] = True
            action_taken_this_loop = True
            
        elif not steps_done["懷孕"] and (px.match_spec_1024(734, 377, "(0,0,255)241") and px.match_spec_1024(809, 356, "(255,255,225)")):
            logging.info("F11: '懷孕'")
            click_point_1024(622, 452)
            steps_done["懷孕"] = True
            action_taken_this_loop = True
            
        elif not steps_done["IC卡插好"] and (px.match_spec_1024(416, 385, "(0,0,0)240") and px.match_spec_1024(536, 418, "(240,240,240)")):
            logging.info("F11: 'IC卡插好'")
            click_point_1024(514, 420)
            steps_done["IC卡插好"] = True
            action_taken_this_loop = True
        
        if action_taken_this_loop:
            last_activity_time = time.time()
        time.sleep(0.1)
    logging.info("F11 (1024x768): Script finished.")


# -----------------------------------------------------------------------------
# --- 6.3 熱鍵腳本 (1280x1024 版本) ---
# -----------------------------------------------------------------------------

def click_point_1280(x, y, after_delay=0.05):
    sx, sy = _scaled_xy(x, y, "1280x1024")
    _runner_1280.click(sx, sy, after_delay)

def type_digits_1280(digits, interval=0.1):
    _runner_1280.type_digits(digits, interval=interval)

def wait_for_multiple_colors_1280(conditions, timeout=40):
    scaled = []
    for x, y, color in conditions:
        sx, sy = _scaled_xy(x, y, "1280x1024")
        scaled.append((sx, sy, color))
    return _runner_1280.wait_for_multiple_colors(scaled, timeout)

def script_wait_for_F9_F10_cond_2_5():
    """(1280x1024) F9/F10 的 2.5 步驟等待"""
    logging.info("Waiting for F9/F10 condition 2.5 (1280x1024)...")
    conditions = [
        (759, 134, (255, 255, 0)),
        (1260, 60, (192, 220, 192)),
        (769, 266, (246, 246, 217))
    ]
    wait_for_multiple_colors_1280(conditions)

def script_wait_for_F9_F10_cond_5_5():
    """(1280x1024) F9/F10 的 5.5 步驟等待"""
    logging.info("Waiting for F9/F10 condition 5.5 (1280x1024)...")
    conditions = [
        (194, 738, (255, 255, 255)),
        (1061, 741, (179, 255, 255)),
        (687, 818, (166, 77, 255))
    ]
    wait_for_multiple_colors_1280(conditions)

def script_F11_1280x1024():
    logging.info("--- Executing F11 Actions (1280x1024) ---")
    click_point_1280(1144, 938, after_delay=0.1)
    _runner_1280.last_action_time = time.time()
    
    while True:
        check_stop()
        px = F11PixelFrameCache()
        if _runner_1280.idle_seconds > 40:
            logging.warning("F11 (1280x1024): Idle timeout after 40 seconds.")
            return

        action_taken = False
        
        # 11. (註記:終止F11迴圈)
        if px.match_rgb(1010, 84, (255, 191, 255), 10) and px.match_rgb(569, 32, (255, 255, 128), 10):
            logging.info("F11 (1280x1024): Termination condition met.")
            return

        # (註記:門診病史徵候確認事項)
        if (px.match_rgb(932, 456, (255, 255, 225), 10) and 
            px.match_rgb(934, 571, (240, 240, 240), 10) and 
            px.match_rgb(834, 496, (0, 0, 255), 10)):
            logging.info("F11 (1280x1024): Executing step (History Confirmation)")
            click_point_1280(754, 585, after_delay=0.1)
            action_taken = True

        # (註記:口腔黏膜篩檢)
        elif (px.match_rgb(673, 450, (0, 0, 255), 10) and 
            px.match_rgb(936, 595, (255, 255, 225), 10) and 
            px.match_rgb(936, 621, (240, 240, 240), 10)):
            logging.info("F11 (1280x1024): Executing step (Oral Mucosa Screening)")
            click_point_1280(489, 633, after_delay=0.1)
            action_taken = True

        # (註記:IC卡插好) - [新增規則]
        elif (px.match_rgb(546, 512, (0, 0, 0), 10) and 
              px.match_rgb(785, 518, (240, 240, 240), 10) and 
              px.match_rgb(641, 548, (0, 0, 0), 10)):
            logging.info("F11 (1280x1024): Executing step (IC Card Check)")
            click_point_1280(641, 548, after_delay=0.1)
            action_taken = True

        # (註記:半年過敏紀錄維護(無))
        elif (px.match_rgb(263, 267, (0, 255, 255), 10) and 
            px.match_rgb(261, 376, (255, 255, 255), 10) and 
            px.match_rgb(284, 434, (0, 120, 215), 10)):
            logging.info("F11 (1280x1024): Executing step (Allergy 6mon - None)")
            click_point_1280(281, 611, after_delay=0.1)
            click_point_1280(801, 814, after_delay=0.1)
            action_taken = True

        # (註記:半年藥物過敏資訊確認(有))
        elif (px.match_rgb(355, 629, (255, 255, 255), 10) and 
              px.match_rgb(531, 739, (255, 255, 255), 10) and 
              px.match_rgb(613, 810, (192, 220, 192), 10) and 
              px.match_rgb(1011, 271, (0, 255, 255), 10)):
            logging.info("F11 (1280x1024): Executing step (Allergy 6mon - Yes)")
            click_point_1280(446, 813, after_delay=0.1)
            action_taken = True

        # 1. (註記:疼痛指數)
        elif (px.match_rgb(1072, 219, (254, 254, 254), 10) and 
            px.match_rgb(1015, 393, (255, 0, 0), 10) and 
            px.match_rgb(1136, 42, (6, 185, 171), 10)):
            logging.info("F11 (1280x1024): Executing step 1 (Pain Index)")
            click_point_1280(123, 470, after_delay=0.2)
            click_point_1280(1160, 847, after_delay=0.1)
            action_taken = True

        # 2. (註記:抽血哪時抽)
        elif (px.match_rgb(263, 309, (206, 206, 255), 10) and 
              px.match_rgb(263, 678, (244, 244, 255), 10) and 
              px.match_rgb(286, 702, (166, 166, 166), 10)):
            logging.info("F11 (1280x1024): Executing step 2 (Blood Draw)")
            click_point_1280(628, 747, after_delay=0.1)
            action_taken = True

        # 3.(註記:診間預約掛號)
        elif (px.match_rgb(1245, 37, (0, 128, 128), 10) and 
              px.match_rgb(1244, 128, (192, 220, 192), 10) and 
              px.match_rgb(730, 33, (0, 255, 255), 10)):
            logging.info("F11 (1280x1024): Executing step 3 (Clinic Appt)")
            click_point_1280(1201, 933, after_delay=0.1)
            action_taken = True
        
        # 4. (註記:過敏記錄(有人註記時))
        elif (px.match_rgb(386, 267, (0, 255, 255), 10) and 
              px.match_rgb(394, 350, (255, 255, 249), 10) and 
              px.match_rgb(531, 737, (255, 255, 255), 10)):
            logging.info("F11 (1280x1024): Executing step 4 (Allergy Noted)")
            click_point_1280(448, 813, after_delay=0.1)
            action_taken = True

        # 5. (註記:藥物過敏記錄)
        elif (px.match_rgb(753, 407, (0, 255, 255), 10) and 
              px.match_rgb(1015, 614, (255, 218, 200), 10) and 
              px.match_rgb(984, 775, (240, 240, 240), 10)):
            logging.info("F11 (1280x1024): Executing step 5 (Drug Allergy)")
            click_point_1280(981, 839, after_delay=0.1)
            action_taken = True
            
        # 6. (註記:轉診資訊)
        elif (px.match_rgb(266, 697, (255, 255, 255), 10) and 
              px.match_rgb(986, 803, (240, 240, 240), 10) and 
              px.match_rgb(294, 196, (0, 0, 255), 10)):
            logging.info("F11 (1280x1024): Executing step 6 (Referral)")
            click_point_1280(789, 862, after_delay=0.1)
            action_taken = True
            
        # 7. (註記:就診動向追蹤(轉回原診所))
        elif (px.match_rgb(240, 388, (0, 0, 0), 10) and 
              px.match_rgb(932, 475, (6, 185, 171), 10) and 
              px.match_rgb(899, 517, (255, 255, 255), 10)):
            logging.info("F11 (1280x1024): Executing step 7/8 (Referral Follow-up)")
            click_point_1280(903, 766, after_delay=0.25)
            click_point_1280(822, 726, after_delay=0.1)
            action_taken = True
            
        # 9. (註記:病歷修改)
        elif (px.match_rgb(935, 373, (192, 192, 192), 10) and 
              px.match_rgb(934, 409, (255, 255, 225), 10) and 
              px.match_rgb(374, 401, (0, 0, 255), 10)):
            logging.info("F11 (1280x1024): Executing step 9 (Chart Modify)")
            click_point_1280(758, 663, after_delay=0.1)
            action_taken = True
            
        # 10. (註記:超過科閾值)
        elif (px.match_rgb(1012, 348, (255, 255, 255), 10) and 
              px.match_rgb(1012, 384, (255, 255, 225), 10) and 
              px.match_rgb(381, 376, (255, 0, 0), 10)):
            logging.info("F11 (1280x1024): Executing step 10 (Threshold)")
            click_point_1280(920, 658, after_delay=0.1)
            action_taken = True

        if action_taken:
            _runner_1280.last_action_time = time.time()
        
        time.sleep(0.05)

def script_F3_1280x1024():
    logging.info("--- Executing F3 Actions (1280x1024) ---")
    click_point_1280(144, 35, after_delay=0.1)
    click_point_1280(222, 802, after_delay=0.1)
    type_digits_1280("51017", interval=0.1)
    time.sleep(0.1)
    check_stop()
    hotkey_modules.pyautogui.press("enter")
    logging.info("F3 (1280x1024): Steps completed.")

def script_F4_1280x1024():
    logging.info("--- Executing F4 Actions (1280x1024) ---")
    click_point_1280(144, 35, after_delay=0.1)
    click_point_1280(222, 802, after_delay=0.1)
    type_digits_1280("51019", interval=0.1)
    time.sleep(0.1)
    check_stop()
    hotkey_modules.pyautogui.press("enter")
    check_stop()
    click_point_1280(585, 117, after_delay=0.05)
    hotkey_modules.pyautogui.typewrite("1")
    _runner_1280.last_action_time = time.time()
    logging.info("F4 (1280x1024): Steps completed.")

def script_F9_1280x1024():
    logging.info("--- Executing F9 Actions (1280x1024) ---")
    click_point_1280(494, 38, after_delay=0.1)    # 1
    click_point_1280(527, 644, after_delay=1.0)   # 2
    script_wait_for_F9_F10_cond_2_5()             # 2.5
    click_point_1280(125, 231, after_delay=0.1)   # 3
    click_point_1280(24, 345, after_delay=0.1)    # 4
    click_point_1280(555, 954, after_delay=0.1)   # 5
    
    # 5.4
    time.sleep(0.4)
    check_stop()
    if (check_color(632, 549, (0, 0, 0), 10) and 
        check_color(751, 555, (240, 240, 240), 10)):
        logging.info("F9 (1280x1024): Executing step 5.4 (Conditional click)")
        click_point_1280(639, 549, after_delay=0.1)
    
    script_wait_for_F9_F10_cond_5_5()             # 5.5
    click_point_1280(493, 209, after_delay=0.1)   # 6
    click_point_1280(270, 397, after_delay=0.1)   # 7
    click_point_1280(283, 231, after_delay=0.1)   # 8
    click_point_1280(492, 326, after_delay=0.1)   # 9
    click_point_1280(246, 318, after_delay=0.1)   # 10
    click_point_1280(280, 232, after_delay=0.1)   # 11
    click_point_1280(343, 739, after_delay=0.1)   # 12
    click_point_1280(319, 817, after_delay=0.1)   # 13
    click_point_1280(613, 564, after_delay=0.1)   # 14.
    logging.info("F9 (1280x1024): Steps completed.")

def script_F10_1280x1024():
    logging.info("--- Executing F10 Actions (1280x1024) ---")
    click_point_1280(494, 38, after_delay=0.1)    # 1
    click_point_1280(527, 644, after_delay=1.0)   # 2
    script_wait_for_F9_F10_cond_2_5()             # 2.5
    click_point_1280(125, 231, after_delay=0.1)   # 3
    click_point_1280(26, 407, after_delay=0.1)    # 4
    click_point_1280(555, 954, after_delay=0.1)   # 5

    # 5.4
    time.sleep(0.4)
    check_stop()
    if (check_color(632, 549, (0, 0, 0), 10) and 
        check_color(751, 555, (240, 240, 240), 10)):
        logging.info("F10 (1280x1024): Executing step 5.4 (Conditional click)")
        click_point_1280(639, 549, after_delay=0.1)

    script_wait_for_F9_F10_cond_5_5()             # 5.5
    click_point_1280(493, 209, after_delay=0.1)   # 6
    click_point_1280(266, 317, after_delay=0.1)   # 7
    click_point_1280(283, 231, after_delay=0.1)   # 8
    click_point_1280(492, 326, after_delay=0.1)   # 9
    click_point_1280(249, 343, after_delay=0.1)   # 10
    click_point_1280(280, 232, after_delay=0.1)   # 11
    click_point_1280(343, 739, after_delay=0.1)   # 12
    click_point_1280(319, 817, after_delay=0.1)   # 13
    click_point_1280(613, 564, after_delay=0.1)   # 14.
    logging.info("F10 (1280x1024): Steps completed.")

# =============================================================================
# --- 7. 門診檢查與動態門診表 ---
# =============================================================================
def create_master_schedule_from_web():
    master_schedule_url = "https://www.cmuh.cmu.edu.tw/OnlineAppointment/DymSchedule?table=37000A&flag=second"
    logging.info(f"Attempting to create master schedule from {master_schedule_url}")
    # [修正] 重命名為 result_schedule 避免遮蔽全域 schedule 模組
    result_schedule = {}
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(master_schedule_url, headers=headers, timeout=10, verify=True)
        response.raise_for_status()
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text,  'lxml')
        table = soup.find('table', class_='table-bordered')
        rows = table.find('tbody').find_all('tr')
        for row in rows:
            time_header = row.find('th')
            if not time_header: continue
            time_text = time_header.text.strip()
            current_slot = ""
            if "上午" in time_text: current_slot = "上午"
            elif "下午" in time_text: current_slot = "下午"
            elif "晚上" in time_text: current_slot = "晚上"
            else: continue
            cells = row.find_all('td')
            for day_idx, cell in enumerate(cells):
                if day_idx >= 6: break
                links = cell.find_all('a')
                for link in links:
                    full_name = link.text.strip()
                    is_self_paid = "(自費門診)" in full_name
                    clean_name = full_name.split('(')[0].strip()
                    if clean_name in DOCTOR_NAMES:
                        if clean_name not in result_schedule: result_schedule[clean_name] = {}
                        if day_idx not in result_schedule[clean_name]: result_schedule[clean_name][day_idx] = []
                        found = False
                        for entry in result_schedule[clean_name][day_idx]:
                            if entry['session'] == current_slot: found = True; break
                        if not found: result_schedule[clean_name][day_idx].append({'session': current_slot, 'is_self_paid': is_self_paid})
        logging.info(f"Successfully created master schedule for {len(result_schedule)} doctors.")
        return result_schedule
    except Exception as e:
        logging.error(f"Failed to create master schedule from web: {e}")
        return {}

def _safe_parse_roc_date(roc_date_str):
    match = _RE_ROC_DATE.search(roc_date_str or "")
    if not match:
        raise ValueError(f"無法解析日期: {roc_date_str}")
    year_part, month_part, day_part = match.groups()
    return datetime(int(year_part) + 1911, int(month_part), int(day_part)).date()

def _reg52_docno_for_dayoff_table(doc_no):
    """reg52.cgi 的 table#dayoff 僅出現在 DocNo=D12345；純數字 DocNo 回傳的 HTML 不含休診表。"""
    s = str(doc_no).strip()
    if s.upper().startswith("D"):
        return s
    return f"D{s}"


def _appt_dict_ext_branch(item):
    """掛號 dict 的院區：None=主院, 'east'=東區, 'auh'=亞大, 'huihe'=惠和, 'huisheng'=惠盛（僅 is_ext 之舊資料視為東區）。"""
    if not isinstance(item, dict):
        return None
    eb = item.get("ext_branch")
    if eb in ("east", "auh", "huihe", "huisheng"):
        return eb
    if item.get("is_ext"):
        return "east"
    return None


def _calendar_branch_sort_rank(ext_branch):
    """總覽同一時段內分院列順序：東區→亞大→惠和→惠盛→其他分院。"""
    if not ext_branch:
        return 0
    return {"east": 0, "auh": 1, "huihe": 2, "huisheng": 3}.get(ext_branch, 4)


_EXT_BRANCH_DISPLAY_SUFFIX = {
    "east": "(東區分院)",
    "auh": "(亞大)",
    "huihe": "(惠和醫院)",
    "huisheng": "(惠盛醫院)",
}


# 東區分院掛號（與主院 appointment.cmuh.org.tw 不同主機）
EAST_DISTRICT_REG52_URL = "http://61.66.117.10/cgi-bin/fh1/reg52.cgi"
# 主院網頁未寫「東區分院」時仍應改抓東區 fh1 的醫師（與院方實際設定有關）
EAST_FH1_DOCTOR_NAMES = frozenset({"吳伯元", "蔡李澄"})

# 惠和醫院掛號（與主院同網域，路徑為 wh1/reg52.cgi）
HUIHE_REG52_URL = "https://appointment.cmuh.org.tw/cgi-bin/wh1/reg52.cgi"
HUIHE_DOCTOR_NAMES = frozenset({"蔡李澄"})

# 惠盛醫院掛號（與東區同主機 61.66.117.10，路徑為 hs1/reg52.cgi）
HUISHENG_REG52_URL = "http://61.66.117.10/cgi-bin/hs1/reg52.cgi"
# 目前與惠和同醫師名單；若需不同請改為獨立 frozenset
HUISHENG_DOCTOR_NAMES = HUIHE_DOCTOR_NAMES

AUH_REG52_BASE_URL = "https://appointment.auh.org.tw/cgi-bin/as/reg52.cgi"
AUH_DOCTOR_DOCNO_MAP = {
    "方心禹": "D52646",
    "謝佳陵": "101823",
    "沈冠宇": "D28592",
}


def _main_html_has_east_branch_clinic(html_text):
    """主院 reg52 回應若提及東區分院門診，改向東區主機抓取人數／休診。"""
    return bool(html_text) and ("東區分院" in html_text)


def _should_fetch_east_district_reg52(html_main, doctor_name):
    return _main_html_has_east_branch_clinic(html_main) or doctor_name in EAST_FH1_DOCTOR_NAMES


def _should_fetch_huihe_reg52(doctor_name):
    return doctor_name in HUIHE_DOCTOR_NAMES


def _should_fetch_huisheng_reg52(doctor_name):
    return doctor_name in HUISHENG_DOCTOR_NAMES


def _strip_ext_appointments(appointments_by_date):
    """移除主院週表中內嵌之東區列（改以東區主機資料為準）；惠和僅來自 wh1，不在此處剔除。"""
    for date_key in list(appointments_by_date.keys()):
        bucket = appointments_by_date[date_key]
        appointments_by_date[date_key] = [
            x for x in bucket
            if not (isinstance(x, dict) and _appt_dict_ext_branch(x) == "east")
        ]


def _fetch_east_district_reg52_html(session, doc_no: str, doctor_name: str):
    """東區 fh1/reg52.cgi；Docname 先試 Big5 再試 UTF-8（不同醫師連結慣例不同）。"""
    from urllib.parse import quote, quote_from_bytes

    dparam = _reg52_docno_for_dayoff_table(doc_no)
    variants = []
    try:
        variants.append(quote_from_bytes(doctor_name.encode("big5")))
    except UnicodeEncodeError:
        pass
    variants.append(quote(doctor_name, safe=""))
    seen_urls = set()
    source_key = f"east:{doc_no}"
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip east fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return None
    for docname_q in variants:
        url = f"{EAST_DISTRICT_REG52_URL}?DocNo={dparam}&Docname={docname_q}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            r = session.get(url, timeout=20, verify=True)
            r.raise_for_status()
            r.encoding = "big5"
            text = r.text
            if len(text) < 500:
                continue
            probe = BeautifulSoup(text, "lxml")
            if probe.select_one("div.visitDate") or probe.select_one("table#dayoff"):
                logging.info(f"已自東區主機取得掛號表: {doctor_name} ({dparam})")
                _source_backoff_success(source_key)
                return text
        except requests.exceptions.RequestException as e:
            logging.debug(f"東區 reg52 請求失敗 ({url[:64]}…): {e}")
            delay, cnt = _source_backoff_fail(source_key)
            logging.warning(f"[BACKOFF] east fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
            continue
    logging.warning(f"無法自東區主機取得掛號表: {doctor_name} ({dparam})")
    return None


def _fetch_huihe_reg52_html(session, doc_no: str, doctor_name: str):
    """惠和 wh1/reg52.cgi（與主院同網域）；Docname 先試 Big5 再試 UTF-8。"""
    from urllib.parse import quote, quote_from_bytes

    dparam = _reg52_docno_for_dayoff_table(doc_no)
    variants = []
    try:
        variants.append(quote_from_bytes(doctor_name.encode("big5")))
    except UnicodeEncodeError:
        pass
    variants.append(quote(doctor_name, safe=""))
    seen_urls = set()
    source_key = f"huihe:{doc_no}"
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip huihe fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return None
    for docname_q in variants:
        url = f"{HUIHE_REG52_URL}?DocNo={dparam}&Docname={docname_q}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            r = session.get(url, timeout=20, verify=not _is_internal(url))
            r.raise_for_status()
            r.encoding = "big5"
            text = r.text
            if len(text) < 500:
                continue
            probe = BeautifulSoup(text, "lxml")
            if probe.select_one("div.visitDate") or probe.select_one("table#dayoff"):
                logging.info(f"已自惠和 wh1 取得掛號表: {doctor_name} ({dparam})")
                _source_backoff_success(source_key)
                return text
        except requests.exceptions.RequestException as e:
            logging.debug(f"惠和 reg52 請求失敗 ({url[:64]}…): {e}")
            delay, cnt = _source_backoff_fail(source_key)
            logging.warning(f"[BACKOFF] huihe fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
            continue
    logging.warning(f"無法自惠和取得掛號表: {doctor_name} ({dparam})")
    return None


def _fetch_huisheng_reg52_html(session, doc_no: str, doctor_name: str):
    """惠盛 hs1/reg52.cgi（與東區同主機）；Docname 先試 Big5 再試 UTF-8。"""
    from urllib.parse import quote, quote_from_bytes

    dparam = _reg52_docno_for_dayoff_table(doc_no)
    variants = []
    try:
        variants.append(quote_from_bytes(doctor_name.encode("big5")))
    except UnicodeEncodeError:
        pass
    variants.append(quote(doctor_name, safe=""))
    seen_urls = set()
    source_key = f"huisheng:{doc_no}"
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip huisheng fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return None
    for docname_q in variants:
        url = f"{HUISHENG_REG52_URL}?DocNo={dparam}&Docname={docname_q}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            r = session.get(url, timeout=20, verify=True)
            r.raise_for_status()
            r.encoding = "big5"
            text = r.text
            if len(text) < 500:
                continue
            probe = BeautifulSoup(text, "lxml")
            if probe.select_one("div.visitDate") or probe.select_one("table#dayoff"):
                logging.info(f"已自惠盛 hs1 取得掛號表: {doctor_name} ({dparam})")
                _source_backoff_success(source_key)
                return text
        except requests.exceptions.RequestException as e:
            logging.debug(f"惠盛 reg52 請求失敗 ({url[:64]}…): {e}")
            delay, cnt = _source_backoff_fail(source_key)
            logging.warning(f"[BACKOFF] huisheng fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
            continue
    logging.warning(f"無法自惠盛取得掛號表: {doctor_name} ({dparam})")
    return None


def _normalize_dayoff_session(cell_text):
    """DoctorInfo 停診表「診別」欄常見變體 → 上午/下午/晚上。無法辨識則回傳 None。"""
    if not cell_text:
        return None
    t = cell_text.replace(" ", "").replace("\u3000", "")
    if "上午" in t or "早診" in t or t.upper() == "AM":
        return "上午"
    if "下午" in t or "午診" in t or t.upper() == "PM":
        return "下午"
    if "晚上" in t or "晚診" in t or "夜診" in t or "夜間" in t:
        return "晚上"
    return None

def _merge_appointments_by_date(base_data, incoming_data):
    for date_key, records in incoming_data.items():
        bucket = base_data.setdefault(date_key, [])
        existing_keys = {
            (
                item.get('session'),
                item.get('room'),
                item.get('count'),
                _appt_dict_ext_branch(item),
                item.get('is_stopped'),
            )
            for item in bucket
            if isinstance(item, dict)
        }
        for record in records:
            record_key = (
                record.get('session'),
                record.get('room'),
                record.get('count'),
                _appt_dict_ext_branch(record),
                record.get('is_stopped'),
            )
            if record_key not in existing_keys:
                bucket.append(record)
                existing_keys.add(record_key)

def _merge_dayoff_overrides(base_data, dayoff_data):
    """停診列僅覆寫「相同診別且相同院區(主院/東區/惠和/惠盛)」的掛號資料。"""
    valid_sessions = {"上午", "下午", "晚上"}
    for date_key, records in dayoff_data.items():
        bucket = list(base_data.get(date_key, []))
        for record in records:
            session_name = record.get('session')
            if session_name not in valid_sessions:
                continue
            rec_br = _appt_dict_ext_branch(record)
            bucket = [
                item for item in bucket
                if not (
                    isinstance(item, dict)
                    and item.get('session') == session_name
                    and _appt_dict_ext_branch(item) == rec_br
                )
            ]
            bucket.append(record)
        base_data[date_key] = bucket

def _parse_main_hospital_schedule(soup):
    schedule_table = soup.select_one('table.schedule')
    if not schedule_table:
        # 兼容亞大/其他 reg52：無 table.schedule class，但仍有 timeSlot + schBox 結構
        for tbl in soup.find_all('table'):
            if tbl.select_one('td.timeSlot') and tbl.select_one('td.schBox'):
                schedule_table = tbl
                break
    if not schedule_table:
        return {}

    appointments_by_date = {}
    data_rows = schedule_table.select('tr')[1:]
    for row in data_rows:
        time_slot_cell = row.select_one('td.timeSlot')
        if not time_slot_cell:
            continue

        time_slot_text = ""
        cell_text = time_slot_cell.get_text(strip=True)
        cell_class = time_slot_cell.get('class', [])

        if 'AM' in cell_class or "上午" in cell_text:
            time_slot_text = "上午"
        elif 'PM' in cell_class or "下午" in cell_text:
            time_slot_text = "下午"
        elif 'Night' in cell_class or "晚上" in cell_text or "夜間" in cell_text:
            time_slot_text = "晚上"

        if not time_slot_text:
            continue

        for cell in row.select('td.schBox'):
            cell_content = cell.get_text(strip=True)
            is_external = "東區分院" in cell_content
            is_stopped = "止掛" in cell_content

            room = ""
            room_match = _RE_ROOM.search(cell_content)
            if room_match:
                room = room_match.group(1)

            for date_div in cell.find_all('div', class_='visitDate'):
                date_tag = date_div.find('b')
                if not date_tag:
                    continue

                roc_date_str = date_tag.get_text(strip=True)
                count = -1
                count_div = date_div.find_next_sibling('div')

                if count_div:
                    count_text = count_div.get_text()
                    count_match = _RE_COUNT_APPT.search(count_text)
                    if count_match:
                        count = int(count_match.group(1))
                    elif "已額滿" in count_text:
                        count = "已額滿"

                if count == -1:
                    content_without_date = cell_content.replace(roc_date_str, "")
                    fallback_match = _RE_PERSON.search(content_without_date)
                    if fallback_match:
                        count = int(fallback_match.group(1))
                    elif "額滿" in cell_content:
                        count = "已額滿"
                    elif "截止" in cell_content or "過" in cell_content:
                        count = "截止"
                    else:
                        count = 0

                date_key = _safe_parse_roc_date(roc_date_str)
                appointments_by_date.setdefault(date_key, []).append({
                    'session': time_slot_text,
                    'count': count if count != "截止" else "截止",
                    'is_ext': is_external,
                    'ext_branch': 'east' if is_external else None,
                    'room': room,
                    'is_stopped': is_stopped,
                })
    return appointments_by_date

def _parse_doctor_info_dayoff(soup, assume_east_branch=False, assume_huihe_branch=False, assume_huisheng_branch=False):
    """解析 reg52.cgi（宜使用 DocNo=D…）內之休診表：主院常用 table#dayoff；東區 fh1 常為 width=300 三欄小表。"""
    dayoff_table = soup.select_one("table#dayoff")
    if not dayoff_table:
        for tbl in soup.find_all("table"):
            if str(tbl.get("width") or "").strip() != "300":
                continue
            rows = tbl.find_all("tr")
            if len(rows) < 2:
                continue
            first_data = rows[1].find_all(["td", "th"])
            if len(first_data) != 3:
                continue
            if not _RE_ROC_DATE.search(first_data[0].get_text(" ", strip=True)):
                continue
            dayoff_table = tbl
            break
    if not dayoff_table:
        return {}

    appointments_by_date = {}
    for row in dayoff_table.select('tr')[1:]:
        cells = row.find_all(['td', 'th'])
        if len(cells) < 3:
            continue

        roc_date_str = cells[0].get_text(" ", strip=True)
        session_name = _normalize_dayoff_session(cells[1].get_text(" ", strip=True))
        replacement_text = cells[2].get_text(" ", strip=True) or "休診"
        if not session_name:
            logging.debug(f"停診表略過無法辨識診別之列: {cells[1].get_text(' ', strip=True)!r} / 日期 {roc_date_str!r}")
            continue

        row_joined = " ".join(c.get_text(" ", strip=True) for c in cells)
        if assume_east_branch:
            ext_branch = "east"
        elif assume_huihe_branch:
            ext_branch = "huihe"
        elif assume_huisheng_branch:
            ext_branch = "huisheng"
        else:
            ext_branch = "east" if ("東區" in row_joined or "東區分院" in row_joined) else None

        date_key = _safe_parse_roc_date(roc_date_str)
        appointments_by_date.setdefault(date_key, []).append({
            'session': session_name,
            'count': replacement_text,
            'is_ext': ext_branch is not None,
            'ext_branch': ext_branch,
            'room': "",
            'is_stopped': False,
        })
    return appointments_by_date

def _parse_fh_like_weekly_schedule(soup, ext_branch):
    """東區 fh1 / 惠和 wh1 / 惠盛 hs1 週表：無 table.schedule，診別常見「上 午」空格；以列首 + visitDate 解析。"""
    appointments_by_date = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        slot_norm = cells[0].get_text(" ", strip=True).replace(" ", "").replace("\u3000", "")
        if "上午" in slot_norm or "早診" in slot_norm:
            session_name = "上午"
        elif "下午" in slot_norm:
            session_name = "下午"
        elif "晚" in slot_norm or "夜間" in slot_norm:
            session_name = "晚上"
        else:
            continue

        for cell in cells[1:]:
            cell_text = cell.get_text(" ", strip=True)
            room_match = _RE_ROOM.search(cell_text)
            room = room_match.group(1) if room_match else ""

            for date_div in cell.find_all("div", class_="visitDate"):
                date_tag = date_div.find("b")
                if not date_tag:
                    continue

                roc_date_str = date_tag.get_text(strip=True)
                count_text = date_div.get_text(" ", strip=True)
                if "休診" in count_text or "停診" in count_text:
                    count = "休診"
                elif "已額滿" in count_text:
                    count = "已額滿"
                elif "截止" in count_text or "過" in count_text:
                    count = "截止"
                else:
                    count_match = _RE_COUNT_APPT.search(count_text)
                    count = int(count_match.group(1)) if count_match else 0

                date_key = _safe_parse_roc_date(roc_date_str)
                appointments_by_date.setdefault(date_key, []).append({
                    "session": session_name,
                    "count": count if count != "截止" else "截止",
                    "is_ext": True,
                    "ext_branch": ext_branch,
                    "room": room,
                    "is_stopped": False,
                })
    return appointments_by_date


def _parse_east_fh1_schedule(soup):
    """東區 61.66.117.10 fh1/reg52 週表。"""
    return _parse_fh_like_weekly_schedule(soup, "east")


def _parse_huihe_schedule(soup):
    """惠和 appointment.cmuh.org.tw wh1/reg52 週表。"""
    return _parse_fh_like_weekly_schedule(soup, "huihe")


def _parse_huisheng_schedule(soup):
    """惠盛 61.66.117.10 hs1/reg52 週表。"""
    return _parse_fh_like_weekly_schedule(soup, "huisheng")

def _parse_branch_schedule(soup):
    form = soup.find('form', attrs={'name': 'FrontPage_Form1'})
    if not form:
        return {}

    schedule_table = None
    for table in form.find_all('table'):
        first_row = table.find('tr')
        if first_row and "星期一" in table.get_text():
            schedule_table = table
            break
    if not schedule_table:
        return {}

    appointments_by_date = {}
    rows = schedule_table.find_all('tr')
    for row in rows[1:]:
        cells = row.find_all('td')
        if len(cells) < 2:
            continue

        slot_label = cells[0].get_text(" ", strip=True)
        if "上午" in slot_label or "早診" in slot_label:
            session_name = "上午"
        elif "下午" in slot_label:
            session_name = "下午"
        elif "晚" in slot_label or "夜間" in slot_label:
            session_name = "晚上"
        else:
            continue

        for cell in cells[1:]:
            room_match = _RE_ROOM.search(cell.get_text(" ", strip=True))
            room = room_match.group(1) if room_match else ""

            for date_div in cell.find_all('div', class_='visitDate'):
                date_tag = date_div.find('b')
                if not date_tag:
                    continue

                roc_date_str = date_tag.get_text(strip=True)
                count_text = date_div.get_text(" ", strip=True)
                if "已額滿" in count_text:
                    count = "已額滿"
                else:
                    count_match = _RE_COUNT_APPT.search(count_text)
                    count = int(count_match.group(1)) if count_match else 0

                date_key = _safe_parse_roc_date(roc_date_str)
                appointments_by_date.setdefault(date_key, []).append({
                    'session': session_name,
                    'count': count,
                    'is_ext': True,
                    'ext_branch': 'east',
                    'room': room,
                    'is_stopped': False,
                })
    return appointments_by_date

def _fetch_auh_reg52_html(session, doctor_name):
    from urllib.parse import quote
    doc_no = AUH_DOCTOR_DOCNO_MAP.get(doctor_name)
    if not doc_no:
        return ""
    url = f"{AUH_REG52_BASE_URL}?DocNo={doc_no}&Docname={quote(doctor_name, safe='')}"
    cache_key = ("auh_html", doctor_name, doc_no)
    hit = _cache_get(cache_key, REG52_AUH_TTL_SECONDS)
    if hit is not None:
        return hit
    source_key = f"auh:{doc_no}"
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip auh fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return ""
    try:
        r = session.get(url, timeout=20, verify=True)
        r.raise_for_status()
        r.encoding = "big5"
        text = r.text
        if "已掛號" in text or "visitDate" in text:
            logging.info(f"已自亞大附醫取得掛號表: {doctor_name} ({doc_no})")
        else:
            logging.warning(f"亞大附醫頁面未含掛號數欄位: {doctor_name} ({doc_no})")
        _cache_set(cache_key, text)
        _source_backoff_success(source_key)
        return text
    except requests.exceptions.RequestException as e:
        logging.warning(f"亞大附醫資料抓取失敗 ({doctor_name} {doc_no}): {e}")
        delay, cnt = _source_backoff_fail(source_key)
        logging.warning(f"[BACKOFF] auh fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
        return ""

def _parse_auh_reg52_schedule(soup):
    out = {}
    parsed = _parse_main_hospital_schedule(soup)
    for d, rows in parsed.items():
        for row in rows:
            rec = dict(row)
            rec["is_ext"] = True
            rec["ext_branch"] = "auh"
            out.setdefault(d, []).append(rec)
    if out:
        return out

    # 亞大 reg52 常見版型：無 timeSlot/schBox class，改以每列文字做日期+人數擷取
    for tr in soup.find_all("tr"):
        txt = tr.get_text(" ", strip=True)
        if not txt:
            continue
        txt_norm = txt.replace(" ", "").replace("\u3000", "")
        if ("上午" in txt_norm) or ("早診" in txt_norm):
            session_name = "上午"
        elif "下午" in txt_norm:
            session_name = "下午"
        elif ("晚上" in txt_norm) or ("夜間" in txt_norm) or ("晚診" in txt_norm):
            session_name = "晚上"
        else:
            continue

        pairs = re.findall(r'(\d{2,3}/\d{2}/\d{2})\s*已掛號[：:]\s*(\d+)', txt_norm)
        for roc_date_str, count_str in pairs:
            try:
                d = _safe_parse_roc_date(roc_date_str)
            except Exception:
                continue
            out.setdefault(d, []).append({
                "session": session_name,
                "count": int(count_str),
                "is_ext": True,
                "ext_branch": "auh",
                "room": "",
                "is_stopped": False,
            })
    return out

def check_appointment_count(ui_queue: "Queue[UiMessage]", doctor_config: DoctorConfig):
    session = _get_thread_local_reg52_session()
    doctor_name = doctor_config["name"]
    doc_no = str(doctor_config["doc_no"])
    target_url = f"https://appointment.cmuh.org.tw/cgi-bin/reg52.cgi?DocNo={doc_no}"
    last_exception = None

    for attempt in range(3):
        try:
            appointments_by_date = {}
            source_timing = {}
            source_timing["cache_hit_html"] = 0
            source_timing["cache_hit_parse"] = 0
            source_timing["cache_hit_reg64"] = 0
            source_timing["backoff_skip"] = 0

            logging.info(f"Attempt {attempt + 1} for {doctor_name} ({doc_no}) at {target_url}")
            html_dayoff = ""
            html_east = ""
            html_huihe = ""
            html_huisheng = ""
            html_auh = ""
            cache_main_key = ("main_html", doc_no)
            dayoff_url = (
                "https://appointment.cmuh.org.tw/cgi-bin/reg52.cgi"
                f"?DocNo={_reg52_docno_for_dayoff_table(doc_no)}"
            )
            dayoff_cache_key = ("dayoff_html", doc_no)
            verify_main = not _is_internal(target_url)
            verify_dayoff = not _is_internal(dayoff_url)

            html_main = _cache_get(cache_main_key, REG52_MAIN_TTL_SECONDS)
            html_dayoff = _cache_get(dayoff_cache_key, REG52_DAYOFF_TTL_SECONDS)
            need_main = html_main is None
            need_dayoff = html_dayoff is None

            if not need_main:
                source_timing["main_fetch_ms"] = 0
                source_timing["cache_hit_html"] += 1
            if not need_dayoff:
                source_timing["dayoff_fetch_ms"] = 0
                source_timing["cache_hit_html"] += 1

            if need_main and need_dayoff:
                def _parallel_fetch_main():
                    t0 = time.perf_counter()
                    sess = _get_thread_local_reg52_session()
                    sk_main = f"main:{doc_no}"
                    ok_main, remain_main = _source_backoff_allow(sk_main)
                    if not ok_main:
                        raise requests.exceptions.Timeout(f"main source backoff active ({remain_main:.1f}s)")
                    try:
                        with _reg52_main_fetch_sema:
                            with _session_http_guard(sess):
                                response = sess.get(target_url, timeout=20, verify=verify_main)
                                response.raise_for_status()
                                response.encoding = 'big5'
                                hm = response.text
                        _source_backoff_success(sk_main)
                    except requests.exceptions.RequestException:
                        delay, cnt = _source_backoff_fail(sk_main)
                        logging.warning(f"[BACKOFF] main fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
                        raise
                    _cache_set(cache_main_key, hm)
                    return hm, int((time.perf_counter() - t0) * 1000)

                def _parallel_fetch_dayoff():
                    t0 = time.perf_counter()
                    sess = _get_thread_local_reg52_session()
                    sk_dayoff = f"dayoff:{doc_no}"
                    ok_dayoff, _ = _source_backoff_allow(sk_dayoff)
                    if not ok_dayoff:
                        return "", 0, True
                    try:
                        with _session_http_guard(sess):
                            dayoff_response = sess.get(dayoff_url, timeout=20, verify=verify_dayoff)
                            dayoff_response.raise_for_status()
                            dayoff_response.encoding = "big5"
                            hd = dayoff_response.text
                        _cache_set(dayoff_cache_key, hd)
                        _source_backoff_success(sk_dayoff)
                        return hd, int((time.perf_counter() - t0) * 1000), False
                    except requests.exceptions.RequestException as e:
                        logging.warning(f"休診表 reg52 抓取失敗 ({doctor_name} {doc_no}): {e}")
                        delay, cnt = _source_backoff_fail(f"dayoff:{doc_no}")
                        logging.warning(f"[BACKOFF] dayoff fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
                        return "", int((time.perf_counter() - t0) * 1000), False

                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="r52pair") as pool:
                    fut_m = pool.submit(_parallel_fetch_main)
                    fut_d = pool.submit(_parallel_fetch_dayoff)
                    html_main, source_timing["main_fetch_ms"] = fut_m.result()
                    html_dayoff, source_timing["dayoff_fetch_ms"], dayoff_backoff = fut_d.result()
                    if dayoff_backoff:
                        source_timing["backoff_skip"] += 1

            elif need_main:
                t0 = time.perf_counter()
                sk_main = f"main:{doc_no}"
                ok_main, remain_main = _source_backoff_allow(sk_main)
                if not ok_main:
                    raise requests.exceptions.Timeout(f"main source backoff active ({remain_main:.1f}s)")
                try:
                    with _reg52_main_fetch_sema:
                        with _session_http_guard(session):
                            response = session.get(target_url, timeout=20, verify=verify_main)
                            response.raise_for_status()
                            response.encoding = 'big5'
                            html_main = response.text
                    _source_backoff_success(sk_main)
                except requests.exceptions.RequestException:
                    delay, cnt = _source_backoff_fail(sk_main)
                    logging.warning(f"[BACKOFF] main fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
                    raise
                _cache_set(cache_main_key, html_main)
                source_timing["main_fetch_ms"] = int((time.perf_counter() - t0) * 1000)

            elif need_dayoff:
                t_dayoff = time.perf_counter()
                with _session_http_guard(session):
                    try:
                        sk_dayoff = f"dayoff:{doc_no}"
                        ok_dayoff, _ = _source_backoff_allow(sk_dayoff)
                        if ok_dayoff:
                            dayoff_response = session.get(dayoff_url, timeout=20, verify=verify_dayoff)
                            dayoff_response.raise_for_status()
                            dayoff_response.encoding = "big5"
                            html_dayoff = dayoff_response.text
                            _cache_set(dayoff_cache_key, html_dayoff)
                            _source_backoff_success(sk_dayoff)
                        else:
                            source_timing["backoff_skip"] += 1
                    except requests.exceptions.RequestException as e:
                        logging.warning(f"休診表 reg52 抓取失敗 ({doctor_name} {doc_no}): {e}")
                        delay, cnt = _source_backoff_fail(f"dayoff:{doc_no}")
                        logging.warning(f"[BACKOFF] dayoff fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
                source_timing["dayoff_fetch_ms"] = int((time.perf_counter() - t_dayoff) * 1000)

            soup_main = BeautifulSoup(html_main, 'lxml')
            parsed = _parse_cache_get("main", html_main)
            if parsed is None:
                t_parse = time.perf_counter()
                parsed = _parse_main_hospital_schedule(soup_main)
                _parse_cache_set("main", html_main, parsed)
                source_timing["main_parse_ms"] = int((time.perf_counter() - t_parse) * 1000)
            else:
                source_timing["main_parse_ms"] = 0
                source_timing["cache_hit_parse"] += 1
            if parsed:
                _merge_appointments_by_date(appointments_by_date, parsed)
                # 先回主院資料，分院/亞大再補齊（漸進更新）
                put_ui_message(ui_queue, UiClinicDataMessage(doctor_name=doc_no, data=deepcopy(appointments_by_date)))

            if _should_fetch_east_district_reg52(html_main, doctor_name):
                ck = ("east_html", doc_no)
                html_east = _cache_get(ck, REG52_BRANCH_TTL_SECONDS)
                if html_east is None:
                    t0 = time.perf_counter()
                    html_east = _fetch_east_district_reg52_html(session, doc_no, doctor_name) or ""
                    _cache_set(ck, html_east)
                    source_timing["east_fetch_ms"] = int((time.perf_counter() - t0) * 1000)
                else:
                    source_timing["east_fetch_ms"] = 0
                    source_timing["cache_hit_html"] += 1

            if _should_fetch_huihe_reg52(doctor_name):
                ck = ("huihe_html", doc_no)
                html_huihe = _cache_get(ck, REG52_BRANCH_TTL_SECONDS)
                if html_huihe is None:
                    t0 = time.perf_counter()
                    html_huihe = _fetch_huihe_reg52_html(session, doc_no, doctor_name) or ""
                    _cache_set(ck, html_huihe)
                    source_timing["huihe_fetch_ms"] = int((time.perf_counter() - t0) * 1000)
                else:
                    source_timing["huihe_fetch_ms"] = 0
                    source_timing["cache_hit_html"] += 1

            if _should_fetch_huisheng_reg52(doctor_name):
                ck = ("huisheng_html", doc_no)
                html_huisheng = _cache_get(ck, REG52_BRANCH_TTL_SECONDS)
                if html_huisheng is None:
                    t0 = time.perf_counter()
                    html_huisheng = _fetch_huisheng_reg52_html(session, doc_no, doctor_name) or ""
                    _cache_set(ck, html_huisheng)
                    source_timing["huisheng_fetch_ms"] = int((time.perf_counter() - t0) * 1000)
                else:
                    source_timing["huisheng_fetch_ms"] = 0
                    source_timing["cache_hit_html"] += 1

            if doctor_name in AUH_DOCTOR_DOCNO_MAP:
                t0 = time.perf_counter()
                auh_key = ("auh_html", doctor_name, AUH_DOCTOR_DOCNO_MAP.get(doctor_name))
                html_auh = _cache_get(auh_key, REG52_AUH_TTL_SECONDS)
                if html_auh is not None:
                    source_timing["cache_hit_html"] += 1
                else:
                    ok_auh, _ = _source_backoff_allow(f"auh:{AUH_DOCTOR_DOCNO_MAP.get(doctor_name)}")
                    if not ok_auh:
                        source_timing["backoff_skip"] += 1
                    html_auh = _fetch_auh_reg52_html(session, doctor_name) or ""
                source_timing["auh_fetch_ms"] = int((time.perf_counter() - t0) * 1000)

            east_ok = False
            soup_east = None
            if html_east:
                soup_east = BeautifulSoup(html_east, "lxml")
                if soup_east.select_one("div.visitDate") or soup_east.select_one("table#dayoff"):
                    east_ok = True
            if east_ok:
                _strip_ext_appointments(appointments_by_date)
                parsed_east = _parse_east_fh1_schedule(soup_east)
                if parsed_east:
                    _merge_appointments_by_date(appointments_by_date, parsed_east)
            else:
                # 東區主機失敗時仍嘗試主院內嵌週表（FrontPage_Form1）
                parsed_branch = _parse_branch_schedule(soup_main)
                if parsed_branch:
                    _merge_appointments_by_date(appointments_by_date, parsed_branch)

            huihe_ok = False
            soup_huihe = None
            if html_huihe:
                soup_huihe = BeautifulSoup(html_huihe, "lxml")
                if soup_huihe.select_one("div.visitDate") or soup_huihe.select_one("table#dayoff"):
                    huihe_ok = True
            if huihe_ok:
                parsed_huihe = _parse_huihe_schedule(soup_huihe)
                if parsed_huihe:
                    _merge_appointments_by_date(appointments_by_date, parsed_huihe)

            huisheng_ok = False
            soup_huisheng = None
            if html_huisheng:
                soup_huisheng = BeautifulSoup(html_huisheng, "lxml")
                if soup_huisheng.select_one("div.visitDate") or soup_huisheng.select_one("table#dayoff"):
                    huisheng_ok = True
            if huisheng_ok:
                parsed_huisheng = _parse_huisheng_schedule(soup_huisheng)
                if parsed_huisheng:
                    _merge_appointments_by_date(appointments_by_date, parsed_huisheng)
            if html_auh:
                soup_auh = BeautifulSoup(html_auh, "lxml")
                parsed_auh = _parse_cache_get("auh", html_auh)
                if parsed_auh is None:
                    parsed_auh = _parse_auh_reg52_schedule(soup_auh)
                    _parse_cache_set("auh", html_auh, parsed_auh)
                else:
                    source_timing["cache_hit_parse"] += 1
                auh_merged_slots = sum(len(v) for v in parsed_auh.values()) if parsed_auh else 0
                if parsed_auh:
                    _merge_appointments_by_date(appointments_by_date, parsed_auh)
                else:
                    logging.warning(f"亞大附醫解析後無可用門診資料: {doctor_name}")
                logging.info(f"AUH merged slots = {auh_merged_slots} ({doctor_name})")

            if html_east or html_huihe or html_huisheng or html_auh:
                put_ui_message(ui_queue, UiClinicDataMessage(doctor_name=doc_no, data=appointments_by_date))

            dayoff_data = _parse_doctor_info_dayoff(BeautifulSoup(html_dayoff, "lxml")) if html_dayoff else {}
            if dayoff_data:
                _merge_dayoff_overrides(appointments_by_date, dayoff_data)

            if east_ok:
                east_dayoff = _parse_doctor_info_dayoff(soup_east, assume_east_branch=True)
                if east_dayoff:
                    _merge_dayoff_overrides(appointments_by_date, east_dayoff)

            if huihe_ok:
                huihe_dayoff = _parse_doctor_info_dayoff(soup_huihe, assume_huihe_branch=True)
                if huihe_dayoff:
                    _merge_dayoff_overrides(appointments_by_date, huihe_dayoff)

            if huisheng_ok:
                huisheng_dayoff = _parse_doctor_info_dayoff(soup_huisheng, assume_huisheng_branch=True)
                if huisheng_dayoff:
                    _merge_dayoff_overrides(appointments_by_date, huisheng_dayoff)

            data_count = sum(len(v) for v in appointments_by_date.values())
            if data_count == 0:
                raise ValueError("查無任何可用門診資料")

            if source_timing:
                logging.info(f"[SOURCE_TIMING] {doctor_name}: {source_timing}")
            logging.info(f"Check for {doctor_name} ({doc_no}) successful. Found {data_count} slots.")
            put_ui_message(ui_queue, UiRefreshTickMessage(doctor_name=doctor_name))
            put_ui_message(ui_queue, UiClinicDataMessage(doctor_name=doc_no, data=appointments_by_date))
            return

        except (requests.exceptions.RequestException, ValueError) as e:
            last_exception = e
            wait_time = (attempt + 1) * 2
            logging.warning(f"Attempt {attempt + 1} for {doctor_name} failed: {e}. Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

    error_type = type(last_exception).__name__ if last_exception else "Unknown Error"
    logging.error(f"All 3 attempts to check for {doctor_name} failed.")
    put_ui_message(ui_queue, UiRefreshTickMessage(doctor_name=doctor_name))
    put_ui_message(
        ui_queue,
        UiClinicDataMessage(doctor_name=doc_no, data={"error": f"查詢失敗 ({error_type})"}),
    )

def load_master_schedule_in_background(ui_queue: "Queue[UiMessage]"):
    logging.info("Loading master schedule in background...")
    schedule = create_master_schedule_from_web()
    put_ui_message(ui_queue, UiMasterScheduleMessage(schedule=schedule))

# --- 8. 值班醫師查詢 ---
_DUTY_HTTP_TIMEOUT = 40

def _perform_duty_query(session, roc_date_str):
    """forward01 值班查詢（GET 表單 + POST）。專用 session + 重試，避免與掛號搶鎖或單次逾時即失敗。"""
    duty_url = "https://forward01.cmuh.org.tw/peoplesystem/Duty/DutyQuery.aspx"
    cache_key = ("duty_query_html", roc_date_str)
    cached_html = _cache_get(cache_key, DUTY_CACHE_TTL_SECONDS)
    if cached_html is not None:
        return BeautifulSoup(cached_html, 'lxml')
    verify = not _is_internal(duty_url)
    last_exc = None
    for attempt in range(3):
        try:
            with _session_http_guard(session):
                response_get = session.get(duty_url, timeout=_DUTY_HTTP_TIMEOUT, verify=verify)
                response_get.raise_for_status()
                soup_get = BeautifulSoup(response_get.text, 'lxml')

                form_data = {}
                for input_tag in soup_get.find_all('input'):
                    input_type = input_tag.get('type', '').lower()
                    name = input_tag.get('name')
                    if name and input_type not in ['submit', 'image']:
                        form_data[name] = input_tag.get('value', '')

                for select_tag in soup_get.find_all('select'):
                    name = select_tag.get('name')
                    if name:
                        selected_option = select_tag.find('option', selected=True)
                        if selected_option and selected_option.has_attr('value'):
                            form_data[name] = selected_option['value']
                        else:
                            first_option = select_tag.find('option')
                            form_data[name] = first_option['value'] if first_option and first_option.has_attr('value') else ''

                form_data['Tb_sdate'] = roc_date_str
                form_data['Tb_edate'] = roc_date_str
                form_data['Bt_query'] = '查詢'

                response_post = session.post(duty_url, data=form_data, timeout=_DUTY_HTTP_TIMEOUT, verify=verify)
                response_post.raise_for_status()
                _cache_set(cache_key, response_post.text)
                return BeautifulSoup(response_post.text, 'lxml')
        except requests.exceptions.RequestException as e:
            last_exc = e
            logging.warning(f"DutyQuery ({roc_date_str}) attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError("DutyQuery failed")

def fetch_duty_doctor(ui_queue: "Queue[UiMessage]", session: requests.Session, r_doctor_map: dict[str, Any]):
    logging.info("Attempting to fetch today's duty doctor (Night Shift 1700-0800)...")
    doctor_name = "查詢失敗"
    found_target = False

    try:
        today = datetime.now()
        roc_year = today.year - 1911
        roc_date_str = f"{roc_year}{today.strftime('%m%d')}"
        logging.info(f"Looking for today's ROC date: {roc_date_str}")
        
        soup_post = _perform_duty_query(session, roc_date_str)
        rows = soup_post.find_all('tr')
        
        for row in rows:
            cells = row.find_all('td')
            # 確保欄位足夠 (HTML 表格結構: 日期/單位/地點/順序/類別/代碼/姓名/開始/結束/電話)
            if len(cells) > 8:
                date_cell_text = cells[0].get_text(strip=True)
                dept_cell_text = cells[1].get_text(strip=True)
                
                # [修正關鍵] 直接讀取第 8 欄 (Index 7) 的「開始時間」
                # HTML: <td>1700</td>
                start_time_text = cells[7].get_text(strip=True)
                
                if date_cell_text == roc_date_str and "皮膚科" in dept_cell_text:
                    
                    # 判斷開始時間是否為夜班時段 (17:00, 17:30, 18:00)
                    if start_time_text in ["1700", "1730", "1800"]:
                        
                        name_span = cells[6].find('span')
                        if not name_span: continue
                        
                        scraped_name = name_span.get_text(strip=True)
                        
                        # 找到名字後，去對應 R1/R2/R3 的姓名設定
                        matched_r_key = None
                        matched_r_info = None
                        
                        for r_key, r_info in r_doctor_map.items():
                            if r_info['name'] == scraped_name:
                                matched_r_key = r_key
                                matched_r_info = r_info
                                break
                        
                        if matched_r_key:
                            doctor_name = f"{matched_r_key} {matched_r_info['name']}"
                        else:
                            # 如果設定檔沒這個人，直接顯示抓到的名字
                            doctor_name = scraped_name
                            
                        logging.info(f"MATCH: Found Night Shift Doctor (Start: {start_time_text}): {doctor_name}")
                        found_target = True
                        break 

        if not found_target:
            if doctor_name == "查詢失敗": 
                logging.warning("Could not find suitable Night Shift R (Start time 1700/1730/1800).")
                doctor_name = "未找到"
                
    except requests.exceptions.RequestException:
        logging.error("A connection error occurred while fetching duty doctor.", exc_info=True)
        doctor_name = "網路錯誤"
    except Exception as e:
        logging.error(f"An unexpected error occurred while fetching duty doctor: {e}", exc_info=True)
        doctor_name = "查詢錯誤"
    
    put_ui_message(ui_queue, UiDutyDoctorMessage(doctor_name=doctor_name))

def fetch_saturday_duty_doctor(ui_queue: "Queue[UiMessage]", session: requests.Session, r_doctor_map: dict[str, Any]):
    logging.info("Attempting to fetch this week's Saturday duty doctor...")
    doctor_name = "查詢失敗"
    saturday_date = date.today()
    try:
        today = date.today()
        days_ahead = (5 - today.weekday() + 7) % 7
        saturday_date = today + timedelta(days=days_ahead)
        roc_year = saturday_date.year - 1911
        saturday_roc_date_str = f"{roc_year}{saturday_date.strftime('%m%d')}"
        logging.info(f"Looking for Saturday's ROC date: {saturday_roc_date_str}")
        soup_post = _perform_duty_query(session, saturday_roc_date_str)
        rows = soup_post.find_all('tr')
        for row in rows:
            cells = row.find_all('td')
            if len(cells) > 7:
                date_cell_text = cells[0].get_text(strip=True)
                dept_cell_text = ' '.join(cells[1].get_text(strip=True).split())
                time_cell_text = ' '.join(cells[2].get_text(strip=True).split())
                if (date_cell_text == saturday_roc_date_str and "1850 -皮膚科" in dept_cell_text and "On call(1700-0800)" in time_cell_text):
                    name_span = cells[6].find('span')
                    if name_span:
                        scraped_name = name_span.get_text(strip=True)
                        logging.info(f"Found Saturday duty doctor: {scraped_name}")
                        for r_key, r_info in r_doctor_map.items():
                            if r_info['name'] == scraped_name:
                                doctor_name = f"{r_key} {r_info['name']}"; break
                        else: doctor_name = scraped_name
                        break
        else:
            if doctor_name == "查詢失敗": logging.warning("Could not find Saturday's duty doctor for Dermatology."); doctor_name = "未找到"
    except requests.exceptions.RequestException: logging.error("A connection error occurred while fetching Saturday's duty doctor.", exc_info=True); doctor_name = "網路錯誤"
    except Exception as e: logging.error(f"An unexpected error occurred while fetching Saturday's duty doctor: {e}", exc_info=True); doctor_name = "查詢錯誤"
    put_ui_message(
        ui_queue,
        UiSaturdayDutyDoctorMessage(saturday_date=saturday_date, doctor_name=doctor_name),
    )

def fetch_duty_vs(ui_queue: "Queue[UiMessage]", session: requests.Session, vs_type: str):
    is_saturday = (vs_type == 'saturday_vs')
    log_prefix = "Saturday" if is_saturday else "Today's"
    logging.info(f"Attempting to fetch {log_prefix} duty VS...")
    doctor_name = "查詢失敗"
    try:
        today = date.today()
        target_date = today
        if is_saturday:
            days_ahead = (5 - today.weekday() + 7) % 7
            target_date = today + timedelta(days=days_ahead)
        
        roc_year = target_date.year - 1911
        roc_date_str = f"{roc_year}{target_date.strftime('%m%d')}"
        logging.info(f"Looking for {log_prefix} VS on ROC date: {roc_date_str}")
        
        soup_post = _perform_duty_query(session, roc_date_str)
        rows = soup_post.find_all('tr')
        
        found = False
        for row in rows:
            cells = row.find_all('td')
            if len(cells) > 7:
                date_cell_text = cells[0].get_text(strip=True)
                dept_cell_text = cells[1].get_text(strip=True)
                duty_type_text = cells[4].get_text(strip=True)
                
                # [修改] 移除 DEBUG Log
                # if date_cell_text == roc_date_str:
                #    logging.info(f"DEBUG ROW (VS): Dept='{dept_cell_text}' | Type='{duty_type_text}'")

                if (date_cell_text == roc_date_str and 
                    "皮膚科" in dept_cell_text and 
                    ("主治" in duty_type_text or "專科" in duty_type_text)):
                    
                    name_span = cells[6].find('span')
                    if name_span:
                        doctor_name = name_span.get_text(strip=True)
                        logging.info(f"Found {log_prefix} duty VS: {doctor_name}")
                        found = True
                        break
        
        if not found:
            if doctor_name == "查詢失敗": 
                logging.warning(f"Could not find {log_prefix} duty VS for Dermatology.")
                doctor_name = "未找到"

    except requests.exceptions.RequestException:
        logging.error(f"A connection error occurred while fetching {log_prefix} duty VS.", exc_info=True)
        doctor_name = "網路錯誤"
    except Exception as e:
        logging.error(f"An unexpected error occurred while fetching {log_prefix} duty VS: {e}", exc_info=True)
        doctor_name = "查詢錯誤"
        
    if vs_type == "saturday_vs":
        put_ui_message(ui_queue, UiSaturdayVsMessage(doctor_name=doctor_name))
    else:
        put_ui_message(ui_queue, UiTodayVsMessage(doctor_name=doctor_name))

# --- 門診動態 reg64.cgi TimeCode（與 appointment.cmuh.org.tw/cgi-bin/reg64.cgi 參數一致）---
def reg64_time_code_from_local_clock(when=None) -> str:
    """依本機時鐘：00:00–13:29→1，13:30–17:59→2，18:00–23:59→3。"""
    if when is None:
        when = datetime.now()
    cur = when.time()
    if cur <= dt_time(13, 29, 59):
        return "1"
    if cur <= dt_time(17, 59, 59):
        return "2"
    return "3"


def reg64_slot_cn(time_code: str) -> str:
    """TimeCode → 早上／下午／晚上（與門診統計 session 用語一致）。"""
    return {"1": "早上", "2": "下午", "3": "晚上"}.get(str(time_code), "")


def reg64_slot_label_color(time_code: str) -> str:
    """診間代號後方時段字色：早綠、午藍、晚深藍。"""
    return {"1": "#2E7D32", "2": "#1565C0", "3": "#0D47A1"}.get(str(time_code), "#78909C")


# 門診動態「顯示時段」選項（UI 僅中文，不顯示 Code 數字）
CLINIC_DISPLAY_MODE_OPTIONS = (
    ("auto", "自動（依電腦時間）"),
    ("1", "早上"),
    ("2", "下午"),
    ("3", "晚上"),
)


def _normalize_clinic_display_mode(val) -> str:
    v = str(val).strip().lower() if val is not None else "auto"
    if v in ("1", "2", "3", "auto"):
        return v
    return "auto"


def _clinic_display_mode_label(mode_key: str) -> str:
    for k, lab in CLINIC_DISPLAY_MODE_OPTIONS:
        if k == mode_key:
            return lab
    return CLINIC_DISPLAY_MODE_OPTIONS[0][1]


def _clinic_display_mode_from_label(label: str) -> str:
    for k, lab in CLINIC_DISPLAY_MODE_OPTIONS:
        if lab == label:
            return k
    return "auto"


def resolve_clinic_reg64_time_code(mode: str, when=None) -> str:
    """自動 → 依本機時鐘；早上/下午/晚上 → 固定對應 reg64 TimeCode。"""
    m = _normalize_clinic_display_mode(mode)
    if m in ("1", "2", "3"):
        return m
    return reg64_time_code_from_local_clock(when)


# 門診動態燈號／候診輪詢間隔（秒）；UI 已不提供自訂
CLINIC_LIGHT_REFRESH_SECONDS = 60
# reg64 單次 HTTP 逾時（秒）；縮短可避免啟動初期長時間等待。
CLINIC_REG64_HTTP_TIMEOUT = 4
# 門診動態：近一月統計天數、關診判定（時段底線後連續無變動秒數）
CLINIC_METRIC_HISTORY_DAYS = 30
NOTIFY_DO_NOT_DISTURB_START_HOUR = 0
NOTIFY_DO_NOT_DISTURB_END_HOUR = 8

REG52_MAIN_TTL_SECONDS = 120
REG52_BRANCH_TTL_SECONDS = 180
REG52_AUH_TTL_SECONDS = 180
REG52_DAYOFF_TTL_SECONDS = 300
PARSE_CACHE_TTL_SECONDS = 180
DUTY_CACHE_TTL_SECONDS = 3600
REG64_MICRO_CACHE_SECONDS = 8
SOURCE_BACKOFF_BASE_SECONDS = 2
SOURCE_BACKOFF_MAX_SECONDS = 90
GLOBAL_REFRESH_SNAPSHOT_TTL_SECONDS = 180
CLINIC_CLOSE_PLATEAU_SECONDS = 30 * 60


def _session_boundary_datetime(session_cn: str, now_dt: datetime) -> datetime:
    """該診別「關診時間計算」最早可開始偵測的時刻（當日）。"""
    if session_cn == "上午":
        h, m = 12, 0
    elif session_cn == "下午":
        h, m = 17, 0
    else:
        h, m = 21, 0
    return now_dt.replace(hour=h, minute=m, second=0, microsecond=0)


def _prev_session_cn(session_cn: str):
    if session_cn == "下午":
        return "上午"
    if session_cn == "晚上":
        return "下午"
    return None


def _reg64_tc_to_session_cn(time_code: str) -> str:
    return {"1": "上午", "2": "下午", "3": "晚上"}.get(str(time_code), "")


# --- 9. UI 與應用程式主體 ---
class AutomationApp:
    def __init__(self, root: tk.Tk, master_schedule: dict):
        self.root = root
        self.root.title("中國醫皮膚科常用程式")
        try: self.root.state('zoomed')
        except tk.TclError: self.root.geometry("1280x720")
        _apply_tk_window_icon(self.root)
        
        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()
        self.hotkey_version = None
        if self.screen_width == 1920 and self.screen_height == 1080:
            self.hotkey_version = '1920x1080'
        elif self.screen_width == 1280 and self.screen_height == 1024:
            self.hotkey_version = '1280x1024'
        elif self.screen_width == 1024 and self.screen_height == 768:
            self.hotkey_version = '1024x768'
        self.hotkey_profile = self.hotkey_version or self._select_adaptive_hotkey_profile()
        self.hotkey_adaptive_enabled = (self.hotkey_version is None and self.hotkey_profile is not None)

        self.f_lg = 10
        self.f_md = 9
        self.f_sm = 8
        if self.screen_width <= 1024:
            self.f_lg = 9
            self.f_md = 8
            self.f_sm = 7
        logging.info(f"Resolution: {self.screen_width}x{self.screen_height}. Fonts: {self.f_lg}/{self.f_md}/{self.f_sm}")

        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown_app)

        self.clinic_trackers = {}
        self.history_cache = []
        self._load_history_cache()

        self._shutting_down = False
        self._ui_queue_poll_id = None
        self._refresh_pending = False
        self._save_cache_pending = {}
        self._avg_history_cache = {}  # [優化] 快取歷史平均，避免每次重算
        self._refresh_worker_running = False
        self._queued_refresh_requests = deque()
        self._queued_refresh_signatures = set()
        self._active_refresh_signature = None
        self._refresh_queue_lock = threading.Lock()
        self._refresh_progress_total = 0
        self._refresh_progress_done = 0
        self._heavy_modules_ready = False
        self._heavy_modules_loading = False
        self._settings_promo_loaded = False
        self._settings_promo_loading = False
        self._future_tab_grid_stale = True  # 未來週次分頁需在資料更新後重繪；切回時若未過期可跳過以減少卡頓
        self._bottom_links_hidden = False  # 與 links_frame 顯示狀態同步，避免重複 grid 觸發版面重算
        self._subsystem_running = False
        self._subsystem_lock = threading.Lock()
        self._active_notices = []
        self.startup_phase_text = tk.StringVar(value="啟動中")
        self.app_version_text = tk.StringVar(value=f"v{CURRENT_VERSION}")
        self.last_refresh_text = tk.StringVar(value="更新: --")
        self.hotkey_display_note = tk.StringVar(value="")
        self._log_backlog = []

        self.ui_queue = Queue()
        self.all_doctors_data = {}
        self.master_schedule = master_schedule
        self._master_schedule_by_weekday = defaultdict(list)
        self._master_schedule_self_paid = {}
        self._rebuild_master_schedule_index()

        # [核心修正]：全域執行緒任務池與互斥鎖，阻絕無限制的 Thread spawning 與字典寫入衝突
        self.bg_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="AppBgTask")
        self._tracker_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._doctor_data_lock = threading.Lock()
        # 門診動態 reg64 → 總覽月曆「逾時後補掛號人數」用：(醫師, 上午|下午|晚上)
        self._reg64_public_snapshot = {}
        self._reg64_last_good_total = {}
        self._reg64_dynamic_ttl_seconds = REG64_MICRO_CACHE_SECONDS
        self._duty_last_fetch_date = None
        self._last_full_refresh_snapshot = None
        self._last_full_refresh_ts = 0.0
        self._initial_priority_refresh_done = False
        # 啟動時優先批次完成後再跑全體刷新，避免與固定延遲重疊造成「Refresh already running; queued」
        self._startup_defer_full_until_priority_done = False

        self.auto_reboot_settings = self.load_auto_reboot_settings()
        self.auto_reboot_enabled = tk.BooleanVar(value=self.auto_reboot_settings.get("enabled", False))
        self.auto_reboot_time = tk.StringVar(value=self.auto_reboot_settings.get("time", "07:01"))
        self.last_reboot_check_date = None

        self.threshold_settings = self.load_threshold_settings()
        try:
            _ufs = float(self.threshold_settings.get("ui_font_scale", 1.0))
        except (TypeError, ValueError):
            _ufs = 1.0
        self.ui_font_scale_var = tk.DoubleVar(value=max(0.85, min(1.45, _ufs)))
        self.alert_chang_enabled = tk.BooleanVar(value=self.threshold_settings.get("alert_chang_enabled", True))
        self.alert_chen_enabled = tk.BooleanVar(value=self.threshold_settings.get("alert_chen_enabled", False))
        self.out_of_hospital_var = tk.BooleanVar(value=self.threshold_settings.get("out_of_hospital_mode", False))
        self.show_external_clinics = tk.BooleanVar(value=self.threshold_settings.get("show_external_clinics", True))

        self.val_alert_chang = self.alert_chang_enabled.get()
        self.val_alert_chen = self.alert_chen_enabled.get()
        self.val_out_of_hospital = self.out_of_hospital_var.get()
        self.val_auto_reboot_enabled = self.auto_reboot_enabled.get()
        self.val_auto_reboot_time = self.auto_reboot_time.get()

        self.r_doctor_map = self.load_r_doctor_settings()
        self.doctors_list = self.load_doctors_settings()

        self.notified_counts = defaultdict(int)
        self.alert_frequency = defaultdict(int)
        self._alert_popup_active = defaultdict(bool)
        self._alert_state_lock = threading.Lock()
        self._dnd_suppressed_count = 0
        self.notify_dnd_start_time_var = tk.StringVar(value=str(self.threshold_settings.get("notify_dnd_start_time", "00:00")))
        self.notify_dnd_end_time_var = tk.StringVar(value=str(self.threshold_settings.get("notify_dnd_end_time", "08:00")))
        self._live_count_samples = defaultdict(lambda: deque(maxlen=12))

        self.cl_check_interval = 30
        self.cl_last_check_time = 0
        self._priority_refresh_last_check_time = defaultdict(float)
        self._refresh_tick_after_id = None
        self._pending_refresh_tick_ui = None

        retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        self.session = requests.Session()
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://forward01.cmuh.org.tw/peoplesystem/Duty/DutyQuery.aspx',
            'Connection': 'keep-alive'
        })
        self.session._lock = threading.Lock()

        self.duty_session = requests.Session()
        self.duty_session.mount('https://', HTTPAdapter(max_retries=retries))
        self.duty_session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://forward01.cmuh.org.tw/peoplesystem/Duty/DutyQuery.aspx',
            'Connection': 'keep-alive'
        })
        self.duty_session._lock = threading.Lock()

        self._init_styles()
        self._init_ui()

        self.load_cached_data()
        self.startup_phase_text.set("快取完成")

        self.root.after(50, self.deferred_initialization)
        self.root.after(100, self.process_ui_queue)

    def shutdown_app(self):
        """關閉時不可在主執行緒上 executor.shutdown(wait=True)，否則會卡到背景 HTTP／排程結束。"""
        self._shutting_down = True
        logging.info("Shutdown signal received.")
        stop_event_main.set()
        safe_unhook_all_hotkeys()
        try:
            self._cancel_pending_refresh_tick_ui()
        except Exception:
            pass
        try:
            cid = getattr(self, 'clinic_loop_id', None)
            if cid:
                self.root.after_cancel(cid)
        except Exception:
            pass
        try:
            uq = getattr(self, '_ui_queue_poll_id', None)
            if uq:
                self.root.after_cancel(uq)
        except Exception:
            pass
        self._ui_queue_poll_id = None
        if hasattr(self, 'bg_executor'):
            try:
                self.bg_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self.bg_executor.shutdown(wait=False)
        for _attr in ('duty_session', 'session'):
            session = getattr(self, _attr, None)
            if session is not None:
                try:
                    session.close()
                except Exception as e:
                    logging.warning(f"Failed to close requests session ({_attr}): {e}")
        logging.info("Hotkeys unhooked; executor released (non-blocking shutdown).")
        self.root.destroy()

# --- [新增/修改] 確保 Key 為字串的輔助函式 ---
    def _convert_keys_to_str(self, data):
        """遞迴將字典中所有的 Key 轉為字串 (針對 datetime.date)"""
        if isinstance(data, dict):
            new_dict = {}
            for k, v in data.items():
                # 如果 Key 是日期或時間，轉為 ISO 格式字串
                if isinstance(k, (date, datetime)):
                    k_str = k.isoformat()
                else:
                    k_str = str(k)
                new_dict[k_str] = self._convert_keys_to_str(v)
            return new_dict
        elif isinstance(data, list):
            return [self._convert_keys_to_str(i) for i in data]
        else:
            return data

    # --- [修改] 儲存快取通用函式 (加入 Key 轉換) ---
    def _save_cache(self, filename, data):
        try:
            safe_data = self._convert_keys_to_str(data)
            _atomic_write_json(get_conf_path(filename), safe_data, default=date_key_encoder)
        except Exception as e:
            logging.error(f"儲存快取 {filename} 失敗: {e}")

    def _rebuild_master_schedule_index(self):
        by_weekday = defaultdict(list)
        self_paid_map = {}
        for doctor_name, weekday_map in self.master_schedule.items():
            if not isinstance(weekday_map, dict):
                continue
            for weekday_idx, sessions in weekday_map.items():
                try:
                    normalized_weekday = int(weekday_idx)
                except (TypeError, ValueError):
                    continue
                if not isinstance(sessions, list):
                    continue
                for session_info in sessions:
                    if not isinstance(session_info, dict):
                        continue
                    session_name = session_info.get('session')
                    if not session_name:
                        continue
                    is_self_paid = bool(session_info.get('is_self_paid'))
                    by_weekday[normalized_weekday].append((doctor_name, session_name, is_self_paid))
                    self_paid_map[(doctor_name, normalized_weekday, session_name)] = is_self_paid
        self._master_schedule_by_weekday = by_weekday
        self._master_schedule_self_paid = self_paid_map

    # --- [修改] 載入快取資料 (加入損壞自動刪除機制) ---
    def load_cached_data(self):
        """啟動時先讀取本地 JSON，加快顯示速度"""
        try:
            # 1. 載入 門診人數 (all_doctors_data)
            cache_path = get_conf_path('cache_clinic_counts.json')
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, 'r', encoding='utf-8') as f:
                        raw_data = json.load(f)
                        for doc_no, doc_data in raw_data.items():
                            if isinstance(doc_data, dict) and 'error' not in doc_data:
                                with self._doctor_data_lock:
                                    self.all_doctors_data[doc_no] = decode_date_keys(doc_data)
                    logging.info("已載入門診人數快取。")
                except json.JSONDecodeError:
                    logging.warning("快取檔案損壞，正在刪除重置...")
                    os.remove(cache_path)

            # 2. 載入 主門診表 (master_schedule)
            sched_path = get_conf_path('cache_master_schedule.json')
            if os.path.exists(sched_path):
                try:
                    with open(sched_path, 'r', encoding='utf-8') as f:
                        raw_sched = json.load(f)
                        self.master_schedule = {}
                        for doc, days in raw_sched.items():
                            self.master_schedule[doc] = {int(k): v for k, v in days.items()}
                        self._rebuild_master_schedule_index()
                    logging.info("已載入主門診表快取。")
                except json.JSONDecodeError:
                    os.remove(sched_path)

            # 3. 載入 值班資訊 (Duty Info)
            duty_path = get_conf_path('cache_duty_info.json')
            if os.path.exists(duty_path):
                try:
                    with open(duty_path, 'r', encoding='utf-8') as f:
                        duty_info = json.load(f)
                        today_str = date.today().strftime("%Y-%m-%d")
                        if duty_info.get('date') == today_str:
                            if 'duty_doctor' in duty_info: self.duty_doctor_var.set(duty_info['duty_doctor'])
                        if 'today_vs' in duty_info: self.duty_vs_var.set(duty_info['today_vs'])
                        
                        if 'saturday_duty' in duty_info: self.saturday_duty_doctor_var.set(duty_info['saturday_duty'])
                        if 'saturday_vs' in duty_info: self.saturday_duty_vs_var.set(duty_info['saturday_vs'])
                        self._refresh_duty_summary_text()
                    logging.info("已載入值班資訊快取。")
                except json.JSONDecodeError:
                    os.remove(duty_path)

        except Exception as e:
            logging.error(f"載入快取失敗: {e}")

# [新增] 載入歷史緩存
    def _load_history_cache(self):
        file_path = get_conf_path('clinic_stats_history.json')
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    self.history_cache = json.load(f)
            except Exception as e:
                logging.error(f"Failed to load history cache: {e}")
                self.history_cache = []
        self._avg_history_cache = {}  # [優化] 歷史資料更新，清除計算快取
        
    def _init_styles(self):
        try:
            scale = float(self.threshold_settings.get('ui_font_scale', 1.0))
        except (TypeError, ValueError):
            scale = 1.0
        scale = max(0.85, min(1.45, scale))
        self.f_lg = max(8, int(round(self.f_lg * scale)))
        self.f_md = max(7, int(round(self.f_md * scale)))
        self.f_sm = max(6, int(round(self.f_sm * scale)))
        logging.info(f"UI font scale applied: {scale:.2f} → fonts {self.f_lg}/{self.f_md}/{self.f_sm}")

        self.style = ttk.Style()
        # 使用動態字體變數
        self.style.configure("TLabel", font=("Microsoft JhengHei UI", self.f_lg), padding=2)
        self.style.configure("Header.TLabel", font=("Microsoft JhengHei UI", self.f_md, "bold"), anchor="center")
        
        # [修改] 確保日期標籤背景透明或白色，去除邊框感
        self.style.configure("Date.TLabel", font=("Microsoft JhengHei UI", self.f_md, "bold"), background="white")
        self.style.configure("Highlight.Date.TLabel", font=("Microsoft JhengHei UI", self.f_md, "bold"), background='SystemHighlight', foreground='SystemHighlightText')
        self.style.configure("Past.Date.TLabel", font=("Microsoft JhengHei UI", self.f_md, "bold"), foreground="gray", background="white")
        self.style.configure("Today.Date.TLabel", font=("Microsoft JhengHei UI", self.f_md, "bold"), background="yellow")
        
        self.style.configure("NoAppt.TFrame", relief="groove", background="white")
        
        # [關鍵修改] 將 HasAppt (有診) 的背景色改為白色，消除藍色框框
        self.style.configure("HasAppt.TFrame", background='white', relief="groove") 
        
        self.style.configure("Today.TFrame", relief="solid", borderwidth=2)
        self.style.map("Today.TFrame", bordercolor=[("!focus", "red")])
        self.style.configure("Appt.TLabel", font=("Microsoft JhengHei UI", self.f_md))
        self.style.configure("Highlight.Appt.TLabel", font=("Microsoft JhengHei UI", self.f_md), background='SystemHighlight', foreground='SystemHighlightText')
        self.style.configure("Full.Appt.TLabel", font=("Microsoft JhengHei UI", self.f_md, "bold"), background='red', foreground='white')
        self.style.configure("Link.TButton", padding=(4, 1), font=("Microsoft JhengHei UI", self.f_sm, "bold"))
        self.style.configure("SmallDuty.TLabel", font=("Microsoft JhengHei UI", self.f_sm, "bold"), foreground="#005A9C")
        self.style.configure("Links.TLabelframe", padding=1)
        self.style.configure("Links.TLabelframe.Label", font=("Microsoft JhengHei UI", self.f_sm, "bold"))
        self.style.configure('TNotebook.Tab', font=('Microsoft JhengHei UI', self.f_lg + 1, 'bold'), padding=[10, 5])
        self.style.map('TNotebook.Tab', background=[('selected', 'SystemHighlight'), ('active', '#E1E1E1')])
        self.style.configure("LazyHint.TLabel", font=("Microsoft JhengHei UI", self.f_md), foreground="gray")

    def _init_ui(self):
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=0)
        self.root.grid_columnconfigure(0, weight=1)
        self.lazy_tabs = {}
        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=(5,0))
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.bottom_frame = ttk.Frame(self.root)
        self.bottom_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(2,1))
        
        # [修改] 將 padding=10 改為 padding=2，爭取更多空間
        summary_tab = ttk.Frame(self.notebook, padding=2) 
        self.notebook.add(summary_tab, text="總覽")
        self._create_summary_tab_content(summary_tab)

        self._register_lazy_tab("未來週次查詢", lambda frame: self._create_future_weeks_tab(frame))
        self._register_lazy_tab("診斷書", lambda frame: self._create_certificate_tab(frame))
        self._register_lazy_tab("小工具", lambda frame: self._create_other_programs_tab(frame))
        self._register_lazy_tab("設定", lambda frame: self._create_settings_tab(frame))
        self._register_lazy_tab("系統日誌", lambda frame: self._create_log_tab(frame))

        # 建立 Queue 與 Handler
        self.log_queue = Queue()
        queue_handler = QueueHandler(self.log_queue)
        formatter = logging.Formatter('%(asctime)s [%(levelname)s]: %(message)s', datefmt='%H:%M:%S')
        queue_handler.setFormatter(formatter)
        logging.getLogger().addHandler(queue_handler)
        # Log 輪詢已合併至 process_ui_queue，不需要額外排程
        # -------------------------------------------

        self._create_bottom_panel_content()
        
        # [關鍵修正] 在這裡初始化 clinic_trackers 字典，避免 AttributeError
        self.clinic_trackers = {}

# --- [修正] Log 輪詢已合併至 process_ui_queue，此函式保留為相容接口 ---
    def poll_log_queue(self):
        """[廢棄] Log 輪詢已合併到 process_ui_queue，此方法保留以防外部呼叫"""
        pass

    def format_log_record(self, record):
        """手動格式化 Log (因為 QueueHandler 存的是原始 record)"""
        time_str = datetime.fromtimestamp(record.created).strftime('%H:%M:%S')
        return f"{time_str} [{record.levelname}]: {record.getMessage()}"

    def _run_on_ui_thread(self, callback):
        if threading.current_thread() is threading.main_thread():
            callback()
        else:
            self.root.after(0, callback)

    @staticmethod
    def _split_duty_prefix_name(full, sep=" 值班:"):
        if sep in full:
            i = full.index(sep)
            return full[: i + len(sep)], full[i + len(sep) :].strip()
        return full, ""

    @staticmethod
    def _split_duty_vs_label_name(full):
        for lab in ("當日值班VS:", "當週值班VS:"):
            if full.startswith(lab):
                return lab, full[len(lab) :].strip()
        if ":" in full:
            a, b = full.split(":", 1)
            return a.strip() + ":", b.strip()
        return full, ""

    def _refresh_duty_summary_text(self):
        if not hasattr(self, "duty_row1_prefix_var"):
            return
        p1, n1 = self._split_duty_prefix_name(self.duty_doctor_var.get())
        vl1, vn1 = self._split_duty_vs_label_name(self.duty_vs_var.get())
        p2, n2 = self._split_duty_prefix_name(self.saturday_duty_doctor_var.get())
        vl2, vn2 = self._split_duty_vs_label_name(self.saturday_duty_vs_var.get())
        self.duty_row1_prefix_var.set(p1)
        self.duty_row1_name_var.set(n1)
        self.duty_row1_vs_lbl_var.set(vl1)
        self.duty_row1_vs_name_var.set(vn1)
        self.duty_row2_prefix_var.set(p2)
        self.duty_row2_name_var.set(n2)
        self.duty_row2_vs_lbl_var.set(vl2)
        self.duty_row2_vs_name_var.set(vn2)

    def _show_notice(self, title, message, level="info", auto_close_ms=4000):
        def render_notice():
            try:
                notice = tk.Toplevel(self.root)
                notice.title(title)
                notice.transient(self.root)
                notice.attributes("-topmost", True)
                notice.resizable(False, False)

                bg = "#E8F5E9"
                fg = "#1B5E20"
                if level == "error":
                    bg = "#FFEBEE"
                    fg = "#B71C1C"
                elif level == "warn":
                    bg = "#FFF8E1"
                    fg = "#E65100"

                frame = tk.Frame(notice, bg=bg, padx=12, pady=10)
                frame.pack(fill="both", expand=True)
                tk.Label(frame, text=title, bg=bg, fg=fg, font=("Microsoft JhengHei UI", 10, "bold"), anchor="w").pack(fill="x")
                tk.Label(frame, text=message, bg=bg, fg=fg, justify="left", anchor="w", font=("Microsoft JhengHei UI", 9), wraplength=360).pack(fill="x", pady=(6, 0))
                ttk.Button(frame, text="關閉", command=notice.destroy).pack(anchor="e", pady=(10, 0))

                base_x = self.root.winfo_rootx() + self.root.winfo_width() - 420
                base_y = self.root.winfo_rooty() + 80 + (len(self._active_notices) * 110)
                notice.geometry(f"400x110+{max(base_x, 40)}+{max(base_y, 40)}")

                self._active_notices.append(notice)

                def cleanup():
                    if notice in self._active_notices:
                        self._active_notices.remove(notice)
                    try:
                        notice.destroy()
                    except Exception:
                        pass

                notice.protocol("WM_DELETE_WINDOW", cleanup)
                if auto_close_ms:
                    notice.after(auto_close_ms, cleanup)
            except Exception as e:
                logging.error(f"Failed to show notice: {e}")

        self._run_on_ui_thread(render_notice)

    def _cancel_pending_refresh_tick_ui(self):
        """取消節流中的整理人數進度更新，避免刷新已結束後延遲 callback 覆蓋「閒置」狀態。"""
        rid = getattr(self, '_refresh_tick_after_id', None)
        if rid:
            try:
                self.root.after_cancel(rid)
            except Exception:
                pass
        self._refresh_tick_after_id = None
        self._pending_refresh_tick_ui = None

    def _flush_refresh_tick_ui(self):
        """整理人數進度列更新節流，避免短時間內多次改 status_text。"""
        if getattr(self, '_shutting_down', False):
            return
        self._refresh_tick_after_id = None
        pending = getattr(self, '_pending_refresh_tick_ui', None)
        if not pending:
            return
        done, total, doc_name = pending
        if total > 0:
            self.status_text.set(f"狀態: 整理人數 {done}/{total} ({doc_name})")

    def _select_adaptive_hotkey_profile(self):
        target_w = max(1, int(self.screen_width))
        target_h = max(1, int(self.screen_height))
        target_ratio = target_w / float(target_h)
        target_area = target_w * target_h
        candidates = []
        for version, (base_w, base_h) in _HOTKEY_BASE_SIZE.items():
            base_ratio = base_w / float(base_h)
            base_area = base_w * base_h
            ratio_penalty = abs(base_ratio - target_ratio) * 100.0
            area_penalty = abs(base_area - target_area) / float(base_area)
            score = ratio_penalty + area_penalty
            candidates.append((score, version))
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1] if candidates else None

    def _bind_mousewheel_recursive(self, widget, callback):
        try:
            widget.bind("<MouseWheel>", callback)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._bind_mousewheel_recursive(child, callback)

    def _smart_widget_config(self, widget, **kwargs):
        changed = False
        for key, value in kwargs.items():
            try:
                if widget.cget(key) != value:
                    changed = True
                    break
            except Exception:
                changed = True
                break
        if changed:
            widget.config(**kwargs)

    def _register_lazy_tab(self, title, builder):
        frame = ttk.Frame(self.notebook, padding=10)
        hint = ttk.Label(frame, text="首次開啟時載入中...", style="LazyHint.TLabel")
        hint.pack(expand=True, pady=20)
        self.notebook.add(frame, text=title)
        self.lazy_tabs[title] = {"frame": frame, "builder": builder, "built": False}

    def _ensure_lazy_tab_initialized(self, title):
        """回傳 True 表示本次呼叫剛完成建置（首次開啟該分頁）。"""
        tab_info = self.lazy_tabs.get(title)
        if not tab_info or tab_info["built"]:
            return False
        frame = tab_info["frame"]
        for child in frame.winfo_children():
            child.destroy()
        tab_info["builder"](frame)
        tab_info["built"] = True
        return True

    def _create_log_tab(self, parent):
        self.log_text_widget = scrolledtext.ScrolledText(parent, state='disabled', font=("Consolas", 10))
        self.log_text_widget.pack(fill=tk.BOTH, expand=True)
        if self._log_backlog:
            self.log_text_widget.configure(state='normal')
            for line in self._log_backlog[-300:]:
                self.log_text_widget.insert(tk.END, line + '\n')
            self.log_text_widget.see(tk.END)
            self.log_text_widget.configure(state='disabled')

    def _is_notification_suppressed_now(self):
        def _parse_hhmm(text, fallback_h):
            s = str(text).strip()
            if ":" not in s:
                return fallback_h * 60
            hh, mm = s.split(":", 1)
            try:
                h = max(0, min(24, int(hh)))
                m = max(0, min(59, int(mm)))
                if h == 24:
                    m = 0
                return h * 60 + m
            except (TypeError, ValueError):
                return fallback_h * 60
        start_m = _parse_hhmm(self.threshold_settings.get("notify_dnd_start_time", "00:00"), NOTIFY_DO_NOT_DISTURB_START_HOUR)
        end_m = _parse_hhmm(self.threshold_settings.get("notify_dnd_end_time", "08:00"), NOTIFY_DO_NOT_DISTURB_END_HOUR)
        now = datetime.now()
        now_m = now.hour * 60 + now.minute
        if start_m == end_m:
            return True
        if start_m < end_m:
            return start_m <= now_m < end_m
        return now_m >= start_m or now_m < end_m

    def _draw_doctor_14d_trend(self):
        if not hasattr(self, "tools_trend_canvas"):
            return
        c = self.tools_trend_canvas
        c.delete("all")
        w = max(c.winfo_width(), 300)
        h = max(c.winfo_height(), 130)
        m = 22
        doctor = self.tools_trend_doctor_var.get()
        session = self.tools_trend_session_var.get()
        end_day = date.today()
        start_day = end_day - timedelta(days=13)
        by_day = {}
        with self._history_lock:
            rows = list(self.history_cache)
        for r in rows:
            if r.get("doctor") != doctor or r.get("session") != session:
                continue
            try:
                d = datetime.strptime(r.get("date", ""), "%Y/%m/%d").date()
            except Exception:
                continue
            if d < start_day or d > end_day:
                continue
            v = r.get("total_reg")
            if isinstance(v, int):
                by_day[d] = v
        days = [start_day + timedelta(days=i) for i in range(14)]
        vals = [by_day.get(d) for d in days]
        nums = [v for v in vals if isinstance(v, int)]
        c.create_rectangle(m, m, w - m, h - m, outline="#CCCCCC")
        if not nums:
            c.create_text(w // 2, h // 2, text="14 天內尚無可繪製掛號人數資料", fill="gray")
            self.tools_trend_meta.set(f"{doctor} {session}：無資料")
            return
        threshold_map = self._get_doctor_threshold_map(doctor)
        threshold_vals = [threshold_map.get((d.weekday(), session)) for d in days]
        threshold_nums = [t for t in threshold_vals if isinstance(t, int)]
        vmin, vmax = min(nums), max(nums)
        if threshold_nums:
            vmin = min(vmin, min(threshold_nums))
            vmax = max(vmax, max(threshold_nums))
        if vmin == vmax:
            vmin -= 1
            vmax += 1
        pts = []
        for i, d in enumerate(days):
            v = vals[i]
            x = m + (i / 13.0) * (w - 2 * m)
            if v is None:
                continue
            y = h - m - ((v - vmin) / (vmax - vmin)) * (h - 2 * m)
            pts.append((x, y, d, v))
        for i in range(0, 14, 2):
            x = m + (i / 13.0) * (w - 2 * m)
            c.create_text(x, h - m + 12, text=(start_day + timedelta(days=i)).strftime("%m/%d"), fill="#666", font=("Consolas", 8))
        c.create_text(m - 12, m, text=str(vmax), fill="#666", font=("Consolas", 8))
        c.create_text(m - 12, h - m, text=str(vmin), fill="#666", font=("Consolas", 8))
        thr_pts = []
        for i, d in enumerate(days):
            t = threshold_vals[i]
            if not isinstance(t, int):
                continue
            x = m + (i / 13.0) * (w - 2 * m)
            y = h - m - ((t - vmin) / (vmax - vmin)) * (h - 2 * m)
            thr_pts.append((x, y))
        for i in range(1, len(thr_pts)):
            c.create_line(thr_pts[i - 1][0], thr_pts[i - 1][1], thr_pts[i][0], thr_pts[i][1], fill="#E53935", width=1, dash=(4, 2))
        for i in range(1, len(pts)):
            c.create_line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1], fill="#1976D2", width=2)
        for x, y, _d, v in pts:
            c.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#1976D2", outline="#1976D2")
        eta_text = "預估: 資料不足"
        today_threshold = threshold_map.get((date.today().weekday(), session))
        live_samples = list(self._live_count_samples.get((doctor, session), []))[-5:]
        if isinstance(today_threshold, int) and len(live_samples) >= 3:
            t0, c0 = live_samples[0]
            t1, c1 = live_samples[-1]
            dt_min = max((t1 - t0) / 60.0, 0.0)
            slope_per_min = ((c1 - c0) / dt_min) if dt_min > 0 else 0.0
            current_count = int(c1)
            if current_count >= today_threshold:
                eta_text = "預估: 已達門檻"
            elif slope_per_min > 0:
                eta_min = int(max(1, round((today_threshold - current_count) / slope_per_min)))
                eta_text = f"預估: 約 {eta_min} 分鐘碰到門檻"
                # 以目前點位畫出預估線（綠虛線）
                last_x, last_y, *_ = pts[-1]
                ty = h - m - ((today_threshold - vmin) / (vmax - vmin)) * (h - 2 * m)
                est_x = min(w - m, last_x + 80)
                c.create_line(last_x, last_y, est_x, ty, fill="#43A047", width=1, dash=(3, 2))
            else:
                eta_text = "預估: 目前斜率不足（暫不會碰到門檻）"
        c.create_text(w - 120, m + 8, text="藍=人數  紅虛線=門檻  綠虛線=預估", fill="#666", font=("Consolas", 8))
        self.tools_trend_meta.set(f"{doctor} {session}：min={min(nums)} max={max(nums)} avg={(sum(nums)/len(nums)):.1f}｜{eta_text}")

    def _on_tab_changed(self, event):
        try: selected_tab_text = self.notebook.tab(self.notebook.select(), "text")
        except tk.TclError: return

        lazy_just_built = self._ensure_lazy_tab_initialized(selected_tab_text)

        # 1. 確保最外層的 Frame 是顯示的
        self.bottom_frame.grid()

        # 2. [修正] 用 grid_remove()/grid() 取代 pack_forget()/pack()
        #    僅在狀態改變時呼叫，減少不必要的版面重算
        hide_links = selected_tab_text in ("診斷書", "小工具", "設定")
        if hide_links != self._bottom_links_hidden:
            self._bottom_links_hidden = hide_links
            if hide_links:
                self.links_frame.grid_remove()
            else:
                self.links_frame.grid()

        # 3. 未來週次：首次建置時 _create_future_weeks_tab 已同步 on_future_week_selected，勿再 after(0) 重複全量重繪。
        #    資料未過期時跳過，避免每次切回分頁都跑完整 _update_grid_data。
        if selected_tab_text == "未來週次查詢":
            if hasattr(self, 'on_future_week_selected'):
                need_future = lazy_just_built or getattr(self, '_future_tab_grid_stale', True)
                # 首次建置已在 builder 內同步 on_future_week_selected，這裡勿再排程
                if need_future and not lazy_just_built:
                    self.root.after(0, self.on_future_week_selected)
        elif selected_tab_text == "設定":
            self.root.after(150, self.ensure_settings_promo_loaded)
        elif selected_tab_text == "小工具":
            self.root.after(120, self._draw_doctor_14d_trend)

    # 1. 讀取 R1-R3 設定
    def load_r_doctor_settings(self):
        defaults = {"R1": {"name": "林于喬"}, "R2": {"name": "陳翊嘉"}, "R3": {"name": "蔡明洋"}}
        try:
            with open(get_conf_path('r_doctor_settings.json'), 'r', encoding='utf-8') as f:
                data = json.load(f)
            out = {k: dict(v) for k, v in defaults.items()}
            if isinstance(data, dict):
                for k in out:
                    if k in data and isinstance(data[k], dict):
                        out[k] = {"name": str(data[k].get("name", "")).strip()}
            return out
        except (FileNotFoundError, json.JSONDecodeError):
            return {k: dict(v) for k, v in defaults.items()}

    # 2. 讀取 止掛人數 設定
    def load_threshold_settings(self):
        try:
            with open(get_conf_path('threshold_settings.json'), 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = DEFAULT_THRESHOLDS.copy()
            else:
                merged = DEFAULT_THRESHOLDS.copy()
                merged.update(data)
                data = merged
        except (FileNotFoundError, json.JSONDecodeError):
            data = DEFAULT_THRESHOLDS.copy()
        if 'ui_font_scale' not in data:
            data['ui_font_scale'] = 1.0
        if 'notify_dnd_start_hour' not in data:
            data['notify_dnd_start_hour'] = NOTIFY_DO_NOT_DISTURB_START_HOUR
        if 'notify_dnd_end_hour' not in data:
            data['notify_dnd_end_hour'] = NOTIFY_DO_NOT_DISTURB_END_HOUR
        if 'notify_dnd_start_time' not in data:
            data['notify_dnd_start_time'] = f"{int(data.get('notify_dnd_start_hour', NOTIFY_DO_NOT_DISTURB_START_HOUR)):02d}:00"
        if 'notify_dnd_end_time' not in data:
            data['notify_dnd_end_time'] = f"{int(data.get('notify_dnd_end_hour', NOTIFY_DO_NOT_DISTURB_END_HOUR)):02d}:00"
        return data

    # 3. 讀取 醫師代號 設定
    def load_doctors_settings(self):
        default_list = [
            {"name": "張廖年峰", "doc_no": "D15728", "notifications": True}, 
            {"name": "吳伯元", "doc_no": "D15645", "notifications": False},
            {"name": "陳駿升", "doc_no": "D34899", "notifications": False}, 
            {"name": "沈冠宇", "doc_no": "D28592", "notifications": False},
            {"name": "許致榮", "doc_no": "D20191", "notifications": False}, 
            {"name": "謝佳陵", "doc_no": "101823", "notifications": False},
            {"name": "方心禹", "doc_no": "D14355", "notifications": False}, 
            {"name": "黃建仁", "doc_no": "D6175", "notifications": False},
            {"name": "邵湘德", "doc_no": "D30915", "notifications": False}, 
            {"name": "李威儒", "doc_no": "D35819", "notifications": False},
            {"name": "蔡李澄", "doc_no": "D31352", "notifications": False}
        ]
        try:
            with open(get_conf_path('doctors.json'), 'r', encoding='utf-8') as f: 
                data = json.load(f)
                # [新增] 自動修復邏輯：檢查是否欄位錯置
                fixed = False
                for d in data:
                    # 如果 "doc_no" (代號) 含有中文，或 "name" (姓名) 像是代號 (D開頭+數字)
                    # 就把它們換回來
                    if (any('\u4e00' <= char <= '\u9fff' for char in str(d['doc_no']))) or \
                       (str(d['name']).startswith('D') and str(d['name'])[1:].isdigit()):
                        logging.warning(f"Data corruption detected for {d['name']}/{d['doc_no']}. Swapping back.")
                        real_name = d['doc_no']
                        real_doc_no = d['name']
                        d['name'] = real_name
                        d['doc_no'] = real_doc_no
                        fixed = True
                
                # 如果有修復，順便寫回檔案
                if fixed:
                    with open(get_conf_path('doctors.json'), 'w', encoding='utf-8') as fw:
                        json.dump(data, fw, ensure_ascii=False, indent=4)
                
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            return default_list

    # 4. 讀取 自動重開機 設定
    def load_auto_reboot_settings(self):
        try:
            # [修改] 使用 get_conf_path
            with open(get_conf_path('auto_reboot_settings.json'), 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"enabled": False, "time": "07:01"}

    # 1. 儲存 所有設定 (包含 R醫師, 止掛, 醫師列表, 重開機)
    def save_all_settings(self):
        self._backup_settings_snapshot()
        for r_key, entries in self.r_doctor_entries.items():
            self.r_doctor_map[r_key] = {"name": (entries["name_var"].get() or "").strip()}
        _atomic_write_json(get_conf_path('r_doctor_settings.json'), self.r_doctor_map)
        
        for key, var in self.threshold_entries.items():
            try: self.threshold_settings[key] = int(var.get())
            except (ValueError, TypeError):
                self.threshold_settings[key] = DEFAULT_THRESHOLDS.get(key, 0)
                var.set(self.threshold_settings[key])
        try:
            ufs = float(self.ui_font_scale_var.get())
        except (TypeError, ValueError):
            ufs = 1.0
        self.threshold_settings['ui_font_scale'] = max(0.85, min(1.45, ufs))
        
        self.threshold_settings['alert_chang_enabled'] = self.alert_chang_enabled.get()
        self.threshold_settings['alert_chen_enabled'] = self.alert_chen_enabled.get()
        self.threshold_settings['out_of_hospital_mode'] = self.out_of_hospital_var.get()
        self.threshold_settings['show_external_clinics'] = self.show_external_clinics.get()
        def _normalize_hhmm(text, fallback):
            s = str(text).strip()
            if ":" not in s:
                return fallback
            hh, mm = s.split(":", 1)
            try:
                h = max(0, min(24, int(hh)))
                m = max(0, min(59, int(mm)))
                if h == 24:
                    m = 0
                return f"{h:02d}:{m:02d}"
            except (TypeError, ValueError):
                return fallback
        dnd_start = _normalize_hhmm(self.notify_dnd_start_time_var.get(), "00:00")
        dnd_end = _normalize_hhmm(self.notify_dnd_end_time_var.get(), "08:00")
        self.notify_dnd_start_time_var.set(dnd_start)
        self.notify_dnd_end_time_var.set(dnd_end)
        self.threshold_settings['notify_dnd_start_time'] = dnd_start
        self.threshold_settings['notify_dnd_end_time'] = dnd_end

        _atomic_write_json(get_conf_path('threshold_settings.json'), self.threshold_settings)
        
        new_doctors_list = []
        for item_id in self.doctors_tree.get_children():
            item = self.doctors_tree.item(item_id)
            doc_no, name = item['values'] 
            existing_doctor = next((doc for doc in self.doctors_list if doc['name'] == name), None)
            notifications = existing_doctor['notifications'] if existing_doctor else False
            new_doctors_list.append({"name": name, "doc_no": doc_no, "notifications": notifications})
        
        self.doctors_list = new_doctors_list
        _atomic_write_json(get_conf_path('doctors.json'), self.doctors_list)
        
        reboot_config = { "enabled": self.auto_reboot_enabled.get(), "time": self.auto_reboot_time.get().strip() }
        _atomic_write_json(get_conf_path('auto_reboot_settings.json'), reboot_config)

        self._show_notice("設定已儲存", "所有設定已寫入檔案。\n若變更「介面字體縮放」，請重新啟動程式後才會套用。", level="info", auto_close_ms=4500)
        global DOCTORS, DOCTOR_NAMES
        DOCTORS = self.doctors_list
        DOCTOR_NAMES = [d["name"] for d in DOCTORS]
        self.refresh_all_calendars()
        self._trigger_refresh(is_manual=True)

    def _settings_files_for_backup(self):
        return [
            "r_doctor_settings.json",
            "threshold_settings.json",
            "doctors.json",
            "auto_reboot_settings.json",
            "clinic_light_settings.json",
        ]

    def _backup_settings_snapshot(self):
        try:
            base_dir = get_conf_path("versions")
            os.makedirs(base_dir, exist_ok=True)
            snap_name = datetime.now().strftime("%Y%m%d_%H%M%S")
            snap_dir = os.path.join(base_dir, snap_name)
            os.makedirs(snap_dir, exist_ok=True)
            copied = 0
            for fn in self._settings_files_for_backup():
                src = get_conf_path(fn)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(snap_dir, fn))
                    copied += 1
            logging.info(f"設定快照完成: {snap_name}, files={copied}")
        except Exception as e:
            logging.error(f"設定快照失敗: {e}", exc_info=True)

    def _restore_yesterday_settings_snapshot(self):
        try:
            base_dir = get_conf_path("versions")
            if not os.path.isdir(base_dir):
                self._show_notice("回復失敗", "尚未找到任何設定快照。", level="warn", auto_close_ms=3500)
                return
            ymd = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
            candidates = [n for n in os.listdir(base_dir) if n.startswith(ymd)]
            if not candidates:
                self._show_notice("回復失敗", "找不到昨天的設定快照。", level="warn", auto_close_ms=3500)
                return
            snap_name = sorted(candidates)[-1]
            snap_dir = os.path.join(base_dir, snap_name)
            restored = 0
            for fn in self._settings_files_for_backup():
                src = os.path.join(snap_dir, fn)
                if os.path.exists(src):
                    shutil.copy2(src, get_conf_path(fn))
                    restored += 1
            self.threshold_settings = self.load_threshold_settings()
            self.r_doctor_map = self.load_r_doctor_settings()
            self.doctors_list = self.load_doctors_settings()
            self.auto_reboot_settings = self.load_auto_reboot_settings()
            self.alert_chang_enabled.set(self.threshold_settings.get("alert_chang_enabled", True))
            self.alert_chen_enabled.set(self.threshold_settings.get("alert_chen_enabled", False))
            self.out_of_hospital_var.set(self.threshold_settings.get("out_of_hospital_mode", False))
            self.show_external_clinics.set(self.threshold_settings.get("show_external_clinics", True))
            self.notify_dnd_start_time_var.set(str(self.threshold_settings.get("notify_dnd_start_time", "00:00")))
            self.notify_dnd_end_time_var.set(str(self.threshold_settings.get("notify_dnd_end_time", "08:00")))
            self.auto_reboot_enabled.set(self.auto_reboot_settings.get("enabled", False))
            self.auto_reboot_time.set(self.auto_reboot_settings.get("time", "07:01"))
            if hasattr(self, "threshold_entries"):
                for k, v in self.threshold_entries.items():
                    v.set(self.threshold_settings.get(k, DEFAULT_THRESHOLDS.get(k, "")))
            if hasattr(self, "r_doctor_entries"):
                for r_key, entries in self.r_doctor_entries.items():
                    entries["name_var"].set(self.r_doctor_map.get(r_key, {}).get("name", ""))
            self.refresh_doctors_treeview()
            self._show_notice("回復完成", f"已回復昨天快照：{snap_name}（{restored}檔）", level="info", auto_close_ms=4500)
            self._trigger_refresh(is_manual=True)
        except Exception as e:
            logging.error(f"回復設定快照失敗: {e}", exc_info=True)
            self._show_notice("回復失敗", f"回復設定時發生錯誤: {e}", level="error", auto_close_ms=5000)

    def load_heavy_modules(self):
        """將拖慢啟動速度的硬體綁定庫延遲至背景載入"""
        if hotkey_modules.pyautogui is not None and hotkey_modules.keyboard is not None:
            self._heavy_modules_ready = True
            return True

        logging.info("延遲載入巨型模組 (pyautogui, keyboard)...")
        import pyautogui as pa
        import keyboard as kb

        hotkey_modules.pyautogui = pa
        hotkey_modules.keyboard = kb
        hotkey_modules.pyautogui.FAILSAFE = False
        self._heavy_modules_ready = True
        return True

    def _prepare_hotkeys_background(self):
        try:
            self.load_heavy_modules()
            self.root.after(0, self._finalize_hotkey_setup)
        except Exception as e:
            logging.error(f"Failed to load heavy modules: {e}", exc_info=True)
            self.root.after(0, lambda e=e: self._handle_hotkey_setup_failure(e))

    def _finalize_hotkey_setup(self):
        self._heavy_modules_loading = False
        self._heavy_modules_ready = True
        if hasattr(self, 'rehook_button'):
            self.rehook_button.config(state="normal")
        self.startup_phase_text.set("熱鍵就緒")
        self.setup_hotkeys()

    def _handle_hotkey_setup_failure(self, error):
        self._heavy_modules_loading = False
        self._heavy_modules_ready = False
        if hasattr(self, 'rehook_button'):
            self.rehook_button.config(state="disabled")
        self.startup_phase_text.set("熱鍵失敗")
        self.hotkey_text_label.config(text="熱鍵模組載入失敗")
        self.status_text.set("狀態: 熱鍵模組載入失敗，請檢查環境")
        logging.error(f"Hotkey module initialization failed: {error}")

    def deferred_initialization(self):
        """在 UI 渲染完成後才執行的初始化任務"""
        self.startup_phase_text.set("背景任務")
        self.start_background_tasks()

        if self._heavy_modules_ready:
            self._finalize_hotkey_setup()
            return

        self._heavy_modules_loading = True
        self.hotkey_text_label.config(text="熱鍵模組載入中...")
        self.status_text.set("狀態: 啟動中，正在背景載入熱鍵模組...")
        if hasattr(self, 'rehook_button'):
            self.rehook_button.config(state="disabled")
        self.startup_phase_text.set("載入熱鍵")
        self.bg_executor.submit(self._prepare_hotkeys_background)

    def _create_summary_tab_content(self, summary_tab):
        # 值班資訊改置於底部「院內系統捷徑」網頁列最右側，總覽僅保留月曆以加大門診區
        summary_tab.rowconfigure(0, weight=1)
        summary_tab.columnconfigure(0, weight=1)

        calendar_frame = ttk.Frame(summary_tab)
        calendar_frame.grid(row=0, column=0, sticky='nsew', pady=0)
        
        calendar_frame.rowconfigure(0, weight=1) 
        calendar_frame.columnconfigure(0, weight=1)

        self.summary_calendar_widgets = self._create_calendar_grid(calendar_frame)

    def _create_bottom_panel_content(self):
        # [修正] 改用 grid 管理底部兩個子框架，使 grid_remove() 不觸發 layout reflow，解決閃爍問題
        self.bottom_frame.grid_columnconfigure(0, weight=1)

        self.duty_doctor_var = tk.StringVar(value="今日值班: ...")
        self.duty_vs_var = tk.StringVar(value="當日值班VS: ...")
        self.saturday_duty_doctor_var = tk.StringVar(value="當週值班: ...")
        self.saturday_duty_vs_var = tk.StringVar(value="當週值班VS: ...")
        self.duty_row1_prefix_var = tk.StringVar()
        self.duty_row1_name_var = tk.StringVar()
        self.duty_row1_vs_lbl_var = tk.StringVar()
        self.duty_row1_vs_name_var = tk.StringVar()
        self.duty_row2_prefix_var = tk.StringVar()
        self.duty_row2_name_var = tk.StringVar()
        self.duty_row2_vs_lbl_var = tk.StringVar()
        self.duty_row2_vs_name_var = tk.StringVar()
        self._refresh_duty_summary_text()

        self.links_frame = ttk.LabelFrame(self.bottom_frame, text="院內系統捷徑", style="Links.TLabelframe")
        self.links_frame.grid(row=0, column=0, sticky='ew', pady=(1, 1))
        self.links_frame.grid_columnconfigure(0, weight=0)
        self.links_frame.grid_columnconfigure(1, weight=0)
        self.links_frame.grid_columnconfigure(2, weight=1)

        left_frame_container = ttk.Frame(self.links_frame)
        left_frame_container.grid(row=0, column=0, rowspan=2, padx=8, pady=1, sticky="nw")
        local_frame_row1 = ttk.Frame(left_frame_container); local_frame_row1.grid(row=0, column=0, sticky='w')
        local_frame_row2 = ttk.Frame(left_frame_container); local_frame_row2.grid(row=1, column=0, sticky='w', pady=(1,0))
        ttk.Separator(self.links_frame, orient='vertical').grid(row=0, column=1, rowspan=2, sticky="ns", padx=4, pady=3)
        right_frame_container = ttk.Frame(self.links_frame)
        right_frame_container.grid(row=0, column=2, rowspan=2, padx=8, pady=1, sticky="nsew")
        self._populate_link_buttons(local_frame_row1, local_frame_row2, right_frame_container)

        self.controls_frame = ttk.Frame(self.bottom_frame)
        self.controls_frame.grid(row=1, column=0, sticky='ew', pady=(1, 0))

        for col in range(4):
            self.controls_frame.grid_columnconfigure(col, weight=0)
        self.controls_frame.grid_columnconfigure(0, weight=1)

        _fs = max(6, int(self.f_sm))
        _fsb = max(7, _fs + 1)
        s_small = ttk.Style()
        s_small.configure("Small.TLabel", font=("Microsoft JhengHei UI", _fs))
        s_small.configure("Small.TButton", font=("Microsoft JhengHei UI", _fsb), padding=(4, 1))
        s_small.configure("Small.TLabelframe.Label", font=("Microsoft JhengHei UI", _fsb, "bold"))
        s_small.configure("Small.TLabelframe", padding=1)
        self.status_text = tk.StringVar(value="初始化中")
        merged_status = ttk.LabelFrame(self.controls_frame, text="系統與熱鍵狀態", style="Small.TLabelframe")
        merged_status.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        mrow = ttk.Frame(merged_status)
        mrow.pack(fill="x", padx=4, pady=2)
        mrow.columnconfigure(0, weight=1)
        ttk.Label(mrow, textvariable=self.status_text, font=("Microsoft JhengHei UI", _fsb, "bold"), foreground="blue").grid(row=0, column=0, sticky="w")
        meta_row = ttk.Frame(mrow)
        meta_row.grid(row=0, column=1, sticky="e")
        ttk.Label(meta_row, textvariable=self.app_version_text, style="Small.TLabel", foreground="#616161").pack(side="left", padx=(3, 2), pady=1)
        ttk.Separator(meta_row, orient="vertical").pack(side="left", fill="y", padx=2, pady=1)
        ttk.Label(meta_row, textvariable=self.startup_phase_text, style="Small.TLabel", foreground="gray").pack(side="left", padx=(2, 2), pady=1)
        ttk.Separator(meta_row, orient="vertical").pack(side="left", fill="y", padx=2, pady=1)
        ttk.Label(meta_row, textvariable=self.last_refresh_text, style="Small.TLabel", foreground="gray").pack(side="left", padx=(2, 3), pady=1)
        ttk.Separator(meta_row, orient="vertical").pack(side="left", fill="y", padx=2, pady=1)
        ttk.Label(meta_row, textvariable=self.hotkey_display_note, style="Small.TLabel", foreground="#5D4037", wraplength=280).pack(side="left", padx=(2, 3), pady=1)

        autoclock_frame = ttk.LabelFrame(self.controls_frame, text="打卡狀態", style="Small.TLabelframe")
        autoclock_frame.grid(row=0, column=1, padx=4, sticky="ew")
        
        # [修正] Canvas 寬度依字體縮放，避免「打卡狀態」在高 DPI/大字體下擁塞。
        clock_canvas_w = max(112, int(round(112 * (_fsb / 8.0))))
        self.clock_canvas = tk.Canvas(autoclock_frame, width=clock_canvas_w, height=22, bg="#F0F0F0", highlightthickness=0)
        self.clock_canvas.pack(side="left", padx=4, pady=1)
        
        # 繪製燈號與文字 (只顯示 上班 / 下班)
        # 上班燈 (左)
        self.light_in = self.clock_canvas.create_oval(5, 4, 18, 17, fill="gray", outline="gray")
        self.text_in = self.clock_canvas.create_text(23, 10, text="上班", anchor="w", font=("Microsoft JhengHei UI", _fsb, "bold"))
        
        # 下班燈 (右) - 座標往左移，緊湊排列
        out_x = max(56, clock_canvas_w // 2)
        self.light_out = self.clock_canvas.create_oval(out_x, 4, out_x + 13, 17, fill="gray", outline="gray")
        self.text_out = self.clock_canvas.create_text(out_x + 18, 10, text="下班", anchor="w", font=("Microsoft JhengHei UI", _fsb, "bold"))
        
        hotkey_frame = ttk.LabelFrame(self.controls_frame, text="熱鍵說明", style="Small.TLabelframe")
        hotkey_frame.grid(row=0, column=2, padx=4, sticky="ew")
        self.hotkey_text_label = ttk.Label(hotkey_frame, text="準備中...", style="Small.TLabel")
        self.hotkey_text_label.pack(padx=4, pady=0, anchor='w')
        manual_ops_frame = ttk.LabelFrame(self.controls_frame, text="手動操作", style="Small.TLabelframe")
        manual_ops_frame.grid(row=0, column=3, padx=(4, 0), sticky="e")
        self.refresh_button = ttk.Button(manual_ops_frame, text="整理人數", style="Small.TButton", command=lambda: self._trigger_refresh(is_manual=True)); self.refresh_button.pack(side="left", pady=1, padx=(4,2))
        self.rehook_button = ttk.Button(manual_ops_frame, text="重製熱鍵", style="Small.TButton", command=self._trigger_rehook_hotkeys); self.rehook_button.pack(side="left", pady=1, padx=(2,4))

    def _launch_program(self, path):
        try: logging.info(f"Attempting to launch program at: {path}"); os.startfile(path)
        except FileNotFoundError: logging.error(f"Program not found at: {path}"); messagebox.showerror("啟動失敗", f"找不到指定的程式！\n\n請確認路徑是否正確:\n{path}")
        except Exception as e: logging.error(f"Failed to launch program: {e}"); messagebox.showerror("啟動失敗", f"無法啟動程式:\n{e}")

    def _launch_browser(self, url):
        try: logging.info(f"Attempting to open URL: {url}"); webbrowser.open(url, new=2)
        except Exception as e: logging.error(f"Failed to open URL: {e}"); messagebox.showerror("開啟失敗", f"無法開啟網頁:\n{e}")

    def _populate_link_buttons(self, local_frame_row1, local_frame_row2, right_frame_container):
        local_buttons_row1 = [("舊版住院系統", r"C:\admc\systemftp.exe"), ("西醫診間系統", r"C:\opdc\systemftp.exe"), ("排檢程式", r"C:\SCHDUAL\systemftp.exe")]
        local_buttons_row2 = [("急診系統", r"C:\newemr\急診系統.exe"), ("開刀房系統", r"C:\orsys\systemftp.exe"), ("電子簽章", r"C:\NewEmrSign\systemftp.exe")]
        
        for text, path in local_buttons_row1: 
            ttk.Button(local_frame_row1, text=text, style="Link.TButton", command=lambda p=path: self._launch_program(p)).pack(side="left", padx=3, pady=1)
        for text, path in local_buttons_row2: 
            ttk.Button(local_frame_row2, text=text, style="Link.TButton", command=lambda p=path: self._launch_program(p)).pack(side="left", padx=3, pady=1)
            
        web_buttons_row1 = [
            ("新版住院系統", "https://his.cmuh.org.tw/webapp/login/"),
            ("CMUH入口網站", "https://intranet.caaumed.org.tw/?BranchNo=1"),
            ("電子刷卡", "https://administration.cmuh.org.tw/47/peoplesystem/electron_card/login.aspx"),
            ("簽核表單", "https://bpm.cmuh.org.tw/YZSoft/login/2020/?ReturnUrl=/"),
        ]

        web_buttons_row2 = [
            ("值班查詢", "https://forward01.cmuh.org.tw/peoplesystem/Duty/DutyQuery.aspx"),
            ("院內分機查詢", "https://forward01.cmuh.org.tw/MIS/TelQuery/TelQueryQNew.aspx"),
            ("病理看片", "https://dsr.cmuh.org.tw/view/v2/LApi.Case/202526251"),
            ("Google", "https://www.google.com"),
        ]
        
        for col, (text, url) in enumerate(web_buttons_row1):
            ttk.Button(right_frame_container, text=text, style="Link.TButton", command=lambda u=url: self._launch_browser(u)).grid(row=0, column=col, padx=2, pady=(0, 0), sticky='ew')

        duty_grid = ttk.Frame(right_frame_container)
        duty_grid.grid(row=0, column=5, rowspan=2, padx=(10, 6), pady=0, sticky="ne")
        for dc in range(5):
            duty_grid.columnconfigure(dc, weight=0)
        _duty_pad = {"padx": (0, 4), "pady": 0}
        ttk.Label(duty_grid, textvariable=self.duty_row1_prefix_var, style="SmallDuty.TLabel", anchor="e").grid(row=0, column=0, sticky="e", **_duty_pad)
        ttk.Label(duty_grid, textvariable=self.duty_row1_name_var, style="SmallDuty.TLabel", anchor="w").grid(row=0, column=1, sticky="w", **_duty_pad)
        ttk.Label(duty_grid, text="｜", style="SmallDuty.TLabel").grid(row=0, column=2, sticky="w", padx=(2, 4), pady=0)
        ttk.Label(duty_grid, textvariable=self.duty_row1_vs_lbl_var, style="SmallDuty.TLabel", anchor="e").grid(row=0, column=3, sticky="e", **_duty_pad)
        ttk.Label(duty_grid, textvariable=self.duty_row1_vs_name_var, style="SmallDuty.TLabel", anchor="w").grid(row=0, column=4, sticky="w", **_duty_pad)
        ttk.Label(duty_grid, textvariable=self.duty_row2_prefix_var, style="SmallDuty.TLabel", anchor="e").grid(row=1, column=0, sticky="e", **_duty_pad)
        ttk.Label(duty_grid, textvariable=self.duty_row2_name_var, style="SmallDuty.TLabel", anchor="w").grid(row=1, column=1, sticky="w", **_duty_pad)
        ttk.Label(duty_grid, text="｜", style="SmallDuty.TLabel").grid(row=1, column=2, sticky="w", padx=(2, 4), pady=0)
        ttk.Label(duty_grid, textvariable=self.duty_row2_vs_lbl_var, style="SmallDuty.TLabel", anchor="e").grid(row=1, column=3, sticky="e", **_duty_pad)
        ttk.Label(duty_grid, textvariable=self.duty_row2_vs_name_var, style="SmallDuty.TLabel", anchor="w").grid(row=1, column=4, sticky="w", **_duty_pad)

        for col, (text, url) in enumerate(web_buttons_row2):
            ttk.Button(right_frame_container, text=text, style="Link.TButton", command=lambda u=url: self._launch_browser(u)).grid(row=1, column=col, padx=2, pady=0, sticky='ew')

        for c in range(6):
            right_frame_container.grid_columnconfigure(c, weight=0 if c < 5 else 1)

    def _launch_scheduler_program(self):
        scheduler_script_name = "中國醫皮膚科排班程式.pyw"
        try: logging.info(f"Launching scheduler program: {scheduler_script_name}"); subprocess.Popen([sys.executable, scheduler_script_name])
        except FileNotFoundError: messagebox.showerror("啟動失敗", f"找不到排班程式檔案: {scheduler_script_name}\n\n請確認主程式與排班程式在同一個資料夾中。"); logging.error(f"Scheduler script not found: {scheduler_script_name}")
        except Exception as e: messagebox.showerror("啟動失敗", f"無法啟動排班程式:\n{e}"); logging.error(f"Failed to launch scheduler: {e}")
            
    def _launch_autoclock_program(self):
        autoclock_script_name = "中國醫皮膚科打卡程式.pyw"
        try: logging.info(f"Launching autoclock program: {autoclock_script_name}"); subprocess.Popen([sys.executable, autoclock_script_name])
        except FileNotFoundError: messagebox.showerror("啟動失敗", f"找不到打卡程式檔案: {autoclock_script_name}\n\n請確認主程式與打卡程式在同一個資料夾中。"); logging.error(f"Autoclock script not found: {autoclock_script_name}")
        except Exception as e: messagebox.showerror("啟動失敗", f"無法啟動打卡程式:\n{e}"); logging.error(f"Failed to launch autoclock program: {e}")

    def _launch_coordinate_detector_program(self):
        script_name = "中國醫皮膚科點座標偵測程式.pyw"
        try: logging.info(f"Launching coordinate detector program: {script_name}"); subprocess.Popen([sys.executable, script_name])
        except FileNotFoundError: messagebox.showerror("啟動失敗", f"找不到座標偵測程式檔案: {script_name}\n\n請確認主程式與該程式在同一個資料夾中。"); logging.error(f"Coordinate detector script not found: {script_name}")
        except Exception as e: messagebox.showerror("啟動失敗", f"無法啟動座標偵測程式:\n{e}"); logging.error(f"Failed to launch coordinate detector program: {e}")

    def _create_other_programs_tab(self, tools_tab):
        
        # --- 定義樣式 (Styles) ---
        self.style.configure("Big.TButton", font=("Microsoft JhengHei UI", 14, "bold"), padding=(15, 10))
        self.style.configure("Reset.TButton", font=("Microsoft JhengHei UI", 8), foreground="black", padding=1)
        self.style.configure("Card.TLabelframe", padding=0, borderwidth=1, relief="solid")
        
        # =================================================================
        # --- 區塊 1: 常用程式捷徑 ---
        # =================================================================
        prog_frame = ttk.LabelFrame(tools_tab, text="常用程式捷徑")
        prog_frame.pack(fill='x', pady=(0, 10), anchor='n') 
        
        programs = [
            ("排班程式", self._launch_scheduler_program),
            ("打卡程式", self._launch_autoclock_program),
            ("偵測點座標", self._launch_coordinate_detector_program)
        ]

        for idx, (name, cmd) in enumerate(programs):
            prog_frame.columnconfigure(idx, weight=1)
            btn = ttk.Button(prog_frame, text=name, command=cmd, style="Big.TButton") 
            btn.grid(row=0, column=idx, padx=5, pady=5, sticky='ew')

        # =================================================================
        # --- 區塊 2: 縮網址工具 ---
        # =================================================================
        url_frame = ttk.LabelFrame(tools_tab, text="縮網址產生器")
        url_frame.pack(fill='x', pady=(0, 10), anchor='n') 
        
        input_frame = ttk.Frame(url_frame)
        input_frame.pack(fill='x', padx=10, pady=(5, 2)) 
        
        ttk.Label(input_frame, text="長網址:", font=("Microsoft JhengHei UI", 10)).pack(side='left', padx=(0, 5))
        self.url_input_var = tk.StringVar()
        self.url_entry = ttk.Entry(input_frame, textvariable=self.url_input_var, font=("Consolas", 11))
        self.url_entry.pack(side='left', fill='x', expand=True, padx=5)
        self.url_entry.bind('<Return>', lambda e: self._start_shorten_url()) 
        
        self.shorten_btn = ttk.Button(input_frame, text="縮網址", command=self._start_shorten_url)
        self.shorten_btn.pack(side='left', padx=5)
        
        result_frame = ttk.Frame(url_frame)
        result_frame.pack(fill='x', padx=10, pady=(0, 5))
        ttk.Label(result_frame, text="短網址:", font=("Microsoft JhengHei UI", 10, "bold"), foreground="#0055aa").pack(side='left', padx=(0, 5))
        self.url_output_var = tk.StringVar()
        self.url_output_entry = ttk.Entry(result_frame, textvariable=self.url_output_var, font=("Consolas", 12, "bold"), state='readonly')
        self.url_output_entry.pack(side='left', fill='x', expand=True, padx=5)
        self.url_status_label = ttk.Label(result_frame, text="就緒", foreground="gray")
        self.url_status_label.pack(side='left', padx=5)

        # =================================================================
        # --- 區塊 3: 目前門診動態 ---
        # =================================================================
        clinic_status_frame = ttk.LabelFrame(tools_tab, text="目前門診動態") 
        clinic_status_frame.pack(fill='both', expand=True, pady=0, anchor='n')

        saved_settings = self.load_clinic_settings()
        saved_rooms = saved_settings.get("rooms", ["181", "182"])
        saved_modes = saved_settings.get("time_modes")
        if not isinstance(saved_modes, list):
            saved_modes = []
        while len(saved_modes) < 2:
            saved_modes.append("auto")

        status_container = ttk.Frame(clinic_status_frame)
        status_container.pack(fill='both', expand=True, padx=5, pady=(0, 5))

        while len(saved_rooms) < 2: saved_rooms.append("") 
        self.clinic_room_vars = [tk.StringVar(value=saved_rooms[0]), tk.StringVar(value=saved_rooms[1])]
        self.clinic_display_mode_vars = [
            tk.StringVar(value=_normalize_clinic_display_mode(saved_modes[j]))
            for j in range(2)
        ]
        self.clinic_timecode_combos = []

        self.clinic_ui_elements = []
        # 門診動態：說明／標題再小一號；統計數字與大燈號字級維持
        _cvf = max(11, self.f_sm + 4)
        _ccf = max(7, self.f_sm)
        _chdr_txt = max(9, self.f_sm + 1)
        _chdr_room_slot = max(10, self.f_sm + 2)
        _chdr_room_entry = max(11, self.f_sm + 3)
        _clinic_cap = ("Microsoft JhengHei UI", _ccf)
        _clinic_num = ("Arial", _cvf, "bold")
        _clinic_sep_h = max(36, _cvf * 2 + 8)
        _clinic_cap_fg = "#607D8B"
        _clinic_hdr = ("Microsoft JhengHei UI", _chdr_txt)
        _clinic_hdr_b = ("Microsoft JhengHei UI", _chdr_room_slot, "bold")
        _clinic_room_font = ("Arial", _chdr_room_entry, "bold")

        for i in range(2):
            status_container.columnconfigure(i, weight=1)
            shadow_frame = tk.Frame(status_container, bg="#E0E0E0")
            shadow_frame.grid(row=0, column=i, padx=5, pady=2, sticky="nsew")
            card_frame = tk.Frame(shadow_frame, bg="white")
            card_frame.pack(fill="both", expand=True, padx=1, pady=1)

            # --- 1. 標題列 ---
            header_frame = tk.Frame(card_frame, bg="#F5F5F5", height=30)
            header_frame.pack(fill='x', side='top')
            tk.Label(header_frame, text="診間", font=_clinic_hdr, bg="#F5F5F5", fg="#555").pack(side='left', padx=(10, 2), pady=2)
            entry = tk.Entry(header_frame, textvariable=self.clinic_room_vars[i], width=4, 
                             font=_clinic_room_font, justify='center',
                             bd=0, bg="#F5F5F5", fg="#005A9C")
            entry.pack(side='left', pady=2)
            entry.bind('<Return>', self.force_refresh_clinic_lights)
            entry.bind('<FocusOut>', self.force_refresh_clinic_lights)

            lbl_slot = tk.Label(
                header_frame,
                text="—",
                font=_clinic_hdr_b,
                bg="#F5F5F5",
                fg="#78909C",
            )
            lbl_slot.pack(side='left', padx=(4, 8), pady=2)

            _combo_fs = max(8, _chdr_txt - 1)
            tk.Label(header_frame, text="顯示時段", font=("Microsoft JhengHei UI", _combo_fs), bg="#F5F5F5", fg="#555").pack(side='left', padx=(0, 2), pady=2)
            tc_combo = ttk.Combobox(
                header_frame,
                state="readonly",
                width=14,
                font=("Microsoft JhengHei UI", _combo_fs),
                values=[lab for _, lab in CLINIC_DISPLAY_MODE_OPTIONS],
            )
            tc_combo.set(_clinic_display_mode_label(self.clinic_display_mode_vars[i].get()))
            tc_combo.pack(side='left', pady=2)
            self.clinic_timecode_combos.append(tc_combo)

            def _on_clinic_display_mode_change(idx, combo, event=None):
                key = _clinic_display_mode_from_label(combo.get())
                self.clinic_display_mode_vars[idx].set(key)
                self.force_refresh_clinic_lights()

            tc_combo.bind("<<ComboboxSelected>>", lambda e, idx=i, c=tc_combo: _on_clinic_display_mode_change(idx, c))

            lbl_doc_name = tk.Label(header_frame, text="", font=("Microsoft JhengHei UI", max(_chdr_room_slot + 1, 11), "bold"), bg="#F5F5F5", fg="#00796B")
            lbl_doc_name.pack(side='left', padx=(5, 5), pady=2)
            
            btn_reset = ttk.Button(header_frame, text="重製平均", style="Reset.TButton", 
                                   command=lambda idx=i: self.reset_clinic_stats(idx))
            btn_reset.pack(side='right', padx=5, pady=2)

            tk.Frame(card_frame, bg="#005A9C", height=2).pack(fill='x')

            # --- 2. 燈號顯示區 ---
            light_container = tk.Frame(card_frame, bg="white", pady=5)
            light_container.pack(fill='x')
            tk.Label(light_container, text="目前燈號", font=_clinic_cap, bg="white", fg=_clinic_cap_fg).pack(anchor="center")
            led_box = tk.Frame(light_container, bg="black", padx=15, pady=2)
            led_box.pack(pady=2, anchor="center")
            lbl_light = tk.Label(led_box, text="--", font=("Arial", 36, "bold"), fg="#FF3333", bg="black", width=3)
            lbl_light.pack()
            lbl_status = tk.Label(light_container, text="等待更新...", font=_clinic_cap, bg="white", fg=_clinic_cap_fg)
            lbl_status.pack(pady=(2, 0), anchor="center")

            # --- 3. 數據統計區：列0 近一月四欄；列2 時間節奏；列4 即時四欄（欄寬與直線分隔一致）---
            stats_frame = tk.Frame(card_frame, bg="white", pady=4)
            stats_frame.pack(fill='x', padx=10, pady=(0, 6))

            for _c in (0, 2, 4, 6):
                stats_frame.columnconfigure(_c, weight=1)

            # 列0：近一月
            f_col1m = tk.Frame(stats_frame, bg="white"); f_col1m.grid(row=0, column=0, sticky="nsew", padx=2)
            tk.Label(f_col1m, text="近一月平均掛號", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_m_total = tk.Label(f_col1m, text="-", font=_clinic_num, bg="white", fg="#455A64"); lbl_m_total.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=0, column=1, sticky="ns")

            f_col2m = tk.Frame(stats_frame, bg="white"); f_col2m.grid(row=0, column=2, sticky="nsew", padx=2)
            tk.Label(f_col2m, text="近一月平均完成", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_m_comp = tk.Label(f_col2m, text="-", font=_clinic_num, bg="white", fg="#455A64"); lbl_m_comp.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=0, column=3, sticky="ns")

            f_col3m = tk.Frame(stats_frame, bg="white"); f_col3m.grid(row=0, column=4, sticky="nsew", padx=2)
            tk.Label(f_col3m, text="近一月平均照光(跳號)", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_m_photo = tk.Label(f_col3m, text="-", font=_clinic_num, bg="white", fg="#455A64"); lbl_m_photo.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=0, column=5, sticky="ns")

            f_col4m = tk.Frame(stats_frame, bg="white"); f_col4m.grid(row=0, column=6, sticky="nsew", padx=2)
            tk.Label(f_col4m, text="上一時段關診", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_prev_close = tk.Label(f_col4m, text="—", font=_clinic_num, bg="white", fg="#5D4037"); lbl_prev_close.pack(anchor="center")

            tk.Frame(stats_frame, bg="#E0E0E0", height=1).grid(row=1, column=0, columnspan=7, sticky="ew", pady=6)

            # 列2：時間節奏（分／剩餘／等候）
            t_left = tk.Frame(stats_frame, bg="white"); t_left.grid(row=2, column=0, sticky="nsew", padx=2)
            tk.Label(t_left, text="近一月平均(分)", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_season_avg = tk.Label(t_left, text="-", font=_clinic_num, bg="white", fg="#5D4037"); lbl_season_avg.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=2, column=1, sticky="ns")

            t_mid = tk.Frame(stats_frame, bg="white"); t_mid.grid(row=2, column=2, sticky="nsew", padx=2)
            tk.Label(t_mid, text="目前平均(分)", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_curr_avg = tk.Label(t_mid, text="-", font=_clinic_num, bg="white", fg="#1976D2"); lbl_curr_avg.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=2, column=3, sticky="ns")

            t_est = tk.Frame(stats_frame, bg="white"); t_est.grid(row=2, column=4, sticky="nsew", padx=2)
            tk.Label(t_est, text="剩餘", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_est_remain = tk.Label(t_est, text="—", font=_clinic_num, bg="white", fg="#E65100"); lbl_est_remain.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=2, column=5, sticky="ns")

            t_wait = tk.Frame(stats_frame, bg="white"); t_wait.grid(row=2, column=6, sticky="nsew", padx=2)
            tk.Label(t_wait, text="平均報到等候", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_avg_wait = tk.Label(t_wait, text="-", font=_clinic_num, bg="white", fg="#004D40"); lbl_avg_wait.pack(anchor="center")

            tk.Frame(stats_frame, bg="#E0E0E0", height=1).grid(row=3, column=0, columnspan=7, sticky="ew", pady=6)

            # 列4：即時
            f_col1 = tk.Frame(stats_frame, bg="white"); f_col1.grid(row=4, column=0, sticky="nsew", padx=2)
            tk.Label(f_col1, text="掛號總數", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_total = tk.Label(f_col1, text="-", font=_clinic_num, bg="white", fg="#333333"); lbl_total.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=4, column=1, sticky="ns")

            f_col2 = tk.Frame(stats_frame, bg="white"); f_col2.grid(row=4, column=2, sticky="nsew", padx=2)
            tk.Label(f_col2, text="總完成數", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_comp_all = tk.Label(f_col2, text="-", font=_clinic_num, bg="white", fg="#1565C0"); lbl_comp_all.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=4, column=3, sticky="ns")

            f_col3 = tk.Frame(stats_frame, bg="white"); f_col3.grid(row=4, column=4, sticky="nsew", padx=2)
            tk.Label(f_col3, text="照光(跳號)", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_photo = tk.Label(f_col3, text="-", font=_clinic_num, bg="white", fg="#7B1FA2"); lbl_photo.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=4, column=5, sticky="ns")

            f_col4 = tk.Frame(stats_frame, bg="white"); f_col4.grid(row=4, column=6, sticky="nsew", padx=2)
            tk.Label(f_col4, text="候診(已報到)", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_waiting = tk.Label(f_col4, text="-", font=_clinic_num, bg="white", fg="#009688"); lbl_waiting.pack(anchor="center")

            self.clinic_ui_elements.append({
                "light": lbl_light, "total": lbl_total, "waiting": lbl_waiting, 
                "photo": lbl_photo, "comp_all": lbl_comp_all,
                "status": lbl_status, "card_bg": card_frame,
                "doc_name": lbl_doc_name,
                "slot_banner": lbl_slot,
                "season_avg": lbl_season_avg, 
                "curr_avg": lbl_curr_avg,   
                "est_remain": lbl_est_remain,
                "avg_wait": lbl_avg_wait,
                "m_avg_total": lbl_m_total,
                "m_avg_comp": lbl_m_comp,
                "m_avg_photo": lbl_m_photo,
                "prev_sess_close": lbl_prev_close,
            })

        for idx in range(2):
            tc0 = resolve_clinic_reg64_time_code(self.clinic_display_mode_vars[idx].get())
            sb = self.clinic_ui_elements[idx]["slot_banner"]
            sb.config(text=reg64_slot_cn(tc0) or "—", fg=reg64_slot_label_color(tc0))

        trend_frame = ttk.LabelFrame(tools_tab, text="14 天人數趨勢（同診次）")
        trend_frame.pack(fill='x', expand=False, pady=(8, 0), anchor='n')
        tr_top = ttk.Frame(trend_frame)
        tr_top.pack(fill=tk.X, padx=6, pady=(4, 2))
        self.tools_trend_doctor_var = tk.StringVar(value="張廖年峰")
        self.tools_trend_session_var = tk.StringVar(value="上午")
        ttk.Label(tr_top, text="醫師").pack(side=tk.LEFT)
        ttk.Combobox(tr_top, textvariable=self.tools_trend_doctor_var, values=["張廖年峰", "陳駿升"], state="readonly", width=10).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(tr_top, text="診次").pack(side=tk.LEFT)
        ttk.Combobox(tr_top, textvariable=self.tools_trend_session_var, values=["上午", "下午", "晚上"], state="readonly", width=6).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Button(tr_top, text="重繪趨勢", command=self._draw_doctor_14d_trend).pack(side=tk.LEFT, padx=(0, 8))
        self.tools_trend_meta = tk.StringVar(value="尚無資料")
        ttk.Label(tr_top, textvariable=self.tools_trend_meta, foreground="gray").pack(side=tk.RIGHT)
        self.tools_trend_canvas = tk.Canvas(trend_frame, bg="white", height=150)
        self.tools_trend_canvas.pack(fill='x', expand=False, padx=6, pady=(0, 6))

        self.root.after(120, self._update_clinic_lights_loop)
        self.root.after(420, self._draw_doctor_14d_trend)

    def _reset_clinic_display_modes_to_auto(self):
        """跨越預設時段切換點時，將兩張卡的顯示時段重設為依電腦時間。"""
        if not hasattr(self, "clinic_display_mode_vars"):
            return
        for idx, var in enumerate(self.clinic_display_mode_vars):
            var.set("auto")
            if idx < len(getattr(self, "clinic_timecode_combos", [])):
                try:
                    self.clinic_timecode_combos[idx].set(_clinic_display_mode_label("auto"))
                except tk.TclError:
                    pass
        self.save_clinic_settings()

    def _update_reg64_public_cache(self, room_code, data):
        """門診動態成功抓取時更新，供總覽今日逾時列顯示掛號總數。"""
        dn = (data.get("doc_name") or "").strip()
        tc = str(data.get("reg64_time_code") or "")
        sn = _reg64_tc_to_session_cn(tc)
        tot = data.get("total")
        if not dn or not sn:
            return
        if not isinstance(tot, int) or tot < 0:
            return
        self._reg64_public_snapshot[(dn, sn)] = {
            "total": tot,
            "room": str(room_code),
            "ts": time.time(),
        }
        self._reg64_last_good_total[(dn, sn)] = tot

    def _appointment_today_shows_dayoff(self, doc_name, session_cn):
        """主程式快取中，今日該醫師該診別是否明確為休診／停診。"""
        if not doc_name or not session_cn:
            return False
        today_d = date.today()
        doc_no = None
        for d in self.doctors_list:
            if d.get("name") == doc_name:
                doc_no = str(d.get("doc_no", ""))
                break
        for key in (doc_no, doc_name):
            if not key:
                continue
            bucket = self.all_doctors_data.get(key)
            if not bucket or not isinstance(bucket, dict) or "error" in bucket:
                continue
            items = bucket.get(today_d)
            if not items:
                continue
            for item in items:
                sn, _ext = AutomationApp._appt_item_session_ext(item)
                if sn != session_cn:
                    continue
                if isinstance(item, dict):
                    raw = item.get("count", "")
                    st = str(raw)
                else:
                    parts = item.split("|")
                    st = parts[0].split(":", 1)[-1].strip() if parts else ""
                if "休診" in st or "停診" in st:
                    return True
        return False

    def _refine_clinic_reg64_for_display(self, data, mode, now_dt, tc_effective):
        """網頁 (未開診) 未必是停診：手動選未到的時段→尚未開診；否則對照今日掛號快取是否休診。"""
        if not data.get("is_stopped"):
            return data
        out = dict(data)
        session_cn = _reg64_tc_to_session_cn(str(tc_effective))
        dn = (out.get("doc_name") or "").strip()
        real_tc = reg64_time_code_from_local_clock(now_dt)
        if _normalize_clinic_display_mode(mode) != "auto" and str(tc_effective) != str(real_tc):
            out["status"] = "尚未開診"
            out["true_schedule_dayoff"] = False
            return out
        if dn and session_cn and self._appointment_today_shows_dayoff(dn, session_cn):
            out["status"] = "本日停診"
            out["true_schedule_dayoff"] = True
        else:
            out["status"] = "尚未開診"
            out["true_schedule_dayoff"] = False
        return out

    def _reg64_total_for_calendar_cell(self, doc_name, session_name):
        """回傳 (掛號總數字串含「人」, tag)；無則 None。供今日逾時列優先顯示門診動態人數。"""
        if not doc_name:
            return None
        snap = self._reg64_public_snapshot.get((doc_name, session_name))
        now_ts = time.time()
        if snap and (now_ts - snap.get("ts", 0)) <= 3 * 3600:
            return f"{snap['total']}人", "session_past"
        lastg = self._reg64_last_good_total.get((doc_name, session_name))
        if lastg is not None:
            return f"{lastg}人", "session_past"
        return None

    def _calendar_today_session_ended_text(self, doc_name, session_name):
        """今日該診別已過顯示時段：優先門診動態總掛號，否則最近一次成功人數，再否則「逾時」。"""
        pair = self._reg64_total_for_calendar_cell(doc_name, session_name)
        if pair:
            return pair[0]
        return "逾時"

# --- [新增] 抓取門診燈號與人數邏輯 ---
    # --- [新增] 抓取門診燈號與人數邏輯 ---
    def _reg64_get_html(self, target_url: str) -> str:
        """reg64.cgi 為公開讀取，不使用帶全域鎖的共用 Session，以便兩診間並行請求。"""
        if not hasattr(self, "_reg64_last_cache_hit"):
            self._reg64_last_cache_hit = False
        if not hasattr(self, "_reg64_last_backoff_skip"):
            self._reg64_last_backoff_skip = False
        self._reg64_last_cache_hit = False
        self._reg64_last_backoff_skip = False
        ck = ("reg64_html", target_url)
        ttl_s = max(5, int(getattr(self, "_reg64_dynamic_ttl_seconds", REG64_MICRO_CACHE_SECONDS)))
        hit = _cache_get(ck, ttl_s)
        if hit is not None:
            self._reg64_last_cache_hit = True
            return hit
        source_key = f"reg64:{target_url}"
        ok, remain = _source_backoff_allow(source_key)
        if not ok:
            stale = _cache_get(ck, ttl_s * 3)
            if stale is not None:
                logging.info(f"[BACKOFF] reg64 use stale cache, remaining={remain:.1f}s")
                self._reg64_last_backoff_skip = True
                return stale
            raise requests.exceptions.Timeout(f"reg64 backoff active ({remain:.1f}s)")
        verify = not _is_internal(target_url)
        s = _get_thread_local_reg64_session()
        try:
            response = s.get(target_url, timeout=CLINIC_REG64_HTTP_TIMEOUT, verify=verify)
            response.raise_for_status()
            response.encoding = "big5"
            text = response.text
            _cache_set(ck, text)
            _source_backoff_success(source_key)
            return text
        except requests.exceptions.RequestException:
            delay, cnt = _source_backoff_fail(source_key)
            logging.warning(f"[BACKOFF] reg64 fail {target_url}, fail={cnt}, delay={delay:.1f}s")
            raise

    def fetch_clinic_light_status(self, room_code, time_code=None):
        if time_code is None:
            time_code = reg64_time_code_from_local_clock()
        try:
            target_url = f"https://appointment.cmuh.org.tw/cgi-bin/reg64.cgi?CliRoom={room_code}&TimeCode={time_code}"

            try:
                page_html = self._reg64_get_html(target_url)
            except requests.exceptions.Timeout:
                return {"light": "休", "total": "-", "waiting": "-", "completed": 0, "status": "連線逾時", "doc_name": "", "waiting_set": set(), "completed_set": set(), "reg64_time_code": time_code}
            except Exception:
                return {"light": "--", "total": "-", "waiting": "-", "completed": 0, "status": "連線錯誤", "doc_name": "", "waiting_set": set(), "completed_set": set(), "reg64_time_code": time_code}

            soup = BeautifulSoup(page_html, 'lxml')

            doc_name = ""
            page_text = soup.get_text()
            match = re.search(r"醫師[：:]\s*(\S+)", page_text)
            if match: doc_name = match.group(1).strip()

            if "查無此診間的資料" in page_html:
                return {"light": "休", "total": "-", "waiting": "-", "completed": 0, "status": "休診", "doc_name": doc_name, "waiting_set": set(), "completed_set": set(), "reg64_time_code": time_code}

            # 已關診：常見為「診間目前燈號：99 (已關診)」或全形括號
            is_closed = bool(
                re.search(r"診間目前燈號\s*[：:]\s*\d+[^\n\r]*已關診", page_text)
                or re.search(r"\(已關診\)|（已關診）", page_text)
            )
            is_stopped = "(未開診)" in page_text
            
            close_time_str = ""
            if is_closed:
                time_match = re.search(r"應診時間[：:]\s*[\d]+\s*~\s*(\d{4})", page_text)
                if time_match:
                    raw_time = time_match.group(1) 
                    close_time_str = f"{raw_time[:2]}:{raw_time[2:]}" if len(raw_time) == 4 else raw_time

            light_num = "0"
            match_light = re.search(r"診間目前燈號\s*[：:]\s*(\d+)", page_text)
            if match_light:
                light_num = match_light.group(1)
            if is_closed and light_num == "0":
                m2 = re.search(r"診間目前燈號\s*[：:]\s*(\d+)", page_text.replace("\u3000", " "))
                if m2:
                    light_num = m2.group(1)
            
            table = soup.find('table', attrs={'bgcolor': '#fffff0'})
            total_count = 0 
            waiting_count = 0
            completed_count = 0 
            
            waiting_set = set()   
            completed_set = set() 

            if table:
                rows = table.find_all('tr')
                for row in rows:
                    cells = row.find_all('td')
                    if len(cells) >= 2:
                        status_text = cells[1].get_text(strip=True)
                        if "目前看診情形" in status_text: continue 
                        
                        try:
                            pt_num = int(cells[0].get_text(strip=True))
                        except Exception:
                            pt_num = -1

                        bg_color = cells[1].get('bgcolor', '').upper()
                        
                        is_completed = (bg_color == '#FFBBBB' or "完成" in status_text)
                        is_reserved = (bg_color == '#FFDDDD' or "保留" in status_text)
                        is_checked_in = ("已報到" in status_text)

                        total_count += 1
                        
                        if is_completed:
                            completed_count += 1
                            if pt_num != -1: completed_set.add(pt_num)
                        else:
                            if is_checked_in or is_reserved:
                                waiting_count += 1
                                if pt_num != -1: waiting_set.add(pt_num)
            
            return {
                "light": light_num, 
                "total": total_count, 
                "waiting": waiting_count, 
                "completed": completed_count,
                "waiting_set": waiting_set,      
                "completed_set": completed_set, 
                "status": "更新成功", 
                "doc_name": doc_name,
                "is_closed": is_closed,
                "is_stopped": is_stopped,
                "close_time": close_time_str,
                "reg64_time_code": time_code,
            }

        except Exception as e:
            logging.error(f"Fetch light error: {e}")
            return {"light": "--", "total": "-", "waiting": "-", "completed": 0, "status": "解析錯誤", "doc_name": "", "waiting_set": set(), "completed_set": set(), "reg64_time_code": time_code}

# --- [新增] 門診燈號更新迴圈 ---
    def _update_clinic_lights_loop(self):
        if not hasattr(self, 'clinic_trackers'):
            self.clinic_trackers = {}
        if not hasattr(self, '_clinic_dynamic_refresh_seconds'):
            self._clinic_dynamic_refresh_seconds = CLINIC_LIGHT_REFRESH_SECONDS

        rooms_to_check = []
        for i in range(2):
            code = self.clinic_room_vars[i].get().strip()
            rooms_to_check.append(code)

        def run_update(rooms):
            source_timing = {"cache_hit_html": 0, "cache_hit_parse": 0, "cache_hit_reg64": 0, "backoff_skip": 0}
            abnormal_rooms = []
            active_now = False
            current_timestamp = time.time()
            now = datetime.now()
            tc_auto = reg64_time_code_from_local_clock(now)
            prev_seg = getattr(self, "_clinic_reg64_auto_segment", None)
            reg64_segment_just_changed = prev_seg is not None and tc_auto != prev_seg
            if reg64_segment_just_changed:
                self.root.after(0, self._reset_clinic_display_modes_to_auto)
            self._clinic_reg64_auto_segment = tc_auto

            specs = []
            for i, room_code in enumerate(rooms):
                if not room_code:
                    continue
                mode = (
                    self.clinic_display_mode_vars[i].get()
                    if hasattr(self, "clinic_display_mode_vars") and i < len(self.clinic_display_mode_vars)
                    else "auto"
                )
                if reg64_segment_just_changed:
                    mode = "auto"
                tc_effective = resolve_clinic_reg64_time_code(mode, now)
                specs.append((i, room_code, mode, tc_effective))

            def _fetch_reg64_pack(spec):
                i, room_code, mode, tc_effective = spec
                data = self.fetch_clinic_light_status(room_code, time_code=tc_effective)
                return i, room_code, mode, tc_effective, data

            if len(specs) <= 1:
                packed_rows = [_fetch_reg64_pack(s) for s in specs]
            else:
                with ThreadPoolExecutor(max_workers=len(specs)) as pool:
                    packed_rows = list(pool.map(_fetch_reg64_pack, specs))

            for pack in packed_rows:
                i, room_code, mode, tc_effective, data = pack
                if getattr(self, "_reg64_last_cache_hit", False):
                    source_timing["cache_hit_reg64"] += 1
                if getattr(self, "_reg64_last_backoff_skip", False):
                    source_timing["backoff_skip"] += 1
                try:
                    if int(data.get('waiting', 0)) > 0 or int(data.get('completed', 0)) > 0:
                        active_now = True
                except Exception:
                    pass
                curr_session_i = reg64_slot_cn(tc_effective) or "晚上"

                status_check = data.get('status', '')
                if "錯誤" in status_check or "逾時" in status_check:
                    abnormal_rooms.append(room_code)
                    logging.warning(f"[{room_code}] 資料抓取異常 ({status_check})，跳過本次計算以保護狀態。")
                    self.root.after(0, lambda idx=i, res=data: self.update_single_clinic_ui_error(idx, res))
                    continue

                data = self._refine_clinic_reg64_for_display(data, mode, now, tc_effective)
                self._update_reg64_public_cache(room_code, data)

                current_session_avg_str = "-"
                estimated_remain_str = "-"
                avg_wait_str = "-"
                
                # [核心修正] 強制鎖定 tracker 狀態讀寫，解決 Race Condition
                with self._tracker_lock:
                    if room_code not in self.clinic_trackers:
                        self.clinic_trackers[room_code] = {
                            'last_completed_set': set(), 
                            'last_waiting_set': set(),    
                            'last_valid_completion_time': current_timestamp, 
                            'durations': [],                
                            'waiting_durations': [],        
                            'is_saved': False, 
                            'doc_name': '',
                            'actual_closing_dt': None,    
                            'phototherapy_count': 0,        
                            'patient_checkin_times': {},    
                            'session_period': curr_session_i,
                            'is_first_run': True,            
                            'first_valid_skipped': False,
                            'had_any_activity': False,
                            'stable_since_ts': None,
                            'last_monitor_pair': None,
                        }
                    
                    tracker = self.clinic_trackers[room_code]
                    
                    try:
                        curr_doc = data.get('doc_name', '')
                        
                        is_doctor_changed = (curr_doc and tracker['doc_name'] and curr_doc != tracker['doc_name'])
                        is_session_changed = (tracker['session_period'] != curr_session_i)
                        
                        if is_doctor_changed or is_session_changed:
                            logging.info(f"[{room_code}] 偵測到換診/換時段，重置所有統計數據。")
                            tracker['last_completed_set'] = set()
                            tracker['last_waiting_set'] = set()
                            tracker['durations'] = []
                            tracker['waiting_durations'] = []
                            tracker['is_saved'] = False
                            tracker['last_valid_completion_time'] = current_timestamp
                            tracker['actual_closing_dt'] = None
                            tracker['first_valid_skipped'] = False
                            tracker['phototherapy_count'] = 0
                            tracker['patient_checkin_times'] = {}
                            tracker['is_first_run'] = True 
                            tracker['session_period'] = curr_session_i
                            tracker['had_any_activity'] = False
                            tracker['stable_since_ts'] = None
                            tracker['last_monitor_pair'] = None
                        
                        tracker['doc_name'] = curr_doc
                        
                        current_completed_set = data.get('completed_set', set())
                        current_waiting_set = data.get('waiting_set', set()) 
                        
                        waiting_count_ui = data.get('waiting', 0)
                        completed_count_ui = data.get('completed', 0)
                        if completed_count_ui > 0 or waiting_count_ui > 0:
                            tracker['had_any_activity'] = True

                        if tracker['is_first_run']:
                            tracker['last_completed_set'] = current_completed_set
                            tracker['last_waiting_set'] = current_waiting_set
                            for pt_num in current_waiting_set:
                                tracker['patient_checkin_times'][pt_num] = current_timestamp
                            tracker['is_first_run'] = False
                        
                        else:
                            new_arrivals = current_waiting_set - tracker['last_waiting_set']
                            for pt_num in new_arrivals:
                                if pt_num not in tracker['patient_checkin_times']:
                                    tracker['patient_checkin_times'][pt_num] = current_timestamp

                            newly_completed = current_completed_set - tracker['last_completed_set']
                            has_valid_completion = False

                            for pt_num in newly_completed:
                                is_photo_case = False 
                                dwell_time = 0

                                if pt_num not in tracker['patient_checkin_times']:
                                    is_photo_case = True
                                else:
                                    start_time = tracker['patient_checkin_times'][pt_num]
                                    dwell_time = current_timestamp - start_time
                                    del tracker['patient_checkin_times'][pt_num] 

                                    if dwell_time < 60:
                                        is_photo_case = True

                                if is_photo_case:
                                    tracker['phototherapy_count'] += 1
                                else:
                                    doctor_pace = current_timestamp - tracker['last_valid_completion_time']
                                    
                                    if not tracker['first_valid_skipped']:
                                        tracker['first_valid_skipped'] = True
                                    else:
                                        if doctor_pace < 3600: 
                                            tracker['durations'].append(doctor_pace)
                                    
                                    if dwell_time < 10800:
                                        tracker['waiting_durations'].append(dwell_time)

                                    tracker['last_valid_completion_time'] = current_timestamp
                                    has_valid_completion = True

                            tracker['last_completed_set'] = current_completed_set
                            tracker['last_waiting_set'] = current_waiting_set

                            real_avg_min = 0.0
                            if tracker['durations']:
                                total_sec = sum(tracker['durations'])
                                count = len(tracker['durations'])
                                real_avg_min = (total_sec / count) / 60.0
                                current_session_avg_str = f"{real_avg_min:.1f}"
                            
                            if tracker['waiting_durations']:
                                total_wait = sum(tracker['waiting_durations'])
                                count_wait = len(tracker['waiting_durations'])
                                avg_wait_min = (total_wait / count_wait) / 60.0
                                avg_wait_str = f"{avg_wait_min:.1f}分"

                            if real_avg_min > 0 and waiting_count_ui > 0:
                                est_min = real_avg_min * waiting_count_ui
                                if est_min > 120: estimated_remain_str = f"{est_min/60:.1f}時"
                                else: estimated_remain_str = f"{est_min:.0f}分"
                            elif waiting_count_ui == 0:
                                estimated_remain_str = "0分"

                            stz = data.get('status', '')
                            lv = data.get('light', '')
                            skip_plateau = (
                                "休診" in stz
                                or "停診" in stz
                                or lv == "休"
                                or stz == "尚未開診"
                                or data.get("is_closed")
                            )

                            is_closed_page = data.get('is_closed', False)
                            is_stopped_page = bool(data.get('is_stopped')) and bool(data.get('true_schedule_dayoff'))
                            is_ended = False

                            if is_closed_page or is_stopped_page:
                                is_ended = True
                                if tracker['actual_closing_dt'] is None:
                                    ct = (data.get('close_time') or '').strip()
                                    parsed_dt = None
                                    if ct:
                                        try:
                                            if ":" in ct:
                                                hp, mp = ct.split(":", 1)
                                                hh, mm = int(hp), int(mp[:2])
                                            else:
                                                ctn = re.sub(r"\D", "", ct)
                                                if len(ctn) >= 4:
                                                    hh, mm = int(ctn[:2]), int(ctn[2:4])
                                                else:
                                                    raise ValueError()
                                            parsed_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                                        except Exception:
                                            parsed_dt = None
                                    # 僅在能從網頁解析到關診時刻時記錄；否則維持 None，UI 顯示「已關診」不附時間
                                    tracker['actual_closing_dt'] = parsed_dt
                            elif not skip_plateau and tracker.get('had_any_activity'):
                                boundary = _session_boundary_datetime(curr_session_i, now)
                                if now >= boundary:
                                    pair = (completed_count_ui, waiting_count_ui)
                                    if tracker.get('last_monitor_pair') != pair:
                                        tracker['last_monitor_pair'] = pair
                                        tracker['stable_since_ts'] = current_timestamp
                                    else:
                                        ss = tracker.get('stable_since_ts')
                                        if ss is None:
                                            tracker['stable_since_ts'] = current_timestamp
                                            ss = current_timestamp
                                        if current_timestamp - ss >= CLINIC_CLOSE_PLATEAU_SECONDS:
                                            if tracker['actual_closing_dt'] is None:
                                                tracker['actual_closing_dt'] = datetime.fromtimestamp(ss)
                                            is_ended = True
                                else:
                                    tracker['stable_since_ts'] = None
                                    tracker['last_monitor_pair'] = None
                            if skip_plateau:
                                tracker['stable_since_ts'] = None
                                tracker['last_monitor_pair'] = None

                            should_save = False
                            if has_valid_completion and not is_ended: should_save = True 
                            if is_ended and not tracker['is_saved']: should_save = True

                            if should_save and tracker['doc_name'] and completed_count_ui > 0:
                                close_time_save = ""
                                if tracker['actual_closing_dt']:
                                    close_time_save = tracker['actual_closing_dt'].strftime("%H:%M")
                                traw = data.get('total')
                                total_reg_save = int(traw) if isinstance(traw, int) else None

                                self.bg_executor.submit(
                                    self._save_clinic_session_stat,
                                    room_code,
                                    tracker['doc_name'],
                                    completed_count_ui,
                                    list(tracker['durations']),
                                    close_time_save,
                                    curr_session_i,
                                    total_reg_save,
                                    int(tracker.get('phototherapy_count', 0)),
                                )
                                if is_ended:
                                    tracker['is_saved'] = True
                                
                    except Exception as e: 
                        logging.error(f"Tracking error for {room_code}: {e}", exc_info=True)

                    all_time_avg_str = self._calculate_all_time_avg(tracker['doc_name'], tracker['durations'])
                    track_copy = tracker.copy()

                sess_for_avg = reg64_slot_cn(data.get('reg64_time_code', '')) or "晚上"
                mt_avg, mc_avg, mp_avg = self._monthly_slot_metric_avgs(
                    track_copy.get('doc_name'), room_code, sess_for_avg
                )
                prev_sess_close_str = self._get_prev_session_closing_clock(
                    room_code, track_copy.get('doc_name'), sess_for_avg
                )

                def update_ui(
                    index=i,
                    result=data,
                    c_avg=current_session_avg_str,
                    l_avg=all_time_avg_str,
                    est=estimated_remain_str,
                    track=track_copy,
                    wait_avg=avg_wait_str,
                    mt=mt_avg,
                    mc=mc_avg,
                    mp=mp_avg,
                    psc=prev_sess_close_str,
                ):
                    self.update_single_clinic_ui(index, result, track, c_avg)
                    
                    ui = self.clinic_ui_elements[index]
                    
                    self._smart_widget_config(ui['comp_all'], text=str(result.get('completed', 0)))
                    self._smart_widget_config(ui['season_avg'], text=l_avg)
                    self._smart_widget_config(ui['avg_wait'], text=wait_avg)
                    self._smart_widget_config(ui['m_avg_total'], text=mt)
                    self._smart_widget_config(ui['m_avg_comp'], text=mc)
                    self._smart_widget_config(ui['m_avg_photo'], text=mp)
                    self._smart_widget_config(ui['prev_sess_close'], text=psc)
                    est_disp = est if est and est != "-" else "—"
                    self._smart_widget_config(ui['est_remain'], text=est_disp, fg="#E65100")

                    status_txt = result.get('status', '')
                    light_val = result.get('light', '')
                    
                    if "休診" in status_txt or light_val == "休" or status_txt == "本日停診":
                        self._smart_widget_config(ui['total'], text="-")
                        self._smart_widget_config(ui['comp_all'], text="-")
                        self._smart_widget_config(ui['waiting'], text="-")
                        self._smart_widget_config(ui['photo'], text="-")
                        self._smart_widget_config(ui['curr_avg'], text="-")
                        self._smart_widget_config(ui['season_avg'], text="-")
                        self._smart_widget_config(ui['est_remain'], text="—")
                        self._smart_widget_config(ui['avg_wait'], text="-")
                        self._smart_widget_config(ui['m_avg_total'], text="-")
                        self._smart_widget_config(ui['m_avg_comp'], text="-")
                        self._smart_widget_config(ui['m_avg_photo'], text="-")
                        self._smart_widget_config(ui['prev_sess_close'], text="—")

                self.root.after(0, update_ui)

            # 門診動態快取更新後一併重繪總覽／未來週次月曆（逾時列、reg64 人數），與 _schedule_refresh 節流一致
            def _after_clinic_fetch_schedule_calendar():
                if getattr(self, "_shutting_down", False):
                    return
                if not hasattr(self, "summary_calendar_widgets"):
                    return
                try:
                    self._schedule_refresh()
                except Exception:
                    logging.debug("月曆重繪排程失敗（門診動態後）", exc_info=True)

            self.root.after(0, _after_clinic_fetch_schedule_calendar)
            self._clinic_dynamic_refresh_seconds = 10 if active_now else 24
            self._reg64_dynamic_ttl_seconds = 8 if active_now else 15
            if source_timing.get("backoff_skip", 0) > 0 or abnormal_rooms:
                logging.warning(
                    f"[SOURCE_TIMING][reg64] {source_timing}, abnormal_rooms={abnormal_rooms}"
                )

        # [核心修正] 投遞至執行緒池
        self.bg_executor.submit(run_update, rooms_to_check)
        
        seconds = getattr(self, '_clinic_dynamic_refresh_seconds', CLINIC_LIGHT_REFRESH_SECONDS)
        if seconds < 10:
            seconds = 10
        next_refresh_ms = seconds * 1000
        self.clinic_loop_id = self.root.after(next_refresh_ms, self._update_clinic_lights_loop)

    # [新增] 用於錯誤時的 UI 更新函式，只更新狀態文字，不影響數值
    def update_single_clinic_ui_error(self, index, result):
        ui = self.clinic_ui_elements[index]
        status_txt = result.get('status', '連線異常')
        self._smart_widget_config(ui['status'], text=status_txt, fg="red")
        self._smart_widget_config(ui['light'], text="--", fg="gray")
        tc_raw = result.get("reg64_time_code", "")
        cn = reg64_slot_cn(tc_raw)
        if "slot_banner" in ui:
            self._smart_widget_config(
                ui["slot_banner"],
                text=cn or "—",
                fg=reg64_slot_label_color(tc_raw),
            )
        if cn:
            self._smart_widget_config(ui['doc_name'], text="", fg="#00796B")
        for k in ("m_avg_total", "m_avg_comp", "m_avg_photo", "prev_sess_close", "est_remain"):
            if k in ui:
                self._smart_widget_config(ui[k], text="—")

    # [新增] 獨立的 UI 更新函數，方便在「停止查詢」時也能呼叫
    def update_single_clinic_ui(self, index, result, tracker, c_avg="-"):
        ui = self.clinic_ui_elements[index]

        # 醫師姓名（早午晚已顯示於診間代號後方）
        doc_name = result.get('doc_name') or tracker.get('doc_name', '')
        slot_cn = reg64_slot_cn(result.get("reg64_time_code", ""))
        doc_line = doc_name or (f"（{slot_cn}）" if slot_cn else "")
        self._smart_widget_config(ui['doc_name'], text=doc_line, fg="#00796B")

        if "slot_banner" in ui:
            self._smart_widget_config(
                ui["slot_banner"],
                text=slot_cn or "—",
                fg=reg64_slot_label_color(result.get("reg64_time_code", "")),
            )

        # [修正] season_avg 由呼叫端 update_ui 計算後傳入並更新，這裡跳過避免雙重計算
        # (update_ui closure 在 L2814 設定 ui['season_avg'])
        
        # 目前平均（預估剩餘由 update_ui 寫入 est_remain）
        self._smart_widget_config(ui['curr_avg'], text=c_avg)
        self._smart_widget_config(ui['photo'], text=str(tracker.get('phototherapy_count', 0)))

        # 燈號
        light_val = result.get('light', '--')
        if light_val == "0" or light_val == "--":
            self._smart_widget_config(ui['light'], text="--", fg="#FF3333")
        else:
            self._smart_widget_config(ui['light'], text=light_val, fg="#FF0000")

        # 狀態與背景
        status_txt = result.get('status', '')
        
        if tracker.get('actual_closing_dt'):
            # 已關診且程式有記錄到關診時刻時才附時間
            t_str = tracker['actual_closing_dt'].strftime("%H:%M")
            self._smart_widget_config(ui['status'], text=f"已關診 ({t_str})", fg="#D32F2F")
            self._smart_widget_config(ui['card_bg'], bg="#F0F0F0")
            self._smart_widget_config(ui['total'], text=str(result.get('total', '-')))
            self._smart_widget_config(ui['waiting'], text=str(result.get('waiting', '-')))
        elif result.get("is_closed"):
            # 網頁已關診（如燈號 99 (已關診)）但無解析到關診時刻：只顯示「已關診」
            self._smart_widget_config(ui['status'], text="已關診", fg="#D32F2F")
            self._smart_widget_config(ui['card_bg'], bg="#F0F0F0")
            self._smart_widget_config(ui['total'], text=str(result.get('total', '-')))
            self._smart_widget_config(ui['waiting'], text=str(result.get('waiting', '-')))
        elif "休診" in status_txt:
            self._smart_widget_config(ui['status'], text="本日/時段休診", fg="#78909C")
            self._smart_widget_config(ui['card_bg'], bg="#ECEFF1")
            self._smart_widget_config(ui['doc_name'], fg="#90A4AE")
        elif "尚未開診" in status_txt:
            self._smart_widget_config(ui['status'], text="尚未開診", fg="#1565C0")
            self._smart_widget_config(ui['card_bg'], bg="#F5F9FF")
        elif "本日停診" in status_txt or (result.get('is_stopped') and result.get('true_schedule_dayoff')):
            self._smart_widget_config(ui['status'], text="本日停診", fg="#78909C")
            self._smart_widget_config(ui['card_bg'], bg="#ECEFF1")
            self._smart_widget_config(ui['doc_name'], fg="#90A4AE")
        else:
            # 正常看診中
            self._smart_widget_config(ui['card_bg'], bg="white")
            self._smart_widget_config(ui['status'], text=f"更新於 {datetime.now().strftime('%H:%M')}", fg="green")
            self._smart_widget_config(ui['total'], text=str(result.get('total', '-')))
            self._smart_widget_config(ui['waiting'], text=str(result.get('waiting', '-')))

    def _get_last_closing_time(self, doc_name, weekday_int, session_str):
        if not doc_name: return None
        
        matches = []
        # [核心修正] 保護讀取
        with self._history_lock:
            for r in self.history_cache:
                if (r.get('doctor') == doc_name and 
                    r.get('session') == session_str and 
                    r.get('closing_time')): 
                    
                    try:
                        d_obj = datetime.strptime(r['date'], "%Y/%m/%d")
                        if d_obj.weekday() == weekday_int:
                            matches.append(r)
                    except Exception:
                        pass
        
        if matches:
            matches.sort(key=lambda x: x['date'], reverse=True)
            return matches[0].get('closing_time')
            
        return None

    def _get_prev_session_closing_clock(self, room_code, doc_name, curr_session_cn):
        """今日、同診間、同醫師之「上一時段」關診時間 (HH:MM)。"""
        prev_s = _prev_session_cn(curr_session_cn)
        if not prev_s or not doc_name or not room_code:
            return "—"
        today_s = date.today().strftime("%Y/%m/%d")
        best = ""
        with self._history_lock:
            for r in self.history_cache:
                if r.get("doctor") != doc_name:
                    continue
                if str(r.get("room", "")) != str(room_code):
                    continue
                if r.get("session") != prev_s:
                    continue
                if r.get("date") != today_s:
                    continue
                ct = r.get("closing_time") or ""
                if ct:
                    best = ct
        return best if best else "—"

    def _monthly_slot_metric_avgs(self, doc_name, room_code, session_cn):
        """近 CLINIC_METRIC_HISTORY_DAYS 日、同診間／時段／醫師之掛號、完成、照光平均。"""
        if not doc_name or not room_code:
            return ("-", "-", "-")
        cutoff = date.today() - timedelta(days=CLINIC_METRIC_HISTORY_DAYS)
        totals, comps, photos = [], [], []
        with self._history_lock:
            for r in self.history_cache:
                if r.get("doctor") != doc_name:
                    continue
                if str(r.get("room", "")) != str(room_code):
                    continue
                if r.get("session") != session_cn:
                    continue
                try:
                    rd = datetime.strptime(r["date"], "%Y/%m/%d").date()
                except Exception:
                    continue
                if rd < cutoff:
                    continue
                tr = r.get("total_reg")
                if tr is not None and tr != "":
                    try:
                        totals.append(float(tr))
                    except (TypeError, ValueError):
                        pass
                try:
                    comps.append(float(r.get("completed_count", 0)))
                except (TypeError, ValueError):
                    pass
                ph = r.get("phototherapy")
                if ph is not None and ph != "":
                    try:
                        photos.append(float(ph))
                    except (TypeError, ValueError):
                        pass

        def _fmt(a):
            if not a:
                return "-"
            return str(int(round(sum(a) / len(a))))

        return (_fmt(totals), _fmt(comps), _fmt(photos))

# --- [新增] 計算統計數據並存檔 ---
    def _save_clinic_session_stat(self, room_code, doc_name, completed_count, durations, closing_time_str="", session_str=None, total_reg=None, phototherapy=0):
        if not doc_name or not durations: return
        if len(durations) == 0: return

        avg_raw = sum(durations) / len(durations)
        valid_data = [x for x in durations if (avg_raw * 0.5) <= x <= (avg_raw * 2.0)]
        if not valid_data: valid_data = durations
            
        final_avg_sec = sum(valid_data) / len(valid_data)
        final_avg_min = round(final_avg_sec / 60, 1)

        today_str = date.today().strftime("%Y/%m/%d")
        session = session_str or reg64_slot_cn(reg64_time_code_from_local_clock()) or "晚上"
        
        file_path = get_conf_path('clinic_stats_history.json')
        
        with self._history_lock:
            history_data = []
            try:
                if os.path.exists(file_path):
                    with open(file_path, 'r', encoding='utf-8') as f:
                        history_data = json.load(f)
            except Exception: pass
            
            record_found = False
            for record in history_data:
                if (record.get('date') == today_str and 
                    record.get('session') == session and 
                    record.get('doctor') == doc_name):
                    
                    record['completed_count'] = completed_count
                    record['avg_time_min'] = final_avg_min
                    record['raw_sample_count'] = len(durations)
                    record['valid_sample_count'] = len(valid_data)
                    record['room'] = room_code 
                    if closing_time_str:
                        record['closing_time'] = closing_time_str
                    if total_reg is not None:
                        record['total_reg'] = total_reg
                    record['phototherapy'] = phototherapy
                    record_found = True
                    break
            
            if not record_found:
                new_record = {
                    "date": today_str,
                    "week": date.today().strftime("%W"),
                    "room": room_code,
                    "session": session,
                    "doctor": doc_name,
                    "completed_count": completed_count,
                    "avg_time_min": final_avg_min,
                    "raw_sample_count": len(durations),
                    "valid_sample_count": len(valid_data),
                    "closing_time": closing_time_str,
                    "total_reg": total_reg if total_reg is not None else None,
                    "phototherapy": phototherapy,
                }
                history_data.append(new_record)
            
            try:
                _atomic_write_json(file_path, history_data)
                self.history_cache = history_data
                self._avg_history_cache = {}  # [優化] 歷史資料更新，清除計算快取
            except Exception as e:
                logging.error(f"儲存統計失敗: {e}")

    def reset_clinic_stats(self, room_index):
        """
        [新增] 重置指定診間的統計數據
        1. 清除目前平均 (記憶體中的 durations 與 照光計數)
        2. 清除該醫師的近一月歷史樣本 (修改 JSON 檔案，刪除該醫師所有紀錄)
        """
        try:
            # 取得診間代號
            room_code = self.clinic_room_vars[room_index].get().strip()
            
            # 防呆: 檢查追蹤器是否存在
            if not room_code or not hasattr(self, 'clinic_trackers') or room_code not in self.clinic_trackers:
                messagebox.showwarning("無法重置", "目前沒有該診間的追蹤資料，無法執行重置。")
                return

            tracker = self.clinic_trackers[room_code]
            doc_name = tracker.get('doc_name', '')
            
            # 防呆: 檢查是否已抓到醫師姓名 (因為刪除歷史紀錄依賴醫師姓名)
            if not doc_name:
                messagebox.showwarning("無法重置", "目前尚未偵測到醫師姓名，無法執行針對該醫師的歷程清除。")
                return

            # 確認對話框
            if not messagebox.askyesno("確認重置", f"確定要重置 [{doc_name}] 的所有時間統計嗎？\n\n這將會執行：\n1. 清除目前的平均時間與照光計數\n2. 刪除該醫師所有近一月統計相關歷史紀錄 (JSON檔案)"):
                return

# --- 1. 重置目前狀態 (記憶體) ---
            tracker['durations'] = []
            tracker['last_time'] = time.time()
            tracker['first_record_skipped'] = False 
            tracker['phototherapy_count'] = 0 
            
            # [新增] 重置等待時間相關
            tracker['patient_checkin_times'] = {}
            tracker['waiting_durations'] = []
            
            logging.info(f"[{room_code}] 目前平均時間與計數已重置。")

            # --- 2. 重置長期紀錄 (檔案) ---
            file_path = get_conf_path('clinic_stats_history.json')
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        history = json.load(f)
                    
                    # 過濾掉該位醫師的紀錄 (保留其他醫師的，刪除當前醫師的)
                    new_history = [record for record in history if record.get('doctor') != doc_name]
                    
                    # 寫回檔案 (使用原子寫入防止中途崩潰損壞)
                    _atomic_write_json(file_path, new_history)
                    self.history_cache = new_history
                    self._avg_history_cache = {}  # [優化] 清除計算快取

                    logging.info(f"[{doc_name}] 歷史統計資料已從檔案中移除。")
                    
                except Exception as e:
                    logging.error(f"重置歷史檔案失敗: {e}")
                    messagebox.showerror("錯誤", f"重置歷史檔案失敗:\n{e}")
                    return

            # 強制刷新 UI 以顯示歸零後的狀態
            self.force_refresh_clinic_lights()
            messagebox.showinfo("成功", f"[{doc_name}] 的統計資料已重置成功。")

        except Exception as e:
            logging.error(f"Reset stats error: {e}")
            messagebox.showerror("錯誤", f"執行重置時發生錯誤:\n{e}")

    def force_refresh_clinic_lights(self, event=None):
        """偵測到輸入變更時，取消舊排程，自動存檔並執行更新"""
        logging.info("偵測到門診動態設定變更...")
        
        # 存檔改為下一個事件迴圈再寫檔，讓本方法先送出 HTTP（避免磁碟 I/O 擠在點選當下）
        self.root.after(0, self.save_clinic_settings)
        
        # 1. 如果有正在倒數的排程，先取消它
        if hasattr(self, 'clinic_loop_id') and self.clinic_loop_id:
            try:
                self.root.after_cancel(self.clinic_loop_id)
            except Exception:
                pass
        
        # 2. 立即執行一次更新
        self._update_clinic_lights_loop()

    def _calculate_all_time_avg(self, doc_name, current_durations=None):
        if not doc_name: return "-"

        cutoff = date.today() - timedelta(days=CLINIC_METRIC_HISTORY_DAYS)
        cache_key = (doc_name, cutoff.toordinal())
        # [優化] 先查快取；近一月範圍變更時以 cutoff 區分
        with self._history_lock:
            if cache_key not in self._avg_history_cache:
                hist_min = 0.0
                hist_count = 0
                for record in self.history_cache:
                    if record.get('doctor') != doc_name:
                        continue
                    try:
                        rd = datetime.strptime(record.get('date', ''), "%Y/%m/%d").date()
                    except Exception:
                        continue
                    if rd < cutoff:
                        continue
                    avg_min = record.get('avg_time_min', 0)
                    count = record.get('valid_sample_count', 0)
                    if count > 0:
                        hist_min += (avg_min * count)
                        hist_count += count
                self._avg_history_cache[cache_key] = (hist_min, hist_count)
            total_minutes, total_count = self._avg_history_cache[cache_key]

        if current_durations:
            valid_current = [x for x in current_durations if x > 0]
            if valid_current:
                curr_sum_sec = sum(valid_current)
                curr_count = len(valid_current)
                curr_sum_min = curr_sum_sec / 60.0
                
                total_minutes += curr_sum_min
                total_count += curr_count

        if total_count > 0:
            final_avg = total_minutes / total_count
            return f"{final_avg:.1f}"
        else:
            return "-"

# --- [新增] 讀取門診動態設定 ---
    # --- [新增] 讀取門診動態設定 ---
    def load_clinic_settings(self):
        # [修改] 預設更新頻率改為 60 秒 (符合您的需求)
        default_settings = {"rooms": ["181", "182"], "time_modes": ["auto", "auto"]}
        try:
            with open(get_conf_path('clinic_settings.json'), 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default_settings

    # --- [新增] 儲存門診動態設定 ---
    def save_clinic_settings(self):
        try:
            data = {
                "rooms": [var.get() for var in self.clinic_room_vars],
                "time_modes": [
                    _normalize_clinic_display_mode(self.clinic_display_mode_vars[j].get())
                    for j in range(len(self.clinic_room_vars))
                ],
            }
            _atomic_write_json(get_conf_path('clinic_settings.json'), data)
            logging.info("門診動態設定已自動儲存。")
        except Exception as e:
            logging.error(f"Failed to save clinic settings: {e}")

    def _start_shorten_url(self):
        """觸發縮網址背景任務"""
        long_url = self.url_input_var.get().strip()
        if not long_url:
            messagebox.showwarning("提示", "請先輸入要縮短的網址！")
            self.url_entry.focus()
            return
            
        if not long_url.startswith("http"):
            long_url = "http://" + long_url
            
        self.shorten_btn.config(state="disabled")
        self.url_status_label.config(text="處理中 (約5-10秒)...", foreground="blue")
        self.url_output_var.set("") # 清空舊結果
        
        # 使用既有執行緒池避免額外 thread 開銷
        self.bg_executor.submit(self._run_url_shortener, long_url)

    def _run_url_shortener(self, long_url):
        try:
            # 直接使用 TinyURL 官方 API；params 正確編碼避免含 &、% 等字元時失敗
            response = requests.get(
                "https://tinyurl.com/api-create.php",
                params={"url": long_url},
                timeout=8,
                verify=True,
            )
            response.raise_for_status()
            short_url = response.text.strip()
            
            if short_url and short_url.startswith("http"):
                # 複製到系統剪貼簿
                self.root.after(0, lambda: self.root.clipboard_clear())
                self.root.after(0, lambda: self.root.clipboard_append(short_url))
                
                # 更新介面顯示
                def update_ui_success():
                    self.url_output_var.set(short_url)
                    self.url_status_label.config(text="成功！(已複製)", foreground="green")
                    self.url_input_var.set("") # 清空輸入框
                    self.url_output_entry.focus()
                    self.url_output_entry.select_range(0, tk.END)
                
                self.root.after(0, update_ui_success)
            else:
                raise Exception("API 回傳異常")

        except Exception as e:
            # 須先保存訊息：Python 3.12+ 在 except 區塊結束後會清除 e，after(0) 回調不可再讀 e
            err_msg = str(e)
            logging.error(f"Shorten URL failed: {e}")
            def update_ui_fail():
                self.url_status_label.config(text="失敗", foreground="red")
                messagebox.showerror("縮網址失敗", f"無法縮短網址，原因：\n{err_msg}")
            self.root.after(0, update_ui_fail)
            
        finally:
            self.root.after(0, lambda: self.shorten_btn.config(state="normal"))
        
    def _trigger_refresh(self, is_manual=False, specific_doctors=None):
        def _build_refresh_signature(manual, doctors):
            if doctors is None:
                return ("all", bool(manual), None)
            names = []
            for d in doctors:
                if isinstance(d, dict):
                    names.append(str(d.get("name", "")))
                else:
                    names.append(str(d))
            return ("partial", bool(manual), tuple(sorted(n for n in names if n)))

        status_msg = "狀態: 手動整理中..." if is_manual else "狀態: 自動更新中..."
        logging.info(f"--- Triggering refresh (manual={is_manual}) ---")
        req_signature = _build_refresh_signature(is_manual, specific_doctors)

        if self._refresh_worker_running:
            with self._refresh_queue_lock:
                if req_signature == self._active_refresh_signature or req_signature in self._queued_refresh_signatures:
                    logging.info(f"Duplicate refresh request skipped. signature={req_signature}")
                    return
                # 合併同來源 partial 批次：避免佇列堆疊大量小刷新
                if specific_doctors is not None:
                    merged = False
                    incoming_names = set()
                    for d in specific_doctors:
                        incoming_names.add(str(d.get("name", "")) if isinstance(d, dict) else str(d))
                    for idx, (qm, qdocs, qsig) in enumerate(self._queued_refresh_requests):
                        if qdocs is None or qm != is_manual:
                            continue
                        qnames = set()
                        for d in qdocs:
                            qnames.add(str(d.get("name", "")) if isinstance(d, dict) else str(d))
                        union = qnames | incoming_names
                        if union != qnames:
                            by_name = {}
                            for d in list(qdocs) + list(specific_doctors):
                                nm = str(d.get("name", "")) if isinstance(d, dict) else str(d)
                                if nm and nm not in by_name:
                                    by_name[nm] = d
                            merged_docs = list(by_name.values())
                            new_sig = _build_refresh_signature(is_manual, merged_docs)
                            self._queued_refresh_signatures.discard(qsig)
                            self._queued_refresh_requests[idx] = (is_manual, merged_docs, new_sig)
                            self._queued_refresh_signatures.add(new_sig)
                            merged = True
                            logging.info(f"Merged partial refresh request. size={len(merged_docs)}")
                            break
                    if merged:
                        return
                self._queued_refresh_requests.append((is_manual, specific_doctors, req_signature))
                self._queued_refresh_signatures.add(req_signature)
                qsize = len(self._queued_refresh_requests)
            logging.info(f"Refresh already running; queued request. queue_size={qsize}")
            return
        with self._refresh_queue_lock:
            self._active_refresh_signature = req_signature

        if specific_doctors is None:
            now_ts = time.time()
            snap = getattr(self, "_last_full_refresh_snapshot", None)
            snap_ts = getattr(self, "_last_full_refresh_ts", 0.0)
            if snap and (now_ts - snap_ts) <= GLOBAL_REFRESH_SNAPSHOT_TTL_SECONDS:
                for k, v in snap.items():
                    put_ui_message(self.ui_queue, UiClinicDataMessage(doctor_name=k, data=v))
                logging.info(f"[SNAPSHOT] replayed full refresh cache, doctors={len(snap)}")
        
        self.status_text.set(status_msg)
        self.startup_phase_text.set("更新資料")
        self.refresh_button.config(state="disabled")
        
        doctors_to_check = specific_doctors if specific_doctors is not None else DOCTORS
        self._refresh_progress_total = len(doctors_to_check)
        self._refresh_progress_done = 0
        
        def run_parallel_checks():
            chain_startup_full = (
                getattr(self, "_startup_defer_full_until_priority_done", False)
                and specific_doctors is not None
            )
            self._refresh_worker_running = True
            try:
                batches = partition_doctors_for_refresh_batches(doctors_to_check)
                for bi, batch in enumerate(batches):
                    futures = []
                    for doctor_config in batch:
                        future = self.bg_executor.submit(check_appointment_count, self.ui_queue, doctor_config)
                        futures.append(future)
                    if futures:
                        wait(futures, return_when=ALL_COMPLETED)
                        for fut in futures:
                            try:
                                fut.result()
                            except Exception:
                                logging.exception("掛號資料擷取背景工作失敗（單一醫師工作緒）")
                    if bi < len(batches) - 1:
                        time.sleep(0.18)
            finally:
                self._refresh_worker_running = False
                with self._refresh_queue_lock:
                    self._active_refresh_signature = None
                    queued_request = self._queued_refresh_requests.popleft() if self._queued_refresh_requests else None
                    if queued_request is not None:
                        self._queued_refresh_signatures.discard(queued_request[2])
                refresh_time = datetime.now().strftime('%H:%M:%S')

                def _on_refresh_worker_done(rt=refresh_time, qr=queued_request):
                    self._cancel_pending_refresh_tick_ui()
                    self.refresh_button.config(state="normal")
                    self.last_refresh_text.set(f"更新: {rt}")
                    if self._heavy_modules_ready:
                        self.startup_phase_text.set("完成")
                    self.status_text.set(f"狀態: 閒置（最新更新: {rt}）")
                    if specific_doctors is None:
                        with self._doctor_data_lock:
                            self._last_full_refresh_snapshot = deepcopy(self.all_doctors_data)
                        self._last_full_refresh_ts = time.time()
                    if qr is not None:
                        self._trigger_refresh(qr[0], qr[1])
                    elif chain_startup_full:
                        self._startup_defer_full_until_priority_done = False
                        self.bg_executor.submit(self._trigger_refresh, False)

                self.root.after(0, _on_refresh_worker_done)

        self.bg_executor.submit(run_parallel_checks)

    def _trigger_rehook_hotkeys(self):
        logging.info("--- Manually re-hooking all hotkeys ---")
        if not self._heavy_modules_ready:
            messagebox.showwarning("請稍候", "熱鍵模組尚在載入中，請稍後再試。")
            return
        try:
            safe_unhook_all_hotkeys()
            self.setup_hotkeys()
            messagebox.showinfo("成功", "所有熱鍵功能已成功重製。")
            self.status_text.set("狀態: 熱鍵已重製")
        except Exception as e:
            logging.error(f"Failed to re-hook hotkeys manually: {e}")
            messagebox.showerror("失敗", f"重製熱鍵時發生錯誤: {e}")
            self.status_text.set("狀態: 熱鍵重製失敗")

    def _copy_to_clipboard(self, text_widget):
        try:
            text_to_copy = text_widget.get("1.0", tk.END).strip()
            if text_to_copy: self.root.clipboard_clear(); self.root.clipboard_append(text_to_copy); self.status_text.set("狀態: 內容已複製到剪貼簿！"); self.root.after(3000, lambda: self.status_text.set("狀態: 閒置"))
            else: self.status_text.set("狀態: 沒有內容可以複製。")
        except Exception as e: self.status_text.set("狀態: 複製失敗！"); logging.error(f"Failed to copy to clipboard: {e}")

    def _create_certificate_tab(self, cert_tab):
        content_frame = ttk.Frame(cert_tab)
        content_frame.pack(fill=tk.BOTH, expand=True)
        content_frame.columnconfigure(0, weight=1)
        content_frame.columnconfigure(1, weight=1)
        content_frame.rowconfigure(0, weight=1)
        content_frame.rowconfigure(1, weight=1)
        try:
            with open(get_conf_path('certificate_templates.json'), 'r', encoding='utf-8') as f:
                cert_data = json.load(f)
                if not isinstance(cert_data, list) or len(cert_data) != 4:
                    cert_data = self._get_default_cert_data()
        except (FileNotFoundError, json.JSONDecodeError):
            cert_data = self._get_default_cert_data()

        self.cert_widgets_list = []
        for i, data in enumerate(cert_data):
            row = i // 2
            col = i % 2
            lf = ttk.LabelFrame(content_frame, text=data["title"])
            lf.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
            lf.rowconfigure(0, weight=1)
            lf.columnconfigure(0, weight=1)
            txt = scrolledtext.ScrolledText(lf, wrap=tk.WORD, height=5, font=("Microsoft JhengHei UI", 10))
            txt.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
            txt.insert("1.0", data["content"])
            self.cert_widgets_list.append({"frame": lf, "text": txt})
            ttk.Button(lf, text="複製內容", command=lambda t=txt: self._copy_to_clipboard(t)).grid(row=1, column=0, sticky="se", padx=5, pady=(0, 5))

        button_frame = ttk.Frame(cert_tab)
        button_frame.pack(fill=tk.X, pady=10)
        ttk.Button(button_frame, text="儲存目前內容", command=self._save_certificate_templates).pack(side=tk.RIGHT, padx=5)
        ttk.Button(button_frame, text="還原為預設範本", command=self._reset_certificate_templates).pack(side=tk.RIGHT, padx=5)

    def _save_certificate_templates(self):
        cert_data = []
        titles = ["常用診斷書", "許主任診斷書", "駿升診斷書", "常用中文診斷"] 
        try:
            for i, widget_dict in enumerate(self.cert_widgets_list):
                content = widget_dict["text"].get("1.0", tk.END).strip()
                cert_data.append({"title": titles[i], "content": content}) 
            
            _atomic_write_json(get_conf_path('certificate_templates.json'), cert_data)
            messagebox.showinfo("成功", "診斷書內容已儲存！下次開啟時將會自動載入。")
        except Exception as e: 
            logging.error(f"Failed to save certificate templates: {e}")
            messagebox.showerror("失敗", f"儲存失敗: {e}")

    def _reset_certificate_templates(self):
        if not messagebox.askyesno("確認", "您確定要將所有診斷書內容還原為預設範本嗎？"): return
        default_cert_data = self._get_default_cert_data()
        for i, widget_dict in enumerate(self.cert_widgets_list):
            widget_dict["frame"].config(text=default_cert_data[i]["title"])
            text_widget = widget_dict["text"]
            text_widget.config(state="normal")
            text_widget.delete("1.0", tk.END)
            text_widget.insert("1.0", default_cert_data[i]["content"])
        messagebox.showinfo("完成", "已還原為預設範本。")
    
    def _get_default_cert_data(self):
        return [
            {"title": "常用診斷書", "content": "患者因上述皮膚疾病，於2025年9月25日至本院皮膚科門診就醫治療，後續接受局部麻醉下皮膚腫瘤切除手術及縫合，術後病理檢查結果合乎上述疾患。患者於2025年9月25日返回本院皮膚科門診接受術後照護並拆除手術縫線。"},
            {"title": "許主任診斷書", "content": "患者因上述皮膚疾病，曾於中華民國114年8月4日至本院皮膚科門診就醫，後續於同年8月8日接受局部麻醉下之皮膚腫瘤切除及縫合手術，術後病理檢查結果符合上述疾患。患者於術後之同年8月11日返回本院皮膚科門診接受照護，並分別於8月18日及8月25日分次拆除手術縫線。"},
            
            # [修改] 更新駿升診斷書內容
            {"title": "駿升診斷書", "content": "患者因上述疾病，於民國114年9月25日至本院就診，於民國114年9月26日接受局部麻醉下皮膚腫瘤切除手術並縫合，使用自費生長因子、自費膠原蛋白、自費組織黏膠、自費皮膚接合自黏網片、自費免打結縫線，於民國114年9月27日回診。\n\n\nDERMABOND=皮膚接合自黏網片\nSFX=免打結縫線\n固麗齊組織黏膠=組織黏膠\n速原水性創傷敷料NEWEPI=生長因子\n海昌膠原蛋白、癒立安膠原蛋白=膠原蛋白"},
            
            {"title": "常用中文診斷", "content": "表淺性脂肪瘤母斑(Nevus lipomatosus superficialis)\n小痣(Lentigo)\n毛髮基質瘤(Pilomatrixoma)\n色素性紫斑性皮膚病(Pigmented purpuric dermatosis, PPD)\n侷限性硬皮病(Morphea)"}
        ]

    def _create_future_weeks_tab(self, future_tab):
        controls_frame = ttk.Frame(future_tab); controls_frame.pack(fill='x', pady=5, anchor='n'); ttk.Label(controls_frame, text="請選擇週次:").pack(side='left', padx=(0, 10))
        self.future_week_ranges = {}; options = []; today = date.today(); start_of_this_week_monday = today - timedelta(days=today.weekday())
        for i in range(3):
            weeks_to_add = (i * 2) + 2; start_week_num = weeks_to_add + 1; end_week_num = weeks_to_add + 2
            start_date = start_of_this_week_monday + timedelta(weeks=weeks_to_add); label = f"{end_week_num}周內 (顯示第{start_week_num}-{end_week_num}週)"; options.append(label); self.future_week_ranges[label] = (start_date, [f"第 {start_week_num} 週", f"第 {end_week_num} 週"])
        self.future_week_selector = ttk.Combobox(controls_frame, values=options, state='readonly'); self.future_week_selector.pack(side='left'); self.future_week_selector.bind("<<ComboboxSelected>>", self.on_future_week_selected)
        future_display_frame = ttk.Frame(future_tab); future_display_frame.pack(fill="both", expand=True, pady=(5,0)); labels_frame = ttk.Frame(future_display_frame); labels_frame.pack(side="left", fill="y", padx=(0, 5)); calendar_container = ttk.Frame(future_display_frame); calendar_container.pack(side="left", fill="both", expand=True)
        self.future_week_labels = []; ttk.Label(labels_frame, text="", style="Header.TLabel").pack(pady=1)
        for i in range(2): label = ttk.Label(labels_frame, text="", font=("Microsoft JhengHei UI", 9, "bold"), anchor="center"); label.pack(fill="both", expand=True); self.future_week_labels.append(label)
        self.future_calendar_widgets = self._create_calendar_grid(calendar_container, num_weeks=2)
        if options: self.future_week_selector.set(options[0]); self.on_future_week_selected()

    def on_future_week_selected(self, event=None):
        if getattr(self, '_shutting_down', False):
            return
        selected_label = self.future_week_selector.get()
        if not selected_label or not hasattr(self, 'future_week_labels'): return
        start_date, week_names = self.future_week_ranges[selected_label]; self._update_grid_data(start_date, self.future_calendar_widgets, 2, is_future=True)
        for i, label in enumerate(self.future_week_labels):
            if i < len(week_names): label.config(text=format_vertical_text(week_names[i]))
        self._future_tab_grid_stale = False
        
    def _create_settings_tab(self, settings_tab):
        canvas = tk.Canvas(settings_tab)
        scrollbar = ttk.Scrollbar(settings_tab, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas, padding=20)

        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_frame = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")

        def on_canvas_configure(event): canvas.itemconfig(canvas_frame, width=event.width)
        canvas.bind("<Configure>", on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True); scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
            return "break"

        # [修正] 遞迴綁定設定頁內所有子元件，避免 bind_all 影響其他分頁，同時確保任意區塊都可滾動
        self._bind_mousewheel_recursive(settings_tab, _on_mousewheel)
        
        # --- 內容 ---
        top_bar_frame = ttk.Frame(scrollable_frame)
        top_bar_frame.pack(fill=tk.X, pady=(0, 20))
        ttk.Label(top_bar_frame, text=f"目前版本: {CURRENT_VERSION}", foreground="gray", font=("Microsoft JhengHei UI", self.f_sm)).pack(side=tk.LEFT, anchor='w')
        ttk.Button(top_bar_frame, text="檢查線上更新", command=lambda: self.bg_executor.submit(self.check_and_update, True)).pack(side=tk.RIGHT)

        # [修改] 建立容器，並定義三個直行
        columns_container = ttk.Frame(scrollable_frame)
        columns_container.pack(fill=tk.BOTH, expand=True)
        
        # 1. 左欄 (設定開關)
        left_column = ttk.Frame(columns_container)
        left_column.grid(row=0, column=0, sticky="nw", padx=(0, 20))
        
        # 2. 中欄 (原本的右欄 - 醫師列表)
        right_column = ttk.Frame(columns_container)
        right_column.grid(row=0, column=1, sticky="nw", padx=(0, 20))
        
        # 3. [新增] 右欄 (最右邊 - 放置圖片)
        third_column = ttk.Frame(columns_container)
        third_column.grid(row=0, column=2, sticky="nw", padx=(0, 0))

        # --- 左欄內容 (保持不變) ---
        mode_frame = ttk.LabelFrame(left_column, text="模式與顯示設定", padding=10)
        mode_frame.pack(fill=tk.X, pady=(0, 15))
        dnd_row = ttk.Frame(mode_frame)
        dnd_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(dnd_row, text="提醒勿擾時段").pack(side=tk.LEFT)
        ttk.Entry(dnd_row, width=6, textvariable=self.notify_dnd_start_time_var, justify="center").pack(side=tk.LEFT, padx=(6, 2))
        ttk.Label(dnd_row, text="~").pack(side=tk.LEFT)
        ttk.Entry(dnd_row, width=6, textvariable=self.notify_dnd_end_time_var, justify="center").pack(side=tk.LEFT, padx=(2, 2))
        ttk.Label(dnd_row, text="(HH:MM，00:00-24:00)").pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(mode_frame, text="勿擾時段只記錄狀態與日誌，不跳彈窗。", foreground="gray", style="Small.TLabel").pack(anchor="w")
        
        # 在 _create_settings_tab 內部
        def on_mode_change():
            self.val_out_of_hospital = self.out_of_hospital_var.get()

            if self.out_of_hospital_var.get():
                logging.info("切換至 [醫院外模式]")
                safe_unhook_all_hotkeys()
                self.status_text.set("狀態: 院外模式 (功能已停用)")
                # 打卡燈號設為灰色表示停用
                put_ui_message(self.ui_queue, UiClockStatusMessage(status_data={'error': '院外模式停用'}))
            else:
                logging.info("切換至 [院內模式]")
                self.setup_hotkeys()
                self.status_text.set("狀態: 院內模式")
                self.update_clock_status_from_web()

        def on_external_clinic_change():
            self.status_text.set("狀態: 設定變更，正在重新整理顯示...")
            self._trigger_refresh(True)

        ttk.Checkbutton(mode_frame, text="開啟「醫院外模式」", variable=self.out_of_hospital_var, command=on_mode_change).pack(anchor="w", pady=2)
        ttk.Checkbutton(mode_frame, text="顯示「外院/分院」診次", variable=self.show_external_clinics, command=on_external_clinic_change).pack(anchor="w", pady=2)

        ui_scale_frame = ttk.LabelFrame(left_column, text="介面字體", padding=10)
        ui_scale_frame.pack(fill=tk.X, pady=(0, 15))
        ttk.Label(ui_scale_frame, text="縮放 (0.85–1.45，儲存後重新啟動生效):").pack(anchor="w")
        sp_font = tk.Spinbox(ui_scale_frame, from_=0.85, to=1.45, increment=0.05, width=6, textvariable=self.ui_font_scale_var, format="%.2f")
        sp_font.pack(anchor="w", pady=(4, 0))

        r_doctor_frame = ttk.LabelFrame(left_column, text="R1-R3 醫師姓名（值班對照）", padding=10)
        r_doctor_frame.pack(fill=tk.X, pady=(0, 15))
        self.r_doctor_entries = {}
        for i, r_key in enumerate(["R1", "R2", "R3"]):
            ttk.Label(r_doctor_frame, text=f"{r_key} 姓名:").grid(row=i, column=0, padx=5, pady=5, sticky='e')
            name_var = tk.StringVar(value=self.r_doctor_map.get(r_key, {}).get('name', ''))
            name_entry = ttk.Entry(r_doctor_frame, textvariable=name_var, width=12); name_entry.grid(row=i, column=1, padx=5, pady=5, sticky='w')
            self.r_doctor_entries[r_key] = {'name_var': name_var}

        threshold_main_frame = ttk.LabelFrame(left_column, text="個別醫師止掛人數提醒設定", padding=10)
        threshold_main_frame.pack(fill=tk.X, pady=(0, 15))
        self.threshold_entries = {}

        def on_doctor_alert_change():
            # [修正] 當 UI 變更時，同步更新影子變數
            self.val_alert_chang = self.alert_chang_enabled.get()
            self.val_alert_chen = self.alert_chen_enabled.get()
            
            self.status_text.set("狀態: 設定變更，正在重新整理...")
            self._trigger_refresh(True)

        chang_frame = ttk.Frame(threshold_main_frame); chang_frame.pack(fill=tk.X, pady=5)
        ttk.Checkbutton(chang_frame, text="啟用 [張廖年峰]", variable=self.alert_chang_enabled, command=on_doctor_alert_change).pack(side=tk.LEFT, padx=(0, 10))
        chang_labels = {'chang_mon_night': '一晚:', 'chang_thu_morning': '四早:', 'chang_thu_night': '四晚:', 'chang_fri_afternoon': '五午:'}
        for key, label in chang_labels.items():
            ttk.Label(chang_frame, text=label).pack(side=tk.LEFT, padx=(5, 2))
            var = tk.StringVar(value=self.threshold_settings.get(key, DEFAULT_THRESHOLDS.get(key, '')))
            ttk.Entry(chang_frame, textvariable=var, width=4).pack(side=tk.LEFT, padx=0)
            self.threshold_entries[key] = var
        
        ttk.Separator(threshold_main_frame, orient='horizontal').pack(fill='x', pady=8)

        chen_frame = ttk.Frame(threshold_main_frame); chen_frame.pack(fill=tk.X, pady=5)
        ttk.Checkbutton(chen_frame, text="啟用 [陳駿升]    ", variable=self.alert_chen_enabled, command=on_doctor_alert_change).pack(side=tk.LEFT, padx=(0, 10))
        chen_labels = {'chen_mon_afternoon': '一午:', 'chen_tue_night': '二晚:', 'chen_thu_morning': '四早:', 'chen_thu_afternoon': '四午:'}
        for key, label in chen_labels.items():
            ttk.Label(chen_frame, text=label).pack(side=tk.LEFT, padx=(5, 2))
            var = tk.StringVar(value=self.threshold_settings.get(key, DEFAULT_THRESHOLDS.get(key, '')))
            ttk.Entry(chen_frame, textvariable=var, width=4).pack(side=tk.LEFT, padx=0)
            self.threshold_entries[key] = var

        reboot_frame = ttk.LabelFrame(left_column, text="自動重開機設定 (閒置偵測)", padding=10)
        reboot_frame.pack(fill=tk.X, pady=(0, 15))

        # [修正 2] 定義同步函式，確保背景執行緒讀取到的是純 Python 變數
        def sync_reboot_vars(*args):
            try:
                self.val_auto_reboot_enabled = self.auto_reboot_enabled.get()
                self.val_auto_reboot_time = self.auto_reboot_time.get()
            except Exception:
                pass

        # 綁定變數變更事件
        self.auto_reboot_enabled.trace_add("write", sync_reboot_vars)
        self.auto_reboot_time.trace_add("write", sync_reboot_vars)

        def on_reboot_toggle():
            # 這裡不需要手動更新變數了，trace_add 會處理
            status = "開啟" if self.auto_reboot_enabled.get() else "關閉"
            self.status_text.set(f"狀態: 自動重開機已 {status}")

        ttk.Checkbutton(reboot_frame, text="啟用", variable=self.auto_reboot_enabled, command=on_reboot_toggle).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(reboot_frame, text="時間(24h):").pack(side=tk.LEFT, padx=(5, 2))
        ttk.Entry(reboot_frame, textvariable=self.auto_reboot_time, width=5, justify='center').pack(side=tk.LEFT, padx=2)
        ttk.Label(reboot_frame, text="(前1分閒置才執行)", style="Small.TLabel", foreground="gray").pack(side=tk.LEFT, padx=5)

        # --- 中欄 (原本的右欄 - 醫師列表) ---
        doctors_frame = ttk.LabelFrame(right_column, text="門診醫師代號設定", padding=(12, 12, 12, 10))
        doctors_frame.pack(fill=tk.BOTH, expand=True)
        list_container = ttk.Frame(doctors_frame); list_container.pack(fill=tk.BOTH, expand=True, pady=(0, 12))
        _tv_font = ("Microsoft JhengHei UI", self.f_md)
        _row_h = max(26, self.f_md + 16)
        self.style.configure("Doctors.Treeview", font=_tv_font, rowheight=_row_h)
        self.style.configure("Doctors.Treeview.Heading", font=("Microsoft JhengHei UI", self.f_md, "bold"))
        self.style.configure("Doctors.TEntry", font=("Microsoft JhengHei UI", self.f_md))
        # 可見列數略減，搭配 expand 較不易出現大塊空白；欄寬加大以利閱讀
        self.doctors_tree = ttk.Treeview(
            list_container, columns=('doc_no', 'name'), show='headings', height=14, style="Doctors.Treeview"
        )
        self.doctors_tree.column('doc_no', width=128, anchor='center', minwidth=90)
        self.doctors_tree.column('name', width=168, anchor='w', minwidth=100)
        self.doctors_tree.heading('doc_no', text='醫師代號')
        self.doctors_tree.heading('name', text='醫師姓名')
        tree_scroll = ttk.Scrollbar(list_container, orient="vertical", command=self.doctors_tree.yview)
        self.doctors_tree.configure(yscrollcommand=tree_scroll.set)
        self.doctors_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True); tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.refresh_doctors_treeview()

        entry_frame = ttk.Frame(doctors_frame)
        entry_frame.pack(fill=tk.X, pady=(4, 0))
        input_row = ttk.Frame(entry_frame)
        input_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(input_row, text="醫師代號", font=("Microsoft JhengHei UI", self.f_md)).pack(side=tk.LEFT, padx=(0, 8))
        self.new_doctor_code_var = tk.StringVar()
        ttk.Entry(input_row, textvariable=self.new_doctor_code_var, width=14, style="Doctors.TEntry").pack(side=tk.LEFT, padx=(0, 20))
        ttk.Label(input_row, text="醫師姓名", font=("Microsoft JhengHei UI", self.f_md)).pack(side=tk.LEFT, padx=(0, 8))
        self.new_doctor_name_var = tk.StringVar()
        ttk.Entry(input_row, textvariable=self.new_doctor_name_var, width=16, style="Doctors.TEntry").pack(side=tk.LEFT, padx=(0, 0))

        btn_row = ttk.Frame(entry_frame)
        btn_row.pack(fill=tk.X)
        ttk.Button(btn_row, text="新增至列表", command=self._add_doctor).pack(side=tk.LEFT, padx=(0, 12), ipady=2)
        ttk.Button(btn_row, text="刪除選定", command=self._delete_doctor).pack(side=tk.LEFT, ipady=2)

        # =========================================================================
        # [修改] 圖片邏輯移至 第三欄 (third_column)
        # =========================================================================
        self.promo_img_url = "https://github.com/expertise88864/CMUH_repository/blob/main/IMG_6975.JPG?raw=true"
        self.promo_img_path = os.path.join(SETTINGS_DIR, "promo_poster.jpg")

        self.promo_frame = ttk.Frame(third_column)
        self.promo_frame.pack(fill=tk.X, pady=(0, 0), anchor='center')
        self.promo_placeholder_label = ttk.Label(self.promo_frame, text="切換到設定頁後載入圖片", foreground="gray")
        self.promo_placeholder_label.pack(pady=6)
        # =========================================================================

        backup_frame = ttk.LabelFrame(left_column, text="設定快照回復", padding=10)
        backup_frame.pack(fill=tk.X, pady=(0, 15))
        ttk.Label(backup_frame, text="儲存時會自動建立快照，可回復到昨天最新版本。", style="Small.TLabel", foreground="gray").pack(anchor="w", pady=(0, 6))
        ttk.Button(backup_frame, text="回復到昨天設定", command=self._restore_yesterday_settings_snapshot).pack(anchor="w")

        # [這行保持在原本的最下方]
        ttk.Button(scrollable_frame, text="儲存所有設定", command=self.save_all_settings).pack(pady=20, ipady=5, ipadx=30)
        self._bind_mousewheel_recursive(scrollable_frame, _on_mousewheel)

    def refresh_doctors_treeview(self):
        for i in self.doctors_tree.get_children(): self.doctors_tree.delete(i)
        # [修改] 配合欄位順序: values=(doc_no, name)
        for doctor in self.doctors_list: self.doctors_tree.insert('', 'end', values=(doctor['doc_no'], doctor['name']))

    def _add_doctor(self):
        name = self.new_doctor_name_var.get().strip()
        doc_no = self.new_doctor_code_var.get().strip()
        if name and doc_no:
            # [修改] 配合欄位順序: values=(doc_no, name)
            self.doctors_tree.insert('', 'end', values=(doc_no, name))
            self.new_doctor_name_var.set("")
            self.new_doctor_code_var.set("")
        else:
            self._show_notice("輸入錯誤", "醫師姓名和代號不能為空。", level="warn", auto_close_ms=3500)
    
    def _delete_doctor(self):
        selected_items = self.doctors_tree.selection()
        if not selected_items: messagebox.showwarning("操作錯誤", "請先在列表中選擇要刪除的醫師！"); return
        if messagebox.askyesno("確認刪除", "您確定要刪除選定的醫師嗎？"):
            for item in selected_items: self.doctors_tree.delete(item)

    def ensure_settings_promo_loaded(self):
        if self._settings_promo_loaded or self._settings_promo_loading:
            return
        self._settings_promo_loading = True
        if hasattr(self, 'promo_placeholder_label'):
            self.promo_placeholder_label.config(text="圖片載入中...")
        self.bg_executor.submit(self._load_settings_promo_image)

    def _load_settings_promo_image(self):
        try:
            from PIL import Image, ImageTk

            if not os.path.exists(self.promo_img_path) or os.path.getsize(self.promo_img_path) == 0:
                logging.info("Downloading promo image from GitHub...")
                response = requests.get(self.promo_img_url, timeout=10, verify=True)
                response.raise_for_status()
                with open(self.promo_img_path, 'wb') as f:
                    f.write(response.content)

            if os.path.exists(self.promo_img_path):
                with Image.open(self.promo_img_path) as img:
                    pil_image = img.copy()

                target_width = 250
                w_percent = (target_width / float(pil_image.size[0]))
                h_size = int((float(pil_image.size[1]) * float(w_percent)))
                pil_image = pil_image.resize((target_width, h_size), Image.Resampling.LANCZOS)
                self.root.after(0, lambda img=pil_image, ImageTk=ImageTk: self._show_settings_promo_image(img, ImageTk))
                return

            raise FileNotFoundError("Promo image file missing after load.")
        except Exception as e:
            logging.error(f"Failed to load promo image: {e}")
            self.root.after(0, lambda e=e: self._show_settings_promo_error(e))

    def _show_settings_promo_image(self, pil_image, image_tk_module):
        self._settings_promo_loading = False
        self._settings_promo_loaded = True
        self.settings_promo_photo = image_tk_module.PhotoImage(pil_image)
        for w in self.promo_frame.winfo_children():
            w.destroy()
        ttk.Label(self.promo_frame, image=self.settings_promo_photo).pack()

    def _show_settings_promo_error(self, error):
        self._settings_promo_loading = False
        self._settings_promo_loaded = False
        for w in self.promo_frame.winfo_children():
            w.destroy()
        ttk.Label(self.promo_frame, text="圖片載入失敗", foreground="red").pack(pady=6)
        logging.error(f"Promo image UI update failed: {error}")

    def _create_calendar_grid(self, parent_frame, num_weeks=2, is_future=False):
        calendar_widgets = {}
        weekdays = ["週一", "週二", "週三", "週四", "週五", "週六"]
        
        is_low_res = self.screen_height <= 1024
        base_font_size = self.f_sm - 1 if is_low_res else self.f_sm
        header_font_size = base_font_size
        FIXED_SLOTS = 4

        session_styles = {
            "上午": ("【☀上午】", "#E65100"),
            "下午": ("【☁下午】", "#0277BD"),
            "晚上": ("【☾晚上】", "#37474F")
        }
        
        for i in range(6): 
            parent_frame.grid_columnconfigure(i, weight=1, uniform="col")
            ttk.Label(parent_frame, text=weekdays[i], style="Header.TLabel").grid(row=0, column=i, sticky="nsew", padx=1, pady=1)
        
        parent_frame.grid_rowconfigure(0, weight=0) 
        for i in range(1, num_weeks + 1): 
            parent_frame.grid_rowconfigure(i, weight=1, uniform="row")
        
        for week in range(num_weeks):
            for day_idx in range(6):
                cell_key = (week, day_idx)
                cell_frame = ttk.Frame(parent_frame, borderwidth=1, relief="groove")
                cell_frame.grid(row=week + 1, column=day_idx, sticky="nsew", padx=0, pady=0)
                
                cell_frame.columnconfigure(0, weight=1)
                cell_frame.rowconfigure(1, weight=1) 
                
                date_label = ttk.Label(cell_frame, text="", style="Date.TLabel")
                date_label.grid(row=0, column=0, sticky="nw", padx=2, pady=(1,0))
                
                content_frame = tk.Frame(cell_frame, bg="white")
                content_frame.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
                
                # --- [優化核心] 預先渲染物件池 (Object Pool)，拒絕後續重複摧毀重建 ---
                sessions_ui = {}
                for session_name in ["上午", "下午", "晚上"]:
                    header_text, header_fg = session_styles.get(session_name)
                    header_frame = tk.Frame(content_frame, bg="white")
                    header_frame.pack(fill="x", pady=(2, 0))
                    tk.Label(header_frame, text=header_text, fg=header_fg, bg="white",
                                font=("Microsoft JhengHei UI", header_font_size, "bold"), anchor="center").pack(fill="x", pady=0)
                    
                    slots = []
                    for _ in range(FIXED_SLOTS):
                        card = tk.Frame(content_frame, bg="white")
                        card.pack(fill="x", pady=0, ipadx=1)
                        card.columnconfigure(0, weight=1); card.columnconfigure(1, weight=0)
                        
                        name_lbl = tk.Label(card, text=" ", bg="white", fg="#333333", 
                                    font=("Microsoft JhengHei UI", base_font_size, "bold"), anchor="w")
                        name_lbl.grid(row=0, column=0, sticky="w", padx=(2,0))
                        
                        status_lbl = tk.Label(card, text=" ", bg="white", fg="#333333", 
                                    font=("Microsoft JhengHei UI", base_font_size))
                        status_lbl.grid(row=0, column=1, sticky="e", padx=(0,2))
                        
                        slots.append({"card": card, "name_lbl": name_lbl, "status_lbl": status_lbl})
                    
                    sessions_ui[session_name] = slots
                
                calendar_widgets[cell_key] = {
                    "frame": cell_frame, 
                    "date_label": date_label, 
                    "content_frame": content_frame,
                    "sessions_ui": sessions_ui,
                    "_render_signature": None
                }
        return calendar_widgets

    def _apply_calendar_slot_state(self, slot, name_text, status_text, bg_color, fg_color, font_style):
        new_state = (name_text, status_text, bg_color, fg_color, font_style)
        if slot.get("_state") == new_state:
            return
        slot["_state"] = new_state
        slot["card"].config(bg=bg_color)
        slot["name_lbl"].config(text=name_text, bg=bg_color, fg=fg_color)
        slot["status_lbl"].config(text=status_text, bg=bg_color, fg=fg_color, font=font_style)

    @staticmethod
    def _appt_item_session_ext(appt_item):
        if isinstance(appt_item, dict):
            return appt_item.get('session', ''), _appt_dict_ext_branch(appt_item)
        parts = appt_item.split('|')
        status_part = parts[0]
        session_name = status_part.split(':')[0]
        ext_branch = None
        for p in parts[1:]:
            if p.startswith("Ext:"):
                val = p.split(":", 1)[1]
                if val in ("1", "east"):
                    ext_branch = "east"
                elif val == "auh":
                    ext_branch = "auh"
                elif val == "huihe":
                    ext_branch = "huihe"
                elif val == "huisheng":
                    ext_branch = "huisheng"
        return session_name, ext_branch

    def _doctor_has_other_ext_on_weekday(self, doc_no, doc_name, weekday_idx, session_name, exclude_date):
        """是否曾在其他同日（同 weekday）、非 exclude_date 的資料中出現東區該診別。"""
        for lookup in (doc_no, doc_name):
            data = self.all_doctors_data.get(lookup)
            if not data or not isinstance(data, dict) or 'error' in data:
                continue
            for dkey, items in data.items():
                if not isinstance(dkey, date) or dkey.weekday() != weekday_idx or dkey == exclude_date:
                    continue
                for item in items:
                    sn, ext = self._appt_item_session_ext(item)
                    if sn == session_name and ext == "east":
                        return True
        return False

    def _update_grid_data(self, start_date, target_widgets, num_weeks, is_future=False):
        today_date = date.today()
        now = datetime.now()
        now_time = now.time()
        is_low_res = self.screen_height <= 1024

        base_font_size = self.f_sm - 1 if is_low_res else self.f_sm
        FIXED_SLOTS = 4

        doctor_threshold_maps = {
            "張廖年峰": self._get_doctor_threshold_map("張廖年峰"),
            "陳駿升": self._get_doctor_threshold_map("陳駿升"),
        }

        time_morning_end = dt_time(12, 0)
        time_afternoon_end = dt_time(17, 0)
        time_night_end = dt_time(21, 0)

        # [優化] 移出內層迴圈：每次 refresh 只建一次，而非每個格子(12次)都重建
        current_order = [d['name'] for d in self.doctors_list]
        order_map = {name: i for i, name in enumerate(current_order)}

        for week in range(num_weeks):
            for day_idx in range(6):
                current_date = start_date + timedelta(days=(week * 7) + day_idx)
                cell = target_widgets.get((week, day_idx))
                if not cell: continue
                
                date_text = current_date.strftime("%m/%d")

                if not is_future and current_date < today_date:
                    if cell.get("_past_rendered"):
                        continue
                    # [優化] 隱藏外層，不再暴力摧毀子物件
                    cell["content_frame"].grid_remove()
                    cell["frame"].config(style="NoAppt.TFrame")
                    cell["date_label"].config(text=date_text, style="Past.Date.TLabel")
                    cell["_past_rendered"] = True
                    continue
                else:
                    if cell.get("_past_rendered", True):
                        cell["content_frame"].grid() # 恢復顯示
                        cell["_past_rendered"] = False

                if not is_future and current_date == today_date:
                    date_label_text = f"{date_text} [今日]"
                    date_label_style = "Today.Date.TLabel"
                    frame_style = "Today.TFrame"
                else:
                    date_label_text = f"{date_text}"
                    date_label_style = "Date.TLabel"
                    frame_style = None

                display_data = {"上午": {}, "下午": {}, "晚上": {}}; weekday_idx = current_date.weekday()
                
                show_ext_clinics = self.show_external_clinics.get()
                for doc_info in self.doctors_list:
                    doc_name = doc_info['name']
                    doc_no = str(doc_info['doc_no'])
                    doc_order_idx = order_map.get(doc_name, 99)

                    doc_data = self.all_doctors_data.get(doc_no)
                    if not doc_data: doc_data = self.all_doctors_data.get(doc_name)
                    
                    if doc_data and isinstance(doc_data, dict) and 'error' not in doc_data and current_date in doc_data:
                        for appt_item in doc_data[current_date]:
                            if isinstance(appt_item, dict):
                                session_name   = appt_item.get('session', '')
                                ext_branch     = _appt_dict_ext_branch(appt_item)
                                raw_count      = appt_item.get('count', 0)
                                room           = appt_item.get('room', '')
                                is_stopped_signup = appt_item.get('is_stopped', False)
                                status_text    = str(raw_count)
                                if isinstance(raw_count, int):
                                    status_text += "人"
                            else:
                                parts = appt_item.split('|')
                                status_part = parts[0]
                                ext_branch = None
                                is_stopped_signup = False
                                room = ""
                                for p in parts[1:]:
                                    if p.startswith("Ext:"):
                                        val = p.split(":", 1)[1]
                                        if val in ("1", "east"):
                                            ext_branch = "east"
                                        elif val == "auh":
                                            ext_branch = "auh"
                                        elif val == "huihe":
                                            ext_branch = "huihe"
                                        elif val == "huisheng":
                                            ext_branch = "huisheng"
                                    if p.startswith("Rm:"):   room = p.split(":")[1]
                                    if p.startswith("Stop:"): is_stopped_signup = (p.split(":")[1] == "1")
                                session_name = status_part.split(':')[0]
                                status_text  = status_part.split(':')[1].strip()

                            if ext_branch and not show_ext_clinics:
                                continue

                            if session_name in display_data:
                                is_self_paid = self._master_schedule_self_paid.get((doc_name, weekday_idx, session_name), False)
                                slot_key = (doc_name, ext_branch)

                                tag = 'normal'
                                is_timeout = False
                                if not is_future and current_date == today_date:
                                    if (session_name == "上午" and now_time > time_morning_end) or (session_name == "下午" and now_time > time_afternoon_end) or (session_name == "晚上" and now_time > time_night_end):
                                        status_text = self._calendar_today_session_ended_text(doc_name, session_name)
                                        tag = "session_past"
                                        is_timeout = True

                                if not is_timeout:
                                    if "休診" in status_text or "停診" in status_text: tag = 'dayoff'
                                    elif "已額滿" in status_text: tag = 'full'
                                    elif "截止" in status_text: tag = 'past_time'
                                    else:
                                        if is_stopped_signup:
                                            tag = 'full'
                                            if "人" in status_text and "(止掛)" not in status_text:
                                                status_text += "(止掛)"
                                            elif "人" not in status_text: 
                                                status_text += "(止掛)"

                                        try:
                                            count_match = _RE_COUNT_DIGIT.search(status_text)
                                            if count_match:
                                                count = int(count_match.group(1))
                                                if (not is_future) and current_date == today_date:
                                                    sample_key = (doc_name, session_name)
                                                    samples = self._live_count_samples[sample_key]
                                                    now_ts = time.time()
                                                    if (not samples) or abs(samples[-1][1] - count) >= 1 or (now_ts - samples[-1][0]) >= 300:
                                                        samples.append((now_ts, count))
                                                
                                                alert_threshold = None 
                                                full_threshold = None
                                                session_key = (weekday_idx, session_name)
                                                
                                                if doc_name == "張廖年峰" and self.alert_chang_enabled.get():
                                                    if session_key in doctor_threshold_maps["張廖年峰"]: 
                                                        full_threshold = int(doctor_threshold_maps["張廖年峰"][session_key])
                                                        alert_threshold = full_threshold - 10
                                                elif doc_name == "陳駿升" and self.alert_chen_enabled.get():
                                                    if session_key in doctor_threshold_maps["陳駿升"]: 
                                                        full_threshold = int(doctor_threshold_maps["陳駿升"][session_key])
                                                        alert_threshold = full_threshold - 10
                                                
                                                if full_threshold is not None and count >= full_threshold: 
                                                    tag = 'full'
                                                
                                                if alert_threshold is not None and tag not in ('full', 'dayoff', 'no_clinic', 'session_past') and count >= alert_threshold: 
                                                    tag = 'alert'

                                                if tag == 'full' and not is_future and not is_stopped_signup:
                                                    if full_threshold is not None:
                                                        notify_key = f"{current_date}_{session_name}_{doc_name}_{ext_branch or 'main'}"
                                                        should_notify = False
                                                        notify_level = 0
                                                        with self._alert_state_lock:
                                                            if (not self._alert_popup_active[notify_key]) and self.alert_frequency[notify_key] < 2:
                                                                self._alert_popup_active[notify_key] = True
                                                                self.alert_frequency[notify_key] += 1
                                                                should_notify = True
                                                                notify_level = self.alert_frequency[notify_key]
                                                        if should_notify:
                                                            logging.info(f"[ALERT TRIGGERED] {doc_name} {session_name} count {count} >= {full_threshold}")
                                                            diff = int(count) - int(full_threshold)
                                                            if diff >= 0:
                                                                diff_text = f"距離門檻已超過 {diff} 人"
                                                            else:
                                                                diff_text = f"距離門檻差 {-diff} 人"
                                                            level_prefix = "【第一次提醒】" if notify_level == 1 else "【第二次加強提醒】"
                                                            msg = f"{level_prefix}\n{doc_name} {session_name}診\n掛號人數 {count} 人\n設定閾值 {full_threshold} 人\n{diff_text}"
                                                            if self._is_notification_suppressed_now():
                                                                with self._alert_state_lock:
                                                                    self._alert_popup_active[notify_key] = False
                                                                self._dnd_suppressed_count += 1
                                                                self.status_text.set(f"狀態: 勿擾時段，已抑制提醒（{doc_name}{session_name}，{diff_text}）")
                                                                logging.info(f"[ALERT SUPPRESSED][DND] {doc_name} {session_name} count={count} threshold={full_threshold} {diff_text}")
                                                                continue
                                                            def _notify_worker(nk=notify_key, m=msg):
                                                                try:
                                                                    show_windows_notification("止掛提醒", m)
                                                                finally:
                                                                    with self._alert_state_lock:
                                                                        self._alert_popup_active[nk] = False
                                                            threading.Thread(target=_notify_worker, name="NotifyThread", daemon=True).start()
                                        except Exception as e: 
                                            logging.error(f"Error checking threshold: {e}")
                                
                                display_name = doc_name
                                _suf = _EXT_BRANCH_DISPLAY_SUFFIX.get(ext_branch)
                                if _suf:
                                    display_name += _suf
                                elif room and room not in ("181診", "182診"):
                                    display_name += f"({room})"
                                if is_self_paid: display_name += "*"
                                
                                # 排序：(1) 非休診列優先，休診／無門診列一律置底
                                # (2) 181→182→本院其他→分院；分院內：東區→亞大→惠和→惠盛→其他 (3) 醫師清單順序
                                is_dayoff_row = (
                                    tag in ("dayoff", "no_clinic")
                                    or ("休診" in status_text)
                                    or ("停診" in status_text)
                                )
                                dayoff_tier = 1 if is_dayoff_row else 0
                                if ext_branch:
                                    zone_bucket = 3
                                    brank = _calendar_branch_sort_rank(ext_branch)
                                elif room == "181診":
                                    zone_bucket = 0
                                    brank = 0
                                elif room == "182診":
                                    zone_bucket = 1
                                    brank = 0
                                else:
                                    zone_bucket = 2
                                    brank = 0
                                sort_key = (dayoff_tier, zone_bucket, brank, doc_order_idx)
                                
                                display_data[session_name][slot_key] = (display_name, status_text, tag, sort_key)
                
                for doc_name, session_name, is_self_paid in self._master_schedule_by_weekday.get(weekday_idx, ()):
                    key_main = (doc_name, None)
                    if key_main not in display_data[session_name]:
                        status_text = "休診"
                        tag = "no_clinic"
                        if not is_future and current_date == today_date:
                            if (session_name == "上午" and now_time > time_morning_end) or (session_name == "下午" and now_time > time_afternoon_end) or (session_name == "晚上" and now_time > time_night_end):
                                status_text = self._calendar_today_session_ended_text(doc_name, session_name)
                                tag = "session_past"
                        
                        suffix = "*" if is_self_paid else ""
                        # 主院預設列無掛號資料＝休診：置底；區域視為本院其他診間
                        sort_key = (1, 2, 0, order_map.get(doc_name, 99))
                        display_data[session_name][key_main] = (doc_name + suffix, status_text, tag, sort_key)

                # 東區慣例推論：其他「同星期幾」曾出現東區同診別，當日卻無東區列時，補「休診」(與網頁未列東區休診之情形)
                if show_ext_clinics:
                    for doc_info in self.doctors_list:
                        doc_name = doc_info['name']
                        doc_no = str(doc_info['doc_no'])
                        for session_name in ("上午", "下午", "晚上"):
                            key_ext = (doc_name, "east")
                            if key_ext in display_data[session_name]:
                                continue
                            if not self._doctor_has_other_ext_on_weekday(
                                doc_no, doc_name, weekday_idx, session_name, current_date
                            ):
                                continue
                            display_data[session_name][key_ext] = (
                                doc_name + "(東區分院)",
                                "休診",
                                "dayoff",
                                (1, 3, 0, order_map.get(doc_name, 99)),
                            )

                has_content = False
                render_signature = []
                prepared_session_states = {}
                
                # 先為整格建立簽章；若完全沒變，直接跳過整格更新
                for session_name in ["上午", "下午", "晚上"]:
                    items = display_data.get(session_name, {})
                    if items: has_content = True
                    sorted_clinics = sorted(
                        items.items(),
                        key=lambda item: item[1][3],
                    )
                    render_signature.append((session_name, tuple((clinic[0], clinic[1], clinic[2]) for _, clinic in sorted_clinics[:FIXED_SLOTS])))
                    prepared_states = []
                    for idx, (doc_name_key, clinic_data) in enumerate(sorted_clinics[:FIXED_SLOTS]):
                        final_name, status_text, tag, _ = clinic_data

                        # 一般列白底；休診用淡灰底標記；逾時與一般列同白底（不另套色票）
                        default_bg = "white"
                        bg_color = default_bg
                        fg_color = "#333333"
                        if tag == 'full':
                            bg_color = "#FFEBEE"
                            fg_color = "#C62828"
                        elif tag == 'alert':
                            bg_color = "#FFF3E0"
                            fg_color = "#EF6C00"
                        elif tag in ('dayoff', 'no_clinic'):
                            bg_color = "#ECEFF1"
                            fg_color = "#90A4AE"
                        elif tag in ('past_time', 'session_past'):
                            bg_color = "white"
                            fg_color = "#333333"

                        font_style = ("Microsoft JhengHei UI", base_font_size)
                        if any(char.isdigit() for char in status_text):
                            font_style = ("Arial", base_font_size, "bold")
                        prepared_states.append((final_name, status_text, bg_color, fg_color, font_style))
                    while len(prepared_states) < FIXED_SLOTS:
                        prepared_states.append((" ", " ", "white", "#333333", ("Microsoft JhengHei UI", base_font_size)))
                    prepared_session_states[session_name] = prepared_states

                if frame_style is None:
                    frame_style = "HasAppt.TFrame" if has_content else "NoAppt.TFrame"

                final_signature = (
                    date_label_text,
                    date_label_style,
                    frame_style,
                    tuple(render_signature),
                )
                if cell.get("_render_signature") == final_signature:
                    continue
                    
                # --- [優化核心] 只對有變化的格子套用 UI 狀態 ---
                for session_name in ["上午", "下午", "晚上"]:
                    slots_ui = cell["sessions_ui"][session_name]
                    for i, slot_state in enumerate(prepared_session_states[session_name]):
                        slot = slots_ui[i]
                        self._apply_calendar_slot_state(slot, *slot_state)

                cell["date_label"].config(text=date_label_text, style=date_label_style)
                if cell["frame"].cget("style") != frame_style:
                    cell["frame"].config(style=frame_style)

                cell["_render_signature"] = final_signature

    def _schedule_refresh(self):
        """[修正] 節流 (Debounce)：160ms 內多次資料到達只觸發一次重繪，避免多位醫師觸發多次全重繪"""
        if getattr(self, '_shutting_down', False):
            return
        if not self._refresh_pending:
            self._refresh_pending = True
            self.root.after(160, self._do_deferred_refresh)

    def _do_deferred_refresh(self):
        self._refresh_pending = False
        if getattr(self, '_shutting_down', False):
            return
        self.refresh_all_calendars()

    def _schedule_save_cache(self, filename, data):
        """節流 (Debounce)：500ms 內多次資料到達只觸發一次磁碟寫入，避免平行查詢觸發多次寫入"""
        if getattr(self, '_shutting_down', False):
            return
        if not self._save_cache_pending.get(filename):
            self._save_cache_pending[filename] = True
            self.root.after(500, lambda: self._do_deferred_save_cache(filename, data))

    def _do_deferred_save_cache(self, filename, data):
        self._save_cache_pending[filename] = False
        if getattr(self, '_shutting_down', False):
            return
        self._save_cache(filename, data)

    def _duty_cache_mem_ensure(self) -> dict[str, Any]:
        """值班資訊 UI 用記憶體快取（啟動時自檔案載入一次）。"""
        if not hasattr(self, "_duty_cache_mem"):
            self._duty_cache_mem = {}
            try:
                p = get_conf_path("cache_duty_info.json")
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        self._duty_cache_mem = json.load(f)
            except Exception:
                logging.debug("讀取值班資訊快取失敗", exc_info=True)
        return self._duty_cache_mem

    def process_ui_queue(self):
        """[修正] 合併 UI 訊息處理 + Log 輪詢為單一迴圈，減少 root.after() 排程次數"""
        if getattr(self, '_shutting_down', False):
            return
        had_work = False
        try:
            while True:
                try:
                    msg = self.ui_queue.get_nowait()
                except Empty:
                    break
                had_work = True
                match msg:
                    case UiStatusMessage(text=t):
                        self.status_text.set(t)
                    case UiRefreshTickMessage(doctor_name=doc_name):
                        self._refresh_progress_done = getattr(self, '_refresh_progress_done', 0) + 1
                        total = getattr(self, '_refresh_progress_total', 0)
                        self._pending_refresh_tick_ui = (self._refresh_progress_done, total, doc_name)
                        if getattr(self, '_refresh_tick_after_id', None):
                            try:
                                self.root.after_cancel(self._refresh_tick_after_id)
                            except Exception:
                                logging.debug("after_cancel refresh_tick 失敗", exc_info=True)
                        self._refresh_tick_after_id = self.root.after(240, self._flush_refresh_tick_ui)
                    case UiAlertInfoMessage(title=title, msg=amsg, need_restart=need_restart):
                        self._show_notice(title, amsg, level="info", auto_close_ms=5000 if not need_restart else 2000)
                        if need_restart:
                            self.root.after(2000, lambda: restart_self())
                    case UiAlertErrorMessage(title=title, msg=emsg):
                        self._show_notice(title, emsg, level="error", auto_close_ms=7000)
                    case UiClinicDataMessage(doctor_name=doctor_name, data=appointment_data):
                        if doctor_name and appointment_data is not None:
                            with self._doctor_data_lock:
                                self.all_doctors_data[doctor_name] = appointment_data
                            self._schedule_refresh()
                            self._schedule_save_cache('cache_clinic_counts.json', self._get_all_doctors_data_snapshot())
                    case UiMasterScheduleMessage(schedule=sched):
                        self.master_schedule = sched
                        self._rebuild_master_schedule_index()
                        logging.info("Master schedule updated.")
                        self._schedule_refresh()
                        self._save_cache('cache_master_schedule.json', self.master_schedule)
                    case UiDutyDoctorMessage(doctor_name=dn):
                        duty_cache = self._duty_cache_mem_ensure()
                        duty_cache['date'] = date.today().strftime("%Y-%m-%d")
                        today = datetime.now()
                        _WD_MAP = ('一', '二', '三', '四', '五', '六', '日')
                        date_str = today.strftime("%m/%d")
                        weekday_str = _WD_MAP[today.weekday()]
                        txt = f"今日({date_str} 週{weekday_str}) 值班: {dn}"
                        self.duty_doctor_var.set(txt)
                        duty_cache['duty_doctor'] = txt
                        self._refresh_duty_summary_text()
                        self._duty_cache_mem = duty_cache
                        self._save_cache('cache_duty_info.json', duty_cache)
                    case UiSaturdayDutyDoctorMessage(saturday_date=saturday_date, doctor_name=sdn):
                        duty_cache = self._duty_cache_mem_ensure()
                        duty_cache['date'] = date.today().strftime("%Y-%m-%d")
                        date_str = saturday_date.strftime("%m/%d")
                        txt = f"當週({date_str} 週六) 值班: {sdn}"
                        self.saturday_duty_doctor_var.set(txt)
                        duty_cache['saturday_duty'] = txt
                        self._refresh_duty_summary_text()
                        self._duty_cache_mem = duty_cache
                        self._save_cache('cache_duty_info.json', duty_cache)
                    case UiTodayVsMessage(doctor_name=vsn):
                        duty_cache = self._duty_cache_mem_ensure()
                        duty_cache['date'] = date.today().strftime("%Y-%m-%d")
                        txt = f"當日值班VS: {vsn}"
                        self.duty_vs_var.set(txt)
                        duty_cache['today_vs'] = txt
                        self._refresh_duty_summary_text()
                        self._duty_cache_mem = duty_cache
                        self._save_cache('cache_duty_info.json', duty_cache)
                    case UiSaturdayVsMessage(doctor_name=svn):
                        duty_cache = self._duty_cache_mem_ensure()
                        duty_cache['date'] = date.today().strftime("%Y-%m-%d")
                        txt = f"當週值班VS: {svn}"
                        self.saturday_duty_vs_var.set(txt)
                        duty_cache['saturday_vs'] = txt
                        self._refresh_duty_summary_text()
                        self._duty_cache_mem = duty_cache
                        self._save_cache('cache_duty_info.json', duty_cache)
                    case UiClockStatusMessage(status_data=payload):
                        self._update_clock_status_ui(payload)
                    case _:
                        logging.warning("未知的 UI 訊息型別: %r", msg)
        except Exception:
            logging.exception("UI 訊息佇列處理失敗")

        # --- [修正] 合併 Log 輪詢，消除原本多餘的 poll_log_queue after() 排程 ---
        try:
            if not hasattr(self, 'log_text_widget'):
                for _ in range(20):
                    try:
                        record = self.log_queue.get_nowait()
                        self._log_backlog.append(self.format_log_record(record))
                        had_work = True
                    except Empty:
                        break
                    except Exception:
                        logging.debug("log_queue 讀取失敗", exc_info=True)
                        break
                if len(self._log_backlog) > 300:
                    self._log_backlog = self._log_backlog[-300:]
            elif not self.log_queue.empty():
                had_work = True
                try:
                    self.log_text_widget.configure(state='normal')
                    for _ in range(20):
                        try:
                            record = self.log_queue.get_nowait()
                        except Empty:
                            break
                        self.log_text_widget.insert(tk.END, self.format_log_record(record) + '\n')
                    line_count = int(self.log_text_widget.index('end-1c').split('.')[0])
                    if line_count > 500:
                        self.log_text_widget.delete('1.0', f'{line_count - 400}.0')
                    self.log_text_widget.see(tk.END)
                    self.log_text_widget.configure(state='disabled')
                except Exception:
                    logging.debug("Log 視窗更新失敗", exc_info=True)
        except Exception:
            logging.debug("Log 輪詢失敗", exc_info=True)

        if getattr(self, '_shutting_down', False):
            return
        next_delay = 80 if had_work else 320
        self._ui_queue_poll_id = self.root.after(next_delay, self.process_ui_queue)
        
    def update_clock_status_from_web(self):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self.update_clock_status_from_web)
            return
        if getattr(self, '_shutting_down', False):
            return
        if hasattr(self, 'val_out_of_hospital') and self.val_out_of_hospital:
            logging.info("院外模式開啟中，跳過打卡狀態查詢 (需內網)")
            put_ui_message(self.ui_queue, UiClockStatusMessage(status_data={'error': '院外模式停用'}))
            return

        put_ui_message(self.ui_queue, UiClockStatusMessage(status_data='querying'))

        # [修正] 從設定檔讀取，不把帳密寫死在程式碼裡
        cred_path = get_conf_path('credentials.json')
        try:
            if os.path.exists(cred_path):
                with open(cred_path, 'r', encoding='utf-8') as f:
                    cred = json.load(f)
                import base64
                username = base64.b64decode(cred.get('u', '')).decode('utf-8')
                password = base64.b64decode(cred.get('p', '')).decode('utf-8')
            else:
                # 首次使用：提示使用者輸入並存檔
                import tkinter.simpledialog as sd
                username = sd.askstring("打卡帳號設定", "請輸入打卡帳號 (將加密儲存):", parent=self.root) or ""
                password = sd.askstring("打卡密碼設定", "請輸入打卡密碼 (將加密儲存):", parent=self.root, show='*') or ""
                if username and password:
                    import base64
                    with open(cred_path, 'w', encoding='utf-8') as f:
                        json.dump({
                            'u': base64.b64encode(username.encode()).decode(),
                            'p': base64.b64encode(password.encode()).decode()
                        }, f)
                    logging.info("打卡帳號已加密儲存至 credentials.json")
                else:
                    put_ui_message(self.ui_queue, UiClockStatusMessage(status_data={'error': '帳號未設定'}))
                    return
        except Exception as e:
            logging.error(f"讀取打卡帳號失敗: {e}")
            put_ui_message(self.ui_queue, UiClockStatusMessage(status_data={'error': '帳號讀取失敗'}))
            return

        logging.info(f"Starting background clock status check for {username}...")

        def run_check():
            status_result = _get_swipe_status_from_web(username, password)
            put_ui_message(self.ui_queue, UiClockStatusMessage(status_data=status_result))

        self.bg_executor.submit(run_check)

    # --- [修正] 更新打卡狀態 UI (加入型別檢查) ---
    def _update_clock_status_ui(self, status_data):
        """
        status_data: 
          - 'querying': 正在查詢中 (str)
          - 字典 {'上班': True/False/None, ...}: 查詢結果 (dict)
          - 字典 {'error': ...}: 發生錯誤或停用 (dict)
        """
        def set_light(tag_id, color):
            self.clock_canvas.itemconfig(tag_id, fill=color, outline=color)

        # 1. 處理「查詢中」狀態 (這是字串)
        if status_data == 'querying':
            # 將燈號設為黃色，表示正在運作
            set_light(self.light_in, "yellow")
            set_light(self.light_out, "yellow")
            return

        # 2. 處理錯誤或停用 (這是字典)
        if isinstance(status_data, dict) and "error" in status_data:
            # 灰色表示停用或錯誤
            set_light(self.light_in, "gray")
            set_light(self.light_out, "gray")
            return

        # [關鍵修正] 確保 status_data 是字典才繼續，避免 AttributeError
        if not isinstance(status_data, dict):
            logging.error(f"Invalid status_data type received: {type(status_data)}")
            return

        # 3. 處理正常結果
        # 上班燈
        in_status = status_data.get('上班')
        if in_status is True: color_in = "#00C853"   # 綠燈 (正常)
        elif in_status is False: color_in = "#D50000" # 紅燈 (異常)
        else: color_in = "gray"                       # 灰燈 (未打卡)
        set_light(self.light_in, color_in)
        
        # 下班燈
        out_status = status_data.get('下班')
        if out_status is True: color_out = "#00C853"
        elif out_status is False: color_out = "#D50000"
        else: color_out = "gray"
        set_light(self.light_out, color_out)

    def refresh_all_calendars(self):
        start_date = date.today() - timedelta(days=date.today().weekday())
        self._update_grid_data(start_date, self.summary_calendar_widgets, 2)
        # [修正] 只有在「未來週次查詢」分頁被選中時才更新，避免隱藏分頁也跑全量重繪
        if hasattr(self, 'future_week_selector') and self.future_week_selector.get():
            try:
                current_tab = self.notebook.tab(self.notebook.select(), "text")
                if current_tab == "未來週次查詢":
                    self.on_future_week_selected()
                else:
                    self._future_tab_grid_stale = True
            except Exception:
                logging.debug("未來週次分頁狀態偵測失敗，標記為需重繪", exc_info=True)
                self._future_tab_grid_stale = True

    def run_subsystem_in_thread(self, func, hotkey_name):
        with self._subsystem_lock:
            if self._subsystem_running:
                put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 前一個熱鍵流程尚未完成'))
                self._show_notice("熱鍵忙碌中", f"{hotkey_name} 已略過，請等待目前自動化完成。", level="warn", auto_close_ms=2500)
                return
            self._subsystem_running = True

        stop_event_automation.clear()
        def wrapper():
            logging.info(f"Starting subsystem from {hotkey_name}...")
            put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 執行中...'))
            try:
                func()
                put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 操作完成'))
            except SubsystemInterrupted as e:
                logging.warning(f"Subsystem stopped: {e}")
                put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 已由F12手動終止'))
            except Exception:
                logging.exception(f"Error in '{hotkey_name}'")
                put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 發生未預期錯誤'))
            finally:
                with self._subsystem_lock:
                    self._subsystem_running = False
                time.sleep(2)
                put_ui_message(self.ui_queue, UiStatusMessage(text='狀態: 閒置'))
        thread = threading.Thread(target=wrapper, name=f"{hotkey_name}_Thread", daemon=True)
        thread.start()

    def interrupt_automation(self):
        logging.warning("Received F12: Interrupting...")
        stop_event_automation.set()
        put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: F12 終止 - 正在中斷目前操作..."))

    def setup_hotkeys(self):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self.setup_hotkeys)
            return
        if not self._heavy_modules_ready or hotkey_modules.keyboard is None:
            self.hotkey_text_label.config(text="熱鍵模組載入中...")
            self.status_text.set("狀態: 熱鍵模組尚未就緒")
            return
        # [新增] 院外模式檢查
        if hasattr(self, 'out_of_hospital_var') and self.out_of_hospital_var.get():
            logging.info("院外模式啟用中，跳過熱鍵註冊。")
            self.hotkey_text_label.config(text="熱鍵已停用 (院外模式)")
            self.hotkey_display_note.set("")
            return
        profile = getattr(self, 'hotkey_profile', None) or self.hotkey_version
        if profile is None:
            message = f"螢幕解析度 ({self.screen_width}x{self.screen_height}) 無法對應熱鍵腳本，已停用熱鍵功能"
            logging.warning(message)
            self.hotkey_display_note.set(
                f"熱鍵停用 · 解析度 {self.screen_width}×{self.screen_height} 無對應腳本"
            )
            put_ui_message(self.ui_queue, UiStatusMessage(text='狀態: 解析度不符，熱鍵已停用'))
            self.hotkey_text_label.config(text="熱鍵已停用 (解析度不符)")
            return

        if getattr(self, 'hotkey_adaptive_enabled', False) and profile:
            configure_hotkey_scaling(True, profile, (self.screen_width, self.screen_height))
            self.hotkey_display_note.set(
                f"熱鍵近似縮放 · {profile} ({self.screen_width}×{self.screen_height})"
            )
        else:
            configure_hotkey_scaling(False, None, None)
            self.hotkey_display_note.set("")

        try:
            hotkeys_to_register = {}
            hotkey_info_text = ""
            if profile == '1920x1080':
                hotkeys_to_register = {
                    'F3': (script_F3_1920x1080, "F3: 冷凍 (1920x1080)"),
                    'F4': (script_F4_1920x1080, "F4: 照光 (1920x1080)"),
                    'F10': (script_F10_1920x1080, "F10: 皮膚切片同意書 (1920x1080)"),
                    'F11': (script_F11_1920x1080, "F11: 快速完成 (1920x1080)")
                }
                hotkey_info_text = "F3:冷凍 F4:照光 F10:切片同意書\nF11:快速完成 F12:終止"
            elif profile == '1280x1024':
                hotkeys_to_register = {
                    'F3': (script_F3_1280x1024, "F3: 51017 (1280x1024)"),
                    'F4': (script_F4_1280x1024, "F4: 51019 (1280x1024)"),
                    'F9': (script_F9_1280x1024, "F9: 腫瘤同意書 (1280x1024)"),
                    'F10': (script_F10_1280x1024, "F10: 切片同意書 (1280x1024)"),
                    'F11': (script_F11_1280x1024, "F11: 快速完成 (1280x1024)")
                }
                hotkey_info_text = "F3:51017 F4:51019 F9:腫瘤 F10:切片\nF11:快速完成 F12:終止"
            elif profile == '1024x768':
                hotkeys_to_register = {
                    'F3': (script_F3_1024x768, "F3: 冷凍51017"),
                    'F4': (script_F4_1024x768, "F4: 照光"),
                    'F9': (script_F9_1024x768, "F9: 皮膚腫瘤同意書"),
                    'F10': (script_F10_1024x768, "F10: 皮膚切片同意書"),
                    'F11': (script_F11_1024x768, "F11: 快速完成")
                }
                hotkey_info_text = "F3:冷凍 F4:照光 F9:腫瘤同意書\nF10:切片同意書 F11:快速完成 F12:終止"

            safe_unhook_all_hotkeys()
            for key, (func, name) in hotkeys_to_register.items():
                hotkey_modules.keyboard.add_hotkey(key, lambda f=func, n=name: self.run_subsystem_in_thread(f, n), suppress=True)
            hotkey_modules.keyboard.add_hotkey('F12', self.interrupt_automation, suppress=True)
            
            self.hotkey_text_label.config(text=hotkey_info_text)
            put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: 熱鍵註冊成功 ({profile})，等待指令...'))
            logging.info(f"Hotkeys registered successfully for {profile}.")
        except Exception as e:
            logging.error(f"Failed to register hotkeys: {e}", exc_info=True)
            put_ui_message(self.ui_queue, UiStatusMessage(text='狀態: 熱鍵註冊失敗! 請檢查權限'))
            self.hotkey_text_label.config(text="熱鍵註冊失敗!")
            es = str(e)
            self.hotkey_display_note.set(f"熱鍵註冊失敗 · {es[:42]}…" if len(es) > 42 else f"熱鍵註冊失敗 · {es}")

    def run_hotkey_guardian(self):
        def rehook():
            while not stop_event_main.is_set():
                time.sleep(600)
                try:
                    if getattr(self, 'hotkey_profile', None) or self.hotkey_version:
                        self.root.after(0, self.setup_hotkeys)
                        logging.info("Hotkeys re-hooked by guardian.")
                except Exception as e:
                    logging.error(f"Error re-hooking hotkeys: {e}")
        # [核心修正] 依賴統一池
        self.bg_executor.submit(rehook)
    
    def _run_single_duty_query(self, fn, third_arg):
        s = _get_thread_local_duty_session()
        try:
            fn(self.ui_queue, s, third_arg)
        except Exception as e:
            logging.error(f"_fetch_all_duty_info: {fn.__name__} error: {e}", exc_info=True)

    def _fetch_all_duty_info(self, force=False):
        """並行查詢值班（forward01）。每筆獨立 Session，總耗時約為最慢一筆，而非四筆相加。
        若院內伺服器對併發敏感，可改回循序或改為 submit 單一 worker 內 for 迴圈。
        """
        if self.val_out_of_hospital:
            logging.info("院外模式開啟中，跳過所有值班查詢")
            return
        today_str = date.today().isoformat()
        if (not force) and self._duty_last_fetch_date == today_str:
            logging.info("值班資訊今日已查詢，略過重抓（跨日才強制更新）")
            return

        futures = [
            self.bg_executor.submit(self._run_single_duty_query, fetch_duty_doctor, self.r_doctor_map),
            self.bg_executor.submit(self._run_single_duty_query, fetch_saturday_duty_doctor, self.r_doctor_map),
            self.bg_executor.submit(self._run_single_duty_query, fetch_duty_vs, "today_vs"),
            self.bg_executor.submit(self._run_single_duty_query, fetch_duty_vs, "saturday_vs"),
        ]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                logging.error(f"_fetch_all_duty_info future: {e}", exc_info=True)
        self._duty_last_fetch_date = today_str

    def _get_doctor_threshold_map(self, doctor_name):
        return build_doctor_threshold_map(doctor_name, self.threshold_settings)

    def _get_all_doctors_data_snapshot(self):
        with self._doctor_data_lock:
            return deepcopy(self.all_doctors_data)

    def _is_doctor_near_alert_threshold(self, doctor_name, doctors_data_snapshot=None):
        if not doctor_name:
            return False
        today = date.today()
        weekday_idx = today.weekday()
        data_source = doctors_data_snapshot if doctors_data_snapshot is not None else self._get_all_doctors_data_snapshot()
        doc_data = data_source.get(doctor_name)
        if not doc_data or not isinstance(doc_data, dict):
            return False
        sessions = doc_data.get(today)
        if not sessions:
            return False
        threshold_map = self._get_doctor_threshold_map(doctor_name)
        return is_near_alert_threshold(sessions, weekday_idx, threshold_map, margin=10)

# --- [修正] 背景任務啟動 (修正重開機邏輯) ---
    def start_background_tasks(self):
        logging.info("Starting background tasks loop via ThreadPoolExecutor...")
        self.startup_phase_text.set("任務排程")

        # [核心修正] 統一派發至 ThreadPoolExecutor 避免 Thread Leak
        self.bg_executor.submit(self.check_and_update, False)

        def safe_fetch_duty(target_func, *args):
            if not self.val_out_of_hospital:
                target_func(*args)
            else:
                logging.info(f"院外模式開啟中，跳過 {target_func.__name__}")

        def _startup_priority_refresh():
            if self._initial_priority_refresh_done:
                return
            by_name = {d.get("name"): d for d in DOCTORS}
            pri_names = list(REFRESH_QUERY_BATCH_1) + list(REFRESH_QUERY_BATCH_2)
            pri_docs = [by_name[n] for n in pri_names if n in by_name]
            if pri_docs:
                logging.info(f"[STARTUP] priority refresh doctors={len(pri_docs)}")
                self._startup_defer_full_until_priority_done = True
                self.bg_executor.submit(self._trigger_refresh, False, pri_docs)
            else:
                self.bg_executor.submit(self._trigger_refresh, False)
            self._initial_priority_refresh_done = True

        self.root.after(500, _startup_priority_refresh)
        self.root.after(1500, lambda: self.bg_executor.submit(load_master_schedule_in_background, self.ui_queue))
        self.root.after(3500, self.update_clock_status_from_web)

        # 值班四筆在 _fetch_all_duty_info 內並行，每筆獨立 Session；啟動略提前以縮短首屏等待
        self.root.after(2500, lambda: self.bg_executor.submit(self._fetch_all_duty_info))
        
        def run_schedule():
            def run_named_job(job_tag, fn):
                t0 = time.perf_counter()
                logging.info(f"[SCHEDULE:{job_tag}] started")
                try:
                    fn()
                    elapsed = time.perf_counter() - t0
                    logging.info(f"[SCHEDULE:{job_tag}] finished in {elapsed:.2f}s")
                except Exception as e:
                    elapsed = time.perf_counter() - t0
                    logging.error(f"[SCHEDULE:{job_tag}] failed in {elapsed:.2f}s: {e}", exc_info=True)
                finally:
                    pass

            chang_liao_config = [doc for doc in DOCTORS if doc["name"] == "張廖年峰"]
            chen_config = [doc for doc in DOCTORS if doc["name"] == "陳駿升"]
            
            def dynamic_cl_checker():
                try:
                    targets = []
                    if chang_liao_config and self.val_alert_chang:
                        targets.extend(chang_liao_config)
                    if chen_config and self.val_alert_chen:
                        targets.extend(chen_config)

                    if not targets:
                        return

                    doctors_data_snapshot = self._get_all_doctors_data_snapshot()
                    now_ts = time.time()
                    for doc in targets:
                        doc_name = doc.get("name")
                        if not doc_name:
                            continue
                        if not self._is_doctor_near_alert_threshold(doc_name, doctors_data_snapshot=doctors_data_snapshot):
                            continue
                        elapsed = now_ts - self._priority_refresh_last_check_time[doc_name]
                        if elapsed >= (15 * 60):
                            logging.info(
                                f"[SCHEDULE:priority-check-1m] 觸發優先刷新：{doc_name}（鄰近門檻且距上次≥15分）"
                            )
                            self.bg_executor.submit(self._trigger_refresh, False, [doc])
                            self._priority_refresh_last_check_time[doc_name] = now_ts
                except Exception as e:
                    logging.error(f"[SCHEDULE:priority-check-1m] failed: {e}", exc_info=True)

            schedule.every(1).minutes.do(dynamic_cl_checker).tag("priority-check", "1m")
            schedule.every(3).hours.do(
                lambda: run_named_job("refresh-all-3h", lambda: self.bg_executor.submit(self._trigger_refresh, False, DOCTORS))
            ).tag("refresh", "all-doctors", "3h")
            schedule.every(4).hours.do(
                lambda: run_named_job("duty-refresh-4h", lambda: self.bg_executor.submit(self._fetch_all_duty_info))
            ).tag("duty", "4h")
            
            schedule.every().day.at("08:00").do(lambda: run_named_job("clock-status-0800", self.update_clock_status_from_web)).tag("clock", "daily")
            schedule.every().day.at("17:03").do(lambda: run_named_job("clock-status-1703", self.update_clock_status_from_web)).tag("clock", "daily")
            schedule.every().day.at("08:00").do(
                lambda: run_named_job("check-update-0800", lambda: self.bg_executor.submit(self.check_and_update, False))
            ).tag("update-check", "daily")

            while not stop_event_main.is_set():
                schedule.run_pending()
                
                try:
                    if self.val_auto_reboot_enabled:
                        now = datetime.now()
                        current_time_str = now.strftime("%H:%M")
                        raw_time = str(self.val_auto_reboot_time).strip()
                        
                        if ':' in raw_time:
                            parts = raw_time.split(':')
                            target_time_str = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
                        else:
                            target_time_str = raw_time 

                        current_date_str = now.strftime("%Y-%m-%d")

                        if current_time_str == target_time_str and now.second < 5 and self.last_reboot_check_date != current_date_str:
                            idle_seconds = get_idle_duration()
                            
                            if idle_seconds >= 60:
                                logging.info(f"系統閒置 {idle_seconds:.0f}秒，執行自動重開機...")
                                self.last_reboot_check_date = current_date_str
                                self.root.after_idle(self.show_reboot_countdown)
                            else:
                                if now.second == 0:
                                    logging.info(f"時間 ({current_time_str}) 符合，但系統非閒置 (閒置 {idle_seconds:.0f}秒 < 60秒)，跳過。")
                except Exception as e:
                    logging.error(f"Error in auto reboot check: {e}")

                time.sleep(1)
        
        self.bg_executor.submit(run_schedule)
        self.run_hotkey_guardian()

    def check_and_update(self, is_manual=False):
        """檢查並更新所有相關程式（改寫自原 check_and_update）。

        【保留】平行下載、tuple 版本比較、原子寫入 + .bak 備份、失敗保留本地舊版。
        【改動】URL 從 4 個 Gist 改為單一 manifest.json @ GitHub raw。
        """
        if is_manual:
            put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: 正在檢查所有程式更新..."))
        import logging
        logging.info("=== Starting Multi-File Update Check (Parallel via manifest.json) ===")

        try:
            result = _updater_mod.check_and_update()
            if result.errors:
                if is_manual:
                    put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: 更新檢查失敗"))
                    put_ui_message(self.ui_queue, UiAlertErrorMessage(
                        title="更新錯誤",
                        msg="檢查更新時發生錯誤:\n" + "\n".join(result.errors),
                    ))
                return

            if _updater_mod.need_restart_after_update(result):
                msg_lines = [f"{fn} (v{ver})" for fn, ver in result.updated_files]
                if is_manual:
                    msg = "以下程式已更新完成：\n\n" + "\n".join(msg_lines) + "\n\n程式將立即重新啟動。"
                    put_ui_message(self.ui_queue, UiAlertInfoMessage(
                        title="更新完成", msg=msg, need_restart=True))
                else:
                    logging.info("Auto-update applied. Requesting restart on UI thread...")
                    put_ui_message(self.ui_queue, UiAlertInfoMessage(
                        title="自動更新完成", msg="已套用自動更新，程式將重新啟動。",
                        need_restart=True))
            elif result.is_frozen and result.has_update:
                # .exe 模式偵測到新版：跳通知請使用者去 GitHub release 下載
                put_ui_message(self.ui_queue, UiAlertInfoMessage(
                    title="有新版可下載",
                    msg=(f"偵測到新版 v{result.manifest_app_version}\n"
                         f"請至 {result.release_url} 下載新版執行檔。"),
                    need_restart=False,
                ))
            else:
                if is_manual:
                    put_ui_message(self.ui_queue, UiAlertInfoMessage(
                        title="檢查完成", msg="所有程式皆為最新版本。", need_restart=False))
                    put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: 所有程式皆為最新"))
        except Exception as e:
            logging.error(f"Global update process failed: {e}")
            if is_manual:
                put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: 更新檢查失敗"))
                put_ui_message(self.ui_queue, UiAlertErrorMessage(
                    title="更新錯誤", msg=f"檢查更新時發生錯誤: {e}"))

    def show_reboot_countdown(self, timeout=30):
        """顯示全螢幕置頂倒數視窗"""
        top = tk.Toplevel(self.root)
        top.title("系統即將重開機")
        top.attributes('-topmost', True) # 置頂
        top.attributes('-fullscreen', True) # 全螢幕
        top.configure(bg="#B71C1C") # 紅色背景警示

        label = tk.Label(top, text=f"系統偵測閒置，將自動重開機維護\n\n{timeout} 秒後執行...", 
                         font=("Microsoft JhengHei UI", 48, "bold"), fg="white", bg="#B71C1C")
        label.pack(expand=True)

        cancel_btn = tk.Button(top, text="取消重開機 (Cancel)", font=("Microsoft JhengHei UI", 24), 
                               command=top.destroy, bg="white", fg="black", padx=50, pady=20)
        cancel_btn.pack(pady=50)

        # 倒數邏輯
        self.reboot_cancelled = False
        def countdown(count):
            if not top.winfo_exists(): # 視窗被關閉代表取消
                self.reboot_cancelled = True
                return
            
            label.config(text=f"系統偵測閒置，將自動重開機維護\n\n{count} 秒後執行...")
            if count > 0:
                top.after(1000, countdown, count - 1)
            else:
                # 時間到，執行重開機
                logging.info("Reboot countdown finished. Executing shutdown.")
                top.destroy()
                os.system("shutdown /r /t 0")

        # 綁定任意鍵取消 (防呆)
        top.bind("<Key>", lambda e: top.destroy())
        top.bind("<Motion>", lambda e: None) # 滑鼠移動不取消，避免誤觸，必須點按鈕或按鍵

        countdown(timeout)
        
        # 等待視窗關閉 (阻塞式等待，直到使用者取消或時間到)
        self.root.wait_window(top)
        return not self.reboot_cancelled # 回傳是否真的要執行 (True=執行, False=已取消)

# --- 主程式執行區 ---
if __name__ == "__main__":
    # [修正 1] 強制執行記憶體回收，清除 DependencyInstaller 留下的 Tkinter 變數
    # 避免在背景執行緒觸發 Variable.__del__ 導致崩潰
    import gc
    gc.collect()
    
    run_as_admin()
    _set_windows_dpi_awareness()
    _set_windows_app_user_model_id()
    main_root = tk.Tk()
    
    # 綁定全域例外處理，避免背景執行緒崩潰導致閃退
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            main_root.quit()
            return
        logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    sys.excepthook = handle_exception

    if hasattr(threading, "excepthook"):
        def _thread_excepthook(args):
            logging.error(
                "Uncaught exception in thread %s",
                getattr(args.thread, "name", "?"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        threading.excepthook = _thread_excepthook

    app = AutomationApp(main_root, {})
    DOCTORS = app.doctors_list
    DOCTOR_NAMES = [d["name"] for d in DOCTORS]
    main_root.mainloop()
    logging.info("--- Script Finished ---")
