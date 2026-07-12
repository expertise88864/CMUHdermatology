# -*- coding: utf-8 -*-
"""Application settings loaders shared by the main app and scheduler."""
from __future__ import annotations

import logging
import os
import time

from cmuh_common.atomic_io import atomic_write_json
from cmuh_common.config_io import (
    clone_default,
    load_json_dict,
    load_json_list,
    normalize_doctor_rows,
)
from cmuh_common.paths import get_conf_path
from cmuh_common.threshold_policy import DEFAULT_THRESHOLDS

DEFAULT_R_DOCTOR_SETTINGS = {
    "R1": {"name": "林于喬"},
    "R2": {"name": "陳翊嘉"},
    "R3": {"name": "蔡明洋"},
}

DEFAULT_DOCTOR_SETTINGS = [
    {"name": "張廖年峰", "doc_no": "D15728", "notifications": True},
    {"name": "吳伯元", "doc_no": "D15645", "notifications": False},
    {"name": "陳駿升", "doc_no": "D34899", "notifications": False},
    {"name": "沈冠宇", "doc_no": "D28592", "notifications": False},
    {"name": "許致榮", "doc_no": "D20191", "notifications": False},
    {"name": "謝佳陵", "doc_no": "101823", "notifications": False},
    {"name": "方心禹", "doc_no": "D14355", "notifications": False},
    {"name": "黃建仁", "doc_no": "D6175", "notifications": False},
    {"name": "邵湘德", "doc_no": "D30915", "notifications": False},
    {"name": "李威儒", "doc_no": "D35819", "notifications": False},
    {"name": "蔡李澄", "doc_no": "D31352", "notifications": False},
]

DEFAULT_AUTO_REBOOT_SETTINGS = {"enabled": False, "time": "07:01"}
DEFAULT_NOTIFY_DND_START_HOUR = 0
DEFAULT_NOTIFY_DND_END_HOUR = 8


def _path(path: str | None, filename: str) -> str:
    return path if path is not None else get_conf_path(filename)


def _legacy_hour_to_hhmm(value: object, fallback_hour: int) -> str:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        hour = fallback_hour
    hour = max(0, min(24, hour))
    return f"{hour:02d}:00"


def load_r_doctor_settings(path: str | None = None) -> dict:
    """Load R1-R3 doctor name mappings with trimmed names."""
    defaults = DEFAULT_R_DOCTOR_SETTINGS
    data = load_json_dict(_path(path, "r_doctor_settings.json"), defaults)
    out = clone_default(defaults)
    for key in out:
        if isinstance(data.get(key), dict):
            out[key] = {"name": str(data[key].get("name", "")).strip()}
    return out


def load_threshold_settings(
    path: str | None = None,
    default_thresholds: dict | None = None,
    *,
    dnd_start_hour: int = DEFAULT_NOTIFY_DND_START_HOUR,
    dnd_end_hour: int = DEFAULT_NOTIFY_DND_END_HOUR,
) -> dict:
    """Load threshold settings and fill legacy notification defaults."""
    defaults = default_thresholds or DEFAULT_THRESHOLDS
    data = load_json_dict(_path(path, "threshold_settings.json"), defaults)
    if "ui_font_scale" not in data:
        data["ui_font_scale"] = 1.0
    if "notify_dnd_start_hour" not in data:
        data["notify_dnd_start_hour"] = dnd_start_hour
    if "notify_dnd_end_hour" not in data:
        data["notify_dnd_end_hour"] = dnd_end_hour
    if "notify_dnd_start_time" not in data:
        data["notify_dnd_start_time"] = _legacy_hour_to_hhmm(
            data.get("notify_dnd_start_hour", dnd_start_hour),
            dnd_start_hour,
        )
    if "notify_dnd_end_time" not in data:
        data["notify_dnd_end_time"] = _legacy_hour_to_hhmm(
            data.get("notify_dnd_end_hour", dnd_end_hour),
            dnd_end_hour,
        )
    return data


def load_doctors_settings(path: str | None = None) -> list:
    """Load doctor rows and repair historical swapped name/doc_no values."""
    target = _path(path, "doctors.json")
    defaults = DEFAULT_DOCTOR_SETTINGS
    data = load_json_list(target, defaults)
    normalized, fixed = normalize_doctor_rows(data, defaults)
    if fixed:
        # [IE-11 2026-07-12] 若正規化結果退回預設(原檔形狀全錯被整個丟棄)且原檔確有異於預設的
        # 內容 → 覆寫前先備份成 .invalid-<ts>,免 OneDrive 還原的舊格式檔被靜默清空無法救回。
        if normalized == defaults and data != defaults:
            try:
                # [codex 2026-07-12] 備份名含 PID,避免同秒兩 process/session 產生同名 .invalid-<ts>
                # 而第二個覆寫掉第一個的原檔備份;且不覆寫既有備份。
                ts = time.strftime("%Y%m%d_%H%M%S")
                dest = f"{target}.invalid-{ts}-{os.getpid()}"
                if os.path.exists(target) and not os.path.exists(dest):
                    os.replace(target, dest)
            except OSError:
                logging.debug("[doctors] 備份 .invalid 失敗", exc_info=True)
        atomic_write_json(target, normalized)
    return normalized


def load_auto_reboot_settings(path: str | None = None) -> dict:
    """Load auto reboot settings."""
    return load_json_dict(
        _path(path, "auto_reboot_settings.json"),
        DEFAULT_AUTO_REBOOT_SETTINGS,
    )
