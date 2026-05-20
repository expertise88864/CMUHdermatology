# -*- coding: utf-8 -*-
"""門診預約資料合併工具 — main.py / scheduler.py 共用。

【重構 2026-05-21】6 個 byte-identical 純函式抽出來：
  - 院區判定 / 排序：_appt_dict_ext_branch、_calendar_branch_sort_rank
  - 過濾／規範：_strip_ext_appointments、_normalize_dayoff_session
  - 合併：_merge_appointments_by_date、_merge_dayoff_overrides

全部純函式（不依賴 class state、不碰 IO / network），可直接 import 共用。
"""
from __future__ import annotations

from typing import Optional


def _appt_dict_ext_branch(item) -> Optional[str]:
    """掛號 dict 的院區：None=主院, 'east'=東區, 'auh'=亞大, 'huihe'=惠和, 'huisheng'=惠盛
    （僅 is_ext 之舊資料視為東區）。"""
    if not isinstance(item, dict):
        return None
    eb = item.get("ext_branch")
    if eb in ("east", "auh", "huihe", "huisheng"):
        return eb
    if item.get("is_ext"):
        return "east"
    return None


def _calendar_branch_sort_rank(ext_branch) -> int:
    """總覽同一時段內分院列順序：東區→亞大→惠和→惠盛→其他分院。"""
    if not ext_branch:
        return 0
    return {"east": 0, "auh": 1, "huihe": 2, "huisheng": 3}.get(ext_branch, 4)


def _strip_ext_appointments(appointments_by_date: dict) -> None:
    """移除主院週表中內嵌之東區列（改以東區主機資料為準）；
    惠和僅來自 wh1，不在此處剔除。in-place 修改 appointments_by_date。"""
    for date_key in list(appointments_by_date.keys()):
        bucket = appointments_by_date[date_key]
        appointments_by_date[date_key] = [
            x for x in bucket
            if not (isinstance(x, dict) and _appt_dict_ext_branch(x) == "east")
        ]


def _normalize_dayoff_session(cell_text) -> Optional[str]:
    """DoctorInfo 停診表「診別」欄常見變體 → 上午/下午/晚上。無法辨識則回傳 None。"""
    if not cell_text:
        return None
    t = cell_text.replace(" ", "").replace("　", "")
    if "上午" in t or "早診" in t or t.upper() == "AM":
        return "上午"
    if "下午" in t or "午診" in t or t.upper() == "PM":
        return "下午"
    if "晚上" in t or "晚診" in t or "夜診" in t or "夜間" in t:
        return "晚上"
    return None


def _merge_appointments_by_date(base_data: dict, incoming_data: dict) -> None:
    """把 incoming_data 內各日期的 records 合併進 base_data，去重。in-place。"""
    for date_key, records in incoming_data.items():
        bucket = base_data.setdefault(date_key, [])
        existing_keys = {
            (
                item.get('session'),
                item.get('room'),
                item.get('count'),
                _appt_dict_ext_branch(item),
                item.get('is_stopped'),
            )
            for item in bucket
            if isinstance(item, dict)
        }
        for record in records:
            record_key = (
                record.get('session'),
                record.get('room'),
                record.get('count'),
                _appt_dict_ext_branch(record),
                record.get('is_stopped'),
            )
            if record_key not in existing_keys:
                bucket.append(record)
                existing_keys.add(record_key)


def _merge_dayoff_overrides(base_data: dict, dayoff_data: dict) -> None:
    """停診列僅覆寫「相同診別且相同院區(主院/東區/惠和/惠盛)」的掛號資料。in-place。"""
    valid_sessions = {"上午", "下午", "晚上"}
    for date_key, records in dayoff_data.items():
        bucket = list(base_data.get(date_key, []))
        for record in records:
            session_name = record.get('session')
            if session_name not in valid_sessions:
                continue
            rec_br = _appt_dict_ext_branch(record)
            bucket = [
                item for item in bucket
                if not (
                    isinstance(item, dict)
                    and item.get('session') == session_name
                    and _appt_dict_ext_branch(item) == rec_br
                )
            ]
            bucket.append(record)
        base_data[date_key] = bucket
