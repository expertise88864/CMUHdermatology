# -*- coding: utf-8 -*-
# =============================================================================
# 由 scripts/transform_pyw.py 自動生成。
# 重構自 _originals/中國醫皮膚科主程式.pyw
# 共用基底已抽出至 cmuh_common/，本檔僅保留業務邏輯（UI、抓網、熱鍵等）。
# =============================================================================
# [perf r5] PEP 563：讓所有型別註解(含函式簽章的 `session: "_RequestsSession"`)變成字串、
# 延後求值。這樣才能把重量級的 requests/urllib3/bs4 import 從模組頂層(splash 之前)延後到
# AutomationApp.__init__(splash 之後)，加快感知啟動。本檔無 get_type_hints/dataclass，
# 不會被 PEP 563 影響。
from __future__ import annotations

import os
import sys

# 把 src/ 加到 sys.path，讓 cmuh_common / network / hotkey / ui / clock 子套件可用
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# === cmuh_common 共用基底 ===
from cmuh_common.version import CURRENT_VERSION
from cmuh_common.paths import (
    get_app_dir, get_settings_dir, get_conf_path, restart_self,
)
from cmuh_common.process_launch import launch_app_script
from cmuh_common.win32_safe import call_with_timeout, WIN_ENUM_TIMEOUT_SEC
from cmuh_common.atomic_io import atomic_write_json as _atomic_write_json
from cmuh_common.config_io import load_json_dict, load_json_list
from cmuh_common.app_settings import (
    load_doctors_settings as _load_doctors_settings,
    load_r_doctor_settings as _load_r_doctor_settings,
    load_threshold_settings as _load_threshold_settings,
)
from cmuh_common.cache_state import (
    build_master_schedule_index,
    decode_date_keys as _decode_cache_date_keys,
    save_json_cache,
)
from cmuh_common.master_schedule_cache import (
    load_master_schedule_cache,
    refresh_master_schedule_if_needed,
)
from cmuh_common.refresh_policy import (
    partition_doctors_for_refresh_batches as _partition_refresh_batches,
)
from cmuh_common.threshold_policy import (
    DEFAULT_THRESHOLDS,
    build_doctor_threshold_map,
    is_near_alert_threshold,
)
from cmuh_common.clinic_state import (
    CLINIC_ROOM_COUNT,
    DEFAULT_CLINIC_ROOMS,
    build_dynamic_state,
    clinic_dynamic_state_key,
    clinic_dynamic_today_str,
    matching_state_keys,
    new_clinic_tracker,
    normalize_clinic_rooms,
    prune_states_for_today,
    restore_tracker_from_state,
    state_matches,
)
from cmuh_common.clinic_history import (
    all_time_average_text,
    historical_duration_totals,
    last_closing_time as _history_last_closing_time,
    monthly_slot_metric_avgs as _history_monthly_slot_metric_avgs,
    prev_session_closing_clock as _history_prev_session_closing_clock,
    remove_doctor_history,
    upsert_session_stat,
)
from cmuh_common.clinic_light_history import (
    historical_light_average,
    record_light_sample,
)
from cmuh_common.platform_win import (
    run_as_admin, set_dpi_awareness, set_app_user_model_id,
    get_primary_monitor_size,
    place_tk_window_on_preferred_monitor,
)
from cmuh_common.notifications import (
    show_windows_notification, show_windows_notification_async, show_winotify_toast)
from cmuh_common.window_icon import apply_tk_window_icon as _apply_tk_window_icon
from cmuh_common.contract_canary import (
    BASELINE_FILENAME as _CANARY_BASELINE_FILENAME,
    ContractBaseline as _ContractBaseline,
    compare_fingerprint as _canary_compare,
)
from cmuh_common.action_ledger import (
    LEDGER_FILENAME as _LEDGER_FILENAME,
    OUTCOME_FAILED as _LEDGER_FAILED,
    OUTCOME_MISMATCH as _LEDGER_MISMATCH,
    OUTCOME_OK as _LEDGER_OK,
    OUTCOME_SKIPPED as _LEDGER_SKIPPED,
    SURFACE_HIS_FIELD as _LEDGER_HIS_FIELD,
    SURFACE_HIS_MENU as _LEDGER_HIS_MENU,
    ActionLedger as _ActionLedger,
)
from cmuh_common.logging_setup import attach_queue_handler
from cmuh_common.bounded_executor import BoundedThreadPoolExecutor, RejectedExecutionError
from cmuh_common.http_client import is_internal as _is_internal
from cmuh_common.ui_messages import (
    UiStatusMessage, UiRefreshTickMessage, UiClinicDataMessage, UiMasterScheduleMessage,
    UiDutyDoctorMessage, UiSaturdayDutyDoctorMessage, UiTodayVsMessage, UiSaturdayVsMessage,
    UiClockStatusMessage, UiAlertInfoMessage, UiAlertErrorMessage, UiMessage, put_ui_message,
)
from cmuh_common.deps_runtime import ensure_dependencies as _ensure_deps_runtime
from cmuh_common.single_instance import (
    ensure_single_instance, is_instance_running, release_single_instance,
)
from cmuh_common.duty_summary import build_duty_summary_parts
from cmuh_common.abbrev_engine import (
    AbbrevEngine,
    AbbrevConfig,
    DEFAULT_ITEMS as ABBREV_DEFAULT_ITEMS,
    MAX_ABBREV_LENGTH,
    ensure_config_file as ensure_abbrev_config_file,
    load_config as load_abbrev_config,
    save_config as save_abbrev_config,
    sort_abbrev_items,
)
# 【重構 2026-05-21】熱鍵座標縮放（原本 main.py 用 _scaled_xy 卻沒定義 — 潛在 NameError）
from cmuh_common.hotkey_scaling import (  # noqa: E402
    configure_hotkey_scaling,
    _scaled_xy,
)
from cmuh_common.hotkey_guardian import (
    GUARDIAN_INTERVAL_SEC,
    PROBE_VK,
    is_hook_probe_failure_confirmed,
    should_auto_restart_for_dead_hook,
    should_bypass_foreground_guard,
    should_emit_interrupt,
    should_emit_idle_status,
    should_probe_hook_health,
    should_show_busy_notice,
    system_idle_seconds,
)
# 【重構 2026-05-21】門診預約合併純函式（與 scheduler.py 共用）
from cmuh_common.appt_utils import (  # noqa: E402
    appointment_data_count as _appointments_data_count,
    _appt_dict_ext_branch,
    _calendar_branch_sort_rank,
    _strip_ext_appointments,
    _normalize_dayoff_session,
    _merge_appointments_by_date,
    _merge_dayoff_overrides,
)
from cmuh_common.memory_cache import trim_oldest_entries

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
    ("sv-ttk", "sv_ttk"),  # [UI 美化] Sun Valley 主題：原生 Win11 風格
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
from cmuh_common.update_policy import AUTO_UPDATE_CHECK_TIMES  # noqa: E402


# 【清理 2026-05-21】sys / os 已於檔首 import（line 7-8 為 sys.path 操作需要），這裡不重覆
import subprocess
import shutil
import threading
import time
import tkinter as tk
from weakref import WeakSet
from tkinter import filedialog, messagebox, scrolledtext, ttk

# =============================================================================
# [開機前導] 自動依賴安裝與進度條介面 (Dependency Installer UI)
# =============================================================================
import ctypes
from ctypes import wintypes
import logging
import random
import re
import schedule
import webbrowser
from collections import defaultdict, deque
from copy import deepcopy
import hashlib
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import date, datetime, timedelta, time as dt_time
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any, NotRequired, Optional, TypedDict

class DoctorConfig(TypedDict):
    name: str
    doc_no: str
    notifications: NotRequired[bool]


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
_RE_ROOM        = re.compile(r'\(([A-Za-z0-9]+診)\)')  # 診間號:含字母前綴(如 A101診)+純數字(101診)
                                                 # [2026-06-19] 原本只配 \d+診 → 漏掉含字母前綴的診間(如 A101診)→ 止掛信顯示「診間未提供」
_RE_COUNT_APPT  = re.compile(r'已掛號：(\d+)')   # 用於 check_appointment_count 掛號數
_RE_PERSON      = re.compile(r'(\d+)\s*人')      # 用於 check_appointment_count 人數
_RE_ROC_DATE    = re.compile(r'(\d{2,3})/(\d{2})/(\d{2})')

# 總覽門診表「本科主診間」:A101→A102→A103→A104→A105(自家固定診間)。語意:
#   1) 醫師列不另標這五間的診間號(免冗餘);其餘診間(他科借診/特殊)才顯示「(診間)」。
#   2) 排序依此序在最前 → 本院其他診間 → 分院最後。
# [2026-06-19] 院方診間改號(舊 181/182診 → A101/A102/A103診);此處只認門診表顯示用的實體診間字串。
# [2026-06-29] 本科固定診擴為 5 間,加入 A104診、A105診 → 這兩診醫師不再被當他科借診誤標「(診間)」、
#              排序緊接 A103 之後(與門診動態診間 101/102/103/104/105 一致)。
# 註:room 由 _RE_ROOM(ASCII 括號+英數)擷取,reg52 多年來都回半形(舊 \(\d+診\) 正常運作),
#     故此處直接 exact match;若實機出現大小寫/全形變體再依實際字串調整(同 _RE_ROOM 維護點)。
_OVERVIEW_PRIMARY_ROOMS = ("A101診", "A102診", "A103診", "A104診", "A105診")

# [O16] reg52 hot-path 預編譯：原本散落在函式內的 inline re.search/findall，集中宣告省 compile 開銷
_RE_REG52_DATE_CNT_PAIRS = re.compile(r'(\d{2,3}/\d{2}/\d{2})\s*已掛號[：:]\s*(\d+)')
_RE_CLINIC_DOCTOR        = re.compile(r"醫師[：:]\s*(\S+)")
_RE_CLINIC_CLOSED        = re.compile(r"\(已關診\)|（已關診）")
_RE_CLINIC_CLOSE_LINE    = re.compile(r"診間目前燈號\s*[：:]\s*\d+[^\n\r]*已關診")
_RE_CLINIC_END_TIME      = re.compile(r"應診時間[：:]\s*[\d]+\s*~\s*(\d{4})")
_RE_CLINIC_LIGHT_NUM     = re.compile(r"診間目前燈號\s*[：:]\s*(\d+)")
_RE_DIGITS_ONLY          = re.compile(r"\D")

# [O16] 預編譯 soupsieve CSS selector（hot path: _parse_main_hospital_schedule 每醫師呼叫 1 次）
# 用 lazy-init 避免 import 順序問題（soupsieve 在 bs4 之後 import）
_CSS_SELECTORS_CACHE: dict = {}


def _css(selector: str):
    """[O16] 取得已編譯的 soupsieve CSS selector（一次編譯，永久重用）。"""
    cached = _CSS_SELECTORS_CACHE.get(selector)
    if cached is not None:
        return cached
    try:
        import soupsieve  # type: ignore[import-untyped]
        compiled = soupsieve.compile(selector)
        _CSS_SELECTORS_CACHE[selector] = compiled
        return compiled
    except Exception:
        # soupsieve 不可用或編譯失敗 → fallback 回字串（呼叫 .select 時 bs4 會自己處理）
        _CSS_SELECTORS_CACHE[selector] = selector
        return selector

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


# [G] urllib3 「Connection pool is full」聚合：原本每次都印一筆 → 改每小時聚合
# urllib3.connectionpool 日誌等級 WARNING 印 "Connection pool is full, discarding..."
# 訊息。它意味著連線洩漏或 worker 太多。聚合避免洗 log，且每小時提示計數。
class _ConnPoolFullAggregator(logging.Filter):
    """攔截 urllib3.connectionpool 的 "Connection pool is full" 訊息。
    第 1 筆讓它過 (使用者看得到問題)，後續 1 小時內全部 swallow + 計數，
    過 1 小時印一筆 summary。
    """
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._first_seen = 0.0
        self._count_since_summary = 0
        self._last_summary = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "Connection pool is full" not in msg:
            return True
        now = time.time()
        with self._lock:
            self._count_since_summary += 1
            if self._first_seen == 0.0:
                self._first_seen = now
                return True  # 第一筆讓使用者看見
            if now - self._last_summary > 3600:
                # 過 1 小時，印一筆 summary
                logging.warning(
                    "[urllib3 pool] 過去 1 小時共 %d 筆「Connection pool is full」"
                    "已聚合 (原本每筆都會 print)。如果頻率過高 (>100/小時) "
                    "代表 worker 太多或連線洩漏",
                    self._count_since_summary)
                self._count_since_summary = 0
                self._last_summary = now
            return False  # swallow 重複的


# 【清理 2026-05-21】threading 已在檔首 import；try/except 是死分支
_cp_filter = _ConnPoolFullAggregator()
logging.getLogger("urllib3.connectionpool").addFilter(_cp_filter)

# [perf r5] 重量級網路相依(requests/urllib3/bs4 累計約 400-500ms)原本在此模組頂層、
# 也就是 splash 視窗出現「之前」同步 import → 使用者按下圖示後約 0.5s 畫面全黑無回饋。
# 改為延後到 AutomationApp.__init__ 開頭(splash 之後)才載入，讓「正在初始化…」提早數百 ms
# 出現。模組頂層與 def 簽章預設值都不使用這些名稱(只有函式 body 用，跑在 __init__ 之後)，
# 加上檔首 `from __future__ import annotations` 讓 `session: "_RequestsSession"` 註解變字串、
# def 時不求值，故可安全延後。以下先佔位為 None，由 _ensure_network_imports() 填入模組全域。
if TYPE_CHECKING:
    # 型別檢查期(pyright)：看到真實模組型別，讓 requests.Session()/BeautifulSoup() 等
    # 呼叫都能正確解析。執行期不在此 import。
    import requests
    from bs4 import BeautifulSoup
    from requests import Session as _RequestsSession
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
else:
    # 執行期佔位，由 _ensure_network_imports() 在 splash 之後才真正 import 並填入。
    requests = None
    BeautifulSoup = None
    HTTPAdapter = None
    Retry = None
_network_imports_ready = False


def _ensure_network_imports():
    """延後載入 requests/urllib3/bs4 並填入模組全域(冪等)。在 splash 顯示後、任何網路
    呼叫之前(AutomationApp.__init__ 開頭)呼叫一次。缺件時跳 MessageBox 並 sys.exit(1)
    (與原模組頂層 try/except 行為一致)。"""
    global requests, BeautifulSoup, HTTPAdapter, Retry, _network_imports_ready
    if _network_imports_ready:
        return
    try:
        import requests as _requests
        # --- [修正] SSL 驗證策略：只對已知院內主機關閉驗證，外部主機保持驗證 ---
        # 全域停用 verify=False 是安全漏洞。改為只對院內 IP/域名例外。
        from urllib3.exceptions import InsecureRequestWarning
        _requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
        from requests.adapters import HTTPAdapter as _HTTPAdapter
        from urllib3.util.retry import Retry as _Retry
        from bs4 import BeautifulSoup as _BeautifulSoup
    except ImportError as e:
        missing_module = str(e).split("'")[1] if "'" in str(e) else str(e)
        error_message = f"缺少必要的模組: {missing_module}\n\n請打開命令提示字元(cmd)並執行:\npip install {missing_module}"
        logging.critical(error_message)
        ctypes.windll.user32.MessageBoxW(0, error_message, "模組錯誤", 0x10)
        sys.exit(1)
    requests = _requests
    HTTPAdapter = _HTTPAdapter
    Retry = _Retry
    BeautifulSoup = _BeautifulSoup
    _network_imports_ready = True

# --- 3. 門診與醫師設定 ---
DOCTORS = []
DOCTOR_NAMES = []

GENERAL_ALERT_THRESHOLD = 60


REFRESH_QUERY_BATCH_1 = ("張廖年峰", "吳伯元", "陳駿升")
REFRESH_QUERY_BATCH_2 = ("謝佳陵", "方心禹", "沈冠宇")

def partition_doctors_for_refresh_batches(doctors):
    return _partition_refresh_batches(
        doctors,
        first_batch_names=REFRESH_QUERY_BATCH_1,
        second_batch_names=REFRESH_QUERY_BATCH_2,
    )

HOTKEY_SUPPORTED_RESOLUTIONS = ((1920, 1080), (1280, 1024), (1024, 768))
_HOTKEY_BASE_SIZE = {
    "1920x1080": (1920, 1080),
    "1280x1024": (1280, 1024),
    "1024x768": (1024, 768),
}
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
_reg52_external_tls = threading.local()
_duty_tls = threading.local()
_reg64_tls = threading.local()
# [v18 2026-05-25] 追蹤所有 thread-local sessions 給 atexit poolmanager.clear()
# 用。threading.local 本身不暴露跨 thread 的 session 給 main thread，所以額外
# 維護一個 set；建 session 時 add，atexit 時 clear adapter pool 強制斷連線。
# (不 call session.close() 避免等待未完成 request — 跟 _kill_orphan handler 同 pattern)
_all_reg_sessions: WeakSet = WeakSet()
_all_reg_sessions_lock = threading.Lock()
_ttl_cache_lock = threading.Lock()
_ttl_cache_store = {}
_parse_cache_store = {}
_source_backoff_state = {}
_source_throttle_state = {}
_reg52_cmuh_fetch_sema = threading.Semaphore(2)
_TTL_CACHE_MAX_ENTRIES = 512
_PARSE_CACHE_MAX_ENTRIES = 256
_SOURCE_STATE_MAX_ENTRIES = 128


# =============================================================================
# [O9] IPv4-only 連線（只對院外醫療系統 host 生效）
# 原因：Windows 預設 IPv4+IPv6 雙堆疊；院外（auh/east/huisheng）若 DNS 解析到
# IPv6 但實際不通，會先試 IPv6 失敗後再試 IPv4，造成每次連線多 2 秒延遲。
# 解法：對 IPV4_ONLY_HOSTS 內的 host 強制只用 IPv4。
# 安全：GitHub、Wikipedia 等仍使用預設雙堆疊（不影響其它連線）。
# =============================================================================
import socket as _socket
from urllib3.util import connection as _urllib3_conn  # type: ignore[import-untyped]

# 院外醫療系統（IPv6 通常不通；強制 IPv4 + 只試前 1 個 IP，失敗 2s 內告終）
IPV4_ONLY_HOSTS = {
    "appointment.auh.org.tw",
    "appointment.cmuh.org.tw",       # 主院
    "forward01.cmuh.org.tw",         # 值班查詢
    "administration.cmuh.org.tw",    # 院內行政
    "10.20.8.47",                     # 內網打卡
    "61.66.117.10",                   # 惠盛 hs1
    "www.cmuh.cmu.edu.tw",           # 院方主站（master schedule）
}

_orig_create_connection = _urllib3_conn.create_connection
_orig_getaddrinfo = _socket.getaddrinfo

# =============================================================================
# [O36] 來源級熔斷器（Circuit Breaker）
# 同個來源（east/auh/huisheng）連續失敗 N 次後暫停嘗試，避免「每 5 分鐘重複等
# 2 秒 timeout」的累積消耗。
# [2026-06-16 韌性] 改為「跳閘後逾 RESET 窗(30 分鐘)自動重置、放行一次重試」——
# 原本一旦跳閘要重啟程式才恢復:醫院端短暫維護(剛好 3 次失敗)就會讓該來源整個
# session(可能一整個下午)都沒資料,使用者只看到「無資料」卻不知是被熔斷。改為
# 定時自我恢復:來源復原就 success 清掉;仍掛則再累積跳閘,不會回到狂打 timeout。
# =============================================================================
# source_key → {"fails": int, "tripped_at": monotonic 或 None}
_CIRCUIT_BREAKER_STATE: dict[str, dict] = {}
_CIRCUIT_BREAKER_LOCK = threading.Lock()
_CIRCUIT_BREAKER_THRESHOLD = 3        # 連續 3 次失敗 → tripped
_CIRCUIT_BREAKER_RESET_SEC = 1800.0   # 跳閘逾 30 分鐘 → 自動重置,放行一次重試


def _circuit_record_fail(source: str) -> bool:
    """記錄失敗，回傳是否剛跳過閾值。"""
    with _CIRCUIT_BREAKER_LOCK:
        st = _CIRCUIT_BREAKER_STATE.setdefault(source, {"fails": 0, "tripped_at": None})
        st["fails"] += 1
        if st["fails"] == _CIRCUIT_BREAKER_THRESHOLD:
            st["tripped_at"] = time.monotonic()
            return True  # 剛跳閾
        return False


def _circuit_record_success(source: str) -> None:
    """成功 → 重置計數。"""
    with _CIRCUIT_BREAKER_LOCK:
        _CIRCUIT_BREAKER_STATE.pop(source, None)


def _circuit_is_tripped(source: str) -> bool:
    """是否仍熔斷中。跳閘逾 RESET 窗 → 自動重置並放行一次重試(回 False)。"""
    with _CIRCUIT_BREAKER_LOCK:
        st = _CIRCUIT_BREAKER_STATE.get(source)
        if not st or st["fails"] < _CIRCUIT_BREAKER_THRESHOLD:
            return False
        ta = st.get("tripped_at")
        if ta is not None and (time.monotonic() - ta) >= _CIRCUIT_BREAKER_RESET_SEC:
            _CIRCUIT_BREAKER_STATE.pop(source, None)
            logging.info("[circuit] 來源 %s 熔斷逾 %d 分鐘,自動重置重試",
                         source, int(_CIRCUIT_BREAKER_RESET_SEC // 60))
            return False
        return True


def _ipv4_first_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """[O35 關鍵修正] 對 IPV4_ONLY_HOSTS 的 host 限制 DNS 結果為「IPv4 + 只 1 個 IP」。

    為何要 patch socket.getaddrinfo（而非只 patch urllib3）：
      urllib3.connection.HTTPConnection 在 module load 時就 import 了
      create_connection，後來在 urllib3.util.connection 上的 monkey-patch 對它無效。
      但 socket.getaddrinfo 是底層函式，所有 DNS 解析最終都走它，patch 它最可靠。

    效果：當 host 在 IPV4_ONLY_HOSTS：
      - 只回 IPv4 結果（跳過 IPv6 嘗試）
      - 只回第 1 個 IP（避免 N IP × timeout 累積）
      → AUH/east 不通時 2 秒 fail，不再 21-42s
    """
    try:
        if isinstance(host, str) and host in IPV4_ONLY_HOSTS:
            results = _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)
            if results:
                return [results[0]]
            return results
    except Exception:
        pass
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


def _ipv4_aware_create_connection(address, *args, **kwargs):
    """[原方案，保留] 對 IPV4_ONLY_HOSTS 強制 AF_INET。"""
    try:
        host = address[0] if isinstance(address, tuple) else None
    except Exception:
        host = None
    if host and host in IPV4_ONLY_HOSTS:
        return _create_ipv4_connection(address, *args, **kwargs)
    return _orig_create_connection(address, *args, **kwargs)


def _create_ipv4_connection(address, *args, **kwargs):
    """socket 連線 wrapper：強制 AF_INET（IPv4 only）+ **只試前 1 個 IP**。

    【關鍵修正】DNS 解析常回傳多個 IP（CDN 5-10 個）。原版逐個試 IP，
    每個 timeout 2s × 10 個 = 20-40s 才失敗。改為只試前 1 個。
    """
    host, port = address
    addrs = _socket.getaddrinfo(host, port, _socket.AF_INET, _socket.SOCK_STREAM)
    if not addrs:
        raise OSError("getaddrinfo returns an empty list")
    # 只試第一個 IP（避免 N×timeout 累積）
    af, socktype, proto, _canonname, sa = addrs[0]
    sock = _socket.socket(af, socktype, proto)
    try:
        timeout = kwargs.get("timeout", _socket._GLOBAL_DEFAULT_TIMEOUT)
        if timeout is not _socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(timeout)
        source_addr = kwargs.get("source_address")
        if source_addr:
            sock.bind(source_addr)
        sock.connect(sa)
        return sock
    except OSError:
        try:
            sock.close()
        except OSError:
            pass
        raise


def _create_ipv4_connection_OLD(address, *args, **kwargs):
    """[棄用] 舊版逐個試 IP，留作 reference。"""
    host, port = address
    err = None
    for af, socktype, proto, _canonname, sa in _socket.getaddrinfo(
        host, port, _socket.AF_INET, _socket.SOCK_STREAM
    ):
        sock = None
        try:
            sock = _socket.socket(af, socktype, proto)
            timeout = kwargs.get("timeout", _socket._GLOBAL_DEFAULT_TIMEOUT)
            if timeout is not _socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            source_addr = kwargs.get("source_address")
            if source_addr:
                sock.bind(source_addr)
            sock.connect(sa)
            return sock
        except OSError as e:
            err = e
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    if err is not None:
        raise err
    raise OSError("getaddrinfo returns an empty list")


# [O35] 主要 patch：socket.getaddrinfo（所有 DNS 都走這個，最可靠）
_socket.getaddrinfo = _ipv4_first_only_getaddrinfo

# [備援] 也 patch urllib3.util.connection.create_connection（雙保險）
_urllib3_conn.create_connection = _ipv4_aware_create_connection
# 同時 patch urllib3.connection（HTTPConnection 在 module load 時 import 了 reference）
try:
    import urllib3.connection as _u3_conn_mod  # type: ignore[import-untyped]
    if hasattr(_u3_conn_mod, "create_connection"):
        _u3_conn_mod.create_connection = _ipv4_aware_create_connection
except Exception:
    pass


def _cache_get(cache_key, ttl_seconds, evict_expired=True):
    now = time.time()
    with _ttl_cache_lock:
        row = _ttl_cache_store.get(cache_key)
        if not row:
            return None
        ts, val = row
        if now - ts > ttl_seconds:
            if evict_expired:
                _ttl_cache_store.pop(cache_key, None)
            return None
        return val


def _cache_set(cache_key, value):
    with _ttl_cache_lock:
        _ttl_cache_store[cache_key] = (time.time(), value)
        trim_oldest_entries(_ttl_cache_store, _TTL_CACHE_MAX_ENTRIES)


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
        trim_oldest_entries(_parse_cache_store, _PARSE_CACHE_MAX_ENTRIES)


def _source_backoff_allow(source_key):
    now = time.time()
    with _ttl_cache_lock:
        row = _source_backoff_state.get(source_key)
        if not row:
            return True, 0.0
        next_allowed_ts, fail_count = row
        remain = max(0.0, next_allowed_ts - now)
        return remain <= 0.0, remain


def _source_backoff_fail(source_key, base_seconds=None, max_seconds=None):
    now = time.time()
    base = SOURCE_BACKOFF_BASE_SECONDS if base_seconds is None else base_seconds
    max_delay = SOURCE_BACKOFF_MAX_SECONDS if max_seconds is None else max_seconds
    with _ttl_cache_lock:
        row = _source_backoff_state.get(source_key)
        fail_count = (row[1] + 1) if row else 1
        delay = min(base * (2 ** (fail_count - 1)), max_delay)
        _source_backoff_state[source_key] = (now + delay, fail_count)
        trim_oldest_entries(_source_backoff_state, _SOURCE_STATE_MAX_ENTRIES)
        return delay, fail_count


def _source_backoff_success(source_key):
    with _ttl_cache_lock:
        _source_backoff_state.pop(source_key, None)


def _source_throttle_allow(source_key, interval_seconds):
    now = time.time()
    with _ttl_cache_lock:
        last_ts = _source_throttle_state.get(source_key, 0.0)
        if now - last_ts < interval_seconds:
            return False, max(0.0, interval_seconds - (now - last_ts))
        _source_throttle_state[source_key] = now
        trim_oldest_entries(
            _source_throttle_state,
            _SOURCE_STATE_MAX_ENTRIES,
            timestamp_of=lambda stamp: stamp,
        )
        return True, 0.0


def _register_reg_session(s):
    """新建 thread-local session 時呼叫，給 atexit cleanup 用。"""
    with _all_reg_sessions_lock:
        _all_reg_sessions.add(s)


def _atexit_clear_thread_local_sessions() -> None:
    """[v18] 程式退出時清所有 thread-local session 的 poolmanager，
    避免 dangling connection。跟 _kill_orphan_chromedriver 路徑同 spirit:
    強制斷連、不等未完成 request、立刻返回。
    """
    with _all_reg_sessions_lock:
        sessions = list(_all_reg_sessions)
        _all_reg_sessions.clear()
    for s in sessions:
        try:
            for adapter in s.adapters.values():
                try:
                    adapter.poolmanager.clear()
                except Exception:
                    pass
        except Exception:
            pass


import atexit as _atexit_for_sessions
_atexit_for_sessions.register(_atexit_clear_thread_local_sessions)


def _get_thread_local_reg52_session():
    """ThreadPool 每個工作執行緒獨立 Session：掛號 reg52 可並行，且不再與 forward01 值班查詢搶同一連線鎖。"""
    s = getattr(_reg52_tls, "session", None)
    if s is None:
        s = requests.Session()
        # 外層 check_appointment_count 已有醫師層級 retry；這裡不要再對 read timeout
        # 做 urllib3 retry，避免一次院方卡頓放大成 30+ 秒阻塞。
        rtry = Retry(
            total=1,
            connect=1,
            read=0,
            status=1,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
        )
        s.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=rtry))
        s.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=rtry))
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Connection": "keep-alive",
        })
        _register_reg_session(s)  # [v18] atexit cleanup
        _reg52_tls.session = s
    return s


def _get_thread_local_reg52_external_session():
    s = getattr(_reg52_external_tls, "session", None)
    if s is None:
        s = requests.Session()
        rtry = Retry(total=0, connect=0, read=0, redirect=0, status=0)
        s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=rtry))
        s.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=rtry))
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Connection": "keep-alive",
        })
        _register_reg_session(s)  # [v18] atexit cleanup
        _reg52_external_tls.session = s
    return s


def _get_thread_local_duty_session():
    s = getattr(_duty_tls, "session", None)
    if s is None:
        s = requests.Session()
        # 【效能 2026.05.20】retry total 2→1，避免 backoff 把首屏拖到 5-15s
        rtry = Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=rtry))
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": "https://forward01.cmuh.org.tw/peoplesystem/Duty/DutyQuery.aspx",
            "Connection": "keep-alive",
        })
        _register_reg_session(s)  # [v18] atexit cleanup
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
        _register_reg_session(s)  # [v18] atexit cleanup
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
    if stop_event_automation.is_set():
        raise SubsystemInterrupted("by F12 key press")
    with _hotkey_cancelled_threads_lock:
        if threading.get_ident() in _hotkey_cancelled_threads:
            raise SubsystemInterrupted("by F12 key press")


def _sleep_interruptible(seconds: float, *,
                         max_slice: float = 0.05) -> None:
    """Sleep in short slices so F12 can stop hotkey automation promptly."""
    try:
        remaining = max(0.0, float(seconds))
        slice_s = max(0.01, float(max_slice))
    except (TypeError, ValueError):
        remaining = 0.0
        slice_s = 0.05
    end_t = time.monotonic() + remaining
    while True:
        check_stop()
        left = end_t - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(slice_s, left))


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


# 【重構 2026-05-21】抽到 cmuh_common.ui_utils（與 scheduler.py 共用）
from cmuh_common.ui_utils import format_vertical_text  # noqa: E402

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

# 【重構 2026-05-21】roc_to_gregorian_year / parse_roc_date_str 三支入口共用版抽到
# cmuh_common.date_utils（main/scheduler/autoclock 原本各有一份功能相同但寫法略異）

def _initialize_status_driver():
    """[2026-05-25 v15] 改用 cmuh_common.chrome_options.build_chrome_options
    共用版 — 跟 autoclock 用同一份 flag (含 mute-audio / renderer-process-limit /
    js-flags max-old-space-size 等省 RAM flag)。
    預期 headless Chrome RSS 從 ~250MB 降到 ~150MB。
    """
    logging.info("Initializing headless WebDriver for status check...")

    try:
        from selenium import webdriver
        from cmuh_common.chrome_options import build_chrome_options
    except ImportError:
        logging.error("Selenium modules not found during runtime import.")
        return None

    try:
        # [優化] Selenium 4.6+ 自動呼叫底層 Selenium Manager 載入驅動，秒開毫秒就緒
        driver = webdriver.Chrome(options=build_chrome_options(headless=True))
        # [2026-06-26] 設網頁載入逾時:院方系統半夜維護/網路閒置斷線時 driver.get 可能【無限期】
        # 卡住 → 打卡查詢執行緒永遠不結束 → _clock_status_worker_running 旗標卡死 → 之後 08:00/
        # 17:03/手動全部被「上一輪仍在查詢」擋掉(跨日後就再也不更新,得重開程式)。設逾時 → 卡住會
        # 丟 TimeoutException、查詢正常結束、旗標歸零。(比照 punch_status.query_accounts_today。)
        try:
            driver.set_page_load_timeout(_STATUS_DRIVER_PAGELOAD_TIMEOUT)
        except Exception:
            logging.debug("set_page_load_timeout 失敗", exc_info=True)
        logging.info("Headless WebDriver initialized successfully.")
        return driver
    except Exception as e:
        logging.error(f"Failed to initialize headless WebDriver: {e}")
        return None


# =============================================================================
# [O3] 常駐 Chrome 池：避免每次按打卡狀態都重新啟動 Chrome（省 ~3 秒）
# 30 分鐘 idle 後自動 quit；程式退出時 atexit 確保 quit
# =============================================================================
_status_driver_pool = {
    "driver": None,
    "last_used": 0.0,
    "lock": threading.Lock(),
    "init_lock": threading.Lock(),
}
# [2026-05-25 v15 RAM 優化] 30 分鐘 → 10 分鐘。打卡狀態查詢一天最多 3-4 次
# (08:00 / 17:03 daily + 手動觸發)，30 分鐘太久。10 分鐘 idle 釋放，
# 下次重新 spin up 約 1-2 秒對使用者觀感無差，省 ~150-250MB Chrome RAM。
_STATUS_DRIVER_IDLE_TIMEOUT = 10 * 60
# [2026-06-26] 常駐查詢 driver 的網頁載入逾時(秒)。防 driver.get 無限期卡住 → 查詢執行緒不死、
# _clock_status_worker_running 旗標不會卡死(跨日後打卡狀態查詢全停的根因)。
_STATUS_DRIVER_PAGELOAD_TIMEOUT = 30
# 打卡查詢「正在查詢」旗標的年齡上限(秒)。超過視為上一輪卡死、允許強制開新一輪(自癒雙保險)。
_CLOCK_WORKER_MAX_AGE_SEC = 180

# ── 打卡查詢錯誤分類（2026-07-16）──────────────────────────────────────────────
# [GPT-5.6 架構審查 P1] 登入/帳密錯原本與逾時同走「灰燈 + 自動重試」→ 錯密碼會被
# 每波重試 5 次(一天 3 波)反覆送出,有【鎖帳號】風險。改成結構化 error_kind:只有
# transient(逾時/driver crash/網路)才自動重試;auth(帳密錯)與 disabled(刻意停用)絕不重試。
CLOCK_ERR_AUTH = "auth"            # 帳號/密碼錯、登入被拒 → 絕不自動重試(防鎖帳號)
CLOCK_ERR_DISABLED = "disabled"   # 刻意停用(院外模式)→ 不重試
CLOCK_ERR_TRANSIENT = "transient"  # 逾時 / driver crash / 網路 / 未分類 → 可自動重試
# [2026-07-16] 原 CLOCK_ERR_PORTAL_CHANGED(以表格元素缺當改版信號)已撤回:空表本來就不
# 渲染表格 → 會把「今天未打卡」誤判成改版。打卡表層級無可靠改版信號,故不設此類。


def _clock_error(text, kind: str = CLOCK_ERR_TRANSIENT) -> dict:
    """打卡查詢錯誤結果。error=UI 顯示字串(維持既有短字串);error_kind 供重試閘門判斷。"""
    return {"error": str(text)[:40], "error_kind": kind}


def _get_or_create_status_driver():
    """取得常駐 status driver。若不存在或已 idle 超時就重建。

    [2026-05-22 v45 P0-2 修補] driver.quit() 移到鎖外。原本持鎖 quit
    若 chromedriver hang 會卡 30s+，期間所有等 lock 的 caller (掛號狀態
    refresh、UI thread) 全部排隊。task #69 標 completed 但實際上沒改完。
    """
    import time as _t
    pool = _status_driver_pool
    old_driver_to_quit = None
    need_init = False

    with pool["lock"]:
        driver = pool["driver"]
        now = _t.time()
        # 若 idle 超時，先標記重建
        if driver is not None and (now - pool["last_used"]) > _STATUS_DRIVER_IDLE_TIMEOUT:
            old_driver_to_quit = driver
            driver = None
            pool["driver"] = None
        # 健康檢查（驗證 driver 仍可用）
        if driver is not None:
            try:
                _ = driver.window_handles  # 觸發一次 RPC 確認 driver 還活著
            except Exception:
                logging.info("既有 status driver 已死，重建")
                old_driver_to_quit = driver
                driver = None
                pool["driver"] = None
        if driver is None:
            need_init = True

    # 鎖外 quit 舊 driver
    if old_driver_to_quit is not None:
        try:
            old_driver_to_quit.quit()
        except Exception:
            logging.debug("status driver quit 失敗", exc_info=True)

    if need_init:
        # initialize 走網路，不能持 pool lock；但要防止多個 refresh 同時
        # 看到 None 而各自開一個 Chrome，造成被覆蓋的 driver 殘留。
        with pool["init_lock"]:
            with pool["lock"]:
                driver = pool["driver"]
                if driver is not None:
                    pool["last_used"] = _t.time()
                    return driver

            driver = _initialize_status_driver()
            with pool["lock"]:
                pool["driver"] = driver
                pool["last_used"] = _t.time()
        return driver

    with pool["lock"]:
        pool["last_used"] = _t.time()
    return driver


# 丟棄 driver 時 graceful quit 的寬限秒數;逾時依該 driver 的 chromedriver PID 砍行程樹
# (正常 quit <2s;卡死才會走到砍樹)。
_STATUS_DISCARD_QUIT_GRACE_SEC = 8


def _discard_status_driver(failed_driver=None) -> None:
    """丟棄失敗的常駐 driver（下一次查詢會重建全新 Chrome，spin up 約 1-2 秒）。

    [2026-07-15 跨夜] 打卡查詢失敗時呼叫：pool 健康檢查（window_handles）只驗 browser
    行程活著，renderer 已死（"tab crashed"/"disconnected"，程式放跨夜最常見）時仍會把
    壞 driver 交回 → 若不丟棄，之後每一輪（含失敗自動重試）都拿到同一個壞 driver 永久
    失敗；且重試會一直刷新 last_used，連 10 分鐘 idle 淘汰都永遠不觸發。

    [codex] 帶 failed_driver 做身分比對：180s worker-age 保險允許新舊查詢重疊，舊查詢
    「晚到的失敗」若無條件清池，會把新查詢正在用的【新 driver】quit 掉、害新查詢跟著
    失敗。只在池中仍是那個失敗的 driver 時才清池；已被汰換 → 只善後失敗的那份自己
    （新一輪汰換時多半已 quit 過，重複 quit 由 except 容忍）。None＝無條件清（相容）。
    比照 _get_or_create_status_driver：鎖內只 nullify、鎖外 quit（chromedriver hang 時
    不可佔住 pool lock 拖累掛號 refresh/UI）。"""
    pool = _status_driver_pool
    with pool["lock"]:
        cur = pool["driver"]
        if failed_driver is None or cur is failed_driver:
            pool["driver"] = None
            to_quit = cur if cur is not None else failed_driver
        else:
            to_quit = failed_driver   # 池中已是新 driver → 不動池,只收失敗的那份
    if to_quit is not None:
        # [codex P1] quit 丟 daemon 緒非同步執行:chromedriver 卡死時 quit 可能永不返回
        # (repo 前例:_release_status_driver 因此改走 taskkill)。若在失敗路徑同步 quit,
        # 查詢的 error 回不去、worker 旗標清不掉、3 分鐘重試也排不了——修跨夜反被 quit
        # 卡死。
        # [codex P2] daemon 緒內做「有界清理」:graceful quit 給寬限秒數,逾時就依【該
        # driver 專屬的 chromedriver PID】精準砍行程樹——否則卡死的 quit 會讓整棵
        # Chrome 樹殘留,每次重試再開新 driver,一波最多累積 5 棵(程式跨夜長跑不會經過
        # atexit 清理)。砍樹後卡住的 quit 連線會斷開自行出錯返回,緒也跟著收掉;
        # 依 PID 鎖定該份 driver,不掃全域、不影響池中的新 driver(身分比對見上)。
        try:
            _svc_pid = to_quit.service.process.pid
        except Exception:
            _svc_pid = None

        def _quit_async(d=to_quit, svc_pid=_svc_pid):
            done = threading.Event()

            def _graceful():
                try:
                    d.quit()
                except Exception:
                    logging.debug("discard status driver quit 失敗", exc_info=True)
                finally:
                    done.set()

            try:
                threading.Thread(target=_graceful, name="StatusDriverQuit",
                                 daemon=True).start()
            except Exception:
                logging.debug("discard graceful quit 緒啟動失敗", exc_info=True)
                done.set()
            if done.wait(_STATUS_DISCARD_QUIT_GRACE_SEC):
                return
            if not svc_pid:
                logging.warning("打卡 driver quit 卡住且無 chromedriver PID 可砍（放生）")
                return
            try:
                import psutil as _ps
                proc = _ps.Process(svc_pid)
                for ch in proc.children(recursive=True):
                    try:
                        ch.kill()
                    except (_ps.NoSuchProcess, _ps.AccessDenied):
                        pass
                proc.kill()
                logging.warning("打卡 driver quit 卡住(>%ds)，已強制結束 chromedriver "
                                "PID %s 行程樹", _STATUS_DISCARD_QUIT_GRACE_SEC, svc_pid)
            except Exception:
                logging.debug("discard 強制砍 chromedriver 樹失敗", exc_info=True)
        try:
            threading.Thread(target=_quit_async, name="StatusDriverDiscard",
                             daemon=True).start()
        except Exception:
            logging.debug("discard quit thread 啟動失敗（略過）", exc_info=True)


def _release_status_driver():
    """程式結束時 quit 常駐 driver。

    [2026-05-22 v45 P1-5 修補] 改 taskkill 不走 driver.quit()。
    atexit 路徑可能跟卡死的 thread 互相等鎖；taskkill chromedriver
    直接砍進程，不等 graceful shutdown，永遠不會 hang。
    """
    pool = _status_driver_pool
    # 鎖內只 nullify (不 quit)
    with pool["lock"]:
        pool["driver"] = None
    # 鎖外 taskkill chromedriver 子進程
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
                for ch in p.children(recursive=True):
                    try:
                        ch.kill()
                    except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                        pass
                p.kill()
            except (_psutil.NoSuchProcess, _psutil.AccessDenied, Exception):
                continue
    except Exception:
        logging.debug("status driver release taskkill 例外", exc_info=True)


import atexit as _atexit
_atexit.register(_release_status_driver)


def _dismiss_status_driver_alert(driver) -> bool:
    """清掉常駐 Chrome 頁面上殘留的 JS alert，回傳是否清掉了至少一個。

    [2026-07-14 實機] 打卡網站閒置一段時間會彈「閒置時間過長，將被導向登入畫面！」的
    JS alert。常駐 status driver 上一輪查完停在打卡頁,放著跨過閒置逾時後那個【未處理的
    alert】會讓下一次查詢的 driver.get()／任何頁面指令直接拋 UnexpectedAlertPresentException
    → 整個打卡狀態查詢失敗、UI 顯示不出打卡狀態(reg64 掛號查詢不受影響,故只有打卡壞)。
    查詢開頭先把它 accept 掉即可恢復(本函式每次查詢都會重新 driver.get 登入頁+重登)。
    迴圈上限 3 次:accept 後極少數情況頁面會再冒一個,給幾次;正常一次就清完。"""
    cleared = False
    for _ in range(3):
        try:
            alert = driver.switch_to.alert
            txt = (alert.text or "").strip()
            alert.accept()
            cleared = True
            logging.info("[打卡] 清除殘留 alert：%s", txt[:40])
        except Exception:
            break
    return cleared


# --- [修正] 打卡狀態抓取 (修正密碼錯誤Alert處理 + TAB優化) ---
# [O3] 改用常駐 Chrome（_get_or_create_status_driver），首次後再按只要 1-2 秒
def _get_swipe_status_from_web(username, password):
    # 定義檢查區間
    AM_START = dt_time(7, 30); AM_END = dt_time(8, 0)
    PM_START = dt_time(17, 0); PM_END = dt_time(17, 30)

    driver = _get_or_create_status_driver()
    if not driver: return _clock_error("Driver失敗", CLOCK_ERR_TRANSIENT)
    
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
        # [2026-07-14] 常駐 Chrome 重用時,先清掉上一輪 session 閒置逾時殘留的 alert
        # (「閒置時間過長，將被導向登入畫面」)。否則接下來的 driver.get 會撞
        # UnexpectedAlertPresentException 而整個查詢失敗 → 放著跨過閒置逾時後打卡狀態就
        # 查不到。清完再照常重新 get 登入頁 + 重登。get 若仍撞殘留 alert(清除與 get 之間
        # races),再清一次重試一次。
        _dismiss_status_driver_alert(driver)
        try:
            driver.get(LOGIN_URL)
        except UnexpectedAlertPresentException:
            _dismiss_status_driver_alert(driver)
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
                
                # [2026-07-06] 登入成功錨點改用 lb_systime,不再等 Gv_attppre。空的 ASP.NET
                # GridView(當日尚無刷卡紀錄)不渲染任何 <table> → 等 Gv_attppre 會逾時 → 誤判
                # 「登入逾時/失敗」(打卡查詢整天壞掉的根因)。lb_systime 在登入後一定存在(空表
                # 也在);下方 JS querySelectorAll 對不存在的表安全回 []=今日無紀錄。
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "lb_systime")))
                login_success = True
                break 
            
            # [關鍵新增] 捕捉「密碼錯誤」或其他 Alert 彈窗
            except UnexpectedAlertPresentException as e:
                alert_text = e.alert_text
                logging.error(f"登入時遇到 Alert: {alert_text}")
                # [P1] 登入被 portal 以 alert 拒絕＝帳密/帳號問題 → AUTH,絕不自動重試(防鎖帳號)
                return _clock_error(alert_text, CLOCK_ERR_AUTH)

            except TimeoutException:
                logging.warning("點擊後未偵測到頁面跳轉，可能點擊無效，準備重試...")
                # 如果有 Alert 擋住，這裡也嘗試切換去接受它
                try:
                    driver.switch_to.alert.accept()
                    return _clock_error("密碼/帳號錯誤", CLOCK_ERR_AUTH)   # [P1] AUTH 不重試
                except Exception:
                    pass

                try:
                    driver.find_element(By.ID, "bt_login")
                except Exception:
                    break

        if not login_success:
            # 登入沒成功但也【沒】撞到帳密 alert(上面 AUTH 已先攔)→ 多為 portal 慢/頁面
            # 未跳轉的暫時性問題 → TRANSIENT,允許有界自動重試。
            return _clock_error("登入逾時/失敗", CLOCK_ERR_TRANSIENT)

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

        # [2026-07-16 撤回打卡改版偵測] 原本想用「#Gv_attppre 表格元素是否存在」當 portal
        # 改版信號,但【空的 ASP.NET GridView(當日尚無刷卡紀錄)本來就不渲染任何 <table>】
        # (見上方 lb_systime 錨點註解、punch_status.py) → 表格不存在＝【今天還沒打卡】,
        # 不是改版。把它當改版會在「早上未打卡」(最該顯示未打卡的時刻)灰燈+不重試,反而
        # 隱藏最重要訊號(GPT-5.6/實測)。故還原:表格不在＝0 列＝未打卡,正常顯示。
        # (登入成功已由 lb_systime 錨點保護;若 portal 把 lb_systime 改名 → 等不到 → 走
        #  登入逾時。表格層級無可靠改版信號,不做勝過做錯——同 reg64 的保守判斷。)
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
        """) or []

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
        # [2026-07-15 跨夜] 走到這裡多半代表 driver/頁面已壞（tab crashed、載入逾時、
        # session 斷線）→ 丟棄【本輪失敗的】driver，下一輪（排程或失敗自動重試）重建全新
        # Chrome，不讓壞 driver 卡住之後所有查詢（健康檢查驗不出 renderer 死亡）。帶身分
        # 比對：重疊查詢時晚到的失敗不可清掉新查詢正在用的新 driver（見 discard 說明）。
        _discard_status_driver(driver)
        return _clock_error(str(e), CLOCK_ERR_TRANSIENT)
    # [O3] 注意：正常路徑不 driver.quit()！常駐 Chrome 由 _get_or_create_status_driver
    # 控管，10 分鐘 idle 自動 quit；程式結束時 atexit 也會清理。

# =============================================================================
# --- 6. 自動化腳本 ---
# =============================================================================

# =============================================================================
# --- [重構] 統一的熱鍵執行器 (HotkeyRunner) ---
# 現行 F1-F12 走 Win32 SendMessage（解析度無關），HotkeyRunner 僅保留
# _runner_1280.last_action_time 作為熱鍵自動化節流計時。
# =============================================================================
class HotkeyRunner:
    """熱鍵自動化節流計時用的輕量執行器（last_action_time）。"""

    def __init__(self, name: str):
        self.name = name
        self.last_action_time: float = 0.0


# 熱鍵自動化節流計時用的執行器實例（僅 _runner_1280 供 last_action_time 節流用）
_runner_1280 = HotkeyRunner("1280x1024")


def _mark_hotkey_action_time() -> None:
    """更新熱鍵自動化節流時間；集中處理避免失敗路徑漏更新。"""
    if hasattr(_runner_1280, "last_action_time"):
        _runner_1280.last_action_time = time.time()


# [MG-02] 自動更新「需重啟」不可在熱鍵自動化(F1-F12)進行中硬砍:重啟是 spawn+sys.exit,daemon 熱鍵緒
# 會在【任意指令中間】被切斷(不像 F12 只在 check_stop 安全點停)→ 醫令欄殘碼沒 Enter、F9/F10 同意書
# 半開、設定頁未存編輯蒸發。排程時點(07:00/13:00/18:00)全在門診時段,撞上機率不低。改為等『無 subsystem
# 在跑且距最後一次熱鍵動作 ≥N 秒』才重啟;忙碌時每隔幾秒重查,並設總延後上限(旗標卡死的極端情況由熱鍵
# watchdog 兜底清除,不讓自動更新永不生效)。
_UPDATE_RESTART_IDLE_GAP_SEC = 8.0        # 距最後一次熱鍵動作至少閒置這麼久才敢重啟
_UPDATE_RESTART_RECHECK_MS = 5000         # 忙碌時每 5 秒重查一次
_UPDATE_RESTART_MAX_DEFER_ATTEMPTS = 180  # ~15 分上限(180×5s);到頂仍未閒置就重啟


# =============================================================================
# 解析度無關熱鍵 (adaptive) — 不依賴座標，直接 Win32 SendMessage 觸發選單
# =============================================================================
# 設計：主程式 (TFopdmain) 在任何解析度／DPI 下，「醫令」選單第三段的 menu
# command ID 是連續 ID。透過 SendMessage(WM_COMMAND, 代碼輸入 id) 就能觸發代碼輸入，焦點
# 自動跳到醫令代碼欄；之後用 pyautogui 模擬鍵盤打代碼 + Enter。
# [2026-06-29] HIS 改版 V.1150629.01 → 整批選單 id +1 位移(user 跑 test_yiling_menu_id.py 實測:
#   代碼輸入 218→219、同意書 668→669)。同段的 類別字首/代碼字首/名稱輸入(目前未被使用)一併 +1。
#
# 設計演化 (僅歷史說明 — 舊 script 已 2026-05-19 移除)：
#   舊版 = pyautogui.click(x, y) 點選單 + 點代碼輸入 — 三個解析度三份 code
#   adaptive = Win32 SendMessage — 一份 code 跨所有解析度
#
# 需要本程式以 admin 執行（UIPI 限制：非 admin 無法對 admin TFopdmain 送
# WM_COMMAND）。主程式本來就 admin，所以 OK。

_HOSPITAL_WIN_CLASS = "TFopdmain"
_HOSPITAL_WIN_TITLE_KW = "西醫門診醫師作業"

# [M1 2026-07-09] HIS 版本守門(偵測 + 警示)。選單 command id(代碼輸入/同意書/完成不印)是對特定
# HIS 版本【硬編碼校正】的;HIS 改版曾整批位移(2026-06-29 全部 +1)→ 舊 id 會觸發到別的選單功能。
# 若主視窗 title 帶的版本字串與校正版本不同 → 記 WARNING 提醒 id 可能已位移(需實測重新校正)。
# 【刻意只警示、不硬停 F 鍵】:硬停在版本字串誤判時會讓醫師整組熱鍵失效(比原 bug 更糟);且完成不印
# 已有 _find_menu_command_id_by_text 動態解析當備援。title 沒有可辨識版本字串 → 不動作(避免假警報)。
# 硬停 + 醫師對話框待有實機可確認 title 格式再補(不確定就不自動動作,連自己的守門也一樣)。
_HIS_CALIBRATED_VERSION = "1150713"   # 選單 id 校正對應的 HIS 版本(2026-07-13 V.1150713.02 改版後,使用者實測選單 id 仍正常;前一版 1150629)
_HIS_VERSION_RE = re.compile(r"[Vv]\.?\s*(\d{6,8})")
# [金絲雀 2026-07-17] 另抓含尾碼的完整版本(V.1150629.01 → 1150629.01)。主版本相同但尾碼
# 不同(.01→.02)也可能是改版;但尾碼比對【只在基線本身帶尾碼時】才生效(見 sample_his_current_fp)
# ,故隱性硬編碼基線(只有主版本、無尾碼)不會一開機就因尾碼把 F 鍵全擋死。
_HIS_VERSION_FULL_RE = re.compile(r"[Vv]\.?\s*(\d{6,8}(?:\.\d{1,3})*)")

# ── 契約金絲雀:HIS 寫入面(2026-07-16) ─────────────────────────────────────────
# F1–F12 靠硬編碼選單 command id 操作 HIS;院方改版(如 2026-06-29 整批 +1)會讓 F 鍵
# 打到別的選單功能 → 寫錯病歷。金絲雀:每次危險寫入前比對「主視窗版本」與基線,不符即
# 【fail-closed 停止自動寫入】+ 疑似改版警告,交醫師手動(把「寫錯」換成「不寫、你手動」)。
# 指紋只用 title 版本號(HIS 選單多為 owner-draw、動態文字讀不到 → 版本字串是最可靠信號)。
_CANARY_HIS_SURFACE = "his_menu"
# [codex] 不再保留「最近裁決/現況指紋」的可變全域——安全關鍵路徑(寫入 gate、重新校正)
# 與設定頁顯示都【自足即時採樣】(用當下 hwnd 的 title 現算),徹底免除跨緒覆寫/清空競態。
_his_canary_warned = False      # 疑似改版警告只記一次 log(避免洗版),非競態敏感
# [金絲雀 2026-07-17 使用者定案] 改版時【不擋自動寫入、不跳窗】,只寄信通知一次(每個偵測到
# 的現況版本一次,避免洗版)。notified=寄信成功、終局去重;inflight=同版本寄送中(擋並發、
# 避免每次找視窗堆一條 60s 逾時背景緒);兩者共用同一 lock(去重跨緒安全)。
_his_drift_notified_versions: set = set()
_his_drift_inflight_versions: set = set()
_his_drift_notify_lock = threading.Lock()
# [codex P2] 找視窗時快照的 (HIS 完整版本, 金絲雀裁決),供稽核在【動作當下】O(1) 取用。
# 刻意是單一 tuple(整體換掉)→ 讀取端不會拿到撕裂的組合;這只餵稽核紀錄,不參與任何
# 安全決策(故與「gate 不得讀可變全域」的結論不衝突)。
_his_last_sample: tuple = ("", "")
_contract_baseline_singleton = None


def _contract_baseline() -> "_ContractBaseline":
    global _contract_baseline_singleton
    if _contract_baseline_singleton is None:
        _contract_baseline_singleton = _ContractBaseline(
            get_conf_path(_CANARY_BASELINE_FILENAME))
    return _contract_baseline_singleton


# ── 外部動作稽核帳本(ExternalActionGateway 第一片,2026-07-17)────────────────
# 使用者定案「改版不擋只通知」後,預防性控制沒了 → 補償控制是【偵測性】紀錄:每次真的動到
# HIS 都留一筆(值、當下 HIS 版本、金絲雀裁決、回讀結果),事後查得出寫了什麼。帳本落在
# settings/(已 gitignore,不會進 public repo);【不得】寫入病人明文識別資料。
_action_ledger_singleton = None
_action_ledger_lock = threading.Lock()


def _action_ledger() -> "_ActionLedger":
    global _action_ledger_singleton
    with _action_ledger_lock:
        if _action_ledger_singleton is None:
            _action_ledger_singleton = _ActionLedger(
                get_conf_path(_LEDGER_FILENAME))
        return _action_ledger_singleton


# [codex P1] 稽核【絕不可阻塞熱鍵】。「不拋例外」不等於「不阻塞」:取 HIS title 會等到
# WIN_ENUM_TIMEOUT_SEC(3s)、寫檔/輪替/搶鎖也可能卡住,而 _record_his_action 被夾在「已送
# 出完成指令」與「開始輪詢 popup」之間 —— 檔案 IO 一卡就把做到一半的臨床自動化懸住。故:
# 熱鍵緒只做 O(1) 入列;取 title 與所有檔案 IO 都在背景寫入緒。佇列滿就【丟棄並回報】,
# 絕不等待(寧可少一筆稽核,也不可拖住 F 鍵)。
_LEDGER_QUEUE_MAX = 256
_ledger_queue: "Queue" = Queue(maxsize=_LEDGER_QUEUE_MAX)
_ledger_writer_started = False
_ledger_writer_lock = threading.Lock()
_ledger_dropped = 0


def _ledger_writer_loop(q=None) -> None:
    """背景寫入緒:只做落檔(鎖/輪替/寫檔)。這裡阻塞不影響臨床流程。
    [codex P2] 版本/裁決已在入列時就快照好,這裡不再做任何 Win32 採樣。

    q:綁定的佇列(啟動時決定,不每圈重讀全域 —— 否則測試/重設全域時,舊的寫入緒會跑去
    消費新的佇列)。收到 None 哨兵即收工。"""
    q = q if q is not None else _ledger_queue
    while True:
        try:
            item = q.get()
        except Exception:
            logging.debug("[ledger] 取佇列失敗", exc_info=True)
            continue
        try:
            if item is None:            # 哨兵 → 收工
                return
            surface, action, fields, ts = item
            _action_ledger().record(surface, action, ts=ts, **fields)
        except Exception:
            logging.debug("[ledger] 背景寫入失敗(不影響操作)", exc_info=True)
        finally:
            try:
                q.task_done()
            except Exception:
                pass


def _ensure_ledger_writer() -> None:
    global _ledger_writer_started
    if _ledger_writer_started:
        return
    with _ledger_writer_lock:
        if _ledger_writer_started:
            return
        threading.Thread(target=_ledger_writer_loop, args=(_ledger_queue,),
                         daemon=True, name="action-ledger-writer").start()
        _ledger_writer_started = True


# 關閉前排空稽核佇列的上限(秒)。夠久到寫完少量積壓,又不會讓使用者點 X 後枯等。
_LEDGER_FLUSH_TIMEOUT_SEC = 2.0
_ledger_shutting_down = False


def _flush_ledger_before_exit(timeout: float = _LEDGER_FLUSH_TIMEOUT_SEC) -> bool:
    """[codex P2] os._exit(0) 前把佇列中的稽核紀錄盡量寫完(daemon 緒會被直接砍掉)。

    【必須等到 task_done】而不是 queue.empty():最後一筆被取走的瞬間佇列就空了,但
    ActionLedger.record() 可能還在寫 —— 那時 os._exit 會把寫到一半的動作砍掉。故等
    unfinished_tasks 歸零(= 真的寫完),並停止收新項目。有硬逾時,絕不拖延關閉。"""
    global _ledger_shutting_down
    try:
        _ledger_shutting_down = True          # 不再收新的稽核項目
        if not _ledger_writer_started:
            return _ledger_queue.unfinished_tasks == 0
        deadline = time.time() + max(0.0, float(timeout))
        with _ledger_queue.all_tasks_done:
            while _ledger_queue.unfinished_tasks:
                remaining = deadline - time.time()
                if remaining <= 0:
                    logging.warning("[ledger] 關閉前排空逾時,尚有 %d 筆稽核未寫入",
                                    _ledger_queue.unfinished_tasks)
                    return False
                _ledger_queue.all_tasks_done.wait(remaining)
        return True
    except Exception:
        logging.debug("[ledger] 關閉前排空失敗(忽略)", exc_info=True)
        return False


def _record_his_action(surface: str, action: str, main_hwnd: int = 0,
                       **fields) -> None:
    """記一筆 HIS 外部動作(非同步)。補上【動作當下】的 HIS 版本與金絲雀裁決。

    【絕不阻塞、絕不拋】:熱鍵緒只做 O(1)(讀快照 tuple + 入列),寫檔在背景緒。
    [codex P2] 版本/裁決取自 _his_last_sample(找視窗時的快照),不在此做 Win32 採樣 ——
    既不會等 3s,也不會像「背景緒稍後才採樣」那樣在佇列積壓時記到 HIS 重啟後的新版本。
    main_hwnd 僅為呼叫端相容保留,不再用於採樣。
    【不得傳入 PII】:value/detail 只放非 PII 的值(醫令代碼/劑量/療程數);病歷號、姓名、
    卡號,以及【採樣到的 HIS 欄位原文】(可能誤抓到識別資料)一律不得傳入。"""
    global _ledger_dropped
    try:
        if _ledger_shutting_down:
            # 關閉排空中 → 不再收新項目(否則排空永遠追不上、拖延關閉)
            return
        if "his_version" not in fields or "canary" not in fields:
            ver, canary = _his_last_sample          # 單次 tuple 讀取,不會撕裂
            fields.setdefault("his_version", ver)
            fields.setdefault("canary", canary)
        fields.setdefault("app_version", str(CURRENT_VERSION))
        ts = datetime.now().isoformat(timespec="seconds")   # 動作發生當下的時間
        _ensure_ledger_writer()
        _ledger_queue.put_nowait((str(surface), str(action), dict(fields), ts))
    except Exception:
        # 佇列滿(Full)或其他任何問題 → 丟棄,絕不等待
        _ledger_dropped += 1
        logging.warning("[ledger] 稽核紀錄入列失敗/佇列滿 → 丟棄(累計 %d 筆)",
                        _ledger_dropped)


def _his_title_version(title: str):
    """從主視窗 title 取 HIS 主版本號(6-8 位數字,如 1150629);找不到回 None。純函式,好測。"""
    m = _HIS_VERSION_RE.search(title or "")
    return m.group(1) if m else None


def _his_title_version_full(title: str):
    """[金絲雀 2026-07-17] 取含尾碼的完整版本(如 1150629.01);無尾碼時等同主版本;找不到回 None。
    純函式。尾碼是否納入 DRIFT 判定,取決於基線是否帶尾碼(見 sample_his_current_fp)。"""
    m = _HIS_VERSION_FULL_RE.search(title or "")
    return m.group(1) if m else None


def _his_write_baseline_fp() -> dict:
    """HIS 寫入契約基線指紋:優先使用者校正過的基線檔;無則以硬編碼校正版本為隱性基線。"""
    fp = _contract_baseline().get(_CANARY_HIS_SURFACE)
    if isinstance(fp, dict) and fp.get("title_version"):
        return fp
    return {"title_version": _HIS_CALIBRATED_VERSION}


def sample_his_current_fp(title: str):
    """從 title 採樣 HIS 寫入契約現況指紋;採不到版本回 None(→ 裁決 UNKNOWN,不擋)。純函式。

    [金絲雀 2026-07-17] 除主版本 title_version(1150629)外,另存含尾碼的 title_version_full
    (1150629.01)。compare_fingerprint 以【基線的鍵】為比對範圍,而隱性硬編碼基線只含
    title_version → 尾碼預設【不】比對(不會一開機就把 F 鍵全擋死);唯有使用者在實機
    「重新校正」把現況(含 full)寫進基線後,尾碼變動(.01→.02)才會被判 DRIFT。這樣
    既補上 GPT 指出的尾碼 false-negative,又不冒盲抓尾碼把熱鍵全擋死的風險。"""
    ver = _his_title_version(title)
    if ver is None:
        return None
    fp = {"title_version": ver}
    full = _his_title_version_full(title)
    if full is not None:
        fp["title_version_full"] = full
    return fp


def _his_write_verdict_for(title: str):
    """純函式:給 title 算 HIS 寫入契約裁決(現況 vs 基線)。gate/顯示/校正共用,無副作用。"""
    return _canary_compare(_CANARY_HIS_SURFACE, _his_write_baseline_fp(),
                           sample_his_current_fp(title))


def _load_alert_recipients() -> list:
    """讀 threshold_settings.json 的 alert_email_recipients(與止掛提醒同一組收件人)。
    模組級、不依賴 app 實例;讀不到/無設定回空 list(→ 不寄、不報錯)。"""
    import json
    try:
        with open(get_conf_path('threshold_settings.json'), encoding='utf-8') as f:
            d = json.load(f)
        r = d.get('alert_email_recipients')
        if isinstance(r, list):
            return [str(x).strip() for x in r if str(x).strip()]
    except Exception:
        logging.debug("[金絲雀] 讀取通知收件人失敗", exc_info=True)
    return []


def _his_drift_current_version(verdict) -> str:
    """從裁決取現況版本當「已通知」去重 key。
    [codex] 優先用含尾碼的完整版本(title_version_full)—— 否則同主版本的點版(如 1150714.01
    與 1150714.02)會共用同一 key、後者被去重吃掉、漏通知。無 full 差異則退主版本,再退 detail。"""
    cur_major = None
    for ch in (verdict.changes or []):
        if not ch:
            continue
        if ch[0] == "title_version_full":
            return str(ch[2])
        if ch[0] == "title_version":
            cur_major = str(ch[2])
    if cur_major is not None:
        return cur_major
    return verdict.detail or "unknown"


def _notify_his_drift(verdict) -> None:
    """[金絲雀 2026-07-17 使用者定案] 偵測到 HIS 改版 → 【寄信通知一次】(每個現況版本只寄
    一次,避免洗版),但【不擋自動寫入、不跳警告視窗】。功能照常執行;醫師若發現異常會自行
    停用。理由:實務上「誤擋 + 每按一次跳窗」比偶發改版更難用;改版風險由醫師現場判斷兜底。
    寄信丟背景 daemon 緒(SMTP 逾時可達 60s,絕不可卡住熱鍵/找視窗流程)。

    [codex] 「已通知」只在【寄信成功後】才記,寄失敗/起緒失敗則釋放、留待下次找視窗重試
    (否則一次暫時性 SMTP 失敗就讓該版本整個 process 再也不通知)。同時用 in-flight 集合鎖住
    「同版本同時只允許一次寄送」,避免每次找視窗都堆一條 60s 逾時的背景緒。"""
    ver = _his_drift_current_version(verdict)
    with _his_drift_notify_lock:
        if (ver in _his_drift_notified_versions
                or ver in _his_drift_inflight_versions):
            return
        _his_drift_inflight_versions.add(ver)

    subject = f"[皮膚科自動化] 偵測到 HIS 主視窗改版:{ver}"
    body = (f"HIS 主視窗版本與金絲雀校正基線不符。\n\n{verdict.human()}\n\n"
            f"F 鍵自動化【未被擋下、仍照常執行】(依使用者設定:改版只通知、不停用)。\n"
            f"請盡快核對 F1–F11 選單 id 是否仍正確;若功能異常,請先手動停用該熱鍵,並更新"
            f"校正版本(_HIS_CALIBRATED_VERSION)或於設定頁按「重新校正」把現況記為新基線。\n\n"
            f"(同一版本此信只寄一次。)")

    def _bg() -> None:
        ok = False
        try:
            recipients = _load_alert_recipients()
            if recipients:
                ok = bool(_send_alert_email_via_smtp(subject, body, recipients))
            else:
                logging.debug("[金絲雀] 偵測到改版但無通知收件人(alert_email_recipients 空),"
                              "稍後找視窗時再試")
        except Exception:
            logging.debug("[金絲雀] 改版通知寄信失敗(不影響操作),稍後重試", exc_info=True)
        finally:
            with _his_drift_notify_lock:
                _his_drift_inflight_versions.discard(ver)
                if ok:   # 只有真的寄成功才標記「已通知」,失敗留待重試
                    _his_drift_notified_versions.add(ver)

    try:
        threading.Thread(target=_bg, daemon=True, name="canary-drift-mail").start()
    except Exception:
        # 連背景緒都起不了 → 釋放 in-flight,讓下次找視窗可重試(不永久卡住通知)
        with _his_drift_notify_lock:
            _his_drift_inflight_versions.discard(ver)
        logging.debug("[金絲雀] 通知背景緒啟動失敗(稍後重試)", exc_info=True)


def _sample_his_write_contract(title: str) -> None:
    """[金絲雀 2026-07-17 使用者定案] 找到主視窗時採樣裁決:偵測到院方改版(DRIFT)→ 記一次
    warning log + 【寄信通知一次】(每個新版本一次);【不擋自動寫入、不跳窗】——誤擋+洗版
    比偶發改版更難用,改版風險由醫師現場判斷兜底(發現功能異常自行停用)。

    [codex P2] 順手把「版本 + 裁決」快照進 _his_last_sample:稽核要記的是【動作當下】的
    版本,不能等背景緒稍後自己去採樣(佇列積壓時 HIS 可能已重啟/升版,會記到錯的版本,
    甚至撞到 hwnd 重用)。這裡本來就在每次找視窗時執行,快取等於動作前一刻的真實版本。"""
    global _his_canary_warned, _his_last_sample
    v = _his_write_verdict_for(title)
    # 單一 tuple 指派 → 讀取端不會拿到撕裂的 (版本, 裁決) 組合
    _his_last_sample = (_his_title_version_full(title) or "", v.status)
    if not v.is_drift:
        return
    if not _his_canary_warned:
        _his_canary_warned = True
        logging.warning("[金絲雀] %s", v.human())
    _notify_his_drift(v)

# 醫令 子選單 command ID (probe + user 確認;2026-06-29 HIS V.1150629.01 改版後整批 +1)
MENU_ID_類別字首 = 216   # 215→216(未使用,隨同段 +1)
MENU_ID_代碼字首 = 218   # 217→218(未使用,隨同段 +1)
MENU_ID_代碼輸入 = 219   # 218→219(user 實測確認;F1~F5 都走這個)
MENU_ID_名稱輸入 = 220   # 219→220(未使用,隨同段 +1)


def _find_hospital_main_window() -> int:
    """找主程式視窗 (class=TFopdmain, title 含「西醫門診醫師作業」)。
    回傳 hwnd；找不到回 0。

    [W2 2026-07-03] callback 內用 raw GetWindowTextW(送 WM_GETTEXT),HIS GUI 執行緒
    凍結時會【無限期阻塞】。此函式是每支熱鍵最先呼叫的入口 → 一卡整個熱鍵子系統死。
    故整個列舉丟到 daemon thread + 逾時,逾時回 0(當作沒找到),讓熱鍵乾淨中止而非卡死。"""
    def _enum() -> int:
        found = [0]

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @EnumWindowsProc
        def cb(hwnd, lparam):
            try:
                if not ctypes.windll.user32.IsWindowVisible(hwnd):
                    return True
                cls_buf = ctypes.create_unicode_buffer(64)
                ctypes.windll.user32.GetClassNameW(hwnd, cls_buf, 64)
                if cls_buf.value != _HOSPITAL_WIN_CLASS:
                    return True
                n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if n > 0:
                    t_buf = ctypes.create_unicode_buffer(n + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, t_buf, n + 1)
                    if _HOSPITAL_WIN_TITLE_KW in t_buf.value:
                        found[0] = hwnd
                        # [金絲雀] 採樣 HIS 寫入契約(版本)並裁決,供危險寫入 gate 讀
                        _sample_his_write_contract(t_buf.value)
                        return False  # 停止枚舉
            except Exception:
                pass
            return True

        ctypes.windll.user32.EnumWindows(cb, 0)
        return found[0]

    # [金絲雀] cb 內呼叫 _sample_his_write_contract 只做「開機/找視窗時 DRIFT 一次性警告
    # log」,【不存任何全域】。安全關鍵路徑(寫入 gate、重新校正)與設定頁顯示都自足即時採樣
    # (用自己拿到的 hwnd 取 title 現算,見 _his_write_verdict_for),故無跨緒覆寫/清空競態。
    return call_with_timeout(_enum, WIN_ENUM_TIMEOUT_SEC, default=0,
                             name="find_hospital_main_window")


def _send_yiling_menu_command(hwnd: int, menu_id: int) -> bool:
    """對主程式視窗送 WM_COMMAND 觸發 menu 項目。

    用 PostMessage (非同步)：實測 (2026-05-18 12:43 F9) 用 SendMessage 會卡 11+ 秒
    沒回應——當 hospital app 處理 WM_COMMAND 開新 modal 視窗時，handler
    可能 block。Post 不會 hang，主程式有空就會處理。後續用 _wait_for_window
    poll 視窗出現。

    HIWORD(wParam)=0 表示來源是 menu (不是 accelerator/control)。
    F3/F4 觸發代碼輸入 (id=219) 用 Send 跑得通，是因為代碼輸入是輕量 UI
    操作（focus 跳到 grid）；開 modal 同意書視窗 (id=669) 重量級。"""
    if not hwnd:
        return False
    WM_COMMAND = 0x0111
    try:
        ok = ctypes.windll.user32.PostMessageW(hwnd, WM_COMMAND, menu_id, 0)
        if not ok:
            logging.warning("PostMessageW WM_COMMAND menu_id=%s 失敗 hwnd=%s",
                            menu_id, hwnd)
            return False
        return True
    except Exception:
        logging.warning("PostMessageW WM_COMMAND menu_id=%s 例外 hwnd=%s",
                        menu_id, hwnd, exc_info=True)
        return False


def _his_title_of(hwnd: int) -> str:
    """取 hwnd 視窗標題(逾時保護:HIS GUI 凍結時 GetWindowTextW 會無限阻塞,包
    call_with_timeout 讓熱鍵不被卡死)。取不到回 ""（→ 採樣 None → 裁決 UNKNOWN → 放行）。"""
    if not hwnd:
        return ""

    def _get() -> str:
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ""

    return call_with_timeout(_get, WIN_ENUM_TIMEOUT_SEC, default="",
                             name="his_title_of")


def _ensure_hospital_foreground(hwnd: int) -> None:
    """確保主程式視窗在前景，這樣後續 pyautogui.typewrite 才會打進去。
    SetForegroundWindow 在 admin 行程通常能成功。"""
    try:
        # 若已 minimize 先還原
        SW_RESTORE = 9
        if ctypes.windll.user32.IsIconic(hwnd):
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        logging.debug("_ensure_hospital_foreground 失敗", exc_info=True)


def _get_thread_focus(target_hwnd: int) -> int:
    """取得 target_hwnd 那個 thread 內目前焦點的 control hwnd。

    cross-thread 的 GetFocus 預設回 0；要用 AttachThreadInput 把當前 thread
    跟 target thread 連起來才能讀。用於知道「使用者鍵盤輸入會送到哪個 control」。"""
    if not target_hwnd:
        return 0
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        target_tid = user32.GetWindowThreadProcessId(target_hwnd, None)
        cur_tid = kernel32.GetCurrentThreadId()
        if target_tid == cur_tid:
            return user32.GetFocus()
        user32.AttachThreadInput(cur_tid, target_tid, True)
        try:
            return user32.GetFocus()
        finally:
            user32.AttachThreadInput(cur_tid, target_tid, False)
    except Exception:
        return 0


def _wait_for_code_input_focus(target_hwnd: int, *,
                               previous_focus: int = 0,
                               timeout: float = 0.6,
                               poll: float = 0.03) -> int:
    """等代碼輸入 menu 讓焦點移到可輸入控件，避免固定 sleep 抓到舊焦點。

    [M2 2026-07-09 + codex] previous_focus 讀不到(=0)時【不可】只憑「有 edit 就通過」——若選單命令
    沒生效(如 HIS 改版 menu id 漂移),焦點可能仍停在醫師的病歷 TMemo/TRichEdit,或病人其他一般
    TEdit 欄位 → 代碼(51019 等)會被打進錯的欄位。改嚴格:previous_focus 未知時必須【正面辨識】
    為格線內嵌代碼輸入器(class 含 inplace / grid),不接受一般 edit/memo/rich;寧可 return 0
    (caller 放棄自動化,交人工)也不把代碼 key 到不確定的欄位。有 previous_focus 時維持原本行為。"""
    end_t = time.time() + timeout
    # 代碼輸入欄是 Delphi 格線的內嵌編輯器(TInplaceEdit / TStringGrid…),用這個正面辨識。
    _GRID_CODE_EDITOR = ("inplace", "grid")
    while time.time() < end_t:
        focus = _get_thread_focus(target_hwnd)
        if focus and focus != target_hwnd:
            cls = _get_class_name_of(focus).lower()
            if previous_focus:
                # 有前焦點:input-like 且焦點確實已從舊焦點移開(維持原本行為)。
                is_input_like = any(
                    s in cls for s in ("edit", "memo", "rich", "grid"))
                if is_input_like and focus != previous_focus:
                    return focus
            else:
                # 前焦點未知:嚴格 —— 只收正面辨識為格線內嵌代碼輸入器者,其餘(一般 edit/memo/
                # rich/病人其他欄位)一律不收,回 0 交人工,避免把代碼 key 錯地方。
                if any(s in cls for s in _GRID_CODE_EDITOR):
                    return focus
        _sleep_interruptible(poll)
    return 0


def _wm_settext_timeout(hwnd: int, text: str, timeout_ms: int = 2500) -> int:
    """WM_SETTEXT，但走 SendMessageTimeout(SMTO_ABORTIFHUNG)。

    [stability] 原本裸 SendMessageW(WM_SETTEXT) 對跨行程視窗是同步阻塞：醫院
    Delphi app 的 GUI 緒卡住(server roundtrip freeze / modal)時會無限期卡住
    hotkey 工作緒 → run_subsystem 的 finally 跑不到 → _subsystem_running 永遠
    True → 之後所有熱鍵失效。改走帶逾時的版本，最多等 timeout_ms 就放棄。
    透過 _send_message_timeout 傳「字串緩衝位址」(OS 會跨行程 marshal WM_SETTEXT)。
    回傳 LRESULT（成功為非 0）。"""
    try:
        buf = ctypes.create_unicode_buffer(text or "")
        addr = ctypes.cast(buf, ctypes.c_void_p).value or 0
        return int(_send_message_timeout(hwnd, 0x000C, 0, addr,
                                         timeout_ms=timeout_ms))  # WM_SETTEXT
    except Exception:
        logging.debug("_wm_settext_timeout 失敗 hwnd=%s", hwnd, exc_info=True)
        return 0


def _wm_gettext_timeout(hwnd: int, timeout_ms: int = 2500) -> str:
    """WM_GETTEXTLENGTH + WM_GETTEXT，走 SendMessageTimeout(SMTO_ABORTIFHUNG)。
    理由同 _wm_settext_timeout（避免讀文字時被凍住的醫院 app 永久阻塞）。"""
    try:
        n = _send_message_timeout(hwnd, 0x000E, 0, 0,
                                  timeout_ms=timeout_ms)  # WM_GETTEXTLENGTH
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        addr = ctypes.cast(buf, ctypes.c_void_p).value or 0
        _send_message_timeout(hwnd, 0x000D, n + 1, addr,
                              timeout_ms=timeout_ms)  # WM_GETTEXT
        return buf.value or ""
    except Exception:
        logging.debug("_wm_gettext_timeout 失敗 hwnd=%s", hwnd, exc_info=True)
        return ""


def _send_chars_to_window(hwnd: int, text: str) -> bool:
    """送 WM_CHAR 一字一字到目標 control。完全繞過 IME。

    pyautogui.typewrite 走 OS keyboard input → IME 攔截（中文模式下「5」被當組
    字輸入）。WM_CHAR 直接到 control，IME 沒機會攔截。

    [stability] 改用 PostMessageW（非同步）取代 SendMessageW：後者對跨行程視窗
    是同步阻塞，醫院 app 凍住時會無限期卡住 hotkey 工作緒並永久鎖死全部熱鍵。
    PostMessage 立即返回、訊息照 FIFO 入該 control 佇列由 Delphi 依序處理。"""
    if not hwnd or not text:
        return False
    WM_CHAR = 0x0102
    try:
        user32 = ctypes.windll.user32
        for ch in text:
            # [UD-12 2026-07-12] 逐字前確認視窗仍在:編輯器中途被關(hwnd 失效)即中止回 False,交
            # caller 走警示,避免代碼欄殘留半截醫令(原本不論如何一律回 True)。
            if not user32.IsWindow(hwnd):
                logging.warning("[send_chars] 目標視窗中途消失,中止(已送部分字元)")
                return False
            user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 0)
            time.sleep(0.02)  # 給 Delphi 依序處理（非同步下保險）
        return True
    except Exception:
        logging.error("_send_chars_to_window 失敗", exc_info=True)
        return False


def _send_enter_to_window(hwnd: int) -> bool:
    """送 VK_RETURN keydown+up 到指定 control。

    [stability] 同 _send_chars_to_window：改用 PostMessageW 非同步送，避免被
    凍住的醫院 app 同步阻塞 hotkey 工作緒。"""
    if not hwnd:
        return False
    try:
        WM_KEYDOWN = 0x0100
        WM_KEYUP = 0x0101
        VK_RETURN = 0x0D
        # 只送 keydown+keyup（等同真人按「一次」Enter）。Delphi VCL 的訊息迴圈會自行
        # TranslateMessage 把 WM_KEYDOWN(VK_RETURN) 轉成對應的 WM_CHAR(\r)，控制項
        # 因而收到「剛好一次」Enter。
        # [修正 2026-06-01] 原本在 keydown/keyup 之後又額外 PostMessage 一個 WM_CHAR \r，
        # 等於控制項收到兩個 Enter 字元（keydown 被翻譯出的 \r + 這個多餘的 \r）→
        # 醫令代碼被送出兩次 → F1/F2/F3/F4/F5 跳「資料重複確認」。移除多餘的 WM_CHAR。
        ctypes.windll.user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0x1C0001)
        ctypes.windll.user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, 0xC01C0001)
        return True
    except Exception:
        return False


def _force_ime_english(hwnd: int = 0) -> None:
    """把當前前景視窗（或指定 hwnd）的 IME 切到英文模式（關閉 IME 轉換）。

    用 ImmSetOpenStatus(himc, False) 對 IME context 設「不開」=「直接送
    英文字」。對 Delphi VCL 應用通常立刻生效，不會像 Ctrl+Space 那樣依賴
    使用者 IME 設定。
    為什麼必要：使用者中文輸入法（注音/新酷音/微軟拼音）打開時，
    pyautogui.typewrite("51017") 的 "5" 會被 IME 攔截當作組字輸入，
    結果什麼都沒寫進輸入欄。強制切英文徹底避免這個問題。"""
    try:
        imm32 = ctypes.windll.imm32
        target = hwnd or ctypes.windll.user32.GetForegroundWindow()
        if not target:
            return
        himc = imm32.ImmGetContext(target)
        if himc:
            try:
                imm32.ImmSetOpenStatus(himc, False)
            finally:
                imm32.ImmReleaseContext(target, himc)
    except Exception:
        logging.debug("_force_ime_english 失敗（IME 模組不可用？忽略）", exc_info=True)


def _script_code_input_adaptive(code: str, label: str = "",
                                  set_療程=None) -> bool:
    """共通流程：找視窗 → SendMessage 觸發代碼輸入 → 等焦點 → 打代碼 → Enter。
    可選 set_療程：完成代碼輸入後動態找頂部「療程」欄並改成該值。

    code="" 時跳過 typewrite + Enter（只開啟代碼輸入 dialog，由使用者手動
    輸入代碼+Enter）。用於 F5 KOH 場景。

    set_療程=None 表示不改 療程（F4 冷凍 / F5 KOH 用）。
    set_療程=1/2/3 用於 F1/F2/F3 照光不同療程次數。

    回傳 True 表示代碼輸入與可選療程修改皆確認完成；False 表示任一步驟失敗。"""
    hwnd = _find_hospital_main_window()
    if not hwnd:
        logging.warning("[%s adaptive] 找不到主程式視窗 (class=%s, title 含 %s)",
                        label or "code-input", _HOSPITAL_WIN_CLASS,
                        _HOSPITAL_WIN_TITLE_KW)
        return False
    workflow_ok = True
    _ensure_hospital_foreground(hwnd)
    time.sleep(0.03)
    # 雖然我們用 WM_CHAR 不經 IME，但 _force_ime_english 留著保險（萬一某
    # 控制項對 IME 狀態敏感）
    _force_ime_english(hwnd)
    previous_focus = _get_thread_focus(hwnd)
    if not _send_yiling_menu_command(hwnd, MENU_ID_代碼輸入):
        logging.warning("[%s] 代碼輸入 menu command 送出失敗", label)
        _mark_hotkey_action_time()
        return False
    # 等焦點移到醫令代碼欄；快時立即通過，慢時最多等 0.6 秒。
    focused = _wait_for_code_input_focus(hwnd, previous_focus=previous_focus)
    _force_ime_english(hwnd)
    check_stop()
    # code 非空才打字 + Enter
    if code:
        # 代碼輸入觸發後，焦點應在 grid 內的 inplace edit。
        if focused:
            logging.info("[%s] 焦點 hwnd=%s (cls=%s)，用 WM_CHAR 送 %r",
                          label, focused, _get_class_name_of(focused), code)
            chars_ok = _send_chars_to_window(focused, code)
            time.sleep(0.05)
            check_stop()
            enter_ok = _send_enter_to_window(focused)
            if not (chars_ok and enter_ok):
                logging.warning("[%s] 代碼輸入訊息送出不完整 chars=%s enter=%s",
                                label, chars_ok, enter_ok)
                workflow_ok = False
            # [稽核 2026-07-17] 醫令代碼是後果最高的寫入,且【無回讀】(只確認訊息送出,
            # 不確認代碼真的落進欄位)。改版不擋之後,至少要留下「幾點、哪支熱鍵、送了什麼
            # 代碼、當時 HIS 版本/金絲雀裁決」的紀錄,事後查得出寫錯了什麼。
            _record_his_action(
                _LEDGER_HIS_MENU, f"{label or '代碼輸入'} 醫令代碼", main_hwnd=hwnd,
                target=f"menu:{MENU_ID_代碼輸入}", value=str(code),
                outcome=_LEDGER_OK if (chars_ok and enter_ok) else _LEDGER_FAILED,
                detail="" if (chars_ok and enter_ok)
                       else f"chars={chars_ok} enter={enter_ok}")
        else:
            logging.warning("[%s] 等不到可信的代碼輸入焦點，停止送出 %r",
                            label, code)
            workflow_ok = False
            # 焦點沒落在代碼輸入欄 → 沒有真的送出(選單 id 位移時就是走這條)
            _record_his_action(
                _LEDGER_HIS_MENU, f"{label or '代碼輸入'} 醫令代碼", main_hwnd=hwnd,
                target=f"menu:{MENU_ID_代碼輸入}", value=str(code),
                outcome=_LEDGER_SKIPPED, detail="等不到可信的代碼輸入焦點,未送出")
    if code and not workflow_ok:
        logging.warning("[%s] 代碼輸入未完成，跳過療程欄位修改", label)
        _mark_hotkey_action_time()
        return False
    # 可選：改 療程 欄位 — 用 WM_SETTEXT 直接設值（繞 IME、不動滑鼠）
    if set_療程 is not None:
        time.sleep(0.08)  # 從 0.15s 降到 0.08s
        check_stop()
        if not _set_療程_only(hwnd, set_療程, label=label):
            workflow_ok = False
    _mark_hotkey_action_time()
    return workflow_ok


def _set_療程_only(main_hwnd: int, value, label: str = "") -> bool:
    """只設頂部「療程」欄=value(WM_SETTEXT→失敗 fallback click→寫回後 read-verify),
    不經代碼輸入。供 F1/F2/F3 的 set_療程,以及 F1 純 Excimer(填療程但不 key 51019)
    共用 —— 單一實作避免分歧。任一步失敗或驗證不符回 False。"""
    liaocheng_hwnd = _find_療程_edit_hwnd(main_hwnd)
    if not liaocheng_hwnd:
        logging.warning("[%s] 找不到 療程 欄位（請手動填）", label)
        return False
    # [UD-05 audit 2026-07-12] 寫入前正向把關(比照 _set_身份_自費):療程欄合法原值=空白或個位
    # 數字(1/2/3)。定位到的欄原值若「非空且非個位數」→ 疑似版面漂移抓錯窄欄 → 不寫,交醫師手動,
    # 避免把 1/2/3 寫進別欄(且身份欄以療程為錨、連帶錯)。讀不到(空)則放行,由寫後 verify 兜底。
    try:
        _療程_before = (_read_tmemo_text(liaocheng_hwnd) or "").strip()
    except Exception:
        _療程_before = ""
    if _療程_before and not re.fullmatch(r"\d", _療程_before):
        logging.warning("[%s] 療程欄原值 %r 不像療程(非空且非個位數)→ 疑似定位錯欄,不寫",
                        label, _療程_before)
        _show_uvb_warning(main_hwnd, "療程未自動設定",
                          f"自動定位到的療程欄內容看起來不對(原值:{_療程_before!r})。\n"
                          f"請醫師手動把療程改成 {value}。")
        # [codex P1] 【不可】把 _療程_before 原文寫進帳本:這條分支正是「疑似抓錯欄位」時
        # 觸發的,那個欄位的內容可能是姓名/病歷號/卡號等識別資料。只記固定原因 + 長度。
        _record_his_action(_LEDGER_HIS_FIELD, f"{label or 'F'} 療程", main_hwnd=main_hwnd,
                           target="field:療程", value=str(value),
                           outcome=_LEDGER_SKIPPED,
                           detail=f"原值不像療程(長度={len(_療程_before)},內容已遮罩),"
                                  f"疑似定位錯欄,未寫")
        return False
    ok = True
    try:
        ret = _wm_settext_timeout(liaocheng_hwnd, str(value))
        logging.info("[%s] 療程欄位 (hwnd=%s) WM_SETTEXT='%s'",
                     label, liaocheng_hwnd, value)
        if not ret:
            logging.warning("[%s] WM_SETTEXT 療程 回傳失敗，fallback click", label)
            if not _replace_edit_text(liaocheng_hwnd, str(value), main_hwnd=main_hwnd):
                ok = False
    except Exception:
        logging.warning("[%s] WM_SETTEXT 療程 失敗，fallback click", label,
                        exc_info=True)
        if not _replace_edit_text(liaocheng_hwnd, str(value), main_hwnd=main_hwnd):
            ok = False
    actual_療程 = None
    try:
        actual_療程 = _read_tmemo_text(liaocheng_hwnd).strip()
        if actual_療程 != str(value):
            logging.warning("[%s] 療程欄位驗證失敗，預期=%s 實際=%r",
                            label, value, actual_療程)
            ok = False
    except Exception:
        logging.debug("[%s] 療程欄位驗證例外", label, exc_info=True)
    # [稽核 2026-07-17] 記錄「寫了什麼 + 回讀對不對」。回讀不符(mismatch)正是改版寫錯欄
    # 位時最關鍵的事後線索。
    # [codex P1] 回讀原文同樣可能是誤抓到的識別資料 → 只記長度,不記內容。
    _record_his_action(
        _LEDGER_HIS_FIELD, f"{label or 'F'} 療程", main_hwnd=main_hwnd,
        target="field:療程", value=str(value),
        outcome=_LEDGER_OK if ok else _LEDGER_MISMATCH,
        detail="" if ok else
               f"回讀與預期不符(回讀長度={len(actual_療程 or '')},內容已遮罩)")
    return ok


def _read_tmemo_text(hwnd: int) -> str:
    """讀 TMemo 全文。[stability] 走帶逾時的 WM_GETTEXT(SMTO_ABORTIFHUNG)，避免
    被凍住的醫院 app 同步阻塞 hotkey 工作緒（原裸 SendMessageW 會無限期卡住）。"""
    return _wm_gettext_timeout(hwnd)


def _find_disposition_memo(main_hwnd: int, keywords: tuple = ("UVB",)) -> int:
    """[v20.1 2026-05-26] 找處置 TMemo — 寬鬆 class 比對 + 內容過濾。

    原本只試 class="TMemo"，但醫院程式的處置欄可能是 TMemoExt / TDBMemo /
    TRichEdit / TRichEdit95 等 Delphi 變體。改成：
      1. 列舉所有 descendant
      2. class 名稱含 "Memo" 或 "Edit" 或 "Rich" (case-insensitive)
      3. text 含 keywords 任一 (UVB / Phototherapy / 光療…)
      4. 回第一個 match

    [2026-06-01] keyword 改 keywords(tuple)：處置行可能寫 "phototherapy"/"光療"
    而非 "UVB"(曾大鈞實機 case)，需多關鍵字才找得到。

    同時 log 出所有候選 (class + hwnd + text 前 80 字) 給 debug 用。
    """
    found = [0]
    candidates: list[tuple[int, str, str]] = []  # (hwnd, class, text_preview)
    keywords_lower = tuple(str(k).lower() for k in keywords)

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 64)
            cls = cls_buf.value
            cls_lower = cls.lower()
            # 寬鬆比對：class 含 memo / edit / rich
            if not any(s in cls_lower for s in ("memo", "edit", "rich")):
                return True
            text = _read_tmemo_text(child)
            if not text:
                return True
            preview = text[:80].replace("\n", " ").replace("\r", "")
            candidates.append((child, cls, preview))
            text_lower = text.lower()
            if any(k in text_lower for k in keywords_lower):
                found[0] = child
                return False  # stop enumeration
        except Exception:
            pass
        return True

    try:
        ctypes.windll.user32.EnumChildWindows(main_hwnd, cb, 0)
    except Exception:
        logging.debug("EnumChildWindows 例外", exc_info=True)

    if found[0]:
        # 找到了：log 命中的那個
        for h, c, p in candidates:
            if h == found[0]:
                logging.info("[UVB][find] 命中處置 hwnd=%s class='%s' "
                              "text='%s...'", h, c, p)
                break
    else:
        # 沒找到：log 所有候選給 user debug
        if candidates:
            logging.warning(
                "[UVB][find] 找不到含 '%s' 的 Memo/Edit 控件。所有候選 "
                "(%d 個):", "/".join(keywords_lower), len(candidates))
            for h, c, p in candidates[:10]:  # 最多印 10 個避免洗 log
                logging.warning("[UVB][find]   hwnd=%s class='%s' "
                                "text='%s...'", h, c, p)
        else:
            logging.warning(
                "[UVB][find] 主視窗下完全沒抓到任何 Memo/Edit 控件 — "
                "可能 class 命名跟想的不同，請拍 snapshot")
    return found[0]


def _write_tmemo_text(hwnd: int, new_text: str) -> bool:
    """寫 TMemo 全文。[stability] 走帶逾時的 WM_SETTEXT(SMTO_ABORTIFHUNG)，避免
    被凍住的醫院 app 同步阻塞 hotkey 工作緒。"""
    return bool(_wm_settext_timeout(hwnd, new_text))


def _show_uvb_warning(main_hwnd: int, title: str, msg: str) -> None:
    """[v20.5 2026-05-26] 統一的 UVB 警告 MessageBox — 三管齊下強制曝光:
      1. owner = 醫院程式 main_hwnd → modal 到醫院程式之上
      2. flags: MB_ICONWARNING + MB_TOPMOST + MB_SETFOREGROUND
      3. winsound beep + FlashWindowEx 醫院視窗 5 次
    """
    try:
        import winsound
        winsound.MessageBeep(0x30)  # MB_ICONWARNING beep
    except Exception:
        pass
    if main_hwnd:
        try:
            class FLASHWINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_uint),
                    ("hwnd", wintypes.HWND),
                    ("dwFlags", ctypes.c_uint),
                    ("uCount", ctypes.c_uint),
                    ("dwTimeout", ctypes.c_uint),
                ]
            fi = FLASHWINFO(
                cbSize=ctypes.sizeof(FLASHWINFO),
                hwnd=main_hwnd,
                dwFlags=0x03,  # FLASHW_ALL (caption + taskbar)
                uCount=5,
                dwTimeout=0,
            )
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(fi))
        except Exception:
            logging.debug("FlashWindowEx 例外", exc_info=True)
    try:
        # MB_ICONWARNING(0x30) | MB_TOPMOST(0x40000) | MB_SETFOREGROUND(0x10000)
        flags = 0x30 | 0x40000 | 0x10000
        # [UD-13 2026-07-12] 阻塞對話框期間標記「等待使用者」,watchdog 不誤報 keep_stuck
        # (比照 _photo_confirm_yesno)。
        with _hotkey_awaiting_user_scope():
            ctypes.windll.user32.MessageBoxW(main_hwnd, msg, title, flags)
    except Exception:
        logging.debug("MessageBox 例外", exc_info=True)


def _photo_confirm_yesno(main_hwnd: int, title: str, intro: str, reason: str,
                         *, tag: str = "UVB", label: str = "F2") -> bool:
    """[2026-06-29] F2/F3 醫囑流程統一的 Yes/No 確認元件(stale 舊紀錄 / 劑量超限共用)。

    原本 excimer 與 UVB 兩條路徑各有一份幾乎相同的確認對話(beep + MessageBoxW),收斂成此單一元件 ——
    日後要改確認文案 / 旗標 / 行為只需動一處。Windows MessageBoxW:
      MB_ICONQUESTION|YESNO|TOPMOST|SETFOREGROUND|DEFBUTTON2(預設『否』,避免隨手 Enter 通過異常劑量)。
    訊息固定 "{intro}\\n\\n{reason}\\n\\n要繼續執行變更嗎?"。
    回 True=按『是』;按『否』/取消/MessageBoxW 失敗一律回 False(保守:不自動更新),失敗會記 traceback。"""
    try:
        import winsound
        winsound.MessageBeep(0x30)
    except Exception:
        pass
    # MB_ICONQUESTION(0x20)|MB_YESNO(0x4)|MB_TOPMOST(0x40000)|MB_SETFOREGROUND(0x10000)
    # |MB_DEFBUTTON2(0x100,預設『否』)
    flags = 0x20 | 0x4 | 0x40000 | 0x10000 | 0x100
    try:
        with _hotkey_awaiting_user_scope():
            ans = ctypes.windll.user32.MessageBoxW(
                main_hwnd, f"{intro}\n\n{reason}\n\n要繼續執行變更嗎?", title, flags)
    except Exception:
        logging.exception("[%s][%s] CONFIRM_NEEDED MessageBoxW 失敗", label, tag)
        return False   # 失敗保守視為『否』:不自動更新劑量
    return ans == 6   # IDYES


# [W7 2026-07-03] 半套帳務精準對帳:記錄本次 F2/F3 實際寫回的 UVB「原值→新值」,供
# 「UVB 已寫、但 51019/療程失敗」時的警告精準列出,醫師可據以【手動補 51019+療程】或
# 【手動把 UVB 改回原值】——保守路線,不自動 rollback(再寫一次也可能失敗、更難收拾)。
# 每次 _update_uvb_dose_core 開頭清空,只在確實寫回+verify 通過後填;只反映本次流程
# (熱鍵序列化執行,無並發)。None = 本次沒有實際改動 UVB。
_last_uvb_write = None


def _record_uvb_write(label, old_dose, old_count, new_dose, new_count,
                      extra_lines: int = 0) -> None:
    # [UD-11 2026-07-12] extra_lines=同批一併更新的其他行/續行/uncertain 行數 —— W7
    # 半套警告的「(B) 改回原值」若只提主行,醫師會漏還原這些行,一併記錄供文案提醒。
    global _last_uvb_write
    _last_uvb_write = {
        "label": label, "old_dose": old_dose, "old_count": old_count,
        "new_dose": new_dose, "new_count": new_count,
        "extra_lines": extra_lines,
    }


def _fmt_uvb_dc(dose, count) -> str:
    d = "?" if dose is None else str(dose)
    c = "（未寫次數）" if count is None else str(count)
    return f"劑量 {d}、次數 {c}"


def _show_light_code_incomplete_warning(label: str, set_療程: int,
                                        *, uvb_already_updated: bool) -> None:
    """照光代碼/療程未確認完成時，用前景警告避免半套狀態被忽略。
    [W7] 若本次確實改過 UVB(_last_uvb_write 有值)→ 精準列出原值→新值 + 兩個手動選項。"""
    main_hwnd = _find_hospital_main_window()
    rec = _last_uvb_write
    if uvb_already_updated and isinstance(rec, dict) and rec.get("label") == label:
        old = _fmt_uvb_dc(rec.get("old_dose"), rec.get("old_count"))
        new = _fmt_uvb_dc(rec.get("new_dose"), rec.get("new_count"))
        # [UD-11] 同批還更新了其他行/續行/uncertain 行 → (B) 只改回主行不完整,追加提醒
        _extra = rec.get("extra_lines") or 0
        extra_note = (
            f"\n    (注意:另有 {_extra} 處其他光療行/同行續行的次數/日期"
            f"也已一併更新,選 (B) 時請一併改回。)"
        ) if _extra else ""
        msg = (
            f"⚠ {label} 半套狀態需要你手動處理:\n\n"
            f"● UVB 處置【已寫回】:\n    原 {old}\n    → 已改為 {new}\n"
            f"● 但 51019 醫令 或 療程 {set_療程} 【沒有】確認完成。\n\n"
            f"請二選一手動處理:\n"
            f"(A) 補齊:手動下 51019 醫令、並把療程欄設為 {set_療程}(維持這次的新劑量);或\n"
            f"(B) 取消:把 UVB 劑量/次數手動改回原值({old})。{extra_note}\n\n"
            f"務必擇一完成再送出,以免病歷只改了劑量卻沒有對應醫令。"
        )
    elif uvb_already_updated:
        # 走到 UVB 步驟成功但本次沒有實際改動 UVB(例如無 UVB 行可改)→ 不誤稱「已更新」
        msg = (
            f"{label} 的 51019 或療程 {set_療程} 沒有確認完成。\n\n"
            f"請檢查醫令是否已有 51019、療程欄位是否為 {set_療程},並手動補齊。"
        )
    else:
        msg = (
            f"{label} 的 51019 或療程 {set_療程} 沒有確認完成。\n\n"
            f"UVB 尚未更新；請檢查醫令與療程欄位後手動處理。"
        )
    _show_uvb_warning(main_hwnd, "照光代碼輸入未完成", msg)


# [stability r4] F2/F3 UVB 會用阻塞式 MessageBoxW 同步等醫師按 Yes/No(劑量異常確認、
# uncertain 行確認)。期間熱鍵工作緒「阻塞但不是卡死」，若醫師離開讓對話框開著超過
# HOTKEY_HARD_TIMEOUT_SEC(180s)，run_subsystem_in_thread 的硬上限看門狗會把它誤判成卡死
# 強制解除 _subsystem_running → 醫師回應前第二支熱鍵(F9/F10/F11 lenient)可重入、與仍卡在
# 對話框的第一流程並行操作同一 HIS 視窗 → 醫令重複 / 療程欄被覆寫。下面的 module 級旗標讓
# 看門狗辨識「正在等使用者」狀態而不強制解鎖(同一時間只有一個熱鍵流程在跑，旗標無並發歧義)。
# 讀寫單一 bool 在 CPython GIL 下為原子，看門狗只讀、context manager 以 try/finally 保證還原。
_hotkey_awaiting_user = False
_hotkey_cancelled_threads = set()
_hotkey_cancelled_threads_lock = threading.Lock()


def _hotkey_watchdog_action(still_ours: bool, alive: bool, awaiting: bool) -> str:
    """[W1 2026-07-03] 硬逾時看門狗的純決策(可測試):
      'gone'         → 流程已正常結束/被後續熱鍵取代,看門狗可退出。
      'clear_dead'   → 旗標殘留但 worker thread 已死(finally 沒清到)→ 清旗標(安全)。
      'keep_awaiting'→ 正卡在「等醫師回應對話框」→ 維持鎖定、再等一個週期。
      'keep_stuck'   → 卡住且 thread 仍活著 → 【維持鎖定】+警告,絕不解鎖。

    關鍵安全變更:舊版在 'keep_stuck' 情況會強制清 _subsystem_running(解鎖),但卡住的
    worker 仍活著、可能正卡在 HIS 半寫入狀態 → 放第二支熱鍵進來並行寫同一病歷/醫令
    會造成 billing 錯亂。改為『thread 還活著就絕不解鎖』;真的卡死請 F12/重啟(卡住的
    worker 一旦結束或 HIS 恢復,其 finally 會自行清旗標、熱鍵自動恢復)。"""
    if not still_ours:
        return "gone"
    if not alive:
        return "clear_dead"
    if awaiting:
        return "keep_awaiting"
    return "keep_stuck"


@contextlib.contextmanager
def _hotkey_awaiting_user_scope():
    """標記目前熱鍵流程正阻塞等待使用者回應(MessageBoxW)。離開時還原為先前狀態
    (支援巢狀)，即使對話框呼叫拋例外也保證清除，不會讓旗標永久卡 True。"""
    global _hotkey_awaiting_user
    prev = _hotkey_awaiting_user
    _hotkey_awaiting_user = True
    try:
        yield
    finally:
        _hotkey_awaiting_user = prev


def _collect_phototherapy_memos(main_hwnd: int) -> list:
    """列舉所有 memo/edit/rich 控件,回傳 text 含任一照光關鍵字者 [(hwnd, text)]。
    這裡的 'uv' 等寬鬆字串只是【粗篩】,真正分類交給 detect_phototherapy_kind(用
    \\bUV\\b 等精準規則),所以粗篩多收到的無關欄位會被歸成 none、不影響結果。"""
    items: list = []
    keywords = ("uvb", "紫外線", "phototherapy", "photo therapy", "光療",
                "excimer", "excime", "準分子", "uv")
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 64)
            cls_lower = (cls_buf.value or "").lower()
            if not any(s in cls_lower for s in ("memo", "edit", "rich")):
                return True
            text = _read_tmemo_text(child)
            if not text:
                return True
            tl = text.lower()
            if any(k in tl for k in keywords):
                items.append((child, text))
        except Exception:
            pass
        return True

    try:
        ctypes.windll.user32.EnumChildWindows(main_hwnd, cb, 0)
    except Exception:
        logging.debug("[Excimer] 列舉照光 memo 例外", exc_info=True)
    return items


def _resolve_phototherapy_disposition(main_hwnd: int):
    """回 (memo_hwnd, kind),kind ∈ "uvb" / "pure_excimer" / "ambiguous" / "none"。

    對【每一個】含照光關鍵字的 memo 各自用 detect_phototherapy_kind 分類(UVB-specific→uvb;
    有 excimer 無 UVB-specific→pure_excimer;只有泛稱光療→uvb_generic),再跨控件彙整:
      - 同時出現【UVB-specific 的 uvb】memo 與 pure_excimer memo(不同控件)→ "ambiguous"
        (真的無法判斷哪個是本次處置 → 交醫師手動)。
      - 泛稱光療(uvb_generic)+ excimer → excimer 涵蓋 → "pure_excimer"(不歧義):
        避免病史/轉介單的「refer for phototherapy」與處置的 excimer 互打而卡住 F2。
      - 只有 uvb/uvb_generic → "uvb";只有 pure_excimer → "pure_excimer";都沒有 → "none"。

    逐 memo 用 detect 分類(而非用關鍵字猜 UVB 側):即使現行 UVB 處置寫成 UVB-specific
    字眼,也會被正確歸成 uvb 而參與歧義判斷,不會被別控件的舊 excimer 靜默搶走
    (Codex 審查連續抓到的誤分流類別)。"""
    from cmuh_common.uvb_dose import (
        combine_phototherapy_kinds, detect_phototherapy_kind)
    # [2026-06-23 user] 傳今天進去 → 日期早於 2 個月前的照光段落(極可能已暫停)分流時忽略,
    # 避免「本月 UVB + 一年多前舊 excimer」被誤判成 ambiguous 而卡住 F2(實機:圖二)。
    today = date.today()
    uvb_hwnd = exc_hwnd = 0
    kinds = []
    for hwnd, text in _collect_phototherapy_memos(main_hwnd):
        k = detect_phototherapy_kind(text, today)
        kinds.append(k)
        if k in ("uvb", "uvb_generic") and not uvb_hwnd:
            uvb_hwnd = hwnd   # 泛稱光療也記為 UVB 更新目標(只有泛稱時 combine 會收斂成 uvb)
        elif k == "pure_excimer" and not exc_hwnd:
            exc_hwnd = hwnd
    combined = combine_phototherapy_kinds(kinds)
    if combined == "ambiguous":
        logging.warning(
            "[Excimer] 同時偵測到 UVB(hwnd=%s)與 Excimer(hwnd=%s)於不同欄位 "
            "→ ambiguous", uvb_hwnd, exc_hwnd)
        return uvb_hwnd, "ambiguous"
    if combined == "uvb":
        return uvb_hwnd, "uvb"
    if combined == "pure_excimer":
        return exc_hwnd, "pure_excimer"
    return 0, "none"


def _f1_phototherapy_route(label: str = "") -> str:
    """只讀:給 F1 在 key 51019 前判斷本次照光走向。回 "pure_excimer" / "ambiguous" /
    "normal"(uvb 或無照光處置都當 normal:F1 照常 51019,UVB 在 51019 後 best-effort
    更新)。找不到主視窗/例外一律 "normal"(安全方向:照常 51019,不誤跳健保)。"""
    try:
        main_hwnd = _find_hospital_main_window()
        if not main_hwnd:
            return "normal"
        _memo, kind = _resolve_phototherapy_disposition(main_hwnd)
        if kind == "ambiguous":
            return "ambiguous"
        if kind == "pure_excimer":
            return "pure_excimer"
        return "normal"
    except Exception:
        logging.exception(
            "[%s][Excimer] F1 照光分流偵測例外 → 當作 normal", label)
        return "normal"


# [2026-06-18] 純自費 Excimer 哨符:_update_uvb_dose_core 偵測到「只有 Excimer、
# 沒有 UVB」時回傳此值(truthy,所以 `if not res` 不會誤判成 abort;caller 用
# `res == _F23_PURE_EXCIMER` 區分,代表要設身份=01、不 key 51019/療程)。其餘路徑
# 仍回 True(正常)/False(中止),不需大改。
_F23_PURE_EXCIMER = "pure_excimer"


class _PureExcimerAbortedType(str):
    """[UD-14 codex 2026-07-12] 「純自費 Excimer 分流【成立】、但劑量更新中止
    (如 TOO_CLOSE)」的哨符 —— 分流結論與更新成敗分離,讓 F1 能對這種情況也跳
    分流矛盾警告(舊版此路徑回 False,分流資訊丟失)。

    刻意 falsy:任何未特別處理的呼叫端(F2/F3 的 `if not res`)一律當「中止」
    (fail-closed:不 key 51019、不設身份,與原本回 False 的行為完全相同)。"""
    def __bool__(self) -> bool:
        return False


_F23_PURE_EXCIMER_ABORTED = _PureExcimerAbortedType("pure_excimer_aborted")


def _f23_pure_excimer_update(main_hwnd: int, memo_hwnd: int, text: str,
                            label: str = "F2"):
    """[2026-06-19] 純自費 Excimer:把 excimer 劑量行更新(次數+1、日期→今天、劑量
    decay/加量並 cap 在 MAX,跟 UVB 一樣)後,回 _F23_PURE_EXCIMER(caller 設身份=01、
    跳過 51019/療程)。

    劑量更新是 best-effort:寫回失敗或非單純 UPDATED(上限/格式)只記錄/警示,
    【不影響身份=01】—— 這次就是自費 excimer visit,身份本來就該設 01。
    例外:TOO_CLOSE(間隔太短)回 falsy 的 _F23_PURE_EXCIMER_ABORTED —— 中止、
    不設身份(同一般 UVB),但保留「分流=純 excimer」結論供 F1 矛盾警告([UD-14])。"""
    try:
        from cmuh_common.uvb_dose import UvbAction, update_uvb_in_text
        result = update_uvb_in_text(text)
        if result.action == UvbAction.UPDATED and result.new_text:
            check_stop()   # [UD-04] 寫回處置欄(唯一直接改病歷的動作)前的最終取消閘門
            if _write_tmemo_text(memo_hwnd, result.new_text):
                logging.info(
                    "[%s][Excimer] 劑量已更新(次數→%s、日期→今天、劑量→%s)",
                    label, result.new_count, result.new_dose)
            else:
                logging.warning(
                    "[%s][Excimer] 劑量寫回失敗(身份仍會設 01)", label)
                _show_uvb_warning(
                    main_hwnd, "Excimer 劑量寫回失敗",
                    "自費 Excimer 的劑量自動更新寫回失敗。\n身份仍會設為自費(01),"
                    "但請醫師手動確認處置的次數/日期/劑量。")
        elif result.action == UvbAction.TOO_CLOSE:
            # [2026-06-25 user] 距上次照光 < 2 天 → 同一般 UVB:跳提示、不加劑量、【不設身份】。
            logging.warning(
                "[%s][Excimer] 距上次照光僅 %s 天 → 不更新劑量、不設身份(同一般 UVB)",
                label, result.days_diff)
            _show_uvb_warning(
                main_hwnd, "Excimer 照光間隔太短",
                f"距上次照光僅 {result.days_diff} 天。\n\n"
                "未自動更新劑量,也【未設定身份(自費 01)】。\n"
                "若確實要照光,請醫師手動設定身份與劑量。")
            # [UD-14 codex] 回 falsy 哨符而非 False:F2/F3 的 `if not res` 行為不變
            # (中止、不設身份),但保留「分流=純 excimer」結論給 F1 判斷矛盾警告。
            return _F23_PURE_EXCIMER_ABORTED   # 不設身份、不續做(同一般 UVB 的太近 → 中止)
        elif result.action == UvbAction.CONFIRM_NEEDED:
            # [2026-06-24] 純 excimer 也可能跳 stale 確認(>1 月舊紀錄)/ 劑量超限確認。
            # 跳 Yes/No;Yes → 帶 skip 旗標重跑並寫回。No → 不改劑量(身份仍設 01)。
            confirm_reason = result.confirm_reason or "請確認"
            is_stale = "距今" in confirm_reason and "天" in confirm_reason
            dlg_title = (f"Excimer 距上次照光時間過長 - {label}" if is_stale
                         else f"Excimer 劑量超過建議上限 - {label}")
            dlg_intro = ("請確認是否要按舊紀錄繼續更新" if is_stale
                         else "請確認劑量")
            _confirmed = _photo_confirm_yesno(
                main_hwnd, dlg_title, dlg_intro,
                confirm_reason, tag="Excimer", label=label)
            # [UD-02/H4 2026-07-10] 不論 Yes/No,對話框返回後一律 check_stop:醫師停在對話框時按 F12
            # → interrupt 會清 _subsystem_running 並把本緒加入取消集合。舊版只在 Yes 分支有 check_stop,
            # 按【否】會 fall-through 到 return _F23_PURE_EXCIMER → caller 照樣寫身份=01,且醫師此刻可
            # 啟新熱鍵 → 兩流程並行操作 HIS(W1 力避)。移到 if 之前,涵蓋 Yes/No 兩分支。
            check_stop()
            if _confirmed:
                result = update_uvb_in_text(
                    text, skip_dose_sanity=True, skip_stale_check=True)
                if result.action == UvbAction.UPDATED and result.new_text:
                    # [audit 2026-07-12] 與 UPDATED 路徑(上方)一致:寫回處置欄前緊鄰的最終
                    # 取消閘門(check_stop 在 if _confirmed 前,但重算與寫回間再確認一次更保險)。
                    check_stop()
                    if _write_tmemo_text(memo_hwnd, result.new_text):
                        logging.info(
                            "[%s][Excimer] (確認後)劑量已更新(次數→%s、劑量→%s)",
                            label, result.new_count, result.new_dose)
                    else:
                        logging.warning(
                            "[%s][Excimer] (確認後)劑量寫回失敗(身份仍設 01)",
                            label)
                else:
                    logging.info("[%s][Excimer] 確認後 action=%s,未更新劑量",
                                 label, result.action)
            else:
                logging.info(
                    "[%s][Excimer] 使用者按否 → 劑量未更新(身份仍設 01)", label)
        else:
            logging.info(
                "[%s][Excimer] 劑量未自動更新(action=%s),身份仍設 01",
                label, result.action)
    except SubsystemInterrupted:
        # [H4 2026-07-09] F12 取消【必須】往上傳,不可被下面的 except Exception 吞掉
        # (否則舊 worker 會照樣寫回處置欄+設身份,違背 F12 取消語意)。
        raise
    except Exception:
        logging.exception("[%s][Excimer] 劑量更新例外(身份仍設 01)", label)
    return _F23_PURE_EXCIMER


def _update_uvb_dose_core(label: str, *, strict: bool,
                          codes_already_placed: str = ""):
    """[v20.9 2026-05-26] F1 / F2/F3 共用核心邏輯。

    [UD-09 2026-07-12] codes_already_placed:非空=呼叫端在本函式【之前】已輸入完成的
    醫令/療程描述(F1 是「先 51019+療程 後 UVB」)。更新失敗時附在警告文案,提醒醫師
    若因此決定不照光,記得手動刪除已下的醫令。F2/F3(先 UVB 後 51019)不傳。

    [2026-06-18] 回傳值:True(正常,可續 51019/療程)/ False(中止)/
    _F23_PURE_EXCIMER(純自費 Excimer — caller 應設身份=01、跳過 51019/療程)。

    strict=True (F2/F3): 沒 UVB / TMemo 空 / 找不到主視窗 → 警告 + return False
        (call site: F2/F3 — 第 2/3 次照光，處置一定要有 UVB 行)

    strict=False (F1): 上述情境 → 跳過 + return True
        (call site: F1 — 第一次照光，沒 UVB 是正常情況不警告)

    其他 uncertain (PARSE_FAIL / SANITY_FAIL / TOO_CLOSE / 寫回失敗) 兩個 mode
    都會跳警告 — 這些是真實異常，user 都該知道。

    回 True → caller 可繼續 (UPDATED / strict=False 路徑下的 best-effort skip)
    回 False → caller 應終止 (僅 strict=True 路徑會回此值)
    """
    global _last_uvb_write
    _last_uvb_write = None  # [W7] 清空;只在本次確實寫回 UVB 後才填,供半套對帳警告
    # [UD-09] 失敗警告的附註:醫令已先行輸入(F1)時提醒勿忘刪除;F2/F3 為空字串無影響。
    _placed_note = (
        f"\n\n注意:{codes_already_placed} 已在本次 {label} 先行輸入完成。\n"
        f"若醫師因此決定今天不照光,請記得手動刪除該醫令與療程設定。"
    ) if codes_already_placed else ""
    main_hwnd = _find_hospital_main_window()
    if not main_hwnd:
        if strict:
            logging.warning("[%s][UVB] 找不到主程式視窗 → 終止", label)
            _show_uvb_warning(
                0, "UVB 自動更新失敗",
                f"找不到西醫門診主視窗\n\n{label} 已停止，請檢查主程式狀態。")
            return False
        logging.info("[%s][UVB] 找不到主程式視窗 — 跳過 (best-effort)", label)
        return True

    # [2026-06-01/06-18] 找照光處置 memo + 分類(見 _resolve_phototherapy_disposition)。
    memo_hwnd, photo_kind = _resolve_phototherapy_disposition(main_hwnd)
    # [2026-06-18] UVB 與 excimer 分屬不同控件 → 無法自動判斷本次是健保 UVB 還是自費
    # Excimer → 一律警告並中止,交醫師手動(billing 風險方向都很糟,寧可不自動動作)。
    if photo_kind == "ambiguous":
        logging.warning(
            "[%s][Excimer] UVB 與 Excimer 分屬不同欄位 → 無法自動分流,中止", label)
        _show_uvb_warning(
            main_hwnd, "照光分流無法判斷",
            "處置/病歷同時偵測到 UVB 與 Excimer,且分屬不同欄位,\n"
            "無法自動判斷本次是健保 UVB 還是自費 Excimer。\n\n"
            f"{label} 已停止自動處理 — 請醫師手動下醫令並確認身份別。")
        return False if strict else True
    if not memo_hwnd:
        if strict:
            logging.warning(
                "[%s][UVB] 處置內無 UVB 行 (見 [UVB][find] candidates) "
                "→ 終止", label)
            _show_uvb_warning(
                main_hwnd, "UVB 自動更新失敗",
                f"處置欄找不到含 UVB 的內容\n\n"
                f"{label} 已停止 — 請確認:\n"
                f"  • 病人是否真的有照光療程?\n"
                f"  • 處置欄是否需要先填 UVB 行 (用 DITTO/醫師上次帶入)?\n\n"
                f"確認後請手動處理。")
            return False
        logging.info("[%s][UVB] 處置內無 UVB 行 — 跳過 (新病人正常情況)", label)
        return True

    text = _read_tmemo_text(memo_hwnd)
    if not text:
        if strict:
            logging.warning("[%s][UVB] TMemo hwnd=%s 讀文字為空 → 終止",
                            label, memo_hwnd)
            _show_uvb_warning(
                main_hwnd, "UVB 自動更新失敗",
                f"處置 TMemo 讀取為空\n\n{label} 已停止。")
            return False
        logging.info("[%s][UVB] TMemo 讀文字為空 — 跳過", label)
        return True

    # [v20.13 2026-05-26] 留紀錄 — repr() 把不可見字元/控制字元都印出來，
    # 方便事後 debug「為什麼 parse 失敗」之類問題 (前 500 字夠看 UVB 行)
    logging.info("[%s][UVB] memo hwnd=%s len=%d text=%r",
                 label, memo_hwnd, len(text), text[:500])

    # [2026-06-18/19] 照光分流:純自費 Excimer(已由 _resolve_phototherapy_disposition
    # 分類)→ 更新 excimer 劑量行(次數/日期/劑量,best-effort)後設身份=01、不 key
    # 51019/療程。劑量更新走 excimer 專屬路徑(excimer 行非 UVB,不能用 UVB 的 round-trip
    # verify),見 _f23_pure_excimer_update。
    if photo_kind == "pure_excimer":
        logging.info(
            "[%s][Excimer] 純自費 Excimer(無 UVB)→ 更新 excimer 劑量 + 身份設 01,"
            "不 key 51019/療程", label)
        return _f23_pure_excimer_update(main_hwnd, memo_hwnd, text, label)

    try:
        from cmuh_common.uvb_dose import update_uvb_in_text, UvbAction
    except Exception:
        logging.exception("[%s][UVB] import cmuh_common.uvb_dose 失敗", label)
        if strict:
            _show_uvb_warning(
                main_hwnd, "UVB 自動更新失敗",
                f"UVB 邏輯模組載入失敗\n\n{label} 已停止，請聯絡開發者。")
            return False
        return True

    result = update_uvb_in_text(text)

    # [v20.12 2026-05-26] CONFIRM_NEEDED — dose / MAX 超過建議上限 (1500 mj/cm2)
    # 跳 Yes/No dialog，按 Yes 重 call 帶 skip_dose_sanity=True 跳過上限檢查。
    # [v20.14 2026-05-26] 也用於 stale check (距上次 > 30 天) — confirm_reason
    # 文字會包含「天」字眼，title 也跟著改。
    # [UD-07 2026-07-12] 舊版按「是」後【同時】帶 skip_dose_sanity+skip_stale_check 重
    # call:一筆既 stale 又劑量超限的紀錄,醫師只確認了其中一種,另一種確認被靜默跳過。
    # 改成迴圈:每次只帶「已確認那種」的 skip flag,另一種 CONFIRM 會再跳一次窗;同種
    # confirm 帶了對應 flag 仍重複出現(理論上不可能)→ 保守中止,防無限跳窗。
    _skip_flags: dict = {}
    _confirmed_kinds: set = set()
    while result.action == UvbAction.CONFIRM_NEEDED:
        confirm_reason = result.confirm_reason or "原劑量或 MAX 超過建議上限"
        # [v20.17] 沒日期已改成 silent first-time update，不會再跳到這裡。
        # 這個 path 只剩 dose-over-limit / stale-record 兩種。
        is_stale = "距今" in confirm_reason and "天" in confirm_reason
        if is_stale:
            dialog_title = f"UVB 距上次照光時間過長 - {label}"
            dialog_intro = "請確認是否要按舊紀錄繼續更新"
            kind = "stale"
            skip_flag = "skip_stale_check"
        else:
            dialog_title = f"UVB 劑量超過建議上限 - {label}"
            dialog_intro = "請確認劑量"
            kind = "dose"
            skip_flag = "skip_dose_sanity"
        if kind in _confirmed_kinds:
            logging.warning(
                "[%s][UVB] CONFIRM_NEEDED (%s) 已確認過仍重複出現 → 保守中止",
                label, kind)
            _show_uvb_warning(
                main_hwnd, "UVB 確認流程異常",
                f"確認後仍重複要求同種確認(程式異常)。\n\n"
                f"{label} 已停止，請醫師確認處置後手動處理。{_placed_note}")
            return False if strict else True
        logging.info("[%s][UVB] CONFIRM_NEEDED (%s): %s — 跳 Yes/No 確認",
                     label, kind, confirm_reason)
        _confirmed = _photo_confirm_yesno(
            main_hwnd, dialog_title, dialog_intro, confirm_reason,
            tag="UVB", label=label)
        check_stop()
        if not _confirmed:   # 否 / 取消 / MessageBoxW 失敗 → 一律停止(保守)
            logging.info("[%s][UVB] CONFIRM_NEEDED user 按否/取消/失敗 → 停止", label)
            return False if strict else True
        _confirmed_kinds.add(kind)
        _skip_flags[skip_flag] = True
        logging.info("[%s][UVB] CONFIRM_NEEDED (%s) user 按是 → 重 call 帶 %s",
                     label, kind, skip_flag)
        result = update_uvb_in_text(text, **_skip_flags)

    # [v20.17] SILENT_SKIP — 處置有 UVB+dose 但缺 MAX/increase (e.g. 梁雯琳
    # "keep UVB 850 mj/cm2") → 不修改處置但繼續執行 51019+療程
    if result.action == UvbAction.SILENT_SKIP:
        logging.info("[%s][UVB] SILENT_SKIP — 處置 UVB 行結構不完整 "
                     "(只有 dose 沒 MAX)，不修改但繼續執行", label)
        return True

    if result.action == UvbAction.NO_UVB_LINE:
        # parse_uvb_line 找不到 — 對 F1 是正常情況、F2/F3 是異常
        if strict:
            logging.warning("[%s][UVB] NO_UVB_LINE → 終止", label)
            _show_uvb_warning(
                main_hwnd, "UVB 自動更新失敗",
                f"處置內找不到可解析的 UVB 行\n\n{label} 已停止。")
            return False
        logging.info("[%s][UVB] NO_UVB_LINE — 跳過 (新病人正常)", label)
        return True

    if result.action == UvbAction.PARSE_FAIL:
        logging.warning(
            "[%s][UVB] 處置有 UVB 但 parse 失敗 → 終止 "
            "(處置前 200 字: %r)", label, text[:200])
        _show_uvb_warning(
            main_hwnd, "UVB parse 失敗",
            f"處置含 UVB 字串但無法解析格式\n\n"
            f"預期: UVB 劑量mj/cm2 (次數) on (日期), increase N, MAX:N\n\n"
            f"{label} 已停止，請醫師確認處置欄 UVB 行格式後手動處理。{_placed_note}")
        return False

    if result.action == UvbAction.SANITY_FAIL:
        logging.warning("[%s][UVB] sanity check 失敗: %s → 終止",
                        label, result.sanity_reason)
        _show_uvb_warning(
            main_hwnd, "UVB 數值異常",
            f"處置 UVB 行的數值看起來不對:\n\n"
            f"{result.sanity_reason}\n\n"
            f"{label} 已停止，請醫師確認後手動處理。{_placed_note}")
        return False

    if result.action == UvbAction.TOO_CLOSE:
        last_str = result.last_date.strftime("%Y/%m/%d")
        logging.warning("[%s][UVB] 距上次 %s 僅 %d 天 → 終止",
                        label, last_str, result.days_diff)
        _show_uvb_warning(
            main_hwnd, "UVB 照光間隔太短",
            f"病人 {last_str} 已照光 (距今僅 {result.days_diff} 天)\n\n"
            f"間隔不足 ≥ 2 天 — 已停止 {label} 自動處理。\n"
            f"若仍要照光，請醫師確認後手動處理。{_placed_note}")
        return False

    # [v20.13 2026-05-26] UPDATED 但偵測到「不確定的其他 triplet」(日期不同
    # 於第一行 UVB) — 跳 Yes/No 給醫師確認:
    #   Yes = 套用全部 (含這些 triplet 的 count+1, date→今天) + 繼續執行
    #   No  = 取消，不寫入處置、不執行 51019 (避免半套更新)
    uncertain = getattr(result, 'uncertain_other_triplets', None) or []
    final_text = result.new_text
    _uncertain_applied = 0   # [UD-11] 實際套用的 uncertain 行數(供 W7 對帳記錄)
    if uncertain:
        lines_show = "\n".join(
            f"  • [{u['date'].strftime('%m/%d')} ({u['days_ago']}天前) "
            f"次數 {u['count']}]\n      {u['line'][:100]}"
            for u in uncertain
        )
        update_summary = (
            f"  • UVB: {result.parsed.dose}→{result.new_dose} mj/cm2\n"
            f"  • 次數: {result.parsed.count}→{result.new_count}\n"
            f"  • 日期: {result.last_date.strftime('%Y/%m/%d')}→今天"
        )
        msg = (
            f"處置內偵測到 {len(uncertain)} 行 (count) ... (日期) 看起來像光療"
            f"紀錄，\n但日期不同於第一行 UVB ({result.last_date.strftime('%Y/%m/%d')})\n\n"
            f"=== 不確定的行 ===\n{lines_show}\n\n"
            f"=== 程式預計更新 (確定) ===\n{update_summary}\n\n"
            f"是 = 套用上述更新 + 上面不確定的行也 count+1, 日期→今天，"
            f"並繼續執行 {label}\n"
            f"否 = 全部取消，不更新處置、不執行 {label}"
        )
        logging.info("[%s][UVB] uncertain others detected (%d) — 跳 Yes/No",
                     label, len(uncertain))
        for u in uncertain:
            logging.info("[%s][UVB]   uncertain: count=%d date=%s line=%r",
                         label, u['count'], u['date'], u['line'][:200])
        try:
            import winsound
            winsound.MessageBeep(0x30)
        except Exception:
            pass
        # MB_ICONQUESTION(0x20) | MB_YESNO(0x4) | MB_TOPMOST(0x40000)
        # | MB_SETFOREGROUND(0x10000) | MB_DEFBUTTON2(0x100) — 預設「否」
        flags = 0x20 | 0x4 | 0x40000 | 0x10000 | 0x100
        try:
            with _hotkey_awaiting_user_scope():
                ans = ctypes.windll.user32.MessageBoxW(
                    main_hwnd, msg,
                    f"UVB 偵測到不確定的其他行 - {label}", flags,
                )
        except Exception:
            logging.exception("[%s][UVB] uncertain MessageBoxW 失敗", label)
            return False if strict else True
        check_stop()
        if ans != 6:  # not IDYES
            logging.info("[%s][UVB] uncertain user 按否 → 終止 (不寫處置)",
                         label)
            return False if strict else True
        # Yes — 套用 uncertain triplets 到 new_text
        try:
            from cmuh_common.uvb_dose import apply_uncertain_updates
            final_text = apply_uncertain_updates(result.new_text, uncertain)
            _uncertain_applied = len(uncertain)
            logging.info("[%s][UVB] uncertain user 按是 → 套用 %d 個額外行",
                         label, len(uncertain))
        except Exception:
            logging.exception("[%s][UVB] apply_uncertain_updates 失敗", label)
            final_text = result.new_text
            # [UD-10 2026-07-12] 醫師剛按「是」承諾這些行會一併更新;套用失敗若靜默
            # fallback,醫師會誤以為額外行已更新。跳警告點名【沒有】更新;主行更新
            # (獨立產生、後續有 round-trip verify)照常寫回,不因額外行失敗放棄。
            _show_uvb_warning(
                main_hwnd, "UVB 額外行未更新",
                f"你剛按「是」要一併更新 {len(uncertain)} 行其他光療紀錄,"
                f"但套用時發生程式錯誤。\n\n"
                f"主要 UVB 行仍會正常更新;上述額外行【沒有】更新\n"
                f"(次數/日期維持原樣),請於 {label} 完成後手動檢查並自行更新。")

    # UPDATED: 寫回 TMemo
    # [UD-04 2026-07-10] 寫回處置欄是全鏈唯一直接改病歷的動作,卻是唯一沒有取消閘門的步驟:從
    # wrapper 的 check_stop 到此要經過『找主視窗+列舉控件+逐一 gettext(各 2.5s 上限)+parse』,
    # HIS 慢時達數秒,期間按 F12 原本完全不被理會、memo 照樣被覆寫。補上最終 check_stop(近零成本)。
    check_stop()
    if not _write_tmemo_text(memo_hwnd, final_text):
        logging.warning("[%s][UVB] WM_SETTEXT 寫回處置失敗 → 終止", label)
        _show_uvb_warning(
            main_hwnd, "UVB 寫回失敗",
            f"寫回處置欄失敗 (WM_SETTEXT)\n\n"
            f"{label} 已停止 — 處置欄未更新，請醫師確認後手動處理。{_placed_note}")
        _record_his_action(_LEDGER_HIS_FIELD, f"{label} UVB 劑量", main_hwnd=main_hwnd,
                           target="field:處置memo",
                           value=f"dose={result.new_dose} count={result.new_count}",
                           outcome=_LEDGER_FAILED, detail="WM_SETTEXT 寫回失敗")
        return False

    # 寫回後實機 read 驗證 — Delphi onChange 可能 reformat 過
    actual_text = _read_tmemo_text(memo_hwnd)
    if not actual_text:
        # [UD-08 2026-07-12] read-back 讀回空字串(HIS 卡頓/WM_GETTEXT 逾時)→ 無法驗證寫回是否
        # 成功。保守中止(strict):避免「寫回其實失敗但 51019 照下」的反向不一致無人把關;已跳警告
        # 請醫師手動核對(原本空字串會靜默跳過 verify、照樣續跑 51019)。
        logging.warning("[%s][UVB] 寫回後 read-back 空字串,無法驗證 → 保守中止", label)
        _show_uvb_warning(
            main_hwnd, "UVB 寫回無法驗證",
            f"{label} 寫回後讀不到處置內容,無法確認是否成功。\n請醫師手動核對後再送出。"
            f"{_placed_note}")
        _record_his_action(_LEDGER_HIS_FIELD, f"{label} UVB 劑量", main_hwnd=main_hwnd,
                           target="field:處置memo",
                           value=f"dose={result.new_dose} count={result.new_count}",
                           outcome=_LEDGER_FAILED, detail="回讀空字串,無法驗證寫回")
        return False
    if actual_text:
        from cmuh_common.uvb_dose import (
            parse_uvb_line, parse_uvb_partial, uvb_written_back_ok)
        # [2026-06-04] 處置「無日期」時 parse_uvb_line 會回 None(它要求 on (日期))，
        # 但 update_uvb_in_text 是用 parse_uvb_partial 處理無日期 → 寫回後也無日期。
        # verify 必須同樣容忍無日期(partial fallback)，否則「劑量其實已改」卻被誤判
        # verify 失敗而中止、跳過 51019(沈冠宇實機 case: "UVB: 950 mj/cm2 (10)" 無日期)。
        # [2026-06-24] 改用 uvb_written_back_ok 掃【所有】UVB 行比對 —— driver 重選
        # (舊行在上、改下面近期行)後,更新的驅動行未必是第一條;只看第一行會把已正確
        # 寫回的 stale-above-fresh 情境誤判失敗。verify 變數仍保留供失敗時的 log/提示顯示。
        if not uvb_written_back_ok(actual_text, result.new_dose,
                                   result.new_count):
            verify = parse_uvb_line(actual_text) or parse_uvb_partial(actual_text)
            logging.warning(
                "[%s][UVB] 寫回後實機 verify 失敗 — 預期 dose=%s count=%s, "
                "實際=%r → 終止", label, result.new_dose, result.new_count,
                verify)
            _show_uvb_warning(
                main_hwnd, "UVB 寫回驗證失敗",
                f"寫回處置欄後驗證不通過\n\n"
                f"預期: dose={result.new_dose} count={result.new_count}\n"
                f"實際讀回: dose={getattr(verify, 'dose', '?')} "
                f"count={getattr(verify, 'count', '?')}\n\n"
                f"{label} 已停止，請醫師手動確認處置內容。{_placed_note}")
            # [稽核] 回讀對不上 —— 改版/欄位漂移把劑量寫錯時,這是最關鍵的事後線索
            _record_his_action(
                _LEDGER_HIS_FIELD, f"{label} UVB 劑量", main_hwnd=main_hwnd,
                target="field:處置memo",
                value=f"dose={result.new_dose} count={result.new_count}",
                outcome=_LEDGER_MISMATCH,
                detail=f"回讀 dose={getattr(verify, 'dose', '?')} "
                       f"count={getattr(verify, 'count', '?')}")
            return False
        # [W7 codex review] 只在「讀回成功(actual_text 非空)且 verify 通過」時才記錄
        # 原值→新值。讀回失敗(_read_tmemo_text 逾時回空字串,跳過整個 if actual_text)
        # 時不記 → 半套警告退回一般版,不精準宣稱「已寫回 X→Y」(寫入未經確認)。
        _record_uvb_write(
            label, getattr(result.parsed, "dose", None),
            getattr(result.parsed, "count", None),
            result.new_dose, result.new_count,
            # [UD-11] 同批一併更新的行數:Step B 同日附加行 + Step C/D 同日 triplet
            # + 醫師按「是」後套用的 uncertain 行(套用失敗時為 0,與實況一致)
            extra_lines=(result.additional_lines_updated
                         + getattr(result, 'additional_triplets_updated', 0)
                         + _uncertain_applied))
        # [稽核 2026-07-17] 已回讀驗證通過的劑量寫入(記原值→新值,非 PII)
        _record_his_action(
            _LEDGER_HIS_FIELD, f"{label} UVB 劑量", main_hwnd=main_hwnd,
            target="field:處置memo",
            value=f"dose={getattr(result.parsed, 'dose', None)}→{result.new_dose} "
                  f"count={getattr(result.parsed, 'count', None)}→{result.new_count}",
            outcome=_LEDGER_OK)

    if result.additional_lines_updated > 0:
        logging.info(
            "[%s][UVB] 同日期 UVB 行共 %d 筆都已更新 (第一行 + %d 行)",
            label, 1 + result.additional_lines_updated,
            result.additional_lines_updated)
    elif result.uvb_line_count >= 2:
        logging.info(
            "[%s][UVB] 處置含 %d 行 UVB (只改第一行，其他日期不同不動)",
            label, result.uvb_line_count)
    if getattr(result, 'additional_triplets_updated', 0) > 0:
        # [v20.12] 同日期 triplet (e.g. excimer light、同行繼續) 額外更新
        logging.info(
            "[%s][UVB] 同日期 triplet 額外更新 %d 個 "
            "(e.g. excimer light、同行繼續 UVB segment)",
            label, result.additional_triplets_updated)

    # [v20.7] count 可能 None (處置沒寫)，log 適配
    # [2026-06-02 修正] first-time 路徑(處置沒日期)的 result.last_date / days_diff
    # 為 None；原本 else 分支呼叫 result.last_date.strftime() → AttributeError →
    # F2/F3 在「UVB 已寫回成功、verify 已通過」之後崩潰 → 例外冒泡 → 51019 沒進、
    # 療程也沒設(簡碧實機 case，log 連續多次 'NoneType has no strftime')。先處理
    # last_date is None，避免在這個收尾 log 把整個流程帶崩。
    if result.last_date is None:
        logging.info(
            "[%s][UVB] (第一次照光/處置無日期) 劑量 %s→%s, 次數 →%s, 已加上今天日期",
            label, result.parsed.dose, result.new_dose, result.new_count)
    elif result.new_count is None:
        logging.info(
            "[%s][UVB] 劑量 %d→%d, 次數 (處置沒寫 N 跳過), 日期 %s→今天 (差 %d 天)",
            label, result.parsed.dose, result.new_dose,
            result.last_date.strftime("%m/%d"), result.days_diff)
    else:
        logging.info(
            "[%s][UVB] 劑量 %d→%d, 次數 %d→%d, 日期 %s→今天 (差 %d 天)",
            label, result.parsed.dose, result.new_dose,
            result.parsed.count, result.new_count,
            result.last_date.strftime("%m/%d"), result.days_diff)
    return True


def _f23_update_uvb_dose(label: str = "F2"):
    """F2/F3 UVB 自動更新 — strict mode。回傳:
      False            → caller 應終止 (不跑 51019)。
      _F23_PURE_EXCIMER → 純自費 Excimer:caller 應設身份=01、跳過 51019/療程。
      True             → 正常,caller 續跑 51019/療程。
    """
    return _update_uvb_dose_core(label, strict=True)


def _f1_update_uvb_dose_if_present(label: str = "F1") -> None:
    """[v20.9] F1 UVB 自動更新 — 寬鬆 mode。

    F1 = 第一次照光，處置可能沒寫 UVB → NO_UVB_LINE 是正常情境不警告。
    其他真實異常 (parse fail / sanity fail / too close / 寫回失敗) 仍警告
    ([UD-09] 文案附「51019+療程1 已下」提醒 — F1 先醫令後 UVB,失敗時醫師
    若決定不照光,容易忘刪已下的醫令)。

    F1 流程是先 51019 後 UVB，所以這個函數沒有 abort 51019 的意義 —
    不論 return 值都不影響 51019 (已執行完)。caller 不需要看 return。
    """
    res = _update_uvb_dose_core(label, strict=False,
                                codes_already_placed="51019 醫令與療程 1")
    # [UD-14 2026-07-12] F1 是「先 51019 後 UVB」:key 51019 前的 _f1_phototherapy_route
    # 偵測【例外/找不到視窗時 fallback normal】,此處 core 二次分流卻判為純自費 Excimer
    # → 健保醫令+自費處置並存的矛盾狀態,原本無任何警示。兩個修正方向(刪醫令 vs 還原
    # excimer 行)都涉計費,不自動回退 → 跳人工核對警告。
    # [codex P2] 更新中止(如 TOO_CLOSE 回 falsy 哨符 _F23_PURE_EXCIMER_ABORTED)時分流
    # 一樣是純 excimer、矛盾一樣存在 → 兩種結局都要警告,僅文案區分行有沒有被更新。
    # (route 正常判到 pure_excimer 的 F1 走 _f1_pure_excimer,不會進到這裡。)
    if res == _F23_PURE_EXCIMER or res == _F23_PURE_EXCIMER_ABORTED:
        _exc_state = ("excimer 行可能已更新次數/日期/劑量"
                      if res == _F23_PURE_EXCIMER
                      else "excimer 行【未】更新(更新因間隔太短等原因中止)")
        logging.warning(
            "[%s] route=normal 已 key 51019,但 core 二次分流=純自費 Excimer(%s) "
            "→ 矛盾,跳人工核對警告", label, res)
        _show_uvb_warning(
            _find_hospital_main_window() or 0,
            "F1 照光分流矛盾,請人工核對",
            f"F1 前段已輸入 51019 醫令+療程 1(當時判定走健保 UVB),\n"
            f"但處置更新階段偵測為【純自費 Excimer】({_exc_state})。\n\n"
            f"健保醫令與自費處置並存,請人工核對本次實際照光種類:\n"
            f"  • 若為自費 Excimer:請刪除 51019/療程 1,並確認身份別與 excimer 行;\n"
            f"  • 若為健保 UVB:請檢查 excimer 行是否被誤更新並改回。")


# [2026-06-19] F1 純自費 Excimer 要 key 的醫令代碼(自費 Excimer 專用,非健保 51019)。
# 使用者確認 = 1850159 → F1 純 Excimer 走『key 1850159 + 療程1』(仍不動身份、不 key 51019)。
# (留空時則只設療程 1、不 key 醫令 —— 保留為日後關閉用的安全退路。)
F1_PURE_EXCIMER_CODE = "1850159"


def _f1_pure_excimer(label: str = "F1") -> bool:
    """F1 純自費 Excimer(使用者 2026-06-18 拍板,僅 F1 改):
      - 不動左上角「身份」(維持原樣,不寫 01,也不改健保)
      - 不 key 51019
      - 把「療程」設為 1(等同 UVB 的療程1)
    F1_PURE_EXCIMER_CODE 有設定(明天確認代碼後)時,改走『key 該醫令 + 療程1』
    (同 51019 流程但換代碼,仍不動身份)。"""
    code = (F1_PURE_EXCIMER_CODE or "").strip()
    if code:
        ok = _script_code_input_adaptive(code, label=label, set_療程=1)
        logging.info("[%s] 純 Excimer:醫令 %s + 療程1 → %s",
                     label, code, "done" if ok else "skipped")
        if not ok:
            _show_uvb_warning(
                0, "純 Excimer 自動處理未完成",
                f"{label} 純 Excimer 的醫令 {code} 或療程 1 沒有確認完成。\n\n"
                "請手動確認醫令與療程=1。")
        return ok
    # 尚未設定醫令代碼:只設療程 1(不動身份、不 key 51019)
    main_hwnd = _find_hospital_main_window()
    療程_ok = bool(main_hwnd) and _set_療程_only(main_hwnd, 1, label=label)
    if not 療程_ok:
        logging.warning("[%s] 純 Excimer:療程 1 未確認完成", label)
        _show_uvb_warning(
            main_hwnd or 0, "療程未自動設定",
            f"{label} 純 Excimer 的療程 1 沒有確認完成。\n\n請手動把療程改成 1。")
    logging.info("[%s] 純 Excimer:已設療程 1(未 key 醫令/51019、身份不動)", label)
    return 療程_ok


def script_F1_adaptive():
    """F1: 照光 (1) — 51019 + 療程 1，之後若有 UVB 行則更新 (寬鬆 mode)。

    [v20.9 2026-05-26] 加 UVB 更新 (best-effort)。
    流程跟 F2/F3 相反: F1 是先 51019 後 UVB。
      - 沒 UVB → 跳過、不警告 (新病人正常情況)
      - 有 UVB → 套同樣劑量規則更新
      - PARSE_FAIL/SANITY_FAIL/TOO_CLOSE/寫回失敗 仍警告 (跟 F2/F3 同)
    [2026-06-18] 純自費 Excimer 只在 F1 改:不動身份、不 key 51019、只設療程1
    (見 _f1_pure_excimer);未來會改 key 另一個醫令(待確認)。F2/F3 維持身份01。
    """
    logging.info("--- Executing F1 (照光 1) ---")
    # F1 是「先 51019 後 UVB」,所以要在 key 51019 【之前】先判斷照光走向。
    # 偵測失敗一律當 normal(照常 51019,不誤跳健保)。
    f1_route = _f1_phototherapy_route(label="F1")
    if f1_route == "ambiguous":
        logging.warning("F1: UVB 與 Excimer 分屬不同欄位 → 中止,交醫師手動")
        _show_uvb_warning(
            0, "照光分流無法判斷",
            "處置/病歷同時偵測到 UVB 與 Excimer,且分屬不同欄位,\n"
            "無法自動判斷本次是健保 UVB 還是自費 Excimer。\n\n"
            "F1 已停止 — 請醫師手動下醫令並確認身份別。")
        return False
    if f1_route == "pure_excimer":
        return _f1_pure_excimer(label="F1")
    ok = _script_code_input_adaptive("51019", label="F1", set_療程=1)
    logging.info("F1 (照光 1) 51019+療程: %s", "done" if ok else "skipped")
    if not ok:
        logging.warning("F1: 51019/療程未完成，跳過 UVB 更新以避免半套寫入")
        _show_light_code_incomplete_warning(
            "F1", 1, uvb_already_updated=False)
        return False
    # 接著 UVB 更新 (best-effort, 沒 UVB 不警告也不終止)
    _f1_update_uvb_dose_if_present(label="F1")
    return True


def script_F2_adaptive():
    """F2: 照光 (2) — UVB 劑量更新 (若有) + 51019 + 療程 2。

    [v20 2026-05-26] 新增 UVB 自動劑量更新前處理。
    """
    logging.info("--- Executing F2 (照光 2) ---")
    res = _f23_update_uvb_dose(label="F2")
    if not res:
        logging.info("F2: UVB 前置檢查/更新未完成，已終止 (跳過 51019)")
        return False
    if res == _F23_PURE_EXCIMER:
        # 純自費 Excimer:不 key 51019/療程,把身份改成 01
        # [audit 2026-07-12] 寫身份(計費欄)前的最終 F12 閘門:pure-excimer 內部 check_stop 通過
        # 後,劑量寫回(可阻塞 SendMessage)期間醫師若按 F12,此處 check_stop 會 raise → 不改身份。
        # 與 UD-04「寫回處置欄前尊重取消」同一語意,補齊下游身份欄。
        check_stop()
        _set_身份_自費("01", label="F2")
        logging.info("F2 (照光 2): 純自費 Excimer — 已設身份 01,未 key 51019/療程")
        return True
    # UVB 前置檢查過了(已 commit 要下 51019)才帶卡號 — 否則 precheck 中止卻已寫計費欄
    # [UD-03 2026-07-10] UVB 已寫回後,51019 階段若以【例外/F12】(而非正常回 False)收場,原本 W7
    # 『UVB 已改 X→Y、請補 51019 或改回』精準警告不會出現(只被 wrapper 改成一行狀態列)→ 病歷已改
    # +醫令沒進的半套狀態無人把關。包 try:攔到任何例外(含 SubsystemInterrupted)→ 若本次確實改過
    # UVB(_last_uvb_write 有本 label 紀錄)→ 先跳 W7 半套警告,再 re-raise 讓 wrapper 照常處理。
    try:
        _autofill_卡號_from_醫師上次(label="F2")
        ok = _script_code_input_adaptive("51019", label="F2", set_療程=2)
    except Exception:
        try:
            _rec = _last_uvb_write
            if isinstance(_rec, dict) and _rec.get("label") == "F2":
                _show_light_code_incomplete_warning("F2", 2, uvb_already_updated=True)
        except Exception:
            logging.exception("[F2] 半套警告顯示失敗(仍照常 re-raise 原例外)")
        raise   # 原例外(含 F12 SubsystemInterrupted)照常往上,由 wrapper 處理
    logging.info("F2 (照光 2): %s", "done" if ok else "skipped")
    if not ok:
        logging.warning("F2: UVB 已更新，但 51019/療程2 未確認完成")
        _show_light_code_incomplete_warning(
            "F2", 2, uvb_already_updated=True)
        return False
    return True


def script_F3_adaptive():
    """F3: 照光 (3) — UVB 劑量更新 (若有) + 51019 + 療程 3。

    [v20 2026-05-26] 新增 UVB 自動劑量更新前處理。
    """
    logging.info("--- Executing F3 (照光 3) ---")
    res = _f23_update_uvb_dose(label="F3")
    if not res:
        logging.info("F3: UVB 前置檢查/更新未完成，已終止 (跳過 51019)")
        return False
    if res == _F23_PURE_EXCIMER:
        # 純自費 Excimer:不 key 51019/療程,把身份改成 01
        # [audit 2026-07-12] 同 F2:寫身份(計費欄)前的最終 F12 閘門(見 script_F2_adaptive 說明)。
        check_stop()
        _set_身份_自費("01", label="F3")
        logging.info("F3 (照光 3): 純自費 Excimer — 已設身份 01,未 key 51019/療程")
        return True
    # UVB 前置檢查過了(已 commit 要下 51019)才帶卡號 — 否則 precheck 中止卻已寫計費欄
    # [UD-03 2026-07-10] 同 F2:51019 階段以例外/F12 收場時,若已改過 UVB → 先跳 W7 半套警告再 re-raise。
    try:
        _autofill_卡號_from_醫師上次(label="F3")
        ok = _script_code_input_adaptive("51019", label="F3", set_療程=3)
    except Exception:
        try:
            _rec = _last_uvb_write
            if isinstance(_rec, dict) and _rec.get("label") == "F3":
                _show_light_code_incomplete_warning("F3", 3, uvb_already_updated=True)
        except Exception:
            logging.exception("[F3] 半套警告顯示失敗(仍照常 re-raise 原例外)")
        raise   # 原例外(含 F12 SubsystemInterrupted)照常往上,由 wrapper 處理
    logging.info("F3 (照光 3): %s", "done" if ok else "skipped")
    if not ok:
        logging.warning("F3: UVB 已更新，但 51019/療程3 未確認完成")
        _show_light_code_incomplete_warning(
            "F3", 3, uvb_already_updated=True)
        return False
    return True


def script_F5_adaptive():
    """F5: KOH — 醫令→代碼輸入 → 13017 → Enter (不改療程)。"""
    logging.info("--- Executing F5 (KOH 13017) ---")
    ok = _script_code_input_adaptive("13017", label="F5", set_療程=None)
    logging.info("F5 (KOH): %s", "done" if ok else "skipped")
    return bool(ok)


# =============================================================================
# F8 — 快速輸入文字 (可在設定頁修改，預設 dtderm25)
# =============================================================================

F8_QUICK_TEXT_DEFAULT = "dtderm25"

# [v10] F8 quick text mtime-guarded 快取：避免每次按 F8 都重讀+parse JSON，
# 但檔案被改 (mtime 變) 時自動重讀 → 維持「設定頁改完不用重啟即時生效」。
_F8_QUICK_TEXT_CACHE = {"mtime": None, "value": F8_QUICK_TEXT_DEFAULT}


def _load_f8_quick_text() -> str:
    """從 threshold_settings.json 讀 quick_text_f8，失敗回預設 dtderm25。
    mtime 沒變就回快取；變了 (設定頁存檔) 才重讀，兼顧效率與即時生效。"""
    path = get_conf_path('threshold_settings.json')
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if mtime is not None and mtime == _F8_QUICK_TEXT_CACHE["mtime"]:
        return _F8_QUICK_TEXT_CACHE["value"]
    cfg = load_json_dict(path, {}, merge_defaults=False)
    t = cfg.get('quick_text_f8', F8_QUICK_TEXT_DEFAULT)
    value = str(t) if t else F8_QUICK_TEXT_DEFAULT
    _F8_QUICK_TEXT_CACHE["mtime"] = mtime
    _F8_QUICK_TEXT_CACHE["value"] = value
    return value


def script_F8_quick_text():
    """F8: 快速輸入文字到目前 focused 控件。
    文字從 settings (quick_text_f8) 讀，預設 dtderm25。
    用 keyboard.write() — 走 OS 鍵盤事件，支援所有 unicode。"""
    text = _load_f8_quick_text()
    if not text:
        logging.info("F8: quick_text 為空，跳過")
        return
    logging.info("--- Executing F8 (快速輸入 %r) ---", text)
    kb = getattr(hotkey_modules, 'keyboard', None)
    if kb is None:
        logging.warning("F8: keyboard 模組未就緒，跳過")
        return
    try:
        kb.write(text)
        logging.info("F8: 已輸入 %d 字", len(text))
    except Exception:
        logging.error("F8: keyboard.write 失敗", exc_info=True)


# =============================================================================
# F11 — 快速完成 (adaptive)
# =============================================================================
# 流程：
#   1. 主程式 TFopdmain 點「全部完成」TButton
#   2. 進入「popup 任意順序輪詢」迴圈 — 以下 popup 可能任意出現、可能跳過：
#      a. 疼痛指數 TFOpdMsg1 → 勾 0 radio (最左) → 點「處理」
#      b. 過敏記錄維護-醫師端 TFrmAllergyM01 → 點「回」
#      c. 藥物過敏記錄 TFAllergyB → 點「完  成」(空白寬鬆比對)
#      d. 健保藥費 TfAskDlg2 → 點「確認」
#      e. 診間預約掛號 TFOPDPreg → 點「處理」
#   迴圈條件：總時間上限 45s；連續 5s 沒看到任何 popup 視為完成
# 全程 PostMessage 不動滑鼠；ForegroundProtector 支援使用者切走後背景完成。

def _click_button_normalized_text(parent_hwnd: int, target_text: str) -> int:
    """找 TButton：把 text 去除「所有」空白後 == 去除空白的 target → PostMessage 點擊。
    解決 Delphi 按鈕常見「完  成」「確  認」這種額外空格。
    回傳：點到的 hwnd (失敗 0)。"""
    target_norm = "".join(target_text.split())
    out = [0]

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 64)
            if cls_buf.value != "TButton":
                return True
            n = ctypes.windll.user32.GetWindowTextLengthW(child)
            if n <= 0:
                return True
            t_buf = ctypes.create_unicode_buffer(n + 1)
            ctypes.windll.user32.GetWindowTextW(child, t_buf, n + 1)
            if "".join(t_buf.value.split()) == target_norm:
                out[0] = child
                return False
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumChildWindows(parent_hwnd, cb, 0)
    if out[0]:
        _post_click_to_control(out[0])
    return out[0]


def _wait_window_closed(hwnd: int, timeout: float = 5.0) -> bool:
    """等視窗關閉，最多 timeout 秒。回傳 True 表示已關。"""
    end_t = time.time() + timeout
    while time.time() < end_t:
        if not ctypes.windll.user32.IsWindow(hwnd):
            return True
        time.sleep(0.1)
        check_stop()
    return False


def _f11_handle_pain(hwnd: int, label: str = "") -> bool:
    """疼痛指數 popup：勾 0 radio + 點「處理」。hwnd 由 caller 找到後傳入。"""
    logging.info("[%s] 疼痛指數 popup hwnd=%s → 勾 0 + 處理", label, hwnd)
    time.sleep(0.15)
    check_stop()

    radios = _enum_class_in_window(hwnd, "TGroupButton")
    if radios:
        from collections import Counter
        tops = Counter(r[1] for r in radios)
        target_top, count = tops.most_common(1)[0]
        same_row = sorted([r for r in radios if r[1] == target_top],
                           key=lambda r: r[2])
        if len(same_row) >= 6:
            _post_click_to_control(same_row[0][0])
            logging.info("[%s]   已勾 0 radio (hwnd=%s)", label, same_row[0][0])
            time.sleep(0.08)
        else:
            logging.warning("[%s]   量表 row 只 %d 個 radios，跳過勾選",
                              label, len(same_row))
    check_stop()

    if _click_button_normalized_text(hwnd, "處理"):
        logging.info("[%s]   已點 處理", label)
        _wait_window_closed(hwnd, timeout=5)
        return True
    logging.warning("[%s]   找不到 處理 button", label)
    return False


def _f11_handle_appt(hwnd: int, label: str = "") -> bool:
    """診間預約掛號 popup：直接點「處理」(不勾任何項)。"""
    logging.info("[%s] 預約掛號 popup hwnd=%s → 處理", label, hwnd)
    time.sleep(0.15)
    check_stop()
    if _click_button_normalized_text(hwnd, "處理"):
        logging.info("[%s]   已點 處理", label)
        _wait_window_closed(hwnd, timeout=5)
        return True
    logging.warning("[%s]   找不到 處理 button", label)
    return False


class _ScrollInfo(ctypes.Structure):
    """SCROLLINFO 結構 (給 GetScrollInfo 用，跨 process 安全)。"""
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("fMask", ctypes.c_uint),
        ("nMin", ctypes.c_int),
        ("nMax", ctypes.c_int),
        ("nPage", ctypes.c_uint),
        ("nPos", ctypes.c_int),
        ("nTrackPos", ctypes.c_int),
    ]


def _grid_has_rows(grid_hwnd: int) -> bool:
    """用 GetScrollInfo(SB_VERT) 推斷 TXStringGrid 是否有資料列。
    有 scroll range > 1 → 多列；否則可能空或只 1 列。"""
    try:
        si = _ScrollInfo()
        si.cbSize = ctypes.sizeof(si)
        si.fMask = 0x07  # SIF_RANGE | SIF_PAGE | SIF_POS
        SB_VERT = 1
        if ctypes.windll.user32.GetScrollInfo(grid_hwnd, SB_VERT,
                                                ctypes.byref(si)):
            return (si.nMax - si.nMin) > 1
    except Exception:
        logging.debug("[grid] GetScrollInfo 失敗", exc_info=True)
    return False


def _f11_detect_allergy_state(hwnd: int, label: str = "") -> str:
    """判別 TFrmAllergyM01 popup 是 state A 還是 state B。

    state A：病人有過敏記錄 → 該按「回」(dismiss)
    state B：病人無過敏記錄、下方 4 個 radio 顯示 → 該按 radio + 處理

    判別策略：
      1. 找「藥物過敏訊息不確定」TRadioButton
      2. 若 IsWindowVisible+IsWindowEnabled → 表示「過敏訊息不確定」tab 是當前
         active tab，是 state B
      3. 若同時 TXStringGrid 有多列 scroll → state A (即使 radio 在某個 tab，但
         grid 有資料優先按回)
      4. 偵測失敗或不確定 → fallback state A (安全，不會誤改病歷)
    """
    radios = _find_descendants_by_exact_text(
        hwnd, "TRadioButton", "藥物過敏訊息不確定")
    visible_radio = 0
    for rh, _, _ in radios:
        try:
            if (ctypes.windll.user32.IsWindowVisible(rh)
                    and ctypes.windll.user32.IsWindowEnabled(rh)):
                visible_radio = rh
                break
        except Exception:
            continue

    # 查 TXStringGrid 有沒有多筆資料
    grid_has_data = False
    try:
        grids = _enum_class_in_window(hwnd, "TXStringGrid")
        for gh, _, _ in grids:
            if _grid_has_rows(gh):
                grid_has_data = True
                break
    except Exception:
        logging.debug("[%s] grid 偵測失敗", label, exc_info=True)

    logging.info(
        "[%s] 過敏 popup 狀態偵測：visible_radio=%s, grid_has_data=%s",
        label, visible_radio, grid_has_data)

    if visible_radio and not grid_has_data:
        return "B"  # 有可見 radio 且 grid 沒多筆 → 無過敏案例
    return "A"  # 否則保守處理


def _f11_handle_allergy_m01(hwnd: int, label: str = "") -> bool:
    """過敏記錄維護-醫師端 popup：依狀態決定行為。

    state A (有過敏記錄)：點「回」dismiss，不動病歷
    state B (無過敏記錄)：勾「藥物過敏訊息不確定」+ 點「處理」
    """
    logging.info("[%s] 過敏記錄維護 popup hwnd=%s", label, hwnd)
    time.sleep(0.15)
    check_stop()

    state = _f11_detect_allergy_state(hwnd, label=label)

    if state == "B":
        logging.info("[%s]   state=B → 勾「藥物過敏訊息不確定」+ 點「處理」", label)
        radios = _find_descendants_by_exact_text(
            hwnd, "TRadioButton", "藥物過敏訊息不確定")
        target_radio = 0
        for rh, _, _ in radios:
            try:
                if (ctypes.windll.user32.IsWindowVisible(rh)
                        and ctypes.windll.user32.IsWindowEnabled(rh)):
                    target_radio = rh
                    break
            except Exception:
                continue
        if not target_radio:
            logging.warning("[%s]   找不到可點的 radio，fallback 點「回」", label)
        else:
            _post_click_to_control(target_radio)
            logging.info("[%s]   已勾 radio (hwnd=%s)", label, target_radio)
            time.sleep(0.12)
            check_stop()
            if _click_button_normalized_text(hwnd, "處理"):
                logging.info("[%s]   已點 處理", label)
                _wait_window_closed(hwnd, timeout=5)
                return True
            logging.warning("[%s]   找不到 處理 button，fallback 點「回」", label)

    # state A 或 state B fallback → 點「回」(安全 dismiss，不改病歷)
    # [2026-05-22 v31] 加 retry — 實測 user 報告 popup 開啟後第一次 PostMessage
    # WM_LBUTTONDOWN 偶爾沒生效 (可能 Delphi OnShow 還在跑、button 還沒 enable)，
    # 第二次按 F11 才work。Handler 內 retry 一次 → 不必 user 手動 F11 第二次。
    logging.info("[%s]   state=A → 點「回」dismiss", label)
    for attempt in range(2):
        if _click_button_normalized_text(hwnd, "回"):
            logging.info("[%s]   已點 回 (attempt %d)", label, attempt + 1)
            if _wait_window_closed(hwnd, timeout=2.5):
                return True
            logging.warning("[%s]   點 回 後 popup 未關 (attempt %d/2)",
                              label, attempt + 1)
        else:
            logging.warning("[%s]   找不到 回 button (attempt %d/2)",
                              label, attempt + 1)
        # 第一次失敗：給 popup 多點時間 settle 後再 retry
        if attempt == 0:
            time.sleep(0.3)
            check_stop()
    logging.warning("[%s]   點 回 retry 2 次仍失敗 — popup 可能卡住", label)
    return False


def _f11_handle_allergy_b(hwnd: int, label: str = "") -> bool:
    """藥物過敏記錄 popup：點「完成」(實際 text 是「完  成」)。"""
    logging.info("[%s] 藥物過敏記錄 popup hwnd=%s → 完成", label, hwnd)
    time.sleep(0.15)
    check_stop()
    if _click_button_normalized_text(hwnd, "完成"):
        logging.info("[%s]   已點 完成", label)
        _wait_window_closed(hwnd, timeout=5)
        return True
    logging.warning("[%s]   找不到 完成 button", label)
    return False


def _f11_handle_ask_dlg(hwnd: int, label: str = "") -> bool:
    """健保藥費/品項管控目標確認 popup：點「確認」。"""
    logging.info("[%s] 健保藥費確認 popup hwnd=%s → 確認", label, hwnd)
    time.sleep(0.15)
    check_stop()
    if _click_button_normalized_text(hwnd, "確認"):
        logging.info("[%s]   已點 確認", label)
        _wait_window_closed(hwnd, timeout=5)
        return True
    logging.warning("[%s]   找不到 確認 button", label)
    return False


def _f11_handle_breast_screening(hwnd: int, label: str = "") -> bool:
    """乳房篩檢訊息 popup (class=TfAskDlg, title 含 '乳房篩檢')：點「自訴未懷孕」。

    安全考量：TfAskDlg 是通用 ask-dialog class，watcher 已用 title_kw='乳房篩檢'
    精確過濾，這裡 handler 再確認一次「自訴未懷孕」TButton 存在才點，避免誤觸。
    """
    logging.info("[%s] 乳房篩檢 popup hwnd=%s → 自訴未懷孕", label, hwnd)
    time.sleep(0.15)
    check_stop()
    if _click_button_normalized_text(hwnd, "自訴未懷孕"):
        logging.info("[%s]   已點 自訴未懷孕", label)
        _wait_window_closed(hwnd, timeout=5)
        return True
    logging.warning("[%s]   找不到 自訴未懷孕 button", label)
    return False


def _f11_handle_oral_screening(hwnd: int, label: str = "") -> bool:
    """口腔黏膜篩檢提示 popup (class=TfAskDlg, title 含 '口腔黏膜篩檢')：
    點「暫不需要」(皮膚科病人不需要做口腔篩檢)。

    [2026-05-25] snapshot_20260525_090405 證實 popup 結構：
      - class=TfAskDlg, title=口腔黏膜篩檢提示
      - 2 個 TButton: 「執行」/「暫不需要」
    跟 _f11_handle_breast_screening 共用 class，靠 watcher 的 title_kw 過濾分流。
    這裡 handler 再驗證「暫不需要」button 存在才點，避免誤觸其他 TfAskDlg。
    """
    logging.info("[%s] 口腔黏膜篩檢 popup hwnd=%s → 暫不需要", label, hwnd)
    time.sleep(0.15)
    check_stop()
    if _click_button_normalized_text(hwnd, "暫不需要"):
        logging.info("[%s]   已點 暫不需要", label)
        _wait_window_closed(hwnd, timeout=5)
        return True
    logging.warning("[%s]   找不到 暫不需要 button", label)
    return False


def _f11_handle_history_ditto_confirm(hwnd: int, label: str = "") -> bool:
    """門診病史徵候確認事項 popup (class=TfAskDlg, title 含 '門診病史徵候確認')：
    點「本次看診不修改」(Ditto 病歷異動超過 10% 提醒，皮膚科 F11 跳過修改)。

    [2026-05-26] snapshot_20260526_101708 證實 popup 結構：
      - class=TfAskDlg, title=門診病史徵候確認事項
      - 2 個 TButton: 「回主畫面」/「本次看診不修改」
      - 內容: 「病委會決議 Ditto 資料後病歷，需異動超過 10%，目前尚需異動 N 個字」
    跟其他 TfAskDlg handlers 共用 class，靠 watcher 的 title_kw 過濾分流。
    """
    logging.info("[%s] 門診病史徵候確認 popup hwnd=%s → 本次看診不修改",
                 label, hwnd)
    time.sleep(0.15)
    check_stop()
    if _click_button_normalized_text(hwnd, "本次看診不修改"):
        logging.info("[%s]   已點 本次看診不修改", label)
        _wait_window_closed(hwnd, timeout=5)
        return True
    logging.warning("[%s]   找不到 本次看診不修改 button", label)
    return False


def _f11_handle_primary_care_refer(hwnd: int, label: str = "") -> bool:
    """健保初級照護轉診訊息 (class=TfChkSpecList)：
    勾「99.本院療程尚未結束，本次不轉院」TGroupButton → 點「確認」TButton。

    [2026-05-22 v32] 加 readiness poll + retry — 實測 popup 開後 radio 不一定
    立刻 Enabled，硬 sleep 0.12s 偶爾不夠 → 找不到 target_radio → handler 失敗
    → user 看到 popup 沒被處理。修法：poll 等 radio Enabled (up to 1.5s)
    + click 失敗 retry 一次。
    """
    logging.info("[%s] 健保初級照護轉診 popup hwnd=%s → 勾 99.本院療程尚未結束 + 確認",
                  label, hwnd)

    # Poll 等 radio 變 Enabled (Delphi popup OnShow 後可能還在 init)
    target_radio = 0
    ready_deadline = time.time() + 1.5
    while time.time() < ready_deadline:
        radios = _find_descendants_by_exact_text(
            hwnd, "TGroupButton", "99.本院療程尚未結束，本次不轉院")
        for rh, _, _ in radios:
            try:
                if (ctypes.windll.user32.IsWindowVisible(rh)
                        and ctypes.windll.user32.IsWindowEnabled(rh)):
                    target_radio = rh
                    break
            except Exception:
                continue
        if target_radio:
            break
        time.sleep(0.05)
        check_stop()

    if not target_radio:
        logging.warning("[%s]   1.5s 內找不到可點的 99.本院療程尚未結束 radio", label)
        return False

    # Try radio click + 確認 click, retry once if popup didn't close
    for attempt in range(2):
        _post_click_to_control(target_radio)
        logging.info("[%s]   已勾 radio (hwnd=%s, attempt %d)",
                      label, target_radio, attempt + 1)
        time.sleep(0.12)
        check_stop()

        if _click_button_normalized_text(hwnd, "確認"):
            logging.info("[%s]   已點 確認 (attempt %d)", label, attempt + 1)
            if _wait_window_closed(hwnd, timeout=3):
                return True
            logging.warning("[%s]   點 確認 後 popup 未關 (attempt %d/2)",
                              label, attempt + 1)
        else:
            logging.warning("[%s]   找不到 確認 button (attempt %d/2)",
                              label, attempt + 1)
        if attempt == 0:
            time.sleep(0.3)
            check_stop()
    logging.warning("[%s]   retry 2 次仍失敗", label)
    return False


# [2026-06-29] 轉診視窗『本次門診預掛紀錄』TXStringGrid 是 owner-drawn,UIA/MSAA/Win32 都讀不到
# 內容(user 探測 _referral_uia_probe 確認:無 Grid pattern、0 個有文字、accChildCount=0)。改用
# 畫面取樣:表頭以下的資料列區若出現『深色文字』(資料列的黑字)→ 有預約列。門檻給寬(空表幾乎 0 個
# 內容像素,有列則數十~數百);log 印出實測 content 值方便在實機微調這兩個常數。
# 表格用既有的 _find_first_descendant_by_class(定義在後方 ~line 3870,EnumChildWindows 第一個)找;
# 轉診視窗內只有一個 TXStringGrid(探測確認),故「第一個」即預掛紀錄表格,不另重複定義。
_APPT_GRID_HEADER_SKIP_PX = 24   # 跳過表頭(欄位標題)那一列再取樣
_APPT_GRID_CONTENT_MIN = 12      # 內容像素 >= 此值 → 視為「有預約列」


def _window_is_ancestor(ancestor_hwnd: int, hwnd: int) -> bool:
    """hwnd 是否為 ancestor_hwnd 本身、或其子孫(沿 parent 鏈上溯)。"""
    if not hwnd or not ancestor_hwnd:
        return False
    GA_PARENT = 1
    cur = hwnd
    for _ in range(64):   # 防環,最多上溯 64 層
        if cur == ancestor_hwnd:
            return True
        try:
            parent = ctypes.windll.user32.GetAncestor(cur, GA_PARENT)
        except Exception:
            return False
        if not parent or parent == cur:
            return False
        cur = parent
    return False


def _screen_point_in_window(root_hwnd: int, x: int, y: int) -> bool:
    """螢幕座標 (x,y) 最上層的視窗是否屬於 root_hwnd(本身或子孫)。
    用於確認畫面取樣點沒被別的視窗(如 Chrome)遮住 —— 遮住時 WindowFromPoint 會回別視窗。"""
    try:
        wfp = ctypes.windll.user32.WindowFromPoint
        wfp.argtypes = [wintypes.POINT]
        wfp.restype = wintypes.HWND
        top = wfp(wintypes.POINT(int(x), int(y)))
    except Exception:
        return False
    return _window_is_ancestor(root_hwnd, top)


def _referral_grid_has_appointments(dialog_hwnd: int, label: str = "") -> bool:
    """轉診視窗的『本次門診預掛紀錄』(TXStringGrid)有沒有預約列。owner-drawn 讀不到內容,改用畫面
    取樣表頭以下的資料列區:有足夠『深色文字』(資料列黑字)→ True。任何失敗一律回 False → 保守走『轉回
    原診所』(等同本功能加入前的既有行為,不會更糟)。"""
    try:
        grid = _find_first_descendant_by_class(dialog_hwnd, "TXStringGrid")
        if not grid:
            logging.info("[%s] 轉診:找不到預掛表格,當作沒預約", label)
            return False
        r = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(grid, ctypes.byref(r)):
            return False
        left, data_top = r.left, r.top + _APPT_GRID_HEADER_SKIP_PX
        w, h = r.right - r.left, r.bottom - data_top
        if w < 20 or h < 8:
            return False
        # [H5 2026-07-09] F11 全程掛 ForegroundProtector,使用者切到 Chrome 時本轉診視窗在背景
        # 被處理;pyautogui 抓的是【螢幕最上層】像素 → 若被 Chrome 等視窗遮住,會抓到網頁的深色
        # 文字 → 誤判「有預約」→ 誤勾本科門診、漏印轉回單(高風險方向)。取樣前用 WindowFromPoint
        # 驗證表格中心垂直線三點都仍屬於本視窗;有任一被遮 → 保守當沒預約(轉回原診所),不亂取樣。
        cx = left + w // 2
        probe_ys = (data_top + 3, data_top + h // 2, data_top + h - 3)
        if not all(_screen_point_in_window(dialog_hwnd, cx, py) for py in probe_ys):
            logging.warning("[%s] 轉診:預掛表格被遮住/不在最上層 → 保守當沒預約(轉回原診所)",
                            label)
            return False
        pag = getattr(hotkey_modules, "pyautogui", None)
        if pag is None:
            logging.warning("[%s] 轉診:pyautogui 未就緒,保守當沒預約", label)
            return False
        img = pag.screenshot(region=(left, data_top, w, h))
        px = img.load()
        iw, ih = img.size
        content = 0
        for y in range(0, ih, 3):
            for x in range(0, iw, 5):
                p = px[x, y]
                rr, gg, bb = p[0], p[1], p[2]
                # [Codex] 只數【深色文字】像素;【不】把藍色選取列當內容 —— 否則空表若仍畫出空白選取
                # 帶,會誤判成有預約 → 誤選本科門診、漏印轉回單(高風險方向)。空表幾乎 0 個深色像素。
                # 代價:被選取那一列若是白字藍底會數不到,但其餘列的深色文字仍足以判定有預約。
                if rr < 110 and gg < 110 and bb < 110:
                    content += 1
        has = content >= _APPT_GRID_CONTENT_MIN
        logging.info("[%s] 轉診:預掛表格內容取樣 content=%s(門檻 %s)→ %s",
                     label, content, _APPT_GRID_CONTENT_MIN,
                     "有預約→本科門診" if has else "沒預約→轉回原診所")
        return has
    except Exception:
        logging.exception("[%s] 轉診:偵測預掛表格例外,保守當沒預約", label)
        return False


def _f11_first_visible_enabled(parent_hwnd: int, target_class: str,
                               target_text: str) -> int:
    """回第一個 class+exact-text 且【可見+enabled】的子孫 hwnd;沒有回 0。
    用 IsWindowVisible gate 過濾 Delphi 隱藏分頁(TTabSheet)上的同名控件。"""
    for h, _, _ in _find_descendants_by_exact_text(parent_hwnd, target_class, target_text):
        try:
            if (ctypes.windll.user32.IsWindowVisible(h)
                    and ctypes.windll.user32.IsWindowEnabled(h)):
                return h
        except Exception:
            continue
    return 0


def _f11_handle_transfer_msg(hwnd: int, label: str = "") -> bool:
    """病人轉診提示畫面 popup (class=TFTunMsg)。[2026-06-29] 依『本次門診預掛紀錄』有無預約決定動向:
      有預約(病人要回本科)→ 勾「本科門診進一步追蹤治療」→ 點「處理/離開」→ 視窗直接關(無 state B);
      沒預約 → 維持原本:勾「轉回原診所繼續治療」→ 點「處理/離開」→ state B 點「印轉回單後離開」。
    預掛表格是 owner-drawn(UIA/MSAA 讀不到),用畫面取樣判斷(_referral_grid_has_appointments);
    偵測失敗一律當『沒預約』走轉回原診所(保守,等同本分支加入前的行為)。
    """
    logging.info("[%s] 轉診提示 popup hwnd=%s", label, hwnd)
    time.sleep(0.12)
    check_stop()

    # [2026-06-30][Codex] 復原:watcher 重進來時,視窗可能已停在『部分負擔提示』分頁(TabSheet6)。
    # 特徵 = 有【可見】的「離開」鈕,且【沒有】可見的「處理/離開」鈕與動向 radio(動向頁的控件都被
    # 切到背景隱藏了)。此時直接按「離開」收尾,讓重試能從這頁恢復,不會掉進『沒預約→轉回原診所』而卡死。
    # 用強門檻(同時要求離開可見+處理離開/radio 都不可見)避免在動向頁誤觸,不新增誤點面。
    if (not _f11_first_visible_enabled(hwnd, "TButton", "處理/離開")
            and not _f11_first_visible_enabled(hwnd, "TGroupButton", "本科門診進一步追蹤治療")
            and not _f11_first_visible_enabled(hwnd, "TGroupButton", "轉回原診所繼續治療")):
        leave_only = _f11_first_visible_enabled(hwnd, "TButton", "離開")
        if leave_only:
            clicked = _post_click_to_control(leave_only)
            logging.info("[%s] 轉診:停在部分負擔提示頁 → 按「離開」(hwnd=%s, sent=%s)",
                         label, leave_only, clicked)
            return _wait_window_closed(hwnd, timeout=5)

    # [2026-06-29] 有預約 → 本科門診進一步追蹤治療(選 + 處理/離開,視窗直接關,無 state B)。
    if _referral_grid_has_appointments(hwnd, label):
        appt_radios = _find_descendants_by_exact_text(
            hwnd, "TGroupButton", "本科門診進一步追蹤治療")
        appt_target = 0
        for rh, _, _ in appt_radios:
            try:
                if (ctypes.windll.user32.IsWindowVisible(rh)
                        and ctypes.windll.user32.IsWindowEnabled(rh)):
                    appt_target = rh
                    break
            except Exception:
                continue
        if appt_target:
            _post_click_to_control(appt_target)
            logging.info("[%s]   有預約 → 已勾「本科門診進一步追蹤治療」", label)
            time.sleep(0.12)
            check_stop()
            if _click_button_normalized_text(hwnd, "處理/離開"):
                # [Codex] 成功條件 = 視窗真的關閉;沒關回 False 讓 watcher 重試,別誤標 handled。
                # [2026-06-30] 處理/離開 後有兩種結果,同一迴圈擇先處理:
                #   ① 視窗直接關(非轉診-in 病人)→ 完成;
                #   ② 轉診-in 病人會切到『部分負擔提示』分頁(TFTunMsg 內 TPageControl 的
                #      TabSheet6,只剩一顆「離開」鈕)→ 需再按「離開」才會關(= user 回報的
                #      「點了本科門診後還是跳出要按離開的畫面」)。只在「離開」鈕真的【可見】
                #      (分頁已切過去)時才點,避免誤點到隱藏分頁上的鈕。
                # 實測:會關就會在 ~1s 內關、部分負擔頁也會在 ~1s 內跳出 → 窗口給 2s(含餘裕)就夠,
                # 不用等 6s。真有更慢的情況,handler 最上面的『部分負擔頁復原』會在 watcher 重進來時兜底。
                end_t = time.time() + 2
                while time.time() < end_t:
                    check_stop()
                    if not ctypes.windll.user32.IsWindow(hwnd):
                        logging.info("[%s]   本科門診:已點處理/離開、視窗已關", label)
                        return True
                    leave_btn = _f11_first_visible_enabled(hwnd, "TButton", "離開")
                    if leave_btn:
                        clicked = _post_click_to_control(leave_btn)
                        logging.info("[%s]   本科門診:部分負擔提示頁按「離開」(hwnd=%s, sent=%s)",
                                     label, leave_btn, clicked)
                        if _wait_window_closed(hwnd, timeout=5):
                            logging.info("[%s]   本科門診:離開後視窗已關", label)
                            return True
                        logging.warning("[%s]   本科門診:按了離開但視窗未關 → 交 watcher 重試", label)
                        return False
                    time.sleep(0.15)
                logging.warning("[%s]   本科門診:處理/離開後 2s 內視窗未關、也沒出現「離開」鈕 "
                                "→ 交 watcher 重試(最上面復原會兜底)", label)
                return False
            logging.warning("[%s]   本科門診路徑:找不到「處理/離開」", label)
            return False
        # [Codex] 已判定有預約但定位不到「本科門診」radio = 矛盾狀態;【不】退回去點轉回原診所
        # (會選錯動向、誤印轉回單)。回 False 讓 watcher 重試;真的一直找不到就交醫師手動。
        logging.warning("[%s]   有預約但找不到「本科門診」radio → 停手交人工(不誤走轉回原診所)", label)
        return False

    # Step A: 如果還在 state A，勾 radio + 點 處理/離開
    radios = _find_descendants_by_exact_text(
        hwnd, "TGroupButton", "轉回原診所繼續治療")
    visible_radio = 0
    for rh, _, _ in radios:
        try:
            if (ctypes.windll.user32.IsWindowVisible(rh)
                    and ctypes.windll.user32.IsWindowEnabled(rh)):
                visible_radio = rh
                break
        except Exception:
            continue

    if visible_radio:
        _post_click_to_control(visible_radio)
        logging.info("[%s]   state A: 已勾「轉回原診所繼續治療」radio", label)
        time.sleep(0.12)
        check_stop()
        if _click_button_normalized_text(hwnd, "處理/離開"):
            logging.info("[%s]   state A: 已點「處理/離開」", label)
        else:
            logging.warning("[%s]   state A: 找不到「處理/離開」", label)
            return False
        # 等 state B 出現 (印轉回單 button 可見)
        time.sleep(0.2)
        check_stop()

    # Step B：找「印轉回單後離開」(可見+enabled) 並點。
    # 即使 state A 跳過 (popup 一開始就是 state B)，這邊也會正常處理。
    end_t = time.time() + 6
    while time.time() < end_t:
        check_stop()
        btns = _find_descendants_by_exact_text(
            hwnd, "TButton", "印轉回單後離開")
        target = 0
        for bh, _, _ in btns:
            try:
                if (ctypes.windll.user32.IsWindowVisible(bh)
                        and ctypes.windll.user32.IsWindowEnabled(bh)):
                    target = bh
                    break
            except Exception:
                continue
        if target:
            _post_click_to_control(target)
            logging.info("[%s]   state B: 已點「印轉回單後離開」(hwnd=%s)",
                          label, target)
            _wait_window_closed(hwnd, timeout=5)
            return True
        time.sleep(0.15)

    logging.warning("[%s]   等不到 state B「印轉回單後離開」(6s)", label)
    return False


def _f11_handle_message_ok(hwnd: int, label: str = "") -> bool:
    """西醫門診系統 OK 對話框 (class=TMessageForm, title 含 '西醫門診系統')：點 OK。

    例：「請確認IC卡必須插好!! 在讀卡機橙色燈停止閃爍後再按【OK】，寫入 IC 卡
    預防保健註記!!」這類 routine 提示。

    TMessageForm 是 Delphi 通用 message box class，超多東西用它。watcher 用
    title_kw='西醫門診系統' 過濾；這裡 handler 再驗：必須剛好只有 1 個 TButton
    且 text='OK' (避免誤觸 Yes/No / OK/Cancel 型對話框)。
    """
    logging.info("[%s] 西醫門診系統 message popup hwnd=%s", label, hwnd)
    time.sleep(0.15)
    check_stop()
    # 安全檢查：必須有「OK」button 且只有這一顆 button (避免誤觸選項型對話框)
    ok_btns = _find_descendants_by_exact_text(hwnd, "TButton", "OK")
    if not ok_btns:
        logging.info("[%s]   沒「OK」button，跳過 (不是 routine 提示)", label)
        return False
    all_btns = _enum_class_in_window(hwnd, "TButton")
    if len(all_btns) != 1:
        logging.info("[%s]   有 %d 顆 TButton (非單 OK)，跳過避免誤觸",
                      label, len(all_btns))
        return False
    if _click_button_normalized_text(hwnd, "OK"):
        logging.info("[%s]   已點 OK", label)
        _wait_window_closed(hwnd, timeout=5)
        return True
    logging.warning("[%s]   點 OK 失敗", label)
    return False


# (class_name, title_kw, handler_fn) — 順序不重要 (任意順序輪詢)
# title_kw='' 表示任何 title 都 match (純靠 class 即可唯一識別)；
# title_kw 非空 表示 class 是通用的，必須加 title 過濾
_F11_POPUP_HANDLERS = [
    ("TFOpdMsg1",       "",              _f11_handle_pain),
    ("TFrmAllergyM01",  "",              _f11_handle_allergy_m01),
    ("TFAllergyB",      "",              _f11_handle_allergy_b),
    ("TfAskDlg2",       "",              _f11_handle_ask_dlg),
    ("TFOPDPreg",       "",              _f11_handle_appt),
    ("TfAskDlg",        "乳房篩檢",       _f11_handle_breast_screening),
    ("TfAskDlg",        "口腔黏膜篩檢",    _f11_handle_oral_screening),
    ("TfAskDlg",        "門診病史徵候確認", _f11_handle_history_ditto_confirm),
    ("TMessageForm",    "西醫門診系統",   _f11_handle_message_ok),
    ("TFTunMsg",        "病人轉診",       _f11_handle_transfer_msg),
    ("TfChkSpecList",   "健保初級照護",   _f11_handle_primary_care_refer),
]


def _scan_unknown_popups(known_classes: set, seen: dict, label: str) -> None:
    """[2026-05-22 v41/v42] F11 watcher 期間掃所有 visible top-level windows，
    若 class 不在已知清單就記下來。

    [v42] 為了不對醫院 app 送任何跨 process 訊息，全程只用 kernel-only API：
      - IsWindowVisible: kernel-only ✓
      - GetClassName: kernel-only ✓ (Windows 維護 class atom table)
      - GetWindowRect: kernel-only ✓
      - GetWindowText: 跨 process WM_GETTEXT ✗ (移除，title 不取)
    這樣 unknown scan 對醫院 app 是 **完全零訊息**。
    User 看到 log 中的 unknown class 後可用 抓取當前視窗結構.cmd 取得詳細資訊。
    """
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(hwnd, lparam):
        try:
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            if hwnd in seen:
                return True
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(hwnd, cls_buf, 64)
            cls = cls_buf.value
            if cls in known_classes:
                return True
            r = wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            w, h = r.right - r.left, r.bottom - r.top
            if w < 100 or h < 40:
                return True
            # [v42] 不再 GetWindowText — class + rect 已足夠識別 unknown popup
            seen[hwnd] = (cls, "", time.time())
            logging.warning(
                "[%s][unknown-popup] 偵測到未知 visible 視窗: class='%s' "
                "hwnd=%s rect=(%dx%d at %d,%d) — 若這擋住 F11 流程，請開 "
                "抓取當前視窗結構.cmd 拍 snapshot 給開發者",
                label, cls, hwnd, w, h, r.left, r.top)
        except Exception:
            pass
        return True

    try:
        ctypes.windll.user32.EnumWindows(cb, 0)
    except Exception:
        pass


def _popup_identity(hwnd: int) -> tuple:
    """[M3/codex 2026-07-09] 輕量視窗『身分』:(標題文字, 直系子視窗數)。同 class 但【不同實例】
    (內容不同,如 chained Delphi dialog 換了一個)時身分會不同 —— 用來偵測某 hwnd 被同 class 的
    新 popup 重用(此時身分變了 → 應重新處理,不可沿用舊 handled 記錄而跳過)。handler 只是按鈕
    click,同一實例的標題/子視窗數穩定 → 身分不變 → 正確視為已處理、不重複動作。"""
    try:
        txt = _get_window_text(hwnd)
    except Exception:
        txt = ""
    try:
        cnt = len(_enum_direct_children(hwnd))
    except Exception:
        cnt = -1
    return (txt, cnt)


def _f11_popup_watcher(label: str = "F11",
                        total_timeout: float = 60.0) -> int:
    """輪詢已知 popup → 依現身順序執行對應 handler。

    退出條件 (任一即退出)：
      1. F12 中止 (check_stop 拋例外)
      2. 病患選擇畫面 TFOpdselpt 變成 foreground (整個 checkout 流程完成)
      3. total_timeout 60s 安全上限到期

    [2026-05-22 v35] 移除 idle timeout — user 報告若任一 popup 卡住、user 手動
      點掉之後 watcher 已 idle out 退出，後續 popup 不會繼續按。改成持續輪詢
      直到回到病患清單 / F12 / 60s 安全上限。

    回傳：處理過的 popup 數量
    """
    start = time.time()
    # [M3 2026-07-09 + codex] key=(hwnd, class),value=該實例的 _popup_identity(標題+子視窗數)。
    # 裸 hwnd 會被系統回收重用 —— 舊 popup 關閉後同 class 新 popup 撿到同一 hwnd 會被誤判
    # already-handled 而永久跳過。三重保險:(1)key 加 class;(2)迴圈內以 IsWindow prune 掉已消失項;
    # (3)value 存視窗身分,同 hwnd+class 但身分不同(內容變了=新實例)即重新處理。
    handled: dict = {}       # (hwnd, class) → 已處理該實例的身分
    # 每個 (hwnd, class) 的 retry 次數 — handler 回 False 但 popup 還在 (race 沒按到 /
    # radio 還沒 enable) 時給最多 3 次機會，超過放棄不卡
    retry_counter: dict = {}
    handled_count = 0
    last_progress_log = time.time()
    # [perf r5] _scan_unknown_popups 走全域 EnumWindows + Python callback(遍歷所有
    # top-level window，~5-15ms/次)，純診斷用(只印 unknown-popup WARNING，不參與實際
    # popup 處理)。原本每輪(0.3-0.4s)都跑，整趟 F11 燒掉數百次。改成 ≥2s 才掃一次：
    # 診斷足夠(unknown popup 不會 200ms 內生滅、且 seen dict 去重)，省下無謂 CPU。
    # 實際 popup 偵測(下方 FindWindowExW 迴圈)不受影響，零正確性風險。
    UNKNOWN_SCAN_INTERVAL = 2.0
    last_unknown_scan = 0.0
    # [2026-05-22 v41] 未知 popup 偵測 — 列出所有 visible top-level windows
    # 跟已知 9 個 class 比對，找出我們不認識的視窗 (這是 user 報「F11 卡死」
    # 但 watcher 0 個 popup 的根因 — 真的有 popup，只是我們不知道它的 class)。
    known_classes = {c for c, _, _ in _F11_POPUP_HANDLERS}
    known_classes.update({
        "TFopdmain", "TFOpdselpt",  # 主程式 + 病患選擇
        "Button", "Static", "Edit", "msctls_statusbar32",  # Windows 標準
        "Shell_TrayWnd", "Progman", "WorkerW",  # Windows shell
        "MSCTFIME UI", "IME", "Default IME",  # IME
        "tooltips_class32", "TToolBar95",  # tooltips/toolbar
        # [2026-05-25 v14] 補白名單 — user log 顯示每次 F11 都把這些印成
        # WARNING unknown popup 把真正問題淹沒。實測都不會擋 F11：
        "TFormMain",                  # 主程式另一個 Form (不是 TFopdmain)
        "Tformupdate", "Tformm1100s", # 主程式更新/診間切換 form
        "TkTopLevel",                 # 我們自己 Tk UI (主程式視窗)
        "Chrome_WidgetWin_1",         # Chrome window (背景開的)
        "ApplicationFrameWindow",     # Windows UWP frame
        "Windows.UI.Core.CoreWindow", # Windows UWP core
        "TfGaugeAPI_SimpleProgress",  # 醫院 app 進度條 (F11 流程中會閃出)
        "TfIspTakMark",               # 醫院 app 簽核相關 (F11 流程內視窗)
    })
    unknown_seen: dict = {}  # hwnd → (class, title, first_seen_ts)

    while time.time() - start < total_timeout:
        check_stop()
        # [快結束信號] 病患選擇畫面 (TFOpdselpt) 變成 foreground →
        # 整個 patient checkout 流程已完成，可以提早結束 watcher。
        try:
            fg = ctypes.windll.user32.GetForegroundWindow()
            if fg:
                fg_cls_buf = ctypes.create_unicode_buffer(64)
                ctypes.windll.user32.GetClassNameW(fg, fg_cls_buf, 64)
                if fg_cls_buf.value == "TFOpdselpt":
                    if unknown_seen:
                        logging.warning(
                            "[%s] 未知 popup 統計 (F11 期間觀察到 %d 個 unknown "
                            "視窗 — 若有 popup 卡住沒處理可能就是這些):",
                            label, len(unknown_seen))
                        for h, (c, t, _) in unknown_seen.items():
                            logging.warning(
                                "[%s]   unknown hwnd=%s class='%s' title='%s'",
                                label, h, c, t[:60])
                    logging.info(
                        "[%s] 偵測到病患選擇畫面 (TFOpdselpt) 為前景 → "
                        "F11 流程完成，watcher 結束 (處理 %d 個 popup)",
                        label, handled_count)
                    return handled_count
        except Exception:
            pass

        # [v41] 偵測 unknown popup — 列舉所有 visible top-level windows([perf r5] 節流 ≥2s)
        _now = time.time()
        if _now - last_unknown_scan >= UNKNOWN_SCAN_INTERVAL:
            last_unknown_scan = _now
            try:
                _scan_unknown_popups(known_classes, unknown_seen, label)
            except Exception:
                logging.debug("[%s] unknown popup scan 例外", label, exc_info=True)

        # [M3] 清掉 hwnd 已被系統回收(IsWindow=False)的 handled/retry 項,避免重用的 hwnd 被
        # 永久跳過。與下方 (hwnd, class) key 雙保險。
        try:
            _dead = [k for k in handled
                     if not ctypes.windll.user32.IsWindow(k[0])]
            for _k in _dead:
                handled.pop(_k, None)
                retry_counter.pop(_k, None)
        except Exception:
            pass

        found_one = False
        for cls_name, title_kw, handler in _F11_POPUP_HANDLERS:
            hwnd = _find_window_by_class_title(cls_name, title_kw)
            key = (hwnd, cls_name)
            # 身分(標題+子視窗數)不同於上次處理 → 視為新實例(hwnd 被同 class 新 popup 重用),重處理。
            # [限制] 若新舊實例的 class/title/子視窗數全同且中間沒被 prune 觀察到關閉,身分無法區分 —— 這是
            # 對外部 owner-drawn Delphi 視窗做去重的先天限制(Win32 無穩定 per-instance id)。實際
            # _F11_POPUP_HANDLERS 各 popup 為【不同 class】,此殘留情境幾乎不會發生。
            ident = _popup_identity(hwnd) if hwnd else None
            if hwnd and handled.get(key) != ident:
                # [codex P2] retry 次數綁定 identity —— hwnd 被新實例重用(身分變)時歸零,讓新
                # popup 拿到完整重試次數,不會沿用舊實例殘留的計數而提早放棄。
                _rc = retry_counter.get(key)
                attempts = _rc[1] if (_rc and _rc[0] == ident) else 0
                # [2026-05-22 v40] 每 popup 處理前後打 timestamp，定位卡死
                t_handler_start = time.time()
                logging.info("[%s][timeline] 偵測到 popup %s hwnd=%s "
                              "(+%.1fs since F11 start)，呼叫 handler",
                              label, cls_name, hwnd,
                              time.time() - start)
                ok = False
                try:
                    ok = bool(handler(hwnd, label=label))
                except Exception:
                    logging.error("[%s] handler %s 例外", label, cls_name,
                                    exc_info=True)
                t_handler_end = time.time()
                logging.info("[%s][timeline] handler %s 完成 (ok=%s, 耗時 %.0fms)",
                              label, cls_name, ok,
                              (t_handler_end - t_handler_start) * 1000)
                # 若 handler 成功 OR popup 已關 (user 可能手動關了) OR 已試 3 次
                # → 標記 handled，不再 retry
                popup_still_open = bool(
                    ctypes.windll.user32.IsWindow(hwnd))
                if ok or not popup_still_open or attempts >= 2:
                    handled[key] = ident   # 記下已處理【這個實例】的身分
                    handled_count += 1
                    if not ok and not popup_still_open:
                        logging.info("[%s] %s popup 已關 (可能 user 手動)，"
                                       "標記 handled", label, cls_name)
                    elif ok and popup_still_open:
                        # [M3/codex 2026-07-09] 成功處理但 popup 尚未關 → 短暫等它關閉,讓該 hwnd
                        # 先釋放,縮小「同 class 的下一個 popup 立刻撿到同一 hwnd、下輪被誤判
                        # already-handled 而跳過」的視窗(chained Delphi dialog 常見)。最多 ~1.2s,
                        # 期間可 F12 中止;popup 通常按完很快就關,多半提早 break。
                        _close_deadline = time.time() + 1.2
                        while time.time() < _close_deadline:
                            if not ctypes.windll.user32.IsWindow(hwnd):
                                break
                            _sleep_interruptible(0.05)
                else:
                    retry_counter[key] = (ident, attempts + 1)   # [codex P2] 綁 identity
                    logging.warning(
                        "[%s] %s handler 回 False 且 popup 仍存在 → "
                        "第 %d 次後 retry", label, cls_name, attempts + 1)
                last_progress_log = time.time()
                found_one = True
                # [2026-05-22 v40] v39 的 0.8s 沒解決卡死 (卡死是醫院 app 自己
                # 處理 server roundtrip)，反而拖慢偵測。回到 0.3s — 給 app
                # 處理我們 click 跟可能下個 popup 開啟的時間，但不過度。
                _sleep_interruptible(0.3)
                break  # 從頭再掃一輪 (這次處理完可能觸發下個 popup)

        if not found_one:
            # 每 5s 印一次「仍在等」log 方便 debug
            if time.time() - last_progress_log >= 5.0:
                elapsed = time.time() - start
                logging.info(
                    "[%s] watcher 等候中... 已處理 %d 個 popup，"
                    "已執行 %.1fs (上限 %.0fs，F12 可中止)",
                    label, handled_count, elapsed, total_timeout)
                last_progress_log = time.time()
            # 沒 popup 時 0.4s polling — 體感即時 + 對 message pump 負擔輕
            _sleep_interruptible(0.4)

    logging.info("[%s] watcher 達總時限 %.0fs (處理 %d 個)",
                  label, total_timeout, handled_count)
    return handled_count


_menu_tree_dumped_once = False


def _dump_menu_tree(main_hwnd: int) -> None:
    """[diagnostic] 把主視窗 menu bar 各子選單項目(text + command id + ownerdraw)印到
    log。HIS 選單是 Delphi owner-drawn 時 GetMenuStringW 抓不到文字，需靠 id+位置對照。

    [perf r5] HIS「完成不印」選單是 owner-draw，_find_menu_command_id_by_text 每次都比不到
    文字 → 每次 F11 route A(照光病人)都 dump 整棵選單樹(~200 行)，是 automation_ui.log
    暴漲(4.4MB/session)的最大單一來源。fallback id(MENU_ID_FINISH_NO_PRINT,2026-06-29 起=277)
    已知且穩定，故每個 session 只 dump 一次供對照即可，之後跳過。若 HIS 改版選單結構變動,下次啟動
    的首次 F11 仍會重新 dump 一次。"""
    global _menu_tree_dumped_once
    if _menu_tree_dumped_once:
        return
    _menu_tree_dumped_once = True
    user32 = ctypes.windll.user32
    MF_BYPOSITION = 0x400
    MF_OWNERDRAW = 0x100
    try:
        hmenu = user32.GetMenu(main_hwnd)
        if not hmenu:
            logging.warning("[menu][dump] GetMenu 回 0 — 主視窗無 menu bar?")
            return
        top_n = user32.GetMenuItemCount(hmenu)
        logging.info("[menu][dump] menu bar 共 %d 個頂層項目 (hmenu=%s)", top_n, hmenu)
        for i in range(top_n):
            tbuf = ctypes.create_unicode_buffer(256)
            user32.GetMenuStringW(hmenu, i, tbuf, 256, MF_BYPOSITION)
            sub = user32.GetSubMenu(hmenu, i)
            logging.info("[menu][dump] top[%d] text=%r 有子選單=%s", i, tbuf.value, bool(sub))
            if not sub:
                continue
            for j in range(user32.GetMenuItemCount(sub)):
                sbuf = ctypes.create_unicode_buffer(256)
                slen = user32.GetMenuStringW(sub, j, sbuf, 256, MF_BYPOSITION)
                cid = int(user32.GetMenuItemID(sub, j))
                od = bool(int(user32.GetMenuState(sub, j, MF_BYPOSITION)) & MF_OWNERDRAW)
                logging.info("[menu][dump]    top[%d].sub[%d] id=%s ownerdraw=%s "
                             "textlen=%d text=%r", i, j, cid, od, slen, sbuf.value)
    except Exception:
        logging.debug("[menu][dump] 例外", exc_info=True)


def _find_menu_command_id_by_text(main_hwnd: int, target_text: str) -> int:
    """走主視窗 menu bar → 各子選單，找文字含 target_text 的項目，回其 WM_COMMAND id。

    [2026-06-04] 用 Win32 menu API 依「文字」動態解析(不寫死 id)，比 hardcode 穩。
    找不到回 0，並 dump 整個選單樹到 log(owner-draw menu 抓不到文字時靠這對照 id)。
    """
    user32 = ctypes.windll.user32
    target = target_text.replace(" ", "")
    MF_BYPOSITION = 0x400
    try:
        hmenu = user32.GetMenu(main_hwnd)
        if not hmenu:
            logging.warning("[menu] GetMenu 回 0 — 主視窗無 menu bar?")
            return 0
        for i in range(user32.GetMenuItemCount(hmenu)):
            submenu = user32.GetSubMenu(hmenu, i)
            if not submenu:
                continue
            for j in range(user32.GetMenuItemCount(submenu)):
                buf = ctypes.create_unicode_buffer(256)
                n = user32.GetMenuStringW(submenu, j, buf, 256, MF_BYPOSITION)
                if n <= 0:
                    continue
                txt = buf.value
                if target and target in txt.replace(" ", ""):
                    cmd_id = int(user32.GetMenuItemID(submenu, j))
                    if cmd_id not in (0, -1, 0xFFFFFFFF):
                        logging.info("[menu] 找到 '%s' → command id=%s",
                                     txt.strip(), cmd_id)
                        return cmd_id
        logging.warning("[menu] 選單中找不到含 '%s' 的項目 → dump 選單樹供對照",
                        target_text)
        _dump_menu_tree(main_hwnd)
        return 0
    except Exception:
        logging.debug("[menu] _find_menu_command_id_by_text 例外", exc_info=True)
        return 0


# 完成 > 完成不印。[2026-06-29] HIS V.1150629.01 改版整批 +1:舊 276→277(使用者確認完成不印壞掉,
# 且 probe 新「完成」選單 top[4] index 1 = id 277,與 +1 一致)。F11 照光療程 2/3 用,避免印繳費單。
MENU_ID_FINISH_NO_PRINT = 277


def _f11_normalize_course_value(raw_value: str) -> str:
    """Return the normalized 療程 value used by F11 route selection."""
    value = str(raw_value or "").strip()
    if not value:
        return ""
    return value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def _f11_read_course_value(main_hwnd: int, label: str = "F11") -> str:
    try:
        course_hwnd = _find_療程_edit_hwnd(main_hwnd)
        if not course_hwnd:
            logging.info("[%s] 找不到療程欄，F11 走「全部完成」路徑", label)
            return ""
        course_value = _f11_normalize_course_value(_read_tmemo_text(course_hwnd))
        logging.info("[%s] 讀到療程=%r", label, course_value or "(空白)")
        return course_value
    except Exception:
        logging.debug("[%s] 讀療程欄失敗，F11 走「全部完成」路徑", label, exc_info=True)
        return ""


def _f11_send_finish_no_print(main_hwnd: int, course_value: str,
                              label: str, started_at: float) -> bool:
    """F11 route A: phototherapy course 2/3 -> 完成不印, then same popup flow."""
    dynamic_id = _find_menu_command_id_by_text(main_hwnd, "完成不印")
    candidate_ids = []
    if dynamic_id:
        candidate_ids.append(dynamic_id)
        if dynamic_id != MENU_ID_FINISH_NO_PRINT:
            logging.info("[%s] 完成不印動態 id=%s（既有備援 id=%s）",
                         label, dynamic_id, MENU_ID_FINISH_NO_PRINT)
    else:
        # owner-draw 選單 GetMenuStringW 讀不到文字 → 按文字解析本就比不到「完成不印」,
        # 屬預期;靠校正過的 MENU_ID_FINISH_NO_PRINT。降為 INFO 避免假警報(非真失敗)。
        logging.info("[%s] 完成不印:owner-draw 選單無文字(預期),使用校正 id=%s",
                     label, MENU_ID_FINISH_NO_PRINT)
    if MENU_ID_FINISH_NO_PRINT not in candidate_ids:
        candidate_ids.append(MENU_ID_FINISH_NO_PRINT)

    for cmd_id in candidate_ids:
        if _send_yiling_menu_command(main_hwnd, cmd_id):
            logging.info("[%s][timeline] route=完成不印 療程=%s → 已送 menu id=%s "
                         "(+%.0fms total)",
                         label, course_value, cmd_id,
                         (time.time() - started_at) * 1000)
            # [稽核 2026-07-17] 完成動作無回讀(送出即結束)→ 至少留下送了哪個 id、當時
            # HIS 版本。改版讓 id 位移誤觸別的選單時,這是唯一的事後線索。
            _record_his_action(_LEDGER_HIS_MENU, f"{label} 完成不印",
                               main_hwnd=main_hwnd, target=f"menu:{cmd_id}",
                               value=f"療程={course_value}", outcome=_LEDGER_OK)
            return True
        logging.warning("[%s] 完成不印 menu id=%s 送出失敗，嘗試下一個候選",
                        label, cmd_id)
    logging.error("[%s] 療程=%s 但「完成不印」menu 找不到/送出失敗；"
                  "照光病人不改按 全部完成，以免印出繳費單",
                  label, course_value)
    _record_his_action(_LEDGER_HIS_MENU, f"{label} 完成不印", main_hwnd=main_hwnd,
                       target=f"menu:{','.join(str(i) for i in candidate_ids)}",
                       value=f"療程={course_value}", outcome=_LEDGER_FAILED,
                       detail="所有候選 id 都送出失敗,未完成")
    return False


def _f11_click_finish_all(main_hwnd: int, course_value: str,
                          label: str, started_at: float) -> bool:
    """F11 route B: non-phototherapy course -> 直接按「全部完成」。

    [2026-06-05] 依使用者要求移除「偵測卡號空白→自動補 IC」與卡號把關，
    一律直接按「全部完成」（卡號交由醫院系統 / 醫師自行處理）。"""
    btns = _find_descendants_by_exact_text(main_hwnd, "TButton", "全部完成")
    logging.info("[%s][timeline] route=全部完成 療程=%s，找到 button: %d 個 "
                 "(+%.0fms total)",
                 label, course_value or "(空白/未知)", len(btns),
                 (time.time() - started_at) * 1000)
    if not btns:
        logging.warning("[%s] 找不到 全部完成 button", label)
        return False
    _post_click_to_control(btns[0][0])
    logging.info("[%s][timeline] PostMessage 全部完成 click 完成 (hwnd=%s)，"
                 "sleep 0.5s 給 app settle", label, btns[0][0])
    return True


def _f11_快速完成_main(label: str = "F11") -> bool:
    """F11 主流程：依療程選擇完成路徑 → 輪詢任意順序 popup。

    [2026-05-22 v40] 加 timing log 全程診斷 user 報告的「西醫門診系統當機卡死」。
    每個關鍵點都打 timestamp，跑一次後查 log 看是哪一步觸發 freeze。
    """
    t_f11_start = time.time()
    main_hwnd = _find_hospital_main_window()
    if not main_hwnd:
        logging.warning("[%s] 找不到主程式視窗", label)
        return False
    logging.info("[%s][timeline] 找到 main_hwnd=%s (+%.0fms)",
                  label, main_hwnd, (time.time() - t_f11_start) * 1000)

    # Step 1: 完成路徑分流。
    #   Route A: 療程=2/3（照光）→ 完成不印，不按「全部完成」。
    #   Route B: 療程不是 2/3 或讀不到 → 直接按「全部完成」（不再讀卡號/補 IC）。
    # 兩條路徑送出完成動作後，都進同一套 popup watcher。
    course_value = _f11_read_course_value(main_hwnd, label=label)
    if course_value in ("2", "3"):
        if not _f11_send_finish_no_print(main_hwnd, course_value, label, t_f11_start):
            return False
    else:
        if not _f11_click_finish_all(main_hwnd, course_value, label, t_f11_start):
            return False

    # [2026-05-22 v40] 退回 v39 的 2s → 0.5s。實測 2s 沒解決「卡死」(因為卡死是
    # 醫院 app 自己在處理 server roundtrip，跟我們 polling 無關)，反而拖慢
    # popup 偵測。0.5s 給 app 進入 OnAllComplete 把 first popup 開出來。
    _sleep_interruptible(0.5)
    logging.info("[%s][timeline] 0.5s 後開始 watcher polling (+%.0fs total)",
                  label, time.time() - t_f11_start)

    # Step 2: 輪詢已知 popup (任意順序、可能跳過)
    _f11_popup_watcher(label=label)
    logging.info("[%s][timeline] F11 完整結束，總執行時間 %.1fs",
                  label, time.time() - t_f11_start)
    return True


def _f11_precheck_card_for_phototherapy(label: str = "F11") -> bool:
    """[2026-06-19] F11 前置:若頂部「療程」是 2 或 3(照光 2/3)且「卡號」欄目前空白
    → 中止 F11 並提示「目前卡號未輸入」(照光要計費,卡號不能漏)。

    回 True=可繼續;False=中止。找不到主視窗/療程/卡號欄、或讀取例外 → 一律放行
    (保守:不確定就不擋,避免誤擋一般快速完成)。"""
    try:
        main_hwnd = _find_hospital_main_window()
        if not main_hwnd:
            return True
        liao_hwnd = _find_療程_edit_hwnd(main_hwnd)
        if not liao_hwnd:
            return True
        # [M5 2026-07-09] 與 _f11_read_course_value 共用同一 normalize —— 否則療程欄若是全形
        # 「２」/「３」會不等於 "2"/"3" → 跳過卡號檢查、照光仍「完成不印」→ 卡號空白照樣完成。
        療程 = _f11_normalize_course_value(_read_tmemo_text(liao_hwnd))
        if 療程 not in ("2", "3"):
            return True  # 不是照光 2/3 → 不檢查
        card_hwnd = _find_療程卡號_edit_hwnd(main_hwnd)
        if not card_hwnd:
            return True  # 找不到卡號欄 → 不擋
        card = (_read_tmemo_text(card_hwnd) or "").strip()
        if card:
            return True  # 卡號有值 → 放行
        logging.warning("[%s] 療程=%s(照光)但卡號空白 → 中止 F11,提示醫師", label, 療程)
        _show_uvb_warning(
            main_hwnd, "目前卡號未輸入",
            f"目前卡號未輸入。\n\n照光(療程 {療程})需要先輸入卡號,F11 已中止。\n"
            "請輸入卡號後再按一次 F11。")
        return False
    except Exception:
        logging.exception("[%s] F11 卡號前置檢查例外 → 放行", label)
        return True


def script_F11_adaptive():
    """F11 (解析度無關)：快速完成 — 全部完成 + 任意順序 popup 處理。

    [2026-05-22 v44] 重新加回 ForegroundProtector — user 確認需要這個保護：
    切到 Chrome / 其他視窗看資料時，popup 不要搶回 focus 打斷工作。

    Protector 機制：背景 thread 每 0.1s 偵測 foreground，user 切到非醫院視窗
    時記下；popup 搶 foreground 時 SetForegroundWindow 回 user 視窗。
    我們的 click 走 PostMessage 不需 foreground → popup 在背景一樣會被點掉。

    對醫院 app 額外送的訊息：GetForegroundWindow 是 kernel-only (零訊息)，
    SetForegroundWindow 只在 user 切走時偶發呼叫 (1-2 個 system 訊息)，
    不會塞爆 app message pump。
    """
    logging.info("--- Executing F11 (快速完成 adaptive) ---")
    # [2026-06-19] 照光(療程 2/3)但卡號空白 → 中止 + 提示「目前卡號未輸入」
    if not _f11_precheck_card_for_phototherapy(label="F11"):
        logging.info("F11: 療程 2/3 但卡號空白 → 已中止(提示醫師輸入卡號)")
        return False
    ok = _run_with_foreground_protector(_f11_快速完成_main, label="F11")
    logging.info("F11: %s", "done" if ok else "中斷")
    return bool(ok)


def _find_療程_edit_hwnd(main_hwnd: int) -> int:
    """動態找頂部 header「療程」輸入欄的 hwnd。

    策略 (2026-05-19 修)：用 width 過濾。療程跟類別在所有解析度上都是
    窄數字欄位 (width 35-50)，比其他長文字欄位 (width 60+) 明顯小。
    依 left 排序後第一個窄欄位 = 療程，第二個 = 類別。

    probe 觀察：
      1280x1024: 7 個 TEditExt，療程=(554,104,40)、類別=(666,104,38)
      1920x1080: 19 個 TEditExt (多了 ICD 等欄位)，療程=(557,?,40)、
                  類別=(669,?,38) — 寬度依舊 40/38

    舊版用「第 5 個」算法在 1280x1024 work，但 1920x1080 上多了 5 個欄位
    所以 idx=4 變成錯的欄位 (log 2026-05-19 08:53 證實)。"""
    main_r = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(main_hwnd, ctypes.byref(main_r)):
        return 0

    edits = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 64)
            if cls_buf.value != "TEditExt":
                return True
            r = wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(child, ctypes.byref(r)):
                return True
            rel_y = r.top - main_r.top
            # 頂部 row：相對 y 80-135
            if 80 <= rel_y <= 135:
                edits.append((child, r.left, r.top, r.right - r.left))
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumChildWindows(main_hwnd, cb, 0)
    # 去重
    seen = set()
    uniq = [e for e in edits if not (e[0] in seen or seen.add(e[0]))]
    uniq.sort(key=lambda e: e[1])  # by left
    logging.info("頂部 row TEditExt 從左至右 (%d 個): %s",
                  len(uniq), [(e[0], e[1] - main_r.left, e[3]) for e in uniq])

    # 找 width 在 35-50 之間的（療程跟類別都在這範圍）
    narrow = [e for e in uniq if 35 <= e[3] <= 50]
    logging.info("窄欄位 (w 35-50): %d 個 → %s", len(narrow),
                  [(e[0], e[1] - main_r.left, e[3]) for e in narrow])
    if not narrow:
        logging.warning("頂部 row 找不到窄欄位 (療程)，回 0")
        return 0
    # 左到右第一個窄欄位 = 療程 (第二個 = 類別)
    療程_hwnd = narrow[0][0]
    logging.info("療程 hwnd=%s (頂部 row 第 1 個窄欄位 w<50)", 療程_hwnd)
    return 療程_hwnd


def _find_身份_edit_hwnd(main_hwnd: int) -> int:
    """動態找頂部 header「身份」輸入欄(原顯示 40/01 等代碼)的 hwnd。

    身份欄與療程在【同一排】(probe:皆 top≈110),身份是該排最左的 TEditExt。
    定位策略:先用已驗證、且有寬度過濾的 _find_療程_edit_hwnd 取得「療程」當錨點,
    再找與療程【同一 y(±8px)】的 TEditExt 取最左 = 身份。
    這樣可避開「診斷排」(top≈136,最左欄 left 與身份相同=81 會撞)—— 之前單用
    rel_y 80-135 的寬頻帶在非最大化視窗可能同時框到兩排、靠 z-order 決勝而誤抓
    (工作流審查抓到的定位脆弱點)。
    probe(2026-06-18 張廖年峰機):身份 rel_left≈84、w≈89;其右為(空白)、負擔(A12)、
    卡號、療程、類別、體重。"""
    療程_hwnd = _find_療程_edit_hwnd(main_hwnd)
    if not 療程_hwnd:
        logging.warning("[身份] 找不到療程錨點 → 無法定位身份欄,回 0")
        return 0
    main_r = wintypes.RECT()
    療程_r = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(main_hwnd, ctypes.byref(main_r)):
        return 0
    if not ctypes.windll.user32.GetWindowRect(療程_hwnd, ctypes.byref(療程_r)):
        return 0
    療程_top = 療程_r.top

    edits = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 64)
            if cls_buf.value != "TEditExt":
                return True
            r = wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(child, ctypes.byref(r)):
                return True
            # 只收與療程【同一排】的欄位(±8px),排除診斷排等其他排
            if abs(r.top - 療程_top) <= 8:
                edits.append((child, r.left, r.top, r.right - r.left))
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumChildWindows(main_hwnd, cb, 0)
    seen = set()
    uniq = [e for e in edits if not (e[0] in seen or seen.add(e[0]))]
    if not uniq:
        logging.warning("[身份] 療程同排找不到 TEditExt,回 0")
        return 0
    uniq.sort(key=lambda e: e[1])  # by left
    logging.info("[身份] 療程同排(top≈%d) TEditExt 從左至右 (%d): %s",
                 療程_top - main_r.top, len(uniq),
                 [(e[0], e[1] - main_r.left, e[3]) for e in uniq])
    身份_hwnd, 身份_left, _t, 身份_w = uniq[0]  # 最左 = 身份
    logging.info("[身份] 身份 hwnd=%s rel_left=%d w=%d (療程同排最左)",
                 身份_hwnd, 身份_left - main_r.left, 身份_w)
    return 身份_hwnd


def _set_身份_自費(value: str = "01", label: str = "") -> bool:
    """純自費 Excimer:把左上角「身份」欄(原本 40 等)改成 value(預設 01)。

    比照療程/卡號:WM_SETTEXT(逾時版)→ 失敗 fallback click → 寫回後 read-verify。
    身份屬計費敏感欄位,任何一步失敗或驗證不符都【警告醫師手動確認】而不靜默放過。
    依使用者拍板:不管原值(空白/40/其他)一律寫成 value。"""
    # [UD-02 2026-07-10] 身份=01 是計費敏感寫入。放在 try 之前 check_stop(不會被下方 except 吞掉):
    # 醫師若在前一步(確認框等)按 F12,到此已被取消 → 不可再寫身份欄。
    check_stop()
    try:
        main_hwnd = _find_hospital_main_window()
        if not main_hwnd:
            logging.warning("[%s][身份] 找不到主視窗 → 無法設身份=%s", label, value)
            _show_uvb_warning(
                0, "身份未自動設定",
                f"找不到西醫門診主視窗,無法把身份改成 {value}。\n\n"
                f"請手動把左上角身份改成 {value} 再送出。")
            return False
        身份_hwnd = _find_身份_edit_hwnd(main_hwnd)
        if not 身份_hwnd:
            logging.warning("[%s][身份] 找不到身份欄 → 無法設 %s", label, value)
            _show_uvb_warning(
                main_hwnd, "身份未自動設定",
                f"找不到左上角身份欄,無法自動改成 {value}。\n\n"
                f"請手動把身份改成 {value} 再送出。")
            return False
        before = (_read_tmemo_text(身份_hwnd) or "").strip()
        # 安全把關(正向辨識):身份別是「空白或 1-3 位純數字代碼」(40/01/10…)。
        # 若原值非空且不符這個樣式(例如抓到負擔 'A12'、卡號 'IC49'、姓名或其他長
        # 文字欄)→ 極可能定位錯欄位 → 不寫、警告。寫入前先正向確認這格「像身份欄」,
        # 補上「寫回後讀同一 hwnd 的驗證會自我參照、寫錯欄仍會 pass」的破口
        # (工作流審查抓到的中心風險)。
        if before and not re.fullmatch(r"\d{1,3}", before):
            logging.warning(
                "[%s][身份] 定位到的欄位原值 %r 不像身份代碼(非空且非 1-3 位數字)"
                " → 疑似定位錯欄位,不寫入", label, before)
            _show_uvb_warning(
                main_hwnd, "身份未自動設定",
                f"自動定位到的身份欄內容看起來不對(原值:{before!r})。\n\n"
                f"為安全起見未自動修改,請手動把身份改成 {value} 再送出。")
            # [codex P1] 原值可能是誤抓到的識別資料 → 帳本只記長度,不記內容
            # (跳警告視窗給醫師看無妨,那是他自己的螢幕;落地成檔案才是外洩風險)。
            _record_his_action(_LEDGER_HIS_FIELD, f"{label or 'F'} 身份",
                               main_hwnd=main_hwnd, target="field:身份",
                               value=str(value), outcome=_LEDGER_SKIPPED,
                               detail=f"原值不像身份代碼(長度={len(before)},內容已遮罩),"
                                      f"疑似定位錯欄,未寫")
            return False

        ret = _wm_settext_timeout(身份_hwnd, value)
        if not ret:
            logging.warning("[%s][身份] WM_SETTEXT 回傳失敗 → fallback click", label)
            _replace_edit_text(身份_hwnd, value, main_hwnd=main_hwnd)
        time.sleep(0.05)
        after = (_read_tmemo_text(身份_hwnd) or "").strip()
        if after != value:
            # 再試一次 fallback click(WM_SETTEXT 對某些 Delphi 自訂欄位無效)
            _replace_edit_text(身份_hwnd, value, main_hwnd=main_hwnd)
            time.sleep(0.05)
            after = (_read_tmemo_text(身份_hwnd) or "").strip()
        if after == value:
            logging.info("[%s][身份] 已設身份 %r→%r(自費 Excimer)",
                         label, before, value)
            _record_his_action(_LEDGER_HIS_FIELD, f"{label or 'F'} 身份",
                               main_hwnd=main_hwnd, target="field:身份",
                               value=f"{before}→{value}", outcome=_LEDGER_OK)
            return True
        logging.warning("[%s][身份] 設身份失敗 期望=%r 實際=%r → 警告醫師",
                        label, value, after)
        _show_uvb_warning(
            main_hwnd, "身份設定驗證失敗",
            f"自動把身份改成 {value} 後讀回驗證不符(實際:{after!r})。\n\n"
            f"請手動確認左上角身份是否為 {value} 再送出。")
        # [codex P1] 回讀原文可能是誤抓到的識別資料 → 只記長度,不記內容。
        _record_his_action(_LEDGER_HIS_FIELD, f"{label or 'F'} 身份",
                           main_hwnd=main_hwnd, target="field:身份",
                           value=str(value), outcome=_LEDGER_MISMATCH,
                           detail=f"回讀與預期不符(回讀長度={len(after or '')},內容已遮罩)")
        return False
    except Exception:
        # 例外路徑也要【警告醫師】—— caller(F1/F2/F3)已跳過 51019/療程,若這裡靜默
        # 失敗會變成「沒 key 51019 又沒設身份」且無提示(Codex 審查抓到的破口)。
        logging.exception("[%s][身份] 設身份例外 → 警告醫師手動確認", label)
        try:
            _show_uvb_warning(
                0, "身份未自動設定",
                f"自動設定身份時發生例外,可能未改成 {value}。\n\n"
                f"請手動確認左上角身份是否為 {value} 再送出。")
        except Exception:
            logging.debug("[%s][身份] 例外警告顯示也失敗", label, exc_info=True)
        return False


def _replace_edit_text(field_hwnd: int, new_text: str,
                       main_hwnd: int = 0) -> bool:
    """把 field_hwnd 那個 Edit 的內容換成 new_text。

    策略：click 該欄位中心 → 全選 (Ctrl+A) → typewrite 新值。比 SendMessage
    WM_SETTEXT 更可靠（後者可能不會觸發 Delphi onChange 事件，server 端
    submit 時拿到舊值）。

    位置是 runtime 從 GetWindowRect 抓的，跟解析度無關。"""
    try:
        r = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(field_hwnd, ctypes.byref(r)):
            return False
        cx = (r.left + r.right) // 2
        cy = (r.top + r.bottom) // 2

        # [M4 2026-07-09] 這是【螢幕座標實體點擊+typewrite】—— HIS 被別視窗(Chrome 等)蓋住時,
        # click 會落在覆蓋視窗上、把值(身份 01/療程)打進別的應用程式。動作前:(1)把 HIS 拉到前景;
        # (2)用 WindowFromPoint 驗點擊中心【確實屬於目標欄位】,被遮/不在最上層就中止不亂打,
        # 交由 caller(F1/F2/F3 設身份/療程)走既有的失敗警示路徑,由醫師手動確認。
        _ensure_hospital_foreground(main_hwnd or field_hwnd)
        time.sleep(0.05)   # 等前景切換 + 重繪
        if not _screen_point_in_window(field_hwnd, cx, cy):
            logging.warning(
                "_replace_edit_text: 目標欄位被遮住/不在最上層 → 中止,不把值打到別的視窗")
            return False

        # 暫存滑鼠位置 (操作完還原)
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        saved_x, saved_y = pt.x, pt.y

        # 切英文 IME
        if main_hwnd:
            _force_ime_english(main_hwnd)
        else:
            _force_ime_english(field_hwnd)

        # Click 進欄位 → 全選 → 換值
        hotkey_modules.pyautogui.click(cx, cy)
        time.sleep(0.05)
        if main_hwnd:
            _force_ime_english(main_hwnd)
        hotkey_modules.pyautogui.hotkey("ctrl", "a")
        time.sleep(0.02)
        hotkey_modules.pyautogui.typewrite(new_text, interval=0.02)

        # 還原滑鼠
        try:
            ctypes.windll.user32.SetCursorPos(saved_x, saved_y)
        except Exception:
            pass
        return True
    except Exception:
        logging.error("_replace_edit_text 失敗", exc_info=True)
        return False


# =============================================================================
# 照光 F2/F3 — 自動帶入「醫師上次」療程卡號 (OCR)
# =============================================================================
# 卡號欄常空白,卡號只在「醫師上次」那個 Delphi 格線 (TStringAlignGrid) 裡,而該
# 格線直接畫到螢幕、Win32/UIA/MSAA/複製/PrintWindow 全部讀不到 (實測全黑/空) →
# 唯一可行 = 視窗顯示時螢幕擷取 + Windows 內建 OCR。規則:取最上面「療程=1」那一
# 列的卡號 (使用者指定)。解析/把關邏輯在 cmuh_common.ditto_card_ocr。
#
# 安全 (計費欄位):只在卡號欄「空白」時動作;只有 OCR 有把握 (4 位數字 + 同卡交叉
# 驗證 + 最上列貼近表頭) 才填,沒把握不填只嗶聲提示;填完跳『非阻塞』提示讓醫師核對;
# 寫入只用 WM_SETTEXT (瞬間、不動滑鼠、無空窗);整段 fail-open,絕不影響 51019 order。
#
# 醫師上次 = 主視窗上的 TButton text="醫師上次" (snapshot 證實);格線 class=
# TStringAlignGrid (在 TFOpdditto1 視窗內)。可用 settings/card_autofill_config.json
# {"enabled": true} 才啟用。[2026-06-17] 預設『關閉』:OCR 偶有誤判,暫時停用自動
# 帶卡號(程式碼保留,待 OCR 準確度修正後再開)。F2/F3 仍會呼叫但立即 early-return,
# 等同回到沒有自動帶卡號的舊行為。
_CARD_AUTOFILL_CONFIG = os.path.join(SETTINGS_DIR, "card_autofill_config.json")


def _card_autofill_enabled() -> bool:
    """讀 settings/card_autofill_config.json 的 enabled。

    [2026-06-17] 預設『關閉』(OCR 待修正);只有設定檔存在且 enabled=true 才啟用。"""
    import json
    try:
        with open(_CARD_AUTOFILL_CONFIG, encoding="utf-8") as f:
            return bool(json.load(f).get("enabled", False))
    except FileNotFoundError:
        return False
    except Exception:
        logging.debug("讀 card_autofill_config 失敗,預設停用", exc_info=True)
        return False


def _card_notify_async(title: str, msg: str) -> None:
    """非阻塞提示:在背景緒跳 MessageBox,避免 modal 卡住 hotkey 工作緒(否則
    autofill 在 51019 之前,未關掉的對話框會擋住後面的 order 輸入)。"""
    import threading
    try:
        threading.Thread(
            target=lambda: show_windows_notification(title, msg),
            daemon=True).start()
    except Exception:
        logging.debug("[卡號] 背景通知啟動失敗", exc_info=True)


def _find_first_descendant_by_class(parent_hwnd: int, target_class: str) -> int:
    """EnumChildWindows 找第一個 class=target_class 的子孫 hwnd。"""
    found = [0]
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, buf, 64)
            if buf.value == target_class:
                found[0] = child
                return False
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumChildWindows(parent_hwnd, cb, 0)
    return found[0]


def _find_療程卡號_edit_hwnd(main_hwnd: int) -> int:
    """找頂部 header「卡號」輸入欄 hwnd = 與「療程」同一列、緊鄰其左邊的一般寬度欄。

    用 production 驗證過的 _find_療程_edit_hwnd 定位療程欄(它若定位錯,療程輸入
    早就壞了),再取「同一列 (top 接近療程)、在療程左邊、寬度像卡號欄 (60-115)」中
    left 最大(最靠近療程)的那個。比自行重算窄欄更穩,也避免抓到別列的欄位。
    snapshot:卡號 rel_left≈509 w≈85(內容為 4 位療程卡號如 0009/0033)。"""
    liao_hwnd = _find_療程_edit_hwnd(main_hwnd)
    if not liao_hwnd:
        logging.warning("[卡號] 定位不到療程欄,無法推算卡號欄")
        return 0
    main_r = wintypes.RECT()
    liao_r = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(main_hwnd, ctypes.byref(main_r)):
        return 0
    if not ctypes.windll.user32.GetWindowRect(liao_hwnd, ctypes.byref(liao_r)):
        return 0
    liao_left, liao_top = liao_r.left, liao_r.top
    edits = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, buf, 64)
            if buf.value != "TEditExt":
                return True
            r = wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(child, ctypes.byref(r)):
                return True
            w = r.right - r.left
            if (abs(r.top - liao_top) <= 8 and r.left < liao_left
                    and 60 <= w <= 115):
                edits.append((child, r.left, w))
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumChildWindows(main_hwnd, cb, 0)
    seen = set()
    uniq = [e for e in edits if not (e[0] in seen or seen.add(e[0]))]
    if not uniq:
        logging.warning("[卡號] 找不到療程左邊同列的卡號欄")
        return 0
    card = max(uniq, key=lambda e: e[1])  # 最靠近療程(left 最大)
    logging.info("[卡號] 卡號欄 hwnd=%s rel_left=%s w=%s (療程 hwnd=%s)",
                 card[0], card[1] - main_r.left, card[2], liao_hwnd)
    return card[0]


def _bring_window_front(hwnd: int) -> None:
    """把任意視窗叫到最前(含 AttachThreadInput)。截圖前要視窗真的顯示才有像素。"""
    try:
        SW_RESTORE = 9
        if ctypes.windll.user32.IsIconic(hwnd):
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        pass
    try:
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        fg = ctypes.windll.user32.GetForegroundWindow()
        ftid = (ctypes.windll.user32.GetWindowThreadProcessId(fg, None)
                if fg else 0)
        attached = False
        if ftid and ftid != cur:
            attached = bool(
                ctypes.windll.user32.AttachThreadInput(ftid, cur, True))
        try:
            HWND_TOP = 0
            SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
            ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                                              SWP_NOMOVE | SWP_NOSIZE)
            ctypes.windll.user32.BringWindowToTop(hwnd)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                ctypes.windll.user32.AttachThreadInput(ftid, cur, False)
    except Exception:
        logging.debug("[卡號] _bring_window_front 失敗", exc_info=True)


def _close_window(hwnd: int) -> None:
    try:
        WM_CLOSE = 0x0010
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    except Exception:
        logging.debug("[卡號] 關視窗失敗 hwnd=%s", hwnd, exc_info=True)


def _read_card_from_ditto_window(ditto_hwnd: int):
    """醫師上次視窗已開:叫到最前 → 找格線 → 螢幕截圖 → OCR → 回 CardResult/None。"""
    import tempfile

    from cmuh_common import ditto_card_ocr
    _bring_window_front(ditto_hwnd)
    time.sleep(0.35)  # 等 Delphi 把格線畫到螢幕
    # 安全:螢幕擷取前確認醫師上次『真的在最前景』。否則可能截到被遮住/別的視窗,
    # OCR 出無關數字 → 誤填計費欄。叫不到前景就放棄(fail-open 退回手動)。
    fg = ctypes.windll.user32.GetForegroundWindow()
    if fg != ditto_hwnd:
        logging.warning("[卡號] 醫師上次未在最前景(fg=%s ditto=%s),放棄擷取避免誤讀",
                        fg, ditto_hwnd)
        return None
    grid = _find_first_descendant_by_class(ditto_hwnd, "TStringAlignGrid")
    if not grid:
        logging.warning("[卡號] 醫師上次視窗內找不到格線 TStringAlignGrid")
        return None
    r = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(grid, ctypes.byref(r)):
        return None
    img = ditto_card_ocr.capture_grid_image(
        grid, (r.left, r.top, r.right, r.bottom))
    return ditto_card_ocr.read_card_from_image(img, tmp_dir=tempfile.gettempdir())


def _autofill_卡號_from_醫師上次(label: str = "") -> None:
    """照光 F2/F3:卡號欄空白時,開醫師上次 → OCR → 填「療程=1」那列卡號。

    完全 fail-open:任何問題都只記 log / 提示,絕不丟例外影響後續 order。"""
    try:
        if not _card_autofill_enabled():
            return
        main_hwnd = _find_hospital_main_window()
        if not main_hwnd:
            return
        card_hwnd = _find_療程卡號_edit_hwnd(main_hwnd)
        if not card_hwnd:
            return
        cur = _wm_gettext_timeout(card_hwnd).strip()
        if cur:   # 只在「空白」時動作:任何非空內容一律不碰(不覆蓋既有值)
            logging.info("[%s 卡號] 卡號欄非空(%r),不動作", label, cur)
            return
        btn = _find_descendant_by_class_text(main_hwnd, "TButton", "醫師上次")
        if not btn:
            logging.info("[%s 卡號] 找不到『醫師上次』按鈕,略過", label)
            return
        existing = _find_window_by_class_title("TFOpdditto1")
        # 從『按下按鈕』就進 try:即使等視窗時被 F12/取消打斷拋例外,finally 也保證
        # 關掉我們開的醫師上次、還原前景,不會留下孤兒視窗。
        ditto = None
        result = None
        try:
            if not _post_click_to_control(btn):
                logging.warning("[%s 卡號] 點『醫師上次』按鈕失敗", label)
                return
            ditto = _wait_for_window("TFOpdditto1", timeout=4.0,
                                     exclude_hwnd=existing)
            if not ditto:
                # 可能在逾時後才開 → 再給一點時間抓,避免留下沒關的視窗
                time.sleep(0.3)
                ditto = _find_window_by_class_title("TFOpdditto1",
                                                    exclude_hwnd=existing)
            if not ditto:
                logging.warning("[%s 卡號] 等不到醫師上次視窗", label)
                return
            result = _read_card_from_ditto_window(ditto)
        finally:
            # 不論成敗/中斷:關掉「我們開的」醫師上次(含逾時後才開的)+ 還原醫院前景。
            # 不關使用者本來就開著的那個(existing)。
            to_close = ditto or _find_window_by_class_title(
                "TFOpdditto1", exclude_hwnd=existing)
            if to_close and to_close != existing:
                _close_window(to_close)
                time.sleep(0.1)
            _ensure_hospital_foreground(main_hwnd)
            time.sleep(0.05)

        filled = False
        if result is not None and result.ok and result.card:
            # 寫入前『緊接著』再確認一次仍空白(OCR 期間可能剛被填)→ 不覆蓋。
            again = _wm_gettext_timeout(card_hwnd).strip()
            if again:
                logging.info("[%s 卡號] OCR 後卡號欄已有值(%r),不覆蓋", label, again)
                return
            # 只用 WM_SETTEXT:同緒、瞬間、不動滑鼠、寫入前後無空窗。計費欄『不』採用
            # click+type 補寫(那會動滑鼠且 click→sleep→type 間有空窗,可能蓋掉別人剛
            # 填的值,風險高於效益)。WM_SETTEXT 對 TEditExt 已於療程欄 production 驗證。
            # [UD-01b audit 2026-07-12] 寫入計費欄(卡號)前的最終 F12 閘門:OCR/cleanup/gettext
            # 期間(數秒)F12 不會經過 check_stop → 若不補,取消後仍會寫卡號。此處 raise 由下方
            # `except SubsystemInterrupted: raise` 傳播,乾淨中止(UD-01 已備妥傳播路徑)。
            check_stop()
            _wm_settext_timeout(card_hwnd, result.card)
            verify = _wm_gettext_timeout(card_hwnd).strip()
            if verify == result.card:
                filled = True
                logging.info("[%s 卡號] 已填卡號=%s", label, result.card)
                _card_notify_async(
                    "照光卡號", f"已自動帶入療程卡號 {result.card}\n(請核對是否正確)")
                # [稽核 2026-07-17] 卡號屬計費/識別資料 → 帳本【不記明文】,只記回讀結果。
                _record_his_action(_LEDGER_HIS_FIELD, f"{label} 卡號自動帶入",
                                   main_hwnd=main_hwnd, target="field:卡號",
                                   value="(已遮罩)", outcome=_LEDGER_OK)
            else:
                logging.warning("[%s 卡號] WM_SETTEXT 未生效(verify=%r),改提示手動",
                                label, verify)
                _record_his_action(_LEDGER_HIS_FIELD, f"{label} 卡號自動帶入",
                                   main_hwnd=main_hwnd, target="field:卡號",
                                   value="(已遮罩)", outcome=_LEDGER_MISMATCH,
                                   detail="WM_SETTEXT 未生效,回讀與預期不符(值已遮罩)")
        if not filled:
            reason = result.reason if result is not None else "讀取失敗"
            logging.warning("[%s 卡號] 未自動填:%s", label, reason)
            try:
                import winsound
                winsound.MessageBeep(0x30)
            except Exception:
                pass
            _card_notify_async(
                "照光卡號", "卡號自動讀取不確定,請手動填入卡號。")
    except SubsystemInterrupted:
        # [UD-01 2026-07-10] 卡號 OCR 要開『醫師上次』視窗+截圖辨識,耗時數秒,是 F2/F3 全程中醫師
        # 最有機會按 F12 的窗口(發現按錯病人)。此函式的 fail-open 只該涵蓋『自身功能失敗』,不可把
        # 『醫師取消(F12)』也吞掉 —— 吞掉會讓 F2/F3 照樣往下進 51019/設身份。往上傳,乾淨中止。
        raise
    except Exception:
        logging.error("[%s 卡號] 自動帶卡號流程例外(已忽略)", label, exc_info=True)


def script_F4_adaptive():
    """F4: 冷凍 — 51017 (no 療程)。"""
    logging.info("--- Executing F4 (冷凍 51017) ---")
    ok = _script_code_input_adaptive("51017", label="F4", set_療程=None)
    logging.info("F4 (冷凍): %s", "done" if ok else "skipped")
    return bool(ok)


# =============================================================================
# F9/F10 (同意書) — 通用 Win32 helpers
# =============================================================================
# 同意書開立作業 視窗 class = TOrMain（snapshot 2026-05-18 證實）。
# 「其他 → 同意書」menu id = 669（user 測試確認;2026-06-29 HIS V.1150629.01 改版後 668→669,整批 +1）。
# 流程：
#   1. SendMessage(TFopdmain, WM_COMMAND, 669)  → 打開 TOrMain 視窗
#   2. 等 TOrMain 出現 (FindWindow loop)
#   3. 切到「手術及治療」TTabSheet（點 tab header）
#   4. 點 MO04 (F9) / MU02 (F10) radio (TGroupButton text 含 code)
#   5. 點「開立電子」TButton
#   6+ 後續 popup 操作（Round 2 之後加）

MENU_ID_同意書 = 669


def _get_window_pid(hwnd: int) -> int:
    """回傳視窗所屬行程 PID(失敗回 0)。用於「只對 HIS 行程的對話框動作」的把關。"""
    try:
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _find_window_by_class_title(class_name: str, title_kw: str = "",
                                  exclude_hwnd: int = 0,
                                  require_pid: int = 0,
                                  exclude_hwnds: tuple = ()) -> int:
    """全域找 class=X 且 title 含 keyword 的可見視窗。

    [H1 2026-07-09] require_pid 非 0 時,只回傳【屬於該 PID(HIS 行程)】的視窗 —— 避免把
    別的程式跳出的同 class(#32770)標準對話框誤當 HIS 警告框去自動按「是」。exclude_hwnds
    可額外排除多個(如已處理過的第一個對話框),避免重複動作。

    [2026-05-22 v38] 從 EnumWindows + Python callback 改 FindWindowExW
    (純 Win32，不走 Python boundary)。EnumWindows + Python cb 每個 top-level
    window 都跨 C→Python 邊界 (~0.05ms/個) — 一台 PC 通常 100-300 個
    top-level windows = 10-30ms per call。9 popup class × 0.12s polling
    = ~70% CPU 都在 Python callback。改 FindWindowExW 後降到 < 1ms。
    """
    user32 = ctypes.windll.user32
    # FindWindowExW(hWndParent=NULL, hWndChildAfter, class, title)
    # hWndParent=NULL + 走 prev_hwnd 鏈 = 跨所有 top-level windows
    FindWindowExW = user32.FindWindowExW
    FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND,
                                wintypes.LPCWSTR, wintypes.LPCWSTR]
    FindWindowExW.restype = wintypes.HWND

    prev = 0
    while True:
        try:
            hwnd = FindWindowExW(None, prev, class_name, None)
        except Exception:
            return 0
        if not hwnd:
            return 0
        prev = hwnd
        if hwnd == exclude_hwnd or (exclude_hwnds and hwnd in exclude_hwnds):
            continue
        try:
            if not user32.IsWindowVisible(hwnd):
                continue
        except Exception:
            continue
        if require_pid:
            try:
                wpid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                if wpid.value != require_pid:
                    continue
            except Exception:
                continue
        if title_kw:
            try:
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    continue
                t_buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, t_buf, n + 1)
                if title_kw not in t_buf.value:
                    continue
            except Exception:
                continue
        return hwnd
    # [2026-05-25 v15 死碼清除] 移除舊 Python callback 路徑 (~50 行) — 上方
    # while True FindWindowExW loop 一定 return (hwnd or 0)，下面永遠到不了。


def _wait_for_window(class_name: str, title_kw: str = "",
                       timeout: float = 10.0,
                       exclude_hwnd: int = 0,
                       poll_sec: float = 0.03,
                       require_pid: int = 0) -> int:
    """每 poll_sec 找一次，最多等 timeout 秒。回傳 hwnd 或 0。
    poll_sec 預設 30ms (比早期 100ms 快 — 對 F9/F10 警告 dialog 反應更即時)。
    [H1] require_pid 非 0 時只等【該 HIS 行程】的視窗(不會被別程式的同 class 對話框攔截)。"""
    end = time.time() + timeout
    while time.time() < end:
        hwnd = _find_window_by_class_title(class_name, title_kw, exclude_hwnd,
                                           require_pid=require_pid)
        if hwnd:
            return hwnd
        _sleep_interruptible(min(poll_sec, max(0.0, end - time.time())))
    return 0


# =============================================================================
# Smart Foreground Protector — F9/F10 背景執行時保護使用者焦點
# =============================================================================
# 設計：背景 thread 監看 foreground。
#   - 預設不動：使用者在醫院視窗，新 popup 開了就讓它開（正常前景）
#   - 偵測到使用者切到「非醫院視窗」(Chrome / Notepad / ...) → 記住該視窗
#   - 此後若有醫院視窗搶 foreground → 立刻 SetForegroundWindow 回到使用者視窗
#
# 這樣 UX：
#   - 「不切走」= 使用者看著流程跑（popup 正常顯示，可監視）
#   - 「切走」= popup 全在背景，使用者不受打擾
# 比硬 HWND_BOTTOM 好太多（後者把 popup 推到底層使用者看不到、且每次都搶
# 一下 focus 跳回主視窗）

HOSPITAL_WINDOW_CLASSES = {
    "TFopdmain",         # 主程式 (西醫門診醫師作業)
    "TOrMain",           # 同意書開立作業
    "Tfm_agree",         # 列印同意/說明書 popup
    "TfrmOrrSentence",   # 請選擇片語 popup
    "#32770",            # Windows 標準警告對話框
    "TFOpdselpt",        # 門診診間選擇病人 (F11 結束、回到病患列表)
}


class _ForegroundProtector:
    """背景 thread 保護使用者非醫院視窗的 focus。"""

    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self.tracked_user_hwnd = 0
        self._restore_count = 0

    def start(self):
        # 初始：若 foreground 是非醫院視窗，先記下來（保護它）
        cur = ctypes.windll.user32.GetForegroundWindow()
        if cur and _get_class_name_of(cur) not in HOSPITAL_WINDOW_CLASSES:
            self.tracked_user_hwnd = cur
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="F9_F10_ForegroundProtector", daemon=True)
        self._thread.start()
        logging.info("ForegroundProtector started, tracked=%s",
                     self.tracked_user_hwnd)

    def stop(self):
        self._stop.set()
        logging.info("ForegroundProtector stopped (restored %d times)",
                     self._restore_count)

    def _run(self):
        # [2026-05-25 v15 CPU 優化] 0.1s → 0.3s polling — F11 期間 (10-60s) 跑
        # GetForegroundWindow + GetClassName 每 100ms 沒必要；300ms 仍體感即時
        # 且 restore foreground 不需秒級精度，CPU 用量降 3x。
        POLL_SEC = 0.3
        while not self._stop.is_set():
            try:
                cur = ctypes.windll.user32.GetForegroundWindow()
                if not cur:
                    time.sleep(POLL_SEC)
                    continue
                cur_cls = _get_class_name_of(cur)
                is_hospital = cur_cls in HOSPITAL_WINDOW_CLASSES
                if is_hospital:
                    # 醫院視窗成為 foreground。如果使用者有追蹤的非醫院視窗，
                    # 就 restore 它。否則 (使用者本來就在醫院視窗) 不動。
                    if (self.tracked_user_hwnd
                            and ctypes.windll.user32.IsWindow(self.tracked_user_hwnd)):
                        ctypes.windll.user32.SetForegroundWindow(self.tracked_user_hwnd)
                        self._restore_count += 1
                else:
                    # 非醫院視窗 = 使用者「真的」想用的視窗，記下來
                    if cur != self.tracked_user_hwnd:
                        self.tracked_user_hwnd = cur
                        logging.debug("ForegroundProtector tracked=%s (%s)",
                                       cur, cur_cls)
            except Exception:
                pass
            time.sleep(POLL_SEC)


def _get_class_name_of(hwnd: int) -> str:
    """便捷 wrapper, 取 hwnd 的 class name。"""
    try:
        buf = ctypes.create_unicode_buffer(64)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 64)
        return buf.value
    except Exception:
        return ""


def _send_window_to_back(hwnd: int) -> bool:
    """把視窗推到 z-order 最底層（不活化、不搶 focus）。

    用於 F9/F10 流程：醫院系統開新視窗時預設會搶 foreground 打斷使用者，
    用這個推到底層 → 使用者保持當前視窗。我們所有的訊息都用 PostMessage，
    不需要視窗是 foreground 就能跑。

    SWP_NOACTIVATE：不要把 hwnd 變 active
    SWP_NOMOVE/SWP_NOSIZE：保持位置 / 大小
    HWND_BOTTOM (=1)：z-order 最底"""
    if not hwnd:
        return False
    try:
        SWP_NOACTIVATE = 0x0010
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        HWND_BOTTOM = 1
        ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_BOTTOM, 0, 0, 0, 0,
            SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)
        return True
    except Exception:
        logging.debug("_send_window_to_back 失敗", exc_info=True)
        return False


def _find_descendant_by_class_text(parent_hwnd: int,
                                     target_class: str,
                                     text_keyword: str) -> int:
    """EnumChildWindows 找 class=X 且 text 含 keyword 的子視窗（遞迴）。"""
    found = [0]

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 64)
            if cls_buf.value == target_class:
                n = ctypes.windll.user32.GetWindowTextLengthW(child)
                if n > 0:
                    t_buf = ctypes.create_unicode_buffer(n + 1)
                    ctypes.windll.user32.GetWindowTextW(child, t_buf, n + 1)
                    if text_keyword in t_buf.value:
                        found[0] = child
                        return False
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumChildWindows(parent_hwnd, cb, 0)
    return found[0]


def _post_click_to_control(hwnd: int, client_x: Optional[int] = None,
                             client_y: Optional[int] = None) -> bool:
    """送 WM_LBUTTONDOWN + WM_LBUTTONUP 到目標 control，完全不動實體滑鼠。

    位置用 client 座標（相對於該 control 左上角）；不指定就用該 control 的
    client 中心。比 pyautogui.click 好處：
      1. 不會移動實體滑鼠（不會干擾使用者）
      2. 不會被 SetCursorPos 競賽條件影響
      3. 訊息直接到目標 control，不會被別人攔截

    對 Delphi VCL 大部分控制項都生效（TButton/TBitBtn/TGroupButton/TabCtrl
    等都處理 WM_LBUTTONDOWN 來觸發 click event）。"""
    if not hwnd:
        return False
    try:
        if client_x is None or client_y is None:
            r = wintypes.RECT()
            if not ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(r)):
                return False
            client_x = (r.right - r.left) // 2 if client_x is None else client_x
            client_y = (r.bottom - r.top) // 2 if client_y is None else client_y
        lparam = ((client_y & 0xFFFF) << 16) | (client_x & 0xFFFF)
        MK_LBUTTON = 0x0001
        WM_LBUTTONDOWN = 0x0201
        WM_LBUTTONUP = 0x0202
        down_ok = bool(ctypes.windll.user32.PostMessageW(
            hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam))
        up_ok = bool(ctypes.windll.user32.PostMessageW(
            hwnd, WM_LBUTTONUP, 0, lparam))
        if not (down_ok and up_ok):
            logging.warning("_post_click_to_control PostMessage failed: "
                            "hwnd=%s down=%s up=%s",
                            hwnd, down_ok, up_ok)
            return False
        return True
    except Exception:
        logging.error("_post_click_to_control 失敗", exc_info=True)
        return False


def _click_control_center(hwnd: int) -> bool:
    """【相容介面】等同 _post_click_to_control(hwnd) — 不動滑鼠，送訊息點擊
    control 的 client center。原本用 pyautogui.click 會閃動滑鼠，已改成訊息。"""
    return _post_click_to_control(hwnd)


def _click_button_by_text(parent_hwnd: int, text: str) -> bool:
    """找 TButton text 完全等於 text → 用 PostMessage WM_LBUTTONDOWN/UP 觸發。

    不用 SendMessage BM_CLICK：對開啟 modal popup 的 button，BM_CLICK 是
    synchronous，會卡在 popup 的 modal message loop 直到 user 關閉。
    PostMessage 非同步立刻返回，popup 由 Delphi 後續處理，呼叫端用
    _wait_for_window poll 偵測。
    （實測 2026-05-18：SendMessage BM_CLICK 在「開立電子」卡了 73 秒）"""
    btn = _find_descendant_by_class_text(parent_hwnd, "TButton", text)
    if not btn:
        return False
    return _post_click_to_control(btn)


# =============================================================================
# F9/F10 Round 4 — popup 開立電子 + 警告對話框 (是)
# =============================================================================
# snapshot (settings/snapshot_20260518_130851.txt) 證實確認對話框是標準
# Windows MessageBox：
#   class = "#32770" (Windows DialogBoxClass)
#   title = "警告"
#   children = 3:
#     Button "是(&Y)" hwnd=??? at (571, 547) ← 要按這個
#     Button "否(&N)" hwnd=??? at (669, 547)
#     Static  "確定沒有病人問題答覆..." at (523, 495)
#
# 對 #32770 標準對話框，PostMessage WM_COMMAND IDYES (=6) 等同按下「是」。
# 比找 Button hwnd 再 click 更乾淨。

IDYES = 6
IDOK = 1
IDCANCEL = 2


def _f9_f10_round4_submit_and_confirm(popup_hwnd: int, label: str = "") -> bool:
    """點 popup 內 開立電子 → 等警告對話框 → 點 是 → 等對話框關。

    若另跳「未滿 18」對話框（同樣是 #32770 但 title 可能不同），也按 是/確定。
    若沒跳對話框（罕見情況），靜默繼續。

    [2026-05-22 v38] 加 timing log 量化每階段延遲，方便 user 跟我們確認
    2-3s 卡頓究竟是 (a) server 自然延遲 (無法優化) 還是 (b) 我們可控的部分。
    """
    t_round_start = time.time()
    # [H1 2026-07-09] 取 popup(確定是 HIS 視窗)的 PID,後續只對【同一 HIS 行程】的
    # #32770 對話框自動按「是」—— 避免 10s 等待窗內別的程式跳出的標準對話框(存檔/刪除/
    # 警告)被誤按「是」而造成文書/資料事故。
    # [F3 audit 2026-07-12] PID 取不到改 fail-closed(原本退回舊 fail-open 行為):require_pid=0
    # 會對【任一行程】的 #32770 自動按是,萬一是別程式的存檔/刪除框即釀事故。popup_hwnd 歷經
    # round1-3 必為有效視窗,取不到 PID 幾乎不可能;真發生時不自動確認、交醫師手動按「是」。
    popup_pid = _get_window_pid(popup_hwnd)
    # Step A: 點 popup 內的 開立電子 button (async)
    if not _click_button_by_text(popup_hwnd, "開立電子"):
        logging.warning("[%s] popup 找不到 開立電子 button", label)
        return False
    t_clicked = time.time()
    logging.info("[%s] 已點 popup 開立電子 (+%.0fms)，等警告對話框",
                  label, (t_clicked - t_round_start) * 1000)
    if not popup_pid:
        logging.warning(
            "[%s] 無法取得同意書 popup 的 HIS 行程 PID → 不自動確認 #32770 警告框"
            "(fail-closed,避免誤按別程式對話框),請醫師手動按「是」", label)
        return True

    # Step B: 等警告對話框出現 (class #32770)
    # title 可能是 "警告" 或其他變體，用 class 即可
    dlg = _wait_for_window("#32770", title_kw="", timeout=10,
                            exclude_hwnd=popup_hwnd, require_pid=popup_pid)
    t_dlg_appeared = time.time()
    if not dlg:
        logging.info("[%s] 沒等到警告對話框 (可能直接送出)", label)
        return True
    server_resp_ms = (t_dlg_appeared - t_clicked) * 1000
    logging.info("[%s] 警告對話框 hwnd=%s (server 處理 %.0fms — 自然延遲)",
                  label, dlg, server_resp_ms)
    time.sleep(0.05)  # 30ms+50ms ≈ 80ms 總 latency；舊版 100ms+300ms = 400ms
    check_stop()

    # Step C: 對對話框 PostMessage WM_COMMAND IDYES
    # 也可以 _post_click_to_control 找 "是(&Y)" button，但對標準 #32770
    # 對話框 WM_COMMAND IDYES 是最乾淨的方式。
    WM_COMMAND = 0x0111
    ctypes.windll.user32.PostMessageW(dlg, WM_COMMAND, IDYES, 0)
    t_idyes_posted = time.time()
    logging.info("[%s] 已 PostMessage WM_COMMAND IDYES (=是) 給對話框", label)

    # 等對話框關 (30ms poll → 對話框關閉延遲最多 30ms)
    end_t = time.time() + 5
    while time.time() < end_t:
        if not ctypes.windll.user32.IsWindow(dlg):
            break
        time.sleep(0.03)
        check_stop()
    t_dlg_closed = time.time()
    dlg_close_ms = (t_dlg_closed - t_idyes_posted) * 1000
    logging.info("[%s] 警告對話框已關 (是→關 %.0fms — server 寫入時間)",
                  label, dlg_close_ms)

    # Step D: 等等看是否還有「未滿 18」之類的後續對話框
    # [2026-05-22 v33] 從硬 sleep 0.4s 改 event-driven poll — 三個 exit 條件：
    #   (a) popup_hwnd 已消失 → 同意書送出完成，無第二 dialog → 立刻 return
    #   (b) 偵測到 dlg2 出現 → 跳出 poll 進入處理
    #   (c) 0.5s 超時 → 認定沒有第二 dialog
    # 常見情況 (無第二 dialog) 通常 50-200ms popup 就消失 → 省 200-350ms。
    dlg2 = 0
    poll_deadline = time.time() + 0.5
    while time.time() < poll_deadline:
        if not ctypes.windll.user32.IsWindow(popup_hwnd):
            logging.info("[%s] popup 已關 → 無第二 dialog 需處理", label)
            return True
        dlg2 = _find_window_by_class_title(
            "#32770", "", exclude_hwnd=popup_hwnd, require_pid=popup_pid,
            exclude_hwnds=(dlg,) if dlg else ())   # 排除已處理的第一個對話框,避免重複轟
        if dlg2:
            break
        time.sleep(0.05)
        check_stop()

    if dlg2:
        logging.info("[%s] 偵測到第二個對話框 hwnd=%s (可能是未滿 18)", label, dlg2)
        # 同樣送 IDYES（IDOK=1 也試）
        ctypes.windll.user32.PostMessageW(dlg2, WM_COMMAND, IDYES, 0)
        time.sleep(0.15)
        # IDOK 備援（某些對話框「確定」是 IDOK 不是 IDYES）
        ctypes.windll.user32.PostMessageW(dlg2, WM_COMMAND, IDOK, 0)
        end_t = time.time() + 5
        while time.time() < end_t:
            if not ctypes.windll.user32.IsWindow(dlg2):
                break
            time.sleep(0.05)
        logging.info("[%s] 第二個對話框處理完", label)
    return True


def _enum_direct_children(parent_hwnd: int,
                           target_class: str = "") -> list:
    """列出 parent_hwnd 的直系子視窗（不遞迴）；可選 class 過濾。"""
    GW_CHILD = 5
    GW_HWNDNEXT = 2
    children = []
    h = ctypes.windll.user32.GetWindow(parent_hwnd, GW_CHILD)
    while h:
        if not target_class:
            children.append(h)
        else:
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(h, cls_buf, 64)
            if cls_buf.value == target_class:
                children.append(h)
        h = ctypes.windll.user32.GetWindow(h, GW_HWNDNEXT)
    return children


def _get_window_text(hwnd: int) -> str:
    n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _find_page_control_by_tab_set(or_hwnd: int, expected_tabs: list) -> int:
    """找 TPageControl 的直系 TTabSheet text 集合『包含』所有 expected_tabs
    的那個。視窗裡可能有很多 nested PageControl（snapshot 顯示 29 個），
    用這個判斷篩出『正確那個』，不會誤觸隱藏的 nested tab。"""
    page_controls = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 64)
            if cls_buf.value == "TPageControl":
                page_controls.append(child)
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumChildWindows(or_hwnd, cb, 0)

    expected_set = set(expected_tabs)
    for pc in page_controls:
        sheets = _enum_direct_children(pc, "TTabSheet")
        texts = {_get_window_text(s) for s in sheets}
        # 必須【完整包含】所有 expected_tabs（允許多）
        if expected_set.issubset(texts):
            logging.info("_find_page_control: pc=%s sheets_texts=%s",
                          pc, sorted(texts))
            return pc
    return 0


def _send_message_timeout(hwnd: int, msg: int, wparam: int, lparam: int,
                           timeout_ms: int = 2000) -> int:
    """SendMessage with timeout — 防止對方視窗 hang 住卡死我們的 thread。
    SMTO_ABORTIFHUNG=0x0002 | SMTO_NORMAL=0x0000.

    [fix] 結果用 c_size_t（=DWORD_PTR，64 位元下 8 bytes）。原因：
    ctypes.windll.user32 是 process 全域共用物件，cmuh_common.abbrev_engine 的
    原生欄位取代會把 SendMessageTimeoutW.argtypes[6] 設成 POINTER(c_size_t)。
    一旦縮寫功能（使用者多半開著）碰過原生欄位，這裡若用 c_long → byref 型別
    不符 → ctypes.ArgumentError（症狀：F9/F10 點「局麻」radio 時整個流程當掉，
    且與螢幕解析度無關）。用 c_size_t 對「有設/沒設 argtypes」兩種情況都正確。"""
    result = ctypes.c_size_t(0)
    SMTO_ABORTIFHUNG = 0x0002
    SendMessageTimeoutW = ctypes.windll.user32.SendMessageTimeoutW
    ret = SendMessageTimeoutW(hwnd, msg, wparam, lparam,
                                SMTO_ABORTIFHUNG, timeout_ms,
                                ctypes.byref(result))
    if ret == 0:
        logging.debug("SendMessageTimeout 失敗或超時 hwnd=%s msg=0x%X", hwnd, msg)
    return result.value


# Kernel32 argtypes 顯式設定 — 64-bit Python ctypes 預設把指標當 c_int (4 bytes)
# 會截斷高位址，VirtualFreeEx 拿錯 address → 漏 4KB。設成 c_void_p (8 bytes) 才對。
_kernel32 = ctypes.windll.kernel32
try:
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.VirtualAllocEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                          ctypes.c_size_t, wintypes.DWORD,
                                          wintypes.DWORD]
    _kernel32.VirtualAllocEx.restype = wintypes.LPVOID
    _kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                         ctypes.c_size_t, wintypes.DWORD]
    _kernel32.VirtualFreeEx.restype = wintypes.BOOL
    _kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                              wintypes.LPVOID, ctypes.c_size_t,
                                              ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.ReadProcessMemory.restype = wintypes.BOOL
    _kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                               wintypes.LPVOID, ctypes.c_size_t,
                                               ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.WriteProcessMemory.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
except Exception:
    logging.debug("kernel32 argtypes 設定失敗", exc_info=True)


# user32.SendMessageTimeoutW 權威簽章 — ctypes.windll.user32 是 process 全域共用
# 物件，cmuh_common.abbrev_engine 也會（lazy）設定同一個函式的 argtypes。這裡在
# main.py 匯入時就設成「與 abbrev_engine 完全相同」的簽章，讓 _send_message_timeout
# 與跨行程 TCM_GETITEMRECT 不論縮寫功能有沒有先跑過都行為一致。
#   第 7 參數 = POINTER(c_size_t)，配 byref(c_size_t) 結果（見 _send_message_timeout）。
#   lParam = c_ssize_t：才容得下 >2^31 的跨行程位址（remote_addr）；若沒設 argtypes
#   ctypes 預設把它當 c_int → OverflowError → 同意書(F9/F10)分頁矩形抓不到、靜默退回估算。
try:
    ctypes.windll.user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    ctypes.windll.user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
except Exception:
    logging.debug("user32 SendMessageTimeoutW argtypes 設定失敗", exc_info=True)


def _get_tab_item_rect_cross_process(tab_hwnd: int, item_index: int):
    """[2026-05-22 任務 B] Cross-process TCM_GETITEMRECT — 抓 tab N 的精準
    client rect。回傳 (left, top, right, bottom) 或 None。

    TCM_GETITEMRECT 的 lParam 是「目標 process 位址空間」的 RECT pointer。
    本 process 直接傳本地 pointer 會段錯誤。正規做法：
      1. OpenProcess (VM_OPERATION|VM_READ|VM_WRITE)
      2. VirtualAllocEx 在目標 process 配 16 bytes (RECT)
      3. SendMessageTimeoutW 用 remote_addr 當 lParam (帶 timeout 防 hang)
      4. ReadProcessMemory 從 remote 讀 RECT 回本地
      5. VirtualFreeEx + CloseHandle

    任一步失敗 → None，呼叫端 fallback 估算座標。

    為何要這個：估算座標 (105, 12) 在不同 DPI / 字體 / Delphi 設計下會落空，
    特別是 1920x1080。精準 rect 才能保證 click 落在正確 tab header。
    """
    PROCESS_VM_OPERATION = 0x0008
    PROCESS_VM_READ = 0x0010
    PROCESS_VM_WRITE = 0x0020
    MEM_COMMIT = 0x1000
    MEM_RELEASE = 0x8000
    PAGE_READWRITE = 0x04
    TCM_GETITEMRECT = 0x130A

    pid = wintypes.DWORD()
    try:
        ctypes.windll.user32.GetWindowThreadProcessId(
            tab_hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        process = _kernel32.OpenProcess(
            PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE,
            False, pid.value)
        if not process:
            return None
        remote_addr = None
        try:
            rect_size = ctypes.sizeof(wintypes.RECT)
            remote_addr = _kernel32.VirtualAllocEx(
                process, None, rect_size, MEM_COMMIT, PAGE_READWRITE)
            if not remote_addr:
                return None
            # 用 timeout 版避免 cross-process call hang
            # c_size_t（DWORD_PTR）：見 _send_message_timeout 說明（全域 argtypes 污染）
            result = ctypes.c_size_t(0)
            SMTO_ABORTIFHUNG = 0x0002
            ret = ctypes.windll.user32.SendMessageTimeoutW(
                tab_hwnd, TCM_GETITEMRECT, item_index, remote_addr,
                SMTO_ABORTIFHUNG, 1500, ctypes.byref(result))
            if ret == 0 or not result.value:
                return None
            local_rect = wintypes.RECT()
            bytes_read = ctypes.c_size_t()
            ok = _kernel32.ReadProcessMemory(
                process, remote_addr, ctypes.byref(local_rect),
                rect_size, ctypes.byref(bytes_read))
            if not ok:
                return None
            return (local_rect.left, local_rect.top,
                     local_rect.right, local_rect.bottom)
        finally:
            if remote_addr:
                try:
                    _kernel32.VirtualFreeEx(process, remote_addr, 0, MEM_RELEASE)
                except Exception:
                    pass
            try:
                _kernel32.CloseHandle(process)
            except Exception:
                pass
    except Exception:
        logging.debug("_get_tab_item_rect_cross_process 例外", exc_info=True)
        return None


def _send_wm_notify_tcn_selchange(tabctrl_hwnd: int) -> bool:
    """[2026-05-22 v29] 跨 process 對 TabCtrl 的 parent 發 WM_NOTIFY TCN_SELCHANGE
    強制 Delphi VCL TPageControl swap ActivePage。

    為何要這個：cross-process PostMessage WM_LBUTTONDOWN 到 TabCtrl 雖然視覺
    切到 target tab，但有時 Delphi VCL 的 CNNotify 沒收到對應 TCN_SELCHANGE
    (timing / message order / reflect 問題) → ActivePage 不 swap → 下方 radio
    區還是舊 tab → 開立電子 submit 錯 radio。

    機制：
      1. TabCtrl 子控制項，發 WM_NOTIFY 必須給「parent」(form)，由 form 的
         WndProc reflect 回 TabCtrl 自己的 CN_NOTIFY → TPageControl.CNNotify
         處理 → SetActivePage。
      2. NMHDR struct = {hwndFrom, idFrom, code}。64-bit: 8 + 8 + 4 padded
         to 16 (struct alignment) = 24 bytes 安全。
      3. NMHDR pointer 必須是 target process 的位址 → VirtualAllocEx +
         WriteProcessMemory。
    """
    WM_NOTIFY = 0x004E
    TCN_FIRST = 0xFFFFFDDA  # -550 unsigned
    TCN_SELCHANGE = (TCN_FIRST - 1) & 0xFFFFFFFF  # -551 unsigned = 0xFFFFFDD9
    PROCESS_VM_OPERATION = 0x0008
    PROCESS_VM_READ = 0x0010
    PROCESS_VM_WRITE = 0x0020
    MEM_COMMIT = 0x1000
    MEM_RELEASE = 0x8000
    PAGE_READWRITE = 0x04

    try:
        parent = ctypes.windll.user32.GetParent(tabctrl_hwnd)
        if not parent:
            logging.debug("_send_wm_notify_tcn_selchange: 找不到 TabCtrl parent")
            return False
        ctrl_id = ctypes.windll.user32.GetDlgCtrlID(tabctrl_hwnd)
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(tabctrl_hwnd, ctypes.byref(pid))
        if not pid.value:
            return False
        process = _kernel32.OpenProcess(
            PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE,
            False, pid.value)
        if not process:
            return False
        remote_addr = None
        try:
            # NMHDR 在 x64 是 24 bytes (HWND=8, UINT_PTR=8, UINT=4+pad=8)
            class NMHDR(ctypes.Structure):
                _fields_ = [("hwndFrom", wintypes.HWND),
                             ("idFrom", ctypes.c_void_p),  # UINT_PTR
                             ("code", ctypes.c_uint)]
            nmhdr = NMHDR()
            nmhdr.hwndFrom = tabctrl_hwnd
            nmhdr.idFrom = ctrl_id
            nmhdr.code = TCN_SELCHANGE
            sz = ctypes.sizeof(NMHDR)
            remote_addr = _kernel32.VirtualAllocEx(
                process, None, sz, MEM_COMMIT, PAGE_READWRITE)
            if not remote_addr:
                return False
            written = ctypes.c_size_t()
            ok = _kernel32.WriteProcessMemory(
                process, remote_addr, ctypes.byref(nmhdr), sz,
                ctypes.byref(written))
            if not ok:
                return False
            # 發給 parent (form)，form 收到後 reflect CN_NOTIFY 給 TabCtrl，
            # TPageControl.CNNotify 看到 TCN_SELCHANGE → SetActivePage
            _send_message_timeout(parent, WM_NOTIFY, ctrl_id, remote_addr,
                                    timeout_ms=1500)
            return True
        finally:
            if remote_addr:
                try:
                    _kernel32.VirtualFreeEx(process, remote_addr, 0, MEM_RELEASE)
                except Exception:
                    pass
            try:
                _kernel32.CloseHandle(process)
            except Exception:
                pass
    except Exception:
        logging.debug("_send_wm_notify_tcn_selchange 例外", exc_info=True)
        return False


def _switch_tab_by_text(or_hwnd: int, tab_text: str) -> tuple[bool, int]:
    """切到指定 text 的 TTabSheet。回傳 (success, target_sheet_hwnd)。

    [2026-05-22 v29 重寫] 解決 v27/v28「上方 tab strip 切了但 ActivePage 不 swap」
    bug — F9/F10 開立電子產出錯誤同意書 (微小皮膚移植 而非 預期類型)。

    Root cause v28：
      v28 在 PostMessage mouse click 之後立刻 SendMessage TCM_SETCURSEL。
      SendMessage 同步先抵達 → tab strip idx 已是 target_idx。然後 mouse
      click 才被 TabCtrl 處理 → HitTest 看到 idx 沒變 → 不發 TCN_SELCHANGE
      → TPageControl.CNNotify 沒被叫 → ActivePage 不 swap。

    v29 修法：
      1. 只送 mouse click，不再事後 TCM_SETCURSEL 蓋掉 SELCHANGE。
      2. 驗證用 z-order — Delphi VCL TPageControl 切 ActivePage 時把 active
         TTabSheet 拉到 Z-order 最上。_enum_direct_children 用 GW_HWNDNEXT
         走 Z-order → sheets[0] 就是 active sheet。實測 snapshot 證實。
         不再用 IsWindowVisible (3 個 sheet 可能都 VISIBLE，不分 active)。
      3. 失敗 fallback：跨 process VirtualAllocEx 配 NMHDR + SendMessage
         WM_NOTIFY TCN_SELCHANGE 給 TabCtrl 的 parent，強制觸發
         TPageControl.CNNotify → SetActivePage。
      4. target_sheet hwnd 用 text 比對 (不靠 array index，避免 Z-order 亂)

    回傳 (True, target_sheet_hwnd) 表示 sheet 找到；success 是否 ActivePage
    真的 swap 寫在 log。即使 swap 失敗，caller 仍可用 target_sheet 做 scope
    search — 但 開立電子 行為依視覺 ActivePage 決定 → 失敗會送錯同意書。
    """
    TAB_DISPLAY_ORDER = ["手術", "手術及治療", "檢查"]
    pc = _find_page_control_by_tab_set(or_hwnd, TAB_DISPLAY_ORDER)
    if not pc:
        logging.warning("_switch_tab: 找不到 包含 3 個 tab 的 PageControl")
        return False, 0
    sheets = _enum_direct_children(pc, "TTabSheet")
    # target_sheet: 用 text 比對找 hwnd (給 caller scope)
    target_sheet = 0
    for s in sheets:
        try:
            if _get_window_text(s) == tab_text:
                target_sheet = s
                break
        except Exception:
            continue
    if not target_sheet:
        logging.warning("_switch_tab: PageControl 內找不到 text='%s' 的 sheet", tab_text)
        return False, 0
    # target_idx: 顯示位置 (= TabCtrl 內部 idx)
    try:
        target_idx = TAB_DISPLAY_ORDER.index(tab_text)
    except ValueError:
        logging.warning("_switch_tab: tab_text='%s' 不在 TAB_DISPLAY_ORDER", tab_text)
        return False, 0
    logging.info("_switch_tab: pc=%s 顯示位置 idx=%d (tab=%s) target_sheet=%s",
                  pc, target_idx, tab_text, target_sheet)

    def _do_mouse_click():
        item_rect = _get_tab_item_rect_cross_process(pc, target_idx)
        if item_rect:
            cx = (item_rect[0] + item_rect[2]) // 2
            cy = (item_rect[1] + item_rect[3]) // 2
            logging.info("_switch_tab: TCM_GETITEMRECT 精準 rect=%s 中心=(%d,%d)",
                          item_rect, cx, cy)
            _post_click_to_control(pc, client_x=cx, client_y=cy)
            return
        # Fallback: 估算 (1280x1024 經驗值)
        est_x = {0: 30, 1: 105, 2: 195}.get(target_idx, 30 + target_idx * 75)
        est_y = 12
        logging.info("_switch_tab: 估算 click client=(%d,%d) [fallback]",
                      est_x, est_y)
        _post_click_to_control(pc, client_x=est_x, client_y=est_y)

    def _is_target_active() -> bool:
        """ActivePage 真切到 target 的可靠判定：z-order 最上 sheet == target_sheet。
        Delphi VCL TPageControl.SetActivePage 會 BringWindowToTop(新 page)
        → EnumChildWindows(GW_HWNDNEXT) 走 Z-order → sheets[0] 就是 active。"""
        try:
            cur_sheets = _enum_direct_children(pc, "TTabSheet")
            return bool(cur_sheets) and cur_sheets[0] == target_sheet
        except Exception:
            return False

    # 第一輪：純 mouse click，不要事後 TCM_SETCURSEL 蓋掉 SELCHANGE
    _do_mouse_click()
    end_t = time.time() + 0.8
    success = False
    while time.time() < end_t:
        if _is_target_active():
            success = True
            break
        time.sleep(0.05)

    if not success:
        # 第二輪 fallback：跨 process WM_NOTIFY TCN_SELCHANGE 強制 swap ActivePage
        logging.warning("_switch_tab: mouse click 後 800ms 內 ActivePage 沒切，"
                          "fallback 用 WM_NOTIFY TCN_SELCHANGE")
        TCM_SETCURSEL = 0x130C
        # 先把 tab strip 對齊 (純視覺，無 SELCHANGE)，再強制發 SELCHANGE
        _send_message_timeout(pc, TCM_SETCURSEL, target_idx, 0, timeout_ms=1000)
        sent = _send_wm_notify_tcn_selchange(pc)
        logging.info("_switch_tab: WM_NOTIFY TCN_SELCHANGE %s",
                      "已送出" if sent else "送出失敗")
        end_t = time.time() + 0.8
        while time.time() < end_t:
            if _is_target_active():
                success = True
                break
            time.sleep(0.05)

    if success:
        logging.info("_switch_tab: ActivePage 已切到 target_sheet (z-order 驗證通過)")
    else:
        logging.warning("_switch_tab: ActivePage 切換失敗 — 視覺仍可能在錯 tab，"
                          "開立電子可能 submit 錯誤頁面 → 回傳 success=False 讓 caller 中止")
    # [H3 2026-07-09] 第一個回傳值改回報【ActivePage 是否真的切成功】,不再永遠 True。
    # 開立電子的 submit 依【視覺 ActivePage】決定,swap 失敗仍送出=歷史上的「微小皮膚移植」
    # 錯誤同意書事故(v27/v28)。改成失敗即讓 caller 中止、交人工開立,寧可不自動也不送錯。
    return success, target_sheet


def _click_radio_by_text(parent_hwnd: int, text_keyword: str) -> bool:
    """找 TGroupButton text 含 keyword → click 中心。TGroupButton 在
    Delphi 是 RadioGroup 的內部 child；BM_CLICK 對 TGroupButton 不可靠，
    用 mouse click 較穩。"""
    radio = _find_descendant_by_class_text(parent_hwnd, "TGroupButton",
                                             text_keyword)
    if not radio:
        return False
    return _click_control_center(radio)


# =============================================================================
# F9/F10 Round 3 — 片語選擇 popup (TfrmOrrSentence)
# =============================================================================
# user 觀察 (2026-05-18) row index 對應：
#   所患疾病 popup 列表 (固定順序所有電腦一致)：
#     [0] 皮膚疾患       ← F10 用這個
#     [1] 指甲內生
#     [2] 皮膚角化症
#     [3] 皮膚腫瘤       ← F9 用這個
#     [4] 血管瘤
#     ...
#   手術原因 popup 列表：
#     [0] 治療           ← F9 用這個
#     [1] 確診           ← F10 用這個
#     [2] 改善
#     [3] 外觀改善
#
# popup 結構：
#   TfrmOrrSentence (popup, 850x600)
#     ├── TPanel (top toolbar)
#     │   ├── TButton "帶回" ← 確認選擇 + 關閉 popup
#     │   └── TButton "離開"
#     └── TPageControl → TTabSheet → TStringAlignGrid ← phrase 列表


def _send_key_to_window(hwnd: int, vk: int, count: int = 1,
                         interval: float = 0.05) -> None:
    """對指定 hwnd 送 N 次 VK 鍵 (WM_KEYDOWN + WM_KEYUP)。用 PostMessage
    非同步，不需要 foreground，也不會被 IME 攔截。"""
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    for _ in range(count):
        ctypes.windll.user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
        ctypes.windll.user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)
        time.sleep(interval)


def _select_phrase_and_return(片語_btn_hwnd: int, row_idx: int,
                                label: str = "") -> bool:
    """單一片語欄位流程：點片語按鈕 → 等 popup → grid VK_DOWN N 次 →
    點帶回 → 等 popup 關。

    Default 高亮列 = row 0（user 觀察）。若 row_idx > 0 就 VK_DOWN row_idx 次。
    """
    # 點 片語 button (async, opens TfrmOrrSentence popup)
    if not _post_click_to_control(片語_btn_hwnd):
        logging.warning("[%s] 點 片語 button 失敗", label)
        return False
    logging.info("[%s] 已點 片語 button hwnd=%s", label, 片語_btn_hwnd)

    # Wait for 片語 popup (timeout 8s)
    phrase_popup = _wait_for_window("TfrmOrrSentence",
                                      title_kw="請選擇片語", timeout=8)
    if not phrase_popup:
        logging.warning("[%s] 等不到 TfrmOrrSentence popup", label)
        return False
    logging.info("[%s] 片語 popup hwnd=%s", label, phrase_popup)
    # [2026-05-22 v31] 從硬 sleep 0.4s 改成 event-driven poll — 等 grid 出現代表
    # popup 已 paint。實測通常 50-150ms 就有 grid，省 250-350ms。
    grid = 0
    paint_deadline = time.time() + 1.0
    while time.time() < paint_deadline:
        grids = _enum_class_in_window(phrase_popup, "TStringAlignGrid")
        if grids:
            grid = grids[0][0]
            break
        time.sleep(0.03)
        check_stop()
    if not grid:
        logging.warning("[%s] popup 內 1.0s 內找不到 TStringAlignGrid", label)
        return False
    logging.info("[%s] grid hwnd=%s", label, grid)

    # Navigate to row_idx by sending VK_DOWN (default selected = row 0)
    # [2026-05-22 v31] interval 0.05→0.03 (省 ~40-90ms 每次)；post-VK_DOWN
    # 0.2→0.08 (省 120ms)。Delphi TStringAlignGrid 對 VK_DOWN 即時反應。
    VK_DOWN = 0x28
    if row_idx > 0:
        _send_key_to_window(grid, VK_DOWN, count=row_idx, interval=0.03)
        logging.info("[%s] grid 已 VK_DOWN %d 次 → row %d", label, row_idx, row_idx)
        time.sleep(0.08)
    else:
        logging.info("[%s] row=0 (default highlight)，無需 VK_DOWN", label)
    check_stop()

    # 點 帶回 button (async)
    if not _click_button_by_text(phrase_popup, "帶回"):
        logging.warning("[%s] popup 內找不到 帶回 button", label)
        return False
    logging.info("[%s] 已點 帶回", label)

    # 等 popup 關閉（class TfrmOrrSentence 消失）
    end_t = time.time() + 5
    while time.time() < end_t:
        if not _find_window_by_class_title("TfrmOrrSentence", "請選擇片語"):
            logging.info("[%s] 片語 popup 已關閉", label)
            return True
        time.sleep(0.1)
        check_stop()
    logging.warning("[%s] 片語 popup 未在 5 秒內關閉（可能仍卡）", label)
    return True  # 還是回 True 讓主流程繼續


def _find_descendants_by_exact_text(parent_hwnd: int, target_class: str,
                                       target_text: str) -> list:
    """找所有 class+text 精確匹配的子孫；按 (top, left) 排序去重。

    跟 _find_descendant_by_class_text 不同：這個比對【完整 strip 後相等】，
    用來精確區分 '片語' vs '單張片語'（同 class、文字含子字串會混淆）。"""
    out = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 64)
            if cls_buf.value != target_class:
                return True
            n = ctypes.windll.user32.GetWindowTextLengthW(child)
            if n > 0:
                t_buf = ctypes.create_unicode_buffer(n + 1)
                ctypes.windll.user32.GetWindowTextW(child, t_buf, n + 1)
                if t_buf.value.strip() == target_text:
                    r = wintypes.RECT()
                    if ctypes.windll.user32.GetWindowRect(child, ctypes.byref(r)):
                        out.append((child, r.top, r.left))
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumChildWindows(parent_hwnd, cb, 0)
    seen = set()
    uniq = [x for x in out if not (x[0] in seen or seen.add(x[0]))]
    uniq.sort(key=lambda x: (x[1], x[2]))
    return uniq


def _f9_f10_round3_phrases(popup_hwnd: int, row_所患: int, row_手術: int,
                              label: str = "") -> bool:
    """對 popup 內 2 個片語欄位依序執行選擇流程。

    row_所患: 所患疾病 popup 內目標 row (從 0 開始)
    row_手術: 手術原因 popup 內目標 row (從 0 開始)
    """
    # 用 EXACT text="片語" 過濾，避免抓到 "單張片語"（同 class TButton、x 差
    # 1 px、sort 順序不穩，曾誤抓 row2 單張片語當成 row3 片語，導致誤填
    # 實施手術名稱）
    pien = _find_descendants_by_exact_text(popup_hwnd, "TButton", "片語")
    if len(pien) < 3:
        logging.warning("[%s] 找不到 3 個 text='片語' 的 button (找到 %d 個)",
                         label, len(pien))
        return False
    btn_所患 = pien[0][0]    # row 1 (y 最小)
    # pien[1] = 實施手術名稱旁邊 (不用)
    btn_手術 = pien[2][0]    # row 3 (y 最大)
    logging.info("[%s] 片語 buttons (text='片語'): 所患=%s 手術=%s",
                  label, btn_所患, btn_手術)

    # Step A: 所患疾病 片語
    if not _select_phrase_and_return(btn_所患, row_所患,
                                       label=label + "/所患疾病片語"):
        return False
    # [2026-05-22 v31] 0.3→0.12s — Tfm_agree popup 重 paint 新值通常 < 100ms
    time.sleep(0.12)

    # Step B: 手術原因 片語
    if not _select_phrase_and_return(btn_手術, row_手術,
                                       label=label + "/手術原因片語"):
        return False
    # [2026-05-22 v31] 0.3→0.08s — Round 4 接著 _f9_f10_round4 也有自己的等待
    time.sleep(0.08)
    return True


# =============================================================================
# F9/F10 Round 2 — popup 視窗 (Tfm_agree) 內操作
# =============================================================================
# Popup class = "Tfm_agree", title 含 "列印同意"
# 結構（snapshot 2026-05-18）：
#   3 個 TEdit 直系子（top-to-bottom 排序）：
#     [0] 所患疾病     popup-rel y=53  (要清空)
#     [1] 實施手術名稱  popup-rel y=109 (read-only，不動)
#     [2] 手術原因     popup-rel y=170 (要清空)
#   3 個 TGroupButton 在麻醉方式 group y=694 (popup-rel 526):
#     leftmost  = 全麻
#     middle    = 半麻
#     rightmost = 局麻  (F9/F10 都要勾)
#   popup 內 開立電子 TButton text="開立電子"
#
# Round 2 只做「清空 + 勾局麻」，不點 片語 也不按 開立電子（保留給 user 手動
# 操作 + 後續 Round 3/4 加自動化）。


def _enum_class_in_window(parent_hwnd: int, target_class: str) -> list:
    """EnumChildWindows 抓全部 class=X 的子孫，按 (top, left) 排序。
    回傳 list of (hwnd, rect_top, rect_left)。"""
    out = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(child, cls_buf, 64)
            if cls_buf.value == target_class:
                r = wintypes.RECT()
                if ctypes.windll.user32.GetWindowRect(child, ctypes.byref(r)):
                    out.append((child, r.top, r.left))
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumChildWindows(parent_hwnd, cb, 0)
    out.sort(key=lambda x: (x[1], x[2]))
    # 去重複（hwnd 可能在 EnumChildWindows 出現多次）
    seen = set()
    uniq = []
    for h, t, l in out:
        if h not in seen:
            seen.add(h)
            uniq.append((h, t, l))
    return uniq


def _clear_edit_text(edit_hwnd: int) -> bool:
    """把 TEdit 內容設為空字串。用 WM_SETTEXT 空字串。

    對 Delphi TEdit，Text property 讀取走 GetWindowText，所以 WM_SETTEXT 空
    立刻反映在 Text 上，server 送單時拿到空字串。

    比 「click + Ctrl+A + Delete」乾淨：不會動到 IME 跟焦點。"""
    if not edit_hwnd:
        return False
    # [stability] 走帶逾時的 WM_SETTEXT(SMTO_ABORTIFHUNG)：F9/F10 round2 清空欄位
    # 在 hotkey 工作緒上，原裸 SendMessageW 遇醫院 app 凍住會無限期阻塞 → 永久
    # 鎖死全部熱鍵。
    _wm_settext_timeout(edit_hwnd, "")
    return True


def _f9_f10_round2_popup_actions(popup_hwnd: int, label: str = "") -> bool:
    """popup 開啟後執行：清空 所患疾病 + 清空 手術原因 + 勾 局麻。

    依 (top, left) 排序的 enumeration 找控制項位置，跟解析度無關。"""
    edits = _enum_class_in_window(popup_hwnd, "TEdit")
    logging.info("[%s] popup TEdit count=%d", label, len(edits))
    if len(edits) < 3:
        logging.warning("[%s] popup 找到 TEdit 不足 3 個 (預期 所患疾病/實施/手術原因)",
                         label)
        return False
    # 由上到下：[0] 所患疾病, [1] 實施手術名稱, [2] 手術原因
    edit_所患疾病 = edits[0][0]
    edit_手術原因 = edits[2][0]
    logging.info("[%s] 所患疾病 hwnd=%s, 手術原因 hwnd=%s", label,
                  edit_所患疾病, edit_手術原因)

    # 清空 所患疾病
    _clear_edit_text(edit_所患疾病)
    logging.info("[%s] 已清空 所患疾病", label)
    # 清空 手術原因（即使已是空白，也保險再清一次）
    _clear_edit_text(edit_手術原因)
    logging.info("[%s] 已清空 手術原因", label)

    # 找 麻醉方式 group 內 3 個 TGroupButton（在 popup 下半部 y > popup top + 400）
    popup_r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(popup_hwnd, ctypes.byref(popup_r))
    all_gb = _enum_class_in_window(popup_hwnd, "TGroupButton")
    # 過濾 popup 下半部（麻醉 group 在 popup-rel y ≈ 526），排除頂部其他 group
    bottom_half = [g for g in all_gb if (g[1] - popup_r.top) > 400]
    # 排序：取 y 範圍接近的 3 個（同一 row）
    if len(bottom_half) < 3:
        logging.warning("[%s] popup 下半 TGroupButton 數不足 3 (麻醉 group)", label)
        return False
    # 找最右邊（局麻）— 同一 row（top 接近）中 left 最大的
    first_row_top = bottom_half[0][1]
    same_row = [g for g in bottom_half if abs(g[1] - first_row_top) <= 5]
    same_row.sort(key=lambda g: g[2])  # 由左至右
    if len(same_row) < 3:
        # 退而求其次：取整批 bottom_half 最右邊
        same_row = sorted(bottom_half, key=lambda g: g[2])
    radio_局麻 = same_row[-1][0]  # 最右邊
    logging.info("[%s] 局麻 radio hwnd=%s (左到右 row 共 %d 個)",
                  label, radio_局麻, len(same_row))

    # 勾 局麻 — 用 BM_CLICK 觸發 Delphi onClick (radio group 會自動 uncheck 其他)
    # [2026-05-22 v33/重新套用] 從 SendMessageW 改 SendMessageTimeoutW 100ms —
    # 原本 SendMessage 同步等 Delphi onClick handler 跑完 (re-render radio group +
    # 觸發 paint cycle)，user 報 1-2s 卡頓 stall。SendMessageTimeout 100ms
    # 內不回就 abort 等待 (Delphi 仍會處理該訊息，只是我們不卡)。
    BM_CLICK = 0x00F5
    _send_message_timeout(radio_局麻, BM_CLICK, 0, 0, timeout_ms=100)
    # 補：PostMessage click 作備援（某些 TGroupButton BM_CLICK 不一定觸發）
    _post_click_to_control(radio_局麻)
    logging.info("[%s] 已勾 局麻 (BM_CLICK + LBUTTON)", label)

    return True


def _run_with_foreground_protector(fn, *args, **kwargs):
    """跑 fn 時啟動 ForegroundProtector 保護使用者非醫院視窗 focus。"""
    protector = _ForegroundProtector()
    protector.start()
    try:
        return fn(*args, **kwargs)
    finally:
        protector.stop()


def script_F9_F10_consent_form_adaptive(form_code: str,
                                          phrase_row_所患: int = 0,
                                          phrase_row_手術: int = 0,
                                          label: str = "") -> bool:
    """Round 1：從主程式 → 開同意書視窗 → 選 tab → 選 radio → 開立電子。

    form_code：'MO04' (F9) 或 'MU02' (F10)，會在 手術及治療 tab 找對應 radio
    回傳 True = 跑到 開立電子 那步；False = 中間某步失敗。

    Round 2+ 還要做：popup 內 所患疾病/手術原因/片語/麻醉 + 開立電子 + 確認"""
    # Step 1: 找主程式 → 觸發 其他→同意書
    main_hwnd = _find_hospital_main_window()
    if not main_hwnd:
        logging.warning("[%s] 找不到主程式視窗", label)
        return False
    # 用 Post (非同步) 避免 SendMessage 卡住 (實測 2026-05-18 12:43)
    if not _send_yiling_menu_command(main_hwnd, MENU_ID_同意書):
        logging.warning("[%s] 同意書 WM_COMMAND 送出失敗", label)
        return False
    logging.info("[%s] 已觸發 其他→同意書 (id=%s, Post)", label, MENU_ID_同意書)

    # Step 2: 等 TOrMain 視窗出現
    # [穩定性] 25s timeout + 失敗 retry 1 次。實測 2026-05-19 11:47 醫院後端慢
    # (reg52.cgi 500 error 同期間) 導致 8s 不夠 → TOrMain 沒開出來。25s 給
    # server slow 時段充裕時間。retry 一次防偶發 message lost。
    or_hwnd = _wait_for_window("TOrMain", title_kw="同意書開立作業",
                                 timeout=25)
    if not or_hwnd:
        logging.warning("[%s] 等 TOrMain 25s 超時，重 Post WM_COMMAND 再試 1 次", label)
        if not _send_yiling_menu_command(main_hwnd, MENU_ID_同意書):
            logging.warning("[%s] 同意書 retry WM_COMMAND 送出失敗", label)
            return False
        or_hwnd = _wait_for_window("TOrMain", title_kw="同意書開立作業",
                                     timeout=15)
        if not or_hwnd:
            logging.warning(
                "[%s] 重試後仍等不到 TOrMain — 可能醫院後端慢/同意書系統未啟動",
                label)
            _record_his_action(
                _LEDGER_HIS_MENU, f"{label} 同意書開啟", main_hwnd=main_hwnd,
                target=f"menu:{MENU_ID_同意書}", value=str(form_code),
                outcome=_LEDGER_FAILED, detail="重試後仍等不到 TOrMain,未開啟同意書")
            return False
        logging.info("[%s] 重試成功", label)
    logging.info("[%s] TOrMain hwnd=%s 已開啟", label, or_hwnd)
    # [稽核 2026-07-17] 同意書視窗真的開起來 = 選單 id 仍正確(隱含驗證)。記下開了哪種
    # 同意書(MO04 腫瘤手術 / MU02 切片)與當時 HIS 版本。
    _record_his_action(_LEDGER_HIS_MENU, f"{label} 同意書開啟", main_hwnd=main_hwnd,
                       target=f"menu:{MENU_ID_同意書}", value=str(form_code),
                       outcome=_LEDGER_OK)
    # 不主動推到底層 — ForegroundProtector 會在使用者切走時才保護
    _sleep_interruptible(0.3)  # 等視窗 paint 完成

    # Step 3: 切到「手術及治療」tab
    tab_ok, target_sheet = _switch_tab_by_text(or_hwnd, "手術及治療")
    if not tab_ok:
        logging.warning("[%s] 找不到/切換 手術及治療 tab 失敗", label)
        return False
    logging.info("[%s] 已切到 手術及治療 tab (sheet=%s)", label, target_sheet)
    _sleep_interruptible(0.5)   # 等 tab 切完 + radio 重繪

    # Step 4: 點 form_code 對應的 radio (MO04 / MU02)
    # [2026-05-22 任務 B] scope 在 target_sheet 而非 or_hwnd — 確保 click 到
    # 「手術及治療」分頁的 radio (即使視覺切換失敗也對)。F9 (MO04) 在「手術」
    # tab 也有相似代號 radio，搜整個 or_hwnd 會抓到隱藏 tab 的同名 radio。
    if not _click_radio_by_text(target_sheet, form_code):
        logging.warning("[%s] target_sheet 下找不到 radio %s，fallback or_hwnd 全域搜",
                         label, form_code)
        if not _click_radio_by_text(or_hwnd, form_code):
            logging.warning("[%s] or_hwnd 全域也找不到 radio %s", label, form_code)
            return False
        logging.warning("[%s] 已用 or_hwnd fallback 點到 %s — 可能誤點分頁",
                         label, form_code)
    else:
        logging.info("[%s] 已選 radio %s (scope=target_sheet)", label, form_code)
    _sleep_interruptible(0.2)

    # Step 5: 點 同意書視窗的 開立電子 按鈕
    # [F1 audit 2026-07-12] 點擊前先記下本 HIS 行程 PID + 既有 Tfm_agree popup:之後只認
    # 【本 HIS 行程新開】的同意書 popup,避免選到殘留(前一份沒關)或別行程/別 HIS 實例的
    # 同一 class 同標題 popup,而對錯病人清欄位+送出。
    or_pid = _get_window_pid(or_hwnd)
    stale_popup = _find_window_by_class_title("Tfm_agree", title_kw="列印同意")
    # 先試 target_sheet (若按鈕在分頁內)，找不到再用 or_hwnd (按鈕通常在
    # TOrMain 底層 panel 不在 tab 頁內)
    if not _click_button_by_text(target_sheet, "開立電子"):
        if not _click_button_by_text(or_hwnd, "開立電子"):
            logging.warning("[%s] 找不到 開立電子 按鈕", label)
            return False
    logging.info("[%s] 已點 開立電子，等 popup 跳出", label)

    # Step 6 (Round 2): 等 popup (Tfm_agree) 出現
    # popup 可能要等 hospital app 從 server load 病歷資料、塞 TEdit、render UI，
    # 約 5-30 秒。timeout 60s 留充裕空間，poll 100ms 一次。
    # [2026-05-22 v37] 若病患未滿 18 歲，Delphi 會先跳一個 TMessageForm
    # title=Information 的 modal dialog (只有 &Yes button) 擋住 popup。
    # 必須先點 Yes 才會繼續開 Tfm_agree。所以這個 loop 也 poll 該 dialog
    # 並自動 click Yes。
    popup = None
    age_dlg_handled = False
    popup_deadline = time.time() + 60
    while time.time() < popup_deadline:
        # 先檢查目標 popup(只認本 HIS 行程新開、且非點擊前既有的 stale popup)
        candidate = _find_window_by_class_title("Tfm_agree",
                                                  title_kw="列印同意",
                                                  exclude_hwnd=stale_popup,
                                                  require_pid=or_pid)
        if candidate:
            popup = candidate
            break
        # 同時檢查 未滿18歲 Information dialog (TMessageForm + title=Information)
        if not age_dlg_handled:
            info_dlg = _find_window_by_class_title("TMessageForm",
                                                     title_kw="Information")
            if info_dlg:
                buttons = _enum_class_in_window(info_dlg, "TButton")
                # [L1 2026-07-09] 原本盲點 buttons[0]。標準未滿18 Information 只有一顆 &Yes;
                # 但若是 Yes/No 等多鈕變體,盲點第一顆可能點到「否」。改:單顆才直接點;多顆時
                # 只點文字為 Yes/確定/是/OK 的那顆,認不出就【不自動點】交醫師,避免誤點。
                target_btn = 0
                if len(buttons) == 1:
                    target_btn = buttons[0][0]
                elif len(buttons) > 1:
                    for b in buttons:
                        try:
                            bt = _get_window_text(b[0]).replace("&", "").strip().lower()
                        except Exception:
                            bt = ""
                        if bt in ("yes", "ok", "確定", "是"):
                            target_btn = b[0]
                            break
                if target_btn:
                    _post_click_to_control(target_btn)
                    logging.info(
                        "[%s] 偵測到未滿18歲 Information dialog (hwnd=%s)，"
                        "已自動點 Yes (button hwnd=%s, 共 %d 顆)",
                        label, info_dlg, target_btn, len(buttons))
                    age_dlg_handled = True
                    _sleep_interruptible(0.3)  # 給 Delphi 處理 dismiss + 開 popup
                    continue
                elif buttons:
                    logging.warning(
                        "[%s] Information dialog 有 %d 顆按鈕但認不出 Yes → 不自動點,交醫師",
                        label, len(buttons))
        _sleep_interruptible(0.1)
    if not popup:
        logging.warning("[%s] 等不到 popup (Tfm_agree) 60s 超時", label)
        return False
    logging.info("[%s] popup hwnd=%s 已開啟 (年齡 dialog 處理=%s)",
                  label, popup, age_dlg_handled)
    # popup 視窗出現 ≠ 資料 load 完。Delphi 通常 popup 先 paint 空白 → 再從病歷
    # 帶入欄位資料。若太早 clear，後續 load 會覆蓋掉我們清空的字。
    # [2026-05-22 v30] 改成 event-driven poll — 等 所患疾病 TEdit 有內容才視為
    # server fill 完成。原本硬 sleep 2.0s 不論快慢一律等滿；poll 50ms 一次，
    # 通常 200-600ms 就回，省 1.4-1.8s。Cap 3.0s 防 server 異常時無限等。
    fill_deadline = time.time() + 3.0
    filled = False
    while time.time() < fill_deadline:
        try:
            edits_now = _enum_class_in_window(popup, "TEdit")
            if len(edits_now) >= 3 and _get_window_text(edits_now[0][0]):
                # 所患疾病 已被 server 帶入內容 → 可以清空了
                filled = True
                break
        except Exception:
            logging.debug("[%s] poll TEdit fill 例外", label, exc_info=True)
        time.sleep(0.05)
        check_stop()
    if filled:
        logging.info("[%s] popup TEdit fill 偵測完成 (%.2fs)", label,
                      3.0 - (fill_deadline - time.time()))
    else:
        logging.warning("[%s] popup 3.0s 內未偵測到 server fill，仍繼續清空", label)
    check_stop()

    # Step 7 (Round 2): 清空 2 個 edit + 勾 局麻
    # [H2 2026-07-09] round2 失敗(TEdit/局麻 radio 找不到 → 欄位沒清成/局麻沒勾)【不可】照樣
    # 進 Round 4 自動送出電子同意書 —— 否則會送出一份沒清空既往內容/沒勾局麻的錯誤文書。中止交人工。
    if not _f9_f10_round2_popup_actions(popup, label=label):
        logging.warning("[%s] Round 2 失敗(欄位沒清成/局麻沒勾)→ 不自動送出,交醫師手動確認", label)
        return False
    logging.info("[%s] Round 2 完成 (清空+局麻)", label)

    # Step 8 (Round 3): 對 2 個片語欄位執行 click 片語→選 row→帶回
    check_stop()
    # [H2] 片語沒選成也一樣不可自動送出(內容不完整的同意書)→ 中止交人工。
    if not _f9_f10_round3_phrases(popup, phrase_row_所患, phrase_row_手術, label=label):
        logging.warning("[%s] Round 3 失敗(片語沒選成)→ 不自動送出,交醫師手動確認", label)
        return False
    logging.info("[%s] Round 3 完成 (片語)", label)

    # Step 9 (Round 4): 點 popup 開立電子 + 警告對話框「是」+ (未滿 18 對話框)
    # [2026-05-22 v38] 0.3s→0.1s — 片語選擇完 popup 已穩定，不需這麼久 settle
    check_stop()
    time.sleep(0.1)
    # [H2] round4 若連 開立電子 都沒點成(找不到按鈕)→ 回報未完成,不假裝成功。
    if not _f9_f10_round4_submit_and_confirm(popup, label=label):
        logging.warning("[%s] Round 4 未完整完成(開立電子/確認框未如預期)", label)
        return False
    logging.info("[%s] Round 4 完成 (整段 F9/F10 流程完成)", label)
    return True


def script_F9_adaptive():
    """F9 (解析度無關)：腫瘤手術同意書全自動流程 R1-R4。
    流程：開同意書 → 手術及治療 → MO04 → 開立電子 → popup 清空+局麻
         → 兩片語自動選 (皮膚腫瘤 / 治療) → popup 開立電子 → 警告對話框「是」
    全程不主動推到背景；ForegroundProtector 只在使用者切到非醫院視窗時
    才保護其 focus（讓使用者可選擇看或不看）。"""
    logging.info("--- Executing F9 (adaptive R1-R4) ---")
    # F9 row index: 所患疾病=3 (皮膚腫瘤), 手術原因=0 (治療)
    ok = _run_with_foreground_protector(
        script_F9_F10_consent_form_adaptive,
        "MO04", phrase_row_所患=3, phrase_row_手術=0, label="F9")
    logging.info("F9 (adaptive): %s", "R1-R4 done" if ok else "中斷")
    return bool(ok)


def script_F10_adaptive():
    """F10 (解析度無關)：切片同意書全自動流程 R1-R4。
    流程：開同意書 → 手術及治療 → MU02 → 開立電子 → popup 清空+局麻
         → 兩片語自動選 (皮膚疾患 / 確診) → popup 開立電子 → 警告對話框「是」
    背景保護同 F9。"""
    logging.info("--- Executing F10 (adaptive R1-R4) ---")
    # F10 row index: 所患疾病=0 (皮膚疾患), 手術原因=1 (確診)
    ok = _run_with_foreground_protector(
        script_F9_F10_consent_form_adaptive,
        "MU02", phrase_row_所患=0, phrase_row_手術=1, label="F10")
    logging.info("F10 (adaptive): %s", "R1-R4 done" if ok else "中斷")
    return bool(ok)


def _get_ime_focus_hwnd():
    """取得前景應用程式真正有焦點的控制項 handle（需 AttachThreadInput）。"""
    try:
        hwnd_fg  = ctypes.windll.user32.GetForegroundWindow()
        fore_tid = ctypes.windll.user32.GetWindowThreadProcessId(hwnd_fg, None)
        cur_tid  = ctypes.windll.kernel32.GetCurrentThreadId()
        ctypes.windll.user32.AttachThreadInput(cur_tid, fore_tid, True)
        hwnd_focus = ctypes.windll.user32.GetFocus()
        ctypes.windll.user32.AttachThreadInput(cur_tid, fore_tid, False)
        return hwnd_focus if hwnd_focus else hwnd_fg
    except Exception:
        return ctypes.windll.user32.GetForegroundWindow()

def _ime_close() -> bool:
    """關閉前景視窗的中文輸入法，回傳原本是否開啟（供還原用）。"""
    try:
        hwnd = _get_ime_focus_hwnd()
        himc = ctypes.windll.imm32.ImmGetContext(hwnd)
        was_open = bool(ctypes.windll.imm32.ImmGetOpenStatus(himc))
        if was_open:
            ctypes.windll.imm32.ImmSetOpenStatus(himc, 0)
            ctypes.windll.imm32.ImmSetConversionStatus(himc, 0, 0)
        ctypes.windll.imm32.ImmReleaseContext(hwnd, himc)
        return was_open
    except Exception:
        return False

def _ime_restore(was_open: bool) -> None:
    """還原輸入法狀態（_ime_close 的配對呼叫）。"""
    if not was_open:
        return
    try:
        hwnd = _get_ime_focus_hwnd()
        himc = ctypes.windll.imm32.ImmGetContext(hwnd)
        ctypes.windll.imm32.ImmSetOpenStatus(himc, 1)
        ctypes.windll.imm32.ImmReleaseContext(hwnd, himc)
    except Exception:
        pass


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


# _appt_dict_ext_branch / _calendar_branch_sort_rank: 抽到 cmuh_common.appt_utils
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


# _strip_ext_appointments: 抽到 cmuh_common.appt_utils


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
    # [O36] Circuit breaker：本 session 連續失敗已達閾值 → 完全跳過
    if _circuit_is_tripped("east"):
        return None
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip east fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return None
    session = _get_thread_local_reg52_external_session()
    last_error = None
    for docname_q in variants:
        url = f"{EAST_DISTRICT_REG52_URL}?DocNo={dparam}&Docname={docname_q}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            r = session.get(url, timeout=REG52_BRANCH_TIMEOUT, verify=True)
            r.raise_for_status()
            r.encoding = "big5"
            text = r.text
            if len(text) < 500:
                continue
            probe = BeautifulSoup(text, "lxml")
            if probe.select_one("div.visitDate") or probe.select_one("table#dayoff"):
                logging.info(f"已自東區主機取得掛號表: {doctor_name} ({dparam})")
                _source_backoff_success(source_key)
                _circuit_record_success("east")
                return text
        except requests.exceptions.RequestException as e:
            logging.debug(f"東區 reg52 請求失敗 ({url[:64]}…): {e}")
            last_error = e
            continue
    if last_error:
        delay, cnt = _source_backoff_fail(
            source_key,
            REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
            REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
        )
        logging.warning(f"[BACKOFF] east fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
        # [O36] 紀錄 session 級失敗
        if _circuit_record_fail("east"):
            logging.warning("[O36] 東區主機連續失敗 %d 次，本 session 不再嘗試（重啟程式才會重試）",
                            _CIRCUIT_BREAKER_THRESHOLD)
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
    session = _get_thread_local_reg52_external_session()
    last_error = None
    for docname_q in variants:
        url = f"{HUIHE_REG52_URL}?DocNo={dparam}&Docname={docname_q}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            r = session.get(url, timeout=REG52_BRANCH_TIMEOUT, verify=not _is_internal(url))
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
            last_error = e
            continue
    if last_error:
        delay, cnt = _source_backoff_fail(
            source_key,
            REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
            REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
        )
        logging.warning(f"[BACKOFF] huihe fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
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
    if _circuit_is_tripped("huisheng"):  # [O36]
        return None
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip huisheng fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return None
    session = _get_thread_local_reg52_external_session()
    last_error = None
    for docname_q in variants:
        url = f"{HUISHENG_REG52_URL}?DocNo={dparam}&Docname={docname_q}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            r = session.get(url, timeout=REG52_BRANCH_TIMEOUT, verify=True)
            r.raise_for_status()
            r.encoding = "big5"
            text = r.text
            if len(text) < 500:
                continue
            probe = BeautifulSoup(text, "lxml")
            if probe.select_one("div.visitDate") or probe.select_one("table#dayoff"):
                logging.info(f"已自惠盛 hs1 取得掛號表: {doctor_name} ({dparam})")
                _source_backoff_success(source_key)
                _circuit_record_success("huisheng")
                return text
        except requests.exceptions.RequestException as e:
            logging.debug(f"惠盛 reg52 請求失敗 ({url[:64]}…): {e}")
            last_error = e
            continue
    if last_error:
        delay, cnt = _source_backoff_fail(
            source_key,
            REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
            REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
        )
        logging.warning(f"[BACKOFF] huisheng fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
        if _circuit_record_fail("huisheng"):  # [O36]
            logging.warning("[O36] 惠盛主機連續失敗 %d 次，本 session 不再嘗試",
                            _CIRCUIT_BREAKER_THRESHOLD)
    logging.warning(f"無法自惠盛取得掛號表: {doctor_name} ({dparam})")
    return None


# _normalize_dayoff_session: 抽到 cmuh_common.appt_utils（13 行刪除）
# _merge_appointments_by_date: 抽到 cmuh_common.appt_utils（26 行刪除）
# _merge_dayoff_overrides: 抽到 cmuh_common.appt_utils（21 行刪除）
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

                # [review C2 2026-06-12] 與 _parse_doctor_info_dayoff 同款防護：
                # 單格日期解析失敗只跳過該格，不可讓整個醫師的班表解析中斷。
                try:
                    date_key = _safe_parse_roc_date(roc_date_str)
                except ValueError:
                    logging.debug("班表略過無法解析日期之格: %r", roc_date_str)
                    continue
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

        # [stability] 單列日期解析失敗只跳過該列，不要讓整個醫師的休診表解析中斷
        # (某列 cells[0] 可能是子標題/合併格/格式異動 → _safe_parse_roc_date raise)。
        try:
            date_key = _safe_parse_roc_date(roc_date_str)
        except ValueError:
            logging.debug("停診表略過無法解析日期之列: %r", roc_date_str)
            continue
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

                # [review C2 2026-06-12] 單格日期解析失敗只跳過該格(同 dayoff 解析防護)
                try:
                    date_key = _safe_parse_roc_date(roc_date_str)
                except ValueError:
                    logging.debug("分院週表略過無法解析日期之格: %r", roc_date_str)
                    continue
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

                # [review C2 2026-06-12] 單格日期解析失敗只跳過該格(同 dayoff 解析防護)
                try:
                    date_key = _safe_parse_roc_date(roc_date_str)
                except ValueError:
                    logging.debug("東區週表略過無法解析日期之格: %r", roc_date_str)
                    continue
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
    hit = _cache_get(cache_key, REG52_AUH_TTL_SECONDS, evict_expired=False)
    if hit is not None:
        return hit
    source_key = f"auh:{doc_no}"
    if _circuit_is_tripped("auh"):  # [O36]
        return _cache_get(cache_key, REG52_STALE_CACHE_SECONDS, evict_expired=False) or ""
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip auh fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return ""
    try:
        session = _get_thread_local_reg52_external_session()
        r = session.get(url, timeout=REG52_AUH_TIMEOUT, verify=True)
        r.raise_for_status()
        r.encoding = "big5"
        text = r.text
        if "已掛號" in text or "visitDate" in text:
            logging.info(f"已自亞大附醫取得掛號表: {doctor_name} ({doc_no})")
        else:
            logging.warning(f"亞大附醫頁面未含掛號數欄位: {doctor_name} ({doc_no})")
        _cache_set(cache_key, text)
        _source_backoff_success(source_key)
        _circuit_record_success("auh")
        return text
    except requests.exceptions.RequestException as e:
        logging.warning(f"亞大附醫資料抓取失敗 ({doctor_name} {doc_no}): {e}")
        delay, cnt = _source_backoff_fail(
            source_key,
            REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
            REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
        )
        logging.warning(f"[BACKOFF] auh fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
        if _circuit_record_fail("auh"):  # [O36]
            logging.warning("[O36] AUH 連續失敗 %d 次，本 session 不再嘗試（重啟才會重試）",
                            _CIRCUIT_BREAKER_THRESHOLD)
        return _cache_get(cache_key, REG52_STALE_CACHE_SECONDS, evict_expired=False) or ""

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

        pairs = _RE_REG52_DATE_CNT_PAIRS.findall(txt_norm)  # [O16] precompiled
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


class Reg52BackoffActive(Exception):
    pass


def check_appointment_count(ui_queue: "Queue[UiMessage]", doctor_config: DoctorConfig):
    session = _get_thread_local_reg52_session()
    doctor_name = doctor_config["name"]
    doc_no = str(doctor_config["doc_no"])
    target_url = f"https://appointment.cmuh.org.tw/cgi-bin/reg52.cgi?DocNo={doc_no}"
    last_exception = None
    cached_appointments = doctor_config.get("_cached_appointments")
    cached_count = _appointments_data_count(cached_appointments)
    is_manual_refresh = bool(doctor_config.get("_is_manual_refresh", False))

    def _emit_cached_appointments(reason):
        if cached_count <= 0:
            return False
        logging.warning(
            f"[CACHE_FALLBACK] {doctor_name} ({doc_no}) 使用已載入門診人數快取，原因: {reason}; slots={cached_count}"
        )
        put_ui_message(ui_queue, UiRefreshTickMessage(doctor_name=doctor_name))
        put_ui_message(ui_queue, UiClinicDataMessage(doctor_name=doc_no, data=deepcopy(cached_appointments)))
        return True

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

            html_main = _cache_get(cache_main_key, REG52_MAIN_TTL_SECONDS, evict_expired=False)
            html_dayoff = _cache_get(dayoff_cache_key, REG52_DAYOFF_TTL_SECONDS, evict_expired=False)
            need_main = html_main is None
            need_dayoff = html_dayoff is None
            if need_main and need_dayoff and cached_count > 0:
                # 啟動時已有門診人數快取可保底；先把主表抓回來更新人數，
                # 休診表留給後續主表已快取的輪次，避免 dayoff 逾時拖住整批刷新。
                need_dayoff = False
                _source_throttle_allow(
                    f"dayoff-bg:{doc_no}",
                    REG52_DAYOFF_BACKGROUND_MIN_INTERVAL_SECONDS,
                )
                source_timing["dayoff_fetch_ms"] = 0
                source_timing["backoff_skip"] += 1
            elif need_dayoff and cached_count > 0 and not is_manual_refresh:
                ok_dayoff_bg, remain_dayoff_bg = _source_throttle_allow(
                    f"dayoff-bg:{doc_no}",
                    REG52_DAYOFF_BACKGROUND_MIN_INTERVAL_SECONDS,
                )
                if not ok_dayoff_bg:
                    logging.info(
                        f"[THROTTLE] skip dayoff fetch {doctor_name} {doc_no}, remaining={remain_dayoff_bg:.1f}s"
                    )
                    need_dayoff = False
                    source_timing["dayoff_fetch_ms"] = 0
                    source_timing["backoff_skip"] += 1

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
                        stale = _cache_get(cache_main_key, REG52_STALE_CACHE_SECONDS, evict_expired=False)
                        if stale is not None:
                            source_timing["backoff_skip"] += 1
                            return stale, 0
                        raise Reg52BackoffActive(f"main source backoff active ({remain_main:.1f}s)")
                    try:
                        with _reg52_cmuh_fetch_sema:
                            with _session_http_guard(sess):
                                response = sess.get(target_url, timeout=REG52_MAIN_TIMEOUT, verify=verify_main)
                                response.raise_for_status()
                                response.encoding = 'big5'
                                hm = response.text
                        _source_backoff_success(sk_main)
                    except requests.exceptions.RequestException:
                        delay, cnt = _source_backoff_fail(
                            sk_main,
                            REG52_MAIN_BACKOFF_BASE_SECONDS,
                            REG52_MAIN_BACKOFF_MAX_SECONDS,
                        )
                        logging.warning(f"[BACKOFF] main fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
                        stale = _cache_get(cache_main_key, REG52_STALE_CACHE_SECONDS, evict_expired=False)
                        if stale is not None:
                            source_timing["backoff_skip"] += 1
                            return stale, int((time.perf_counter() - t0) * 1000)
                        raise
                    _cache_set(cache_main_key, hm)
                    return hm, int((time.perf_counter() - t0) * 1000)

                def _parallel_fetch_dayoff():
                    t0 = time.perf_counter()
                    sess = _get_thread_local_reg52_session()
                    sk_dayoff = f"dayoff:{doc_no}"
                    ok_dayoff, _ = _source_backoff_allow(sk_dayoff)
                    if not ok_dayoff:
                        stale = _cache_get(dayoff_cache_key, REG52_STALE_CACHE_SECONDS, evict_expired=False)
                        return (stale or ""), 0, True
                    try:
                        with _reg52_cmuh_fetch_sema:
                            with _session_http_guard(sess):
                                dayoff_response = sess.get(dayoff_url, timeout=REG52_DAYOFF_TIMEOUT, verify=verify_dayoff)
                                dayoff_response.raise_for_status()
                                dayoff_response.encoding = "big5"
                                hd = dayoff_response.text
                        _cache_set(dayoff_cache_key, hd)
                        _source_backoff_success(sk_dayoff)
                        return hd, int((time.perf_counter() - t0) * 1000), False
                    except requests.exceptions.RequestException as e:
                        logging.warning(f"休診表 reg52 抓取失敗 ({doctor_name} {doc_no}): {e}")
                        delay, cnt = _source_backoff_fail(
                            f"dayoff:{doc_no}",
                            REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
                            REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
                        )
                        logging.warning(f"[BACKOFF] dayoff fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
                        stale = _cache_get(dayoff_cache_key, REG52_STALE_CACHE_SECONDS, evict_expired=False)
                        return (stale or ""), int((time.perf_counter() - t0) * 1000), False

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
                    stale = _cache_get(cache_main_key, REG52_STALE_CACHE_SECONDS, evict_expired=False)
                    if stale is not None:
                        html_main = stale
                        source_timing["main_fetch_ms"] = 0
                        source_timing["backoff_skip"] += 1
                        need_main = False
                    else:
                        raise Reg52BackoffActive(f"main source backoff active ({remain_main:.1f}s)")
                if need_main:
                    try:
                        with _reg52_cmuh_fetch_sema:
                            with _session_http_guard(session):
                                response = session.get(target_url, timeout=REG52_MAIN_TIMEOUT, verify=verify_main)
                                response.raise_for_status()
                                response.encoding = 'big5'
                                html_main = response.text
                        _source_backoff_success(sk_main)
                    except requests.exceptions.RequestException:
                        delay, cnt = _source_backoff_fail(
                            sk_main,
                            REG52_MAIN_BACKOFF_BASE_SECONDS,
                            REG52_MAIN_BACKOFF_MAX_SECONDS,
                        )
                        logging.warning(f"[BACKOFF] main fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
                        stale = _cache_get(cache_main_key, REG52_STALE_CACHE_SECONDS, evict_expired=False)
                        if stale is not None:
                            html_main = stale
                            source_timing["backoff_skip"] += 1
                        else:
                            raise
                    else:
                        _cache_set(cache_main_key, html_main)
                    source_timing["main_fetch_ms"] = int((time.perf_counter() - t0) * 1000)

            elif need_dayoff:
                t_dayoff = time.perf_counter()
                with _session_http_guard(session):
                    try:
                        sk_dayoff = f"dayoff:{doc_no}"
                        ok_dayoff, _ = _source_backoff_allow(sk_dayoff)
                        if ok_dayoff:
                            with _reg52_cmuh_fetch_sema:
                                dayoff_response = session.get(dayoff_url, timeout=REG52_DAYOFF_TIMEOUT, verify=verify_dayoff)
                                dayoff_response.raise_for_status()
                                dayoff_response.encoding = "big5"
                                html_dayoff = dayoff_response.text
                            _cache_set(dayoff_cache_key, html_dayoff)
                            _source_backoff_success(sk_dayoff)
                        else:
                            stale = _cache_get(dayoff_cache_key, REG52_STALE_CACHE_SECONDS, evict_expired=False)
                            if stale is not None:
                                html_dayoff = stale
                            source_timing["backoff_skip"] += 1
                    except requests.exceptions.RequestException as e:
                        logging.warning(f"休診表 reg52 抓取失敗 ({doctor_name} {doc_no}): {e}")
                        delay, cnt = _source_backoff_fail(
                            f"dayoff:{doc_no}",
                            REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
                            REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
                        )
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

            external_jobs = []

            def _queue_external_html(label, cache_key, ttl_seconds, fetcher, source_key=None):
                cached = _cache_get(cache_key, ttl_seconds, evict_expired=False)
                timing_key = f"{label}_fetch_ms"
                if cached is not None:
                    source_timing[timing_key] = 0
                    source_timing["cache_hit_html"] += 1
                    return cached
                if source_key:
                    ok_external, remain_external = _source_backoff_allow(source_key)
                    if not ok_external:
                        logging.info(f"[BACKOFF] skip {label} fetch {doctor_name} {doc_no}, remaining={remain_external:.1f}s")
                        source_timing[timing_key] = 0
                        source_timing["backoff_skip"] += 1
                        stale = _cache_get(cache_key, REG52_STALE_CACHE_SECONDS, evict_expired=False)
                        return stale or ""
                stale = _cache_get(cache_key, REG52_STALE_CACHE_SECONDS, evict_expired=False)
                external_jobs.append((label, cache_key, fetcher, stale))
                return ""

            if _should_fetch_east_district_reg52(html_main, doctor_name):
                html_east = _queue_external_html(
                    "east",
                    ("east_html", doc_no),
                    REG52_BRANCH_TTL_SECONDS,
                    lambda: _fetch_east_district_reg52_html(session, doc_no, doctor_name),
                    f"east:{doc_no}",
                )

            if _should_fetch_huihe_reg52(doctor_name):
                html_huihe = _queue_external_html(
                    "huihe",
                    ("huihe_html", doc_no),
                    REG52_BRANCH_TTL_SECONDS,
                    lambda: _fetch_huihe_reg52_html(session, doc_no, doctor_name),
                    f"huihe:{doc_no}",
                )

            if _should_fetch_huisheng_reg52(doctor_name):
                html_huisheng = _queue_external_html(
                    "huisheng",
                    ("huisheng_html", doc_no),
                    REG52_BRANCH_TTL_SECONDS,
                    lambda: _fetch_huisheng_reg52_html(session, doc_no, doctor_name),
                    f"huisheng:{doc_no}",
                )

            if doctor_name in AUH_DOCTOR_DOCNO_MAP:
                html_auh = _queue_external_html(
                    "auh",
                    ("auh_html", doctor_name, AUH_DOCTOR_DOCNO_MAP.get(doctor_name)),
                    REG52_AUH_TTL_SECONDS,
                    lambda: _fetch_auh_reg52_html(session, doctor_name),
                    f"auh:{AUH_DOCTOR_DOCNO_MAP.get(doctor_name)}",
                )

            def _run_external_job(label, cache_key, fetcher, stale_html=""):
                t0 = time.perf_counter()
                html = fetcher() or ""
                if html:
                    _cache_set(cache_key, html)
                elif stale_html:
                    html = stale_html
                return label, html, int((time.perf_counter() - t0) * 1000)

            if external_jobs:
                if len(external_jobs) == 1:
                    completed_external_jobs = [_run_external_job(*external_jobs[0])]
                else:
                    completed_external_jobs = []
                    max_workers = min(REG52_EXTERNAL_MAX_WORKERS, len(external_jobs))
                    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="r52ext") as pool:
                        futures = [
                            pool.submit(_run_external_job, label, cache_key, fetcher, stale_html)
                            for label, cache_key, fetcher, stale_html in external_jobs
                        ]
                        for fut in as_completed(futures):
                            completed_external_jobs.append(fut.result())
                for label, html, elapsed_ms in completed_external_jobs:
                    source_timing[f"{label}_fetch_ms"] = elapsed_ms
                    if label == "east":
                        html_east = html
                    elif label == "huihe":
                        html_huihe = html
                    elif label == "huisheng":
                        html_huisheng = html
                    elif label == "auh":
                        html_auh = html

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
                # [MG-01] 這是「先送一版讓 UI 有東西」的預備送出,但緊接著下面 _merge_dayoff_overrides
                # 會【原地】改寫同一個 appointments_by_date;若原樣把活 dict 交給 UI,UI 緒存進
                # all_doctors_data、主緒 _update_grid_data 無鎖迭代時 worker 正在原地合併 → 偶發
                # dict-changed-size 月曆重繪炸掉/畫出合併到一半的休診。與 6719 對齊:交出 deepcopy 快照。
                put_ui_message(ui_queue, UiClinicDataMessage(doctor_name=doc_no, data=deepcopy(appointments_by_date)))

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

        except Reg52BackoffActive as e:
            last_exception = e
            if _emit_cached_appointments(str(e)):
                return
            logging.warning(f"Attempt {attempt + 1} for {doctor_name} skipped: {e}.")
            break

        except (requests.exceptions.RequestException, ValueError) as e:
            last_exception = e
            if _emit_cached_appointments(type(e).__name__):
                return
            wait_time = (attempt + 1) * 2
            logging.warning(f"Attempt {attempt + 1} for {doctor_name} failed: {e}. Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

    error_type = type(last_exception).__name__ if last_exception else "Unknown Error"
    if _emit_cached_appointments(f"all attempts failed ({error_type})"):
        return
    logging.error(f"All 3 attempts to check for {doctor_name} failed.")
    put_ui_message(ui_queue, UiRefreshTickMessage(doctor_name=doctor_name))
    put_ui_message(
        ui_queue,
        UiClinicDataMessage(doctor_name=doc_no, data={"error": f"查詢失敗 ({error_type})"}),
    )

def load_master_schedule_in_background(ui_queue: "Queue[UiMessage]", *, force: bool = False):
    logging.info("Loading master schedule in background...")
    refresh_master_schedule_if_needed(
        ui_queue,
        create_master_schedule_from_web,
        get_conf_path('cache_master_schedule.json'),
        force=force,
    )

# --- 8. 值班醫師查詢 ---
# 【效能 2026.05.20】duty timeout 從 40 降為 (connect=3, read=8)。內網應該快，
# 慢就是壞掉 — 等 40 秒只會把首屏拖垮。
_DUTY_HTTP_TIMEOUT = (3, 8)

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

def fetch_duty_doctor(ui_queue: "Queue[UiMessage]", session: "_RequestsSession", r_doctor_map: dict[str, Any]):
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
    return doctor_name not in {"查詢失敗", "網路錯誤", "查詢錯誤"}

def fetch_saturday_duty_doctor(ui_queue: "Queue[UiMessage]", session: "_RequestsSession", r_doctor_map: dict[str, Any]):
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
    return doctor_name not in {"查詢失敗", "網路錯誤", "查詢錯誤"}

def fetch_duty_vs(ui_queue: "Queue[UiMessage]", session: "_RequestsSession", vs_type: str):
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
    return doctor_name not in {"查詢失敗", "網路錯誤", "查詢錯誤"}

# --- 門診動態 reg64.cgi TimeCode（與 appointment.cmuh.org.tw/cgi-bin/reg64.cgi 參數一致）---
# 【重構 2026-05-21】reg64 / clinic display mode 函式 + CLINIC_DISPLAY_MODE_OPTIONS
# 抽到 cmuh_common.reg64_utils（與 scheduler.py 共用）
from cmuh_common.reg64_utils import (  # noqa: E402
    canonical_clinic_session_str as _canonical_clinic_session_str,
    clinic_int_count as _clinic_int_count,
    prev_session_cn as _prev_session_cn,
    reg64_clinic_quiet_hours as _reg64_clinic_quiet_hours,
    reg64_next_allowed_fetch_time as _reg64_next_allowed_fetch_time,
    reg64_time_code_from_local_clock,
    overrun_effective_time_code,
    is_residual_stale_closed,
    reg64_slot_cn,
    reg64_slot_label_color,
    session_boundary_datetime as _session_boundary_datetime,
    CLINIC_DISPLAY_MODE_OPTIONS,
    _normalize_clinic_display_mode,
    _clinic_display_mode_label,
    _clinic_display_mode_from_label,
    resolve_clinic_reg64_time_code,
)


# 門診動態燈號／候診輪詢間隔（秒）；07:00-00:00 一律 45-75 秒隨機，00:00-07:00 完全靜默
CLINIC_LIGHT_REFRESH_SECONDS = 60
# reg64 單次 HTTP 逾時（秒）；院方尖峰易逾時，略增並搭配共用退避與序向請求。
CLINIC_REG64_HTTP_TIMEOUT = 10
# 同一主機 reg64 共用退避計數，避免 181／182 兩 URL 各自累加 fail 至 90s 仍同時狂打
REG64_CMUH_BACKOFF_KEY = "reg64:appointment.cmuh.org.tw"
# 門診動態：近一月統計天數、關診判定（時段底線後連續無變動秒數）
CLINIC_METRIC_HISTORY_DAYS = 30
CLINIC_LIGHT_HISTORY_DAYS = 30
# 浮動視窗:連續「錯誤/逾時」達此次數,才把該診間【候選】視為「今天沒這個診」並隱藏。
# 給暫時性連線異常(冷啟動、換節、院方瞬斷)幾輪緩衝,避免把其實有診的診間從 pending 誤藏。
# 候選還要再過「有別的診間連得上(網路正常)」這關(_floating_network_seems_up 看
# _reg64_room_reachable);冷啟動 + 全網斷線時沒有任何診間可達 → 不隱藏(連線錯誤本身不是「沒診」
# 的證據,須有別的診間可達佐證網路正常)。
FLOATING_ERROR_HIDE_STREAK = 3
CLINIC_LIGHT_HISTORY_WINDOW_MINUTES = 9
CLINIC_DYNAMIC_STATE_FILENAME = "clinic_dynamic_state.json"
# 已寄出的止掛提醒信記錄(跨重啟去重用)。notify_key 內含日期。
# [MN-04] 總覽涵蓋今天+13 天且止掛不限今天:對遠期診次(最多 13 天後)寄信後,若在該診次
# 到來前重啟,保留期以「寄出日」過濾——只保留近 7 天會把仍未過期的遠期診次記錄剪掉 →
# frequency 歸零 → 同診次重寄。保留期需 > 總覽 13 天視窗;取 21 天(留 8 天安全邊際)。
ALERT_EMAIL_SENT_FILENAME = "alert_email_sent.json"
ALERT_EMAIL_SENT_RETAIN_DAYS = 21


# ── 個別醫師止掛「優先刷新」分級（2026-07-13 使用者需求）───────────────────────
# 越接近門檻刷越密（只刷【該醫師】、不連動其他醫師）：
#   門檻-10(near)→ 30 分、門檻-5(mid)→ 15 分、門檻-3(critical)→ 10 分。
# tier 名稱 →(margin, 基準間隔秒)。判級時由最接近門檻往外試（critical→mid→near）。
PRIORITY_REFRESH_TIERS = (
    ("critical", 3, 10 * 60),
    ("mid", 5, 15 * 60),
    ("near", 10, 30 * 60),
)
PRIORITY_REFRESH_TIER_BASE = {name: base for name, _m, base in PRIORITY_REFRESH_TIERS}
# 反 bot 抖動幅度：實際間隔為基準的 ±(10%~20%)（幅度恆落在 10-20%、不會退化成固定
# 節拍被判為 bot、也不超過 20% 讓資料太舊）。
PRIORITY_REFRESH_JITTER_MIN = 0.10
PRIORITY_REFRESH_JITTER_MAX = 0.20
# 優先刷新檢查【喚醒間隔】（秒）。排程每 PRIORITY_REFRESH_CHECK_SECONDS 喚醒一次評估
# 「距上次是否已達目標間隔」。30 秒遠細於最短基準 10 分。
PRIORITY_REFRESH_CHECK_SECONDS = 30
# 排程喚醒延遲上限估計（秒）：schedule 於工作完成後才排下一次、master loop 每 5 秒 pump，
# 故實際喚醒可能比「上次觸發 + N×間隔」晚達近一個檢查間隔。抖動候選須【內縮】此邊際 →
# 即使喚醒延遲,實際觸發間隔仍落在 ±[10%,20%]、且絕不進位回基準（codex 第二輪指出：純
# 30s 網格假設完美對齊、未計排程延遲 → -10% 候選可能被延成 -4%）。取 2×檢查間隔保守覆蓋
# 實測 <35s 的最壞延遲；候選 t 之 [t, t+guard] 需完整落在子帶內。
PRIORITY_REFRESH_DRIFT_GUARD = 2 * PRIORITY_REFRESH_CHECK_SECONDS   # 60s


def _priority_refresh_jitter_choices(
        base_seconds: int,
        step: int = PRIORITY_REFRESH_CHECK_SECONDS,
        guard: int = PRIORITY_REFRESH_DRIFT_GUARD) -> tuple:
    """回傳基準的 ±(10%~20%) 抖動候選（秒），對齊 step 網格、且【排除基準值本身】。

    每個候選 t 皆滿足 [t, t+guard] 完整落在負向子帶 [-20%,-10%] 或正向子帶 [+10%,+20%]
    → 即使排程喚醒比目標晚（延遲 <guard），實際觸發間隔 = t + 延遲 仍落在 ±[10%,20%] 帶內
    且永不等於基準（反 bot、且忠實 10-20% 抖動）。候選 t 的 [t,t+guard] 相鄰重疊 → 加上
    連續的喚醒延遲,實際觸發時點連續覆蓋整個子帶。純函式以便測試。"""
    lo = base_seconds - base_seconds // 5           # -20%
    lo_edge = base_seconds - base_seconds // 10     # -10%
    hi_edge = base_seconds + base_seconds // 10     # +10%
    hi = base_seconds + base_seconds // 5           # +20%
    out = []
    v = (lo // step) * step
    if v < lo:
        v += step
    while v <= hi:
        # [t, t+guard] 需完整落在某一子帶（內縮 guard 抵銷喚醒延遲）
        if (lo <= v and v + guard <= lo_edge) or (hi_edge <= v and v + guard <= hi):
            out.append(v)
        v += step
    return tuple(out)


PRIORITY_REFRESH_JITTER_CHOICES = {
    name: _priority_refresh_jitter_choices(base)
    for name, base in PRIORITY_REFRESH_TIER_BASE.items()
}


def _priority_refresh_interval_seconds(base_seconds: int) -> int:
    """從該基準的 ±(10%~20%) 抖動候選中隨機取一（反 bot、避免固定節拍）。純函式以便測試。"""
    choices = _priority_refresh_jitter_choices(base_seconds)
    return random.choice(choices) if choices else base_seconds


def _filter_recent_alert_sent(data, cutoff: str) -> dict:
    """保留 value(ISO 日期字串)>= cutoff 的項目;非 dict / 非字串鍵值一律剔除。
    ISO 日期零補位 → 可直接字典序比較。純函式以便測試。"""
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v >= cutoff}


def _clinic_refresh_seconds(hour: int) -> int:
    """[MN-03] 診間燈號輪詢間隔(秒)。半夜監測開啟時,00-07 點放慢到 180-300 秒
    (多機夜間負載禮貌);其餘時段維持 45-75 秒隨機。純函式以便測試。"""
    if hour < 7:
        return random.randint(180, 300)
    return random.randint(45, 75)


def _reg64_micro_ttl_seconds(hour: int) -> int:
    """[MN-03] reg64 micro-cache TTL(秒)。夜間放寬到 170 秒配合放慢的輪詢,
    其餘時段 50 秒。純函式以便測試。"""
    return 170 if hour < 7 else 50
NOTIFY_DO_NOT_DISTURB_START_HOUR = 0
NOTIFY_DO_NOT_DISTURB_END_HOUR = 8

# [O10] 拉長主院 cache TTL 120s → 300s（5 分鐘）；分院 180 → 600s（10 分鐘）
# 院方主機自身慢（~3-7s），cache 命中時 UI 立即顯示，不必每 2 分鐘抓一次
REG52_MAIN_TTL_SECONDS = 300
REG52_BRANCH_TTL_SECONDS = 600
REG52_AUH_TTL_SECONDS = 600
REG52_DAYOFF_TTL_SECONDS = 600
REG52_MAIN_TIMEOUT = (5, 10)
REG52_DAYOFF_TIMEOUT = (3, 5)
# [O2] 院外連線 timeout 從 (4,8) 縮為 (2,5)：AUH/惠盛/東區若不通，2 秒就失敗，避免拖慢首批
REG52_BRANCH_TIMEOUT = (2, 5)
REG52_AUH_TIMEOUT = (2, 5)
REG52_EXTERNAL_MAX_WORKERS = 2
REG52_STALE_CACHE_SECONDS = 15 * 60
REG52_DAYOFF_BACKGROUND_MIN_INTERVAL_SECONDS = 30 * 60
REG52_EXTERNAL_BACKGROUND_MIN_INTERVAL_SECONDS = 20 * 60
PARSE_CACHE_TTL_SECONDS = 180
DUTY_CACHE_TTL_SECONDS = 3600
REG64_MICRO_CACHE_SECONDS = 8
REG64_STALE_CACHE_SECONDS = 5 * 60
SOURCE_BACKOFF_BASE_SECONDS = 2
SOURCE_BACKOFF_MAX_SECONDS = 90
REG52_MAIN_BACKOFF_BASE_SECONDS = 30
REG52_MAIN_BACKOFF_MAX_SECONDS = 5 * 60
# [O2] 院外失敗 backoff 從 60s 拉長到 300s（5 分鐘）；上限 15 分鐘 → 30 分鐘
# 院外（AUH/東區/惠盛）若不通通常 5 分鐘內也不會恢復，過短重試只是浪費時間
REG52_EXTERNAL_BACKOFF_BASE_SECONDS = 300
REG52_EXTERNAL_BACKOFF_MAX_SECONDS = 30 * 60
REG64_BACKOFF_BASE_SECONDS = 60
REG64_BACKOFF_MAX_SECONDS = 5 * 60
GLOBAL_REFRESH_SNAPSHOT_TTL_SECONDS = 180
CLINIC_CLOSE_PLATEAU_SECONDS = 30 * 60
# [2026-06-26] 拖班診(最後一次看診進展 last_activity_ts 發生在「過關診時間之後」)久久沒變更,可能是
# 醫師在看一個很久的病人/中間有空檔,而非真的關診。對這種「確認拖班」的診把 plateau 門檻拉長為 60 分,
# 避免把還在看的診誤判關診、從浮動視窗消失(實機:早診拖到下午 14:42 還在看卻不見了)。在關診時間前就停
# (last_activity_ts < boundary)的診維持 30 分正常偵測,不受影響。
# [Codex] 用 last_activity_ts(絕對時戳)判定 → 不必處理「過關診 > N 小時」太晚套門檻、或「剛好在 boundary
# 前後之間才進展」的漏判;且前一節/前一天的活動時戳必早於本節 boundary,跨日/跨節不會誤判。
CLINIC_CLOSE_PLATEAU_SECONDS_OVERRUN = 60 * 60  # 確認拖班(最後進展在關診時間後)的 plateau 門檻 = 60 分


from cmuh_common.reg64_utils import _reg64_tc_to_session_cn  # noqa: E402


def _hotkey_builtin_map_for_profile(profile: str) -> dict:
    """profile → 熱鍵鍵名 → 內建函式（覆寫載入失敗時回退）。

    新熱鍵配置 (2026-05-18 重排, F11 adaptive 2026-05-19)：
      F1=照光1, F2=照光2, F3=照光3, F4=冷凍, F5=KOH(13017),
      F9=腫瘤同意書, F10=切片同意書, F11=快速完成 (adaptive),
      F12=中止 (special key)
    F1-F5 + F9 + F10 + F11 全部 adaptive (Win32, 跨解析度)。"""
    common_adaptive = {
        "F1": script_F1_adaptive,
        "F2": script_F2_adaptive,
        "F3": script_F3_adaptive,
        "F4": script_F4_adaptive,
        "F5": script_F5_adaptive,
        "F9": script_F9_adaptive,
        "F10": script_F10_adaptive,
        "F11": script_F11_adaptive,
    }
    if profile in ("1920x1080", "1280x1024", "1024x768"):
        return dict(common_adaptive)
    return {}


# =============================================================================
# 止掛提醒寄信（Outlook COM，在獨立執行緒+逾時，避免卡到主迴圈）
# =============================================================================
def _send_alert_email_via_smtp(subject: str, body: str,
                                recipients: list, timeout: float = 60.0) -> bool:
    """達到門檻時透過 SMTP (Gmail) 寄信。回傳是否成功（失敗只 log，不影響主程式）。

    為何用 SMTP 不用 Outlook：admin 行程的 Outlook COM 會起一個 admin Outlook
    實例，用 administrator 的 MAPI profile（通常沒設定郵件帳號），mail.Send()
    成功但信永遠卡在隱形 Outbox 寄不出。SMTP 跳過整個 UAC 跟 Outlook profile
    地獄，admin/user 任何權限都能寄。設定見 settings/smtp_credentials.json。"""
    if not recipients:
        return False
    try:
        from cmuh_common.smtp_mail import (
            SmtpNotConfiguredError, send_mail,
        )
    except Exception:
        logging.warning("smtp_mail 模組載入失敗，止掛信跳過", exc_info=True)
        return False
    try:
        send_mail(recipients=recipients, subject=subject, body=body,
                  attachment_path=None, timeout=timeout)
        return True
    except SmtpNotConfiguredError as e:
        logging.warning("止掛提醒寄信跳過（SMTP 尚未設定）：%s", e)
        return False
    except Exception as e:
        logging.warning("止掛提醒 SMTP 寄信失敗：%s", e)
        return False


def _send_alert_email_via_outlook(subject: str, body: str,
                                  recipients: list, timeout: float = 60.0,
                                  sender_account: str = "") -> bool:
    """【已淘汰，保留作為備援】透過 Outlook 寄信。
    主流請用 _send_alert_email_via_smtp（直接走 Gmail SMTP，不會卡 Outbox）。

    sender_account：強制用此 SMTP 地址對應的 Outlook 帳號寄。找不到時退回
    預設帳號，並在 log 留 warning。空字串則直接用 Outlook 預設帳號。

    回傳是否成功（失敗只記 log，不影響主程式運作）。"""
    if not recipients:
        return False
    result: dict = {}

    def _worker() -> None:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            try:
                outlook = win32com.client.GetActiveObject("Outlook.Application")
            except Exception:
                outlook = win32com.client.DispatchEx("Outlook.Application")
            mail = outlook.CreateItem(0)  # olMailItem
            mail.To = "; ".join(recipients)
            mail.Subject = subject
            mail.Body = body
            # 強制寄件人帳號（SendUsingAccount）
            if sender_account:
                target = sender_account.strip().lower()
                picked = None
                try:
                    accounts = outlook.Session.Accounts
                    for i in range(1, accounts.Count + 1):
                        acc = accounts.Item(i)
                        try:
                            smtp = (acc.SmtpAddress or "").strip().lower()
                        except Exception:
                            smtp = ""
                        if smtp == target:
                            picked = acc
                            break
                except Exception:
                    logging.warning("列舉 Outlook accounts 失敗", exc_info=True)
                if picked is not None:
                    try:
                        # SendUsingAccount 在某些 Outlook 版本要走 _oleobj_ Invoke
                        mail._oleobj_.Invoke(*(0xF01C, 0, 8, 0, picked))
                    except Exception:
                        try:
                            mail.SendUsingAccount = picked
                        except Exception:
                            logging.warning("無法套用 SendUsingAccount，將以預設帳號寄",
                                            exc_info=True)
                else:
                    logging.warning(
                        "Outlook 找不到帳號 %r，將以預設帳號寄止掛提醒信",
                        sender_account)
            mail.Send()
            result["ok"] = True
        except Exception as e:  # noqa: BLE001
            result["error"] = e
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    t = threading.Thread(target=_worker, name="AlertMailSender", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logging.warning("止掛提醒寄信逾時（>%ss），放棄", int(timeout))
        return False
    if result.get("error"):
        logging.warning("止掛提醒寄信失敗：%s", result["error"])
        return False
    return bool(result.get("ok"))


# [perf r5] 東區休診推論索引 — 取代月曆重繪時每格×每醫師×每時段重掃整份
# all_doctors_data(最壞 ~396 次/重繪 × 整月掃)的 _doctor_has_other_ext_on_weekday。
# 每次 refresh 只全掃一次建索引，per-cell 查詢降為 O(1)。抽成純函式以便單元測試對拍
# 等價性(見 tests/test_east_clinic_index.py 對 _doctor_has_other_ext_on_weekday 差分測試)。
def _build_east_weekday_index(all_doctors_data, parse_item):
    """建 (lookup_key, weekday, session) -> set(有東區的日期)。parse_item(item) ->
    (session_name, ext_branch)，同時處理 dict 與舊式 str。語意對齊原方法：isinstance(date)
    過濾、僅收 east、session 非空。"""
    index: dict = {}
    for lk, data in all_doctors_data.items():
        if not isinstance(data, dict) or 'error' in data:
            continue
        for dkey, items in data.items():
            if not isinstance(dkey, date):
                continue
            wd = dkey.weekday()
            for item in items:
                sn, ext = parse_item(item)
                if ext == "east" and sn:
                    index.setdefault((lk, wd, sn), set()).add(dkey)
    return index


def _east_index_has_other(index, doc_no, doc_name, weekday_idx, session_name, exclude_date):
    """索引版查詢：是否有「其他(非 exclude_date)同 weekday」出現東區該診別。
    doc_no/doc_name 兩鍵聯集，與 _doctor_has_other_ext_on_weekday 等價。"""
    for lk in (doc_no, doc_name):
        dates = index.get((lk, weekday_idx, session_name))
        if dates and any(d != exclude_date for d in dates):
            return True
    return False


# --- 9. UI 與應用程式主體 ---
class AutomationApp:
    def __init__(self, root: tk.Tk, master_schedule: dict):
        # [perf r5] splash 已顯示 → 此時才載入延後的重量級網路相依(requests/urllib3/bs4)，
        # 填入模組全域供後續抓網函式使用。必須在任何網路呼叫之前(放 __init__ 最前)。
        _ensure_network_imports()
        self.root = root
        self.root.title("中國醫皮膚科常用程式")
        place_tk_window_on_preferred_monitor(self.root)
        _apply_tk_window_icon(self.root)
        
        # [雙螢幕] 解析度偵測一律以「主螢幕」為準(GetSystemMetrics)，而非虛擬桌面。
        # winfo_screenwidth() 在 Windows 雖然也回主螢幕，但用 GetSystemMetrics 可確保
        # 「精準命中 1920×1080 → 不縮放」的行為，不受 Tk 版本差異影響(避免誤觸座標縮放)。
        _prim_w, _prim_h = get_primary_monitor_size()
        self.screen_width = _prim_w or self.root.winfo_screenwidth()
        self.screen_height = _prim_h or self.root.winfo_screenheight()
        self.hotkey_version = None
        if self.screen_width == 1920 and self.screen_height == 1080:
            self.hotkey_version = '1920x1080'
        elif self.screen_width == 1280 and self.screen_height == 1024:
            self.hotkey_version = '1280x1024'
        elif self.screen_width == 1024 and self.screen_height == 768:
            self.hotkey_version = '1024x768'
        self.hotkey_profile = self.hotkey_version or self._select_adaptive_hotkey_profile()

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
        self._exit_cleanup_done = False
        self._ui_queue_poll_id = None
        self._refresh_pending = False
        self._save_cache_pending = {}
        self._save_cache_latest = {}
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
        self._clock_status_worker_running = False
        # [GPT-5.6 P1 2026-07-16] 打卡查詢「世代序號」:180s age 保險允許卡死的舊 worker 尚未
        # 結束就開新一輪,兩者共用 driver。序號只由主緒在開新一輪時 +1;worker 收尾/發布結果
        # 前比對自己的 gen 是否仍是最新 → 晚完成的舊 worker 不清新一輪的旗標、不覆寫新結果。
        self._clock_status_generation = 0
        # [2026-07-15 跨夜] 打卡查詢失敗自動重試（3 分鐘、連續上限 5 次、成功歸零）：
        # 排程一天只有 07:40/08:00/17:03，任一次暫時性失敗原本會灰燈掛到下個排程。
        self._clock_status_retry_count = 0
        self._clock_status_retry_after_id = None
        self._clinic_duplicate_rooms_notice = ()
        self._clinic_lights_worker_running = False
        self._clinic_stat_pending_keys = set()
        self._clinic_stat_pending_lock = threading.Lock()
        self._future_tab_grid_stale = True  # 未來週次分頁需在資料更新後重繪；切回時若未過期可跳過以減少卡頓
        self._bottom_links_hidden = False  # 與 links_frame 顯示狀態同步，避免重複 grid 觸發版面重算
        self._subsystem_running = False
        self._subsystem_lock = threading.Lock()
        self._subsystem_token = 0
        self._subsystem_thread_ident = None   # 目前流程 thread 的 ident(供搶占/取消)
        self._subsystem_thread = None          # 目前流程 thread 物件(供搶占判斷)
        self._subsystem_current_hotkey = None  # 目前流程的熱鍵名稱(供同熱鍵搶占判斷)
        self._last_hotkey_busy_notice_at = 0.0
        # === 熱鍵健康監看狀態 ===
        # heartbeat hook 最近一次看到任何按鍵事件的時間（monotonic）。
        self._hk_last_event_monotonic = time.monotonic()
        self._hk_heartbeat_handle = None        # keyboard.hook() 回傳的移除把手
        self._hk_dead_strikes = 0               # 連續探針未回應次數
        self._hk_auto_restart_count = 0         # 本 session 已自動重啟次數
        self._hk_last_auto_restart_monotonic = 0.0
        self._active_notices = []
        self.startup_phase_text = tk.StringVar(value="啟動中")
        self.app_version_text = tk.StringVar(value=f"v{CURRENT_VERSION}")
        self.last_refresh_text = tk.StringVar(value="更新: --")
        self.hotkey_display_note = tk.StringVar(value="")
        self._log_backlog = []

        # [O15] Queue 加上界，避免極端狀況 OOM；UI 端用 get_nowait 批次拉取
        self.ui_queue = Queue(maxsize=10000)
        self.all_doctors_data = {}
        self.master_schedule = master_schedule
        self._master_schedule_by_weekday = defaultdict(list)
        self._master_schedule_self_paid = {}
        self._rebuild_master_schedule_index()

        # [核心修正]：全域執行緒任務池與互斥鎖，阻絕無限制的 Thread spawning 與字典寫入衝突
        self.bg_executor = BoundedThreadPoolExecutor(
            max_workers=10,
            max_pending=60,
            thread_name_prefix="AppBgTask",
            reject_message="main background task backlog is full",
        )
        # [2026-06-16 韌性] 鎖使用守則(避免 ABBA 死鎖):這些 app 狀態鎖各自保護一份
        # 資料,「原則上一次只持有一把、且只圈住純記憶體操作、不在持鎖時做網路/磁碟
        # /子行程 I/O」。若真的需要巢狀,固定以下取得順序(由外而內):
        #   _subsystem_lock → _refresh_queue_lock → _tracker_lock → _doctor_data_lock
        #   → _clinic_dynamic_state_lock → _history_lock → _alert_state_lock
        # 跨物件鎖(模組級)如 status_driver_pool 的 init_lock→lock 為獨立子系統,不與
        # 上列交叉持有。新增鎖請接在此清單末端並沿用「先外後內」原則。
        self._tracker_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._doctor_data_lock = threading.Lock()
        self._clinic_dynamic_state_lock = threading.Lock()
        self._clinic_dynamic_state_cache = self._load_clinic_dynamic_state_cache()
        # 門診動態 reg64 → 總覽月曆「逾時後補掛號人數」用：(醫師, 上午|下午|晚上)
        self._reg64_public_snapshot = {}
        self._reg64_last_good_total = {}
        # [stability r4] 背景燈號 worker 寫、main thread 月曆繪製讀，加一把專屬輕量鎖
        # (不複用 _tracker_lock 以免無謂耦合)。臨界區只做小 dict 讀寫、不含阻塞 I/O。
        self._reg64_cache_lock = threading.Lock()
        self._reg64_dynamic_ttl_seconds = REG64_MICRO_CACHE_SECONDS
        self._duty_last_fetch_date = None
        self._duty_fetch_worker_running = False
        self._duty_fetch_lock = threading.Lock()
        self._update_check_running = False
        self._update_check_lock = threading.Lock()
        self._last_full_refresh_snapshot = None
        self._last_full_refresh_ts = 0.0
        self._initial_priority_refresh_done = False
        self._background_tasks_started = False
        # 啟動時優先批次完成後再跑全體刷新，避免與固定延遲重疊造成「Refresh already running; queued」
        self._startup_defer_full_until_priority_done = False

        self.threshold_settings = self.load_threshold_settings()
        # [2026-06-29] 載入可選的 UVB 劑量規則覆寫(settings/uvb_rules.json):沒檔→寫出預設模板供編輯,
        # 壞值→逐欄退回程式內預設。best-effort,任何失敗都不影響啟動與劑量計算。
        try:
            from cmuh_common.uvb_dose import load_and_apply_uvb_rules
            load_and_apply_uvb_rules()
        except Exception:
            pass
        try:
            _ufs = float(self.threshold_settings.get("ui_font_scale", 1.0))
        except (TypeError, ValueError):
            _ufs = 1.0
        self.ui_font_scale_var = tk.DoubleVar(value=max(0.85, min(1.45, _ufs)))
        # [預設關閉] 多台電腦同時跑時，若有人達到止掛門檻會重複寄信 → 預設關。
        # 想開啟止掛提醒/寄信的電腦，到設定頁勾選對應醫師即可。
        self.alert_chang_enabled = tk.BooleanVar(value=self.threshold_settings.get("alert_chang_enabled", False))
        self.alert_chen_enabled = tk.BooleanVar(value=self.threshold_settings.get("alert_chen_enabled", False))
        # 止掛達門檻時要寄信通知的收件人（可多人）
        self.alert_email_recipients = list(self.threshold_settings.get(
            "alert_email_recipients",
            ["expertise88864@gmail.com",
             "chilly840724@gmail.com",
             "mbpushowo@gmail.com"]))
        # 止掛提醒信的寄件人帳號（必須先在 Outlook 設定此 SMTP 帳號）
        self.alert_email_sender = str(self.threshold_settings.get(
            "alert_email_sender", "cmuhdermatology@gmail.com"))
        self.out_of_hospital_var = tk.BooleanVar(value=self.threshold_settings.get("out_of_hospital_mode", False))
        # [2026-07-13 使用者] 外院/分院診次固定顯示（設定已移除、不再讓使用者勾選）。
        self.show_external_clinics = tk.BooleanVar(value=True)

        self.val_alert_chang = self.alert_chang_enabled.get()
        self.val_alert_chen = self.alert_chen_enabled.get()
        self.val_out_of_hospital = self.out_of_hospital_var.get()

        self.r_doctor_map = self.load_r_doctor_settings()
        self.doctors_list = self.load_doctors_settings()

        self.notified_counts = defaultdict(int)
        self.alert_frequency = defaultdict(int)
        self._alert_popup_active = defaultdict(bool)
        self._alert_state_lock = threading.Lock()
        # 已寄出止掛信的記錄(跨重啟去重;寄信前先查、寄成功後寫)
        self._alert_email_sent = self._load_alert_email_sent()
        self._dnd_suppressed_count = 0
        # [2026-07-13 使用者] 「提醒勿擾時段」與「半夜也監測」設定已移除、不再讓使用者勾選；
        # 固定行為：止掛提醒(reg52)email 全天候照寄；夜間 00:00–08:00 只寄 email+記 log、不跳彈窗
        # (見 _is_notification_suppressed_now)；門診進度/現場人數(reg64)固定 00:00–07:00 不刷新
        # (見 _update_clinic_lights_loop 的 _reg64_clinic_quiet_hours 閘)。
        # F8 快速輸入文字 (預設 dtderm25，可在設定頁修改)
        self.quick_text_f8_var = tk.StringVar(value=str(self.threshold_settings.get("quick_text_f8", F8_QUICK_TEXT_DEFAULT)))
        self._live_count_samples = defaultdict(lambda: deque(maxlen=12))

        self.cl_check_interval = 30
        self.cl_last_check_time = 0
        self._priority_refresh_last_check_time = defaultdict(float)
        # [2026-07-13 user] 鄰近門檻醫師的「下一次優先刷新」計畫：{doc_name: (tier, 目標間隔秒)}。
        # tier='critical'(門檻-3)→10分、'mid'(門檻-5)→15分、'near'(門檻-10)→30分，各帶 ±10-20% 抖動。
        # 存 tier 是為了在 tier 升/降級時(例如從門檻-10 追進門檻-3)立刻改用新間隔，而非等舊間隔跑完。
        # 每次刷新後依當前 tier 重隨機(抖動)。
        self._priority_refresh_plan = {}
        self._refresh_tick_after_id = None
        self._pending_refresh_tick_ui = None

        # 【效能 2026.05.20】retry total 3→1：內網慢就是壞，3 次 backoff 把首屏拖到 10s+
        retries = Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
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
        # 【UX 2026-05-21】立刻觸發 UI refresh 顯示已載入的快取資料
        # 原本 _init_ui 建空白 widgets → load_cached_data 填 self.all_doctors_data
        # → 但沒人通知 UI refresh → 使用者看到「窗開了內容空白」直到第一次網路
        # 結果回來才觸發 _schedule_refresh。新增此行讓 160ms debounce 後立刻畫快取。
        self._schedule_refresh()
        self.startup_phase_text.set("快取完成")

        # ─── 浮動門診動態小視窗(半透明置頂、預設關) ───────────────────
        # 以「診間號」為 key(非 index)→ 不受診間重排影響;且輪詢時【無論視窗開沒開
        # 都更新】,使用者一開視窗就有最新資料,不會卡在 60-90 秒前的 "?"。
        self._floating_status_by_room = {}             # room_code -> floating_clinic.RoomStatus
        # room_code -> 連續「錯誤/逾時且無今日快取」次數。達門檻才視為「今天真的沒這個診」
        # 而以 error 旗標餵浮動視窗隱藏;單次/前幾次連線異常(冷啟動、換節、院方瞬斷)只是暫時
        # 性,不馬上藏掉「其實有診」的診間(成功/有快取的那輪會在 _capture_floating_status 歸零)。
        self._floating_error_streak = {}
        # 每個診間【本輪】reg64 是否可達(room_code -> bool;每輪輪詢預掃時逐診間覆寫)。可達 =
        # 非 backoff 用舊快取(stale fallback 代表 reg64 正在失敗)且非錯誤/逾時(含 TTL 內 cache
        # 命中,代表近期連得到)。_floating_network_seems_up 用它判斷「有沒有別的診間連得上(網路
        # 正常)」,才決定要不要把持續連不上的診間隱藏(見 update_single_clinic_ui_error)。
        self._reg64_room_reachable = {}
        self.floating_clinic_win = None
        self.floating_clinic_tick_id = None
        self._floating_clinic_settings = self._load_floating_clinic_settings()
        # 門診動態顯示方式(單一真實來源):off / floating(浮動視窗)
        self.clinic_widget_mode = tk.StringVar(
            value=self._normalize_widget_mode(self._floating_clinic_settings.get("mode", "off")))
        self.floating_clinic_opacity = tk.DoubleVar(
            value=float(self._floating_clinic_settings.get("opacity", 0.85)))

        self.root.after(50, self.deferred_initialization)
        self.root.after(100, self.process_ui_queue)

    def _cleanup_for_exit(self):
        if self._exit_cleanup_done:
            return
        self._exit_cleanup_done = True
        self._shutting_down = True
        logging.info("Shutdown signal received.")
        # 門診動態小工具:關閉前存好設定並銷毀浮動視窗(fail-open)
        try:
            self._save_floating_clinic_settings()
            self._close_floating_clinic()
        except Exception:
            pass
        stop_event_main.set()
        # [O21] stop_event_automation 也設，讓所有業務迴圈即時退出
        try:
            stop_event_automation.set()
        except Exception:
            logging.debug("stop_event_automation.set 失敗", exc_info=True)
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

        # [O21 v3] 加速關閉：跳過慢的 driver.quit() (~1-2s 等 Chrome graceful
        # shutdown)，直接 taskkill chromedriver+chrome 子進程 (~100ms)。
        # 先 nullify pool 引用避免別人重用死 driver，再快速砍 process。
        try:
            pool = _status_driver_pool
            with pool["lock"]:
                pool["driver"] = None
        except Exception:
            logging.debug("status driver pool reset 失敗", exc_info=True)

        if hasattr(self, 'bg_executor'):
            try:
                self.bg_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self.bg_executor.shutdown(wait=False)
        # 【穩定性 2026-05-21】不要呼叫 session.close()，會等所有未完成 request；
        # 卡 read 就 hang 0.5-2s。改 clear poolmanager 強制斷所有連線、立刻返回。
        for _attr in ('duty_session', 'session'):
            session = getattr(self, _attr, None)
            if session is None:
                continue
            try:
                for adapter in session.adapters.values():
                    try:
                        adapter.poolmanager.clear()
                    except Exception:
                        pass
            except Exception as e:
                logging.warning(f"Failed to clear requests session pool ({_attr}): {e}")

        # [O21 v3] 同步 (而非 background) taskkill chromedriver/chrome：
        # 用 psutil 直接 SIGKILL 比 selenium driver.quit() 快 10x。同步跑是因為
        # 主流程立刻會 os._exit(0)，background thread 沒機會收尾。100ms 內完成。
        try:
            self._kill_orphan_chromedriver()
        except Exception:
            logging.debug("taskkill chromedriver 失敗", exc_info=True)

        logging.info("Cleanup done: hotkeys unhooked, Chrome released, executor released.")

    @staticmethod
    def _kill_orphan_chromedriver() -> None:
        """[O21] 結束殘留的 chromedriver.exe + 其子 chrome.exe（防止崩潰留下的孤兒）。

        策略：找父進程為本程式的 chromedriver → 連帶遞迴 kill 它的所有子 chrome
              .exe / chrome_native_messaging_host 等。比 driver.quit() 快 10x
              (taskkill 100ms vs Chrome graceful shutdown 1-2s)。

        [MG-04] kill「父進程是本程式」或「父進程已死的真孤兒」chromedriver（後者=前次崩潰殘留）;
        父進程存活且非本程式者不動,避免誤殺其他 Chrome。
        """
        try:
            import psutil
        except ImportError:
            return
        try:
            my_pid = os.getpid()
            to_kill = []
            for p in psutil.process_iter(['pid', 'name', 'ppid']):
                try:
                    n = (p.info.get('name') or '').lower()
                    if 'chromedriver' not in n:
                        continue
                    ppid = p.info.get('ppid', 0)
                    # [MG-04 2026-07-12] 除本程式直屬子 chromedriver 外,也收「父行程已不存在」的真
                    # 孤兒(前次崩潰殘留、父已死);診間機的 chromedriver 皆來自本套件自動化,父已死者
                    # 即洩漏行程(~150MB)。用 pid_exists 保守判定(PID 被重用→存在→不誤殺)。
                    if ppid == my_pid or (ppid and not psutil.pid_exists(ppid)):
                        to_kill.append(p)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            # 對每個 chromedriver，連帶遞迴 kill 子孫 (chrome.exe / 渲染器等)
            for cd in to_kill:
                try:
                    for child in cd.children(recursive=True):
                        try:
                            child.kill()  # kill 比 terminate 快 (immediate)
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                    cd.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            logging.debug("[O21] iter chromedriver 失敗", exc_info=True)

    def _restart_app(self):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self._restart_app)
            return
        logging.info("Restart requested.")
        self._cleanup_for_exit()
        try:
            self.root.update_idletasks()
        except Exception:
            logging.debug("update_idletasks before restart failed.", exc_info=True)
        try:
            self.root.destroy()
        except Exception:
            logging.debug("root.destroy during restart failed.", exc_info=True)
        # [2026-05-22 v29] 必須在 restart_self() 之前 release mutex，否則新 process
        # 起來時 mutex 還被舊 process 持有 → ensure_single_instance() 看到 mutex
        # 存在 → 跳「已在執行中」MessageBox → exit → user 看到「自動更新沒重啟」。
        # atexit handler 在 os.execv / subprocess+sys.exit 路徑可能不保證會跑，
        # 必須顯式釋放。
        try:
            release_single_instance()
            logging.info("[restart] mutex released before respawn")
        except Exception:
            logging.debug("release_single_instance during restart failed.",
                           exc_info=True)
        # 帶 --background：重啟後的新行程靜默啟動（不開 splash、最小化進工作列），
        # 不打斷使用者當下操作。此方法是所有 app 端重啟（自動更新 / 閒置熱鍵恢復）的匯流點。
        restart_self(["--background"])

    def _restart_when_hotkey_idle(self, attempts: int = 0):
        """[MG-02] 自動更新需重啟時的閘門:熱鍵自動化進行中【不可】重啟(見 _UPDATE_RESTART_* 常數旁
        說明)。等『無 subsystem 在跑且距最後一次熱鍵動作 ≥N 秒』才 _restart_app;忙碌則每隔幾秒重查。
        到延後上限仍未閒置就重啟(旗標卡死由熱鍵 watchdog 兜底,不讓更新永不生效)。只在主緒操作。"""
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, lambda: self._restart_when_hotkey_idle(attempts))
            return
        busy = bool(getattr(self, "_subsystem_running", False))
        idle_gap = time.time() - getattr(_runner_1280, "last_action_time", 0.0)
        if attempts >= _UPDATE_RESTART_MAX_DEFER_ATTEMPTS:
            logging.warning("[更新] 熱鍵仍忙但已達重啟延後上限(%d),仍執行重啟(busy=%s, idle_gap=%.1fs)",
                            attempts, busy, idle_gap)
            self._restart_app()
            return
        if not busy and idle_gap >= _UPDATE_RESTART_IDLE_GAP_SEC:
            self._restart_app()
            return
        logging.info("[更新] 熱鍵自動化進行中,延後重啟(busy=%s, idle_gap=%.1fs, 第 %d 次重查)",
                     busy, idle_gap, attempts + 1)
        self.root.after(_UPDATE_RESTART_RECHECK_MS,
                        lambda: self._restart_when_hotkey_idle(attempts + 1))

    def shutdown_app(self):
        """關閉時不可在主執行緒上 executor.shutdown(wait=True)，否則會卡到背景 HTTP／排程結束。

        [加速] cleanup 跑完立刻 destroy；不等待背景任何工作。Daemon thread/Chrome
        會被 _kill_orphan_chromedriver 處理或隨進程結束。

        【穩定性 2026.05.20】os._exit(0) 跳過 atexit，但 mutex / log handler 需顯式釋放。
        """
        # [加速關閉] 先把視窗即時藏起 → 使用者「點 X 立刻消失」的感受;隨後的 cleanup
        # (解鉤熱鍵、砍殘留 chromedriver 等約 100-400ms)在視窗已不可見時進行,不再讓
        # 使用者盯著還沒消失的視窗枯等。cleanup 仍同步完成(砍 chromedriver 必須在
        # os._exit 前跑完,否則留孤兒),只是移到「視覺上已關閉」之後。
        # 註:withdraw() 在 Windows 即時 SW_HIDE,毋需 update_idletasks();刻意不呼叫
        # 它 —— 那會 pump 待處理的 after_idle(可能含阻塞式覆蓋窗,拖慢關閉)。
        try:
            self.root.withdraw()
        except Exception:
            pass
        try:
            self._cleanup_for_exit()
        except Exception:
            logging.debug("cleanup 例外（忽略，繼續退出）", exc_info=True)
        try:
            self.root.destroy()
        except Exception:
            pass

        # 顯式 cleanup（os._exit 會跳過 atexit）
        # [codex P2] 稽核寫入緒是 daemon,os._exit(0) 會直接砍掉它 → 關閉/更新重啟前剛入列
        # 的動作紀錄會憑空消失(更新重啟很頻繁,這不是罕見路徑)。在此做【有上限】的排空,
        # 寧可少等一下也不要丟稽核;但絕不可拖延關閉,故有硬逾時。
        try:
            _flush_ledger_before_exit()
        except Exception:
            logging.debug("[ledger] 關閉前排空例外(忽略)", exc_info=True)
        try:
            release_single_instance()
        except Exception:
            pass
        try:
            logging.shutdown()  # flush + close 所有 handler
        except Exception:
            pass

        # 強制立刻終止進程
        try:
            os._exit(0)
        except SystemExit:
            raise
        except Exception:
            sys.exit(0)

    # --- [修改] 儲存快取通用函式 (加入 Key 轉換) ---
    def _save_cache(self, filename, data):
        try:
            # [O22] cache_clinic_counts 改用 SQLite 增量寫入；其他仍用 JSON
            if filename == 'cache_clinic_counts.json':
                from cmuh_common.sqlite_cache import save_clinic_counts
                save_clinic_counts(data)
                return
            save_json_cache(get_conf_path(filename), data)
        except Exception as e:
            logging.error(f"儲存快取 {filename} 失敗: {e}")

    def _rebuild_master_schedule_index(self):
        indexes = build_master_schedule_index(self.master_schedule)
        self._master_schedule_by_weekday, self._master_schedule_self_paid = indexes

    # --- [修改] 載入快取資料 (加入損壞自動刪除機制) ---
    def load_cached_data(self):
        """啟動時先讀取本地 cache，加快顯示速度。
        [O22] 門診人數改用 SQLite（自動從舊 JSON 一次性遷移）。
        """
        try:
            # 1. [O22] 載入 門診人數 (all_doctors_data) — SQLite
            try:
                from cmuh_common.sqlite_cache import load_clinic_counts
                raw_data = load_clinic_counts()
                if raw_data:
                    for doc_no, doc_data in raw_data.items():
                        if isinstance(doc_data, dict) and 'error' not in doc_data:
                            with self._doctor_data_lock:
                                self.all_doctors_data[doc_no] = _decode_cache_date_keys(doc_data)
                    logging.info("[O22] 已載入門診人數快取（SQLite，%d 醫師）", len(raw_data))
            except Exception:
                logging.warning("[O22] SQLite cache 載入失敗，fallback 到 JSON", exc_info=True)
                # Fallback：舊 JSON 還在的話讀取
                cache_path = get_conf_path('cache_clinic_counts.json')
                raw_data = load_json_dict(cache_path, {}, merge_defaults=False)
                if raw_data:
                    for doc_no, doc_data in raw_data.items():
                        if isinstance(doc_data, dict) and 'error' not in doc_data:
                            with self._doctor_data_lock:
                                self.all_doctors_data[doc_no] = _decode_cache_date_keys(doc_data)
                    logging.info("已載入門診人數快取（JSON fallback）。")

            # [perf r5] 清一次過老(>30天)的門診人數 row,避免 DB 隨運行天數累積舊日期
            # row 拖慢全表載入。顯示只用近期/未來日期,刪舊不影響 UI。
            # [perf 2026-06-15] 改丟背景 daemon 緒:此 DELETE 與啟動畫面無關,放背景不卡
            # 開啟。SQLite 自行序列化讀寫;刪的是舊日期、UI 讀的是近期/未來,無邏輯衝突。
            def _vacuum_old_counts_bg():
                try:
                    from cmuh_common.sqlite_cache import vacuum_old_entries
                    _removed = vacuum_old_entries(older_than_days=30)
                    if _removed:
                        logging.info("[O22] 背景清理 %d 筆過老門診人數 row(>30天)", _removed)
                except Exception:
                    logging.debug("[O22] vacuum 過老 row 失敗(忽略)", exc_info=True)
            try:
                threading.Thread(target=_vacuum_old_counts_bg,
                                 name="VacuumOldCounts", daemon=True).start()
            except Exception:
                logging.debug("[O22] 啟動 vacuum 背景緒失敗(忽略)", exc_info=True)

            # 2. 載入 主門診表 (master_schedule)
            sched_path = get_conf_path('cache_master_schedule.json')
            cached_schedule = load_master_schedule_cache(sched_path)
            if cached_schedule:
                self.master_schedule = cached_schedule
                self._rebuild_master_schedule_index()
                logging.info("已載入主門診表快取。")

            # 3. 載入 值班資訊 (Duty Info)
            duty_path = get_conf_path('cache_duty_info.json')
            duty_info = load_json_dict(duty_path, {}, merge_defaults=False)
            if duty_info:
                today_str = date.today().strftime("%Y-%m-%d")
                if duty_info.get('date') == today_str:
                    if 'duty_doctor' in duty_info: self.duty_doctor_var.set(duty_info['duty_doctor'])
                if 'today_vs' in duty_info: self.duty_vs_var.set(duty_info['today_vs'])

                if 'saturday_duty' in duty_info: self.saturday_duty_doctor_var.set(duty_info['saturday_duty'])
                if 'saturday_vs' in duty_info: self.saturday_duty_vs_var.set(duty_info['saturday_vs'])
                self._refresh_duty_summary_text()
                logging.info("已載入值班資訊快取。")

        except Exception as e:
            logging.error(f"載入快取失敗: {e}")

# [新增] 載入歷史緩存
    def _load_history_cache(self):
        file_path = get_conf_path('clinic_stats_history.json')
        self.history_cache = load_json_list(file_path, [])
        self._avg_history_cache = {}  # [優化] 歷史資料更新，清除計算快取

    def _clinic_dynamic_today_str(self):
        return clinic_dynamic_today_str()

    def _clinic_dynamic_state_key(self, room_code, time_code):
        return clinic_dynamic_state_key(room_code, time_code)

    def _new_clinic_tracker(self, curr_session_i, current_timestamp):
        return new_clinic_tracker(curr_session_i, current_timestamp)

    def _load_clinic_dynamic_state_cache(self):
        file_path = get_conf_path(CLINIC_DYNAMIC_STATE_FILENAME)
        payload = load_json_dict(file_path, {}, merge_defaults=False)
        if payload.get("date") != self._clinic_dynamic_today_str():
            return {}
        states = payload.get("states", {})
        return states if isinstance(states, dict) else {}

    def _write_clinic_dynamic_state_cache(self):
        payload = {
            "date": self._clinic_dynamic_today_str(),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "states": self._clinic_dynamic_state_cache,
        }
        _atomic_write_json(get_conf_path(CLINIC_DYNAMIC_STATE_FILENAME), payload)

    # ── 止掛提醒信「跨重啟去重」──────────────────────────────────────────
    # 問題:寄信去重原本只靠記憶體 (self.alert_frequency / _alert_popup_active),
    # 重開程式就歸零 → 同一診次當天會再寄一次。改為持久化『已寄出』的 notify_key,
    # 寄信前先讀此記錄;已寄過就跳過,確保每個診次當天只寄一封。
    def _load_alert_email_sent(self) -> dict:
        """讀『已寄止掛信』記錄 → {notify_key: 'YYYY-MM-DD'}。只留近 N 天避免膨脹。"""
        try:
            data = load_json_dict(get_conf_path(ALERT_EMAIL_SENT_FILENAME), {},
                                  merge_defaults=False)
        except Exception:
            return {}
        cutoff = (date.today()
                  - timedelta(days=ALERT_EMAIL_SENT_RETAIN_DAYS)).isoformat()
        return _filter_recent_alert_sent(data, cutoff)

    def _has_alert_email_been_sent(self, notify_key: str) -> bool:
        with self._alert_state_lock:
            return notify_key in self._alert_email_sent

    def _mark_alert_email_sent(self, notify_key: str) -> None:
        """記錄某止掛信已寄出並持久化(僅在『確實寄出成功』後呼叫)。

        寫檔在鎖內完成:把「快照建立」與「落地」序列化,避免兩個通知緒各自在鎖外
        以不同順序寫入 → 較舊的快照後到、覆蓋掉較新的(漏記 key → 重啟後重寄)。
        寄信頻率極低(一天數次),寫檔僅 atomic rename(~毫秒),持鎖成本可忽略。"""
        with self._alert_state_lock:
            self._alert_email_sent[notify_key] = date.today().isoformat()
            snapshot = dict(self._alert_email_sent)
            try:
                _atomic_write_json(get_conf_path(ALERT_EMAIL_SENT_FILENAME),
                                   snapshot)
            except Exception:
                logging.warning("寫入止掛信寄出記錄失敗(不影響本次提醒)", exc_info=True)

    def _clinic_dynamic_state_matches(self, state, room_code, time_code, doc_name=None, session_cn=None):
        return state_matches(
            state, room_code, time_code, self._clinic_dynamic_today_str(),
            _canonical_clinic_session_str, doc_name, session_cn)

    def _restore_clinic_tracker_from_state(self, state, curr_session_i, current_timestamp):
        return restore_tracker_from_state(
            state, curr_session_i, current_timestamp,
            _canonical_clinic_session_str)

    def _get_clinic_dynamic_state(self, room_code, time_code, doc_name=None, session_cn=None):
        key = self._clinic_dynamic_state_key(room_code, time_code)
        # [stability r5] persist 會整個 rebind cache(prune→新 dict)、clear 會 .pop，皆在
        # _clinic_dynamic_state_lock 下。讀取取同一鎖避免讀到過渡狀態(背景 worker 呼叫)。
        with self._clinic_dynamic_state_lock:
            state = self._clinic_dynamic_state_cache.get(key)
        if self._clinic_dynamic_state_matches(state, room_code, time_code, doc_name, session_cn):
            return state
        return None

    def _persist_clinic_dynamic_state(self, room_code, time_code, tracker, result, curr_avg="-", est_remain="—", hist_light="—", prev_close="—"):
        doc_name = (tracker.get("doc_name") or result.get("doc_name") or "").strip()
        session_cn = _canonical_clinic_session_str(tracker.get("session_period")) or reg64_slot_cn(time_code)
        if not room_code or not time_code or not doc_name or not session_cn:
            return
        today_s = self._clinic_dynamic_today_str()
        state = build_dynamic_state(
            today_s,
            datetime.now().isoformat(timespec="seconds"),
            room_code,
            time_code,
            session_cn,
            doc_name,
            tracker,
            result,
            current_timestamp=time.time(),
            curr_avg=curr_avg,
            est_remain=est_remain,
            hist_light=hist_light,
            prev_close=prev_close,
        )
        key = self._clinic_dynamic_state_key(room_code, time_code)
        try:
            with self._clinic_dynamic_state_lock:
                self._clinic_dynamic_state_cache = prune_states_for_today(
                    self._clinic_dynamic_state_cache, today_s)
                self._clinic_dynamic_state_cache[key] = state
                self._write_clinic_dynamic_state_cache()
        except Exception as e:
            logging.warning(f"儲存門診動態即時快取失敗: {e}")

    def _clear_clinic_dynamic_state(self, room_code, time_code=None, doc_name=None):
        try:
            with self._clinic_dynamic_state_lock:
                keys_to_delete = matching_state_keys(
                    self._clinic_dynamic_state_cache, room_code, time_code, doc_name)
                for key in keys_to_delete:
                    self._clinic_dynamic_state_cache.pop(key, None)
                if keys_to_delete:
                    self._write_clinic_dynamic_state_cache()
        except Exception as e:
            logging.warning(f"清除門診動態即時快取失敗: {e}")

    def _prepaint_clinic_dynamic_state(self):
        if not getattr(self, "clinic_ui_elements", None):
            return
        now = datetime.now()
        for idx, ui in enumerate(self.clinic_ui_elements[:CLINIC_ROOM_COUNT]):
            if idx >= len(getattr(self, "clinic_room_vars", [])):
                continue
            room_code = self.clinic_room_vars[idx].get().strip()
            if not room_code:
                continue
            mode = (
                self.clinic_display_mode_vars[idx].get()
                if idx < len(getattr(self, "clinic_display_mode_vars", []))
                else "auto"
            )
            time_code = resolve_clinic_reg64_time_code(mode, now)
            session_cn = reg64_slot_cn(time_code)
            state = self._get_clinic_dynamic_state(room_code, time_code, session_cn=session_cn)
            if not state:
                continue
            tracker_key = self._clinic_dynamic_state_key(room_code, time_code)
            tracker = self._restore_clinic_tracker_from_state(state, session_cn, time.time())
            with self._tracker_lock:
                self.clinic_trackers.setdefault(tracker_key, tracker)
            result = dict(state.get("last_result") or {})
            result.setdefault("doc_name", tracker.get("doc_name", ""))
            result.setdefault("reg64_time_code", str(time_code))
            result.setdefault("light", "--")
            result.setdefault("total", "-")
            result.setdefault("waiting", "-")
            result.setdefault("completed", 0)
            result["status"] = result.get("status") or "上次快取"
            display = state.get("last_display") or {}
            self.update_single_clinic_ui(idx, result, tracker, display.get("curr_avg", "-"))
            self._smart_widget_config(ui["comp_all"], text=str(result.get("completed", 0)))
            self._smart_widget_config(ui["est_remain"], text=display.get("est_remain", "—"), fg="#E65100")
            self._smart_widget_config(ui["hist_light"], text=display.get("hist_light", "—"))
            self._smart_widget_config(ui["prev_sess_close"], text=display.get("prev_close", "—"))
            self._smart_widget_config(ui["status"], text="已載入上次快取，等待更新", fg="#607D8B")
            logging.info(f"[CACHE] restored clinic dynamic state: room={room_code}, session={session_cn}, doctor={tracker.get('doc_name', '')}")
        
    # =========================================================================
    # UI 主題（深淺色）切換
    # =========================================================================
    def _get_ui_theme_mode(self) -> str:
        """[2026-06-01] 主題切換功能已移除：一律使用 Windows 原生主題 (vista)，維持原本外觀。
        （不再讀 clinic_settings 的 ui_theme，避免有人之前切到深色而卡住。）"""
        return "vista"

    # [2026-06-01] _toggle_ui_theme 已移除：主題切換(深/淺色)功能取消，固定 Windows 原生主題。

    def _safe_load_clinic_settings(self) -> dict:
        settings = load_json_dict(get_conf_path('clinic_settings.json'), {},
                                  merge_defaults=False)
        settings["rooms"], _changed = normalize_clinic_rooms(settings.get("rooms"))
        return settings

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

        # [UI 主題] 依使用者設定套用：dark / light（sv-ttk）或 vista（原生最快）
        theme_mode = self._get_ui_theme_mode()
        sv_ttk_applied = False
        if theme_mode == "vista":
            # 使用者選原生主題（切換最快）
            try:
                self.style.theme_use("vista")
                logging.info("[UI] 套用 vista 原生主題")
            except Exception:
                try:
                    self.style.theme_use("clam")
                except Exception:
                    pass
        else:
            # sv-ttk light / dark
            try:
                import sv_ttk  # type: ignore[import-not-found]
                sv_ttk.set_theme(theme_mode)
                sv_ttk_applied = True
                logging.info("[UI] 已套用 sv-ttk %s 主題（Win11 風格）", theme_mode)
            except Exception:
                logging.debug("sv-ttk 不可用，fallback vista/clam", exc_info=True)
                try:
                    available = self.style.theme_names()
                    for preferred in ('vista', 'xpnative', 'clam', 'default'):
                        if preferred in available:
                            self.style.theme_use(preferred)
                            break
                except Exception:
                    pass
        self._sv_ttk_applied = sv_ttk_applied
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

        # =============================================================
        # [UI 美化] 統一配色 + 互動效果（hover / pressed）
        # =============================================================
        # 主題色：中國醫深藍
        BRAND_BLUE = "#005A9C"
        BRAND_BLUE_LIGHT = "#1976D2"
        BRAND_BLUE_PRESS = "#003F73"
        ACCENT = "#00897B"        # 強調色（搶眼但不刺眼）
        SURFACE = "#FAFAFA"
        BORDER = "#DDDDDD"

        # 一般按鈕 hover/active 高亮
        try:
            self.style.map("TButton",
                           background=[("active", "#E3F2FD"), ("pressed", "#BBDEFB")])
        except Exception:
            pass

        # 主要按鈕（重要動作）
        self.style.configure("Primary.TButton",
                             font=("Microsoft JhengHei UI", self.f_md, "bold"),
                             padding=(10, 6))
        try:
            self.style.map("Primary.TButton",
                           foreground=[("active", BRAND_BLUE_PRESS),
                                       ("!active", BRAND_BLUE)])
        except Exception:
            pass

        # 次要按鈕（用於大量按鈕排列）
        self.style.configure("Secondary.TButton",
                             font=("Microsoft JhengHei UI", self.f_md),
                             padding=(8, 4))

        # 危險按鈕（刪除/重啟等）
        self.style.configure("Danger.TButton",
                             font=("Microsoft JhengHei UI", self.f_md, "bold"),
                             foreground="#C62828",
                             padding=(10, 6))

        # LabelFrame 邊框淡化、文字深藍 bold
        self.style.configure("TLabelframe", borderwidth=1, relief="solid",
                             bordercolor=BORDER)
        self.style.configure("TLabelframe.Label",
                             font=("Microsoft JhengHei UI", self.f_md, "bold"),
                             foreground=BRAND_BLUE)

        # Combobox 高度 / 字級統一
        self.style.configure("TCombobox",
                             font=("Microsoft JhengHei UI", self.f_md),
                             padding=2)

        # Entry 文字較深、padding 統一
        self.style.configure("TEntry", padding=4)

        # Notebook：sv-ttk / vista 主題已有完善樣式，不要強制覆寫 foreground/background
        # 否則會造成「白底白字」（如 sv-ttk light 選中分頁背景為白，被我們設成白字 → 看不到）
        # 只在 clam / default 主題下才覆寫
        try:
            current_theme = self.style.theme_use()
        except Exception:
            current_theme = ""
        if current_theme in ("clam", "default") and not self._sv_ttk_applied:
            try:
                self.style.map('TNotebook.Tab',
                               background=[('selected', BRAND_BLUE),
                                           ('active', '#E3F2FD'),
                                           ('!active', '#F0F0F0')],
                               foreground=[('selected', 'white'),
                                           ('active', BRAND_BLUE),
                                           ('!active', '#333333')])
            except Exception:
                pass

        # Treeview 行距加大、選中色加深
        self.style.configure("Treeview",
                             font=("Microsoft JhengHei UI", self.f_md),
                             rowheight=max(22, self.f_md * 2))
        self.style.configure("Treeview.Heading",
                             font=("Microsoft JhengHei UI", self.f_md, "bold"),
                             background=SURFACE,
                             foreground=BRAND_BLUE,
                             padding=(4, 4))

        # Progressbar 顏色
        self.style.configure("TProgressbar",
                             troughcolor=SURFACE,
                             background=BRAND_BLUE,
                             bordercolor=BORDER,
                             lightcolor=BRAND_BLUE,
                             darkcolor=BRAND_BLUE)

        # 儲存色票供後續使用（也可被別的方法引用）
        self._brand_blue = BRAND_BLUE
        self._brand_blue_light = BRAND_BLUE_LIGHT
        self._accent = ACCENT
        self._surface = SURFACE
        self._border = BORDER

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
        self._register_lazy_tab("縮寫速寫", lambda frame: self._create_abbrev_tab(frame))
        self._register_lazy_tab("設定", lambda frame: self._create_settings_tab(frame))
        self._register_lazy_tab("系統日誌", lambda frame: self._create_log_tab(frame))

        # 建立 Queue 與 Handler
        self.log_queue = Queue(maxsize=5000)
        queue_handler = attach_queue_handler(self.log_queue, replace_existing=True)
        formatter = logging.Formatter('%(asctime)s [%(levelname)s]: %(message)s', datefmt='%H:%M:%S')
        queue_handler.setFormatter(formatter)
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
        if getattr(self, "_shutting_down", False) or stop_event_main.is_set():
            return False
        if threading.current_thread() is threading.main_thread():
            callback()
            return True
        else:
            try:
                self.root.after(0, callback)
            except (tk.TclError, RuntimeError):
                logging.debug("略過已關閉 UI 的 callback", exc_info=True)
                return False
            return True

    def _refresh_duty_summary_text(self):
        if not hasattr(self, "duty_row1_prefix_var"):
            return
        parts = build_duty_summary_parts(
            self.duty_doctor_var.get(),
            self.duty_vs_var.get(),
            self.saturday_duty_doctor_var.get(),
            self.saturday_duty_vs_var.get(),
        )
        self.duty_row1_prefix_var.set(parts["row1_prefix"])
        self.duty_row1_name_var.set(parts["row1_name"])
        self.duty_row1_vs_lbl_var.set(parts["row1_vs_label"])
        self.duty_row1_vs_name_var.set(parts["row1_vs_name"])
        self.duty_row2_prefix_var.set(parts["row2_prefix"])
        self.duty_row2_name_var.set(parts["row2_name"])
        self.duty_row2_vs_lbl_var.set(parts["row2_vs_label"])
        self.duty_row2_vs_name_var.set(parts["row2_vs_name"])

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
        # [2026-07-13 使用者] 「提醒勿擾時段」可調設定已移除，固定 00:00–08:00 為勿擾窗：此時段止掛提醒
        # 只寄 email + 記 log、【不跳彈窗】(夜間診間無人看螢幕)，其餘時段照跳。email 一律 24 小時照寄
        # (本函式只管『要不要跳彈窗』，不影響寄信偵測)。
        start_m = NOTIFY_DO_NOT_DISTURB_START_HOUR * 60
        end_m = NOTIFY_DO_NOT_DISTURB_END_HOUR * 60
        now = datetime.now()
        now_m = now.hour * 60 + now.minute
        if start_m == end_m:
            return True
        if start_m < end_m:
            return start_m <= now_m < end_m
        return now_m >= start_m or now_m < end_m

    def _on_tab_changed(self, event):
        try: selected_tab_text = self.notebook.tab(self.notebook.select(), "text")
        except tk.TclError: return

        lazy_just_built = self._ensure_lazy_tab_initialized(selected_tab_text)

        # 1. 確保最外層的 Frame 是顯示的
        self.bottom_frame.grid()

        # 2. [修正] 用 grid_remove()/grid() 取代 pack_forget()/pack()
        #    僅在狀態改變時呼叫，減少不必要的版面重算
        hide_links = selected_tab_text in ("診斷書", "小工具", "縮寫速寫", "設定")
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

    # 1. 讀取 R1-R3 設定
    def load_r_doctor_settings(self):
        return _load_r_doctor_settings()

    # 2. 讀取 止掛人數 設定
    def load_threshold_settings(self):
        return _load_threshold_settings(
            dnd_start_hour=NOTIFY_DO_NOT_DISTURB_START_HOUR,
            dnd_end_hour=NOTIFY_DO_NOT_DISTURB_END_HOUR,
        )

    # 3. 讀取 醫師代號 設定
    def load_doctors_settings(self):
        return _load_doctors_settings()

    # 1. 儲存 所有設定 (包含 R醫師, 止掛, 醫師列表)
    def save_all_settings(self):
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
        # [2026-07-13 使用者] show_external_clinics / notify_dnd / clinic_night_monitor 設定已移除；
        # 行為固定（外院分院固定顯示、勿擾窗固定 00–08 只不跳彈窗、reg64 固定 00–07 暫停），不再存這幾個鍵。
        # F8 快速輸入文字 — 空字串不存（讓 _load_f8_quick_text 回 default）
        try:
            qt = str(self.quick_text_f8_var.get())
        except Exception:
            qt = F8_QUICK_TEXT_DEFAULT
        self.threshold_settings['quick_text_f8'] = qt if qt else F8_QUICK_TEXT_DEFAULT
        # 從 Listbox 同步止掛提醒收件人（若 UI 已建立）
        if hasattr(self, 'alert_mail_listbox') and self.alert_mail_listbox is not None:
            try:
                self.alert_email_recipients = [
                    a for a in self.alert_mail_listbox.get(0, tk.END)
                    if str(a).strip()
                ]
            except Exception:
                logging.debug("讀取止掛提醒收件人 Listbox 失敗", exc_info=True)
        self.threshold_settings['alert_email_recipients'] = list(self.alert_email_recipients)

        _atomic_write_json(get_conf_path('threshold_settings.json'), self.threshold_settings)

        # [2026-07-04] 設定變更後立即讓門診監測套用新的「半夜監測」設定：取消可能仍
        # 停在 07:00 的排程、立刻重跑一次（否則在 00–07 點重新開啟時要等到 07:00）。
        try:
            if getattr(self, "clinic_loop_id", None) is not None:
                self.root.after_cancel(self.clinic_loop_id)
                self.clinic_loop_id = None
            if hasattr(self, "_update_clinic_lights_loop"):
                self.clinic_loop_id = self.root.after(1000, self._update_clinic_lights_loop)
        except Exception:
            logging.debug("設定變更後重啟門診監測迴圈失敗", exc_info=True)

        new_doctors_list = []
        for item_id in self.doctors_tree.get_children():
            item = self.doctors_tree.item(item_id)
            doc_no, name = item['values'] 
            existing_doctor = next((doc for doc in self.doctors_list if doc['name'] == name), None)
            # [MG-03 2026-07-12] Treeview 對純數字代號回 int → str() 統一;notifications 缺鍵改 .get
            # 兜底(原本下標缺鍵 KeyError 會讓 doctors.json 沒寫成,但 r_doctor/threshold 已寫=半套且無提示)。
            notifications = existing_doctor.get('notifications', False) if existing_doctor else False
            new_doctors_list.append({"name": name, "doc_no": str(doc_no), "notifications": notifications})
        
        self.doctors_list = new_doctors_list
        _atomic_write_json(get_conf_path('doctors.json'), self.doctors_list)

        self._show_notice("設定已儲存", "所有設定已寫入檔案。\n若變更「介面字體縮放」，請重新啟動程式後才會套用。", level="info", auto_close_ms=4500)
        global DOCTORS, DOCTOR_NAMES
        DOCTORS = self.doctors_list
        DOCTOR_NAMES = [d["name"] for d in DOCTORS]
        self.refresh_all_calendars()
        self._trigger_refresh(is_manual=True)

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
        self.startup_phase_text.set("熱鍵就緒")
        self.setup_hotkeys()

    def _handle_hotkey_setup_failure(self, error):
        self._heavy_modules_loading = False
        self._heavy_modules_ready = False
        self.startup_phase_text.set("熱鍵失敗")
        self.hotkey_text_label.config(text="熱鍵模組載入失敗")
        self.status_text.set("狀態: 熱鍵模組載入失敗，請檢查環境")
        logging.error(f"Hotkey module initialization failed: {error}")

    def _start_hotkey_module_loading(self):
        if self._heavy_modules_ready:
            self._finalize_hotkey_setup()
            return
        if self._heavy_modules_loading:
            return

        self._heavy_modules_loading = True
        self.hotkey_text_label.config(text="熱鍵模組載入中...")
        self.status_text.set("狀態: 啟動中，正在背景載入熱鍵模組...")
        self.startup_phase_text.set("載入熱鍵")

        def _handle_hotkey_loader_rejected(fut):
            if fut.cancelled():
                rejected = True
            else:
                try:
                    rejected = isinstance(fut.exception(), RejectedExecutionError)
                except Exception:
                    rejected = False
            if not rejected:
                return
            logging.warning("熱鍵模組背景載入未啟動：背景佇列已滿")

            def _retry_hotkey_loader():
                self._heavy_modules_loading = False
                self.hotkey_text_label.config(text="熱鍵模組等待重試...")
                self.status_text.set("狀態: 背景佇列忙碌，熱鍵模組稍後重試")
                self.startup_phase_text.set("熱鍵待重試")
                if not getattr(self, '_shutting_down', False):
                    self.root.after(5000, self._start_hotkey_module_loading)

            if threading.current_thread() is threading.main_thread():
                _retry_hotkey_loader()
            elif not getattr(self, '_shutting_down', False):
                self.root.after(0, _retry_hotkey_loader)

        hotkey_future = self.bg_executor.submit(self._prepare_hotkeys_background)
        hotkey_future.add_done_callback(_handle_hotkey_loader_rejected)

    def deferred_initialization(self):
        """在 UI 渲染完成後才執行的初始化任務"""
        self.startup_phase_text.set("背景任務")
        self.start_background_tasks()
        # 門診動態：勿等「小工具」懶載入才輪詢，否則無法累積關診／掛號統計與總覽 reg64 快取
        self.root.after(450, self._start_clinic_lights_polling_once)

        self._start_hotkey_module_loading()

        # 門診動態小工具:依上次選的顯示方式,延後自動開窗(fail-open)。
        # 排 _apply_clinic_widget_mode(而非直接 _open_*),讓回呼於觸發當下重新讀 mode →
        # 即使使用者在這 800ms 內改了顯示方式,也只會開出符合目前選擇的那一種(且兩者互斥)。
        try:
            if self._normalize_widget_mode(self.clinic_widget_mode.get()) != "off":
                self.root.after(800, self._apply_clinic_widget_mode)
        except Exception:
            pass

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
        self.refresh_button = ttk.Button(manual_ops_frame, text="整理人數", style="Small.TButton", command=lambda: self._trigger_refresh(is_manual=True)); self.refresh_button.pack(side="left", pady=1, padx=(4,4))

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
            ("病理看片", "https://dsr.cmuh.org.tw/view/v2/LApi.Case/202630242"),
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
        # [v16 2026-05-25] 改多行 + 加 creationflags=CREATE_NO_WINDOW 避免黑框閃
        scheduler_script_name = "中國醫皮膚科排班程式.pyw"
        try:
            logging.info(f"Launching scheduler program: {scheduler_script_name}")
            launch_app_script(scheduler_script_name)
        except FileNotFoundError:
            messagebox.showerror("啟動失敗", f"找不到排班程式檔案: {scheduler_script_name}\n\n請確認主程式與排班程式在同一個資料夾中。")
            logging.error(f"Scheduler script not found: {scheduler_script_name}")
        except Exception as e:
            messagebox.showerror("啟動失敗", f"無法啟動排班程式:\n{e}")
            logging.error(f"Failed to launch scheduler: {e}")

    def _launch_autoclock_program(self):
        autoclock_script_name = "中國醫皮膚科打卡程式.pyw"
        if is_instance_running("Local\\CMUH_Skin_AutoClock_SingleInstance_v1"):
            logging.info("Autoclock program is already running; skip launch")
            return
        try:
            logging.info(f"Launching autoclock program: {autoclock_script_name}")
            launch_app_script(autoclock_script_name)
        except FileNotFoundError:
            messagebox.showerror("啟動失敗", f"找不到打卡程式檔案: {autoclock_script_name}\n\n請確認主程式與打卡程式在同一個資料夾中。")
            logging.error(f"Autoclock script not found: {autoclock_script_name}")
        except Exception as e:
            messagebox.showerror("啟動失敗", f"無法啟動打卡程式:\n{e}")
            logging.error(f"Failed to launch autoclock program: {e}")

    def _launch_coordinate_detector_program(self):
        script_name = "中國醫皮膚科點座標偵測程式.pyw"
        try:
            logging.info(f"Launching coordinate detector program: {script_name}")
            launch_app_script(script_name)
        except FileNotFoundError:
            messagebox.showerror("啟動失敗", f"找不到座標偵測程式檔案: {script_name}\n\n請確認主程式與該程式在同一個資料夾中。")
            logging.error(f"Coordinate detector script not found: {script_name}")
        except Exception as e:
            messagebox.showerror("啟動失敗", f"無法啟動座標偵測程式:\n{e}")
            logging.error(f"Failed to launch coordinate detector program: {e}")

    def _launch_consult_query_program(self):
        # 只啟動常駐托盤（不帶 --run-now），讓使用者由托盤選單或排程觸發；
        # 已啟動則靜默結束（不彈視窗）。需要立即執行可右鍵托盤「立即執行一次」。
        script_name = "中國醫皮膚科會診查詢程式.pyw"
        if is_instance_running("Local\\CMUH_Skin_ConsultQuery_SingleInstance_v1"):
            logging.info("Consult query program is already running; skip launch")
            return
        try:
            logging.info(f"Launching consult query program: {script_name}")
            launch_app_script(script_name)
        except FileNotFoundError:
            messagebox.showerror("啟動失敗", f"找不到會診查詢程式檔案: {script_name}\n\n請確認主程式與該程式在同一個資料夾中。")
            logging.error(f"Consult query script not found: {script_name}")
        except Exception as e:
            messagebox.showerror("啟動失敗", f"無法啟動會診查詢程式:\n{e}")
            logging.error(f"Failed to launch consult query program: {e}")

    def _create_other_programs_tab(self, tools_tab):
        
        # --- 定義樣式 (Styles) ---
        # [UI 美化] Big.TButton 縮小 padding，避免過於高大
        self.style.configure("Big.TButton",
                             font=("Microsoft JhengHei UI", 12, "bold"),
                             padding=(10, 6))
        try:
            self.style.map("Big.TButton",
                           background=[("active", "#E3F2FD"), ("pressed", "#BBDEFB")],
                           foreground=[("active", "#003F73"), ("!active", "#005A9C")])
        except Exception:
            pass
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
            ("偵測點座標", self._launch_coordinate_detector_program),
            ("會診查詢", self._launch_consult_query_program)
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

        # ─── 浮動門診動態視窗:啟用開關 + 透明度(由「設定」分頁移來,就近放門診動態區) ───
        floating_frame = ttk.LabelFrame(clinic_status_frame, text="浮動門診動態視窗", padding=8)
        floating_frame.pack(fill='x', padx=5, pady=(4, 6))
        _op_row = ttk.Frame(floating_frame)
        _op_row.pack(fill='x')
        ttk.Checkbutton(
            _op_row,
            text="啟用（半透明置頂、點擊穿透小窗）",
            variable=self.clinic_widget_mode,
            onvalue="floating", offvalue="off",
            command=self._apply_clinic_widget_mode,
        ).pack(side='left', pady=2)
        ttk.Label(_op_row, text="透明度:").pack(side='left', padx=(16, 4))
        ttk.Scale(
            _op_row,
            from_=0.25,
            to=0.95,
            variable=self.floating_clinic_opacity,
            command=lambda v: self._set_floating_clinic_opacity(),
            orient="horizontal",
        ).pack(side='left', fill='x', expand=True)
        ttk.Label(
            floating_frame,
            text=("半透明可拖曳小窗、點擊穿透(點得到後方 HIS),顯示下方三診、"
                  "依電腦時間自動切早/午/晚。"),
            foreground="gray",
            style="Small.TLabel",
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        self._ensure_clinic_room_model_from_disk()

        status_container = ttk.Frame(clinic_status_frame)
        status_container.pack(fill='both', expand=True, padx=5, pady=(0, 5))

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

        for i in range(CLINIC_ROOM_COUNT):
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

            # --- 3. 數據統計區：列0 即時四欄；列2 剩餘/歷史燈號/上一時段關診 ---
            stats_frame = tk.Frame(card_frame, bg="white", pady=4)
            stats_frame.pack(fill='x', padx=10, pady=(0, 6))

            for _c in (0, 2, 4, 6):
                stats_frame.columnconfigure(_c, weight=1)

            # 列0：即時四欄
            f_col1 = tk.Frame(stats_frame, bg="white"); f_col1.grid(row=0, column=0, sticky="nsew", padx=2)
            tk.Label(f_col1, text="掛號總數", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_total = tk.Label(f_col1, text="-", font=_clinic_num, bg="white", fg="#333333"); lbl_total.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=0, column=1, sticky="ns")

            f_col2 = tk.Frame(stats_frame, bg="white"); f_col2.grid(row=0, column=2, sticky="nsew", padx=2)
            tk.Label(f_col2, text="總完成數", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_comp_all = tk.Label(f_col2, text="-", font=_clinic_num, bg="white", fg="#1565C0"); lbl_comp_all.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=0, column=3, sticky="ns")

            f_col3 = tk.Frame(stats_frame, bg="white"); f_col3.grid(row=0, column=4, sticky="nsew", padx=2)
            tk.Label(f_col3, text="照光(跳號)完成", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_photo = tk.Label(f_col3, text="-", font=_clinic_num, bg="white", fg="#7B1FA2"); lbl_photo.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=0, column=5, sticky="ns")

            f_col4 = tk.Frame(stats_frame, bg="white"); f_col4.grid(row=0, column=6, sticky="nsew", padx=2)
            tk.Label(f_col4, text="候診(已報到)", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_waiting = tk.Label(f_col4, text="-", font=_clinic_num, bg="white", fg="#009688"); lbl_waiting.pack(anchor="center")

            tk.Frame(stats_frame, bg="#E0E0E0", height=1).grid(row=1, column=0, columnspan=7, sticky="ew", pady=6)

            # 列2：推估剩餘 | 歷史平均當前診號 | 上一時段關診時間
            t_est = tk.Frame(stats_frame, bg="white"); t_est.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=2)
            tk.Label(t_est, text="推估剩餘時間", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_est_remain = tk.Label(t_est, text="—", font=_clinic_num, bg="white", fg="#E65100"); lbl_est_remain.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=2, column=2, sticky="ns")

            t_hist = tk.Frame(stats_frame, bg="white"); t_hist.grid(row=2, column=3, columnspan=2, sticky="nsew", padx=2)
            tk.Label(t_hist, text="近月此刻平均診號", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_hist_light = tk.Label(t_hist, text="—", font=_clinic_num, bg="white", fg="#6A1B9A"); lbl_hist_light.pack(anchor="center")
            tk.Frame(stats_frame, width=1, bg="#E0E0E0", height=_clinic_sep_h).grid(row=2, column=5, sticky="ns")

            t_prev = tk.Frame(stats_frame, bg="white"); t_prev.grid(row=2, column=6, sticky="nsew", padx=2)
            tk.Label(t_prev, text="上一時段關診", font=_clinic_cap, bg="white", fg=_clinic_cap_fg, justify="center").pack(anchor="center", pady=(0, 2))
            lbl_prev_close = tk.Label(t_prev, text="—", font=_clinic_num, bg="white", fg="#5D4037"); lbl_prev_close.pack(anchor="center")

            # curr_avg: 背景計算用，不顯示於 UI
            lbl_curr_avg = tk.Label(card_frame, text="-", font=_clinic_num, bg="white", fg="#1976D2")
            # 故意不 pack/grid，保持不可見

            self.clinic_ui_elements.append({
                "light": lbl_light, "total": lbl_total, "waiting": lbl_waiting,
                "photo": lbl_photo, "comp_all": lbl_comp_all,
                "status": lbl_status, "card_bg": card_frame,
                "doc_name": lbl_doc_name,
                "slot_banner": lbl_slot,
                "curr_avg": lbl_curr_avg,
                "est_remain": lbl_est_remain,
                "hist_light": lbl_hist_light,
                "prev_sess_close": lbl_prev_close,
            })

        for idx in range(CLINIC_ROOM_COUNT):
            tc0 = resolve_clinic_reg64_time_code(self.clinic_display_mode_vars[idx].get())
            sb = self.clinic_ui_elements[idx]["slot_banner"]
            sb.config(text=reg64_slot_cn(tc0) or "—", fg=reg64_slot_label_color(tc0))

        self._prepaint_clinic_dynamic_state()

        if getattr(self, "_clinic_lights_poll_armed", False):
            self.root.after(80, self._clinic_tab_first_paint_refresh)
        else:
            self._start_clinic_lights_polling_once()

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
        with self._reg64_cache_lock:
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

    def _ensure_clinic_room_model_from_disk(self):
        """建立門診動態用的診間／時段變數（不依賴小工具分頁 UI），供背景輪詢使用。"""
        if getattr(self, "clinic_room_vars", None):
            return
        saved_settings = self.load_clinic_settings()
        saved_rooms, _changed = normalize_clinic_rooms(saved_settings.get("rooms"))
        saved_modes = saved_settings.get("time_modes")
        if not isinstance(saved_modes, list):
            saved_modes = []
        while len(saved_modes) < CLINIC_ROOM_COUNT:
            saved_modes.append("auto")
        while len(saved_rooms) < CLINIC_ROOM_COUNT:
            saved_rooms.append("")
        self.clinic_room_vars = [
            tk.StringVar(value=saved_rooms[j])
            for j in range(CLINIC_ROOM_COUNT)
        ]
        self.clinic_display_mode_vars = [
            tk.StringVar(value=_normalize_clinic_display_mode(saved_modes[j]))
            for j in range(CLINIC_ROOM_COUNT)
        ]
        if not hasattr(self, "clinic_timecode_combos"):
            self.clinic_timecode_combos = []
        if not hasattr(self, "clinic_ui_elements"):
            self.clinic_ui_elements = []

    def _start_clinic_lights_polling_once(self):
        if getattr(self, "_shutting_down", False):
            return
        self._ensure_clinic_room_model_from_disk()
        if getattr(self, "_clinic_lights_poll_armed", False):
            return
        self._clinic_lights_poll_armed = True
        self._update_clinic_lights_loop()

    def _clinic_tab_first_paint_refresh(self):
        """小工具分頁首次建置後立刻拉一次資料，讓卡片有初值且不與背景排程打架。"""
        if getattr(self, "_shutting_down", False):
            return
        cid = getattr(self, "clinic_loop_id", None)
        if cid:
            try:
                self.root.after_cancel(cid)
            except Exception:
                pass
            self.clinic_loop_id = None
        self._update_clinic_lights_loop()

    def _reg64_total_for_calendar_cell(self, doc_name, session_name):
        """回傳 (掛號總數字串含「人」, tag)；無則 None。供今日逾時列優先顯示門診動態人數。"""
        if not doc_name:
            return None
        with self._reg64_cache_lock:
            snap = self._reg64_public_snapshot.get((doc_name, session_name))
            lastg = self._reg64_last_good_total.get((doc_name, session_name))
        now_ts = time.time()
        if snap and (now_ts - snap.get("ts", 0)) <= 3 * 3600:
            return f"{snap['total']}人", "session_past"
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
        """reg64.cgi 為公開讀取；同一院區主機共用退避，減少雙診間並行打爆連線。"""
        if not hasattr(self, "_reg64_last_cache_hit"):
            self._reg64_last_cache_hit = False
        if not hasattr(self, "_reg64_last_backoff_skip"):
            self._reg64_last_backoff_skip = False
        self._reg64_last_cache_hit = False
        self._reg64_last_backoff_skip = False
        ck = ("reg64_html", target_url)
        ttl_s = max(5, int(getattr(self, "_reg64_dynamic_ttl_seconds", REG64_MICRO_CACHE_SECONDS)))
        hit = _cache_get(ck, ttl_s, evict_expired=False)
        if hit is not None:
            self._reg64_last_cache_hit = True
            return hit
        ok, remain = _source_backoff_allow(REG64_CMUH_BACKOFF_KEY)
        if not ok:
            stale = _cache_get(ck, REG64_STALE_CACHE_SECONDS, evict_expired=False)
            if stale is not None:
                logging.debug(f"[BACKOFF] reg64 use stale cache, remaining={remain:.1f}s")
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
            _source_backoff_success(REG64_CMUH_BACKOFF_KEY)
            return text
        except requests.exceptions.RequestException:
            delay, cnt = _source_backoff_fail(
                REG64_CMUH_BACKOFF_KEY,
                REG64_BACKOFF_BASE_SECONDS,
                REG64_BACKOFF_MAX_SECONDS,
            )
            logging.warning(f"[BACKOFF] reg64 fail {target_url}, fail={cnt}, delay={delay:.1f}s")
            stale = _cache_get(ck, REG64_STALE_CACHE_SECONDS, evict_expired=False)
            if stale is not None:
                logging.debug("[BACKOFF] reg64 request failed; using stale cache")
                self._reg64_last_backoff_skip = True
                return stale
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
            match = _RE_CLINIC_DOCTOR.search(page_text)  # [O16]
            if match: doc_name = match.group(1).strip()

            if "查無此診間的資料" in page_html:
                return {"light": "休", "total": "-", "waiting": "-", "completed": 0, "status": "休診", "doc_name": doc_name, "waiting_set": set(), "completed_set": set(), "reg64_time_code": time_code}

            # 已關診：常見為「診間目前燈號：99 (已關診)」或全形括號
            is_closed = bool(
                _RE_CLINIC_CLOSE_LINE.search(page_text)  # [O16]
                or _RE_CLINIC_CLOSED.search(page_text)
            )
            is_stopped = "(未開診)" in page_text
            
            close_time_str = ""
            if is_closed:
                time_match = _RE_CLINIC_END_TIME.search(page_text)  # [O16]
                if time_match:
                    raw_time = time_match.group(1) 
                    close_time_str = f"{raw_time[:2]}:{raw_time[2:]}" if len(raw_time) == 4 else raw_time

            light_num = "0"
            match_light = _RE_CLINIC_LIGHT_NUM.search(page_text)  # [O16]
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
        if getattr(self, "_shutting_down", False):
            return
        if not hasattr(self, 'clinic_trackers'):
            self.clinic_trackers = {}
        if not hasattr(self, '_clinic_dynamic_refresh_seconds'):
            self._clinic_dynamic_refresh_seconds = CLINIC_LIGHT_REFRESH_SECONDS

        now_gate = datetime.now()
        # [2026-07-13 使用者] 「半夜也監測」設定已移除；固定在 00:00–07:00 暫停 reg64（門診進度/現場
        # 人數），此時段不刷新。止掛提醒(reg52 掛號數)另有排程、全天候，不受此閘影響。
        if _reg64_clinic_quiet_hours(now_gate):
            nxt = _reg64_next_allowed_fetch_time(now_gate)
            delay_ms = max(int((nxt - datetime.now()).total_seconds() * 1000), 5_000)
            self.clinic_loop_id = self.root.after(delay_ms, self._update_clinic_lights_loop)
            return
        if getattr(self, "_clinic_lights_worker_running", False):
            logging.info("診間燈號上一輪仍在執行，延後下一次輪詢")
            self.clinic_loop_id = self.root.after(5_000, self._update_clinic_lights_loop)
            return

        rooms_to_check = []
        for i in range(CLINIC_ROOM_COUNT):
            code = self.clinic_room_vars[i].get().strip()
            mode = (
                self.clinic_display_mode_vars[i].get()
                if hasattr(self, "clinic_display_mode_vars") and i < len(self.clinic_display_mode_vars)
                else "auto"
            )
            rooms_to_check.append((code, mode))

        def run_update(rooms):
            source_timing = {"cache_hit_html": 0, "cache_hit_parse": 0, "cache_hit_reg64": 0, "backoff_skip": 0}
            abnormal_rooms = []
            current_timestamp = time.time()
            now = datetime.now()
            tc_auto = reg64_time_code_from_local_clock(now)
            prev_seg = getattr(self, "_clinic_reg64_auto_segment", None)
            reg64_segment_just_changed = prev_seg is not None and tc_auto != prev_seg
            if reg64_segment_just_changed:
                self.root.after(0, self._reset_clinic_display_modes_to_auto)
            self._clinic_reg64_auto_segment = tc_auto

            specs = []
            seen_spec_keys = set()
            duplicate_specs = set()
            for i, (room_code, configured_mode) in enumerate(rooms):
                if not room_code:
                    continue
                mode = configured_mode
                if reg64_segment_just_changed:
                    mode = "auto"
                tc_effective = resolve_clinic_reg64_time_code(mode, now)
                # [2026-06-19] 早診拖班:時段雖已前進,但前一節今天看過診且尚未關診 → 繼續輪前一節
                # (同一診間同時只有一節在看診 → 不增加負載),直到它真的關診才前進。
                tc_effective = self._overrun_effective_tc(room_code, tc_effective)
                spec_key = (str(room_code), str(tc_effective))
                if spec_key in seen_spec_keys:
                    duplicate_specs.add(spec_key)
                    continue
                seen_spec_keys.add(spec_key)
                specs.append((i, room_code, mode, tc_effective))

            duplicate_specs_key = tuple(sorted(duplicate_specs))
            if duplicate_specs_key != getattr(self, "_clinic_duplicate_rooms_notice", ()):
                self._clinic_duplicate_rooms_notice = duplicate_specs_key
                if duplicate_specs_key:
                    dup_text = ", ".join(f"{room}/{tc}" for room, tc in duplicate_specs_key)
                    logging.warning(f"診間輪詢略過重複設定: {dup_text}")

            def _fetch_reg64_pack(spec):
                i, room_code, mode, tc_effective = spec
                data = self.fetch_clinic_light_status(room_code, time_code=tc_effective)
                cache_hit = bool(getattr(self, "_reg64_last_cache_hit", False))
                backoff_skip = bool(getattr(self, "_reg64_last_backoff_skip", False))
                return i, room_code, mode, tc_effective, data, cache_hit, backoff_skip

            # 序向抓取：尖峰時並行 reg64 易觸發院方限制與連線錯誤
            packed_rows = []
            for si, spec in enumerate(specs):
                if si > 0:
                    time.sleep(0.4)
                packed_rows.append(_fetch_reg64_pack(spec))

            # [2026-06-22] 先逐診間記錄「本輪 reg64 是否可達」:可達 = 非 backoff 用舊快取
            # (stale fallback 代表 reg64 正在失敗)且非錯誤/逾時(TTL 內 cache 命中算可達,代表近
            # TTL 內連得到)。必須在下方處理迴圈用 root.after 排任何「錯誤診間是否隱藏」之前就把
            # 【所有診間】這輪的可達狀態設好 —— 否則 UI 執行緒可能搶在 worker 設好前就跑隱藏判斷
            # (race),連不上的診間排在前面時會讀到上一輪殘留而誤判。每輪逐診間覆寫;冷啟動 + 全網
            # 斷線時沒有任何診間可達(連舊快取都沒有 → 不是 cache 命中、會走 backoff/錯誤)→ 全 False。
            for _pk in packed_rows:
                _room_pk = str(_pk[1])
                _st = (_pk[4] or {}).get("status", "") if _pk[4] else ""
                _bs_pk = _pk[6]
                self._reg64_room_reachable[_room_pk] = (
                    not _bs_pk and "錯誤" not in _st and "逾時" not in _st)

            for pack in packed_rows:
                i, room_code, mode, tc_effective, data, cache_hit, backoff_skip = pack
                if cache_hit:
                    source_timing["cache_hit_reg64"] += 1
                if backoff_skip:
                    source_timing["backoff_skip"] += 1
                curr_session_i = reg64_slot_cn(tc_effective) or "晚上"

                tracker_key = f"{room_code}/{tc_effective}"
                status_check = data.get('status', '')
                if "錯誤" in status_check or "逾時" in status_check:
                    cached_state = self._get_clinic_dynamic_state(
                        room_code,
                        tc_effective,
                        session_cn=curr_session_i,
                    )
                    if cached_state:
                        tracker = self._restore_clinic_tracker_from_state(
                            cached_state,
                            curr_session_i,
                            current_timestamp,
                        )
                        with self._tracker_lock:
                            self.clinic_trackers.setdefault(tracker_key, tracker)
                        cached_result = dict(cached_state.get("last_result") or {})
                        cached_result.setdefault("doc_name", tracker.get("doc_name", ""))
                        cached_result.setdefault("reg64_time_code", str(tc_effective))
                        cached_result.setdefault("light", "--")
                        cached_result.setdefault("total", "-")
                        cached_result.setdefault("waiting", "-")
                        cached_result.setdefault("completed", 0)
                        display = cached_state.get("last_display") or {}

                        def update_cached_ui(
                            index=i,
                            result=cached_result,
                            track=tracker,
                            display_cache=display,
                        ):
                            # [2026-06-19] 先呼叫(最前面會擷取浮動快取、自身有 els 防護),
                            # 再 guard 後面額外 widget → 沒開過門診分頁也能餵浮動視窗。
                            self.update_single_clinic_ui(index, result, track, display_cache.get("curr_avg", "-"))
                            if index >= len(getattr(self, "clinic_ui_elements", ())):
                                return
                            ui = self.clinic_ui_elements[index]
                            self._smart_widget_config(ui["comp_all"], text=str(result.get("completed", 0)))
                            self._smart_widget_config(ui["est_remain"], text=display_cache.get("est_remain", "—"), fg="#E65100")
                            self._smart_widget_config(ui["hist_light"], text=display_cache.get("hist_light", "—"))
                            self._smart_widget_config(ui["prev_sess_close"], text=display_cache.get("prev_close", "—"))
                            self._smart_widget_config(ui["status"], text="保留上次快取，等待連線恢復", fg="#607D8B")

                        self.root.after(0, update_cached_ui)
                        source_timing["backoff_skip"] += 1
                        continue
                    abnormal_rooms.append(tracker_key)
                    logging.warning(f"[{room_code}] 資料抓取異常 ({status_check})，跳過本次計算以保護狀態。")
                    self.root.after(0, lambda idx=i, res=data: self.update_single_clinic_ui_error(idx, res))
                    continue

                # 新鮮成功時間戳已在上方 packed_rows 預掃時設好(在排任何隱藏判斷之前,避免 race)。
                data = self._refine_clinic_reg64_for_display(data, mode, now, tc_effective)
                self._update_reg64_public_cache(room_code, data)

                current_session_avg_str = "-"
                estimated_remain_str = "-"
                
                # [核心修正] 強制鎖定 tracker 狀態讀寫，解決 Race Condition
                with self._tracker_lock:
                    if tracker_key not in self.clinic_trackers:
                        cached_state = self._get_clinic_dynamic_state(
                            room_code,
                            tc_effective,
                            doc_name=data.get("doc_name"),
                            session_cn=curr_session_i,
                        )
                        if cached_state:
                            self.clinic_trackers[tracker_key] = self._restore_clinic_tracker_from_state(
                                cached_state,
                                curr_session_i,
                                current_timestamp,
                            )
                        else:
                            self.clinic_trackers[tracker_key] = self._new_clinic_tracker(
                                curr_session_i,
                                current_timestamp,
                            )
                    
                    tracker = self.clinic_trackers[tracker_key]
                    
                    try:
                        curr_doc = data.get('doc_name', '')
                        
                        is_doctor_changed = (curr_doc and tracker['doc_name'] and curr_doc != tracker['doc_name'])
                        is_session_changed = (tracker['session_period'] != curr_session_i)
                        # [2026-06-22] 跨日重置:記憶體 tracker 沒有日期概念,連續執行跨午夜時昨天的
                        # actual_closing_dt/had_any_activity 會殘留 → 今天早上誤判「已關診」。日期一變就清。
                        today_str = now.strftime("%Y/%m/%d")
                        is_new_day = tracker.get('date') != today_str

                        if is_doctor_changed or is_session_changed or is_new_day:
                            logging.info(f"[{room_code}] 偵測到換診/換時段/跨日，重置所有統計數據。")
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
                            tracker['is_ended'] = False
                            tracker['stable_since_ts'] = None
                            tracker['last_monitor_pair'] = None
                            # [2026-06-26 Codex] 換診/跨日重置要清 last_activity_ts,否則新診次的第一輪
                            # (is_first_run)因新加的 guard 不會覆寫它 → 會繼承上一節「過關診時間後的活動
                            # 時戳」而誤判拖班用 60 分門檻。
                            tracker['last_activity_ts'] = None
                        tracker['date'] = today_str

                        tracker['doc_name'] = curr_doc

                        # [2026-06-22] 早晨殘留盤面防呆 —— 必須在任何 tracker 統計被本輪 data 污染【之前】做:
                        # reg64 盤面在今天該時段開診前,可能還停留在上一個看診日同時段(已關診)。盤面說已關診,
                        # 但「今天稍早尚無活動(had_any_activity,跨日 reset 已清 False)+ 還沒到該時段正常關診
                        # 時間」→ 八成是殘留盤面 → 視為尚未開診,把顯示用 data 蓋成 pending;這樣後續 completed_set/
                        # first-run/關診偵測都看 pending,不會把昨天的看診號吃進今天 tracker、也不誤判已關診。
                        # had_any_activity 用「本輪尚未更新前」的值(就在下面幾行才會被本輪 completed/waiting 更新)。
                        if is_residual_stale_closed(
                                data.get('is_closed', False),
                                bool(data.get('is_stopped')) and bool(data.get('true_schedule_dayoff')),
                                tracker.get('had_any_activity'),
                                now < _session_boundary_datetime(curr_session_i, now)):
                            logging.info(
                                "[%s] 早晨殘留盤面(已關診但今天尚無活動、未到關診時間)→ 視為尚未開診",
                                room_code)
                            tracker['actual_closing_dt'] = None
                            data['is_closed'] = False
                            data['is_stopped'] = True
                            data['status'] = "尚未開診"
                            data['light'] = "--"
                            data['total'] = "-"
                            data['waiting'] = "-"
                            data['completed'] = 0
                            data['waiting_set'] = set()
                            data['completed_set'] = set()

                        current_completed_set = data.get('completed_set', set())
                        current_waiting_set = data.get('waiting_set', set())

                        waiting_count_ui = _clinic_int_count(data.get('waiting'), 0)
                        completed_count_ui = _clinic_int_count(data.get('completed'), 0)
                        if completed_count_ui > 0 or waiting_count_ui > 0:
                            tracker['had_any_activity'] = True

                        # [2026-05-22] 記 last_activity_ts — completed/waiting set 有變化才更新。
                        # [2026-06-26 Codex] 第一次觀測(is_first_run)只是建立基準、不算「進展」—— 否則
                        # 重啟/快取 miss 後遇到「其實早就停了」的診,會把初次快照(空集合→非空)誤當成
                        # boundary 後的進展,讓拖班 plateau 門檻誤用 60 分。基準不更新時戳,第二輪起真的有變才記。
                        if (not tracker['is_first_run']
                                and (current_completed_set != tracker.get('last_completed_set', set())
                                     or current_waiting_set != tracker.get('last_waiting_set', set()))):
                            tracker['last_activity_ts'] = current_timestamp

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

                            # 殘留盤面已在本輪稍早(任何 tracker 污染前)蓋成 pending(見上方 is_residual_stale_closed),
                            # 故此處 is_closed_page 已是 False、不會誤判已關診。
                            is_closed_page = data.get('is_closed', False)
                            is_stopped_page = bool(data.get('is_stopped')) and bool(data.get('true_schedule_dayoff'))
                            is_ended = False
                            boundary = _session_boundary_datetime(curr_session_i, now)

                            # [2026-06-22] 自我修復:盤面【現在】不是已關診/停診、且還沒到該時段正常關診時間
                            # (plateau 偵測本就只在過 boundary 後才跑)→ 此時段不可能已關診。主動清掉先前殘留/誤設
                            # 的 actual_closing_dt。不論殘留怎麼來的(跨日殘留/還原舊狀態/瞬時誤判),只要盤面現在
                            # 在看診、又還沒到關診時間,下一輪就清掉 → 解掉「明明在看診卻一直顯示已關診」
                            # (實機:101 早診 09:51 顯示已關診12:00)。盤面真的顯示已關診時不在此清(交下方處理)。
                            if (not is_closed_page and not is_stopped_page and now < boundary
                                    and tracker.get('actual_closing_dt') is not None):
                                logging.info("[%s] 盤面非關診且未到關診時間 → 清除殘留已關診標記", room_code)
                                tracker['actual_closing_dt'] = None

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
                                                ctn = _RE_DIGITS_ONLY.sub("", ct)  # [O16]
                                                if len(ctn) >= 4:
                                                    hh, mm = int(ctn[:2]), int(ctn[2:4])
                                                else:
                                                    raise ValueError()
                                            parsed_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                                        except Exception:
                                            parsed_dt = None
                                    # [2026-05-22 修] 用 last_activity_ts 取代網頁抓的應診時間結束。
                                    # 網頁 close_time 抓的是「應診時間：1700~2100」的 2100，
                                    # 是預定關診時間 (= 排班結束)，不是醫師實際關診時刻。
                                    # 若 tracker 有記到最後活動時間 (last completed/waiting 變化)，
                                    # 那更貼近真實關診時刻 (= 最後一位病人完成 ± 數秒)。
                                    activity_ts = tracker.get('last_activity_ts')
                                    if activity_ts:
                                        try:
                                            activity_dt = datetime.fromtimestamp(activity_ts)
                                            # 用兩者中較早的 (真實關診 < 排班結束 是常態)
                                            if parsed_dt and activity_dt < parsed_dt:
                                                logging.info(
                                                    "[close_time] 採用 last_activity_ts=%s (網頁 close_time=%s 較晚)",
                                                    activity_dt.strftime("%H:%M"),
                                                    parsed_dt.strftime("%H:%M"))
                                                parsed_dt = activity_dt
                                            elif not parsed_dt:
                                                parsed_dt = activity_dt
                                        except Exception:
                                            logging.debug("last_activity_ts 轉換失敗", exc_info=True)
                                    # 僅在能解析到關診時刻時記錄；否則維持 None，UI 顯示「已關診」不附時間
                                    tracker['actual_closing_dt'] = parsed_dt
                            elif not skip_plateau and tracker.get('had_any_activity'):
                                if now >= boundary:
                                    pair = (completed_count_ui, waiting_count_ui)
                                    if tracker.get('last_monitor_pair') != pair:
                                        # 進展有變(還在看診/拖班過了關診時間又動)→ 重置 plateau 計時,
                                        # 並清掉先前殘留的關診標記(明明還在動,不算已關診)。
                                        tracker['last_monitor_pair'] = pair
                                        tracker['stable_since_ts'] = current_timestamp
                                        tracker['actual_closing_dt'] = None
                                    else:
                                        ss = tracker.get('stable_since_ts')
                                        if ss is None:
                                            tracker['stable_since_ts'] = current_timestamp
                                            ss = current_timestamp
                                        # [2026-06-26] 確認拖班 = 最後一次看診進展(last_activity_ts)發生在「過關診
                                        # 時間之後」→ plateau 門檻拉長 30→60 分,避免把還在看的久病人/空檔誤判關診而
                                        # 從浮窗消失。在關診時間前就停的診(last_activity_ts < boundary)維持 30 分。
                                        # 用絕對時戳判定 → 自動不受跨日/跨節漏判或殘留影響(前一節/前一天的活動時戳
                                        # 必早於本節 boundary);也涵蓋「在 boundary 前後之間才進展」的邊界 case(Codex)。
                                        last_act = tracker.get('last_activity_ts')
                                        overran = (last_act is not None
                                                   and last_act >= boundary.timestamp())
                                        plateau_sec = (CLINIC_CLOSE_PLATEAU_SECONDS_OVERRUN
                                                       if overran else CLINIC_CLOSE_PLATEAU_SECONDS)
                                        if current_timestamp - ss >= plateau_sec:
                                            if tracker['actual_closing_dt'] is None:
                                                tracker['actual_closing_dt'] = datetime.fromtimestamp(ss)
                                            is_ended = True
                                else:
                                    tracker['stable_since_ts'] = None
                                    tracker['last_monitor_pair'] = None
                            if skip_plateau:
                                tracker['stable_since_ts'] = None
                                tracker['last_monitor_pair'] = None

                            # [2026-06-19] 持久化關診狀態供早診拖班(overrun)判定:actual_closing_dt 在
                            # 「網頁已關診但解析不到關診時刻」時會維持 None,故另存 is_ended 旗標。
                            tracker['is_ended'] = bool(is_ended)

                            should_save = False
                            if has_valid_completion and not is_ended: should_save = True 
                            if is_ended and not tracker['is_saved']: should_save = True

                            close_time_save = ""
                            if tracker.get('actual_closing_dt'):
                                close_time_save = tracker['actual_closing_dt'].strftime("%H:%M")
                            elif is_closed_page and (data.get('close_time') or '').strip():
                                close_time_save = (data.get('close_time') or '').strip()

                            save_durations = list(tracker['durations'])
                            traw = data.get('total')
                            total_reg_save = _clinic_int_count(traw, -1)
                            if total_reg_save < 0:
                                total_reg_save = None

                            want_metrics = bool(save_durations) and completed_count_ui > 0
                            want_closing = bool(is_ended and close_time_save)
                            if should_save and tracker['doc_name'] and (want_metrics or want_closing):
                                stat_submitted = self._submit_clinic_session_stat(
                                    room_code,
                                    tracker['doc_name'],
                                    completed_count_ui,
                                    save_durations,
                                    close_time_save,
                                    curr_session_i,
                                    total_reg_save,
                                    int(tracker.get('phototherapy_count', 0)),
                                )
                                if is_ended and stat_submitted:
                                    tracker['is_saved'] = True
                                
                    except Exception as e: 
                        logging.error(f"Tracking error for {room_code}: {e}", exc_info=True)

                    all_time_avg_str = self._calculate_all_time_avg(tracker['doc_name'], tracker['durations'])
                    track_copy = tracker.copy()

                # [新增] 若本診次尚無計時樣本，以歷史平均作為推估剩餘的備援
                if estimated_remain_str == "-":
                    _wc_fb = _clinic_int_count(data.get('waiting'), 0)
                    if _wc_fb == 0:
                        estimated_remain_str = "0分"
                    elif all_time_avg_str and all_time_avg_str not in ("-", ""):
                        try:
                            _ha_fb = float(all_time_avg_str)
                            if _ha_fb > 0:
                                _em_fb = _ha_fb * _wc_fb
                                if _em_fb > 120:
                                    estimated_remain_str = f"~{_em_fb/60:.1f}時"
                                else:
                                    estimated_remain_str = f"~{int(_em_fb)}分"
                        except (ValueError, TypeError):
                            pass

                sess_for_avg = reg64_slot_cn(data.get('reg64_time_code', '')) or "晚上"
                prev_sess_close_str = self._get_prev_session_closing_clock(
                    room_code, track_copy.get('doc_name'), sess_for_avg
                )
                _doc_for_hist = track_copy.get('doc_name', '')
                _light_val_raw = data.get('light', '')
                _is_active = (
                    _light_val_raw not in ('--', '休', '', '0')
                    and not data.get('is_closed')
                    and 'status' in data
                    and "休診" not in data.get('status', '')
                    and "停診" not in data.get('status', '')
                )
                if _is_active and _doc_for_hist and _light_val_raw:
                    try:
                        int(_light_val_raw)
                        self._save_clinic_light_sample(room_code, _doc_for_hist, sess_for_avg, _light_val_raw, now)
                    except (ValueError, TypeError):
                        pass
                hist_light_str = self._get_hist_avg_light(room_code, _doc_for_hist, sess_for_avg, now)

                self._persist_clinic_dynamic_state(
                    room_code,
                    tc_effective,
                    track_copy,
                    data,
                    current_session_avg_str,
                    estimated_remain_str,
                    hist_light_str,
                    prev_sess_close_str,
                )

                def update_ui(
                    index=i,
                    result=data,
                    c_avg=current_session_avg_str,
                    est=estimated_remain_str,
                    track=track_copy,
                    psc=prev_sess_close_str,
                    hl=hist_light_str,
                ):
                    # [2026-06-19] 先呼叫(它最前面會擷取浮動門診快取,且自身有 els 防護)
                    # → 即使使用者沒開過門診分頁(clinic_ui_elements 空)也能餵浮動視窗;
                    # 再 guard 後面只在有 UI 時才跑的額外 widget。
                    self.update_single_clinic_ui(index, result, track, c_avg)
                    if index >= len(getattr(self, "clinic_ui_elements", ())):
                        return
                    ui = self.clinic_ui_elements[index]

                    self._smart_widget_config(ui['comp_all'], text=str(result.get('completed', 0)))
                    self._smart_widget_config(ui['prev_sess_close'], text=psc)
                    self._smart_widget_config(ui['hist_light'], text=hl)
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
                        self._smart_widget_config(ui['est_remain'], text="—")
                        self._smart_widget_config(ui['hist_light'], text="—")
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
            # [2026-06-22] 07:00–00:00 一律 45-75 秒隨機輪詢(隨機避免固定節拍打爆院方限制)。
            # [MN-03/舊註解訂正] 舊註解稱「00:00–07:00 由 reg64_clinic_quiet_hours 暫停」——僅在
            # 使用者【關閉半夜監測】時才暫停(見上方 gate);預設半夜監測開啟時 00-07 仍會走到這裡。
            # 半夜監測開啟時 00-07 點放慢輪詢(多機夜間負載禮貌;白天不變)。本段在 bg_executor
            # worker 執行,用 datetime.now() 取時(勿讀 tk 變數);間隔邏輯抽成純函式以便測試。
            _now_hour = datetime.now().hour
            self._clinic_dynamic_refresh_seconds = _clinic_refresh_seconds(_now_hour)
            self._reg64_dynamic_ttl_seconds = _reg64_micro_ttl_seconds(_now_hour)
            if source_timing.get("backoff_skip", 0) > 0 or abnormal_rooms:
                now_ts = time.time()
                last_ts = float(getattr(self, "_reg64_abnormal_log_ts", 0.0))
                log_interval = 300.0 if source_timing.get("backoff_skip", 0) > 0 and not abnormal_rooms else 120.0
                if now_ts - last_ts >= log_interval:
                    self._reg64_abnormal_log_ts = now_ts
                    logging.warning(
                        f"[SOURCE_TIMING][reg64] {source_timing}, abnormal_rooms={abnormal_rooms}"
                    )

        def guarded_run_update(rooms):
            try:
                run_update(rooms)
            except Exception:
                # [MN-05] run_update 例外原本被丟進 future、done_callback 只認
                # RejectedExecutionError → 靜默吞掉、除錯全盲。這裡先記下再吞
                # (下一輪已在主緒排程,監測不中斷;僅需可見的錯誤軌跡)。
                logging.exception("診間燈號輪詢例外")
            finally:
                self._clinic_lights_worker_running = False

        def _handle_clinic_submit_rejected(fut):
            if fut.cancelled():
                rejected = True
            else:
                try:
                    rejected = isinstance(fut.exception(), RejectedExecutionError)
                except Exception:
                    rejected = False
            if rejected:
                logging.warning("診間燈號背景工作未啟動：背景佇列已滿")
                self._clinic_lights_worker_running = False

        # [核心修正] 投遞至執行緒池
        self._clinic_lights_worker_running = True
        clinic_future = self.bg_executor.submit(guarded_run_update, rooms_to_check)
        clinic_future.add_done_callback(_handle_clinic_submit_rejected)
        
        seconds = getattr(self, '_clinic_dynamic_refresh_seconds', 60)
        if seconds < 45:   # [2026-06-22] 下限 60→45,讓早上起跑窗的 45-75 秒不被夾回 60
            seconds = 45
        next_refresh_ms = seconds * 1000
        self.clinic_loop_id = self.root.after(next_refresh_ms, self._update_clinic_lights_loop)

    # [新增] 用於錯誤時的 UI 更新函式，只更新狀態文字，不影響數值
    def update_single_clinic_ui_error(self, index, result):
        # [2026-06-22 user 選「積極」] 浮動視窗:reg64 連線逾時/錯誤(無今日明確訊號)的診間,
        # 要同時滿足兩個條件才視為「今天真的沒這個診」(例 102)並隱藏,避免誤藏其實有診的診間:
        #   (1) 連續錯誤達門檻 FLOATING_ERROR_HIDE_STREAK(給暫時性瞬斷幾輪緩衝);且
        #   (2) 同一時間【其他診間連得上、正在顯示有診】(_floating_network_seems_up)→ 佐證
        #       reg64/網路是通的,那這一診持續連不上多半是它本來就沒診;全網斷線/冷啟動時
        #       沒有任何診間有資料 → 不隱藏(連線錯誤本身不是「沒診」的證據)。
        # 達標才以 error 旗標餵浮動視窗 → should_show_room 隱藏無醫師的錯誤診間;否則維持
        # pending 顯示。成功/有快取那輪會在 _capture_floating_status 把連續計數歸零。
        try:
            room = (self.clinic_room_vars[index].get().strip()
                    if index < len(self.clinic_room_vars) else "")
            if room:
                streak = self._floating_error_streak.get(room, 0) + 1
                self._floating_error_streak[room] = streak
                if (streak >= FLOATING_ERROR_HIDE_STREAK
                        and self._floating_network_seems_up(room)):
                    self._capture_floating_status(index, result, {})
        except Exception:
            logging.debug("[浮動門診] 錯誤狀態擷取失敗", exc_info=True)
        els = getattr(self, "clinic_ui_elements", None)
        if not els or index >= len(els):
            return
        ui = els[index]
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
        for k in ("prev_sess_close", "est_remain", "hist_light"):
            if k in ui:
                self._smart_widget_config(ui[k], text="—")

    # [新增] 獨立的 UI 更新函數，方便在「停止查詢」時也能呼叫
    def update_single_clinic_ui(self, index, result, tracker, c_avg="-"):
        # [2026-06-19] 浮動門診動態:在最前面就擷取(不依賴門診分頁是否已建好 UI)。
        # 之前掛在函式尾端,但若使用者沒開過門診分頁,clinic_ui_elements 為空 → 下面
        # 提早 return → 浮動視窗永遠拿不到資料而顯示 "?"。改放最前面、與 UI 無關。
        try:
            self._capture_floating_status(index, result, tracker)
        except Exception:
            pass
        els = getattr(self, "clinic_ui_elements", None)
        if not els or index >= len(els):
            return
        ui = els[index]

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

    # ─── 浮動門診動態小視窗 ────────────────────────────────────────────
    @staticmethod
    def _normalize_widget_mode(mode):
        """門診動態顯示方式正規化:只認 off / floating。
        [2026-06-19] 邊緣常駐條(appbar)已移除(保留空間會與強制近全螢幕的醫囑系統衝突)
        → 舊設定 "appbar" 一律遷移為 "floating"。"""
        m = str(mode).strip().lower() if mode is not None else "off"
        if m == "appbar":
            return "floating"
        return m if m in ("off", "floating") else "off"

    def _load_floating_clinic_settings(self):
        defaults = {"mode": "off", "opacity": 0.85, "geometry": ""}
        try:
            cfg = load_json_dict(
                get_conf_path('floating_clinic_settings.json'), dict(defaults))
        except Exception:
            logging.debug("[浮動門診] 讀取設定失敗", exc_info=True)
            cfg = dict(defaults)
        # 欄位型別正規化:設定檔可能被手改成壞值(opacity=null/字串),__init__ 會直接
        # float() → 不正規化會在建構期就炸掉整個程式。clamp_opacity 對壞值回 0.85。
        from cmuh_common.floating_clinic import clamp_opacity
        cfg["opacity"] = clamp_opacity(cfg.get("opacity"))
        # 顯示方式:相容舊版只有 "enabled" 布林的設定檔 → 推導 mode;舊 "appbar" → "floating"。
        _mode = cfg.get("mode")
        if _mode in ("off", "floating", "appbar"):
            _mode = self._normalize_widget_mode(_mode)   # appbar 一律遷移為 floating
        else:
            _mode = "floating" if bool(cfg.get("enabled", False)) else "off"
        cfg["mode"] = _mode
        if not isinstance(cfg.get("geometry"), str):
            cfg["geometry"] = ""
        return cfg

    def _save_floating_clinic_settings(self):
        try:
            cfg = {
                "mode": self._normalize_widget_mode(self.clinic_widget_mode.get()),
                "opacity": round(float(self.floating_clinic_opacity.get()), 2),
                "geometry": self._floating_clinic_settings.get("geometry", ""),
            }
            self._floating_clinic_settings = cfg
            _atomic_write_json(get_conf_path('floating_clinic_settings.json'), cfg)
        except Exception:
            logging.debug("[浮動門診] 儲存設定失敗", exc_info=True)

    def _on_floating_clinic_geometry(self, geometry):
        # 視窗關閉/銷毀時回呼;先存起來,下次 save 時寫檔
        self._floating_clinic_settings["geometry"] = geometry

    def _on_floating_clinic_user_closed(self):
        # 使用者按了視窗右上角 X → 顯示方式改 off、存檔、銷毀視窗
        try:
            self.clinic_widget_mode.set("off")
        except Exception:
            pass
        self._save_floating_clinic_settings()
        self._close_floating_clinic()

    def _open_floating_clinic(self):
        if getattr(self, "_shutting_down", False):
            return  # 結束中不可再開窗(延後排程的回呼若在清理後觸發)
        if getattr(self, "floating_clinic_win", None) is not None:
            try:
                if self.floating_clinic_win.exists():
                    return
            except Exception:
                pass
        try:
            from cmuh_common import floating_clinic
            self.floating_clinic_win = floating_clinic.ClinicFloatingWindow(
                self.root,
                opacity=float(self.floating_clinic_opacity.get()),
                geometry=self._floating_clinic_settings.get("geometry", ""),
                on_close=self._on_floating_clinic_user_closed,
                on_geometry_change=self._on_floating_clinic_geometry,
            )
            self._floating_clinic_tick()
        except Exception:
            logging.debug("[浮動門診] 開啟視窗失敗", exc_info=True)
            self.floating_clinic_win = None

    def _close_floating_clinic(self):
        if getattr(self, "floating_clinic_tick_id", None):
            try:
                self.root.after_cancel(self.floating_clinic_tick_id)
            except Exception:
                pass
            self.floating_clinic_tick_id = None
        if getattr(self, "floating_clinic_win", None):
            try:
                self.floating_clinic_win.destroy()
            except Exception:
                pass
            self.floating_clinic_win = None

    def _floating_clinic_tick(self):
        if getattr(self, "_shutting_down", False):
            return
        win = getattr(self, "floating_clinic_win", None)
        try:
            if not win or not win.exists():
                return
        except Exception:
            return
        try:
            # [2026-06-19] 直接顯示輪詢到的「正在看診中的時段」(含早診拖班,見
            # _collect_widget_room_status / _overrun_effective_tc);已關診的診間會被隱藏。
            win.update_rooms(self._collect_widget_room_status())
            win.lift_to_top()
        except Exception:
            logging.debug("[浮動門診] tick 更新失敗", exc_info=True)
        finally:
            # 永遠重新排程,讓暫時性錯誤不會殺掉整個迴圈
            try:
                if not getattr(self, "_shutting_down", False) and win and win.exists():
                    self.floating_clinic_tick_id = self.root.after(
                        15000, self._floating_clinic_tick)
            except Exception:
                pass

    def _apply_clinic_widget_mode(self):
        """依目前選的顯示方式(off/floating)開關浮動視窗。"""
        if getattr(self, "_shutting_down", False):
            return  # 結束中:延後排程的此回呼若在 _cleanup_for_exit 後才觸發,不可再開窗
        mode = self._normalize_widget_mode(self.clinic_widget_mode.get())
        try:
            if mode == "floating":
                self._open_floating_clinic()
            else:  # off
                self._close_floating_clinic()
        except Exception:
            logging.debug("[門診小工具] 切換顯示方式失敗", exc_info=True)
        self._save_floating_clinic_settings()

    def _set_floating_clinic_opacity(self, *args):
        try:
            win = getattr(self, "floating_clinic_win", None)
            if win and win.exists():
                win.set_opacity(float(self.floating_clinic_opacity.get()))
            self._save_floating_clinic_settings()
        except Exception:
            logging.debug("[門診小工具] 調整透明度失敗", exc_info=True)

    def _overrun_effective_tc(self, room_code, tc):
        """早診/午診拖班的有效輪詢時段:讀「所有更早時段」tracker 狀態(最早→最晚),委派純函式
        overrun_effective_time_code 判定(見其 docstring)。純讀 tracker、不增加負載。

        關診以 is_ended 旗標為準(網頁已關診但解析不到關診時刻時 actual_closing_dt 仍為 None,
        故不能只看 actual_closing_dt,否則會卡住一直拖)。"""
        try:
            tc_i = int(tc)
        except (TypeError, ValueError):
            return tc
        if tc_i <= 1:
            return tc   # 早上(1)沒有更早時段可拖,免讀 tracker
        # 先讀 in-memory tracker(最即時)。鎖內只讀、不呼叫別的鎖(避免巢狀)。
        in_mem = {}
        try:
            with self._tracker_lock:
                for s in range(1, tc_i):   # 最早 → 最晚
                    t = self.clinic_trackers.get(f"{room_code}/{s}")
                    if t is not None:
                        in_mem[s] = (
                            bool(t.get('had_any_activity')),
                            bool(t.get('is_ended') or t.get('actual_closing_dt')))
        except Exception:
            return tc
        # [2026-06-26 user] in-memory 沒有的早時段 → 從持久化狀態(clinic_dynamic_state.json)補。
        # 下午【重啟】程式時,早上的 tracker 還沒在本次行程建起來(本行程只會輪詢目前時段)→ 少了這層
        # 補償就讀不到「早診今天看過診且還沒關」→ overrun 判定不到 → 早診拖班直接被隱藏(使用者實機)。
        # cache 載入時已用日期過濾(只今日),故不會誤用昨日;讀不到 → (False, False)。
        earlier = []
        for s in range(1, tc_i):
            if s in in_mem:
                had, closed = in_mem[s]
            else:
                had, closed = self._persisted_session_overrun_state(room_code, s)
            earlier.append((s, had, closed))
        return overrun_effective_time_code(tc, earlier)

    def _persisted_session_overrun_state(self, room_code, s):
        """讀持久化門診動態狀態裡某早時段的 (had_activity, closed),供 overrun 在【重啟後早上 tracker
        還沒建】時判定早診是否還在拖。cache 只含今日;讀不到/壞值 → (False, False)。純讀、取自己的鎖。"""
        try:
            key = self._clinic_dynamic_state_key(room_code, s)
            with self._clinic_dynamic_state_lock:
                st = self._clinic_dynamic_state_cache.get(key)
            if not isinstance(st, dict):
                return (False, False)
            # [FC-02] 只信「今日」的持久化狀態。cache 只在 load 時整批以日期過濾、且只在 persist 時
            # prune_states_for_today;若程式【跨午夜連續執行】,早診昨天的 state(had_activity 但當時
            # 崩潰沒落 is_ended)會殘留在 cache 裡 → 今日誤判早診仍在拖班、把昨天早診當「還在看」。
            # 與權威讀取器 _get_clinic_dynamic_state → state_matches 的日期守門(clinic_state.py:105)
            # 一致:state.date != 今日 → 視同無早診狀態,回 (False, False)。
            if st.get("date") != self._clinic_dynamic_today_str():
                return (False, False)
            return (bool(st.get('had_any_activity')),
                    bool(st.get('is_ended') or st.get('actual_closing_dt')))
        except Exception:
            return (False, False)

    def _collect_widget_room_status(self):
        """收集要餵給浮動視窗的各診間狀態:直接顯示輪詢到的「正在看診中的時段」
        (含早診拖班 → 持續顯示早診直到關診)。已關診的診間由 should_show_room 隱藏。"""
        from cmuh_common import floating_clinic
        rooms = []
        for i in range(CLINIC_ROOM_COUNT):
            try:
                code = self.clinic_room_vars[i].get().strip()
            except Exception:
                continue
            if not code:
                continue
            rs = self._floating_status_by_room.get(code)
            if rs is None:
                rs = floating_clinic.RoomStatus(room=code, light="")  # 還沒輪到 → pending
            rooms.append(rs)
        return rooms

    def _floating_network_seems_up(self, exclude_room):
        """是否有【其他】診間本輪 reg64 可達(見 _update_clinic_lights_loop 預掃 _reg64_room_reachable:
        非 backoff stale fallback、非錯誤/逾時)→ 佐證 reg64/網路正常,那這一診持續連不上多半是它
        今天真的沒診。冷啟動 + 全網斷線時所有診間都不可達(連舊快取都沒得命中)→ 回 False → 不把
        連不上的診間誤藏。
        [關鍵] 判定用『可達』(會排除 backoff stale fallback)而非『畫面上還顯示著』:斷線時其他診間
        雖仍顯示舊快取,但它們本輪是 backoff stale → 不算可達(Codex 第 3 輪指出的盲點)。"""
        exclude = str(exclude_room)
        # [FC-03 audit 2026-07-12] 讀端快照:bg worker 會寫此 dict,主緒直接迭代可能 dict-changed-size。
        for code, reachable in list(self._reg64_room_reachable.items()):
            if code != exclude and reachable:
                return True
        return False

    def _capture_floating_status(self, index, result, tracker):
        # [2026-06-19] 無論浮動視窗開沒開都快取(成本極小:只是組一個 dataclass),
        # 這樣使用者一打開視窗就有最新資料,不會卡在前一輪 60-90 秒的 "?"。以診間號為 key。
        try:
            room = (self.clinic_room_vars[index].get().strip()
                    if index < len(self.clinic_room_vars) else "")
            if not room:
                return
            slot = reg64_slot_cn(result.get("reg64_time_code", "")) or ""
            doctor = result.get("doc_name") or tracker.get("doc_name", "")
            status_txt = result.get("status", "") or ""
            error = ("錯誤" in status_txt) or ("逾時" in status_txt)
            stopped = (bool(result.get("is_stopped"))
                       or ("尚未開診" in status_txt) or ("休診" in status_txt))
            closed = bool(result.get("is_closed")) or bool(tracker.get("actual_closing_dt"))
            try:
                waiting = int(result.get("waiting"))
            except (TypeError, ValueError):
                waiting = None
            light = str(result.get("light", "") or "")
            # 這一輪有成功/有快取(非 error)→ 連續錯誤計數歸零:有診的診間遇到暫時性瞬斷後
            # 恢復,就不會被先前累積的錯誤次數誤判成「沒診」而隱藏。
            if not error:
                self._floating_error_streak[room] = 0
            from cmuh_common import floating_clinic
            self._floating_status_by_room[room] = floating_clinic.RoomStatus(
                room=room,
                slot=slot,
                doctor=doctor,
                light=light,
                waiting=waiting,
                closed=closed,
                stopped=stopped,
                error=error,
                fetched=True,  # 已從 reg64 查到資料 → 浮動視窗才會依「有無醫師」決定隱藏
            )
        except Exception:
            logging.debug("[浮動門診] 擷取狀態失敗", exc_info=True)

    def _get_last_closing_time(self, doc_name, weekday_int, session_str):
        with self._history_lock:
            rows = list(self.history_cache)
        return _history_last_closing_time(
            rows, doc_name, weekday_int, session_str, _canonical_clinic_session_str)

    def _get_prev_session_closing_clock(self, room_code, doc_name, curr_session_cn):
        """今日、同診間、同醫師之「上一時段」關診時間 (HH:MM)。"""
        curr_c = _canonical_clinic_session_str(curr_session_cn)
        prev_s = _canonical_clinic_session_str(_prev_session_cn(curr_c))
        today_s = date.today().strftime("%Y/%m/%d")
        with self._history_lock:
            rows = list(self.history_cache)
        return _history_prev_session_closing_clock(
            rows, room_code, doc_name, prev_s, today_s,
            _canonical_clinic_session_str)

    def _monthly_slot_metric_avgs(self, doc_name, room_code, session_cn):
        """近 CLINIC_METRIC_HISTORY_DAYS 日、同診間／時段／醫師之掛號、完成、照光平均。"""
        cutoff = date.today() - timedelta(days=CLINIC_METRIC_HISTORY_DAYS)
        with self._history_lock:
            rows = list(self.history_cache)
        return _history_monthly_slot_metric_avgs(
            rows, doc_name, room_code, session_cn, cutoff,
            _canonical_clinic_session_str)

# --- [新增] 歷史燈號樣本（三分鐘桶）---
    def _save_clinic_light_sample(self, room_code, doc_name, session_cn, light_val, now=None):
        """將目前燈號記錄到歷史檔案（每3分鐘一個時間桶）。"""
        if now is None:
            now = datetime.now()
        session_key = _canonical_clinic_session_str(session_cn)
        file_path = get_conf_path('clinic_light_history.json')
        data = load_json_dict(file_path, {}, merge_defaults=False)
        data, changed = record_light_sample(
            data,
            room_code=room_code,
            doc_name=doc_name,
            session_key=session_key,
            light_val=light_val,
            when=now,
            retain_days=max(60, CLINIC_LIGHT_HISTORY_DAYS + 7),
        )
        if not changed:
            return
        try:
            # [perf r5] clinic_light_history 是純機器讀寫的大型快取(~220KB，每次門診輪詢
            # 每診間寫一次)，沒人會手看。改 compact(indent=None + 無空白分隔)可把 json.dump
            # 從 ~6ms 降到 ~1ms、檔案砍近半，fsync 位元組數也減半。讀端 safe_load_json 與
            # 格式無關，round-trip 完全等價。其餘小型人讀設定檔維持預設 indent=4。
            _atomic_write_json(file_path, data, indent=None, separators=(",", ":"))
        except Exception:
            pass

    def _get_hist_avg_light(self, room_code, doc_name, session_cn, now=None):
        """回傳近月同時刻門診進度均值；優先取同星期幾，樣本不足時退回全月。"""
        if not room_code or not doc_name:
            return "—"
        if now is None:
            now = datetime.now()
        session_key = _canonical_clinic_session_str(session_cn)
        file_path = get_conf_path('clinic_light_history.json')
        data = load_json_dict(file_path, {}, merge_defaults=False)
        return historical_light_average(
            data,
            room_code=room_code,
            doc_name=doc_name,
            session_key=session_key,
            when=now,
            history_days=CLINIC_LIGHT_HISTORY_DAYS,
            window_minutes=CLINIC_LIGHT_HISTORY_WINDOW_MINUTES,
        )

# --- [新增] 計算統計數據並存檔 ---
    def _submit_clinic_session_stat(self, room_code, doc_name, completed_count, durations, closing_time_str="", session_str=None, total_reg=None, phototherapy=0):
        """同診間、醫師、時段僅保留一筆待寫入工作，避免輪詢期間重複堆積。"""
        pending_key = (str(room_code), str(doc_name), str(session_str or ""))
        with self._clinic_stat_pending_lock:
            if pending_key in self._clinic_stat_pending_keys:
                logging.debug("診間統計仍在寫入，略過重複提交: %s", pending_key)
                return False
            self._clinic_stat_pending_keys.add(pending_key)

        try:
            future = self.bg_executor.submit(
                self._save_clinic_session_stat,
                room_code,
                doc_name,
                completed_count,
                durations,
                closing_time_str,
                session_str,
                total_reg,
                phototherapy,
            )
        except RuntimeError:
            with self._clinic_stat_pending_lock:
                self._clinic_stat_pending_keys.discard(pending_key)
            logging.warning("診間統計背景工作未啟動：executor 已關閉")
            return False

        def _release_pending_key(fut):
            with self._clinic_stat_pending_lock:
                self._clinic_stat_pending_keys.discard(pending_key)
            try:
                rejected = fut.cancelled() or isinstance(fut.exception(), RejectedExecutionError)
            except Exception:
                rejected = False
            if rejected:
                logging.warning("診間統計背景工作未啟動：背景佇列已滿")

        future.add_done_callback(_release_pending_key)
        try:
            return not (
                future.cancelled()
                or (future.done() and isinstance(future.exception(), RejectedExecutionError))
            )
        except Exception:
            return True

    def _save_clinic_session_stat(self, room_code, doc_name, completed_count, durations, closing_time_str="", session_str=None, total_reg=None, phototherapy=0):
        today_str = date.today().strftime("%Y/%m/%d")
        session = _canonical_clinic_session_str(
            session_str or reg64_slot_cn(reg64_time_code_from_local_clock()) or "晚上"
        )

        file_path = get_conf_path('clinic_stats_history.json')

        with self._history_lock:
            history_data = load_json_list(file_path, [])
            history_data, changed = upsert_session_stat(
                history_data,
                today_str=today_str,
                week_str=date.today().strftime("%W"),
                room_code=room_code,
                doc_name=doc_name,
                completed_count=completed_count,
                durations=durations,
                session=session,
                closing_time=closing_time_str,
                total_reg=total_reg,
                phototherapy=phototherapy,
                canonical_session=_canonical_clinic_session_str,
                match_room=True,
                allow_empty_sample=True,
            )
            if not changed:
                return

            try:
                _atomic_write_json(file_path, history_data)
                self.history_cache = history_data
                self._avg_history_cache = {}
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
            mode = (
                self.clinic_display_mode_vars[room_index].get()
                if hasattr(self, "clinic_display_mode_vars") and room_index < len(self.clinic_display_mode_vars)
                else "auto"
            )
            tracker_key = f"{room_code}/{resolve_clinic_reg64_time_code(mode, datetime.now())}"
            
            # 防呆: 檢查追蹤器是否存在
            if not room_code or not hasattr(self, 'clinic_trackers') or (
                tracker_key not in self.clinic_trackers and room_code not in self.clinic_trackers
            ):
                messagebox.showwarning("無法重置", "目前沒有該診間的追蹤資料，無法執行重置。")
                return

            tracker = self.clinic_trackers.get(tracker_key) or self.clinic_trackers[room_code]
            doc_name = tracker.get('doc_name', '')
            
            # 防呆: 檢查是否已抓到醫師姓名 (因為刪除歷史紀錄依賴醫師姓名)
            if not doc_name:
                messagebox.showwarning("無法重置", "目前尚未偵測到醫師姓名，無法執行針對該醫師的歷程清除。")
                return

            # 確認對話框
            if not messagebox.askyesno("確認重置", f"確定要重置 [{doc_name}] 的所有時間統計嗎？\n\n這將會執行：\n1. 清除目前的平均時間與照光計數\n2. 刪除該醫師所有近一月統計相關歷史紀錄 (JSON檔案)"):
                return

# --- 1. 重置目前狀態 (記憶體) ---
            # [stability r5] 與背景燈號 worker(run_update 全程持 _tracker_lock 改寫同一
            # tracker)序列化：避免 main thread 在此清空 dict/set 的同時，worker 正在迭代
            # last_waiting_set / 對 patient_checkin_times 做 membership+del，觸發 KeyError /
            # 'dict changed size' 殺背景緒，或剛重置又被舊計算寫回(lost update)。
            # 鎖內重新取 tracker 引用(askyesno 對話框可能等了很久，原引用可能過時)。
            with self._tracker_lock:
                tracker = (self.clinic_trackers.get(tracker_key)
                           or self.clinic_trackers.get(room_code))
                if tracker is not None:
                    tracker['durations'] = []
                    tracker['waiting_durations'] = []
                    tracker['last_completed_set'] = set()
                    tracker['last_waiting_set'] = set()
                    tracker['last_valid_completion_time'] = time.time()
                    tracker['first_valid_skipped'] = False
                    tracker['is_first_run'] = True
                    tracker['had_any_activity'] = False
                    tracker['stable_since_ts'] = None
                    tracker['last_monitor_pair'] = None
                    tracker['last_activity_ts'] = None  # [2026-06-26 Codex] 重置清活動時戳,別讓拖班門檻誤繼承
                    tracker['actual_closing_dt'] = None
                    tracker['phototherapy_count'] = 0
                    # [新增] 重置等待時間相關
                    tracker['patient_checkin_times'] = {}

            # _clear_clinic_dynamic_state 用不同鎖(_clinic_dynamic_state_lock)且含檔案 I/O，
            # 留在 _tracker_lock 外，避免持鎖做磁碟 I/O / 巢狀鎖。
            self._clear_clinic_dynamic_state(
                room_code,
                resolve_clinic_reg64_time_code(mode, datetime.now()),
                doc_name,
            )
            
            logging.info(f"[{room_code}] 目前平均時間與計數已重置。")

            # --- 2. 重置長期紀錄 (檔案) ---
            file_path = get_conf_path('clinic_stats_history.json')
            try:
                # [stability] 讀-改-寫 + 更新快取整段包進 _history_lock，與背景的
                # _save_clinic_session_stat(同樣持 _history_lock)序列化，避免兩者
                # 交錯造成 lost-update / history 資料回退。
                with self._history_lock:
                    history = load_json_list(file_path, [])
                    new_history = remove_doctor_history(history, doc_name)

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
                self._avg_history_cache[cache_key] = historical_duration_totals(
                    self.history_cache, doc_name, cutoff)
            totals = self._avg_history_cache[cache_key]

        return all_time_average_text(totals, current_durations)

# --- [新增] 讀取門診動態設定 ---
    # --- [新增] 讀取門診動態設定 ---
    def load_clinic_settings(self):
        # [修改] 預設更新頻率改為 60 秒 (符合您的需求)
        default_settings = {"rooms": list(DEFAULT_CLINIC_ROOMS), "time_modes": ["auto"] * CLINIC_ROOM_COUNT}
        file_path = get_conf_path('clinic_settings.json')
        settings = load_json_dict(file_path, default_settings)
        rooms, changed = normalize_clinic_rooms(settings.get("rooms"))
        settings["rooms"] = rooms
        if changed:
            try:
                _atomic_write_json(file_path, settings)
                logging.info("門診動態診間設定已遷移為: %s", rooms)
            except Exception:
                logging.warning("門診動態診間設定遷移寫回失敗", exc_info=True)
        return settings

    # --- [新增] 儲存門診動態設定 ---
    def save_clinic_settings(self):
        try:
            rooms = [var.get().strip() for var in self.clinic_room_vars]
            time_modes = [
                _normalize_clinic_display_mode(self.clinic_display_mode_vars[j].get())
                for j in range(len(self.clinic_room_vars))
            ]
            duplicate_pairs = sorted({
                (room, mode)
                for room, mode in zip(rooms, time_modes)
                if room and sum(1 for r, m in zip(rooms, time_modes) if r == room and m == mode) > 1
            })
            data = {
                "rooms": rooms,
                "time_modes": time_modes,
            }
            _atomic_write_json(get_conf_path('clinic_settings.json'), data)
            if duplicate_pairs:
                dup_text = ", ".join(f"{room}/{mode}" for room, mode in duplicate_pairs)
                logging.warning(f"診間設定含重複診號，輪詢時將自動略過重複項目: {dup_text}")
                if hasattr(self, "status_text"):
                    self.status_text.set(f"狀態: 診間 {dup_text} 重複，輪詢時會自動略過重複項目")
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
        shorten_future = self.bg_executor.submit(self._run_url_shortener, long_url)

        def _handle_shorten_submit_rejected(fut):
            try:
                rejected = fut.cancelled() or isinstance(fut.exception(), RejectedExecutionError)
            except Exception:
                rejected = False
            if not rejected or getattr(self, '_shutting_down', False):
                return
            logging.warning("縮網址背景工作未啟動：背景佇列已滿")

            def _reset_shorten_ui():
                self.shorten_btn.config(state="normal")
                self.url_status_label.config(text="背景忙碌，請稍後再試", foreground="red")

            self._run_on_ui_thread(_reset_shorten_ui)

        shorten_future.add_done_callback(_handle_shorten_submit_rejected)

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
        if threading.current_thread() is not threading.main_thread():
            if getattr(self, "_shutting_down", False):
                return
            queued_doctors = list(specific_doctors) if specific_doctors is not None else None
            self.root.after(0, lambda: self._trigger_refresh(is_manual=is_manual, specific_doctors=queued_doctors))
            return

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
            # [stability r4] 在 main thread 同步搶下單飛旗標(原本只在 worker 內 9137 才設，
            # 而 worker 是 bg_executor 非同步才跑到那行)。否則 submit 後、worker 設旗標前的
            # 空窗內，下一個 _trigger_refresh 讀到 False→跳過去重→重複 submit 同一刷新，
            # 對掛號站送雙倍請求、惡化 backoff。同步設定後同 signature 會被 9073 去重正確攔下。
            self._refresh_worker_running = True

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
            self._refresh_worker_running = True  # 冪等：旗標已在 main thread 同步設過(見上)
            try:
                batches = partition_doctors_for_refresh_batches(doctors_to_check)
                for bi, batch in enumerate(batches):
                    futures = []
                    batch_workers = max(1, min(len(batch), 6))
                    with ThreadPoolExecutor(
                        max_workers=batch_workers,
                        thread_name_prefix="RefreshBatch",
                    ) as refresh_pool:
                        for doctor_config in batch:
                            worker_config = dict(doctor_config)
                            doc_no = str(worker_config.get("doc_no", ""))
                            doc_name = str(worker_config.get("name", ""))
                            with self._doctor_data_lock:
                                cached_data = self.all_doctors_data.get(doc_no) or self.all_doctors_data.get(doc_name)
                                if _appointments_data_count(cached_data) > 0:
                                    worker_config["_cached_appointments"] = deepcopy(cached_data)
                            worker_config["_is_manual_refresh"] = bool(is_manual)
                            future = refresh_pool.submit(check_appointment_count, self.ui_queue, worker_config)
                            futures.append(future)
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
                        self._trigger_refresh(False)

                self.root.after(0, _on_refresh_worker_done)

        def _handle_refresh_submit_rejected(fut):
            if fut.cancelled():
                rejected = True
            else:
                try:
                    rejected = isinstance(fut.exception(), RejectedExecutionError)
                except Exception:
                    rejected = False
            if not rejected:
                return
            logging.warning("掛號刷新背景工作未啟動：背景佇列已滿")

            def _reset_rejected_refresh():
                with self._refresh_queue_lock:
                    self._active_refresh_signature = None
                    queued_request = self._queued_refresh_requests.popleft() if self._queued_refresh_requests else None
                    if queued_request is not None:
                        self._queued_refresh_signatures.discard(queued_request[2])
                self._refresh_worker_running = False
                self._cancel_pending_refresh_tick_ui()
                self.refresh_button.config(state="normal")
                self.status_text.set("狀態: 背景佇列忙碌，刷新稍後重試")
                if queued_request is not None:
                    self._trigger_refresh(queued_request[0], queued_request[1])

            if threading.current_thread() is threading.main_thread():
                _reset_rejected_refresh()
            elif not getattr(self, "_shutting_down", False):
                self.root.after(0, _reset_rejected_refresh)

        refresh_future = self.bg_executor.submit(run_parallel_checks)
        refresh_future.add_done_callback(_handle_refresh_submit_rejected)

    def _on_watchdog_toggle(self):
        """切換 watchdog master_enabled 並寫回 settings/watchdog_config.json。"""
        try:
            from cmuh_common.watchdog_core import (
                load_config as _wd_load,
                CONFIG_PATH as _wd_cfg_path,
            )
            new_val = bool(self.watchdog_enabled_var.get())
            cfg = _wd_load()
            cfg["master_enabled"] = new_val
            _atomic_write_json(str(_wd_cfg_path), cfg, indent=2)
            state = "啟用" if new_val else "停用"
            logging.info("[watchdog] master_enabled 改為 %s (由設定 UI 切換)", new_val)
            self.status_text.set(
                f"狀態: watchdog 已{state}（下次 30s 內生效；不需重啟主程式）")
        except Exception as e:
            logging.error("watchdog 切換失敗: %s", e, exc_info=True)
            messagebox.showerror("失敗", f"切換 watchdog 失敗: {e}")

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
        cert_data = load_json_list(get_conf_path('certificate_templates.json'),
                                   self._get_default_cert_data())
        if len(cert_data) != 4:
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
        
    # =================================================================
    # 縮寫速寫（PhraseExpress-like text expansion）
    # =================================================================
    def _abbrev_settings_path(self):
        return get_conf_path('abbrev_settings.json')

    def _ensure_abbrev_engine(self):
        """確保 abbrev_engine 物件存在；keyboard 模組未就緒時回 None。"""
        if not getattr(self, '_heavy_modules_ready', False):
            return None
        if hotkey_modules.keyboard is None:
            return None
        eng = getattr(self, 'abbrev_engine', None)
        if eng is None:
            eng = AbbrevEngine(hotkey_modules.keyboard)
            self.abbrev_engine = eng
            # [v6] 啟動週期監看：外部文字展開程式 (PhraseExpress 等) 出現/消失
            # 時自動暫停/恢復本程式縮寫
            if not getattr(self, '_abbrev_monitor_started', False):
                self._abbrev_monitor_started = True
                try:
                    self.root.after(180000, self._abbrev_monitor_external)
                except Exception:
                    logging.debug("[abbrev] 啟動 external monitor 失敗",
                                  exc_info=True)
        return eng

    def _maybe_warn_abbrev_external_conflict(self, ext: str | None) -> None:
        """縮寫啟用且偵測到其他展開器時，單次提示使用者目前本程式會暫停縮寫。"""
        if not ext or getattr(self, '_abbrev_external_warning_shown', False):
            return
        self._abbrev_external_warning_shown = True
        logging.warning("[abbrev] 偵測到外部縮寫/展開程式 '%s'，本程式縮寫暫停", ext)

        def _show_warning() -> None:
            if getattr(self, '_shutting_down', False):
                return
            try:
                messagebox.showwarning(
                    "偵測到其他縮寫軟體",
                    f"目前偵測到其他縮寫/文字展開軟體正在執行：{ext}\n\n"
                    "為避免同一段文字被重複展開，本程式縮寫功能會先暫停。\n"
                    "若要改用本程式縮寫，請先關閉該軟體，或到縮寫設定中手動開啟"
                    "「允許自動關閉其他縮寫軟體」。",
                    parent=getattr(self, "root", None),
                )
            except Exception:
                logging.debug("[abbrev] 外部縮寫提示顯示失敗", exc_info=True)

        try:
            self.root.after(0, _show_warning)
        except Exception:
            _show_warning()

    def _maybe_notify_abbrev_closed_external(self, closed) -> None:
        """[2026-06-08] 縮寫啟用且這次 install 真的自動關閉了其他展開軟體 → 主動跳提示
        告知使用者(預設開啟自動關閉，但要讓使用者知道剛剛關掉了什麼)。"""
        if not closed:
            return
        names = "、".join(str(c) for c in closed)
        logging.warning("[abbrev] 已自動關閉其他展開軟體 %s，改用本程式縮寫", names)

        def _show() -> None:
            if getattr(self, '_shutting_down', False):
                return
            try:
                self._show_notice(
                    "已切換為本程式縮寫",
                    f"偵測到其他縮寫/文字展開軟體（{names}），已自動關閉它、改用本程式縮寫，"
                    "避免同一段文字被重複展開。\n"
                    "若想改用該軟體：請先在「縮寫設定」關閉「啟用縮寫速寫」，再開啟該軟體。",
                    level="info", auto_close_ms=6000)
            except Exception:
                logging.debug("[abbrev] 自動關閉提示顯示失敗", exc_info=True)

        try:
            self.root.after(0, _show)
        except Exception:
            _show()

    def _abbrev_monitor_external(self):
        """[v6] 週期檢查外部文字展開程式 (PhraseExpress 等)。
        狀態改變 (出現/消失) → 重新 install (install 內部會依偵測結果決定
        掛 hook 或暫停)，避免雙重展開衝突。[perf r5] 每 ~180s 跑一次（v9 由 20s→60s，
        本輪 60s→180s：detect 走 psutil.process_iter 全行程列舉、跑在 Tk main thread，
        屬粗粒度防呆，使用者極少在 session 中途開關 PhraseExpress，降頻減少 UI thread 負擔）。
        """
        try:
            eng = getattr(self, 'abbrev_engine', None)
            cfg = getattr(self, '_abbrev_config_cache', None)
            if (eng is not None and cfg is not None and cfg.enabled
                    and not getattr(self, '_shutting_down', False)):
                from cmuh_common.abbrev_engine import detect_external_expander
                ext = detect_external_expander()
                last = getattr(self, '_abbrev_last_external', None)
                if ext != last:
                    # [fix B 2026-06-09] last 改由 install 完成後依「實際結果」更新
                    # (_finish_install_abbrev)：自動關閉成功 → last=None，對方若自動重啟，
                    # 下一輪 ext!=None 會再次處理(受 close 冷卻保護)。原本這裡直接記
                    # last=ext，自動關閉成功後對方重啟 → ext==last → 永不再處理 →
                    # 兩套縮寫並存、同段文字雙重展開。
                    if ext:
                        logging.info(
                            "[abbrev] 偵測到外部展開程式 '%s' → 重新評估"
                            "(依設定自動關閉或禮讓暫停)", ext)
                    else:
                        logging.info(
                            "[abbrev] 外部展開程式已關閉 → 恢復本程式縮寫")
                    self._install_abbrev_listeners()
        except Exception:
            logging.debug("[abbrev] external monitor 例外", exc_info=True)
        finally:
            # reschedule (即使這次例外也要繼續監看)
            if not getattr(self, '_shutting_down', False):
                try:
                    self.root.after(180000, self._abbrev_monitor_external)
                except Exception:
                    logging.debug("[abbrev] reschedule monitor 失敗",
                                  exc_info=True)

    def _install_abbrev_listeners(self):
        """依目前 cfg 掛上 hook。keyboard 未就緒會自動 noop。"""
        eng = self._ensure_abbrev_engine()
        if eng is None:
            return
        # 此處是「unhook_all 後重新掛載」的共同匯流點（所有解除全域 hook 的路徑
        # 之後都會呼叫到這）。順手重掛健康監看 heartbeat，確保它不會在 unhook_all
        # 後永久消失。heartbeat 與縮寫無關，獨立於 abbrev enabled 狀態。
        self._install_hotkey_heartbeat()
        cfg = getattr(self, '_abbrev_config_cache', None)
        if cfg is None:
            try:
                cfg = load_abbrev_config(self._abbrev_settings_path())
            except Exception:
                logging.exception("[abbrev] 載入設定失敗，使用空 cfg")
                cfg = AbbrevConfig()
                # [fix D] 設定損毀原本只寫 log 就靜默停用縮寫，使用者不知道 → 主動提示
                self._show_notice(
                    "縮寫設定載入失敗",
                    "縮寫設定檔讀取失敗，縮寫功能已暫停。\n"
                    "請開啟「縮寫設定」重新儲存一次，或檢查 settings/abbrev_settings.json。",
                    level="error", auto_close_ms=8000)
            # [AB-08] load_abbrev_config 內部吞例外回 defaults、幾乎不 raise → 上面 except 是
            # 死路徑。改由 recovered_from_corrupt 旗標觸發提示：設定檔曾損壞、已 backup 為
            # .corrupt-* 並還原成預設，使用者才知道自訂縮寫可能要手動救回。
            if getattr(cfg, "recovered_from_corrupt", False):
                self._show_notice(
                    "縮寫設定曾損壞",
                    "縮寫設定檔內容損壞，已備份為 .corrupt-* 並還原成預設。\n"
                    "自訂縮寫可能遺失，請開啟「縮寫設定」重新確認，"
                    "或從 settings/ 的 .corrupt-* 備份手動還原。",
                    level="warn", auto_close_ms=8000)
            # [AB-04/codex P1] 設定「持續讀取失敗」回的是 fallback 預設 → 不可快取為權威
            # （否則日後存檔會用預設覆寫使用者好檔）。本次仍以 cfg 掛載（多半停用），但不快取，
            # 下輪 _install_abbrev_listeners 會重載重試，鎖解除後即恢復真正的設定。
            if getattr(cfg, "load_failed", False):
                logging.warning("[abbrev] 設定載入持續失敗，本次不快取，下輪重試")
            else:
                self._abbrev_config_cache = cfg
        # [fix A 2026-06-09] 自動關閉外部展開程式的 taskkill(最壞 3s/個)不可在 Tk UI
        # thread 跑(monitor/設定儲存/guardian 重掛都在 UI thread 呼叫本函式)。偵測到
        # 「可自動關閉」的展開程式時 → 先丟背景執行緒把 taskkill 做完，再回 UI thread
        # 掛 hook；其他情況(無外部程式/只剩 AHK)維持原同步路徑(engine 內部不會 taskkill)。
        try:
            if (cfg.enabled and cfg.close_external_expander
                    and not getattr(self, '_abbrev_bg_close_running', False)):
                from cmuh_common.abbrev_engine import (
                    detect_external_expander, is_auto_closable)
                _ext = detect_external_expander()
                if _ext and is_auto_closable(_ext):
                    self._abbrev_bg_close_running = True

                    def _bg_close():
                        closed = []
                        try:
                            from cmuh_common.abbrev_engine import (
                                close_auto_closable_expanders)
                            closed = close_auto_closable_expanders()
                        except Exception:
                            logging.debug("[abbrev] 背景關閉外部展開程式例外",
                                          exc_info=True)

                        def _finish(closed=closed):
                            self._abbrev_bg_close_running = False
                            self._finish_install_abbrev(eng, cfg, pre_closed=closed)
                        try:
                            self.root.after(0, _finish)
                        except Exception:
                            self._abbrev_bg_close_running = False
                    # [review C 2026-06-12] submit 可能被 BoundedExecutor 拒絕；原本被
                    # 外層 except 吃掉但旗標已設 True 且永不重置 → 整個 session 的背景
                    # 關閉路徑永久停用。失敗時重置旗標並 fall through 同步收尾路徑。
                    try:
                        self.bg_executor.submit(_bg_close)
                        return
                    except Exception:
                        self._abbrev_bg_close_running = False
                        logging.warning("[abbrev] 背景關閉任務提交失敗，改走同步路徑",
                                        exc_info=True)
        except Exception:
            logging.debug("[abbrev] 背景關閉前置判斷例外", exc_info=True)
        self._finish_install_abbrev(eng, cfg)

    def _finish_install_abbrev(self, eng, cfg, pre_closed=None):
        """install 的收尾(掛 hook + 依實際結果跳提示/警告 + 同步監看狀態)。
        pre_closed: [fix A] 背景執行緒已先關閉的展開程式清單(供跳提示)。"""
        try:
            eng.install(cfg)
            if cfg.enabled:
                # [2026-06-08] 若這次自動關閉了其他展開軟體 → 主動跳提示告知
                closed = list(pre_closed or []) + list(
                    getattr(eng, '_closed_expanders', None) or [])
                self._maybe_notify_abbrev_closed_external(closed)
                self._maybe_warn_abbrev_external_conflict(
                    getattr(eng, '_external_expander', None))
            # [fix B] 監看狀態同步：記「install 後的實際狀態」(自動關閉成功=None)。
            # 對方自動重啟時 ext!=None 才會再次觸發處理；冷卻保護避免無限互殺。
            self._abbrev_last_external = getattr(eng, '_external_expander', None)
        except Exception:
            logging.exception("[abbrev] install 失敗")

    def _uninstall_abbrev_listeners(self):
        eng = getattr(self, 'abbrev_engine', None)
        if eng is None:
            return
        try:
            eng.uninstall()
        except Exception:
            logging.debug("[abbrev] uninstall 失敗", exc_info=True)

    def _abbrev_save_and_reload(self):
        """把 self._abbrev_config_cache 寫回檔，再重新 install。"""
        cfg = getattr(self, '_abbrev_config_cache', None)
        if cfg is None:
            return
        try:
            save_abbrev_config(self._abbrev_settings_path(), cfg)
        except Exception:
            logging.exception("[abbrev] 存檔失敗")
            messagebox.showerror("縮寫速寫", "設定存檔失敗，請查看系統日誌。")
            return
        self._install_abbrev_listeners()

    def _abbrev_export_settings(self):
        """[新功能 2026-06-11] 匯出縮寫設定到使用者選的 json(換電腦搬設定用)。"""
        cfg = getattr(self, '_abbrev_config_cache', None)
        if cfg is None or not cfg.items:
            messagebox.showwarning("縮寫速寫", "目前沒有可匯出的縮寫。")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="匯出縮寫設定",
            defaultextension=".json",
            initialfile=f"abbrev_settings_{date.today().isoformat()}.json",
            filetypes=[("JSON 設定檔", "*.json")])
        if not path:
            return
        try:
            save_abbrev_config(path, cfg)
            messagebox.showinfo(
                "縮寫速寫", f"已匯出 {len(cfg.items)} 筆縮寫到：\n{path}")
        except Exception:
            logging.exception("[abbrev] 匯出失敗")
            messagebox.showerror("縮寫速寫", "匯出失敗，請查看系統日誌。")

    def _abbrev_import_settings(self):
        """[新功能 2026-06-11] 從 json 匯入縮寫清單(完整取代、匯入前確認)。
        只取代縮寫項目；enabled/IME/自動關閉等開關維持本機現值(那些是機台偏好)。
        先驗證檔案確實含 items 清單，避免亂選檔案被 load 的預設值機制靜默變成
        「匯入了內建預設清單」。"""
        path = filedialog.askopenfilename(
            parent=self.root, title="匯入縮寫設定",
            filetypes=[("JSON 設定檔", "*.json"), ("所有檔案", "*.*")])
        if not path:
            return
        try:
            raw = load_json_dict(path, {}, merge_defaults=False)
        except Exception:
            raw = {}
        if (not isinstance(raw, dict)
                or not isinstance(raw.get("items"), list)
                or not raw.get("items")):
            messagebox.showerror(
                "縮寫速寫", "檔案格式不正確（找不到縮寫項目清單），已取消匯入。")
            return
        try:
            # 走既有驗證/去重/排序；persist_migrations=False 唯讀解析 ——
            # 匯入來源檔(可能是使用者 USB 上的備份)不可被遷移寫回改動。
            new_cfg = load_abbrev_config(path, persist_migrations=False)
        except Exception:
            logging.exception("[abbrev] 匯入解析失敗")
            messagebox.showerror("縮寫速寫", "檔案無法解析為縮寫設定，已取消匯入。")
            return
        if not new_cfg.items:
            messagebox.showwarning("縮寫速寫", "檔案內沒有有效縮寫項目，已取消匯入。")
            return
        cur = getattr(self, '_abbrev_config_cache', None)
        cur_n = len(cur.items) if cur is not None else 0
        if not messagebox.askyesno(
                "確認匯入",
                f"將以匯入檔的 {len(new_cfg.items)} 筆縮寫「完整取代」目前的 "
                f"{cur_n} 筆。\n\n確定要匯入嗎？\n（建議先用「匯出設定」備份目前清單）"):
            return
        if cur is not None:
            cur.items = new_cfg.items
        else:
            self._abbrev_config_cache = new_cfg
        self._abbrev_save_and_reload()
        self._abbrev_refresh_tree()
        messagebox.showinfo("縮寫速寫", f"已匯入 {len(new_cfg.items)} 筆縮寫。")

    def _abbrev_refresh_tree(self):
        tree = getattr(self, '_abbrev_tree', None)
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        cfg = getattr(self, '_abbrev_config_cache', None)
        if cfg is None:
            return
        # 排序：縮寫字首 A -> Z，方便編輯時快速定位。
        items = sort_abbrev_items(cfg.items)
        for idx, it in enumerate(items):
            abbrev = str(it.get('abbrev', ''))
            expansion = str(it.get('expansion', ''))
            display = expansion if len(expansion) <= 80 else expansion[:77] + '...'
            tree.insert('', 'end', iid=f"row_{idx}", values=(abbrev, display))
        # 更新計數
        lbl = getattr(self, '_abbrev_count_label', None)
        if lbl is not None:
            try:
                lbl.config(text=f"共 {len(cfg.items)} 筆")
            except Exception:
                pass

    def _abbrev_on_toggle(self):
        """啟用 checkbox 變動時即時存檔 + reload。
        [2026-07-13 使用者] IME 暫停/保留結尾空白/自動關閉其他縮寫軟體不再讓使用者勾選，
        一律固定開啟（from_dict 已強制 True），這裡只同步「啟用」開關。"""
        cfg = getattr(self, '_abbrev_config_cache', None)
        if cfg is None:
            return
        cfg.enabled = bool(self.abbrev_enabled_var.get())
        cfg.skip_when_ime_active = True
        cfg.preserve_trailing_space = True
        cfg.close_external_expander = True
        self._abbrev_save_and_reload()

    def _abbrev_validate_input(self, abbrev_text, expansion_text, *, ignore_dup=None):
        """回傳 (abbrev_clean, error_msg)。error_msg=None 表示通過。"""
        abbrev = (abbrev_text or '').strip()
        if not abbrev:
            return abbrev, "縮寫不可為空"
        # 限制：只能英數，避免和 token regex 衝突
        if not re.fullmatch(r"[A-Za-z0-9]+", abbrev):
            return abbrev, "縮寫只能用英數字（不可有空白或符號）"
        if len(abbrev) > MAX_ABBREV_LENGTH:
            return abbrev, f"縮寫不可超過 {MAX_ABBREV_LENGTH} 個字元"
        if (expansion_text or '') == '':
            return abbrev, "展開內文不可為空"
        cfg = getattr(self, '_abbrev_config_cache', None)
        if cfg is not None:
            key = abbrev.lower()
            for it in cfg.items:
                if str(it.get('abbrev', '')).lower() == key and key != (ignore_dup or '').lower():
                    return abbrev, f"縮寫 '{abbrev}' 已存在"
        return abbrev, None

    def _abbrev_add_item(self):
        abbrev = self.abbrev_new_abbrev_var.get()
        expansion = self.abbrev_new_expansion_text.get("1.0", "end-1c")
        abbrev_clean, err = self._abbrev_validate_input(abbrev, expansion)
        if err:
            messagebox.showwarning("縮寫速寫", err)
            return
        cfg = self._abbrev_config_cache
        cfg.items.append({"abbrev": abbrev_clean, "expansion": expansion})
        self._abbrev_save_and_reload()
        self._abbrev_refresh_tree()
        self.abbrev_new_abbrev_var.set("")
        self.abbrev_new_expansion_text.delete("1.0", "end")

    def _abbrev_delete_selected(self):
        tree = self._abbrev_tree
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("縮寫速寫", "請先選擇要刪除的縮寫")
            return
        cfg = self._abbrev_config_cache
        to_delete: set[str] = set()
        for iid in sel:
            try:
                abbrev = tree.item(iid, 'values')[0]
                to_delete.add(str(abbrev).lower())
            except Exception:
                continue
        if not to_delete:
            return
        if not messagebox.askyesno("縮寫速寫", f"確定刪除 {len(to_delete)} 筆縮寫？"):
            return
        cfg.items = [it for it in cfg.items
                     if str(it.get('abbrev', '')).lower() not in to_delete]
        self._abbrev_save_and_reload()
        self._abbrev_refresh_tree()

    def _abbrev_edit_selected(self, event=None):
        tree = self._abbrev_tree
        sel = tree.selection()
        if not sel:
            return
        iid = sel[0]
        try:
            abbrev = tree.item(iid, 'values')[0]
        except Exception:
            return
        cfg = self._abbrev_config_cache
        target = None
        for it in cfg.items:
            if str(it.get('abbrev', '')).lower() == str(abbrev).lower():
                target = it
                break
        if target is None:
            return
        self._open_abbrev_editor(target)

    def _open_abbrev_editor(self, item_ref):
        """彈出編輯視窗。item_ref 是 cfg.items 中的 dict（原地修改）。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("編輯縮寫")
        dlg.transient(self.root)
        dlg.grab_set()
        try:
            _apply_tk_window_icon(dlg)
        except Exception:
            pass
        dlg.resizable(True, True)
        dlg.geometry("560x360")

        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill='both', expand=True)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(1, weight=1)

        ttk.Label(frm, text="縮寫:", font=("Microsoft JhengHei UI", 10)).grid(row=0, column=0, sticky='w', padx=(0, 6), pady=(0, 6))
        abbrev_var = tk.StringVar(value=str(item_ref.get('abbrev', '')))
        abbrev_entry = ttk.Entry(frm, textvariable=abbrev_var, font=("Consolas", 12), width=18)
        abbrev_entry.grid(row=0, column=1, sticky='ew', pady=(0, 6))

        ttk.Label(frm, text="展開內文:", font=("Microsoft JhengHei UI", 10)).grid(row=1, column=0, sticky='nw', padx=(0, 6))
        text_widget = tk.Text(frm, wrap='word', font=("Microsoft JhengHei UI", 11), height=8)
        text_widget.grid(row=1, column=1, sticky='nsew')
        text_widget.insert("1.0", str(item_ref.get('expansion', '')))

        scrollbar = ttk.Scrollbar(frm, orient='vertical', command=text_widget.yview)
        scrollbar.grid(row=1, column=2, sticky='ns')
        text_widget.configure(yscrollcommand=scrollbar.set)

        btn_frame = ttk.Frame(frm)
        btn_frame.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(10, 0))

        def on_save():
            new_abbrev = abbrev_var.get()
            new_expansion = text_widget.get("1.0", "end-1c")
            cleaned, err = self._abbrev_validate_input(
                new_abbrev, new_expansion,
                ignore_dup=str(item_ref.get('abbrev', '')))
            if err:
                messagebox.showwarning("縮寫速寫", err, parent=dlg)
                return
            item_ref['abbrev'] = cleaned
            item_ref['expansion'] = new_expansion
            self._abbrev_save_and_reload()
            self._abbrev_refresh_tree()
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        ttk.Button(btn_frame, text="儲存", command=on_save).pack(side='right', padx=(6, 0))
        ttk.Button(btn_frame, text="取消", command=on_cancel).pack(side='right')

        abbrev_entry.focus_set()

    def _abbrev_reset_defaults(self):
        if not messagebox.askyesno(
            "縮寫速寫",
            "確定要把所有縮寫還原成內建預設？（你目前自訂的全部會被覆蓋）"
        ):
            return
        cfg = self._abbrev_config_cache
        cfg.items = [dict(it) for it in ABBREV_DEFAULT_ITEMS]
        self._abbrev_save_and_reload()
        self._abbrev_refresh_tree()

    def _create_abbrev_tab(self, abbrev_tab):
        # 載入設定（首次啟動會自動寫入預設檔）
        try:
            cfg = ensure_abbrev_config_file(self._abbrev_settings_path())
        except Exception:
            logging.exception("[abbrev] 建立設定檔失敗，改用記憶體預設值")
            cfg = AbbrevConfig(
                enabled=False,
                skip_when_ime_active=True,
                preserve_trailing_space=True,
                items=[dict(it) for it in ABBREV_DEFAULT_ITEMS],
            )
        self._abbrev_config_cache = cfg

        # 控制變數（[2026-07-13 使用者] IME暫停/保留結尾空白/自動關閉其他縮寫軟體不再給勾選，固定開啟）
        self.abbrev_enabled_var = tk.BooleanVar(value=cfg.enabled)
        self.abbrev_new_abbrev_var = tk.StringVar()

        # [2026-06-15] 整頁可捲動:原本「動態日期 token」說明在最底,視窗不夠高時
        # 被截掉看不到下半。改與「設定」頁相同作法,用 Canvas 包一層可垂直捲動內容區。
        _abbrev_canvas = tk.Canvas(abbrev_tab, highlightthickness=0)
        _abbrev_sb = ttk.Scrollbar(abbrev_tab, orient="vertical",
                                   command=_abbrev_canvas.yview)
        _body = ttk.Frame(_abbrev_canvas)
        _body.bind("<Configure>", lambda e: _abbrev_canvas.configure(
            scrollregion=_abbrev_canvas.bbox("all")))
        _body_win = _abbrev_canvas.create_window((0, 0), window=_body, anchor="nw")
        _abbrev_canvas.bind("<Configure>", lambda e: _abbrev_canvas.itemconfig(
            _body_win, width=e.width))
        _abbrev_canvas.configure(yscrollcommand=_abbrev_sb.set)
        _abbrev_canvas.pack(side="left", fill="both", expand=True)
        _abbrev_sb.pack(side="right", fill="y")

        # 上方控制列
        ctrl_frame = ttk.LabelFrame(_body, text="總開關")
        ctrl_frame.pack(fill='x', pady=(0, 8))
        row1 = ttk.Frame(ctrl_frame)
        row1.pack(fill='x', padx=10, pady=(6, 2))
        ttk.Checkbutton(
            row1, text="啟用縮寫速寫（打縮寫 + 空白鍵自動展開）",
            variable=self.abbrev_enabled_var,
            command=self._abbrev_on_toggle,
        ).pack(side='left')
        self._abbrev_count_label = ttk.Label(
            row1, text=f"共 {len(cfg.items)} 筆", foreground="#607D8B")
        self._abbrev_count_label.pack(side='right')

        # [2026-07-13 使用者] 三項行為（中文組字中暫停、保留結尾空白、自動關閉其他縮寫
        # 軟體）啟用縮寫速寫後一律自動開啟；不再顯示勾選，也不顯示說明文字。

        # 縮寫列表
        list_frame = ttk.LabelFrame(_body, text="縮寫清單（雙擊可編輯）")
        list_frame.pack(fill='both', expand=True, pady=(0, 8))

        tree_container = ttk.Frame(list_frame)
        tree_container.pack(fill='both', expand=True, padx=8, pady=(6, 6))

        columns = ("abbrev", "expansion")
        tree = ttk.Treeview(tree_container, columns=columns, show='headings', height=12)
        tree.heading("abbrev", text="縮寫")
        tree.heading("expansion", text="展開內文")
        tree.column("abbrev", width=110, anchor='w', stretch=False)
        tree.column("expansion", width=560, anchor='w', stretch=True)
        ysb = ttk.Scrollbar(tree_container, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=ysb.set)
        tree.pack(side='left', fill='both', expand=True)
        ysb.pack(side='right', fill='y')
        tree.bind("<Double-1>", self._abbrev_edit_selected)
        self._abbrev_tree = tree

        btn_row = ttk.Frame(list_frame)
        btn_row.pack(fill='x', padx=8, pady=(0, 6))
        ttk.Button(btn_row, text="編輯選取", command=self._abbrev_edit_selected).pack(side='left')
        ttk.Button(btn_row, text="刪除選取", command=self._abbrev_delete_selected).pack(side='left', padx=(6, 0))
        # [新功能 2026-06-11] 匯出/匯入：方便換電腦搬縮寫設定
        ttk.Button(btn_row, text="匯出設定", command=self._abbrev_export_settings).pack(side='left', padx=(18, 0))
        ttk.Button(btn_row, text="匯入設定", command=self._abbrev_import_settings).pack(side='left', padx=(6, 0))
        ttk.Button(btn_row, text="重設為預設清單", command=self._abbrev_reset_defaults).pack(side='right')

        # 新增區塊
        add_frame = ttk.LabelFrame(_body, text="新增縮寫")
        add_frame.pack(fill='x', pady=(0, 8))
        add_inner = ttk.Frame(add_frame)
        add_inner.pack(fill='x', padx=10, pady=8)
        add_inner.columnconfigure(1, weight=1)

        ttk.Label(add_inner, text="縮寫:", font=("Microsoft JhengHei UI", 10)).grid(
            row=0, column=0, sticky='w', padx=(0, 6))
        abbrev_entry = ttk.Entry(
            add_inner, textvariable=self.abbrev_new_abbrev_var,
            font=("Consolas", 12), width=14)
        abbrev_entry.grid(row=0, column=1, sticky='w', pady=(0, 4))

        ttk.Label(add_inner, text="展開內文:", font=("Microsoft JhengHei UI", 10)).grid(
            row=1, column=0, sticky='nw', padx=(0, 6), pady=(4, 0))
        expansion_text = tk.Text(
            add_inner, wrap='word', height=4,
            font=("Microsoft JhengHei UI", 11))
        expansion_text.grid(row=1, column=1, sticky='ew', pady=(4, 4))
        self.abbrev_new_expansion_text = expansion_text

        ttk.Button(add_inner, text="加入清單", command=self._abbrev_add_item).grid(
            row=2, column=1, sticky='e', pady=(4, 0))

        # token 說明區
        hint_frame = ttk.LabelFrame(_body, text="動態日期 token（可寫在「展開內文」裡）")
        hint_frame.pack(fill='x', pady=(0, 8))
        hint_text = (
            "  da        → 今日日期（斜線），例：(2026/5/27)\n"
            "  da1       → 現在時間，例：23:34\n"
            "  da2       → 今日日期 + 現在時間，例：(2026/5/27) 23:34\n"
            "  da+N      → 今日 + N 天（斜線），例：da+7 → (2026/6/3)\n"
            "  da-N      → 今日 - N 天（斜線），例：da-21 → (2026/5/6)\n"
            "  da_zh     → 今日日期（中文），例：2026年5月27日\n"
            "  da_zh+N   → 今日 + N 天（中文），例：da_zh+7 → 2026年6月3日\n"
            "  da_zh-N   → 今日 - N 天（中文），例：da_zh-21 → 2026年5月6日\n"
            "\n"
            "  - 觸發方式：打縮寫後直接按「空白鍵」自動展開。\n"
            "  - 大小寫不敏感（DA / Da / dA / da 都可以）。\n"
            "  - 中文輸入法組字中會盡量自動暫停；若仍誤觸，可暫時取消上方\n"
            "    「啟用縮寫速寫」即可。"
        )
        ttk.Label(hint_frame, text=hint_text, justify='left', foreground="#37474F",
                  font=("Consolas", 10)).pack(anchor='w', padx=10, pady=8)

        # 列表填資料
        self._abbrev_refresh_tree()

        # 滑鼠滾輪:整頁捲動;游標在縮寫清單(tree)上時改捲動清單本身。
        def _abbrev_wheel(event):
            _abbrev_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        self._bind_mousewheel_recursive(abbrev_tab, _abbrev_wheel)

        def _tree_wheel(event):
            tree.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        tree.bind("<MouseWheel>", _tree_wheel)

    # ── 契約金絲雀（院方改版偵測）設定區塊 ──────────────────────────────────
    def _canary_status_text_now(self) -> str:
        """[codex] 即時採樣算 HIS 寫入契約顯示文字(自足,不讀全域→顯示永不 stale)。
        找不到主視窗＝尚未偵測(誠實);找到就用當下 title 現算裁決。"""
        try:
            hwnd = _find_hospital_main_window()
        except Exception:
            hwnd = 0
        if not hwnd:
            return "HIS 寫入契約：尚未偵測（找不到 HIS 主視窗，開啟後按「重新整理」）"
        return "HIS 寫入契約：" + _his_write_verdict_for(_his_title_of(hwnd)).human()

    def _build_canary_settings(self, parent) -> None:
        cf = ttk.LabelFrame(parent, text="契約金絲雀（院方改版偵測）", padding=10)
        cf.pack(fill=tk.X, pady=(0, 15))
        # 初始不主動找視窗(免建 UI 卡逾時);按「重新整理」才即時採樣
        self._canary_status_var = tk.StringVar(
            value="HIS 寫入契約：按「重新整理」查詢")
        ttk.Label(cf, textvariable=self._canary_status_var, wraplength=300,
                  justify="left", style="Small.TLabel").pack(anchor="w", pady=(0, 6))
        btns = ttk.Frame(cf)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="重新整理",
                   command=self._refresh_canary_status).pack(side=tk.LEFT)
        ttk.Button(btns, text="重新校正",
                   command=self._recalibrate_his_canary).pack(side=tk.LEFT, padx=6)
        ttk.Label(cf, text="偵測到院方改版（主視窗版本與基線不符）時，會【寄信通知】"
                  "(止掛提醒收件人),但【不會擋住自動寫入、不影響操作、不重複跳窗】。"
                  "若發現 F1–F11 功能異常，請手動停用該熱鍵並通知開發者核對選單 id；"
                  "確認新版無誤後按「重新校正」把現況記為新基線、停止通知。",
                  foreground="gray", wraplength=300, justify="left",
                  style="Small.TLabel").pack(anchor="w", pady=(6, 0))

    def _refresh_canary_status(self) -> None:
        """即時採樣並更新顯示（自足,找不到視窗顯示「尚未偵測」,永不 stale）。"""
        if hasattr(self, "_canary_status_var"):
            self._canary_status_var.set(self._canary_status_text_now())

    def _recalibrate_his_canary(self) -> None:
        """重新校正 HIS 寫入契約：把現況版本記為新基線（醫師確認新版無誤後）。

        [codex P1] 自足即時採樣:用當下找到的 hwnd 取 title 當場算指紋,不讀全域
        _his_current_fp(避免拿到被並行呼叫覆寫/清空的過期值)。"""
        try:
            hwnd = _find_hospital_main_window()
        except Exception:
            logging.debug("[金絲雀] 校正前找視窗失敗", exc_info=True)
            hwnd = 0
        fp = sample_his_current_fp(_his_title_of(hwnd)) if hwnd else None
        if not isinstance(fp, dict) or not fp.get("title_version"):
            messagebox.showwarning(
                "無法校正金絲雀",
                "找不到 HIS 主視窗、或主視窗標題沒有版本號，無法校正。\n"
                "請先確認「西醫門診醫師作業」主程式已開啟，再試一次。")
            return
        ver = fp["title_version"]
        if not messagebox.askyesno(
                "確認重新校正金絲雀",
                f"將把目前 HIS 版本 {ver} 記為新基線。\n\n"
                f"⚠ 請只在你【已確認新版本的 F 鍵選單/欄位都正確】後才校正——\n"
                f"校正後金絲雀不再對此版本示警，F 鍵自動寫入會照常進行。\n\n確定校正？"):
            return
        try:
            saved = _contract_baseline().set(_CANARY_HIS_SURFACE, fp,
                                             note=f"UI 校正 v{ver}")
        except Exception:
            logging.exception("[金絲雀] 重新校正失敗")
            messagebox.showerror("校正失敗", "寫入基線檔失敗，請查看日誌。")
            return
        if not saved:
            # [codex] 基線檔為較新版本 schema → 拒絕覆寫(防降版毀損),不可誤報成功
            messagebox.showerror(
                "校正被拒（防降版）",
                "契約基線檔是較新版本程式寫的，已拒絕用舊版覆寫以免毀損。\n"
                "請先把本程式更新到最新版，再重新校正。")
            return
        if hasattr(self, "_canary_status_var"):
            self._canary_status_var.set(self._canary_status_text_now())
        messagebox.showinfo("金絲雀已校正",
                            f"已記錄 HIS 版本 {ver} 為新基線，自動寫入恢復。")

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
        ttk.Button(top_bar_frame, text="檢查線上更新", command=lambda: self._submit_update_check(True)).pack(side=tk.RIGHT)
        # [2026-06-01] 主題切換按鈕已移除（固定 Windows 原生主題，維持原本外觀）。

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
        # [v9] 1024×768 等窄螢幕：設定頁 canvas 只縱向捲動，三欄並排會讓第三欄
        # (250px 海報) 被右緣裁切。低解析度時改把第三欄堆疊到下方 (row=1)，
        # 跨兩欄寬，確保不裁切。
        third_column = ttk.Frame(columns_container)
        if getattr(self, 'screen_width', 1920) <= 1024:
            third_column.grid(row=1, column=0, columnspan=2, sticky="nw", pady=(15, 0))
        else:
            third_column.grid(row=0, column=2, sticky="nw", padx=(0, 0))

        # --- 左欄內容 (保持不變) ---
        mode_frame = ttk.LabelFrame(left_column, text="模式與顯示設定", padding=10)
        mode_frame.pack(fill=tk.X, pady=(0, 15))
        # [2026-07-13 使用者] 已移除「提醒勿擾時段」「半夜也監測」「顯示外院/分院」三個設定；行為固定：
        # 止掛提醒 email 全天候照寄、夜間(00–08)只不跳彈窗；外院/分院固定顯示；reg64 固定 00–07 暫停。
        ttk.Label(mode_frame,
                  text="止掛提醒 24 小時偵測（夜間 00–08 只寄 email、不跳彈窗）；外院/分院固定顯示；"
                       "門診進度 00–07 暫停刷新。",
                  foreground="gray", style="Small.TLabel", wraplength=320,
                  justify="left").pack(anchor="w")

        # 在 _create_settings_tab 內部
        def on_mode_change():
            self.val_out_of_hospital = self.out_of_hospital_var.get()

            if self.out_of_hospital_var.get():
                logging.info("切換至 [醫院外模式]")
                safe_unhook_all_hotkeys()
                # 縮寫速寫獨立於 HIS 模式：unhook_all 後重掛
                try:
                    self._install_abbrev_listeners()
                except Exception:
                    logging.exception("[abbrev] 院外模式切換後 install 失敗")
                self.status_text.set("狀態: 院外模式 (功能已停用)")
                # 打卡燈號設為灰色表示停用（disabled → 不觸發自動重試）
                put_ui_message(self.ui_queue, UiClockStatusMessage(
                    status_data=_clock_error("院外模式停用", CLOCK_ERR_DISABLED)))
            else:
                logging.info("切換至 [院內模式]")
                self.setup_hotkeys()
                self.status_text.set("狀態: 院內模式")
                self.update_clock_status_from_web()

        ttk.Checkbutton(mode_frame, text="開啟「醫院外模式」", variable=self.out_of_hospital_var, command=on_mode_change).pack(anchor="w", pady=2)

        ui_scale_frame = ttk.LabelFrame(left_column, text="介面字體", padding=10)
        ui_scale_frame.pack(fill=tk.X, pady=(0, 15))
        ttk.Label(ui_scale_frame, text="縮放 (0.85–1.45，儲存後重新啟動生效):").pack(anchor="w")
        sp_font = tk.Spinbox(ui_scale_frame, from_=0.85, to=1.45, increment=0.05, width=6, textvariable=self.ui_font_scale_var, format="%.2f")
        sp_font.pack(anchor="w", pady=(4, 0))

        self._build_canary_settings(left_column)   # [金絲雀] 院方改版偵測狀態 + 重新校正

        r_doctor_frame = ttk.LabelFrame(left_column, text="R1-R3 醫師姓名（值班對照）", padding=10)
        r_doctor_frame.pack(fill=tk.X, pady=(0, 15))
        self.r_doctor_entries = {}
        for i, r_key in enumerate(["R1", "R2", "R3"]):
            ttk.Label(r_doctor_frame, text=f"{r_key} 姓名:").grid(row=i, column=0, padx=5, pady=5, sticky='e')
            name_var = tk.StringVar(value=self.r_doctor_map.get(r_key, {}).get('name', ''))
            name_entry = ttk.Entry(r_doctor_frame, textvariable=name_var, width=12); name_entry.grid(row=i, column=1, padx=5, pady=5, sticky='w')
            self.r_doctor_entries[r_key] = {'name_var': name_var}

        # ─── 程式監看 (watchdog) 總開關 ─────────────────────────────────
        # 預設關閉：沒設定過 會診查詢/打卡 的電腦完全不會啟動 watchdog，
        # 不會跳出莫名的 ClockApp 設定視窗。要在本機跑 watchdog 就勾起來。
        watchdog_frame = ttk.LabelFrame(left_column, text="背景監看 (watchdog)", padding=10)
        watchdog_frame.pack(fill=tk.X, pady=(0, 15))
        try:
            from cmuh_common.watchdog_core import load_config as _wd_load
            _wd_cfg = _wd_load()
            self.watchdog_enabled_var = tk.BooleanVar(
                value=bool(_wd_cfg.get("master_enabled", False)))
        except Exception:
            self.watchdog_enabled_var = tk.BooleanVar(value=False)
        ttk.Label(
            watchdog_frame,
            text=(
                "勾選後，主程式會在背景每 30 秒檢查【會診查詢】/【打卡】是否卡死，"
                "卡住自動 kill+重啟。沒設定過該功能的電腦不會被打擾 (per-machine opt-in)。"
            ),
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Checkbutton(
            watchdog_frame,
            text="啟用 watchdog 監看背景程式",
            variable=self.watchdog_enabled_var,
            command=self._on_watchdog_toggle,
        ).pack(anchor="w")

        # 註:浮動門診動態小視窗的開關 + 透明度已移到「小工具」分頁的「目前門診動態」區
        #     (_create_other_programs_tab),就近與門診動態卡片同處。設定值/變數不變。

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

        # 止掛達門檻時 → 用 Outlook 寄信通知（可多位收件人，留空=不寄）
        ttk.Separator(threshold_main_frame, orient='horizontal').pack(fill='x', pady=8)
        mail_block = ttk.Frame(threshold_main_frame); mail_block.pack(fill=tk.X, pady=2)
        ttk.Label(mail_block, text="止掛達門檻寄信通知（收件人 Email，可多人）:",
                  font=("Microsoft JhengHei UI", self.f_sm, "bold")
                  ).pack(anchor="w")
        mail_row = ttk.Frame(mail_block); mail_row.pack(fill=tk.X, pady=(2, 0))
        self.alert_mail_listbox = tk.Listbox(mail_row, height=3, font=("Consolas", 10))
        self.alert_mail_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for r in self.alert_email_recipients:
            self.alert_mail_listbox.insert(tk.END, r)
        mail_btns = ttk.Frame(mail_row); mail_btns.pack(side=tk.LEFT, padx=4)
        self.alert_mail_entry = ttk.Entry(mail_btns, width=22, font=("Consolas", 10))
        self.alert_mail_entry.pack(pady=1)
        ttk.Button(mail_btns, text="新增", width=8,
                   command=self._add_alert_mail).pack(fill=tk.X, pady=1)
        ttk.Button(mail_btns, text="刪除選定", width=8,
                   command=self._del_alert_mail).pack(fill=tk.X, pady=1)
        ttk.Label(mail_block,
                  text="（透過本機 Outlook 寄出；留空則只跳 Windows 通知不寄信。記得按下方「儲存所有設定」）",
                  foreground="#666", font=("Microsoft JhengHei UI", self.f_sm)
                  ).pack(anchor="w", pady=(2, 0))

        # F8 快速輸入文字設定 — 按 F8 → 輸入此欄位文字到目前 focused 控件
        f8_frame = ttk.LabelFrame(left_column, text="F8 快速輸入文字", padding=10)
        f8_frame.pack(fill=tk.X, pady=(0, 15))
        f8_row = ttk.Frame(f8_frame)
        f8_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(f8_row, text="輸入內容:").pack(side=tk.LEFT)
        ttk.Entry(f8_row, textvariable=self.quick_text_f8_var, width=24).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(f8_frame,
                  text=f"按 F8 會在目前游標位置輸入此文字 (預設 {F8_QUICK_TEXT_DEFAULT})。儲存後即時生效，不需重啟。",
                  foreground="gray", style="Small.TLabel", wraplength=420, justify="left").pack(anchor="w", pady=(2, 0))

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

    def _add_alert_mail(self):
        addr = self.alert_mail_entry.get().strip()
        if not addr:
            return
        if addr in self.alert_mail_listbox.get(0, tk.END):
            return
        self.alert_mail_listbox.insert(tk.END, addr)
        self.alert_mail_entry.delete(0, tk.END)

    def _del_alert_mail(self):
        sel = self.alert_mail_listbox.curselection()
        if sel:
            self.alert_mail_listbox.delete(sel[0])

    def ensure_settings_promo_loaded(self):
        if self._settings_promo_loaded or self._settings_promo_loading:
            return
        self._settings_promo_loading = True
        if hasattr(self, 'promo_placeholder_label'):
            self.promo_placeholder_label.config(text="圖片載入中...")
        promo_future = self.bg_executor.submit(self._load_settings_promo_image)

        def _handle_promo_submit_rejected(fut):
            try:
                rejected = fut.cancelled() or isinstance(fut.exception(), RejectedExecutionError)
            except Exception:
                rejected = False
            if not rejected or getattr(self, '_shutting_down', False):
                return
            logging.warning("設定頁圖片背景載入未啟動：背景佇列已滿")

            def _reset_promo_loading():
                self._settings_promo_loading = False
                if hasattr(self, 'promo_placeholder_label'):
                    self.promo_placeholder_label.config(text="圖片稍後重試")
                self.root.after(5000, self.ensure_settings_promo_loaded)

            self._run_on_ui_thread(_reset_promo_loading)

        promo_future.add_done_callback(_handle_promo_submit_rejected)

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

        # [perf r5] 東區休診推論索引：每次 refresh 只全掃一次建索引(取代每格重掃整月，
        # 見 _build_east_weekday_index 上方說明)。只在顯示東區時才建。
        east_weekday_index = (
            _build_east_weekday_index(self.all_doctors_data, self._appt_item_session_ext)
            if self.show_external_clinics.get() else {}
        )

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
                                                            # 詳細通知(toast 與 email 共用):年/月/日(週X)、早上/下午/晚上、
                                                            # 醫師、診間(例 101/102/103)、目前人數。依需求不寫預設/止掛門檻人數。
                                                            _sess_label = {"上午": "早上"}.get(session_name, session_name)
                                                            _wd = "一二三四五六日"[current_date.weekday()]
                                                            _date_str = (f"{current_date.year}/{current_date.month}/"
                                                                         f"{current_date.day}(週{_wd})")
                                                            _branch_suffix = (_EXT_BRANCH_DISPLAY_SUFFIX.get(ext_branch, "")
                                                                              if ext_branch else "")
                                                            # _RE_ROOM 擷取的 room 已含「診」(如 101診);僅缺時才補,避免「101診診」。
                                                            # [2026-06-19] 掛號資料沒帶診間時(如張廖年峰),回填 reg64 門診動態
                                                            # 即時抓到的診間(同醫師同時段)。止掛當下診間燈亮著、reg64 有在輪詢,
                                                            # snapshot 多半有值;仍取不到才顯示「(診間未提供)」。
                                                            _room = room
                                                            if not _room:
                                                                with self._reg64_cache_lock:
                                                                    _snap = self._reg64_public_snapshot.get(
                                                                        (doc_name, session_name))
                                                                if _snap and _snap.get("room"):
                                                                    _room = str(_snap["room"])
                                                            _room_label = ((_room if "診" in _room else f"{_room}診")
                                                                           if _room else "(診間未提供)")
                                                            _where = f"{doc_name}醫師 {_room_label}{_branch_suffix}"
                                                            msg = (f"{level_prefix}\n"
                                                                   f"{_date_str} {_sess_label}\n"
                                                                   f"{_where}\n"
                                                                   f"目前掛號 {count} 人")
                                                            # DND 邏輯改動 (2026-05-18)：
                                                            # 原本 DND 時直接 continue → toast 跟 email 都被擋掉，導致使用者
                                                            # 醒來完全不知道半夜門檻爆掉。改成：DND 只抑制 toast (避免半夜
                                                            # 跳訊息打擾)，email 仍然寄 (醒來就能在信箱看到)。
                                                            is_dnd = self._is_notification_suppressed_now()
                                                            if is_dnd:
                                                                self._dnd_suppressed_count += 1
                                                                # [MN-06] DND 只抑制 toast;email 是否真的會寄取決於 level 與是否已寄過
                                                                # (見 _notify_worker 的 MN-01/02)。狀態文字須據實——第二次提醒若前次
                                                                # 已成功寄出,這次純略過(不再寄信),顯示「僅寄 email」會誤導。
                                                                # _notify_worker 實際會寄 email 的充要條件=「此 notify_key 尚未成功寄過」
                                                                # (lvl==1 未寄過才寄;lvl==2 也只在未寄過時補寄)。狀態文字據此判斷,
                                                                # 涵蓋:重啟後記憶體 frequency 歸零→首觸發 lvl=1,但持久化記錄顯示已寄過
                                                                # → worker 其實跳過不寄,狀態就不能再說「僅寄 email」。
                                                                _will_email = not self._has_alert_email_been_sent(notify_key)
                                                                if _will_email:
                                                                    self.status_text.set(f"狀態: 勿擾時段，僅寄 email（{doc_name}{session_name}，{diff_text}）")
                                                                    logging.info(f"[ALERT DND] toast 抑制但 email 仍寄 {doc_name} {session_name} count={count} threshold={full_threshold} {diff_text}")
                                                                else:
                                                                    self.status_text.set(f"狀態: 勿擾時段，第二次提醒略過（{doc_name}{session_name}，前次已寄）")
                                                                    logging.info(f"[ALERT DND] 第二次提醒略過(前次已寄,僅抑制 toast) {doc_name} {session_name} count={count} {diff_text}")
                                                            _dnd_tag = "【夜間勿擾】" if is_dnd else ""
                                                            # 主旨同樣帶日期/時段/診間/醫師/目前人數,不寫門檻人數
                                                            alert_subject = (
                                                                f"【止掛提醒】{_dnd_tag}{_date_str} {_sess_label} "
                                                                f"{_where} 目前 {count} 人")
                                                            def _notify_worker(nk=notify_key, m=msg,
                                                                                subj=alert_subject,
                                                                                lvl=notify_level, dnd=is_dnd):
                                                                try:
                                                                    # [MN-01] email 先寄,再跳通知。原本先跳阻塞式 MessageBox 再寄信 →
                                                                    # 診間無人按掉彈窗時 email 被卡住;期間程式被關(daemon 緒死)→
                                                                    # 該信永遠沒寄也沒記錄。寄信擺前面確保不被 UI 阻塞/中止。
                                                                    # email：DND 也寄(第一次提醒)。
                                                                    # [2026-06-15] 跨重啟去重:寄前查持久化記錄,已寄過就跳過
                                                                    # (避免重開程式/刷新人數重複寄同一診次的信);寄成功才記錄。
                                                                    # [MN-02] 第二次加強提醒(lvl=2)原本一律不寄 → 第一次遇 SMTP 暫時
                                                                    # 故障失敗後當天整診次零封信。改成:前次已成功寄出者 lvl=2 仍只跳
                                                                    # 通知(不重複信);前次未成功者,lvl=2 給一次補寄機會。
                                                                    if lvl == 1 or (lvl == 2 and
                                                                                    not self._has_alert_email_been_sent(nk)):
                                                                        if self._has_alert_email_been_sent(nk):
                                                                            logging.info("[ALERT] 止掛信先前已寄出，跳過重寄：%s", nk)
                                                                        else:
                                                                            rcpts = list(self.alert_email_recipients)
                                                                            if rcpts:
                                                                                try:
                                                                                    if _send_alert_email_via_smtp(
                                                                                            subj, m, rcpts):
                                                                                        self._mark_alert_email_sent(nk)
                                                                                except Exception:
                                                                                    logging.warning("止掛提醒寄信例外", exc_info=True)
                                                                    # toast 只在非 DND 時跳;優先用非阻塞 winotify,失敗才 fallback
                                                                    # 阻塞式 MessageBox(MN-01:即使 fallback,email 已在上面寄完)。
                                                                    if not dnd:
                                                                        if not show_winotify_toast("止掛提醒", m):
                                                                            show_windows_notification("止掛提醒", m)
                                                                finally:
                                                                    with self._alert_state_lock:
                                                                        self._alert_popup_active[nk] = False
                                                            # [v10] 防 latch：start() 失敗（thread 耗盡等）會讓 gate
                                                            # 永久卡 True → 該診次當天不再提醒。失敗即重置 gate。
                                                            try:
                                                                threading.Thread(target=_notify_worker, name="NotifyThread", daemon=True).start()
                                                            except Exception:
                                                                logging.exception("[ALERT] NotifyThread 啟動失敗，重置 gate 避免永久卡死")
                                                                with self._alert_state_lock:
                                                                    self._alert_popup_active[notify_key] = False
                                        except Exception as e: 
                                            logging.error(f"Error checking threshold: {e}")
                                
                                display_name = doc_name
                                _suf = _EXT_BRANCH_DISPLAY_SUFFIX.get(ext_branch)
                                if _suf:
                                    display_name += _suf
                                elif room and room not in _OVERVIEW_PRIMARY_ROOMS:
                                    display_name += f"({room})"
                                if is_self_paid: display_name += "*"
                                
                                # 排序：(1) 非休診列優先，休診／無門診列一律置底
                                # (2) A101→A102→A103→本院其他→分院；分院內：東區→亞大→惠和→惠盛→其他 (3) 醫師清單順序
                                is_dayoff_row = (
                                    tag in ("dayoff", "no_clinic")
                                    or ("休診" in status_text)
                                    or ("停診" in status_text)
                                )
                                dayoff_tier = 1 if is_dayoff_row else 0
                                if ext_branch:
                                    zone_bucket = len(_OVERVIEW_PRIMARY_ROOMS) + 1   # 分院最後
                                    brank = _calendar_branch_sort_rank(ext_branch)
                                elif room in _OVERVIEW_PRIMARY_ROOMS:
                                    zone_bucket = _OVERVIEW_PRIMARY_ROOMS.index(room)  # A101→0 A102→1 A103→2
                                    brank = 0
                                else:
                                    zone_bucket = len(_OVERVIEW_PRIMARY_ROOMS)       # 本院其他診間
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
                        sort_key = (1, len(_OVERVIEW_PRIMARY_ROOMS), 0, order_map.get(doc_name, 99))
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
                            # [perf r5] O(1) 索引查詢取代每格重掃整月(語意等價，見上方索引建置)
                            if not _east_index_has_other(
                                east_weekday_index, doc_no, doc_name,
                                weekday_idx, session_name, current_date
                            ):
                                continue
                            display_data[session_name][key_ext] = (
                                doc_name + "(東區分院)",
                                "休診",
                                "dayoff",
                                (1, len(_OVERVIEW_PRIMARY_ROOMS) + 1, 0, order_map.get(doc_name, 99)),
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
        """節流 (Debounce)：500ms 內多次資料到達只寫最後一份資料。"""
        if getattr(self, '_shutting_down', False):
            return
        self._save_cache_latest[filename] = data
        if not self._save_cache_pending.get(filename):
            self._save_cache_pending[filename] = True
            self.root.after(500, lambda: self._do_deferred_save_cache(filename))

    def _do_deferred_save_cache(self, filename):
        self._save_cache_pending[filename] = False
        data = self._save_cache_latest.pop(filename, None)
        if getattr(self, '_shutting_down', False):
            return
        if data is None:
            return
        # [效能] data 可為 thunk(callable)：把昂貴的快照(deepcopy 整份門診資料)延後到
        # 真正寫檔這一刻才做一次，避免 500ms debounce 視窗內每則訊息都先 deepcopy 又被丟棄。
        if callable(data):
            try:
                data = data()
            except Exception:
                logging.exception("[save_cache] 快照 thunk 失敗: %s", filename)
                return
            if data is None:
                return
        self._save_cache(filename, data)

    def _duty_cache_mem_ensure(self) -> dict[str, Any]:
        """值班資訊 UI 用記憶體快取（啟動時自檔案載入一次）。"""
        if not hasattr(self, "_duty_cache_mem"):
            self._duty_cache_mem = load_json_dict(
                get_conf_path("cache_duty_info.json"), {}, merge_defaults=False)
        return self._duty_cache_mem

    def process_ui_queue(self):
        """[修正] 合併 UI 訊息處理 + Log 輪詢為單一迴圈，減少 root.after() 排程次數"""
        if getattr(self, '_shutting_down', False):
            return
        had_work = False
        try:
            for _ in range(250):
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
                            # [MG-02] 不再無條件 2 秒後硬砍;改走閘門:熱鍵自動化進行中會延後到閒置才重啟,
                            # 避免把 daemon 熱鍵緒在指令中間切斷(醫令殘碼/同意書半開)。
                            self.root.after(2000, self._restart_when_hotkey_idle)
                    case UiAlertErrorMessage(title=title, msg=emsg):
                        self._show_notice(title, emsg, level="error", auto_close_ms=7000)
                    case UiClinicDataMessage(doctor_name=doctor_name, data=appointment_data):
                        if doctor_name and appointment_data is not None:
                            with self._doctor_data_lock:
                                if (
                                    isinstance(appointment_data, dict)
                                    and "error" in appointment_data
                                    and _appointments_data_count(self.all_doctors_data.get(doctor_name)) > 0
                                ):
                                    logging.warning(f"[CACHE_PROTECT] 保留 {doctor_name} 既有門診人數快取，略過錯誤覆蓋。")
                                    continue
                                self.all_doctors_data[doctor_name] = appointment_data
                            self._schedule_refresh()
                            # [效能] 傳 bound method(thunk)而非預先 deepcopy：deepcopy 延到寫檔時做一次
                            self._schedule_save_cache('cache_clinic_counts.json', self._get_all_doctors_data_snapshot)
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
                        # 【效能 2026.05.20】debounce — 4 個 duty future 完成順序不一，
                        # 同步寫 4 次同一檔會凍 UI 150-300ms。
                        self._schedule_save_cache('cache_duty_info.json', duty_cache)
                    case UiSaturdayDutyDoctorMessage(saturday_date=saturday_date, doctor_name=sdn):
                        duty_cache = self._duty_cache_mem_ensure()
                        duty_cache['date'] = date.today().strftime("%Y-%m-%d")
                        date_str = saturday_date.strftime("%m/%d")
                        txt = f"當週({date_str} 週六) 值班: {sdn}"
                        self.saturday_duty_doctor_var.set(txt)
                        duty_cache['saturday_duty'] = txt
                        self._refresh_duty_summary_text()
                        self._duty_cache_mem = duty_cache
                        # 【效能 2026.05.20】debounce — 4 個 duty future 完成順序不一，
                        # 同步寫 4 次同一檔會凍 UI 150-300ms。
                        self._schedule_save_cache('cache_duty_info.json', duty_cache)
                    case UiTodayVsMessage(doctor_name=vsn):
                        duty_cache = self._duty_cache_mem_ensure()
                        duty_cache['date'] = date.today().strftime("%Y-%m-%d")
                        txt = f"當日值班VS: {vsn}"
                        self.duty_vs_var.set(txt)
                        duty_cache['today_vs'] = txt
                        self._refresh_duty_summary_text()
                        self._duty_cache_mem = duty_cache
                        # 【效能 2026.05.20】debounce — 4 個 duty future 完成順序不一，
                        # 同步寫 4 次同一檔會凍 UI 150-300ms。
                        self._schedule_save_cache('cache_duty_info.json', duty_cache)
                    case UiSaturdayVsMessage(doctor_name=svn):
                        duty_cache = self._duty_cache_mem_ensure()
                        duty_cache['date'] = date.today().strftime("%Y-%m-%d")
                        txt = f"當週值班VS: {svn}"
                        self.saturday_duty_vs_var.set(txt)
                        duty_cache['saturday_vs'] = txt
                        self._refresh_duty_summary_text()
                        self._duty_cache_mem = duty_cache
                        # 【效能 2026.05.20】debounce — 4 個 duty future 完成順序不一，
                        # 同步寫 4 次同一檔會凍 UI 150-300ms。
                        self._schedule_save_cache('cache_duty_info.json', duty_cache)
                    case UiClockStatusMessage(status_data=payload, generation=gen):
                        self._on_clock_status_message(payload, gen)
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
        
    def update_clock_status_from_web(self, from_retry=False):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, lambda: self.update_clock_status_from_web(from_retry))
            return
        if getattr(self, '_shutting_down', False):
            return
        # [2026-07-15 跨夜] 新一波查詢（排程/跨日/啟動/手動）重置重試預算——每波最多
        # 連續自動重試 _CLOCK_RETRY_MAX 次；重試自身(from_retry=True)不重置，否則上限失效。
        if not from_retry:
            self._clock_status_retry_count = 0
        if hasattr(self, 'val_out_of_hospital') and self.val_out_of_hospital:
            logging.info("院外模式開啟中，跳過打卡狀態查詢 (需內網)")
            put_ui_message(self.ui_queue, UiClockStatusMessage(
                status_data=_clock_error("院外模式停用", CLOCK_ERR_DISABLED)))
            return
        if self._clock_status_worker_running:
            # [2026-06-26] 年齡保險:正常一輪查詢頂多幾十秒。若「正在查詢」已超過上限(疑似上一輪
            # 卡死、旗標沒歸零)→ 不再無限略過,視為上一輪已死、強制開新的一輪(自癒)。搭配 Part1
            # 的 page-load 逾時,理論上不會卡住;此為雙保險,避免任何意外讓打卡狀態跨日後永遠不更新。
            started = getattr(self, '_clock_status_worker_started_at', 0.0)
            if (time.time() - started) < _CLOCK_WORKER_MAX_AGE_SEC:
                logging.info("打卡狀態上一輪仍在查詢，略過重複請求")
                return
            logging.warning(
                "打卡狀態上一輪查詢疑似卡住(>%d秒)，強制開新一輪", _CLOCK_WORKER_MAX_AGE_SEC)

        # [GPT-5.6 P1 pass1] 世代序號在【發布 querying 之前】就遞增,並讓後續 worker 結果
        # 一律帶 gen 由主緒消費端閘控 → 晚到的舊世代結果一定排在新 querying 之後也會被拒。
        self._clock_status_generation += 1
        gen = self._clock_status_generation
        put_ui_message(self.ui_queue,
                       UiClockStatusMessage(status_data='querying'))

        # [修正] 從設定檔讀取，不把帳密寫死在程式碼裡
        # [v18 2026-05-25] base64 decode 加保護 — credentials.json 部分損壞 /
        # user 手動編輯成非 base64 → 原本 binascii.Error 冒泡，整個打卡查詢功能掛掉。
        # 改成 decode 失敗 fallback 寫回預設帳號 (跟首次使用同流程)。
        import base64
        cred_path = get_conf_path('credentials.json')
        DEFAULT_USERNAME = "101358"
        DEFAULT_PASSWORD = "101AA358"

        def _write_default_credentials() -> None:
            try:
                _atomic_write_json(cred_path, {
                    'u': base64.b64encode(DEFAULT_USERNAME.encode()).decode(),
                    'p': base64.b64encode(DEFAULT_PASSWORD.encode()).decode()
                })
                logging.info("已寫入預設打卡帳號（%s）到 credentials.json",
                             DEFAULT_USERNAME)
            except Exception:
                logging.warning("寫入預設打卡帳號失敗", exc_info=True)

        try:
            if os.path.exists(cred_path):
                cred = load_json_dict(cred_path, {}, merge_defaults=False)
                try:
                    username = base64.b64decode(cred.get('u', '')).decode('utf-8')
                    password = base64.b64decode(cred.get('p', '')).decode('utf-8')
                except (ValueError, TypeError, UnicodeDecodeError) as decode_err:
                    # binascii.Error / padding 錯 / 非 utf-8 都進這條路
                    logging.warning(
                        "credentials.json base64 decode 失敗 (%s)，"
                        "fallback 寫回預設帳號", decode_err)
                    username = DEFAULT_USERNAME
                    password = DEFAULT_PASSWORD
                    _write_default_credentials()
            else:
                # 首次使用：直接寫入預設帳密
                username = DEFAULT_USERNAME
                password = DEFAULT_PASSWORD
                _write_default_credentials()
        except Exception as e:
            logging.error(f"讀取打卡帳號失敗: {e}")
            put_ui_message(self.ui_queue, UiClockStatusMessage(
                status_data=_clock_error("帳號讀取失敗", CLOCK_ERR_TRANSIENT)))
            return

        logging.info(f"Starting background clock status check for {username}...")

        # [GPT-5.6 P1 pass1] worker 不再自行比對 gen / 清旗標(check-then-act 跨緒非原子)。
        # 改為:worker 只【一律發布】帶自己 gen 的結果訊息;主緒消費端(_on_clock_status_message,
        # 唯一改 generation 者)比對 gen → 過時世代直接拒收、gen 相符才套用結果並清 running
        # 旗標。比對與清旗標同在主緒 = 原子,徹底消除「舊 worker 晚到覆寫新結果/誤清新旗標」。
        def run_check(gen=gen):
            try:
                status_result = _get_swipe_status_from_web(username, password)
            except Exception:
                logging.exception("打卡狀態背景查詢失敗")
                status_result = _clock_error("查詢失敗", CLOCK_ERR_TRANSIENT)
            put_ui_message(self.ui_queue, UiClockStatusMessage(
                status_data=status_result, generation=gen))

        self._clock_status_worker_running = True
        self._clock_status_worker_started_at = time.time()   # [2026-06-26] 給年齡保險判斷用
        clock_future = self.bg_executor.submit(run_check)

        def _handle_clock_submit_rejected(fut, gen=gen):
            try:
                rejected = fut.cancelled() or isinstance(fut.exception(), RejectedExecutionError)
            except Exception:
                rejected = False
            if rejected:
                logging.warning("打卡狀態背景查詢未啟動：背景佇列已滿")
                # 帶 gen:主緒消費端會據此清旗標(gen 相符時)+套用錯誤,不在此跨緒直接清旗標
                put_ui_message(self.ui_queue, UiClockStatusMessage(
                    status_data=_clock_error("背景忙碌", CLOCK_ERR_TRANSIENT),
                    generation=gen))

        clock_future.add_done_callback(_handle_clock_submit_rejected)

    # ── [2026-07-15 跨夜] 打卡查詢失敗自動重試 ──────────────────────────────
    _CLOCK_RETRY_DELAY_MS = 3 * 60 * 1000   # 失敗後 3 分鐘重試
    _CLOCK_RETRY_MAX = 5                    # 連續失敗上限（成功歸零；之後等下個排程/跨日）

    def _maybe_retry_clock_status(self, err, kind=CLOCK_ERR_TRANSIENT) -> None:
        """打卡查詢失敗的自癒重試。排程一天只有 07:40/08:00/17:03（+跨日/啟動），
        任何一次暫時性失敗（portal 慢、閒置 alert race、Chrome renderer 死）原本會
        灰燈掛到下個排程——程式放跨夜時＝整個上午看不到打卡狀態。改為失敗後 3 分鐘
        自動重試、連續上限 5 次（配 _discard_status_driver 每輪都是全新 Chrome）。
        只由 UI queue 消費端（主緒）呼叫 → root.after 安全；重排前取消前一顆不堆疊。

        [GPT-5.6 P1] 只重試 transient(逾時/driver/網路);auth(帳密錯)與 disabled(院外模式)
        絕不重試——否則錯密碼會被每波 5 次反覆送出而【鎖帳號】。未帶 kind → 視為 transient
        (相容;所有 auth 路徑已在 _get_swipe_status_from_web 明確標記)。"""
        if kind != CLOCK_ERR_TRANSIENT:
            # [GPT-5.6 P1 pass1] auth/disabled 不只是「不再排新重試」——還要【取消先前
            # transient 失敗已排下的重試】,否則那顆 3 分鐘 callback 仍會帶已知錯的帳密重登
            # → 破壞「auth 絕不重試」保證、多送一次鎖帳號嘗試。
            logging.info("打卡狀態錯誤類型=%s（非暫時性）→ 不自動重試,並取消既有重試（%s）",
                         kind, err)
            self._cancel_clock_status_retry()
            return
        if self._clock_status_retry_count >= self._CLOCK_RETRY_MAX:
            logging.warning("打卡狀態連續失敗 %d 次，暫停自動重試（等下個排程/跨日再查）",
                            self._clock_status_retry_count)
            return
        self._clock_status_retry_count += 1
        self._cancel_clock_status_retry()
        self._clock_status_retry_after_id = self.root.after(
            self._CLOCK_RETRY_DELAY_MS, self._run_clock_status_retry)
        logging.info("打卡狀態查詢失敗（%s），3 分鐘後自動重試（第 %d/%d 次）",
                     err, self._clock_status_retry_count, self._CLOCK_RETRY_MAX)

    def _cancel_clock_status_retry(self) -> None:
        prev = self._clock_status_retry_after_id
        self._clock_status_retry_after_id = None
        if prev is not None:
            try:
                self.root.after_cancel(prev)
            except Exception:
                logging.debug("取消打卡重試排程失敗", exc_info=True)

    def _run_clock_status_retry(self) -> None:
        self._clock_status_retry_after_id = None
        if getattr(self, '_shutting_down', False):
            return
        self.update_clock_status_from_web(from_retry=True)

    def _on_clock_status_message(self, status_data, generation) -> None:
        """[GPT-5.6 P1 pass1] 打卡查詢訊息在主緒(唯一改 generation 者)的世代閘門。

        generation is None → 非 worker 結果(querying/停用/設定錯)→ 直接套用、不動旗標。
        generation 有值 → worker 結果:與目前世代不符即拒收(卡死舊 worker 晚到,不覆寫新
        一輪)；相符才清 running 旗標並套用。比對與清旗標同在主緒 = 原子,無跨緒 check-then-act
        競態(worker 只讀 generation、不寫)。"""
        if generation is not None:
            if generation != self._clock_status_generation:
                logging.info("打卡狀態舊世代(gen=%s)結果已過時,丟棄(現 gen=%s)",
                             generation, self._clock_status_generation)
                return
            # 這一輪(最新世代)的 worker 已回報 → 清單飛旗標(主緒,安全)
            self._clock_status_worker_running = False
        self._update_clock_status_ui(status_data)

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
            # [2026-07-15 跨夜／GPT-5.6 P1] 只有 transient 才 3 分鐘後自動重試;
            # auth(帳密錯,防鎖帳號)與 disabled(院外模式)不重試。未帶 kind 視為 transient。
            self._maybe_retry_clock_status(
                status_data["error"],
                status_data.get("error_kind", CLOCK_ERR_TRANSIENT))
            return

        # [關鍵修正] 確保 status_data 是字典才繼續，避免 AttributeError
        if not isinstance(status_data, dict):
            logging.error(f"Invalid status_data type received: {type(status_data)}")
            return

        # [2026-07-15 跨夜] 查詢成功 → 歸零連續失敗計數、取消 pending 重試
        self._clock_status_retry_count = 0
        self._cancel_clock_status_retry()

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

    def run_subsystem_in_thread(self, func, hotkey_name, preempt_same=False):
        is_busy = False
        show_busy_notice = False
        # 僅在 else 分支(非忙碌)會被覆寫；忙碌時提前 return 不會用到。
        # 預先綁定以消除靜態分析的 possibly-unbound 雜訊（行為不變）。
        subsystem_token = 0
        is_preempt = False  # 本次是否為「同熱鍵搶占」(F11 執行中又按 F11)
        with self._subsystem_lock:
            if self._subsystem_running:
                if preempt_same and self._subsystem_current_hotkey == hotkey_name:
                    # [2026-06-15] 同一支熱鍵(F11)執行中又被按下 → 終止前一次、改從這次
                    # 重新開始(其餘熱鍵維持「忙碌略過」)。等同對舊流程做一次 F12:把舊
                    # thread 加入 per-thread 取消集合(check_stop 會讓它 bail),並 bump token
                    # 讓舊流程的 finally/看門狗失效;本次直接接手 running。下方沿用既有
                    # 「取得流程後 clear stop_event」邏輯讓本次乾淨開跑,舊流程靠取消集合
                    # 終止。注意:本專案在「等醫師回應對話框」期間本就允許他鍵重入,故不
                    # 以鎖強制互斥(那會破壞該既有行為);搶占與既有 F12→他鍵 行為一致。
                    is_preempt = True
                    old_ident = self._subsystem_thread_ident
                    if old_ident is not None:
                        with _hotkey_cancelled_threads_lock:
                            _hotkey_cancelled_threads.add(old_ident)
                    # 清殘留 stop_event 必須在「同一臨界區」內完成(不可放到鎖外):否則被
                    # 後續搶占而失效的本呼叫,仍會在鎖外執行 clear、抹掉給新流程的 F12。
                    # 此處 running 本就為 True(舊流程),clear 只會抹掉「針對舊流程」的 F12,
                    # 而舊流程已用取消集合中止 → 等同 F12 之效,故安全;給新流程的 F12 會在
                    # 本臨界區之後、running 仍為 True 時送達,worker 的 check_stop 會看到。
                    stop_event_automation.clear()
                    self._subsystem_token += 1
                    subsystem_token = self._subsystem_token
                    self._subsystem_running = True   # 維持佔用,直接接手
                    self._subsystem_current_hotkey = hotkey_name
                    self._subsystem_thread_ident = None
                    self._subsystem_thread = None
                else:
                    is_busy = True
                    now = time.monotonic()
                    if should_show_busy_notice(
                        now, getattr(self, '_last_hotkey_busy_notice_at', 0.0),
                    ):
                        self._last_hotkey_busy_notice_at = now
                        show_busy_notice = True
            else:
                # 先 clear 再發佈 running:F12 只在 running=True 時設 stop_event,running 在
                # clear 之後才為真,故此 clear 不會抹掉本次取得後使用者新按的 F12;且 clear
                # 在臨界區內,被後續搶占而失效的呼叫不會在鎖外另行 clear 抹掉新流程的 F12。
                stop_event_automation.clear()
                self._subsystem_running = True
                self._subsystem_token += 1
                subsystem_token = self._subsystem_token
                self._subsystem_current_hotkey = hotkey_name

        if is_busy:
            put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 前一個熱鍵流程尚未完成'))
            if show_busy_notice:
                self._show_notice("熱鍵忙碌中", f"{hotkey_name} 已略過，請等待目前自動化完成。", level="warn", auto_close_ms=2500)
            return

        if is_preempt:
            put_ui_message(self.ui_queue, UiStatusMessage(
                text=f'狀態: {hotkey_name} - 偵測到再次按下，終止前次流程並重新開始'))

        def wrapper():
            my_ident = threading.get_ident()
            try:
                # 在本緒內以 token 守衛註冊身分(取代「thread.start() 之後才在外面註冊」),
                # 消除啟動與註冊之間被第二次 F11 搶占時讀到 ident=None、無法取消前一流程
                # 的競態。若註冊前 token 已前進=已被後續同熱鍵搶占 → 直接放棄本次。
                with self._subsystem_lock:
                    if self._subsystem_token != subsystem_token:
                        logging.info("[hotkey] %s 啟動前已被後續搶占，放棄本次", hotkey_name)
                        return
                    self._subsystem_thread_ident = my_ident
                    self._subsystem_thread = threading.current_thread()
                logging.info(f"Starting subsystem from {hotkey_name}...")
                put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 執行中...'))
                # 啟動前若已被 F12(stop_event)或後續搶占(取消集合)中止 → 先行 bail。
                check_stop()
                result = func()
                if result is False:
                    logging.warning("Subsystem from %s returned incomplete status", hotkey_name)
                    put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 操作未完成，請檢查畫面'))
                else:
                    put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 操作完成'))
            except SubsystemInterrupted as e:
                logging.warning(f"Subsystem stopped: {e}")
                put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 已由F12手動終止'))
            except Exception:
                logging.exception(f"Error in '{hotkey_name}'")
                put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: {hotkey_name} - 發生未預期錯誤'))
            finally:
                with self._subsystem_lock:
                    if getattr(self, '_subsystem_thread_ident', None) == threading.get_ident():
                        self._subsystem_thread_ident = None
                    # [stability] 只在「仍是本流程 token」時才復位，避免本 thread 若曾卡死、
                    # 被硬上限看門狗強制復位、之後又有新熱鍵啟動(token 前進)時,這個遲來的
                    # finally 誤清掉新流程的 running 旗標。
                    if self._subsystem_token == subsystem_token:
                        self._subsystem_running = False
                        self._subsystem_thread = None
                        self._subsystem_current_hotkey = None
                # 在「復位 running 之後」才從取消集合移除本緒 ident:否則在「移除 →
                # running=False」之間,搶占路徑(僅在 running=True 時)可能把本(即將結束的)
                # ident 又加回取消集合而永久殘留;該 ident 日後被新 thread 重用時,新流程會
                # 無故立即中止。復位後 running=False,搶占不再以本流程為對象,此時移除才安全。
                with _hotkey_cancelled_threads_lock:
                    _hotkey_cancelled_threads.discard(threading.get_ident())
                time.sleep(2)
                with self._subsystem_lock:
                    emit_idle = should_emit_idle_status(
                        self._subsystem_token,
                        subsystem_token,
                        subsystem_running=self._subsystem_running,
                    )
                if emit_idle:
                    put_ui_message(self.ui_queue, UiStatusMessage(text='狀態: 閒置'))
        # 身分註冊改在 wrapper 內(其首個動作,token 守衛)完成 —— 見上,以消除
        # 「啟動→註冊」之間的搶占競態。此處只負責啟動。
        thread = threading.Thread(target=wrapper, name=f"{hotkey_name}_Thread", daemon=True)
        thread.start()

        # [stability][W1 2026-07-03] 硬上限看門狗：流程若卡住超過 HOTKEY_HARD_TIMEOUT_SEC，
        # wrapper 的 finally 跑不到 → _subsystem_running 永遠 True。
        # 【安全變更】舊版逾時後會「強制解鎖」讓其他熱鍵恢復,但卡住的 worker 仍活著、
        # 可能正卡在 HIS 半寫入 → 第二支熱鍵並行寫同一病歷/醫令 = billing 錯亂。
        # 改為:worker thread 還活著就【絕不解鎖】,只警告醫師(F12/重啟);worker 一旦
        # 結束或 HIS 恢復,其 finally 會自行清旗標、熱鍵自動恢復。只有「旗標殘留但 thread
        # 已死」(finally 沒清到,罕見)才由看門狗代清。搭配 W2 主視窗尋找逾時,多數卡死
        # 已能自解,真正需要重啟的永久死結才會維持鎖定。timeout 180s 避免誤判慢流程。
        HOTKEY_HARD_TIMEOUT_SEC = 180

        def _hotkey_hard_timeout_watch():
            time.sleep(HOTKEY_HARD_TIMEOUT_SEC)
            while True:
                with self._subsystem_lock:
                    still_ours = (self._subsystem_running
                                  and self._subsystem_token == subsystem_token)
                    alive = thread.is_alive()
                    awaiting = still_ours and alive and _hotkey_awaiting_user
                    action = _hotkey_watchdog_action(still_ours, alive, awaiting)
                    if action == "clear_dead":
                        self._subsystem_running = False
                        self._subsystem_thread = None
                if action == "gone":
                    return  # 流程已正常結束或被後續熱鍵取代
                if action == "clear_dead":
                    logging.critical(
                        "[hotkey-watchdog] %s worker 已結束但 _subsystem_running 殘留 "
                        "→ 已代為清除,恢復熱鍵。", hotkey_name)
                    return
                if action == "keep_awaiting":
                    put_ui_message(self.ui_queue, UiStatusMessage(
                        text=f'狀態: {hotkey_name} 等待醫師回應中，按 F12 可取消等待'))
                    time.sleep(HOTKEY_HARD_TIMEOUT_SEC)
                    continue
                # action == "keep_stuck":卡住且 thread 仍活著 → 維持鎖定,絕不放第二支
                # 熱鍵並行操作 HIS。持續警告直到 worker 結束(HIS 恢復)或使用者重啟。
                logging.critical(
                    "[hotkey-watchdog] %s 執行超過 %ds 仍在跑且未在等醫師回應(疑似卡在"
                    "無逾時跨行程呼叫/HIS freeze)→ 維持熱鍵鎖定以免第二支熱鍵並行操作"
                    "HIS;請按 F12,無效則重新啟動程式(卡住工作緒無法強制 kill)。",
                    hotkey_name, HOTKEY_HARD_TIMEOUT_SEC)
                put_ui_message(self.ui_queue, UiStatusMessage(
                    text=f'狀態: {hotkey_name} 卡住未結束，熱鍵暫停中，請按 F12 或重啟程式'))
                time.sleep(HOTKEY_HARD_TIMEOUT_SEC)  # 再等一個週期重評(worker 可能恢復)

        threading.Thread(target=_hotkey_hard_timeout_watch,
                         name=f"{hotkey_name}_HardTimeout", daemon=True).start()

    def interrupt_automation(self):
        if not should_emit_interrupt(
            getattr(self, '_subsystem_running', False),
            stop_already_requested=stop_event_automation.is_set(),
        ):
            logging.debug("Received F12 but no automation is running; ignored.")
            return
        logging.warning("Received F12: Interrupting...")
        stop_event_automation.set()
        if _hotkey_awaiting_user:
            with self._subsystem_lock:
                thread_ident = getattr(self, '_subsystem_thread_ident', None)
                if thread_ident is not None:
                    with _hotkey_cancelled_threads_lock:
                        _hotkey_cancelled_threads.add(thread_ident)
                if self._subsystem_running:
                    self._subsystem_running = False
                    self._subsystem_token += 1
            put_ui_message(self.ui_queue, UiStatusMessage(
                text="狀態: F12 終止 - 已解除等待醫師回應，請關閉確認視窗"))
        else:
            put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: F12 終止 - 正在中斷目前操作..."))


    def _open_main_script_at_line(self, line_no: int):
        """以外部編輯器開啟主檔（勿用 os.startfile(.pyw) 以免又啟動一個程式）。"""
        path = os.path.abspath(os.path.realpath(__file__))
        for args in (
            ["cursor", "-g", f"{path}:{line_no}"],
            ["code", "-g", f"{path}:{line_no}"],
        ):
            exe = shutil.which(args[0])
            if not exe:
                continue
            try:
                subprocess.Popen(args, close_fds=os.name != "nt")
                return
            except Exception as e:
                logging.debug("以 %s 開啟腳本失敗: %s", args[0], e)
        if os.name == "nt":
            ntp = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "notepad.exe")
            if os.path.isfile(ntp):
                try:
                    subprocess.Popen([ntp, path], close_fds=False)
                    return
                except Exception as e:
                    logging.debug("notepad 開啟失敗: %s", e)
        try:
            subprocess.Popen(["xdg-open", path], close_fds=True)
        except Exception as e:
            messagebox.showerror("無法開啟", f"無法開啟主程式檔案供編輯：\n{e}\n\n{path}")


    def setup_hotkeys(self):
        if getattr(self, '_shutting_down', False) or stop_event_main.is_set():
            return
        if threading.current_thread() is not threading.main_thread():
            if not getattr(self, '_shutting_down', False):
                self.root.after(0, self.setup_hotkeys)
            return
        if not self._heavy_modules_ready or hotkey_modules.keyboard is None:
            self.hotkey_text_label.config(text="熱鍵模組載入中...")
            self.status_text.set("狀態: 熱鍵模組尚未就緒")
            return
        # [新增] 院外模式檢查
        if hasattr(self, 'out_of_hospital_var') and self.out_of_hospital_var.get():
            logging.info("院外模式啟用中，跳過熱鍵註冊。")
            safe_unhook_all_hotkeys()
            # 縮寫速寫獨立於 HIS 模式：unhook_all 後重掛
            try:
                self._install_abbrev_listeners()
            except Exception:
                logging.exception("[abbrev] 院外模式 setup 路徑 install 失敗")
            configure_hotkey_scaling(False, None, None)
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
            safe_unhook_all_hotkeys()
            # 縮寫速寫獨立於熱鍵解析度：unhook_all 後重掛
            try:
                self._install_abbrev_listeners()
            except Exception:
                logging.exception("[abbrev] 解析度不符路徑 install 失敗")
            configure_hotkey_scaling(False, None, None)
            put_ui_message(self.ui_queue, UiStatusMessage(text='狀態: 解析度不符，熱鍵已停用'))
            self.hotkey_text_label.config(text="熱鍵已停用 (解析度不符)")
            return

        # [清理 2026-05-19] 移除 configure_hotkey_scaling 呼叫 — F1-F11 已全
        # adaptive (Win32 訊息，跨解析度 hwnd-based)，座標縮放邏輯已死。
        self.hotkey_display_note.set("")

        try:
            hotkeys_to_register = {}
            hotkey_info_text = ""
            # 統一熱鍵 (F1-F5 + F9 + F10 + F11 全 adaptive, 跨解析度)
            _adaptive_descs = {
                'F1':  (script_F1_adaptive,  "F1: 照光(1) — 51019+療程1"),
                'F2':  (script_F2_adaptive,  "F2: 照光(2) — 51019+療程2"),
                'F3':  (script_F3_adaptive,  "F3: 照光(3) — 51019+療程3"),
                'F4':  (script_F4_adaptive,  "F4: 冷凍 — 51017"),
                'F5':  (script_F5_adaptive,  "F5: KOH — 13017"),
                'F8':  (script_F8_quick_text, "F8: 快速輸入文字 (設定頁可改)"),
                'F9':  (script_F9_adaptive,  "F9: 腫瘤同意書"),
                'F10': (script_F10_adaptive, "F10: 切片同意書"),
                'F11': (script_F11_adaptive, "F11: 快速完成 (全部完成→疼痛→預約)"),
            }
            hotkey_info_text = ("F1:照光(1) F2:照光(2) F3:照光(3) F4:冷凍 F5:KOH\n"
                                "F8:快速輸入 F9:腫瘤 F10:切片 F11:快速完成 F12:中止")
            if profile in ('1920x1080', '1280x1024', '1024x768'):
                hotkeys_to_register = dict(_adaptive_descs)

            # ─── 熱鍵守門：F1-F5 嚴格 (僅 TFopdmain)；F9-F12 寬鬆 (含醫院子視窗) ────
            # 使用者反映：
            #   1. F5 在 Chrome 會卡到瀏覽器刷新 → suppress=False (放過 keypress)
            #   2. F1-F5 在 TfrmOpdCS (排檢) 不能執行 → 必須嚴格 (only TFopdmain)
            #   3. F11/F9/F10/F12 在醫院子視窗 (popup/同意書/警告 dialog) 點要
            #      還能觸發 → 寬鬆，允許所有已知醫院 class
            #   4. F8 (快速輸入文字) 要在「任何 app」都能觸發 (含瀏覽器登入欄位)
            #      → 完全不檢查 class
            HOTKEY_STRICT_CLASSES = {"TFopdmain"}
            HOTKEY_LENIENT_CLASSES = {
                "TFopdmain",       # 主視窗
                "TOrMain",         # 同意書視窗
                "Tfm_agree",       # 列印同意 popup
                "TfrmOrrSentence", # 片語 popup
                "#32770",          # Windows 標準對話框 (警告/確認等)
                "TFOpdMsg1",       # 疼痛指數
                "TFOPDPreg",       # 預約掛號
                "TfAskDlg",        # 乳房篩檢 / 一般 ask
                "TfAskDlg2",       # 健保藥費管控
                "TMessageForm",    # Delphi 通用 message box
                "TFTunMsg",        # 轉診提示
                "TFAllergyB",      # 藥物過敏記錄
                "TFrmAllergyM01",  # 過敏記錄維護-醫師端
            }
            STRICT_HOTKEYS = {'F1', 'F2', 'F3', 'F4', 'F5'}
            NO_GUARD_HOTKEYS = {'F8'}  # 跳過 class 檢查，任何 app 都能觸發

            def _hotkey_guard(action_fn, key_name, strict):
                allow = HOTKEY_STRICT_CLASSES if strict else HOTKEY_LENIENT_CLASSES
                tag = "(嚴格)" if strict else "(寬鬆)"
                def _wrapped():
                    try:
                        fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
                        if not fg_hwnd:
                            return
                        cls_buf = ctypes.create_unicode_buffer(64)
                        ctypes.windll.user32.GetClassNameW(fg_hwnd, cls_buf, 64)
                        if cls_buf.value not in allow:
                            logging.debug(
                                "[hotkey] %s 觸發但前景=%r 不在 allow list %s → skip",
                                key_name, cls_buf.value, tag)
                            return
                        # [L5 2026-07-09] #32770 是【任何程式】共用的 Windows 標準對話框 class;
                        # 只憑 class 放行會讓別的程式的對話框在前景時也能觸發 F9-F12(對 HIS 背景
                        # 亂動)。額外要求該 #32770 確實屬於 HIS 行程,否則 skip。其餘 class 都是 HIS
                        # 專屬,不需此檢查。找不到 HIS/取不到 PID → 保守 skip。
                        if cls_buf.value == "#32770":
                            his_hwnd = _find_hospital_main_window()
                            his_pid = _get_window_pid(his_hwnd) if his_hwnd else 0
                            if not his_pid or _get_window_pid(fg_hwnd) != his_pid:
                                logging.debug(
                                    "[hotkey] %s 前景 #32770 不屬 HIS 行程 → skip",
                                    key_name)
                                return
                    except Exception:
                        logging.debug("[hotkey] 前景偵測失敗，保險 skip",
                                       exc_info=True)
                        return
                    action_fn()
                return _wrapped

            safe_unhook_all_hotkeys()
            for key, (func, name) in hotkeys_to_register.items():
                f_use = func
                # F11(快速完成)執行中再按 F11 → 終止前一次、改從這次重新開始;
                # 其餘熱鍵維持「忙碌中略過」。
                _preempt = (key == 'F11')
                action = lambda f=f_use, n=name, p=_preempt: self.run_subsystem_in_thread(f, n, preempt_same=p)
                if key in NO_GUARD_HOTKEYS:
                    # 完全跳過 class guard — 任何 app 都觸發 (e.g. F8 在瀏覽器)
                    callback = action
                else:
                    strict = key in STRICT_HOTKEYS
                    callback = _hotkey_guard(action, key, strict)
                hotkey_modules.keyboard.add_hotkey(
                    key,
                    callback,
                    suppress=False,
                )
            # F12 (中止) 是救援鍵：自動化執行中不做前景限制；平常仍用寬鬆 guard。
            f12_guarded = _hotkey_guard(
                self.interrupt_automation, 'F12', strict=False)

            def _f12_callback():
                if should_bypass_foreground_guard(
                    'F12',
                    subsystem_running=getattr(self, '_subsystem_running', False),
                ):
                    self.interrupt_automation()
                    return
                f12_guarded()

            hotkey_modules.keyboard.add_hotkey(
                'F12',
                _f12_callback,
                suppress=False,
            )
            
            self.hotkey_text_label.config(text=hotkey_info_text)
            put_ui_message(self.ui_queue, UiStatusMessage(text=f'狀態: 熱鍵註冊成功 ({profile})，等待指令...'))
            logging.info(f"Hotkeys registered successfully for {profile}.")
            # [穩定性] 註冊成功 → 重置 retry 計數
            self._hotkey_register_retry_count = 0
            # 縮寫速寫：F1-F12 註冊完成後重掛 abbrev hook（unhook_all 會清掉）
            try:
                self._install_abbrev_listeners()
            except Exception:
                logging.exception("[abbrev] setup_hotkeys 結尾 install 失敗")
        except Exception as e:
            logging.error(f"Failed to register hotkeys: {e}", exc_info=True)
            put_ui_message(self.ui_queue, UiStatusMessage(text='狀態: 熱鍵註冊失敗! 請檢查權限'))
            self.hotkey_text_label.config(text="熱鍵註冊失敗!")
            es = str(e)
            self.hotkey_display_note.set(f"熱鍵註冊失敗 · {es[:42]}…" if len(es) > 42 else f"熱鍵註冊失敗 · {es}")
            # [穩定性] 失敗自動 retry：30 秒後重試，最多 5 次 (給管理員授權 / 鍵盤
            # hook 隊伍清空時間)。超過 5 次就放棄，使用者要手動重啟主程式。
            try:
                self._hotkey_register_retry_count = getattr(
                    self, '_hotkey_register_retry_count', 0) + 1
                if self._hotkey_register_retry_count <= 5:
                    delay_ms = 30 * 1000
                    logging.warning(
                        "熱鍵註冊失敗，第 %d 次重試將在 30s 後 (最多 5 次)",
                        self._hotkey_register_retry_count)
                    if not getattr(self, '_shutting_down', False):
                        self.root.after(delay_ms, self.setup_hotkeys)
                else:
                    logging.error(
                        "熱鍵註冊已連續失敗 %d 次，放棄自動 retry — 請手動重啟主程式",
                        self._hotkey_register_retry_count)
            except Exception:
                logging.debug("hotkey retry 排程失敗", exc_info=True)

    def _on_hotkey_heartbeat_event(self, _event=None):
        """heartbeat hook callback：每個按鍵事件都會觸發。必須極快（跑在 keyboard
        listener thread，太慢會害 LL hook 被 Windows timeout 移除——正是我們要防的）。
        只記一個時間戳，不做任何其他事。"""
        self._hk_last_event_monotonic = time.monotonic()

    def _install_hotkey_heartbeat(self):
        """(重)掛一個輕量全域鍵盤 hook，對每個按鍵事件蓋時間戳。守護程式用它跟
        OS 層級的輸入時間比對，偵測「Windows 已靜默移除我們的 hook」。任何
        unhook_all() 之後都要重掛。本方法可重複呼叫（先移除舊把手再掛，不累積）。"""
        kb = hotkey_modules.keyboard
        if kb is None:
            return
        old = getattr(self, '_hk_heartbeat_handle', None)
        if old is not None:
            try:
                kb.unhook(old)
            except Exception:
                pass  # unhook_all 可能已清掉它，把手失效屬正常
            self._hk_heartbeat_handle = None
        try:
            self._hk_heartbeat_handle = kb.hook(self._on_hotkey_heartbeat_event)
            # 剛掛好視為「剛確認存活」，避免裝好瞬間就被誤判為安靜
            self._hk_last_event_monotonic = time.monotonic()
            self._hk_dead_strikes = 0
        except Exception:
            logging.exception("[hotkey] heartbeat hook 安裝失敗")
            self._hk_heartbeat_handle = None

    def _probe_hotkey_hook_alive(self, timeout_sec: float = 0.6) -> bool:
        """主動探針：注入一個無副作用的 F24，看 heartbeat hook 有沒有在
        timeout 內捕捉到（hook 活著一定攔得到自己注入的鍵；被 Windows 移除則攔不到）。
        判定方式不依賴鍵名解析，只看 heartbeat 時間戳有沒有前進，較穩健。
        無法注入/keyboard 未就緒時回 True（無從判定就不誤殺）。"""
        kb = hotkey_modules.keyboard
        if kb is None:
            return True
        before = getattr(self, '_hk_last_event_monotonic', 0.0)
        try:
            from cmuh_common.abbrev_engine import inject_vk_tap
            if not inject_vk_tap(PROBE_VK):
                return True  # SendInput 沒送出，無從判定，不誤殺
        except Exception:
            logging.debug("[hotkey] 探針注入失敗", exc_info=True)
            return True
        deadline = time.monotonic() + max(0.1, timeout_sec)
        while time.monotonic() < deadline:
            if getattr(self, '_hk_last_event_monotonic', 0.0) != before:
                return True  # heartbeat 前進 → hook 活著（攔到了注入的鍵）
            time.sleep(0.03)
        return False

    def _hotkey_health_tick(self):
        """守護程式每輪：偵測全域熱鍵 hook 是否已失效；確認失效且閒置時自動重啟。"""
        # 自動化執行中跳過：(1) 此時熱鍵剛觸發過、hook 必然活著；(2) 不在自動化
        # 流程中途注入 F24 探針，避免干擾。
        if (getattr(self, '_shutting_down', False)
                or getattr(self, '_subsystem_running', False)
                or not getattr(self, '_heavy_modules_ready', False)
                or hotkey_modules.keyboard is None):
            return
        has_profile = bool(getattr(self, 'hotkey_profile', None)
                           or getattr(self, 'hotkey_version', None))
        # [fix C 2026-06-09] 縮寫速寫獨立於 HIS 模式：院外模式/解析度不符雖沒掛 F 鍵，
        # 縮寫 hook 仍可能在跑(共用同一個 keyboard 底層 hook)。原本 has_profile=False
        # 直接 return → 這種模式下 hook 被 Windows 移除(LowLevelHooksTimeout)時縮寫
        # 無聲失效、沒人偵測。改為「有 F 鍵 profile 或 縮寫啟用中」都要監看。
        _abbrev_cfg = getattr(self, '_abbrev_config_cache', None)
        abbrev_active = bool(_abbrev_cfg is not None
                             and getattr(_abbrev_cfg, 'enabled', False))
        if not has_profile and not abbrev_active:
            return  # 沒 F 鍵熱鍵也沒啟用縮寫：無 hook 可監看

        now = time.monotonic()
        hook_silent = now - getattr(self, '_hk_last_event_monotonic', now)
        # 近期看過真實按鍵 → hook 確定活著，連 strike 歸零、不浪費注入探針
        if not should_probe_hook_health(hook_silent):
            self._hk_dead_strikes = 0
            return

        # hook 安靜一段時間（可能只是沒人打字，也可能 hook 死了）→ 主動探針確認
        if self._probe_hotkey_hook_alive():
            self._hk_dead_strikes = 0
            return

        self._hk_dead_strikes = getattr(self, '_hk_dead_strikes', 0) + 1
        logging.warning("[hotkey] 健康探針未回應（連續 %d 次）", self._hk_dead_strikes)
        if not is_hook_probe_failure_confirmed(self._hk_dead_strikes):
            return  # 單次可能是暫態 race，等下一輪再確認

        idle = system_idle_seconds()
        last_restart = getattr(self, '_hk_last_auto_restart_monotonic', 0.0)
        secs_since_restart = (now - last_restart) if last_restart else 1e9
        if should_auto_restart_for_dead_hook(
            hook_dead=True,
            shutting_down=getattr(self, '_shutting_down', False),
            subsystem_running=getattr(self, '_subsystem_running', False),
            modules_ready=getattr(self, '_heavy_modules_ready', False),
            system_idle_sec=idle,
            seconds_since_last_restart=secs_since_restart,
            restarts_this_session=getattr(self, '_hk_auto_restart_count', 0),
            # [SP-03 2026-07-12] idle 門檻 3s→30s:僅在使用者閒置較久才自動重啟,避免操作中打斷。
            # (重啟計數跨行程持久化屬 SP-03 之(b),涉 settings 檔隔離,緩修。)
            idle_required_sec=30.0,
        ):
            self._hk_last_auto_restart_monotonic = now
            self._hk_auto_restart_count = getattr(self, '_hk_auto_restart_count', 0) + 1
            logging.error(
                "[hotkey] 全域熱鍵 hook 已確認失效且使用者閒置 → 自動重啟程式以恢復"
                "（本 session 第 %d 次）", self._hk_auto_restart_count)
            put_ui_message(self.ui_queue, UiStatusMessage(
                text="狀態: 熱鍵失效，正在自動重啟以恢復…"))
            self.root.after(0, self._restart_app)
        else:
            logging.warning(
                "[hotkey] hook 已失效但暫不重啟（idle=%.1fs, automation=%s, "
                "已重啟=%d 次）— 待安全時機再處理",
                idle, getattr(self, '_subsystem_running', False),
                getattr(self, '_hk_auto_restart_count', 0))

    def run_hotkey_guardian(self):
        existing = getattr(self, "_hotkey_guardian_thread", None)
        if existing is not None and existing.is_alive():
            logging.debug("Hotkey guardian already running; duplicate start ignored.")
            return

        def guardian_loop():
            while not stop_event_main.is_set():
                if stop_event_main.wait(GUARDIAN_INTERVAL_SEC):
                    break
                try:
                    self._hotkey_health_tick()
                except Exception:
                    logging.exception("[hotkey] guardian tick 例外")

        self._hotkey_guardian_thread = threading.Thread(
            target=guardian_loop,
            name="HotkeyGuardian",
            daemon=True,
        )
        self._hotkey_guardian_thread.start()
    
    def _run_single_duty_query(self, fn, third_arg):
        s = _get_thread_local_duty_session()
        try:
            return bool(fn(self.ui_queue, s, third_arg))
        except Exception as e:
            logging.error(f"_fetch_all_duty_info: {fn.__name__} error: {e}", exc_info=True)
            return False

    def _fetch_all_duty_info(self, force=False):
        """並行查詢值班（forward01）。每筆獨立 Session，總耗時約為最慢一筆，而非四筆相加。
        若院內伺服器對併發敏感，可改回循序或改為 submit 單一 worker 內 for 迴圈。
        """
        if self.val_out_of_hospital:
            logging.info("院外模式開啟中，跳過所有值班查詢")
            return False
        today_str = date.today().isoformat()
        with self._duty_fetch_lock:
            if self._duty_fetch_worker_running:
                logging.info("值班資訊上一輪仍在查詢，略過重複請求")
                return False
            if (not force) and self._duty_last_fetch_date == today_str:
                logging.info("值班資訊今日已查詢，略過重抓（跨日才強制更新）")
                return True
            self._duty_fetch_worker_running = True

        try:
            duty_jobs = [
                (fetch_duty_doctor, self.r_doctor_map),
                (fetch_saturday_duty_doctor, self.r_doctor_map),
                (fetch_duty_vs, "today_vs"),
                (fetch_duty_vs, "saturday_vs"),
            ]
            all_succeeded = True
            with ThreadPoolExecutor(
                max_workers=len(duty_jobs),
                thread_name_prefix="DutyInfo",
            ) as duty_pool:
                futures = [
                    duty_pool.submit(self._run_single_duty_query, fetch_func, third_arg)
                    for fetch_func, third_arg in duty_jobs
                ]
                for f in as_completed(futures):
                    try:
                        if not f.result():
                            all_succeeded = False
                    except Exception as e:
                        all_succeeded = False
                        logging.error(f"_fetch_all_duty_info future: {e}", exc_info=True)
            if all_succeeded:
                self._duty_last_fetch_date = today_str
            else:
                logging.warning("值班資訊部分查詢失敗，不寫入今日完成快取")
            return all_succeeded
        finally:
            with self._duty_fetch_lock:
                self._duty_fetch_worker_running = False

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
        threshold_map = self._get_doctor_threshold_map(doctor_name)
        if not threshold_map:
            return False
        doc_no = ""
        for doc in self.doctors_list:
            if doc.get("name") == doctor_name:
                doc_no = str(doc.get("doc_no", ""))
                break
        for lookup_key in (doc_no, doctor_name):
            if not lookup_key:
                continue
            doc_data = data_source.get(lookup_key)
            if not doc_data or not isinstance(doc_data, dict) or "error" in doc_data:
                continue
            sessions = doc_data.get(today)
            if not sessions:
                continue
            return is_near_alert_threshold(sessions, weekday_idx, threshold_map, margin=10)
        return False

    def _doctor_alert_proximity_tier(self, doctor_name, doctors_data_snapshot=None):
        """[2026-07-13 使用者] 回傳醫師鄰近止掛門檻的等級（決定優先刷新間隔）：
          'critical' 已達門檻-3  → 每 10 分刷新該醫師一次（±10-20% 抖動）
          'mid'      已達門檻-5  → 每 15 分刷新該醫師一次（±10-20% 抖動）
          'near'     已達門檻-10 → 每 30 分刷新該醫師一次（±10-20% 抖動）
          None       皆非        → 不納入優先刷新（走一般 3 小時全體刷新）
        純讀當前快取，只刷該醫師、不連動其他醫師。"""
        if not doctor_name:
            return None
        threshold_map = self._get_doctor_threshold_map(doctor_name)
        if not threshold_map:
            return None
        today = date.today()
        weekday_idx = today.weekday()
        data_source = (doctors_data_snapshot if doctors_data_snapshot is not None
                       else self._get_all_doctors_data_snapshot())
        doc_no = ""
        for doc in self.doctors_list:
            if doc.get("name") == doctor_name:
                doc_no = str(doc.get("doc_no", ""))
                break
        for lookup_key in (doc_no, doctor_name):
            if not lookup_key:
                continue
            doc_data = data_source.get(lookup_key)
            if not doc_data or not isinstance(doc_data, dict) or "error" in doc_data:
                continue
            sessions = doc_data.get(today)
            if not sessions:
                continue
            # 由最接近門檻往外判：門檻-3→critical、門檻-5→mid、門檻-10→near。
            if is_near_alert_threshold(sessions, weekday_idx, threshold_map, margin=3):
                return "critical"
            if is_near_alert_threshold(sessions, weekday_idx, threshold_map, margin=5):
                return "mid"
            if is_near_alert_threshold(sessions, weekday_idx, threshold_map, margin=10):
                return "near"
            return None
        return None

    @staticmethod
    def _priority_refresh_interval_for_tier(tier: str) -> int:
        """優先刷新目標間隔(秒)，帶 ±(10%~20%) 隨機避免固定節拍（反 bot，同 reg64 隨機的
        理由）：門檻-3(critical)→ 10 分、門檻-5(mid)→ 15 分、門檻-10(near)→ 30 分，各
        套 ±10-20% 抖動（對齊 30 秒喚醒網格 → 實際觸發即此值、不回退基準）。未知 tier
        退回 near(30 分)基準。"""
        base = PRIORITY_REFRESH_TIER_BASE.get(tier, PRIORITY_REFRESH_TIER_BASE["near"])
        return _priority_refresh_interval_seconds(base)

# --- [修正] 背景任務啟動 (修正重開機邏輯) ---
    def start_background_tasks(self):
        if self._background_tasks_started:
            logging.warning("Background tasks already started; duplicate start ignored.")
            return
        self._background_tasks_started = True
        logging.info("Starting background tasks loop via ThreadPoolExecutor...")

        def _submit_startup_background(task_name, fn, *args, attempt=1):
            future = self.bg_executor.submit(fn, *args)

            def _retry_if_rejected(fut):
                try:
                    rejected = fut.cancelled() or isinstance(fut.exception(), RejectedExecutionError)
                except Exception:
                    rejected = False
                if not rejected or getattr(self, '_shutting_down', False):
                    return
                if attempt >= 3:
                    logging.error("[STARTUP] %s 放棄：背景佇列持續滿載", task_name)
                    return
                logging.warning("[STARTUP] %s 延後重試 (%d/3)：背景佇列已滿", task_name, attempt + 1)
                self._run_on_ui_thread(
                    lambda: self.root.after(
                        2000,
                        lambda: _submit_startup_background(
                            task_name,
                            fn,
                            *args,
                            attempt=attempt + 1,
                        ),
                    )
                )

            future.add_done_callback(_retry_if_rejected)
            return future

        # ===== 背景下載所有子程式 (manifest.json 列出的全部檔案) =====
        # 【穩定性 2026.05.20】改回 fire-and-forget。.result(timeout=180) 會阻塞
        # UI thread 等 GitHub raw，院內網路慢時 splash 卡 8-180s。子程式自己也會
        # 做 check_and_update，不必由主程式同步保證。
        _submit_startup_background("update-check", self.check_and_update, False)

        self.startup_phase_text.set("任務排程")

        def _startup_priority_refresh():
            """[O5 優化] 拆兩波啟動：BATCH_1（主院多）500ms 立即跑、
            BATCH_2（含院外 AUH/東區）1500ms 後再跑，讓首屏更快出現資料。"""
            if self._initial_priority_refresh_done:
                return
            by_name = {d.get("name"): d for d in DOCTORS}
            batch_1 = [by_name[n] for n in REFRESH_QUERY_BATCH_1 if n in by_name]
            batch_2 = [by_name[n] for n in REFRESH_QUERY_BATCH_2 if n in by_name]

            if batch_1:
                logging.info(f"[STARTUP] phase A refresh doctors={len(batch_1)} (主院優先)")
                self._startup_defer_full_until_priority_done = True
                self._trigger_refresh(False, batch_1)

            if batch_2:
                # 1.5 秒後跑第二波（含院外，timeout 已縮短為 2s 不會卡太久）
                def _phase_b():
                    logging.info(f"[STARTUP] phase B refresh doctors={len(batch_2)} (含院外)")
                    self._trigger_refresh(False, batch_2)
                self.root.after(1500, _phase_b)

            if not (batch_1 or batch_2):
                # 無 priority 醫師則直接跑全部
                self._trigger_refresh(False)

            self._initial_priority_refresh_done = True

        self.root.after(500, _startup_priority_refresh)
        self.root.after(1500, lambda: _submit_startup_background(
            "master-schedule", load_master_schedule_in_background, self.ui_queue))
        self.root.after(3500, self.update_clock_status_from_web)

        # 值班四筆在 _fetch_all_duty_info 內並行，每筆獨立 Session；啟動略提前以縮短首屏等待
        self.root.after(2500, lambda: _submit_startup_background(
            "duty-info", self._fetch_all_duty_info))

        # [O13] 啟動後 6 秒背景暖機 Chrome：使用者第一次按打卡狀態時 Chrome 已就緒（省 ~3 秒）
        # 條件：credentials.json 存在（使用者已設定過帳密）才暖機，避免無謂啟動
        def _prewarm_chrome():
            try:
                cred_path = get_conf_path('credentials.json')
                if not os.path.exists(cred_path):
                    logging.debug("[O13] credentials.json 不存在，跳過 Chrome 暖機")
                    return
                logging.info("[O13] 背景暖機 Chrome ...")
                d = _get_or_create_status_driver()
                if d:
                    logging.info("[O13] Chrome 暖機完成（下次打卡狀態查詢免等啟動）")
            except Exception:
                logging.debug("[O13] Chrome 暖機例外（忽略）", exc_info=True)
        self.root.after(6000, lambda: _submit_startup_background(
            "chrome-prewarm", _prewarm_chrome))

        # [O17] 啟動 30 秒後背景清理舊 cache、log 備份、tmp、過期 pyc
        try:
            from cmuh_common.cache_cleanup import schedule_cleanup_in_background
            schedule_cleanup_in_background(self.bg_executor, delay_seconds=30)
        except Exception:
            logging.debug("[O17] schedule_cleanup_in_background 失敗", exc_info=True)
        
        def run_schedule():
            def _future_was_rejected(future):
                if future is None or not hasattr(future, "done") or not future.done():
                    return False
                try:
                    return isinstance(future.exception(), RejectedExecutionError)
                except Exception:
                    return False

            def run_named_job(job_tag, fn):
                t0 = time.perf_counter()
                logging.info(f"[SCHEDULE:{job_tag}] started")
                try:
                    result = fn()
                    elapsed = time.perf_counter() - t0
                    if _future_was_rejected(result):
                        logging.warning(
                            f"[SCHEDULE:{job_tag}] skipped in {elapsed:.2f}s: background queue full"
                        )
                        return
                    logging.info(f"[SCHEDULE:{job_tag}] finished in {elapsed:.2f}s")
                except Exception as e:
                    elapsed = time.perf_counter() - t0
                    logging.error(f"[SCHEDULE:{job_tag}] failed in {elapsed:.2f}s: {e}", exc_info=True)
                finally:
                    pass

            def dynamic_cl_checker():
                # [2026-07-13 user] 依鄰近門檻等級分三級縮短該醫師的刷新間隔（只刷該醫師、不連動他人）：
                #   門檻-10(near)→ 每 30 分；門檻-5(mid)→ 每 15 分；門檻-3(critical)→ 每 10 分（各 ±10-20% 抖動）。
                #   tier 升/降級即刻改用新間隔。
                try:
                    doctors_data_snapshot = self._get_all_doctors_data_snapshot()
                    now_ts = time.time()
                    for doc in DOCTORS:
                        doc_name = doc.get("name")
                        if not doc_name:
                            continue
                        threshold_map = self._get_doctor_threshold_map(doc_name)
                        if not threshold_map:
                            continue  # 無設定門檻的醫師不納入
                        tier = self._doctor_alert_proximity_tier(
                            doc_name, doctors_data_snapshot=doctors_data_snapshot)
                        if tier is None:
                            self._priority_refresh_plan.pop(doc_name, None)  # 離開門檻區 → 清計畫
                            continue
                        plan = self._priority_refresh_plan.get(doc_name)
                        # tier 首見或改變(升/降級)→ 立刻以新 tier 的目標間隔評估，不等舊間隔跑完
                        if plan is None or plan[0] != tier:
                            target = self._priority_refresh_interval_for_tier(tier)
                            self._priority_refresh_plan[doc_name] = (tier, target)
                        else:
                            target = plan[1]
                        elapsed = now_ts - self._priority_refresh_last_check_time[doc_name]
                        if elapsed >= target:
                            logging.info(
                                f"[SCHEDULE:priority-check] 觸發優先刷新：{doc_name}"
                                f"（{tier} 且距上次≥{int(target // 60)}分）"
                            )
                            future = self.bg_executor.submit(self._trigger_refresh, False, [doc])
                            if _future_was_rejected(future):
                                logging.warning(
                                    f"[SCHEDULE:priority-check] 略過優先刷新：{doc_name}，背景佇列已滿"
                                )
                                continue
                            self._priority_refresh_last_check_time[doc_name] = now_ts
                            # 下一輪依當前 tier 重新隨機間隔，讓刷新時點不固定
                            self._priority_refresh_plan[doc_name] = (
                                tier, self._priority_refresh_interval_for_tier(tier))
                except Exception as e:
                    logging.error(f"[SCHEDULE:priority-check] failed: {e}", exc_info=True)

            # 鄰近門檻檢查：每 PRIORITY_REFRESH_CHECK_SECONDS(30s) 喚醒（細於最短基準 10 分，
            # 讓抖動忠實呈現、不被喚醒粒度侵蝕回固定節拍；內部依 tier 達目標間隔才觸發實際
            # refresh、且只讀快取+比對時間戳，30s 一輪成本極輕）。
            schedule.clear()
            schedule.every(PRIORITY_REFRESH_CHECK_SECONDS).seconds.do(
                dynamic_cl_checker).tag("priority-check", "30s")
            schedule.every(3).hours.do(
                lambda: run_named_job("refresh-all-3h", lambda: self.bg_executor.submit(self._trigger_refresh, False, DOCTORS))
            ).tag("refresh", "all-doctors", "3h")
            schedule.every(4).hours.do(
                lambda: run_named_job("duty-refresh-4h", lambda: self.bg_executor.submit(self._fetch_all_duty_info))
            ).tag("duty", "4h")
            
            # [2026-07-15 跨夜] 加 07:40:程式放跨夜時,打卡程式 07:31 自動打卡後原本要等
            # 08:00 才第一次查詢(每天重開程式的人啟動 +3.5s 就查,反而看得到)→ 07:40 查一次,
            # 早上到院即看到綠燈。08:00 保留(涵蓋 07:40-08:00 間的手動補打卡)。
            schedule.every().day.at("07:40").do(lambda: run_named_job("clock-status-0740", self.update_clock_status_from_web)).tag("clock", "daily")
            schedule.every().day.at("08:00").do(lambda: run_named_job("clock-status-0800", self.update_clock_status_from_web)).tag("clock", "daily")
            schedule.every().day.at("17:03").do(lambda: run_named_job("clock-status-1703", self.update_clock_status_from_web)).tag("clock", "daily")
            for update_time in AUTO_UPDATE_CHECK_TIMES:
                schedule.every().day.at(update_time).do(
                    lambda scheduled_at=update_time: run_named_job(
                        f"check-update-{scheduled_at.replace(':', '')}",
                        lambda: self._submit_update_check(False),
                    )
                ).tag("update-check", "daily-3x")

            _sched_last_date = date.today()   # [2026-06-26] 跨日偵測用
            while not stop_event_main.is_set():
                # [穩定性] run_pending 包 try/except：schedule 預設會把工作的例外往外拋，
                # 若任一工作(尤其直接註冊、未經 run_named_job 的 dynamic_cl_checker)拋例外，
                # 會中斷此常駐迴圈 → 當天所有定時刷新停擺。包起來保住心跳。
                try:
                    schedule.run_pending()
                except Exception:
                    logging.error("schedule.run_pending 例外（已忽略，保住排程迴圈）", exc_info=True)

                # [2026-06-26] 跨日強制刷新打卡狀態:程式開著跨過半夜時,主動觸發一次乾淨查詢(等於
                # 自動幫使用者「重開那一下」),不用等 08:00、也不用真的重開。00:00 一過幾秒內就更新成
                # 新的一天(尚未打卡前正常顯示未打卡,07:31 打卡後 08:00 排程那次會帶到)。
                try:
                    _today = date.today()
                    if _today != _sched_last_date:
                        _sched_last_date = _today
                        logging.info("[SCHEDULE] 偵測到跨日 → 強制刷新今日打卡狀態")
                        self.update_clock_status_from_web()
                except Exception:
                    logging.debug("跨日打卡刷新觸發失敗", exc_info=True)

                # [2026-05-25 v15 CPU 優化] 1s wait → 5s wait — master loop 是常駐
                # 背景 thread，1s 太密 (每分鐘醒 60 次但 schedule jobs 都是 2 分鐘
                # 以上精度)。改 5s 後 CPU 用量降 5x，shutdown 仍 ≤5s 內返回。
                if stop_event_main.wait(5.0):
                    break

        self._schedule_thread = threading.Thread(
            target=run_schedule,
            name="ScheduleLoop",
            daemon=True,
        )
        self._schedule_thread.start()
        self.run_hotkey_guardian()

    def _submit_update_check(self, is_manual=False):
        future = self.bg_executor.submit(self.check_and_update, is_manual)

        def _handle_update_submit_rejected(fut):
            try:
                rejected = fut.cancelled() or isinstance(fut.exception(), RejectedExecutionError)
            except Exception:
                rejected = False
            if not rejected:
                return
            logging.warning("更新檢查背景工作未啟動：背景佇列已滿")
            if is_manual:
                put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: 背景忙碌，請稍後再檢查更新"))
                put_ui_message(self.ui_queue, UiAlertErrorMessage(
                    title="更新檢查忙碌",
                    msg="目前背景工作較多，請稍後再試。",
                ))

        future.add_done_callback(_handle_update_submit_rejected)
        return future

    def check_and_update(self, is_manual=False):
        with self._update_check_lock:
            if self._update_check_running:
                logging.info("更新檢查上一輪仍在執行，略過重複請求")
                if is_manual:
                    put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: 更新檢查仍在執行中"))
                return False
            self._update_check_running = True

        try:
            return self._run_update_check(is_manual)
        finally:
            with self._update_check_lock:
                self._update_check_running = False

    def _run_update_check(self, is_manual=False):
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
                    # [MG-02] 重啟改走閘門(熱鍵忙碌時會延後),用字改「將於目前操作結束後」以免與實際不符。
                    msg = ("以下程式已更新完成：\n\n" + "\n".join(msg_lines)
                           + "\n\n將於目前操作結束後自動重新啟動。")
                    put_ui_message(self.ui_queue, UiAlertInfoMessage(
                        title="更新完成", msg=msg, need_restart=True))
                else:
                    logging.info("Auto-update applied. Requesting restart on UI thread...")
                    put_ui_message(self.ui_queue, UiAlertInfoMessage(
                        title="自動更新完成", msg="已套用自動更新，將於目前操作結束後自動重新啟動。",
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

# --- 主程式執行區 ---
if __name__ == "__main__":
    # [修正 1] 強制執行記憶體回收，清除 DependencyInstaller 留下的 Tkinter 變數
    # 避免在背景執行緒觸發 Variable.__del__ 導致崩潰
    import gc
    gc.collect()

    run_as_admin()
    _set_windows_dpi_awareness()
    _set_windows_app_user_model_id()

    # 【穩定性 2026.05.20】Mutex 單例 — 防雙開搶 keyboard hook / 兩個 Chrome / log rotate 撞檔
    if not ensure_single_instance("Local\\CMUH_Skin_Main_SingleInstance_v1"):
        ctypes.windll.user32.MessageBoxW(
            0, "主程式已在執行中。", "中國醫皮膚科主程式", 0x40 | 0x1000)
        sys.exit(0)
    import atexit as _atexit_mtx
    _atexit_mtx.register(release_single_instance)

    # 必須先建 main_root，splash 才能用 Toplevel（避免兩個 tk.Tk() 造成 ttk 樣式錯亂）
    main_root = tk.Tk()
    main_root.withdraw()  # 主視窗先隱藏，等初始化完成再顯示，避免閃爍

    # [背景啟動] 由「閒置自動重啟 / 自動更新重啟 / watchdog 重啟」帶 --background 旗標
    # 重啟時，全程靜默：不開 splash、視窗以最小化進工作列，不跳到最上層也不搶焦點，
    # 避免打斷使用者當下操作。使用者手動雙擊開啟（無旗標）維持原本正常最大化顯示。
    _start_background = ("--background" in sys.argv)

    # [O18] 啟動 splash：給使用者「程式正在開」的即時反饋（背景重啟時不顯示）
    _splash = None
    if not _start_background:
        try:
            from cmuh_common.splash import StartupSplash
            _splash = StartupSplash(main_root, "正在初始化…")
            _splash.show()
        except Exception:
            _splash = None
            logging.debug("splash 啟動失敗（忽略）", exc_info=True)

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

    # [穩定性] Tk callback (.after() / 事件 binding) 未捕獲例外處理：
    # Tk 預設只 print 到 stderr (pythonw 看不到)。override 後寫進 logging，
    # 任何 callback 拋例外都能在 automation_ui.log 看到完整 traceback。
    # [v18 2026-05-25] 抽到 cmuh_common.tk_exception 共用，讓 scheduler /
    # consult_query / autoclock 三支也用同一份 handler。
    try:
        from cmuh_common.tk_exception import install_tk_exception_handler
        install_tk_exception_handler(main_root)
    except Exception:
        logging.debug("Tk callback exception hook 失敗", exc_info=True)

    if _splash:
        _splash.update_text("載入主視窗…")

    try:
        app = AutomationApp(main_root, {})
    except Exception:
        # [stability] 主程式初始化若拋例外，原本 splash 會卡在畫面、process 靜默
        # 死掉只留 log，使用者一頭霧水(以為當機)。改為：關掉 splash、記完整
        # traceback、跳一個可見的錯誤框提示使用者去看 log，再乾淨退出。
        logging.exception("主程式初始化失敗 (AutomationApp 建構)")
        try:
            if _splash:
                _splash.close()
        except Exception:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(
                0, "主程式初始化失敗，請查看 automation_ui.log 後重新啟動。",
                "中國醫皮膚科主程式", 0x10)  # MB_ICONERROR
        except Exception:
            pass
        sys.exit(1)
    DOCTORS = app.doctors_list
    DOCTOR_NAMES = [d["name"] for d in DOCTORS]

    # [穩定性] health monitor — RAM/時鐘/硬碟 + 記憶體 leak 自動重啟 (A/E/F)
    # 主程式 + Chrome (status driver) 正常 ~200-400MB；warn 500、crit 900
    # 注意：主程式是有 GUI 的，os._exit 會把 UI 直接殺掉 → 但這是 RAM > 900MB
    # 持續 30 分鐘的極端情況，本來就該 restart。外層 watchdog 不會自動重啟主程式
    # (master_enabled 即使開了主程式也是 outer_only/disabled)，所以 os._exit 後
    # 使用者要手動重啟。寧可資料潛在掉一點也比 RAM 失控好。
    try:
        from cmuh_common.health import start_health_monitor
        # 主程式沒有外層 watchdog 會接手，所以給 restart_callback：RAM 連續爆表時
        # 先 spawn 一個新 instance 再 os._exit 本 process（單例 mutex 重啟競態由
        # ensure_single_instance 的重試處理），而不是只 os._exit 後就再也不回來。
        # hard_exit_code=1：health 監看跑在 daemon thread，sys.exit 殺不掉 process，
        # 必須 os._exit。
        # [2026-06-16 觀測] warn_callback:自動重啟前一個 tick(~5 分鐘前)先跳通知,
        # 讓使用者有機會先存檔,不再無預警重啟消失。daemon 緒呼叫,只做輕量通知。
        def _ram_restart_warn(rss_mb, crit_mb, eta_min):
            try:
                # [IF-03] 此 callback 由 health monitor 監看緒 inline 呼叫;必須用【非阻塞】版,否則
                # 無人按掉「記憶體偏高」MessageBox → 監看緒卡死不再 tick → consecutive_critical_ram
                # 永遠到不了門檻 → RAM 失控自動重啟在最需要的無人場景失效(main 無外層 watchdog 接手)。
                show_windows_notification_async(
                    "記憶體偏高",
                    f"記憶體使用 {int(rss_mb)}MB(上限 {int(crit_mb)}MB),"
                    f"約 {eta_min} 分鐘後將自動重啟以釋放記憶體,請先存檔。")
            except Exception:
                logging.debug("RAM 重啟前通知失敗", exc_info=True)
        start_health_monitor("main", ram_warn_mb=500, ram_crit_mb=900,
                              interval_sec=300, network_check=False,
                              auto_restart_on_crit=True,
                              crit_persistence_ticks=6,
                              restart_callback=lambda: restart_self(["--background"], hard_exit_code=1),
                              warn_callback=_ram_restart_warn)
    except Exception:
        logging.debug("health monitor 啟動失敗", exc_info=True)

    # ─── 內層 watchdog (B)：daemon thread，每 30s 巡邏 ─────────────────
    # 目的：自動 kill+重啟 卡死的 consult_query / 被誤關的打卡
    # 配合 schtasks 每 2 分鐘觸發 watchdog_runner.py --once 為外層 C
    # B+C 之間用 settings/.watchdog_locks/ 互斥（同程式 90s 內不會被雙方
    # 同時 kill+restart）。即使 B 隨主程式 crash，C 在 2 分鐘內接手。
    def _inner_watchdog_loop():
        try:
            from cmuh_common import watchdog_core
        except Exception:
            logging.exception("[watchdog/inner] 載入 watchdog_core 失敗，停用")
            return
        logging.info("[watchdog/inner] 啟動 — 監看 consult_query/打卡")
        last_heartbeat = 0.0
        # 【穩定性 2026.05.20】用 stop_event_main.wait 取代 time.sleep — shutdown
        # 時立即返回，避免 daemon 在 sleep 中被 Python interpreter 腰斬留下殭屍 chromedriver。
        while not stop_event_main.is_set():
            try:
                cfg = watchdog_core.load_config()
                actions = watchdog_core.run_one_tick(mode="inner")
                heartbeat, interval = watchdog_core.get_loop_timing(cfg)
                now_monotonic = time.monotonic()
                if now_monotonic - last_heartbeat >= heartbeat:
                    logging.info("[watchdog/inner heartbeat] %s",
                                  " | ".join(actions) if actions else "-")
                    last_heartbeat = now_monotonic
            except Exception:
                logging.exception("[watchdog/inner] tick 例外")
                interval = 30
            if stop_event_main.wait(interval):
                break
        logging.info("[watchdog/inner] 收到 stop_event，退出")

    try:
        _wd_thread = threading.Thread(
            target=_inner_watchdog_loop,
            name="InnerWatchdog",
            daemon=True,
        )
        _wd_thread.start()
    except Exception:
        logging.exception("[watchdog/inner] thread 啟動失敗")

    # [O18] splash 關閉後再顯示主視窗（避免主視窗閃爍）
    if _splash:
        try:
            _splash.close()
        except Exception:
            pass
    try:
        # [2026-07-08 使用者需求] 一般啟動與背景重啟一致：直接從 withdrawn 進 iconify
        # （實測不會 map、不閃、不搶焦點），主程式開啟後就縮在工作列，不像一般程式疊加顯示
        # 在螢幕上。使用者第一次從工作列還原(<Map>, state=normal)時，才放到偏好螢幕
        # （現為【主螢幕】，見 choose_preferred_monitor）並最大化——兼顧「開啟不干擾」與
        # 「還原後仍是熟悉的最大化視窗」。原本一般啟動會 deiconify 疊加、且偏好副螢幕。
        main_root.attributes("-topmost", False)

        def _maximize_on_first_restore(event=None):
            if event is not None and getattr(event, "widget", None) is not main_root:
                return
            if main_root.state() != "normal":
                return
            try:
                main_root.unbind("<Map>", _first_map_bind_id)
            except Exception:
                pass
            try:
                place_tk_window_on_preferred_monitor(main_root)
            except Exception:
                logging.debug("還原後定位/最大化失敗", exc_info=True)

        _first_map_bind_id = main_root.bind(
            "<Map>", _maximize_on_first_restore, add="+")
        main_root.iconify()
    except Exception:
        pass

    main_root.mainloop()
    logging.info("--- Script Finished ---")
