# -*- coding: utf-8 -*-
"""中國醫皮膚科點座標偵測程式（重構自 中國醫皮膚科點座標偵測程式.pyw v2）。

功能（保留原行為）：
- F8 熱鍵記錄當前時間、座標與顏色
- 視窗加寬並啟用手動縮放
- 滑鼠靜止時不重複截圖（_last_pos 暫存）
- 狀態列訊息 2 秒後自動還原預設提示

【新增】
- 接入線上更新（原本沒有），啟動時非阻塞檢查 GitHub manifest
- 改用 cmuh_common.deps_runtime / paths（雙軌相容）
"""
from __future__ import annotations

import os
import sys
import threading

# === 必須在最前面：把 src/ 加到 sys.path（pyw 與 exe 模式都要）===
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# === 自動依賴安裝 ===
from cmuh_common.deps_runtime import ensure_dependencies  # noqa: E402

REQUIRED_LIBS = [
    ("requests", "requests"),
    ("keyboard", "keyboard"),
    ("pyautogui", "pyautogui"),
    ("Pillow", "PIL"),
]
ensure_dependencies(REQUIRED_LIBS)

# === 主要 import（依賴已就緒）===
import logging  # noqa: E402
import tkinter as tk  # noqa: E402
from datetime import datetime  # noqa: E402
from tkinter import messagebox, scrolledtext, ttk  # noqa: E402

import keyboard  # noqa: E402
import pyautogui  # noqa: E402
from PIL import ImageGrab  # noqa: E402

from cmuh_common.logging_setup import setup_logging  # noqa: E402
from cmuh_common.paths import get_log_path  # noqa: E402
from cmuh_common.version import CURRENT_VERSION  # noqa: E402

setup_logging(get_log_path("coord_detector.log"))
logging.info("=== coord_detector v%s 啟動 ===", CURRENT_VERSION)


def _check_update_in_background() -> None:
    """非阻塞檢查線上更新（失敗時不影響主流程）。"""
    try:
        from cmuh_common.updater import check_and_update, need_restart_after_update, perform_restart
        result = check_and_update()
        if need_restart_after_update(result):
            logging.info("座標偵測程式偵測到新版，將重啟…")
            perform_restart()
    except Exception:
        logging.debug("背景更新檢查失敗（不影響主流程）", exc_info=True)


