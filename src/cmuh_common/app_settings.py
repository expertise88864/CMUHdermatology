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

# [2026-07-26 審查 ★設定被覆蓋★] 本次執行中「暫時讀不到」(檔案仍在,只是被防毒/備份鎖住)
# 的設定檔名。載入失敗時這些 loader 會回【預設值】—— 若呼叫端拿那份預設值去存檔,
# 使用者的門檻/止掛提醒收件人/醫師清單就永久消失,而且過程完全沒有徵兆。
# 寫入端(main.save_all_settings)必須先查這裡,有紀錄就拒絕存檔。
_LOAD_FAILED_FILES: set = set()


def settings_load_failed() -> set:
    """本次執行中曾經「暫時讀不到」的設定檔名(空 set = 都正常)。"""
    return set(_LOAD_FAILED_FILES)


def clear_load_failed(filename: str) -> None:
    """把某個設定檔標記為「已無讀取失敗疑慮」。

    [2026-07-27] 只有「還原預設」該呼叫:那條路徑是使用者【明確要求】把該檔覆蓋成
    預設,而且已先備份原檔 —— 拒絕存檔的守衛(防的是無意間覆蓋)在此已無意義,
    再留著只會讓使用者重置完卻永遠不能按儲存。**成功寫入之後**才可呼叫。
    """
    if filename in _LOAD_FAILED_FILES:
        _LOAD_FAILED_FILES.discard(filename)
        logging.info("設定檔 %s 已還原為預設 → 解除本次執行的「拒絕存檔」保護", filename)


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
    # ★[使用者定案 2026-08-03] 補上漏掉的 R4★
    #   升年是每個人往上一階：林于喬 R1→R2、陳翊嘉 R2→R3、蔡明洋 R3→【R4】，
    #   賴奕彰是新的 R1。原本這組只寫到 R3 —— 蔡明洋就此從值班姓名對照裡
    #   整個消失，8/1 起他的值班在院方值班表上比對不到。
    "R4": {"name": "蔡明洋"},
}

# ★名單修訂版號★（2026-08-03 使用者定案：直接複寫每台電腦上的舊存檔）
#   `r_doctor_settings.json` 一旦存在就會蓋過預設值，所以光改預設值救不了
#   已經存過檔的機器 —— 它們會繼續顯示漏掉 R4 的舊名單。
#   存檔裡的版號小於這個數字（或根本沒有）就以【預設名單】為準；
#   使用者之後在設定頁改過並儲存，存檔就會帶上新版號而重新被尊重。
R_DOCTOR_ROSTER_REVISION = 2
_ROSTER_REVISION_KEY = "_roster_revision"


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
    try:
        saved_revision = int(data.get(_ROSTER_REVISION_KEY, 0))
    except (TypeError, ValueError):
        saved_revision = 0
    if saved_revision < R_DOCTOR_ROSTER_REVISION:
        # ★存檔版本比程式舊 → 以預設名單為準（使用者定案：直接複寫）★
        #   不在這裡寫檔：載入不該有副作用，而且「拒絕存檔」保護正是為了
        #   避免讀到一半的狀態被寫回去。使用者按一次儲存就會帶上新版號。
        logging.info("R 醫師名單存檔版本較舊(%s < %s) → 本次以程式內建名單為準",
                     saved_revision, R_DOCTOR_ROSTER_REVISION)
        return out
    for key in out:
        if isinstance(data.get(key), dict):
            out[key] = {"name": str(data[key].get("name", "")).strip()}
    return out


def stamp_r_doctor_revision(mapping: dict) -> dict:
    """存檔前蓋上名單版號 —— 之後這份存檔才會被尊重。"""
    out = dict(mapping)
    out[_ROSTER_REVISION_KEY] = R_DOCTOR_ROSTER_REVISION
    return out


def load_threshold_settings(
    path: str | None = None,
    default_thresholds: dict | None = None,
    *,
    dnd_start_hour: int = DEFAULT_NOTIFY_DND_START_HOUR,
    dnd_end_hour: int = DEFAULT_NOTIFY_DND_END_HOUR,
) -> dict:
    """Load threshold settings and fill legacy notification defaults."""
    # [2026-07-27] 預設值改由 settings_defaults 統一宣告(門檻 + 收件人 + F8 + 介面),
    # 這樣「新增一個設定鍵」只要動那一份 dict,載入/還原預設/摘要三件事自動涵蓋。
    # 呼叫端仍可用 default_thresholds 覆寫(測試用)。
    from cmuh_common.settings_defaults import default_threshold_settings
    defaults = dict(default_threshold_settings())
    if default_thresholds:
        defaults.update(default_thresholds)
    # ★順序很重要★ 下面那幾條「舊格式推導」的條件都是 `if key not in data`。
    # 若先把預設合進來,每個鍵都會存在 → 推導全部失效,而舊機器的檔案往往【只有】
    # notify_dnd_*_hour(沒有 *_time),它們的勿擾時段就會被悄悄換成預設值。
    # 故:先拿【原始檔案內容】做推導,最後才用預設補齊缺的鍵。
    data, _st = load_json_dict_ex(_path(path, "threshold_settings.json"), None)
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
    out = defaults
    out.update(data)
    return out


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
