# -*- coding: utf-8 -*-
"""依賴安裝 UI（Tkinter）。搬自原中國醫皮膚科主程式.pyw line 24-129 的 DependencyInstaller。

外觀行為（首次執行/例行檢查雙文案、進度條）維持原樣。
[2026-05-29 強化] 安裝失敗不再靜默繼續 → import 崩潰：
  - 收集失敗清單，結束時若有失敗 → 明確 messagebox 報錯 + is_finished 維持
    False（deps_runtime 會 sys.exit(0) 乾淨退出），不繼續 import 缺套件而崩潰。
  - 安裝指令改吃 requirements.txt 的版本 spec（含上限 pin），避免 runtime
    fallback 抓到破壞性新版。
"""
import importlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from cmuh_common.paths import get_app_dir, get_settings_dir


# requirements.txt 解析快取（每進程讀一次）
_REQ_SPECS_CACHE: dict | None = None


def _normalize_pkg(name: str) -> str:
    """pip 套件名正規化：小寫 + 底線/連字號統一（PEP 503 風格）。"""
    return re.sub(r"[-_.]+", "-", str(name).strip().lower())


def _load_requirement_specs() -> dict:
    """讀 requirements.txt → {normalized_pkg_name: full_spec_line}。

    full_spec_line 例如 'selenium>=4.15.0,<5'（已去除行內註解與多餘空白）。
    讀不到檔就回空 dict（fallback 用裸套件名安裝）。
    """
    global _REQ_SPECS_CACHE
    if _REQ_SPECS_CACHE is not None:
        return _REQ_SPECS_CACHE
    specs: dict = {}
    try:
        req_path = os.path.join(get_app_dir(), "requirements.txt")
        if os.path.exists(req_path):
            with open(req_path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.split("#", 1)[0].strip()  # 去行內註解
                    if not line:
                        continue
                    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
                    if not m:
                        continue
                    specs[_normalize_pkg(m.group(1))] = line
    except Exception:
        logging.debug("[deps] 讀 requirements.txt 失敗", exc_info=True)
    _REQ_SPECS_CACHE = specs
    return specs


def _resolve_pip_spec(pkg_name: str) -> str:
    """回傳 pip install 用的目標字串：優先 requirements.txt 的完整 spec
    （含版本上限 pin），找不到就回裸套件名。"""
    specs = _load_requirement_specs()
    return specs.get(_normalize_pkg(pkg_name), pkg_name)


def _pip_python_executable(executable: str | None = None) -> str:
    """用 console python 跑 pip，避免 pythonw 吞掉安裝錯誤輸出。"""
    current = os.path.abspath(executable or sys.executable)
    base_name = os.path.basename(current).lower()
    if base_name in {"pythonw.exe", "pythonw"}:
        console_name = "python.exe" if base_name.endswith(".exe") else "python"
        console_exe = os.path.join(os.path.dirname(current), console_name)
        if os.path.isfile(console_exe):
            return console_exe
    return current


def _dependency_install_log_path() -> str:
    return os.path.join(get_settings_dir(), "dependency_install.log")


class DependencyInstaller(tk.Tk):
    """[修正] missing_libs 用以判斷顯示「首次執行」或「例行驗證」文案。"""

    def __init__(self, required_libs: list, missing_libs: list):
        super().__init__()
        self.libs = required_libs
        self.total_libs = len(self.libs) or 1
        self.is_finished = False
        self.failed_libs: list = []  # 安裝失敗的套件 pip 名稱

        is_first_run = len(missing_libs) > 0

        self.title("系統啟動中...")
        self.geometry("400x180")
        self.resizable(False, False)
        self.attributes('-topmost', True)

        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = int((screen_width / 2) - 200)
        y = int((screen_height / 2) - 90)
        self.geometry(f"+{x}+{y}")

        main_frame = ttk.Frame(self, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)

        if is_first_run:
            header_text = "首次執行正在配置環境..."
            status_text = "正在下載並安裝必要元件..."
        else:
            header_text = "正在驗證系統環境..."
            status_text = "系統檢查中..."

        ttk.Label(main_frame, text=header_text,
                  font=("Microsoft JhengHei UI", 12, "bold")).pack(pady=(0, 10))

        self.status_var = tk.StringVar(value=status_text)
        ttk.Label(main_frame, textvariable=self.status_var,
                  font=("Microsoft JhengHei UI", 10)).pack(pady=5, anchor="w")

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(main_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=10)

        self.detail_var = tk.StringVar(value="")
        ttk.Label(main_frame, textvariable=self.detail_var,
                  font=("Consolas", 8), foreground="gray").pack(anchor="e")

        threading.Thread(target=self.run_installation, name="DepInstallThread", daemon=True).start()

    def _run_on_ui_thread(self, callback):
        if threading.current_thread() is threading.main_thread():
            callback()
        else:
            self.after(0, callback)

    def run_installation(self):
        step_value = 100 / self.total_libs
        current_progress = 0

        for pkg_name, import_name in self.libs:
            self.update_ui(current_progress, f"檢查元件: {pkg_name}...")
            try:
                importlib.import_module(import_name)
            except Exception:
                self.update_ui(current_progress, f"正在下載並安裝: {pkg_name}...")
                self._run_on_ui_thread(lambda: self.detail_var.set("這可能需要一些時間，請勿關閉視窗..."))
                try:
                    startupinfo = None
                    if os.name == 'nt':
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    # 【守門 2026.05.20】.exe 模式絕不跑 pip：sys.executable 是 app exe，
                    # 會無限 spawn 自己 → fork bomb。deps_runtime 已 early-return，這是第二道防線。
                    if getattr(sys, 'frozen', False):
                        raise RuntimeError(
                            f"[.exe mode] 缺漏依賴 {pkg_name} 應由 PyInstaller spec 補齊，"
                            f"runtime 不執行 pip"
                        )
                    # [O19] pip 加速旗標：
                    #   --no-input：不互動（避免 hang）
                    #   --disable-pip-version-check：跳過 pip 自身版本檢查
                    #   --prefer-binary：優先用 wheel（避免 source 編譯）
                    # [v16 2026-05-25 P0] 加 timeout=240s + retry 1 次。原本
                    # check_call 沒 timeout，網路慢/PyPI mirror 斷時整支程式在
                    # import 階段 hang 死 (GUI 還沒出來)。240s 對 pywin32 (~50MB)
                    # 等大包剛好夠用；retry 1 次以防偶發 connection reset。
                    # 用 requirements.txt 的版本 spec（含上限 pin），避免抓到
                    # 破壞性新版；找不到 spec 時 fallback 裸套件名。
                    install_target = _resolve_pip_spec(pkg_name)
                    pip_python = _pip_python_executable()
                    cmd = [
                        pip_python, "-m", "pip", "install", install_target,
                        "--upgrade", "--quiet", "--no-input",
                        "--disable-pip-version-check", "--prefer-binary",
                    ]
                    install_log_path = _dependency_install_log_path()
                    last_err: Exception | None = None
                    for attempt in (1, 2):
                        try:
                            with open(install_log_path, "a", encoding="utf-8") as log_file:
                                log_file.write(
                                    f"\n[deps] install={pkg_name} attempt={attempt} "
                                    f"python={pip_python}\n"
                                )
                                log_file.flush()
                                subprocess.run(
                                    cmd, check=True, timeout=240,
                                    startupinfo=startupinfo,
                                    stdout=log_file,
                                    stderr=subprocess.STDOUT,
                                )
                            last_err = None
                            break
                        except subprocess.TimeoutExpired as e:
                            last_err = e
                            logging.warning(
                                "[deps] pip install %s timeout (240s)，"
                                "第 %d 次嘗試", pkg_name, attempt)
                            if attempt == 1:
                                time.sleep(3)
                        except subprocess.CalledProcessError as e:
                            last_err = e
                            logging.warning(
                                "[deps] pip install %s 失敗 (rc=%s)，第 %d 次嘗試",
                                pkg_name, e.returncode, attempt)
                            if attempt == 1:
                                time.sleep(3)
                    if last_err is not None:
                        raise last_err
                    importlib.invalidate_caches()
                    try:
                        importlib.import_module(import_name)
                    except Exception as e:
                        raise RuntimeError(
                            f"pip install {pkg_name} 完成，但 import {import_name} "
                            f"仍失敗；詳見 {install_log_path}"
                        ) from e
                except Exception as e:
                    self.failed_libs.append(pkg_name)
                    self._run_on_ui_thread(
                        lambda pkg_name=pkg_name: self.status_var.set(f"安裝失敗: {pkg_name}"))
                    logging.error("Install Error: %s", e)
                    time.sleep(1)

            current_progress += step_value
            self.update_ui(current_progress, f"驗證完成: {pkg_name}")

        # [2026-05-29] 有任何套件安裝失敗 → 明確報錯，不靜默繼續導致 import 崩潰。
        # is_finished 維持 False，deps_runtime 收到後會 sys.exit(0) 乾淨退出。
        if self.failed_libs:
            logging.error("[deps] 安裝失敗清單: %s", self.failed_libs)
            self._run_on_ui_thread(self._show_failure_and_quit)
            return

        self.update_ui(100, "環境驗證完成，正在啟動...")
        self.is_finished = True
        self.quit()

    def _show_failure_and_quit(self) -> None:
        """安裝失敗時顯示明確錯誤對話框，使用者按確定後關閉（is_finished=False）。"""
        names = "、".join(self.failed_libs)
        try:
            self.progress_bar.stop()
        except Exception:
            pass
        try:
            messagebox.showerror(
                "元件安裝失敗",
                "以下必要元件安裝失敗：\n"
                f"  {names}\n\n"
                "可能原因：無網路連線、防火牆阻擋、或 PyPI 暫時無法連線。\n\n"
                "請確認可連上網路後重新啟動本程式，\n"
                "或先執行資料夾內的「安裝Python.bat」一次性安裝所有元件。\n\n"
                "詳細紀錄：settings/dependency_install.log",
                parent=self,
            )
        except Exception:
            logging.debug("[deps] 顯示失敗對話框失敗", exc_info=True)
        # is_finished 保持 False → deps_runtime sys.exit(0)
        self.quit()

    def update_ui(self, progress: float, status_text: str) -> None:
        def apply_update():
            self.progress_var.set(progress)
            self.status_var.set(status_text)
            self.update_idletasks()
        self._run_on_ui_thread(apply_update)
