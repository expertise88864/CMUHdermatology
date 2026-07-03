# -*- coding: utf-8 -*-
"""排班 UI 的純函式與共用小元件（純函式可獨立測試，不需顯示器）。"""
from __future__ import annotations

import tkinter as tk
from datetime import date
from tkinter import ttk

from cmuh_common.roster.model import month_dates

# 週一起始（與引擎 ISO 週一致；週六/日排在最右，方便看週末成對）
WEEKDAY_HEADERS = ("一", "二", "三", "四", "五", "六", "日")

# 成員色：色盲友善固定調色盤（藍/橙/綠/紫/棕/粉/青/黃），足夠 R(≤4)/VS(≤8) 用。
MEMBER_PALETTE = (
    "#4C78A8", "#F58518", "#54A24B", "#B279A2",
    "#8C564B", "#E377C2", "#17BECF", "#BCBD22",
)


def member_color(index: int) -> str:
    """依成員在名單中的序位取固定色（超過調色盤數量則循環）。"""
    return MEMBER_PALETTE[index % len(MEMBER_PALETTE)]


def fg_for(bg_hex: str) -> str:
    """依背景色亮度回傳可讀的前景色（深底→白字，淺底→黑字）。"""
    try:
        r = int(bg_hex[1:3], 16)
        g = int(bg_hex[3:5], 16)
        b = int(bg_hex[5:7], 16)
    except (ValueError, IndexError):
        return "#000000"
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "#000000" if lum > 150 else "#ffffff"


def ym_add(ym: str, delta_months: int) -> str:
    """"2026-08" ± n 個月 → "YYYY-MM"（跨年自動進退位）。"""
    y, m = int(ym[:4]), int(ym[5:7])
    idx = y * 12 + (m - 1) + delta_months
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def ym_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def calendar_matrix(year: int, month: int) -> list:
    """回傳週列矩陣（每列 7 格，週一起始）；非本月格為 None。"""
    days = month_dates(year, month)
    lead = days[0].weekday()                 # 週一=0 → 月初前的空格數
    cells: list = [None] * lead + list(days)
    while len(cells) % 7:
        cells.append(None)
    return [cells[i:i + 7] for i in range(0, len(cells), 7)]


def next_in_cycle(current, member_ids: list):
    """點格循環：None → 名單[0] → … → 名單[-1] → None。

    current 不在名單（例如已被移除的舊人）→ 回 None（一次點擊即清掉異常值）。
    """
    seq = [None] + list(member_ids)
    try:
        i = seq.index(current)
    except ValueError:
        return None
    return seq[(i + 1) % len(seq)]


class MonthSelector(ttk.Frame):
    """◀ 年月 ▶；變更時呼叫 on_change(new_ym)。"""

    def __init__(self, master, initial_ym: str, on_change):
        super().__init__(master)
        self._ym = initial_ym
        self._on_change = on_change
        ttk.Button(self, text="◀", width=3,
                   command=lambda: self._shift(-1)).pack(side="left")
        self._label = ttk.Label(self, width=12, anchor="center",
                                font=("Microsoft JhengHei UI", 11, "bold"))
        self._label.pack(side="left", padx=4)
        ttk.Button(self, text="▶", width=3,
                   command=lambda: self._shift(1)).pack(side="left")
        self._refresh()

    @property
    def ym(self) -> str:
        return self._ym

    def set_ym(self, ym: str) -> None:
        """外部同步月份（不觸發 on_change，避免回圈）。"""
        self._ym = ym
        self._refresh()

    def _shift(self, delta: int) -> None:
        self._ym = ym_add(self._ym, delta)
        self._refresh()
        self._on_change(self._ym)

    def _refresh(self) -> None:
        y, m = int(self._ym[:4]), int(self._ym[5:7])
        self._label.config(text=f"{y} 年 {m:02d} 月")


class StatusBar(ttk.Frame):
    """底部狀態列 + 忙碌指示（自動排班時顯示訊息）。"""

    def __init__(self, master):
        super().__init__(master)
        self._var = tk.StringVar(value="就緒")
        ttk.Separator(self, orient="horizontal").pack(fill="x")
        ttk.Label(self, textvariable=self._var, anchor="w",
                  padding=(8, 2)).pack(fill="x")

    def set(self, text: str) -> None:
        self._var.set(text)
