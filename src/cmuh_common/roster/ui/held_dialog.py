"""[2026-11-10 使用者] 人員固定的兩個按鈕用的勾選視窗。

* 只排未鎖定人員:勾選要鎖定的人(PGY/Clerk/外訓/家醫科),勾選存在該月份;
  按「儲存並排班」才生效,一般「自動排班」不看這份勾選。
* 局部重排(請假):勾選請假者 → 只重排他們請假的那幾個半天(當天所有人可動),
  請假者其他日子只補空位,其餘人不動。
兩者排完都會跳預覽,列出變動與未滿足條件,由使用者決定是否套用。
"""
import tkinter as tk
from tkinter import ttk


class HeldPickDialog(tk.Toplevel):
    """依身分分組的勾選清單。`result` = (動作, [代號]) 或 None(取消)。"""

    def __init__(self, parent, *, title: str, hint: str, groups, selected=(),
                 actions=(("run", "排班"),)):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.result = None
        self._vars: dict = {}
        ttk.Label(self, text=hint, wraplength=480, justify="left",
                  padding=(10, 8)).pack(fill="x")
        body = ttk.Frame(self, padding=(10, 0))
        body.pack(fill="both", expand=True)
        chosen = set(selected)
        for col, (group, codes) in enumerate(groups):
            box = ttk.LabelFrame(body, text=group, padding=6)
            box.grid(row=0, column=col, sticky="nsew", padx=4, pady=4)
            for row, code in enumerate(codes):
                var = tk.BooleanVar(value=code in chosen)
                self._vars[code] = var
                ttk.Checkbutton(box, text=code, variable=var).grid(
                    row=row, column=0, sticky="w")
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        for key, label in reversed(actions):
            ttk.Button(bar, text=label,
                       command=lambda k=key: self._finish(k)).pack(side="right", padx=4)
        self.grab_set()

    def chosen(self) -> list:
        return sorted(c for c, var in self._vars.items() if var.get())

    def _finish(self, action: str) -> None:
        self.result = (action, self.chosen())
        self.destroy()
