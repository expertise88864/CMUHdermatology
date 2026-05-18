# -*- coding: utf-8 -*-
"""中國醫皮膚科會診查詢程式（重構自手動操作流程，全自動化）。

功能：
  1. 開啟 C:\\admc\\systemftp.exe（住院醫囑系統）
  2. 自動登入（帳密由設定檔提供）
  3. 處理「請勿開啟超過兩個」多開提示、以及登入後的「訊息通知主畫面」
  4. 用 Win32 選單命令直接跳到「病人清單及交班 → 會診清單 → 我的會診清單」
  5. 擷取「會診通知單回覆」視窗畫面
  6. 透過 Outlook 寄出截圖給設定的收件人
  7. 每日於設定時間（預設 12:00 / 17:00）自動執行

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
]
ensure_dependencies(REQUIRED_LIBS)

# === 主要 import（依賴已就緒）===
import ctypes  # noqa: E402
import logging  # noqa: E402
import queue  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import tkinter as tk  # noqa: E402
from datetime import datetime  # noqa: E402
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

from cmuh_common.atomic_io import atomic_write_json  # noqa: E402
from cmuh_common.logging_setup import QueueHandler, setup_logging  # noqa: E402
from cmuh_common.paths import get_app_dir, get_settings_dir  # noqa: E402
from cmuh_common.platform_win import is_admin, run_as_admin  # noqa: E402
from cmuh_common.single_instance import (  # noqa: E402
    ensure_single_instance, release_single_instance,
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

DEFAULT_CONFIG = {
    "username": "101358",
    "password": "101aa358",
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
    # 每天 12:30 + 17:00 都跑（不分平假日）
    "weekday_times": ["12:30", "17:00"],   # 週一～週五
    "weekend_times": ["12:30", "17:00"],   # 週六、週日（與平日相同）
    "subject_template": "{date} {time} 皮膚科會診通知單",
    "body_template": "附件為 {date} {time} 皮膚科會診通知單截圖，由系統自動擷取寄送。",
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
}

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
tray_icon_object = None
log_queue: "queue.Queue" = queue.Queue(maxsize=5000)
_config_lock = threading.Lock()


# =============================================================================
# Logging
# =============================================================================
def _setup_logging() -> None:
    setup_logging(str(LOG_FILE), max_bytes=3 * 1024 * 1024, backup_count=2)
    qh = QueueHandler(log_queue)
    qh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(qh)


# =============================================================================
# 設定檔
# =============================================================================
def load_config() -> dict:
    with _config_lock:
        cfg = dict(DEFAULT_CONFIG)
        try:
            if CONFIG_FILE.exists():
                import json
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
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
            cfg[key] = [r.strip() for r in cfg[key] if str(r).strip()]
        # 白名單比對全小寫，避免大小寫差異漏判
        cfg["allowed_trigger_senders"] = [a.lower() for a in
                                            cfg["allowed_trigger_senders"]]
        for key in ("weekday_times", "weekend_times"):
            if not isinstance(cfg.get(key), list):
                cfg[key] = list(DEFAULT_CONFIG[key])
            cfg[key] = [str(t).strip() for t in cfg[key] if str(t).strip()]
        # 數值欄位防呆
        try:
            cfg["retry_count"] = max(1, int(cfg.get("retry_count", 3) or 3))
        except (TypeError, ValueError):
            cfg["retry_count"] = DEFAULT_CONFIG["retry_count"]
        return cfg


def save_config(cfg: dict) -> None:
    with _config_lock:
        try:
            atomic_write_json(str(CONFIG_FILE), cfg)
            logging.info("設定已儲存")
        except Exception:
            logging.error("儲存設定檔失敗", exc_info=True)


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


def capture_window_image(hwnd: int):
    """用 PrintWindow 擷取視窗影像（即使被遮住/非前景也能擷取，不干擾使用者）。"""
    from PIL import Image

    left, top, right, bot = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bot - top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"視窗尺寸異常: {width}x{height}")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bmp)
    try:
        # PW_RENDERFULLCONTENT=2：抓得到 Delphi/DirectComposition 內容
        result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        bmpinfo = bmp.GetInfo()
        bmpstr = bmp.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpstr, "raw", "BGRX", 0, 1,
        )
    finally:
        try:
            win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        try:
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass

    if result != 1:
        # PrintWindow 對 Delphi 視窗即使回傳非 1 通常仍產出有效影像；
        # 視窗在螢幕外，不能用 ImageGrab 後備，直接記錄並沿用 PrintWindow 結果。
        logging.warning("PrintWindow 回傳 %s（仍沿用擷取結果）", result)
    return img


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
def run_consult_flow(trigger_label: str = "") -> Path:
    """執行完整會診查詢流程，回傳截圖路徑。失敗會 raise。

    優先用「隱藏桌面」執行 systemftp——它的所有視窗都在使用者看不到的
    虛擬桌面，永遠不會出現在使用者畫面、不會搶前景、滑鼠也不會動。
    若無法建立隱藏桌面（群組原則限制等），退回 SW_HIDE 後備模式。
    """
    cfg = load_config()
    logging.info("=== 開始會診查詢流程（觸發：%s）===", trigger_label or "手動")

    hdesk = _ensure_hidden_desktop()
    if hdesk:
        logging.info("使用隱藏桌面執行（systemftp 不會出現在你的畫面）")
        result: dict = {}

        def worker() -> None:
            try:
                if not _set_thread_desktop(hdesk):
                    raise RuntimeError("SetThreadDesktop 失敗")
                result["shot"] = _automation_on_hidden(cfg)
            except Exception as e:  # noqa: BLE001
                result["error"] = e

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
    return _run_with_sw_hide(cfg)


def _automation_on_hidden(cfg: dict) -> Path:
    """在隱藏桌面執行完整流程（呼叫者需已 SetThreadDesktop）。

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
        return shot_path

    finally:
        cleanup_pids = our_pids or (_systemftp_pids() - before)
        try:
            close_pids(cleanup_pids)
            logging.info("已關閉本次開啟的 systemftp 實例")
        except Exception:
            logging.warning("關閉 systemftp 失敗", exc_info=True)


