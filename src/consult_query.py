# -*- coding: utf-8 -*-
"""中國醫皮膚科會診查詢程式（重構自手動操作流程，全自動化）。

功能：
  1. 開啟 C:\\admc\\systemftp.exe（住院醫囑系統）
  2. 自動登入（帳密由設定檔提供）
  3. 處理「請勿開啟超過兩個」多開提示、以及登入後的「訊息通知主畫面」
  4. 用 Win32 選單命令直接跳到「病人清單及交班 → 會診清單 → 我的會診清單」
  5. 擷取「會診通知單回覆」視窗畫面
  6. 透過 Outlook 寄出截圖給設定的收件人
  7. 每 N 分鐘（預設 15）輪詢會診清單，只在出現「新病歷號」時才寄信（信內含目前全部
     未回覆清單）；00:00–06:00 休息不輪詢/不寄，過夜新增的由休息結束後第一輪一次補寄

【解析度無關設計】
  全程不使用任何寫死的螢幕座標。所有控制項都在執行當下用 Win32 API
  列舉 HWND，直接對控制項送訊息（WM_SETTEXT / BM_CLICK / WM_COMMAND）。
  截圖用 PrintWindow（即使視窗被蓋住或不在前景也能擷取，不干擾使用者）。
  因此可在多台不同解析度的電腦上執行。

啟動模式：
  （無參數）  常駐系統列 + 排程器
  --run-now   觸發一次立即執行（若已有常駐實例，改為通知該實例執行）
  --configure 開啟設定視窗
"""
from __future__ import annotations

import os
import sys

# === 必須在最前面：把 src/ 加到 sys.path（.pyw 與 .exe 模式都要）===
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# === 自動依賴安裝 ===
from cmuh_common.deps_runtime import ensure_dependencies  # noqa: E402

REQUIRED_LIBS = [
    ("schedule", "schedule"),
    ("pystray", "pystray"),
    ("Pillow", "PIL"),
    ("psutil", "psutil"),
    ("pywin32", "win32gui"),
    # [2026-06-15] 信件併入打卡狀態需用 selenium 查打卡 portal(headless Chrome)。
    ("selenium", "selenium"),
]
ensure_dependencies(REQUIRED_LIBS)

# === 主要 import（依賴已就緒）===
import ctypes  # noqa: E402
import html as _html  # noqa: E402
import logging  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import tkinter as tk  # noqa: E402
from datetime import datetime, time as dt_time  # noqa: E402
from pathlib import Path  # noqa: E402
from tkinter import messagebox, scrolledtext, ttk  # noqa: E402

import psutil  # noqa: E402
import schedule  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402
import win32ui  # noqa: E402

# Win32 函式簽章（CreateDesktop/SetThreadDesktop 的指標型別在 64 位元下要用 c_void_p）
_user32 = ctypes.windll.user32
_user32.OpenDesktopW.restype = ctypes.c_void_p
_user32.OpenDesktopW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong,
                                  ctypes.c_bool, ctypes.c_ulong]
_user32.CreateDesktopW.restype = ctypes.c_void_p
_user32.CreateDesktopW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                    ctypes.c_void_p, ctypes.c_ulong,
                                    ctypes.c_ulong, ctypes.c_void_p]
_user32.SetThreadDesktop.restype = ctypes.c_bool
_user32.SetThreadDesktop.argtypes = [ctypes.c_void_p]
_user32.CloseDesktop.restype = ctypes.c_bool
_user32.CloseDesktop.argtypes = [ctypes.c_void_p]

from cmuh_common.atomic_io import atomic_write_json, safe_load_json  # noqa: E402
from cmuh_common.logging_setup import attach_queue_handler, setup_logging  # noqa: E402
from cmuh_common.paths import get_app_dir, get_settings_dir, restart_self  # noqa: E402
from cmuh_common.platform_win import is_admin, run_as_admin  # noqa: E402
from cmuh_common.process_launch import launch_python_script  # noqa: E402
from cmuh_common.win32_safe import call_with_timeout  # noqa: E402
from cmuh_common.single_instance import (  # noqa: E402
    ensure_single_instance, release_single_instance,
)
from cmuh_common.task_gate import ActiveTaskGate  # noqa: E402
from cmuh_common.version import CURRENT_VERSION  # noqa: E402

# DPI 感知：讓 GetWindowRect 回實體像素，跨機/跨縮放比例一致
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# =============================================================================
# 路徑與設定
# =============================================================================
BASE_DIR = Path(get_app_dir())
SETTINGS_DIR = Path(get_settings_dir())
CONFIG_FILE = SETTINGS_DIR / "consult_query_config.json"
LOG_FILE = SETTINGS_DIR / "consult_query.log"
SHOTS_DIR = SETTINGS_DIR / "consult_shots"
RUNNOW_FLAG = SETTINGS_DIR / "consult_query_runnow.flag"
RELOAD_FLAG = SETTINGS_DIR / "consult_query_reload.flag"
MAX_SHOT_FILES = 60

SYSTEMFTP_PATH = r"C:\admc\systemftp.exe"
MUTEX_NAME = "Local\\CMUH_Skin_ConsultQuery_SingleInstance_v1"
CONFIG_MUTEX_NAME = "Local\\CMUH_Skin_ConsultQuery_Config_v1"

# 設定視窗「收件人」清單上限(可多人;留些緩衝,避免誤填一大串)。
_MAX_RECIPIENTS = 8

DEFAULT_CONFIG = {
    # [CQ-04] 不硬編碼院內 HIS 帳密(此檔進 public repo)。首啟無設定檔會強制開設定
    # 視窗填寫(見 main());既有部署 config 已存在、不受影響。
    "username": "",
    "password": "",
    # 一般排程（每日 12:30 / 17:00）收件人
    "recipients": [
        "expertise88864@gmail.com",
        "chilly840724@gmail.com",
        "wesjefflee1111@gmail.com",
        "mbpushowo@gmail.com",
    ],
    # 系統匣「測試寄信」用的收件人（只給一個人，免打擾）
    "test_recipients": [
        "expertise88864@gmail.com",
    ],
    # 【舊欄位，留作 fallback】信件觸發但白名單比對不到寄件人時用的收件人。
    # 新邏輯：觸發信會被 IMAP 抓到，自動把結果寄回給「寄信來觸發的那個人」，
    # 前提是該寄件人 email 在 allowed_trigger_senders 白名單內。
    "email_trigger_recipients": [
        "expertise88864@gmail.com",
    ],
    # 觸發白名單：只有這些 email 寄來的觸發信會生效（避免任何人猜到信箱就
    # 能拉醫療截圖）。預設等於 recipients 名單（合理：能收排程信的人就能
    # 自己觸發）。比對時不分大小寫。
    "allowed_trigger_senders": [
        "expertise88864@gmail.com",
        "chilly840724@gmail.com",
        "wesjefflee1111@gmail.com",
        "mbpushowo@gmail.com",
    ],
    # [2026-06-16] 每天 12:40 + 17:10 都跑（不分平假日）。打卡系統於 7:31/12:31/17:01
    # 才登入打卡，故延後到 12:40 / 17:10 再查詢寄信，確保中午(12:31)上班與下午(17:01)
    # 下班打卡都「已完成並寫入紀錄」後才查，不會還沒打卡就先寄出誤判未打卡。
    "weekday_times": ["12:40", "17:10"],   # 週一～週五（已停用,改為 poll_interval_minutes 輪詢）
    "weekend_times": ["12:40", "17:10"],   # 週六、週日（已停用,同上）
    # [2026-06-25] 即時偵測:每 N 分鐘輪詢「我的會診清單」,只在出現「新病歷號」時才寄信
    # (信內含目前全部未回覆清單)。已取代固定時間排程(12:40/17:10)。
    "poll_interval_minutes": 15,
    # 半夜休息時段 [start, end):此區間不輪詢、不寄信;過夜新增的會診由 end 之後第一輪一次補寄。
    "quiet_start_hour": 0,
    "quiet_end_hour": 6,
    "subject_template": "{date} {time} 皮膚科會診通知單",
    "body_template": "附件為 {date} {time} 皮膚科會診通知單截圖，由系統自動擷取寄送。",
    # [2026-06-15] 信件併入「今日打卡狀態」(autoclock 各帳號 上/下班)。關掉就不查不附。
    "punch_status_in_email": True,
    "enabled": True,
    # 寄信方式："smtp"（推薦，預設，直接連 Gmail SMTP）或 "outlook"（透過
    # Outlook COM；admin 行程跟 user-level Outlook profile 不同會卡在 Outbox，
    # 不建議）。SMTP 設定見 settings/smtp_credentials.json。
    "mail_method": "smtp",
    # （Outlook 模式才用）強制寄件人帳號。SMTP 模式忽略此欄，用 smtp_credentials
    # 的 from_address。
    "sender_account": "cmuhdermatology@gmail.com",
    # 失敗自動重試：每次重試前 taskkill systemftp.exe 確保乾淨環境
    "retry_count": 3,
    # 信件觸發：從任何地方（手機 / 任何信箱）寄一封信到
    # cmuhdermatology@gmail.com，主旨包含關鍵字 → 程式每 60 秒透過 IMAP 連
    # imap.gmail.com:993 檢查一次，看到就把信標為已讀並立即跑一次 consult
    # flow（截圖會診單 → 寄給 email_trigger_recipients，預設只給觸發者一人）。
    # 用同一個 Gmail App Password (settings/smtp_credentials.json)。
    "email_trigger_enabled": True,
    "email_trigger_subject_keyword": "皮膚科會診觸發",
    # IMAP 輪詢週期（秒）。預設 20 秒（從原本 60 秒縮短，加速觸發回應 ~40 秒）。
    # Gmail rate limit 對 IMAP 寬鬆，10-20 秒都很安全；想要更即時可降至 10
    # 秒；想要省連線可調回 60 秒。
    "email_trigger_poll_seconds": 20,
    # [會診2 2026-06-11] 觸發信時效上限（小時）：程式停機數天恢復後，累積的舊未讀
    # 觸發信不回放(標已讀清掉、不觸發)，避免把幾天前的請求當現在處理。0=不過濾。
    # 解析不出信件時間時 fail-open 照常觸發(寧可多觸發、不可漏會診請求)。
    "email_trigger_max_age_hours": 6,
    # [新功能 2026-06-13;2026-06-15 改用 TRadioButton] 會診單內容文字擷取:
    # 病人清單 = 一顆顆 TRadioButton(文字含姓名+床號+病歷號),直接解析其文字
    # 即得最準確的病人清單;再逐顆 BM_CLICK 選取、以 WM_GETTEXT 讀下方「會診
    # 事項/病情摘要」文字控制項,一併附進信件(截圖照常為主)。完全 fail-open。
    # 下列 extract_* 為「無 TRadioButton 時」的格線像素後備路徑參數(現環境用不到)。
    "extract_text_enabled": True,
    "extract_max_rows": 12,        # [後備] 最多嘗試點選幾列(病人數上限)
    "extract_first_row_y": 32,     # [後備] 第一列資料的 client Y(略過表頭)
    "extract_row_height": 19,      # [後備] 每列高度(px)
    "extract_click_x": 12,         # [後備] 點擊 X:病人姓名前的選取框欄
}

MAX_RETRY_COUNT = 10

# Win32 視窗特徵（由探測 spike 實測得到，非寫死座標）
LOGIN_CLASS = "TFrmLogin"
LOGIN_TITLE_PREFIX = "中國醫藥大學附設醫院住院系統---簽入系統"
MAIN_CLASS = "TFMNewMain"
MULTI_INSTANCE_CLASS = "TMessageForm"        # 「請勿開啟超過兩個」提示
MULTI_INSTANCE_TITLE = "住院醫囑系統"
NOTICE_CLASS = "TFMShowMessage"              # 登入後的「訊息通知主畫面」
CONSULT_CLASS = "TFMJoinResponse"            # 「會診通知單回覆」目標視窗
# 選單路徑：主選單[4]病人清單及交班 → 子[8]會診清單 → 子[0]我的會診清單
MENU_PATH = (4, 8, 0)
MENU_ID_EXPECTED = 446                       # 我的會診清單（探測實測值，作為後備）

BM_CLICK = 0x00F5
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
OFFSCREEN_X, OFFSCREEN_Y = -32000, -32000  # 把視窗藏到虛擬桌面外（使用者看不到）

# 隱藏桌面名稱：systemftp 整個在這個虛擬桌面上跑，使用者畫面完全不會出現
HIDDEN_DESKTOP_NAME = "CMUHConsultHidden_v1"
_DESKTOP_GENERIC_ALL = 0x10000000

running = threading.Event()
running.set()
_flow_lock = threading.Lock()
_consult_job_gate = ActiveTaskGate(stale_after_sec=45 * 60)
_test_email_gate = ActiveTaskGate(stale_after_sec=10 * 60)
tray_icon_object = None
_exit_lock = threading.Lock()
_exit_started = False
# 背景更新檢查（daemon thread）偵測到新版時設 True；實際重啟由 main thread 在
# tray run() 返回後執行（見 _request_restart_for_update / main 尾端）。
_restart_after_run = False
log_queue: "queue.Queue" = queue.Queue(maxsize=5000)
LOG_POLL_MAX_RECORDS = 200
_config_lock = threading.Lock()
_self_watchdog_thread_ref: threading.Thread | None = None
_self_watchdog_lock = threading.Lock()


def _normalize_retry_count(value) -> int:
    try:
        raw = int(value or DEFAULT_CONFIG["retry_count"])
        return max(1, min(MAX_RETRY_COUNT, raw))
    except (TypeError, ValueError):
        return DEFAULT_CONFIG["retry_count"]


def _sleep_while_running(seconds: float, step: float = 0.5) -> bool:
    """Sleep up to seconds, but return quickly after running.clear()."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    step = max(0.05, float(step))
    while running.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(step, remaining))
    return False


# =============================================================================
# Logging
# =============================================================================
def _setup_logging() -> None:
    setup_logging(str(LOG_FILE), max_bytes=3 * 1024 * 1024, backup_count=2)
    qh = attach_queue_handler(log_queue, replace_existing=True)
    qh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))


# =============================================================================
# 設定檔
# =============================================================================
# [2026-06-15] 舊預設排程時間 → 新預設的自動升級對照。只有「完全等於某一代舊預設」
# 的設定檔才升級(沿用內建預設、沒自訂過的機器);使用者改過時間一律不動。
# 每個值 = (歷代舊預設清單, 新預設):同時涵蓋 12:30/17:00 與上一版 12:31/17:01,
# 沿用任一代舊預設的機器更新後都會自動升級到 12:40/17:10。
_OLD_SCHED_DEFAULTS = [["12:30", "17:00"], ["12:31", "17:01"]]
_NEW_SCHED_DEFAULT = ["12:40", "17:10"]
_SCHED_TIME_MIGRATION = {
    "weekday_times": (_OLD_SCHED_DEFAULTS, _NEW_SCHED_DEFAULT),
    "weekend_times": (_OLD_SCHED_DEFAULTS, _NEW_SCHED_DEFAULT),
}


def _has_his_credentials(cfg: dict) -> bool:
    """[CQ-04] 設定是否已填 HIS 帳號/密碼。空帳密不啟動——否則每輪排程/手動都以空字串
    登入、每次失敗(甚至有 portal 鎖定風險),而使用者只會覺得「都沒收到信」。"""
    return bool(str(cfg.get("username") or "").strip()
                and str(cfg.get("password") or "").strip())


def load_config() -> dict:
    with _config_lock:
        cfg = dict(DEFAULT_CONFIG)
        try:
            if CONFIG_FILE.exists():
                saved = safe_load_json(str(CONFIG_FILE), default={})
                if isinstance(saved, dict):
                    cfg.update(saved)
        except Exception:
            logging.warning("讀取設定檔失敗，使用預設值", exc_info=True)
        # 正規化（每個 list 欄位都防呆：缺欄位/型別錯 → 退回 default；strip 空白；
        # 過濾空字串）
        for key in ("recipients", "test_recipients", "email_trigger_recipients",
                     "allowed_trigger_senders"):
            if not isinstance(cfg.get(key), list):
                cfg[key] = list(DEFAULT_CONFIG[key])
            cfg[key] = [str(r).strip() for r in cfg[key]
                        if r is not None and str(r).strip()]
        # 白名單比對全小寫，避免大小寫差異漏判
        cfg["allowed_trigger_senders"] = [a.lower() for a in
                                            cfg["allowed_trigger_senders"]]
        for key in ("weekday_times", "weekend_times"):
            if not isinstance(cfg.get(key), list):
                cfg[key] = list(DEFAULT_CONFIG[key])
            cfg[key] = [str(t).strip() for t in cfg[key] if str(t).strip()]
        # [2026-06-16] 把「沿用任一代舊預設(12:30/17:00 或 12:31/17:01)」的存檔自動
        # 升級為新預設 12:40/17:10(延後以確保打卡完成後才查)。只有完全等於某代舊預設
        # 才升級;自訂過的時間不動。已在鎖內 → 直接 atomic_write_json 寫回。
        migrated = False
        for key, (old_defs, new_def) in _SCHED_TIME_MIGRATION.items():
            if cfg.get(key) in old_defs:
                cfg[key] = list(new_def)
                migrated = True
        if migrated:
            try:
                atomic_write_json(str(CONFIG_FILE), cfg)
                logging.info("[migrate] 會診排程時間升級為 %s", _NEW_SCHED_DEFAULT)
            except Exception:
                logging.warning("[migrate] 寫回升級後設定失敗(不影響本次執行)",
                                exc_info=True)
        # 數值欄位防呆
        cfg["retry_count"] = _normalize_retry_count(cfg.get("retry_count", 3))
        # 觸發輪詢週期：限制 5-300 秒，超出範圍退回預設
        try:
            v = float(cfg.get("email_trigger_poll_seconds",
                               DEFAULT_CONFIG["email_trigger_poll_seconds"]))
            cfg["email_trigger_poll_seconds"] = max(5.0, min(300.0, v))
        except (TypeError, ValueError):
            cfg["email_trigger_poll_seconds"] = \
                DEFAULT_CONFIG["email_trigger_poll_seconds"]
        # [2026-06-25] 輪詢/休息時段數值防呆:None/壞值/超界 → 退回預設並夾範圍,
        # 避免後續 _rebuild_schedule / poll 休息判斷的 int() 直接炸掉(Codex 指出)。
        try:
            cfg["poll_interval_minutes"] = max(
                5, min(120, int(cfg.get("poll_interval_minutes", 15))))
        except (TypeError, ValueError):
            cfg["poll_interval_minutes"] = DEFAULT_CONFIG["poll_interval_minutes"]
        for _qk in ("quiet_start_hour", "quiet_end_hour"):
            try:
                cfg[_qk] = max(0, min(23, int(cfg.get(_qk, DEFAULT_CONFIG[_qk]))))
            except (TypeError, ValueError):
                cfg[_qk] = DEFAULT_CONFIG[_qk]
        return cfg


def save_config(cfg: dict) -> None:
    with _config_lock:
        try:
            atomic_write_json(str(CONFIG_FILE), cfg)
            logging.info("設定已儲存")
        except Exception:
            logging.error("儲存設定檔失敗", exc_info=True)


# =============================================================================
# [2026-06-25] 會診即時偵測:每 N 分鐘輪詢「我的會診清單」,只在出現「新病歷號」時才寄信
# (信內含目前全部未回覆清單)。已通知過的病歷號集合持久化 → 跨重啟、跨多輪不重複寄。
# =============================================================================
_NOTIFIED_FILE = SETTINGS_DIR / "consult_notified.json"
_CHART_RE = re.compile(r"\d{6,}")  # 病歷號:6+ 連續數字(會診清單列裡的識別碼)


def _consult_signature(extracted_text: str) -> set:
    """從擷取的會診清單文字抓所有病歷號(6+ 位數字)當「目前未回覆會診」識別集合。純函式。
    病歷號穩定且必出現在病人清單列;以集合比對 → 新增的病歷號 = 新會診。

    [CQ-02 legacy] 此函式會掃「整段信文」——病情摘要內文的身分證/手機/日期等雜數字也
    會被 _CHART_RE 誤當病歷號 → 假新會診重複寄。poll/基準路徑已改用下方 _from_roster
    只掃清單列;此函式僅留作向後相容。"""
    return set(_CHART_RE.findall(extracted_text or ""))


def _consult_signature_from_roster(roster_texts) -> set:
    """[CQ-02] 只從病人清單列(roster_texts)抓病歷號當識別集合,不看病情摘要內文。

    逐列以 _ROSTER_ROW_RE 取 chart 欄;解析不到結構的列(外籍病人無中文姓名等)退回
    掃該「單列」的 6+ 位數字(清單列只有病歷號是 6+ 位、日期是 M/D 不會誤中,故安全)。
    roster_texts=None(擷取失敗/停用) → 回空集合(呼叫端另以 None 走 fail-open,不更新基準)。"""
    out: set = set()
    for row in (roster_texts or []):
        row = (row or "").strip()
        m = _ROSTER_ROW_RE.fullmatch(row)
        if m and m.group("chart"):
            out.add(m.group("chart"))
        else:
            out.update(_CHART_RE.findall(row))
    return out


# 行程內的權威基準:即使檔案寫入失敗(磁碟滿/權限),記憶體仍記得已通知過誰 → 下一輪 poll 不會
# 重寄同一批(Codex 指出:只靠檔案、寫失敗會每 15 分鐘狂寄)。檔案只負責「跨重啟」記憶;單一
# job 互斥(_consult_job_gate)→ 同時只有一個 _do_full_job 在跑,無並發競爭。None = 本行程尚未載入。
_notified_memory = None
# 基準是否「曾經建立過」。用來區分「空集合(沒人未回覆,但已建過基準)」與「從沒建過基準」——
# 後者(第一次啟動/檔案不存在)第一輪 poll 只建基準、不寄,避免重啟收一封全清單。None=尚未載入。
_notified_initialized = None


def _load_notified() -> set:
    """讀「已通知過的病歷號」基準。行程內已有記憶體值就用它(權威,不受檔案寫入失敗影響);
    否則(剛啟動)從 SETTINGS_DIR/consult_notified.json 載入。失敗回空集合。"""
    global _notified_memory, _notified_initialized
    if _notified_memory is not None:
        return set(_notified_memory)
    try:
        data = safe_load_json(str(_NOTIFIED_FILE), default=None)
        if isinstance(data, dict):
            _notified_memory = {str(x) for x in (data.get("charts") or [])}
            # [Codex] 檔案存在且是合法 dict → 先前已建過基準(即使 charts 為空,也代表「已建、
            # 目前沒人未回覆」而非從沒建過)。只有「檔案不存在 / 壞掉」才算從沒建過 → 第一輪 poll
            # 才靜默建基準。避免升級(舊版只有 charts、甚至空 charts)後把當下新會診靜默吞掉漏寄。
            _notified_initialized = True
            return set(_notified_memory)
    except Exception:
        logging.debug("讀取 consult_notified 失敗", exc_info=True)
    _notified_memory = set()
    _notified_initialized = False
    return set()


def _baseline_initialized() -> bool:
    """基準是否曾經建立過(檔案有 initialized=true / 本行程已 _save_notified 過)。
    False 代表第一次啟動還沒建基準 → 第一輪 poll 只建基準、不寄(避免重啟收全清單)。"""
    global _notified_initialized
    if _notified_initialized is None:
        _load_notified()   # 順帶載入 _notified_initialized
    return bool(_notified_initialized)


def _save_notified(charts: set) -> None:
    """把「目前清單的病歷號」設為已通知基準(寄信成功後呼叫;poll/email/手動皆更新)。
    【先更新記憶體(權威)再寫檔】→ 即使寫檔失敗,本行程後續 poll 也絕不重寄同一批;檔案僅供跨重啟。"""
    global _notified_memory, _notified_initialized
    _notified_memory = set(charts)
    _notified_initialized = True
    try:
        atomic_write_json(str(_NOTIFIED_FILE),
                          {"charts": sorted(charts), "initialized": True})
    except Exception:
        logging.warning("寫入 consult_notified 失敗(記憶體已記住,本行程不會重寄)", exc_info=True)


def _in_quiet_hours(now: datetime, cfg: dict) -> bool:
    """是否在「半夜休息」時段(預設 [0,6):00:00-06:00 不輪詢/不寄信)。純函式。"""
    try:
        start = int(cfg.get("quiet_start_hour", 0))
        end = int(cfg.get("quiet_end_hour", 6))
    except (TypeError, ValueError):
        start, end = 0, 6
    h = now.hour
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end   # 容錯:若設定跨午夜(start>end)


# =============================================================================
# Win32 視窗工具（全部執行期查詢，零寫死座標）
# =============================================================================
def _systemftp_pids() -> set:
    out = set()
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() == "systemftp.exe":
                out.add(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return out


def _window_pid(hwnd: int) -> int:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid
    except Exception:
        return -1


def _pid_session(pid: int):
    """[CQ-05] 回 PID 所屬的 Windows 登入 session id(取不到回 None)。用於多使用者/RDS
    機器把孤兒清掃限縮在本 session,避免誤殺其他使用者的行程。"""
    try:
        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(int(pid), ctypes.byref(sid)):
            return sid.value
    except Exception:
        pass
    return None


def find_windows(class_name: str | None = None, title_prefix: str | None = None,
                 pids: set | None = None, visible_only: bool = True) -> list:
    """列舉符合條件的 top-level 視窗，回傳 hwnd list。"""
    result = []

    def cb(hwnd, _):
        try:
            if visible_only and not win32gui.IsWindowVisible(hwnd):
                return True
            if class_name and win32gui.GetClassName(hwnd) != class_name:
                return True
            if title_prefix and not win32gui.GetWindowText(hwnd).startswith(title_prefix):
                return True
            if pids is not None and _window_pid(hwnd) not in pids:
                return True
            result.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return result


def wait_window(class_name: str | None = None, title_prefix: str | None = None,
                pids: set | None = None, timeout: float = 60.0,
                interval: float = 0.4) -> int | None:
    """輪詢等待視窗出現，回傳 hwnd（逾時回 None）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not running.is_set():
            return None
        hits = find_windows(class_name, title_prefix, pids)
        if hits:
            return hits[0]
        time.sleep(interval)
    return None


