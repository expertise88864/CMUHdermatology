"""Explicit-save teaching quota editor; unfinished edits survive refreshes."""
import tkinter as tk
from tkinter import messagebox, ttk

from ..training_params import TrainingParams
from .common import guard_write


class TrainingSettings(ttk.LabelFrame):
    def __init__(self, master, service, cfg, on_changed=None):
        super().__init__(master, text="教學排班參數", padding=8)
        self.service = service
        self.on_changed = on_changed
        self.variables: dict[str, tk.StringVar] = {}
        self._loaded: dict[str, int] = {}
        self._loading = False
        self._status = tk.StringVar(self)
        for col, label in enumerate(("人員", "單位", "低標", "正常目標", "高標上限")):
            ttk.Label(self, text=label).grid(row=0, column=col, sticky="w", padx=6, pady=3)
        for row, (scope, label, unit, maximum) in enumerate((
                ("clerk", "Clerk", "次／兩週梯次", 20),
                ("pgy", "PGY", "次／週", 10),
                ("external", "外訓", "可參與時段 %", 100),
                ("family", "家醫", "可參與時段 %", 100)), 1):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=6)
            ttk.Label(self, text=unit).grid(row=row, column=1, sticky="w", padx=6)
            for col, suffix in enumerate(("low", "target", "high"), 2):
                self._spin(f"{scope}_{suffix}", row, col, maximum)
        ttk.Separator(self).grid(row=5, column=0, columnspan=5, sticky="ew", pady=8)
        ttk.Label(self, text="切片室").grid(row=6, column=0, sticky="w", padx=6)
        ttk.Label(self, text="Clerk／每梯").grid(row=7, column=0, columnspan=2, sticky="w", padx=6)
        self._spin("clerk_biopsy_low", 7, 2, 10)
        self._spin("clerk_biopsy_high", 7, 4, 10)
        for row, scope, label in ((8, "external", "外訓／每半月"), (9, "family", "家醫／每半月")):
            ttk.Label(self, text=label).grid(row=row, column=0, columnspan=2, sticky="w", padx=6)
            self._spin(f"{scope}_biopsy", row, 3, 10)
        buttons = ttk.Frame(self)
        buttons.grid(row=10, column=0, columnspan=5, sticky="w", pady=(8, 0))
        self.save_button = ttk.Button(buttons, text="儲存", command=self.save)
        self.save_button.pack(side="left", padx=4)
        ttk.Button(buttons, text="恢復預設值", command=self.defaults).pack(side="left", padx=4)
        ttk.Button(buttons, text="重新載入", command=self.reload_from_disk).pack(side="left", padx=4)
        ttk.Label(self, textvariable=self._status).grid(row=11, column=0, columnspan=5, sticky="w", padx=6)
        self.reload(cfg, force=True)

    def _spin(self, key, row, col, maximum):
        var = tk.StringVar(self)
        self.variables[key] = var
        ttk.Spinbox(self, from_=0, to=maximum, width=7, textvariable=var).grid(
            row=row, column=col, sticky="w", padx=6, pady=3)
        var.trace_add("write", self._edited)

    def dirty(self):
        return any(v.get() != str(self._loaded.get(k, "")) for k, v in self.variables.items())

    def _edited(self, *_):
        if not self._loading:
            self._status.set("未儲存" if self.dirty() else "")

    def _populate(self, values):
        self._loading = True
        try:
            for key, value in values.items():
                self.variables[key].set(str(value))
        finally:
            self._loading = False

    def reload(self, cfg, *, force=False):
        if self.dirty() and not force:
            return
        values = TrainingParams.from_config(cfg).to_dict()
        self._populate(values)
        self._loaded = values
        self._status.set("")

    def defaults(self):
        self._populate(TrainingParams().to_dict())
        self._edited()

    def reload_from_disk(self):
        if self.dirty() and not messagebox.askyesno(
                "重新載入", "放棄尚未儲存的教學排班參數？", parent=self):
            return
        def read():
            cfg, _ = self.service.storage.canonical_snapshot("config.json")
            self.reload(cfg, force=True)
        guard_write(read, title="教學排班參數", parent=self)

    def save(self):
        try:
            values = {key: int(var.get()) for key, var in self.variables.items()}
            TrainingParams.from_config({"training_params": values})
        except ValueError:
            messagebox.showerror("教學排班參數未儲存",
                                 "請輸入範圍內的整數，且低標 ≤ 正常目標 ≤ 高標上限；"
                                 "Clerk 切片低標不可大於上限。", parent=self)
            return
        if not self.dirty():
            return
        saved = {}
        def write():
            saved.update(self.service.set_training_params(values, baseline=self._loaded))
        if not guard_write(write, title="教學排班參數", parent=self):
            self._status.set("儲存失敗；輸入保留")
            return
        self.reload({"training_params": saved}, force=True)
        self._status.set("已儲存")
        if self.on_changed:
            self.on_changed()
