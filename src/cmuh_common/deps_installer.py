# -*- coding: utf-8 -*-
"""依賴安裝 UI（Tkinter）。搬自原中國醫皮膚科主程式.pyw line 24-129 的 DependencyInstaller。

不可改變外觀行為（首次執行/例行檢查雙文案、進度條、安裝失敗顯示等）。
"""
import importlib
import logging
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk


class DependencyInstaller(tk.Tk):
    """[修正] missing_libs 用以判斷顯示「首次執行」或「例行驗證」文案。"""

    def __init__(self, required_libs: list, missing_libs: list):
        super().__init__()
        self.libs = required_libs
        self.total_libs = len(self.libs) or 1
        self.is_finished = False

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
            except ImportError:
                self.update_ui(current_progress, f"正在下載並安裝: {pkg_name}...")
                self._run_on_ui_thread(lambda: self.detail_var.set("這可能需要一些時間，請勿關閉視窗..."))
                try:
                    startupinfo = None
                    if os.name == 'nt':
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    subprocess.check_call(
                        [sys.executable, "-m", "pip", "install", pkg_name, "--upgrade", "--quiet"],
                        startupinfo=startupinfo,
                    )
                    importlib.invalidate_caches()
                except Exception as e:
                    self._run_on_ui_thread(
                        lambda pkg_name=pkg_name: self.status_var.set(f"安裝失敗: {pkg_name}"))
                    logging.error("Install Error: %s", e)
                    time.sleep(2)

            current_progress += step_value
            self.update_ui(current_progress, f"驗證完成: {pkg_name}")

        self.update_ui(100, "環境驗證完成，正在啟動...")
        self.is_finished = True
        self.quit()

    def update_ui(self, progress: float, status_text: str) -> None:
        def apply_update():
            self.progress_var.set(progress)
            self.status_var.set(status_text)
            self.update_idletasks()
        self._run_on_ui_thread(apply_update)