def enum_children(parent_hwnd: int) -> list:
    """回傳 [(hwnd, classname, text, rect)]。"""
    out = []

    def cb(hwnd, _):
        try:
            out.append((
                hwnd,
                win32gui.GetClassName(hwnd),
                win32gui.GetWindowText(hwnd),
                win32gui.GetWindowRect(hwnd),
            ))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(parent_hwnd, cb, None)
    except Exception:
        pass
    return out


def find_child(parent_hwnd: int, class_name: str | None = None,
               text: str | None = None) -> int | None:
    for hwnd, cls, txt, _rect in enum_children(parent_hwnd):
        if class_name and cls != class_name:
            continue
        if text is not None and txt != text:
            continue
        return hwnd
    return None


def force_foreground(hwnd: int) -> bool:
    """強制把視窗帶到前景。

    單純的 SetForegroundWindow 在非前景行程常被 Windows 擋下（只閃工作列）。
    可靠作法：AttachThreadInput 把本執行緒接到「目前前景執行緒」與「目標執行緒」
    的輸入佇列，解除前景鎖定後再 SetForegroundWindow / SetActiveWindow。
    回傳是否確實成為前景視窗。
    """
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass
    fg = win32gui.GetForegroundWindow()
    cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    tgt_tid = win32process.GetWindowThreadProcessId(hwnd)[0]
    attached = []
    for tid in (fg_tid, tgt_tid):
        if tid and tid != cur_tid:
            try:
                ctypes.windll.user32.AttachThreadInput(cur_tid, tid, True)
                attached.append(tid)
            except Exception:
                pass
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.BringWindowToTop(hwnd)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        try:
            win32gui.SetActiveWindow(hwnd)
        except Exception:
            pass
    finally:
        for tid in attached:
            try:
                ctypes.windll.user32.AttachThreadInput(cur_tid, tid, False)
            except Exception:
                pass
    time.sleep(0.5)
    ok = win32gui.GetForegroundWindow() == hwnd
    if not ok:
        logging.warning("force_foreground 未必成功（目標未成為前景視窗）")
    return ok


def hide_window(hwnd: int) -> None:
    """SW_HIDE 隱藏視窗。對「最大化」的視窗也有效（SetWindowPos 移位則無效，
    這是先前使用者仍看到視窗的原因——systemftp 的視窗都是最大化的）。
    隱藏的視窗沒有工作列按鈕，但 BM_CLICK / WM_COMMAND 等背景訊息照常運作。"""
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
    except Exception:
        pass


def show_offscreen(hwnd: int) -> None:
    """把視窗（即使原本最大化）解除最大化、設成工具視窗、移到螢幕外後顯示。

    用於登入視窗（需在前景才能 SetFocus）與會診單視窗（需 PrintWindow 擷取）：
    視窗在螢幕外 → 使用者看不到；工具視窗屬性 → 不出現在工作列。
    先 SW_HIDE 再改樣式再 SetWindowPlacement，整個過程使用者看不到、無閃爍。"""
    try:
        left, top, right, bot = win32gui.GetWindowRect(hwnd)
        w, h = max(600, right - left), max(400, bot - top)
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        ex = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, GWL_EXSTYLE,
                               (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        # WINDOWPLACEMENT: (flags, showCmd, ptMin, ptMax, rcNormalPosition)
        # showCmd=SW_SHOWNORMAL 會解除最大化並依 rcNormalPosition 定位＋顯示
        win32gui.SetWindowPlacement(hwnd, (
            0, win32con.SW_SHOWNORMAL, (-1, -1), (-1, -1),
            (OFFSCREEN_X, OFFSCREEN_Y, OFFSCREEN_X + w, OFFSCREEN_Y + h)))
        # SetWindowPlacement 的座標可能被 Windows 夾住；此時視窗已非最大化，
        # 再用 SetWindowPos 強制定位到螢幕外（最大化視窗無法這樣移，現在可以）。
        win32gui.SetWindowPos(hwnd, 0, OFFSCREEN_X, OFFSCREEN_Y, w, h,
                              win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
    except Exception:
        logging.debug("show_offscreen 失敗", exc_info=True)


def _save_window_state(hwnd: int):
    """[CQ-06] 記錄視窗原始 placement + 擴充樣式(GWL_EXSTYLE),供借用使用者既有實例時
    finally 還原 —— 否則 show_offscreen 把使用者的住院系統移到螢幕外並改成工具視窗後不還原,
    使用者的視窗會「憑空消失」到重開程式為止。失敗回 None。"""
    try:
        return (win32gui.GetWindowPlacement(hwnd),
                win32gui.GetWindowLong(hwnd, GWL_EXSTYLE))
    except Exception:
        logging.debug("[CQ-06] 記錄視窗狀態失敗", exc_info=True)
        return None


def _restore_window_state(hwnd: int, state) -> None:
    """[CQ-06] 還原 _save_window_state 存下的 placement + 樣式(借用視窗收尾用)。"""
    if not state:
        return
    placement, exstyle = state
    try:
        if win32gui.IsWindow(hwnd):
            win32gui.SetWindowLong(hwnd, GWL_EXSTYLE, exstyle)
            win32gui.SetWindowPlacement(hwnd, placement)
    except Exception:
        logging.debug("[CQ-06] 還原借用視窗狀態失敗", exc_info=True)


def settext_safe(hwnd: int, text: str) -> None:
    """WM_SETTEXT 但用 SendMessageTimeout，避免目標忙線時無限阻塞。"""
    SMTO_ABORTIFHUNG = 0x0002
    res = ctypes.c_ulong(0)
    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            hwnd, win32con.WM_SETTEXT, 0, ctypes.c_wchar_p(text),
            SMTO_ABORTIFHUNG, 1500, ctypes.byref(res))
    except Exception:
        logging.debug("settext_safe 失敗", exc_info=True)


def type_via_focus(edit_hwnd: int, top_hwnd: int, text: str) -> None:
    """讓 Delphi TEditExt 真正取得鍵盤焦點，再逐字 PostMessage WM_CHAR。

    隱藏桌面上 SetForegroundWindow 經常失敗、SetFocus 跟著失敗 → 帳密沒打進去
    → 登入失敗 → 等不到主畫面。本版採三層保險：
      (1) PostMessage WM_LBUTTONDOWN/UP 給欄位 → Delphi 的 OnClick 會自動把
          焦點搶到該欄位（不動真實滑鼠，因為是直接送訊息給控制項，不經過
          系統 cursor）。對 Delphi 自訂編輯框最可靠。
      (2) SetForegroundWindow + SetFocus 最多重試 5 次並驗證 GetFocus。
      (3) WM_CHAR 逐字輸入。"""
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    tgt = win32process.GetWindowThreadProcessId(top_hwnd)[0]
    attached = False
    if tgt and tgt != cur:
        try:
            ctypes.windll.user32.AttachThreadInput(cur, tgt, True)
            attached = True
        except Exception:
            pass
    try:
        # (1) 模擬點擊欄位 → 讓 Delphi 自己把焦點搶過去（不動真實滑鼠）
        try:
            l, t_, r, b = win32gui.GetWindowRect(edit_hwnd)
            cw = max(2, (r - l) // 2)
            ch = max(2, (b - t_) // 2)
            lparam = (ch << 16) | cw  # client 座標：點欄位中央
            win32gui.PostMessage(edit_hwnd, win32con.WM_LBUTTONDOWN,
                                 win32con.MK_LBUTTON, lparam)
            time.sleep(0.03)
            win32gui.PostMessage(edit_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
            time.sleep(0.08)
        except Exception:
            logging.debug("模擬點擊欄位失敗", exc_info=True)

        # (2) 雙保險：再用 SetForeground + SetFocus 重試
        focus_ok = False
        for attempt in range(5):
            try:
                win32gui.BringWindowToTop(top_hwnd)
                win32gui.SetForegroundWindow(top_hwnd)
            except Exception:
                pass
            try:
                win32gui.SetFocus(edit_hwnd)
            except Exception:
                logging.debug("SetFocus attempt %d 失敗", attempt, exc_info=True)
            time.sleep(0.08)
            try:
                if win32gui.GetFocus() == edit_hwnd:
                    focus_ok = True
                    break
            except Exception:
                pass
        if not focus_ok:
            logging.warning("GetFocus 未確認落在 hwnd=%s（仍嘗試輸入；模擬點擊可能已搶到焦點）",
                            edit_hwnd)

        # (3) 清空 + 逐字輸入
        settext_safe(edit_hwnd, "")
        for ch in text:
            win32gui.PostMessage(edit_hwnd, win32con.WM_CHAR, ord(ch), 0)
            time.sleep(0.03)
        time.sleep(0.2)
    finally:
        if attached:
            try:
                ctypes.windll.user32.AttachThreadInput(cur, tgt, False)
            except Exception:
                pass


def click_button(hwnd: int) -> None:
    """對按鈕 PostMessage BM_CLICK（非同步、不阻塞——即使目標正忙於網路登入
    也不會卡住呼叫端；SendMessage 會同步等待而可能無限阻塞）。"""
    try:
        win32gui.PostMessage(hwnd, BM_CLICK, 0, 0)
    except Exception:
        logging.debug("BM_CLICK 失敗", exc_info=True)


def resolve_menu_command_id(main_hwnd: int) -> int | None:
    """走訪主視窗選單樹，取得「我的會診清單」的命令 ID。

    走 MENU_PATH=(4,8,0)：主選單第4項→子選單第8項→子選單第0項。
    讀不到時退回 MENU_ID_EXPECTED。
    """
    try:
        hmenu = win32gui.GetMenu(main_hwnd)
        if not hmenu:
            logging.warning("主視窗無標準選單，退回預設選單 ID %s", MENU_ID_EXPECTED)
            return MENU_ID_EXPECTED
        sub = hmenu
        for depth, idx in enumerate(MENU_PATH):
            if depth < len(MENU_PATH) - 1:
                sub = win32gui.GetSubMenu(sub, idx)
                if not sub:
                    logging.warning("選單路徑第 %s 層取不到子選單，退回預設 ID", depth)
                    return MENU_ID_EXPECTED
            else:
                cmd_id = win32gui.GetMenuItemID(sub, idx)
                if cmd_id and cmd_id != -1:
                    if cmd_id != MENU_ID_EXPECTED:
                        logging.info("選單 ID 走訪結果 %s（預設值 %s）",
                                     cmd_id, MENU_ID_EXPECTED)
                    return cmd_id
        return MENU_ID_EXPECTED
    except Exception:
        logging.warning("走訪選單失敗，退回預設選單 ID", exc_info=True)
        return MENU_ID_EXPECTED


_CAPTURE_TIMEOUT_SEC = 15.0
_CAPTURE_SENTINEL = object()
# [W11] 逐病人文字擷取的總體上限(病人多/後端慢時,保留已確認前段停止)。
_EXTRACT_TOTAL_TIMEOUT_SEC = 25


def capture_window_image(hwnd: int):
    """[W11 2026-07-03] PrintWindow 會送 WM_PRINT 給目標視窗;Delphi HIS GUI 凍結時
    可能【無限阻塞】。把整個擷取丟到 daemon thread + 逾時,逾時/失敗一律 raise,交由
    run_consult_flow 的重試處理(不會卡死流程)。GDI 資源在該 thread 內建立與釋放。"""
    img = call_with_timeout(lambda: _capture_window_image_impl(hwnd),
                            _CAPTURE_TIMEOUT_SEC, default=_CAPTURE_SENTINEL,
                            name="capture_window_image")
    if img is _CAPTURE_SENTINEL:
        raise RuntimeError(
            f"PrintWindow 截圖失敗或逾時(>{_CAPTURE_TIMEOUT_SEC:.0f}s,視窗可能凍結/"
            "正被關閉)——本次流程將重試")
    return img


def _capture_window_image_impl(hwnd: int):
    """用 PrintWindow 擷取視窗影像（即使被遮住/非前景也能擷取，不干擾使用者）。"""
    from PIL import Image

    left, top, right, bot = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bot - top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"視窗尺寸異常: {width}x{height}")

    # 全部 GDI handle 先設 None：即使在「建立階段」就拋例外（GDI handle 耗盡、
    # 視窗剛好被關等），finally 也能逐一釋放已建立的物件，避免長駐程式反覆失敗
    # 時穩定洩漏 DC/bitmap，最終整個 process 再也擷取不到。
    hwnd_dc = mfc_dc = save_dc = bmp = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bmp)
        # PW_RENDERFULLCONTENT=2：抓得到 Delphi/DirectComposition 內容
        result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        bmpinfo = bmp.GetInfo()
        bmpstr = bmp.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpstr, "raw", "BGRX", 0, 1,
        )
    finally:
        if bmp is not None:
            try:
                win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
        if save_dc is not None:
            try:
                save_dc.DeleteDC()
            except Exception:
                pass
        if mfc_dc is not None:
            try:
                mfc_dc.DeleteDC()
            except Exception:
                pass
        if hwnd_dc is not None:
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass

    if result != 1:
        # PrintWindow 對 Delphi 視窗即使回傳非 1 通常仍產出有效影像；
        # 視窗在螢幕外，不能用 ImageGrab 後備，直接記錄並沿用 PrintWindow 結果。
        logging.warning("PrintWindow 回傳 %s（仍沿用擷取結果）", result)
    return img


# =============================================================================
# [新功能 2026-06-13] 會診單內容文字擷取
# 原理：會診清單是 Delphi 格線，下方「會診事項/病情摘要」是 Memo/RichEdit 類
# 文字控制項。背景 PostMessage 依序點選每位病人列(同 type_via_focus 的點擊
# idiom，不動真實滑鼠)，每次點選後用 WM_GETTEXT 讀取文字面板 → 彙整進信件。
# 完全 fail-open：抓不到就回空字串、照常只寄截圖。每次執行都把控制項樹 dump
# 進 log，供依實機結構微調 extract_* 設定參數。
# =============================================================================
def _read_ctrl_text(hwnd: int, max_len: int = 8192) -> str:
    """WM_GETTEXT 讀控制項文字(SendMessageTimeout，目標忙線不阻塞)。"""
    try:
        buf = ctypes.create_unicode_buffer(max_len)
        # lpdwResult 是 PDWORD_PTR(64 位元下 8 bytes);用 c_size_t 才不會寫越界。
        res = ctypes.c_size_t(0)
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            hwnd, win32con.WM_GETTEXT, max_len, buf,
            SMTO_ABORTIFHUNG, 1200, ctypes.byref(res))
        return buf.value or ""
    except Exception:
        return ""


def _find_text_panes(children: list, min_height: int = 40) -> list:
    """從控制項樹挑出可能承載會診事項/病情摘要的多行文字控制項。

    純函式(輸入為 enum_children 的 (hwnd, class, text, rect) list)以便測試。
    篩選:class 含 memo/richedit/richview/edit(大小寫無關)且高度 >= min_height
    (排除單行篩選輸入框)。回傳依畫面位置(上→下、左→右)排序。"""
    panes = []
    for hwnd, cls, _txt, rect in children:
        c = (cls or "").lower()
        if not any(k in c for k in ("memo", "richedit", "richview", "edit")):
            continue
        try:
            height = rect[3] - rect[1]
        except (TypeError, IndexError):
            continue
        if height < min_height:
            continue
        panes.append((hwnd, cls, rect))
    panes.sort(key=lambda item: (item[2][1], item[2][0]))
    return panes


# [2026-06-15 consult-extract 結構修正] 實機 dump(consult_query.log)證實:會診
# 清單的每位病人是一顆 **TRadioButton**(文字如 '莊振銘B7(163)002958' =
# 姓名+床號+房號+病歷號),裝在 TPageControl→TTabSheet 內;清單**不是 Delphi
# 格線**。舊版只找 class 含 "grid" 的控制項 → 永遠找不到 → 整個逐列點選迴圈
# 被跳過,實測「0 位病人」。故改為直接從 TRadioButton 文字解析病人清單(免
# OCR/截圖猜/像素點選),要逐病人內文則 BM_CLICK 該 radio 再讀下方 memo。
# CJK 範圍涵蓋 Ext A(㐀-䶿)、基本區(一-鿿)、相容表意文字、Ext B+(astral)
# —— 罕用字姓名(如 𠮷)也不致被漏判或截斷。
_CJK_CHARS = (r"㐀-䶿一-鿿豈-﫿𠀀-𯿿"
              r"-�■-◿")
_NAME_RE = re.compile(f"[{_CJK_CHARS}·]+")
# 病人列文字結構:含床號/房號 '(數字)' 或 >=4 碼病歷號。以「結構」判定而非「含
# 中文」—— 否則外籍病人(羅馬拼音姓名、無中文)會被漏掉=漏會診通知,有安全疑慮。
_PATIENT_LABEL_RE = re.compile(r"\(\d+\)|\d{4,}")
_PATIENT_RADIO_CLASS = "TRadioButton"


def _find_patient_radios(children: list) -> list:
    """從控制項樹挑出病人列 → [(hwnd, text, rect)]。純函式以便測試。

    病人 = class 精確為 TRadioButton(排除篩選選項 —— 那些是 TRadioGroup 內的
    TGroupButton,class 不同)且文字帶病人標記結構(床號/房號/病歷號)。以結構
    而非「含中文」判定,外籍病人(無中文姓名)也不會被漏掉。依文字去重(同列只
    留一筆),再依畫面位置(上→下、左→右)排序 = 清單實際顯示順序。呼叫端會先以
    「在會診視窗子樹中可見」過濾,排除非作用分頁的殘留 radio。"""
    out = []
    seen = set()
    for hwnd, cls, txt, rect in children:
        if cls != _PATIENT_RADIO_CLASS:
            continue
        t = (txt or "").strip()
        if not t or not _PATIENT_LABEL_RE.search(t) or t in seen:
            continue
        seen.add(t)
        out.append((hwnd, t, rect))
    out.sort(key=lambda it: (it[2][1], it[2][0]))
    return out


def _patient_display_name(text: str) -> str:
    """取病人顯示簡名:開頭連續中文(含·)= 姓名。取不到回前 8 字。
    '莊振銘B7(163)002958' → '莊振銘'。純函式。"""
    t = (text or "").strip()
    m = _NAME_RE.match(t)
    return m.group(0) if m else t[:8]


def _format_patient_roster(texts: list, label: str = "今日會診病人") -> str:
    """把病人 radio 文字組成清單(純文字版)。純函式;空回空字串。
    label 依寄送時段帶入(昨晚今早/下午會診清單)。這份清單直接來自 UI 控制項
    文字,最準確,與下方逐病人內文/截圖互為佐證。"""
    items = [t.strip() for t in texts if t and t.strip()]
    if not items:
        return ""
    lines = [f"{label}({len(items)} 位):"]
    for i, t in enumerate(items, 1):
        lines.append(f"{i}. {t}")
    return "\n".join(lines)


