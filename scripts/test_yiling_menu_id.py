# -*- coding: utf-8 -*-
"""互動測試：找出「醫令 → 代碼輸入」的 WM_COMMAND ID。

probe 已知：
  - 視窗 class = TFopdmain
  - 主選單 pos=2 = 醫令 (16 個主選單項目，截圖數出來：病史徵候=0, 診斷=1, 醫令=2)
  - 醫令子選單有 44 項，從第三段（類別字首/代碼字首/代碼輸入...）id 範圍
    大概 214-222

用法：
  1. 主程式打開、有患者掛入、看得到「醫令」選單
  2. 跑 python scripts/test_yiling_menu_id.py
  3. 視窗會列出一排按鈕，每個對應一個 id (213-225)
  4. 從 id=215 開始按（中間值），觀察主程式畫面：
       - 焦點跳到「醫令代碼」輸入欄 → 找到了！記下 id 回報給 Claude
       - 跳出其他對話框（例如「請選擇類別」） → 不是，按下一個 id 試
  5. 若 215-225 都不對，再試 200-214 範圍

回報格式：「代碼輸入 = id=XXX，pos=YY」
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox, ttk

# === Win32 ===
user32 = ctypes.windll.user32
WM_COMMAND = 0x0111
TARGET_CLASS = "TFopdmain"
TARGET_TITLE_KW = "西醫門診醫師作業"

EnumWindowsProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _get_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _get_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_target_hwnd() -> int:
    """找 class=TFopdmain 且 title 含目標關鍵字的視窗。"""
    found = [0]

    @EnumWindowsProc
    def cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = _get_class(hwnd)
            if cls != TARGET_CLASS:
                return True
            title = _get_title(hwnd)
            if TARGET_TITLE_KW in title:
                found[0] = hwnd
                return False
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return found[0]


def send_menu_command(hwnd: int, cmd_id: int) -> None:
    """送 WM_COMMAND；HIWORD=0（menu 來源）。

    用 SendMessage（同步）而非 PostMessage（非同步）——對 Delphi VCL menu
    更可靠，VCL 內部的 action 派發要等 message handler 回應才能完成。

    注意：必須以 admin 權限執行本腳本，否則 UIPI 會擋掉發給 admin 主程式
    視窗的 WM_COMMAND，表現是「按按鈕無反應」。"""
    user32.SendMessageW(hwnd, WM_COMMAND, cmd_id, 0)


def main() -> int:
    target = find_target_hwnd()
    if not target:
        messagebox.showerror(
            "找不到主程式",
            f"找不到 class={TARGET_CLASS} 且 title 含「{TARGET_TITLE_KW}」的視窗。\n\n"
            "請先打開「中國醫藥大學附設醫院西醫門診醫師作業」並掛入患者。")
        return 1

    root = tk.Tk()
    root.title("HIS 選單 ID 重新校正 (V.1150629 改版後)")
    root.geometry("760x780")
    root.attributes("-topmost", True)

    ttk.Label(root, text=f"目標視窗 hwnd={target}",
              font=("Microsoft JhengHei UI", 10)).pack(pady=(10, 0))
    ttk.Label(root, text=f"title: {_get_title(target)}",
              font=("Microsoft JhengHei UI", 9),
              foreground="gray").pack()

    ttk.Label(root,
              text=("[2026-06-29] HIS 今天改版 V.1150629.01 → 選單指令 id 位移,要重新校正。\n"
                    "按某區的 id 按鈕 → 送該選單指令給主程式 → 看主程式畫面反應,\n"
                    "找出每個功能【現在】正確的 id,回報給 Claude(例:代碼輸入=id220、同意書=id665)。"),
              font=("Microsoft JhengHei UI", 10),
              foreground="darkblue").pack(pady=5)
    ttk.Label(root,
              text=("⚠ 這些只會「開啟對話框/視窗」,看完按取消即可,不會送出或完成病歷。\n"
                    "⚠ 請【不要】自己亂試「完成」選單的 id(可能會送出/完成該次門診)。"),
              font=("Microsoft JhengHei UI", 9, "bold"),
              foreground="#B00020").pack(pady=(0, 4))

    result_var = tk.StringVar(value="目前最後送出的 id:(無)")
    ttk.Label(root, textvariable=result_var,
              font=("Consolas", 12, "bold"),
              foreground="darkgreen").pack(pady=6)

    def make_handler(cid: int):
        def _click():
            send_menu_command(target, cid)
            result_var.set(f"剛送出 id={cid} → 請看主程式畫面反應")
        return _click

    # 依今天 probe 的新選單結構分區;只放「開對話框」的安全範圍,刻意不含「完成」選單。
    groups = [
        ("① 代碼輸入(修 F1~F5):按了焦點跳到下方『醫令代碼』輸入欄 = 對(舊 id 是 218)",
         range(213, 235)),
        ("② 同意書(修 F9/F10):按了跳出『同意書開立作業』視窗 = 對(舊 id 是 668)",
         range(660, 672)),
    ]
    for title, ids in groups:
        ttk.Label(root, text=title, font=("Microsoft JhengHei UI", 10, "bold"),
                  foreground="#8B0000").pack(pady=(10, 2), anchor="w", padx=14)
        gf = ttk.Frame(root)
        gf.pack(pady=2)
        for i, cid in enumerate(ids):
            ttk.Button(gf, text=f"id={cid}", width=8,
                       command=make_handler(cid)).grid(
                           row=i // 6, column=i % 6, padx=3, pady=3)

    ttk.Separator(root, orient="horizontal").pack(fill="x", pady=10, padx=20)

    # 任意 id 輸入
    custom_frame = ttk.Frame(root)
    custom_frame.pack(pady=5)
    ttk.Label(custom_frame, text="或輸入任意 id：",
              font=("Microsoft JhengHei UI", 10)).pack(side="left")
    custom_entry = ttk.Entry(custom_frame, width=10, font=("Consolas", 11))
    custom_entry.pack(side="left", padx=4)
    custom_entry.insert(0, "217")

    def send_custom():
        try:
            cid = int(custom_entry.get().strip())
            send_menu_command(target, cid)
            result_var.set(f"目前最後送出的 id：{cid}    請看主程式畫面")
        except ValueError:
            messagebox.showerror("錯誤", "id 必須是整數")

    ttk.Button(custom_frame, text="送出", command=send_custom).pack(side="left", padx=4)

    ttk.Label(root,
              text=("\n找到後請告訴 Claude 是 id=多少。\n"
                    "備註：本 GUI 一直浮在最上層，方便邊試邊看主程式畫面。"),
              font=("Microsoft JhengHei UI", 9),
              foreground="gray").pack(pady=10)

    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        input("按 Enter 結束...")
        sys.exit(1)
