# -*- coding: utf-8 -*-
"""門診預約資料合併工具 — main.py / scheduler.py 共用。

【重構 2026-05-21】6 個 byte-identical 純函式抽出來：
  - 院區判定 / 排序：_appt_dict_ext_branch、_calendar_branch_sort_rank
  - 過濾／規範：_strip_ext_appointments、_normalize_dayoff_session
  - 合併：_merge_appointments_by_date、_merge_dayoff_overrides

全部純函式（不依賴 class state、不碰 IO / network），可直接 import 共用。
"""
from __future__ import annotations

from datetime import date

from typing import Optional


def appointment_data_count(data) -> int:
    """Count appointment records in a cached doctor data payload."""
    if not isinstance(data, dict) or "error" in data:
        return 0
    total = 0
    for rows in data.values():
        if isinstance(rows, list):
            total += len(rows)
    return total


def _appt_dict_ext_branch(item) -> Optional[str]:
    """掛號 dict 的院區：None=主院, 'east'=東區, 'auh'=亞大, 'huihe'=惠和,
    'huisheng'=惠盛, 'tcmc'=老人醫院（僅 is_ext 之舊資料視為東區）。"""
    if not isinstance(item, dict):
        return None
    eb = item.get("ext_branch")
    if eb in ("east", "auh", "huihe", "huisheng", "tcmc"):
        return eb
    if item.get("is_ext"):
        return "east"
    return None


def _calendar_branch_sort_rank(ext_branch) -> int:
    """總覽同一時段內分院列順序：東區→亞大→惠和→惠盛→老人醫院→其他分院。"""
    if not ext_branch:
        return 0
    return {"east": 0, "auh": 1, "huihe": 2, "huisheng": 3,
            "tcmc": 4}.get(ext_branch, 5)


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


# ── reg52 schBox 解析（P2-06 第三刀 2026-07-31 從 main.py 搬入）──────────────
def split_schbox_by_date(cell):
    """把一格 schBox 依 visitDate 切開,回 (格首共用文字, {id(visitDate div): 該日期自己的文字})。

    [2026-07-26 審查 ★止掛提醒★] 同一格常列出好幾個日期(同一診每週重複),而「止掛」是
    【單一日期】的狀態 —— reg52 把它寫在該日期後面的 div。原本 `"止掛" in cell_content`
    用【整格】文字判斷,只要其中一天止掛,同格其他日期全部被標成 is_stopped;止掛提醒掃描
    看到 is_stopped 就 `continue`(已止掛不必再提醒)→ 那些日期的提醒信被靜默吃掉。
    格首(第一個 visitDate 之前)的文字是整格共用的標題(診間號碼等),仍套用到所有日期
    —— 若整格都停,「止掛」會寫在這裡,不能漏掉。
    結構不符(visitDate 不是本格直接子節點)時回空 dict,呼叫端退回整格文字=維持既有行為。
    """
    header_parts = []
    groups = {}
    current = None
    for child in cell.children:
        get_attr = getattr(child, "get", None)
        classes = child.get("class") or [] if get_attr is not None else []
        text = (child.get_text(strip=True)
                if hasattr(child, "get_text") else str(child).strip())
        if "visitDate" in classes:
            current = id(child)
            groups[current] = [text]
        elif current is not None:
            if text:
                groups[current].append(text)
        elif text:
            header_parts.append(text)
    return "".join(header_parts), {k: "".join(v) for k, v in groups.items()}


def reg52_docno_for_dayoff_table(doc_no):
    """reg52.cgi 的 table#dayoff 僅出現在 DocNo=D12345；純數字 DocNo 回傳的 HTML 不含休診表。"""
    s = str(doc_no).strip()
    if s.upper().startswith("D"):
        return s
    return f"D{s}"


# ── 東區休診推論索引（第三刀）────────────────────────────────────────────────
# [perf r5] 取代月曆重繪時每格×每醫師×每時段重掃整份 all_doctors_data
# (最壞 ~396 次/重繪 × 整月掃)的 _doctor_has_other_ext_on_weekday。
# 每次 refresh 只全掃一次建索引，per-cell 查詢降為 O(1)。抽成純函式以便單元測試對拍
# 等價性(見 tests/test_east_clinic_index.py 對 _doctor_has_other_ext_on_weekday 差分測試)。
def build_east_weekday_index(all_doctors_data, parse_item):
    """建 (lookup_key, weekday, session) -> set(有東區的日期)。parse_item(item) ->
    (session_name, ext_branch)，同時處理 dict 與舊式 str。語意對齊原方法：isinstance(date)
    過濾、僅收 east、session 非空。"""
    index: dict = {}
    for lk, data in all_doctors_data.items():
        if not isinstance(data, dict) or 'error' in data:
            continue
        for dkey, items in data.items():
            if not isinstance(dkey, date):
                continue
            wd = dkey.weekday()
            for item in items:
                sn, ext = parse_item(item)
                if ext == "east" and sn:
                    index.setdefault((lk, wd, sn), set()).add(dkey)
    return index


def east_index_has_other(index, doc_no, doc_name, weekday_idx, session_name,
                         exclude_date):
    """索引版查詢：是否有「其他(非 exclude_date)同 weekday」出現東區該診別。
    doc_no/doc_name 兩鍵聯集，與 _doctor_has_other_ext_on_weekday 等價。"""
    for lk in (doc_no, doc_name):
        dates = index.get((lk, weekday_idx, session_name))
        if dates and any(d != exclude_date for d in dates):
            return True
    return False