# =============================================================================
# 信件美化(HTML)— 與純文字版並存(multipart/alternative)。所有 HTML 用 inline
# style + table 排版(email client 不吃 <style>/CSS 變數),文字一律 escape。
# =============================================================================
# 高質感色板:單一強調色 + 中性灰階 + 大量留白 + 髮絲線。會診原因(綠)/病情摘要
# (靛)兩色底橫幅清楚區分、好閱讀。
_MAIL_ACCENT = "#0f766e"       # 主強調:醫療綠
_MAIL_INK = "#1a2230"          # 主要文字
_MAIL_BODY = "#39434f"         # 內文
_MAIL_SUB = "#5b6470"          # 次要(表格欄位)
_MAIL_MUTED = "#8a9099"        # 灰標
_MAIL_FAINT = "#a3a8b0"        # 更淡(欄位小標/頁尾)
_MAIL_HAIR = "#eef0f3"         # 區段髮絲線
_MAIL_ROW = "#f2f3f5"          # 表格列線
_MAIL_HEAD = "#e9ebee"         # 表頭線
_MAIL_REASON_BG = "#e9f4f0"    # 會診原因底(綠)
_MAIL_REASON_FG = "#134b40"
_MAIL_SUMMARY_BD = "#3f5d7a"   # 病情摘要(靛)框線/標籤
_MAIL_SUMMARY_BG = "#eef2f8"   # 病情摘要底
_MAIL_SUMMARY_FG = "#39434f"

# 病人列結構解析(best-effort):'莊振銘B7(163)0029588049(沈冠宇)06/15(08:20)'
# → 姓名 / 病房 / 床號 / 病歷號 / 主治 / 時間。解析不到(如外籍病人無中文姓名)
# 回 None,呼叫端整列顯示原字串 —— 絕不漏人、不亂拆。
_ROSTER_ROW_RE = re.compile(
    rf"^(?P<name>[{_CJK_CHARS}·]+)"
    rf"(?P<ward>[A-Za-z]+\d*)?"          # 病房:字母開頭,數字可有可無(C16/B7,也含純字母如 BURN/ICU)
    rf"(?:\((?P<bed>[0-9A-Za-z]+)\))?"   # 床號可含英數,如 18A
    rf"(?P<chart>\d{{6,}})?"
    rf"(?:\((?P<vs>[{_CJK_CHARS}·]+)\))?"
    rf"\s*(?P<date>\d{{1,2}}/\d{{1,2}})?"
    rf"\s*(?:\((?P<time>\d{{1,2}}:\d{{2}})\))?")

# 文字面板序號 → 有意義的標籤(實機:內容1=會診原因,內容2=病情摘要)
_PANE_LABEL_MAP = {"內容1": "會診原因", "內容2": "病情摘要"}


def _consult_slot_label(trigger_label: str, now: datetime) -> str:
    """依寄送時段給清單標題。純函式。
    中午班(<15:00,含 12:30 排程)= 昨晚今早會診清單;
    下午班(>=15:00,含 17:30 排程)= 下午會診清單。
    scheduled trigger(如 '12:30')用其時刻;email/手動用 now 的時鐘。"""
    hour = now.hour
    if trigger_label and ":" in trigger_label:
        try:
            hour = int(trigger_label.split(":")[0])
        except (ValueError, IndexError):
            hour = now.hour
    return "昨晚今早會診清單" if hour < 15 else "下午會診清單"


def _parse_roster_row(text: str):
    """把一列病人文字解析成欄位 dict;結構太弱或非預期格式回 None(走 raw fallback)。
    [codex review] 用 fullmatch:整列都被解析掉才算結構化,否則(尾端有未預期文字)
    回 None 改顯示原字串 —— 避免 prefix match 把尾端資訊靜默丟掉。"""
    m = _ROSTER_ROW_RE.fullmatch((text or "").strip())
    if not m or not m.group("name"):
        return None
    chart = m.group("chart") or ""
    bed = m.group("bed") or ""
    ward = m.group("ward") or ""
    if not chart and not bed:
        return None  # 只認到姓名、無病歷號/床號 → 寧可顯示原字串避免遺漏資訊
    ward_bed = " · ".join(p for p in (ward, bed) if p)
    return {"name": m.group("name"), "ward_bed": ward_bed, "chart": chart,
            "vs": m.group("vs") or "", "date": m.group("date") or "",
            "time": m.group("time") or ""}


def _roster_when(p: dict) -> str:
    """把解析結果的日期+時間組成顯示字串:'06/17 11:23' / '11:23' / ''。"""
    return " ".join(x for x in (p.get("date", ""), p.get("time", "")) if x)


def _patient_head(raw: str) -> tuple:
    """從病人列原文取 (姓名, meta);meta = '病房·床 病歷號 日期時間'(存在才放,
    全形空白分隔)。解析不出結構 → (顯示簡名, '')。純函式,給逐病人內文標題用。"""
    p = _parse_roster_row(raw)
    if not p:
        return _patient_display_name(raw), ""
    parts = [x for x in (p["ward_bed"], p["chart"], _roster_when(p)) if x]
    return p["name"], "　".join(parts)


def _esc(s) -> str:
    return _html.escape(str(s or ""))


def _section_label(text: str, top: int = 26) -> str:
    """小節標籤:字距微調的小寫強調色標題(信箋式)。"""
    return (f'<div style="font-size:11px;letter-spacing:1.5px;'
            f'color:{_MAIL_ACCENT};text-transform:uppercase;'
            f'margin:{top}px 0 14px;">{_esc(text)}</div>')


def _format_patient_roster_html(texts: list, label: str) -> str:
    """病人清單 → HTML 表格(髮絲線、字距小標、數字對齊)。解析得到欄位就分欄;
    失敗整列顯示原字串。空回空字串。"""
    items = [t.strip() for t in texts if t and t.strip()]
    if not items:
        return ""
    th = (f"padding:0 0 8px;border-bottom:1px solid {_MAIL_HEAD};font-size:10.5px;"
          f"letter-spacing:.8px;color:{_MAIL_FAINT};text-transform:uppercase;"
          "text-align:left;")
    th_r = th + "text-align:right;"
    rows = [
        f'<tr><td style="{th}">姓名</td><td style="{th}">病房 / 床</td>'
        f'<td style="{th}">病歷號</td><td style="{th}">主治</td>'
        f'<td style="{th_r}">時間</td></tr>']
    last = len(items)
    for i, t in enumerate(items, 1):
        line = "" if i == last else f"border-bottom:1px solid {_MAIL_ROW};"
        td = f"padding:11px 0;{line}font-size:13px;color:{_MAIL_SUB};"
        td_num = td + "font-variant-numeric:tabular-nums;"
        td_r = td_num + "text-align:right;"
        p = _parse_roster_row(t)
        if p:
            rows.append(
                f'<tr><td style="{td}color:{_MAIL_INK};font-weight:500;">'
                f'{_esc(p["name"])}</td>'
                f'<td style="{td}">{_esc(p["ward_bed"])}</td>'
                f'<td style="{td_num}">{_esc(p["chart"])}</td>'
                f'<td style="{td}">{_esc(p["vs"])}</td>'
                f'<td style="{td_r}">{_esc(_roster_when(p))}</td></tr>')
        else:
            rows.append(
                f'<tr><td style="{td}color:{_MAIL_INK};" colspan="5">'
                f'{_esc(t)}</td></tr>')
    return (
        _section_label(f"{label}　·　{len(items)} 位")
        + '<table class="cq-tbl" style="width:100%;border-collapse:collapse;">'
        + "".join(rows) + "</table>")


def _consult_band(label: str, para: str, *, bg: str, border: str,
                  label_fg: str, text_fg: str, text_size: str,
                  line_height: str, text_cls: str = "") -> str:
    """一段有底色的橫幅(左側細框 + 字距小標 + 內文),會診原因/病情摘要共用。
    text_cls 讓手機 media query 放大內文字級/行高(長內文好讀)。"""
    cls = f' class="{text_cls}"' if text_cls else ""
    return (
        f'<div style="background:{bg};border-left:3px solid {border};'
        f'border-radius:0 6px 6px 0;padding:10px 14px;margin-bottom:9px;">'
        f'<div style="font-size:10.5px;letter-spacing:1px;color:{label_fg};'
        f'text-transform:uppercase;font-weight:600;margin-bottom:4px;">{label}</div>'
        f'<div{cls} style="font-size:{text_size};color:{text_fg};'
        f'line-height:{line_height};">{para}</div></div>')


def _format_extracted_entries_html(entries: list, labels: list | None = None) -> str:
    """逐病人擷取內容 → 文件式區塊:姓名(細直線)+ 會診原因(綠橫幅)+ 病情摘要
    (靛橫幅),病人間以髮絲線分隔。空回空字串。"""
    rich = [(i, panes) for i, panes in enumerate(entries, 1)
            if any((txt or "").strip() for _l, txt in panes)]
    blocks = []
    for pos, (i, panes) in enumerate(rich):
        texts = [(lab, (txt or "").strip()) for lab, txt in panes]
        texts = [(lab, txt) for lab, txt in texts if txt]
        raw_head = (labels[i - 1] if labels and i - 1 < len(labels)
                    and labels[i - 1] else "")
        name, meta = _patient_head(raw_head) if raw_head else (f"病人 {i}", "")
        # 姓名後接床位/病歷號/時間(較小、淡色)。手機(cq-meta)會掉到下一行不跑版。
        meta_html = (f'<span class="cq-meta" style="font-weight:400;'
                     f'font-size:12.5px;color:{_MAIL_SUB};margin-left:10px;">'
                     f'{_esc(meta)}</span>') if meta else ""
        bands = []
        for lab, txt in texts:
            disp = _PANE_LABEL_MAP.get(lab, lab)
            para = _esc(txt).replace("\n", "<br>")
            if disp == "會診原因":
                bands.append(_consult_band(
                    "會診原因", para, bg=_MAIL_REASON_BG, border=_MAIL_ACCENT,
                    label_fg=_MAIL_ACCENT, text_fg=_MAIL_REASON_FG,
                    text_size="14px", line_height="1.55", text_cls="cq-read"))
            else:
                # 病情摘要常很長 → 基礎字級拉到 14px/行高 1.8,手機再經 .cq-read 放大
                bands.append(_consult_band(
                    _esc(disp), para, bg=_MAIL_SUMMARY_BG,
                    border=_MAIL_SUMMARY_BD, label_fg=_MAIL_SUMMARY_BD,
                    text_fg=_MAIL_SUMMARY_FG, text_size="14px",
                    line_height="1.8", text_cls="cq-read"))
        sep = ("" if pos == len(rich) - 1
               else f"border-bottom:1px solid {_MAIL_HAIR};padding-bottom:22px;")
        blocks.append(
            f'<div style="margin-bottom:22px;{sep}">'
            f'<div style="font-size:15px;font-weight:600;color:{_MAIL_INK};'
            f'border-left:2px solid {_MAIL_ACCENT};padding-left:11px;'
            f'margin-bottom:11px;">{_esc(name)}{meta_html}</div>'
            + "".join(bands) + "</div>")
    if not blocks:
        return ""
    return _section_label("會診內容", top=30) + "".join(blocks)


def _fmt_mail_datetime(date_str, time_str) -> str:
    """'2026/6/15','1230' → '2026 年 6 月 15 日　12:30'。解析失敗回原樣串接。
    [codex review] 先把輸入強制轉字串:None/數字等非預期型別不可在送信路徑拋例外。"""
    date_str = str(date_str or "")
    time_str = str(time_str or "")
    d = date_str
    try:
        y, m, day = date_str.split("/")
        d = f"{y} 年 {int(m)} 月 {int(day)} 日"
    except Exception:
        pass
    t = time_str
    if len(time_str) == 4 and time_str.isdigit():
        t = f"{time_str[:2]}:{time_str[2:]}"
    return f"{d}　{t}".strip()


def _build_consult_email_html(date_str: str, time_str: str, intro: str,
                              content_html: str) -> str:
    """組整封 HTML 信(信箋式 + 響應式手機版)。content_html 可空(擷取失敗仍是
    乾淨的標題+前言+頁尾)。

    手機可讀性:完整 HTML 文件帶 viewport=device-width → iPhone 等不再用桌面寬度
    縮放整封信導致字超小;@media(≤600px)讓卡片滿版、縮左右留白、放大內文字級與
    行高(.cq-read)。支援 <style> 的客戶端(iOS Mail/Apple Mail)會套用;不支援的
    (部分 Gmail)則退回 inline 基礎樣式,內文基礎字級也已拉到 14px,仍可讀。"""
    dt = _fmt_mail_datetime(date_str, time_str)
    style = (
        "<style>@media only screen and (max-width:600px){"
        ".cq-bg{padding:0!important;}"
        ".cq-card{border-radius:0!important;border-left:0!important;"
        "border-right:0!important;}"
        ".cq-pad{padding-left:18px!important;padding-right:18px!important;}"
        ".cq-hr{margin-left:18px!important;margin-right:18px!important;}"
        ".cq-read{font-size:15px!important;line-height:1.85!important;}"
        ".cq-tbl td{font-size:12px!important;padding-top:9px!important;"
        "padding-bottom:9px!important;}"
        ".cq-meta{display:block!important;margin-left:0!important;"
        "margin-top:4px!important;}"
        "}</style>")
    return (
        '<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        + style + '</head><body style="margin:0;padding:0;background:#f5f6f8;">'
        '<div class="cq-bg" style="padding:22px;font-family:-apple-system,'
        "'Segoe UI','PingFang TC','Microsoft JhengHei',Roboto,sans-serif;\">"
        '<div class="cq-card" style="max-width:600px;margin:0 auto;background:#fff;'
        'border:1px solid #ecedf0;border-radius:12px;overflow:hidden;">'
        f'<div style="height:3px;background:{_MAIL_ACCENT};"></div>'
        '<div class="cq-pad" style="padding:30px 34px 0;">'
        f'<div style="font-size:11px;letter-spacing:2px;color:{_MAIL_MUTED};'
        'text-transform:uppercase;">皮膚科會診系統</div>'
        f'<div style="font-size:21px;font-weight:600;color:{_MAIL_INK};'
        'margin-top:7px;">會診通知單</div>'
        f'<div style="font-size:13px;color:{_MAIL_MUTED};margin-top:5px;">'
        f'{_esc(dt)}　·　系統自動擷取</div></div>'
        f'<div class="cq-hr" style="height:1px;background:{_MAIL_HAIR};'
        'margin:22px 34px;"></div>'
        '<div class="cq-pad" style="padding:0 34px;font-size:13px;'
        f'line-height:1.7;color:#6b7280;">{_esc(intro)}</div>'
        f'<div class="cq-pad" style="padding:0 34px;">{content_html}</div>'
        # [2026-06-17] 移除頁尾「本信由中國醫皮膚科系統自動擷取寄送 · 內容僅供
        # 輔助閱讀,正式內容以附件截圖為準」(user 要求)。保留 30px 底部留白,避免
        # 卡片內容貼齊邊緣。
        '<div style="height:30px;"></div>'
        '</div></div></body></html>')


# =============================================================================
# [新功能 2026-06-15] 今日打卡狀態併入信件
# 查 autoclock 各帳號今日「上班(07:30-12:40,含早上/中午上班)」與「下班
# (17:00-17:30)」是否完成,排了班卻沒打到才標紅「未打卡」,沒排班標「無排班」。
# (上班窗到 12:40 而非 12:30 的原因見下方 _PUNCH_AM_WINDOW 註解:要含 12:31 的中午上班。)
# 資料源 = 打卡 portal 真實紀錄(cmuh_common.punch_status,自建 headless Chrome)。
# 完全 fail-open:查不到/查失敗都不影響會診信寄出。
# =============================================================================
_AUTOCLOCK_CONFIG_FILE = SETTINGS_DIR / "autoclock_config.json"
# 上班窗涵蓋早上(am_in,7:31)與中午(midday_in,12:31)上班。打卡系統中午是 12:31 才
# 打卡(落在官方 12:30-13:00 窗),故上班窗需到 12:40(信件 12:40 才寄,屆時該筆已寫入)
# 才抓得到中午上班;若只到 12:30 會漏掉 12:31 的中午上班、誤判未打卡。下班窗為 pm_out。
_PUNCH_AM_WINDOW = (dt_time(7, 30), dt_time(12, 40))
_PUNCH_PM_WINDOW = (dt_time(17, 0), dt_time(17, 30))

# state → (純文字標籤, HTML 文字色, HTML 底色)
_PUNCH_VIEW = {
    "ok":   ("✅ 成功", "#15803d", "#e8f5ee"),
    "fail": ("❌ 未打卡", "#c0392b", "#fbeceb"),
    "off":  ("➖ 今日無排班", _MAIL_FAINT, "#f4f5f6"),
}


def _punch_text_cell(state, time_str) -> str:
    """單一上/下班狀態 → 純文字。純函式。"""
    label = _PUNCH_VIEW.get(state, ("— 不明", "", ""))[0]
    if state == "ok" and time_str:
        return f"{label}（{time_str}）"
    return label


def _format_punch_text(results: list, show_off: bool = True) -> str:
    """各帳號今日上/下班狀態 → 純文字段落。純函式;空回空字串。
    results=[{username, on, on_time, off, off_time, error}]。
    show_off=False(尚未過 17:10)→ 只列上班、不列下班(避免顯示誤導的「下班未打卡」)。"""
    if not results:
        return ""
    win = ("上班 07:30-12:40 / 下班 17:00-17:30" if show_off
           else "上班 07:30-12:40（過 17:10 才附下班）")
    lines = [f"今日打卡狀態（{len(results)} 個帳號，{win}）："]
    for r in results:
        u = str(r.get("username", "")).strip()
        if r.get("error"):
            lines.append(f"  {u}　⚠️ 查詢失敗（{r['error']}）")
            continue
        on = _punch_text_cell(r.get("on"), r.get("on_time"))
        if show_off:
            off = _punch_text_cell(r.get("off"), r.get("off_time"))
            lines.append(f"  {u}　上班 {on}　下班 {off}")
        else:
            lines.append(f"  {u}　上班 {on}")
    return "\n".join(lines)


def _punch_badge_html(state, time_str) -> str:
    """單一狀態 → 彩色徽章 HTML。純函式。"""
    label, fg, bg = _PUNCH_VIEW.get(state, ("不明", _MAIL_MUTED, "#f4f5f6"))
    t = f"　{_esc(time_str)}" if (state == "ok" and time_str) else ""
    return (f'<span style="display:inline-block;padding:3px 10px;border-radius:11px;'
            f'background:{bg};color:{fg};font-size:12px;font-weight:600;'
            f'white-space:nowrap;">{_esc(label)}{t}</span>')


def _format_punch_html(results: list, show_off: bool = True) -> str:
    """各帳號今日上/下班狀態 → HTML 表格(信箋式)。純函式;空回空字串。
    show_off=False(尚未過 17:10)→ 不出「下班」欄(避免顯示誤導的「下班未打卡」)。"""
    if not results:
        return ""
    th = (f"padding:0 0 8px;border-bottom:1px solid {_MAIL_HEAD};font-size:10.5px;"
          f"letter-spacing:.8px;color:{_MAIL_FAINT};text-transform:uppercase;"
          "text-align:left;")
    off_th = f'<td style="{th}">下班</td>' if show_off else ""
    rows = [f'<tr><td style="{th}">打卡帳號</td><td style="{th}">上班</td>'
            f'{off_th}</tr>']
    err_colspan = "2" if show_off else "1"
    last = len(results)
    for i, r in enumerate(results, 1):
        line = "" if i == last else f"border-bottom:1px solid {_MAIL_ROW};"
        td = f"padding:11px 0;{line}font-size:13px;color:{_MAIL_SUB};"
        u = str(r.get("username", "")).strip()
        name_td = (f'<td style="{td}color:{_MAIL_INK};font-weight:500;'
                   f'font-variant-numeric:tabular-nums;">{_esc(u)}</td>')
        if r.get("error"):
            rows.append(
                f'<tr>{name_td}<td style="{td}color:#b7791f;" colspan="{err_colspan}">'
                f'⚠️ 查詢失敗（{_esc(r["error"])}）</td></tr>')
        else:
            off_td = (f'<td style="{td}">'
                      f'{_punch_badge_html(r.get("off"), r.get("off_time"))}</td>'
                      if show_off else "")
            rows.append(
                f'<tr>{name_td}'
                f'<td style="{td}">{_punch_badge_html(r.get("on"), r.get("on_time"))}</td>'
                f'{off_td}</tr>')
    label = "今日打卡狀態" if show_off else "今日上班打卡狀態"
    return (
        _section_label(f"{label}　·　{len(results)} 個帳號")
        + '<table class="cq-tbl" style="width:100%;border-collapse:collapse;">'
        + "".join(rows) + "</table>")


def _load_autoclock_accounts() -> list:
    """讀 autoclock_config.json 的帳號清單(有 username 的 dict,依 username 去重,保留
    第一筆)。fail-open 回 []。去重避免設定檔誤填重複帳號時白白多登入一次。"""
    try:
        data = safe_load_json(_AUTOCLOCK_CONFIG_FILE, [])
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out, seen = [], set()
    for a in data:
        if not (isinstance(a, dict) and a.get("username")):
            continue
        u = str(a["username"]).strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(a)
    return out


def _build_punch_status_sections(cfg: dict, now: datetime = None) -> tuple:
    """查各帳號今日上/下班 → (純文字段落, HTML 段落)。完全 fail-open:任何失敗回
    ('','')、不影響會診信寄出(打卡只是附帶資訊)。

    [2026-06-25 user] 時間閘:過了 12:40 才附「上班」、過了 17:10 才附「下班」。避免 poll 在
    還沒到下班打卡時間就寄信、打卡表顯示誤導的「下班未打卡」。12:40 前兩者都還沒到 → 不查、不附
    (連打卡 portal 都不登入)。email 觸發本就不進這支(在 _do_full_job 已先擋掉)。"""
    if not cfg.get("punch_status_in_email", True):
        return "", ""
    now = now or datetime.now()
    show_on = now.time() >= dt_time(12, 40)    # 過了 12:40 才附上班
    show_off = now.time() >= dt_time(17, 10)   # 過了 17:10 才附下班(必然 show_on 也成立)
    if not show_on:
        logging.info("[punch] 尚未過 12:40,本次不附今日打卡狀態(不登入打卡 portal)")
        return "", ""
    try:
        accounts = _load_autoclock_accounts()
        if not accounts:
            logging.info("[punch] 無 autoclock 帳號,信件不附打卡狀態")
            return "", ""
        from cmuh_common.punch_status import query_accounts_today
        logging.info("[punch] 查詢 %d 個帳號今日打卡狀態(附下班=%s)…",
                     len(accounts), show_off)
        results = query_accounts_today(
            accounts, am_window=_PUNCH_AM_WINDOW, pm_window=_PUNCH_PM_WINDOW)
        return (_format_punch_text(results, show_off),
                _format_punch_html(results, show_off))
    except Exception:
        logging.warning("[punch] 打卡狀態查詢/組裝失敗(會診信照常寄,不附打卡)",
                        exc_info=True)
        return "", ""


