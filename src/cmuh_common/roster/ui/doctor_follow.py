# -*- coding: utf-8 -*-
"""Doctor follow statistics: cohort summary, person ranking and dated visits."""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

from ..doctor_follow import doctor_follow_rows
from .common import MonthSelector


class DoctorFollowStats(ttk.Frame):
    def __init__(self, master, service, app, *, on_back, on_month_change=None):
        super().__init__(master)
        self.service, self.app = service, app
        self._rows = ()
        self._visible = {}
        self._doctor_names: dict[str, str | None] = {}
        self._courses: dict[str, tuple[str, str] | None] = {"全部梯次／月份": None}
        self._person = None
        self._ym = None
        self._on_month_change = on_month_change

        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        ttk.Button(bar, text="返回排班", command=on_back).pack(side="left")
        ttk.Label(bar, text="醫師跟診統計", font=("Microsoft JhengHei UI", 12, "bold")
                  ).pack(side="left", padx=12)
        self._scope = tk.StringVar(value="全員")
        scope = ttk.Combobox(bar, textvariable=self._scope, width=9, state="readonly",
                             values=("全員", "Clerk", "PGY", "外訓", "家醫科"))
        scope.pack(side="left", padx=4)
        scope.bind("<<ComboboxSelected>>", self._filter_changed)
        self._course = tk.StringVar(value="全部梯次／月份")
        self._course_box = ttk.Combobox(bar, textvariable=self._course, width=38,
                                        state="readonly")
        self._course_box.pack(side="left", padx=4)
        self._course_box.bind("<<ComboboxSelected>>", self._render_summary)
        ttk.Button(bar, text="重新整理", command=self.refresh).pack(side="right")
        month_bar = ttk.Frame(self, padding=(6, 2))
        month_bar.pack(fill="x")
        ttk.Label(month_bar, text="統計月份").pack(side="left", padx=(0, 8))
        self._month_selector = MonthSelector(month_bar, self.app.ym, self._change_month)
        self._month_selector.pack(side="left")
        self._status = ttk.Label(self, anchor="w", padding=(6, 2))
        self._status.pack(fill="x")

        panes = ttk.Panedwindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=6, pady=4)
        summary = ttk.Frame(panes)
        details = ttk.Frame(panes)
        panes.add(summary, weight=3)
        panes.add(details, weight=2)
        self._summary = self._table(summary, (
            ("scope", "類別", 65), ("code", "代號", 60),
            ("period", "統計期間", 215), ("follow", "已排跟診", 75),
            ("doctors", "醫師數", 65), ("first", "最多", 160),
            ("second", "次多", 160), ("others", "其他", 155),
            ("unknown", "醫師未標示", 90), ("biopsy", "切片", 55),
            ("photo", "照光", 55), ("tx", "治療", 55), ("rest", "放假", 55),
        ), height=8)
        self._summary.bind("<<TreeviewSelect>>", self._select_person)
        self._detail_title = ttk.Label(details, text="尚未選取人員", padding=(0, 4))
        self._detail_title.pack(fill="x")
        split = ttk.Panedwindow(details, orient="horizontal")
        split.pack(fill="both", expand=True)
        ranks, visits = ttk.Frame(split), ttk.Frame(split)
        split.add(ranks, weight=1)
        split.add(visits, weight=2)
        self._ranks = self._table(ranks, (
            ("rank", "排名", 60), ("doctor", "醫師", 145), ("count", "次數", 65),
        ), height=6)
        self._visits = self._table(visits, (
            ("date", "日期", 110), ("session", "時段", 65),
            ("room", "診間", 65), ("doctor", "醫師", 145),
        ), height=6)
        self._ranks.bind("<<TreeviewSelect>>", self._select_doctor)

    @staticmethod
    def _table(parent, columns, *, height):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=tuple(c[0] for c in columns),
                            show="headings", selectmode="browse", height=height)
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=width, stretch=False, anchor="w")
        ybar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        return tree

    @staticmethod
    def _clear(tree):
        children = tree.get_children()
        if children:
            tree.delete(*children)

    def _change_month(self, ym):
        if self._on_month_change is not None:
            self._on_month_change(ym)
        else:
            self.app.ym = ym
            self.refresh()

    def refresh(self):
        previous = self._person.identity if self._person and self._ym == self.app.ym else None
        self._ym = self.app.ym
        self._month_selector.set_ym(self.app.ym)
        try:
            data = self.service.day_current_course_stats(self.app.ym)
            self._rows = doctor_follow_rows(data, self.app.ym)
        except Exception:
            logging.exception("[roster.ui] 醫師跟診統計讀取失敗")
            self._rows = ()
            self._filter_changed()
            self._status.config(text="統計讀取失敗，請重新整理；未顯示舊資料。",
                                foreground="#b42318")
            return
        self._filter_changed(keep_identity=previous)

    def _filter_changed(self, _event=None, *, keep_identity=None):
        self._courses = {"全部梯次／月份": None}
        for row in self._rows:
            if self._scope.get() not in ("全員", row.scope):
                continue
            label = f"{row.scope} {row.period} [{row.course}]"
            self._courses[label] = (row.scope, row.course)
        if self._course.get() not in self._courses:
            self._course.set("全部梯次／月份")
        self._course_box.configure(values=tuple(self._courses))
        self._render_summary(keep_identity=keep_identity)

    def _render_summary(self, _event=None, *, keep_identity=None):
        self._clear(self._summary)
        self._visible = {}
        self._clear_details()
        selected = self._courses.get(self._course.get())
        for row in self._rows:
            if self._scope.get() not in ("全員", row.scope):
                continue
            if selected is not None and selected != (row.scope, row.course):
                continue
            groups = row.rank_groups()
            top_counts = sorted({n for _, n in row.doctors}, reverse=True)[:2]
            other_count = sum(n not in top_counts for _, n in row.doctors)
            others = "、".join(groups[2:]) or "—"
            if len(others) > 20:
                others = f"另 {other_count} 位"
            iid = str(len(self._visible))
            self._visible[iid] = row
            period = row.period + ("（跨月至下月）" if row.ongoing else "")
            self._summary.insert("", "end", iid=iid, values=(
                row.scope, row.code, period, row.follow, len(row.doctors),
                groups[0] if groups else "—", groups[1] if len(groups) > 1 else "—",
                others, row.unknown, row.biopsy, row.photo, row.treatment, row.rest))
        self._fit_text(self._summary, ("period", "first", "second", "others"))
        self._status.config(
            text=f"{self.app.ym}｜{len(self._visible)} 人｜已排跟診次數", foreground="")
        if not self._visible:
            self._status.config(text=f"{self.app.ym}｜目前範圍無人員")
            return
        iid = next((key for key, r in self._visible.items() if r.identity == keep_identity),
                   next(iter(self._visible)))
        self._summary.selection_set(iid)
        self._summary.see(iid)
        self._select_person()

    @staticmethod
    def _fit_text(tree, columns):
        font = tkfont.nametofont("TkDefaultFont")
        for column in columns:
            width = max((font.measure(tree.set(iid, column)) + 20
                         for iid in tree.get_children()), default=0)
            tree.column(column, width=max(int(tree.column(column, "minwidth")), width))

    def _clear_details(self):
        self._person = None
        self._doctor_names = {}
        self._clear(self._ranks)
        self._clear(self._visits)
        self._detail_title.config(text="尚未選取人員")

    def _select_person(self, _event=None):
        selection = self._summary.selection()
        row = self._visible.get(selection[0]) if selection else None
        self._clear_details()
        if row is None:
            return
        self._person = row
        warning = f"｜重複排班 {row.conflicts} 時段" if row.conflicts else ""
        self._detail_title.config(
            text=f"{row.scope} {row.code}｜{row.period}｜已排跟診 {row.follow} 次{warning}")
        self._doctor_names = {"all": None}
        self._ranks.insert("", "end", iid="all", values=("", "全部醫師", row.follow))
        last_count, rank = None, 0
        for i, (name, count) in enumerate(row.doctors):
            if count != last_count:
                rank += 1
                last_count = count
            iid = str(i)
            self._doctor_names[iid] = name
            self._ranks.insert("", "end", iid=iid, values=(rank, name, count))
        if row.unknown:
            self._doctor_names["unknown"] = ""
            self._ranks.insert("", "end", iid="unknown",
                               values=("—", "醫師未標示", row.unknown))
        self._fit_text(self._ranks, ("doctor",))
        self._ranks.selection_set("all")
        self._select_doctor()

    def _select_doctor(self, _event=None):
        self._clear(self._visits)
        if self._person is None:
            return
        selection = self._ranks.selection()
        doctor = self._doctor_names.get(selection[0]) if selection else None
        for visit in self._person.visits:
            if doctor is not None and doctor != visit.doctor:
                continue
            self._visits.insert("", "end", values=(
                visit.day.isoformat(), visit.session, visit.room,
                visit.doctor or "醫師未標示"))
        self._fit_text(self._visits, ("room", "doctor"))
