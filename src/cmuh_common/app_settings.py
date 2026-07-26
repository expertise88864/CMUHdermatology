# -*- coding: utf-8 -*-
"""Application settings loaders shared by the main app and scheduler."""
from __future__ import annotations

import logging
import os
import time
from datetime import date

from cmuh_common.atomic_io import atomic_write_json
from cmuh_common.config_io import (
    clone_default,
    load_json_dict,
    load_json_dict_ex,
    load_json_list_ex,
    normalize_doctor_rows,
)
from cmuh_common.paths import get_conf_path
from cmuh_common.threshold_policy import DEFAULT_THRESHOLDS

# [2026-07-26 審查 ★設定被覆蓋★] 本次執行中「暫時讀不到」(檔案仍在,只是被防毒/備份鎖住)
# 的設定檔名。載入失敗時這些 loader 會回【預設值】—— 若呼叫端拿那份預設值去存檔,
# 使用者的門檻/止掛提醒收件人/醫師清單就永久消失,而且過程完全沒有徵兆。
# 寫入端(main.save_all_settings)必須先查這裡,有紀錄就拒絕存檔。
_LOAD_FAILED_FILES: set = set()


def settings_load_failed() -> set:
    """本次執行中曾經「暫時讀不到」的設定檔名(空 set = 都正常)。"""
    return set(_LOAD_FAILED_FILES)


def _note_load_status(filename: str, status: str) -> None:
    """只記錄 "error"(原檔完好、暫時讀不到)。missing/corrupt 不記 ——
    那兩種情況磁碟上本來就沒有可用內容,用預設值存檔是合理的修復。"""
    if status == "error":
        if filename not in _LOAD_FAILED_FILES:
            logging.error(
                "設定檔 %s 暫時讀不到(檔案仍在,可能被防毒/備份鎖住)→ 本次執行改用預設值;"
                "在重新讀到之前【不會】允許存檔,以免把您的設定覆蓋成預設", filename)
        _LOAD_FAILED_FILES.add(filename)
    else:
        _LOAD_FAILED_FILES.discard(filename)

# [使用者定案] R1-R3 值班對照姓名(僅供依姓名比對院方值班表 fetch_duty_doctor;name-only,
# 無 doc_no/公務機 欄位)。住院醫師升年:2026-08-01 起更替。
# [codex] 設【生效日閘門】—— 舊組保留到 7/31,8/1(含)起才換新組。否則無存檔的機器(新裝/
# 刪檔)在 7 月就會把現任 R 顯示成下一年的階級(值班對照靠姓名比對,直接影響顯示)。
R_DOCTOR_TRANSITION_DATE = date(2026, 8, 1)
_R_DOCTOR_SETTINGS_BEFORE = {
    "R1": {"name": "林于喬"},
    "R2": {"name": "陳翊嘉"},
    "R3": {"name": "蔡明洋"},
}
_R_DOCTOR_SETTINGS_FROM_2026_08_01 = {
    "R1": {"name": "賴奕彰"},
    "R2": {"name": "林于喬"},
    "R3": {"name": "陳翊嘉"},
}


def default_r_doctor_settings(today: date | None = None) -> dict:
    """依生效日回傳 R1-R3 值班對照預設姓名:2026-08-01(含)起用新組,之前用舊組。"""
    today = today or date.today()
    return (_R_DOCTOR_SETTINGS_FROM_2026_08_01
            if today >= R_DOCTOR_TRANSITION_DATE else _R_DOCTOR_SETTINGS_BEFORE)


# 向後相容常數(import 當下凍結)。呼叫端要【當下】正確值請用 default_r_doctor_settings()。
DEFAULT_R_DOCTOR_SETTINGS = default_r_doctor_settings()

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
    # [使用者定案 2026-07-20] 新增門診人數查詢預設醫師
    {"name": "蔡明洋", "doc_no": "D34257", "notifications": False},
    {"name": "陳翊嘉", "doc_no": "101358", "notifications": False},
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


def load_r_doctor_settings(path: str | None = None,
                           today: date | None = None) -> dict:
    """Load R1-R3 doctor name mappings with trimmed names.
    預設值依生效日決定(見 default_r_doctor_settings);已存檔者以檔案為準。"""
    defaults = default_r_doctor_settings(today)
    data, _st = load_json_dict_ex(_path(path, "r_doctor_settings.json"), defaults)
    _note_load_status("r_doctor_settings.json", _st)
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
    data, _st = load_json_dict_ex(_path(path, "threshold_settings.json"), defaults)
    _note_load_status("threshold_settings.json", _st)
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
    data, _st = load_json_list_ex(target, defaults)
    _note_load_status("doctors.json", _st)
    normalized, fixed = normalize_doctor_rows(data, defaults)
    # [2026-07-26 審查] 讀不到就【絕不】寫回:這條「正規化後順手修檔」的路徑在
    # 暫時讀取失敗時拿到的是預設清單,寫下去等於把使用者的醫師清單清成預設 ——
    # 而且不需要使用者做任何事,光是啟動就會發生。
    if fixed and _st == "error":
        logging.error("doctors.json 暫時讀不到 → 跳過正規化寫回(避免把醫師清單覆蓋成預設)")
        fixed = False
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
