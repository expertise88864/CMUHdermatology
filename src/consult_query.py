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
from ctypes import wintypes  # noqa: E402
import html as _html  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import tkinter as tk  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, time as dt_time  # noqa: E402
from pathlib import Path  # noqa: E402
from tkinter import messagebox, scrolledtext, ttk  # noqa: E402
from typing import Any  # noqa: E402

import psutil  # noqa: E402
import schedule  # noqa: E402
import win32con  # noqa: E402
import win32event  # noqa: E402
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

from cmuh_common.atomic_io import (  # noqa: E402
    atomic_write_json, safe_load_json, safe_load_json_ex,
)
from cmuh_common.logging_setup import attach_queue_handler, setup_logging  # noqa: E402
from cmuh_common.paths import get_app_dir, get_settings_dir, restart_self  # noqa: E402
from cmuh_common.platform_win import is_admin, run_as_admin  # noqa: E402
from cmuh_common.process_launch import launch_python_script  # noqa: E402
from cmuh_common.win32_safe import call_with_timeout  # noqa: E402
from cmuh_common.single_instance import (  # noqa: E402
    ensure_single_instance, release_single_instance,
)
from cmuh_common.task_gate import (  # noqa: E402
    ActiveTaskGate, current_worker_superseded, worker_lease_scope,
)
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
from cmuh_common import consult_keepalive as _keepalive  # noqa: E402

BASE_DIR = Path(get_app_dir())
SETTINGS_DIR = Path(get_settings_dir())
CONFIG_FILE = SETTINGS_DIR / "consult_query_config.json"
LOG_FILE = SETTINGS_DIR / "consult_query.log"
SHOTS_DIR = SETTINGS_DIR / "consult_shots"
RUNNOW_FLAG = SETTINGS_DIR / "consult_query_runnow.flag"
RELOAD_FLAG = SETTINGS_DIR / "consult_query_reload.flag"
MAX_SHOT_FILES = 60          # ★容量後備,不是保留期政策★（見 _prune_old_shots）

SYSTEMFTP_PATH = r"C:\admc\systemftp.exe"
# [2026-07-25 審查] 行程名（小寫）；強制結束前重新確認身分用，避免 PID 重用時誤殺。
SYSTEMFTP_EXE_NAME = "systemftp.exe"
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
    # [2026-08-06 外審 P1-05] 只接受【通過 SPF/DKIM/DMARC 驗證】的觸發信。
    #   白名單比對的 From 是寄件者自填、可偽造的字串;本項要求 Gmail 在
    #   Authentication-Results 標記通過才觸發(fail-closed)。
    #
    # ★[2026-08-08 外審] 預設改為 True★
    #   之前預設 False,理由是「貿然開啟可能讓正在用的觸發功能靜默失效」。
    #   但那個預設的實際意義是:**任何能寄信到這個信箱的人,都可以遠端啟動
    #   一次 HIS 會診查詢,並讓一封含全院會診清單的信被寄出去。**
    #   只要把 From 偽造成白名單醫師就成立 —— 而 From 本來就是寄件者自填的。
    #   一個預設就開著的未授權遠端觸發,比「功能可能要調一次設定」嚴重得多。
    #
    #   ★原本那個顧慮用另一種方式解掉:不讓它「靜默」★
    #   白名單來信但驗證不過時,除了 log 之外還會寄一封開發者告警
    #   (見 `_alert_trigger_rejected`)。所以若真的是我們的判定太嚴,
    #   第一次就會有人知道,而不是等到有人抱怨「寄了信卻沒收到」。
    "require_authenticated_trigger": True,
    # [2026-06-16] 每天 12:40 + 17:10 都跑（不分平假日）。打卡系統於 7:31/12:31/17:01
    # 才登入打卡，故延後到 12:40 / 17:10 再查詢寄信，確保中午(12:31)上班與下午(17:01)
    # 下班打卡都「已完成並寫入紀錄」後才查，不會還沒打卡就先寄出誤判未打卡。
    "weekday_times": ["12:40", "17:10"],   # 週一～週五（已停用,改為 poll_interval_minutes 輪詢）
    "weekend_times": ["12:40", "17:10"],   # 週六、週日（已停用,同上）
    # [2026-06-25] 即時偵測:每 N 分鐘輪詢「我的會診清單」,只在出現「新病歷號」時才寄信
    # (信內含目前全部未回覆清單)。已取代固定時間排程(12:40/17:10)。
    "poll_interval_minutes": 3,   # [2026-08-03 常駐登入] 3 分鐘=keepalive 節奏(±10% 抖動;院方 5 分鐘閒置強制登出)
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
# 同一個通知視窗最多按幾次「確認」。超過就認定「按這個沒有用」而停手 ——
# 見 _wait_main_window_after_login 的說明(實機按了 200 次都沒關掉)。
_MAX_CLICKS_PER_NOTICE = 5


class JobSuperseded(RuntimeError):
    """本輪已被 gate 逾時接管（另一輪正在做同一件事）。

    ★不可重試，但【仍要走完終局收尾】★—— 不能直接 return：
    email 觸發的醫師會被去重卡住（5 分鐘内重發無效）又收不到任何通知，
    只能乾等一個永遠不會來的結果（同樣的錖在 2026-07-30 已經踩過一次）。
    沖進與 LoginNotCompleted 同一條 fatal 路徑：不 backoff、不重試，
    但釋放去重、回信告知、清孤兒。
    """


class LoginNotCompleted(RuntimeError):
    """登入視窗仍在畫面上 —— ★不可自動重試★

    [2026-07-30 外審] 我原本只是把錯誤訊息改對,並在說明裡寫「不自動重試登入,
    以免把帳號鎖死」。但 `_do_full_job` 的 `except Exception` 對任何例外都會殺掉
    systemftp、backoff、再跑一遍完整流程(retry_count 預設 3),而連續失敗告警的
    門檻又是 3 個任務 —— 使用者收到信之前,同一組帳密可能已經被送出 9 次。
    我聲稱在防的風險,我的修法完全沒有防到。

    故獨立成一個例外類:重試迴圈認得它就【立刻停止】,不再送出登入。
    帳密被院方停用/改過、或認證方式改版時,重試一百次也一樣,只會逼近鎖定門檻。
    """
class HISStartupBlocked(RuntimeError):
    """住院醫囑系統【自己】起不來（例：BDE 初始化失敗）——★不可自動重試★

    [2026-08-03 實機] 使用者收到「連續失敗 22 次／請確認帳號密碼是否被院方改過
    停用」的告警，手動開程式才看到真正的畫面是：
        住院醫囑系統 — An error occurred while attempting to initialize the
        Borland Database Engine (error $250E)
    也就是 HIS 根本沒起來，登入視窗因此被那個 modal 擋著（實機視窗清單正是
    `TFrmLogin(vis=1,en=0)` ＋ 一個 enabled 的對話框）。程式看到「登入視窗還在」
    就回報帳密問題，把人導去查完全無關的方向——帳密沒有任何問題。

    這一類要與 LoginNotCompleted 分開：處置不同（重開機／清 BDE 鎖檔／找資訊室，
    而不是去改密碼），而且【重試也沒有意義】——BDE 起不來，再登一百次也一樣。
    """


# 只比對這個固定英文字串與後面的錯誤碼，不記錄任何視窗文字內容（隱私邊界同
# _describe_windows_for_diag：診斷資訊只留 class/旗標/已知錯誤碼）。
BDE_ERROR_MARKER = "Borland Database Engine"
BDE_ERROR_CODE_RE = re.compile(r"\$[0-9A-Fa-f]{4}")

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
# ★[2026-08-10 批次SF] `_flow_lock` 卡住 = 會診查詢永久停擺,而且完全無聲★
#   這把鎖是 non-blocking acquire,所以不會像 autoclock 的 clock_lock 那樣堆積
#   等待中的緒 —— 它的失效模式更安靜:持鎖者永遠不釋放,之後【每一輪】都只印
#   一行 INFO「已有任務進行中」然後跳過。heartbeat 照常、scheduler tick 照常、
#   self-watchdog 也照常 —— 所有觀測點都說一切正常,而會診查詢再也不會執行。
#   本檔 5719 行的註解早就寫著「ActiveTaskGate 45 分鐘會自癒,_flow_lock 不會」,
#   卻從來沒有人在旁邊量它有沒有真的卡住。
#
#   ★可達路徑(不是理論)★ 休息時段(00-06)那條 `_session_close(...)` 是在
#   【持鎖的工作緒】上跑的,而它會走到
#     `_terminate_session_process` → `_close_session_windows`
#     → `_dismiss_blocking_modals` → `enum_children` → raw `GetWindowText()`
#   —— 那是【送 WM_GETTEXT 給目標視窗】,systemftp 凍結時永久不返回
#   (本檔 1240 行與 win32_safe 的模組說明講的就是同一件事)。
#   於是 `_do_full_job` 的 `finally: _flow_lock.release()` 永遠走不到。
#
#   對策與 autoclock 的 `bounded_clock_lock` 同一套:量持有時間,超過上限就
#   升級成重啟 —— 重啟是唯一能終結 native-wedged thread 的手段。
#   ★門檻要高於 gate 的 45 分鐘接管★:系統本來就認定超過 45 分鐘的工作已死,
#   所以持鎖滿 60 分鐘的那一條不可能還在做有用的事。合法上限遠低於此
#   (3 次 attempt × (240s 自動化 + 90s backoff + 60s SMTP) ≈ 20 分鐘)。
_FLOW_LOCK_WEDGED_SEC = 3600.0
_flow_lock_held_since = [0.0]          # monotonic;0.0 = 目前沒有人持有
_flow_wedge_restart_requested = [False]
# [2026-07-30 外審 P2-01] label 讓「逾時接管」的 warning 講得出是哪一支。
_consult_job_gate = ActiveTaskGate(stale_after_sec=45 * 60, label="consult")
_test_email_gate = ActiveTaskGate(stale_after_sec=10 * 60,
                                  label="consult/test-email")
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


_TRIGGER_AUTHZ_MIGRATION_KEY = "trigger_authz_migrated_2026_08"


def _migrate_trigger_authz(saved: dict) -> None:
    """把既有設定檔裡的 `require_authenticated_trigger=false` 一次性打開。

    ★[2026-08-08 外審第 2 回 F1]★ 只改 `DEFAULT_CONFIG` 保護不到【已經存在的
    設定檔】—— 而診間那台一定有(設定頁存過檔就會把整份寫下來)。
    那個 false 的實際意義是「任何能寄信到這個信箱的人都可以遠端啟動 HIS 查詢」,
    所以這不是使用者的偏好設定,是一個不該留著的舊預設。

    ★仍然留一條明確的 opt-out★ 遷移只做一次(用 `trigger_authz_migrated_2026_08`
    記號)。使用者若在遷移【之後】自己把它關掉,那是他知情的選擇,不會再被改回來。
    """
    if saved.get(_TRIGGER_AUTHZ_MIGRATION_KEY):
        return
    saved[_TRIGGER_AUTHZ_MIGRATION_KEY] = True
    changed = saved.get("require_authenticated_trigger") is False
    if changed:
        saved["require_authenticated_trigger"] = True
        logging.warning(
            "[trigger] 舊設定檔的 require_authenticated_trigger=false 已自動打開"
            " —— 那個值讓任何能寄信到觸發信箱的人都能遠端啟動 HIS 查詢。"
            "若確定要關,請在設定頁/設定檔改回 false(遷移只做一次,不會再動它)")
    # ★[第 3 回] marker 與新值要【寫回磁碟】★
    #   上一版只改記憶體:每次啟動都重新遷移一次,而註解承諾的
    #   「遷移後可以明確 opt-out」根本不成立 —— 使用者改回 false,
    #   下次開機又被打開。宣稱與實作不符,又一次。
    #   ★寫回失敗仍然維持本次 fail-closed★:安全姿態不因存檔失敗而退讓。
    try:
        from cmuh_common.atomic_io import atomic_write_json  # noqa: PLC0415
        atomic_write_json(str(CONFIG_FILE), saved)
    except Exception:
        logging.warning("[trigger] 授權遷移寫回設定檔失敗(本次仍要求驗證,"
                        "下次啟動會再遷移一次)", exc_info=True)


def load_config() -> dict:
    with _config_lock:
        cfg = dict(DEFAULT_CONFIG)
        try:
            if CONFIG_FILE.exists():
                saved = safe_load_json(str(CONFIG_FILE), default={})
                if isinstance(saved, dict):
                    _migrate_trigger_authz(saved)
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
            _pim = int(cfg.get("poll_interval_minutes", 3))
            # [2026-08-03 常駐登入] 既有部署存的是舊預設 15 → 一次性升級為 3
            # (keepalive 節奏)。只升級「恰等於舊預設」者;升級後立旗標,使用者
            # 之後刻意改回 15 不會被再次覆蓋。
            # [codex P2 已評估→維持] 「值=15」無法分辨舊預設或使用者自選——本機隊
            # 單一操作者,3 分鐘正是使用者 2026-08-03 的直接指示(「每3分鐘查詢一次」),
            # 且現存 15 全部來自舊預設;旗標保證只遷移一次,之後改回 15 不再被動。
            if _pim == 15 and not cfg.get("keepalive_migrated_v1"):
                _pim = 3
                # [codex P1 R5] 遷移值必須【寫進 cfg 再落地】——只改區域變數的話,
                # 檔案存的是 15+旗標,下次啟動旗標擋住遷移 → 永遠退回 15,
                # 5 分鐘閒置登出把常駐 session 整個打掉。
                cfg["poll_interval_minutes"] = 3
                cfg["keepalive_migrated_v1"] = True
                try:
                    atomic_write_json(str(CONFIG_FILE), cfg)
                    logging.info("[migrate] 輪詢間隔 15 分鐘(舊預設)升級為 3 分鐘"
                                 "(常駐 keepalive)")
                except Exception:
                    logging.warning("[migrate] 寫回升級後設定失敗(不影響本次執行)",
                                    exc_info=True)
            cfg["poll_interval_minutes"] = max(2, min(120, _pim))
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


def _consult_id_from_row(row: str) -> str:
    """一列清單 → 這【一張會診單】的識別字串。純函式。

    ★[2026-08-04 外審 P1-04]★ 原本整份清單只取「病歷號集合」，那只能回答
    「這位病人在不在清單上」，回答不了「這是不是同一位病人的【另一張】會診」。
    同一病人的第二張會診會被集合吸收掉 —— `{"12345678", "12345678"}` 就是
    `{"12345678"}` —— 結果是★漏寄★，臨床上最糟的方向。

    識別取 `病歷號|日期|時間`。缺日期或時間時退回只用病歷號（＝舊行為），
    所以鑑別力只會【變好或持平】，不會變差。

    ★不採納外審建議把病房/床號放進識別★：病人轉床很常見，而轉床【不是新的
    會診】。把床號放進去會在每次轉床多寄一封（誤寄）。真正屬於「這張會診單」
    的是【申請時間】—— 它在轉床時不變，在新開一張時必然不同。

    ★解析不到結構的列由呼叫端處理★，這裡回空字串。原因見
    `_consult_signature_from_roster`：那條路必須保留「掃出該列【全部】病歷號」
    的舊行為 —— 實機上會有整塊文字被當成一列傳進來的情況，只取第一個會把
    後面的會診整個丟掉（★漏寄★，比識別粒度不夠更嚴重）。
    """
    row = (row or "").strip()
    m = _ROSTER_ROW_RE.fullmatch(row)
    if not m or not m.group("chart"):
        return ""
    chart = m.group("chart")
    date, tm = m.group("date"), m.group("time")
    return f"{chart}|{date}|{tm}" if (date and tm) else chart


def _chart_of_consult_id(consult_id: str) -> str:
    """從識別字串取回病歷號（升級時要拿它跟舊基準比對）。純函式。

    [2026-08-05 外審第 6 輪 P1-01] 同一識別的第 2 份會帶 "#n" 序號
    (見 `_with_occurrence_suffixes`),取病歷號時要把它剝掉。
    """
    head = (consult_id or "").split("|", 1)[0]
    return head.split("#", 1)[0]


def _with_occurrence_suffixes(ids: list) -> set:
    """[2026-08-05 外審第 6 輪 P1-01] 同一識別出現第 2 次起加 "#序號"。

    ★兩列一模一樣的會診,集合不可以把第二張吸收掉★
    上一批把 radio 的去重從「文字」改成「控制項」,第二列已經能活到這裡 ——
    但這裡回傳 `set`,`{"A|8/5|10:30", "A|8/5|10:30"}` 仍然只剩一筆,
    `_new_consult_ids` 的相減就看不到第二張 → 漏寄,而且無聲。

    ★為什麼是「每個識別自己數」而不是「清單位置」★
    位置相依的 occurrence 在任何一張會診被回覆離開清單時全體位移 → 整份誤判成
    「新的」→ 重寄整份。以識別字串為鍵各自計數,別張會診的來去不影響這一張:
        1 張 → {"A"}          2 張 → {"A", "A#2"}
    第二張出現 = 差集多出 "A#2" = 一張新會診;回到 1 張 = 差集為空,剪枝自然收斂。
    檔案格式仍是字串集合,舊基準不需要遷移。
    """
    counts: dict = {}
    out = set()
    for cid in ids:
        n = counts.get(cid, 0) + 1
        counts[cid] = n
        out.add(cid if n == 1 else f"{cid}#{n}")
    return out


def _consult_signature_from_roster(roster_texts) -> set:
    """[CQ-02] 只從病人清單列(roster_texts)取識別集合,不看病情摘要內文。

    逐列交給 `_consult_id_from_row` → 每張會診單一個識別(`病歷號|日期|時間`,
    缺日期/時間時退回只用病歷號)。解析不到結構的列(外籍病人無中文姓名等)退回
    掃該「單列」的 6+ 位數字(清單列只有病歷號是 6+ 位、日期是 M/D 不會誤中,故安全)。
    roster_texts=None(擷取失敗/停用) → 回空集合(呼叫端另以 None 走 fail-open,不更新基準)。

    ★[2026-08-04 外審 P1-04] 這裡以前只回病歷號★ —— 同一病人的第二張會診會被
    集合吸收掉而漏寄。詳見 `_consult_id_from_row`。"""
    ids: list = []
    for row in (roster_texts or []):
        cid = _consult_id_from_row(row)
        if cid:
            ids.append(cid)
        else:
            # ★解析不到 → 保留舊行為:掃出該列【全部】病歷號★
            #   實機上會有整塊文字被當成一列傳進來(見
            #   test_poll_first_startup_builds_baseline_silently 的 fixture)，
            #   只取第一個會把後面的會診整個丟掉 —— 那是漏寄，比識別粒度不夠
            #   更嚴重。這條路沒有日期/時間可用，維持病歷號粒度。
            ids.extend(_CHART_RE.findall((row or "").strip()))
    # ★[外審第 6 輪 P1-01] 重複的識別以 "#n" 存活★(見 _with_occurrence_suffixes)
    return _with_occurrence_suffixes(ids)


# 行程內的權威基準:即使檔案寫入失敗(磁碟滿/權限),記憶體仍記得已通知過誰 → 下一輪 poll 不會
# 重寄同一批(Codex 指出:只靠檔案、寫失敗會每 15 分鐘狂寄)。檔案只負責「跨重啟」記憶;單一
# job 互斥(_consult_job_gate)→ 同時只有一個 _do_full_job 在跑,無並發競爭。None = 本行程尚未載入。
_notified_memory = None
# 基準是否「曾經建立過」。用來區分「空集合(沒人未回覆,但已建過基準)」與「從沒建過基準」——
# 後者(第一次啟動/檔案不存在)第一輪 poll 只建基準、不寄,避免重啟收一封全清單。None=尚未載入。
_notified_initialized = None
# 目前記憶體裡的基準是否來自【舊格式】(只有病歷號、沒有 ids)。見 `_new_consult_ids`。
_notified_is_legacy = False
# 上一次讀基準檔的結果:"ok" | "missing" | "corrupt" | "error"(見 safe_load_json_ex)。
# ★[2026-08-04 外審 P1-05] 這四種不可以混為一談★ 見 `_baseline_absence_reason`。
_notified_load_status = None

# 「這台機器成功建立過基準」的標記。★不是啟動時寫,是第一次成功存基準之後才寫★
#   啟動時寫的話,真正的首次安裝在第一輪 poll 就已經有標記,會被誤判成「基準遺失」
#   而寄出整份清單 —— 首次安裝寄整份正是當初要避免的事。
_INSTALL_MARKER = SETTINGS_DIR / "consult_baseline_established.json"


def _mark_baseline_established() -> bool:
    """留下「這台機器建立過基準」的痕跡。回傳是否【確定落地】。

    ★[2026-08-08 外審] 成敗要說出來★ 舊版把例外吞掉、回 None。
    但這個標記是「基準遺失」與「首次安裝」的唯一分辨依據 —— 它沒寫成,
    之後基準檔一旦遺失就會被判成 `first_install`,而 first_install 的處理是
    【把當下所有未回覆會診靜默記成已通知然後不寄】。那批會診從此沒有人
    收到通知,也沒有任何跡象。
    """
    try:
        if _INSTALL_MARKER.exists():
            return True
        atomic_write_json(str(_INSTALL_MARKER),
                          {"first_established": datetime.now().isoformat()})
        return True
    except Exception:
        logging.warning("[會診] 基準標記寫入失敗 —— 若基準檔之後遺失,"
                        "會被誤判成首次安裝而靜默吞掉現有會診", exc_info=True)
        return False


def _baseline_absence_reason() -> str:
    """基準不在時，這到底是什麼情況。→
        "first_install"            真的第一次跑(沒有標記、檔案也不存在)
        "missing_after_prior_run"  建立過基準，但檔案不見了
        "corrupt"                  檔案在但內容壞掉
        "read_error"               讀不到(權限/防毒暫時鎖住) —— ★原檔通常還在★

    ★[2026-08-04 外審 P1-05]★ 原本這四種全部走同一條路:第一輪 poll 靜默把
    當下【所有】未回覆會診標成「已通知」然後 return。對真正的首次安裝那是對的
    (避免每次重裝收一封全清單)，但對後三種就是★把現有會診整批靜默吞掉★ ——
    那些會診從此不會有人收到通知，而且沒有任何跡象。
    """
    if _notified_load_status == "corrupt":
        return "corrupt"
    if _notified_load_status == "error":
        return "read_error"
    if _INSTALL_MARKER.exists():
        return "missing_after_prior_run"
    # ★[2026-08-08 外審第 4 回] 不要用「產物痕跡」去猜★
    #   我前兩版試圖從 log／截圖目錄／去重檔推斷「這台機器跑過沒有」。
    #   那個啟發式在【兩個方向】都會錯:
    #     * `--configure` 開個設定就留下 log → 全新機器被誤判成跑過;
    #     * email 觸發會建出去重檔與截圖,但它【明確不更新團隊基準】
    #       → 沒有基準卻被當成有過基準。
    #   基準到底建立過沒有,只有那個標記說得準。標記寫不成的補救不在這裡,
    #   而在【建立基準的那一刻就不要宣稱成功】(見下面 `first_install` 的處理)。
    return "first_install"


def _load_notified() -> set:
    """讀「已通知過的病歷號」基準。行程內已有記憶體值就用它(權威,不受檔案寫入失敗影響);
    否則(剛啟動)從 SETTINGS_DIR/consult_notified.json 載入。失敗回空集合。"""
    global _notified_memory, _notified_initialized
    if _notified_memory is not None:
        return set(_notified_memory)
    global _notified_is_legacy
    try:
        data, _status = safe_load_json_ex(str(_NOTIFIED_FILE), default=None)
        global _notified_load_status
        _notified_load_status = _status
        if isinstance(data, dict):
            # ★[2026-08-04 外審 P1-04] 舊基準只有病歷號★
            #   識別改成「病歷號|日期|時間」之後，若拿新識別去跟舊基準做集合相減，
            #   升級當下【每一張既有會診都會變成「新的」】→ 整份重寄。
            #   所以認得舊格式，並在下面的比對降級成病歷號粒度一輪；
            #   `_save_notified` 一寫就是新格式，之後就恢復完整鑑別力。
            ids = data.get("ids")
            if ids is not None:
                _notified_memory = {str(x) for x in ids}
                _notified_is_legacy = False
            else:
                _notified_memory = {str(x) for x in (data.get("charts") or [])}
                _notified_is_legacy = True
            # [Codex] 檔案存在且是合法 dict → 先前已建過基準(即使 charts 為空,也代表「已建、
            # 目前沒人未回覆」而非從沒建過)。只有「檔案不存在 / 壞掉」才算從沒建過 → 第一輪 poll
            # 才靜默建基準。避免升級(舊版只有 charts、甚至空 charts)後把當下新會診靜默吞掉漏寄。
            _notified_initialized = True
            return set(_notified_memory)
    except Exception:
        logging.debug("讀取 consult_notified 失敗", exc_info=True)
    _notified_memory = set()
    _notified_initialized = False
    _notified_is_legacy = False
    return set()


def _new_consult_ids(current_ids: set) -> set:
    """目前清單裡【還沒通知過】的會診 → 集合。

    一般情況就是集合相減。基準還是舊格式(只有病歷號)時降級成病歷號粒度比對 ——
    ★升級那一輪絕不可以把既有會診全部當成新的整份重寄★。代價是：升級當下就已經
    存在的「同一病人第二張會診」這一輪不會被認出來（它本來就已經通知過了），
    下一次 `_save_notified` 之後恢復完整鑑別力。
    """
    base = _load_notified()
    if not _notified_is_legacy:
        return set(current_ids) - base
    known_charts = {_chart_of_consult_id(x) for x in base}
    fresh = {i for i in current_ids if _chart_of_consult_id(i) not in known_charts}
    if fresh != (set(current_ids) - base):
        logging.info("[會診] 基準仍是舊格式(只有病歷號) → 本輪以病歷號粒度比對，"
                     "避免升級後整份重寄；寫入後即恢復完整鑑別力")
    return fresh


def _baseline_initialized() -> bool:
    """基準是否曾經建立過(檔案有 initialized=true / 本行程已 _save_notified 過)。
    False 代表第一次啟動還沒建基準 → 第一輪 poll 只建基準、不寄(避免重啟收全清單)。"""
    global _notified_initialized
    if _notified_initialized is None:
        _load_notified()   # 順帶載入 _notified_initialized
    return bool(_notified_initialized)


def _save_notified_if_eligible(roster_texts, ids: set, *, reason: str) -> bool:
    """★基準的唯一合法入口★(2026-08-05 外審第 6 輪 P2-01)

    上一批加了 `_may_update_baseline`,但只守住「寄信成功後」那一個寫入點;
    「首次安裝建基準」與「無新會診剪枝」兩條路仍直接呼叫 `_save_notified` ——
    一份【沒被確認過】的清單照樣能建立/剪出基準:
      * 首次安裝:過期清單建的基準少一筆沒關係(下一輪會補寄),但語意上
        不該用沒確認的資料「宣稱這些都處理過了」
      * 剪枝:用過期的短清單剪 → 還在清單上的會診被剪掉 → 之後又變「新」→ 重寄
    """
    if not _may_update_baseline(roster_texts):
        logging.warning("[會診] 本輪清單未經回讀確認 → 不%s(下一輪重新比對)", reason)
        return False
    _save_notified(ids)
    return True


def _save_notified(charts: set) -> None:
    """把「目前清單的會診識別」設為已通知基準(寄信成功後呼叫;poll/email/手動皆更新)。
    【先更新記憶體(權威)再寫檔】→ 即使寫檔失敗,本行程後續 poll 也絕不重寄同一批;檔案僅供跨重啟。

    寫出 `ids`(新格式,`病歷號|日期|時間`)與 `charts`(只有病歷號)兩份:
    `charts` 是給【舊版程式】看的 —— 診間電腦不是同時更新的,新版寫的檔可能被還沒
    更新的舊版讀到。舊版只認得 `charts`,少了它會把整份清單當成新會診重寄。
    """
    global _notified_memory, _notified_initialized, _notified_is_legacy
    _notified_memory = set(charts)
    _notified_initialized = True
    _notified_is_legacy = False        # 寫下去的就是新格式
    saved = False
    try:
        atomic_write_json(str(_NOTIFIED_FILE),
                          {"ids": sorted(charts),
                           "charts": sorted(
                               {_chart_of_consult_id(x) for x in charts}),
                           "initialized": True})
        saved = True
    except Exception:
        logging.warning("寫入 consult_notified 失敗(記憶體已記住,本行程不會重寄)", exc_info=True)
    if saved:
        # ★寫成功【之後】才留標記★（順序不可以反過來）
        #   反過來的話:標記寫了、基準檔卻沒寫成 → 下次啟動看到「標記在、檔案不在」
        #   → 判成基準遺失 → 白白對團隊重寄整份清單並發告警。那是自己製造的假警報。
        #   另外包一層:標記只是輔助,它出事不可以把已經存好的基準這條主線也拖垮。
        # ★標記出事不可以把已經存好的基準這條主線拖垮★
        #   (helper 自己已經吞例外並回 False,這裡再包一層是防它日後被改壞 ——
        #    第一版拿掉這層保護,測試立刻抓到主線被拖垮。)
        try:
            _marked = _mark_baseline_established()
        except Exception:
            logging.warning("[會診] 基準標記寫入拋出例外(主線不受影響)",
                            exc_info=True)
            _marked = False
        if not _marked:
            # 標記沒落地 → 下次基準檔一旦遺失就會被誤判成首次安裝。
            # 這裡只能記錄與告警(基準本身已經存好了,不該回滾)。
            logging.error("[會診] 基準已存檔,但「建立過基準」的標記沒能寫入 —— "
                          "請確認 settings 目錄可寫;否則基準檔遺失時會被當成"
                          "首次安裝而靜默吞掉一批會診")


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
            if (p.info["name"] or "").lower() == SYSTEMFTP_EXE_NAME:
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


def type_via_focus(edit_hwnd: int, top_hwnd: int, text: str) -> bool:
    """讓 Delphi TEditExt 真正取得鍵盤焦點，再逐字 PostMessage WM_CHAR。

    隱藏桌面上 SetForegroundWindow 經常失敗、SetFocus 跟著失敗 → 帳密沒打進去
    → 登入失敗 → 等不到主畫面。本版採三層保險：
      (1) PostMessage WM_LBUTTONDOWN/UP 給欄位 → Delphi 的 OnClick 會自動把
          焦點搶到該欄位（不動真實滑鼠，因為是直接送訊息給控制項，不經過
          系統 cursor）。對 Delphi 自訂編輯框最可靠。
      (2) SetForegroundWindow + SetFocus 最多重試 5 次並驗證 GetFocus。
      (3) WM_CHAR 逐字輸入。

    → 回傳「焦點有沒有被確認落在該欄位」。★這是登入失敗時的第二個關鍵證據★
    (2026-08-10 實機):HIS 跳出錯誤對話框 = 帳密真的送出去而被拒絕;
    沒有任何對話框而登入視窗還在 = 很可能字根本沒打進欄位。
    兩者的處置完全不同,而舊版把這個訊號只寫進 log、沒有帶進告警信。"""
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
        return focus_ok
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


MENU_CAPTION_EXPECTED = "我的會診清單"
MF_BYPOSITION = 0x00000400        # GetMenuStringW 以「位置」而非命令 ID 取項目


def _normalize_menu_caption(text: str) -> str:
    """正規化選單標題：去掉助記符 & 與快捷鍵欄(Tab 之後)、去空白。
    例：'我的會診清單(&M)\\tCtrl+M' → '我的會診清單'。"""
    s = str(text or "").split("\t")[0]
    s = re.sub(r"\(&.\)", "", s).replace("&", "")
    return s.strip()


def _menu_caption_at(sub_menu: int, idx: int) -> str:
    """讀取選單項標題（正規化後）；取不到回空字串。純 Win32 查詢,不改任何狀態。

    用 ctypes 直呼 user32.GetMenuStringW —— pywin32 的 win32gui **沒有** GetMenuString
    （第一版誤用,會永遠 AttributeError → 永遠回 False → 選單真的位移時也永遠救不回來）。"""
    try:
        buf = ctypes.create_unicode_buffer(256)
        n = ctypes.windll.user32.GetMenuStringW(
            ctypes.c_void_p(int(sub_menu)), ctypes.c_uint(int(idx)),
            buf, ctypes.c_int(len(buf)), ctypes.c_uint(MF_BYPOSITION))
        return _normalize_menu_caption(buf.value) if n > 0 else ""
    except Exception:
        logging.debug("讀取選單標題失敗（視為取不到）", exc_info=True)
        return ""


def _find_menu_id_by_caption(sub_menu: int, caption: str) -> "int | None":
    """[codex] 在子選單裡【依確切標題】找命令 ID。

    只比對「含『會診』」是不夠的：院方若插入『全部會診清單』『會診回覆』等項目,
    位移後的項目照樣含「會診」→ 會把【別的命令】送進住院醫囑系統。改為要求標題
    正規化後與 MENU_CAPTION_EXPECTED 完全相同,並直接以標題定位(不再信任位置索引)。"""
    try:
        count = ctypes.windll.user32.GetMenuItemCount(
            ctypes.c_void_p(int(sub_menu)))
        for i in range(max(0, int(count))):
            if _menu_caption_at(sub_menu, i) == caption:
                cmd = win32gui.GetMenuItemID(sub_menu, i)
                if cmd and cmd != -1:
                    return cmd
    except Exception:
        logging.debug("依標題尋找選單項失敗", exc_info=True)
    return None


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
                        # [2026-07-25 審查] 走訪結果與預期不同 → 多半是院方在選單插了
                        # 一項而位置索引 (4,8,0) 位移。此 ID 會被 PostMessage 送進
                        # 【住院醫囑系統】,送錯等於在醫囑程式裡按下不明功能表項目。
                        # [codex] 位置已不可信 → 改【依確切標題】重新定位;找不到就放棄
                        # 本次(呼叫端會重試/告警),絕不硬送未知命令。
                        by_caption = _find_menu_id_by_caption(
                            sub, MENU_CAPTION_EXPECTED)
                        if by_caption is None:
                            logging.error(
                                "選單走訪得到非預期 ID %s(預設 %s),且在該子選單中找不到"
                                "標題為「%s」的項目 → 疑似院方改版,本次不送出選單命令",
                                cmd_id, MENU_ID_EXPECTED, MENU_CAPTION_EXPECTED)
                            return None
                        logging.info(
                            "選單位置疑似位移(位置 ID=%s),改依標題「%s」定位 → ID %s",
                            cmd_id, MENU_CAPTION_EXPECTED, by_caption)
                        return by_caption
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


# roster 穩定性判定的參數。★[2026-08-04 外審 P1-03]★
#   `_query_cycle` 只 `time.sleep(1.8)` 就當清單載入完了。Delphi 視窗是先建立、
#   資料再逐步填進去的,所以那一秒八【不保證】看到的是最終狀態:
#     * 還沒載入 → 空清單被當成「成功且真的沒有病人」→ 基準被剪成空
#                  → 下一輪所有既有會診都變「新」→ 對團隊重寄整份清單
#     * 載入到一半 → partial roster 被存成基準 → 還沒出現的病人此後不算新 → 漏寄
#   固定睡多久都治不了(慢的機器仍會失手),要看的是【內容有沒有還在變】。
_ROSTER_SETTLE_READS = 3        # 連續幾次讀到一樣才算穩定
_ROSTER_SETTLE_INTERVAL = 0.25  # 每次取樣間隔(秒)
_ROSTER_SETTLE_TIMEOUT = 6.0    # 一直在變就放棄(秒);放棄=回報「判斷不了」
# ★空清單要多觀察一段時間才能算數★(2026-08-04 外審第 3 輪 P1-01)
#   「沒有變」不等於「載入完成」——【空】剛好也是還沒開始載入的樣子。連續三次
#   讀到空只要 0.5 秒就達成,若 HIS 在 1.5 秒才開始填,那 0.5 秒的「穩定空清單」
#   會被當成「今天真的沒有會診」→ 基準被剪成空 → 下一輪整份重寄。
#   有內容的清單不需要這道關(資料都出現了,不可能是「還沒載入」)。
_ROSTER_EMPTY_MIN_OBSERVE = 3.0
# ★有內容的清單也要觀察一段時間★(2026-08-05 外審第 4 輪 P1-07)
#   上一版的理由寫著「有內容就不必等,資料都出現了,不可能是【還沒載入】」——
#   ★那句話只對「完全沒載入」成立,對【載到一半】完全不成立★。Delphi 是逐列填的,
#   4 位病人只填出 2 位、然後停頓超過 0.5 秒(慢機器/後端慢很常見),就會拿 2 位那份
#   當成最終清單存進基準 → 另外 2 位此後永遠不算「新」→ ★漏寄★。
#   而「漏寄」正是本檔開頭第 1243 行自己列出的失敗模式 —— 程式卻不防它。
#   1.5 秒不是白等:它取代了截圖前那個固定的 `time.sleep(1.8)`(見
#   `_capture_with_settled_roster`),整輪的實際耗時反而略短。
_ROSTER_MIN_OBSERVE = 1.5


def _read_roster_snapshot(consult_hwnd: int) -> tuple:
    """列舉【一次】→ (children, radios, texts)。三者必然同源。

    ★[2026-08-05 外審第 4 輪 P1-08]★ 以前清單文字與 radio 控制項是【兩次】列舉
    的結果(穩定判定一次、擷取前再一次)。中間長出一位病人時:信裡列 N 位、
    逐病人內文卻是 N±1 位,基準也只存到其中一份。改成一次列舉、三個結果同源,
    「不一致」在結構上就不可能發生,而不是靠事後再比對一次。
    """
    children = enum_children(consult_hwnd)
    radios = _find_patient_radios(
        [c for c in children if _is_visible_below(c[0], consult_hwnd)])
    return children, radios, [t for _h, t, _r in radios]


def _read_roster_once(consult_hwnd: int) -> list:
    """讀一次目前的病人清單列 → [文字, ...]。純 IO,不做判定。"""
    return _read_roster_snapshot(consult_hwnd)[2]


class _RosterTexts(list):
    """病人清單列 + 「這份可不可以拿去更新已通知基準」。

    ★[2026-08-05 外審第 5 輪 P1-06]★ 「這封信要不要寄」與「這份清單可不可以
    更新基準」是**兩個不同的問題**,以前共用 `roster_texts is None` 一個通道:
      * None → fail-open 照寄、不更新基準
      * 非 None → 照寄、更新基準
    中間缺了一格:「內容可信、可以寄,但我無法確認它仍代表當下狀態」。
    截圖後回讀失敗就落在這一格 —— 上一版把它當成完全正常(基準照更新),
    等於用一份沒有被確認的清單去宣稱「這些都通知過了」。

    用 list 子類別是刻意的:所有既有消費端(迭代、len、`is None` 判斷)完全不受
    影響,只有真正要決定「能不能更新基準」的那一處會去看這個旗標。
    """

    baseline_eligible = True

    def __init__(self, rows=(), *, baseline_eligible: bool = True):
        super().__init__(rows)
        self.baseline_eligible = baseline_eligible


def _may_update_baseline(roster_texts) -> bool:
    """這份清單可不可以拿去更新「已通知」基準。

    None(擷取失敗/不穩定)→ 不行(既有語意)。
    普通 list(沒有帶旗標)→ 可以(既有語意,不改變任何既有路徑)。
    """
    if roster_texts is None:
        return False
    return bool(getattr(roster_texts, "baseline_eligible", True))


class _RosterSnapshot(tuple):
    """(texts, stable, children, radios) —— 同一次列舉的四個面向。

    刻意是四元組而不是二元組:舊呼叫端寫 `texts, stable = ...` 會當場 ValueError,
    不會靜默地只拿到一半而把 children/radios 留在別的時間點。
    """

    __slots__ = ()

    def __new__(cls, texts, stable, children, radios):
        return super().__new__(cls, (texts, stable, list(children),
                                     list(radios)))

    texts = property(lambda self: self[0])
    stable = property(lambda self: self[1])
    children = property(lambda self: self[2])
    radios = property(lambda self: self[3])

    def as_unstable(self):
        """同一份內容,但標記成「判斷不了」(走 fail-open 通道)。"""
        return _RosterSnapshot(self[0], False, self[2], self[3])

    def as_unverified(self):
        """內容可信、可以寄,但【無法確認】它仍代表截圖那一刻 → 不可更新基準。

        與 `as_unstable` 的差別就是外審第 4 輪那張表的中間那一列:
          穩定且已確認 → 可寄、可更新基準
          確定變過了   → 可寄(走 fail-open 註記)、不可更新基準
          ★無法確認★ → 可寄(內容本身是穩定的)、不可更新基準
        把第三種當成第一種,等於用一份沒被確認的清單宣稱「這些都通知過了」。
        """
        return _RosterSnapshot(
            _RosterTexts(self[0], baseline_eligible=False),
            self[1], self[2], self[3])


def _await_stable_roster(consult_hwnd: int, *, read=None, sleep=None,
                         now=None) -> _RosterSnapshot:
    """等清單不再變動 → `_RosterSnapshot`。

    連續 `_ROSTER_SETTLE_READS` 次讀到完全相同才算穩定。逾時仍在變 → 回
    `stable=False`,呼叫端據此回報「判斷不了」(而不是把當下這份半成品當真)。

    ★空清單也要通過同一道關★:「找不到 radio」與「真的沒有病人」在畫面上長得
    一模一樣,唯一分得出來的線索就是它穩不穩定。所以空清單一樣要連續讀到相同
    才算數 —— 這正是「載入中的空清單把基準剪成空」那條路的堵法。

    ★「不再變動」還要加上「看得夠久」★(外審第 3 輪 P1-01 + 第 4 輪 P1-07)
    「沒有變」不等於「載入完成」——【空】是還沒開始載入的樣子,【半份】是載到
    一半的樣子,兩者都可以連續三次讀到相同(只要 0.5 秒)。所以兩種都要一個最短
    觀察窗:空 3.0 秒、有內容 1.5 秒。

    `read` 回傳 `(children, radios, texts)`(見 `_read_roster_snapshot`);
    `read`/`sleep`/`now` 只給測試注入。
    """
    read = read or _read_roster_snapshot
    sleep = sleep or time.sleep
    now = now or time.monotonic
    started = now()
    deadline = started + _ROSTER_SETTLE_TIMEOUT
    last = None
    same = 0
    # ★[2026-08-05 外審第 5 輪 P1-07] 觀察窗要從【最後一次變動】起算★
    #   上一版是從「進函式」起算:清單在第 1.4 秒才長出最後一位時,第 1.5 秒就
    #   已經滿足「總共觀察了 1.5 秒」——實際上只安靜了 0.1 秒。
    #   「穩定」的意思是「有一段時間沒有再變」,那就該從變動的那一刻起算。
    #   ★誠實說明它不能做到什麼★:清單若在觀察窗【之後】才繼續載入(例:2.1 秒
    #   才出現第 3、4 位),任何以時間為判準的做法都會失手 —— 那需要 HIS 自己的
    #   「查詢完成」訊號(loading 指示、列數標籤、dataset 狀態),目前沒有找到。
    #   這是一道防線,不是「roster readiness 已解決」。
    last_change_at = started
    while True:
        children, radios, cur = read(consult_hwnd)
        if last is not None and cur == last:
            same += 1
            if same >= _ROSTER_SETTLE_READS - 1:
                need = (_ROSTER_MIN_OBSERVE if cur
                        else _ROSTER_EMPTY_MIN_OBSERVE)
                if now() - last_change_at >= need:
                    return _RosterSnapshot(cur, True, children, radios)
        else:
            same = 0
            if last is not None:
                last_change_at = now()
        last = cur
        if now() >= deadline:
            logging.warning("[consult-extract] 病人清單在 %.0f 秒內一直在變動"
                            "(最後看到 %d 列) → 本輪不據此判斷有無新會診",
                            _ROSTER_SETTLE_TIMEOUT, len(cur))
            return _RosterSnapshot(cur, False, children, radios)
        sleep(_ROSTER_SETTLE_INTERVAL)


_CAPTURE_BLANK_RETRIES = 2      # 整張單色(全黑)截圖的重截次數
_CAPTURE_BLANK_WAIT_SEC = 1.0   # 每次重截前給視窗繪製的時間
_SETTLED_RETRY_ROUNDS = 1       # 「截圖後清單又變了」整輪重來的次數


def _image_is_blank(img) -> bool:
    """整張圖每個色版都只有單一個值(全黑/全白/單色) → 零資訊。

    ★[2026-08-06 15:23 實機]★ HIS 忙著向後端要資料時視窗還沒繪製任何內容,
    PrintWindow(WM_PRINT) 印出來就是整張黑 —— 那封「以截圖為準」的信附了
    一張全黑的圖,收信的人什麼都核對不了。單色不可能是真實視窗畫面(至少有
    標題列/邊框/文字)。非 PIL 影像(測試樁)一律視為非單色。
    """
    try:
        extrema = img.getextrema()
        if not extrema:
            return False
        if not isinstance(extrema[0], (tuple, list)):   # 單色版(L)模式
            extrema = (extrema,)
        return all(lo == hi for lo, hi in extrema)
    except Exception:
        return False


def _capture_nonblank(consult_hwnd: int, capture, *, sleep=None):
    """截圖;整張單色就等視窗繪製後重截(最多 _CAPTURE_BLANK_RETRIES 次)。

    ★仍為 fail-open★ 重截用盡仍單色時照樣回傳那張圖(有圖可寄勝過沒信),
    但呼叫端必須知道「這張沒有參考價值」—— 見 `_capture_with_settled_roster`
    把它降級為【不可更新基準】(2026-08-06 外審 P1-08)。
    """
    sleep = sleep or time.sleep
    img = capture(consult_hwnd)
    for _ in range(_CAPTURE_BLANK_RETRIES):
        if not _image_is_blank(img):
            return img
        logging.warning("[consult-extract] 截圖整張單色(視窗尚未繪製?) → "
                        "%.0f 秒後重截", _CAPTURE_BLANK_WAIT_SEC)
        sleep(_CAPTURE_BLANK_WAIT_SEC)
        img = capture(consult_hwnd)
    if _image_is_blank(img):
        logging.warning("[consult-extract] 重截 %d 次後截圖仍整張單色 → "
                        "沿用但降級為【不可更新基準】(附圖無參考價值)",
                        _CAPTURE_BLANK_RETRIES)
    return img


def _capture_with_settled_roster(consult_hwnd: int, *, capture=None,
                                 settle=None, read=None, sleep=None):
    """等清單穩定 → 截圖 → 回讀確認清單沒變 → (img, snapshot)。

    ★[2026-08-05 外審第 4 輪 P1-09]★ 截圖以前排在固定的 `time.sleep(1.8)` 之後、
    而清單是在那之【後】才等到穩定的。於是信裡的清單是 Tn 的、附圖是 T0+1.8s 的
    —— 圖上可能少了幾位病人。收信的醫師拿到的是兩份互相矛盾的證據。

    ★截圖不能挪到擷取【之後】★:擷取會逐位點選病人 radio,點完畫面顯示的是
    最後一位的內容,不再是「打開時的原始清單畫面」。所以順序必須是
    【先等穩定 → 再截圖 → 再逐位擷取】。

    截圖後再讀一次:期間又變了 → 這份快照已經不代表圖上那一刻。
    ★[2026-08-06 15:23 實機] 對不上先重來一輪,不要直接認輸★ 後端慢時清單
    可以在空清單觀察窗(3 秒)【之後】才載入(這次 ~6 秒):settle 在 0 列時判
    「穩定」、截圖全黑、回讀才看到 2 列 → 白白寄了一封附全黑截圖的 fail-open
    信,而 3 分鐘後的下一輪其實完全正常。「回讀對不上」= 資料【剛剛已經到了】,
    整輪重來(重新等穩定+重截+重驗)幾乎必然成功;重來仍對不上才標 unstable
    走既有的 fail-open 通道(照寄、但不更新基準)。
    """
    capture = capture or capture_window_image
    settle = settle or _await_stable_roster
    read = read or _read_roster_snapshot

    for round_no in range(_SETTLED_RETRY_ROUNDS + 1):
        snap = settle(consult_hwnd)
        img = _capture_nonblank(consult_hwnd, capture, sleep=sleep)
        # ★[2026-08-06 外審 P1-08] 重截用盡仍是整張單色 → 不可當成正常成功★
        #   附圖零資訊,收信的人什麼都核對不了。舊版只寫一行 warning 就照常回傳,
        #   於是這一輪照樣更新「已通知基準」—— 等於用一張黑圖宣稱「這些都通知過
        #   了」,下一輪就不會再補寄那些病人。改走既有的 as_unverified 通道:
        #   信照寄(有圖總比沒信好),但【不更新基準】,下一輪會重新比對並補寄。
        if _image_is_blank(img):
            logging.warning("[consult-extract] 截圖仍為單色 → 本輪不更新已通知"
                            "基準(下一輪會重新比對並補寄)")
            return img, snap.as_unverified()
        if not snap.stable:
            return img, snap
        try:
            _c, _r, after = read(consult_hwnd)
        except Exception:
            # ★[2026-08-05 外審第 5 輪 P1-06] 讀不到 ≠ 沒有變★
            #   上一版把 `after` 冒充成 `snap.texts`,於是這份【沒有被確認過】的
            #   快照照樣去更新「已通知」基準 —— 期間真的多出一位病人的話,
            #   他會被當成「已經通知過」而從此不再被視為新的 → 漏寄。
            #   現在:內容照寄(它本身是穩定的),但不可以更新基準。
            logging.warning("[consult-extract] 截圖後回讀清單失敗 → "
                            "本輪不更新已通知基準(下一輪會重新比對)",
                            exc_info=True)
            return img, snap.as_unverified()
        if after == snap.texts:
            return img, snap
        if round_no < _SETTLED_RETRY_ROUNDS:
            logging.info("[consult-extract] 截圖後病人清單又變了(%d 列 → "
                         "%d 列)=資料剛載入完成 → 重新等穩定+重截(第 %d 次"
                         "重試)", len(snap.texts), len(after), round_no + 1)
            continue
        logging.warning("[consult-extract] 截圖後病人清單又變了"
                        "(%d 列 → %d 列) → 本輪不據此判斷有無新會診",
                        len(snap.texts), len(after))
        return img, snap.as_unstable()
    raise AssertionError("unreachable")


def _find_patient_radios(children: list) -> list:
    """從控制項樹挑出病人列 → [(hwnd, text, rect)]。純函式以便測試。

    病人 = class 精確為 TRadioButton(排除篩選選項 —— 那些是 TRadioGroup 內的
    TGroupButton,class 不同)且文字帶病人標記結構(床號/房號/病歷號)。以結構
    而非「含中文」判定,外籍病人(無中文姓名)也不會被漏掉。依畫面位置(上→下、
    左→右)排序 = 清單實際顯示順序。呼叫端會先以「在會診視窗子樹中可見」過濾,
    排除非作用分頁的殘留 radio。

    ★[2026-08-05 外審第 5 輪 P1-09] 去重的依據是【控制項】,不是【文字】★
    舊寫法是 `if t in seen: continue` —— 兩個【不同的 radio】只要顯示文字完全
    相同,第二個就會在任何識別邏輯看到它之前被丟掉。而顯示文字相同不代表是同
    一張會診單:同一位病人、同一分鐘由不同科別開的兩張會診,清單上就是兩列
    一模一樣的字。丟掉第二列 = ★漏寄★,而且丟在最上游,下游怎麼修都救不回來。

    去重原本要擋的是「同一個控制項被列舉到兩次」——那用 hwnd 判斷才對,
    而且更精準。"""
    out = []
    seen = set()
    for hwnd, cls, txt, rect in children:
        if cls != _PATIENT_RADIO_CLASS:
            continue
        t = (txt or "").strip()
        if not t or not _PATIENT_LABEL_RE.search(t) or hwnd in seen:
            continue
        seen.add(hwnd)
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
                          roster_label: str = "今日會診病人",
                          settled: _RosterSnapshot | None = None) -> tuple:
    """主入口:從會診視窗擷取逐病人文字。回 (純文字版, HTML內容片段, roster_texts)。

    roster_texts(第三個回傳,CQ-01/02):病人清單「列字串」清單 —— None=擷取失敗/停用
    (無法判斷有沒有新會診 → 呼叫端 fail-open);[]=擷取成功但真的沒病人;[...]=清單列。
    text/html 仍為 best-effort(任何失敗回 ""),但 roster 通道讓 poll 能區分「沒新會診」
    與「解析失敗」,不再把解析失敗誤當「沒新會診」而靜默不寄。"""
    if not cfg.get("extract_text_enabled", True):
        return "", "", None
    try:
        # ★[2026-08-05 外審第 4 輪 P1-08/P1-09] 清單、radio、截圖是同一份快照★
        #   `settled` 由呼叫端在【截圖之前】等到穩定並傳進來(見
        #   `_capture_with_settled_roster`)。沒傳就自己等一次(手動/測試路徑)。
        #   清單文字與 radio 控制項來自【同一次】列舉,不再有「信裡 N 位、
        #   內文 N±1 位」的可能。
        snap = settled if settled is not None else _await_stable_roster(
            consult_hwnd)
        roster_texts, _roster_stable = snap.texts, snap.stable
        children, radios = snap.children, snap.radios
        # 控制項樹 dump(每次執行記一次)：供依實機結構微調 extract_* 參數。
        # [2026-07-25 審查] 只記 class 與座標,**不再記控制項文字**——TRadioButton 的
        # 文字就是「姓名+病房+床號+病歷號」(見 _find_patient_radios 註解),而本函式每
        # ~15 分鐘的輪詢都會跑一次 → 等於把全院會診病人清單持續寫進沒有保存期限的
        # consult_query.log。這個 dump 當初只是為了「依實機結構微調參數」,結構資訊
        # (class/尺寸/位置)就足夠,病人識別資料不需要也不該留在這裡。
        logging.info(
            "[consult-extract] 控制項樹(%d 個): %s",
            len(children),
            " | ".join(
                f"{cls}@({r[0]},{r[1]},{r[2]-r[0]}x{r[3]-r[1]})"
                + (f" len={len(txt)}" if txt else "")
                for _h, cls, txt, r in children[:80]))

        # 病人清單 = TRadioButton 文字(最準確,直接來自 UI,免 OCR/像素點選)。
        # 以「在會診視窗子樹中可見」過濾,排除非作用分頁的殘留 radio(在
        # `_read_roster_snapshot` 裡做)。此判定不看會診視窗本身是否被 SW_HIDE,
        # 故隱藏桌面/正常/後備三種模式皆正確 —— 作用分頁真的沒病人時清單即為空,
        # 不會誤把其他分頁的隱藏 radio 當成今日病人。
        roster = _format_patient_roster(roster_texts, label=roster_label)
        roster_html = _format_patient_roster_html(roster_texts, roster_label)
        if not _roster_stable:
            # ★顯示用與判斷用要分開★ 看到什麼照樣附在信裡(上面兩行已經用過了)，
            #   但「有沒有新會診」這個判斷不能拿一份還在變的清單去做。
            #   None = 既有的「判斷不了」通道 → fail-open 照寄、不更新基準。
            roster_texts = None

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


def _validated_systemftp_pids(pids: set) -> set:
    """[2026-07-25 審查/codex] 從候選 PID 篩出「確實是本 session 的 systemftp」。

    pids 是流程早期(登入視窗出現時)的快照,最長可能在 ~5 分鐘後才被使用：
      ① PID 重用：該行程若已結束、Windows 把 PID 回收給別的程式 → 不可動它。
      ② 跨 session：共用/RDS 診間機上別的使用者也可能開著住院醫囑系統,他的 PID 不在
         before 快照裡 → 會被誤判成「本次新增」。
    取不到自己的 session 時【fail-closed】(整批不動),與 _cleanup_orphan_systemftp 一致
    —— 寧可留下孤兒(下輪清掃會處理),也不要關掉別人的醫囑系統。"""
    if not pids:
        return set()
    my_session = _pid_session(os.getpid())
    if my_session is None:
        logging.warning("[cleanup] 取不到本行程 session → 不動任何行程(fail-closed)")
        return set()
    out = set()
    for pid in pids:
        try:
            p = psutil.Process(pid)
            if (p.name() or "").lower() != SYSTEMFTP_EXE_NAME:
                logging.info("[cleanup] pid %s 已非 systemftp(PID 重用?),略過", pid)
                continue
            if _pid_session(pid) != my_session:
                logging.info("[cleanup] pid %s 屬其他登入 session,略過", pid)
                continue
            out.add(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            logging.debug("[cleanup] 驗證 pid %s 失敗(略過)", pid, exc_info=True)
    return out


def close_pids(pids: set, grace: float = 2.5) -> None:
    """關閉指定行程：先對其視窗送 WM_CLOSE，逾時再強制結束。

    [codex] 驗證必須在【送 WM_CLOSE 之前】：舊版先對快照 PID 的所有視窗送 WM_CLOSE,
    才在 terminate 前檢查身分 → 別人的住院醫囑系統早就被關掉了(WM_CLOSE 也是關閉)。"""
    pids = _validated_systemftp_pids(pids)
    if not pids:
        return
    # ★[2026-08-05 外審第 5 輪 P2-06] 驗證與動手之間 PID 會被回收★
    #   `_validated_systemftp_pids` 驗完之後,下面兩個動作(WM_CLOSE、terminate)
    #   都是【之後】才發生的。中間該行程若結束、PID 被 Windows 發給另一支
    #   systemftp(很可能就是醫師剛開的),我們就會關掉他的程式。
    #   舊版只在 terminate 前補驗【名稱】—— 而回收給同一支程式時名稱一樣。
    #   把「建立時間」一起記下來當指紋,動手前重驗一次。
    fingerprints = {pid: _process_started_at(pid) for pid in pids}

    def _still_the_same(pid) -> bool:
        want = fingerprints.get(pid)
        if want is None:
            return True                    # 當初就讀不到 → 不採用這個訊號
        now = _process_started_at(pid)
        return now is not None and abs(now - want) <= 1.0

    live = {pid for pid in pids if _still_the_same(pid)}
    for hwnd in find_windows(pids=live, visible_only=False):
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        if not (_systemftp_pids() & pids):
            return
        time.sleep(0.3)
    for pid in pids:                       # 已由 _validated_systemftp_pids 驗證過
        try:
            p = psutil.Process(pid)
            if (p.name() or "").lower() != SYSTEMFTP_EXE_NAME:
                continue                   # grace 期間才被回收的極端情況,再擋一次
            if not _still_the_same(pid):
                logging.warning("[cleanup] pid %s 在等待期間已換成別的行程 → 不動它",
                                pid)
                continue                   # ★同名不代表同一個★
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        except Exception:
            logging.debug("terminate pid %s 失敗", pid, exc_info=True)


def _cleanup_pids_excluding_borrowed(our_pids: set, before: set,
                                     borrowed: bool, root_pid=None) -> set:
    """[review C2 fix 2026-06-12] SW_HIDE 後備模式收尾要關哪些 pid。

    borrowed=True(本次沒有出現新登入視窗、借用了「啟動前就存在」的實例 ——
    那可能是使用者自己開著的住院系統)時，排除 before 內的 pid：流程可以借
    它完成截圖，但收尾絕不可替使用者關掉他的程式。一般情況(borrowed=False)
    維持原行為，關掉本次開啟的全部實例。

    ★[2026-08-04 外審 P1-01] `borrowed` 這個開關不足以封閉所有權★
    它只擋掉「啟動前就存在」的行程。可是冷啟動要等最多 120 秒 —— 醫師在這
    段期間【新開】的住院系統不在 `before` 裡，於是 borrowed=False，那個行程
    照樣會被關掉。`before` 快照只認得「更早以前」，認不得「剛剛才開」。

    有 `root_pid`（本次真正 spawn 出來的行程）時改用它做所有權驗證；
    沒有的話維持舊行為（呼叫端還沒留住 handle 時的過渡）。
    """
    pids = set(our_pids)
    if borrowed:
        pids -= set(before)
    if root_pid is not None:
        pids = _verified_owned_pids(root_pid, pids)
    return pids


# =============================================================================
# 隱藏桌面（systemftp 完全在使用者看不到的虛擬桌面上跑，零干擾）
# =============================================================================
# ══ 資源耗盡 → 閒置重開機（2026-08-11 使用者定案）═══════════════════════
#
# 建不出隱藏桌面幾乎只有兩個原因，而它們的處置完全相反：
#   * ★USER object 配額被耗光★ —— 累積出來的，★重開機真的會修好★；
#   * 群組原則/權限不允許建立桌面 —— 永久性的，重開一百次也一樣。
# 分辨方法不必猜：★本行程之內曾經成功過嗎★。曾經成功、現在連續失敗
# ＝耗盡；從頭到尾沒成功過＝那台就是不給建，重開機只會變成每天無謂重開一次
# （`bde_reboot_decision` 的 24 小時上限擋得住迴圈，但擋不住「每天一次」）。
# 分辨不出來時保守不重開。
#
# 現況（不加這段的話）：建不出來就降級到 SW_HIDE ——「每輪完整登入」，
# 那正是登入鎖定門檻的來源；查詢還在跑，所以既有告警也不會響。
# 換句話說這是一個會自己惡化、而且沒有人會發現的狀態。
_HIDDEN_DESKTOP_FAIL_STREAK_MAX = 3       # 連續幾輪建不出來才算數
_hidden_desktop_state = {"streak": 0, "ever_ok": False}
_hidden_desktop_lock = threading.Lock()


def _hidden_desktop_exhausted() -> bool:
    """★重開機前再驗一次★ —— 現在還是建不出來嗎（順手把 handle 收掉）。

    看守可能在半夜開火，而最後一次觀測可能是幾小時前（休息時段不輪詢）。
    「在動手的那一刻確認條件仍然成立」比「相信一個舊結論」重要。

    ★[外審 SH 第 1 輪 P2-3] 建得起來就要【當成一次恢復】★
    上一版只是回 False 就走人，沒有重置 streak —— 於是那一波故障之後的
    每一次失敗都從 4、5、6 往上加，再也不會等於門檻，★整段故障期間永遠
    不會重新排定重開機★。恢復就是恢復，走同一條路。
    """
    h = _ensure_hidden_desktop()
    if not h:
        return True
    try:
        _user32.CloseDesktop(h)
    except Exception:
        logging.debug("CloseDesktop(recheck) 失敗", exc_info=True)
    _note_hidden_desktop_ok()
    return False


def _note_hidden_desktop_ok() -> None:
    """隱藏桌面（又）建得起來 → 重置 streak，並結掉 RESOURCE 這個原因。"""
    with _hidden_desktop_lock:
        _hidden_desktop_state["ever_ok"] = True
        had = _hidden_desktop_state["streak"]
        _hidden_desktop_state["streak"] = 0
    if had:
        logging.info("[資源] 隱藏桌面又建得起來了(先前連續失敗 %d 次)", had)
    # ★只結掉自己這一個原因★(外審 SH 第 1 輪 P2-2):共用的取消令若在這裡
    #   無條件 set,會把一個【還沒好】的 BDE 看守一起解除掉。
    _clear_reboot_reason("RESOURCE")


def _note_hidden_desktop_result(ok: bool) -> None:
    """記下這一次建隱藏桌面的結果；連續失敗到門檻就排重開機看守。"""
    if ok:
        _note_hidden_desktop_ok()
        return
    with _hidden_desktop_lock:
        _hidden_desktop_state["streak"] += 1
        streak = _hidden_desktop_state["streak"]
        ever_ok = _hidden_desktop_state["ever_ok"]
        # ★決定要不要重開機的地方【只有這一行】★
        #   我第一版把「曾經成功過」寫成兩道(這裡一道、下面再一道 early
        #   return)。突變驗證當場量出來:拿掉其中一道,測試【全綠】——
        #   兩道守衛表達同一件事,就沒有任何測試真的在守它。
        # ★`>=` 而不是 `==`★(外審 SH 第 1 輪 P3):看守可能因為別的理由退場
        #   (24 小時 give_up、shutdown 被拒),那時 streak 早就超過門檻 ——
        #   用 `==` 的話這個事故從此再也沒有人看著。排程本身是冪等的
        #   (已在站崗就只是續命,不會重複開緒、也不會重複刷 log)。
        arm = ever_ok and streak >= _HIDDEN_DESKTOP_FAIL_STREAK_MAX
    if ever_ok:
        logging.error(
            "[資源] ★建不出隱藏桌面,連續第 %d 次(本行程先前成功過)★ → "
            "判為 USER object 配額耗盡。後備模式是【每輪完整登入】,"
            "那正是登入鎖定門檻的來源,而查詢還在跑所以不會有人發現", streak)
    else:
        # 從來沒成功過 → 多半是群組原則,重開機修不了 → 只降級,不重開。
        logging.warning(
            "[資源] 建不出隱藏桌面(第 %d 次;本行程從未成功過 → 判為權限/原則"
            "限制,不自動重開機),本輪走 SW_HIDE 後備", streak)
    if arm:
        _schedule_reboot_watch(
            "RESOURCE",
            f"連續 {_HIDDEN_DESKTOP_FAIL_STREAK_MAX} 輪建不出隱藏桌面"
            "(USER object 配額耗盡;重開機可修復)")


def _ensure_hidden_desktop():
    """建立或開啟隱藏桌面；回傳 HDESK（整數位址）或 None 表失敗。

    ★不在這裡記結果★:本函式自己被 `_hidden_desktop_exhausted()`(重開機前的
    再驗)呼叫,在那裡記一次會把 streak 重複累加。記錄由真正的使用端負責。
    """
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


# ★[2026-08-10 批次SC #2] 上一條被放生的隱藏桌面 worker★
#
# 240 秒的 `join()` 到期只是【放棄等待】—— worker 本身沒有被取消:凍結的
# Delphi/systemftp 讓 raw `GetWindowText()` / `EnumWindows` callback 永久
# 不返回,那條 daemon thread 與它的 HDESK 就一直活著(`CloseDesktop` 只有
# worker 自己走到 finally 才會執行)。
#
# 沒有 single-flight 的話:每次重試或下一輪查詢都再開一條
# `ConsultAutomationHidden` + 新的 desktop handle + 新的 systemftp 自動化。
# 持續故障下會累積 daemon threads、HDESK/視窗資源與互相衝突的 HIS session,
# 最後會診輪詢與整個 Win32 GUI 子系統都可能失效。
#
# 對策與 `_run_imap_check_with_timeout` 完全相同:記住上一條;它還活著就
# 【不再疊加】,直接回報本輪失敗(排程下一輪會再試;它自己的迴圈 deadline
# 到了會結束並釋放 HDESK)。只由持有 `_flow_lock` 的那條緒讀寫,無並發。
_last_hidden_worker = None

#: 隱藏桌面 worker 的硬上限(秒)。
_HIDDEN_WORKER_TIMEOUT_SEC = 240

# ★[2026-08-10 批次SF #4] single-flight 把「資源爆炸」換成了「永久服務拒絕」★
#
#   ★這是批次SC(今天上午)那個修正自己開的洞,不是舊債★
#   加了守衛之後的行為是:worker 卡在 raw `GetWindowText` 永不返回 →
#   `_last_hidden_worker` 永遠 alive → 之後每一輪都拿得到 `_flow_lock`
#   (它有正常釋放)、進來、被守衛擋掉、正常釋放鎖。於是:
#     * 資源不再爆炸 ✓(守衛達成了它的目的)
#     * 但會診查詢【永遠】不會再執行,而且 ★`_flow_lock` 卡死判定永遠不成立★
#       —— 那道剛加的升級機制對這個情況完全無效(鎖每輪都好好地放掉了);
#     * scheduler tick、heartbeat 一切正常。
#   唯一能終結一條 native-blocked thread 的手段是【重啟行程】,而在守衛加上去
#   之後,已經沒有任何一條路會走到重啟。
#
#   ★「一個修正的正確性不能只看它自己」★ —— 守衛必須自帶出口:
#   放生滿這個上限就升級成重啟,讓外層 watchdog 換一個乾淨的行程來跑。
#   上限取 1 小時:遠高於 240 秒的正常上限,而 HIS 若只是暫時忙,那條 worker
#   自己的迴圈 deadline 到期就會結束並解除守衛(那時不會走到這裡)。
_HIDDEN_WORKER_STRANDED_MAX_SEC = 3600.0
_last_hidden_worker_since = [0.0]        # monotonic;0.0 = 目前沒有放生中的


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

    # ★[批次SC #2] single-flight 必須在【開任何資源之前】★
    #   (外審第 1 輪抓到)第一版擺在 `_ensure_hidden_desktop()` 之後:
    #     * 上一條還卡著時,每一輪 poll 照樣先開一個新的 HDESK 才拋 ——
    #       洩漏一模一樣,守衛等於沒加;
    #     * 更糟的是 `_ensure_hidden_desktop()` 回 None 的後備路徑
    #       (SW_HIDE)【完全繞過守衛】,直接與卡住的 worker 併行操作
    #       同一套 systemftp → 互相衝突的 HIS session。
    #   守衛保護的是「不可以再開一輪自動化」,不是「不可以開隱藏桌面」,
    #   所以位置要在所有路徑的共同上游。
    global _last_hidden_worker
    prev = _last_hidden_worker
    if prev is not None and prev.is_alive():
        # ★守衛自己要有出口★(批次SF #4)不然它只是把「資源爆炸」換成
        #   「永久服務拒絕」——而且是連 `_flow_lock` 卡死判定都看不見的那種。
        since = _last_hidden_worker_since[0]
        stranded = (time.monotonic() - since) if since else 0.0
        if since and stranded >= _HIDDEN_WORKER_STRANDED_MAX_SEC:
            # ★不可以在這裡先 logging★(外審 SF 第 1 輪 P1-1)
            #   handler lock 可能正被卡死的那條緒持有 —— 那樣連升級都做不到。
            #   `_force_exit` 會先掛保險絲才去寫 log。
            _force_exit(
                f"放生的自動化 worker 已卡住 {stranded / 60.0:.0f} 分鐘"
                "(raw GetWindowText 送給凍結的 systemftp,永遠不返回)。"
                "守衛擋住了資源累積,但會診查詢也【永遠】不會再執行 —— "
                "而且鎖每輪都正常釋放,流程鎖卡死判定對這個情況完全無效。"
                "重啟行程是唯一能終結它的手段", code=1)
        logging.error(
            "[consult] 上一條 ConsultAutomationHidden 仍未結束(卡在凍結的"
            " systemftp,已 %.0f 分鐘)→ 本輪不再開新的自動化(含 SW_HIDE 後備),"
            "避免累積 daemon thread、HDESK 與互相衝突的 HIS session;"
            "滿 %.0f 分鐘會強制重啟", stranded / 60.0,
            _HIDDEN_WORKER_STRANDED_MAX_SEC / 60.0)
        raise RuntimeError(
            "上一輪自動化仍在執行(疑似 systemftp 凍結)，本輪略過")

    hdesk = _ensure_hidden_desktop()
    _note_hidden_desktop_result(bool(hdesk))     # [批次SH] 資源耗盡的觀測點
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
        _last_hidden_worker = t
        t.start()
        t.join(timeout=_HIDDEN_WORKER_TIMEOUT_SEC)  # 4 分鐘硬上限
        if t.is_alive():
            # ★引用留著★ 下一輪會看到它還活著而跳過,直到它自己的迴圈
            #   deadline 到期結束(那時 finally 會 CloseDesktop)。
            # ★同時記下「從什麼時候開始放生」★(批次SF #4)守衛需要一個出口,
            #   而出口要有年齡才量得出來。
            _last_hidden_worker_since[0] = time.monotonic()
            raise RuntimeError("自動化執行超過 4 分鐘，已放棄（可能網路異常）")
        _last_hidden_worker = None      # 正常結束 → 不擋下一輪
        _last_hidden_worker_since[0] = 0.0
        if result.get("error"):
            raise result["error"]
        return result["shot"]

    logging.warning("無法建立隱藏桌面，改用 SW_HIDE 後備模式（可能短暫看到視窗）")
    # [codex P1 R17] 排程若還停在 3 分鐘常駐節奏,SW_HIDE=每輪完整登入 → 立刻降速
    _demote_schedule_to_legacy()
    return _run_with_sw_hide(cfg, roster_label)


def _wait_main_window_after_login(our_pids: set, *, visible_only: bool,
                                  timeout_sec: float = 120.0) -> int:
    """等住院醫囑主畫面出現;期間把擋在前面的「訊息通知」按掉。回傳 main hwnd。

    ★[2026-07-29 實機故障] 原本的迴圈條件是 `if mains and not notice`★
      —— 只要還找得到 NOTICE_CLASS 視窗就【拒絕】接受主畫面。實機 log 顯示
      「已關閉訊息通知主畫面」每 0.6 秒重複一次、整整刷滿 120 秒(1,568 行 log
      幾乎全是它),然後回報「等不到主畫面」。也就是說:點了確認之後那個視窗仍然
      找得到,於是永遠卡在「先把通知關掉」這一步。

    改用 Win32 的正規訊號:**主視窗被 modal 擋住時會被 disable**。
    `IsWindowEnabled(main)` 與可見性無關,在隱藏桌面/SW_HIDE 兩種模式下都成立 ——
    這正是原本用 `not notice` 想表達、卻表達錯了的那件事。
    (visible_only 不可一律改成 True:`_stealth()` 會把視窗 SW_HIDE,
     那條路徑上可見性根本不是有效訊號 —— 見 2026-05-15 的既有註解。)

    另外兩件事也一起補上,因為這次的 log 讓人查不下去:
      * 點確認的訊息【節流】並帶上 hwnd 與次數 —— 原本無法分辨「同一個視窗一直
        關不掉」與「通知有一整排、關掉一個又來一個」,兩者的處置完全不同。
      * 逾時時吐出【當下看到什麼】(class / 可見 / enabled),而不是只說「等不到」。
    """
    deadline = time.time() + timeout_sec
    clicks = 0
    last_notice_hwnd = None
    distinct_notices = set()
    # ★[2026-08-10 實機 A01-11106-001]★ 登入途中的對話框寫了什麼要留下來 ——
    #   那台連續失敗六小時,而診斷只留下「按了 1 次確認」,連是不是密碼錯誤
    #   都分辨不出來。清空:診斷要講的是【這一輪】看到什麼。
    # ★`visible_only=False` = SW_HIDE 後備模式★(隱藏桌面那條傳 True)——
    #   那個模式的隱形執行緒會把對話框藏掉,「沒攔到」在那裡不是證據。
    _reset_login_dialog_texts(stealth=not visible_only)
    # ★[2026-07-30 實機] 同一個通知按不掉就不要再按★
    #   實機是 200 次點擊全打在同一個 hwnd(整整 120 秒的預算),而那個視窗根本
    #   沒關掉。click_button 是純 PostMessage(BM_CLICK)、沒有回讀,而 Delphi 的
    #   modal form 關閉後只是 Hide —— 在 visible_only=False 的路徑上照樣找得到。
    #   不管是哪一種,連按 N 次沒有任何變化就代表「按這個沒有用」,繼續按只是把
    #   時間耗完,然後回報一句無從下手的「等不到主畫面」。
    clicks_on_same = 0
    stuck_notice = None
    next_bde_check = 0.0
    while time.time() < deadline:
        if not running.is_set():
            raise RuntimeError("流程已被中止")
        # ★[codex P2] BDE 初始化失敗要【當場】收工，不要空等滿 120 秒★
        #   那個對話框在啟動當下就出現，而且不會自己消失（HIS 根本沒起來）。
        #   等滿逾時只是把每一輪都拖兩分鐘、告警也晚兩分鐘才發得出去。
        #   節流成每 5 秒一次：偵測要列舉所有視窗與子控制項，不值得每 0.4 秒跑。
        now = time.time()
        if now >= next_bde_check:
            next_bde_check = now + 5.0
            code = detect_bde_startup_error(our_pids)
            if code:
                raise _bde_blocked(code, our_pids, clicks,
                                   last_notice_hwnd, distinct_notices)
        mains = find_windows(MAIN_CLASS, pids=our_pids, visible_only=visible_only)
        if mains:
            try:
                unblocked = bool(win32gui.IsWindowEnabled(mains[0]))
            except Exception:
                unblocked = True        # 問不到就別擋住流程(最壞是多按一次確認)
            if unblocked:
                if clicks:
                    logging.info("主畫面已可操作(期間關掉 %d 次訊息通知,"
                                 "不同視窗 %d 個)", clicks, len(distinct_notices))
                return mains[0]
        # ★[2026-08-05 實機] 先處理【真正擋住輸入】的那個對話框★
        #   診斷傾印顯示:TFMShowMessage 自己也是 disabled 的(en=0),壓在它上面
        #   的是 `TFMTimeOut_1`(en=1)。程式對著一個 disabled 的視窗按了 6 次
        #   「確認」——disabled 的視窗不會處理點擊——120 秒的登入預算就這樣燒光。
        #   判準改用 Win32 的正規訊號(可見 + enabled + 不是內容視窗),
        #   不再猜對話框叫什麼 class。
        if _dismiss_blocking_modals(pids=our_pids, record_text=True):
            clicks += 1
            time.sleep(0.6)
            continue
        notice = find_windows(NOTICE_CLASS, pids=our_pids,
                              visible_only=visible_only)
        if notice:
            try:
                # 它自己被別的 modal 擋住時,按它沒有任何作用(實機燒掉 120 秒的
                # 那 6 次點擊就是這樣)。等上面那個對話框先被處理掉。
                notice_actionable = bool(win32gui.IsWindowEnabled(notice[0]))
            except Exception:
                notice_actionable = True
            if not notice_actionable:
                time.sleep(0.4)
                continue
            btn = find_child(notice[0], "TButton", "確認")
            if btn and notice[0] != stuck_notice:
                if notice[0] == last_notice_hwnd:
                    clicks_on_same += 1
                else:
                    clicks_on_same = 1
                if clicks_on_same > _MAX_CLICKS_PER_NOTICE:
                    # 不再對它出手,但也不放棄整個流程:主畫面可能自己會出來
                    # (真正該回報的事在下面的 login 檢查與逾時訊息裡)。
                    logging.warning(
                        "訊息通知 hwnd=%s 按了 %d 次「確認」仍在,停止對它出手"
                        "(PostMessage 沒有回讀;Delphi 視窗關閉後也只是 Hide)",
                        notice[0], clicks_on_same)
                    stuck_notice = notice[0]
                    time.sleep(0.4)
                    continue
                click_button(btn)
                clicks += 1
                distinct_notices.add(notice[0])
                # 節流:前 3 次逐次記,之後每 20 次記一次(原本每 0.6 秒記一行,
                # 120 秒就把整份 log 洗掉,真正有用的訊息全被淹沒)。
                if clicks <= 3 or clicks % 20 == 0:
                    logging.info("已關閉訊息通知主畫面(hwnd=%s,第 %d 次,"
                                 "至今不同視窗 %d 個)",
                                 notice[0], clicks, len(distinct_notices))
                last_notice_hwnd = notice[0]
                time.sleep(0.6)
                continue
        time.sleep(0.4)
    # ★登入視窗還在 = 登入沒完成,那是完全不同的一件事★
    #   原本一律回報「等不到住院醫囑主畫面」,讓人往「主畫面被什麼擋住」的方向查;
    #   實機真正的狀況是 TFrmLogin 還可見 —— 帳密、院方認證、或連線根本沒過。
    #   兩者的處置差很遠,訊息就得說對。
    # ★先問「HIS 自己起來了嗎」再問「登入過了嗎」★ [2026-08-03 實機]
    #   BDE 初始化失敗時，那個 modal 會把登入視窗擋成 disabled —— 外觀與「帳密
    #   不對」一模一樣，舊版因此叫使用者去查帳號密碼（完全無關的方向）。
    #（迴圈內每 5 秒就查一次，這裡只是收尾補網：對話框在最後幾秒才冒出來時仍要認得）
    bde = detect_bde_startup_error(our_pids)
    if bde:
        raise _bde_blocked(bde, our_pids, clicks, last_notice_hwnd,
                           distinct_notices)
    if find_windows(LOGIN_CLASS, pids=our_pids, visible_only=True):
        raise LoginNotCompleted(
            "登入沒有完成(登入視窗仍在畫面上)—— 請確認帳號密碼是否被院方改過/"
            "停用,以及 HIS 是否連得上。★本次不再重試登入★(避免同一組帳密被"
            "連續送出而逼近鎖定門檻)。" + _describe_windows_for_diag(
                our_pids, clicks, last_notice_hwnd, distinct_notices))
    raise RuntimeError(
        "登入後等不到住院醫囑主畫面 —— " + _describe_windows_for_diag(
            our_pids, clicks, last_notice_hwnd, distinct_notices))


def bde_error_code_in(texts) -> str | None:
    """從一組視窗文字裡認出 BDE 初始化錯誤 → 回錯誤碼（如 "$250E"）或 None。

    純函式（可測）。★只回錯誤碼★——比對的是固定英文字串，取出的是十六進位碼，
    不會把視窗內容帶進 log/告警信（隱私邊界與 _describe_windows_for_diag 一致）。
    """
    for t in texts:
        if t and BDE_ERROR_MARKER in t:
            m = BDE_ERROR_CODE_RE.search(t)
            return m.group(0) if m else "(未知碼)"
    return None


def _window_texts(hwnd) -> list:
    """視窗自身 + 直接子控制項的文字（MessageBox 的內文在 Static 子控制項裡）。"""
    texts = []
    try:
        texts.append(win32gui.GetWindowText(hwnd))
    except Exception:
        pass

    def cb(child, _):
        try:
            texts.append(win32gui.GetWindowText(child))
        except Exception:
            pass
        return True
    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return texts


def detect_bde_startup_error(our_pids: set) -> str | None:
    """掃我們自己開的那個 systemftp 的視窗，找 BDE 初始化錯誤 → 錯誤碼或 None。

    ★[codex P1] 必須連【不可見】的視窗一起掃★ `_run_with_sw_hide` 的 stealth
    thread 會把該行程所有可見視窗一律隱藏；只掃可見視窗的話，正好在實際會用到的
    那條路徑上永遠找不到這個對話框，於是又退回誤導人的通用逾時訊息。
    被隱藏不等於不存在——它照樣是把登入視窗擋成 disabled 的那個 modal。
    （_describe_windows_for_diag 也是 visible_only=False，理由相同。）
    """
    try:
        for hwnd in find_windows(pids=our_pids, visible_only=False):
            code = bde_error_code_in(_window_texts(hwnd))
            if code:
                return code
    except Exception:
        logging.debug("[BDE] 偵測失敗（略過）", exc_info=True)
    return None


def _bde_blocked(code: str, our_pids: set, clicks: int, last_notice,
                 distinct_notices: set) -> "HISStartupBlocked":
    """組出 BDE 失敗的例外（兩處呼叫共用，措辭不會各改各的）。"""
    return HISStartupBlocked(
        f"住院醫囑系統自己沒起來:Borland Database Engine 初始化失敗"
        f"(error {code})——★這不是帳號密碼問題★,是這台電腦的 HIS/BDE 環境問題"
        f"(通常重開機可解;詳見 docs/會診查詢_BDE初始化失敗處理.md)。"
        f"★本次不重試★(BDE 起不來,再登一百次也一樣)。"
        + _describe_windows_for_diag(our_pids, clicks, last_notice,
                                     distinct_notices))


# ══ 登入階段對話框的文字（2026-08-10 實機 A01-11106-001）═════════════════
#
# ★為什麼原本不記,以及為什麼這一段是例外★
# `_describe_windows_for_diag` 的既有原則是「只記 class 與旗標,不記任何視窗
# 文字」—— 那是對的,因為登入之後畫面上就是病人清單。
#
# 但 2026-08-10 那台(A01-11106-001)連續失敗六小時,診斷只留下
#     「期間按了 1 次確認(不同通知視窗 0 個,最後一個 hwnd=None)」
# 這一行的意思是:那一下按的不是「登入後訊息通知」,而是
# `_dismiss_blocking_modals` 在登入途中抓到、按掉之後就消失的一個對話框。
# ★那個對話框寫了什麼,是整件事最有價值的一行字,而我們把它丟掉了★ ——
# 於是只能在「密碼被改過」「帳號被鎖」「帳密沒打進欄位」之間用猜的。
#
# ★邊界(結構上保證,不是靠紀律)★
# 只有 `_wait_main_window_after_login` 會傳 `record_text=True`,而那個函式
# 【在主畫面可操作之前就會返回】:此刻 HIS 連主畫面都還沒交出來,更沒有送出
# 任何查詢,畫面上不可能有病人資料。它能出現的只有認證/連線類訊息。
# 會診單、主畫面、以及登入完成之後的任何路徑一律沿用舊原則(預設 False)。
_LOGIN_DIALOG_TEXT_MAX = 200      # 單筆截斷(認證訊息很短;長的必然不是我們要的)
_LOGIN_DIALOG_KEEP = 5            # 最多留幾筆(同一輪內)
_login_dialog_texts: list = []    # [(class, text)]
# 帳號/密碼欄位的焦點有沒有被確認。★這是【另一半】的證據★:
#   有錯誤對話框 → 帳密真的送出去而被拒絕(去查密碼/鎖定);
#   沒有對話框、焦點又沒確認 → 字很可能根本沒打進欄位(去查隱藏桌面的焦點)。
#   舊版把這個訊號只寫進 log,沒有帶進告警信 —— 於是收到信的人分不出是哪一種。
_login_focus_report: list = []    # [(欄位, 是否確認)]
# 本輪登入跑在 SW_HIDE 後備模式嗎。★這會改變「沒攔到對話框」的意義★:
#   那個模式的隱形執行緒每 80ms 就把新出現的視窗 SW_HIDE,而對話框偵測
#   (`_blocking_dialogs`)只認【可見】的視窗 —— 於是「一個都沒攔到」在那裡
#   完全不能當成「HIS 沒有跳訊息」的證據。不講清楚的話,這一批新加的那句
#   「往『字沒打進欄位』的方向查」反而會把人指向錯的方向。
_login_stealth_mode = [False]


def _reset_login_dialog_texts(stealth: bool = False) -> None:
    """每一次登入等待開始時清空 —— 診斷要講的是【這一輪】看到什麼。

    ★只清對話框,不清焦點回報★:焦點是在【呼叫本函式之前】就打好的,
    一起清掉的話那半邊證據永遠是空的。
    """
    _login_dialog_texts.clear()
    _login_stealth_mode[0] = bool(stealth)
    _login_dialog_shot_done[0] = False      # 新的一輪 → 可以再截一張


# ══ 登入階段對話框的【截圖】(2026-08-12 使用者要求)═══════════════════════
#
# 為什麼光有上面的文字還不夠:Delphi 訊息框的內文是 TLabel(TGraphicControl,
# ★沒有 HWND★),`_window_texts` 拿不到 —— 實機告警只看得到
# 「[TMessageForm] 住院醫囑系統 / OK」,HIS 到底說了什麼仍然要用猜的。
# 截「那個對話框視窗本身」的圖(PrintWindow,不是全螢幕)就能看到內文,
# 而且畫面上只有那個視窗的範圍。
#
# 邊界與 record_text 完全相同(見上面那段):只在登入階段、主畫面交出來之前,
# 畫面上不可能有病人資料。另外再加一道尺寸上限當第二道防線 ——
# 對話框不會有整個螢幕那麼大,太大代表抓錯視窗,寧可不存。
_LOGIN_DIALOG_SHOT_FILE = "login_dialog_evidence.png"
_LOGIN_DIALOG_SHOT_TIMEOUT_SEC = 5.0    # 比一般截圖短:這是在登入迴圈裡順手做的
_LOGIN_DIALOG_SHOT_MAX_W = 1600         # 大於這個尺寸就當成抓錯視窗,不存
_LOGIN_DIALOG_SHOT_MAX_H = 1200
# 告警信寄出時,截圖比這更舊就不附 —— 舊事故的截圖會把診斷帶錯方向
# (告警最多 6 小時一封;故障期間每一輪登入都會重截,新鮮的一定跟得上)。
_LOGIN_DIALOG_SHOT_MAX_AGE_SEC = 12 * 3600.0
# 本輪已經截過了嗎。★一輪只截第一個★:第一個對話框通常就是拒絕原因,
# 之後的多半是連鎖噪音;之後不同的對話框文字仍會被上面的機制記到。
_login_dialog_shot_done = [False]


def _login_dialog_shot_path() -> str:
    from cmuh_common.paths import get_settings_dir  # noqa: PLC0415
    return os.path.join(get_settings_dir(), _LOGIN_DIALOG_SHOT_FILE)


def _login_dialog_shot_sending_path() -> str:
    """寄信當下用的【不可變快照】路徑(★外審 SK 第 1 輪 P2★)。

    正式檔會被兩條路動到:每輪登入的重截(os.replace)與恢復時的清理
    (os.remove)。告警 worker 決定「要附」到 SMTP 真正開檔之間,正式檔
    被刪的話,信會「說有附卻沒附」,或整封寄失敗 —— 而告警 6 小時才一封。
    所以寄信前先 copy 成這個快照、附快照;寄完(成敗都)刪掉。
    保留 .png 副檔名:附件的 MIME 型別是靠副檔名判斷的。
    """
    from cmuh_common.paths import get_settings_dir  # noqa: PLC0415
    return os.path.join(get_settings_dir(), "login_dialog_evidence_sending.png")


def _capture_login_dialog_shot(hwnd: int, cls: str) -> None:
    """把登入途中的對話框【本身】截圖存檔。整條路 fail-open ——
    截圖是診斷的錦上添花,任何失敗都不可以影響登入流程本身。
    """
    try:
        if _login_dialog_shot_done[0]:
            return
        img = call_with_timeout(
            lambda: _capture_window_image_impl(hwnd),
            _LOGIN_DIALOG_SHOT_TIMEOUT_SEC, default=None,
            name="login-dialog-shot")
        if img is None:
            logging.warning("[login] 對話框截圖失敗/逾時(class=%s)"
                            "—— 告警信將只有文字證據", cls)
            return
        w, h = img.size
        if w > _LOGIN_DIALOG_SHOT_MAX_W or h > _LOGIN_DIALOG_SHOT_MAX_H:
            logging.warning("[login] 對話框截圖尺寸異常(%dx%d,class=%s)"
                            "—— 對話框不該這麼大,可能抓錯視窗,不存", w, h, cls)
            return
        path = _login_dialog_shot_path()
        tmp = path + ".tmp"
        # ★先寫暫存再原子換名★:告警是背景執行緒在讀這個檔,
        #   直接寫的話它可能讀到半張圖。
        img.save(tmp, "PNG")
        os.replace(tmp, path)
        _login_dialog_shot_done[0] = True
        logging.info("[login] 已把登入對話框截圖存檔(class=%s,%dx%d)"
                     "—— 會附在下一封連續失敗告警信裡", cls, w, h)
    except Exception:
        logging.warning("[login] 對話框截圖存檔失敗(class=%s)", cls,
                        exc_info=True)


def _login_dialog_shot_for_alert():
    """告警信要不要附截圖 → 附哪一個檔(str),或 None。

    ★夠新鮮才附★:mtime 在未來(被校時過)或太舊的一律不附 ——
    附一張舊事故的圖比不附更糟,它會把人帶去查一個已經不存在的原因。
    """
    try:
        path = _login_dialog_shot_path()
        age = time.time() - os.path.getmtime(path)
        if 0 <= age <= _LOGIN_DIALOG_SHOT_MAX_AGE_SEC:
            return path
    except OSError:
        pass
    return None


def _note_login_focus(field: str, ok: bool) -> None:
    """記下某個登入欄位的焦點有沒有被確認(每次登入覆寫,不累積)。"""
    _login_focus_report[:] = [x for x in _login_focus_report if x[0] != field]
    _login_focus_report.append((str(field), bool(ok)))


def _note_login_dialog(hwnd: int, cls: str, buttons) -> None:
    """記下一個【登入階段】對話框的文字（去重、截斷、有上限）。

    去重不只是省空間:那個迴圈每 0.4 秒跑一次,不去重的話同一句話會把 log
    洗掉(本檔已經因為這件事吃過兩次虧,見 `_reported_unknown_dialogs` 與
    「已關閉訊息通知主畫面」的節流)。
    """
    # ★截圖要排在所有 early-return 之前★:內文是 TLabel 時 `_window_texts`
    #   常常只拿得到標題+按鈕,甚至整個是空的 —— 文字記不到的那幾種,
    #   正是最需要截圖的(截圖自己有「一輪一張」的去重,不怕重複進來)。
    _capture_login_dialog_shot(hwnd, cls)
    try:
        raw = call_with_timeout(lambda: _window_texts(hwnd), 3.0, default=[],
                                name="login-dialog-text") or []
    except Exception:
        return
    text = " / ".join(t.strip() for t in raw if t and t.strip())
    text = " ".join(text.split())[:_LOGIN_DIALOG_TEXT_MAX]
    if not text:
        return
    item = (str(cls), text)
    if item in _login_dialog_texts:
        return                          # 同一句只講一次
    if len(_login_dialog_texts) >= _LOGIN_DIALOG_KEEP:
        return                          # 到上限就不再收(不擠掉先出現的那幾筆)
    _login_dialog_texts.append(item)
    logging.warning(
        "[login] ★登入途中出現對話框★ class=%s 按鈕=%s 內容=%r "
        "—— 這是判斷「帳密被改/帳號被鎖/帳密沒打進去」的關鍵證據",
        cls, [str(b) for b in (buttons or [])], text)


def _login_dialog_digest() -> str:
    """這一輪登入的證據摘要（★要排在診斷字串的最前面★）。

    ★[外審 SG 第 1 輪 P1] 告警信只保留前 300 字★
    `_note_job_failure` / `_send_failure_notice_async` 都是 `str(reason)[:300]`。
    實機那封信光是「前言 + 視窗清單」就已經逼近上限 —— 這一批要送出去的東西
    如果接在視窗清單後面,正好會被剪掉,整個修正等於沒做。
    視窗清單是這串裡最不重要的部分,讓它去當被剪掉的那一段。
    """
    parts = []
    if _login_stealth_mode[0]:
        # 哪一種模式會改變後續判讀(後備模式是每輪完整登入,節奏也不同)。
        parts.append("模式=SW_HIDE後備")
    if _login_focus_report:
        parts.append("輸入欄位焦點:" + "、".join(
            f"{f}={'確認' if ok else '★未確認★'}"
            for f, ok in _login_focus_report))
    if _login_dialog_texts:
        parts.append("登入途中的對話框:" + "、".join(
            f"[{c}] {t}" for c, t in _login_dialog_texts))
    else:
        # ★「一個都沒有」在兩種模式下都算數★(外審 SG 第 2 輪 P2 修正之後):
        #   隱形執行緒現在會【放過】擋路的對話框,不再把偵測要找的東西藏掉。
        parts.append("登入途中沒有攔到任何對話框"
                     "(帳密若被拒,HIS 通常會跳一個 —— 一個都沒有時,"
                     "要往「字沒打進欄位」的方向查)")
    return ";".join(parts) + ";"


def _describe_windows_for_diag(our_pids: set, clicks: int, last_notice,
                               distinct_notices: set) -> str:
    """逾時時把「當下看到什麼」寫清楚。

    視窗清單一律只有 class/旗標、不含文字內容;唯一的例外是登入階段收集到的
    對話框文字(見上面那段說明的邊界)。
    """
    try:
        seen = []
        for hwnd in find_windows(pids=our_pids, visible_only=False):
            try:
                seen.append("%s(vis=%d,en=%d)" % (
                    win32gui.GetClassName(hwnd),
                    int(win32gui.IsWindowVisible(hwnd)),
                    int(win32gui.IsWindowEnabled(hwnd))))
            except Exception:
                continue
        detail = "、".join(seen[:12]) or "(列舉不到任何視窗)"
    except Exception:
        detail = "(視窗列舉失敗)"
    # ★證據在前、視窗清單在後★(外審 SG 第 1 輪 P1:告警信只留前 300 字)
    return (_login_dialog_digest()
            + f"期間按了 {clicks} 次「確認」"
            f"(不同通知視窗 {len(distinct_notices)} 個,最後一個 hwnd={last_notice});"
            f"當下看到的視窗:{detail}")


# =============================================================================
# 常駐 session（2026-08-03 使用者定案）
# =============================================================================
# 背景:院方 HIS 閒置 5 分鐘強制登出;舊的「每輪開程式→登入→查→關程式」冷啟動
# 既慢又讓登入次數逼近鎖定門檻。改為登入一次後【停在主畫面】,每輪只「按會診
# 查詢→截圖/擷取→按『回』退回主畫面」——查詢本身就是 keepalive。
# 純政策(間隔/冷卻/定期重啟/BDE 重開機門檻)在 cmuh_common/consult_keepalive.py。
class _PersistentSession:
    """一個活著的 systemftp(隱藏桌面)+ 已登入停在主畫面。"""

    def __init__(self, hproc, hthread, pid, our_pids, main_hwnd=None):
        self.hproc = hproc            # CreateProcess 的行程 handle(殺人執照)
        self.hthread = hthread
        self.pid = pid
        # ★`our_pids` 只能拿來【找】東西,不可以拿來判定身分★(2026-08-05 實機證據)
        #   診間 log 三次登入、三次都在收尾時排除掉一個外來的 systemftp
        #   (pid 15056 / 8036 / 18748),而且 `登入視窗 hwnd=... pid=[8036, 16276]`
        #   顯示【登入當下】這個集合就已經含著別人的行程。它是全機 PID 差集,
        #   冷啟動那 120 秒內醫師自己開一次住院系統就會混進來。
        #   找視窗找錯只是找不到;判身分弄錯是關掉別人的程式 / 把別人的視窗
        #   當成自己還活著。後者一律改用 `main_*` 這組確切身分。
        self.our_pids = set(our_pids)
        # ★我們【確切登入的那個】主畫面★(2026-08-04 外審後自查 P0)
        #   `our_pids` 是全機 PID 差集,實機證實會混進外來的 systemftp
        #   (log:「pid 10928 已非 systemftp」「收尾時排除 1 個不屬於本次啟動的」)。
        #   拿它當「哪些視窗是我的」去送 WM_CLOSE,等於把批次 P 擋掉的傷害從
        #   視窗那道門放回來 —— 醫師的住院系統會被關掉。
        #   `_wait_main_window_after_login()` 本來就【回傳】這個 hwnd,只是以前
        #   被丟掉。存下來,收尾就只關這一個。
        self.main_hwnd = main_hwnd
        # ★hwnd 自己不足以當身分★(2026-08-05 外審第 4 輪 P1-04)
        #   視窗銷毀後 handle 值會被 Windows 回收再發給別人。只憑 hwnd 就送
        #   WM_CLOSE,可能關到「剛好拿到同一個號碼」的另一個視窗;只憑 hwnd 判活,
        #   也可能把別人的視窗當成我們還活著。連 class 與 pid 一起記下來,
        #   用之前先驗這三樣還是不是同一個。三者都對不代表宇宙唯一,但足以
        #   把「handle 被回收」這個實際會發生的情況擋掉。
        self.main_pid, self.main_class = _window_identity(main_hwnd)
        # PID 會被回收 → 連行程的建立時間一起記(見 `_is_same_window`)。
        # 讀不到就是 None,之後一律不採用這個訊號,不會因此判定「換人了」。
        self.main_proc_started = _process_started_at(self.main_pid)
        self.started_at = time.time() # 供 6 小時定期重啟判斷
        # [codex P1 R12] 租約:正在被某個 worker 使用中。run_consult_flow 的 240 秒
        # join 逾時會【棄置】worker(daemon 緒仍在跑),下一輪絕不可跟它共用同一個
        # session(兩個 worker 對同一組 HIS 視窗送命令=截圖錯亂/互相關窗)。
        self.in_use = False


_session_lock = threading.Lock()      # 只保護 _psession 參照的取放
_psession = None
_login_cooldown_until = 0.0           # 登入失敗冷卻(見 _cold_start_session)


def _set_login_cooldown_until(ts: float, *, persist: bool = True) -> None:
    """設定登入冷卻到期時間（並落地）。

    ★[2026-08-11 批次SH] 一定要走這個函式★ 直接指派全域變數的話那次冷卻
    就不會落地，而重啟正是這個防護最沒有防備的時刻（見 `_load_job_fail_state`）。
    `persist=False` 只給【載入】用：剛從檔案讀出來的東西不需要再寫回去。
    """
    global _login_cooldown_until
    _login_cooldown_until = float(ts)
    if persist:
        _save_job_fail_state()


def _session_pids() -> set:
    with _session_lock:
        return set(_psession.our_pids) if _psession else set()


def _session_death_reason(sess) -> str:
    """session 為什麼不能用了 → "" 表示還活著,否則是【單一】原因。

    ★[2026-08-04 實機] 原本兩個條件共用一句話★
    舊訊息是「session 已死(行程結束或主畫面不見了)」—— 兩個成因完全不同、修法也
    完全不同,卻分不出是哪一個。診間 log 顯示【每一輪】都走這條路(13:44→14:29 共
    15 輪無一例外):每 3 分鐘判死、冷啟動、★重新送一次帳密★,而
    `_cold_start_session_impl` 的 docstring 明文寫著「絕不可把同一組帳密每 3 分鐘
    送一次」。要修它就得先知道是哪個條件在觸發。

    ★[2026-08-04 診間 log 已證實]★ 加了診斷之後的實機 log:21 次判死【100% 都是】
    「我們啟動的 systemftp 行程已結束」，「找不到主畫面視窗」零次。也就是說
    **systemftp.exe 是啟動器型行程** —— 起來、把工作交給既有實例、自己立刻結束。
    `sess.hproc` 握的是那個啟動器,它一定馬上 signaled,所以舊判定永遠說「死了」。

    ★所以權威訊號改成【主畫面視窗】★:session 能不能用,取決於主畫面在不在,
    不取決於我們當初 spawn 的那個啟動器有沒有活著。行程 handle 降級成「主畫面
    不在時，用來說明是哪一種不在」。

    ★[2026-08-05 外審第 4 輪 P1-05 + 實機證據] 但不可以問 `our_pids`★
    上一版寫的是 `find_windows(MAIN_CLASS, pids=sess.our_pids)`。診間 log 顯示
    這個集合【每一次登入】都含著外來的 systemftp(`登入視窗 hwnd=... pid=[8036,
    16276]`,三次登入三次都要在收尾時排除一個)。於是這個判定會因為**醫師自己那個
    住院系統的主畫面還開著**而回答「我們的 session 還活著」——我們接著對一個
    早就不存在的 session 送查詢、失敗、走恢復路徑;更糟的是誤以為手上有東西。
    改問我們【確切登入的那一個】視窗:`sess.main_hwnd` + 身分指紋。
    """
    hwnd = getattr(sess, "main_hwnd", None)
    if not hwnd:
        return "這個 session 從未登入成功(沒有記到主畫面)"
    if _window_alive(hwnd):
        # ★存在 ≠ 還是我們那一個★ handle 被回收後會指向別人的視窗
        if _is_same_window(sess):
            return ""      # 主畫面還在 → 可以用(啟動器早就結束是正常的)
        return "主畫面 hwnd 已被回收給別的視窗(不再是我們登入的那一個)"
    try:
        if win32event.WaitForSingleObject(sess.hproc, 0) != win32event.WAIT_TIMEOUT:
            return "我們啟動的 systemftp 行程已結束,主畫面也不在"
    except Exception:
        return "無法查詢 systemftp 行程狀態,且主畫面不在"
    return "行程還在,但我們登入的主畫面已經不存在"


def _session_is_alive(sess) -> bool:
    """我們登入的那個主畫面還在不在(身分要對得上)。

    ★[2026-08-05] 不再依賴呼叫緒的桌面★ 舊版用 `find_windows`(只列舉本緒桌面)
    所以要求呼叫緒必須在隱藏桌面上;現在只對一個已知 hwnd 做 IsWindow /
    GetWindowThreadProcessId / GetClassName,這三個不受呼叫緒桌面影響。
    主畫面在但被 modal 擋住的情況這裡不驗——查詢會失敗,由恢復機制處理。"""
    return not _session_death_reason(sess)


def _verified_owned_pids(root_pid: int, candidates) -> set:
    """從候選集裡只留下【確定是我們開的】：root 自己，或可驗證的後代。

    ★[2026-08-04 外審 P1-01]★ `our_pids` 是用【全機 PID 差集】算出來的
    （`_systemftp_pids() - before`）。冷啟動要等最多 120 秒，這段期間醫師若手動
    打開住院系統，他那個行程就會落進差集裡 —— 之後 teardown 對整個集合送
    `WM_CLOSE`，等於【替醫師關掉他自己開的 HIS】。

    ★分清楚兩件事★
      * 「找視窗的候選集」可以寬鬆（找錯只是找不到，不會造成傷害）
      * 「可以終止的集合」必須封閉（弄錯就是關掉別人的程式）
    這個函式只用在後者。

    驗不出後代時只認 root —— root 是我們自己 `CreateProcess`/`Popen` 出來的，
    身分由建立行為本身保證，不需要任何列舉。代價是它的後代可能變成孤兒；
    但孤兒有既有的清理機制（`_kill_systemftp` 的 before 快照），而關掉醫師的
    HIS 沒有任何補救。★寧可留孤兒，不可誤關★
    """
    owned = {root_pid}
    try:
        import psutil                                     # noqa: PLC0415
        for child in psutil.Process(root_pid).children(recursive=True):
            owned.add(child.pid)
    except Exception:
        # root 可能已經結束（後代驗不出來）→ 只認 root，保守但安全
        logging.debug("[session] 列舉 systemftp 後代失敗 → 只終止 root",
                      exc_info=True)
    keep = {p for p in (candidates or ()) if p in owned}
    keep.add(root_pid)
    dropped = {p for p in (candidates or ()) if p not in owned}
    if dropped:
        # ★這行就是實機證據★ 差集裡有不屬於我們的 systemftp —— 很可能是醫師
        #   自己開的。只記數量與 PID（PID 不是病人資料），不記視窗標題。
        logging.warning("[session] 收尾時排除 %d 個不屬於本次啟動的 systemftp"
                        "(pid=%s) —— 那可能是使用者自己開的，不動它",
                        len(dropped), sorted(dropped))
    return keep


def _process_started_at(pid):
    """行程的建立時間 → float;查不到回 None。

    ★[2026-08-05 外審第 5 輪 P1-05/P2-06] PID 會被回收★
    Windows 會把結束行程的 PID 發給新的行程。只比對 PID(甚至「PID + 執行檔
    名稱」)擋不住「同一支 systemftp 換了一個行程」—— 而那個新行程很可能是
    醫師自己開的。建立時間是同一個 PID 底下唯一穩定的區分點。
    """
    if not pid:
        return None
    try:
        import psutil  # noqa: PLC0415
        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        # ★查不到就是查不到,不編一個值出來★ 呼叫端一律當成「這個訊號不可用」,
        #   而不是當成「不一樣」——後者會把讀不到權限變成每輪重登。
        return None


def _window_identity(hwnd) -> tuple:
    """(pid, class) —— 視窗的身分指紋。查不到回 (None, None)。"""
    if not hwnd:
        return (None, None)
    pid = cls = None
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        logging.debug("[session] 取視窗 pid 失敗 hwnd=%s", hwnd, exc_info=True)
    try:
        cls = win32gui.GetClassName(hwnd)
    except Exception:
        logging.debug("[session] 取視窗 class 失敗 hwnd=%s", hwnd, exc_info=True)
    return (pid, cls)


def _is_same_window(sess) -> bool:
    """`sess.main_hwnd` 現在指的還是不是【當初登入的那一個】視窗。

    handle 值會被回收再發給別人,所以「IsWindow 為真」只證明這個號碼現在有主,
    不證明主人還是我們。比對登入當下記下的 (pid, class)。

    ★沒有記到身分(舊 session/查不到)時回 False★ —— 「不知道」不可以當成
    「是」:下游會拿它去送 WM_CLOSE。寧可少關一個(有告警),不可關錯一個。
    """
    hwnd = getattr(sess, "main_hwnd", None)
    if not hwnd:
        return False
    want_pid = getattr(sess, "main_pid", None)
    want_cls = getattr(sess, "main_class", None)
    if want_pid is None or want_cls is None:
        return False
    if _window_identity(hwnd) != (want_pid, want_cls):
        return False
    # ★[2026-08-05 外審第 5 輪 P1-05] 連行程也可能是「同一個 PID、不同行程」★
    #   Windows 會回收 PID。hwnd + pid + class 全對得上,底下的行程仍可能已經
    #   換人(而且很可能就是醫師自己開的同一支程式)。建立時間是唯一穩定的區分點。
    #
    #   ★這個訊號是【選用】的★:登入當下若讀不到建立時間(權限/psutil 不可用),
    #   或現在讀不到,一律【不採用這個訊號】,不是判定「不一樣」——
    #   把「讀不到」當成「不是同一個」會讓每一輪都重登,那正是 2026-08-04
    #   才修掉的實機故障的形狀。只有【當初記到了、現在也讀得到、而且不同】
    #   才算換了行程。
    want_started = getattr(sess, "main_proc_started", None)
    if want_started is not None:
        now_started = _process_started_at(want_pid)
        if now_started is not None and abs(now_started - want_started) > 1.0:
            return False
    return True


def _window_alive(hwnd: int) -> bool:
    """這個視窗還存在嗎 —— 給【判活/要不要重登】用。

    ★這裡刻意【不看】可見性★:`_stealth()` 的 SW_HIDE 後備模式會主動把視窗藏起來
    (見 `_wait_main_window_after_login` 的既有說明「那條路徑上可見性根本不是有效
    訊號」)。把「被我們自己藏起來」讀成「死了」,結果就是每一輪重登、每 3 分鐘重送
    一次帳密 —— 那正是 2026-08-04 才剛修掉的實機故障。

    查不到 → False(當作不能用):判活的安全方向是「寧可重登」。
    """
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:
        logging.debug("[session] 查詢視窗存在與否失敗 hwnd=%s", hwnd, exc_info=True)
        return False


def _window_destroyed(hwnd: int) -> bool:
    """這個視窗是不是【真的被銷毀了】—— 給【收尾確認】用。

    ★與 `_window_alive` 不是互為反面★,兩者對「不知道」的處置刻意相反:
      * 判活問「還能用嗎」→ 不知道就當不能用(重登,代價是多一次登入)
      * 收尾問「關掉了嗎」→ 不知道就當【沒關掉】(告警,代價是多一次人工確認)
    兩個問題若共用一個述詞,其中一邊必然是錯的方向。這正是外審第 4 輪 P1-01/P1-02
    指出的:舊的 `_window_is_gone` 把「隱藏」與「API 例外」都算成「已經關掉了」——
    於是 HIS 明明還登入著,程式卻回報收尾成功、還把參照丟掉。

    ★也不看可見性★:Delphi 的表單關閉後常常只是 Hide(2026-07-27 事故),
    所以「看不見」【更不能】拿來當「已經關掉」的證據。只有 handle 真的失效才算。
    """
    try:
        return not win32gui.IsWindow(hwnd)
    except Exception:
        logging.warning("[session] 無法確認視窗是否已關閉 hwnd=%s → 一律視為【尚未關閉】",
                        hwnd, exc_info=True)
        return False                   # 不知道 ≠ 關掉了


def _close_session_windows(sess, *, close=None, gone=None, sleep=None) -> bool:
    """關掉【我們自己登入的那一個】主畫面並回讀確認 → 有沒有真的關掉。

    ★[2026-08-04 外審第 3 輪 P1-05]★ 行程層面已經關不掉了（見呼叫端說明），
    所以改從視窗層面關。

    ★[同日自查 P0] 但不可以用 `our_pids` 去找「哪些視窗是我的」★
    第一版寫成 `find_windows(MAIN_CLASS, pids=sess.our_pids)`。`our_pids` 是全機
    PID 差集，實機已證實會混進外來的 systemftp（log：「pid 10928 已非 systemftp」、
    「收尾時排除 1 個不屬於本次啟動的」）。拿它當授權去送 WM_CLOSE，等於把批次 P
    擋掉的傷害從【視窗】這道門放回來 —— ★醫師的住院系統會被關掉★。

    改成只關 `sess.main_hwnd`：那是 `_wait_main_window_after_login()` 回傳的、
    我們【確切登入進去】的那一個視窗，身分由「我們自己登入它」這件事保證。

    ★`main_hwnd is None` 的意思是「這個 session 從未登入成功」★，不是「不知道」：
    `_wait_main_window_after_login()` 只有 `return mains[0]` 一條成功出口，失敗一律
    拋例外。所以成功建立的 session 必然帶著 hwnd；只有冷啟動失敗那條路徑上、為了
    收掉行程而臨時建的 session 才是 None —— 那時本來就沒有我們的主畫面可關。
    （這個不變式由 `test_the_session_records_the_window_it_logged_into` 守著。）

    `close`/`gone`/`sleep` 只給測試注入。
    """
    close = close or (lambda h: win32gui.PostMessage(
        h, win32con.WM_CLOSE, 0, 0))
    gone = gone or _window_destroyed
    sleep = sleep or time.sleep

    hwnd = getattr(sess, "main_hwnd", None)
    if not hwnd:
        # 登入沒成功 → 沒有屬於我們的主畫面。★也不會退回用 PID 差集去找★
        # （那個集合含醫師的行程，見上面）。
        logging.debug("[session] 這個 session 沒有主畫面可關(從未登入成功)")
        return True
    if gone(hwnd):
        logging.info("[session] 主畫面已不存在(hwnd=%s) → 無需關閉", hwnd)
        return True                    # 本來就不在了 → 沒東西要關
    # ★送 WM_CLOSE 之前先確認這個號碼還是當初那個視窗★(外審第 4 輪 P1-04)
    #   handle 會被回收:原視窗銷毀後,同一個數值可能已經是別的視窗 —— 極可能
    #   就是醫師自己開的住院系統(同一支程式、同一個 class)。比對 (pid, class)。
    if not _is_same_window(sess):
        now_pid, now_cls = _window_identity(hwnd)
        want_pid = getattr(sess, "main_pid", None)
        want_cls = getattr(sess, "main_class", None)
        # ★[2026-08-06 深度穩定] 讀得到現任身分、也記過自己的身分 → 判定是
        #   【結論性】的:handle 只有在原視窗銷毀之後才會被回收給別人;身分驗證
        #   不過(不論是 pid/class 換人、還是行程建立時間對不上)都代表我們登入的
        #   那個視窗/行程早已不存在 —— session 已結束,沒有東西要關。
        #   舊行為把這種情況回 False → 永遠掛在帳上:每輪重試永遠失敗、每 6 小時
        #   告警一次、還要每 15 分鐘擋一次新登入 —— 一次 handle 回收換來永久假故障。
        if (now_pid is not None and now_cls is not None
                and want_pid is not None and want_cls is not None):
            logging.info("[session] hwnd=%s 已被回收成別的視窗(當初 pid=%s "
                         "class=%s,現在 pid=%s class=%s) → 我們的主畫面早已銷毀,"
                         "session 視為已結束", hwnd, want_pid, want_cls,
                         now_pid, now_cls)
            return True
        # 讀不到現任身分/當初沒記到身分 → 「不知道」不可以當成「已結束」,
        # 也不可以送 WM_CLOSE(可能關到醫師的視窗) → 保守留帳,下一輪再驗。
        logging.error("[session] ★hwnd=%s 身分無法確認★"
                      "(當初 pid=%s class=%s,現在 pid=%s class=%s) → 不送 WM_CLOSE。"
                      "我們的 HIS session 可能仍登入中,請人工確認隱藏桌面",
                      hwnd, want_pid, want_cls, now_pid, now_cls)
        return False                   # 沒關成功,而且不可以亂關 → 交給掛帳機制
    # ★[2026-08-05 實機事故] 先把 modal 按掉,不然 WM_CLOSE 一定沒用★
    #   被 modal 擋住的主畫面是 disabled 的,disabled 的視窗不會處理 WM_CLOSE。
    #   當天三次「★主畫面關不掉★」全部是這個原因(前一行 log 就寫著
    #   「主畫面被 modal 對話框擋住」),而我卻把它當成獨立故障去掛帳。
    _dismiss_blocking_modals(sess)
    try:
        close(hwnd)
    except Exception:
        logging.warning("[session] 送 WM_CLOSE 失敗 hwnd=%s", hwnd, exc_info=True)
        return False
    for _ in range(6):                 # 最多等 3 秒
        sleep(0.5)
        if gone(hwnd):
            logging.info("[session] 主畫面已關閉(hwnd=%s)", hwnd)
            return True
    logging.error("[session] ★主畫面關不掉★(hwnd=%s) —— 這個 HIS session 仍然登入中,"
                  "本程式已經放掉它的參照,請人工確認隱藏桌面上的住院系統", hwnd)
    return False


def _terminate_session_process(sess) -> None:
    """優雅關閉 → handle 強制結束 → 關 handle。

    沿用 2026-07-27 事故的教訓:handle 綁定的必然是我們自己開的那個行程,
    不依賴名稱/視窗列舉,資源耗盡時照樣有效、不可能誤殺使用者的醫囑系統。

    ★[2026-08-04 外審 P1-01] 上面那句話原本只對後半段成立★
    `TerminateProcess(sess.hproc)` 確實只會結束我們開的那一個；但它前面的
    `close_pids(sess.our_pids)` 吃的是【全機 PID 差集】，裡面可能有醫師自己開的
    住院系統。優雅關閉那一步改成只送給驗證過的自有行程。
    """
    try:
        close_pids(_verified_owned_pids(sess.pid, sess.our_pids))
    except Exception:
        logging.debug("[session] 優雅關閉失敗(續走 handle 強制結束)", exc_info=True)
    # ★[2026-08-04 外審第 3 輪 P1-05] 行程層面關不掉,要從【視窗層面】關★
    #   實機證實 systemftp 是啟動器型行程:我們 spawn 的 root 立刻結束,於是
    #     * `_verified_owned_pids` 只剩一個【已死的】root → close_pids 關不到東西
    #     * `WaitForSingleObject(hproc)` 早已 signaled → 下面的 TerminateProcess 不會執行
    #   淨結果是 teardown 只清掉 Python 這邊的參照,真正的 HIS UI 還留在隱藏桌面 ——
    #   「6 小時定期重啟」「休息時段收掉」「接管舊 worker」全都只是說說而已。
    #   ★這是上一批(只終止自有行程)換來的代價,必須補回來★
    # ★[2026-08-05 外審第 4 輪 P1-03] 關不掉要掛帳★ 呼叫端已經把 `_psession`
    #   清成 None 了,這裡是最後一個還認得這個 session 的地方。不掛帳就等於
    #   讓一個【仍然登入中】的 HIS 從此無人認領。
    if not _close_session_windows(sess):
        _note_unclosed_session(sess, "teardown 時主畫面沒有關掉")
    try:
        still = (win32event.WaitForSingleObject(sess.hproc, 0)
                 == win32event.WAIT_TIMEOUT)
        if still:
            logging.warning("[session] systemftp(pid=%s) 優雅關閉後仍在 → "
                            "以行程 handle 強制結束", sess.pid)
            win32process.TerminateProcess(sess.hproc, 1)
            win32event.WaitForSingleObject(sess.hproc, 3000)
    except Exception:
        logging.warning("[session] 以 handle 確認/結束 systemftp 失敗", exc_info=True)
    finally:
        for _h in (sess.hthread, sess.hproc):
            try:
                _h.Close()
            except Exception:
                pass


# ★關不掉的 session 要【掛帳】,不可以連參照一起丟★(2026-08-05 外審第 4 輪 P1-03)
#   `_session_close` 是先 `_psession = None` 再去關。關失敗時(HIS 沒回應 WM_CLOSE、
#   hwnd 身分對不上、modal 擋著)我們已經丟掉唯一的參照 —— 那個【仍然登入中】的 HIS
#   從此沒有任何程式碼認得它,下一輪看到「沒有 session」就再冷啟動登入一次。
#   而 systemftp 是啟動器型行程(實機證實),`TerminateProcess` 那條後路對它無效,
#   `_kill_systemftp` 也已經不再 taskkill。淨結果:隱藏桌面上逐次累積登入中的 HIS,
#   沒有任何一段程式碼會再去收它。
#   → 關不掉就掛在這裡,每一輪重試,並且告警(不會自己好的事情要讓人知道)。
_unclosed_lock = threading.Lock()
_unclosed_sessions: list = []


def _note_unclosed_session(sess, reason: str) -> None:
    """關不掉 → 掛帳。已經在帳上的不重複加。

    ★[2026-08-05 外審第 5 輪 P1-03] 沒有上限,一筆都不丟★
    上一版寫了 `if len >= _MAX_UNCLOSED: return` 並在註解宣稱「上限只是防爆,
    不丟掉任何一筆」。★那句話與程式碼不符★:舊的 8 筆確實沒被擠掉,但【新來的
    第 9 筆】被直接丟棄 —— 而那一筆同樣是一個仍然登入中、從此沒人認得的 HIS。
    測試也只驗了「舊的沒被擠掉」,剛好避開了真正的缺陷。

    現在不需要上限:帳上只要有一筆,`_acquire_session` 就【禁止冷啟動】
    (見 `_ensure_no_unmanaged_sessions`),所以這個清單不可能因為我們自己
    再開新 session 而增長。
    """
    with _unclosed_lock:
        if any(s is sess for s in _unclosed_sessions):
            return
        _unclosed_sessions.append(sess)
        depth = len(_unclosed_sessions)
    logging.error("[session] ★關不掉,先掛帳待重試★(pid=%s hwnd=%s):%s"
                  " —— 這個 HIS session 可能仍登入中(帳上共 %d 個)",
                  sess.pid, getattr(sess, "main_hwnd", None), reason, depth)
    _alert_unmanaged_session(depth, reason)


# 告警節流:這種狀況不會自己好,但也不該每 3 分鐘寄一封信。
_UNMANAGED_ALERT_INTERVAL_SEC = 6 * 3600
_unmanaged_alert_at = 0.0


def _alert_unmanaged_session(depth: int, reason: str) -> None:
    """關不掉的 session 要走【開發者告警】通道,不能只寫 log。

    ★[2026-08-05 外審第 5 輪 P2-04]★ 註解一路寫著「掛帳＋告警」,實際上只有
    `logging.error` —— 而使用者看不到隱藏桌面、也不會去翻 log。這種狀況不會
    自行恢復,不主動說就沒有人會知道。
    """
    global _unmanaged_alert_at
    now = time.time()
    if now - _unmanaged_alert_at < _UNMANAGED_ALERT_INTERVAL_SEC:
        return
    _unmanaged_alert_at = now
    body = (f"有 {depth} 個住院系統登入(HIS session)送出關閉命令後仍未關閉。\n"
            f"最近一次的原因:{str(reason)[:200]}\n\n"
            f"程式會先暫停登入約 {_UNMANAGED_BLOCK_MAX_SEC // 60} 分鐘並重試關閉;"
            "仍關不掉時會放行一次恢復嘗試,再重新暫停(避免臨床查詢無限期停擺)。\n"
            "若持續出現,請到該台電腦人工確認隱藏桌面上的住院系統。")

    def _worker():
        try:
            from cmuh_common.smtp_mail import send_mail  # noqa: PLC0415
            send_mail(
                recipients=[str(r) for r in _developer_alert_recipients()],
                subject="會診自動化:有無法關閉的住院系統登入",
                body=body,
                attachment_path=None,
                category="system",
            )
        except Exception:
            # [外審第 6 輪 P2-06] 寄失敗不可沉默 6 小時:把時間戳回撥到
            # 「10 分鐘後可再試」,讓下一輪掛帳重新觸發。
            global _unmanaged_alert_at
            _unmanaged_alert_at = time.time() - _UNMANAGED_ALERT_INTERVAL_SEC + 600
            logging.warning("[session] 掛帳告警寄送失敗(10 分鐘後重試)",
                            exc_info=True)

    try:
        threading.Thread(target=_worker, name="ConsultUnmanagedAlert",
                         daemon=True).start()
    except Exception:
        logging.debug("[session] 掛帳告警執行緒啟動失敗", exc_info=True)


def _retry_unclosed_sessions() -> int:
    """重試帳上關不掉的 session → 回傳仍未關掉的數量。每輪查詢前呼叫。"""
    with _unclosed_lock:
        pending = list(_unclosed_sessions)
    if not pending:
        return 0
    still = []
    for sess in pending:
        try:
            ok = _close_session_windows(sess)
        except Exception:
            logging.debug("[session] 重試關閉掛帳 session 失敗", exc_info=True)
            ok = False
        if ok:
            logging.info("[session] 掛帳的 session 已關掉(pid=%s)", sess.pid)
        else:
            still.append(sess)
    with _unclosed_lock:
        # 只移除這一輪確認關掉的,期間新掛上來的不動
        closed = [s for s in pending if not any(s is t for t in still)]
        for s in closed:
            for i, t in enumerate(_unclosed_sessions):
                if t is s:
                    _unclosed_sessions.pop(i)
                    break
        return len(_unclosed_sessions)


# ★[2026-08-06 外審 P1-03] 與 SMTP 共用【同一個】類別★
#   舊版在這裡自己定義一份,只有 Outlook 逾時會拋;SMTP 逾時走普通 RuntimeError
#   → 被外層當可重試 → 同一封信可能再提交一次。改成 import 之後,兩條寄信路徑
#   拋的是同一個類別,下面 `isinstance(e, DeliveryOutcomeUnknown)` 才涵蓋得到 SMTP。
from cmuh_common.smtp_mail import DeliveryOutcomeUnknown  # noqa: E402


class UnmanagedSessionError(RuntimeError):
    """帳上還有關不掉的 HIS session → 不得再建立新的登入。"""


# ★閘門必須有上限★(2026-08-05 實機事故)
#   fail-closed 的前提是「這個狀況遲早會解除」。當天的實機證明前提不成立:
#   主畫面被 modal 擋住 → disabled → 不處理 WM_CLOSE → 永遠關不掉 → 閘門
#   把整個會診查詢停掉,而且【重開機也一樣】(重開機之後第一輪又走到同一個狀態)。
#   臨床上「會診查詢完全停擺」比「隱藏桌面多一個登入中的 session」嚴重得多,
#   而且後者本來就有 systemftp「最多兩個」的自然上限會讓它自己浮現。
#   → 擋一段時間(讓真的會自己好的情況有機會恢復),超過就放行,但持續告警。
_UNMANAGED_BLOCK_MAX_SEC = 15 * 60
_unmanaged_since = 0.0


def _ensure_no_unmanaged_sessions(*, now=None, block: bool = True) -> None:
    """先重試關掉帳上的 session;還有殘留就【擋下本輪】——但不會擋到天荒地老。

    ★[2026-08-06 外審 P1-02] block=False:只清理、不擋★
    這道閘門要擋的是「在已有未管理 session 時【再開一個新登入】」,而不是
    「使用一個已經存在、身分精確相符、還活著的 session」。舊版把它放在
    `_acquire_session()` 最前面,於是:15 分鐘窗口放行一次 → 冷啟動成功 →
    新 session 健康地留在主畫面 → 3 分鐘後下一輪【還沒檢查那個健康 session】
    就先撞閘門 → UnmanagedSessionError → 剛恢復的 session 拿不到 keepalive →
    很可能在院方 5 分鐘閒置上限後被登出,白白浪費那次恢復。
    現在重用路徑用 block=False:照樣每輪重試關閉殘留、照樣累計窗口,只是不擋;
    真正要冷啟動前才用預設的 block=True。

    ★[2026-08-05 外審第 5 輪 P1-02]★ 上一版只呼叫 `_retry_unclosed_sessions()`
    而【把回傳值丟掉】。於是掛帳機制只做到「我知道有一個關不掉的 session」,
    做不到它自己註解宣稱的「不先收,隱藏桌面上就會同時有兩個登入中的 HIS」。

    ★[2026-08-05 實機事故] 但 fail-closed 不可以沒有上限★
    我當天把它寫成無限期阻擋,理由是「停掉查詢會寄信告警、關掉後自動恢復」。
    ★那個理由預設了『遲早關得掉』,而實機證明關不掉★:主畫面被 modal 擋住時
    是 disabled 的,disabled 的視窗不會處理 WM_CLOSE —— 於是永遠掛在帳上,
    會診查詢從下午 2:52 起全停,使用者重開機也一樣。
    現在:擋 `_UNMANAGED_BLOCK_MAX_SEC`(給真的會自己好的情況一個機會),
    超過就放行並持續告警 —— 寧可多一個沒收乾淨的 session,不可以讓臨床查詢
    無聲停擺。
    """
    global _unmanaged_since
    now = now or time.time
    try:
        remaining = _retry_unclosed_sessions()
    except Exception:
        # 連重試都出錯 → 帳上狀態不明,一樣視為有殘留
        logging.warning("[session] 重試關閉掛帳 session 時出錯", exc_info=True)
        with _unclosed_lock:
            remaining = len(_unclosed_sessions)
    if not remaining:
        _unmanaged_since = 0.0
        return
    if not _unmanaged_since:
        _unmanaged_since = now()
    blocked_for = now() - _unmanaged_since
    if not block:
        # 重用既有健康 session 的路徑:清理與記帳照做,但不擋(見 docstring P1-02)。
        logging.debug("[session] 仍有 %d 個掛帳 session(已 %.0f 分鐘);"
                      "本輪重用既有 session,不擋", remaining, blocked_for / 60.0)
        return
    if blocked_for >= _UNMANAGED_BLOCK_MAX_SEC:
        # ★[外審第 6 輪 P1-06] 放行【一次】,然後重新起算★
        #   上一版超過上限後就永遠放行 —— 閘門變成只擋前 15 分鐘,之後每 3 分鐘
        #   都冷啟動一次,掛帳清單照樣增長,連告警信裡「不會再登入」都成了謊話。
        #   改成:每個窗口放行一次恢復嘗試,之後重新擋滿一個窗口再試。
        _unmanaged_since = now()
        logging.error(
            "[session] ★仍有 %d 個關不掉的 session,已擋滿 %.0f 分鐘★ → "
            "放行【一次】恢復嘗試,之後重新起算;請人工確認隱藏桌面上的住院系統",
            remaining, blocked_for / 60.0)
        return
    raise UnmanagedSessionError(
        f"仍有 {remaining} 個無法確認關閉的住院系統登入 → "
        f"本輪不建立新登入(已擋 {blocked_for / 60.0:.0f} 分鐘,"
        f"最多擋 {_UNMANAGED_BLOCK_MAX_SEC // 60} 分鐘);"
        "每一輪會自動重試關閉,關掉後即自行恢復")


def _session_close(reason: str) -> None:
    """收掉常駐 session(沒有 session 時靜默)。任何執行緒都可呼叫:
    優雅關閉跨桌面可能無效,但 handle 強制結束與桌面無關,結果是確定的。"""
    global _psession
    with _session_lock:
        sess = _psession
        _psession = None
    if sess is None:
        return
    logging.info("[session] 收掉常駐 systemftp(pid=%s):%s", sess.pid, reason)
    _terminate_session_process(sess)


def _session_release(sess) -> None:
    """歸還租約(僅現任;已被收掉/換人則無事)。"""
    with _session_lock:
        if _psession is sess:
            sess.in_use = False


def _session_close_if_current(sess, reason: str) -> bool:
    """[codex P1 R11] 只收掉【自己手上那個】session → 回傳「我是否仍是現任」。

    逾時被接管的舊 worker 之後才走到錯誤處理時,全域 _psession 可能已是新一輪
    建立的【替代 session】——無條件 _session_close() 會把好端端的新 session
    一起殺掉。這裡先比對身分:是現任才清全域參照;不是就只終結自己的行程。
    [codex P1 R12/R13] 回傳值給恢復分支當【所有權證明】:已卸任者不得重登。"""
    global _psession
    with _session_lock:
        current = _psession is sess
        if current:
            _psession = None
    logging.info("[session] 收掉 systemftp(pid=%s,%s):%s", sess.pid,
                 "現任" if current else "已卸任的舊 worker 自理", reason)
    _terminate_session_process(sess)
    return current


# [codex P1 R14] 冷啟動(登入)進行中的預約:worker 在登入途中被 240 秒 join 逾時
# 棄置時,session 還沒發布(_psession=None),下一輪會看到「沒有 session」而並行
# 再登一次 → 兩份帳密、兩個 systemftp、互相蓋掉參照。冷啟動前先在鎖內預約;
# 預約超過 _COLD_START_STALE_SECONDS(> 登入 120s+主畫面 120s 的合法上限)視為
# 孤兒殘留,可搶走。
_cold_start_owner = None            # (身分 token, 開始時間)
_COLD_START_STALE_SECONDS = 360


def _cold_start_session(cfg: dict):
    """冷啟動的【預約閘門】→ 實作見 _cold_start_session_impl。

    同一時間只允許一個冷啟動:另一個仍在進行(未逾期)→ 本輪直接放棄,
    絕不並行送出第二份帳密。

    ★[2026-08-08 外審第 9 輪 P1-01] UNMANAGED 閘門也放這裡★
    舊寫法把閘門接在 `_acquire_session()` 的冷啟動分支上,而【掉線恢復】那條路
    (`_automation_on_hidden` 的 except:teardown 失敗會掛帳 → 立刻重登)是
    直接呼叫本函式的,完全繞過閘門 —— 偏偏那正是「剛剛才關不掉一個 session」
    的時刻,是閘門最該生效的一刻。啟動器 handle 又終止不了實際 UI,於是登入
    會累積、撞上 systemftp「最多兩個」上限。
    ★閘門只能查一次★:超過 15 分鐘上限的那條分支會【放行一次並重新起算】,
    連查兩次的話第二次 `blocked_for≈0` 反而會擋下剛剛放行的那一次。所以
    `_acquire_session` 冷啟動前的那一次已移除,這裡是唯一的阻擋點。
    """
    global _cold_start_owner
    _ensure_no_unmanaged_sessions()
    me = object()
    now = time.time()
    with _session_lock:
        owner = _cold_start_owner
        if owner is not None and (now - owner[1]) < _COLD_START_STALE_SECONDS:
            raise RuntimeError(
                "另一個冷啟動(登入)仍在進行中 → 本輪跳過,不並行送出第二份帳密")
        if owner is not None:
            logging.warning("[session] 發現逾期 %d 秒的冷啟動預約(孤兒殘留) → 搶走",
                            int(now - owner[1]))
        _cold_start_owner = (me, now)
    try:
        return _cold_start_session_impl(cfg, owner_token=me)
    finally:
        with _session_lock:
            if _cold_start_owner is not None and _cold_start_owner[0] is me:
                _cold_start_owner = None


def _cold_start_session_impl(cfg: dict, owner_token=None):
    """啟動 systemftp + 登入 + 等主畫面 → 存成常駐 session(呼叫緒須在隱藏桌面)。

    登入類失敗(LoginNotCompleted / HISStartupBlocked)→ 收掉行程、設【登入冷卻】
    後再拋:3 分鐘 keepalive 節奏絕不可把同一組帳密每 3 分鐘送一次——冷卻期取
    15 分鐘(=舊輪詢節奏),登入壓力不高於改版前;BDE 取 30 分鐘(等重開機/人工)。
    """
    global _psession
    remaining = _keepalive.login_cooldown_remaining(_login_cooldown_until,
                                                    time.time())
    if remaining > 0:
        raise RuntimeError(
            f"登入冷卻中(前次登入失敗,剩 {remaining / 60.0:.0f} 分)——"
            f"不重複送出帳密以免逼近鎖定門檻")
    username = cfg["username"]
    password = cfg["password"]
    before = _systemftp_pids()
    si = win32process.STARTUPINFO()
    si.dwFlags = win32con.STARTF_USESHOWWINDOW
    si.wShowWindow = win32con.SW_SHOW  # 隱藏桌面上正常顯示，使用者看不到
    si.lpDesktop = HIDDEN_DESKTOP_NAME
    # [2026-07-27 實機故障根因] CreateProcess 的行程 handle 一定要留住:
    #   ① handle 沒關,核心就不會回收該 PID → 不可能「PID 重用」而誤殺別人;
    #   ② handle 來自我們自己的 CreateProcess → 終止的必然是我們開的那一個,
    #      完全不需要名稱/session/視窗列舉,資源耗盡時照樣有效。
    try:
        _hproc, _hthread, _spawned_pid, _ = win32process.CreateProcess(
            SYSTEMFTP_PATH, None, None, None, False, 0, None, None, si)
    except Exception as e:
        raise RuntimeError(f"在隱藏桌面啟動 systemftp.exe 失敗：{e}") from e
    logging.info("已在隱藏桌面啟動 systemftp.exe (pid=%s)", _spawned_pid)
    our_pids: set = {_spawned_pid}
    creds_sent = False        # [codex P1 R6] 帳密送出後的任何失敗都要進冷卻
    try:
        # 等登入視窗（期間關多開提示）
        login = None
        saw_multi_instance = False
        deadline = time.time() + 120
        while time.time() < deadline:
            if not running.is_set():
                raise RuntimeError("流程已被中止")
            for ph in find_windows(MULTI_INSTANCE_CLASS, MULTI_INSTANCE_TITLE):
                ok_btn = find_child(ph, "TButton", "OK")
                if ok_btn:
                    click_button(ok_btn)
                    saw_multi_instance = True
                    logging.info("已關閉多開提示視窗")
                    time.sleep(0.6)
            cands = find_windows(LOGIN_CLASS, LOGIN_TITLE_PREFIX)
            # ★[2026-08-05 外審第 5 輪 P1-08] 這裡的「借用」與後備模式不同★
            #   `find_windows` 只列舉【呼叫緒所在桌面】的視窗,而這條路徑已經
            #   SetThreadDesktop 到我們自己建立的隱藏桌面上。醫師的住院系統在
            #   互動桌面,不可能出現在這裡 —— 撿到的必然是我們前幾輪留下的孤兒
            #   (硬退/更新重啟遺留),重用它反而是對的。
            #   後備模式(SW_HIDE)跑在使用者桌面上,那裡就【不可以】借用,見該處。
            fresh = [h for h in cands if _window_pid(h) not in before]
            if cands and not fresh:
                logging.info("隱藏桌面上只有前幾輪殘留的登入視窗 → 重用它"
                             "(pid=%s)", sorted({_window_pid(h) for h in cands}))
            pick = fresh or cands
            if pick:
                login = pick[0]
                break
            time.sleep(0.5)
        if not login:
            if saw_multi_instance:
                raise RuntimeError(
                    "等不到登入視窗（systemftp 疑似已達『最多兩個』上限：隱藏桌面有殘留孤兒"
                    "佔位、或使用者已手動開啟住院系統）")
            raise RuntimeError("等不到登入視窗")
        our_pid = _window_pid(login)
        our_pids = (_systemftp_pids() - before) | {our_pid}
        logging.info("登入視窗 hwnd=%s pid=%s", login, sorted(our_pids))

        # 登入：隱藏桌面上 systemftp 是唯一前景應用,不干擾使用者
        force_foreground(login)
        edits = sorted(
            (c for c in enum_children(login) if c[1] == "TEditExt"),
            key=lambda c: c[3][1])
        if len(edits) < 2:
            raise RuntimeError(f"登入視窗只找到 {len(edits)} 個輸入框")
        # ★焦點有沒有真的落在欄位上,是登入失敗時的另一半證據★(2026-08-10 實機)
        _note_login_focus("帳號", type_via_focus(edits[0][0], login, username))
        _note_login_focus("密碼", type_via_focus(edits[1][0], login, password))
        confirm = find_child(login, "TButton", "確認")
        if not confirm:
            raise RuntimeError("找不到「確認」鈕")
        click_button(confirm)
        creds_sent = True
        logging.info("已送出登入")

        # ★接住回傳的 hwnd★ 以前這行把它丟掉,於是收尾只能靠 PID 差集猜哪個視窗
        #   是自己的 —— 那個集合會混進醫師的行程。見 `_PersistentSession.main_hwnd`。
        _main_hwnd = _wait_main_window_after_login(our_pids, visible_only=True)
        logging.info("已進入主畫面")
        sess = _PersistentSession(_hproc, _hthread, _spawned_pid, our_pids,
                                  main_hwnd=_main_hwnd)
        sess.in_use = True                       # 建立者即持有租約
        # [codex P1 R16] 發布前在鎖內驗「預約還是不是我的」——冷啟動拖過
        # _COLD_START_STALE_SECONDS 被搶走後,本 worker 若仍走到這裡,無條件發布
        # 會蓋掉接手者的 session → 兩個已登入的 HIS 行程、參照遺失。
        with _session_lock:
            stolen = (owner_token is not None
                      and (_cold_start_owner is None
                           or _cold_start_owner[0] is not owner_token))
            if not stolen:
                _psession = sess
        if stolen:
            logging.warning("[session] 冷啟動完成時預約已被搶走(本 worker 逾時"
                            "被接管) → 不發布,自行收掉剛開的 systemftp")
            _terminate_session_process(sess)
            raise RuntimeError("冷啟動完成但已被新一輪接管 → 本輪放棄")
        logging.info("[session] 常駐 session 已建立(pid=%s):停在主畫面,"
                     "之後每輪只按查詢再退回", _spawned_pid)
        return sess
    except BaseException as e:
        if isinstance(e, HISStartupBlocked):
            _set_login_cooldown_until(
                time.time() + _keepalive.BDE_COOLDOWN_SECONDS)
        elif isinstance(e, LoginNotCompleted) or creds_sent:
            # [codex P1 R6] 帳密【已送出】後的任何啟動失敗(例:登入視窗消失但
            # 主畫面沒出現的通用 RuntimeError)都要進冷卻——不然 3 分鐘節奏會
            # 每 3 分鐘再送一次帳密,鎖定防護只剩 LoginNotCompleted 一種等於沒防。
            _set_login_cooldown_until(
                time.time() + _keepalive.LOGIN_COOLDOWN_SECONDS)
            logging.error("[session] 登入未完成(帳密已送出) → 進入 %d 分鐘"
                          "登入冷卻(不重複送出帳密)",
                          _keepalive.LOGIN_COOLDOWN_SECONDS // 60)
        _terminate_session_process(
            _PersistentSession(_hproc, _hthread, _spawned_pid, our_pids))
        raise


def _retire_session_if_no_keepalive(sess, cfg: dict) -> None:
    """[codex P2 R21] 休息時段(00-06)的 email/手動觸發:查完【不留】session。

    休息時段沒有輪詢 keepalive,留著 5 分鐘就被院方登出,已登出的 systemftp
    呆掛到 06:00 才被下一輪收掉——查完直接收,06:00 後首輪照常冷啟動。"""
    try:
        if _in_quiet_hours(datetime.now(), load_config()):
            _session_close_if_current(
                sess, "休息時段觸發的查詢:查完即收(無 keepalive 可維持)")
    except Exception:
        logging.debug("[session] 休息時段收尾判斷失敗(略過)", exc_info=True)


def _acquire_session(cfg: dict):
    """取用常駐 session:死了/到 6 小時定期重啟 → 收掉冷啟動,否則直接重用。

    [codex P1 R12] 租約制:上一輪逾時被棄置的 worker 若仍握著 session(in_use),
    本輪【奪走並終結】後冷啟動——絕不兩個 worker 共用;被終結的舊 worker 隨後的
    操作會失敗,由它自己的錯誤處理走 _session_close_if_current(非現任→只自理)。"""
    global _psession
    # ★[2026-08-05 外審第 4 輪 P1-03] 先把上次關不掉的收乾淨★
    #   每一輪查詢都是必經之路,所以清理放這裡(不清,隱藏桌面上會累積登入中的 HIS)。
    # ★[2026-08-06 外審 P1-02] 但這裡【只清不擋】★
    #   此刻還不知道要不要開新 session ——「重用一個已存在且健康的 session」不該被
    #   擋(擋了會害剛恢復的 session 拿不到 keepalive、5 分鐘後被院方登出)。
    #   真正的阻擋移到下方冷啟動前。
    _ensure_no_unmanaged_sessions(block=False)
    stale = None
    with _session_lock:
        sess = _psession
        if sess is not None and sess.in_use:
            _psession = None
            stale, sess = sess, None
        elif sess is not None:
            sess.in_use = True                   # 取得租約
    if stale is not None:
        logging.warning("[session] 前一輪逾時的 worker 仍握著 session(pid=%s) → "
                        "終結後冷啟動,絕不共用", stale.pid)
        _terminate_session_process(stale)
    if sess is not None:
        death = _session_death_reason(sess)
        if death:
            # ★說出【哪一個】原因★(2026-08-04 實機):兩個成因的修法完全不同，
            #   而舊訊息把它們寫成同一句，於是 45 分鐘的 log 也判斷不出來。
            _session_close_if_current(sess, f"session 已死：{death}")
            sess = None
        elif _keepalive.session_needs_restart(sess.started_at, time.time()):
            _session_close_if_current(
                sess,
                f"定期重啟(常駐已逾 {_keepalive.SESSION_MAX_AGE_HOURS:.0f} 小時,"
                f"清 BDE/資源殘留)")
            sess = None
    if sess is not None:
        return sess                      # 重用既有健康 session:不經過阻擋閘門
    # ★[2026-08-08 外審第 9 輪 P1-01] 阻擋已移進 `_cold_start_session`★
    #   上面的 teardown 若失敗會把該 session 掛帳,而阻擋要涵蓋【每一條】冷啟動
    #   路徑(還有掉線恢復那條),所以放在唯一的冷啟動入口,不放這裡 ——
    #   兩邊都查的話,15 分鐘上限「放行一次」會被第二次查詢立刻擋回去。
    return _cold_start_session(cfg)


def _consult_form_dismissed(hwnd: int) -> bool:
    """會診單這一張表單是不是已經不在畫面上了。

    ★這裡看可見性是【對的】★ 與 `_window_destroyed` 的情境相反:Delphi 的
    modal form 關閉後只是 Hide(e348d27 教訓),所以對「這張表單還擋在畫面上嗎」
    來說,「看不見了」就是答案。而 `_window_destroyed` 回答的是「HIS session
    關掉了嗎」—— 那裡看不見【不能】當成關掉了。同一個 API、不同的問題。

    查不到 → False(不知道就不宣稱已經退回主畫面)。
    """
    try:
        if not win32gui.IsWindow(hwnd):
            return True
        return not win32gui.IsWindowVisible(hwnd)
    except Exception:
        logging.debug("[session] 查詢會診視窗狀態失敗 hwnd=%s", hwnd, exc_info=True)
        return False


_MAIN_REENABLE_TIMEOUT = 6.0     # 等主畫面重新可操作的上限(秒)
_MAIN_REENABLE_INTERVAL = 0.25


def _main_ready_for_next_cycle(sess, *, sleep=None, now=None) -> str:
    """主畫面是不是回到「可以再下一次命令」的狀態 → "" 表示可以,否則是原因。

    ★[2026-08-05 外審第 5 輪 P2-02]★ 舊版只確認「看不到會診視窗了」就宣布
    session 續留。會診視窗收掉、但主畫面被另一個 modal 擋住時,那句「續留」是
    錯的。`IsWindowEnabled` 正是「被 modal 擋住」的正規訊號。

    ★[2026-08-05 實機事故] 但【不可以只取樣一次】★
    我第一版在 `_consult_form_dismissed` 回 True 之後【立刻】問一次 enabled,
    當天下午三次全部答「disabled」→ 三次都收掉 session → 三次都關不掉 →
    掛帳閘門把整個會診查詢停掉。診間 log:

        14:52:28,451 擷取完成
        14:52:28,762 會診單已收掉,但主畫面沒回到可操作狀態:被 modal 擋住
        14:52:31,765 ★主畫面關不掉★

    相隔 311 毫秒。Delphi 的 modal form 是【先 Hide、後把 owner 重新 enable】,
    而 `_consult_form_dismissed` 把「看不見」就算退場 —— 我剛好卡在那個縫裡問。
    改成【輪詢】等它恢復;真的一直沒恢復才算異常。
    """
    sleep = sleep or time.sleep
    now = now or time.monotonic
    death = _session_death_reason(sess)
    if death:
        return death
    deadline = now() + _MAIN_REENABLE_TIMEOUT
    last = ""
    while True:
        try:
            if win32gui.IsWindowEnabled(sess.main_hwnd):
                return ""
            last = "主畫面仍被 modal 對話框擋住(disabled)"
        except Exception:
            last = "無法查詢主畫面是否可操作"
        if now() >= deadline:
            return last
        sleep(_MAIN_REENABLE_INTERVAL)


# 這些是「內容視窗」,不是擋路的對話框 —— 它們被擋住的時候是 disabled 的。
#
# ★TApplication 是 Delphi 的基礎建設視窗,不是對話框★(2026-08-05 實機)
#   每個 Delphi 程式都有一個 class 為 `TApplication` 的隱形擁有者視窗,
#   它有 WS_VISIBLE、永遠 enabled、而且【沒有任何按鈕】。第一版把它算成
#   「擋路的對話框」,於是每一輪都印一行「有擋路的對話框但沒有可按的按鈕」——
#   那是雜訊,而 log 正是我們唯一的實機診斷管道,不能讓它被固定雜訊淹掉。
#   (實害只有雜訊:它沒有按鈕,所以本來就不會被按。)
_CONTENT_CLASSES = frozenset({MAIN_CLASS, LOGIN_CLASS, CONSULT_CLASS,
                              "TApplication"})

# 「不認得的對話框」警告:同一個 class 只講一次,不要每 0.4 秒刷一行。
_reported_unknown_dialogs: set = set()
# 只按這些字樣的按鈕。★絕不盲按★:不認得的對話框只記下它有哪些按鈕,不出手。
_AFFIRMATIVE_CAPTIONS = ("確認", "確定", "OK", "Ok", "是", "繼續")
# ★class 專屬按鈕★(2026-08-06 實機回報,正是「請回報這一行」等的那筆):
#   TFMTimeOut_1 = 院方【閒置逾時】對話框,按鈕=繼續使用/離開系統/重新簽入。
#   「繼續使用」不在泛用肯定字樣裡(精確比對,"繼續"≠"繼續使用") → 整個上午
#   每輪收尾都被它擋住:modal 擋住的主畫面是 disabled,不吃 WM_CLOSE,
#   於是 session 關不掉、掛帳累積、每輪告警。
#   一律按「繼續使用」:只把 modal 收掉、讓主畫面恢復 enabled——常駐要續命
#   靠它,收尾也要主畫面先恢復 enabled 才關得掉。★絕不按「離開系統/重新簽入」★
#   (會改變 session 狀態;收尾該怎麼關由 teardown 自己的流程決定)。
_CLASS_SPECIFIC_CAPTIONS = {"TFMTimeOut_1": ("繼續使用",)}


def _is_blocking_dialog(hwnd: int) -> bool:
    """單一視窗:它是不是「擋路的對話框」(可見性另外判)。

    ★抽出來是為了讓兩個地方【不可能各自漂移】★(外審 SG 第 2 輪 P2):
    `_blocking_dialogs` 用它來【找】,`_run_with_sw_hide` 的隱形執行緒用它來
    【放過】。兩邊若各寫一份判準,隱形執行緒就會把偵測要找的東西藏掉 ——
    那正是這條 finding 的內容。
    """
    try:
        if not win32gui.IsWindowEnabled(hwnd):
            return False            # 自己都被擋住 → 它不是擋路的那個
        return win32gui.GetClassName(hwnd) not in _CONTENT_CLASSES
    except Exception:
        return False


def _blocking_dialogs(pids: set) -> list:
    """找出【正在擋住輸入】的對話框 → [(hwnd, class)]。

    ★[2026-08-05 實機 log 的關鍵發現]★ 診斷傾印把答案直接寫出來了:

        TFrmLogin(vis=1,en=0)      ← 我們的登入視窗,被擋住
        TFMTimeOut_1(vis=1,en=1)   ← ★唯一 enabled 的,它才是擋路的那個★
        TFMShowMessage(vis=1,en=0) ← 通知視窗,它自己也被擋住
        TFMNewMain(vis=1,en=0)     ← 主畫面,被擋住

    而程式對著【disabled 的】TFMShowMessage 按了 6 次「確認」——
    disabled 的視窗根本不會處理點擊,所以整整 120 秒的登入預算就這樣燒光,
    最後回報「登入沒有完成」。`TFMTimeOut_1` 這個 class 程式從來不認得。

    ★所以判準不可以是「class 是不是 NOTICE_CLASS」★ —— 那是在猜對話框長什麼樣。
    Win32 已經有正規訊號:modal 會把其他視窗 disable,而它自己是 enabled 的。
    改成:可見 + enabled + 不是內容視窗 = 它就是擋路的那個,不管它叫什麼名字。
    """
    out = []
    if not pids:
        return out
    try:
        for hwnd in find_windows(pids=set(pids), visible_only=True):
            if not _is_blocking_dialog(hwnd):
                continue
            try:
                out.append((hwnd, win32gui.GetClassName(hwnd)))
            except Exception:
                continue
    except Exception:
        logging.debug("[session] 列舉擋路對話框失敗", exc_info=True)
    return out


def _dismiss_blocking_modals(sess=None, *, pids=None,
                             record_text: bool = False) -> int:
    """把擋住輸入的對話框按掉 → 按了幾個。

    `record_text=True` 只由 `_wait_main_window_after_login` 傳入 ——
    ★那是唯一一條「主畫面都還沒交出來」的路徑★,畫面上不可能有病人資料
    (邊界的完整說明見 `_note_login_dialog` 上方)。其餘所有呼叫端一律維持
    「不記任何視窗文字」的既有原則,預設就是 False。

    ★[2026-08-05 實機事故]★ 主畫面被 modal 擋住時它是 disabled 的 ——
    **disabled 的視窗不會處理 WM_CLOSE**。所以「收尾關不掉主畫面」與「主畫面
    被 modal 擋住」根本是同一件事的兩個症狀,而我當天把前者當成獨立故障、
    還讓它去觸發一個 fail-closed 閘門,結果是整個會診查詢停擺。

    ★只按【我們自己的行程】的對話框★,而且★只按肯定字樣的按鈕★ ——
    不認得的對話框只把它的 class 與按鈕字樣記進 log(讓下一次知道它長什麼樣),
    絕不盲按。在醫院系統上亂點按鈕的代價,比多停一輪查詢大得多。
    """
    owner = set(pids or ())
    if not owner:
        one = getattr(sess, "main_pid", None)
        if one:
            owner = {one}
    if not owner:
        return 0
    clicked = 0
    for hwnd, cls in _blocking_dialogs(owner):
        try:
            # ★[2026-08-06 深度穩定,實機 log 09:06]★ class=#32770 按鈕=[]:
            #   原生 Win32 對話框(MessageBox/驅動程式跳窗)的按鈕 class 是
            #   "Button",不是 Delphi 的 "TButton" —— 只列 TButton 等於原生
            #   對話框【永遠】按不掉,擋住就只能等 15 分鐘放行。
            #   字樣白名單不變,原生的「確定/OK」一樣只按肯定鈕。
            buttons = [(h, (t or "").strip())
                       for h, c, t, _r in enum_children(hwnd)
                       if c in ("TButton", "Button")]
        except Exception:
            logging.debug("[session] 列舉對話框按鈕失敗 hwnd=%s", hwnd,
                          exc_info=True)
            continue
        wanted = _CLASS_SPECIFIC_CAPTIONS.get(cls, _AFFIRMATIVE_CAPTIONS)
        target = next((h for h, t in buttons if t in wanted), None)
        if record_text:
            # ★按掉之前就要記★ 按下去之後那個視窗通常就消失了 ——
            #   2026-08-10 那台正是「按了一下、然後永遠不知道它寫什麼」。
            _note_login_dialog(hwnd, cls, [t for _h, t in buttons])
        if target is None:
            # 同一個 class 只講一次:這個迴圈每 0.4 秒跑一次,不節流會把 log 洗掉
            # (2026-07-29 就發生過 1,568 行幾乎全是同一句的實機 log)。
            if cls not in _reported_unknown_dialogs:
                _reported_unknown_dialogs.add(cls)
                logging.warning("[session] 有擋路的對話框但沒有可按的按鈕 —— "
                                "class=%s 按鈕=%s(不盲按,請回報這一行)",
                                cls, [t for _h, t in buttons])
            continue
        click_button(target)
        clicked += 1
        logging.info("[session] 已按掉擋路的對話框 class=%s(按鈕=%s)", cls,
                     next(t for h, t in buttons if h == target))
    return clicked


def _return_to_main(sess, consult_hwnd) -> None:
    """按「回」退回主畫面(後備:WM_CLOSE=右上角X)。

    ★關不掉 → 收掉 session、下一輪冷啟動,但絕不拋例外★——查詢本身已成功,
    不能因退場失敗把已到手的結果丟掉、更不能觸發整套殺掉重啟再查一次。

    ★[2026-08-05 外審第 5 輪 P1-01/P2-02] 只看【我們這一張】會診單★
    舊版判準是 `not find_windows(CONSULT_CLASS, pids=sess.our_pids)` ——
    「污染的 PID 集合裡看不到任何會診視窗」。兩個方向都會錯:
      * 醫師自己開著一張會診單 → 我們這張明明關掉了,卻永遠等不到「都沒有」
        → 誤判退場失敗 → 收掉一個健康的 session、下一輪重新送帳密。
      * 反之亦然(我們這張還在、別人的先消失)不會被發現。
    改成只觀察 `consult_hwnd` 這一個 handle,並且確認主畫面真的回到可操作。
    """
    try:
        back = find_child(consult_hwnd, "TButton", "回")
        if back:
            click_button(back)
        else:
            win32gui.PostMessage(consult_hwnd, win32con.WM_CLOSE, 0, 0)
        deadline = time.time() + 8
        while time.time() < deadline:
            if _consult_form_dismissed(consult_hwnd):
                break
            time.sleep(0.3)
        else:
            win32gui.PostMessage(consult_hwnd, win32con.WM_CLOSE, 0, 0)
            deadline = time.time() + 5
            while time.time() < deadline:
                if _consult_form_dismissed(consult_hwnd):
                    logging.info("[session] 以 WM_CLOSE(=右上角X) 收掉會診單")
                    break
                time.sleep(0.3)
            else:
                _session_close_if_current(
                    sess, "會診視窗關不掉(退場失敗) → 下一輪冷啟動")
                return
        not_ready = _main_ready_for_next_cycle(sess)
        if not_ready:
            # ★[2026-08-05 實機事故] 先按掉 modal,不要直接收掉 session★
            #   主畫面 disabled = 被 modal 擋住 = 它不會處理 WM_CLOSE,
            #   所以「收掉 session」在這個狀態下【必然失敗】,只會把一個可以
            #   自己恢復的暫態變成掛帳、然後被閘門停掉整個查詢。
            if _dismiss_blocking_modals(sess):
                not_ready = _main_ready_for_next_cycle(sess)
        if not_ready:
            # 按掉之後仍然不可操作 → 這個 session 確實不能再用了。收尾時
            # `_close_session_windows` 會再試一次按掉 modal 才送 WM_CLOSE。
            _session_close_if_current(
                sess, f"會診單已收掉,但主畫面沒回到可操作狀態:{not_ready}")
            return
        logging.info("[session] 已退回主畫面,session 續留")
        return
    except Exception:
        logging.warning("[session] 退回主畫面時例外", exc_info=True)
    _session_close_if_current(sess, "會診視窗關不掉(退場失敗) → 下一輪冷啟動")


def _query_cycle(sess, cfg: dict, roster_label: str) -> tuple:
    """在既有 session 上跑一輪:選單→會診單→截圖/擷取→退回主畫面。
    回傳 (截圖路徑, 擷取文字, 擷取HTML, roster_texts);失敗拋例外
    (呼叫端 _automation_on_hidden 會殺掉重啟、重新登入一次)。

    ★[2026-08-05 外審第 5 輪 P1-01] 操作面也必須用確切身分★
    上一批(批次S)把【判活】與【收尾】改成用 `sess.main_hwnd`,卻把這裡漏掉了 ——
    真正送出命令的那一行仍然是:

        mains = find_windows(MAIN_CLASS, pids=sess.our_pids)
        main_hwnd = mains[0]          # ← 列舉順序決定的「第一個」

    而實機 log 已證實 `our_pids` 每一次登入都混著醫師自己的 systemftp
    (`登入視窗 hwnd=... pid=[8036, 16276]`,三次登入三次都要在收尾時排除一個)。
    於是可能發生:**用確切身分確認自己的 session 健康,下一行卻從污染的集合裡
    挑出醫師正在用的那個 HIS,對它送出「我的會診清單」命令** —— 醫師畫面會
    自己跳出會診單,我們還會把他螢幕上的病人清單擷取下來寄出去。

    會診視窗同理:`hits[0]` 會撿到醫師自己已經開著的會診單。改成只認
    【本次命令送出【之後】才出現、而且屬於我們主畫面那個行程】的視窗。
    """
    # ★操作之前先確認手上這個 session 還是我們那一個、而且【可以操作】★
    # ★[2026-08-08 外審第 9 輪 P2-01] 只判「活著」不夠★
    #   `_session_death_reason` 只看 hwnd 還在、身分相符。主畫面被閒置提示
    #   (實機見過的 `TFMTimeOut_1`)擋住時它是 disabled 的 —— 判活會過,
    #   但 disabled 的視窗不會處理我們 PostMessage 過去的 WM_COMMAND。
    #   於是:命令送出去像是成功了 → 等滿 60 秒等不到會診視窗 → 走恢復路徑
    #   拆掉一個【只要按一下「繼續使用」就好】的 session。這與 2026-08-05
    #   實機事故是同一個形狀,只是換了一個位置(那次在退場、這次在進場)。
    #   退場路徑已經有「按掉 modal → 輪詢重新 enabled」,進場也要有,而且要在
    #   【送命令之前】就中止,不能把命令送進一個不會處理它的視窗。
    not_ready = _main_ready_for_next_cycle(sess)
    if not_ready and _dismiss_blocking_modals(sess):
        not_ready = _main_ready_for_next_cycle(sess)
    if not_ready:
        raise RuntimeError(f"開始查詢前主畫面不可操作:{not_ready}")
    main_hwnd = sess.main_hwnd
    owner_pid = sess.main_pid
    cmd_id = resolve_menu_command_id(main_hwnd)
    if cmd_id is None:
        raise RuntimeError(
            "無法確認「我的會診清單」選單命令(疑似住院醫囑系統改版),"
            "本次中止以免對醫囑系統送出不明命令")
    # ★命令送出【前】的既有會診視窗要先記下來★ 之後只認新出現的那一個。
    #   候選集用 `owner_pid`(我們登入的那個行程),不是 `our_pids`。
    # ★[外審第 6 輪 P1-02] Delphi 常重用 form:同一個 hwnd 由隱藏轉可見★
    #   只認「命令後【新出現】的 hwnd」的話,上一輪被 Hide 的那張會診單若被
    #   HIS 重新 Show(同一個 hwnd),永遠不會被採認 → 每輪等滿 60 秒失敗。
    #   記下命令前每個 hwnd 的可見狀態:採認條件 = 新 hwnd,或「原本不可見、
    #   命令後轉為可見」的同一個 hwnd。
    def _visible(h):
        try:
            return bool(win32gui.IsWindowVisible(h))
        except Exception:
            # ★[2026-08-06 外審第 7 輪 P1-05] 查不到是第三態,不是「明確隱藏」★
            #   回 False 的話,一張【命令前就可見】的舊表單只要那一刻查詢失敗,
            #   就會在命令後被誤判成「hidden → visible」而被採認。
            #   這是這幾天一路在修的同一個病灶:「讀不到」被當成某個確定的答案。
            return None                    # UNKNOWN
    before_consults = {h: _visible(h)
                       for h in find_windows(CONSULT_CLASS, pids={owner_pid},
                                             visible_only=False)}
    if any(v is not False for v in before_consults.values()):
        # 上一輪沒有退回主畫面。不當成本輪結果(那是舊資料),照樣往下走 ——
        # 若命令沒有生出新視窗就會逾時,交給既有的恢復路徑。
        logging.warning("[session] 送命令前已有 %d 個會診視窗(上一輪未退回主畫面)"
                        " → 本輪只採認新出現的那一個", len(before_consults))
    win32gui.PostMessage(main_hwnd, win32con.WM_COMMAND, cmd_id, 0)
    logging.info("已送出選單命令（id=%s）", cmd_id)
    consult = None
    deadline = time.time() + 60
    while time.time() < deadline:
        if not running.is_set():
            raise RuntimeError("流程已被中止")
        # ★採認條件(外審第 7 輪 P1-05)★ 兩種都要求【現在明確可見】:
        #   * 命令後才出現的新視窗 —— Delphi 常「先建立 → 載入資料 → 最後才
        #     Show」,在它 Show 之前就開始擷取,會拿到一張還沒填好的表單。
        #   * 命令前【明確隱藏】、現在轉為可見的同一個視窗(form 重用)。
        #   命令前已可見、或可見性未知的,一律不採認。
        hits = [h for h in find_windows(CONSULT_CLASS, pids={owner_pid},
                                        visible_only=False)
                if _visible(h) is True
                and (h not in before_consults or before_consults[h] is False)]
        if hits:
            consult = hits[0]
            break
        time.sleep(0.3)
    if not consult:
        raise RuntimeError("等不到會診單視窗")
    logging.info("會診單視窗已開啟，準備擷取")
    # ★[2026-08-04 外審 P1-08] 不在這裡落地★
    #   這裡以前無條件 `img.save()`,而且發生在解析 roster【之前】——跟有沒有新
    #   會診完全無關。常駐模式 3 分鐘一輪 = 每小時 20 張沒寄出去、也沒有臨床用途
    #   的完整病人畫面躺在磁碟上。改成先留在記憶體,真的要寄信時才落地
    #   (見 `_materialize_shot`)。
    # ★[2026-08-05 外審第 4 輪 P1-09] 先等清單穩定再截圖★
    #   原本是固定 `time.sleep(1.8)` 之後截圖,而清單是在那之【後】才等到穩定的
    #   → 信裡的清單是 Tn 的、附圖是 T0+1.8s 的,醫師拿到兩份互相矛盾的證據。
    #   固定睡的那 1.8 秒也一併拿掉(穩定判定本身就已經在等)。
    img, _snap = _capture_with_settled_roster(consult)
    extracted, extracted_html, roster_texts = _extract_consult_text(
        consult, cfg, roster_label, settled=_snap)
    _return_to_main(sess, consult)
    return img, extracted, extracted_html, roster_texts


def _automation_on_hidden(cfg: dict, roster_label: str = "今日會診病人") -> tuple:
    """在隱藏桌面執行一輪查詢(呼叫者需已 SetThreadDesktop)。回傳
    (截圖, 文字, HTML, roster_texts)。

    [2026-08-03 使用者定案] 常駐登入:重用活著的 session,只有第一次/掉線/定期
    重啟才冷啟動登入。掉線恢復=「整個殺掉重啟、重新登入一次;能登入就繼續常駐,
    再失敗就放棄讓既有告警機制發信」。登入類失敗不在此重試(冷卻期見
    _cold_start_session)。
    """
    sess = _acquire_session(cfg)
    try:
        result = _query_cycle(sess, cfg, roster_label)
        _session_release(sess)
        _retire_session_if_no_keepalive(sess, cfg)
        return result
    except (LoginNotCompleted, HISStartupBlocked, JobSuperseded):
        _session_close_if_current(sess, "登入類/接管失敗")
        raise
    except Exception as e:
        if not running.is_set():
            _session_close_if_current(sess, "流程被中止")
            raise
        # 掉線恢復(使用者定案):殺掉重啟、重新登入一次;能登入就繼續常駐
        logging.warning("[session] 查詢失敗(%s) → 殺掉重啟、重新登入一次", e)
        was_current = _session_close_if_current(sess, "查詢失敗 → 殺掉重啟")
        # [codex P1 R13] 已卸任(=本 worker 是逾時被接管的孤兒,新一輪已經在跑)
        # → 不得重登:兩個 worker 各開一個 session 會互相蓋掉 _psession、
        # 重複送帳密,單一 session/鎖定防護全破。孤兒到此為止。
        if not was_current:
            raise RuntimeError(
                "本輪已被新一輪接管(session 已換代) → 不重登、不再碰 HIS") from e
        sess = _cold_start_session(cfg)
        try:
            result = _query_cycle(sess, cfg, roster_label)
            _session_release(sess)
            _retire_session_if_no_keepalive(sess, cfg)
            return result
        except BaseException:
            _session_close_if_current(sess,
                                      "重啟重登後查詢仍失敗 → 放棄(交給告警機制)")
            raise


# =============================================================================
# BDE 錯誤 → 閒置自動重開機(2026-08-03 使用者定案)
# =============================================================================
_bde_watch_lock = threading.Lock()
_bde_watch_active = False
_bde_watch_gen = 0        # [codex P2 R4] 事故世代:退場瞬間有新事故 → 接力開新看守
_bde_shutdown_pending = False   # [codex P1 R19] 已下達 shutdown /r 且尚未取消
_bde_reboot_cancel = threading.Event()


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _user_idle_seconds_or_none():
    """使用者最後一次鍵盤/滑鼠輸入距今秒數(GetTickCount 回繞安全)。

    → float,或 None=【查不出來】。
    ★查不出來不可以用 0.0 冒充「剛剛有輸入」★(2026-08-04 外審 P1-06):
    倒數期間要據此寫 log,把「量到使用者回來了」和「根本量不到」講成同一件事
    就是在說程式不知道的事(措辭鐵律)。兩者的【處置】相同(都取消重開),但
    【說法】必須不同,否則事後看 log 無從分辨機器為什麼沒修好。
    """
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if not _user32.GetLastInputInfo(ctypes.byref(lii)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount()
        return ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


def _user_idle_seconds() -> float:
    """同上,但查詢失敗回 0=當作剛有輸入 → 寧可不重開,絕不誤重開。"""
    v = _user_idle_seconds_or_none()
    return 0.0 if v is None else v


# 倒數期間留給 `shutdown /a` 執行的餘裕。
#
# ★[2026-08-08 外審] 餘裕要從【OS 的倒數】拿,不可以從【監測時間】扣★
#   舊做法是下 `/t 60` 卻只監測 55 秒 —— 最後 5 秒不再讀 idle time,
#   醫師若在那時回座打字,機器照重開。那是一個「用停止偵測換來的安全窗」,
#   而它換掉的正是這個功能唯一的前提:沒有人在用這台電腦。
#   現在:OS 倒數下 `_REBOOT_COUNTDOWN_SEC`(65 秒),監測完整
#   `_REBOOT_WATCH_SEC`(60 秒),餘裕仍是 5 秒 —— 兩邊都拿到,不用互相犧牲。
_REBOOT_CANCEL_MARGIN_SEC = 5.0
_REBOOT_WATCH_SEC = 60.0                                   # 要監測滿的秒數
_REBOOT_COUNTDOWN_SEC = _REBOOT_WATCH_SEC + _REBOOT_CANCEL_MARGIN_SEC


# 倒數結束的原因 → 取消重開的理由。★沒有列在這裡的原因就是「讓它重開」★
_COUNTDOWN_ABORT_REASONS = {
    "cancelled": "倒數期間 HIS 已恢復",
    "user_back": "★倒數期間使用者回來操作★",
    "idle_unknown": "倒數期間查不出使用者閒置時間(可能有人在)",
}


def _finish_bde_reboot(outcome: str, *, rollback, run=None) -> None:
    """倒數監測結束後的收尾。★這裡才是「要不要真的重開」的決定點★

    ★[2026-08-08 外審第 2 回]★ 舊做法是「監測 N 秒 → 剩下的交給 OS 倒數」,
    於是監測結束到 OS 重開之間必然有一段沒有人在看的時間 —— 不管那段是
    5 秒還是 1 秒,使用者在那時回來都會被重開。把餘裕從監測時間裡扣、
    或把 OS 倒數拉長,都只是把那個窗口搬到別的位置。

    現在:
      1. 監測期間有人回來/HIS 恢復/查不出 → 照舊 `shutdown /a` 取消。
      2. 監測滿了而且一路都沒人 → **先取消排定的重開**,再取最後一次 idle 樣本;
         仍然沒人才下 `shutdown /r /t 0`(立即)。
         最後一次觀測與重開之間不再有無人看守的時間。
      3. 取消不掉 → 原本排定的重開仍會發生(那是既有行為,而且是安全的:
         它本來就要重開)。
    """
    if outcome != "elapsed":
        _abort_reboot_if_needed(outcome, rollback=rollback, run=run)
        return
    runner = run or (lambda cmd: subprocess.run(
        cmd, capture_output=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    # 先把排定的那一次取消掉,才有辦法「看完最後一眼再決定」。
    try:
        cpa = runner(["shutdown", "/a"])
        if cpa.returncode != 0:
            logging.warning("[BDE] 監測期滿但取消不掉排定的重開(rc=%s)→ "
                            "讓原本排定的重開發生", cpa.returncode)
            return                      # 排定的重開仍會來,結果一樣是重開
    except Exception:
        logging.warning("[BDE] 監測期滿但 shutdown /a 失敗 → "
                        "讓原本排定的重開發生", exc_info=True)
        return
    # ★最後一眼★ 查不出來就當作有人(與倒數期間同一個保守方向)。
    idle = _user_idle_seconds_or_none()
    threshold = float(_keepalive.BDE_REBOOT_MIN_IDLE_SECONDS)
    if idle is None or idle < threshold:
        logging.error("[BDE] 監測期滿後的最後一次確認:%s → 不重開",
                      "查不出閒置時間" if idle is None
                      else f"使用者已回來(閒置僅 {idle:.0f} 秒)")
        _bde_rollback_after_abort(rollback, "最後一刻確認到有人/查不出閒置")
        return
    # ★[2026-08-08 外審 P2-01] 這一段必須與「退出時取消重開」互斥★
    #   上面那個 `shutdown /a` 已經把排定的重開取消掉了 —— 從那一刻到下面
    #   `/r /t 0` 之間,作業系統其實【沒有】排定中的重開機。使用者若剛好在這
    #   個窗口按退出:`_abort_bde_shutdown_on_exit` 會看到 `pending=True`、
    #   跑一個取消不到任何東西的 `shutdown /a`(rc!=0,log 寫「機器仍將重開」),
    #   然後這裡照樣下 `/r /t 0` —— **使用者剛剛關掉程式,機器立刻重開**。
    #   `_bde_reboot_cancel` 是退出時一定會設的旗標,所以:
    #     ① 拿鎖,讓「檢查旗標 → 下重開令」與退出那側的 `/a` 不能交錯;
    #     ② 拿到鎖之後【再看一次】旗標,設了就不重開。
    #   拿不到鎖(退出那側正在跑 `/a`)也一樣不重開:那正是「程式要收掉了」。
    #   不重開不等於放棄修復 —— 時間戳會回滾,下一輪重新判定。
    cancel_why = ""
    if not _bde_watch_lock.acquire(timeout=20):
        cancel_why = "取不到重開機鎖(退出流程正在取消)"
    else:
        try:
            if _bde_reboot_cancel.is_set():
                cancel_why = "下達前偵測到取消令(使用者退出/HIS 恢復)"
            else:
                try:
                    cp = runner(
                        ["shutdown", "/r", "/t", "0", "/c",
                         "皮膚科會診查詢:HIS BDE 初始化失敗,閒置自動重開機修復"])
                    if cp.returncode != 0:
                        logging.error("[BDE] 立即重開下達失敗(rc=%s)→ 本次不重開,"
                                      "交給下一輪", cp.returncode)
                        cancel_why = "立即重開下達失敗"
                except Exception:
                    logging.error("[BDE] 立即重開下達失敗 → 本次不重開",
                                  exc_info=True)
                    cancel_why = "立即重開下達例外"
        finally:
            # ★一定要先放掉再回滾★ `_bde_rollback_after_abort` 自己會拿同一把
            #   鎖,而 threading.Lock 不可重入 —— 在鎖裡呼叫它會死鎖。
            _bde_watch_lock.release()
    if cancel_why:
        logging.error("[BDE] %s → 不重開", cancel_why)
        _bde_rollback_after_abort(rollback, cancel_why)


def _bde_rollback_after_abort(rollback, why: str) -> None:
    """取消重開之後把時間戳回滾(沒真的重開就別吃掉 24 小時防護)。

    ★[2026-08-08 外審第 3 回] `rollback` 是【單引數】的★
    生產的回呼是 `_rollback_ts(why, ...)`。我第一版用 `rollback()` 呼叫,
    TypeError 又被 `except Exception` 吞掉 —— 時間戳沒回滾,於是
    「最後一刻使用者回來」或「立即重開下達失敗」會把自動修復壓住 24 小時。
    而我的測試用 `lambda: None` 當 stub,剛好把這個錯蓋過去。
    """
    global _bde_shutdown_pending
    with _bde_watch_lock:
        _bde_shutdown_pending = False
    try:
        if callable(rollback):
            rollback(why)
    except Exception:
        logging.warning("[BDE] 回滾時間戳失敗 → 自動修復可能被壓住 24 小時",
                        exc_info=True)


def _abort_reboot_if_needed(outcome: str, *, rollback,
                            run=None) -> bool:
    """依倒數結束的原因決定要不要 `shutdown /a`。→ 有沒有真的取消掉。

    ★兩個方向都要守★
      * 該取消卻沒取消 = 在有人正在用的時候把診間電腦重開(P1-06 的原始災情)。
      * 不該取消卻取消 = 自動修復【永遠不會發生】,BDE 壞了就一直壞著 ——
        把一個 fail-open 修成同樣有害的 fail-closed。
    所以 `outcome == "elapsed"`(沒有人回來、HIS 也沒好)必須原封不動讓它重開。

    `run` 只給測試注入(預設真的執行 shutdown /a)。
    """
    global _bde_shutdown_pending
    reason = _COUNTDOWN_ABORT_REASONS.get(outcome)
    if reason is None:
        return False        # 倒數走完 → 機器要重開了,那正是我們要的修復動作
    runner = run or (lambda cmd: subprocess.run(
        cmd, capture_output=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    logging.error("[BDE] %s → shutdown /a 取消重開機", reason)
    try:
        cpa = runner(["shutdown", "/a"])
        if cpa.returncode != 0:
            # ★critical:這是「該取消卻取消不掉」——機器【仍然會重開】,而且
            #   可能是在有人正在用的時候。log 要留得下最強的痕跡。
            logging.critical(
                "[BDE] shutdown /a 失敗(rc=%s) → ★機器仍會重開★(%s)",
                cpa.returncode, reason)
            return False
    except Exception:
        logging.critical("[BDE] shutdown /a 例外 → ★機器仍會重開★(%s)",
                         reason, exc_info=True)
        return False
    with _bde_watch_lock:
        _bde_shutdown_pending = False
    rollback(f"{reason},沒有真的重開")
    return True


def _await_reboot_countdown(total_sec: float) -> str:
    """重開機倒數期間的等待。→ 為什麼結束:

        "cancelled"     取消令被設起來(HIS 恢復/程式退出)
        "user_back"     ★量到使用者回來操作★
        "idle_unknown"  查不出閒置時間 → 保守當作可能有人在
        "elapsed"       倒數走完,機器要重開了

    ★[2026-08-04 外審 P1-06]★ 原本這裡只有 `_bde_reboot_cancel.wait(55)`,而那個
    事件只由「HIS 恢復」或「程式退出」設置 —— 倒數期間完全不再看使用者。醫師在
    這 60 秒內回座打字,機器照樣重開。這是看診時間的實體破壞,比查不到會診嚴重。

    ★[2026-08-04 外審第 2 輪 P1-01] 判準從【相對】改成【絕對】★
    第一版是「閒置秒數比執行中的峰值倒退超過容差就是有人回來」。那是相對判準,
    需要一個乾淨的 baseline —— 而 baseline 是進入本函式【之後】才取的第一個樣本。
    使用者若在「決定重開」與「第一個樣本」之間回來,第一個樣本就已經是 0～1 秒;
    之後閒置從這個低點單調上升,永遠看不到「倒退」,機器照樣重開。★初始取樣競態★

    改用絕對判準:整個自動重開機的前提就是「已經閒置滿 30 分鐘」,那個前提在倒數
    期間必須【持續】成立。任何一次量到閒置低於門檻,就代表有人動過 —— 不需要
    baseline、不需要容差,也就沒有初始取樣競態。
    """
    threshold = float(_keepalive.BDE_REBOOT_MIN_IDLE_SECONDS)
    deadline = time.monotonic() + total_sec
    while True:
        idle = _user_idle_seconds_or_none()
        if idle is None:
            return "idle_unknown"
        if idle < threshold:
            logging.error("[BDE] 倒數期間使用者已回來操作(閒置僅 %.0f 秒,"
                          "門檻 %.0f 秒)", idle, threshold)
            return "user_back"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "elapsed"
        if _bde_reboot_cancel.wait(timeout=min(1.0, remaining)):
            return "cancelled"


def _bde_reboot_state_path() -> str:
    return os.path.join(get_settings_dir(), "consult_bde_reboot_state.json")


def _load_last_auto_reboot_ts():
    try:
        data = safe_load_json(_bde_reboot_state_path(), default={}) or {}
        ts = data.get("last_auto_reboot_ts")
        return float(ts) if ts is not None else None
    except Exception:
        return None


def _save_last_auto_reboot_ts(ts: float) -> bool:
    """→ 是否真的落地。[codex P1] 呼叫端以此決定可不可以重開機。"""
    try:
        atomic_write_json(_bde_reboot_state_path(),
                          {"last_auto_reboot_ts": float(ts)})
        return True
    except Exception:
        logging.warning("[BDE] 重開機狀態寫入失敗", exc_info=True)
        return False


def _abort_bde_shutdown_on_exit() -> None:
    """[codex P1 R19] 使用者退出程式時,若 60 秒重開倒數還在跑 → shutdown /a 取消。

    退出只是關這支程式;已下達的 OS 重開機不會跟著消失,使用者按下退出後機器
    照樣重開會非常錯愕。時間戳【不回滾】(保守方向:寧可多等 24 小時,不迴圈)。

    ★[2026-08-08 外審 P2-01]★ 這裡整段(含 `shutdown /a`)都在 `_bde_watch_lock`
    裡,而且旗標【先於】任何動作設起來 —— 見 `_finish_bde_reboot` 的說明:
    收尾那側在「取消排定的重開」與「下立即重開」之間有一段沒有排定中的重開,
    退出若在那時插進來,會下一個取消不到任何東西的 `/a`,然後機器照樣重開。
    """
    global _bde_shutdown_pending
    _bde_reboot_cancel.set()        # ★先設★ 收尾那側的第二次檢查才看得到
    acquired = _bde_watch_lock.acquire(timeout=20)
    if not acquired:
        # 收尾那側正持鎖(它只在「下立即重開」那一瞬間持鎖)→ 機器已經在重開了,
        # `/a` 對 `/t 0` 也無效。照舊往下跑,至少留下 log。
        logging.warning("[BDE] 退出時取不到重開機鎖 → 收尾流程可能正在下重開令")
    try:
        if not _bde_shutdown_pending:
            return
        logging.warning("[BDE] 使用者退出時仍有排定中的重開機 → shutdown /a 取消")
        try:
            cpa = subprocess.run(
                ["shutdown", "/a"], capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if cpa.returncode == 0:
                _bde_shutdown_pending = False
            else:
                logging.error("[BDE] shutdown /a 失敗(rc=%s) — 機器仍將重開",
                              cpa.returncode)
        except Exception:
            logging.error("[BDE] shutdown /a 例外 — 機器仍將重開", exc_info=True)
    finally:
        if acquired:
            _bde_watch_lock.release()


def _schedule_bde_reboot_watch() -> None:
    """BDE 起不來 → 開「閒置重開機」看守。HIS 恢復會解除站崗。"""
    _schedule_reboot_watch("BDE", "住院醫囑系統的 BDE 初始化失敗($250E)")


# ★[外審 SH 第 1 輪 P1]★ 站崗的「原因」是一個【集合】,不是一個槽位。
#
#   我第一版用單一變數 `_reboot_watch_reason`,而 BDE 與資源耗盡是兩個
#   各自獨立、可以同時存在的事故。後果:
#     * 後到的事故把前一個的原因【覆寫】掉,但看守只有一條 →
#       之後 BDE 恢復會把一個【還沒好】的 RESOURCE 看守一起解除;
#     * 反過來,RESOURCE 再驗成功會終止一個【還在壞】的 BDE 看守;
#     * 而 RESOURCE 一旦錯過排程時機就再也不會重排 → 永久失去看守。
#   ★恢復訊號只能結掉【自己那一個】原因;還有任何原因沒好就繼續站崗。★
_reboot_reasons: set = set()          # 由 `_bde_watch_lock` 保護


def _clear_reboot_reason(reason: str) -> None:
    """某一個原因恢復了 → 只移除它；★全部都好了才解除站崗★。

    ★[外審 SH 第 2 輪 P1] 移除原因與下取消令必須在【同一個臨界區】★
    我上一版是「鎖內移除 → 放鎖 → 下取消令」。那中間新事故若剛好完成
    「登記原因 + 清令 + 世代 +1」，我們接著把令又 set 回去 ——
    看守醒來看到令還在就退場，接力的那條也立刻看到同一個令而退場，
    最後 `_reboot_reasons` 還有東西、`_bde_watch_active` 卻是 False：
    ★事故完全失去看守★，而休息時段根本不會有下一輪觀測把它救回來。
    """
    with _bde_watch_lock:
        if reason not in _reboot_reasons:
            return                      # 本來就沒為它站崗
        _reboot_reasons.discard(reason)
        remaining = sorted(_reboot_reasons)
        if not remaining:
            _bde_reboot_cancel.set()    # ★在同一個臨界區內★
    # log 不影響狀態,放到鎖外(它可能很慢)。
    if remaining:
        logging.info("[重開機看守] %s 已恢復,但仍有未恢復的原因 %s → 繼續站崗",
                     reason, remaining)
    else:
        logging.info("[重開機看守] %s 已恢復,沒有其他未恢復的原因 → 解除站崗",
                     reason)


def _reboot_all_conditions_cleared() -> bool:
    """★動手前再驗一次：還掛著的原因，現在是不是都已經恢復了★

    看守可能在半夜開火，而最後一次觀測可能是幾小時前（休息時段 00-06 根本
    不輪詢，不會有新的觀測進來）。「相信一個舊結論」與「在動手的那一刻確認」
    的差別，是一台好好的診間電腦會不會被白白重開。

    能重驗的就重驗（RESOURCE）；不能重驗的（BDE 的恢復是事件式的，由取消令
    表達）一律當成「還在」—— 保守方向是重開，因為那條路已經被
    「閒置 30 分鐘」與「24 小時一次」保護著。
    """
    with _bde_watch_lock:
        reasons = sorted(_reboot_reasons)
    if not reasons:
        return True
    for r in reasons:
        if r != "RESOURCE":
            return False                # 無法重驗 → 視為仍在
        if _hidden_desktop_exhausted():
            return False
        # 建得起來了 → `_hidden_desktop_exhausted` 已經把它當成一次恢復
        #   (重置 streak + 結掉這個原因)。
    with _bde_watch_lock:
        return not _reboot_reasons


def _schedule_reboot_watch(reason: str, detail: str) -> None:
    """開「閒置重開機」看守(singleton;已在站崗就只是登記原因,不重複開緒)。

    使用者定案:重開機通常可修 BDE($250E)與 USER object 耗盡,但
    【使用者連續 30 分鐘沒有輸入】才能重開;且 24 小時內只自動重開一次
    (重開後仍壞＝重開機修不了,絕不能進入重開機迴圈)。

    ★每個原因的「什麼算恢復」不一樣★
      * BDE      —— HIS 查詢成功就是恢復(`_note_job_success`);
      * RESOURCE —— ★不可以用「查詢成功」當恢復★:SW_HIDE 後備模式下查詢
        照樣會成功,而 USER object 還是耗盡的、每輪還是在送帳密。
        它的恢復訊號是「隱藏桌面又建得起來」(`_note_hidden_desktop_ok`)。
      這正是「便利的判斷式不等於那個狀態」。
    """
    global _bde_watch_active, _bde_watch_gen
    with _bde_watch_lock:
        # ★[外審 SH 第 2 輪 P1] 登記原因 + 清令 + 世代 + active 判定要在
        #   【同一個臨界區】★ 分成兩段的話,恢復訊號會擠在中間,
        #   把新事故剛清掉的令又 set 回去 —— 事故從此沒有人看著。
        # 冪等:重複呼叫(例如 streak 4、5、6…)只是續命,不重複開緒也不刷 log。
        known = str(reason) in _reboot_reasons
        _reboot_reasons.add(str(reason))
        # [codex P2 R3] 新事故先把取消令作廢——舊看守若還沒消化掉上一次的
        # cancel,它會在鎖內發現令已被清掉而【繼續站崗】(見 watch_loop)。
        # [codex P2 R4] 世代 +1:舊看守若正在退場(give_up/shutdown 被拒),
        # 它會在 finally 的鎖內發現世代前進了 → 接力開新看守,同樣無空窗。
        _bde_reboot_cancel.clear()
        _bde_watch_gen += 1
        gen = _bde_watch_gen
        spawn = not _bde_watch_active
        _bde_watch_active = True
    if spawn or not known:
        logging.error(
            "[重開機看守] 已排定:%s → 使用者連續閒置滿 %d 分鐘且 24 小時內未"
            "自動重開過就 shutdown /r;狀況恢復則自動解除",
            detail, _keepalive.BDE_REBOOT_MIN_IDLE_SECONDS // 60)
    if spawn:
        threading.Thread(target=_bde_reboot_watch_loop, args=(gen,),
                         name="BDERebootWatch", daemon=True).start()


def _bde_reboot_watch_loop(my_gen: int) -> None:
    global _bde_watch_active
    try:
        while running.is_set():
            if _bde_reboot_cancel.wait(timeout=60.0):
                # [codex P2 R3] 解除與否必須在鎖內判定(與 schedule 原子):
                # 令還在=真解除(HIS 恢復),在鎖內宣告退場;令被清掉=新事故
                # 已到,本看守直接接手繼續站崗。
                with _bde_watch_lock:
                    if _bde_reboot_cancel.is_set():
                        logging.info("[BDE] HIS 已恢復 → 重開機看守解除")
                        return    # active 由 finally 的世代判定統一收尾
                logging.info("[BDE] 舊取消令已被新事故作廢 → 看守繼續站崗")
                continue
            action, why = _keepalive.bde_reboot_decision(
                _user_idle_seconds(), _load_last_auto_reboot_ts(), time.time())
            if action == "wait":
                continue
            if action == "give_up":
                logging.error("[BDE] 不再自動重開機:%s", why)
                return
            # ★[批次SH] 動手前再驗一次:當初那個狀況現在還在嗎★
            #   看守可能在半夜開火,而最後一次觀測可能是幾小時前 ——
            #   休息時段(00-06)根本不輪詢,不會有任何新的觀測進來。
            #   「相信一個舊結論」與「在動手的那一刻確認」的差別,
            #   是一台好好的診間電腦會不會被白白重開。
            if _reboot_all_conditions_cleared():
                logging.info("[重開機看守] 動手前再驗:掛著的原因都已經恢復"
                             " → 取消自動重開機")
                return
            # ★先落地狀態、再下 shutdown★ 順序反過來的話,重開後狀態沒寫到,
            # BDE 若沒修好會再重開 → 無限重開機迴圈。
            # [codex P1] 落地【失敗】也一樣:狀態不在磁碟上,24 小時防護等於不存在
            # → 直接取消自動重開機,改請人工(寧可不修,絕不迴圈)。
            prev_ts = _load_last_auto_reboot_ts()
            if not _save_last_auto_reboot_ts(time.time()):
                logging.error("[BDE] 重開機狀態寫不進磁碟 → 【取消自動重開機】"
                              "(重開後無從辨識已重開過,可能無限迴圈);請人工重開機")
                return
            logging.error("[BDE] %s → 60 秒後自動重開機修復(shutdown /r)", why)
            # [codex P1 R2] shutdown 可能被拒(權限/原則,rc≠0 但不拋例外)——
            # 那時沒有任何重開會發生,卻已寫入時間戳=白白吃掉 24 小時防護
            # → 驗 returncode,失敗就【回滾時間戳】並改請人工。
            rc = -1
            try:
                cp = subprocess.run(
                    ["shutdown", "/r", "/t", str(int(_REBOOT_COUNTDOWN_SEC)),
                     "/c",
                     "皮膚科會診查詢:偵測到重開機可修復的狀況("
                     + ",".join(sorted(_reboot_reasons))
                     + "),閒置自動重開機"],
                    capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                rc = cp.returncode
            except Exception:
                logging.error("[BDE] shutdown 指令失敗", exc_info=True)
            if rc == 0:
                global _bde_shutdown_pending
                with _bde_watch_lock:
                    _bde_shutdown_pending = True   # [R19] 退出時據此 shutdown /a
            # ruff B023:預設參數綁定當下的 prev_ts(本分支所有路徑都在同一輪
            # 迭代內用完即 return,語意不變,只是把繫結寫明)。
            def _rollback_ts(why: str, prev_ts=prev_ts) -> None:
                try:
                    atomic_write_json(
                        _bde_reboot_state_path(),
                        ({"last_auto_reboot_ts": prev_ts}
                         if prev_ts is not None else {}))
                    logging.info("[BDE] 已回滾重開機時間戳(%s)", why)
                except Exception:
                    # 回滾也失敗 → 保守方向:多等 24 小時(不迴圈),不再吵
                    logging.warning("[BDE] 時間戳回滾失敗(保守維持 24 小時防護)",
                                    exc_info=True)

            if rc != 0:
                logging.error("[BDE] shutdown 被拒(rc=%s) → 回滾重開機時間戳,"
                              "請人工重開機", rc)
                _rollback_ts("shutdown 被拒,沒有真的重開")
                return
            # [codex P1 R15] 60 秒倒數期間 HIS 若恢復(瞬時故障/有人手動修好),
            # 要 shutdown /a 取消重開——不然 log 說「看守解除」機器卻照樣重開。
            # 取消成功也要回滾時間戳(沒真的重開,別吃掉 24 小時防護)。
            #
            # ★[2026-08-04 外審 P1-06] 使用者回來也要取消★
            #   原本只等取消令,倒數期間完全不再看使用者 —— 醫師回座打字,機器
            #   照重開。整個自動重開機的前提就是「沒有人在用這台電腦」,那個前提
            #   在這 60 秒內隨時可能不成立,所以要持續驗證。
            #
            #   ★[2026-08-08 外審第 2 回] 盲區不能只是「搬家」★
            #   我第一版把 OS 倒數拉長到 65 秒、監測 60 秒 —— 第 60~65 秒
            #   仍然沒有人在看,使用者在那時回來一樣會被重開。窗口只是換了位置。
            #   真正消滅它的做法:監測期滿之後【先取消排定的重開】,
            #   再取最後一次 idle 樣本,確認仍然沒人才下【立即】重開。
            #   這樣「最後一次觀測」與「重開」之間不再有任何無人看守的時間。
            #   排定的 65 秒仍然有意義:它是我們自己掛掉時的保險。
            _finish_bde_reboot(_await_reboot_countdown(_REBOOT_WATCH_SEC),
                               rollback=_rollback_ts)
            return
    finally:
        # [codex P2 R4] 退場與排程必須原子:本看守決定退場(give_up/shutdown 被拒/
        # 令仍在的真解除)到這裡之間,若有【新事故】進來,schedule 只看到 active=True
        # 而沒開新緒——世代前進就是證據 → 接力開新看守,絕不留無人看守的事故。
        respawn_gen = None
        with _bde_watch_lock:
            if _bde_watch_gen != my_gen and running.is_set():
                respawn_gen = _bde_watch_gen
            else:
                _bde_watch_active = False
        if respawn_gen is not None:
            logging.info("[BDE] 退場期間有新事故 → 接力開新看守")
            threading.Thread(target=_bde_reboot_watch_loop,
                             args=(respawn_gen,),
                             name="BDERebootWatch", daemon=True).start()

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
        # ★留住 Popen★（2026-08-04 外審 P1-01）：原本啟動完就把物件丟掉，
        #   於是這條路【沒有任何直接的所有權事實】，只能靠全機 PID 差集反推
        #   —— 而差集會把醫師在這 120 秒內新開的住院系統算成自己的。
        #   這與 2026-07-27 事故的教訓同一條：spawn 子行程要留 handle。
        _spawned = subprocess.Popen([SYSTEMFTP_PATH], startupinfo=startup)
    except FileNotFoundError as e:
        raise RuntimeError(f"找不到住院醫囑系統程式：{SYSTEMFTP_PATH}") from e
    spawned_root_pid = _spawned.pid
    logging.info("已啟動 systemftp.exe（SW_HIDE 後備模式，pid=%s）",
                 spawned_root_pid)

    stealth_stop = threading.Event()
    stealth_skip: set = set()

    def _stealth() -> None:
        while not stealth_stop.is_set():
            try:
                # ★[2026-08-08 外審第 9 輪 P1-02] 只藏【我們自己那個行程】的視窗★
                #   舊寫法用全機差集,於是醫師在這 120 秒內自己開的住院系統
                #   會被我們每 80 毫秒藏一次,他根本沒辦法用。
                for h in find_windows(pids={spawned_root_pid},
                                      visible_only=True):
                    if h in stealth_skip:
                        continue
                    # ★[外審 SG 第 2 輪 P2] 擋路的對話框【不藏】★
                    #   藏掉的話,登入迴圈的 `_blocking_dialogs`(只認可見視窗)
                    #   永遠看不到它 —— 於是認證錯誤訊息既不會被記下來、也不會
                    #   被按掉,登入就這樣空等滿 120 秒,而診斷還會說「一個對話框
                    #   都沒攔到」。★隱形執行緒把偵測要找的東西藏掉了★。
                    #   代價是後備模式下那個對話框會短暫可見(它隨即會被按掉),
                    #   而這條路本來就已經警告「可能短暫看到視窗」。
                    #   ★這裡不新增任何點擊面★:判準與偵測共用同一個函式,
                    #   而隱形執行緒本來就只掃我們自己 spawn 的那個 pid。
                    if _is_blocking_dialog(h):
                        continue
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
            if _spawned.poll() is not None:
                # ★我們啟動的那個行程已經結束★ = 啟動器把工作交給既有 instance
                #   (實機契約),或它自己失敗了。兩種情況都代表【接下來畫面上
                #   任何登入視窗都不能證明是我們的】,而且 PID 可能被回收重用。
                raise RuntimeError(
                    f"SW_HIDE 後備模式:我們啟動的 systemftp(pid={spawned_root_pid})"
                    "已結束(啟動器把工作交給既有實例)→ 無法證明畫面上的登入視窗"
                    "是我們的,本輪放棄 —— 不對醫師自己的住院系統輸入帳密")
            cands = find_windows(LOGIN_CLASS, LOGIN_TITLE_PREFIX,
                                 visible_only=False)
            # ★[2026-08-05 外審第 5 輪 P1-08] 這條路【不可以】借用既有實例★
            #   舊寫法是 `pick = fresh or cands` —— 找不到新的就撿一個既有的。
            #   而這條是 SW_HIDE 後備模式:它跑在【使用者自己的桌面】上,
            #   `cands` 裡那個「既有的登入視窗」極可能就是醫師剛打開、正要自己
            #   登入的住院系統。接下來我們會對它做的事是:
            #       把自動化帳密打進去 → 把他的視窗移到螢幕外 → 開會診單
            #       → 擷取全院病人資料 → 最後試著還原
            #   「收尾不關掉它」只是把傷害縮小到「不關窗」,前面那一串照做不誤。
            #   ★寧可本輪查不到,也不要動醫師的住院系統★
            #   (隱藏桌面那條路徑不同:那裡 `find_windows` 只列舉我們自己的桌面,
            #    撿到的必然是我們前一次留下的孤兒,不可能是醫師的 —— 見該處說明。)
            # ★[2026-08-08 外審第 9 輪 P1-02] 「本次啟動之後才出現」證明不了所有權★
            #   上一版的 `fresh = pid not in before` 只說明那個行程比我們晚出現,
            #   醫師在這 120 秒內自己按下住院系統也完全符合。真正能證明的只有
            #   一件事:那個視窗屬於【我們用 Popen 開的那個 pid】。
            #   (上面的 poll() 已保證它還活著 → pid 不可能是回收重用來的。)
            ours = [h for h in cands if _window_pid(h) == spawned_root_pid]
            if ours:
                login = ours[0]
                break
            if cands:
                logging.warning(
                    "[SW_HIDE 後備] 畫面上有登入視窗,但它不屬於我們啟動的行程"
                    "(pid=%s,我們的是 %s)—— 那可能是使用者自己開的住院系統,不碰它",
                    sorted({_window_pid(h) for h in cands}), spawned_root_pid)
            time.sleep(0.5)
        if not login:
            raise RuntimeError(
                "等不到【屬於我們這個行程】的登入視窗(多開提示可能未正確關閉、"
                "網路過慢,或 systemftp 已達『最多兩個』上限);本輪放棄 —— "
                "不借用既有實例,以免對使用者自己開的住院系統輸入帳密")

        our_pid = _window_pid(login)
        # [review C2 fix → 2026-08-05 P1-08] 這裡已經不可能是借用的:上面只接受
        # 「本次啟動之後才出現」的登入視窗。保留這個變數是因為收尾邏輯仍以它
        # 決定要不要保留實例;現在它恆為 False,語意變成「我們永遠只關自己開的」。
        borrowed = our_pid in before
        if borrowed:
            # 不該發生(fresh 的定義已排除)。真的發生代表 pid 在等待期間被回收 →
            # 身分無法確定,一樣不碰。
            raise RuntimeError(
                f"登入視窗的 pid({our_pid})在本次啟動前就存在 → 身分無法確定,本輪放棄")
        # ★[2026-08-08 外審第 9 輪 P1-02] 不再用全機差集★
        #   這條路跑在【醫師自己的桌面】上,差集裡的外來 pid 會真的貢獻出視窗
        #   (隱藏桌面那條路不會,因為 `find_windows` 只列舉我們自己的桌面)。
        #   主畫面、會診視窗一路都只認這一個經過證明的 pid。
        our_pids = {our_pid}
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
        # ★焦點有沒有真的落在欄位上,是登入失敗時的另一半證據★(2026-08-10 實機)
        _note_login_focus("帳號", type_via_focus(edits[0][0], login, username))
        _note_login_focus("密碼", type_via_focus(edits[1][0], login, password))
        confirm = find_child(login, "TButton", "確認")
        if not confirm:
            raise RuntimeError("登入視窗找不到「確認」鈕")
        click_button(confirm)  # PostMessage BM_CLICK，非阻塞
        logging.info("已送出登入")

        # 等主視窗；期間若跳「訊息通知主畫面」就按確認。
        # visible_only=False:本路徑會把視窗 SW_HIDE(見 2026-05-15 註解),
        # 可見性在這裡不是有效訊號。
        main_hwnd = _wait_main_window_after_login(our_pids, visible_only=False)
        logging.info("已進入主畫面")

        # 送選單命令：我的會診清單（背景 PostMessage，不點滑鼠、解析度無關）
        cmd_id = resolve_menu_command_id(main_hwnd)
        if cmd_id is None:   # 同上：寧可失敗重試,也不對醫囑系統送不明命令
            raise RuntimeError(
                "無法確認「我的會診清單」選單命令(疑似住院醫囑系統改版),本次中止")
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
        logging.info("會診通知單視窗已開啟，準備擷取")

        # 截圖（PrintWindow，視窗在螢幕外也能擷取）
        # ★[2026-08-04 外審 P1-08] 不在這裡落地★ 見 `_materialize_shot`
        # ★[2026-08-05 外審第 4 輪 P1-09] 先等清單穩定再截圖★(見另一個呼叫點)
        #   固定的 `time.sleep(1.8)` 由穩定判定取代。
        # [新功能 2026-06-13] 先擷取原始畫面,再逐列點選擷取文字(fail-open)
        img, _snap = _capture_with_settled_roster(consult)
        extracted, extracted_html, roster_texts = _extract_consult_text(
            consult, cfg, roster_label, settled=_snap)
        return img, extracted, extracted_html, roster_texts

    finally:
        # 收尾：停掉隱形執行緒、關閉我們這份 systemftp、把前景還給使用者。
        # [review C2 fix] 借用使用者既有實例時，排除啟動前就存在的 pid 不關。
        stealth_stop.set()
        # ★[2026-08-08 外審第 9 輪 P1-02] 沒認出登入視窗就只關我們自己那個★
        #   舊的 fallback 是全機差集 —— 提早中止時(例如上面判定啟動器已把工作
        #   交出去)會把醫師剛開的住院系統一起關掉。
        cleanup_pids = _cleanup_pids_excluding_borrowed(
            our_pids or {spawned_root_pid}, before, borrowed,
            root_pid=spawned_root_pid)
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


@dataclass(frozen=True)
class _DeliveryArtifact:
    """一封【已經組好、不會再變】的信。

    ★[2026-08-05 外審第 5 輪 P1-04]★ 寄信重試必須是「重送同一封」,不是
    「重查一次再組一封新的」。舊寫法每個 attempt 都重跑整個流程,於是:
      * HIS 被重複操作(再開一次會診畫面、再點選一次每位病人)
      * 第二次查到的清單可能已經不同 → 兩次寄出去的內容不一樣
      * 每次都新的 Message-ID → SMTP 若是「已收下但回應逾時」,收件人會收到兩封
      * 每次都多落地一張病人截圖
    frozen=True 讓「組好之後不再變」這件事由型別系統保證,而不是靠紀律。
    """

    recipients: tuple
    subject: str
    text_body: str
    html_body: str
    attachment: Any
    message_id: str
    business_key: str = ""


def _new_message_id() -> str:
    """產生一個 Message-ID,整份 delivery 共用(重試也用同一個)。"""
    from email.utils import make_msgid  # noqa: PLC0415
    return make_msgid()


def _discard_undelivered_shot(delivery) -> None:
    """整輪放棄之後,把【沒有寄出去】的那張截圖刪掉。

    ★[2026-08-05 外審第 5 輪 P2-05 / 自查 P1-C]★
    `_materialize_shot` 的既有取捨是「已經寄給臨床收件人的圖留著當線索」——
    那個理由**只對寄成功的情況成立**。寄失敗的那張病人畫面既沒有臨床用途、
    也沒有人看過,卻會留在磁碟上等 TTL 到期。這是純粹多出來的 PHI 暴露面。
    """
    path = getattr(delivery, "attachment", None)
    if not isinstance(path, Path):
        return
    try:
        if path.exists():
            path.unlink()
            logging.info("[privacy] 本輪沒有寄出 → 已刪除未送達的截圖")
    except Exception:
        logging.warning("[privacy] 刪除未送達的截圖失敗:%s", path, exc_info=True)


def _materialize_shot(img):
    """把記憶體裡的截圖落地成檔案 → 路徑。已經是路徑就原樣回傳（相容）。

    ★[2026-08-04 外審 P1-08]★ 截圖以前是在 `_query_cycle` 裡【無條件】存檔，
    而且發生在解析 roster 之前 —— 跟有沒有新會診毫無關係。常駐模式 3 分鐘一輪
    ＝每小時 20 張「沒寄出去、也沒有臨床用途」的完整病人畫面躺在磁碟上。

    改成只有真的要寄信時才呼叫本函式。沒有新會診的輪次在更上面就 return 了，
    磁碟上不會多出任何東西。

    ★與外審建議的差異（刻意）★
    外審建議「寄完 finally 刪掉」。這裡改成落地到既有的 `consult_shots/` 並沿用
    既有的 TTL 保留期：那張圖【本來就已經寄給臨床收件人】了，留在本機不會擴大
    暴露面，而出事時「當時畫面長怎樣」是最有用的線索。真正該消滅的是「沒寄出去
    卻留著」的那 20 張/小時，那個已經消滅了。保留期本來就有上限，不會無限累積。
    """
    # 本檔慣例：PIL 一律區域 import（模組層不相依）
    from PIL import Image  # noqa: PLC0415
    if not isinstance(img, Image.Image):
        return img                     # 已經是路徑（或 None）→ 不動
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    _prune_old_shots()
    # [外審第 6 輪 P2-08] 秒級檔名會在同秒兩個 delivery 時互相覆蓋,
    # 之後 _discard_undelivered_shot 還可能刪到別人正在用的那張。微秒+pid。
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    shot_path = SHOTS_DIR / f"consult_{stamp}_{os.getpid()}.png"
    # ★[2026-08-08 外審第 10 輪 P1-04] 先寫暫存檔,成功才改名★
    #   `img.save()` 是【先建檔再逐段寫入】。磁碟滿/IO 錯誤/編碼失敗時,
    #   一個寫到一半的 PNG 已經留在磁碟上了 —— 而此刻 `_DeliveryArtifact`
    #   還沒建立,`delivery` 仍是 None,收尾的 `_discard_undelivered_shot`
    #   根本不知道有這個路徑可以刪。那是一張沒寄出去、沒人知道存在、
    #   還原得出病人清單的畫面。
    #   暫存檔用 `.part` 後綴:既有的 TTL 清掃只認 `consult_*.png`,
    #   萬一連刪除都失敗,它也不會被誤認成一張正常截圖。
    #   ★格式要明講★ PIL 是從【副檔名】推格式的,`.part` 它不認識,
     #   會直接 ValueError —— 那會讓每一張截圖都存不了,比原本的問題嚴重
     #   得多。(修正本身開了一個更大的洞,是這個專案反覆踩到的坑。)
    part_path = shot_path.with_suffix(".png.part")
    try:
        img.save(part_path, format="PNG")
        os.replace(part_path, shot_path)
    except BaseException:
        try:
            part_path.unlink(missing_ok=True)
        except Exception:
            logging.warning("[shot] 截圖寫入失敗,殘檔也刪不掉:%s", part_path,
                            exc_info=True)
        raise
    logging.info("已存檔截圖（本輪確定要寄信）：%s", shot_path)
    return shot_path


def _prune_old_shots() -> None:
    """清掉過期(TTL)與超量(容量後備)的會診截圖。

    TTL 才是保留期政策(截圖含整份病人清單);`MAX_SHOT_FILES` 只是避免爆量塞爆
    磁碟的後備,它會刪掉還在期限內的檔。原本這裡只有數量這一關 —— 只要沒破 60 張,
    截圖可以永久留著。詳見 `cmuh_common/retention.py` 與 autoclock 的同一段說明。
    """
    try:
        from cmuh_common.retention import consult_shot_rule, sweep
        sweep([consult_shot_rule(str(SHOTS_DIR))])
    except Exception:
        logging.debug("會診截圖 TTL 清理失敗(略過)", exc_info=True)
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
def _outlook_probe() -> bool:
    """實際的 Outlook 可用性探測（在 worker 緒執行，自己 CoInitialize）。"""
    import pythoncom          # noqa: PLC0415
    pythoncom.CoInitialize()
    try:
        import win32com.client        # noqa: PLC0415
        try:
            win32com.client.GetActiveObject("Outlook.Application")
            return True
        except Exception:
            pass
        try:
            win32com.client.DispatchEx("Outlook.Application")
            return True
        except Exception:
            return False
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _outlook_available(timeout: float = 5.0) -> bool:
    """快速檢查本機 Outlook 是否可用：能 GetActiveObject 或 DispatchEx 成功就回 True。
    用於「多台電腦只有一台登入 Outlook」情境——沒 Outlook 的機就靜默跳過排程，
    不再啟動 systemftp、不寄信、不跳任何提示。

    ★[2026-08-10 批次SF] 改走共用的 `call_with_timeout`★
    舊版自己開 thread + `join(timeout)`,逾時就回 False —— 那條 worker 沒有被
    取消,它【已經 CoInitialize 過】而且卡在 `DispatchEx` 裡。Outlook 忙線/跳
    安全提示/正在修復是持續性的,於是:

      * 每一輪 poll(2-3 分鐘)都再開一條 `OutlookAvailCheck` + 一個 COM
        apartment,一天累積數百條,永遠不會收斂;
      * ★而且它是 `send_via_outlook` 的【門】★ —— 批次SE 才剛給那邊加了
        single-flight,但 Outlook 一卡住,`_do_full_job` 在這裡就 return 了,
        根本走不到 SE 的守衛。把門留著漏,等於後面那道鎖沒有意義。

    `call_with_timeout` 已經備齊這一組:有界等待、檢查與佔位同一個臨界區、
    未 start 也算佔位、start 失敗釋放、同名上限(4 條)到頂就不再疊加。
    ★不必再手寫第三份 single-flight★(本檔已經有兩份;`capture_window_image`
    早就是這個寫法)。逾時/到頂一律回 `False` —— 探測不出來時 Outlook 對我們
    來說就是不可用,語意正確。
    """
    return bool(call_with_timeout(_outlook_probe, timeout, default=False,
                                  name="OutlookAvailCheck"))


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


# ★[2026-08-10 批次SE #9] 上一條被放生的 Outlook COM worker★
#
# `worker.join(timeout)` 到期只是放棄等待 —— COM apartment、MailItem 與
# 可能延遲送出的那封信都還活著。呼叫端正確地【不重試同一封】
# (DeliveryOutcomeUnknown),但**後續不同的會診事件仍可各開一條新 worker**。
# Outlook 忙線/跳安全提示是持續性的,於是 COM apartment 與 MailItem
# 逐條堆積,長期可能拖垮 Outlook,並產生很晚才送達的舊通知。
#
# 與隱藏桌面 worker、IMAP check 同一套立場:上一條還卡著就不再疊加。
# ★這條路徑只在 mail_method="outlook" 備援模式才會走到★
_last_outlook_worker = None
# ★[外審 SE 第1輪] 這個狀態轉移必須有自己的鎖★
#   `send_via_outlook` 有【兩條各自 gate 的呼叫路徑】:一般會診寄送
#   (`_flow_lock`)與托盤的測試信(`_test_email_gate`)—— 它們是不同的守衛,
#   可以真的併行。沒有鎖的 check-then-set 會讓兩邊都看到 None、各開一條
#   COM worker;而無條件的清除還會把【別人】還活著的引用清掉 →
#   single-flight 形同虛設。(同一形狀本輪已在 win32_safe 犯過一次。)
_outlook_worker_lock = threading.Lock()


def _outlook_worker_occupies_slot(t) -> bool:
    """這條 worker 還占不占位。

    ★不可以只看 is_alive()★ 佔位發生在 `start()` 之前(檢查與佔位要在
    同一個臨界區),而【還沒 start 的 thread】`is_alive()` 也是 False ——
    併發窗內別條呼叫會把它當成死的而一起佔位,single-flight 又被繞過。
    `ident is None` = 還沒 start = 仍占位;started 且不 alive 才是真結束。
    (win32_safe 的放生上限踩過同一個坑,同一個修法。)
    """
    if t is None:
        return False
    return t.ident is None or t.is_alive()


def _claim_outlook_worker(worker):
    """原子地佔位。→ 佔到了嗎(False = 上一條還占著)。"""
    global _last_outlook_worker
    with _outlook_worker_lock:
        if _outlook_worker_occupies_slot(_last_outlook_worker):
            return False
        _last_outlook_worker = worker
        return True


def _release_outlook_worker(worker):
    """釋放佔位 —— ★只有當它還是我那一條時★(別清掉別人的)。"""
    global _last_outlook_worker
    with _outlook_worker_lock:
        if _last_outlook_worker is worker:
            _last_outlook_worker = None


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
    # ★[批次SE #9] 上一條放生的 COM worker 仍卡著 → 不疊加★
    #   (Outlook 忙線/安全提示是持續性的;每個新事件再開一條 =
    #    COM apartment 與 MailItem 堆積、很晚才送達的舊通知。)
    #   ★檢查與佔位在同一個臨界區★(外審 SE 第1輪:兩條呼叫路徑各有各的
    #   gate,可以真的併行 —— 沒鎖的 check-then-set 會讓兩邊都開一條)。
    if not _claim_outlook_worker(worker):
        raise DeliveryOutcomeUnknown(
            "上一封 Outlook 寄信仍未結束(疑似 Outlook 忙線或跳安全提示)——"
            "本封不再疊加新的 COM worker;結果不明,不自動重試以免重複寄出")
    try:
        worker.start()
    except Exception:
        # ★start 失敗要釋放佔位★ 否則這條永遠不會 alive、也永遠不被清掉,
        #   Outlook 備援從此永久停擺(win32_safe 記過同一個坑)。
        _release_outlook_worker(worker)
        raise
    worker.join(timeout)
    if worker.is_alive():
        # ★引用留著★ 下一封會看到它還活著而跳過(見上面的 single-flight)。
        # ★[外審第 6 輪 P1-07] 逾時的 worker 沒有被終止,它可能【稍後仍寄成功】★
        #   把這當成可重試錯誤的話,下一個 attempt 會啟動第二個 worker →
        #   兩個都 Send 成功 → 收件人收到兩封。結果不明就不得自動重試。
        raise DeliveryOutcomeUnknown(
            f"Outlook 寄信逾時（超過 {int(timeout)} 秒）——原 worker 可能仍在寄,"
            "不自動重試以免重複寄出;請檢查 Outlook 是否跳出安全提示或忙線")
    # 正常結束 → 釋放佔位(只有還是我那一條時才清)
    _release_outlook_worker(worker)
    if result.get("error"):
        raise result["error"]
    if not result.get("ok"):
        raise RuntimeError("Outlook 寄信未完成（原因不明）")
    sender_note = f"（寄件人 {sender_account}）" if sender_account else ""
    logging.info("已透過 Outlook 寄出給：%s%s", ", ".join(recipients), sender_note)


# ── 寄送帳本接線（2026-08-07 外審 AT/AW）────────────────────────────────────
# 帳本是【觀測】用的,不可以因為它壞掉就寄不出信 —— 所有接點都 fail-open。
_ledger_lock = threading.Lock()
_ledger_obj = None


def _get_ledger():
    """取共用帳本（第一次使用才建；失敗回 None，呼叫端自然略過）。"""
    global _ledger_obj
    with _ledger_lock:
        if _ledger_obj is None:
            try:
                from cmuh_common.delivery_ledger import DeliveryLedger
                _ledger_obj = DeliveryLedger()
            except Exception:
                logging.warning("[delivery] 帳本初始化失敗(寄信不受影響)",
                                exc_info=True)
                return None
        return _ledger_obj


def _flush_delivery_ledger() -> None:
    """結束前把還沒落地的帳本變更補寫一次。

    ★[2026-08-08 外審第 10 輪第 2 回 P2-3] atexit 在這裡不夠★
    會診程式有兩條 `os._exit()` 出口(self-watchdog 強制重啟、托盤結束),
    而 `os._exit` 不跑 atexit —— 帳本的「程式結束時會再試一次」在這支程式上
    本來是一句空話。兩條出口各自明呼叫這裡。
    """
    try:
        led = _ledger_obj
    except Exception:
        return
    if led is None:
        return
    try:
        led.flush()
    except Exception:
        logging.debug("[delivery] 結束前補寫帳本失敗", exc_info=True)


def _consult_business_key(roster_texts, recipients, subject="") -> str:
    """這一次寄送對應的【事件識別】。同一件事要得到同一把鑰匙。

    ★[2026-08-08 外審第 10 輪 P2-07]★ 舊的 key 是 `consult:{主旨}`。
    主旨裡有會變的東西(時間/筆數),同一批會診在另一分鐘重跑就成了另一件事;
    反過來,兩天的主旨若剛好一樣又會撞在一起。改用【清單內容】當識別。

    ★寄給誰也是識別的一部分★ 同一份清單寄給團隊、寄給 email 觸發的醫師本人,
    是兩件不同的寄送(所以更新已通知基準的規則也不同)。

    ★★不可以把病歷號寫進磁碟★★
    `_consult_signature_from_roster` 回的是 `病歷號|日期|時間` 的集合 ——
    那是病人識別資料,而帳本是會落地的 JSON 檔。所以這裡取的是它的雜湊:
    「同一批會診會得到同一把鑰匙」這個性質完全保留,而檔案裡沒有任何一個
    病歷號。收件人同理(那是內部信箱,但沒有理由多存一份)。

    ★退化路徑也必須能分辨不同的事件★(外審第 10 輪第 2 回 P2-6)
    上一版的退化 key 只有收件人雜湊 —— 於是【每一次】解析失敗的寄送,
    不分日期、不分主旨,通通得到同一把鑰匙。稽核紀錄把不同事件混成一筆;
    將來接成閘門的話,第一次寄送會把之後每一次「解析失敗」都擋掉。
    (而且上一版的 docstring 寫著「退回主旨」,函式卻根本沒有 subject 參數 ——
     宣稱與實作不符,又一次。)
    現在退化 key = 主旨 + 日期的雜湊:同一個工作重試會得到同一把鑰匙,
    不同日/不同主旨則分得開。主旨一樣雜湊(它不該含病人資料,但沒有理由
    在會落地的檔案裡多留一份可讀文字)。
    """
    import hashlib  # noqa: PLC0415
    audience = hashlib.sha256(json.dumps(
        sorted(str(r) for r in (recipients or [])),
        ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    sig = None
    try:
        sig = _consult_signature_from_roster(roster_texts)
    except Exception:
        logging.debug("[delivery] 取會診簽章失敗 → business_key 退回主旨",
                      exc_info=True)
    if sig:
        digest = hashlib.sha256(json.dumps(
            sorted(str(x) for x in sig),
            ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        return f"consult:{digest}|{audience}"
    stamp = hashlib.sha256(json.dumps(
        [str(subject), datetime.now().strftime("%Y-%m-%d")],
        ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return f"consult-unsigned:{stamp}|{audience}"


_REFUSAL_RESEND_ATTEMPTS = 2      # 同一輪之內,最多再補寄兩次
# ★跨輪的退避排程★(外審第 10 輪第 3 回 P1-1)
#   信箱滿、greylisting 這類 4xx 不會在毫秒之間好起來,所以「同一輪連按兩次」
#   幾乎必然全部失敗 —— 而失敗之後主流程照樣把已通知基準往前推,那位收件人
#   就永久收不到這則臨床通知了。改成排進退避佇列,由後續輪次接手。
_REFUSAL_RETRY_BACKOFF_SEC = (120.0, 600.0, 1800.0)   # 2 分 / 10 分 / 30 分
_pending_refusal_retries: list = []
_pending_refusal_lock = threading.Lock()


def _schedule_refusal_retry(delivery, refused: dict, trigger_label: str,
                            origin_did: str = "") -> None:
    """把仍未送達的暫時性拒收排進退避佇列(由後續輪次的 `_drain_...` 處理)。

    ★為什麼是記憶體佇列,不是持久化★(刻意,不是疏漏)
    要跨重啟續傳就得把【信件內文】寫到磁碟上,而會診信的內文就是病人清單。
    本專案的紅線是「不得存放病人明文」,所以這裡停在記憶體。程式重啟會忘掉
    這些待補寄 —— 那不是無聲的:給不出去的時候會用 error 級別把「誰沒收到」
    講清楚(見 `_give_up_on_refusals`),而不是假裝寄成功了。
    """
    with _pending_refusal_lock:
        _pending_refusal_retries.append({
            "delivery": delivery, "refused": dict(refused),
            "trigger": trigger_label, "attempt": 0, "origin": origin_did,
            "due_at": time.time() + _REFUSAL_RETRY_BACKOFF_SEC[0],
        })
    logging.warning("[delivery] %d 位收件人仍暫時被拒 → 排入退避重試(%.0f 分後)",
                    len(refused), _REFUSAL_RETRY_BACKOFF_SEC[0] / 60.0)


def _give_up_on_refusals(item) -> None:
    """退避重試用完了。不可以無聲吞掉 —— 講清楚誰沒收到,並寄開發者告警。"""
    who = sorted(str(a) for a in (item["refused"] or {}))
    logging.error(
        "[delivery] ★這幾位收件人始終沒收到會診通知★:%s(主旨:%s)。"
        "已用完 %d 次退避重試;請確認對方信箱狀態(信箱滿/被暫時封鎖)",
        ", ".join(who), item["delivery"].subject,
        len(_REFUSAL_RETRY_BACKOFF_SEC))
    _alert_missed_recipients(who, item["delivery"].subject, "退避重試用盡")


_MISSED_ALERT_INTERVAL_SEC = 3600.0
_missed_alert_at = 0.0


def _alert_missed_recipients(who: list, subject: str, why: str) -> None:
    """有人始終沒收到臨床通知 → 走【開發者告警】通道,不能只寫 log。

    ★[2026-08-08 外審第 10 輪第 4 回 P1-1]★ 使用者不會去翻 log;
    「沒收到」這件事不主動說,就永遠沒有人會知道。
    """
    global _missed_alert_at
    now = time.time()
    if now - _missed_alert_at < _MISSED_ALERT_INTERVAL_SEC:
        return
    _missed_alert_at = now
    body = (f"以下收件人沒有收到會診通知:\n  {', '.join(who)}"
            f"\n\n信件主旨:{subject}\n原因:{why}\n\n"
            "SMTP 端是暫時性拒收(4xx,例如信箱容量已滿、被暫時封鎖),"
            "程式已經重試過並放棄。請確認對方信箱狀態,必要時人工轉寄。")

    def _worker():
        global _missed_alert_at
        try:
            from cmuh_common.smtp_mail import send_mail  # noqa: PLC0415
            send_mail(recipients=[str(r) for r in _developer_alert_recipients()],
                      subject="會診自動化:有收件人沒收到會診通知",
                      body=body, attachment_path=None, category="system")
        except Exception:
            _missed_alert_at = time.time() - _MISSED_ALERT_INTERVAL_SEC + 600
            logging.warning("[delivery] 漏收告警寄送失敗(10 分鐘後重試)",
                            exc_info=True)
    try:
        threading.Thread(target=_worker, name="ConsultMissedAlert",
                         daemon=True).start()
    except Exception:
        logging.debug("[delivery] 漏收告警執行緒啟動失敗", exc_info=True)


def _confirm_on_origin(origin_did: str, delivered: list) -> None:
    """補寄送達了 → 把初次那一筆對應的收件人結成已送達。"""
    if not origin_did or not delivered:
        return
    led = _get_ledger()
    if led is None:
        return
    try:
        done = led.confirm_recipients(origin_did, delivered)
        if done:
            logging.info("[delivery] 補寄成功,已回寫初次紀錄:%s",
                         ", ".join(done))
    except Exception:
        logging.warning("[delivery] 回寫初次紀錄失敗(這幾位可能被誤報漏收):%s",
                        ", ".join(sorted(delivered)), exc_info=True)


# ── UNKNOWN 回查(outbox reconciliation)────────────────────────────────────
# ★實作在 `cmuh_common/delivery_reconcile.py`★(外審 2026-08-09 P1-04)
#   寫進這本帳的有【兩支程式】:會診通知(這裡)與主程式的止掛提醒。
#   回查原本只寫在本檔 —— 只跑主程式、沒裝會診查詢的診間電腦,止掛信的
#   UNKNOWN 與卡住的 SUBMITTING 就【永遠沒有人收斂】。實作搬到共用模組,
#   兩邊都驅動它;節流改成跨 process(不然同一台機器兩支會互相覆蓋收斂結果)。
from cmuh_common.delivery_reconcile import Reconciler as _Reconciler  # noqa: E402

# ★用 lambda 做【延遲查找】,不要把函式物件綁死★
#   直接傳 `_get_ledger` 會把 import 當下的那個物件釘住,之後任何對
#   `consult_query._get_ledger` 的取代(測試的 seam、未來的注入)都不再生效 ——
#   而且是【安靜地】不生效:測試照跑,只是測到的是舊的取得器。
_RECONCILER = _Reconciler(lambda: _get_ledger(), tag="delivery")


def _reconcile_unknown_deliveries(now=None, finder=None) -> int:
    """把帳本上的 UNKNOWN／卡住的 SUBMITTING 拿去寄件備份回查。→ 收斂幾筆。"""
    return _RECONCILER.run_once(now=now, finder=finder)


_ABANDON_RETRY_AFTER_SEC = 3600.0     # 帳上掛超過一小時 → 明確結案 + 告警


def _close_out_stale_recipient_retries(now=None) -> None:
    """★重啟之後的那一半★(外審第 10 輪第 4 回 P1-1)

    退避佇列在記憶體裡,程式一重啟就忘光 —— 那正是外審指出的缺口:
    連「講清楚誰沒收到」的告警都不會執行,因為佇列已經隨 process 消失了。
    但【帳本是落地的】:重啟之後 `needs_recipient_retry()` 仍然看得到
    「這幾位還沒收到」。所以這裡負責把那一半接回來:掛太久的,明確結案
    (帳本上改記成不再補寄)並告警。

    ★為什麼不是重新把信寄出去★ 要重建那封信就得把【病人清單】寫到磁碟上,
    而本專案不存病人明文。所以重啟後能做、也應該做的事是:讓人知道。
    「送不到」變成一則有人看得到的告警,而不是一件沒有人知道的事。
    """
    led = _get_ledger()
    if led is None:
        return
    now = now or time.time()
    try:
        pending = led.needs_recipient_retry()
    except Exception:
        logging.debug("[delivery] 讀取待補寄清單失敗", exc_info=True)
        return
    for did, _todo in pending:
        try:
            rec = led.get(did) or {}
            if now - float(rec.get("created_at") or 0) < _ABANDON_RETRY_AFTER_SEC:
                continue                       # 還在退避窗口內,交給佇列
            gone = led.abandon_recipient_retry(
                did, note="補寄未成功(可能跨越程式重啟)→ 已告警")
            if gone:
                logging.error("[delivery] ★帳上有始終沒收到的收件人★:%s(主旨:%s)",
                              ", ".join(gone), rec.get("subject", ""))
                _alert_missed_recipients(gone, str(rec.get("subject") or ""),
                                         "補寄未成功(可能跨越程式重啟)")
        except Exception:
            logging.debug("[delivery] 結案待補寄 %s 失敗", did, exc_info=True)


def _drain_pending_refusal_retries(now=None) -> None:
    """到期的補寄各做一次。每一輪查詢開始時呼叫。

    ★這是 `_resend_transient_refusals` 的跨輪版本★ 一樣只寄給被拒的那幾位、
    一樣每次自己一筆帳與自己的 Message-ID。
    """
    now = now or time.time()
    with _pending_refusal_lock:
        due = [i for i in _pending_refusal_retries if i["due_at"] <= now]
        for i in due:
            _pending_refusal_retries.remove(i)
    for item in due:
        left = _resend_transient_refusals(item["delivery"], item["refused"],
                                          item["trigger"],
                                          origin_did=item.get("origin", ""))
        if not left:
            logging.info("[delivery] 退避重試成功,補寄完成")
            continue
        nxt = item["attempt"] + 1
        if nxt >= len(_REFUSAL_RETRY_BACKOFF_SEC):
            item["refused"] = left
            _give_up_on_refusals(item)
            continue
        with _pending_refusal_lock:
            _pending_refusal_retries.append({
                **item, "refused": left, "attempt": nxt,
                "due_at": now + _REFUSAL_RETRY_BACKOFF_SEC[nxt],
            })


def _resend_transient_refusals(delivery, refused: dict,
                               trigger_label: str = "",
                               origin_did: str = "") -> dict:
    """把【暫時性】拒收的收件人補寄。回傳最後仍未送達的拒收 dict。

    ★為什麼不是往上拋讓外層重試★ 外層重試會把整封信重寄給【所有】收件人,
    已經收到的人就收到第二封。要補的是那幾個人,不是那封信。

    ★永久拒收(5xx:位址打錯、帳號不存在)不補寄★ —— 重寄一百次也不會變好,
    只是浪費配額;那要靠人去改收件人設定,所以留給既有的 error log 講清楚。

    ★每一次補寄是【自己一筆】,有自己的 Message-ID★
    (外審第 10 輪第 2 回 P2-7)上一版沿用初次的 Message-ID,理由是「對那些人
    來說這是同一封信」。但那些人【一封都沒收到】,所以沒有重複的問題;
    而沿用的代價是:將來拿這個 Message-ID 去 Gmail 寄件備份回查,找到的會是
    初次寄送(它成功送達了 A),於是把 B 誤判成也送到了。
    每一次補寄自己登記一筆、自己一個 Message-ID,回查才問得出正確答案。
    """
    from dataclasses import replace as _replace  # noqa: PLC0415
    # ★常數要從那個模組拿★ 這裡原本寫死字面值 "transient",而
    #   `classify_refusal` 回的是 `R_TRANSIENT`(= "transient_refused")——
    #   永遠不相等,於是「補寄」這段程式碼一次都不會執行。守衛靜默失效,
    #   而且測試若只驗「helper 存在」也照樣全綠。
    from cmuh_common.delivery_ledger import (  # noqa: PLC0415
        R_TRANSIENT, classify_refusal,
    )
    for _n in range(_REFUSAL_RESEND_ATTEMPTS):
        targets = [addr for addr, info in (refused or {}).items()
                   if classify_refusal(_refusal_code(info)) == R_TRANSIENT]
        if not targets:
            return refused
        logging.warning("[delivery] 暫時性拒收 %d 位 → 只對這幾位補寄一次:%s",
                        len(targets), ", ".join(sorted(targets)))
        attempt = _replace(
            delivery, recipients=tuple(sorted(targets)),
            message_id=_new_message_id(),
            business_key=f"{delivery.business_key}|retry{_n + 1}")
        _rid = _delivery_begin(attempt, trigger_label, parent_id=origin_did)
        try:
            again = send_via_smtp(
                attempt.attachment, attempt.subject, attempt.text_body,
                list(attempt.recipients), html_body=attempt.html_body,
                message_id=attempt.message_id) or {}
        except DeliveryOutcomeUnknown:
            _delivery_settle(_rid, unknown=True)
            # ★不可以往上拋★ 初次那一筆的已知結果已經落地了;往上拋會讓
            #   整個工作失敗、下一輪重跑整份清單,已收到的人再收一次。
            logging.error("[delivery] 補寄結果不明(%d 位)→ 留在拒收清單待查",
                          len(targets))
            return refused
        except Exception:
            _delivery_settle(_rid, failed=True)
            logging.warning("[delivery] 補寄暫時性拒收失敗,保留原拒收清單",
                            exc_info=True)
            return refused
        _delivery_settle(_rid, refused=again)
        # ★回寫初次那一筆★(外審第 10 輪第 5 回)
        #   補寄是自己一筆,但「這位到底收到了沒有」的答案要回到【初次】。
        #   不回寫的話,初次紀錄永遠掛著暫時被拒 → 一小時後
        #   `_close_out_stale_recipient_retries` 把它判成始終漏收、寄告警 →
        #   人工照著告警轉寄 = 醫師收到重複的臨床通知。
        #   ★修正本身開的洞比原本的問題更難發現,所以特別記在這裡。★
        _confirm_on_origin(origin_did, [a for a in targets if a not in again])
        # 這一輪送達的人要從拒收清單裡拿掉;仍被拒的換成新的原因。
        refused = {a: i for a, i in (refused or {}).items()
                   if a not in targets}
        refused.update(again)
    return refused


def _refusal_code(info):
    """smtplib 的拒收值是 (code, msg);容錯地取出 code。"""
    if isinstance(info, (tuple, list)) and info:
        return info[0]
    return info


def _delivery_begin(delivery, trigger_label: str, parent_id: str = "") -> str:
    """送出【之前】登記一筆。回 delivery_id（失敗回空字串）。

    ★[2026-08-08 外審第 10 輪 P2-08] `mark_submitting` 失敗仍要回傳 did★
    舊寫法把 begin 與 mark 包在同一個 try 裡,mark 失敗就回空字串 —— 那筆
    【已經落地的 PREPARED】從此沒有人會去 settle 它,而信照樣寄出去了。
    於是磁碟上留著一筆永遠停在 PREPARED、其實已送達的孤兒。
    現在 mark 失敗只記 log,did 照樣回傳,終局狀態仍會寫回那一筆。
    這也讓「陳舊的 PREPARED」有了單一明確的含意:**在送出之前就死了**,
    因此可以安全地收斂成 FAILED（見 `converge_stale_prepared`）。
    """
    led = _get_ledger()
    if led is None:
        return ""
    try:
        did = led.begin(
            business_key=delivery.business_key or f"consult:{delivery.subject}",
            category="consult",
            recipients=list(delivery.recipients),
            subject=delivery.subject,
            message_id=delivery.message_id or "",
            parent_id=parent_id,
            # ★只落地文字★(批次AD-3,使用者定案):Sent 查無後自動補寄
            #   要拿什麼重建 —— PHI 截圖依既有隱私定案不落地。
            body_text=delivery.text_body or "")
    except Exception:
        logging.debug("[delivery] 登記寄送失敗(略過)", exc_info=True)
        return ""
    try:
        led.mark_submitting(did)
    except Exception:
        # 已經有一筆落地的紀錄了 —— 一定要把 did 交出去,否則沒人會結案它。
        logging.warning("[delivery] 標記 SUBMITTING 失敗(仍會結案)", exc_info=True)
    return did


def _delivery_settle(delivery_id: str, *, refused=None,
                     unknown: bool = False, failed: bool = False) -> None:
    """把結果寫回帳本（含逐位收件人狀態）。"""
    if not delivery_id:
        return
    led = _get_ledger()
    if led is None:
        return
    try:
        state = led.settle(delivery_id, refused=refused or {},
                           unknown=unknown, failed=failed)
        if state != "confirmed":
            logging.warning("[delivery] 本次寄送狀態=%s(id=%s)", state, delivery_id)
    except Exception:
        logging.debug("[delivery] 寫回寄送結果失敗(略過)", exc_info=True)


def send_via_smtp(image_path: Path, subject: str, body: str,
                  recipients: list, timeout: float = 60.0,
                  html_body: str = "", message_id: str = "") -> dict:
    """用 SMTP 直接寄（Gmail / smtp.gmail.com）。

    為何不用 Outlook：admin 行程的 Outlook COM 會起一個 admin Outlook 實例，
    用 administrator 的 MAPI profile（通常沒設定任何郵件帳號），mail.Send()
    成功但信永遠卡在隱形 Outbox 寄不出。SMTP 跳過整個 UAC + Outlook profile
    地獄，任何權限都能寄。

    使用 settings/smtp_credentials.json 的 cmuhdermatology@gmail.com + App
    Password。檔案不存在會自動建立範本，password 為空會 raise
    SmtpNotConfiguredError。"""
    from cmuh_common.smtp_mail import send_mail
    # [外審第 6 輪 P2-02] 只留一層重試:外層 _do_full_job 已有 3 次 attempt,
    # 內層再各自重試會變成最多 9 次提交(重複寄出的機率跟著放大)。
    # ★[2026-08-07 外審 AW] 回傳值(被拒收件人)不可以再丟掉★
    #   smtplib 只有【全部】收件人被拒才拋例外;部分被拒是【正常返回】並把
    #   那些人放在回傳值裡。舊版整句丟掉 → 四個人裡有一位收不到,這一輪仍被
    #   判為完全成功、基準照樣更新 → 那位【永遠】不會補寄,而且無跡可循。
    #   現在把逐位結果寫進寄送帳本,由 needs_recipient_retry() 挑出該補的人。
    refused = send_mail(recipients=recipients, subject=subject, body=body,
                        attachment_path=image_path, timeout=timeout,
                        max_retries=0,
                        html_body=html_body or None,
                        # ★[2026-08-05 外審第 5 輪 P1-04]★ 重試要重送【同一封】——
                        #   換 Message-ID 會讓「已收下但回應逾時」變成收件人收到兩封。
                        message_id=message_id or None) or {}
    if refused:
        logging.error(
            "[consult] ★有收件人沒收到會診通知★:%s(其餘已送達)。"
            "已記入寄送帳本,暫時性拒收會在後續補寄;永久性(位址錯誤)請修設定。",
            ", ".join(sorted(str(r) for r in refused)))
    return refused


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
    # ★[2026-08-04 外審第 3 輪 P1-06] 全機 PID 差集不可以當成 kill 授權★
    #   這條路完全繞過這幾輪建立的 ownership 驗證:醫師若在本次任務【執行期間】
    #   手動開住院系統,他的行程不在 before 快照裡 → 落進差集 → 被 `taskkill /F`。
    #   而它會在任何可重試錯誤前執行,包括【寄信失敗】—— 也就是:
    #       會診查完 → 醫師手動開 HIS → SMTP timeout → 醫師的 HIS 被強殺
    #   函式自己的舊 docstring 也承認了這個窄窗。
    #
    #   ★改成:收掉【我們自己的 session】,差集只留作證據★
    #   session 收法已經是「對確切主畫面送 WM_CLOSE 並回讀確認」(見
    #   `_close_session_windows`),那才是有身分依據的清理。真的清不掉時寧可讓
    #   下一次 attempt 撞上「最多兩個」而【明確失敗】,也不要無聲強殺別人的程式
    #   —— 前者看得見、修得了;後者醫師只會看到自己的系統忽然消失。
    _session_close("重試前重置(不再以全機 PID 差集強殺)")
    if before_pids is None:
        return
    try:
        appeared = sorted(_systemftp_pids() - set(before_pids))
    except Exception:
        logging.debug("[cleanup] 計算差集失敗", exc_info=True)
        return
    if appeared:
        # 只記錄、不動作。這行是判斷「孤兒會不會累積」的依據(PID 非病人資料)。
        logging.info("[cleanup] 本任務期間新出現的 systemftp:%s"
                     "(不強殺 —— 已改由收掉自身 session 處理)", appeared)
    return


def _hidden_desktop_pids() -> set:
    """[codex 2026-07-17] 正面識別:列舉【本程式隱藏桌面 HIDDEN_DESKTOP_NAME】上所有
    top-level 視窗的擁有者 PID = 確實在隱藏桌面上跑的行程。EnumDesktopWindows 列舉該桌面
    全部視窗(含被隱形執行緒 SW_HIDE 的),故已登入的隱形孤兒也抓得到。取不到桌面/失敗回
    空集合(保守 → 上層不殺任何行程)。"""
    _DESKTOP_ENUMERATE = 0x0040
    _DESKTOP_READOBJECTS = 0x0001
    hdesk = None
    try:
        hdesk = _user32.OpenDesktopW(HIDDEN_DESKTOP_NAME, 0, False,
                                     _DESKTOP_ENUMERATE | _DESKTOP_READOBJECTS)
        if not hdesk:
            return set()
        pids: set = set()
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                      wintypes.LPARAM)

        @EnumProc
        def _cb(hwnd, _lparam):
            try:
                pid = wintypes.DWORD()
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    pids.add(pid.value)
            except Exception:
                pass
            return True

        _user32.EnumDesktopWindows(hdesk, _cb, 0)
        return pids
    except Exception:
        logging.debug("[CQ-05] 列舉隱藏桌面視窗失敗(保守回空)", exc_info=True)
        return set()
    finally:
        if hdesk:
            try:
                _user32.CloseDesktop(hdesk)
            except Exception:
                pass


def _cleanup_orphan_systemftp() -> None:
    """[CQ-05] 清掃硬退(self-watchdog/托盤退出/更新重啟)遺留在隱藏桌面的 systemftp —— 已
    登入 HIS、隱形、佔記憶體,且下次任務的 before_pids 快照會把它圈進「不可殺」而永久存活
    累積(≥2 個時配合『請勿開啟超過兩個』限制會讓後續登入更不穩)。啟動時、以及每次任務
    重試到底放棄後各呼叫一次(後者讓運行期累積的孤兒也能被清、下一輪自癒)。

    [codex 2026-07-17] 【正面識別】隱藏桌面孤兒:只殺「本 session 且【確實在本程式隱藏桌面
    上有視窗】」的 systemftp,不靠「使用者桌面暫無視窗」的負面推斷 —— 使用者手動開啟/正在
    啟動(登入視窗尚未出現)的住院系統在【使用者桌面】、不在隱藏桌面,故【永不】被誤殺,
    也無任何時間競態(不需去抖動)。清掃時機(啟動前、放棄後)隱藏桌面上不會有本次任務
    在用的實例,所以隱藏桌面上的 systemftp 都是前世孤兒。"""
    try:
        my_sid = _pid_session(os.getpid())
        if my_sid is None:
            # 多使用者/RDS:取不到本 session id → 保守整個跳過(不誤動其他 session 行程)。
            logging.debug("[CQ-05] 取不到本 session id → 保守跳過孤兒清掃")
            return
        same_session = {p for p in _systemftp_pids() if _pid_session(p) == my_sid}
        if not same_session:
            return
        orphans = same_session & _hidden_desktop_pids()
        # [2026-08-03 常駐] 活著的常駐 session 在隱藏桌面上是【常態】,不是孤兒。
        orphans -= _session_pids()
        if orphans:
            logging.warning(
                "[CQ-05] 清掃隱藏桌面殘留的 systemftp 孤兒(正面識別在隱藏桌面上): %s",
                sorted(orphans))
            close_pids(orphans)
    except Exception:
        logging.warning("[CQ-05] systemftp 孤兒清掃失敗(略過,不影響啟動)", exc_info=True)


def _note_flow_lock_skipped(trigger_label: str) -> None:
    """本輪被 `_flow_lock` 擋下 —— 順便量【持鎖者卡了多久】。

    ★這是這把鎖唯一的觀測點★ 被擋下來是它唯一會被外界看見的時刻;不在這裡量,
    「持鎖者已經死了」這件事就永遠沒有人知道(見鎖定義處的說明)。
    """
    since = _flow_lock_held_since[0]
    held = (time.monotonic() - since) if since else 0.0
    logging.info("已有一個會診查詢任務進行中，本次（%s）略過(持鎖已 %.0f 分鐘)",
                 trigger_label, held / 60.0)
    if not since or held < _FLOW_LOCK_WEDGED_SEC:
        return
    if _flow_wedge_restart_requested[0]:
        return                              # 已經要求過重啟,不重複
    _flow_wedge_restart_requested[0] = True
    # ★不可以在這裡先 logging★(外審 SF 第 1 輪 P1-1,理由見 `_force_exit`)
    _force_exit(
        f"會診查詢的流程鎖已被持有 {held / 60.0:.0f} 分鐘 → 判定持鎖者卡死"
        "(多半卡在凍結的 systemftp 視窗:raw GetWindowText 永久不返回)。"
        "這把鎖不會自癒 —— 放著不管的話之後每一輪都只是靜默略過,"
        "而 heartbeat/tick 全部正常,沒有人會發現會診查詢已經停了", code=1)


def _do_full_job(trigger_label: str, override_recipients=None, *,
                 from_retrigger: bool = False,
                 trigger_uids=(), requeued_out=None) -> None:
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
    # ★到期的「補寄給被暫時拒收的收件人」先做★(外審第 10 輪第 3 回 P1-1)
    #   放在拿 `_flow_lock` 之前:補寄不碰 HIS,不需要那把鎖,而且被鎖擋下的
    #   輪次也應該讓補寄有機會發生(它跟這一輪查不查得到會診無關)。
    try:
        _drain_pending_refusal_retries()
        # 帳上的 UNKNOWN 先拿 Message-ID 去寄件備份回查(自帶節流)——
        # 排在結案之前:結案要用的是【收斂之後】的狀態。
        _reconcile_unknown_deliveries()
        # 帳上掛太久的(多半是跨越了程式重啟,記憶體佇列已消失)→ 明確結案 + 告警
        _close_out_stale_recipient_retries()
    except Exception:
        logging.warning("[delivery] 處理待補寄時出錯(不影響本輪查詢)",
                        exc_info=True)
    if not _flow_lock.acquire(blocking=False):
        _note_flow_lock_skipped(trigger_label)
        # ★[2026-07-30 外審第 1 輪] email 觸發的要排隊補跑,不可直接丟掉★
        #   `trigger_job_async` 只在【gate 擋下】時排隊；gate 放行但 `_flow_lock`
        #   被佔住(例如 gate 逾時接管之後,舊 worker 還握著鎖)就整筆消失 ——
        #   email 觸發的醫師於是被去重卡住又收不到任何東西,乾等一個不會來的結果。
        #   只補 email：poll/排程觸發本來就會自己再來一輪，補跑只會多做白工；而
        #   `_flow_lock` 若真的永久洩漏，只補 email 也不會變成無止盡的自我重觸發。
        #
        # ★[2026-07-30 外審第 2 輪 finding：已驗證後 REJECT]★
        #   外審認為這個補跑會造成「重複寄出」：舊 worker 稍後寄完之後，排隊的這筆
        #   又會再寄一次。查證後不採納，理由三點：
        #   ① 兩者【收件人不同、對應不同請求】。舊 worker 那一輪是 45 分鐘前開始的
        #      （poll 或別人的觸發）；排隊這筆是某位醫師【剛剛親自寄信要的】，而他在
        #      這之前什麼都沒收到。回覆他不是重複，是本來就該做的事。
        #   ② 同一位醫師在去重窗過後重試也不會變成多封：`_enqueue_pending_retrigger`
        #      以 label 為鍵合併（`_merge_retrigger_recipients`），多次觸發只會合成
        #      【一筆】、收件人取聯集。
        #   ③ 外審建議的替代方案「superseded 之後所有權就不可逆轉移」會直接重現
        #      它自己在第 1 輪抓到的 bug：舊 worker 放棄、接管者拿不到 `_flow_lock`
        #      也放棄 → 兩邊都不寄。那比現況嚴重得多。
        if trigger_label == "email" and override_recipients:
            _enqueue_pending_retrigger(trigger_label, override_recipients,
                                       trigger_uids)
            # ★交還 uid:這筆工作【還沒做】,呼叫端不可以把 journal 結案★
            if requeued_out is not None:
                requeued_out.extend(trigger_uids)
            logging.info("[re-trigger] 已排隊，等目前任務結束後補跑這筆 email 觸發")
        return
    # [2026-07-25 審查] import/CoInitialize 必須在 try 內：舊版放在 acquire 與 try 之間,
    # 這兩行只要拋一次例外(自動更新正在改寫 pywin32 檔案、CoInitialize 回
    # RPC_E_CHANGED_MODE 等),鎖就【永久洩漏】——之後每次輪詢都只印 INFO「已有任務
    # 進行中」然後跳過,log 看起來完全正常,實際上會診查詢再也不會執行。
    # (ActiveTaskGate 45 分鐘會自癒,_flow_lock 不會。)
    # [codex] 兩個哨兵缺一不可：pythoncom 只代表 import 成功,不代表 CoInitialize 成功。
    # CoInitialize 若拋例外(如 RPC_E_CHANGED_MODE——該緒早被別處初始化成別種 apartment)
    # 卻仍在 finally 呼叫 CoUninitialize,等於去拆別人的 apartment,之後該緒的 COM 會壞掉。
    # ★[批次SF] 記下「從什麼時候開始持有」★ 這是被擋下的那一輪唯一能據以
    #   判斷「持鎖者是不是已經死了」的證據(見 `_note_flow_lock_skipped`)。
    _flow_lock_held_since[0] = time.monotonic()
    pythoncom = None
    com_initialized = False
    try:
        import pythoncom       # noqa: PLC0415
        pythoncom.CoInitialize()
        com_initialized = True
        # ★[2026-07-30 外審第 5 輪] 補跑在【拿到 _flow_lock 之後】要再確認一次★
        #   drain 那邊的「看墓碑 → 派送」不是原子的：舊 worker 可能在那兩步之間才
        #   寄成功。而拿到 `_flow_lock` 代表舊 worker 已經完全結束（它在最外層
        #   finally 才釋放），此刻的墓碑才是最終狀態。
        #   ★只對【補跑】做★：正常的新觸發是醫師的新請求，必須照跑。
        #   ★逐人過濾而非整批放棄★：只把已經收到的人剔掉，其餘照寄。
        #   `override_recipients` 為 None／空（解析不出寄件人）時整段跳過 —— 那時要用
        #   設定裡的 `email_trigger_recipients`，墓碑無從逐人比對（外審第 6 輪）。
        if from_retrigger and trigger_label == "email" and override_recipients:
            override_recipients = _unserved_recipients(override_recipients)
            if not override_recipients:
                logging.info(
                    "[re-trigger] 補跑的收件人在等鎖期間都已經收到結果了 → 不重複寄送")
                return
        cfg = load_config()
        # [2026-06-25] 輪詢 poll:00:00-06:00 休息時段 → 直接不開 systemftp、不寄
        # (過夜新增的會診由休息結束後第一輪 poll 的「新病歷號」比對一次補寄)。
        # [codex P2 R10] 這一段必須在寄信前置檢查【之前】:SMTP/Outlook 執行期
        # 變成不可用時,前置檢查會提早 return,永遠走不到收 session → 已登出的
        # systemftp 殭屍行程掛整夜。
        if trigger_label == "poll" and _in_quiet_hours(datetime.now(), cfg):
            logging.info("[poll] 休息時段(%02d:00-%02d:00),本次不輪詢/不寄信",
                         int(cfg.get("quiet_start_hour", 0)),
                         int(cfg.get("quiet_end_hour", 6)))
            # [2026-08-03 常駐] 休息時段把常駐 session 收掉:不查詢就沒有 keepalive,
            # 5 分鐘後會被院方強制登出,留著只是殭屍行程掛整夜;06:00 後首輪冷啟動。
            _session_close("休息時段(00-06 不輪詢),收掉常駐 session")
            return
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
        # ★[2026-08-05 外審第 5 輪 P1-04] 查詢與寄送分成兩段★
        #   `his_result` 一旦有值就不再重查 HIS;`delivery` 一旦組好就不再重組。
        #   寄信重試 = 重送【同一份】payload、同一個 Message-ID、同一張附件。
        his_result = None
        delivery = None
        for attempt in range(1, retry_count + 1):
            # ★[2026-08-05 外審第 4 輪 P1-10]★ 這一輪的 HIS 那一段做完了沒有。
            #   做完之後才失敗的(組信、附截圖、SMTP/Outlook)都與 HIS 無關 ——
            #   見下面重試分支的說明。每次 attempt 都要重設。
            his_stage_done = his_result is not None
            # [2026-08-07 外審 AT] 本次 attempt 的寄送帳本 id（每輪重設,
            # 免得上一輪的 id 被這一輪的失敗收尾誤用）。
            _did = ""
            try:
                logging.info("會診查詢任務 第 %d/%d 次嘗試（trigger=%s, 收件人組=%s, mail=%s）",
                             attempt, retry_count, trigger_label,
                             recipients_label, mail_method)
                # ★[2026-08-05 外審第 5 輪 P1-04] HIS 只查一次★
                #   上一版每個 attempt 都重跑 `run_consult_flow`,於是 SMTP timeout
                #   會導致:再開一次會診畫面、再擷取一次、再點選一次每位病人、
                #   再產生一張截圖、再組一封【內容可能不同】的信。log 卻寫著
                #   「只重試寄信」—— 措辭與行為不符。
                #   而且 SMTP 可能【已經收下第一封】只是回應逾時,第二次 attempt
                #   會用新的 Message-ID 再寄一封 → 收件人收到兩封不一樣的清單。
                #   查詢成功之後就把結果釘住,之後的 attempt 只重試寄送。
                if his_result is None:
                    his_result = run_consult_flow(trigger_label)
                    his_stage_done = True   # 這裡之後的失敗都不是 HIS 的問題
                else:
                    logging.info("沿用上一次 attempt 已查到的會診結果(HIS 不重查)")
                shot, extracted_text, extracted_html, roster_texts = his_result
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
                        _lost = ""
                        if not _baseline_initialized():
                            _why = _baseline_absence_reason()
                            if _why == "first_install":
                                # [2026-06-25 user] 第一次啟動還沒建過基準 → 開機這輪只建
                                # 基準、不寄,避免每次重啟收一封「全部未回覆清單」的信。
                                # [外審第 6 輪 P2-01] 未經確認的清單不建基準(下輪再建)。
                                # ★[2026-08-08 外審第 5 回] 順序:先確認標記能
                                #   落地,再建基準★
                                #   我上一版是「先建基準 → 發現標記沒落地 →
                                #   改走 fail-open 寄信」。但基準【已經 commit
                                #   了】(記憶體 + initialized + 可能已存檔),
                                #   而 fail-open 那封信如果寄不出去,下一輪就會
                                #   認為所有會診都通知過而什麼都不寄 ——
                                #   那批會診永遠送不出去。
                                #   基準只能在【送達之後】才可以前進,所以標記
                                #   落不了地時,連基準都不要建:走 fail-open,
                                #   由既有的「寄成功才更新基準」那條路接手。
                                # ★[外審第 6 回] 先做【不變動狀態】的合格檢查★
                                #   `_may_update_baseline()` 是純判斷。上一版
                                #   直接先寫標記,而清單若是「未經回讀確認」的,
                                #   後面就不會建基準 —— 於是留下「有標記、沒基準」
                                #   → 下一輪判成基準遺失,把整份清單寄出去 + 假告警。
                                #   不合格的清單必須讓標記與基準【都不要出現】。
                                if not _may_update_baseline(roster_texts):
                                    logging.warning(
                                        "[poll] 首輪清單未經回讀確認 → 這一輪不建"
                                        "基準也不留標記(下一輪重新比對)")
                                    _note_job_success()
                                    return
                                if not _mark_baseline_established():
                                    logging.error(
                                        "[poll] ★「建立過基準」的標記無法寫入★ "
                                        "→ 本輪不建基準、改為 fail-open 照常寄信"
                                        "並告警;請確認 settings 目錄可寫")
                                    _why = "marker_not_durable"
                                elif _save_notified_if_eligible(
                                        roster_texts, _poll_sig,
                                        reason="建立首次基準"):
                                    logging.info(
                                        "[poll] 首次建立會診基準(%d 筆),本輪不寄信",
                                        len(_poll_sig))
                                    _note_job_success()
                                    return
                                else:
                                    _note_job_success()
                                    return
                            # ★[2026-08-04 外審 P1-05] 基準遺失/損毀不可以靜默吞掉★
                            #   這台機器建立過基準,現在卻讀不到 —— 我們【不知道】哪些
                            #   會診已經通知過。靜默重建等於把當下所有未回覆會診標成
                            #   「已通知」,它們從此不會有人收到,而且沒有任何跡象。
                            #   改成 fail-open:當作全部都是新的寄出去一次,信裡註明要
                            #   人工核對。寧可多寄一封讓人核對,不可以無聲漏掉會診。
                            _lost = _why
                            logging.error(
                                "[poll] ★會診通知基準遺失/損毀(%s)★ 無從得知哪些已通知 → "
                                "本輪視為全新並寄出整份清單供人工核對(%d 筆)",
                                _why, len(_poll_sig))
                            _alert_baseline_lost(_why, len(_poll_sig))
                            _poll_extract_note = (
                                "⚠ 會診通知基準遺失／損毀，或首次基準無法可靠留存，"
                                "本程式已無從得知哪些會診先前通知過。本信列出"
                                "【目前全部未回覆的會診】，請人工核對是否有新的；"
                                "下一輪起恢復正常比對。")
                        # [2026-08-04 外審 P1-04] 不可以直接集合相減 —— 升級當下
                        # 基準還是舊格式(只有病歷號)，直接相減會把每一張既有會診
                        # 都當成新的而整份重寄。見 `_new_consult_ids`。
                        _new = set(_poll_sig) if _lost else _new_consult_ids(_poll_sig)
                        if not _new:
                            # [2026-07-25 審查] 基準必須【剪枝】成目前清單,否則已回覆而
                            # 離開清單的病歷號會永遠留在基準裡 → 同一床日後【再次開會診】
                            # 時算不出「新」,那張會診單就永遠不會通知(除非剛好有別的新病人
                            # 一起出現才連帶寄出整份清單)。
                            # 安全性:_save_notified 是整組取代,而「有寄信」那條路徑
                            # (下方 _save_notified(_consult_signature_from_roster(...)))
                            # 本來就是這樣剪枝的 → 兩條路徑語意一致,不引入新的重寄風險;
                            # 且此處必然是清單解析成功的分支(解析失敗走 fail-open 不更新基準)。
                            if _poll_sig != _load_notified():
                                # [外審第 6 輪 P2-01] 未確認的短清單不可拿來剪枝:
                                # 還在清單上的會診被剪掉 → 之後又變「新」→ 重寄。
                                if _save_notified_if_eligible(
                                        roster_texts, _poll_sig, reason="剪枝"):
                                    logging.info(
                                        "[poll] 已回覆離開清單的會診從基準剪除"
                                        "(日後同一床再會診才通知得到)")
                            logging.info("[poll] 目前 %d 筆會診都已通知過,無新會診 → 不寄信",
                                         len(_poll_sig))
                            # [codex] 「跑成功但沒有新會診」是【健康】的一輪,必須清零
                            # 連續失敗計數——否則零星失敗會被累加成假的「連續故障」,
                            # 恢復後也永遠清不掉,冷卻期一過又誤報一次。
                            _note_job_success()
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
                # ★[2026-07-30 外審 P2-01] 寄信前先確認「我還是現役嗎」★
                #   這段流程可能跑很久（HIS 慢/凍結/登入重試）。超過 gate 的
                #   stale_after_sec（45 分）之後，新的一輪已經接手在做同一件事；
                #   這時才把【十幾分鐘前抓的清單】寄出去，收信人會拿到舊資料，
                #   而且下面還會去更新「已通知病歷號」基準 → 新的那一輪反而看不到
                #   新會診、漏寄給團隊。gate 終止不了我們，但我們可以自己退場。
                if current_worker_superseded():
                    raise JobSuperseded(
                        "本輪會診查詢已執行超過逾時上限並被新的一輪接管，"
                        "手上這份清單已經過時 → 不寄、也不更新已通知基準")
                # ★[2026-08-05 外審第 5 輪 P1-04] payload 只組一次★
                #   組好之後就固定:同一份主旨/內文/附件/Message-ID。重試 = 重送
                #   同一封信,而不是「再查一次、再組一封新的」。
                #   截圖也只落地一次 —— 舊寫法每個 attempt 都 `_materialize_shot`,
                #   三次 attempt 就是三張病人畫面躺在磁碟上。
                if delivery is None:
                    # ★[2026-08-04 外審 P1-08] 到這裡才把截圖落地★
                    #   走到這一行代表「這一輪真的要寄信」。沒有新會診的輪次在
                    #   上面就 return 了,磁碟上不會多出任何病人畫面。
                    delivery = _DeliveryArtifact(
                        recipients=tuple(recipients),
                        subject=subject,
                        text_body=final_body,
                        html_body=final_html,
                        attachment=_materialize_shot(shot),
                        message_id=_new_message_id(),
                        business_key=_consult_business_key(
                            roster_texts, recipients, subject),
                    )
                if mail_method == "smtp":
                    # ★[2026-08-07 外審 AT/AW] 每一次寄送都進帳本★
                    #   begin 在【送出之前】——這樣即使送出當下斷電,重啟後看到的
                    #   是一筆 SUBMITTING(待查),而不是「什麼都沒發生」。
                    _did = _delivery_begin(delivery, trigger_label)
                    # ★[2026-08-07 外審第 8 輪 P1-03] 每一筆都要有終局狀態★
                    #   舊寫法只在【成功】與【結果不明】兩條路 settle。連不上、
                    #   認證失敗、5xx、SMTP 未設定這些「確定沒送出去」的錯誤直接
                    #   往上拋 —— 那一筆就永遠停在 SUBMITTING,而 prune 不會清掉
                    #   SUBMITTING。三次 attempt 就留下兩筆假的「可能送到一半」。
                    #   現在還無害(帳本純寫入、沒有當閘門),但等 has_live_delivery
                    #   接上寄送決策,這些假 SUBMITTING 會永久擋住該筆會診。
                    #   ★不變式:begin 之後,每一條出口都必須 settle★
                    try:
                        _refused = send_via_smtp(
                            delivery.attachment, delivery.subject,
                            delivery.text_body, list(delivery.recipients),
                            html_body=delivery.html_body,
                            message_id=delivery.message_id)
                    except DeliveryOutcomeUnknown:
                        # 可能已送達 → 待查(留給 Message-ID 回查收斂)
                        _delivery_settle(_did, unknown=True)
                        _did = ""              # 已結案,終局分支不要再 settle 一次
                        raise
                    except Exception:
                        # 確定沒送出去(連不上/認證/5xx/未設定)→ FAILED,不是待查。
                        # 若哪天 SMTP 層把「DATA 之後斷線」也改判成結果不明,
                        # 它會走上面那條,不會被誤記成 FAILED。
                        _delivery_settle(_did, failed=True)
                        _did = ""
                        raise
                    # ★先把【已知的】結果落地,再去補寄★
                    #   (外審第 10 輪第 2 回 P2-7)舊寫法是補寄完才 settle,
                    #   於是「補寄給 B 時結果不明」會讓整筆變成 UNKNOWN ——
                    #   連【已經確定送達 A】這個事實都一起丟掉了。
                    #   已知的事實要先寫下來,不能被後面的不確定性吃掉。
                    _origin_did = _did
                    _delivery_settle(_did, refused=_refused)
                    _did = ""
                    # ★[2026-08-08 外審第 10 輪 P1-01] 暫時性拒收要真的補寄★
                    #   舊寫法把 refused 記進帳本就繼續往下走:更新已通知基準、
                    #   任務記成功。那幾位收件人不但這一輪沒收到,下一輪也不會
                    #   再寄(基準已經前進)——一則臨床通知就這樣永久消失,
                    #   而 log 上是一次成功的寄送。
                    _refused = _resend_transient_refusals(
                        delivery, _refused, trigger_label,
                        origin_did=_origin_did)
                    # ★同一輪補不完的,不可以就這樣走人★(第 3 回 P1-1)
                    #   下面馬上要更新「已通知基準」,一旦推進,這批會診就再也
                    #   不會被寄給任何人了 —— 那幾位等於永久收不到。
                    from cmuh_common.delivery_ledger import (  # noqa: PLC0415
                        R_TRANSIENT as _RT, classify_refusal as _cls,
                    )
                    if any(_cls(_refusal_code(_i)) == _RT
                           for _i in (_refused or {}).values()):
                        _schedule_refusal_retry(delivery, _refused,
                                                trigger_label, _origin_did)
                else:
                    send_via_outlook(delivery.attachment, delivery.subject,
                                     delivery.text_body,
                                     list(delivery.recipients),
                                     sender_account=sender,
                                     html_body=delivery.html_body)
                # [2026-06-25] 寄出成功 → 更新「已通知病歷號」基準,下一輪 poll 不再重複寄同一批。
                # 【只在寄給一般收件人時更新】(poll / 手動):email 觸發是寄給「觸發醫師本人」、
                # 不是團隊一般名單,若也更新基準會害下一輪 poll 看不到這筆新會診而漏寄給團隊
                # (Codex 指出)。override_recipients 只在 email 觸發時有值 → 用 label 判斷即可。
                # [CQ-03] 只在「roster 擷取成功(非 None)」時才更新基準:手動觸發但擷取失敗
                # 時,若用空集合覆寫基準,下一輪 poll 擷取恢復 → 全部未回覆會診變「新」→ 對團隊
                # 重複寄整份清單。roster is None(解析失敗/停用)一律不動基準。
                # ★[2026-08-05 外審第 5 輪 P1-06]★ 「可以寄」與「可以更新基準」
                #   是兩個問題。截圖後回讀失敗時內容仍可信(照寄),但我們沒有
                #   確認過它仍代表當下 → 不可以宣稱這些都已經通知過。
                #   見 `_may_update_baseline` / `_RosterSnapshot.as_unverified`。
                if trigger_label != "email":
                    try:
                        _save_notified_if_eligible(
                            roster_texts,
                            _consult_signature_from_roster(roster_texts),
                            reason="更新已通知基準")
                    except Exception:
                        logging.debug("更新 consult_notified 失敗", exc_info=True)
                # ★[2026-07-30 外審第 2/3 輪] 已經親自寄給這些醫師 → 撤掉補跑佇列裡
                #   同一批收件人，否則他們會在幾秒內收到兩封幾乎一樣的清單。
                #   詳細取捨（為何不用「所有權不可逆轉移」）見 _discard_served_retriggers。
                if trigger_label == "email" and override_recipients:
                    _discard_served_retriggers(trigger_label, override_recipients)
                    # 已派出去、但還沒進 `_flow_lock` 的補跑拿不到佇列了，
                    # 只能靠這份墩碑在它真正做事之前自己發現（外審第 4 輪）。
                    _note_served_recipients(override_recipients)
                logging.info("會診查詢任務成功（第 %d 次嘗試）", attempt)
                _note_job_success()      # [2026-07-25] 清空連續失敗計數
                return  # 成功就跳出
            except Exception as e:
                last_err = e
                # ★[2026-07-30 外審] 不可重試,但【仍要走完終局收尾】★
                #   我第一版另開一個 except 分支直接 return —— 那會跳過下面的
                #   `_release_trigger_dedup` 與 `_send_failure_notice_async`:
                #   email 觸發的醫師會被去重卡住(5 分鐘內重發無效)、又收不到失敗
                #   通知,只能乾等一個永遠不會來的結果。修一個洞不可以開另一個。
                #   故改成沿用同一條路,只是【略過 backoff 重試】。
                # ★[2026-07-30 外審 P2-01] JobSuperseded 也是 fatal：重試沒意義
                #   （新的一輪正在做同一件事），但必須走完下面的終局收尾。
                # [2026-08-03] HISStartupBlocked（BDE 起不來）同樣 fatal：重試
                # 沒有意義，而且它與帳密無關 → log 也不能沿用「帳密」那句措辭。
                # [2026-08-05 外審第 5 輪 P1-02] UnmanagedSessionError 也是 fatal:
                #   帳上有關不掉的 session 時,重試三次只是再撞三次同一道閘門,
                #   而且每次都會多等一輪 backoff。等下一輪排程重試就好。
                # [外審第 6 輪 P1-07] DeliveryOutcomeUnknown 也是 fatal:
                #   信可能已經送達,重試 = 可能寄第二封。
                fatal = isinstance(e, (LoginNotCompleted, JobSuperseded,
                                       HISStartupBlocked, UnmanagedSessionError,
                                       DeliveryOutcomeUnknown))
                if isinstance(e, DeliveryOutcomeUnknown):
                    logging.error("會診查詢:寄信結果不明 → 不重試(避免重複寄出):%s", e)
                elif isinstance(e, UnmanagedSessionError):
                    logging.error("會診查詢:%s", e)
                elif isinstance(e, JobSuperseded):
                    logging.error("會診查詢：%s", e)
                elif isinstance(e, HISStartupBlocked):
                    logging.error("會診查詢:住院醫囑系統起不來 → 不重試"
                                  "(與帳密無關)：%s", e)
                    # [2026-08-03 使用者定案] BDE 起不來 → 閒置滿 30 分鐘自動重開機
                    _schedule_bde_reboot_watch()
                elif fatal:
                    logging.error("會診查詢:登入沒有完成 → 不重試(避免同一組帳密"
                                  "被連續送出而逼近鎖定門檻)：%s", e)
                else:
                    logging.error("會診查詢任務第 %d/%d 次失敗：%s",
                                  attempt, retry_count, e, exc_info=True)
                if attempt < retry_count and not fatal:
                    # exponential backoff (3s, 30s, 90s)；attempt 從 1 開始
                    backoff = (BACKOFF_SCHEDULE[attempt - 1]
                               if attempt - 1 < len(BACKOFF_SCHEDULE)
                               else BACKOFF_SCHEDULE[-1])
                    # ★[2026-08-05 外審第 4 輪 P1-10] 寄信失敗不可以重置 HIS session★
                    #   `_kill_systemftp` 現在做的是 `_session_close(...)` —— 收掉
                    #   常駐登入。但它掛在【所有】可重試錯誤上,包括組信/截圖/SMTP:
                    #       會診查完(HIS 一切正常) → SMTP timeout → 收掉登入
                    #       → 下一次 attempt 冷啟動 → ★再送一次帳密★
                    #   帳密重送正是這幾批一路在防的事,而失敗根本不在 HIS 這一側。
                    #   HIS 那一段做完之後才失敗 → 保留 session,只重試寄信。
                    if his_stage_done:
                        logging.info(
                            "失敗發生在【寄信/組信】階段(HIS 這一段已完成) → "
                            "保留常駐登入,只重試寄信(sleep %d 秒)", backoff)
                    else:
                        logging.info(
                            "殺 systemftp.exe 後重試（sleep %d 秒，exponential backoff）",
                            backoff)
                        _kill_systemftp(job_before_pids)
                    time.sleep(backoff)
                else:
                    if isinstance(last_err, JobSuperseded):
                        logging.error("會診查詢：被逾時接管 → 放棄且不重試"
                                      "（新的一輪正在跑）。%s", last_err)
                    elif isinstance(last_err, HISStartupBlocked):
                        logging.error("會診查詢:住院醫囑系統起不來 → 放棄且不"
                                      "重試(與帳密無關)。最後錯誤：%s", last_err)
                    elif fatal:
                        logging.error("會診查詢:登入沒有完成 → 放棄且不重試。"
                                      "最後錯誤：%s", last_err)
                    else:
                        logging.error(
                            "會診查詢任務已重試 %d 次仍失敗，放棄。最後錯誤：%s",
                            retry_count, last_err)
                    # 連續失敗達門檻 → 告警。輪詢模式「沒信」是常態,故障不主動說
                    # 就沒人會發現(見 _note_job_failure)。
                    # ★[2026-08-02 使用者定案] 收件人改為【只有開發者】★
                    #   推翻 2026-07-25 的「寄給團隊名單」定案 —— 使用者實際收到那封信
                    #   之後認為:系統/自動化故障訊息不該騷擾整組臨床人員,他們對
                    #   「等不到主畫面」也無從處理。臨床事件(會診查詢結果、止掛達門檻)
                    #   仍照舊寄給各自的名單,只有【故障告警】改道。
                    #   原本的兩個坑因此自然消失:①email 觸發時 recipients 已被改寫成
                    #   觸發醫師本人(沿用會讓他同一次失敗收兩封);②團隊名單為空時
                    #   不可 `or recipients` 當後備(codex R2)。現在收件人與 cfg 無關。
                    # ★[2026-08-05 外審第 5 輪 P2-05] 沒寄出去的截圖不留在磁碟上★
                    #   `_materialize_shot` 的「留著當線索」取捨只對【寄成功】成立。
                    #   這張圖沒有任何人看過、也沒有臨床用途,留著純粹是多出來的
                    #   PHI 暴露面。
                    # ★[2026-08-06 外審 P1-04] 「結果不明」不可以走一般失敗收尾★
                    #   UNKNOWN 的定義就是「原信可能【稍後仍會送達】」。一般收尾會
                    #   刪截圖、釋放 email 觸發去重、並回信告訴觸發者「可立即重試」
                    #   —— 使用者照做 → 原信隨後送達 → 他收到【兩封】。
                    #   故另走一條:保留截圖、不釋放去重、明確請他先不要重發。
                    if isinstance(last_err, DeliveryOutcomeUnknown):
                        # [2026-08-07 外審 AT] 記進帳本(跨重啟仍知道有一筆待查),
                        # 之後可用 Message-ID 回查寄件備份收斂成送達/未送達。
                        _delivery_settle(_did, unknown=True)
                        logging.error(
                            "會診查詢:寄信結果不明 → 保留截圖、不釋放觸發去重、"
                            "不建議重試(原信可能稍後送達);已記入寄送帳本待回查:%s",
                            last_err)
                        if trigger_label == "email" and override_recipients:
                            _send_delivery_unknown_notice_async(
                                override_recipients, str(last_err))
                        try:
                            _note_job_failure(_developer_alert_recipients(),
                                              f"[寄信結果不明] {last_err}")
                        except Exception:
                            logging.debug("結果不明告警處理失敗（略過）",
                                          exc_info=True)
                        break
                    _discard_undelivered_shot(delivery)
                    try:
                        _note_job_failure(_developer_alert_recipients(),
                                          str(last_err))
                    except Exception:
                        logging.debug("連續失敗告警處理失敗（略過）", exc_info=True)
                    # [2026-07-17] 「等不到登入視窗」多因隱藏桌面累積的 systemftp 孤兒
                    # (更新重啟/硬退遺留,運行期間累積)佔滿『最多兩個』上限。啟動清掃只在
                    # 啟動跑一次、重試的 _kill_systemftp 又只殺本次新增 → 孤兒永久存活、會診
                    # 查詢卡死到下次重啟。放棄後【清一次孤兒】(只殺本 session 且使用者桌面無
                    # 可見視窗者＝隱藏桌面殘留,絕不動使用者手動開的住院系統)讓下一輪能自癒。
                    try:
                        _cleanup_orphan_systemftp()
                    except Exception:
                        logging.debug("放棄後孤兒清掃失敗（略過）", exc_info=True)
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
                    # ★[外審第 3 輪] 收尾完就結束,不可讓迴圈再跑一輪★
                    #   原本沒有 break 是因為 else 只在【最後一次】attempt 才進得來;
                    #   fatal 讓它可能在第 1 次就進來 —— 沒 break 就會回頭再送一次
                    #   帳密,而且把失敗通知再寄一遍。我上一版正是這樣。
                    break
    finally:
        if com_initialized:            # 只有 CoInitialize 真的成功過才配對 Uninitialize
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        # ★清空必須在 release【之前】★ 反過來的話,下一條緒可能已經拿到鎖並
        #   寫入自己的起始時間,我們接著把它抹成 0 —— 那條的持有時間就永遠
        #   量不到,卡死判定對它完全失效。
        _flow_lock_held_since[0] = 0.0
        _flow_lock.release()           # ★無論如何都要釋放（見上方鎖洩漏註解）


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


_pending_retrigger_uids: dict = {}


def _enqueue_pending_retrigger(trigger_label: str, override_recipients,
                               trigger_uids=()) -> None:
    """記下一筆 pending re-trigger；同 label 合併 email 收件人，不無限堆積。

    ★uid 也要合併★(外審第 11 輪第 2 回 F3)同 label 的多筆會被併成一筆,
    對應的觸發信 uid 自然也不只一個 —— 補跑做完要把【全部】結案,
    少結一個就會在下次開機被補跑、醫師收到第二封。
    """
    with _pending_retriggers_lock:
        existing = _pending_retriggers.get(trigger_label)
        _pending_retriggers[trigger_label] = _merge_retrigger_recipients(
            existing, override_recipients)
        if trigger_uids:
            cur = set(_pending_retrigger_uids.get(trigger_label) or ())
            cur |= set(trigger_uids)
            _pending_retrigger_uids[trigger_label] = cur


# ★[2026-07-30 外審第 4 輪] 已服務收件人的墓碑（tombstone）★
#   只把人從 `_pending_retriggers` 拿掉還不夠：`_drain_pending_retriggers()` 是
#   【先把佇列複製走並清空、才啟動補跑 worker】。若舊 worker 剛好在這個空窗裡寄成功，
#   `_discard_served_retriggers()` 看到的是空佇列，什麼都拿不掉 → 那個已經派出去的
#   補跑照樣執行、照樣再寄一封。故另外記一份「剛剛已經親自服務到誰」，由補跑 worker
#   在【真正要做事之前】自己檢查。
_SERVED_TOMBSTONE_TTL_SEC = 180.0
_served_recipients_recent: dict = {}


def _note_served_recipients(recipients) -> None:
    """記下「剛剛已經親自寄給這些人」。"""
    if not recipients:
        return
    now = time.time()
    with _pending_retriggers_lock:
        for r in recipients:
            key = str(r).strip().lower()
            if key:
                _served_recipients_recent[key] = now
        # 順手清掉過期的，不讓它無限長大
        for key in [k for k, ts in _served_recipients_recent.items()
                    if now - ts > _SERVED_TOMBSTONE_TTL_SEC]:
            _served_recipients_recent.pop(key, None)


def _unserved_recipients(recipients):
    """把【剛剛已經親自寄過】的人溤掉，只留還沒收到的。

    ★[2026-07-30 外審第 5 輪] 不可做 all-or-nothing 判斷★
    佇列是 `[D, E]`、舊 worker 已經寄給 D 但 E 還沒收到 —— 上一版回「不是全部」
    就拿原名單 `[D, E]` 整批補跑，D 還是收到兩封。逐人過濾才對：
    E 照寄（少寄給等結果的醫師比多寄嚴重得多）、D 不重複。

    recipients 為 None（非 email 觸發）→ 原樣回 None，不介入。
    """
    if recipients is None:
        return None
    now = time.time()
    out = []
    with _pending_retriggers_lock:
        for r in recipients:
            key = str(r).strip().lower()
            if not key:
                continue
            ts = _served_recipients_recent.get(key)
            if ts is not None and now - ts <= _SERVED_TOMBSTONE_TTL_SEC:
                continue
            out.append(r)
    return out


def _discard_served_retriggers(trigger_label: str, served_recipients) -> None:
    """已經【親自寄給】這些收件人了 → 把他們從補跑佇列拿掉。

    ★[2026-07-30 外審第 2/3 輪] 這是「同一位醫師收到兩封」的正解★
    情境：醫師 D 在 t0 寄觸發信 → 任務卡住 45 分鐘 → D 在 t45 再寄一次 → gate 逾時
    接管 → 接管者拿不到 `_flow_lock` 於是把 D 排進補跑佇列 → 舊 worker 這時終於寄給
    D（回答 t0 那次）→ 佇列補跑又寄一次（回答 t45 那次）。D 在幾秒內收到兩封幾乎一樣
    的清單。

    為什麼不採用外審建議的「所有權不可逆轉移」：那會讓舊 worker 放棄、接管者又拿不到
    `_flow_lock` 也放棄 → **兩邊都不寄，D 什麼都收不到**（外審自己在第 1 輪抓到的
    bug）。「絕不讓醫師等一個不會來的結果」優先於「資料新鮮度」。

    為什麼丟掉補跑是安全的：email 觸發【不會】更新「已通知病歷號」基準，所以這 45
    分鐘內若真的來了新會診，下一輪 poll 仍然會照常寄給團隊 —— 沒有任何會診被吞掉。
    """
    if not served_recipients:
        return
    served = {str(r).strip().lower() for r in served_recipients if str(r).strip()}
    if not served:
        return
    with _pending_retriggers_lock:
        pending = _pending_retriggers.get(trigger_label)
        if pending is None:
            return
        kept = [r for r in pending
                if str(r).strip().lower() not in served]
        if len(kept) == len(pending):
            return
        if kept:
            _pending_retriggers[trigger_label] = kept
        else:
            _pending_retriggers.pop(trigger_label, None)
        logging.info(
            "[re-trigger] 已親自寄給 %s → 從補跑佇列移除（避免同一位醫師收到兩封）",
            ", ".join(sorted(served)))


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
                pending_uids = dict(_pending_retrigger_uids)
                _pending_retriggers.clear()
                _pending_retrigger_uids.clear()
            for label, override in pending.items():
                # ★[2026-07-30 外審第 4 輪] 派出去之前先看墩碑★
                #   上面已經「複製佇列並清空」了，而這裡還要等
                #   `_RETRIGGER_DELAY_SEC`。舊 worker 若在這個空窗裡寄成功，
                #   `_discard_served_retriggers()` 面對的是空佇列、什麼都拿不掉
                #   → 這筆補跑照樣執行、同一位醫師收到第二封。
                #   ★只在這裡檢★：放在 `_do_full_job` 會連【正常的新觸發】也一起
                #   擋掉（實測弄紅了四支既有測試）—— 墩碑只能管補跑。
                send_to = override
                # ★override is None 必須【原樣派送】★
                #   email 觸發若解析不出寄件人（malformed From），override 就是 None，
                #   而 `_do_full_job` 會退回設定裡的 `email_trigger_recipients`。
                #   我上一版用 `if not send_to: continue` 一併把 None 當成「沒人要寄」
                #   → 那批收件人永遠收不到結果，而觸發者還被去重窗卡著（外審第 6 輪）。
                #   墓碑本來就只能對【指名的收件人】逐人比對。
                if label == "email" and override is not None:
                    send_to = _unserved_recipients(override)
                    dropped = [r for r in override if r not in send_to]
                    if dropped:
                        logging.info(
                            "[re-trigger] 這些人剛剛已經收到結果了，不重複補跑：%s",
                            ", ".join(str(x) for x in dropped))
                    if not send_to:
                        # ★這裡不 continue 掉 uid★ 那幾封信的請求【已經被服務】
                        #   (剛剛那一輪就是寄給他們的),所以要結案,
                        #   否則下次開機會補跑一次、醫師收到第二封。
                        for _u in (pending_uids.get(label) or ()):
                            try:
                                _trigger_journal_done(_u)
                            except Exception:
                                logging.debug("[trigger] 結案失敗", exc_info=True)
                        continue
                logging.info(
                    "[re-trigger] 補跑被擋下的觸發：%s", label)
                try:
                    trigger_job_async(
                        label, override_recipients=send_to,
                        from_retrigger=True,
                        trigger_uids=tuple(pending_uids.get(label) or ()))
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


_TRIGGER_REJECT_ALERT_INTERVAL_SEC = 3600.0
_trigger_reject_alert_at = 0.0


def _alert_trigger_rejected(senders: list) -> None:
    """白名單醫師的觸發信被擋下來 → 主動說。

    ★[2026-08-08 外審 F1]★ `require_authenticated_trigger` 之所以一直不敢
    預設打開,是怕「功能靜默失效」。那個顧慮的正確解法不是把門開著,而是
    【不讓它靜默】:這封告警一寄出,就有兩種可能都被涵蓋 ——
      * 我們的驗證判定太嚴 → 第一次就有人知道要調整;
      * 真的有人在偽造這位醫師的位址 → 那更該讓人知道。
    """
    global _trigger_reject_alert_at
    now = time.time()
    if now - _trigger_reject_alert_at < _TRIGGER_REJECT_ALERT_INTERVAL_SEC:
        return
    _trigger_reject_alert_at = now
    body = (
        "以下白名單寄件人寄來了觸發信,但沒有通過 SPF/DKIM/DMARC 驗證,"
        "因此【沒有觸發】會診查詢:\n"
        f"  {', '.join(sorted(str(x) for x in senders))}\n\n"
        "兩種可能:\n"
        "  (1) 這位醫師的寄送路徑不帶可信的 Authentication-Results ——\n"
        "      那要調整判定,否則他會一直以為寄了信卻沒下文;\n"
        "  (2) 有人偽造這位醫師的 From 想遠端觸發 —— 那這次擋對了。\n\n"
        "請看 consult_query.log 中同一時間的「未通過寄件人驗證」那幾行。")

    def _worker():
        global _trigger_reject_alert_at
        try:
            from cmuh_common.smtp_mail import send_mail  # noqa: PLC0415
            send_mail(recipients=[str(x) for x in _developer_alert_recipients()],
                      subject="會診自動化:觸發信未通過寄件人驗證(未觸發)",
                      body=body, attachment_path=None, category="system")
        except Exception:
            _trigger_reject_alert_at = (time.time()
                                        - _TRIGGER_REJECT_ALERT_INTERVAL_SEC + 600)
            logging.warning("[trigger] 未驗證告警寄送失敗(10 分鐘後重試)",
                            exc_info=True)
    try:
        threading.Thread(target=_worker, name="ConsultTriggerRejectAlert",
                         daemon=True).start()
    except Exception:
        logging.debug("[trigger] 未驗證告警執行緒啟動失敗", exc_info=True)


def _handoff_email_triggers(matched_uids, senders,
                            require_auth: bool = True) -> None:
    """先落地、再標已讀、最後觸發 —— 順序就是這個修正的全部內容。

    ★[2026-08-08 外審 F3]★ 舊流程是「標已讀 → 回到排程器 → 起 worker」,
    中間任何中止都讓那封信永久消失(它已經不是 UNSEEN,再也掃不到)。
    現在:journal 落地失敗就【不標已讀】—— 信留在 UNSEEN,下一輪重來,
    寧可多觸發一次也不要漏掉一次會診請求。
    """
    want = {str(x).strip().lower() for x in (senders or [])}
    todo = []
    for _row in (matched_uids or []):
        _uid, _addr = str(_row[0]), str(_row[1] or "").strip().lower()
        _ok = bool(_row[2]) if len(_row) > 2 else False
        # ★只處理【通過驗證】的那幾封★(外審)同一位寄件人可能同時有一封合法
        #   已驗證信與一封偽造未驗證信;把兩封都拿去觸發等於讓偽造那封也生效。
        # ★但「被接受」的定義要跟著 strict 設定走★(外審第 2 回)
        #   使用者明確關掉 `require_authenticated_trigger` 時,未驗證的信【是】
        #   被接受的。上一版仍然把它們全部濾掉 → 掉到下面「找不到 uid」的
        #   後備路徑直接觸發(沒有 journal),而 `_final(only_unauth=True)` 又把
        #   那封信標成已讀 —— 中途一中止,請求就消失了。
        if _addr in want and (_ok or not require_auth):
            todo.append((_uid, _addr))
    if not todo:
        # 沒有配對到 uid(理論上不會)→ 退回舊行為,至少不要漏掉請求。
        logging.warning("[trigger] 找不到觸發信的 uid → 直接觸發(信會留在未讀)")
        for addr in sorted(want):
            trigger_job_async("email", override_recipients=[addr])
        return
    landed = []
    _failed_addrs: set = set()
    for uid, addr in todo:
        if _trigger_journal_add(uid, addr):
            landed.append((uid, addr))
        else:
            _failed_addrs.add(addr)
            # ★[外審] 去重預約也要一起撤銷★
            #   `_trigger_is_duplicate()` 在這之前就把「這位五分鐘內處理過」
            #   寫下去了。journal 沒落地時信會留在未讀 —— 但下一輪它會撞上
            #   去重分支,而去重是【終局處置】會把信標成已讀。
            #   結果:工作沒做、journal 沒紀錄、信卻永久消失。
            #   兩個各自正確的修正組合出來的洞。
            logging.error("[trigger] 工作沒能落地 → 不標已讀,下一輪重來:%s",
                          addr)
    # ★只有【這位寄件人一封都沒落地】時才撤銷去重★(外審第 2 回)
    #   同一位可能有兩封:一封落地了、工作正在跑,另一封沒落地。
    #   這時撤銷去重,那封沒落地的下一輪就會再開一個工作 —— 同一位醫師
    #   同時被服務兩次,正是去重要防的事。
    _landed_addrs = {a for _u, a in landed}
    for _a in sorted(_failed_addrs - _landed_addrs):
        _undo_trigger_dedup(_a)
    if not landed:
        return
    try:
        from cmuh_common.imap_reader import mark_uids_seen  # noqa: PLC0415
        mark_uids_seen([u for u, _ in landed])
    except Exception:
        # 標不掉只是會重複命中,而重複命中有 dedup 擋著;工作已經落地了。
        logging.warning("[trigger] 標已讀失敗(工作已落地,不影響)", exc_info=True)
    for uid, addr in landed:
        trigger_job_async("email", override_recipients=[addr],
                          trigger_uids=(uid,))


# ═══════════ 觸發信的持久化接手(外審第 11 輪 F3)═══════════
#   舊流程:`check_trigger` 在回傳【之前】就把命中信標成 \Seen,
#   之後才回到排程器、才起 worker。這中間程式結束/重啟的話,那封信
#   已經不是 UNSEEN,永遠不會再被掃到 —— 醫師乾等一個不會來的結果。
#   新流程:先把「這個 uid 對應的工作」寫進 journal(落地) → 才標 \Seen
#   → 才觸發。任一步之前中止,重啟後都補得回來:
#     * journal 之前中止  → 信還是 UNSEEN,下一輪照樣掃到;
#     * journal 之後中止  → journal 記得,開機補跑(標沒標到都一樣)。
#   ★journal 只存 uid 與寄件人位址★ —— 那是醫師的公務信箱,不是病人資料。
_TRIGGER_JOURNAL_NAME = "consult_trigger_journal.json"
_trigger_journal_lock = threading.Lock()


def _trigger_journal_path() -> str:
    from cmuh_common.paths import get_settings_dir  # noqa: PLC0415
    return os.path.join(get_settings_dir(), _TRIGGER_JOURNAL_NAME)


def _trigger_journal_load() -> tuple:
    """回 (內容, 是否讀得到)。

    ★[2026-08-08 外審第 2 回 F2] 「讀不到」必須是一個【可分辨的答案】★
    上一版讀不到就回空字典,而 `_trigger_journal_add` 拿著那個空字典加上
    新的 uid 就寫回去 —— 帳上原本那幾筆【已經標成已讀、再也掃不到】的待辦
    就這樣被覆蓋掉、永久消失。這正是這個專案一路在修的同一個病灶:
    把「讀不到」當成某個確定的答案。
    """
    from cmuh_common.atomic_io import safe_load_json_ex  # noqa: PLC0415
    data, status = safe_load_json_ex(_trigger_journal_path(), {},
                                     backup_on_corrupt=False)
    if status not in ("ok", "missing") or not isinstance(data, dict):
        logging.error("[trigger] 讀不到觸發 journal(status=%s)", status)
        return {}, False
    return {k: v for k, v in data.items() if isinstance(v, dict)}, True


def _trigger_journal_save(data: dict) -> bool:
    from cmuh_common.atomic_io import atomic_write_json  # noqa: PLC0415
    try:
        atomic_write_json(_trigger_journal_path(), data)
        return True
    except Exception:
        logging.error("[trigger] 寫入觸發 journal 失敗 → 不標已讀,留給下一輪",
                      exc_info=True)
        return False


def _trigger_journal_add(uid: str, sender: str) -> bool:
    """登記一筆待處理的觸發。回傳是否【確定落地】。

    讀不到既有內容時一律失敗 —— 寫回去會把別的待辦蓋掉,而那些待辦對應的信
    已經標成已讀、再也掃不到了。回 False 讓這封新的信留在 UNSEEN,下一輪重來。
    """
    with _trigger_journal_lock:
        data, ok = _trigger_journal_load()
        if not ok:
            logging.error("[trigger] 讀不到 journal → 不寫入(避免蓋掉既有待辦),"
                          "這封信留在未讀,下一輪再處理")
            return False
        data[str(uid)] = {"sender": str(sender or ""), "at": time.time()}
        return _trigger_journal_save(data)


def _trigger_journal_done(uid: str) -> None:
    with _trigger_journal_lock:
        data, ok = _trigger_journal_load()
        if not ok:
            # 讀不到就不要寫。這一筆留著會在下次開機被補跑一次(重複寄一封),
            # 比把別人的待辦蓋掉好。
            logging.warning("[trigger] 讀不到 journal → 不結案 %s(可能重複補跑)",
                            uid)
            return
        if data.pop(str(uid), None) is not None:
            _trigger_journal_save(data)


def _trigger_journal_pending() -> tuple:
    """→ (待辦, 讀得到嗎)。

    ★[2026-08-10 批次SF #7] 「讀不到」必須傳下去★
    上一版把 `_ok` 丟掉、只回 `data` —— 於是 journal 損毀/被鎖住時,開機補跑
    看到的是一個空字典,結論是「沒有待辦」。而那些 uid 對應的觸發信【已經標成
    \\Seen】,IMAP 再也掃不到它們:醫師的會診請求就這樣永久消失,而且開機
    log 上完全正常。
    `_trigger_journal_load` 早就把這件事分辨出來了(它自己的 docstring 就在講
    這個病灶),只是這一層又把答案壓回成「空的」。
    """
    with _trigger_journal_lock:
        return _trigger_journal_load()


_TRIGGER_JOURNAL_MAX_AGE_SEC = 6 * 3600.0


def _alert_trigger_journal_unreadable() -> None:
    """journal 讀不到 → 開發者告警（不會自己好的事情要讓人知道）。

    ★沒有節流★:這只在【開機補跑】那一次呼叫,一次啟動最多一封。
    """
    body = ("開機補跑時讀不到會診觸發 journal"
            f"（settings/{_TRIGGER_JOURNAL_NAME}）。\n\n"
            "這代表:如果先前有醫師寄了觸發信、工作已登記但還沒做完,那幾筆\n"
            "請求現在無法被補跑 —— 而對應的觸發信【已經標成已讀】,IMAP 不會\n"
            "再掃到它們。醫師會乾等一個不會來的結果。\n\n"
            "請確認該檔是否損毀、被防毒鎖住或權限有變;並請需要結果的醫師\n"
            "重寄一次觸發信。")

    def _worker():
        try:
            from cmuh_common.smtp_mail import send_mail  # noqa: PLC0415
            send_mail(recipients=[str(x) for x in _developer_alert_recipients()],
                      subject="會診自動化:觸發 journal 讀不到(可能遺失請求)",
                      body=body, attachment_path=None, category="system")
        except Exception:
            logging.warning("[trigger] journal 讀不到的告警寄送失敗",
                            exc_info=True)

    try:
        threading.Thread(target=_worker, name="ConsultJournalAlert",
                         daemon=True).start()
    except Exception:
        logging.debug("[trigger] journal 告警執行緒啟動失敗", exc_info=True)


def resume_pending_triggers() -> int:
    """開機補跑 journal 裡還沒完成的觸發。回傳補跑幾筆。

    ★太舊的不補跑★ 會診清單是「現在」的狀態,補寄一份六小時前的請求
    只會讓醫師拿到與當下不符的資料(這與 IMAP 的陳舊觸發信過濾同一個道理)。
    太舊的就結案並留 error log。
    """
    n = 0
    now = time.time()
    pending, readable = _trigger_journal_pending()
    if not readable:
        # ★不可以當成「沒有待辦」★(批次SF #7)那些觸發信已經是 \Seen,
        #   IMAP 再也掃不到 —— 讀不到就是「可能有請求正在消失」,要說出來。
        logging.critical(
            "★開機補跑讀不到觸發 journal★ 可能有醫師的會診請求正在遺失"
            "(對應的觸發信已標成已讀,IMAP 不會再掃到)。請檢查 settings 目錄下的"
            " %s 是否損毀或被鎖住;修好之前,那幾筆請求需要請醫師重寄一次觸發信",
            _TRIGGER_JOURNAL_NAME)
        _alert_trigger_journal_unreadable()
        return 0
    for uid, rec in list(pending.items()):
        sender = str((rec or {}).get("sender") or "")
        age = now - float((rec or {}).get("at") or 0)
        if age > _TRIGGER_JOURNAL_MAX_AGE_SEC:
            logging.error("[trigger] 待補觸發已過時(%.1f 小時)→ 不補跑:%s",
                          age / 3600.0, sender or "(無寄件人)")
            _trigger_journal_done(uid)
            continue
        if not sender:
            _trigger_journal_done(uid)
            continue
        logging.warning("[trigger] 補跑上次沒做完的 email 觸發:%s", sender)
        trigger_job_async("email", override_recipients=[sender],
                          trigger_uids=(uid,))
        n += 1
    return n


def trigger_job_async(trigger_label: str, override_recipients=None, *,
                      from_retrigger: bool = False,
                      trigger_uids=()) -> None:
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
        #   ★uid 要一起帶走★(第 2 回 F3)否則補跑成功也不會結案,
        #   下次開機又補跑一次 = 醫師收到第二封。
        _enqueue_pending_retrigger(trigger_label, override_recipients,
                                   trigger_uids)
        return

    _requeued: list = []

    def _worker():
        # worker_lease_scope：把 lease 綁在本緒上，讓 _do_full_job 深處（寄信前）也
        # 查得到自己有沒有被逾時接管（見 cmuh_common/task_gate.py）。
        with worker_lease_scope(lease):
            _completed = False
            try:
                _do_full_job(trigger_label,
                             override_recipients=override_recipients,
                             from_retrigger=from_retrigger,
                             trigger_uids=tuple(trigger_uids),
                             requeued_out=_requeued)
                _completed = True
            finally:
                _consult_job_gate.release(key, lease)
                # ★做完了才把 journal 那幾筆結案★(外審第 11 輪 F3)
                #   ★[第 2 回] 但「被排進補跑佇列」不算做完★
                #   `_do_full_job` 拿不到 `_flow_lock` 時會把這筆 email 觸發
                #   排進 pending queue 然後 return。那時工作【還沒做】,
                #   結案的話:補跑前當機 → 信早就標成已讀、journal 也空了 →
                #   永久漏信。所以只結案「沒有被重新排隊」的那幾個。
                # ★[第 3 回] 例外時也不可以結案★
                #   `_do_full_job` 非預期拋錯(例如 CoInitialize 失敗)時,
                #   信【已經標成已讀】而工作沒做完 —— 結案的話重啟也補不回來,
                #   醫師既收不到結果、也收不到失敗通知。
                #   `finally` 會在成功與例外兩種情況都跑,所以要自己記住走到哪。
                _still_queued = set(_requeued)
                for _u in (trigger_uids if _completed else ()):
                    if _u in _still_queued:
                        continue
                    try:
                        _trigger_journal_done(_u)
                    except Exception:
                        logging.debug("[trigger] journal 結案失敗", exc_info=True)
                # [v17] release 後檢查有沒有 pending re-trigger 需要補跑
                _drain_pending_retriggers()

    threading.Thread(target=_worker, name="ConsultJob", daemon=True).start()


# =============================================================================
# 排程器
# =============================================================================
# [codex P1 R17] 目前排程節奏:"keepalive"=3 分鐘常駐、"legacy"=≥15 分鐘冷啟動。
# run_consult_flow 執行期發現隱藏桌面失效(USER 資源耗盡等)時據此判斷要不要降速。
_sched_mode = "legacy"


def _demote_schedule_to_legacy() -> None:
    """[codex P1 R17] 隱藏桌面於【執行期】失效 → 排程立刻降回冷啟動節奏。

    排程建立時 probe 成功、之後才失效的話,job 仍每 162-198 秒觸發,而 SW_HIDE
    後備是【每輪完整登入】——等於每 3 分鐘送一次帳密,鎖定防護全破。"""
    if _sched_mode != "keepalive":
        return
    logging.warning("[排程] 隱藏桌面於執行期失效 → 排程降回冷啟動節奏(≥15 分鐘)")
    _rebuild_schedule()


def _rebuild_schedule() -> None:
    global _sched_mode
    _sched_mode = "legacy"
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
        interval = int(cfg.get("poll_interval_minutes", 3))
    except (TypeError, ValueError):
        interval = 3
    # [2026-08-03 常駐登入] 3 分鐘 ±10%(使用者指定):常駐後每輪只是「按會診查詢→
    # 擷取→按回」,不再每輪冷啟動登入;3 分鐘節奏本身就是 keepalive(院方 5 分鐘
    # 閒置會強制登出)。
    hdesk_probe = _ensure_hidden_desktop()
    _note_hidden_desktop_result(bool(hdesk_probe))   # [批次SH] 這也是一次觀測
    if hdesk_probe:
        try:
            _user32.CloseDesktop(hdesk_probe)
        except Exception:
            logging.debug("CloseDesktop(probe) 失敗", exc_info=True)
        # [codex P1 R8/R18] 常駐節奏上限:院方 5 分鐘閒置登出,間隔從任務結束
        # 起算,還要扣掉寄信尾段(SMTP ≤60s、Outlook 最壞 120s) → SMTP 夾 3 分、
        # Outlook 夾 2 分。設定值更大(10/30 分)會讓 session 每輪過期=每輪冷啟動。
        cap = (_keepalive.POLL_KEEPALIVE_CAP_OUTLOOK_MINUTES
               if str(cfg.get("mail_method", "smtp")).lower() == "outlook"
               else _keepalive.POLL_KEEPALIVE_CAP_MINUTES)
        eff = min(interval, cap) if interval > 0 else cap
        if eff != interval:
            logging.info("[排程] 常駐模式節奏由設定的 %d 分鐘夾為 %d 分鐘"
                         "(keepalive 須低於院方 5 分鐘閒置登出)", interval, eff)
        lo_s, hi_s = _keepalive.poll_seconds_range(eff)
        _sched_mode = "keepalive"
        schedule.every(lo_s).to(hi_s).seconds.do(trigger_job_async,
                                                 trigger_label="poll")
        logging.info(
            "已排程每 %d 分鐘(±10%% 隨機)輪詢會診清單(常駐登入,查完退回主畫面;"
            "有新會診才寄信;%02d:00-%02d:00 休息)",
            max(_keepalive.POLL_MIN_MINUTES, min(_keepalive.POLL_MAX_MINUTES,
                                                 interval)),
            int(cfg.get("quiet_start_hour", 0)), int(cfg.get("quiet_end_hour", 6)))
    else:
        # ★SW_HIDE 後備=每輪【完整登入】★ 絕不可套 3 分鐘節奏——那是每小時把
        # 同一組帳密送出 20 次,正是登入鎖定門檻的來源。維持舊 15 分鐘冷啟動節奏。
        legacy = max(15, interval)
        schedule.every(max(5, legacy - 1)).to(legacy + 1).minutes.do(
            trigger_job_async, trigger_label="poll")
        logging.warning(
            "無法建立隱藏桌面 → 常駐登入停用,維持每 %d 分鐘冷啟動輪詢"
            "(SW_HIDE 後備;%02d:00-%02d:00 休息)", legacy,
            int(cfg.get("quiet_start_hour", 0)), int(cfg.get("quiet_end_hour", 6)))


# ══ 遠端指令（2026-08-11 使用者定案；短語與去重於同日改版）══════════════
#
# 主旨【開頭】是下面其中一句就算數（不用括號、不用填機器）：
#     皮膚科會診重開   → 重開那台電腦
#     皮膚科會診重啟   → 只重啟會診查詢程式
#     皮膚科打卡重啟   → 只重啟打卡程式
# 想指定單一台時，在後面加一個空白 + 電腦名稱（告警信「發生在：」那個）。
#
# ★不填機器＝所有【正在跑會診查詢】的電腦★（使用者定案 2026-08-11）
#   這不是「廣播到全部電腦」——會去收這個信箱的只有會診查詢那支程式，
#   所以「收得到指令」與「正在執行會診程式」是同一件事。沒在跑的電腦
#   根本看不到那封信，自然不會動作。
#
# ★去重不可以用已讀旗標★（這是機器可省略之後的必然結果）
#   `\Seen` 是【信箱全域】的狀態：第一台標掉之後，其他台再也搜不到那封
#   UNSEEN —— 於是只有一台會動作。改用【本機收據】：每台自己記下
#   「這封信我做過了」，信件本身留到過期才標掉。
#   收據以 (UIDVALIDITY, uid) 為鍵並【落地】—— ★重開機之後那封信通常
#   還沒過期，沒有落地的收據就會再重開一次，變成重開機迴圈。★
#
# ★指令一律強制驗證，不看 `require_authenticated_trigger`★
#   那個設定是給查詢觸發的（誤觸發的代價是多寄一封信）。指令的代價是重啟
#   臨床自動化、甚至重開一台診間電腦 —— 沒有任何情況值得為它開後門。
#   From 是可偽造的純文字，所以必須通過 SPF/DKIM/DMARC 才算數。
#
# ★先寫收據、再執行★（與查詢觸發的順序相反）
#   查詢觸發是「先落地 journal 再標已讀」，因為漏掉一次查詢＝醫師乾等。
#   指令反過來：收據寫不下去就別執行。失敗模式不對稱 ——
#     * 指令遺失：使用者沒收到回信，重寄一次就好（可恢復）；
#     * 指令重複：每一輪都重啟一次 → ★無限重啟/重開機迴圈★，
#       而那正是「程式一直沒好」時最可能發生的狀況。
#: 主旨開頭的固定短語 → 內部代號。★完全比對開頭，絕不模糊比對★
#   （「重開」與「重啟」差一個字，代價差很多）。
_REMOTE_CMD_PHRASES = {
    "皮膚科會診重開": "reboot",
    "皮膚科會診重啟": "restart_consult",
    "皮膚科打卡重啟": "restart_autoclock",
}
_REMOTE_CMD_MAX_AGE_SEC = 30 * 60.0      # 半小時前的指令不再執行
_REMOTE_REPLY_WAIT_SEC = 30.0            # 要重啟時,等回信寄完的上限
#: 收據保留多久（必須遠大於指令時效，否則過期前收據就先被剪掉→重做）。
_REMOTE_RECEIPT_TTL_SEC = 24 * 3600.0
_REMOTE_RECEIPT_FILE = "consult_remote_receipts.json"
_remote_receipt_lock = threading.Lock()


def _this_machine_name() -> str:
    try:
        return (socket.gethostname() or "").strip()
    except Exception:
        return ""


def parse_remote_command(subject: str) -> tuple:
    """主旨 → (動作代號, 目標機器)；不是合法指令回 (None, "")。純函式。

    目標為空字串＝沒有指定機器＝每一台【正在跑會診查詢】的電腦都做。

    ★寬鬆的地方只有空白★：郵件客戶端會插入 `Re:`、多餘空白、全形空格。
    短語與機器名稱本身一律【完全比對】。
    ★短語後面必須是空白或結束★：`皮膚科會診重開機` 不是「皮膚科會診重開」
    加一個叫「機」的電腦 —— 那種主旨不執行任何東西。
    """
    from cmuh_common.imap_reader import normalize_subject  # noqa: PLC0415
    # ★與掃描端共用同一個正規化★(外審 SJ 第 1 輪 P2-4):兩邊各寫一套
    #   的話會有一邊靜默失效 —— `Re: 皮膚科會診重開` 曾經在掃描端就被
    #   濾掉,根本到不了這裡。
    text = normalize_subject(subject)
    for phrase, action in _REMOTE_CMD_PHRASES.items():
        if not text.startswith(phrase):
            # ★必須在【開頭】★:`find` 會讓「請不要寄 皮膚科會診重開」
            #   也被當成指令。
            continue
        rest = text[len(phrase):]
        if rest and not rest[:1].isspace():
            continue        # 短語後面直接接別的字 → 不是這一句
        parts = rest.split()
        if len(parts) > 1:
            # 多出來的字代表這封信不是我們約定的格式 ——
            # 「皮膚科會診重開 PC-1 順便清一下」不可以被當成指令執行。
            return None, ""
        return action, (parts[0] if parts else "")
    return None, ""


def _remote_command_is_for_me(target: str) -> bool:
    """這封指令要不要由本機執行。

    ★沒有指定機器 → 要★（見檔頭：會去收信的就是正在跑會診查詢的電腦）。
    有指定就必須完全相符；取不到自己的電腦名稱時不可以「猜」自己是目標。
    """
    if not target:
        return True
    me = _this_machine_name()
    return bool(me) and target == me


def _remote_receipt_path() -> str:
    from cmuh_common.paths import get_settings_dir  # noqa: PLC0415
    return os.path.join(get_settings_dir(), _REMOTE_RECEIPT_FILE)


def _remote_command_was_done(key: str) -> bool:
    """這封指令【本機】確實執行完了嗎（純查詢，不寫入）。

    給「過期時要不要回信」用：執行過的信刻意不標已讀（要留給其他也在跑
    會診查詢的電腦看），所以它 30 分鐘後會再被掃到一次。
    讀不到就回 False —— 這裡 fail-open 的方向是「多回一封信」，無害。

    ★[外審 SJ 第 2 輪 P1] 收據存在 ≠ 執行過★
    收據是【執行之前】就落地的（claim-before-execute，不然重開機之後會
    再重開一次）。claim 到 `_run_remote_command` 之間掛掉的話，收據還在，
    卻什麼都沒做 —— 只看「鍵在不在」會把它講成執行過，連帶把過期通知也
    吃掉，使用者兩頭落空。所以拆兩段：claim 擋重複執行，done 才代表做完。
    """
    from cmuh_common.atomic_io import safe_load_json_ex  # noqa: PLC0415
    with _remote_receipt_lock:
        data, status = safe_load_json_ex(_remote_receipt_path(), {},
                                         backup_on_corrupt=False)
    if status not in ("ok", "missing") or not isinstance(data, dict):
        return False
    rec = data.get(key)
    return isinstance(rec, dict) and bool(rec.get("done"))


def _mark_remote_command_done(key: str) -> None:
    """執行完成才蓋這一章（見 `_remote_command_was_done`）。

    盡力而為：寫不下去只會多一封誠實的過期通知，不影響「不重複執行」——
    那一條靠的是 claim 那一筆，早就落地了。
    """
    from cmuh_common.atomic_io import (  # noqa: PLC0415
        atomic_write_json, safe_load_json_ex,
    )
    path = _remote_receipt_path()
    with _remote_receipt_lock:
        data, status = safe_load_json_ex(path, {}, backup_on_corrupt=False)
        if status not in ("ok", "missing") or not isinstance(data, dict):
            return
        rec = data.get(key)
        if not isinstance(rec, dict):
            return
        rec["done"] = True
        try:
            atomic_write_json(path, data)
        except Exception:
            logging.warning("[遠端] 執行完成的收據寫不下去"
                            "（只會多一封過期通知）", exc_info=True)


def _claim_remote_command(key: str, now=None) -> bool:
    """★做之前先把收據落地★ → 這一封是不是「我還沒做過、而且已經記下來了」。

    回 False 有兩種情況，兩種都不可以執行：
      * 收據裡已經有它 → 我做過了（重開機之後那封信通常還沒過期，
        靠的就是這一條，★不然會變成重開機迴圈★）；
      * 寫不下去 → 不知道下次還認不認得它 → 寧可不做。
    """
    from cmuh_common.atomic_io import (  # noqa: PLC0415
        atomic_write_json, safe_load_json_ex,
    )
    now = now or time.time()
    path = _remote_receipt_path()
    with _remote_receipt_lock:
        # ★[外審 SJ 第 1 輪 P1-2] 要用會【回報狀態】的那一個★
        #   `safe_load_json` 把讀取錯誤吞掉並回 default —— 於是
        #   「讀不到就不執行」那句話從來沒有生效過(那個 except 永遠不會
        #   進去),損毀的收據會被當成空的,然後照樣執行。
        #   而我的測試把它 monkeypatch 成【會拋例外】—— 那是生產函式
        #   不會有的行為,等於在測一個不存在的情境。
        data, status = safe_load_json_ex(path, {}, backup_on_corrupt=False)
        if status not in ("ok", "missing") or not isinstance(data, dict):
            logging.warning("[遠端] 讀不到/讀壞執行收據(status=%s) → "
                            "本封不執行(不知道做過沒有)", status)
            return False
        if key in data:
            return False
        kept = {k: v for k, v in data.items()
                if _remote_receipt_is_fresh(v, now)}
        kept[key] = {"at": now, "done": False}   # done 由執行完成才蓋
        try:
            atomic_write_json(path, kept)
        except Exception:
            logging.error("[遠端] 執行收據寫不下去 → 本封不執行"
                          "(執行了的話重開之後會再做一次)", exc_info=True)
            return False
    return True


def _remote_receipt_is_fresh(rec, now: float) -> bool:
    """收據還在保留期內嗎。壞掉的時間戳一律【丟掉】(不是留著)。

    留著壞資料的話它會永遠壓住那個 uid;而 uid 會隨 UIDVALIDITY 重用。
    """
    ts = rec.get("at") if isinstance(rec, dict) else rec
    if ts is None:
        return False
    try:
        at = float(ts)
    except (TypeError, ValueError):
        return False
    return 0 < at <= now and (now - at) <= _REMOTE_RECEIPT_TTL_SEC




_FLAG_CLAIM_WARN_INTERVAL_SEC = 300.0
_flag_claim_warned_at: dict = {}


def _claim_flag_file(path) -> bool:
    """把旗標檔【拿走】→ 這一次要求是不是我收到的。

    ★[2026-08-10 批次SF #1]★ 舊寫法是
        `if FLAG.exists(): try: FLAG.unlink() except OSError: pass` 然後照樣執行。
    防毒鎖檔、唯讀屬性、ACL 變動時 `unlink()` 會【持續】失敗,而旗標還在 ——
    於是排程迴圈的每一次 tick(0.5~5 秒)都把它當成一次全新的要求:

      * `RUNNOW` → 每一輪都排一次完整 HIS 查詢 + 寄一封信。被 gate 擋下的
        還會排進補跑佇列,做完再跑一次 —— 持續的 HIS 負載與寄信,
        直到郵件配額耗盡。手動觸發【不看有沒有新會診】,所以每一封都寄得出去。
      * `RELOAD` → 每一輪都 `schedule.clear()` + 重建。而重建會把輪詢 job 的
        下次執行時間一併重設 —— 重建週期(≤5 秒)遠短於輪詢週期(162~198 秒),
        ★輪詢 job 於是永遠不會到期、永遠不會執行★,而 log 上只有一行
        「偵測到設定變更」在重複,看不出會診查詢已經停了。

    「觀察到」不等於「取得」。取不走就不可以消費它 —— 而且要說出來
    (節流,否則這行本身會把 log 洗掉)。
    """
    try:
        os.unlink(str(path))
        return True
    except FileNotFoundError:
        return False          # 別人(或上一次 tick)已經拿走了 → 不是我的
    except OSError:
        key = str(path)
        now = time.monotonic()
        last = _flag_claim_warned_at.get(key, 0.0)
        if now - last >= _FLAG_CLAIM_WARN_INTERVAL_SEC:
            _flag_claim_warned_at[key] = now
            logging.error(
                "★旗標檔刪不掉,本次要求【不執行】★:%s —— 刪不掉卻照做的話,"
                "每一次排程 tick 都會重做一次(立即執行=不斷寄信;設定變更="
                "排程被反覆重建,輪詢永遠不會觸發)。請確認防毒/唯讀屬性/權限",
                key, exc_info=True)
        return False


#: 打卡程式的「請重啟」命令檔（會診查詢是信差，打卡自己看到就重啟自己）。
AUTOCLOCK_RESTART_REQUEST = "autoclock_restart_request.json"


def _write_autoclock_restart_request(why: str) -> bool:
    """請打卡程式重啟自己 → 有沒有寫成功。

    ★不是直接去 kill 它★：那會在它正在按打卡按鈕的當下把它砍掉。
    打卡自己在迴圈裡看到這個檔就【選一個安全的時點】重啟。
    """
    try:
        from cmuh_common.atomic_io import atomic_write_json  # noqa: PLC0415
        from cmuh_common.paths import get_settings_dir       # noqa: PLC0415
        atomic_write_json(
            os.path.join(get_settings_dir(), AUTOCLOCK_RESTART_REQUEST),
            {"at": time.time(), "why": str(why)[:200],
             "by": _this_machine_name()})
        return True
    except Exception:
        logging.error("[遠端] 寫入打卡重啟請求失敗", exc_info=True)
        return False


def _reply_remote_command(sender: str, subject_line: str, body: str,
                          wait_sec: float = 0.0) -> None:
    """把指令結果回給【寄指令的那個人】（不寄給別人）。

    ★[外審 SI 第 1 輪 P2-4] `wait_sec` 是給「馬上要重啟」的動作用的★
    回信是背景 daemon 緒,而 `restart_consult` 隨即讓舊行程退出 ——
    行程一死,那條緒就跟著沒了。指令做了、使用者卻沒收到回覆,
    於是他重寄一次 → 多一次重啟。等它寄完再走(有上限,不會卡死)。
    """

    def _worker():
        try:
            from cmuh_common.smtp_mail import send_mail  # noqa: PLC0415
            send_mail(recipients=[str(sender)], subject=subject_line,
                      body=body, attachment_path=None, category="system")
        except Exception:
            logging.warning("[遠端] 指令回覆寄送失敗(指令本身已執行)",
                            exc_info=True)

    try:
        t = threading.Thread(target=_worker, name="ConsultRemoteReply",
                             daemon=True)
        t.start()
    except Exception:
        logging.debug("[遠端] 回覆執行緒啟動失敗", exc_info=True)
        return
    if wait_sec > 0:
        t.join(wait_sec)
        if t.is_alive():
            logging.warning("[遠端] 回覆在 %.0f 秒內沒寄完 → 仍照常執行指令"
                            "(使用者可能收不到回覆)", wait_sec)


def _run_remote_command(action: str, sender: str) -> None:
    """執行一個【已經授權、已經標成已讀】的遠端指令。"""
    me = _this_machine_name() or "(不明)"
    if action == "restart_consult":
        logging.warning("[遠端] %s 要求重啟會診查詢 → 執行", sender)
        _reply_remote_command(
            sender, f"會診自動化:已重啟會診查詢({me})",
            f"收到你的遠端指令,{me} 上的會診查詢程式正在重啟。\n"
            "重啟後它會自動抓最新版(常駐中的實例只在啟動時檢查更新)。\n"
            "★注意★:重啟會把記憶體裡的登入冷卻清掉,所以它會很快再試一次登入;"
            "若你懷疑帳號已被鎖定,請先人工確認帳密。",
            # ★等它寄完★ 下一行就讓這個行程退出了(外審 SI 第 1 輪 P2-4)。
            wait_sec=_REMOTE_REPLY_WAIT_SEC)
        # 走與自動更新完全相同的乾淨重啟路徑(收托盤 → main thread 重啟),
        # 不可以在這條背景緒直接 restart —— 那會留下舊行程 + 兩個托盤圖示。
        _request_restart_for_update()
        return
    if action == "restart_autoclock":
        ok = _write_autoclock_restart_request(f"遠端指令({sender})")
        logging.warning("[遠端] %s 要求重啟打卡 → 已寫入請求=%s", sender, ok)
        _reply_remote_command(
            sender, f"會診自動化:已{'轉達' if ok else '★無法轉達★'}重啟打卡({me})",
            (f"{me} 上的打卡程式已收到重啟請求,它會在下一個安全時點重啟"
             "(不會在正在打卡的當下被砍掉)。\n"
             if ok else
             f"★寫入重啟請求失敗★({me}) —— 打卡程式不會重啟,請看 log。\n"))
        return
    if action == "reboot":
        logging.warning("[遠端] %s 要求重開機 → 排入閒置重開機看守", sender)
        _reply_remote_command(
            sender, f"會診自動化:已排定重開機({me})",
            f"{me} 已排入自動重開機看守。\n\n"
            "★不會立刻重開★:必須使用者連續閒置滿 "
            f"{_keepalive.BDE_REBOOT_MIN_IDLE_SECONDS // 60} 分鐘、"
            "且 24 小時內沒有自動重開過,才會真的下 shutdown /r。\n"
            "有人在用那台電腦的話它會一直等;倒數期間有人回來也會取消。\n"
            "要取消這個排定,把那台的會診查詢程式重啟一次即可。")
        _schedule_reboot_watch("REMOTE", f"遠端指令要求重開機({sender})")
        return
    logging.error("[遠端] 不認得的動作代號:%r(不執行)", action)


# [外審 SI 第 4 輪] 指令掃描【自己】的放生引用。
#   ★不可以跟觸發檢查共用 worker★:指令掃描卡在 DNS/connect/TLS(還沒有
#   socket,`force_close_active()` 救不了)時,共用的 single-flight 會讓之後
#   每一輪都回「上一條還在跑」而【完全不做 check_trigger】——
#   一個附屬功能就這樣把臨床的信件觸發永久關掉。各自一條、互不擋。
_last_imap_cmd_thread = None


def _run_imap_commands_with_timeout(timeout: float = 30.0) -> dict:
    """在自己的 daemon thread 掃遠端指令信；逾時就砍 socket 並回空結果。

    與 `_run_imap_check_with_timeout` 同一套保護（逾時 force_close、再等 2 秒、
    仍活著就 clear、保留引用讓下一輪不疊加），但★狀態完全獨立★。

    註：`force_close_active()` 會關掉當下所有活動連線。這兩個掃描在
    `scheduler_loop` 裡是【前後呼叫】的，同一時間只有一個在真的做 IMAP；
    另一條若還在，那就是已經放生、本來就該被關掉的那一條。
    """
    from cmuh_common.imap_reader import check_commands, force_close_active
    global _last_imap_cmd_thread

    prev = _last_imap_cmd_thread
    if prev is not None and prev.is_alive():
        logging.warning("[遠端] 上一條指令掃描仍未結束,本輪跳過(不疊加)")
        return {"items": [], "error": "previous command scan still running"}

    box: dict = {}

    def _worker():
        try:
            box["r"] = check_commands(list(_REMOTE_CMD_PHRASES),
                                      timeout=12.0,
                                      max_age_sec=_REMOTE_CMD_MAX_AGE_SEC)
        except Exception as e:  # noqa: BLE001
            box["r"] = {"items": [], "error": f"command scan exception: {e!r}"}

    t = threading.Thread(target=_worker, name="IMAPCommands", daemon=True)
    _last_imap_cmd_thread = t
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        logging.warning("[遠端] 指令掃描超過 %.0fs 無回應,強制砍 socket", timeout)
        force_close_active()
        t.join(timeout=2.0)
        if t.is_alive():
            logging.warning("[遠端] 指令掃描緒仍未結束,已放棄;保留引用,"
                            "下一輪不疊加新 thread")
            force_close_active(clear=True)
        return {"items": [], "error": f"command scan timeout > {timeout:.0f}s"}
    _last_imap_cmd_thread = None
    return box.get("r") or {"items": [], "error": "command result missing"}


#: 標已讀的放生引用（與掃描各自一條，互不擋）。
_last_imap_ack_thread = None


def _ack_command_mail(uids, why: str, timeout: float = 30.0) -> bool:
    """★終局處置要標已讀★（外審 SI 第 1 輪 P2-5）→ 有沒有標成功。

    不標的話那封信永遠停在 UNSEEN —— 每一輪（20 秒）都要為它 FETCH header
    + FETCH INTERNALDATE 並寫一行 warning，而且每一台共用信箱的機器各做
    一份。那是一個★不需要通過驗證★就能發動的資源與 log DoS。
    （`check_trigger` 那邊 2026-08-08 外審 F4 記過同一件事。）

    ★收【清單】而不是單一 uid★（外審 SI-2 第 1 輪 P2）：一封一次連線的話，
    一次掃描最多 50 封 → 50 條序列 TLS 連線跑在 scheduler 緒上。
    終局處置那一批可以合併成一次 STORE；要執行的那幾封才逐封標
    （mark-before-execute 不能批次：一封失敗會讓另一封被誤判成已結案）。

    ★整段有界★：`mark_uids_seen` 自己的 socket timeout 蓋不到 DNS/connect，
    而這裡是 scheduler 緒（程式的心跳）。逾時就 force-close 並放生，
    放生的那一條只擋下一次標記，不擋掃描、也不擋臨床觸發。
    """
    from cmuh_common.imap_reader import (  # noqa: PLC0415
        force_close_active, mark_uids_seen,
    )
    global _last_imap_ack_thread

    ids = [str(u) for u in (uids or []) if str(u)]
    if not ids:
        return True
    prev = _last_imap_ack_thread
    if prev is not None and prev.is_alive():
        logging.warning("[遠端] 上一次標已讀仍未結束 → 本次不執行(不疊加)")
        return False
    box: dict = {}

    def _worker():
        try:
            box["ok"] = bool(mark_uids_seen(ids))
        except Exception:
            box["ok"] = False
            logging.warning("[遠端] 標記指令信已讀時例外", exc_info=True)

    t = threading.Thread(target=_worker, name="IMAPCommandAck", daemon=True)
    _last_imap_ack_thread = t
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        logging.warning("[遠端] 標已讀超過 %.0fs 無回應,強制砍 socket", timeout)
        force_close_active()
        t.join(timeout=2.0)
        if t.is_alive():
            force_close_active(clear=True)
        return False
    _last_imap_ack_thread = None
    ok = bool(box.get("ok"))
    if not ok:
        logging.warning("[遠端] %s 的指令信標不成已讀(下一輪會再看到)", why)
    return ok


def _poll_remote_commands(cfg: dict, scan: dict) -> None:
    """處理【已經抓好的】遠端指令信（授權不通過就只是忽略＋告警）。

    ★`scan` 由 `_run_imap_commands_with_timeout` 帶進來★ —— 本函式不自己
    開 IMAP 掃描:那個 worker 才有「逾時就 force_close socket + single-flight」
    的完整保護。

    ★流程是「先分類、再結案」★(外審 SI-2 第 1 輪 P2)
    上一版每一封都各自呼叫一次 `mark_uids_seen`,而那是【一條新的 TLS
    連線 + login + select + store】。一次掃描最多 50 封 → 50 條序列連線,
    全部跑在 scheduler 緒上;而連線還沒建立時 `force_close_active()` 也
    救不了它 —— 心跳停掉,watchdog 反而會把臨床程式重啟。
    改成:終局處置的 uid 收集起來【一次】標掉;要執行的那幾封才各自
    先標再執行(mark-before-execute 必須逐封,不能批次)。
    """
    r = scan or {}
    if r.get("error"):
        logging.warning("[遠端] 掃描指令信失敗:%s", r["error"])
        return
    uv = str(r.get("uidvalidity") or "")
    # ★[外審 SJ 第 1 輪 P2-3] 取不到 UIDVALIDITY 就不執行★
    #   收據的鍵會變成 ":<uid>";下一輪拿到真的 UIDVALIDITY 之後鍵就
    #   不一樣了,同一封未讀信會【再執行一次】。信箱重建時,那個空鍵
    #   的舊收據也可能反過來壓住一封新指令。不標已讀,讓它自然過期。
    if not uv:
        logging.error("★取不到 UIDVALIDITY → 本輪不執行任何遠端指令★"
                      "(收據的鍵會不穩定,可能重複執行)")
    allow = {str(x).lower() for x in (cfg.get("allowed_trigger_senders") or [])}
    terminal: list = []       # 沒有人會再處理的 → 一次標掉
    actionable: list = []     # (uid, action, sender)
    expired_replies: list = []  # (sender, target)
    rejected: list = []
    for it in (r.get("items") or []):
        uid = str(it.get("uid") or "")
        sender = str(it.get("sender") or "")
        action, target = parse_remote_command(str(it.get("subject") or ""))
        if action is None:
            logging.warning(
                "[遠端] 指令信格式不對(主旨開頭要是 %s,後面最多再加一個"
                "電腦名稱)→ 不執行", "/".join(_REMOTE_CMD_PHRASES))
            terminal.append(uid)
            continue
        # ★授權要在【目標比對之前】★(外審 SI-2 第 1 輪 P1)
        #   授權與「這封是給誰的」無關,對每一台機器的答案都一樣 ——
        #   所以它是【全域確定】的終局處置,誰看到誰就可以結案。
        #   放在目標比對後面的話:一封未授權、又指向不存在主機的信
        #   會在每一台都走到「不是給這台的」而留著不動,整整半小時。
        #   掃描一次只看最新 50 封 —— 持續投遞就能把掃描視窗占滿,
        #   ★把合法指令餓死★,而且不需要通過任何驗證。
        if sender not in allow or not it.get("authenticated"):
            logging.error(
                "★遠端指令未通過授權 → 不執行★(白名單=%s、通過驗證=%s):%s",
                sender in allow, bool(it.get("authenticated")), sender)
            rejected.append(sender or "(解析不出寄件人)")
            terminal.append(uid)      # 已告警;不結案就能被拿來洗 log
            continue
        if it.get("expired"):
            logging.warning("[遠端] 指令信已過期(超過 %.0f 分鐘)→ 不執行",
                            _REMOTE_CMD_MAX_AGE_SEC / 60.0)
            terminal.append(uid)
            # ★[外審 SJ 第 1 輪 P1-1] 自己做過的不可以說「沒人做」★
            #   執行過的信【刻意不標已讀】(要留給別台看),所以 30 分鐘
            #   後它會再被掃到一次並走到這裡。無條件回信的話,使用者
            #   先收到成功信、再收到一封說沒人執行 —— 他會再寄一次,
            #   於是多一次重啟/重開機。
            if uv and _remote_command_was_done(f"{uv}:{uid}"):
                logging.info("[遠端] 這封指令本機已執行過,過期後不再回信")
                continue
            expired_replies.append((sender, target))
            continue
        if not _remote_command_is_for_me(target):
            # ★這一種【不可以】標已讀★:那台機器還沒收到。
            #   它不會永遠留著 —— 半小時後會走上面那條「已過期」。
            continue
        if not uv:
            continue        # 見上面:鍵不穩定就不執行(也不標已讀)
        actionable.append((uid, action, sender))
    if rejected:
        _alert_trigger_rejected(rejected)
    terminal_acked = True
    if terminal:
        # ★一次標掉★:終局處置那一批合併成一次 STORE(一封一條連線的話,
        #   一次掃描最多 50 封 → 50 條序列 TLS 連線跑在 scheduler 緒上)。
        terminal_acked = _ack_command_mail(terminal, "終局處置")
    # ★標不掉就不要回信★(外審 SI-2 第 2 輪 P1)
    #   標不掉代表那封信還是 UNSEEN —— 下一輪、以及【每一台】機器
    #   都會再回一封,直到把寄信配額耗光,而且會把真正的通知洗掉。
    # ★[外審 SJ 第 2 輪 P1] 一封信只能講【本機】知道的事★
    #   一台機器沒有「全體有沒有做」的資訊：不指定機器的信要留給每一台看，
    #   所以永遠 UNSEEN —— 晚開機、或 IMAP 斷了半小時的那一台，第一眼看到
    #   的就是「已過期」，它身上當然沒有收據。舊文案讓它代表全體宣告
    #   「沒有被任何機器執行」，而別台可能早就做完並寄了成功信 ——
    #   使用者於是再寄一次，多一次重開機。
    me = _this_machine_name() or "(這一台)"
    for sender, target in (expired_replies if terminal_acked else ()):
        _reply_remote_command(
            sender, "會診自動化:遠端指令已過期作廢(本機沒有執行)",
            f"你寄的遠端指令超過 {_REMOTE_CMD_MAX_AGE_SEC / 60.0:.0f} "
            f"分鐘,已作廢。★{me} 沒有執行它。★\n"
            f"信裡指定的目標:{target or '(沒有指定,每一台跑會診查詢的都要做)'}\n\n"
            "★這封信只講得出這一台的狀況★ —— 收指令的每一台各自判斷,\n"
            "所以【如果你已經收到過「已執行」的通知信,那就是別台做掉了,\n"
            "請忽略這封,不要再寄一次】。\n\n"
            "都沒收到成功通知的話,常見原因:\n"
            "  * 有指定機器但名稱打錯 —— 請用告警信裡「發生在：」的那一個;\n"
            "  * 當下沒有任何一台在跑會診查詢程式"
            "(收指令的就是那一支)。\n")
    for uid, action, sender in actionable:
        # ★先把收據落地、再執行★(不是靠已讀旗標 —— 那是信箱全域的,
        #   第一台標掉別台就看不到了,見檔頭說明)。
        #   ★重開機之後那封信通常還沒過期,靠這張收據才不會再重開一次。★
        if not _claim_remote_command(f"{uv}:{uid}"):
            continue
        _run_remote_command(action, sender)
        # ★做完才蓋章★（外審 SJ 第 2 輪 P1）：claim 那一筆只代表「開始做了、
        #   不要再做一次」，不代表做完了。
        _mark_remote_command_done(f"{uv}:{uid}")


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
            # ★命中信延後標記★ 由 `_handoff_email_triggers` 在工作落地之後
            #   才標(陳舊命中信仍然照舊標掉清乾淨)。
            box["r"] = check_trigger(kw, max_age_sec=max_age_sec or None,
                                     defer_mark_matched=True)
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
# ★[2026-08-10 批次SB #6] 兩個時間戳都是 monotonic★(理由見 autoclock 同名處)
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


def _collect_final_uids(addrs, uid_map, only_unauth: bool = False) -> list:
    """這幾位寄件人的信,哪些 uid 屬於【本輪終局處置】(要標成已讀)。

    `uid_map`: 寄件人 → [(uid, 是否通過驗證), ...]
    `only_unauth=True`: 只取【沒通過驗證】的那幾封(用在「寄件人整體通過、
    但他還有別的偽造信」的情況)。純函式。
    """
    out: list = []
    for x in (addrs or []):
        for uid, ok in (uid_map.get(str(x).strip().lower()) or []):
            if only_unauth and ok:
                continue
            out.append(uid)
    return out


def _undo_trigger_dedup(sender: str) -> None:
    """撤銷剛剛那筆去重預約(工作沒能落地時)。

    ★[2026-08-08 外審]★ 去重是「這位五分鐘內已經處理過」的宣稱。工作根本
    沒有被接手的話,那個宣稱就是假的 —— 而它會讓下一輪把這封信當成
    「已處理過」而標成已讀(去重是終局處置)。信就這樣沒了。
    """
    with _trigger_dedup_lock:
        if _recent_trigger_senders.pop(str(sender or "").lower(), None) is not None:
            _persist_trigger_dedup_locked()


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
                category="system",      # [P2-02] 重複觸發提醒不可吃掰臨床告警額度
            )
            logging.info("[dedup] 已回告知信(重複觸發已略過)：%s",
                         ", ".join(to_notify))
        except Exception:
            logging.warning("[dedup] 告知信寄送失敗(不影響流程)", exc_info=True)

    threading.Thread(target=_worker, name="ConsultDedupNotice",
                     daemon=True).start()


# [2026-07-25 審查] 輪詢模式的「整個任務失敗」原本【誰都不會被通知】：email 觸發者有
# 回信、poll/手動沒有。而輪詢模式「沒收到信」本來就是常態(沒有新會診就不寄) → HIS 改版
# 之類的永久故障與「今天沒有新會診」在使用者眼中完全一樣;外層 watchdog 只看 log 檔
# mtime,而錯誤一直在寫 log → 它會回報健康。結果是團隊看著一片安靜、以為沒有會診。
# 故:連續失敗達門檻就寄一封節流告警,恢復後自動重置。
# 系統故障告警的收件人 = 開發者本人(單一宣告處在 cmuh_common.settings_defaults)
from cmuh_common.settings_defaults import (  # noqa: E402
    developer_alert_recipients as _developer_alert_recipients,
)

_JOB_FAIL_ALERT_THRESHOLD = 3          # 連續 3 次放棄(≈45 分鐘無法查詢)才告警,避免暫時性抖動
_JOB_FAIL_ALERT_COOLDOWN_SEC = 6 * 3600
_JOB_FAIL_STATE_SCHEMA = 1
# 時鐘往前跳(NTP 校時、使用者改時間)時,存下來的時間戳可能落在未來。
# 容忍這麼多;超過就當它壞掉 ——★不可以讓一個壞掉的時間戳把告警永久靜音★。
_JOB_FAIL_CLOCK_SKEW_SEC = 3600
_job_fail_streak = 0
_job_fail_last_alert = 0.0
_job_fail_lock = threading.Lock()

# ★[2026-08-03] 節流狀態必須落地★
#   原本 `_job_fail_streak` / `_job_fail_last_alert` 只活在記憶體裡,而這支程式
#   【本來就會被 watchdog 重啟】(watchdog_config.json 的「會診查詢」項:log 停更
#   180 秒就重啟)。每重啟一次,冷卻時間就歸零 → 再累積 3 次失敗就又寄一封。
#   信裡卻寫著「同一波故障最多 6 小時提醒一次」——★宣稱與實作不符★,而使用者
#   實際收到的是一整天的重複告警(2026-08-03 回報)。
#   落地之後那句話才是真的。跨機器仍然各寄各的(沒有共用狀態),所以信裡要寫出
#   是哪一台,收件人才分得出來是同一波還是多台一起壞。


def _job_fail_state_path() -> str:
    from cmuh_common.paths import get_settings_dir
    return os.path.join(get_settings_dir(), "consult_alert_state.json")


def _load_job_fail_state() -> None:
    """開機時把節流狀態讀回來。★讀不到不等於「剛剛才寄過」★

    讀失敗時保留記憶體中的預設值(streak=0、從沒寄過)——也就是【會寄】的那一邊。
    這是刻意的:告警存在的理由就是「故障時沒人會發現」,拿一個壞掉/讀不到的
    節流檔去把它靜音,等於用一個小問題製造一個大問題。噪音可以忍,靜音不行。
    """
    global _job_fail_streak, _job_fail_last_alert
    path = _job_fail_state_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return                      # 第一次跑,不是錯誤
    except (OSError, ValueError):
        logging.warning("[health] 告警節流狀態讀不到 → 這一輪照原樣判斷(可能重複寄)",
                        exc_info=True)
        return
    if not isinstance(data, dict) or data.get("schema") != _JOB_FAIL_STATE_SCHEMA:
        logging.warning("[health] 告警節流狀態的 schema 不認得 → 忽略")
        return
    streak = data.get("streak")
    last = data.get("last_alert_ts")
    if isinstance(streak, int) and streak >= 0:
        _job_fail_streak = streak
    if isinstance(last, (int, float)) and last >= 0:
        _job_fail_last_alert = float(last)
    # ★[2026-08-11 批次SH] 登入冷卻也要跨重啟★
    #   `_login_cooldown_until` 原本只在記憶體裡 —— 任何一次行程重啟
    #   (自動更新、使用者手動、卡死升級重啟、watchdog)都把 15 分鐘的
    #   防鎖定冷卻清成 0,下一輪立刻又送一次帳密。防護的用意正是
    #   「同一組帳密不要密集送出」,而重啟恰好是它最沒有防備的時刻。
    #   ★沒讀到就維持 0★:讀不到是「不知道有沒有冷卻」,而這裡 fail-open
    #   的代價只是早一點重試一次登入(既有的 `login_cooldown_remaining`
    #   也是這個方向),不是無限重試。
    cooldown = data.get("login_cooldown_until")
    # ★`>= 0` 而不是 `> 0`★:0 是一個【有意義的值】(登入成功後的解除),
    #   磁碟上的狀態要在兩個方向都算數,不然「已解除」這件事就落不了地。
    if isinstance(cooldown, (int, float)) and cooldown >= 0:
        _set_login_cooldown_until(float(cooldown), persist=False)
    _forget_future_alert_ts(time.time())


def _forget_future_alert_ts(now: float) -> bool:
    """最後告警時間落在未來就丟掉。→ 有沒有丟掉。

    ★載入與執行期必須用同一套判準★（2026-08-03 外審第 1 輪 P2）：
    原本只在載入時檢查。可是校時是【執行期間】發生的 —— 機器先被設到未來
    （於是存下一個未來的時間戳），之後 NTP 把時鐘校回來，`now - last` 就變成
    負數，永遠小於冷卻時間 → ★告警被靜音到時鐘追上為止★，可能是好幾個月。
    而這支程式在正常運作時 log 一直在更新，watchdog 不會重啟它，也就不會重新
    載入 —— 靠載入時檢查救不到。

    所以判準抽成這一個函式，載入與每次冷卻判斷都呼叫它。
    """
    global _job_fail_last_alert
    if _job_fail_last_alert <= now + _JOB_FAIL_CLOCK_SKEW_SEC:
        return False
    logging.warning("[health] 告警節流時間戳在未來(%.0f 秒後) → 當作沒寄過",
                    _job_fail_last_alert - now)
    _job_fail_last_alert = 0.0
    return True


def _save_job_fail_state() -> None:
    """把節流狀態原子寫下去。寫不成不致命(頂多退回舊行為:重啟後可能重複寄)。"""
    try:
        from cmuh_common.atomic_io import atomic_write_json
        atomic_write_json(_job_fail_state_path(),
                          {"schema": _JOB_FAIL_STATE_SCHEMA,
                           "streak": _job_fail_streak,
                           "last_alert_ts": _job_fail_last_alert,
                           # [批次SH] 登入冷卻:跨重啟仍然有效(見載入處說明)。
                           #   多一個鍵不必動 schema —— 舊版讀到會忽略它,
                           #   新版讀到舊檔則因為缺這個鍵而維持 0,兩邊都安全。
                           "login_cooldown_until": _login_cooldown_until})
    except Exception:
        logging.debug("[health] 告警節流狀態寫不下去(略過)", exc_info=True)


_BASELINE_ALERT_COOLDOWN_SEC = 6 * 3600
_baseline_alert_at = 0.0
_baseline_alert_lock = threading.Lock()


def _alert_baseline_lost(reason: str, count: int) -> None:
    """基準遺失/損毀 → 寄一封系統告警給開發者。失敗只記 log。

    ★這封不是臨床通知★ 臨床端已經在同一輪收到「整份清單請人工核對」那封了；
    這封是要讓維護的人知道那台機器的 settings 出過事(防毒隔離、磁碟、誤刪)。

    ★[2026-08-05 自查 P1-B] 要節流★ 這支函式被呼叫的位置在【重試迴圈裡面】:
      * 同一輪:寄信失敗會重試 3 次 → 最多 3 封
      * 跨輪:基準要等到「寄信成功」才會被重建,所以只要寄不出去,
        每 3 分鐘的下一輪又會再發現一次「基準不見了」
    一次磁碟事故可以變成整天每 3 分鐘一封。與 `_note_job_failure`、
    掛帳告警一致,採 6 小時冷卻。
    """
    global _baseline_alert_at
    with _baseline_alert_lock:
        now = time.time()
        # 時鐘往前跳(NTP/使用者改時間)會讓上次時間落在未來 → 直接重置,
        # 否則會被自己鎖死到那個未來時間為止(既有 _note_job_failure 的同款坑)。
        if _baseline_alert_at > now:
            _baseline_alert_at = 0.0
        if now - _baseline_alert_at < _BASELINE_ALERT_COOLDOWN_SEC:
            logging.info("[會診] 基準遺失告警在冷卻期內(%d 小時),本次不重複寄",
                         _BASELINE_ALERT_COOLDOWN_SEC // 3600)
            return
        _baseline_alert_at = now
    human = {
        "missing_after_prior_run": "基準檔不見了(建立過但檔案已不存在)",
        "corrupt": "基準檔內容損壞(已備份壞檔)",
        "read_error": "基準檔讀不到(權限/防毒暫時鎖住;原檔通常還在)",
    }.get(reason, reason)
    host = ""
    try:
        host = socket.gethostname()
    except OSError:
        pass

    def _worker():
        try:
            from cmuh_common.smtp_mail import send_mail
            send_mail(
                recipients=[str(r) for r in _developer_alert_recipients()],
                subject="⚠ 會診通知基準遺失/損毀" + (f"（{host}）" if host else ""),
                body=(f"{human}\n\n"
                      + (f"發生在：{host}\n" if host else "")
                      + f"本輪目前未回覆的會診共 {count} 筆，已【全部】寄給臨床收件人"
                      "並註明請人工核對（寧可多寄一封，不可無聲漏掉）。\n\n"
                      "下一輪起會以本輪清單為新基準，恢復正常比對。\n"
                      "若這是防毒或備份軟體造成的，請確認 settings 目錄的排除設定。\n"
                      "（本信只寄給開發者；臨床同仁不會收到。）"),
                attachment_path=None,
                category="system",
            )
        except Exception:
            logging.warning("[會診] 基準遺失告警寄送失敗", exc_info=True)

    try:
        threading.Thread(target=_worker, name="ConsultBaselineAlert",
                         daemon=True).start()
    except Exception:
        logging.warning("[會診] 基準遺失告警執行緒起不來", exc_info=True)


def _note_job_success() -> None:
    """任務成功跑完 → 清空連續失敗計數（並在剛從故障恢復時留一行 log）。"""
    # [2026-08-03 常駐] 登入已成功 → 冷卻解除(★也要落地★,否則重啟後
    #   又把舊的冷卻讀回來,白白多等 15 分鐘不做事)。
    _set_login_cooldown_until(0.0)
    # ★查詢成功只證明【BDE 這一個原因】恢復了★(批次SH)
    #   SW_HIDE 後備模式下查詢照樣會成功,而 USER object 還是耗盡的、
    #   每輪還是在送帳密(那正是要修的事)。RESOURCE 的恢復訊號是
    #   「隱藏桌面又建得起來」(`_note_hidden_desktop_ok`)。
    #   ★所以這裡只結掉自己那一個,不可以動共用的取消令。★
    _clear_reboot_reason("BDE")
    global _job_fail_streak
    with _job_fail_lock:
        if not _job_fail_streak:
            return
        logging.info("[health] 會診查詢已恢復正常(先前連續失敗 %d 次)", _job_fail_streak)
        _job_fail_streak = 0
        # ★恢復了就要把它寫掉★ 否則下次重啟又把舊的 streak 讀回來,
        #   第一次失敗就直接跨過門檻。
        _save_job_fail_state()
    # 恢復了 → 對話框截圖代表的那個原因已經不存在,清掉(盡力而為;
    # 刪不掉也沒關係,告警那邊還有 12 小時的新鮮度檢查擋著)。
    try:
        os.remove(_login_dialog_shot_path())
    except OSError:
        pass


def _note_job_failure(recipients, reason: str) -> None:
    """任務重試用盡 → 累計；達門檻且過了冷卻時間就寄一封告警（節流，不洗信箱）。

    [2026-08-02] recipients 由呼叫端固定傳入開發者信箱(見呼叫點的說明),
    不再取自 cfg —— 這封是【系統故障】告警,不是臨床通知。
    """
    global _job_fail_streak, _job_fail_last_alert
    with _job_fail_lock:
        _job_fail_streak += 1
        _save_job_fail_state()      # 先把計數存起來,重啟後才接得下去
        if _job_fail_streak < _JOB_FAIL_ALERT_THRESHOLD:
            return
        now = time.time()
        # ★每次都要檢查★ 校時是執行期間發生的，只在載入時檢查救不到
        #   （這支程式正常運作時 log 一直在更新，watchdog 不會重啟它）。
        if _forget_future_alert_ts(now):
            _save_job_fail_state()      # 壞掉的時間戳也要從磁碟上清掉
        if now - _job_fail_last_alert < _JOB_FAIL_ALERT_COOLDOWN_SEC:
            return
        if not recipients:
            logging.warning("[health] 會診查詢連續失敗 %d 次,但無收件人可告警",
                            _job_fail_streak)
            return
        _job_fail_last_alert = now
        # ★寄之前就先把冷卻時間寫下去★ 寄信是在背景執行緒做的,而這台機器隨時
        #   可能被 watchdog 重啟;等寄完再寫的話,重啟正好卡在中間就等於沒節流。
        #   寧可「寫了但信沒寄成」(下一輪 6 小時後補寄)也不要洗信箱。
        _save_job_fail_state()
        streak = _job_fail_streak
    host = ""
    try:
        host = socket.gethostname()
    except OSError:
        pass

    def _worker():
        snap = None
        try:
            from cmuh_common.smtp_mail import send_mail
            # ★附件與內文那一句由同一個判斷決定★:說有附就真的有附,
            #   說沒有就真的沒有 —— 兩者分開判斷的話,遲早一邊先改而另一邊
            #   還在講舊話(宣稱要與實作一致)。
            # ★而且附的是不可變快照★(外審 SK 第 1 輪 P2):正式檔隨時會被
            #   「恢復清理」刪掉、被下一輪重截換掉 —— 快照失敗就當成沒有
            #   截圖(不附、也不宣稱),不能讓附件問題弄丟整封告警。
            shot = None
            src = _login_dialog_shot_for_alert()
            if src:
                try:
                    snap = _login_dialog_shot_sending_path()
                    shutil.copyfile(src, snap)
                    shot = snap
                except OSError:
                    logging.warning("[health] 截圖快照失敗 → 本封告警不附截圖"
                                    "(告警本身照寄)", exc_info=True)
                    snap = None
            shot_line = ("已附上登入途中攔到的對話框截圖 —— "
                         "它通常就是 HIS 拒絕登入的原因(內文是畫在視窗上的,"
                         "文字抓不到,只能用看的)。\n" if shot else "")
            send_mail(
                recipients=[str(r) for r in recipients],
                subject="⚠ 會診查詢自動化連續失敗"
                        + (f"（{host}）" if host else ""),
                body=("會診查詢的自動輪詢已連續失敗 "
                      f"{streak} 次，目前很可能查不到任何會診。\n\n"
                      + (f"發生在：{host}\n" if host else "")
                      + f"最後錯誤：{str(reason)[:300]}\n"
                      + shot_line +
                      "\n請注意：輪詢模式在「沒有新會診」時本來就不會寄信，因此這種故障"
                      "從外觀上與「今天沒有新會診」完全一樣——期間請以人工方式確認會診，"
                      "並查看 settings/consult_query.log。\n"
                      "（本信只寄給開發者；臨床同仁不會收到，需要時請自行轉知。）\n"
                      "（恢復正常後不會再寄；同一波故障最多 6 小時提醒一次，"
                      "重啟也不會重來。多台電腦各自計算，所以信中會註明是哪一台。）"),
                attachment_path=Path(shot) if shot else None,
                category="system",      # [P2-02] 連續失敗告警走系統額度
            )
            logging.info("[health] 已寄出連續失敗告警(%d 次,截圖=%s)",
                         streak, "有附" if shot else "無")
        except Exception:
            logging.warning("[health] 連續失敗告警寄送失敗", exc_info=True)
        finally:
            # 快照是一次性的,寄完(成敗都)清掉;刪不掉也只是留一個小檔,
            # 下一封會直接覆蓋它。
            if snap:
                try:
                    os.remove(snap)
                except OSError:
                    pass

    threading.Thread(target=_worker, name="ConsultHealthAlert",
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
                category="system",      # [P2-02] 故障通知走系統額度
            )
            logging.info("[notify] 已寄失敗通知給觸發者：%s",
                         ", ".join(str(r) for r in recipients))
        except Exception:
            logging.warning("[notify] 失敗通知寄送失敗(不影響流程)", exc_info=True)

    threading.Thread(target=_worker, name="ConsultFailNotice",
                     daemon=True).start()


def _send_delivery_unknown_notice_async(recipients, reason: str) -> None:
    """[2026-08-06 外審 P1-04] 寄信「結果不明」時回信告知觸發者 —— 措辭必須與
    「失敗」相反:請他【先不要】重發。

    一般失敗通知會說「已解除重查限制,您可立即重寄一封觸發信再試」。對 UNKNOWN
    照抄那句話會直接製造重複寄送:原信稍後送達 + 使用者重發又送一封。
    這裡也【不】釋放 `_release_trigger_dedup` —— 去重窗自然過期前擋住重發。"""
    if not recipients:
        return

    def _worker():
        try:
            from cmuh_common.smtp_mail import send_mail
            send_mail(
                recipients=[str(r) for r in recipients],
                subject="會診查詢：寄送結果尚未確認",
                body=("您的會診查詢已執行完成，但【寄送結果尚未確認】"
                      "（送信過程逾時，伺服器可能已經收下）。\n\n"
                      f"詳細：{str(reason)[:300]}\n\n"
                      "請先檢查信箱：\n"
                      "  • 若已收到會診通知信 → 一切正常，不需要做任何事。\n"
                      "  • 若過幾分鐘仍未收到 → 再重寄一封觸發信。\n\n"
                      "為避免您收到兩封相同的會診通知，本次【不會自動重寄】。"),
                attachment_path=None,
                category="system",
            )
            logging.info("[notify] 已寄「結果不明」通知給觸發者：%s",
                         ", ".join(str(r) for r in recipients))
        except Exception:
            logging.warning("[notify] 結果不明通知寄送失敗(不影響流程)",
                            exc_info=True)

    threading.Thread(target=_worker, name="ConsultUnknownNotice",
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
    _flush_delivery_ledger()
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
            age = time.monotonic() - last

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
                force_closed_at = time.monotonic()
                continue

            # [I] scheduler 半死：tick 正常但 IMAP 一直失敗
            #   (e.g. cooldown 中, 或網路斷)
            last_ok = _SCHEDULER_LIVENESS.get("last_imap_success", 0.0)
            if last_ok > 0:
                imap_age = time.monotonic() - last_ok
                if imap_age > HALF_DEAD_THRESHOLD:
                    if time.monotonic() - last_half_dead_log > 600:
                        logging.warning(
                            "[half-dead] scheduler tick 正常但 IMAP 已 %.0f 秒"
                            "沒成功 poll (>%.0fs)。網路問題或 IMAP 認證失效？",
                            imap_age, HALF_DEAD_THRESHOLD)
                        last_half_dead_log = time.monotonic()

            # Stage 2：force_close 沒救活 → _hard_exit 強制重啟
            if force_closed_at > 0:
                since_force = time.monotonic() - force_closed_at
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
    # ★開機先補跑上次沒做完的 email 觸發★(外審第 11 輪 F3)
    #   那些觸發信已經被標成已讀了 —— 不從 journal 補跑的話,IMAP 那邊
    #   再也掃不到它們,醫師會乾等一個不會來的結果。
    #   接在這裡而不是留成一個沒人呼叫的 API:「有 API」不等於「會發生」。
    try:
        resume_pending_triggers()
    except Exception:
        logging.warning("[trigger] 補跑待處理觸發時出錯(不影響排程)",
                        exc_info=True)

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
        _SCHEDULER_LIVENESS["last_tick"] = time.monotonic()  # [批次SB #6]
        try:
            schedule.run_pending()
            # 「立即執行」旗標檔（由 --run-now 的第二個實例、或設定視窗寫入）
            # ★要「取得」旗標才算收到要求★(批次SF #1,見 `_claim_flag_file`)
            if RUNNOW_FLAG.exists() and _claim_flag_file(RUNNOW_FLAG):
                logging.info("收到立即執行要求")
                trigger_job_async("手動")
            # 「設定已變更」旗標檔（由設定視窗存檔後寫入）→ 重建排程 + 重 load cfg
            if RELOAD_FLAG.exists() and _claim_flag_file(RELOAD_FLAG):
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
                    # ★遠端指令與查詢觸發同一個節奏★(批次SI)
                    #   接在這裡而不是另開一個排程:兩者都要在【信件觸發啟用】
                    #   時才動,而且共用同一組白名單與驗證管線。
                    #   本身 fail-open 到「不執行」,不會影響下面的觸發處理。
                    try:
                        _poll_remote_commands(
                            cfg, _run_imap_commands_with_timeout())
                    except Exception:
                        logging.warning("[遠端] 指令輪詢出錯(不影響會診查詢)",
                                        exc_info=True)
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
                        _SCHEDULER_LIVENESS["last_imap_success"] = time.monotonic()  # [批次SB #6]
                        logging.info(
                            "檢查觸發信 [IMAP/%s]：未讀 %d 封，主旨含 %r 的 %d 封",
                            cfg.get("sender_account", "?"),
                            r["scanned"], kw, r["matched"])
                        if r["matched"] == 0 and r["samples"]:
                            # ★[2026-08-04 外審 P2-05] 只記指紋,不記主旨原文★
                            #   這行一天出現 3850 次(實機 log 量到)。那個信箱收到
                            #   的【任何】信件主旨都會進 consult_query.log，而其他
                            #   醫療/個人信件的主旨可能含病人姓名、床號。
                            #   要看主旨請直接開那個信箱 —— 使用者本來就讀得到,
                            #   log 不需要複製一份。
                            logging.info(
                                "（最近未讀信件指紋，用來確認收件匣有沒有在變動；"
                                "要看主旨請直接開該信箱）：%s",
                                " | ".join(r["samples"]))
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
                        # ★[2026-08-08 外審第 2 回 F4] 終局處置的信要標已讀★
                        #   命中信改成【延後】標記之後,只有被接受的那幾封會走
                        #   handoff。被白名單/驗證拒絕的、以及去重略過的,
                        #   沒有任何人去 acknowledge 它們 —— 拒絕信於是每 20 秒
                        #   被重掃一次(每輪都寫一行 warning),而被去重的合法信
                        #   在五分鐘窗過後又會變成可執行,把「已略過」的請求
                        #   延後重跑一次。收集起來,在這一輪結束時一起標掉。
                        _final_uids: list = []
                        # ★一位寄件人可能寄了【好幾封】★(第 3 回)
                        #   上一版用 dict 以寄件人為鍵,同一位的多封只留下最後
                        #   一個 uid —— 其餘那幾封沒有人 acknowledge,
                        #   會一直被重掃、或在去重窗過後再觸發一次完整查詢。
                        _uid_of: dict = {}
                        for _row in (r.get("matched_uids") or []):
                            _u, _a = _row[0], _row[1]
                            _ok = bool(_row[2]) if len(_row) > 2 else False
                            _uid_of.setdefault(
                                str(_a or "").strip().lower(),
                                []).append((str(_u), _ok))

                        # ★不要在迴圈裡定義巢狀函式★
                        #   原本這裡有一個 `_final(...)` 包住整段邏輯。pyright 對
                        #   它判定「refers to itself」而算成一筆型別債(CI 的棘輪
                        #   會紅,本機 `pyright src` 卻是 0 —— 那是兩道不同的關卡);
                        #   而為了避開 late binding 加的預設引數又觸發 ruff B023。
                        #   直接呼叫 module-level 的純函式,兩個問題一起消失。
                        _final_uids.extend(_collect_final_uids(blocked, _uid_of))
                        # ★[2026-08-06 外審 P1-05] 白名單比對的是可偽造的 From★
                        #   `From:` 是寄件者自填的純文字。imap_reader 現在會一併回
                        #   `authenticated_senders`(通過 SPF/DKIM/DMARC 者)。
                        #   未通過驗證的一律不觸發(fail-closed,2026-08-08 起
                        #   `require_authenticated_trigger` 預設就是 True)。
                        #
                        #   ★「不可以靜默失效」這個顧慮怎麼解★
                        #   舊版把預設留成 False,理由是怕使用者的觸發信路徑不帶
                        #   Authentication-Results、開了之後功能靜默失效。
                        #   但那個預設的代價是「任何人都能遠端觸發」。
                        #   現在改成:照樣擋下來,但【擋下來這件事會被主動說出來】
                        #   —— 白名單醫師的信被擋時寄一封開發者告警。
                        #   要嘛它從來不觸發(表示我們的判定沒問題),
                        #   要嘛第一次就有人知道要調整,不會等到有人抱怨。
                        #
                        #   風險註記:觸發結果是回寄給【From 那個位址】,所以偽造
                        #   From 不會把病人資料送到攻擊者手上(只會騷擾該位醫師)——
                        #   但那仍然是一次未授權的 HIS 操作與一封 PHI 郵件。
                        authed = {str(s).lower()
                                  for s in (r.get("authenticated_senders") or [])}
                        unverified = [s for s in allowed if s.lower() not in authed]
                        if unverified:
                            if cfg.get("require_authenticated_trigger", True):
                                logging.error(
                                    "★觸發信未通過寄件人驗證 → 不觸發★(From 可偽造):%s",
                                    ", ".join(unverified))
                                allowed = [s for s in allowed
                                           if s.lower() in authed]
                                # ★未通過驗證也是【終局處置】★(第 3 回)
                                #   不標已讀的話,同一封偽造信會每 20 秒被重掃
                                #   一次、每輪寫一行 error,把 log 洗掉。
                                _final_uids.extend(
                                    _collect_final_uids(unverified, _uid_of))
                                _alert_trigger_rejected(unverified)
                            else:
                                logging.warning(
                                    "觸發信寄件人未通過 SPF/DKIM/DMARC 驗證(From 可"
                                    "偽造),仍照舊觸發:%s —— 這個設定"
                                    "(require_authenticated_trigger=false)讓任何人"
                                    "都能遠端觸發,強烈建議改回 true",
                                    ", ".join(unverified))
                        # 通過的寄件人若還有【沒通過驗證的其他信】,
                        # 那幾封也是終局處置 —— 不標掉會一直被重掃。
                        # ★只在 strict 模式下才算終局★(外審第 2 回)
                        #   關掉 strict 時那些信【是要被處理的】,標成已讀等於
                        #   把一封已被接受、卻還沒登記的請求丟掉。
                        if cfg.get("require_authenticated_trigger", True):
                            _final_uids.extend(_collect_final_uids(
                                allowed, _uid_of, only_unauth=True))
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
                                # 去重＝【本輪的終局處置】(已經回了一封告知信),
                                # 不標掉的話五分鐘後它會變成可執行、重跑一次。
                                _final_uids.extend(
                                    _collect_final_uids(dedup_skipped, _uid_of))
                            if dedup_proceed:
                                logging.info(
                                    "收到觸發信（IMAP），立即執行 consult flow；"
                                    "結果將回寄給觸發者：%s",
                                    ", ".join(dedup_proceed))
                                _handoff_email_triggers(
                                    r.get("matched_uids") or [], dedup_proceed,
                                    require_auth=bool(cfg.get(
                                        "require_authenticated_trigger", True)))
                        elif not blocked:
                            # 比對到主旨但完全沒抓到 From → fallback 用設定的 recipients
                            # [opt A1] 此 fallback 分支原本沒去重：若觸發信 From 解析不出
                            # (畸形 From) 且 imap_reader 標已讀又失敗(只 log 不 raise)，這封
                            # UNSEEN 信會每輪 IMAP poll(~20s)重新命中→每 20s 重跑完整 consult
                            # flow+寄信，直到撞 SMTP rate-limit。用固定哨兵 key 套用與 allowed
                            # 路徑一致的去重，把「每 20s」壓成「最多每 dedup 窗一次」。
                            # ★[2026-08-06 外審] strict 模式下這條 fallback 必須關死★
                            #   它完全繞過白名單與寄件人驗證:任何人只要寄一封主旨
                            #   帶關鍵字、且 From 解析不出來的信,就能遠端啟動 HIS 查詢
                            #   與 PHI 郵件。開了 require_authenticated_trigger 卻留著
                            #   這個洞,等於沒開。
                            # ★[2026-08-08 外審] 這條路一律關死,不看設定★
                            #   它完全繞過白名單與寄件人驗證:任何人只要寄一封
                            #   主旨帶關鍵字、且 From 解析不出來的信,就能遠端
                            #   啟動 HIS 查詢與 PHI 郵件。
                            #   上一版把它綁在 `require_authenticated_trigger` 上,
                            #   而那個開關預設是 False —— 等於這個洞預設開著。
                            #   「解析不出寄件人」永遠不可能通過授權,所以這裡
                            #   沒有任何合法情況需要保留。
                            logging.error(
                                "★收到無法解析 From 的觸發信 → 不觸發★"
                                "(無從驗證寄件人身分;此路徑已永久關閉)")
                            # ★[外審] 這也是終局處置★ 不標已讀的話,同一封畸形
                            #   信會每 20 秒被重新 FETCH + INTERNALDATE 查詢
                            #   並寫一行 error,直到六小時過時 —— 持續投遞就能
                            #   把有效的診斷訊息洗掉。
                            _final_uids.extend(
                                _collect_final_uids([""], _uid_of))
                        # ★放在整個 if/elif 之後★ 夾在 `if allowed:` 與
                        #   `elif not blocked:` 中間的話,那個 elif 會接到
                        #   新的 if 上,整條分支的意思就變了(我第一版就是這樣)。
                        if _final_uids:
                            try:
                                from cmuh_common.imap_reader import (  # noqa: PLC0415
                                    mark_uids_seen,
                                )
                                mark_uids_seen(_final_uids)
                            except Exception:
                                logging.warning(
                                    "[trigger] 標記終局處置信失敗(會重複掃到)",
                                    exc_info=True)
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
            install_tk_exception_handler(self, program="會診查詢")
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
        self.interval_var = tk.StringVar(value=str(self.cfg.get("poll_interval_minutes", 3)))
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
            # [codex P1 R5] 下限與 load_config 一致(2):舊下限 5 會讓「開設定→存檔」
            # 把 3 夾成 5,±10% 抖動後可能超過院方 5 分鐘閒置登出 → 常駐一直斷線。
            cfg["poll_interval_minutes"] = max(2, min(120, int(self.interval_var.get().strip())))
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
        """log 視窗幫浦。★[2026-08-10 穩定性] 兩個潛伏很久的問題★

        ① 舊版沒有任何 try：`rec.getMessage()` 對格式錯誤的 log 呼叫
           （`logging.warning("%d", "x")` 這種）是【在這裡】才爆的 ——
           一筆壞紀錄就讓幫浦死掉、再也不重排，log 視窗從此凍結且無聲。
        ② 舊版只插入、從不刪除：常駐數週後 Text widget 抱著幾十萬行。
        改走共用的 `pump_log_records`（單筆各自 try、截 500 行）；
        重排無條件執行。
        """
        try:
            from cmuh_common.tk_stability import pump_log_records  # noqa: PLC0415
            pump_log_records(self.log_text, log_queue,
                             max_records=LOG_POLL_MAX_RECORDS)
        except Exception:  # noqa: BLE001  幫浦壞掉也不可以殺掉重排
            logging.debug("[UI] log 幫浦失敗", exc_info=True)
        self.after(150, self._poll_log)


# =============================================================================
# 托盤
# =============================================================================
# ★[2026-08-10 批次SF] 退場的硬性期限★
#
# `exit_action` 在建立那條唯一會 `os._exit` 的 `_shutdown` 緒【之前】,先做了
# `_session_close(...)` 與 `_abort_bde_shutdown_on_exit()`。而 `_session_close`
# 的收尾路徑是:
#     `_terminate_session_process` → `_close_session_windows`
#     → `_dismiss_blocking_modals` → `enum_children` → raw `GetWindowText()`
# 最後那個是【送 WM_GETTEXT 給目標視窗】,systemftp 凍結時永久不返回
# (本檔 `capture_window_image` 與 `win32_safe` 的模組說明講的就是同一件事)。
#
# 於是:托盤回呼那條緒卡死 → `tray_icon_object.stop()` 沒跑到(圖示還在,
# 選單再也沒有反應)→ `_shutdown` 根本沒有被建立 → ★行程永遠不會結束★。
# 而 `running` 在更前面就已經清掉了:scheduler 停了、log 不再更新 →
# 外層 watchdog 判定它死了而啟動新實例 → 新實例撞上舊實例仍持有的單例 mutex
# → 依設計【完全沉默】地退出(見 `main()` 裡那段刻意不寫 log 的說明)
# → 會診查詢從此停擺,而且沒有任何一行 log 說得出原因。
#
# 對策:退場的第一件事就是掛上這個期限。之後不論卡在哪一個 Win32 呼叫上,
# 行程都會死 —— 而「行程死掉」正是 watchdog 認得、也救得回來的狀態。
_EXIT_HARD_DEADLINE_SEC = 25.0
#: 「盡力留證據」的寬限:超過就不再等它,直接硬退。
_FORCE_EXIT_GRACE_SEC = 5.0


def _exit_now(code: int = 0) -> None:
    """★真正無條件的退場★ —— 不 logging、不取任何鎖、不碰檔案系統。

    ★[外審 SF 第 1 輪 P1-1]★ 我第一版的「保證退場」自己就會卡住:
    * 升級點都先 `logging.critical(...)` —— 那要拿 handler lock,而 lock 很可能
      正被【卡死的那條緒】持有(`_hard_exit` 自己的 docstring 就在講這件事);
    * `_hard_exit` 雖然已經把 handler flush 改成非阻塞,但它接著會
      `_flush_delivery_ledger()` → `DeliveryLedger.flush()` 會【無界】等一把
      RLock 並寫檔。
    於是「保證會死」的那條路上有兩件會等別人的事 —— 它一件都不能有。
    """
    os._exit(code)


def _exit_now_after(delay_sec: float, code: int) -> None:
    """保險絲:睡飽就無條件退場（不做任何其他事）。"""
    try:
        time.sleep(delay_sec)
    except BaseException:       # noqa: BLE001  這條路上不可以有任何理由不死
        pass
    _exit_now(code)


def _force_exit(reason: str, code: int = 1) -> None:
    """卡死時的升級退場 —— ★保證會死★，但盡量先留下證據。

    順序就是這個 helper 的全部內容:
      ① 先掛一條「`_FORCE_EXIT_GRACE_SEC` 秒後無條件 `os._exit`」的保險絲;
      ② 才去做那些【可能永遠不返回】的事(寫 log、補寫帳本)。
    保險絲開不出來(thread 耗盡 —— 那正是本批在處理的情境之一)就【立刻】硬退,
    連試都不要試:試了而卡住的話,這個函式承諾的保證就沒有了。
    """
    try:
        threading.Thread(target=_exit_now_after,
                         args=(_FORCE_EXIT_GRACE_SEC, code),
                         name="ConsultForceExit", daemon=True).start()
    except BaseException:       # noqa: BLE001
        _exit_now(code)
        return                  # ★到此為止★ `_exit_now` 不會返回,但「不再往下
        #                         做任何會等別人的事」這件事要由程式結構保證,
        #                         不是靠「它應該不會返回」這個假設。
    try:
        logging.critical("[exit] ★強制結束行程★:%s", reason)
    except BaseException:       # noqa: BLE001  留證據失敗也不可以擋住退場
        pass
    _hard_exit(reason, code=code)


def _arm_exit_deadline(deadline_sec: float = _EXIT_HARD_DEADLINE_SEC) -> bool:
    """掛上「無論如何都會退場」的硬性期限 → 掛上了嗎。

    ★[外審 SF 第 1 輪 P1-2] 回傳值是有意義的★ 掛不上(thread 耗盡)時
    呼叫端【不可以】再走進可能永久阻塞的收尾 —— 那等於這個承諾根本不存在。
    """

    def _guard() -> None:
        # ★不可以用 `_sleep_while_running`★ 呼叫端在這之後就會把 `running`
        #   清掉,那個 helper 會立刻返回 —— 期限等於沒掛(它是為了「程式還活著
        #   的時候可被中止」設計的,而這裡要的正好相反)。
        try:
            time.sleep(deadline_sec)
        except BaseException:   # noqa: BLE001
            pass
        # 使用者要求的退出 → code=0(與 `_shutdown` 的正常路徑一致)。
        _force_exit(
            f"退出收尾超過 {deadline_sec:.0f} 秒仍未結束"
            "(多半卡在凍結的 systemftp 視窗:raw GetWindowText 永久不返回)。"
            "不強制的話托盤沒收掉、單例 mutex 沒放掉,watchdog 起的新實例會被"
            "靜默擋退,會診查詢從此停擺", code=0)

    try:
        threading.Thread(target=_guard, name="ConsultExitDeadline",
                         daemon=True).start()
        return True
    except BaseException:       # noqa: BLE001
        return False


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
    # ★保證退場★ 一切收尾之前先掛上硬性期限(理由見 `_arm_exit_deadline`):
    #   下面的 `_session_close` 會走到可能永久阻塞的 raw Win32 呼叫。
    if not _arm_exit_deadline():
        # ★[外審 SF 第 2 輪 P1] 沒有保險絲就【連 log 都不可以寫】★
        #   我第一版只跳過 `_session_close`,卻仍然照走 `logging.info` /
        #   `logging.error` —— 而 handler lock 同樣可能正被卡死的那條緒持有。
        #   那等於把卡點從 Win32 換到 logging,原本的殭屍行程照樣發生。
        #   沒有保險絲的時候,唯一安全的動作就是「不做任何會等別人的事」。
        #
        #   ★唯一的例外,而且它是有界的★:已排定的 OS 重開機。不取消的話,
        #   使用者按了退出、機器卻照樣重開,那非常錯愕。`shutdown /a` 是
        #   subprocess + timeout,不碰任何鎖、不寫 log,15 秒必然返回。
        #   (走的是這個直接呼叫,不是 `_abort_bde_shutdown_on_exit` ——
        #    後者要拿 `_bde_watch_lock` 並寫 log。)
        try:
            if _bde_shutdown_pending:
                subprocess.run(
                    ["shutdown", "/a"], capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except BaseException:   # noqa: BLE001
            pass
        _exit_now(0)
        return
    logging.info("使用者要求退出會診查詢程式")
    running.clear()
    _session_close("程式結束")
    _abort_bde_shutdown_on_exit()   # [codex P1 R19] 退出不可留下排定中的重開機
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
        _flush_delivery_ledger()
        try:
            release_single_instance()
        except Exception:
            pass
        try:
            time.sleep(0.5)
        except Exception:
            pass
        # ★走同一個原語★ 裸的 `os._exit` 只留在 `_exit_now` 一處:
        #   散落各處的話,測試攔不住它 —— 而一條測試緒真的把 pytest
        #   殺掉的話,結果會變成【假綠】(本批實際發生過兩次)。
        _exit_now(0)

    try:
        threading.Thread(target=_shutdown, daemon=True,
                         name="ConsultShutdown").start()
    except BaseException:       # noqa: BLE001
        # ★收尾緒也開不出來 → 直接死★(外審 SF 第 1 輪 P1-2)
        #   舊寫法讓這個例外冒到 pystray 的 dispatcher 被吞掉,而
        #   `os._exit` 只寫在 `_shutdown` 裡面 —— 於是行程永遠不會結束。
        _exit_now(0)


def _tray_run_now(icon=None, item=None) -> None:
    trigger_job_async("手動")


def _tray_configure(icon=None, item=None) -> None:
    """用獨立行程開啟設定視窗，常駐的托盤程式不中斷（先前用 restart 重啟，
    在某些情況下重啟後設定視窗沒出現，且托盤也消失了）。"""
    try:
        # ★[外審 P2-01] 另開設定程式也要走固定 launcher★
        #   版本化之後 `sys.argv[0]` 是 `versions/<V1>/src/consult_query.py`。
        #   常駐的還是 V1、而 current.txt 已切到 V2 時,點「設定」會開出
        #   【舊版的設定 UI】去寫【新版的 settings】—— 舊預設值覆寫新欄位。
        from cmuh_common.paths import self_entry_path  # noqa: PLC0415
        launch_python_script(
            self_entry_path(),
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
                category="system",      # [P2-02] 測試信走系統額度
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

        # ★把告警節流狀態讀回來★ 這支程式會被 watchdog 重啟(log 停更 180 秒),
        #   狀態只放記憶體的話,每重啟一次冷卻就歸零 → 同一波故障重複洗信箱。
        #   必須在 _setup_logging 之後(讀失敗的警告要進得了 log)。
        _load_job_fail_state()

        # [穩定性] health monitor — RAM/網路/時鐘/硬碟 + 記憶體 leak 自動重啟 (A/E/F)
        try:
            from cmuh_common.health import start_health_monitor
            # ★[外審第 10 輪第 3 回 P2-2] RAM 保護會直接 os._exit(1)★
            #   那條路不跑 atexit —— 帳本裡還沒落地的終局狀態會消失。
            start_health_monitor("consult", pre_exit_callback=_flush_delivery_ledger,
                                 ram_warn_mb=200, ram_crit_mb=500,
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
        # [2026-08-04] 自報 PID 給 watchdog 的半死救援用（見 cmuh_common/pidfile）
        try:
            from cmuh_common.pidfile import write_pid_file  # noqa: PLC0415
            write_pid_file("consult_query")
        except Exception:
            logging.debug("[pidfile] 自報 PID 失敗（不影響會診查詢）", exc_info=True)
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
