# -*- coding: utf-8 -*-
"""Settings JSON helpers.

Small wrapper around atomic_io so large entry scripts do not each reimplement
the same "load JSON, validate type, merge defaults" pattern.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

from cmuh_common.atomic_io import safe_load_json_ex


def clone_default(default: Any) -> Any:
    """Return an independent copy of a default config object."""
    return copy.deepcopy(default)


def load_json_dict_ex(path: str, default: dict | None = None, *,
                      merge_defaults: bool = True) -> tuple:
    """同 load_json_dict,但額外回傳載入狀態:(dict, status)。

    status 沿用 safe_load_json_ex 契約:ok / missing / corrupt / error。
    [2026-07-26 審查 ★設定被覆蓋★] 需要它的原因:`load_json_dict` 對「暫時讀不到」
    (防毒/備份鎖檔)與「檔案不存在」都回【預設值】。開機那一刻若剛好被鎖住,
    門檻、止掛提醒收件人、F8 文字全部退回預設【載入到記憶體】;使用者之後在設定頁按一次
    「儲存」,就把那份預設值原子性地寫回磁碟 —— 真正的設定永久消失,而且過程完全沒有徵兆。
    呼叫端拿到 "error" 應該拒絕用這份資料去覆寫原檔(見 app_settings.settings_load_failed)。
    """
    base = clone_default(default or {})
    data, status = safe_load_json_ex(path, default=None)
    if not isinstance(data, dict):
        return base, status
    if not merge_defaults:
        return data, status
    base.update(data)
    return base, status


def load_json_dict(path: str, default: dict | None = None, *,
                   merge_defaults: bool = True) -> dict:
    """Load a JSON object from path, falling back to default.

    Corrupt JSON is handled by safe_load_json, which backs up the bad file.
    If merge_defaults is true, missing top-level keys are filled from default.
    需要分辨「讀不到」與「沒有資料」請改用 load_json_dict_ex。
    """
    return load_json_dict_ex(path, default, merge_defaults=merge_defaults)[0]


def load_json_list_ex(path: str, default: list | None = None) -> tuple:
    """同 load_json_list,但額外回傳載入狀態:(list, status)。理由同 load_json_dict_ex ——
    「暫時讀不到」與「沒有資料」都回預設值時,呼叫端會拿預設值去覆寫使用者的真實設定。"""
    data, status = safe_load_json_ex(path, default=None)
    if isinstance(data, list):
        return data, status
    if data is not None:
        logging.warning("[config_io] %s 不是 list，改用預設值", path)
    return clone_default(default or []), status


def load_json_list(path: str, default: list | None = None) -> list:
    """Load a JSON list from path, falling back to a copied default list.
    需要分辨「讀不到」與「沒有資料」請改用 load_json_list_ex。"""
    return load_json_list_ex(path, default)[0]


def normalize_doctor_rows(rows: list, default: list | None = None) -> tuple[list, bool]:
    """Normalize doctor settings rows.

    Returns (rows, changed). Repairs the historical name/doc_no swap where
    doc_no contains Chinese text or name looks like a D-number.
    """
    normalized = []
    changed = False
    for row in rows:
        if not isinstance(row, dict):
            changed = True
            continue
        name = str(row.get('name', '')).strip()
        doc_no = str(row.get('doc_no', '')).strip()
        if (any('\u4e00' <= char <= '\u9fff' for char in doc_no)) or \
           (name.startswith('D') and name[1:].isdigit()):
            logging.warning(
                "Data corruption detected for %s/%s. Swapping back.",
                name, doc_no)
            name, doc_no = doc_no, name
            changed = True
        fixed = dict(row)
        if fixed.get('name') != name or fixed.get('doc_no') != doc_no:
            changed = True
        fixed['name'] = name
        fixed['doc_no'] = doc_no
        normalized.append(fixed)
    if normalized:
        return normalized, changed
    return clone_default(default or []), True
