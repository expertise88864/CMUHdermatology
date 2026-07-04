# -*- coding: utf-8 -*-
# =============================================================================
# 中國醫皮膚科排班程式 —— 乾淨骨架（2026-06-05 清空重寫）
#
# 背景：本檔原本是「門診看診管理」的近似複本（與 main.py 高度重複，是複製貼上
#       分岔 bug 的來源）。經確認 main.py 已是其功能超集，故清空，改為乾淨起點。
#
# 保留：cmuh_common 共用基礎建設 —— logging、DPI、single-instance、視窗圖示、
#       全域例外攔截、線上自動更新。讓本程式仍是「下載即跑、會自動更新」的 CMUH app。
# 移除：所有門診業務邏輯（總覽/未來週次/診斷書/小工具分頁、reg52/64 抓網、
#       F3~F11 熱鍵、值班查詢、14 天趨勢圖等）。
#
# 待辦：真正的「醫師排班」（班表資料模型、輪值規則、衝突檢查、輸出/列印）
#       由此 ScheduleApp 開始實作即可。
# =============================================================================
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import atexit
import ctypes
import logging
import threading
import tkinter as tk
from datetime import date
from tkinter import ttk

# --- cmuh_common 共用基礎建設（stdlib/ctypes 基底，可於 ensure_dependencies 前先 import）---
from cmuh_common.version import CURRENT_VERSION
from cmuh_common.paths import get_app_dir, get_settings_dir, restart_self
from cmuh_common.platform_win import set_dpi_awareness, set_app_user_model_id
from cmuh_common.window_icon import apply_tk_window_icon
from cmuh_common.logging_setup import setup_logging
from cmuh_common.single_instance import (
    ensure_single_instance, release_single_instance,
)
from cmuh_common.deps_runtime import ensure_dependencies

# 骨架的最小依賴：requests（線上更新）、Pillow（視窗圖示）、pywin32（win32/圖示）。
# 日後做真排班若需要別的套件，往這裡加即可（「下載即跑」會自動補裝）。
REQUIRED_LIBS = [
    ("requests", "requests"),
    ("Pillow", "PIL"),
    ("pywin32", "win32gui"),
]
ensure_dependencies(REQUIRED_LIBS)

# 需要第三方套件（requests）的模組，於 ensure_dependencies 之後才 import。
from cmuh_common import updater as _updater_mod  # noqa: E402

# 排班業務層與 UI（純 stdlib + cmuh_common；ortools 於「自動排班」時才 lazy 裝）。
from cmuh_common.roster.gitsync_storage import GitSyncStorage  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.ui.day_tab import DayScheduleTab  # noqa: E402
from cmuh_common.roster.ui.duty import CalendarDutyTab  # noqa: E402
from cmuh_common.roster.ui.settings import SettingsTab  # noqa: E402

BASE_DIR = get_app_dir()
LOG_FILE = os.path.join(BASE_DIR, "automation_ui.log")
setup_logging(LOG_FILE)

SINGLE_INSTANCE_MUTEX = "Local\\CMUH_Skin_Scheduler_SingleInstance_v1"
WINDOW_TITLE = "中國醫皮膚科排班程式"


def _check_updates_in_background(root: tk.Tk) -> None:
    """背景執行緒檢查線上更新；若套用了需重啟的更新，回主執行緒重啟程式。

    與主程式共用同一套 manifest.json 更新機制（平行下載 + SHA256 + 原子寫入）。
    """
    def _worker() -> None:
        try:
            result = _updater_mod.check_and_update()
        except Exception:
            logging.exception("[update] 檢查更新失敗")
            return
        if getattr(result, "errors", None):
            logging.warning("[update] 更新發生錯誤：%s", result.errors)
            return
        if _updater_mod.need_restart_after_update(result):
            logging.info("[update] 已套用自動更新，將重新啟動。")
            root.after(0, restart_self)

    threading.Thread(target=_worker, name="update-check", daemon=True).start()


