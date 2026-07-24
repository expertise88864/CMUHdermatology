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
        self.root.geometry("1080x720")     # 取消最大化後的還原尺寸
        self.root.minsize(820, 560)
        # [2026-07-24 使用者] 開啟直接全螢幕（最大化）——月曆兩列卡片需要空間
        try:
            self.root.state("zoomed")
        except tk.TclError:
            logging.debug("視窗最大化失敗（非 Windows?）", exc_info=True)
        try:
            apply_tk_window_icon(self.root)
        except Exception:
            logging.debug("套用視窗圖示失敗", exc_info=True)

        self._duty_tabs: dict = {}
        self._day_tabs: dict = {}
        self._diverged_warned = False

        # GitSyncStorage：roster 目錄若是 git repo（使用者設好 private repo）→
        # 開檔 pull、存檔背景 push、週期性 pull 跨機同步；否則退化為純本機 storage。
        # 同步狀態/遠端變更 callback 在背景 thread 觸發，於此 marshal 回主執行緒。
        self.storage = GitSyncStorage(
            os.path.join(get_settings_dir(), "roster"),
            on_sync_state=self._on_sync_state,
            on_remote_change=self._on_remote_change)
        self.service = RosterService(self.storage)
        # [2026-07-13 使用者] 打開就預設【下個月】——通常打開排班程式就是要排下個月的班
        # （7 月開 → 顯示 8 月；12 月開 → 顯示隔年 1 月）。R/VS/PGY/Clerk 共用此月份。
        today = date.today()
        ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        self.ym = f"{ny:04d}-{nm:02d}"

        self._build_ui()

    def _build_ui(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        self._nb = nb
        self._containers: dict = {}
        self._builders: dict = {}
        self._built: dict = {}

        # [2026-07-23 使用者整合] R+VS 合併為「值班排班」單一分頁（月曆每格顯示
        # 一線/三線、右側兩個結算）；PGY+Clerk 合併為「PGY/Clerk 排班」（本來就共用
        # day_slots，右側兩個統計）。
        specs = [
            ("設定", self._build_settings),
            ("值班排班 R/VS", self._build_duty),
            ("PGY / Clerk 排班", self._build_day),
        ]
        for name, builder in specs:
            cont = ttk.Frame(nb)
            nb.add(cont, text=name)
            self._containers[name] = cont
            self._builders[name] = builder
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._ensure_built("設定")   # 首頁先建好

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x")
        # 同步狀態（跨機 git 同步）：預設空字串，由 _on_sync_state 更新。
        self._sync_label = ttk.Label(bottom, text="", anchor="w",
                                     font=("Microsoft JhengHei UI", 8),
                                     foreground="gray")
        self._sync_label.pack(side="left", padx=(6, 0))
        ttk.Label(bottom, text=f"v{CURRENT_VERSION}", anchor="e",
                  font=("Consolas", 8), foreground="gray").pack(side="right")

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

    def _build_duty(self, cont):
        tab = CalendarDutyTab(cont, self.service, self)
        tab.pack(fill="both", expand=True)
        self._duty_tabs["rvs"] = tab
        return tab

    def _build_day(self, cont):
        tab = DayScheduleTab(cont, self.service, self)
        tab.pack(fill="both", expand=True)
        self._day_tabs["day"] = tab
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

    # ── 跨機 git 同步 callback（背景 thread → marshal 回主執行緒）──────────
    def _on_sync_state(self, state: str, detail: str = "") -> None:
        """GitSyncStorage 回報同步狀態（背景 thread）→ 更新底部狀態列。"""
        try:
            self.root.after(0, lambda: self._apply_sync_state(state, detail))
        except (tk.TclError, RuntimeError):
            pass                                  # mainloop 已結束（關閉/flush 階段）

    def _apply_sync_state(self, state: str, detail: str) -> None:
        texts = {
            "ok": ("已同步", "gray"),
            "offline": ("離線，變更僅存本機", "#c06000"),
            "diverged": ("同步衝突！需人工處理", "#c00000"),
            "error": ("同步設定異常", "#c00000"),
        }
        text, color = texts.get(state, (state, "gray"))
        try:
            self._sync_label.config(text=text, foreground=color)
        except (tk.TclError, AttributeError):
            return
        if state == "diverged" and not self._diverged_warned:
            self._diverged_warned = True
            try:
                from tkinter import messagebox
                messagebox.showwarning(
                    "排班資料同步衝突",
                    "本機與另一台電腦的排班各自有修改且自動合併失敗。\n\n"
                    "請保留一台繼續編輯，另一台關閉程式後在 settings/roster 資料夾\n"
                    "執行 git pull --rebase 並解決衝突；完成前該台的修改不會同步。")
            except Exception:
                logging.debug("[roster.ui] 同步衝突提示失敗", exc_info=True)

    def _on_remote_change(self) -> None:
        """週期性 pull 抓到遠端新資料（背景 thread）→ 重畫所有已建分頁。"""
        try:
            self.root.after(0, self._refresh_all_tabs)
        except (tk.TclError, RuntimeError):
            pass

    def _refresh_all_tabs(self) -> None:
        for tab in list(self._duty_tabs.values()) + list(self._day_tabs.values()):
            try:
                tab.refresh()
            except Exception:
                logging.exception("[roster.ui] 遠端變更後重繪失敗")


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

    try:
        app = ScheduleApp(root)
    except Exception:
        # [EH-03] 建構失敗原本只進 log(sys.excepthook)、視窗閃一下就消失,使用者一頭霧水(以為當機)。
        # 比照主程式 AutomationApp 建構的處理:記完整 traceback + 跳可見錯誤框提示看 log,再乾淨退出。
        logging.exception("排班程式初始化失敗 (ScheduleApp 建構)")
        try:
            ctypes.windll.user32.MessageBoxW(
                0, "排班程式初始化失敗，請查看 log 後重新啟動。", WINDOW_TITLE, 0x10)  # MB_ICONERROR
        except Exception:
            logging.debug("排班初始化失敗錯誤框顯示失敗", exc_info=True)
        sys.exit(1)
    _check_updates_in_background(root)

    logging.info("--- 排班程式骨架啟動 (v%s) ---", CURRENT_VERSION)
    _run_app(root, app)


def _run_app(root: tk.Tk, app: "ScheduleApp") -> None:
    """跑 mainloop，並在任何離開路徑（正常關閉 / KeyboardInterrupt / 自動更新
    restart_self 的 SystemExit 穿出 mainloop）都收尾：先釋放單例 mutex 讓重啟的
    新行程能啟動，再 flush 把去抖中的 push 推完（跨機同步，非 git repo 時為 no-op）。
    """
    try:
        root.mainloop()
    finally:
        # restart_self 已 spawn 新行程；flush 的 git push 可能卡到 30s（離線 timeout），
        # 新行程 ensure_single_instance 只重試 1.5s，必須先釋放 mutex 讓它能啟動。
        release_single_instance()   # 冪等；atexit 再跑一次是 no-op
        try:
            app.storage.flush()
        except Exception:
            logging.debug("關閉前 git 同步 flush 失敗", exc_info=True)
        logging.info("--- Script Finished ---")


if __name__ == "__main__":
    main()
