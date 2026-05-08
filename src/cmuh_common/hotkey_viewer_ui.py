# -*- coding: utf-8 -*-
"""[人性化熱鍵步驟檢視器 UI]

Tkinter 視窗，顯示熱鍵腳本的人類可讀步驟。

不修改原始 .py，純查看：
  - 解析度切換（1920x1080 / 1280x1024 / 1024x768）
  - 熱鍵切換（F3 / F4 / F9 / F10 / F11）
  - 顯示「第 N 步: 點擊 (x, y) 延遲 0.1s」格式
  - 點某步驟可開啟 main.py 跳到該行
"""
from __future__ import annotations

import logging
import os
import subprocess
import tkinter as tk
from tkinter import ttk

from cmuh_common.hotkey_viewer import (
    HotkeyScript,
    format_script_for_display,
    parse_all_hotkeys,
)


class HotkeyViewerWindow(tk.Toplevel):
    """熱鍵步驟檢視視窗。"""

    RESOLUTIONS = ("1280x1024", "1920x1080", "1024x768")
    HOTKEYS = ("F3", "F4", "F9", "F10", "F11")

    def __init__(self, parent: tk.Misc, *, default_resolution: str = "1280x1024",
                 main_py_path: str = ""):
        super().__init__(parent)
        self.title("熱鍵腳本步驟（人性化檢視）")
        self.geometry("780x620")
        self.minsize(680, 500)
        self.attributes("-topmost", False)

        self._main_py_path = main_py_path
        self._scripts = parse_all_hotkeys()
        self._current_script: HotkeyScript | None = None

        self._build_ui()
        self.res_var.set(default_resolution)
        self.hk_var.set("F11")  # 最常用
        self._refresh_display()

    def _build_ui(self) -> None:
        # 頂部選擇列
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="解析度:", font=("Microsoft JhengHei UI", 10)).pack(side=tk.LEFT, padx=(0, 4))
        self.res_var = tk.StringVar(value=self.RESOLUTIONS[0])
        res_combo = ttk.Combobox(top, textvariable=self.res_var, values=self.RESOLUTIONS,
                                 state="readonly", width=12)
        res_combo.pack(side=tk.LEFT, padx=(0, 12))
        res_combo.bind("<<ComboboxSelected>>", lambda *_: self._refresh_display())

        ttk.Label(top, text="熱鍵:", font=("Microsoft JhengHei UI", 10)).pack(side=tk.LEFT, padx=(0, 4))
        self.hk_var = tk.StringVar(value="F11")
        for hk in self.HOTKEYS:
            ttk.Radiobutton(top, text=hk, variable=self.hk_var, value=hk,
                            command=self._refresh_display).pack(side=tk.LEFT, padx=2)

        ttk.Button(top, text="重新解析", command=self._reparse).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(top, text="編輯 main.py", command=self._open_in_editor).pack(side=tk.RIGHT)

        # 中間：步驟摘要 + 詳細
        body = ttk.Frame(self, padding=(8, 0, 8, 8))
        body.pack(fill=tk.BOTH, expand=True)

        # 左側：步驟列表（Treeview）
        left = ttk.LabelFrame(body, text="步驟列表（點選查看詳細）", padding=4)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))

        self.steps_tree = ttk.Treeview(left, columns=("name",), show="headings",
                                       selectmode="browse", height=22)
        self.steps_tree.heading("name", text="步驟名稱")
        self.steps_tree.column("name", width=240, anchor="w")
        self.steps_tree.pack(side=tk.LEFT, fill=tk.Y)
        ysb = ttk.Scrollbar(left, orient="vertical", command=self.steps_tree.yview)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)
        self.steps_tree.configure(yscrollcommand=ysb.set)
        self.steps_tree.bind("<<TreeviewSelect>>", lambda *_: self._on_step_selected())
        self.steps_tree.bind("<Double-Button-1>", lambda *_: self._jump_to_selected_line())

        # 右側：詳細顯示
        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 上半：腳本概覽
        summary_frame = ttk.LabelFrame(right, text="腳本概覽", padding=4)
        summary_frame.pack(fill=tk.X)
        self.summary_var = tk.StringVar(value="")
        ttk.Label(summary_frame, textvariable=self.summary_var,
                  font=("Microsoft JhengHei UI", 10), justify="left",
                  wraplength=500).pack(anchor="w", padx=4, pady=4)

        # 下半：選中步驟的詳細
        detail_frame = ttk.LabelFrame(right, text="步驟詳細", padding=4)
        detail_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        self.detail_text = tk.Text(detail_frame, wrap="word",
                                   font=("Consolas", 10), bg="#FAFAFA",
                                   relief="flat", padx=8, pady=8, height=20)
        self.detail_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        det_sb = ttk.Scrollbar(detail_frame, orient="vertical",
                               command=self.detail_text.yview)
        det_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.detail_text.configure(yscrollcommand=det_sb.set, state="disabled")

        # 標籤樣式（在詳細區塊）
        self.detail_text.tag_configure("title", font=("Microsoft JhengHei UI", 12, "bold"),
                                       foreground="#005A9C")
        self.detail_text.tag_configure("section", font=("Microsoft JhengHei UI", 10, "bold"),
                                       foreground="#333333")
        self.detail_text.tag_configure("hint", foreground="#888888",
                                       font=("Microsoft JhengHei UI", 9))

        # 底部
        bottom = ttk.Frame(self, padding=(8, 0, 8, 8))
        bottom.pack(fill=tk.X)
        ttk.Label(bottom, text="提示：雙擊步驟可跳到 main.py 對應行；要修改請按【編輯 main.py】",
                  foreground="#888888",
                  font=("Microsoft JhengHei UI", 9)).pack(side=tk.LEFT)
        ttk.Button(bottom, text="關閉", command=self.destroy).pack(side=tk.RIGHT)

    # ----- UI 操作 -----
    def _refresh_display(self) -> None:
        res = self.res_var.get()
        hk = self.hk_var.get()
        script = self._scripts.get(res, {}).get(hk)
        self._current_script = script

        # 清 step list
        for iid in self.steps_tree.get_children():
            self.steps_tree.delete(iid)

        if script is None:
            self.summary_var.set(f"找不到 {hk} @ {res} 的腳本（可能未實作此解析度的此熱鍵）")
            self._set_detail("找不到腳本。\n\n可能原因：\n"
                             "  1. 此解析度未實作該熱鍵\n"
                             "  2. main.py 路徑無法定位\n"
                             "  3. 函式名稱不符合 script_F<N>_<res>x<res> 慣例")
            return

        # 概覽
        n_steps = len(script.steps)
        init_text = (f"起始動作: {script.init_action.to_human()}"
                     if script.init_action else "起始動作: (無)")
        self.summary_var.set(
            f"函式: {script.function_name}()    "
            f"main.py 第 {script.line_start}–{script.line_end} 行\n"
            f"{init_text}\n"
            f"迴圈步驟: 共 {n_steps} 個（迴圈內依序判斷，符合條件即執行）"
        )

        # step list
        for i, step in enumerate(script.steps, 1):
            self.steps_tree.insert("", "end", iid=str(i),
                                   values=(f"第 {i:>2} 步: {step.name}",))

        # 預設選第一個
        if n_steps > 0:
            self.steps_tree.selection_set("1")
            self.steps_tree.see("1")
            self._on_step_selected()
        else:
            self._set_detail(f"此熱鍵 ({hk} @ {res}) 沒有迴圈步驟。\n\n"
                             "可能是僅執行單一動作的快捷鍵（如切換病患的 F3/F4）。\n"
                             f"完整邏輯請看 main.py 第 {script.line_start} 行。")

    def _on_step_selected(self) -> None:
        sel = self.steps_tree.selection()
        if not sel or self._current_script is None:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        if idx < 1 or idx > len(self._current_script.steps):
            return
        step = self._current_script.steps[idx - 1]
        self._render_step_detail(idx, step)

    def _render_step_detail(self, idx: int, step) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert(tk.END, f"第 {idx} 步: {step.name}\n", "title")
        self.detail_text.insert(tk.END, f"main.py 第 {step.line_number} 行\n\n", "hint")

        if step.matches:
            self.detail_text.insert(tk.END, "▸ 偵測條件（全部符合才執行）：\n", "section")
            for c in step.matches:
                self.detail_text.insert(tk.END, f"     • {c.to_human()}\n")
            self.detail_text.insert(tk.END, "\n")

        if step.actions:
            self.detail_text.insert(tk.END, "▸ 執行動作（依序）：\n", "section")
            for j, act in enumerate(step.actions, 1):
                self.detail_text.insert(tk.END, f"     {j}. {act.to_human()}\n")
        else:
            self.detail_text.insert(tk.END, "▸ 此步驟僅做判斷，無實際動作\n", "section")

        self.detail_text.configure(state="disabled")

    def _set_detail(self, text: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert(tk.END, text)
        self.detail_text.configure(state="disabled")

    def _reparse(self) -> None:
        try:
            self._scripts = parse_all_hotkeys()
            self._refresh_display()
        except Exception as e:
            logging.error("重新解析失敗", exc_info=True)
            self._set_detail(f"重新解析失敗:\n{e}")

    def _open_in_editor(self) -> None:
        target = self._main_py_path
        if not target or not os.path.isfile(target):
            from cmuh_common.paths import get_app_dir
            target = os.path.join(get_app_dir(), "src", "main.py")
        if not os.path.isfile(target):
            self._set_detail(f"找不到 main.py:\n{target}")
            return
        try:
            import shutil as _sh
            editor = _sh.which("notepad++") or "notepad.exe"
            subprocess.Popen([editor, target], close_fds=True)
        except Exception as e:
            logging.error("開啟編輯器失敗: %s", e)
            self._set_detail(f"開啟編輯器失敗:\n{e}")

    def _jump_to_selected_line(self) -> None:
        """雙擊步驟：開啟 main.py 並提示目標行（多數編輯器不支援命令列指定行）。"""
        sel = self.steps_tree.selection()
        if not sel or self._current_script is None:
            return
        try:
            idx = int(sel[0])
            step = self._current_script.steps[idx - 1]
        except (ValueError, IndexError):
            return
        # Notepad++ 支援 -n<line>；其他編輯器不行，仍開檔讓使用者搜尋
        target = self._main_py_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "main.py")
        target = os.path.normpath(target)
        if not os.path.isfile(target):
            return
        try:
            import shutil as _sh
            npp = _sh.which("notepad++")
            if npp:
                subprocess.Popen([npp, target, f"-n{step.line_number}"], close_fds=True)
            else:
                subprocess.Popen(["notepad.exe", target], close_fds=True)
        except Exception:
            logging.debug("跳轉開啟失敗", exc_info=True)
