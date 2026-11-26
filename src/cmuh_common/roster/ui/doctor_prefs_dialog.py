# -*- coding: utf-8 -*-
"""[2026-11-26 使用者] 「指定多跟醫師」對話框:選類別 → 梯次/月份 → 人員 → 醫師 →(次數)。

設定只存不排:按「自動排班」才會套用(規則見 `doctor_prefs.py`)。"""
from __future__ import annotations

import tkinter as tk
from collections import Counter
from tkinter import messagebox, ttk

from ..doctor_prefs import LABELS, MAX_TARGET, DoctorPref, calendar_days, possible_sessions
from .common import guard_write

_SCOPE_OF = {v: k for k, v in LABELS.items()}
_ORDER = ("PGY", "Clerk", "外訓", "家醫科")


def period_days(inp, scope: str, period: str) -> set:
    """這個期間的日期:Clerk 為整梯(含跨月),其他為本月。"""
    return calendar_days(inp, scope, period)


def doctor_sessions(inp, days) -> Counter:
    """每位醫師在這些日期有幾個半天門診(門診資訊設定裡標示得出醫師的診間)。"""
    out = Counter()
    for d in days:
        for rooms in (inp.clinic_doctors.get(d) or {}).values():
            for name in set(rooms.values()):
                out[name] += 1
    return out


def people_of(inp, scope: str, period: str) -> list:
    if scope == "clerk":
        batch = next((b for b in inp.clerk_batches if b.id == period), None)
        return list(batch.members) if batch is not None else []
    return list({"pgy": inp.pgy_roster, "external": inp.external_roster,
                 "family": inp.family_roster}[scope])


def parse_target(text: str):
    """→ (次數或 None, 錯誤訊息)。空白＝盡量多。"""
    text = (text or "").strip()
    if not text:
        return None, ""
    if not text.isdecimal() or not 1 <= int(text) <= MAX_TARGET:
        return None, f"次數請填 1～{MAX_TARGET} 的整數，或留空白（盡量多）"
    return int(text), ""


