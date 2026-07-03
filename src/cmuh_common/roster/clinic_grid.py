# -*- coding: utf-8 -*-
"""開診格網（PGY/Clerk 排班的前提資料，設計文件 §3.7）。

由「門診輪值表週模板」（clinic_template）展開成當月每個工作日各時段的**跟診
診間清單**（房號升冪）。治療室/切片室為獨立房間，不在此格網（solve_day 另處理）。

規則（定案）：
- 只含週一~週五、非國定假日（假日休診全空）。
- 週三下午 **跟診診間關閉**（回 []）；但治療室照開（由 solve_day 處理）。
- `is_self_paid` 的診（自費/美容）不算診間、不進格網。
- 月度覆寫（grid_overrides）：某日某時段可 closed_rooms / added_rooms。
"""
from __future__ import annotations

from datetime import date

from cmuh_common.roster.model import STUDENT_SESSIONS, is_weekend, month_dates

WED = 2   # 週三（weekday 0=一）


def _template_rooms(entries: list) -> list:
    """模板某(weekday,session)的 entries → 跟診房號清單（排除自費、升冪去重）。"""
    rooms = {e.get("room") for e in (entries or [])
             if e.get("room") and not e.get("is_self_paid")}
    return sorted(rooms)


def month_grid(ym: str, template: dict, holidays: set,
               overrides: "dict | None" = None) -> dict:
    """回 {date: {session: [room,...]}}，只含週一~五非假日的跟診診間。"""
    overrides = overrides or {}
    y, m = int(ym[:4]), int(ym[5:7])
    grid: dict = {}
    for d in month_dates(y, m):
        if is_weekend(d) or d in holidays:
            continue
        wd = str(d.weekday())
        ov = overrides.get(d.isoformat()) or {}
        day: dict = {}
        for session in STUDENT_SESSIONS:            # 上午 / 下午
            if d.weekday() == WED and session == "下午":
                day[session] = []                   # 週三下午跟診關閉（治療室另開）
                continue
            rooms = list(_template_rooms((template.get(wd) or {}).get(session)))
            sov = ov.get(session) or {}
            for r in (sov.get("closed_rooms") or []):
                if r in rooms:
                    rooms.remove(r)
            for r in (sov.get("added_rooms") or []):
                if r not in rooms:
                    rooms.append(r)
            day[session] = sorted(rooms)
        grid[d] = day
    return grid


def is_session_open(grid: dict, d: date, session: str) -> bool:
    """該日該時段是否有跟診診間開診。"""
    return bool((grid.get(d) or {}).get(session))
