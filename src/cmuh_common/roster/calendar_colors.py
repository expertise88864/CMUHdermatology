# -*- coding: utf-8 -*-
"""行事曆週色（粉/綠）——決定性規則，不需每年匯入 PDF。

由 115 年（西元 2026）官方行事曆 `1_115年行事曆.pdf` 實測歸納：週色以 **4 個
ISO 週為一段** 交替（非逐週），且相位對「絕對週」連續 → 往後年度（116/117…）
自動延續同一節奏，無需再匯入 PDF。實測 2026 全年 53 週完全吻合本規則。

錨定：2026-W03 的週一（2026-01-12）為一個「粉」段的起點。
規則：blocks = floor((該週週一 − 錨)/7 / 4)；blocks 為偶數→粉，奇數→綠
（Python floor 除法，錨之前的負值亦連續）。

用途：ColorRule（連續兩週末同色須換人）。UI/service 以此自動填 week_colors，
使用者仍可於設定頁手動覆蓋個別週（覆蓋值存 storage，優先於此自動值）。
"""
from __future__ import annotations

from datetime import date, timedelta

from cmuh_common.roster.model import week_key

# 2026-W03 週一＝粉段起點（實測自 115 行事曆）
COLOR_ANCHOR_MONDAY = date(2026, 1, 12)
CYCLE_WEEKS = 4                       # 每 4 個 ISO 週換色
PINK = "pink"
GREEN = "green"


def week_color(d: date) -> str:
    """該日期所屬 ISO 週的顏色（"pink"/"green"）。"""
    monday = d - timedelta(days=d.weekday())
    blocks = ((monday - COLOR_ANCHOR_MONDAY).days // 7) // CYCLE_WEEKS
    return PINK if blocks % 2 == 0 else GREEN


def week_colors_for_year(year: int) -> dict:
    """回傳涵蓋該年所有 ISO 週的 {week_key: color}。

    含年初/年末跨年的 ISO 週（如 2026-W01 的週一在 2025-12-29）。
    """
    out: dict = {}
    start = date(year, 1, 1)
    monday = start - timedelta(days=start.weekday())   # Jan1 所屬週的週一
    end = date(year, 12, 31)
    while monday <= end:
        out[week_key(monday)] = week_color(monday)
        monday += timedelta(days=7)
    out[week_key(end)] = week_color(end)               # 保險：年末週
    return out