def _run_with_sw_hide(cfg: dict) -> Path:
    """後備模式：使用者桌面上跑，配合 SW_HIDE 隱形執行緒（可能有短暫閃爍）。"""
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
        our_pids = (_systemftp_pids() - before) | {our_pid}
        logging.info("登入視窗 hwnd=%s，本次實例 pid=%s", login, sorted(our_pids))

        # 登入：TEditExt 是 Delphi 自訂控制項，必須有「真實鍵盤焦點」才收得到字，
        # 取得焦點需視窗在前景——但「前景」不需要「可見」。所以把登入視窗解除
        # 最大化、移到螢幕外後顯示再 SetForegroundWindow（使用者看不到、滑鼠不動），
        # 再 SetFocus + WM_CHAR 打字。stealth_skip 讓隱形執行緒別把它藏回去。
        stealth_skip.add(login)
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
        return shot_path

    finally:
        # 收尾：停掉隱形執行緒、關閉我們這份 systemftp、把前景還給使用者
        stealth_stop.set()
        cleanup_pids = our_pids or (_systemftp_pids() - before)
        try:
            close_pids(cleanup_pids)
            logging.info("已關閉本次開啟的 systemftp 實例")
        except Exception:
            logging.warning("關閉 systemftp 實例失敗", exc_info=True)
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