def _is_email_trigger(trigger_label: str) -> bool:
    """是否為「email(皮膚科會診觸發)」觸發。IMAP 觸發固定用 trigger_label=='email'
    (見 trigger_job_async('email', override_recipients=...))。只有這種觸發省略今日
    打卡狀態(連打卡 portal 都不登入查詢);排程(HH:MM 時間字串如 '12:40'/'17:10')
    與手動('手動')觸發都要附今日打卡狀態。純函式。"""
    return trigger_label == "email"


def _format_extracted_entries(entries: list, labels: list | None = None) -> str:
    """把逐病人擷取結果組成信件附文。entries=[ [(label, text), ...], ... ]。
    labels(可選)為各病人的標題(對齊 entries 索引),用於以姓名標示;未提供時
    退回「病人 N」。純函式以便測試;全空回空字串(信件就不附這段)。"""
    blocks = []
    for i, panes in enumerate(entries, 1):
        texts = [(label, (text or "").strip()) for label, text in panes]
        texts = [(label, text) for label, text in texts if text]
        if not texts:
            continue
        if labels and i - 1 < len(labels) and labels[i - 1]:
            name, meta = _patient_head(labels[i - 1])
        else:
            name, meta = f"病人 {i}", ""
        lines = [f"【{name}】" + (f"　{meta}" if meta else "")]
        for label, text in texts:
            lines.append(f"[{label}]")
            lines.append(text)
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return ("── 以下為自動擷取的會診文字內容(輔助閱讀，請以截圖為準) ──\n\n"
            + "\n\n".join(blocks))


def _click_grid_point(grid_hwnd: int, x: int, y: int) -> None:
    """背景點擊格線 client 座標(PostMessage，不動真實滑鼠)。"""
    lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    win32gui.PostMessage(grid_hwnd, win32con.WM_LBUTTONDOWN,
                         win32con.MK_LBUTTON, lparam)
    time.sleep(0.05)
    win32gui.PostMessage(grid_hwnd, win32con.WM_LBUTTONUP, 0, lparam)


def _read_panes_snapshot(panes: list) -> list:
    """讀取全部文字面板目前內容 → [(label, text), ...]。label 依畫面順序編號
    (內容1=最上面的面板;實機跑過一次後可依 log 對照其實際意義)。"""
    out = []
    for i, (hwnd, _cls, _rect) in enumerate(panes, 1):
        out.append((f"內容{i}", _read_ctrl_text(hwnd)))
    return out


def _is_visible_below(hwnd: int, top: int) -> bool:
    """hwnd 在「top 以下的子樹」中是否可見:檢查 hwnd 及其各祖先(往上到 top 為
    止、不含 top)是否都有 WS_VISIBLE。

    刻意忽略 top 本身的可見性 —— SW_HIDE 後備模式會把整個會診視窗藏起(top 無
    WS_VISIBLE),此時 IsWindowVisible 對每個子控制項都回 False、無法分辨分頁;
    但非作用分頁的 TTabSheet 其 WS_VISIBLE 仍被 TPageControl 清掉(與 top 無關),
    故只看「到 top 為止」的鏈即可在兩種模式下都正確排除非作用分頁的殘留 radio。
    任何例外回 True(fail-open,寧可多列也不漏病人)。"""
    try:
        cur = hwnd
        for _ in range(50):  # 防環/防失控的上限
            if not cur or cur == top:
                return True
            style = win32gui.GetWindowLong(cur, win32con.GWL_STYLE)
            if not (style & win32con.WS_VISIBLE):
                return False
            cur = win32gui.GetParent(cur)
        return True
    except Exception:
        return True


def _select_patient_radio(hwnd: int) -> bool:
    """同步選取病人 radio:SendMessageTimeout(BM_CLICK) 會等控制項處理完點擊
    (Delphi OnClick 已觸發、開始載入下方會診內文),不動真實滑鼠。回傳是否確實
    送達 —— 未送達時呼叫端必須放棄逐病人內文擷取(否則面板仍是上一位的內容,
    會被錯置到這位病人名下)。"""
    try:
        # lpdwResult 是 PDWORD_PTR(64 位元下 8 bytes);用 c_size_t 才不會寫越界。
        res = ctypes.c_size_t(0)
        SMTO_ABORTIFHUNG = 0x0002
        ok = ctypes.windll.user32.SendMessageTimeoutW(
            hwnd, win32con.BM_CLICK, 0, 0, SMTO_ABORTIFHUNG, 1500,
            ctypes.byref(res))
        return bool(ok)
    except Exception:
        logging.debug("BM_CLICK radio %s 失敗", hwnd, exc_info=True)
        return False


def _read_panes_after_change(panes: list, baseline_sig, timeout: float = 2.5,
                             interval: float = 0.12) -> tuple:
    """選病人後輪詢面板,等內容(1)變得跟「點選前的 baseline」不同 且(2)連兩
    次讀取一致(已穩定)。回 (snap, ok):
      ok=True  → 已「脫離 baseline 且穩定」,snap 可信為這位病人的內文。
      ok=False → 逾時仍未達成(沒換/載入過慢/多面板分批未定),snap 不可信,
                 呼叫端必須放棄逐病人內文(絕不把混合/殘留內容錯置到病人名下)。
    要求「穩定」是因多面板分批載入時,單看「有變」可能讀到「新面板+另一面板殘留
    舊值」的混合快照。"""
    deadline = time.time() + timeout
    snap = _read_panes_snapshot(panes)
    prev_sig = None
    while time.time() < deadline:
        sig = tuple(t for _l, t in snap)
        if sig != baseline_sig and sig == prev_sig:
            return snap, True      # 已脫離 baseline 且穩定 → 可信
        prev_sig = sig
        time.sleep(interval)
        snap = _read_panes_snapshot(panes)
    return snap, False             # 逾時:未達「脫離+穩定」→ 不可信


def _extract_consult_text(consult_hwnd: int, cfg: dict,
                          roster_label: str = "今日會診病人") -> tuple:
    """主入口:從會診視窗擷取逐病人文字。回 (純文字版, HTML內容片段, roster_texts)。

    roster_texts(第三個回傳,CQ-01/02):病人清單「列字串」清單 —— None=擷取失敗/停用
    (無法判斷有沒有新會診 → 呼叫端 fail-open);[]=擷取成功但真的沒病人;[...]=清單列。
    text/html 仍為 best-effort(任何失敗回 ""),但 roster 通道讓 poll 能區分「沒新會診」
    與「解析失敗」,不再把解析失敗誤當「沒新會診」而靜默不寄。"""
    if not cfg.get("extract_text_enabled", True):
        return "", "", None
    try:
        children = enum_children(consult_hwnd)
        # 控制項樹 dump(每次執行記一次)：供依實機結構微調 extract_* 參數
        logging.info(
            "[consult-extract] 控制項樹(%d 個): %s",
            len(children),
            " | ".join(
                f"{cls}@({r[0]},{r[1]},{r[2]-r[0]}x{r[3]-r[1]})"
                + (f" t={txt[:16]!r}" if txt else "")
                for _h, cls, txt, r in children[:80]))

        # 病人清單 = TRadioButton 文字(最準確,直接來自 UI,免 OCR/像素點選)。
        # 以「在會診視窗子樹中可見」過濾,排除非作用分頁的殘留 radio。此判定不看
        # 會診視窗本身是否被 SW_HIDE,故隱藏桌面/正常/後備三種模式皆正確 —— 作用
        # 分頁真的沒病人時清單即為空,不會誤把其他分頁的隱藏 radio 當成今日病人。
        radios = _find_patient_radios(
            [c for c in children if _is_visible_below(c[0], consult_hwnd)])
        roster_texts = [t for _h, t, _r in radios]
        roster = _format_patient_roster(roster_texts, label=roster_label)
        roster_html = _format_patient_roster_html(roster_texts, roster_label)

        panes = _find_text_panes(children)
        if not panes:
            # 抓不到文字面板:逐病人內文擷取不了,但準確的病人清單仍可寄出。
            logging.info("[consult-extract] 找不到文字面板(Memo/RichEdit)，"
                         "本次只附病人清單+截圖;請把上行控制項樹回報以便調整")
            return roster, roster_html, roster_texts

        entries: list = []
        labels: list = []

        if radios:
            # ── 主路徑:逐顆病人 radio 同步選取 → 等內文更新 → 讀 memo ──
            # 每位病人「點選前」先記 baseline。逐位確認面板內容已是這位病人的;遇到
            # 第一個無法確認者就【保留已確認的前段、就此停止】,不續讀後續病人。
            # [安全] 為何停止而非跳過續讀:被跳過病人的「延遲非同步更新」可能在下一位
            # 的「變化+穩定」判定期間才落地 → 把上一位內容錯置到下一位名下。停止即可
            # 完全杜絕此 race(已確認的前段都是正確對位的)。準確的病人清單仍照常附上。
            #   (a) 選取未送達;或
            #   (b) 第二位以後點選後內文仍未更新(載入過慢/被忽略 → 無法確認)。
            # 第一位(idx 0)是開窗預設選取列,內容本就為其所屬,直接讀。
            logging.info("[consult-extract] 偵測到 TRadioButton 病人清單(%d 位)",
                         len(radios))
            # [W11] 逐病人擷取的總體 deadline:病人多 + 後端慢時,避免 N×每列等待累積
            # 拖住整個流程。逾時就保留已確認的前段停止(與逐位確認失敗同語意)。
            extract_deadline = time.monotonic() + _EXTRACT_TOTAL_TIMEOUT_SEC
            for idx, (hwnd, text, _rect) in enumerate(radios):
                if time.monotonic() > extract_deadline:
                    logging.info("[consult-extract] 逐病人擷取超過 %ds → 保留已確認的"
                                 "前 %d 位、就此停止", _EXTRACT_TOTAL_TIMEOUT_SEC,
                                 len(entries))
                    break
                baseline = tuple(t for _l, t in _read_panes_snapshot(panes))
                if not _select_patient_radio(hwnd):
                    logging.info("[consult-extract] 第 %d 位選取未送達;保留已確認的"
                                 "前 %d 位、就此停止(不冒險續讀以免錯置)",
                                 idx + 1, len(entries))
                    break
                if idx == 0:
                    # 開窗預設選取列:內容本就為其所屬(開窗前已 sleep 等載入),
                    # 直接讀,不必等「變化」(它不會變)。
                    snap = _read_panes_snapshot(panes)
                else:
                    snap, ok = _read_panes_after_change(panes, baseline)
                    if not ok:
                        # 內文未「脫離 baseline 且穩定」→ 無法確認 → 保留前段、停止
                        # (避免被跳過病人的延遲更新錯置到後續病人名下)。
                        logging.info("[consult-extract] 第 %d 位內文未穩定更新;保留"
                                     "已確認的前 %d 位、就此停止", idx + 1, len(entries))
                        break
                entries.append(snap)
                # 存整列原文(非僅姓名):逐病人標題要由它取出 姓名+床位+病歷號+時間
                labels.append(text)
        else:
            # ── 後備路徑:舊式 Delphi 格線像素逐列點選(現環境非格線,僅保險) ──
            logging.info("[consult-extract] 無 TRadioButton 病人清單，"
                         "退回格線逐列點選後備路徑")
            seen_signatures: set = set()

            def _snap_and_collect() -> bool:
                snap = _read_panes_snapshot(panes)
                sig = tuple(t for _l, t in snap)
                if any(t.strip() for t in sig) and sig not in seen_signatures:
                    seen_signatures.add(sig)
                    entries.append(snap)
                    return True
                return False

            _snap_and_collect()  # 開窗預設選取列先收一次
            grid = next((h for h, cls, _t, _r in children
                         if "grid" in (cls or "").lower()), None)
            grid_rect = next(
                (r for h, _c, _t, r in children if h == grid), None)
            if grid is not None and grid_rect is not None:
                max_rows = int(cfg.get("extract_max_rows", 12) or 12)
                first_y = int(cfg.get("extract_first_row_y", 32) or 32)
                row_h = int(cfg.get("extract_row_height", 19) or 19)
                click_x = int(cfg.get("extract_click_x", 12) or 12)
                grid_height = grid_rect[3] - grid_rect[1]
                no_new = 0
                for row in range(max_rows):
                    y = first_y + row * row_h
                    if y >= grid_height - 2:
                        break
                    _click_grid_point(grid, click_x, y)
                    time.sleep(0.35)  # 等 Delphi 把下方面板換成該病人內容
                    if _snap_and_collect():
                        no_new = 0
                    else:
                        no_new += 1
                        if no_new >= 2:  # 連兩列沒有新內容=已過最後一列
                            break

            # [CQ-01 codex] 格線後備真的收到病人(entries 非空)但產不出乾淨清單列 →
            # roster_texts 設 None 讓 poll fail-open(有病人卻回空清單會被誤判無新會診);
            # entries 為空(今天真沒病人)→ 維持 []=無新會診,不因空清單每輪 fail-open 狂寄。
            if entries:
                roster_texts = None

        body = _format_extracted_entries(entries, labels=labels or None)
        text = "\n\n".join(part for part in (roster, body) if part)
        body_html = _format_extracted_entries_html(entries, labels=labels or None)
        html_inner = roster_html + body_html
        logging.info("[consult-extract] 擷取完成:清單 %d 位、內文區塊 %d 字",
                     len(radios) or len(entries), len(text))
        return text, html_inner, roster_texts
    except Exception:
        logging.warning("[consult-extract] 擷取失敗(照常只寄截圖)", exc_info=True)
        return "", "", None


def close_pids(pids: set, grace: float = 2.5) -> None:
    """關閉指定行程：先對其視窗送 WM_CLOSE，逾時再強制結束。"""
    if not pids:
        return
    for hwnd in find_windows(pids=pids, visible_only=False):
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        if not (_systemftp_pids() & pids):
            return
        time.sleep(0.3)
    for pid in pids:
        try:
            p = psutil.Process(pid)
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        except Exception:
            logging.debug("terminate pid %s 失敗", pid, exc_info=True)


def _cleanup_pids_excluding_borrowed(our_pids: set, before: set,
                                     borrowed: bool) -> set:
    """[review C2 fix 2026-06-12] SW_HIDE 後備模式收尾要關哪些 pid。

    borrowed=True(本次沒有出現新登入視窗、借用了「啟動前就存在」的實例 ——
    那可能是使用者自己開著的住院系統)時，排除 before 內的 pid：流程可以借
    它完成截圖，但收尾絕不可替使用者關掉他的程式。一般情況(borrowed=False)
    維持原行為，關掉本次開啟的全部實例。"""
    pids = set(our_pids)
    if borrowed:
        return pids - set(before)
    return pids


# =============================================================================
# 隱藏桌面（systemftp 完全在使用者看不到的虛擬桌面上跑，零干擾）
# =============================================================================
def _ensure_hidden_desktop():
    """建立或開啟隱藏桌面；回傳 HDESK（整數位址）或 None 表失敗。"""
    try:
        h = _user32.OpenDesktopW(HIDDEN_DESKTOP_NAME, 0, False,
                                  _DESKTOP_GENERIC_ALL)
        if h:
            return h
    except Exception:
        logging.debug("OpenDesktop 失敗", exc_info=True)
    try:
        h = _user32.CreateDesktopW(HIDDEN_DESKTOP_NAME, None, None, 0,
                                    _DESKTOP_GENERIC_ALL, None)
        return h or None
    except Exception:
        logging.warning("CreateDesktop 失敗", exc_info=True)
        return None


def _set_thread_desktop(hdesk) -> bool:
    """把目前執行緒切到指定桌面。回傳是否成功。"""
    try:
        return bool(_user32.SetThreadDesktop(hdesk))
    except Exception:
        return False


# =============================================================================
# 自動化主流程
# =============================================================================
def run_consult_flow(trigger_label: str = "") -> tuple:
    """執行完整會診查詢流程，回傳 (截圖路徑, 擷取純文字, 擷取HTML片段, roster_texts)。失敗會
    raise。擷取內容為 best-effort:抓不到時為空字串(信件就只有截圖)。roster_texts 見
    _extract_consult_text(None=解析失敗/停用、[]=無病人、[...]=清單列;供 poll 判斷)。

    優先用「隱藏桌面」執行 systemftp——它的所有視窗都在使用者看不到的
    虛擬桌面，永遠不會出現在使用者畫面、不會搶前景、滑鼠也不會動。
    若無法建立隱藏桌面（群組原則限制等），退回 SW_HIDE 後備模式。
    """
    cfg = load_config()
    logging.info("=== 開始會診查詢流程（觸發：%s）===", trigger_label or "手動")
    # 清單標題依寄送時段:12:30→昨晚今早會診清單、17:30→下午會診清單
    roster_label = _consult_slot_label(trigger_label, datetime.now())

    hdesk = _ensure_hidden_desktop()
    if hdesk:
        logging.info("使用隱藏桌面執行（systemftp 不會出現在你的畫面）")
        result: dict = {}

        def worker() -> None:
            try:
                if not _set_thread_desktop(hdesk):
                    raise RuntimeError("SetThreadDesktop 失敗")
                result["shot"] = _automation_on_hidden(cfg, roster_label)
            except Exception as e:  # noqa: BLE001
                result["error"] = e
            finally:
                # [stability] 由 worker(已 SetThreadDesktop 到此 hdesk)結束時關閉
                # HDESK handle，修正 _ensure_hidden_desktop 的 OpenDesktopW/
                # CreateDesktopW 從不 CloseDesktop 的永久 USER object 洩漏：常駐程式
                # 每次排程/IMAP 觸發/重試都洩一個，數天不重啟會逼近 per-process 上限
                # → 之後建立隱藏桌面失敗、退化成 SW_HIDE。逾時孤兒 worker 最終走到
                # 自身迴圈 deadline 結束時也會在此釋放(故洩漏被收斂、不再單調累積)。
                try:
                    _user32.CloseDesktop(hdesk)
                except Exception:
                    logging.debug("CloseDesktop 失敗", exc_info=True)

        t = threading.Thread(target=worker, name="ConsultAutomationHidden",
                              daemon=True)
        t.start()
        t.join(timeout=240)  # 4 分鐘硬上限
        if t.is_alive():
            raise RuntimeError("自動化執行超過 4 分鐘，已放棄（可能網路異常）")
        if result.get("error"):
            raise result["error"]
        return result["shot"]

    logging.warning("無法建立隱藏桌面，改用 SW_HIDE 後備模式（可能短暫看到視窗）")
    return _run_with_sw_hide(cfg, roster_label)


def _automation_on_hidden(cfg: dict, roster_label: str = "今日會診病人") -> tuple:
    """在隱藏桌面執行完整流程（呼叫者需已 SetThreadDesktop）。回傳 (截圖, 文字)。

    因為隱藏桌面上 systemftp 是唯一前景應用，不需要 stealth thread、不需要
    show_offscreen、不會與使用者畫面衝突——程式碼相對單純。
    """
    username = cfg["username"]
    password = cfg["password"]

    before = _systemftp_pids()
    si = win32process.STARTUPINFO()
    si.dwFlags = win32con.STARTF_USESHOWWINDOW
    si.wShowWindow = win32con.SW_SHOW  # 隱藏桌面上正常顯示，使用者看不到
    si.lpDesktop = HIDDEN_DESKTOP_NAME
    try:
        win32process.CreateProcess(SYSTEMFTP_PATH, None, None, None,
                                    False, 0, None, None, si)
    except Exception as e:
        raise RuntimeError(f"在隱藏桌面啟動 systemftp.exe 失敗：{e}")
    logging.info("已在隱藏桌面啟動 systemftp.exe")

    our_pids: set = set()
    try:
        # 等登入視窗（期間關多開提示）。隱藏桌面上 find_windows 自動列舉
        # 該桌面的視窗（因為本執行緒已 SetThreadDesktop 過去）。
        login = None
        deadline = time.time() + 120
        while time.time() < deadline:
            if not running.is_set():
                raise RuntimeError("流程已被中止")
            for ph in find_windows(MULTI_INSTANCE_CLASS, MULTI_INSTANCE_TITLE):
                ok_btn = find_child(ph, "TButton", "OK")
                if ok_btn:
                    click_button(ok_btn)
                    logging.info("已關閉多開提示視窗")
                    time.sleep(0.6)
            cands = find_windows(LOGIN_CLASS, LOGIN_TITLE_PREFIX)
            fresh = [h for h in cands if _window_pid(h) not in before]
            pick = fresh or cands
            if pick:
                login = pick[0]
                break
            time.sleep(0.5)
        if not login:
            raise RuntimeError("等不到登入視窗")
        our_pid = _window_pid(login)
        our_pids = (_systemftp_pids() - before) | {our_pid}
        logging.info("登入視窗 hwnd=%s pid=%s", login, sorted(our_pids))

        # 登入：隱藏桌面上 systemftp 是唯一前景應用，直接 SetForegroundWindow
        # + SetFocus 完全不會干擾使用者（使用者畫面在另一個桌面）。
        force_foreground(login)
        edits = sorted(
            (c for c in enum_children(login) if c[1] == "TEditExt"),
            key=lambda c: c[3][1])
        if len(edits) < 2:
            raise RuntimeError(f"登入視窗只找到 {len(edits)} 個輸入框")
        type_via_focus(edits[0][0], login, username)
        type_via_focus(edits[1][0], login, password)
        confirm = find_child(login, "TButton", "確認")
        if not confirm:
            raise RuntimeError("找不到「確認」鈕")
        click_button(confirm)
        logging.info("已送出登入")

        # 等主視窗（期間關訊息通知）
        main_hwnd = None
        deadline = time.time() + 120
        while time.time() < deadline:
            if not running.is_set():
                raise RuntimeError("流程已被中止")
            notice = find_windows(NOTICE_CLASS, pids=our_pids)
            if notice:
                btn = find_child(notice[0], "TButton", "確認")
                if btn:
                    click_button(btn)
                    logging.info("已關閉訊息通知主畫面")
                    time.sleep(0.6)
                    continue
            mains = find_windows(MAIN_CLASS, pids=our_pids)
            if mains and not notice:
                main_hwnd = mains[0]
                break
            time.sleep(0.4)
        if not main_hwnd:
            raise RuntimeError("等不到主畫面")
        logging.info("已進入主畫面")

        # 送選單命令：我的會診清單
        cmd_id = resolve_menu_command_id(main_hwnd)
        win32gui.PostMessage(main_hwnd, win32con.WM_COMMAND, cmd_id, 0)
        logging.info("已送出選單命令（id=%s）", cmd_id)

        # 等會診單
        consult = None
        deadline = time.time() + 60
        while time.time() < deadline:
            if not running.is_set():
                raise RuntimeError("流程已被中止")
            hits = find_windows(CONSULT_CLASS, pids=our_pids)
            if hits:
                consult = hits[0]
                break
            time.sleep(0.3)
        if not consult:
            raise RuntimeError("等不到會診單視窗")
        time.sleep(1.8)
        logging.info("會診單視窗已開啟，準備擷取")

        # 截圖
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        _prune_old_shots()
        img = capture_window_image(consult)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot_path = SHOTS_DIR / f"consult_{stamp}.png"
        img.save(shot_path)
        logging.info("已存檔截圖：%s", shot_path)
        # [新功能 2026-06-13] 截圖(原始畫面)存檔後才逐列點選擷取文字(fail-open)
        extracted, extracted_html, roster_texts = _extract_consult_text(
            consult, cfg, roster_label)
        return shot_path, extracted, extracted_html, roster_texts

    finally:
        cleanup_pids = our_pids or (_systemftp_pids() - before)
        try:
            close_pids(cleanup_pids)
            logging.info("已關閉本次開啟的 systemftp 實例")
        except Exception:
            logging.warning("關閉 systemftp 失敗", exc_info=True)


