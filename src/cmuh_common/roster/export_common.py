# -*- coding: utf-8 -*-
"""匯出共用純函式（供 export_xlsx / export_docx）：統計、檔名、請假摘要。"""
from __future__ import annotations

from cmuh_common.roster.model import day_point, is_weekend, roc

WD_CN = "一二三四五六日"                       # 週一..週日


def member_tally(block: dict, holidays: set, params) -> dict:
    """{member_id: {"wd":平日班,"we":假日班,"pt":點數}}。block 為 build_export 的
    scope 區塊（含 members / duty）。"""
    out = {mid: {"wd": 0, "we": 0, "pt": 0} for mid in block["members"]}
    for d, p in block["duty"].items():
        if p not in out:
            continue
        t = out[p]
        if is_weekend(d):
            t["we"] += 1
        else:
            t["wd"] += 1
        t["pt"] += day_point(d, holidays, params)
    return out


def leaves_summary(block: dict) -> str:
    """請假摘要字串：'甲(7/3、7/10)　乙(7/20)'（無則空字串）。"""
    parts = []
    for mid, ds in block["leaves"].items():
        if not ds:
            continue
        name = block["names"].get(mid, mid)
        dd = "、".join(f"{d.month}/{d.day}" for d in ds)
        parts.append(f"{name}({dd})")
    return "　".join(parts)


def default_filename(data: dict, ext: str) -> str:
    """民國年檔名，如 '115年07月班表.xlsx'。ext 含點（'.xlsx'/'.docx'）。"""
    return f"{roc(data['year'])}年{data['month']:02d}月班表{ext}"


def title_text(data: dict) -> str:
    return f"中國醫藥大學附設醫院 皮膚部　{roc(data['year'])}年{data['month']:02d}月 值班表"