class ScheduleApp:
    """醫師排班程式主視窗：ttk.Notebook 五分頁（lazy 建）。

    共用基礎建設（logging / single-instance / 圖示 / 線上更新 / 例外攔截）已於
    main() 接好。所有排班讀寫經 RosterService；R/VS 分頁共用 self.ym（月份）。
    PGY / Clerk 分頁為 Phase 3，先放占位。
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(WINDOW_TITLE)
        self.root.geometry("1080x720")
        self.root.minsize(820, 560)
        try:
            apply_tk_window_icon(self.root)
        except Exception:
            logging.debug("套用視窗圖示失敗", exc_info=True)

        # GitSyncStorage：roster 目錄若是 git repo（使用者設好 private repo）→
        # 開檔 pull、存檔背景 push 跨機同步；否則自動退化為純本機 storage。
        self.storage = GitSyncStorage(os.path.join(get_settings_dir(), "roster"))
        self.service = RosterService(self.storage)
        today = date.today()
        self.ym = f"{today.year:04d}-{today.month:02d}"   # R/VS 共用月份
        self._duty_tabs: dict = {}

        self._build_ui()

    def _build_ui(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        self._nb = nb
        self._containers: dict = {}
        self._builders: dict = {}
        self._built: dict = {}

        specs = [
            ("設定", self._build_settings),
            ("R 排班", lambda c: self._build_duty(c, "r")),
            ("VS 排班", lambda c: self._build_duty(c, "vs")),
            ("PGY", lambda c: self._build_day(c, "pgy")),
            ("見習 Clerk", lambda c: self._build_day(c, "clerk")),
        ]
        for name, builder in specs:
            cont = ttk.Frame(nb)
            nb.add(cont, text=name)
            self._containers[name] = cont
            self._builders[name] = builder
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._ensure_built("設定")   # 首頁先建好

        ttk.Label(self.root, text=f"v{CURRENT_VERSION}", anchor="e",
                  font=("Consolas", 8), foreground="gray").pack(fill="x")

    def _on_tab_changed(self, _event) -> None:
        try:
            name = self._nb.tab(self._nb.select(), "text")
        except tk.TclError:
            return
        self._ensure_built(name)
        inst = self._built.get(name)
        if hasattr(inst, "on_shown"):
            inst.on_shown()          # 切到 R/VS → 同步共用月份並重畫

    def _ensure_built(self, name: str) -> None:
        if name in self._built:
            return
        self._built[name] = self._builders[name](self._containers[name])

    def _build_settings(self, cont):
        tab = SettingsTab(cont, self.service, on_changed=self._on_settings_changed)
        tab.pack(fill="both", expand=True)
        return tab

    def _build_duty(self, cont, scope):
        tab = CalendarDutyTab(cont, self.service, scope, self)
        tab.pack(fill="both", expand=True)
        self._duty_tabs[scope] = tab
        return tab

    def _build_day(self, cont, scope):
        tab = DayScheduleTab(cont, self.service, scope, self)
        tab.pack(fill="both", expand=True)
        return tab

    def _build_placeholder(self, cont, text):
        ttk.Label(cont, text=text, foreground="gray",
                  font=("Microsoft JhengHei UI", 15)).pack(pady=80)
        return None

    def _on_settings_changed(self) -> None:
        """設定變動（名單/假日/週色/參數）→ 已建的 R/VS 分頁重畫。"""
        for tab in self._duty_tabs.values():
            try:
                tab.refresh()
            except Exception:
                logging.exception("[roster.ui] 設定變更後重繪失敗")


def main() -> None:
    # 清掉依賴安裝器可能殘留的 Tk 變數，避免背景執行緒 Variable.__del__ 崩潰。
    import gc
    gc.collect()

    set_dpi_awareness()
    set_app_user_model_id()

    # 單例：防雙開搶 log rotate / 之後的排班檔寫入撞檔。
    if not ensure_single_instance(SINGLE_INSTANCE_MUTEX):
        ctypes.windll.user32.MessageBoxW(
            0, "排班程式已在執行中。", WINDOW_TITLE, 0x40 | 0x1000)
        sys.exit(0)
    atexit.register(release_single_instance)

    root = tk.Tk()

    # 全域例外 → log，避免背景執行緒崩潰造成閃退（與主程式一致）。
    def _handle_exception(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            root.quit()
            return
        logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _handle_exception

    if hasattr(threading, "excepthook"):
        def _thread_excepthook(args):
            logging.error(
                "Uncaught exception in thread %s",
                getattr(args.thread, "name", "?"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        threading.excepthook = _thread_excepthook

    # Tk callback 例外進 log（否則進 stderr 黑洞）。
    try:
        from cmuh_common.tk_exception import install_tk_exception_handler
        install_tk_exception_handler(root)
    except Exception:
        logging.debug("Tk callback exception hook 安裝失敗", exc_info=True)

    app = ScheduleApp(root)
    _check_updates_in_background(root)

    logging.info("--- 排班程式骨架啟動 (v%s) ---", CURRENT_VERSION)
    root.mainloop()
    # 關閉前把尚未去抖推送的變更同步推上去（跨機同步，非 git repo 時為 no-op）。
    try:
        app.storage.flush()
    except Exception:
        logging.debug("關閉前 git 同步 flush 失敗", exc_info=True)
    logging.info("--- Script Finished ---")


if __name__ == "__main__":
    main()