def _run_with_sw_hide(cfg: dict, roster_label: str = "今日會診病人") -> tuple:
    """後備模式：使用者桌面上跑，配合 SW_HIDE 隱形執行緒（可能有短暫閃爍）。
    回傳 (截圖路徑, 擷取文字)。"""
    username = cfg["username"]
    password = cfg["password"]

    before = _systemftp_pids()
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE
    try:
        subprocess.Popen([SYSTEMFTP_PATH], startupinfo=startup)
    except FileNotFoundError:
        raise RuntimeError(f"找不到住院醫囑系統程式：{SYSTEMFTP_PATH}")
    logging.info("已啟動 systemftp.exe（SW_HIDE 後備模式）")

    stealth_stop = threading.Event()
    stealth_skip: set = set()

    def _stealth() -> None:
        while not stealth_stop.is_set():
            try:
                for h in find_windows(pids=_systemftp_pids() - before,
                                      visible_only=True):
                    if h not in stealth_skip:
                        hide_window(h)
            except Exception:
                pass
            time.sleep(0.08)

    threading.Thread(target=_stealth, name="ConsultStealth", daemon=True).start()
    fg_before = win32gui.GetForegroundWindow()
    our_pids: set = set()
    borrowed = False  # 是否借用了啟動前就存在的實例(finally 收尾依此決定保留)
    borrowed_win_state: dict = {}  # [CQ-06] 借用視窗 hwnd → 原始 (placement, exstyle)

    try:
        # 等登入視窗出現；期間冒出「請勿開啟超過兩個」提示就立刻 PostMessage OK。
        # 隱形執行緒會把視窗 SW_HIDE，所以這裡用 visible_only=False 才找得到。
        login = None
        deadline = time.time() + 120
        while time.time() < deadline:
            if not running.is_set():
                raise RuntimeError("流程已被中止")
            for ph in find_windows(MULTI_INSTANCE_CLASS, MULTI_INSTANCE_TITLE,
                                   visible_only=False):
                ok_btn = find_child(ph, "TButton", "OK")
                if ok_btn:
                    click_button(ok_btn)
                    logging.info("已關閉多開提示視窗")
                    time.sleep(0.6)
            cands = find_windows(LOGIN_CLASS, LOGIN_TITLE_PREFIX,
                                 visible_only=False)
            fresh = [h for h in cands if _window_pid(h) not in before]
            pick = fresh or cands
            if pick:
                login = pick[0]
                break
            time.sleep(0.5)
        if not login:
            raise RuntimeError("等不到登入視窗（多開提示可能未正確關閉，或網路過慢）")

        our_pid = _window_pid(login)
        # [review C2 fix] 借用偵測：登入視窗的 pid 在啟動前就存在 = 我們的新實例
        # 沒有出現視窗(可能被多開限制擋下)、撿到的是「使用者自己開的」住院系統。
        # 流程仍繼續(否則本次查詢直接失敗)，但收尾不可關掉使用者的實例。
        borrowed = our_pid in before
        if borrowed:
            logging.warning(
                "[SW_HIDE 後備] 未偵測到新登入視窗，借用既有 systemftp 實例"
                "(pid=%s，可能是使用者開啟的住院系統)完成本次查詢；"
                "收尾將保留該實例不關閉。", our_pid)
        our_pids = (_systemftp_pids() - before) | {our_pid}
        logging.info("登入視窗 hwnd=%s，本次實例 pid=%s", login, sorted(our_pids))

        # 登入：TEditExt 是 Delphi 自訂控制項，必須有「真實鍵盤焦點」才收得到字，
        # 取得焦點需視窗在前景——但「前景」不需要「可見」。所以把登入視窗解除
        # 最大化、移到螢幕外後顯示再 SetForegroundWindow（使用者看不到、滑鼠不動），
        # 再 SetFocus + WM_CHAR 打字。stealth_skip 讓隱形執行緒別把它藏回去。
        stealth_skip.add(login)
        if borrowed:  # [CQ-06] 借用使用者實例 → 先存原始狀態,finally 還原(免視窗消失)
            borrowed_win_state[login] = _save_window_state(login)
        show_offscreen(login)
        if not force_foreground(login):
            logging.warning("登入視窗未取得前景，仍嘗試輸入")
        edits = sorted(
            (c for c in enum_children(login) if c[1] == "TEditExt"),
            key=lambda c: c[3][1],  # 依 rect.top 由上而下：上=代碼、下=密碼
        )
        if len(edits) < 2:
            raise RuntimeError(f"登入視窗只找到 {len(edits)} 個輸入框（預期 2）")
        type_via_focus(edits[0][0], login, username)
        type_via_focus(edits[1][0], login, password)
        confirm = find_child(login, "TButton", "確認")
        if not confirm:
            raise RuntimeError("登入視窗找不到「確認」鈕")
        click_button(confirm)  # PostMessage BM_CLICK，非阻塞
        logging.info("已送出登入")

        # 等主視窗；期間若跳「訊息通知主畫面」就按確認（全部背景訊息、視窗已隱藏）
        main_hwnd = None
        deadline = time.time() + 120
        while time.time() < deadline:
            if not running.is_set():
                raise RuntimeError("流程已被中止")
            notice = find_windows(NOTICE_CLASS, pids=our_pids,
                                  visible_only=False)
            if notice:
                btn = find_child(notice[0], "TButton", "確認")
                if btn:
                    click_button(btn)
                    logging.info("已關閉訊息通知主畫面")
                    time.sleep(0.6)
                    continue
            mains = find_windows(MAIN_CLASS, pids=our_pids, visible_only=False)
            if mains and not notice:
                main_hwnd = mains[0]
                break
            time.sleep(0.4)
        if not main_hwnd:
            raise RuntimeError("登入後等不到住院醫囑主畫面")
        logging.info("已進入主畫面")

        # 送選單命令：我的會診清單（背景 PostMessage，不點滑鼠、解析度無關）
        cmd_id = resolve_menu_command_id(main_hwnd)
        win32gui.PostMessage(main_hwnd, win32con.WM_COMMAND, cmd_id, 0)
        logging.info("已送出選單命令（我的會診清單，id=%s）", cmd_id)

        # 等「會診通知單回覆」視窗（隱形執行緒會把它 SW_HIDE，用 visible_only=False）
        consult = None
        deadline = time.time() + 60
        while time.time() < deadline:
            if not running.is_set():
                raise RuntimeError("流程已被中止")
            hits = find_windows(CONSULT_CLASS, pids=our_pids, visible_only=False)
            if hits:
                consult = hits[0]
                break
            time.sleep(0.3)
        if not consult:
            raise RuntimeError("等不到會診通知單視窗")
        # 主執行緒接手：別讓隱形執行緒藏它，解除最大化移到螢幕外顯示後 PrintWindow
        stealth_skip.add(consult)
        if borrowed:  # [CQ-06] 借用視窗 → 存原始狀態供 finally 還原
            borrowed_win_state[consult] = _save_window_state(consult)
        show_offscreen(consult)
        time.sleep(1.8)  # 讓清單內容載入完成
        logging.info("會診通知單視窗已開啟，準備擷取")

        # 截圖（PrintWindow，視窗在螢幕外也能擷取）
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        _prune_old_shots()
        img = capture_window_image(consult)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot_path = SHOTS_DIR / f"consult_{stamp}.png"
        img.save(shot_path)
        logging.info("已存檔截圖：%s", shot_path)
        # [新功能 2026-06-13] 截圖(原始畫面)存檔後才逐列點選擷取文字(fail-open)
        extracted, extracted_html, roster_texts = _extract_consult_text(
            consult, cfg, roster_label)
        return shot_path, extracted, extracted_html, roster_texts

    finally:
        # 收尾：停掉隱形執行緒、關閉我們這份 systemftp、把前景還給使用者。
        # [review C2 fix] 借用使用者既有實例時，排除啟動前就存在的 pid 不關。
        stealth_stop.set()
        cleanup_pids = _cleanup_pids_excluding_borrowed(
            our_pids or (_systemftp_pids() - before), before, borrowed)
        try:
            close_pids(cleanup_pids)
            logging.info("已關閉本次開啟的 systemftp 實例")
        except Exception:
            logging.warning("關閉 systemftp 實例失敗", exc_info=True)
        # [CQ-06] 借用使用者既有實例的視窗被 show_offscreen 移到螢幕外+改工具視窗 → 還原
        # 原始位置/樣式,否則使用者的住院系統會消失到重開為止。放在關閉本次實例後、還前景前。
        for _hwnd, _state in borrowed_win_state.items():
            _restore_window_state(_hwnd, _state)
        try:
            if fg_before and win32gui.IsWindow(fg_before):
                win32gui.SetForegroundWindow(fg_before)
        except Exception:
            pass