def _check_outlook_trigger(keyword: str, timeout: float = 30.0) -> tuple:
    """掃描 Outlook 收件匣未讀郵件，主旨包含 keyword 的就標為已讀。
    回傳 (triggered, scanned, matched, inbox_path, samples, error)。

    給 email-triggered run 用：使用者從任何地方寄信到自己的 Outlook 信箱，主旨
    含關鍵字即可遠端觸發本程式執行一次。

    為了效率：用 DASL Restrict 直接在 MAPI 層篩「主旨 LIKE %keyword% 且未讀」，
    對幾千封信的信箱比逐筆 iterate 快幾十倍。沒匹配時取最近 3 封未讀主旨當
    診斷樣本（讓使用者看得到 Outlook 實際收到什麼）。"""
    if not keyword:
        return (False, 0, 0, "", [], "未設定關鍵字")
    result: dict = {"samples": []}

    def w() -> None:
        import pythoncom
        pythoncom.CoInitialize()
        try:
            outlook = _connect_outlook()
            ns = outlook.GetNamespace("MAPI")
            inbox = ns.GetDefaultFolder(6)  # olFolderInbox
            try:
                result["inbox_path"] = f"{inbox.Parent.Name}\\{inbox.Name}"
            except Exception:
                result["inbox_path"] = "?"

            kw_esc = keyword.replace("'", "''")
            # DASL：主旨含 keyword 且未讀（MAPI 層篩，快）
            dasl = (
                f'@SQL="urn:schemas:httpmail:subject" LIKE \'%{kw_esc}%\' '
                f'AND "urn:schemas:httpmail:read" = 0'
            )
            use_dasl = True
            try:
                items = inbox.Items.Restrict(dasl)
                # 觸發一次 Count 確認 DASL 有效（無效會在這裡丟）
                _ = items.Count
            except Exception:
                use_dasl = False
                try:
                    items = inbox.Items.Restrict("[Unread] = True")
                except Exception:
                    items = inbox.Items

            scanned = 0
            matched = 0
            try:
                scanned = int(items.Count)
            except Exception:
                pass

            if use_dasl:
                # 預過濾後逐筆標已讀
                for i in range(scanned, 0, -1):
                    try:
                        m = items.Item(i)
                        m.UnRead = False
                        try:
                            m.Save()
                        except Exception:
                            pass
                        matched += 1
                    except Exception:
                        pass
            else:
                # 後備：逐筆檢查 Subject（含關鍵字才標已讀）
                for i in range(scanned, 0, -1):
                    try:
                        m = items.Item(i)
                        subj = m.Subject or ""
                        if keyword in subj:
                            m.UnRead = False
                            try:
                                m.Save()
                            except Exception:
                                pass
                            matched += 1
                    except Exception:
                        pass

            # 若沒匹配，撈最近 3 封未讀的主旨當診斷樣本
            if matched == 0:
                try:
                    unread = inbox.Items.Restrict("[Unread] = True")
                    unread.Sort("[ReceivedTime]", True)  # 最新優先
                    total = int(unread.Count)
                    samples = []
                    for i in range(1, min(4, total + 1)):
                        try:
                            samples.append((unread.Item(i).Subject or "")[:60])
                        except Exception:
                            pass
                    result["samples"] = samples
                except Exception:
                    pass

            result["scanned"] = scanned
            result["matched"] = matched
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    t = threading.Thread(target=w, name="OutlookTriggerCheck", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return (False, 0, 0, "", [], "查詢逾時")
    return (
        result.get("matched", 0) > 0,
        result.get("scanned", 0),
        result.get("matched", 0),
        result.get("inbox_path", "?"),
        result.get("samples", []),
        result.get("error"),
    )


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
                          sender_account: str = "") -> None:
    """實際的 Outlook COM 寄信動作，在獨立執行緒執行（自己 CoInitialize）。

    sender_account：指定要用哪個 Outlook 帳號寄（SMTP 地址）。找不到時退回
    Outlook 預設帳號，並在 log 留 warning。"""
    import pythoncom
    pythoncom.CoInitialize()
    try:
        outlook = _connect_outlook()
        mail = outlook.CreateItem(0)  # olMailItem
        mail.To = "; ".join(recipients)
        mail.Subject = subject
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
                     sender_account: str = "") -> None:
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
        args=(image_path, subject, body, recipients, result, sender_account),
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
                  recipients: list, timeout: float = 60.0) -> None:
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
              attachment_path=image_path, timeout=timeout)


