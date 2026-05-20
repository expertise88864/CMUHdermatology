# -*- coding: utf-8 -*-
"""日期相關工具 — 集中三支入口共用的 ROC 民國日期轉換。

統一前 main.py / scheduler.py / autoclock.py 各有一份，行為略有差異
（autoclock 多了 `y > 0` 的防呆，main/scheduler 沒）。本模組取
autoclock 較嚴謹的版本當 single source of truth。

API:
  - roc_to_gregorian_year("113") -> 2024
  - parse_roc_date_str("1130505") -> date(2024, 5, 5)
"""
from __future__ import annotations

from datetime import date
from typing import Optional


def roc_to_gregorian_year(roc_year_str) -> Optional[int]:
    """民國年 → 西元年。
    "113" -> 2024；無效輸入 / 非正數 → None。"""
    try:
        y = int(roc_year_str)
        return y + 1911 if y > 0 else None
    except (ValueError, TypeError):
        return None


def parse_roc_date_str(roc_date_str) -> Optional[date]:
    """民國日期字串 "YYYMMDD"（7 字元）→ datetime.date。
    "1130505" -> date(2024, 5, 5)；任何錯誤 → None。"""
    try:
        if not roc_date_str or len(str(roc_date_str)) != 7:
            return None
        s = str(roc_date_str)
        gy = roc_to_gregorian_year(s[:3])
        if gy is None:
            return None
        return date(gy, int(s[3:5]), int(s[5:7]))
    except (ValueError, TypeError):
        return None