def _prune_old_shots() -> None:
    try:
        files = sorted(SHOTS_DIR.glob("consult_*.png"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[MAX_SHOT_FILES:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


# =============================================================================
# 寄信（Outlook COM）
# =============================================================================
def _outlook_available(timeout: float = 5.0) -> bool:
    """快速檢查本機 Outlook 是否可用：能 GetActiveObject 或 DispatchEx 成功就回 True。
    用於「多台電腦只有一台登入 Outlook」情境——沒 Outlook 的機就靜默跳過排程，
    不再啟動 systemftp、不寄信、不跳任何提示。"""
    result: dict = {}

    def w() -> None:
        import pythoncom
        pythoncom.CoInitialize()
        try:
            import win32com.client
            try:
                win32com.client.GetActiveObject("Outlook.Application")
                result["ok"] = True
                return
            except Exception:
                pass
            try:
                win32com.client.DispatchEx("Outlook.Application")
                result["ok"] = True
            except Exception:
                result["ok"] = False
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    t = threading.Thread(target=w, name="OutlookAvailCheck", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False
    return bool(result.get("ok"))


def _connect_outlook():
    """連到本機 Outlook：先試 GetActiveObject（接已開啟的最穩），失敗再試
    DispatchEx（強制 CoCreateInstance）。各試三輪、每輪間 sleep 2 秒——
    對應 com_error '伺服器執行失敗' / '操作無法使用' 等偶發狀況。"""
    import win32com.client
    last_err = None
    for attempt in range(3):
        try:
            return win32com.client.GetActiveObject("Outlook.Application")
        except Exception as e:
            last_err = e
        try:
            return win32com.client.DispatchEx("Outlook.Application")
        except Exception as e:
            last_err = e
        if attempt < 2:
            time.sleep(2)
    raise RuntimeError(
        f"無法連到 Outlook：{last_err}\n"
        "請手動開啟 Outlook 並確認它可正常收發信，然後再試一次。")


def _pick_outlook_account(outlook, sender_account: str):
    """從 outlook.Session.Accounts 找出 SmtpAddress 等於 sender_account 的帳號。
    找不到就回 None；呼叫端決定回退到預設帳號或 raise。比對大小寫無關。"""
    if not sender_account:
        return None
    target = sender_account.strip().lower()
    try:
        accounts = outlook.Session.Accounts
        for i in range(1, accounts.Count + 1):  # Outlook COM accounts 是 1-based
            acc = accounts.Item(i)
            try:
                smtp = (acc.SmtpAddress or "").strip().lower()
            except Exception:
                smtp = ""
            if smtp == target:
                return acc
    except Exception:
        logging.warning("列舉 Outlook accounts 失敗", exc_info=True)
    return None


def _outlook_send_worker(image_path, subject, body, recipients, result,
                          sender_account: str = "", html_body: str = "") -> None:
    """實際的 Outlook COM 寄信動作，在獨立執行緒執行（自己 CoInitialize）。

    sender_account：指定要用哪個 Outlook 帳號寄（SMTP 地址）。找不到時退回
    Outlook 預設帳號，並在 log 留 warning。
    html_body：有值時用 HTMLBody（美化版排版）；空字串則用純文字 Body。"""
    import pythoncom
    pythoncom.CoInitialize()
    try:
        outlook = _connect_outlook()
        mail = outlook.CreateItem(0)  # olMailItem
        mail.To = "; ".join(recipients)
        mail.Subject = subject
        if html_body:
            mail.HTMLBody = html_body
        else:
            mail.Body = body
        if image_path and Path(image_path).exists():
            mail.Attachments.Add(str(Path(image_path).resolve()))
        # 強制寄件人帳號（SendUsingAccount）—— Outlook 必須已設定此帳號
        if sender_account:
            acc = _pick_outlook_account(outlook, sender_account)
            if acc is not None:
                # SendUsingAccount 是 property，要用底層 _oleobj_ 設定（直接賦值在某些
                # Outlook 版本會失敗 "Member not found"），下式對所有版本都有效。
                try:
                    mail._oleobj_.Invoke(*(0xF01C, 0, 8, 0, acc))  # PR_SENT_REPRESENTING
                except Exception:
                    # 退回直接賦值
                    try:
                        mail.SendUsingAccount = acc
                    except Exception:
                        logging.warning(
                            "無法套用 SendUsingAccount（將以 Outlook 預設帳號寄）",
                            exc_info=True)
            else:
                logging.warning(
                    "Outlook 找不到帳號 %r，將以預設帳號寄信。"
                    "請先在 Outlook 加入此帳號或修改 sender_account 設定。",
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


def send_via_outlook(image_path: Path, subject: str, body: str,
                     recipients: list, timeout: float = 120.0,
                     sender_account: str = "", html_body: str = "") -> None:
    """用本機 Outlook 寄出。COM 動作在獨立執行緒執行並設逾時——若 Outlook 跳出
    安全提示或忙線卡住，最多等 timeout 秒就放棄，不會無限阻塞整個排程
    （先前第二次寄信卡死、整個任務不結束就是這個原因）。逾時或失敗會 raise。

    sender_account：強制用此 SMTP 地址對應的 Outlook 帳號寄信。空字串/None 則
    用 Outlook 預設帳號。

    【註】2026-05-18 改用 SMTP 為主（見 send_via_smtp）。本函式保留作為備援，
    僅 mail_method="outlook" 時才會走到。"""
    if not recipients:
        raise RuntimeError("沒有設定收件人")
    result: dict = {}
    worker = threading.Thread(
        target=_outlook_send_worker,
        args=(image_path, subject, body, recipients, result, sender_account,
              html_body),
        name="OutlookSend", daemon=True,
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise RuntimeError(
            f"Outlook 寄信逾時（超過 {int(timeout)} 秒）——"
            "可能是 Outlook 跳出「允許程式寄信」安全提示或忙線，請檢查 Outlook")
    if result.get("error"):
        raise result["error"]
    if not result.get("ok"):
        raise RuntimeError("Outlook 寄信未完成（原因不明）")
    sender_note = f"（寄件人 {sender_account}）" if sender_account else ""
    logging.info("已透過 Outlook 寄出給：%s%s", ", ".join(recipients), sender_note)


def send_via_smtp(image_path: Path, subject: str, body: str,
                  recipients: list, timeout: float = 60.0,
                  html_body: str = "") -> None:
    """用 SMTP 直接寄（Gmail / smtp.gmail.com）。

    為何不用 Outlook：admin 行程的 Outlook COM 會起一個 admin Outlook 實例，
    用 administrator 的 MAPI profile（通常沒設定任何郵件帳號），mail.Send()
    成功但信永遠卡在隱形 Outbox 寄不出。SMTP 跳過整個 UAC + Outlook profile
    地獄，任何權限都能寄。

    使用 settings/smtp_credentials.json 的 cmuhdermatology@gmail.com + App
    Password。檔案不存在會自動建立範本，password 為空會 raise
    SmtpNotConfiguredError。"""
    from cmuh_common.smtp_mail import send_mail
    send_mail(recipients=recipients, subject=subject, body=body,
              attachment_path=image_path, timeout=timeout,
              html_body=html_body or None)


def _kill_systemftp(before_pids=None) -> None:
    """[W6 2026-07-03] 重試前清理『本次任務期間新出現的』systemftp 殘留 —— 只殺
    `目前 systemftp PID − before_pids`(before_pids 為 _do_full_job 開始前的快照)。

    改法理由:絕不再 taskkill /IM systemftp.exe 全機掃殺(會殺掉使用者手動開的住院
    系統、或另一台自動化實例)。使用者『任務開始前就已存在』的實例都在 before_pids
    裡,一律不動;卡死超時而 finally 來不及關的孤兒(在本任務期間才出現)則會被清掉,
    避免下一輪 attempt 撞到 wedged 實例。於清理當下即時計算(不靠 worker 事後回填),
    避免 worker 超時仍存活/事後回填造成的競態。

    殘留邊界:使用者若『恰好在本任務進行中』才手動開 systemftp,會被納入(窄窗,與既有
    finally 清理同語意)。before_pids=None 時保守不動作(fail-open,不誤殺)。
    失敗時靜默(可能已結束、沒 process 可殺)。"""
    if before_pids is None:
        logging.debug("[cleanup] 未提供 before 快照 → 略過清理(不做全機 taskkill)")
        return
    try:
        orphans = sorted(_systemftp_pids() - set(before_pids))
    except Exception:
        logging.debug("[cleanup] 計算孤兒 PID 失敗", exc_info=True)
        return
    if not orphans:
        logging.debug("[cleanup] 本任務期間無新增 systemftp,無需清理")
        return
    args = ["taskkill", "/F"]
    for p in orphans:
        args += ["/PID", str(p)]
    try:
        subprocess.run(args, capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        logging.info("[cleanup] 已清本任務期間新增的 systemftp PID: %s", orphans)
    except Exception:
        logging.debug("taskkill 本任務 PID 失敗（可能已結束）", exc_info=True)


def _cleanup_orphan_systemftp() -> None:
    """[CQ-05] 啟動清掃:上次硬退(self-watchdog/托盤退出/更新重啟)遺留在隱藏桌面的
    systemftp —— 已登入 HIS、隱形、佔記憶體,且下次任務的 before_pids 快照會把它圈進
    「不可殺」而永久存活累積(≥2 個時配合『請勿開啟超過兩個』限制會讓後續登入更不穩)。

    判定:某 systemftp PID 在【使用者桌面】沒有任何可見 top-level 視窗 → 視為前世孤兒
    (隱藏桌面殘留)→ 關閉。保守起見只殺『使用者桌面無可見視窗』者,絕不動使用者正常開啟的
    住院系統(它在使用者桌面必有可見視窗,含最小化)。只在啟動時(持有單例 mutex 後)呼叫一次。
    """
    try:
        all_pids = _systemftp_pids()
        if not all_pids:
            return
        # [CQ-05 codex] 多使用者/RDS/快速使用者切換:_systemftp_pids() 是全機掃描,但
        # EnumWindows 只看得到「本 session 桌面」的視窗 → 其他使用者作用中的 HIS(不同
        # session)會被誤判無視窗=孤兒而被殺(本程式可能提權)。故先把候選 PID 過濾到
        # 本登入 session,取不到本 session id 時保守整個跳過。
        my_sid = _pid_session(os.getpid())
        if my_sid is None:
            logging.debug("[CQ-05] 取不到本 session id → 保守跳過孤兒清掃")
            return
        all_pids = {p for p in all_pids if _pid_session(p) == my_sid}
        if not all_pids:
            return
        # 本主執行緒在使用者互動桌面 → EnumWindows 只列舉本 session 桌面的視窗;
        # 隱藏桌面(HIDDEN_DESKTOP_NAME)上的孤兒在此看不到任何視窗 → 被判為孤兒。
        visible = find_windows(pids=all_pids, visible_only=True)
        on_user_desktop = {_window_pid(h) for h in visible}
        orphans = all_pids - on_user_desktop
        if orphans:
            logging.warning(
                "[CQ-05] 清掃前世遺留的隱形 systemftp 孤兒(使用者桌面無視窗): %s",
                sorted(orphans))
            close_pids(orphans)
    except Exception:
        logging.warning("[CQ-05] systemftp 孤兒清掃失敗(略過,不影響啟動)", exc_info=True)


def _do_full_job(trigger_label: str, override_recipients=None) -> None:
    """完整一次任務：跑流程 → 寄信。供排程／手動共用，整體互斥。

    多機共存策略：先檢查本機 Outlook 是否可用，不可用就直接靜默跳過——
    省得多台電腦同時跑 systemftp 又同時嘗試寄信。全程不跳任何視窗提示
    （成功與失敗都只記 log，不打擾使用者）。

    重試策略：
      - 任一步驟失敗（systemftp 啟動失敗、登入失敗、截圖失敗、寄信失敗）→
        taskkill /F /IM systemftp.exe 清環境，sleep 3 秒，重試整個流程
      - 預設最多 3 次，由 cfg.retry_count 控制
      - 三次都掛才放棄並記 log（不再彈視窗）

    收件人路由：
      - override_recipients（IMAP 觸發傳入：實際觸發信的寄件人 email）→ 用它
      - trigger_label == "email" 且無 override → 用 email_trigger_recipients
        （fallback，例如手動觸發或寄件人解析失敗）
      - 其他（排程／手動）→ 用 recipients（一般四人名單）"""
    if not _flow_lock.acquire(blocking=False):
        logging.info("已有一個會診查詢任務進行中，本次（%s）略過", trigger_label)
        return
    import pythoncom
    pythoncom.CoInitialize()
    try:
        cfg = load_config()
        mail_method = str(cfg.get("mail_method", "smtp")).lower()
        # SMTP 模式：檢查 password 是否已填，沒填則靜默跳過（多機部署：只有有
        # 設 SMTP 的那台才寄）
        if mail_method == "smtp":
            from cmuh_common.smtp_mail import is_configured as _smtp_ready
            if not _smtp_ready():
                logging.info("SMTP 尚未設定（settings/smtp_credentials.json 缺 "
                              "password），本次（%s）整個流程靜默跳過", trigger_label)
                return
        elif mail_method == "outlook":
            if not _outlook_available():
                logging.info("本機無可用 Outlook，本次（%s）整個流程靜默跳過",
                              trigger_label)
                return
        now = datetime.now()
        # [2026-06-25] 輪詢 poll:00:00-06:00 休息時段 → 直接不開 systemftp、不寄
        # (過夜新增的會診由休息結束後第一輪 poll 的「新病歷號」比對一次補寄)。
        if trigger_label == "poll" and _in_quiet_hours(now, cfg):
            logging.info("[poll] 休息時段(%02d:00-%02d:00),本次不輪詢/不寄信",
                         int(cfg.get("quiet_start_hour", 0)),
                         int(cfg.get("quiet_end_hour", 6)))
            return
        date_str = f"{now.year}/{now.month}/{now.day}"
        time_str = (trigger_label.replace(":", "")
                    if trigger_label and ":" in trigger_label
                    else now.strftime("%H%M"))

        # 收件人路由：
        #   1. override_recipients 有值（IMAP 觸發傳入觸發信寄件人）→ 用它，
        #      標籤 email_trigger_sender
        #   2. trigger_label == "email" 但無 override（解析失敗或手動觸發）→
        #      退回 email_trigger_recipients
        #   3. 其他（排程／手動）→ 一般 recipients
        if override_recipients:
            recipients = list(override_recipients)
            recipients_label = "email_trigger_sender"
        elif trigger_label == "email":
            recipients = cfg.get("email_trigger_recipients") or cfg["recipients"]
            recipients_label = "email_trigger_recipients(fallback)"
        else:
            recipients = cfg["recipients"]
            recipients_label = "recipients"
        sender = cfg.get("sender_account", "") or ""
        retry_count = _normalize_retry_count(cfg.get("retry_count", 3))

        # [CQ-04] 執行中設定被改成空帳密 → 不跑 HIS 自動化(避免以空帳密每輪登入失敗、
        # 甚至觸發 portal 帳號鎖定;啟動守衛只擋開機那次,這裡擋執行期改動)。
        if not _has_his_credentials(cfg):
            logging.error(
                "[會診] 尚未設定 HIS 帳號/密碼,本次(%s)不執行流程;請至設定填寫。",
                trigger_label)
            return
        # [CQ-07] 收件人被清空 → 不跑完整 HIS 自動化(免每輪白開 systemftp、登入、擷取 3 次
        # 才在寄信步驟失敗),直接記 error 返回。
        if not recipients:
            logging.error(
                "[會診] 收件人清單為空(%s),本次(%s)不執行流程/不寄信;請至設定填寫收件人。",
                recipients_label, trigger_label)
            return

        subject = cfg["subject_template"].format(date=date_str, time=time_str)
        body = cfg["body_template"].format(date=date_str, time=time_str)

        last_err = None  # 最後一次的失敗例外，用於三次都失敗的 log
        # [W6 2026-07-03] 任務開始前的 systemftp 快照:重試清理只殺「這之後才出現」的
        # 實例(使用者既有的住院系統都在這份快照裡,永不誤殺)。
        job_before_pids = _systemftp_pids()
        # [v17 2026-05-25] Exponential backoff — 原本 retry 間固定 sleep 3s，
        # 三次重試集中在 5-6 分鐘窗口內，醫院 systemftp 後端 transient 慢時
        # 三次都撞在同個 server 卡死期。今天 16:54 IMAP 觸發 → 三次「等不到
        # 登入視窗」全部失敗 (6 分鐘) → 17:00 排程被擋 → user 沒收信。
        # 改 [3, 30, 90] 秒：給 server 越來越長的恢復時間。
        # 第 3 次撞上恢復視窗的機率變大。三次總時長 6→8 分鐘 (僅多 2 分鐘)。
        BACKOFF_SCHEDULE = [3, 30, 90]
        for attempt in range(1, retry_count + 1):
            try:
                logging.info("會診查詢任務 第 %d/%d 次嘗試（trigger=%s, 收件人組=%s, mail=%s）",
                             attempt, retry_count, trigger_label,
                             recipients_label, mail_method)
                shot, extracted_text, extracted_html, roster_texts = run_consult_flow(
                    trigger_label)
                # [2026-06-25] 輪詢 poll:只在「出現新病歷號」時才寄;否則靜默結束
                # (不寄、不更新基準 → 下一輪仍會再比對)。email/手動觸發不受此限,照常無條件寄。
                _poll_extract_note = ""
                if trigger_label == "poll":
                    if roster_texts is None:
                        # [CQ-01] 清單解析失敗/停用 → 無法判斷有沒有新會診。fail-open 照常
                        # 寄信(信首註明以截圖為準),且【不更新基準】——避免把「解析失敗=空
                        # 集合」當成基準,下輪擷取恢復後所有未回覆會診都變「新」→ 對團隊重複
                        # 寄整份清單。此路徑會落到下方正常寄信(2395 因 roster is None 而不更新基準)。
                        logging.warning(
                            "[poll] 會診清單解析失敗/停用 → fail-open 照常寄信(以截圖為準)")
                        _poll_extract_note = (
                            "⚠ 會診清單自動解析失敗,本信以截圖為準,請人工核對是否有新會診。")
                    else:
                        _poll_sig = _consult_signature_from_roster(roster_texts)
                        if not _baseline_initialized():
                            # [2026-06-25 user] 第一次啟動還沒建過基準 → 開機這輪只建基準、不寄,
                            # 避免每次重啟收一封「全部未回覆清單」的信。之後才比對新病歷號。
                            _save_notified(_poll_sig)
                            logging.info("[poll] 首次建立會診基準(%d 筆),本輪不寄信",
                                         len(_poll_sig))
                            return
                        _new = _poll_sig - _load_notified()
                        if not _new:
                            logging.info("[poll] 目前 %d 筆會診都已通知過,無新會診 → 不寄信",
                                         len(_poll_sig))
                            return
                        logging.info("[poll] 偵測到 %d 筆新會診 → 寄出目前全部未回覆清單",
                                     len(_new))
                # [2026-06-17] 今日打卡狀態:排程(12:40/17:10)與手動觸發都查/附;
                # 只有 email(皮膚科會診觸發)省略,連打卡 portal 都不登入,直接查會診。
                # [新功能 2026-06-15] 查詢本身完全 fail-open:查不到只回空字串。
                if _is_email_trigger(trigger_label):
                    punch_text, punch_html = "", ""
                else:
                    punch_text, punch_html = _build_punch_status_sections(cfg)
                # [新功能 2026-06-13] 擷取到的會診文字附在信件內文(截圖仍為主)
                text_parts = []
                if _poll_extract_note:                     # [CQ-01] 解析失敗 fail-open 註記置信首
                    text_parts.append(_poll_extract_note)
                text_parts.append(body)
                if punch_text:
                    text_parts.append(punch_text)
                if extracted_text:
                    text_parts.append(extracted_text)
                final_body = "\n\n".join(text_parts)
                # [美化 2026-06-15] HTML 版排版(multipart/alternative;純文字為
                # fallback)。打卡狀態置於會診內容之前。截圖附件照常夾帶。
                final_html = _build_consult_email_html(
                    date_str, time_str,
                    (_poll_extract_note + "\n" + body) if _poll_extract_note else body,
                    punch_html + extracted_html)
                if mail_method == "smtp":
                    send_via_smtp(shot, subject, final_body, recipients,
                                  html_body=final_html)
                else:
                    send_via_outlook(shot, subject, final_body, recipients,
                                      sender_account=sender,
                                      html_body=final_html)
                # [2026-06-25] 寄出成功 → 更新「已通知病歷號」基準,下一輪 poll 不再重複寄同一批。
                # 【只在寄給一般收件人時更新】(poll / 手動):email 觸發是寄給「觸發醫師本人」、
                # 不是團隊一般名單,若也更新基準會害下一輪 poll 看不到這筆新會診而漏寄給團隊
                # (Codex 指出)。override_recipients 只在 email 觸發時有值 → 用 label 判斷即可。
                # [CQ-03] 只在「roster 擷取成功(非 None)」時才更新基準:手動觸發但擷取失敗
                # 時,若用空集合覆寫基準,下一輪 poll 擷取恢復 → 全部未回覆會診變「新」→ 對團隊
                # 重複寄整份清單。roster is None(解析失敗/停用)一律不動基準。
                if trigger_label != "email" and roster_texts is not None:
                    try:
                        _save_notified(_consult_signature_from_roster(roster_texts))
                    except Exception:
                        logging.debug("更新 consult_notified 失敗", exc_info=True)
                logging.info("會診查詢任務成功（第 %d 次嘗試）", attempt)
                return  # 成功就跳出
            except Exception as e:
                last_err = e
                logging.error("會診查詢任務第 %d/%d 次失敗：%s",
                              attempt, retry_count, e, exc_info=True)
                if attempt < retry_count:
                    # exponential backoff (3s, 30s, 90s)；attempt 從 1 開始
                    backoff = (BACKOFF_SCHEDULE[attempt - 1]
                               if attempt - 1 < len(BACKOFF_SCHEDULE)
                               else BACKOFF_SCHEDULE[-1])
                    logging.info(
                        "殺 systemftp.exe 後重試（sleep %d 秒，exponential backoff）",
                        backoff)
                    _kill_systemftp(job_before_pids)
                    time.sleep(backoff)
                else:
                    logging.error("會診查詢任務已重試 %d 次仍失敗，放棄。最後錯誤：%s",
                                  retry_count, last_err)
                    # [stability] email 觸發整個失敗(沒寄出結果) → 把觸發者從去重
                    # 名單移除，讓使用者可立即重發觸發信重試，不必等 5 分鐘去重窗
                    # 過期才生效。
                    if trigger_label == "email" and override_recipients:
                        _release_trigger_dedup(override_recipients)
                        logging.info(
                            "[dedup] 已釋放失敗的 email 觸發者，可立即重試：%s",
                            ", ".join(str(x) for x in override_recipients))
                        # [新功能 2026-06-11] 回信告知觸發者失敗(原本只寫 log，
                        # 觸發醫師不知道沒成功、苦等不到結果)
                        _send_failure_notice_async(override_recipients,
                                                   str(last_err))
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
        _flow_lock.release()


def _notify(title: str, msg: str) -> None:
    try:
        from winotify import Notification
        Notification(app_id="CMUH.SkinDept.ConsultQuery",
                     title=title, msg=msg).show()
    except Exception:
        logging.debug("winotify 通知失敗（不影響流程）", exc_info=True)


# [v17 2026-05-25] Pending re-trigger queue — 排程被 task_gate 擋掉時記下來，
# 當前 job 結束 release lease 後自動補跑。
# 防今天 17:00 排程被 16:54 IMAP retry 擋掉就「掉地上」 user 沒收信。
# 同一個 trigger_label 只記一個 (defer dict by label)，避免無限堆積。
_pending_retriggers: dict = {}  # trigger_label -> override_recipients
_pending_retriggers_lock = threading.Lock()
_pending_retrigger_drain_running = False
_RETRIGGER_DELAY_SEC = 5.0  # release 後等 5s 讓 systemftp/網路喘息再重觸發


def _merge_retrigger_recipients(existing, incoming):
    """Merge same-label email recipients without losing earlier trigger senders."""
    if incoming is None:
        return existing
    if existing is None:
        return incoming
    merged = []
    seen = set()
    for addr in list(existing) + list(incoming):
        key = str(addr).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(addr)
    return merged


def _enqueue_pending_retrigger(trigger_label: str, override_recipients) -> None:
    """記下一筆 pending re-trigger；同 label 合併 email 收件人，不無限堆積。"""
    with _pending_retriggers_lock:
        existing = _pending_retriggers.get(trigger_label)
        _pending_retriggers[trigger_label] = _merge_retrigger_recipients(
            existing, override_recipients)


def _drain_pending_retriggers() -> None:
    """release 後跑這個 — 把擋下的觸發補上。等 _RETRIGGER_DELAY_SEC 後執行。
    在背景 thread 跑，避免拖長 release 路徑。"""
    global _pending_retrigger_drain_running
    with _pending_retriggers_lock:
        if _pending_retrigger_drain_running or not _pending_retriggers:
            return
        _pending_retrigger_drain_running = True

    def _delayed():
        global _pending_retrigger_drain_running
        try:
            if not _sleep_while_running(_RETRIGGER_DELAY_SEC):
                logging.info("[re-trigger] 程式正在關閉，略過 pending 補跑")
                with _pending_retriggers_lock:
                    _pending_retriggers.clear()
                return
            with _pending_retriggers_lock:
                pending = dict(_pending_retriggers)
                _pending_retriggers.clear()
            for label, override in pending.items():
                logging.info(
                    "[re-trigger] 補跑被 task_gate 擋下的觸發：%s", label)
                try:
                    trigger_job_async(label, override_recipients=override)
                except Exception:
                    logging.exception("[re-trigger] 補跑 %s 失敗", label)
        finally:
            with _pending_retriggers_lock:
                _pending_retrigger_drain_running = False
                has_pending = bool(_pending_retriggers)
            if has_pending and running.is_set():
                _drain_pending_retriggers()

    try:
        threading.Thread(target=_delayed,
                         name="ConsultRetrigger", daemon=True).start()
    except Exception:
        with _pending_retriggers_lock:
            _pending_retrigger_drain_running = False
        logging.exception("[re-trigger] 啟動補跑 thread 失敗")


def trigger_job_async(trigger_label: str, override_recipients=None) -> None:
    key = "consult"
    lease = _consult_job_gate.acquire_lease(key)
    if lease is None:
        age = _consult_job_gate.active_age_sec(key)
        logging.warning(
            "Consult query job is still running (age=%ss), skip trigger: %s "
            "(will re-trigger after current job finishes)",
            "?" if age is None else f"{age:.0f}",
            trigger_label,
        )
        # [v17] 排隊：當前 job release 後補跑這個 trigger
        _enqueue_pending_retrigger(trigger_label, override_recipients)
        return

    def _worker():
        try:
            _do_full_job(trigger_label, override_recipients=override_recipients)
        finally:
            _consult_job_gate.release(key, lease)
            # [v17] release 後檢查有沒有 pending re-trigger 需要補跑
            _drain_pending_retriggers()

    threading.Thread(target=_worker, name="ConsultJob", daemon=True).start()


# =============================================================================
# 排程器
# =============================================================================
def _rebuild_schedule() -> None:
    schedule.clear()
    cfg = load_config()
    if not cfg.get("enabled", True):
        logging.info("排程目前為停用狀態")
        return
    # [CQ-01] 輪詢靠擷取病人清單比對「新病歷號」偵測新會診;擷取關閉時無法判斷新舊,
    # 若照建 poll job 會每輪 fail-open 狂寄。故此情況【不建立輪詢】並大聲警告。
    if not cfg.get("extract_text_enabled", True):
        logging.error(
            "[排程] 『擷取會診文字』已關閉,但排程為輪詢模式——輪詢需擷取病人清單才能偵測"
            "新會診,已【停用輪詢以免每輪重複寄信】。請於設定開啟『擷取會診文字』後再啟用輪詢。")
        return
    # [2026-06-25] 改為「每 N 分鐘輪詢會診清單」取代固定 12:40/17:10 排程。是否真的寄信由
    # _do_full_job 的 poll 邏輯決定:只有出現「新病歷號」才寄、且 00:00-06:00 休息不輪詢/不寄。
    try:
        interval = int(cfg.get("poll_interval_minutes", 15))
    except (TypeError, ValueError):
        interval = 15
    interval = max(5, min(120, interval))   # 夾在 5-120 分鐘,避免太密集打爆 systemftp/院方系統
    # [2026-06-25 user] 加 ±1 分鐘隨機抖動(N-1 ~ N+1 分),避免固定節拍打院方系統(同 reg64 45-75
    # 秒隨機的理由);schedule 的 .to() 會在每次跑完後重新隨機下一次間隔。下限不低於 5 分。
    lo = max(5, interval - 1)
    hi = interval + 1
    schedule.every(lo).to(hi).minutes.do(trigger_job_async, trigger_label="poll")
    logging.info(
        "已排程每 %d 分鐘(±1 隨機)輪詢會診清單(有新會診才寄信;%02d:00-%02d:00 休息)",
        interval, int(cfg.get("quiet_start_hour", 0)), int(cfg.get("quiet_end_hour", 6)))


def _empty_imap_result(err: str) -> dict:
    return {"triggered": False, "scanned": 0, "matched": 0,
            "matched_senders": [], "samples": [], "error": err}


# [stability r4] 上一條被放生的 IMAPCheck thread 引用：force_close 對「socket 尚未建立」
# 的卡死階段(DNS getaddrinfo / TCP connect / TLS handshake)無效，逾時放生的 thread 可能
# 仍卡著。記住它，下一輪若仍 alive 就跳過不再疊加新 thread，避免長期半死網路下緩慢累積。
# 只由單一 scheduler thread 讀寫，無並發、不需鎖。
_last_imap_thread = None


def _run_imap_check_with_timeout(kw: str, timeout: float = 60.0,
                                 max_age_sec: float = 0.0) -> dict:
    """跑 check_trigger 在 daemon thread；超過 timeout 就 force-close socket 並回 error。

    為什麼要這層保護：imaplib 內部 socket recv 在某些情境（網路斷、Gmail TLS
    死握、Windows hibernate 喚醒後 socket 半死）不吃 socket timeout，會永遠
    blocking。一旦 scheduler 卡在 _imap_check 整個 thread 就凍住，外層 except
    抓不到（因為沒拋例外，只是在等）。

    這個 wrapper：
      1. 在 daemon thread 跑 check_trigger
      2. main thread 用 join(timeout) 等
      3. 超時就 force_close_active() 砍 socket → 被卡的 recv 立刻拋 OSError
         → daemon thread finally 收尾
      4. 不管 thread 有沒有收尾完，這個 call 都回 error result 給 main thread
         繼續輪詢（worst case daemon thread leak 一次，但會自殺）
    """
    from cmuh_common.imap_reader import check_trigger, force_close_active
    global _last_imap_thread

    # [stability r4] 上一條放生的 IMAPCheck 仍卡著 → 本輪不疊加新 thread，直接回 error
    # (走既有 consecutive_imap_errors / cooldown 路徑)，等它自己(DNS/connect 逾時)結束。
    prev = _last_imap_thread
    if prev is not None and prev.is_alive():
        logging.warning(
            "[watchdog] 上一條 IMAPCheck thread 仍未結束，本輪跳過以免累積 daemon thread")
        return _empty_imap_result(
            "previous IMAP check still running (skipped to avoid thread pile-up)")

    box: dict = {}

    def _worker():
        try:
            box["r"] = check_trigger(kw, max_age_sec=max_age_sec or None)
        except Exception as e:  # noqa: BLE001
            box["r"] = _empty_imap_result(f"imap thread exception: {e!r}")

    t = threading.Thread(target=_worker, name="IMAPCheck", daemon=True)
    _last_imap_thread = t
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        logging.warning(
            "[watchdog] IMAP check 超過 %.0fs 無回應，強制砍 socket", timeout)
        force_close_active()
        # 再給 2 秒讓 daemon thread 收尾（finally 會跑）
        t.join(timeout=2.0)
        if t.is_alive():
            logging.warning(
                "[watchdog] daemon thread 仍未結束，已放棄；保留引用，下一輪不疊加新 thread")
            # [opt B2] worker 被放生、永遠走不到 finally 的 _clear_active → 主動把這條已關閉
            # 的連線從 _active_conns 移除，避免死連線物件被 set 永久強引用無法 GC。
            # single-flight 保證此刻 set 內只有這條(不會誤清新連線)。
            force_close_active(clear=True)
        # 維持 _last_imap_thread = t（仍 alive），下一輪會看到並跳過直到它自己結束
        return _empty_imap_result(
            f"IMAP check timeout > {timeout:.0f}s (socket 已強制關閉)")
    # 正常結束(thread 已 not alive) → 清掉引用，不擋下一輪正常 poll
    _last_imap_thread = None
    return box.get("r", _empty_imap_result("imap result missing"))


# [穩定性] scheduler liveness — 給 self-watchdog thread 用
_SCHEDULER_LIVENESS = {"last_tick": 0.0, "last_imap_success": 0.0}

# [2026-05-22 v34] scheduler thread 引用 — self-watchdog 用 is_alive() 直接偵測
# thread 死亡 (比 last_tick 訊號更可靠：thread 真死了 last_tick 永遠不會更新)
_scheduler_thread_ref = None

# [B] 觸發信去重 — 同一寄件人 + 最近 5 分鐘 → 視為重複，跳過
# 防 mark-read 失敗導致重複處理
_TRIGGER_DEDUP_WINDOW_SEC = 300
_recent_trigger_senders: dict = {}  # sender_email → last_processed_ts
# [stability] 保護 _recent_trigger_senders：scheduler thread(去重判斷)與 job
# thread(失敗時釋放觸發者)會併發存取此 dict，無鎖時 job thread 的 pop 可能撞上
# scheduler thread 的 .items() 迭代 → RuntimeError(dict changed size)。
_trigger_dedup_lock = threading.Lock()


# [opt 2026-06-11 會診1] 去重狀態輕量持久化：原本純記憶體，process 重啟(watchdog 重啟/
# _hard_exit/自動更新重啟)即清空 → 若觸發信「標已讀失敗」(信仍 UNSEEN)，重啟後同一封信
# 會被重新命中、重複截圖+寄信。把 {sender: ts} 存到小 json，啟動時載回未過期項。
# 所有檔案 IO 都 try/except 降級回純記憶體行為，絕不讓持久化失敗影響主流程。
_TRIGGER_DEDUP_STATE_FILE = SETTINGS_DIR / "consult_trigger_dedup.json"


def _persist_trigger_dedup_locked() -> None:
    """(呼叫端須持 _trigger_dedup_lock) 寫盤；檔案僅數筆 sender→ts，失敗只 debug。"""
    try:
        atomic_write_json(str(_TRIGGER_DEDUP_STATE_FILE),
                          dict(_recent_trigger_senders))
    except Exception:
        logging.debug("[dedup] 去重狀態寫盤失敗(降級純記憶體)", exc_info=True)


def load_trigger_dedup_state() -> None:
    """啟動時載回未過期的去重狀態(跨重啟防重複觸發)。壞檔/缺檔靜默忽略。"""
    try:
        raw = safe_load_json(str(_TRIGGER_DEDUP_STATE_FILE), default={})
        if not isinstance(raw, dict):
            return
        now = time.time()
        loaded = 0
        with _trigger_dedup_lock:
            for k, v in raw.items():
                try:
                    ts = float(v)
                except (TypeError, ValueError):
                    continue
                # 只載「未過期」項；ts 在未來(時鐘倒退)也丟棄。
                # 注意用 <=：寫盤與重載可能落在同一時鐘 tick(now-ts==0)，不可誤丟。
                if 0 <= now - ts < _TRIGGER_DEDUP_WINDOW_SEC:
                    _recent_trigger_senders[str(k).strip().lower()] = ts
                    loaded += 1
        if loaded:
            logging.info("[dedup] 已載回 %d 筆未過期去重狀態(跨重啟防重複觸發)",
                         loaded)
    except Exception:
        logging.debug("[dedup] 去重狀態載入失敗(忽略)", exc_info=True)


def _trigger_is_duplicate(sender: str) -> bool:
    """同 sender 5 分鐘內處理過 → True (應跳過)。"""
    now = time.time()
    with _trigger_dedup_lock:
        last = _recent_trigger_senders.get(sender.lower(), 0.0)
        if now - last < _TRIGGER_DEDUP_WINDOW_SEC:
            return True
        _recent_trigger_senders[sender.lower()] = now
        # 順便清過期項
        cutoff = now - _TRIGGER_DEDUP_WINDOW_SEC * 4
        expired = [k for k, v in _recent_trigger_senders.items() if v < cutoff]
        for k in expired:
            _recent_trigger_senders.pop(k, None)
        _persist_trigger_dedup_locked()  # [會診1] 同步寫盤(跨重啟生效)
        return False


def _release_trigger_dedup(senders) -> None:
    """把指定觸發者從去重名單移除，讓其可立即重發觸發信。用於 job 整個失敗
    (沒寄出結果)時：否則觸發者在 5 分鐘去重窗內重發都會被當重複而吞掉。"""
    if not senders:
        return
    with _trigger_dedup_lock:
        for s in senders:
            try:
                _recent_trigger_senders.pop(str(s).strip().lower(), None)
            except Exception:
                pass
        _persist_trigger_dedup_locked()  # [會診1] 釋放也同步寫盤(保持檔案一致)


# [opt 2026-06-11 會診3] 去重吞掉觸發信時回「告知信」：原本被去重的觸發信只寫 log 就
# 靜默忽略 → 醫師重發查詢卻苦等不到結果、也不知道被忽略了。改為回一封簡短告知信。
# 同一 sender 每去重窗最多通知一次(避免連寄多封觸發信被通知轟炸)；寄送走獨立 daemon
# thread(不卡 scheduler)，失敗只記 log。
_dedup_notice_sent: dict = {}  # sender → 上次通知 ts(受 _trigger_dedup_lock 保護)


def _send_dedup_notice_async(senders) -> None:
    now = time.time()
    to_notify = []
    with _trigger_dedup_lock:
        for s in senders:
            k = str(s).strip().lower()
            if now - _dedup_notice_sent.get(k, 0.0) >= _TRIGGER_DEDUP_WINDOW_SEC:
                _dedup_notice_sent[k] = now
                to_notify.append(str(s))
        cutoff = now - _TRIGGER_DEDUP_WINDOW_SEC * 4
        for k in [k for k, v in _dedup_notice_sent.items() if v < cutoff]:
            _dedup_notice_sent.pop(k, None)
    if not to_notify:
        return

    def _worker():
        try:
            from cmuh_common.smtp_mail import send_mail
            mins = _TRIGGER_DEDUP_WINDOW_SEC // 60
            send_mail(
                recipients=to_notify,
                subject="會診查詢：剛已處理（重複觸發已略過）",
                body=(f"您在 {mins} 分鐘內的上一封觸發信已處理並回寄結果，"
                      f"本次觸發已略過（避免重複查詢）。\n\n"
                      f"如需最新清單，請於上次查詢約 {mins} 分鐘後再寄一次觸發信。"),
                attachment_path=None,
            )
            logging.info("[dedup] 已回告知信(重複觸發已略過)：%s",
                         ", ".join(to_notify))
        except Exception:
            logging.warning("[dedup] 告知信寄送失敗(不影響流程)", exc_info=True)

    threading.Thread(target=_worker, name="ConsultDedupNotice",
                     daemon=True).start()


def _send_failure_notice_async(recipients, reason: str) -> None:
    """[新功能 2026-06-11] email 觸發的會診查詢整個失敗(重試用盡)時回信告知觸發者。
    原本只寫 log → 觸發醫師不知道沒成功、苦等不到結果。獨立 daemon thread 寄送。"""
    if not recipients:
        return

    def _worker():
        try:
            from cmuh_common.smtp_mail import send_mail
            send_mail(
                recipients=[str(r) for r in recipients],
                subject="會診查詢失敗通知",
                body=("您的會診查詢觸發信已收到，但執行失敗（已重試多次仍未成功）。\n\n"
                      f"最後錯誤：{str(reason)[:300]}\n\n"
                      "已解除重查限制，您可立即重寄一封觸發信再試；"
                      "若持續失敗請通知管理者查看 settings/consult_query.log。"),
                attachment_path=None,
            )
            logging.info("[notify] 已寄失敗通知給觸發者：%s",
                         ", ".join(str(r) for r in recipients))
        except Exception:
            logging.warning("[notify] 失敗通知寄送失敗(不影響流程)", exc_info=True)

    threading.Thread(target=_worker, name="ConsultFailNotice",
                     daemon=True).start()


def _hard_exit(reason: str, code: int = 1) -> None:
    """[2026-05-22 v34] 強制終止 process，不走 logging.shutdown (會 deadlock)。

    背景：原本 self-watchdog 的 os._exit 路徑會 call logging.shutdown()，但
    若另一 thread 正持有 handler lock (e.g. scheduler 卡在 logging.info)，
    我們的 thread 在 close() 時無限等 → kill path 完全失效 → process 永遠
    不會死 → 外層 watchdog 也救不回來 (因為 process 還活著)。

    這個 helper：
      1. 只做非阻塞 flush；handler lock 拿不到就跳過 (不 close、不等待)
      2. 不論成功與否 → 立刻 os._exit(code)
    """
    import os as _os
    # 嘗試 flush 但不卡死
    try:
        # 只 flush，不 close；handler lock 拿不到就跳過，避免 hard-exit 自己卡死。
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
    _os._exit(code)


def _scheduler_self_watchdog() -> None:
    """獨立 daemon thread — 每 30s 看 scheduler_loop 是否還活著。

    [2026-05-22 v34 重寫] 修兩個關鍵問題：
      A. 原本 kill path call logging.shutdown 會 deadlock — 改 _hard_exit
         (只 flush 不 close handlers，直接 os._exit)
      B. 加 thread is_alive() 檢查 — last_tick 訊號可能 race，但 thread
         物件 is_alive() 是 Python 直接判讀 thread state，最可靠

    階梯式處理：
      1. scheduler thread is_alive()==False → 立刻 _hard_exit (thread 真死了)
      2. last_tick 超過 3 分鐘 → log CRITICAL + force_close IMAP socket
         (希望讓卡在 socket recv 的 thread 解套)
      3. 再過 20 秒 last_tick 仍沒更新 → force_close 沒救活，_hard_exit
         讓 process 死亡 → 外層 watchdog 偵測沒在跑 → 重啟新 instance

    為什麼必須 _hard_exit：
      - sys.exit() 只結束 main thread，daemon 用無效
      - threading 沒有 thread.kill()
      - logging.shutdown() 在死 handler lock 情境下會 deadlock
    """
    DEAD_THRESHOLD = 180       # 3 分鐘無 tick → 嘗試 force_close (原 300s 太鬆)
    KILL_THRESHOLD = 20        # force_close 後再 20s 沒救 → _hard_exit
    CHECK_INTERVAL = 30        # 縮短為 30s 巡邏一次 (原 60s)
    force_closed_at = 0.0      # 記錄上次 force_close 時間，避免重複
    # [I] scheduler 半死偵測：tick 還在跑但沒有成功 IMAP poll > 10 分鐘
    HALF_DEAD_THRESHOLD = 600
    last_half_dead_log = 0.0
    while running.is_set():
        try:
            if not _sleep_while_running(CHECK_INTERVAL):
                break

            # [2026-05-22 v34] Stage 0：scheduler thread 直接死了 → 立刻退場
            global _scheduler_thread_ref
            if _scheduler_thread_ref is not None and not _scheduler_thread_ref.is_alive():
                logging.critical(
                    "[self-watchdog] scheduler thread is_alive()=False (thread 真死了) "
                    "→ _hard_exit(1) 強制重啟整個 process (外層 watchdog 會接手)")
                _hard_exit("scheduler thread dead", code=1)

            last = _SCHEDULER_LIVENESS.get("last_tick", 0.0)
            if last == 0.0:
                continue  # 還沒第一次 tick，給它時間 init
            age = time.time() - last

            # Stage 1：偵測卡死 → force_close socket
            if age > DEAD_THRESHOLD and force_closed_at == 0.0:
                logging.critical(
                    "[self-watchdog] scheduler 已 %.0f 秒沒 tick (>%.0fs 視為死亡)！"
                    " 強制關閉 IMAP socket 嘗試解套",
                    age, DEAD_THRESHOLD)
                try:
                    from cmuh_common.imap_reader import force_close_active
                    force_close_active()
                except Exception:
                    logging.exception("[self-watchdog] force_close 例外")
                force_closed_at = time.time()
                continue

            # [I] scheduler 半死：tick 正常但 IMAP 一直失敗
            #   (e.g. cooldown 中, 或網路斷)
            last_ok = _SCHEDULER_LIVENESS.get("last_imap_success", 0.0)
            if last_ok > 0:
                imap_age = time.time() - last_ok
                if imap_age > HALF_DEAD_THRESHOLD:
                    if time.time() - last_half_dead_log > 600:
                        logging.warning(
                            "[half-dead] scheduler tick 正常但 IMAP 已 %.0f 秒"
                            "沒成功 poll (>%.0fs)。網路問題或 IMAP 認證失效？",
                            imap_age, HALF_DEAD_THRESHOLD)
                        last_half_dead_log = time.time()

            # Stage 2：force_close 沒救活 → _hard_exit 強制重啟
            if force_closed_at > 0:
                since_force = time.time() - force_closed_at
                if last > force_closed_at:
                    # scheduler 復活了，重置
                    logging.info(
                        "[self-watchdog] scheduler 已恢復 tick，取消重啟")
                    force_closed_at = 0.0
                elif since_force > KILL_THRESHOLD:
                    logging.critical(
                        "[self-watchdog] force_close 後 %.0fs scheduler 仍卡死 "
                        "→ _hard_exit(1) 強制重啟整個 process (外層 watchdog 會接手)",
                        since_force)
                    _hard_exit("scheduler stuck after force_close", code=1)
        except Exception:
            logging.exception("[self-watchdog] tick 例外")


def _ensure_scheduler_self_watchdog() -> None:
    global _self_watchdog_thread_ref
    with _self_watchdog_lock:
        if (_self_watchdog_thread_ref is not None
                and _self_watchdog_thread_ref.is_alive()):
            return
        _self_watchdog_thread_ref = threading.Thread(
            target=_scheduler_self_watchdog,
            name="SchedulerSelfWatchdog",
            daemon=True,
        )
        _self_watchdog_thread_ref.start()


def scheduler_loop() -> None:
    logging.info("=== 會診查詢排程器啟動 v%s ===", CURRENT_VERSION)
    _rebuild_schedule()

    # [穩定性] 啟動 self-watchdog 子 thread (獨立監看 scheduler 是否還活著)
    _ensure_scheduler_self_watchdog()

    last_email_check = 0.0
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 60.0  # 至少每 60s 寫一筆 log，方便看出 thread 死了
    IMAP_HARD_TIMEOUT = 60.0   # 單次 IMAP check 上限，過了就放棄
    # [穩定性] IMAP 連續失敗 backoff — 避免持續每 20s 撞牆 (網路斷時不停 log)
    IMAP_FAIL_THRESHOLD = 3       # 連續 N 次 error
    IMAP_COOLDOWN_SEC = 300       # 之後暫停 5 分鐘
    consecutive_imap_errors = 0
    imap_cooldown_until = 0.0
    last_cooldown_log = 0.0  # [opt B3] cooldown 進度 log 的時間節流(取代失效的 %60 modulo)
    # [優化] cfg 快取：原本每秒 load_config → 86400 reads/day。改快取 + 60s
    # 過期重讀。設定變更走 RELOAD_FLAG 強制重讀，所以使用者改設定也即時生效。
    cfg = None
    cfg_loaded_at = 0.0
    while running.is_set():
        # [穩定性] 每次迴圈頂端打卡 — self-watchdog 用這個判斷 scheduler 活著
        _SCHEDULER_LIVENESS["last_tick"] = time.time()
        try:
            schedule.run_pending()
            # 「立即執行」旗標檔（由 --run-now 的第二個實例、或設定視窗寫入）
            if RUNNOW_FLAG.exists():
                try:
                    RUNNOW_FLAG.unlink()
                except OSError:
                    pass
                logging.info("收到立即執行要求")
                trigger_job_async("手動")
            # 「設定已變更」旗標檔（由設定視窗存檔後寫入）→ 重建排程 + 重 load cfg
            if RELOAD_FLAG.exists():
                try:
                    RELOAD_FLAG.unlink()
                except OSError:
                    pass
                logging.info("偵測到設定變更，重新建立排程")
                _rebuild_schedule()
                cfg = load_config()  # RELOAD_FLAG 觸發時重讀
            # 信件觸發：每 N 秒輪詢一次收件匣（啟用時）。改用 IMAP 直連
            # Gmail（imap.gmail.com:993），不再依賴 Outlook COM——後者在 admin
            # 行程下會起一個沒設定郵件帳號的 admin Outlook，永遠收不到信。
            # 輪詢週期可由 cfg.email_trigger_poll_seconds 調整（預設 20 秒，
            # 與 Gmail rate limit 完全相容；想更即時可降至 10 秒）。
            # [優化] 不再每秒 load_config — 改快取 + RELOAD_FLAG / 60s 過期重讀
            if cfg is None or time.time() - cfg_loaded_at > 60:
                cfg = load_config()
                cfg_loaded_at = time.time()
            if cfg.get("email_trigger_enabled"):
                poll_sec = float(cfg.get("email_trigger_poll_seconds", 20))
                # [穩定性] 如果在 cooldown 期間，跳過 IMAP poll (5 分鐘內不再撞)
                in_cooldown = time.time() < imap_cooldown_until
                if in_cooldown and time.time() - last_email_check >= poll_sec:
                    # cooldown 期間：仍要把 last_email_check 推進避免一直 spam
                    # 但實際上不要 IMAP poll，等 cooldown 結束
                    remaining = imap_cooldown_until - time.time()
                    # [opt B3] 原本 int(remaining) % 60 == 0 因評估點落在 ~20s 顆粒、
                    # remaining 是浮點，幾乎永遠命中不到 60 倍數秒 → 這行提醒實務上從不印，
                    # cooldown 進度在 log 中不可見。改用時間節流(比照同檔 half-dead log idiom)。
                    if time.time() - last_cooldown_log >= 60:
                        logging.info("[IMAP cooldown] 連續失敗中，剩 %.0fs 後恢復",
                                      remaining)
                        last_cooldown_log = time.time()
                    last_email_check = time.time()
                if not in_cooldown and time.time() - last_email_check >= poll_sec:
                    last_email_check = time.time()
                    kw = cfg.get("email_trigger_subject_keyword",
                                 DEFAULT_CONFIG["email_trigger_subject_keyword"])
                    # ★ 用 thread + 60s timeout 包起來，避免 imaplib socket 卡死整個 scheduler
                    # [會診2] 觸發信時效上限(小時→秒)；0/負值=不過濾
                    try:
                        _max_age_h = float(cfg.get(
                            "email_trigger_max_age_hours",
                            DEFAULT_CONFIG["email_trigger_max_age_hours"]))
                    except (TypeError, ValueError):
                        _max_age_h = DEFAULT_CONFIG["email_trigger_max_age_hours"]
                    r = _run_imap_check_with_timeout(
                        kw, timeout=IMAP_HARD_TIMEOUT,
                        max_age_sec=max(0.0, _max_age_h) * 3600)
                    if r.get("error"):
                        consecutive_imap_errors += 1
                        logging.warning("檢查觸發信失敗 (%d/%d): %s",
                                          consecutive_imap_errors,
                                          IMAP_FAIL_THRESHOLD, r["error"])
                        if consecutive_imap_errors >= IMAP_FAIL_THRESHOLD:
                            imap_cooldown_until = time.time() + IMAP_COOLDOWN_SEC
                            logging.warning(
                                "[IMAP cooldown] 連續 %d 次失敗，暫停 IMAP 輪詢 "
                                "%.0f 秒；網路恢復後自動回 normal poll",
                                consecutive_imap_errors, IMAP_COOLDOWN_SEC)
                            consecutive_imap_errors = 0  # 重置避免 cooldown 結束又馬上 trigger
                    else:
                        # 成功 → 重置連續失敗計數 + [I] 更新 last_imap_success
                        if consecutive_imap_errors > 0:
                            logging.info("[IMAP] 連續失敗已恢復 (之前 %d 次)",
                                          consecutive_imap_errors)
                        consecutive_imap_errors = 0
                        _SCHEDULER_LIVENESS["last_imap_success"] = time.time()
                        logging.info(
                            "檢查觸發信 [IMAP/%s]：未讀 %d 封，主旨含 %r 的 %d 封",
                            cfg.get("sender_account", "?"),
                            r["scanned"], kw, r["matched"])
                        if r["matched"] == 0 and r["samples"]:
                            logging.info(
                                "（最近未讀主旨樣本，用來確認你的觸發信是否真的進收件匣）：%s",
                                " | ".join(repr(s) for s in r["samples"]))
                    # 任何一筆 log 都重置 heartbeat（避免重複記）
                    last_heartbeat = time.time()
                    if r.get("triggered"):
                        # 寄件人白名單過濾：只有授權的 email 寄來的觸發信才生效
                        senders = r.get("matched_senders") or []
                        allow = set(cfg.get("allowed_trigger_senders") or [])
                        allowed = [s for s in senders if s.lower() in allow]
                        blocked = [s for s in senders if s.lower() not in allow]
                        if blocked:
                            logging.warning(
                                "收到觸發信但寄件人不在白名單，已忽略：%s",
                                ", ".join(blocked))
                        if allowed:
                            # [B] Dedup：同一 sender 5 分鐘內重複觸發 → 跳過
                            dedup_skipped = [s for s in allowed
                                              if _trigger_is_duplicate(s)]
                            dedup_proceed = [s for s in allowed
                                              if s not in dedup_skipped]
                            if dedup_skipped:
                                logging.warning(
                                    "[dedup] %s 在 %ds 內已處理過 → 略過避免重複寄信",
                                    ", ".join(dedup_skipped),
                                    _TRIGGER_DEDUP_WINDOW_SEC)
                                # [會診3 2026-06-11] 回告知信(原本靜默忽略，醫師重發
                                # 查詢卻苦等不到結果也不知道被略過)
                                _send_dedup_notice_async(dedup_skipped)
                            if dedup_proceed:
                                logging.info(
                                    "收到觸發信（IMAP），立即執行 consult flow；"
                                    "結果將回寄給觸發者：%s",
                                    ", ".join(dedup_proceed))
                                trigger_job_async("email",
                                                  override_recipients=dedup_proceed)
                        elif not blocked:
                            # 比對到主旨但完全沒抓到 From → fallback 用設定的 recipients
                            # [opt A1] 此 fallback 分支原本沒去重：若觸發信 From 解析不出
                            # (畸形 From) 且 imap_reader 標已讀又失敗(只 log 不 raise)，這封
                            # UNSEEN 信會每輪 IMAP poll(~20s)重新命中→每 20s 重跑完整 consult
                            # flow+寄信，直到撞 SMTP rate-limit。用固定哨兵 key 套用與 allowed
                            # 路徑一致的去重，把「每 20s」壓成「最多每 dedup 窗一次」。
                            if _trigger_is_duplicate("__no_sender__"):
                                logging.warning(
                                    "[dedup] 無法解析 From 的觸發信在 %ds 內已處理過 → "
                                    "略過避免重複寄信", _TRIGGER_DEDUP_WINDOW_SEC)
                            else:
                                logging.info(
                                    "收到觸發信但無法解析 From，fallback 用 "
                                    "email_trigger_recipients")
                                trigger_job_async("email")
            # ★ Heartbeat：每 60s 一定寫一筆 log。下次再卡住 1 分鐘內就能發現。
            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                next_poll_in = "-"
                if cfg.get("email_trigger_enabled"):
                    try:
                        ps = float(cfg.get("email_trigger_poll_seconds", 20))
                        next_poll_in = f"{max(0, ps - (time.time() - last_email_check)):.0f}s"
                    except (TypeError, ValueError):
                        pass
                logging.info("[heartbeat] scheduler alive (下次 IMAP 輪詢: %s)",
                              next_poll_in)
                last_heartbeat = time.time()
        except Exception:
            logging.error("排程迴圈例外", exc_info=True)
        # [優化] 自適應 sleep — 算下次「真的有事要做」之前的時間，最久 5s。
        # 早期固定 sleep(1)，每秒醒來幾乎都沒事。改 0.5-5s 範圍對使用者觀感
        # 沒差：schedule 套件 12:30/17:00 在 5s 內仍會準時觸發；email 觸發信
        # 本來內建 20s 容差；CPU 用量降 5 倍。
        now = time.time()
        next_imap_due = 5.0  # 預設上限 5s
        try:
            if cfg and cfg.get("email_trigger_enabled"):
                ps = float(cfg.get("email_trigger_poll_seconds", 20))
                next_imap_due = (last_email_check + ps) - now
        except Exception:
            pass
        next_hb_due = (last_heartbeat + HEARTBEAT_INTERVAL) - now
        sleep_for = min(5.0, next_imap_due, next_hb_due)
        if sleep_for < 0.5:
            sleep_for = 0.5
        if not _sleep_while_running(sleep_for):
            break


# =============================================================================
# 設定視窗
# =============================================================================
class ConfigApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"皮膚科會診查詢設定 (v{CURRENT_VERSION})")
        self.geometry("760x720")
        # [v18 2026-05-25] 攔截 Tk callback 例外進 log (原本進 stderr 黑洞)
        try:
            from cmuh_common.tk_exception import install_tk_exception_handler
            install_tk_exception_handler(self)
        except Exception:
            logging.debug("Tk callback exception hook 失敗", exc_info=True)
        self.cfg = load_config()
        try:
            from cmuh_common.window_icon import apply_tk_window_icon
            apply_tk_window_icon(self)
        except Exception:
            pass
        self._build_ui()
        self.after(150, self._poll_log)

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        cred = ttk.LabelFrame(root, text="登入資訊", padding=8)
        cred.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(cred, text="使用者代碼:").grid(row=0, column=0, sticky="w", **pad)
        self.user_var = tk.StringVar(value=self.cfg["username"])
        ttk.Entry(cred, textvariable=self.user_var, width=24,
                  font=("Consolas", 11)).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(cred, text="密碼:").grid(row=1, column=0, sticky="w", **pad)
        self.pass_var = tk.StringVar(value=self.cfg["password"])
        self.pass_entry = ttk.Entry(cred, textvariable=self.pass_var, show="●",
                                    width=24, font=("Consolas", 11))
        self.pass_entry.grid(row=1, column=1, sticky="w", **pad)
        self.show_pw = tk.BooleanVar()
        ttk.Checkbutton(cred, text="顯示", variable=self.show_pw,
                        command=lambda: self.pass_entry.config(
                            show="" if self.show_pw.get() else "●")
                        ).grid(row=1, column=2, sticky="w", **pad)

        rcp = ttk.LabelFrame(
            root, text=f"收件人（可隨時新增/刪除，最多 {_MAX_RECIPIENTS} 位）",
            padding=8)
        rcp.pack(fill=tk.X, pady=(0, 8))
        self.rcp_list = tk.Listbox(rcp, height=7, font=("Consolas", 10))
        self.rcp_list.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for r in self.cfg["recipients"]:
            self.rcp_list.insert(tk.END, r)
        rcp_btns = ttk.Frame(rcp)
        rcp_btns.pack(side=tk.LEFT, padx=6)
        self.rcp_entry = ttk.Entry(rcp_btns, width=28, font=("Consolas", 10))
        self.rcp_entry.pack(pady=2)
        ttk.Button(rcp_btns, text="新增", command=self._add_rcp).pack(fill=tk.X, pady=1)
        ttk.Button(rcp_btns, text="刪除選定", command=self._del_rcp).pack(fill=tk.X, pady=1)

        sched = ttk.LabelFrame(root, text="輪詢（每隔幾分鐘查一次,有新會診才寄信）", padding=8)
        sched.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(sched, text="輪詢間隔（分鐘,5~120）:").grid(
            row=0, column=0, sticky="w", **pad)
        self.interval_var = tk.StringVar(value=str(self.cfg.get("poll_interval_minutes", 15)))
        ttk.Entry(sched, textvariable=self.interval_var, width=10,
                  font=("Consolas", 11)).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(sched, text="半夜休息（不查不寄）起/迄時:").grid(
            row=1, column=0, sticky="w", **pad)
        qrow = ttk.Frame(sched)
        qrow.grid(row=1, column=1, sticky="w", **pad)
        self.quiet_start_var = tk.StringVar(value=str(self.cfg.get("quiet_start_hour", 0)))
        self.quiet_end_var = tk.StringVar(value=str(self.cfg.get("quiet_end_hour", 6)))
        ttk.Entry(qrow, textvariable=self.quiet_start_var, width=4,
                  font=("Consolas", 11)).pack(side=tk.LEFT)
        ttk.Label(qrow, text=" 時 ～ ").pack(side=tk.LEFT)
        ttk.Entry(qrow, textvariable=self.quiet_end_var, width=4,
                  font=("Consolas", 11)).pack(side=tk.LEFT)
        ttk.Label(qrow, text=" 時（預設 0~6）").pack(side=tk.LEFT)
        self.enabled_var = tk.BooleanVar(value=self.cfg.get("enabled", True))
        ttk.Checkbutton(sched, text="啟用自動輪詢", variable=self.enabled_var
                        ).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        trig = ttk.LabelFrame(root, text="信件遠端觸發（從手機/任何信箱寄一封信來即可遠端觸發）",
                              padding=8)
        trig.pack(fill=tk.X, pady=(0, 8))
        self.email_trigger_var = tk.BooleanVar(
            value=self.cfg.get("email_trigger_enabled", False))
        ttk.Checkbutton(trig, text="啟用信件觸發",
                        variable=self.email_trigger_var
                        ).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(trig, text="觸發主旨關鍵字:").grid(
            row=1, column=0, sticky="w", **pad)
        self.email_trigger_kw_var = tk.StringVar(
            value=self.cfg.get("email_trigger_subject_keyword",
                               "[皮膚科會診觸發]"))
        ttk.Entry(trig, textvariable=self.email_trigger_kw_var, width=30,
                  font=("Consolas", 11)).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(
            trig,
            text="用法：從任何信箱寄信到你 Outlook 接收的信箱，主旨含上方關鍵字 → 60 秒內自動觸發一次。",
            foreground="#666", font=("Microsoft JhengHei UI", 9), wraplength=600,
        ).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        btns = ttk.Frame(root)
        btns.pack(fill=tk.X, pady=4)
        ttk.Button(btns, text="儲存設定",
                   command=self._save_and_close).pack(side=tk.LEFT)
        ttk.Button(btns, text="儲存並立即執行一次",
                   command=self._test_run).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="關閉", command=self.destroy).pack(side=tk.RIGHT)

        logf = ttk.LabelFrame(root, text="執行紀錄", padding=4)
        logf.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.log_text = scrolledtext.ScrolledText(
            logf, height=10, state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _add_rcp(self) -> None:
        addr = self.rcp_entry.get().strip()
        if not addr:
            return
        if self.rcp_list.size() >= _MAX_RECIPIENTS:
            messagebox.showwarning("上限", f"最多 {_MAX_RECIPIENTS} 位收件人")
            return
        if addr in self.rcp_list.get(0, tk.END):
            return
        self.rcp_list.insert(tk.END, addr)
        self.rcp_entry.delete(0, tk.END)

    def _del_rcp(self) -> None:
        sel = self.rcp_list.curselection()
        if sel:
            self.rcp_list.delete(sel[0])

    def _collect(self) -> dict:
        cfg = dict(self.cfg)
        cfg["username"] = self.user_var.get().strip()
        cfg["password"] = self.pass_var.get()
        cfg["recipients"] = list(self.rcp_list.get(0, tk.END))
        # [2026-06-25] 改存輪詢間隔 / 半夜休息時段(取代舊的 12:40/17:10 固定排程)。壞值退回預設。
        try:
            cfg["poll_interval_minutes"] = max(5, min(120, int(self.interval_var.get().strip())))
        except (TypeError, ValueError):
            cfg["poll_interval_minutes"] = DEFAULT_CONFIG["poll_interval_minutes"]
        try:
            cfg["quiet_start_hour"] = max(0, min(23, int(self.quiet_start_var.get().strip())))
        except (TypeError, ValueError):
            cfg["quiet_start_hour"] = DEFAULT_CONFIG["quiet_start_hour"]
        try:
            cfg["quiet_end_hour"] = max(0, min(23, int(self.quiet_end_var.get().strip())))
        except (TypeError, ValueError):
            cfg["quiet_end_hour"] = DEFAULT_CONFIG["quiet_end_hour"]
        cfg["enabled"] = self.enabled_var.get()
        cfg["email_trigger_enabled"] = self.email_trigger_var.get()
        cfg["email_trigger_subject_keyword"] = self.email_trigger_kw_var.get().strip() \
            or DEFAULT_CONFIG["email_trigger_subject_keyword"]
        return cfg

    def _save_and_close(self) -> None:
        save_config(self._collect())
        # 通知常駐的托盤程式重新載入設定／重建排程
        try:
            RELOAD_FLAG.write_text(datetime.now().isoformat(), encoding="utf-8")
        except Exception:
            logging.debug("寫入 reload 旗標失敗", exc_info=True)
        messagebox.showinfo("已儲存", "設定已儲存，背景常駐程式會自動套用新設定。")
        self.destroy()

    def _test_run(self) -> None:
        save_config(self._collect())
        # 透過旗標檔通知「正在系統列常駐的那個實例」重載設定並立即執行一次，
        # 避免在這個獨立的設定行程內另外跑流程造成兩份同時動作。
        try:
            RELOAD_FLAG.write_text(datetime.now().isoformat(), encoding="utf-8")
            RUNNOW_FLAG.write_text(datetime.now().isoformat(), encoding="utf-8")
        except Exception:
            logging.debug("寫入旗標失敗", exc_info=True)
        messagebox.showinfo("測試", "已儲存設定，並通知背景程式立即執行一次，"
                                    "請稍候至收件匣確認。")

    def _poll_log(self) -> None:
        lines = []
        for _ in range(LOG_POLL_MAX_RECORDS):
            try:
                rec = log_queue.get_nowait()
                lines.append(
                    f"{datetime.fromtimestamp(rec.created).strftime('%H:%M:%S')} "
                    f"[{rec.levelname}] {rec.getMessage()}\n"
                )
            except queue.Empty:
                break
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, "".join(lines))
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        self.after(150, self._poll_log)


# =============================================================================
# 托盤
# =============================================================================
def exit_action(icon=None, item=None) -> None:
    """[v19 2026-05-26] 修 tray 退出關不掉 bug — 跟 autoclock.exit_action 同 pattern。

    原本 sys.exit(0) 被 pystray._dispatcher 吞掉，main thread message pump
    沒退 → process 永遠不結束。改成把 cleanup + os._exit 移到 daemon thread，
    callback 乾淨返回，0.5s 後強制 os._exit。
    """
    global _exit_started
    with _exit_lock:
        if _exit_started:
            return
        _exit_started = True
    logging.info("使用者要求退出會診查詢程式")
    running.clear()
    if tray_icon_object:
        try:
            tray_icon_object.visible = False
        except Exception:
            pass
        try:
            tray_icon_object.stop()
        except Exception:
            pass

    def _shutdown() -> None:
        try:
            release_single_instance()
        except Exception:
            pass
        try:
            time.sleep(0.5)
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_shutdown, daemon=True,
                     name="ConsultShutdown").start()


def _tray_run_now(icon=None, item=None) -> None:
    trigger_job_async("手動")


def _tray_configure(icon=None, item=None) -> None:
    """用獨立行程開啟設定視窗，常駐的托盤程式不中斷（先前用 restart 重啟，
    在某些情況下重啟後設定視窗沒出現，且托盤也消失了）。"""
    try:
        launch_python_script(
            os.path.abspath(sys.argv[0]),
            args=["--configure"],
            cwd=get_app_dir(),
        )
    except Exception:
        logging.error("開啟設定視窗失敗", exc_info=True)
        _notify("開啟設定失敗", "請改用雙擊會診查詢程式 + --configure")


def _send_test_email() -> None:
    """測試寄信。依 cfg.mail_method 選 SMTP 或 Outlook。失敗會在 log 詳細記錄
    並用 winotify 跳通知（讓使用者知道測試結果）。

    用 test_recipients（預設只給 expertise88864@gmail.com 一個人，免擾其他收
    件人）。SMTP 模式直接連 Gmail；Outlook 模式才需要 sender_account。"""
    cfg = load_config()
    mail_method = str(cfg.get("mail_method", "smtp")).lower()
    recipients = cfg.get("test_recipients") or cfg["recipients"]
    now = datetime.now()

    if mail_method == "smtp":
        from cmuh_common.smtp_mail import (
            SmtpNotConfiguredError, is_configured, load_credentials, send_mail,
        )
        if not is_configured():
            cred = load_credentials()
            msg = (f"SMTP 尚未設定。請編輯 {Path(get_settings_dir()) / 'smtp_credentials.json'} "
                    f"填入 password（cmuhdermatology@gmail.com 的 App Password）。\n"
                    f"目前 host={cred['host']}, username={cred['username']}, "
                    f"password={'已設定' if cred['password'] else '空字串'}")
            logging.warning("測試寄信跳過：%s", msg)
            _notify("測試寄信失敗", "SMTP password 未設定，請看 log")
            return
        try:
            send_mail(
                recipients=recipients,
                subject="皮膚科會診查詢 — 測試信 (SMTP)",
                body=(f"這是一封測試信，寄送時間 {now:%Y-%m-%d %H:%M:%S}。\n"
                      f"若收到此信，代表 SMTP 寄信與收件人設定正常。\n"
                      f"（寄件人：{load_credentials()['from_address']}, "
                      f"方式：SMTP / smtp.gmail.com）"),
                attachment_path=None,
            )
            _notify("測試寄信成功", f"已寄給 {recipients[0]}（SMTP）")
        except SmtpNotConfiguredError as e:
            logging.warning("測試寄信跳過：%s", e)
            _notify("測試寄信失敗", "SMTP 未設定完整，請看 log")
        except Exception as e:
            logging.error("測試寄信失敗：%s", e, exc_info=True)
            _notify("測試寄信失敗", f"{type(e).__name__}: {e}")
        return

    # Outlook fallback path
    if not _outlook_available():
        logging.info("本機無可用 Outlook，測試寄信靜默跳過")
        return
    import pythoncom
    pythoncom.CoInitialize()
    try:
        sender = cfg.get("sender_account", "") or ""
        send_via_outlook(
            None,
            "皮膚科會診查詢 — 測試信 (Outlook)",
            f"這是一封測試信，寄送時間 {now:%Y-%m-%d %H:%M:%S}。\n"
            f"若收到此信，代表 Outlook 寄信與收件人設定正常。\n"
            f"（寄件人：{sender or 'Outlook 預設帳號'}）",
            recipients,
            sender_account=sender,
        )
        _notify("測試寄信成功", f"已寄給 {recipients[0]}（Outlook）")
    except Exception as e:
        logging.error("測試寄信失敗：%s", e, exc_info=True)
        _notify("測試寄信失敗", f"{type(e).__name__}: {e}")
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _tray_test_email(icon=None, item=None) -> None:
    lease = _test_email_gate.acquire_lease("test-email")
    if lease is None:
        logging.info("測試寄信仍在執行中，本次點擊略過")
        _notify("測試寄信執行中", "請等待目前測試完成")
        return

    def _worker():
        try:
            _send_test_email()
        finally:
            _test_email_gate.release("test-email", lease)

    threading.Thread(target=_worker, name="ConsultTestMail",
                     daemon=True).start()


def _request_restart_for_update() -> None:
    """背景 thread 偵測到新版 → 收掉托盤圖示並標記重啟，讓 main thread 在 run()
    返回後乾淨重啟。

    【2026-06-03 修「系統列出現兩個圖示」】絕不可在此 daemon thread 直接
    restart_self()：預設走 sys.exit(0) 在子 thread 只會結束「本 thread」、整個
    process 不會退 → 舊 process（main thread 仍卡在 tray run()）持續存活，新
    process 又起來 → 系統列同時出現新舊兩個圖示。
    正解：在這裡 stop() 托盤（NIM_DELETE 移除舊圖示 + 解除 main thread 的 run()），
    main thread 返回後由它自己 restart_self()（sys.exit 在 main thread 才會真正
    結束整個 process）。釋放單例 mutex 也延到 main thread 重啟前一刻才做。
    """
    global _restart_after_run, _exit_started
    with _exit_lock:
        if _exit_started:
            return  # 使用者已按退出，或已在收尾 → 不重複觸發
        _exit_started = True
        _restart_after_run = True
    running.clear()  # 中止 ImportError fallback 的 while running 迴圈
    if tray_icon_object:
        try:
            tray_icon_object.visible = False
        except Exception:
            pass
        try:
            tray_icon_object.stop()
        except Exception:
            pass


def _check_update_in_background() -> None:
    try:
        from cmuh_common.updater import (
            check_and_update,
            need_restart_after_update,
        )
        result = check_and_update()
        if need_restart_after_update(result):
            logging.info("會診查詢程式偵測到新版，準備重新啟動")
            _request_restart_for_update()
    except Exception:
        logging.debug("背景更新檢查失敗", exc_info=True)


# =============================================================================
# 主入口
# =============================================================================
def main() -> None:
    try:
        # 強制以系統管理員身份執行：systemftp.exe manifest 標記 requireAdministrator，
        # 非 admin 行程呼叫 CreateProcess 會直接得到 ERROR_ELEVATION_REQUIRED (740)，
        # 排程到點就會失敗（見 2026-05-16/17 log）。非 admin 一律走 UAC 重啟，
        # run_as_admin() 內部會 sys.exit(0) 結束本進程，admin 重啟後才會繼續往下。
        if not is_admin():
            run_as_admin()
            return  # 保險：理論上 run_as_admin 已 sys.exit

        args = sys.argv[1:]

        # [關鍵 fix 2026-05-20] mutex 擋退時 *完全不能寫 log*！否則：
        #   舊 scheduler thread 卡死 → watchdog 偵測 log mtime 過期 → 啟動新 process
        #   → 新 process 被 mutex 擋退但寫了一筆「已在執行中」log
        #   → log mtime 被更新 → watchdog 下次以為 consult 還活著 → 不 kill 舊的
        #   → 舊的 mutex 仍 hold → 新 instance 永遠被擋 → 死循環 N 小時
        # 修法：mutex check 放在 _setup_logging 之前。被擋退的 process 完全沉默
        # exit (沒 file handler 被建立 → log mtime 不會被新 process 污染)。
        # --configure 例外 (設定模式不搶 mutex，要寫 log 可)。
        if "--configure" not in args:
            # 先做 mutex 試探 — 不是 first_instance 就靜默退出
            # ensure_single_instance 內部只用 winapi，不依賴 logging
            first_instance = ensure_single_instance(MUTEX_NAME)
            if not first_instance:
                # --run-now 仍要寫 RUNNOW_FLAG 給常駐實例
                if "--run-now" in args:
                    try:
                        RUNNOW_FLAG.write_text(datetime.now().isoformat(),
                                               encoding="utf-8")
                    except Exception:
                        pass  # 不能 logging.error — 會污染 log mtime
                # 退出時不寫任何 log，避免污染 mtime 干擾 watchdog 判斷
                sys.exit(0)

        # ↓ 以下只有 first_instance 才會跑 ↓
        _setup_logging()

        # [穩定性] health monitor — RAM/網路/時鐘/硬碟 + 記憶體 leak 自動重啟 (A/E/F)
        try:
            from cmuh_common.health import start_health_monitor
            start_health_monitor("consult", ram_warn_mb=200, ram_crit_mb=500,
                                  interval_sec=300, network_check=True,
                                  auto_restart_on_crit=True,  # [A] 連續 6 次 (~30 分) RAM 超 crit → os._exit
                                  crit_persistence_ticks=6)
        except Exception:
            logging.debug("health monitor 啟動失敗", exc_info=True)

        # [穩定性] 全域 thread/sys excepthook：未捕獲例外寫 log。
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

        # 設定模式：不搶常駐單例，但設定視窗本身仍需防重複開啟，
        # 避免多個設定視窗同時儲存互相覆蓋。
        if "--configure" in args:
            if not ensure_single_instance(CONFIG_MUTEX_NAME):
                return
            try:
                ConfigApp().mainloop()
            finally:
                release_single_instance()
            return

        # 第一次啟動(無設定檔)或【尚未填 HIS 帳密】→ 強制開設定視窗。
        # [CQ-04] 帳密不再硬編碼,故也要擋「設定檔存在但缺帳密」→ 否則每輪以空帳密狂試登入。
        if not CONFIG_FILE.exists() or not _has_his_credentials(load_config()):
            logging.info("首次啟動或尚未設定 HIS 帳號/密碼，先開啟設定視窗")
            ConfigApp().mainloop()
            if not _has_his_credentials(load_config()):
                logging.info("設定視窗關閉但仍未填 HIS 帳號/密碼，結束"
                             "(不以空帳密啟動,避免每輪登入失敗)")
                return

        logging.info("=== 會診查詢程式啟動 v%s ===", CURRENT_VERSION)
        # [opt B1] 啟動時建一次 SMTP 設定範本(load_credentials 已改純讀取，不再於熱路徑寫檔)
        try:
            from cmuh_common.smtp_mail import ensure_credentials_template
            ensure_credentials_template()
        except Exception:
            logging.debug("ensure_credentials_template 失敗（忽略）", exc_info=True)
        # [會診1 2026-06-11] 載回未過期去重狀態(跨重啟防「標已讀失敗的信」重複觸發)
        load_trigger_dedup_state()
        # 啟動權限狀態（給「自動提權有沒有真的生效」一個白紙黑字證據）
        logging.info("執行權限：%s",
                     "admin ✓" if is_admin() else "一般使用者 ✗（systemftp 會 740 失敗）")
        # [CQ-05] 清掃前世硬退遺留在隱藏桌面的 systemftp 孤兒(持有單例 mutex 後才做,
        # 確保不會誤殺另一個實例的作用中 systemftp)。
        _cleanup_orphan_systemftp()
        threading.Thread(target=_check_update_in_background,
                         name="ConsultUpdateChecker", daemon=True).start()

        # 排程器執行緒 — [2026-05-22 v34] 保存 thread 引用給 self-watchdog 檢查 is_alive()
        global _scheduler_thread_ref
        _scheduler_thread_ref = threading.Thread(target=scheduler_loop,
                         name="ConsultScheduler", daemon=True)
        _scheduler_thread_ref.start()

        # 啟動即帶 --run-now → 立刻先跑一次
        if "--run-now" in args:
            trigger_job_async("手動")

        # 系統列圖示
        try:
            from PIL import Image
            import pystray

            ico = None
            try:
                from cmuh_common.icons import ensure_cmuh_app_icon_path
                p = ensure_cmuh_app_icon_path()
                if p and os.path.exists(p):
                    ico = Image.open(p)
            except Exception:
                ico = None
            if ico is None:
                ico = Image.new("RGB", (64, 64), "#3070B0")

            menu = (
                pystray.MenuItem("立即執行一次（擷取並寄出）", _tray_run_now,
                                 default=True),
                pystray.MenuItem("測試寄信", _tray_test_email),
                pystray.MenuItem("設定（收件人／寄送時間）", _tray_configure),
                pystray.MenuItem("退出", exit_action),
            )
            global tray_icon_object
            tray_icon_object = pystray.Icon(
                "ConsultQuery", ico, f"皮膚科會診查詢 v{CURRENT_VERSION}", menu)
            tray_icon_object.run()
        except ImportError:
            while running.is_set():
                if not _sleep_while_running(1):
                    break

    except Exception:
        err = f"會診查詢程式發生嚴重錯誤：\n{traceback.format_exc()}"
        # 先寫 log（如果 logging 已 setup）——之前只有 MessageBox，排程模式下
        # 對話框被關掉就沒任何證據，事後完全沒法追。
        try:
            logging.exception("main() 攔截到未處理例外")
        except Exception:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(0, err, "會診查詢程式錯誤", 0x10)
        except Exception:
            print(err, file=sys.stderr)

    # [2026-06-03] 背景更新檢查要求重啟 → 一律由 main thread 在此處理。
    # 此時 tray run() 已返回（舊圖示已 NIM_DELETE 移除），釋放單例後 restart_self
    # （main thread 走 sys.exit，能真正結束整個 process）→ 系統列只會有一個圖示。
    if _restart_after_run:
        logging.info("會診查詢程式：套用更新後重新啟動")
        try:
            release_single_instance()
        except Exception:
            pass
        restart_self()


if __name__ == "__main__":
    main()