class CoordinateDetectorApp:
    """搬自原 中國醫皮膚科點座標偵測程式.pyw。所有 UI 行為保持一致。"""

    HOTKEY = "F8"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"座標與顏色偵測器 v{CURRENT_VERSION}")

        # 視窗設定（加寬 + 啟用縮放）
        self.root.geometry("450x420")
        self.root.minsize(420, 400)
        self.root.resizable(True, True)
        self.root.attributes("-topmost", True)

        self._status_after_id: str | None = None

        self.style = ttk.Style()
        self.style.configure("TLabel", font=("Microsoft JhengHei UI", 12))
        self.style.configure("Value.TLabel", font=("Microsoft JhengHei UI", 12, "bold"),
                             foreground="#005A9C")
        self.style.configure("TButton", font=("Microsoft JhengHei UI", 10))
        self.style.configure("Status.TLabel", font=("Microsoft JhengHei UI", 9), foreground="gray")

        self.main_frame = ttk.Frame(root, padding="15")
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        info_frame = ttk.Frame(self.main_frame)
        info_frame.pack(fill=tk.X)

        ttk.Label(info_frame, text="目前座標 (X, Y):").grid(row=0, column=0, sticky="w", pady=5)
        self.coord_var = tk.StringVar(value="(..., ...)")
        ttk.Label(info_frame, textvariable=self.coord_var, style="Value.TLabel").grid(
            row=0, column=1, sticky="w")
        ttk.Button(info_frame, text="複製", command=self.copy_coords).grid(row=0, column=2, padx=10)

        ttk.Label(info_frame, text="顏色 (R, G, B):").grid(row=1, column=0, sticky="w", pady=5)
        self.rgb_var = tk.StringVar(value="(..., ..., ...)")
        ttk.Label(info_frame, textvariable=self.rgb_var, style="Value.TLabel").grid(
            row=1, column=1, sticky="w")
        ttk.Button(info_frame, text="複製", command=self.copy_rgb).grid(row=1, column=2, padx=10)

        ttk.Label(info_frame, text="顏色 (Hex):").grid(row=2, column=0, sticky="w", pady=5)
        self.hex_var = tk.StringVar(value="#...")
        ttk.Label(info_frame, textvariable=self.hex_var, style="Value.TLabel").grid(
            row=2, column=1, sticky="w")
        ttk.Button(info_frame, text="複製", command=self.copy_hex).grid(row=2, column=2, padx=10)

        ttk.Label(info_frame, text="顏色預覽:").grid(row=3, column=0, sticky="w", pady=10)
        self.color_preview = tk.Label(info_frame, background="white", width=15, height=1, relief="sunken")
        self.color_preview.grid(row=3, column=1, sticky="w")
        info_frame.columnconfigure(1, weight=1)

        ttk.Separator(self.main_frame, orient="horizontal").pack(fill="x", pady=10)

        log_frame = ttk.LabelFrame(self.main_frame, text=f"記錄區 (按 {self.HOTKEY} 記錄)")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8, font=("Consolas", 10), wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.log_text.config(state="disabled")

        bottom_frame = ttk.Frame(self.main_frame)
        bottom_frame.pack(fill=tk.X, pady=(10, 0))

        self._default_status = f"準備就緒... 按 {self.HOTKEY} 開始記錄"
        self.status_var = tk.StringVar(value=self._default_status)
        ttk.Label(bottom_frame, textvariable=self.status_var, style="Status.TLabel").pack(side=tk.LEFT)
        ttk.Button(bottom_frame, text="清除紀錄", command=self.clear_log).pack(side=tk.RIGHT)

        # 滑鼠靜止時不重複截圖
        self._last_pos = (-1, -1)
        self._last_color = (0, 0, 0)

        self.setup_hotkey()
        self.update_info()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def update_info(self) -> None:
        """持續更新座標與顏色資訊（節流：滑鼠靜止時跳過截圖）。"""
        if not self.root.winfo_exists():
            return
        try:
            x, y = pyautogui.position()
            self.coord_var.set(f"({x}, {y})")

            if (x, y) != self._last_pos:
                try:
                    img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
                    r, g, b = img.getpixel((0, 0))[:3]
                except Exception:
                    r, g, b = self._last_color
                self._last_color = (r, g, b)
                self._last_pos = (x, y)
                hex_color = f"#{r:02x}{g:02x}{b:02x}"
                self.rgb_var.set(f"({r}, {g}, {b})")
                self.hex_var.set(hex_color)
                self.color_preview.config(background=hex_color)
        except tk.TclError:
            return
        except Exception as e:
            self.coord_var.set("讀取錯誤")
            self.rgb_var.set(f"(錯誤: {e})")

        self.root.after(50, self.update_info)

    def setup_hotkey(self) -> None:
        try:
            keyboard.add_hotkey(self.HOTKEY,
                                lambda: self.root.after(0, self.record_current_data))
        except Exception:
            self.status_var.set(f"熱鍵 {self.HOTKEY} 設定失敗! (可能需要管理員權限)")

    def record_current_data(self) -> None:
        if not self.root.winfo_exists():
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        coords = self.coord_var.get()
        rgb = self.rgb_var.get()
        log_entry = f"{timestamp} - 座標: {coords}, 顏色: {rgb}\n"

        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, log_entry)
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

        self.update_status(f"記錄成功 ({self.HOTKEY})")

    def clear_log(self) -> None:
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")
        self.update_status("紀錄已清空")

    def update_status(self, message: str) -> None:
        if self._status_after_id is not None:
            try:
                self.root.after_cancel(self._status_after_id)
            except tk.TclError:
                pass
            self._status_after_id = None
        self.status_var.set(message)
        self._status_after_id = self.root.after(2000, self._reset_status)

    def _reset_status(self) -> None:
        self._status_after_id = None
        if self.root.winfo_exists():
            self.status_var.set(self._default_status)

    def copy_to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.update_status(f"'{text}' 已複製到剪貼簿")

    def copy_coords(self) -> None:
        self.copy_to_clipboard(self.coord_var.get())

    def copy_rgb(self) -> None:
        self.copy_to_clipboard(self.rgb_var.get())

    def copy_hex(self) -> None:
        self.copy_to_clipboard(self.hex_var.get())

    def on_close(self) -> None:
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    # 啟動時開背景執行緒檢查更新（不阻塞 UI）
    threading.Thread(target=_check_update_in_background,
                     name="CoordUpdateChecker", daemon=True).start()
    try:
        root = tk.Tk()
        try:
            from cmuh_common.window_icon import apply_tk_window_icon
            apply_tk_window_icon(root)
        except Exception:
            logging.debug("套用視窗圖示失敗", exc_info=True)
        CoordinateDetectorApp(root)
        root.mainloop()
    except Exception as e:
        logging.exception("座標偵測程式啟動失敗")
        try:
            messagebox.showerror("程式啟動失敗", f"發生未預期的錯誤: {e}")
        except Exception:
            print("程式啟動失敗:", e, file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
