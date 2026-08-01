# -*- coding: utf-8 -*-
"""診間燈號歷史／診間設定的**存取層**（誰負責讀寫哪個 JSON 檔）。
（P2-06 分層第五刀(a) 2026-08-05，從 `AutomationApp` 搬出）

【和 `clinic_light_history` 的分工】
`clinic_light_history` 是**純函式**：給它一份 dict，它算平均、加樣本，不碰磁碟。
這裡是它的**存取層**：知道檔案叫 `clinic_light_history.json`、怎麼原子寫回、
保留幾天。原本這兩件事一起塞在 `AutomationApp` 的 method 裡，於是
「檔名對不對、寫回失敗會不會炸掉診間流程」這一半永遠沒被測到。

【★寫回失敗不可以打斷臨床流程★】
燈號歷史只是統計用的輔助資料。寫不出去（磁碟滿、權限、防毒鎖檔）時只能吞掉，
**絕不能**讓看診中的畫面更新整條掛掉 —— 這是搬家前就有的行為，照樣保留，
只是現在寫成明確的契約而不是一個裸露的 `except: pass`。
"""
from __future__ import annotations

import logging
from datetime import datetime

from cmuh_common.atomic_io import atomic_write_json
from cmuh_common.clinic_light_history import (
    historical_light_average,
    record_light_sample,
)
from cmuh_common.config_io import load_json_dict
from cmuh_common.reg64_utils import canonical_clinic_session_str
from cmuh_common.paths import get_conf_path

LIGHT_HISTORY_FILENAME = "clinic_light_history.json"
CLINIC_SETTINGS_FILENAME = "clinic_settings.json"


def save_light_sample(room_code, doc_name, session_cn, light_val, *,
                      now: datetime | None = None,
                      history_days: int) -> bool:
    """將目前燈號記錄到歷史檔案（每3分鐘一個時間桶）。→ 有沒有真的寫入。

    回傳值是新的：原本無論寫成功與否都是 `None`，呼叫端無從知道。
    """
    now = now or datetime.now()
    file_path = get_conf_path(LIGHT_HISTORY_FILENAME)
    data = load_json_dict(file_path, {}, merge_defaults=False)
    data, changed = record_light_sample(
        data,
        room_code=room_code,
        doc_name=doc_name,
        session_key=canonical_clinic_session_str(session_cn),
        light_val=light_val,
        when=now,
        retain_days=max(60, history_days + 7),
    )
    if not changed:
        return False
    try:
        # [perf r5] clinic_light_history 是純機器讀寫的大型快取(~220KB，每次門診輪詢
        # 每診間寫一次)，沒人會手看。改 compact(indent=None + 無空白分隔)可把 json.dump
        # 從 ~6ms 降到 ~1ms、檔案砍近半，fsync 位元組數也減半。讀端 safe_load_json 與
        # 格式無關，round-trip 完全等價。其餘小型人讀設定檔維持預設 indent=4。
        atomic_write_json(file_path, data, indent=None, separators=(",", ":"))
    except Exception:
        # ★吞掉是刻意的★ 見模組 docstring：統計資料寫不出去不可以打斷看診畫面。
        logging.debug("[診間燈號] 歷史寫回失敗（不影響看診流程）", exc_info=True)
        return False
    return True


def hist_avg_light(room_code, doc_name, session_cn, *,
                   now: datetime | None = None,
                   history_days: int, window_minutes: int):
    """回傳近月同時刻門診進度均值；優先取同星期幾，樣本不足時退回全月。

    ★沒有可用資料時回的是 em dash `"—"`★ 那個字元會直接顯示在門診動態表格上，
    換成別的破折號（`－`／`-`）就是改了畫面。搬家不可以動它。
    """
    if not room_code or not doc_name:
        return "—"
    now = now or datetime.now()
    data = load_json_dict(get_conf_path(LIGHT_HISTORY_FILENAME), {},
                          merge_defaults=False)
    return historical_light_average(
        data,
        room_code=room_code,
        doc_name=doc_name,
        session_key=canonical_clinic_session_str(session_cn),
        when=now,
        history_days=history_days,
        window_minutes=window_minutes,
    )


def load_clinic_settings(default_rooms, room_count: int,
                         normalize_rooms) -> dict:
    """讀診間設定；診間代碼被正規化過就順手寫回去。

    `normalize_rooms` 由呼叫端注入，避免這一層反過來相依 UI 那邊的規則。
    """
    default_settings = {"rooms": list(default_rooms),
                        "time_modes": ["auto"] * room_count}
    file_path = get_conf_path(CLINIC_SETTINGS_FILENAME)
    settings = load_json_dict(file_path, default_settings)
    rooms, changed = normalize_rooms(settings.get("rooms"))
    settings["rooms"] = rooms
    if changed:
        try:
            atomic_write_json(file_path, settings)
            logging.info("門診動態診間設定已遷移為: %s", rooms)
        except Exception:
            logging.warning("門診動態診間設定遷移寫回失敗", exc_info=True)
    return settings