def _kill_systemftp() -> None:
    """taskkill /F /IM systemftp.exe — 強制清理殘留實例。

    用於重試前的環境清理。失敗時靜默（可能是「沒有 process 可殺」也算正常）。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "systemftp.exe"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        logging.debug("taskkill systemftp.exe 失敗（可能本來就沒在跑）", exc_info=True)


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
        retry_count = max(1, int(cfg.get("retry_count", 3) or 3))

        subject = cfg["subject_template"].format(date=date_str, time=time_str)
        body = cfg["body_template"].format(date=date_str, time=time_str)

        last_err = None  # 最後一次的失敗例外，用於三次都失敗的 log
        for attempt in range(1, retry_count + 1):
            try:
                logging.info("會診查詢任務 第 %d/%d 次嘗試（trigger=%s, 收件人組=%s, mail=%s）",
                             attempt, retry_count, trigger_label,
                             recipients_label, mail_method)
                shot = run_consult_flow(trigger_label)
                if mail_method == "smtp":
                    send_via_smtp(shot, subject, body, recipients)
                else:
                    send_via_outlook(shot, subject, body, recipients,
                                      sender_account=sender)
                logging.info("會診查詢任務成功（第 %d 次嘗試）", attempt)
                return  # 成功就跳出
            except Exception as e:
                last_err = e
                logging.error("會診查詢任務第 %d/%d 次失敗：%s",
                              attempt, retry_count, e, exc_info=True)
                if attempt < retry_count:
                    logging.info("殺 systemftp.exe 後重試（sleep 3 秒）")
                    _kill_systemftp()
                    time.sleep(3)
                else:
                    logging.error("會診查詢任務已重試 %d 次仍失敗，放棄。最後錯誤：%s",
                                  retry_count, last_err)
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


def trigger_job_async(trigger_label: str, override_recipients=None) -> None:
    threading.Thread(target=_do_full_job,
                     args=(trigger_label,),
                     kwargs={"override_recipients": override_recipients},
                     name="ConsultJob", daemon=True).start()


# =============================================================================
# 排程器
# =============================================================================
def _rebuild_schedule() -> None:
    schedule.clear()
    cfg = load_config()
    if not cfg.get("enabled", True):
        logging.info("排程目前為停用狀態")
        return
    weekday_days = ("monday", "tuesday", "wednesday", "thursday", "friday")
    weekend_days = ("saturday", "sunday")

    def _add(days: tuple, times: list, label: str) -> None:
        for t in times:
            t = str(t).strip()
            ok = True
            for d in days:
                try:
                    getattr(schedule.every(), d).at(t).do(
                        trigger_job_async, trigger_label=t)
                except Exception:
                    logging.error("排程時間格式錯誤：%r（需 HH:MM）", t)
                    ok = False
                    break
            if ok:
                logging.info("已排程%s %s 自動執行", label, t)

    _add(weekday_days, cfg["weekday_times"], "平日(一～五)")
    _add(weekend_days, cfg["weekend_times"], "假日(六、日)")


def scheduler_loop() -> None:
    logging.info("=== 會診查詢排程器啟動 v%s ===", CURRENT_VERSION)
    _rebuild_schedule()
    last_email_check = 0.0
    while running.is_set():
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
            # 「設定已變更」旗標檔（由設定視窗存檔後寫入）→ 重建排程
            if RELOAD_FLAG.exists():
                try:
                    RELOAD_FLAG.unlink()
                except OSError:
                    pass
                logging.info("偵測到設定變更，重新建立排程")
                _rebuild_schedule()
            # 信件觸發：每 60 秒輪詢一次收件匣（啟用時）。改用 IMAP 直連
            # Gmail（imap.gmail.com:993），不再依賴 Outlook COM——後者在 admin
            # 行程下會起一個沒設定郵件帳號的 admin Outlook，永遠收不到信。
            cfg = load_config()
            if cfg.get("email_trigger_enabled"):
                if time.time() - last_email_check >= 60.0:
                    last_email_check = time.time()
                    kw = cfg.get("email_trigger_subject_keyword",
                                 DEFAULT_CONFIG["email_trigger_subject_keyword"])
                    try:
                        from cmuh_common.imap_reader import check_trigger as _imap_check
                        r = _imap_check(kw)
                    except Exception:
                        logging.error("IMAP 觸發檢查模組例外", exc_info=True)
                        r = {"triggered": False, "scanned": 0, "matched": 0,
                              "samples": [], "error": "imap module exception"}
                    if r.get("error"):
                        logging.warning("檢查觸發信失敗: %s", r["error"])
                    else:
                        logging.info(
                            "檢查觸發信 [IMAP/%s]：未讀 %d 封，主旨含 %r 的 %d 封",
                            cfg.get("sender_account", "?"),
                            r["scanned"], kw, r["matched"])
                        if r["matched"] == 0 and r["samples"]:
                            logging.info(
                                "（最近未讀主旨樣本，用來確認你的觸發信是否真的進收件匣）：%s",
                                " | ".join(repr(s) for s in r["samples"]))
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
                            logging.info(
                                "收到觸發信（IMAP），立即執行 consult flow；"
                                "結果將回寄給觸發者：%s",
                                ", ".join(allowed))
                            trigger_job_async("email",
                                              override_recipients=allowed)
                        elif not blocked:
                            # 比對到主旨但完全沒抓到 From → fallback 用設定的 recipients
                            logging.info(
                                "收到觸發信但無法解析 From，fallback 用 "
                                "email_trigger_recipients")
                            trigger_job_async("email")
        except Exception:
            logging.error("排程迴圈例外", exc_info=True)
        time.sleep(1)


# =============================================================================
# 設定視窗
# =============================================================================
class ConfigApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"皮膚科會診查詢設定 (v{CURRENT_VERSION})")
        self.geometry("760x620")
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

        rcp = ttk.LabelFrame(root, text="收件人（可隨時新增/刪除，最多 4 位）", padding=8)
        rcp.pack(fill=tk.X, pady=(0, 8))
        self.rcp_list = tk.Listbox(rcp, height=4, font=("Consolas", 10))
        self.rcp_list.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for r in self.cfg["recipients"]:
            self.rcp_list.insert(tk.END, r)
        rcp_btns = ttk.Frame(rcp)
        rcp_btns.pack(side=tk.LEFT, padx=6)
        self.rcp_entry = ttk.Entry(rcp_btns, width=28, font=("Consolas", 10))
        self.rcp_entry.pack(pady=2)
        ttk.Button(rcp_btns, text="新增", command=self._add_rcp).pack(fill=tk.X, pady=1)
        ttk.Button(rcp_btns, text="刪除選定", command=self._del_rcp).pack(fill=tk.X, pady=1)

        sched = ttk.LabelFrame(root, text="排程（HH:MM，多個時間用逗號分隔）", padding=8)
        sched.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(sched, text="平日（一～五）寄送時間:").grid(
            row=0, column=0, sticky="w", **pad)
        self.weekday_var = tk.StringVar(value=", ".join(self.cfg["weekday_times"]))
        ttk.Entry(sched, textvariable=self.weekday_var, width=30,
                  font=("Consolas", 11)).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(sched, text="假日（六、日）寄送時間:").grid(
            row=1, column=0, sticky="w", **pad)
        self.weekend_var = tk.StringVar(value=", ".join(self.cfg["weekend_times"]))
        ttk.Entry(sched, textvariable=self.weekend_var, width=30,
                  font=("Consolas", 11)).grid(row=1, column=1, sticky="w", **pad)
        self.enabled_var = tk.BooleanVar(value=self.cfg.get("enabled", True))
        ttk.Checkbutton(sched, text="啟用自動排程", variable=self.enabled_var
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
        if self.rcp_list.size() >= 4:
            messagebox.showwarning("上限", "最多 4 位收件人")
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
        cfg["weekday_times"] = [t.strip() for t in self.weekday_var.get().split(",")
                                if t.strip()]
        cfg["weekend_times"] = [t.strip() for t in self.weekend_var.get().split(",")
                                if t.strip()]
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
        while not log_queue.empty():
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
    logging.info("使用者要求退出會診查詢程式")
    running.clear()
    if tray_icon_object:
        try:
            tray_icon_object.stop()
        except Exception:
            pass
    release_single_instance()
    sys.exit(0)


def _tray_run_now(icon=None, item=None) -> None:
    trigger_job_async("手動")


def _tray_configure(icon=None, item=None) -> None:
    """用獨立行程開啟設定視窗，常駐的托盤程式不中斷（先前用 restart 重啟，
    在某些情況下重啟後設定視窗沒出現，且托盤也消失了）。"""
    try:
        subprocess.Popen([sys.executable, os.path.abspath(sys.argv[0]),
                          "--configure"])
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
    threading.Thread(target=_send_test_email, name="ConsultTestMail",
                     daemon=True).start()


def _check_update_in_background() -> None:
    try:
        from cmuh_common.updater import check_and_update, need_restart_after_update
        result = check_and_update()
        if need_restart_after_update(result):
            logging.info("偵測到新版，下次重啟生效")
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

        _setup_logging()
        args = sys.argv[1:]

        # 設定模式：不搶單例，直接開設定視窗
        if "--configure" in args:
            ConfigApp().mainloop()
            return

        # 第一次啟動：設定檔不存在 → 強制開設定視窗讓使用者填帳密／收件人；
        # 設定視窗關閉後才繼續走常駐流程（這樣別人裝在他自己的電腦上就不會
        # 用到預設帳密誤登入別人的身份）。
        if not CONFIG_FILE.exists():
            logging.info("首次啟動，未偵測到設定檔，先開啟設定視窗")
            ConfigApp().mainloop()
            if not CONFIG_FILE.exists():
                logging.info("設定視窗關閉但未儲存任何設定，結束")
                return

        first_instance = ensure_single_instance(MUTEX_NAME)

        if not first_instance:
            # 已有常駐實例：靜默處理（shell:startup 重開機都會撞到這裡，不能跳視窗）
            if "--run-now" in args:
                try:
                    RUNNOW_FLAG.write_text(datetime.now().isoformat(),
                                           encoding="utf-8")
                    logging.info("已通知常駐實例立即執行")
                except Exception:
                    logging.error("寫入立即執行旗標失敗", exc_info=True)
            else:
                logging.info("會診查詢程式已在執行中（系統列），本次啟動靜默結束")
            sys.exit(0)

        logging.info("=== 會診查詢程式啟動 v%s ===", CURRENT_VERSION)
        # 啟動權限狀態（給「自動提權有沒有真的生效」一個白紙黑字證據）
        logging.info("執行權限：%s",
                     "admin ✓" if is_admin() else "一般使用者 ✗（systemftp 會 740 失敗）")
        threading.Thread(target=_check_update_in_background,
                         name="ConsultUpdateChecker", daemon=True).start()

        # 排程器執行緒
        threading.Thread(target=scheduler_loop,
                         name="ConsultScheduler", daemon=True).start()

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
                time.sleep(1)

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


if __name__ == "__main__":
    main()
