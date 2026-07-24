# -*- coding: utf-8 -*-
"""排班 UI 的純函式與共用小元件（純函式可獨立測試，不需顯示器）。"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from datetime import date
from tkinter import ttk

from cmuh_common.roster.model import month_dates

# 週一起始（與引擎 ISO 週一致；週六/日排在最右，方便看週末成對）
WEEKDAY_HEADERS = ("一", "二", "三", "四", "五", "六", "日")

# ── 月曆卡片共用視覺（2026-07-23 使用者：PGY/Clerk 總覽樣式套用到 R/VS）────────
# 色籤配色（chip 底色, chip 字色）——淡底深字，一眼分得出角色
OVR_STYLE = {
    "photo": ("#FFE9A8", "#7A5C00"),    # 照光=琥珀
    "tx": ("#CDEBDC", "#1B6B45"),       # 治療室=綠
    "biopsy": ("#E5D6F5", "#5E3B8C"),   # 切片室=紫
    "room": ("#D6E6F7", "#1F4E8C"),     # 跟診房=藍
    "rest": ("#EAEAEA", "#808080"),     # 放假=灰
}
OVR_FONT = "Microsoft JhengHei UI"
CARD_BG = "#FFFFFF"
CARD_CANVAS_BG = "#F7F8FA"              # 月曆底色（格與格之間）
CARD_BORDER = "#C9CFD6"
CARD_TODAY_BORDER = "#E8A317"           # 今日=金框（一眼定位今天）
CARD_HDR_NORMAL = ("#E7EDF4", "#2A3B50")     # 平日標頭（底, 字）
CARD_HDR_WEEKEND = ("#F3DDDD", "#8B2020")    # 週末標頭
CARD_HDR_HOLIDAY = ("#FFE3B8", "#8A5A00")    # 平日國定假日標頭
CARD_SEP = "#E3E7EC"                    # 早/午分隔線
CARD_HOVER_BORDER = "#5B8DEF"           # 滑鼠懸停框（可點擊格的視覺回饋）
# R/VS 合併分頁的線別色籤（chip 底色, chip 字色, 標籤）。
# [2026-07-24 使用者] 淡底色籤在小尺寸下一線/三線幾乎分不出來 → 改深底白字高對比：
# 一線=深紅底白字、三線=深藍底白字，一眼可辨。
LINE_CHIP = {"r": ("#C0392B", "#FFFFFF", "一線"),
             "vs": ("#1F4E8C", "#FFFFFF", "三線")}


def bind_hover_highlight(card, normal_color, hover=CARD_HOVER_BORDER) -> None:
    """[2026-07-23 UI 互動強化] 可點擊卡片的滑鼠懸停回饋：進入變藍框、離開還原。
    Enter 綁到卡片與所有子元件（游標移到子元件時保持高亮）；Leave 只綁卡片本體。"""
    def _on(_e):
        try:
            card.config(highlightbackground=hover)
        except tk.TclError:
            pass

    def _off(_e):
        try:
            card.config(highlightbackground=normal_color)
        except tk.TclError:
            pass

    def _walk(w):
        w.bind("<Enter>", _on, add="+")
        for ch in w.winfo_children():
            _walk(ch)
    _walk(card)
    card.bind("<Leave>", _off, add="+")

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
        self._prev_btn = ttk.Button(self, text="◀", width=3,
                                    command=lambda: self._shift(-1))
        self._prev_btn.pack(side="left")
        self._label = ttk.Label(self, width=12, anchor="center",
                                font=("Microsoft JhengHei UI", 11, "bold"))
        self._label.pack(side="left", padx=4)
        self._next_btn = ttk.Button(self, text="▶", width=3,
                                    command=lambda: self._shift(1))
        self._next_btn.pack(side="left")
        self._refresh()

    @property
    def ym(self) -> str:
        return self._ym

    def set_ym(self, ym: str) -> None:
        """外部同步月份（不觸發 on_change，避免回圈）。"""
        self._ym = ym
        self._refresh()

    def set_enabled(self, on: bool) -> None:
        """啟用/停用 ◀▶（求解中停用，避免切月造成錯月重解/套用）。"""
        st = "normal" if on else "disabled"
        self._prev_btn.config(state=st)
        self._next_btn.config(state=st)

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


def archive_finalize_pdf_async(parent, service, ym) -> None:
    """定案後於背景 lazy 安裝 reportlab 並輸出定案 PDF 留底，完成以 messagebox 告知。
    非阻塞（背景執行緒）；reportlab 已在 → 直接產生；未安裝 → 下載後產生。"""
    from tkinter import messagebox

    from cmuh_common.deps_runtime import ensure_dependencies

    def work():
        err, path = "", ""
        try:
            try:
                import reportlab  # noqa: F401,PLC0415
            except ImportError:
                ensure_dependencies([("reportlab", "reportlab")])
            path = service.archive_finalize_pdf(ym)
        except (Exception, SystemExit) as e:  # noqa: BLE001
            logging.exception("[roster.ui] 定案 PDF 留底失敗")
            err = str(e) or "已取消或安裝失敗"

        def done():
            if err:
                messagebox.showwarning("定案 PDF 留底", f"PDF 留底未完成：\n{err}")
            else:
                messagebox.showinfo("定案 PDF 留底", f"已輸出定案留底 PDF：\n{path}")
        try:
            parent.after(0, done)
        except (tk.TclError, RuntimeError):
            # [RP3-18] 背景輸出完成時視窗可能已關閉,after 指令失效會拋 TclError →
            # 靜默略過完成通知,別讓背景緒未捕捉例外炸掉。
            logging.info("[roster.ui] 定案 PDF 完成時視窗已關閉，略過完成通知")
    threading.Thread(target=work, name="finalize-pdf", daemon=True).start()