class DoctorPrefsDialog(tk.Toplevel):
    def __init__(self, master, service, ym: str):
        super().__init__(master)
        self.service, self.ym = service, ym
        ctx = service.doctor_pref_context(ym)
        self.inp, self.slots = ctx["inp"], ctx["slots"]
        self.baseline, self.inherited = ctx["baseline"], ctx["inherited"]
        self.prefs = list(ctx["prefs"])
        self.title(f"指定多跟醫師 · {ym}")
        self.transient(master)
        ttk.Label(self, padding=8, justify="left", text=(
            "指定某人多跟某位醫師。只在同一個半天換診間、或同一天上午／下午互換，"
            "不會改日子，也不會增減跟診次數。\n"
            "次數留空白＝盡量多；有填＝做到就好，之後也不會被換到少於這個次數。"
            "原本有跟其他醫師的人，至少保留一次其他醫師。\n"
            "名額不夠時依 Clerk ＞ 家醫科 ＞ 外訓 ＞ PGY。Clerk 的設定跟著梯次，跨月兩個月都有效。\n"
            "「最多約換到」依目前班表估算，已扣除鎖定、固定人員與切片等工作，未計名額。\n"
            "儲存後要按「自動排班」才會套用。")).pack(anchor="w")

        cols = (("scope", "類別", 60), ("period", "梯次／月份", 190), ("code", "人員", 80),
                ("doctor", "醫師", 110), ("target", "目標", 70),
                ("clinic", "期間門診", 75), ("cap", "最多約換到", 85))
        self.tree = ttk.Treeview(self, columns=tuple(c[0] for c in cols), show="headings",
                                 height=8, selectmode="browse")
        for key, title, width in cols:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8)

        form = ttk.LabelFrame(self, text="新增一筆", padding=6)
        form.pack(fill="x", padx=8, pady=6)
        self.scope_var, self.period_var = tk.StringVar(value="外訓"), tk.StringVar()
        self.code_var, self.doctor_var = tk.StringVar(), tk.StringVar()
        self.target_var = tk.StringVar()
        self._periods: dict = {}
        self._doctors: dict = {}
        self._scope_box = ttk.Combobox(form, textvariable=self.scope_var, values=_ORDER,
                                       width=7, state="readonly")
        self._period_box = ttk.Combobox(form, textvariable=self.period_var, width=26,
                                        state="readonly")
        self._code_box = ttk.Combobox(form, textvariable=self.code_var, width=10,
                                      state="readonly")
        self._doctor_box = ttk.Combobox(form, textvariable=self.doctor_var, width=30,
                                        state="readonly")
        target = ttk.Entry(form, textvariable=self.target_var, width=6)
        for col, (label, widget) in enumerate((
                ("類別", self._scope_box), ("梯次／月份", self._period_box),
                ("人員", self._code_box), ("醫師", self._doctor_box),
                ("次數（可空白）", target))):
            ttk.Label(form, text=label).grid(row=0, column=col, sticky="w", padx=4)
            widget.grid(row=1, column=col, sticky="w", padx=4)
        self._scope_box.bind("<<ComboboxSelected>>", lambda _e: self._scope_changed())
        self._period_box.bind("<<ComboboxSelected>>", lambda _e: self._period_changed())
        self._code_box.bind("<<ComboboxSelected>>", lambda _e: self._person_changed())
        ttk.Button(form, text="加入", command=self._add).grid(row=1, column=5, padx=6)

        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="刪除選取", command=self._remove).pack(side="left")
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        save = ttk.Button(bar, text="儲存（下次自動排班生效）", command=self._save)
        save.pack(side="right", padx=6)
        if ctx["finalized"]:
            save.state(["disabled"])
            ttk.Label(bar, text="本月已定案，只能檢視", foreground="#b42318").pack(side="left", padx=8)
        self._scope_changed()
        self._render()
        self.grab_set()

    # ── 表單 ──
    def _scope_changed(self):
        scope = _SCOPE_OF[self.scope_var.get()]
        if scope == "clerk":
            self._periods = {f"{b.id}（{b.start_monday:%m/%d} 起兩週）": b.id
                             for b in self.inp.clerk_batches}
        else:
            self._periods = {f"{self.ym}（整月）": self.ym}
        self._period_box.configure(values=tuple(self._periods))
        self.period_var.set(next(iter(self._periods), ""))
        self._period_changed()

    def _period_changed(self):
        scope = _SCOPE_OF[self.scope_var.get()]
        people = people_of(self.inp, scope, self._periods.get(self.period_var.get(), ""))
        self._code_box.configure(values=tuple(people))
        self.code_var.set(people[0] if people else "")
        self._person_changed()

    def _person_changed(self):
        scope = _SCOPE_OF[self.scope_var.get()]
        period = self._periods.get(self.period_var.get(), "")
        days = period_days(self.inp, scope, period)
        sessions = doctor_sessions(self.inp, days)
        self._doctors = {}
        for name in sorted(sessions):
            cap = possible_sessions(self.inp, self.slots,
                                    DoctorPref(scope, period, self.code_var.get(), name),
                                    days)
            self._doctors[f"{name}（{sessions[name]} 診，目前班表最多約換到 {cap} 次）"] = name
        self._doctor_box.configure(values=tuple(self._doctors))
        self.doctor_var.set("")

    def _add(self):
        scope = _SCOPE_OF[self.scope_var.get()]
        period = self._periods.get(self.period_var.get())
        code, doctor = self.code_var.get(), self._doctors.get(self.doctor_var.get())
        if not period or not code or not doctor:
            messagebox.showwarning("指定多跟醫師", "請選好梯次／月份、人員與醫師", parent=self)
            return
        target, err = parse_target(self.target_var.get())
        if err:
            messagebox.showwarning("指定多跟醫師", err, parent=self)
            return
        pref = DoctorPref(scope, period, code, doctor, target)
        if any(p.key == pref.key and p.doctor == doctor for p in self.prefs):
            messagebox.showwarning("指定多跟醫師", "這個人已經指定過這位醫師；要改次數請先刪除再加入",
                                   parent=self)
            return
        self.prefs.append(pref)
        self.target_var.set("")
        self._render()

    def _remove(self):
        sel = self.tree.selection()
        if sel:
            del self.prefs[int(sel[0])]
            self._render()

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.prefs):
            days = period_days(self.inp, p.scope, p.period)
            self.tree.insert("", "end", iid=str(i), values=(
                LABELS[p.scope], p.period if p.scope == "clerk" else f"{p.period}（整月）",
                p.code, p.doctor, p.target or "盡量多",
                doctor_sessions(self.inp, days).get(p.doctor, 0),
                possible_sessions(self.inp, self.slots, p, days)))

    def _save(self):
        ids = [b.id for b in self.inp.clerk_batches]
        if guard_write(lambda: self.service.set_doctor_prefs(
                self.ym, self.prefs, baseline=self.baseline, inherited=self.inherited,
                batch_ids=ids), title="指定多跟醫師", parent=self):
            self.destroy()
