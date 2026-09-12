"""Calendar leave editor with monthly weekday batch selection."""
import tkinter as tk
from tkinter import ttk

from .duty import LeaveEditor
from .common import guard_write
from ..session_leave import SESSIONS


class SessionLeaveEditor(LeaveEditor):
    def _build_grid(self):
        controls = ttk.Frame(self._grid)
        controls.grid(row=0, column=0, columnspan=7, pady=6)
        self._period = tk.StringVar(value="整天")
        ttk.Label(controls, text="點選日期請假：").pack(side="left")
        ttk.Combobox(controls, textvariable=self._period, state="readonly",
                     values=("整天", *SESSIONS), width=6).pack(side="left")
        self._weekday = tk.StringVar(value="四")
        ttk.Label(controls, text="　本月每週").pack(side="left")
        ttk.Combobox(controls, textvariable=self._weekday, state="readonly",
                     values=tuple("一二三四五六日"), width=3).pack(side="left")
        ttk.Button(controls, text="加入請假", command=lambda: self._weekly(True)).pack(side="left")
        ttk.Button(controls, text="取消請假", command=lambda: self._weekly(False)).pack(side="left")
        # Reuse the date calendar below the batch controls.
        holder = self._grid
        self._grid = ttk.Frame(holder)
        self._grid.grid(row=1, column=0, columnspan=7)
        super()._build_grid()
        ttk.Label(holder, text="橘色：整天　黃色：半天（早／午）；每週操作僅套用本月。"
                  "\n自動排班會避開請假；既有保留排班衝突會提示手動確認。",
                  wraplength=520).grid(row=2, column=0, columnspan=7, pady=6)

    def _periods(self):
        return SESSIONS if self._period.get() == "整天" else (self._period.get(),)

    def _toggle(self, d):
        values = {(d, s) for s in self._periods()}
        if values <= self._selected:
            self._selected.difference_update(values)
        else:
            self._selected.update(values)
        self._refresh_buttons()

    def _weekly(self, add):
        weekday = "一二三四五六日".index(self._weekday.get())
        values = {(d, s) for d in self._buttons if d.weekday() == weekday
                  for s in self._periods()}
        if add:
            self._selected.update(values)
        else:
            self._selected.difference_update(values)
        self._refresh_buttons()

    def _refresh_buttons(self):
        for d, button in self._buttons.items():
            parts = [s for s in SESSIONS if (d, s) in self._selected]
            label = "整天" if len(parts) == 2 else "早" if parts == ["上午"] else "午" if parts else ""
            button.config(text=f"{d.day}\n{label}",
                          relief="sunken" if parts else "raised",
                          bg="#F58518" if len(parts) == 2 else "#FFD166" if parts else "SystemButtonFace")

    def _load_member(self):
        self._loaded_mid = self._member_id()
        self._selected = set(self.service.get_trainee_leave_slots(
            self.scope, self.ym, self._loaded_mid))
        self._loaded_baseline = set(self._selected)
        self._refresh_buttons()

    def _commit_current(self):
        if self._loaded_mid is None or self._selected == self._loaded_baseline:
            return True
        if not guard_write(lambda: self.service.set_trainee_leave_slots(
                self.scope, self.ym, self._loaded_mid, self._selected,
                baseline=self._loaded_baseline), title="時段請假未儲存", parent=self):
            return False
        self._loaded_baseline = set(self._selected)
        return True
